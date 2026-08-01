"""interp/store.py round-trip tests. The theses/thesis_reviews paths are
exercised end-to-end by test_thesis.py; this file covers store.py's own
concerns (ensure_schema idempotency, and the interpretations/job_runs tables
that ST2/ST3 will write through this same module later)."""
from __future__ import annotations

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
