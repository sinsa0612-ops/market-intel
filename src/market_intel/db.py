"""PIT SQLite storage (spec A3/A4). Append-only is enforced by DB triggers,
not application convention."""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

INLINE_LIMIT = 256 * 1024  # 256KB

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source_published_at TEXT,
    safe_source_url TEXT,
    content_sha256 TEXT NOT NULL,
    payload_path TEXT,
    payload_inline TEXT,
    fetch_status TEXT,
    UNIQUE(provider, external_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS fact_revisions (
    fact_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL CHECK(revision_no > 0),
    snapshot_id TEXT REFERENCES raw_snapshots(snapshot_id),
    observed_at TEXT,
    event_at TEXT,
    known_at TEXT NOT NULL,
    market TEXT,
    country TEXT,
    subject TEXT NOT NULL,
    category TEXT,
    metric TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    unit TEXT,
    comparison_basis TEXT NOT NULL DEFAULT '',
    publisher TEXT,
    safe_source_url TEXT,
    data_status TEXT CHECK(data_status IN ('source_verified','reconstructed','partial','unverified')),
    correction_reason TEXT,
    supersedes_revision INTEGER,
    session_label TEXT,
    extra_json TEXT,
    PRIMARY KEY(fact_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_fact_revisions_subject_metric ON fact_revisions(subject, metric);
CREATE INDEX IF NOT EXISTS idx_fact_revisions_known_at ON fact_revisions(known_at);

CREATE TABLE IF NOT EXISTS collect_runs (
    run_id TEXT PRIMARY KEY,
    workflow TEXT,
    cutoff_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS provider_runs (
    provider_run_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES collect_runs(run_id),
    provider TEXT,
    started_at TEXT,
    finished_at TEXT,
    item_count INTEGER,
    status TEXT,
    reason_code TEXT,
    safe_detail TEXT
);

-- Stage-2 tables: created now (empty), populated in a later run.
CREATE TABLE IF NOT EXISTS data_gaps (
    gap_id TEXT PRIMARY KEY,
    subject TEXT,
    metric TEXT,
    detected_at TEXT,
    reason TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS label_revisions (
    label_id TEXT PRIMARY KEY,
    fact_id TEXT,
    old_label TEXT,
    new_label TEXT,
    changed_at TEXT,
    reason TEXT
);

CREATE TRIGGER IF NOT EXISTS trg_fact_revisions_no_update
BEFORE UPDATE ON fact_revisions
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_fact_revisions_no_delete
BEFORE DELETE ON fact_revisions
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_raw_snapshots_no_update
BEFORE UPDATE ON raw_snapshots
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_raw_snapshots_no_delete
BEFORE DELETE ON raw_snapshots
BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- 2B: Thesis 계층 (명세 §3.1 ③). 5테마 × 슬롯 1~3 = 최대 15행이 스키마로 강제된다.
CREATE TABLE IF NOT EXISTS theses (
    thesis_id TEXT PRIMARY KEY,
    theme TEXT NOT NULL CHECK(theme IN
        ('ai_semi','power_energy','fin_credit','consumer_cycle','policy_geo')),
    slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 3),
    statement TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    leading_indicators TEXT NOT NULL,
    next_check_date TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    -- **그 가설 하나의 지문**(문장 + 조건). `source_sha256`은 파일 전체 해시라
    -- 다른 가설을 고쳐도 같이 변한다 — "이 가설의 기준이 바뀌었나"에는 답하지
    -- 못한다. 판정 기록에 이 값을 함께 찍어, 나중에 "가설이 맞아서 강화"와
    -- "기준을 낮춰서 강화"를 구별한다.
    rules_sha256 TEXT NOT NULL DEFAULT '',
    UNIQUE(theme, slot)
);

-- 2B: Review 계층 (명세 §3.1 ④). append-only.
CREATE TABLE IF NOT EXISTS thesis_reviews (
    review_id TEXT PRIMARY KEY,
    thesis_id TEXT NOT NULL,
    report_type TEXT NOT NULL,
    report_date TEXT NOT NULL,
    cutoff_utc TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('강화','유지','약화','무효','판정 불가')),
    prev_verdict TEXT,
    changed INTEGER NOT NULL CHECK(changed IN (0,1)),
    atoms_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    error_type TEXT,                     -- 명세 §10.3 오류 분해. 엔진은 항상 NULL로 둔다(사람 판단). 뒤 단계용 예약.
    engine_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- 이 판정을 만든 **가설 판(版)의 지문**과, 직전 판정 때와 달라졌는지.
    -- 가설은 살아 있는 문서라 목표치가 바뀐다(CEO 2026-08-04: "다시 재설정할 수
    -- 있잖아, 목표치 같은걸"). 그런데 기준을 바꾸면 그 전후 판정은 서로 비교할
    -- 수 없는 것이 된다 — 기록에 판을 안 남기면 원장이 "8/10 강화 · 9/10 강화"만
    -- 보여주고, 그 사이에 골대가 움직였다는 사실이 사라진다. 그러면 가설 검증이
    -- 아니라 자기합리화가 된다.
    rules_sha256 TEXT NOT NULL DEFAULT '',
    rules_changed INTEGER NOT NULL DEFAULT 0 CHECK(rules_changed IN (0,1)),
    UNIQUE(thesis_id, report_type, report_date, cutoff_utc)
);

-- 2B: 해석 생성 이력(질의용 미러. 정본은 리포트 JSON의 meta). append-only.
CREATE TABLE IF NOT EXISTS interpretations (
    interpretation_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    report_date TEXT NOT NULL,
    cutoff_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('ok','partial','llm_unavailable','llm_timeout','bad_output','validation_failed','disabled')),
    model TEXT, prompt_version TEXT, prompt_sha256 TEXT,
    fields_json TEXT NOT NULL,
    violations_json TEXT,
    evidence_json TEXT,
    attempts INTEGER, elapsed_ms INTEGER,
    engine_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interpretations_report
    ON interpretations(report_type, report_date, cutoff_utc);

-- 2B: 운영 상태 (최종 검수 F3).
CREATE TABLE IF NOT EXISTS job_runs (
    job_run_id TEXT PRIMARY KEY,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    lock TEXT,
    steps_json TEXT,
    catchup_generated INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('running','ok','partial','fail')),
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job, started_at);

CREATE TRIGGER IF NOT EXISTS trg_thesis_reviews_no_update
BEFORE UPDATE ON thesis_reviews BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_thesis_reviews_no_delete
BEFORE DELETE ON thesis_reviews BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_interpretations_no_update
BEFORE UPDATE ON interpretations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_interpretations_no_delete
BEFORE DELETE ON interpretations BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

# Columns fact_revisions may be filtered on from facts_as_of(**filters).
# Deliberately not f-stringing arbitrary caller input into SQL.
_FILTERABLE_COLUMNS = {
    "subject",
    "metric",
    "market",
    "country",
    "category",
    "comparison_basis",
    "data_status",
    "session_label",
}

# `comparison_basis` names a *different fact*, not a later vintage of the same
# one. `engine._fact_id` is 제공자:종목:항목:날짜 — it has no period length — so
# "MSFT FY2026 FCF" and "MSFT FY26 4분기 FCF" both land on
# `sec_edgar:MSFT:free_cash_flow:20260630` and become revisions of each other.
# When that happens the later `known_at` wins, and a cumulative number takes the
# quarter's place (실측 2026-08-03: 라이브가 8/1에 넣은 1년치 66,987,000,000이
# 백필이 계산한 분기값 19,639,000,000을 가렸다 — 11건, MSFT·GOOGL·EQIX).
# So when a caller pins the basis, "latest revision" must mean latest *within
# that basis*. Only this column is propagated into the barrier's subquery:
# the others are either already encoded in the fact_id (subject/metric) or are
# genuinely per-revision attributes (data_status) where masking is correct.
_IDENTITY_FILTERS = ("comparison_basis",)


def iso_utc(dt: datetime | str | None = None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if isinstance(dt, str):
        # Parse and re-normalize instead of passing the string straight
        # through: facts_as_of compares known_at against this string
        # lexicographically, so an un-normalized offset (e.g. +09:00)
        # sorted next to UTC known_at values silently breaks the cutoff
        # (repair.md finding #2).
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Concurrent collect runs (e.g. overlapping cron) hit this same file;
    # without a busy timeout a second writer fails immediately instead of
    # waiting the first one out (repair.md finding #3).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# 이미 만들어진 DB에 뒤늦게 붙는 칼럼들. `CREATE TABLE IF NOT EXISTS`는 기존
# 테이블을 손대지 않으므로, 스키마에 칼럼을 적어도 **운영 DB에는 안 생긴다** —
# 새로 만든 DB에서만 통과하는 테스트가 되고 운영에서만 깨진다.
#
# `ADD COLUMN`만 쓴다: 값 있는 칼럼을 지우거나 이름을 바꾸는 마이그레이션은
# 이 원장의 append-only 성격과 맞지 않는다(트리거가 UPDATE/DELETE를 막는다).
# `NOT NULL`은 기본값과 함께여야 기존 행에 적용된다.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("theses", "rules_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("thesis_reviews", "rules_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("thesis_reviews", "rules_changed", "INTEGER NOT NULL DEFAULT 0"),
    # 해석 재사용 판단용 사실 지문(`interp.digest.facts_fingerprint`).
    # 빈 문자열은 "지문을 안 남기던 시절의 판" = 재사용 대상 아님이다.
    ("interpretations", "facts_sha256", "TEXT NOT NULL DEFAULT ''"),
    # 해석 **본문**. 지금까지는 칸별 상태(ok/blank)만 남기고 글 자체는 리포트
    # JSON에만 있었다 — 그래서 리포트를 다시 만들면 글이 사라지고 되살릴
    # 방법이 없었다(CEO 지적 2026-08-05). 원장에 남겨 복원 가능하게 한다.
    ("interpretations", "text_json", "TEXT NOT NULL DEFAULT ''"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """-> 실제로 추가한 `표.칼럼` 목록. 이미 있으면 아무것도 하지 않는다."""
    added = []
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # 그 표가 아직 없다 — SCHEMA가 방금 만들었거나 이 DB엔 없다
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    return added


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        conn.commit()
    finally:
        conn.close()


def insert_raw_snapshot(conn: sqlite3.Connection, raw_dir: str, provider: str, item) -> str:
    """Idempotent: same (provider, external_id, content) returns the
    existing snapshot_id instead of inserting a duplicate.

    The existence check and the insert are done as a single atomic
    INSERT OR IGNORE followed by a SELECT of whichever row won, instead of
    a separate SELECT-then-INSERT: two collect runs racing on the same DB
    file previously hit a bare UNIQUE-constraint IntegrityError between the
    two statements, which errored the whole provider batch even though the
    row was a legitimate duplicate (repair.md finding #3)."""
    payload = item.payload
    payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
    content_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    snapshot_id = f"{provider}:{item.external_id}:{content_sha256[:16]}"

    payload_inline = None
    payload_path = None
    if len(payload_bytes) <= INLINE_LIMIT:
        payload_inline = payload if isinstance(payload, str) else payload_bytes.decode("utf-8", errors="replace")
    else:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        out_dir = Path(raw_dir) / provider / day
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{snapshot_id}.json.gz"
        with gzip.open(out_path, "wb") as f:
            f.write(payload_bytes)
        payload_path = str(out_path)

    conn.execute(
        """INSERT OR IGNORE INTO raw_snapshots
           (snapshot_id, provider, external_id, fetched_at, source_published_at,
            safe_source_url, content_sha256, payload_path, payload_inline, fetch_status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            snapshot_id, provider, item.external_id, iso_utc(), item.source_published_at,
            item.safe_source_url, content_sha256, payload_path, payload_inline, item.fetch_status,
        ),
    )
    existing = conn.execute(
        "SELECT snapshot_id FROM raw_snapshots WHERE provider=? AND external_id=? AND content_sha256=?",
        (provider, item.external_id, content_sha256),
    ).fetchone()
    return existing["snapshot_id"]


def upsert_fact(conn: sqlite3.Connection, fact_id: str, snapshot_id: str | None, known_at: str, fc) -> bool:
    """Append a new revision iff the value actually changed. Returns True if
    a new revision row was appended, False on no-op (spec A4).

    "Current revision" means the **latest known_at**, not the highest
    revision_no (spec 백필 §4 S1). revision_no is an append counter, not a
    time axis: once a backfill appends older vintages behind a live
    revision, the highest revision_no is an *older* value, and comparing
    against it makes every subsequent live collect append a spurious
    revision (backfill spec 반증 4)."""
    latest = conn.execute(
        "SELECT * FROM fact_revisions WHERE fact_id=? "
        "ORDER BY known_at DESC, revision_no DESC LIMIT 1",
        (fact_id,),
    ).fetchone()

    if latest is not None:
        same = (
            latest["value_num"] == fc.value_num
            and (latest["value_text"] or None) == (fc.value_text or None)
            and latest["data_status"] == fc.data_status
            and (latest["unit"] or "") == (fc.unit or "")
        )
        if same:
            return False
        # revision_no stays a monotonic append counter (PRIMARY KEY), so it
        # comes from MAX(revision_no) — which is *not* necessarily `latest`.
        revision_no = conn.execute(
            "SELECT MAX(revision_no) m FROM fact_revisions WHERE fact_id=?", (fact_id,)
        ).fetchone()["m"] + 1
        supersedes = latest["revision_no"]
    else:
        revision_no = 1
        supersedes = None

    conn.execute(
        """INSERT INTO fact_revisions
           (fact_id, revision_no, snapshot_id, observed_at, event_at, known_at, market, country,
            subject, category, metric, value_num, value_text, unit, comparison_basis, publisher,
            safe_source_url, data_status, correction_reason, supersedes_revision, session_label, extra_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fact_id, revision_no, snapshot_id, iso_utc(), fc.event_at, known_at, fc.market, fc.country,
            fc.subject, fc.category, fc.metric, fc.value_num, fc.value_text, fc.unit, fc.comparison_basis,
            fc.publisher, getattr(fc, "safe_source_url", "") or None, fc.data_status,
            None, supersedes, fc.session_label,
            json.dumps(fc.extra, ensure_ascii=False) if fc.extra else None,
        ),
    )
    return True


def facts_as_of(conn: sqlite3.Connection, cutoff: datetime | str, **filters) -> list[sqlite3.Row]:
    """Point-in-time read: for each fact_id, the latest revision with
    known_at <= cutoff. Revisions known after cutoff are never returned.

    "Latest" is ordered by known_at (ties broken by revision_no), not by
    revision_no alone (spec 백필 §4 S1). A backfill appends older vintages
    *after* the live revision, so the highest revision_no can be an older
    value — selecting it would let a backfilled past value mask today's
    value (backfill spec 반증 2). The known_at <= cutoff filter stays on
    both sides of the comparison: the barrier must never see a revision
    that was not knowable at the cutoff.

    Pinning `comparison_basis` also narrows what counts as "latest" — see
    `_IDENTITY_FILTERS`. `facts_as_of(cutoff, subject="MSFT",
    metric="free_cash_flow", comparison_basis="quarterly")` returns the newest
    *quarterly* revision per period, instead of dropping periods whose newest
    revision happens to be an annual cumulative."""
    cutoff_str = iso_utc(cutoff)
    clauses = ["known_at <= ?"]
    params: list = [cutoff_str]
    for k, v in filters.items():
        if k not in _FILTERABLE_COLUMNS:
            raise ValueError(f"facts_as_of: unfilterable column {k!r}")
        clauses.append(f"{k} = ?")
        params.append(v)
    inner = ["o.fact_id = fr.fact_id", "o.known_at <= ?"]
    inner_params: list = [cutoff_str]
    for k in _IDENTITY_FILTERS:
        if k in filters:
            inner.append(f"o.{k} = ?")
            inner_params.append(filters[k])
    where = " AND ".join(clauses)
    query = f"""
        SELECT fr.* FROM fact_revisions fr
        WHERE {where}
          AND NOT EXISTS (
            SELECT 1 FROM fact_revisions o
            WHERE {" AND ".join(inner)}
              AND (o.known_at > fr.known_at
                   OR (o.known_at = fr.known_at AND o.revision_no > fr.revision_no))
          )
    """
    return conn.execute(query, [*params, *inner_params]).fetchall()
