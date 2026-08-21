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
from datetime import datetime

from .. import db as db_mod
from ..reporting.cutoff import KST

# 이 상수는 더 이상 판정 엔진의 의미 버전을 대표하지 않는다 — 그 자리는
# `thesis.ENGINE_VERSION`이다(명세 §2 "지문/엔진버전 함정의 해법": 배선이
# 틀려 있었다. 이 컬럼이 "판정 엔진"을 버전한다면서 이 모듈의 상수를 찍고
# 있었고, 판정 의미를 실제로 결정하는 `thesis.ENGINE_VERSION`은 아무도 읽지
# 않았다). `record_reviews`는 이제 호출자가 행에 담아 넘긴 `engine_version`을
# 쓴다(`store`가 `thesis`를 import하면 순환이 생긴다 — `thesis.py`가 이미
# `store`를 지연 import하는 이유와 같다). 이 상수는 그 값을 안 넘기는 호출자
# (테스트 등)를 위한 폴백 기본값으로만 남는다. **과거 208개 운영 행이 이
# 값으로 찍혔으므로 바꾸지 않는다** — 바꾸면 그 뜻이 소급 재해석된다.
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


def last_engine_version(conn: sqlite3.Connection, thesis_id: str) -> str | None:
    """직전 판정을 만든 판정 엔진의 의미 버전. 없으면 None(= 첫 판정).
    `last_rules_sha256`의 복제(명세 §2 구현 3) — 같은 이유로 같은 동점 처리
    (`created_at` DESC, `rowid` DESC)를 쓴다."""
    row = conn.execute(
        "SELECT engine_version FROM thesis_reviews WHERE thesis_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (thesis_id,),
    ).fetchone()
    return row["engine_version"] if row else None


def engine_introduced_on(conn: sqlite3.Connection, engine_version: str) -> str | None:
    """§2 구현 4: 도입일은 상수가 아니라 원장에서 뽑는다 — 그 엔진 버전을 가진
    행 중 가장 이른 `created_at`을 KST 날짜로 변환한 값. `report_date`가 아니라
    `created_at`을 쓰는 이유: 캐치업이 과거 `report_date`로 리포트를 만들 수
    있어, `report_date`를 쓰면 도입일이 실제보다 앞당겨진다. 그 엔진 버전이
    원장에 아직 없으면(첫 실행) None. 예전에는 호출자가 `report_date`로
    대체했지만 그 대체는 final-review F4로 제거됐다 — 값을 지어내지 않는
    게 원칙이므로, 없으면 없는 대로 두고 그 None을 어떻게 다룰지는 호출자
    (`ops.thesis_display_introduced_on`)의 몫이다."""
    row = conn.execute(
        "SELECT MIN(created_at) m FROM thesis_reviews WHERE engine_version=?",
        (engine_version,),
    ).fetchone()
    if not row or not row["m"]:
        return None
    return datetime.fromisoformat(row["m"]).astimezone(KST).strftime("%Y-%m-%d")


# --- thesis_reviews (append-only) -------------------------------------------

def record_reviews(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Persists `thesis.review()`'s output rows. Each row must carry
    thesis_id/report_type/report_date/cutoff_utc/verdict/prev_verdict/changed/
    atoms_json/evidence_json — everything `thesis.review()` already computed.

    `engine_version`/`engine_changed` are read from the row (`thesis.review()`
    stamps both — spec §2 구현 1/3), falling back to this module's own
    `ENGINE_VERSION`/0 for callers that don't supply them (existing tests
    that build rows by hand, e.g. `test_ops.py`)."""
    created_at = db_mod.iso_utc()
    n = 0
    for r in rows:
        review_id = f"revw_{uuid.uuid4().hex[:20]}"
        conn.execute(
            "INSERT INTO thesis_reviews(review_id, thesis_id, report_type, report_date, cutoff_utc, "
            "verdict, prev_verdict, changed, atoms_json, evidence_json, error_type, engine_version, created_at, "
            "rules_sha256, rules_changed, engine_changed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review_id, r["thesis_id"], r["report_type"], r["report_date"], r["cutoff_utc"],
                r["verdict"], r["prev_verdict"], r["changed"], r["atoms_json"], r["evidence_json"],
                None, r.get("engine_version", ENGINE_VERSION), created_at,
                r.get("rules_sha256", ""), r.get("rules_changed", 0), r.get("engine_changed", 0),
            ),
        )
        n += 1
    conn.commit()
    return n


def reviews_for_thesis(conn: sqlite3.Connection, thesis_id: str) -> list[dict]:
    """Read path for the transition-rules derived view (`interp.transitions`,
    rules-doc R0/R1): every `thesis_reviews` row for one thesis, in R1's
    order — `cutoff_utc` ascending, `rowid` ascending as the tie-break.
    `thesis_reviews` is append-only (db.py's triggers), so `rowid` is a
    permanent record of insertion order, not just today's snapshot.

    Read-only, and the only 2B accessor `transitions.py` uses — SA-1 keeps
    every raw SQL statement against the 2B tables inside this file."""
    rows = conn.execute(
        "SELECT report_date, atoms_json, rules_changed, engine_version, engine_changed, verdict "
        "FROM thesis_reviews WHERE thesis_id=? ORDER BY cutoff_utc ASC, rowid ASC",
        (thesis_id,),
    ).fetchall()
    return [
        {
            "report_date": r["report_date"],
            "atoms_json": json.loads(r["atoms_json"]),
            "rules_changed": bool(r["rules_changed"]),
            "engine_version": r["engine_version"],
            "engine_changed": bool(r["engine_changed"]),
            "verdict": r["verdict"],
        }
        for r in rows
    ]


# --- interpretations (append-only mirror; written by ST2's cli_interpret) ---

def record_interpretation(conn: sqlite3.Connection, row: dict) -> str:
    interpretation_id = f"intp_{uuid.uuid4().hex[:20]}"
    conn.execute(
        "INSERT INTO interpretations(interpretation_id, report_type, report_date, cutoff_utc, status, "
        "model, prompt_version, prompt_sha256, fields_json, violations_json, evidence_json, "
        "attempts, elapsed_ms, engine_version, created_at, facts_sha256, text_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            interpretation_id, row["report_type"], row["report_date"], row["cutoff_utc"], row["status"],
            row.get("model"), row.get("prompt_version"), row.get("prompt_sha256"),
            json.dumps(row.get("fields", {}), ensure_ascii=False),
            json.dumps(row["violations"], ensure_ascii=False) if row.get("violations") is not None else None,
            json.dumps(row["evidence"], ensure_ascii=False) if row.get("evidence") is not None else None,
            row.get("attempts"), row.get("elapsed_ms"), row.get("engine_version", ENGINE_VERSION),
            db_mod.iso_utc(),
            row.get("facts_sha256", ""),
            # 본문 + 그 해석의 이력을 한 덩어리로. 옛 판은 본문만 들어 있고
            # 읽는 쪽(`reusable_interpretation`)이 두 모양을 모두 받는다.
            json.dumps({"text": row["text"], "meta": row.get("restorable_meta")},
                       ensure_ascii=False) if row.get("text") else "",
        ),
    )
    conn.commit()
    return interpretation_id


# --- 해석 성적표 (CEO 지시 2026-08-20) ---------------------------------------
#
# SQL이 여기에만 있는 이유: `interp/` 아래에서 원시 SQL을 쓸 수 있는 파일은
# `store.py`(2B의 DB 접근 계층)와 `ops.py`뿐이다. 해석 경로가 DB에 닿는 두 번째
# 길을 만들지 않기 위한 규칙이고, 시험이 소스 문자열로 지킨다
# (`test_interp_never_touches_fact_revisions_or_raw_sql`).

def insert_check(conn: sqlite3.Connection, row: dict) -> None:
    """`INSERT OR IGNORE` — 같은 (해석, 조건 id)는 UNIQUE라 재실행해도 안 늘어난다."""
    conn.execute(
        "INSERT OR IGNORE INTO interpretation_checks(check_id, interpretation_id, "
        "report_type, report_date, atom_id, atom_json, basis_json, why, due_date, "
        "model, registered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["check_id"], row["interpretation_id"], row["report_type"], row["report_date"],
         row["atom_id"], row["atom_json"], row["basis_json"], row["why"], row["due_date"],
         row["model"], row["registered_at"]))


def due_checks(conn: sqlite3.Connection, as_of: str) -> list[dict]:
    """만기가 지났는데 아직 채점 안 된 조건들."""
    rows = conn.execute(
        "SELECT * FROM interpretation_checks WHERE scored_at IS NULL AND due_date <= ? "
        "ORDER BY due_date, registered_at", (as_of,)).fetchall()
    return [dict(r) for r in rows]


def mark_check_scored(conn: sqlite3.Connection, check_id: str, scored_at: str,
                      verdict: str, detail_json: str) -> None:
    """**`scored_at IS NULL`이 두 번째 잠금이다.** 1차 방어는 `due_checks`의 같은
    필터라 이 조건은 기능적으로 도달할 수 없지만, 지우면 그쪽이 바뀌는 날 재채점이
    조용히 뚫린다 — 재채점은 곧 골대 이동이다."""
    conn.execute(
        "UPDATE interpretation_checks SET scored_at=?, verdict=?, detail_json=? "
        "WHERE check_id=? AND scored_at IS NULL",
        (scored_at, verdict, detail_json, check_id))


def scored_checks(conn: sqlite3.Connection, report_type: str | None = None) -> list[tuple]:
    """(verdict, atom_json) 목록 — 채점이 끝난 것만."""
    sql = "SELECT verdict, atom_json FROM interpretation_checks WHERE scored_at IS NOT NULL"
    args: list = []
    if report_type:
        sql += " AND report_type=?"
        args.append(report_type)
    return list(conn.execute(sql, args))


def pending_checks(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """아직 채점 안 된 조건의 (개수, 가장 이른 만기).

    성적표가 비어 있는 동안에도 **배관이 살아 있다**는 것을 화면이 말할 수 있게
    한다 — 등록만 되고 만기가 아직 안 온 기간(첫 등록일 +7일)에 성적표가 그냥
    비면, 등록이 안 되는 것과 화면상 구별되지 않는다."""
    row = conn.execute(
        "SELECT COUNT(*), MIN(due_date) FROM interpretation_checks "
        "WHERE scored_at IS NULL").fetchone()
    return (int(row[0]), row[1])


def reusable_interpretation(conn: sqlite3.Connection, report_type: str, report_date: str,
                            cutoff_utc: str, facts_sha256: str) -> dict | None:
    """같은 리포트(종류·날짜·차단선)에 대해 **같은 사실 위에서** 쓰인 해석 중
    가장 최근 것. 없으면 None.

    리포트를 다시 만들 때 해석이 사라지는 것을 막는다(CEO 지적 2026-08-05).
    `facts_sha256`이 맞을 때만 돌려주는 이유: 해석은 "그때 그 사실들"을 보고 쓴
    글이라, 사실이 바뀐 뒤 옛 글을 붙이면 리포트가 거짓말을 한다. 지문은 표시
    형식이 아니라 데이터로 재므로(`digest.facts_fingerprint`), 표기만 손댄
    수정에서는 해석이 그대로 살아남는다.

    `status='ok'`만 대상이다 — 검증에 걸렸거나 LLM이 죽어서 비었던 판을
    되살리면 그때의 실패를 오늘 리포트에 다시 붙이는 셈이다.
    """
    if not facts_sha256:
        return None
    row = conn.execute(
        "SELECT * FROM interpretations WHERE report_type=? AND report_date=? AND cutoff_utc=? "
        "AND facts_sha256=? AND status='ok' AND text_json<>'' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (report_type, report_date, cutoff_utc, facts_sha256),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    stored = json.loads(d.pop("text_json") or "{}")
    # 새 모양은 {"text": …, "meta": …}, 옛 모양은 본문 그대로다.
    if "text" in stored:
        d["text"], d["restorable_meta"] = stored.get("text") or {}, stored.get("meta")
    else:
        d["text"], d["restorable_meta"] = stored, None
    d["fields"] = json.loads(d.pop("fields_json") or "{}")
    d["violations"] = json.loads(d.pop("violations_json")) if d.get("violations_json") else None
    d["evidence"] = json.loads(d.pop("evidence_json")) if d.get("evidence_json") else None
    return d


def previous_interpretation(conn: sqlite3.Connection, report_type: str,
                            before_report_date: str) -> dict | None:
    """**같은 종류**의 직전 리포트에 쓰인 해석. 없으면 None.

    "같은 종류"가 곧 비교 주기다(CEO 지시 2026-08-13) — 일간 리포트는 전날 일간과,
    주간은 지난주 주간과, 월간은 전월 월간과 대조된다. 종류를 섞으면 주간 해석이
    어제 것과 대조돼 "일주일 뒤 어떻게 됐나"라는 질문에 답하지 못한다. 리포트
    주기와 비교 창이 어긋나면 안 된다는 원칙은 2026-08-10에 가격 비교에서 이미
    한 번 겪은 것이다(주간 리포트가 전일대비를 쓰고 있었다).

    `status='ok'`만 본다: 검증에 걸려 비었던 판은 대조할 주장 자체가 없다.
    `evidence_json`이 있는 것만 본다 — 그게 F-번호를 (종목, 지표)로 푸는 유일한
    열쇠이고, 없으면 무엇을 이야기했는지 기계적으로 알 수 없다.
    """
    row = conn.execute(
        "SELECT * FROM interpretations WHERE report_type=? AND report_date<? "
        "AND status='ok' AND text_json<>'' AND evidence_json IS NOT NULL "
        "ORDER BY report_date DESC, created_at DESC, rowid DESC LIMIT 1",
        (report_type, before_report_date),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    stored = json.loads(d.pop("text_json") or "{}")
    d["text"] = stored.get("text") if "text" in stored else stored
    d["fields"] = json.loads(d.pop("fields_json") or "{}")
    d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
    return d


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
