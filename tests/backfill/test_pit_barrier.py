"""ST1 인수 조건 1번 — 정보 차단선(point-in-time barrier) 회귀 (spec §4 S10).

세 층:
  (1) 골든 케이스 — 실제 PAYEMS 개정 이력. 백필 vintage를 라이브 revision *뒤에*
      붙여도 과거 질의가 그 시점 값만 돌려주고(누수 없음), 오늘 질의가 과거 값에
      가려지지 않는다(spec 반증 2 회귀). KST 표기 cutoff와 경계 ±1초 포함.
  (2) 원장 불변식 — 임의의 `fact_revisions` 상태에 대한 전수 검사.
  (3) 소스별 known_at 상한 — 백필된 모든 revision은 `known_at >= event_at`.

(2)와 (3)은 `MI_DB_PATH`가 가리키는 실제 DB 사본에 대해서도 돈다(ST2·ST3의
성공 기준에서 3만 행 백필 DB에 재사용된다). 설정돼 있지 않으면 skip.

주의: 이 파일은 `from conftest import` 를 쓰지 않는다 — 전체 실행 시
형제 폴더의 conftest로 연결된다(과거 사고). 헬퍼는 전부 이 파일 안에 있다.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from market_intel import db as db_mod
from market_intel.backfill.ledger import append_vintage
from market_intel.models import FactCandidate, RawItem

PAYEMS_FACT_ID = "fred:PAYEMS:value:20260201"
LIVE_KNOWN_AT = "2026-08-01T02:35:06+00:00"

# 실제 PAYEMS 2026-02 개정 이력 (BRIEF 실증값): (realtime_start, value)
PAYEMS_VINTAGES = [
    ("2026-03-06T13:30:00+00:00", 158466.0),
    ("2026-04-03T12:30:00+00:00", 158459.0),
    ("2026-05-08T12:30:00+00:00", 158436.0),
]


# --------------------------------------------------------------------------
# 헬퍼 (이 파일 전용 — conftest import 금지)
# --------------------------------------------------------------------------
def _fc(value: float, *, event_at: str = "2026-02-01T00:00:00+00:00",
        data_status: str = "reconstructed", subject: str = "PAYEMS",
        metric: str = "value", unit: str = "Thous. of Persons") -> FactCandidate:
    return FactCandidate(
        raw_ref="r1", subject=subject, category="macro", metric=metric,
        event_at=event_at, market="US", country="US",
        value_num=value, unit=unit, publisher="FRED", data_status=data_status,
    )


def _snapshot(conn, raw_dir: str, provider: str = "fred", external_id: str = "r1") -> str:
    raw = RawItem(
        external_id=external_id, source_published_at="2026-02-01",
        safe_source_url="https://example.test/series", payload="{}",
    )
    return db_mod.insert_raw_snapshot(conn, raw_dir, provider, raw)


def _shift(known_at: str, seconds: int) -> str:
    return db_mod.iso_utc(datetime.fromisoformat(known_at) + timedelta(seconds=seconds))


def _values(rows) -> list[float]:
    return sorted(r["value_num"] for r in rows)


def assert_ledger_invariants(conn, max_cutoffs: int | None = None) -> int:
    """spec S10-(2). 모든 known_at과 그 ±1초, 그리고 양 끝 ±1일을 cutoff로 넣어
    `facts_as_of`가 (a) 누수하지 않고 (b) fact당 정확히 1행을 주고 (c) 고른 판이
    진짜 '그 시점 최신'인지 검사한다.

    기대값은 SQL이 아니라 파이썬에서 독립적으로 계산한다 — 구현과 같은 쿼리로
    검증하면 항진명제가 되어 이빨이 없다.

    -> 실제로 검사한 cutoff 개수."""
    rows = conn.execute("SELECT fact_id, known_at, revision_no FROM fact_revisions").fetchall()
    assert rows, "빈 원장에서는 불변식을 검사할 수 없다"
    ledger = [(r["fact_id"], r["known_at"], r["revision_no"]) for r in rows]

    known = sorted({ka for _, ka, _ in ledger})
    cutoffs = set()
    sample = known
    if max_cutoffs is not None and len(known) > max_cutoffs:
        step = len(known) / max_cutoffs
        sample = [known[int(i * step)] for i in range(max_cutoffs)]
    for ka in sample:
        cutoffs |= {ka, _shift(ka, -1), _shift(ka, 1)}
    cutoffs.add(_shift(known[0], -86400))
    cutoffs.add(_shift(known[-1], 86400))

    for cut in sorted(cutoffs):
        cut_s = db_mod.iso_utc(cut)
        expected: dict[str, tuple[str, int]] = {}
        for fid, ka, rev in ledger:
            if ka <= cut_s and (fid not in expected or (ka, rev) > expected[fid]):
                expected[fid] = (ka, rev)

        got_rows = db_mod.facts_as_of(conn, cut_s)
        got = {r["fact_id"]: (r["known_at"], r["revision_no"]) for r in got_rows}

        assert len(got) == len(got_rows), f"cutoff {cut_s}: fact_id당 1행이어야 한다"
        for r in got_rows:
            assert r["known_at"] <= cut_s, (
                f"cutoff {cut_s}: 차단선 누수 — known_at={r['known_at']} fact={r['fact_id']}"
            )
        assert got == expected, f"cutoff {cut_s}: 선택된 판이 '그 시점 최신'이 아니다"
    return len(cutoffs)


def assert_backfill_known_at_not_before_event(conn) -> int:
    """spec S10-(3). 백필된 revision은 미래에 일어난 일을 그 전에 알 수 없다."""
    rows = conn.execute(
        "SELECT fact_id, revision_no, known_at, event_at FROM fact_revisions "
        "WHERE correction_reason LIKE 'backfill:%' AND event_at IS NOT NULL"
    ).fetchall()
    bad = [dict(r) for r in rows if db_mod.iso_utc(r["known_at"]) < db_mod.iso_utc(r["event_at"])]
    assert not bad, f"known_at < event_at 인 백필 revision: {bad[:5]}"
    return len(rows)


# --------------------------------------------------------------------------
# (1) 골든 케이스
# --------------------------------------------------------------------------
def _seed_payems(tmp_path, live_value: float) -> object:
    db_path = str(tmp_path / "t.db")
    raw_dir = str(tmp_path / "raw")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    snap = _snapshot(conn, raw_dir)

    # 라이브가 먼저 들어와 있다(현재 원장의 실제 상태).
    db_mod.upsert_fact(
        conn, PAYEMS_FACT_ID, snap, LIVE_KNOWN_AT,
        _fc(live_value, data_status="source_verified"),
    )
    # 그 뒤에 백필 vintage가 known_at 역순으로 붙는다.
    for known_at, value in PAYEMS_VINTAGES:
        append_vintage(conn, PAYEMS_FACT_ID, snap, known_at, _fc(value),
                       correction_reason="backfill:macro_us")
    conn.commit()
    return conn


def test_golden_payems_revision_history(tmp_path):
    """cutoff별로 '그때 알 수 있었던 값'만 나온다."""
    conn = _seed_payems(tmp_path, live_value=158436.0)
    try:
        assert db_mod.facts_as_of(conn, "2026-02-15T00:00:00+00:00") == []
        for cutoff, expected in [
            ("2026-03-10T00:00:00+00:00", 158466.0),
            ("2026-04-10T00:00:00+00:00", 158459.0),
            ("2026-06-01T00:00:00+00:00", 158436.0),
        ]:
            rows = db_mod.facts_as_of(conn, cutoff)
            assert len(rows) == 1, cutoff
            assert rows[0]["value_num"] == expected, cutoff
    finally:
        conn.close()


def test_today_value_is_not_masked_by_a_backfilled_past_value(tmp_path):
    """spec 반증 2 회귀. 라이브가 백필 창보다 더 늦은 개정을 이미 잡아둔 경우,
    오늘 질의는 반드시 라이브 값을 준다. `MAX(revision_no)` 기준으로 돌아가면
    백필 마지막 vintage(158436)가 오늘 값(158400)을 가린다."""
    conn = _seed_payems(tmp_path, live_value=158400.0)
    try:
        rows = db_mod.facts_as_of(conn, "2026-08-02T00:00:00+00:00")
        assert len(rows) == 1
        assert rows[0]["value_num"] == 158400.0, "오늘 값이 과거 백필 값에 가려졌다"
        assert rows[0]["data_status"] == "source_verified"
        # 과거 질의는 그대로 그 시점 값이어야 한다(양방향 고정).
        past = db_mod.facts_as_of(conn, "2026-03-10T00:00:00+00:00")
        assert len(past) == 1 and past[0]["value_num"] == 158466.0
    finally:
        conn.close()


def test_cutoff_boundary_is_inclusive_to_the_second(tmp_path):
    """경계 ±1초. known_at 그 순간은 '알 수 있었다', 1초 전은 '아직 아니다'."""
    conn = _seed_payems(tmp_path, live_value=158400.0)
    try:
        first_known, first_value = PAYEMS_VINTAGES[0]
        assert db_mod.facts_as_of(conn, _shift(first_known, -1)) == []
        rows = db_mod.facts_as_of(conn, first_known)
        assert len(rows) == 1 and rows[0]["value_num"] == first_value

        second_known, second_value = PAYEMS_VINTAGES[1]
        rows = db_mod.facts_as_of(conn, _shift(second_known, -1))
        assert rows[0]["value_num"] == first_value
        rows = db_mod.facts_as_of(conn, second_known)
        assert rows[0]["value_num"] == second_value

        # 라이브 known_at 경계도 같다.
        assert db_mod.facts_as_of(conn, _shift(LIVE_KNOWN_AT, -1))[0]["value_num"] == 158436.0
        assert db_mod.facts_as_of(conn, LIVE_KNOWN_AT)[0]["value_num"] == 158400.0
    finally:
        conn.close()


def test_kst_cutoff_notation_resolves_to_the_same_revision(tmp_path):
    """CEO가 보는 리포트 cutoff는 KST 표기다. `+09:00` 문자열이 UTC로
    정규화되지 않으면 사전식 비교에서 차단선이 조용히 깨진다."""
    conn = _seed_payems(tmp_path, live_value=158400.0)
    try:
        # 2026-03-06T13:30:00Z == 2026-03-06T22:30:00+09:00
        assert db_mod.facts_as_of(conn, "2026-03-06T22:29:59+09:00") == []
        rows = db_mod.facts_as_of(conn, "2026-03-06T22:30:00+09:00")
        assert len(rows) == 1 and rows[0]["value_num"] == 158466.0

        kst = db_mod.facts_as_of(conn, "2026-04-10T09:00:00+09:00")
        utc = db_mod.facts_as_of(
            conn, datetime(2026, 4, 10, 0, 0, tzinfo=timezone.utc))
        assert _values(kst) == _values(utc) == [158459.0]
    finally:
        conn.close()


def test_backfill_does_not_rewrite_or_delete_live_revisions(tmp_path):
    """append-only 불변: 라이브 revision 1은 백필 뒤에도 원래 값 그대로 남는다."""
    conn = _seed_payems(tmp_path, live_value=158400.0)
    try:
        rows = conn.execute(
            "SELECT * FROM fact_revisions WHERE fact_id=? ORDER BY revision_no",
            (PAYEMS_FACT_ID,),
        ).fetchall()
        assert len(rows) == 4
        assert rows[0]["revision_no"] == 1
        assert rows[0]["value_num"] == 158400.0
        assert rows[0]["known_at"] == db_mod.iso_utc(LIVE_KNOWN_AT)
        assert rows[0]["correction_reason"] is None
        assert [r["known_at"] for r in rows[1:]] == [
            db_mod.iso_utc(k) for k, _ in PAYEMS_VINTAGES
        ]
        assert all(r["correction_reason"] == "backfill:macro_us" for r in rows[1:])
    finally:
        conn.close()


# --------------------------------------------------------------------------
# (2) 원장 불변식 — 전수 검사
# --------------------------------------------------------------------------
def test_ledger_invariants_on_mixed_live_and_backfill_ledger(tmp_path):
    db_path = str(tmp_path / "t.db")
    raw_dir = str(tmp_path / "raw")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    try:
        snap = _snapshot(conn, raw_dir)
        # 라이브 fact 3개 (하루치, 오늘 known_at)
        for i, sym in enumerate(["MSFT", "NVDA", "AAPL"]):
            fc = _fc(100.0 + i, event_at="2026-07-31T20:00:00+00:00",
                     data_status="source_verified", subject=sym,
                     metric="price_close", unit="USD")
            db_mod.upsert_fact(conn, f"yfinance:{sym}:price_close:20260731",
                               snap, LIVE_KNOWN_AT, fc)
        # 백필 vintage: known_at 역순, 값이 라이브와 같은 것도 섞는다.
        for i, sym in enumerate(["MSFT", "NVDA", "AAPL"]):
            for d, value in [("2026-07-29", 90.0 + i), ("2026-07-30", 100.0 + i),
                             ("2026-07-31", 100.0 + i)]:
                fc = _fc(value, event_at=f"{d}T20:00:00+00:00", subject=sym,
                         metric="price_close", unit="USD")
                append_vintage(conn, f"yfinance:{sym}:price_close:{d.replace('-', '')}",
                               snap, f"{d}T20:00:00+00:00", fc,
                               correction_reason="backfill:prices")
        # 거시: 같은 fact_id에 vintage 4판
        for known_at, value in PAYEMS_VINTAGES:
            append_vintage(conn, PAYEMS_FACT_ID, snap, known_at, _fc(value),
                           correction_reason="backfill:macro_us")
        db_mod.upsert_fact(conn, PAYEMS_FACT_ID, snap, LIVE_KNOWN_AT,
                           _fc(158400.0, data_status="source_verified"))
        conn.commit()

        checked = assert_ledger_invariants(conn)
        assert checked >= 10
        assert assert_backfill_known_at_not_before_event(conn) >= 9
    finally:
        conn.close()


# --------------------------------------------------------------------------
# (2)(3) 실제 DB 사본 재사용 — ST2·ST3 성공 기준에서 같은 파일을 다시 돌린다
# --------------------------------------------------------------------------
@pytest.mark.skipif(not os.environ.get("MI_DB_PATH"), reason="MI_DB_PATH 미설정")
def test_ledger_invariants_on_real_db_copy():
    """`MI_DB_PATH=<사본> pytest tests/backfill/test_pit_barrier.py`.
    읽기 전용 — 이 테스트는 DB에 한 줄도 쓰지 않는다."""
    conn = db_mod.connect(os.environ["MI_DB_PATH"])
    try:
        assert_ledger_invariants(conn, max_cutoffs=24)
        assert_backfill_known_at_not_before_event(conn)
    finally:
        conn.close()
