"""ST1 acceptance test 1: revision append-only, PIT cutoff reads, DB-level
append-only enforcement via triggers."""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from market_intel import db as db_mod
from market_intel.models import FactCandidate, RawItem

FACT_ID = "testprov:MSFT:price_close:20260731"


def _seed(conn, raw_dir):
    raw = RawItem(
        external_id="x1", source_published_at="2026-07-31",
        safe_source_url="https://example.test/x", payload="{}",
    )
    return db_mod.insert_raw_snapshot(conn, raw_dir, "testprov", raw)


def test_pit_regression(tmp_path):
    db_path = str(tmp_path / "t.db")
    raw_dir = str(tmp_path / "raw")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    snap_id = _seed(conn, raw_dir)

    fc1 = FactCandidate(
        raw_ref="x1", subject="MSFT", category="price", metric="price_close",
        event_at="2026-07-31T20:00:00+00:00", market="US", country="US",
        value_num=100.0, unit="USD",
    )
    assert db_mod.upsert_fact(conn, FACT_ID, snap_id, "2026-07-31T21:00:00+00:00", fc1) is True
    conn.commit()

    fc2 = FactCandidate(
        raw_ref="x1", subject="MSFT", category="price", metric="price_close",
        event_at="2026-07-31T20:00:00+00:00", market="US", country="US",
        value_num=200.0, unit="USD",
    )
    assert db_mod.upsert_fact(conn, FACT_ID, snap_id, "2026-07-31T23:00:00+00:00", fc2) is True
    conn.commit()

    rows = conn.execute(
        "SELECT * FROM fact_revisions WHERE fact_id=? ORDER BY revision_no", (FACT_ID,)
    ).fetchall()
    assert len(rows) == 2, "original revision must still exist alongside the new one"
    assert rows[0]["revision_no"] == 1 and rows[0]["value_num"] == 100.0
    assert rows[1]["revision_no"] == 2 and rows[1]["value_num"] == 200.0
    assert rows[1]["supersedes_revision"] == 1

    before = db_mod.facts_as_of(conn, "2026-07-31T22:00:00+00:00", subject="MSFT")
    assert len(before) == 1 and before[0]["value_num"] == 100.0

    after = db_mod.facts_as_of(conn, "2026-08-01T00:00:00+00:00", subject="MSFT")
    assert len(after) == 1 and after[0]["value_num"] == 200.0

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE fact_revisions SET value_num=999 WHERE fact_id=? AND revision_no=1", (FACT_ID,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM fact_revisions WHERE fact_id=?", (FACT_ID,))

    conn.close()


def test_raw_snapshots_also_append_only(tmp_path):
    db_path = str(tmp_path / "t.db")
    raw_dir = str(tmp_path / "raw")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    snap_id = _seed(conn, raw_dir)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE raw_snapshots SET fetch_status='tampered' WHERE snapshot_id=?", (snap_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM raw_snapshots WHERE snapshot_id=?", (snap_id,))
    conn.close()


def test_upsert_is_noop_when_unchanged(tmp_path):
    db_path = str(tmp_path / "t.db")
    raw_dir = str(tmp_path / "raw")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    snap_id = _seed(conn, raw_dir)

    fc = FactCandidate(
        raw_ref="x1", subject="MSFT", category="price", metric="price_close",
        event_at="2026-07-31T20:00:00+00:00", market="US", country="US",
        value_num=100.0, unit="USD",
    )
    assert db_mod.upsert_fact(conn, FACT_ID, snap_id, "2026-07-31T21:00:00+00:00", fc) is True
    conn.commit()
    assert db_mod.upsert_fact(conn, FACT_ID, snap_id, "2026-07-31T22:00:00+00:00", fc) is False
    conn.commit()

    rows = conn.execute("SELECT COUNT(*) c FROM fact_revisions WHERE fact_id=?", (FACT_ID,)).fetchone()
    assert rows["c"] == 1
    conn.close()


def test_facts_as_of_normalizes_non_utc_string_cutoff(tmp_path):
    """A string cutoff with a non-UTC offset must be parsed and normalized
    to UTC before the known_at<=cutoff comparison — otherwise it is
    compared lexicographically against UTC-normalized known_at values and
    the information barrier silently breaks (repair.md finding #2)."""
    db_path = str(tmp_path / "t.db")
    raw_dir = str(tmp_path / "raw")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    snap_id = _seed(conn, raw_dir)

    fc1 = FactCandidate(
        raw_ref="x1", subject="MSFT", category="price", metric="price_close",
        event_at="2026-07-31T20:00:00+00:00", market="US", country="US",
        value_num=100.0, unit="USD",
    )
    db_mod.upsert_fact(conn, FACT_ID, snap_id, "2026-07-31T23:00:00Z", fc1)
    conn.commit()

    fc2 = FactCandidate(
        raw_ref="x1", subject="MSFT", category="price", metric="price_close",
        event_at="2026-07-31T20:00:00+00:00", market="US", country="US",
        value_num=111.0, unit="USD",
    )
    db_mod.upsert_fact(conn, FACT_ID, snap_id, "2026-08-01T05:00:00Z", fc2)
    conn.commit()

    # Same instant, expressed two ways: a tz-aware datetime and an
    # equivalent +09:00 ISO string (== 2026-08-01T00:00:00Z), which sits
    # strictly between the two known_at values above.
    cutoff_dt = datetime(2026, 8, 1, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    by_datetime = db_mod.facts_as_of(conn, cutoff_dt, subject="MSFT")
    by_string = db_mod.facts_as_of(conn, "2026-08-01T09:00:00+09:00", subject="MSFT")

    assert len(by_datetime) == 1 and by_datetime[0]["value_num"] == 100.0
    assert len(by_string) == 1 and by_string[0]["value_num"] == 100.0, (
        "string cutoff must resolve to the same revision as the equivalent datetime cutoff"
    )
    conn.close()


def test_concurrent_insert_raw_snapshot_does_not_raise(tmp_path):
    """Two collect runs racing on the same DB file for an identical
    (provider, external_id, content) snapshot must not raise
    IntegrityError — the old SELECT-then-INSERT pattern let a second
    writer's INSERT collide with the first writer's commit, erroring the
    whole provider batch even though the row was a legitimate duplicate
    (repair.md finding #3)."""
    db_path = str(tmp_path / "t.db")
    raw_dir = str(tmp_path / "raw")
    db_mod.init_db(db_path)

    raw = RawItem(
        external_id="race-1", source_published_at="2026-08-01",
        safe_source_url="https://example.test/race", payload="{}",
    )

    n_workers = 8
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(n_workers)

    def worker():
        conn = db_mod.connect(db_path)
        try:
            barrier.wait()
            snap_id = db_mod.insert_raw_snapshot(conn, raw_dir, "testprov", raw)
            conn.commit()
            results.append(snap_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent insert_raw_snapshot raised: {errors}"
    assert len(set(results)) == 1, "all racing callers must agree on one snapshot_id"

    conn = db_mod.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) c FROM raw_snapshots WHERE provider='testprov' AND external_id='race-1'"
    ).fetchone()["c"]
    conn.close()
    assert count == 1, "exactly one row must exist despite the concurrent race"
