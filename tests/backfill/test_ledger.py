"""ST1 — `backfill.ledger.append_vintage` (spec §4 S5) 회귀.

덮어야 하는 것(spec ST1 과제 본문):
  - 멱등 재실행 no-op
  - vintage 값이 라이브와 같아도 revision이 생긴다 (spec 반증 3)
  - 백필 후 라이브 `upsert_fact` 재호출이 no-op (spec 반증 4)
  - 한 fact_id의 revision이 50을 넘으면 예외
  - 정규화 안 된 오프셋 known_at도 UTC로 정규화된다
  - `supersedes_revision` = 그 known_at 직전에 유효했던 revision
  - append-only 트리거는 그대로 살아 있다

`from conftest import` 금지 — 헬퍼는 이 파일 안에 있다.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from market_intel import db as db_mod
from market_intel.backfill.ledger import MAX_REVISIONS_PER_FACT, append_vintage
from market_intel.models import FactCandidate, RawItem

FACT_ID = "fred:UNRATE:value:20260201"


def _fc(value: float, *, data_status: str = "reconstructed", unit: str = "%",
        value_text: str | None = None, extra: dict | None = None) -> FactCandidate:
    return FactCandidate(
        raw_ref="r1", subject="UNRATE", category="macro", metric="value",
        event_at="2026-02-01T00:00:00+00:00", market="US", country="US",
        value_num=value, value_text=value_text, unit=unit, publisher="FRED",
        data_status=data_status, extra=extra or {},
    )


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "t.db")
    db_mod.init_db(db_path)
    c = db_mod.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def snap(conn, tmp_path) -> str:
    raw = RawItem(
        external_id="r1", source_published_at="2026-02-01",
        safe_source_url="https://example.test/series", payload="{}",
    )
    return db_mod.insert_raw_snapshot(conn, str(tmp_path / "raw"), "fred", raw)


def _rows(conn):
    return conn.execute(
        "SELECT * FROM fact_revisions WHERE fact_id=? ORDER BY revision_no", (FACT_ID,)
    ).fetchall()


def test_append_vintage_writes_the_backfill_discriminators(conn, snap):
    """spec S4: `data_status`만으로는 파생 FCF와 구분되지 않는다.
    `correction_reason='backfill:*'`가 판별자다."""
    assert append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00",
                          _fc(4.2, extra={"backfill": {"known_at_basis": "alfred realtime_start"}}),
                          correction_reason="backfill:macro_us") is True
    conn.commit()
    row = _rows(conn)[0]
    assert row["revision_no"] == 1
    assert row["data_status"] == "reconstructed"
    assert row["correction_reason"] == "backfill:macro_us"
    assert row["supersedes_revision"] is None
    assert '"known_at_basis"' in row["extra_json"]
    assert row["snapshot_id"] == snap


def test_same_vintage_twice_is_a_noop(conn, snap):
    """멱등: 같은 백필을 두 번 → 신규 revision 0."""
    fc = _fc(4.2)
    assert append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00", fc,
                          correction_reason="backfill:macro_us") is True
    conn.commit()
    assert append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00", fc,
                          correction_reason="backfill:macro_us") is False
    conn.commit()
    assert len(_rows(conn)) == 1


def test_same_value_at_a_different_known_at_is_kept(conn, snap):
    """spec 반증 3 회귀. `upsert_fact`는 값이 같으면 소리 없이 버린다 —
    그 시점에 알 수 있었던 사실이 원장에서 사라진다. `append_vintage`는
    known_at이 다르면 반드시 남긴다."""
    fc = _fc(4.2, data_status="source_verified")
    db_mod.upsert_fact(conn, FACT_ID, snap, "2026-08-01T02:35:00+00:00", fc)
    conn.commit()
    # 같은 값, 더 이른 vintage
    assert append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00", _fc(4.2),
                          correction_reason="backfill:macro_us") is True
    conn.commit()

    assert len(_rows(conn)) == 2
    seen = db_mod.facts_as_of(conn, "2026-03-10T00:00:00+00:00", subject="UNRATE")
    assert len(seen) == 1 and seen[0]["value_num"] == 4.2, (
        "그날 알 수 있었던 사실이 원장에서 사라졌다"
    )


def test_two_vintages_with_an_identical_value_are_two_revisions(conn, snap):
    """`known_at`만 다르고 나머지가 전부 같은 두 판도 서로 다른 판이다.

    FRED는 값이 그대로인 개정도 새 vintage로 준다. 둘을 하나로 합치면
    "4월 10일에 알 수 있었던 판이 무엇이었나"가 원장에서 사라진다.
    (변이 검사 M2가 처음에 이 구멍을 찾아냈다 — 원래 테스트는 `data_status`도
    함께 달라서 known_at이 멱등성 키에 있는지 증명하지 못했다.)"""
    fc = _fc(4.2)
    assert append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00", fc,
                          correction_reason="backfill:macro_us") is True
    assert append_vintage(conn, FACT_ID, snap, "2026-04-03T12:30:00+00:00", fc,
                          correction_reason="backfill:macro_us") is True
    conn.commit()

    rows = _rows(conn)
    assert len(rows) == 2, "known_at만 다른 판이 삼켜졌다"
    assert [r["known_at"] for r in rows] == [
        "2026-03-06T13:30:00+00:00", "2026-04-03T12:30:00+00:00"
    ]
    assert db_mod.facts_as_of(conn, "2026-03-10T00:00:00+00:00")[0]["revision_no"] == 1
    assert db_mod.facts_as_of(conn, "2026-04-10T00:00:00+00:00")[0]["revision_no"] == 2


def test_live_upsert_after_backfill_is_a_noop(conn, snap):
    """spec 반증 4 회귀. 백필이 붙은 뒤 라이브 수집이 매 실행마다 revision을
    찍으면 안 된다 — 라이브는 `known_at` 최대 판과 비교해야 한다."""
    live = _fc(4.2, data_status="source_verified")
    db_mod.upsert_fact(conn, FACT_ID, snap, "2026-08-01T02:35:00+00:00", live)
    conn.commit()
    for known_at, value in [("2026-03-06T13:30:00+00:00", 4.1),
                            ("2026-04-03T12:30:00+00:00", 4.15)]:
        append_vintage(conn, FACT_ID, snap, known_at, _fc(value),
                       correction_reason="backfill:macro_us")
    conn.commit()
    before = len(_rows(conn))

    assert db_mod.upsert_fact(conn, FACT_ID, snap, "2026-08-01T10:20:00+00:00", live) is False
    assert db_mod.upsert_fact(conn, FACT_ID, snap, "2026-08-02T02:35:00+00:00", live) is False
    conn.commit()
    assert len(_rows(conn)) == before, "라이브 수집이 매 실행마다 revision을 찍는다"


def test_live_upsert_after_backfill_still_appends_a_real_change(conn, snap):
    """반증 4의 반대편 — 값이 진짜 바뀌면 라이브는 여전히 revision을 찍어야 한다."""
    live = _fc(4.2, data_status="source_verified")
    db_mod.upsert_fact(conn, FACT_ID, snap, "2026-08-01T02:35:00+00:00", live)
    append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00", _fc(4.1),
                   correction_reason="backfill:macro_us")
    conn.commit()

    changed = _fc(4.3, data_status="source_verified")
    assert db_mod.upsert_fact(conn, FACT_ID, snap, "2026-08-02T02:35:00+00:00", changed) is True
    conn.commit()
    rows = _rows(conn)
    assert rows[-1]["value_num"] == 4.3
    # 비교 대상은 revision_no 최대(백필 vintage)가 아니라 known_at 최대(라이브)다.
    assert rows[-1]["supersedes_revision"] == 1


def test_supersedes_points_at_the_revision_effective_just_before(conn, snap):
    for known_at, value in [("2026-03-06T13:30:00+00:00", 4.1),
                            ("2026-05-08T12:30:00+00:00", 4.3),
                            ("2026-04-03T12:30:00+00:00", 4.2)]:
        append_vintage(conn, FACT_ID, snap, known_at, _fc(value),
                       correction_reason="backfill:macro_us")
    conn.commit()
    by_rev = {r["revision_no"]: r for r in _rows(conn)}
    assert by_rev[1]["supersedes_revision"] is None          # 가장 이른 vintage
    assert by_rev[2]["supersedes_revision"] == 1             # 05-08 직전 = 03-06
    assert by_rev[3]["supersedes_revision"] == 1             # 04-03 직전 = 03-06
    assert by_rev[3]["revision_no"] == 3, "revision_no는 append 순번이다"


def test_known_at_is_normalized_to_utc(conn, snap):
    """정규화 안 된 오프셋이 그대로 들어가면 사전식 비교에서 차단선이 깨진다."""
    assert append_vintage(conn, FACT_ID, snap, "2026-03-06T22:30:00+09:00", _fc(4.2),
                          correction_reason="backfill:macro_kr") is True
    conn.commit()
    assert _rows(conn)[0]["known_at"] == "2026-03-06T13:30:00+00:00"

    # 같은 순간을 UTC 표기로 다시 넣으면 중복이 아니라 no-op이어야 한다.
    assert append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00", _fc(4.2),
                          correction_reason="backfill:macro_kr") is False
    conn.commit()
    assert len(_rows(conn)) == 1


def test_value_text_and_unit_and_status_are_part_of_the_idempotency_key(conn, snap):
    known_at = "2026-03-06T13:30:00+00:00"
    append_vintage(conn, FACT_ID, snap, known_at, _fc(4.2),
                   correction_reason="backfill:macro_us")
    conn.commit()
    # data_status만 달라도 서로 다른 판이다.
    assert append_vintage(conn, FACT_ID, snap, known_at, _fc(4.2, data_status="partial"),
                          correction_reason="backfill:macro_us") is True
    # unit만 달라도 다른 판이다.
    assert append_vintage(conn, FACT_ID, snap, known_at, _fc(4.2, unit="percent"),
                          correction_reason="backfill:macro_us") is True
    conn.commit()
    assert len(_rows(conn)) == 3


def test_revision_guard_stops_at_the_cap(conn, snap):
    """spec S6: 조용히 자르지 않고 중단하고 보고한다."""
    day = date(2026, 3, 1)
    for i in range(MAX_REVISIONS_PER_FACT):
        known_at = f"{day + timedelta(days=i)}T00:00:00+00:00"
        assert append_vintage(conn, FACT_ID, snap, known_at,
                              _fc(4.0 + i / 100), correction_reason="backfill:macro_us") is True
    conn.commit()
    assert len(_rows(conn)) == MAX_REVISIONS_PER_FACT

    with pytest.raises(RuntimeError) as exc:
        append_vintage(conn, FACT_ID, snap, "2026-07-01T00:00:00+00:00", _fc(9.9),
                       correction_reason="backfill:macro_us")
    assert FACT_ID in str(exc.value)
    assert str(MAX_REVISIONS_PER_FACT) in str(exc.value)
    conn.rollback()
    assert len(_rows(conn)) == MAX_REVISIONS_PER_FACT


def test_append_vintage_never_updates_or_deletes(conn, snap):
    """append-only 불변 — 트리거가 여전히 막는지 직접 확인한다."""
    append_vintage(conn, FACT_ID, snap, "2026-03-06T13:30:00+00:00", _fc(4.2),
                   correction_reason="backfill:macro_us")
    append_vintage(conn, FACT_ID, snap, "2026-04-03T12:30:00+00:00", _fc(4.3),
                   correction_reason="backfill:macro_us")
    conn.commit()
    first = _rows(conn)[0]
    assert first["value_num"] == 4.2, "기존 revision이 백필로 덮였다"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE fact_revisions SET value_num=9 WHERE fact_id=?", (FACT_ID,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM fact_revisions WHERE fact_id=?", (FACT_ID,))
    conn.rollback()
