"""All 2B DB access (spec SA-1). Nothing outside this module touches the
`theses` / `thesis_reviews` / `interpretations` / `job_runs` tables directly.

`thesis.py` (verdict engine) never runs SQL itself — it reads facts through
`db.facts_as_of` (the information barrier, spec SA-10) and hands finished
rows to this module to persist. That split is what keeps the blackout
guarantee auditable: every raw-SQL write in the 2B layer lives in one file.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from .. import db as db_mod

ENGINE_VERSION = "2b.1"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """SA-2's tables are appended to `db.SCHEMA`, so a plain `init_db` already
    creates them. This wrapper exists so callers in this package never need
    to know that detail — they just call `store.ensure_schema(conn)`."""
    conn.executescript(db_mod.SCHEMA)
    conn.commit()


# --- theses -----------------------------------------------------------------

def rules_fingerprint(statement: str, conditions: dict) -> str:
    """그 가설 하나의 지문 — **문장과 조건만** 넣는다.

    `next_check_date`나 `leading_indicators`는 빼는데, 그것이 바뀌었다고 판정
    기준이 바뀐 것은 아니기 때문이다. 반대로 문장이 바뀌면 조건이 같아도 다른
    가설로 본다 — 같은 조건에 다른 주장을 붙이는 것이 골대를 옮기는 가장 흔한
    방식이다("주가가 오른다" -> "주가가 안 내린다").

    조건은 `sort_keys`로 직렬화한다: 파일에서 키 순서만 바꾼 것을 기준 변경으로
    잘못 세면 매번 '기준 바뀜'이 뜬다."""
    payload = json.dumps({"statement": statement, "conditions": conditions},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replace_theses(conn: sqlite3.Connection, theses: list[dict], source_sha256: str) -> int:
    """Whole-file replace in one transaction: DELETE all rows, INSERT the
    loaded set, COMMIT — or ROLLBACK and re-raise on any failure. Never a
    partial swap (spec SA-2 / ST1 "부분 적재 금지").

    `theses` is `thesis.load_file()`'s output: already-validated dicts with
    keys thesis_id/theme/slot/statement/conditions/leading_indicators/
    next_check_date. This function does not re-validate — `load_file` is the
    only gate."""
    loaded_at = db_mod.iso_utc()
    try:
        conn.execute("DELETE FROM theses")
        for t in theses:
            conditions_json = json.dumps(t["conditions"], ensure_ascii=False)
            conn.execute(
                "INSERT INTO theses(thesis_id, theme, slot, statement, conditions_json, "
                "leading_indicators, next_check_date, source_sha256, loaded_at, rules_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t["thesis_id"], t["theme"], t["slot"], t["statement"],
                    conditions_json,
                    json.dumps(t["leading_indicators"], ensure_ascii=False),
                    t["next_check_date"], source_sha256, loaded_at,
                    rules_fingerprint(t["statement"], t["conditions"]),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(theses)


def list_theses(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM theses ORDER BY theme, slot").fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "thesis_id": r["thesis_id"], "theme": r["theme"], "slot": r["slot"],
                "statement": r["statement"], "conditions": json.loads(r["conditions_json"]),
                "leading_indicators": json.loads(r["leading_indicators"]),
                "next_check_date": r["next_check_date"], "source_sha256": r["source_sha256"],
                "loaded_at": r["loaded_at"], "rules_sha256": r["rules_sha256"],
            }
        )
    return out


def last_verdict(conn: sqlite3.Connection, thesis_id: str) -> str | None:
    row = conn.execute(
        "SELECT verdict FROM thesis_reviews WHERE thesis_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (thesis_id,),
    ).fetchone()
    return row["verdict"] if row else None


def last_rules_sha256(conn: sqlite3.Connection, thesis_id: str) -> str | None:
    """직전 판정을 만든 가설 판의 지문. 없으면 None(= 첫 판정).

    동점 처리를 `review_id`가 아니라 `rowid`로 하는 이유: `created_at`은 초
    단위라 한 번에 여러 판정을 기록하면 같은 값이 되고, `review_id`는 uuid라
    정렬하면 **입력 순서와 무관한 아무 행**이 직전으로 뽑힌다(실측: 같은 초에
    3건을 넣으니 '기준이 바뀐 뒤 첫 판정'이 두 번 떴다). `rowid`는 삽입 순서
    그대로다."""
    row = conn.execute(
        "SELECT rules_sha256 FROM thesis_reviews WHERE thesis_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (thesis_id,),
    ).fetchone()
    return row["rules_sha256"] if row else None


# --- thesis_reviews (append-only) -------------------------------------------

def record_reviews(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Persists `thesis.review()`'s output rows. Each row must carry
    thesis_id/report_type/report_date/cutoff_utc/verdict/prev_verdict/changed/
    atoms_json/evidence_json — everything `thesis.review()` already computed."""
    created_at = db_mod.iso_utc()
    n = 0
    for r in rows:
        review_id = f"revw_{uuid.uuid4().hex[:20]}"
        conn.execute(
            "INSERT INTO thesis_reviews(review_id, thesis_id, report_type, report_date, cutoff_utc, "
            "verdict, prev_verdict, changed, atoms_json, evidence_json, error_type, engine_version, created_at, "
            "rules_sha256, rules_changed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review_id, r["thesis_id"], r["report_type"], r["report_date"], r["cutoff_utc"],
                r["verdict"], r["prev_verdict"], r["changed"], r["atoms_json"], r["evidence_json"],
                None, ENGINE_VERSION, created_at,
                r.get("rules_sha256", ""), r.get("rules_changed", 0),
            ),
        )
        n += 1
    conn.commit()
    return n


# --- interpretations (append-only mirror; written by ST2's cli_interpret) ---

def record_interpretation(conn: sqlite3.Connection, row: dict) -> str:
    interpretation_id = f"intp_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO interpretations(interpretation_id, report_type, report_date, cutoff_utc, status, "
        "model, prompt_version, prompt_sha256, fields_json, violations_json, evidence_json, "
        "attempts, elapsed_ms, engine_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            interpretation_id, row["report_type"], row["report_date"], row["cutoff_utc"], row["status"],
            row.get("model"), row.get("prompt_version"), row.get("prompt_sha256"),
            json.dumps(row.get("fields", {}), ensure_ascii=False),
            json.dumps(row["violations"], ensure_ascii=False) if row.get("violations") is not None else None,
            json.dumps(row["evidence"], ensure_ascii=False) if row.get("evidence") is not None else None,
            row.get("attempts"), row.get("elapsed_ms"), row.get("engine_version", ENGINE_VERSION),
            db_mod.iso_utc(),
        ),
    )
    conn.commit()
    return interpretation_id


def last_interpretation(conn: sqlite3.Connection, report_type: str | None = None) -> dict | None:
    if report_type:
        row = conn.execute(
            "SELECT * FROM interpretations WHERE report_type=? ORDER BY created_at DESC LIMIT 1",
            (report_type,),
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM interpretations ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    d = dict(row)
    d["fields"] = json.loads(d.pop("fields_json") or "{}")
    d["violations"] = json.loads(d.pop("violations_json")) if d.get("violations_json") else None
    d["evidence"] = json.loads(d.pop("evidence_json")) if d.get("evidence_json") else None
    return d


# --- job_runs (operational status; written by ST3's jobs.py) ---------------

def start_job_run(conn: sqlite3.Connection, job: str) -> str:
    job_run_id = f"jobr_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO job_runs(job_run_id, job, started_at, status) VALUES (?,?,?,?)",
        (job_run_id, job, db_mod.iso_utc(), "running"),
    )
    conn.commit()
    return job_run_id


def finish_job_run(conn: sqlite3.Connection, job_run_id: str, steps: dict, status: str,
                    catchup: bool = False, note: str = "") -> None:
    conn.execute(
        "UPDATE job_runs SET finished_at=?, steps_json=?, catchup_generated=?, status=?, note=? "
        "WHERE job_run_id=?",
        (db_mod.iso_utc(), json.dumps(steps, ensure_ascii=False), 1 if catchup else 0, status, note, job_run_id),
    )
    conn.commit()


def last_job_runs(conn: sqlite3.Connection) -> list[dict]:
    """One row per job: its most recent run. Ties on `started_at` (iso_utc is
    second-resolution, so two runs of the same job in one second are
    possible) are broken by sqlite's physical insertion order (`rowid`),
    not by `started_at` alone — a plain MAX(started_at) join would return
    every tied row instead of exactly one per job."""
    rows = conn.execute(
        "SELECT jr.* FROM job_runs jr "
        "WHERE jr.rowid = (SELECT MAX(rowid) FROM job_runs WHERE job = jr.job) "
        "ORDER BY jr.job"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["steps"] = json.loads(d.pop("steps_json")) if d.get("steps_json") else {}
        out.append(d)
    return out
