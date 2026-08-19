"""interp/store.py round-trip tests. The theses/thesis_reviews paths are
exercised end-to-end by test_thesis.py; this file covers store.py's own
concerns (ensure_schema idempotency, and the interpretations/job_runs tables
that ST2/ST3 will write through this same module later)."""
from __future__ import annotations

import sqlite3

from market_intel import db as db_mod
from market_intel.interp import store as store_mod


def test_ensure_schema_is_idempotent_and_additive(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    store_mod.ensure_schema(conn)
    store_mod.ensure_schema(conn)  # must not raise, must not drop anything
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"fact_revisions", "theses", "thesis_reviews", "interpretations", "job_runs"} <= tables


def test_record_and_last_interpretation_round_trip(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    row = {
        "report_type": "morning", "report_date": "2026-08-01", "cutoff_utc": "2026-07-31T23:00:00+00:00",
        "status": "ok", "model": "qwen3.5:9b", "prompt_version": "interpretation_v1", "prompt_sha256": "abc",
        "fields": {"reading": "ok"}, "violations": None, "evidence": [["F1", "x", 1]],
        "attempts": 1, "elapsed_ms": 1000,
    }
    iid = store_mod.record_interpretation(conn, row)
    assert iid

    last = store_mod.last_interpretation(conn, report_type="morning")
    assert last["status"] == "ok"
    assert last["fields"] == {"reading": "ok"}
    assert last["evidence"] == [["F1", "x", 1]]

    assert store_mod.last_interpretation(conn, report_type="weekly_review") is None


def test_interpretations_is_append_only(settings):
    import sqlite3
    import pytest

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    row = {
        "report_type": "morning", "report_date": "2026-08-01", "cutoff_utc": "2026-07-31T23:00:00+00:00",
        "status": "llm_unavailable", "fields": {}, "violations": None, "evidence": None,
    }
    iid = store_mod.record_interpretation(conn, row)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE interpretations SET status='ok' WHERE interpretation_id=?", (iid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM interpretations WHERE interpretation_id=?", (iid,))


def test_job_run_start_finish_and_last_job_runs(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    jr1 = store_mod.start_job_run(conn, "morning")
    store_mod.finish_job_run(conn, jr1, steps={"collect": "ok", "report": "ok"}, status="ok")

    jr2 = store_mod.start_job_run(conn, "morning")
    store_mod.finish_job_run(conn, jr2, steps={"collect": "fail"}, status="fail", note="timeout")

    latest = store_mod.last_job_runs(conn)
    assert len(latest) == 1  # one row per job: the most recent
    assert latest[0]["job_run_id"] == jr2
    assert latest[0]["status"] == "fail"
    assert latest[0]["steps"] == {"collect": "fail"}


# ---------------------------------------------------------------------------
# ST3: engine_changed column + last_engine_version + engine_introduced_on
# (spec §2 "지문/엔진버전 함정의 해법")
# ---------------------------------------------------------------------------

_LEGACY_THESIS_REVIEWS_SCHEMA = """
CREATE TABLE thesis_reviews (
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
    error_type TEXT,
    engine_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL DEFAULT '',
    rules_changed INTEGER NOT NULL DEFAULT 0 CHECK(rules_changed IN (0,1)),
    UNIQUE(thesis_id, report_type, report_date, cutoff_utc)
);
"""


def test_engine_changed_column_is_added_by_migration_and_backfills_zero(settings):
    """실행 가능한 체크(ST3 성공 기준 1번)의 pytest 버전: `engine_changed`
    컬럼이 아직 없는(=ST3 이전) DB에 `init_db`를 다시 돌리면 컬럼이 생기고,
    기존 행은 append-only 트리거 때문에 백필될 수 없으므로 `DEFAULT 0`으로
    채워져야 한다. `db.SCHEMA`의 `CREATE TABLE IF NOT EXISTS`는 이미 있는
    표를 건드리지 않으므로(`db.py:257-` 주석), 새 컬럼이 스키마 문자열에만
    있고 `_ADDED_COLUMNS`에는 없으면 이 테스트가 그것을 잡는다."""
    raw = sqlite3.connect(settings.db_path)
    raw.executescript(_LEGACY_THESIS_REVIEWS_SCHEMA)
    raw.execute(
        "INSERT INTO thesis_reviews(review_id, thesis_id, report_type, report_date, cutoff_utc, "
        "verdict, prev_verdict, changed, atoms_json, evidence_json, engine_version, created_at) "
        "VALUES ('r1','t1','morning','2026-08-01','2026-08-01T00:00:00+00:00','유지',NULL,0,'{}','{}','2b.1','2026-08-01T00:00:00+00:00')"
    )
    raw.commit()
    raw.close()

    db_mod.init_db(settings.db_path)  # the migration under test

    conn = db_mod.connect(settings.db_path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(thesis_reviews)")}
    assert "engine_changed" in columns
    row = conn.execute("SELECT engine_changed FROM thesis_reviews WHERE review_id='r1'").fetchone()
    assert row["engine_changed"] == 0


def test_last_engine_version_is_none_before_any_review_then_tracks_the_latest(settings):
    """`last_rules_sha256`의 복제(§2 구현 3) — 같은 동점 처리를 검증한다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    assert store_mod.last_engine_version(conn, "t1") is None

    store_mod.record_reviews(conn, [{
        "thesis_id": "t1", "report_type": "morning", "report_date": "2026-08-01",
        "cutoff_utc": "2026-08-01T00:00:00+00:00", "verdict": "유지", "prev_verdict": None,
        "changed": 0, "atoms_json": "{}", "evidence_json": "[]", "engine_version": "2b.1",
    }])
    assert store_mod.last_engine_version(conn, "t1") == "2b.1"

    store_mod.record_reviews(conn, [{
        "thesis_id": "t1", "report_type": "morning", "report_date": "2026-08-02",
        "cutoff_utc": "2026-08-02T00:00:00+00:00", "verdict": "유지", "prev_verdict": "유지",
        "changed": 0, "atoms_json": "{}", "evidence_json": "[]", "engine_version": "2b.3",
    }])
    assert store_mod.last_engine_version(conn, "t1") == "2b.3"


def test_engine_introduced_on_reads_the_earliest_created_at_for_that_version_in_kst(settings):
    """§2 구현 4: 도입일은 상수가 아니라 원장에서 뽑는다. `created_at`(UTC)을
    KST 날짜로 변환해야 한다 — 경계 사례: UTC 2026-08-11 15:30 = KST
    2026-08-12 00:30, 하루가 넘어간다. `report_date`가 아니라 `created_at`을
    쓰는 이유도 함께 확인한다: 캐치업으로 과거 `report_date`(2026-07-01)로
    기록된 행이 있어도, 실제로 기록된 시각(`created_at`)이 도입일이다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    assert store_mod.engine_introduced_on(conn, "2b.3") is None  # 아직 원장에 없음

    conn.execute(
        "INSERT INTO thesis_reviews(review_id, thesis_id, report_type, report_date, cutoff_utc, "
        "verdict, prev_verdict, changed, atoms_json, evidence_json, engine_version, created_at) "
        # UTC 경계 사례: 이 created_at은 KST로 넘어가면 08-12다.
        "VALUES ('r1','t1','morning','2026-07-01','2026-08-11T15:00:00+00:00','유지',NULL,0,'{}','{}','2b.3','2026-08-11T15:30:00+00:00')"
    )
    conn.commit()
    assert store_mod.engine_introduced_on(conn, "2b.3") == "2026-08-12"
