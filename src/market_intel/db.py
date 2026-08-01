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
"""

# Columns fact_revisions may be filtered on from facts_as_of(**filters).
# Deliberately not f-stringing arbitrary caller input into SQL.
_FILTERABLE_COLUMNS = {
    "subject",
    "metric",
    "market",
    "country",
    "category",
    "data_status",
    "session_label",
}


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


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
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
    a new revision row was appended, False on no-op (spec A4)."""
    latest = conn.execute(
        "SELECT * FROM fact_revisions WHERE fact_id=? ORDER BY revision_no DESC LIMIT 1",
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
        revision_no = latest["revision_no"] + 1
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
    known_at <= cutoff. Revisions known after cutoff are never returned."""
    cutoff_str = iso_utc(cutoff)
    clauses = ["known_at <= ?"]
    params: list = [cutoff_str]
    for k, v in filters.items():
        if k not in _FILTERABLE_COLUMNS:
            raise ValueError(f"facts_as_of: unfilterable column {k!r}")
        clauses.append(f"{k} = ?")
        params.append(v)
    where = " AND ".join(clauses)
    query = f"""
        SELECT fr.* FROM fact_revisions fr
        INNER JOIN (
            SELECT fact_id, MAX(revision_no) AS max_rev
            FROM fact_revisions
            WHERE {where}
            GROUP BY fact_id
        ) latest ON fr.fact_id = latest.fact_id AND fr.revision_no = latest.max_rev
    """
    return conn.execute(query, params).fetchall()
