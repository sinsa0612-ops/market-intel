"""누적치가 분기 자리를 차지하는 충돌 (실측 2026-08-03).

`engine._fact_id`는 제공자:종목:항목:날짜라 **기간 길이가 없다**. 그래서 SEC가
같은 기간종료일로 내는 "FY 누적"과 백필이 차분해 만든 "그 분기"가 같은 fact_id에
얹히고 서로의 개정본이 된다. 시점 조회는 늦게 알게 된 쪽을 고르므로, 라이브가
8/1에 넣은 1년치가 백필이 7/29 시점으로 넣은 분기값을 가렸다.

실제로 가려진 것: MSFT free_cash_flow 2026-06-30이 19,639,000,000(분기) 대신
66,987,000,000(연간)으로 읽혔고, 그 앞 분기가 15,803,000,000이라 가설 엔진의
"최근 2구간 연속 down" 판정이 **1년치와 한 분기를 비교해서** 나왔다.
"""
from market_intel import db as db_mod
from market_intel.interp import thesis as thesis_mod
from market_intel.models import FactCandidate, RawItem

CUTOFF = "2026-08-05T00:00:00+00:00"


def _fin(subject, metric, event_at, value, basis):
    return FactCandidate(
        raw_ref="x1", subject=subject, category="financials", metric=metric,
        event_at=event_at, market="US", country="US", value_num=value,
        unit="USD", comparison_basis=basis, data_status="source_verified",
    )


def _conn(tmp_path):
    db_path = str(tmp_path / "t.db")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    raw = RawItem(external_id="x1", source_published_at="2026-06-30",
                  safe_source_url="https://example.test/x", payload="{}")
    snap = db_mod.insert_raw_snapshot(conn, str(tmp_path / "raw"), "sec_edgar", raw)
    return conn, snap


def _seed_msft(conn, snap):
    """분기 3개 + 마지막 기간에 얹힌 1년치 누적. 누적을 **나중에** 알았다는
    것이 이 결함의 핵심이라 known_at 순서를 실제와 같게 둔다."""
    quarters = [("2025-12-31", 5_882_000_000.0, "2026-01-28"),
                ("2026-03-31", 15_803_000_000.0, "2026-04-29"),
                ("2026-06-30", 19_639_000_000.0, "2026-07-29")]
    for event_date, value, known in quarters:
        db_mod.upsert_fact(
            conn, f"sec_edgar:MSFT:free_cash_flow:{event_date.replace('-', '')}", snap,
            f"{known}T00:00:00+00:00",
            _fin("MSFT", "free_cash_flow", f"{event_date}T00:00:00+00:00", value, "quarterly"),
        )
    # 같은 fact_id에 얹히는 FY 누적 — 8/1에 알게 된다.
    db_mod.upsert_fact(
        conn, "sec_edgar:MSFT:free_cash_flow:20260630", snap, "2026-08-01T02:34:57+00:00",
        _fin("MSFT", "free_cash_flow", "2026-06-30T00:00:00+00:00", 66_987_000_000.0, "annual"),
    )
    conn.commit()


def test_cumulative_masks_quarter_without_basis_pin(tmp_path):
    """결함 재현: 기간 길이를 지정하지 않으면 누적치가 최신 분기 자리를 먹는다."""
    conn, snap = _conn(tmp_path)
    _seed_msft(conn, snap)

    rows = db_mod.facts_as_of(conn, CUTOFF, subject="MSFT", metric="free_cash_flow")
    latest = max(rows, key=lambda r: r["event_at"])
    assert latest["comparison_basis"] == "annual"
    assert latest["value_num"] == 66_987_000_000.0


def test_basis_pin_returns_the_quarter_and_drops_no_period(tmp_path):
    """수리: 기간 길이를 지정하면 그 기간 길이 안에서 최신 개정본을 고른다.

    누적이 이겼다는 이유로 그 분기가 통째로 사라지면 안 된다 — 3분기 전부
    돌아와야 한다."""
    conn, snap = _conn(tmp_path)
    _seed_msft(conn, snap)

    rows = db_mod.facts_as_of(conn, CUTOFF, subject="MSFT", metric="free_cash_flow",
                              comparison_basis="quarterly")
    by_date = {r["event_at"][:10]: r["value_num"] for r in rows}
    assert by_date == {
        "2025-12-31": 5_882_000_000.0,
        "2026-03-31": 15_803_000_000.0,
        "2026-06-30": 19_639_000_000.0,
    }


def test_thesis_observations_use_one_period_length(tmp_path):
    """가설 엔진은 1년치와 한 분기를 나란히 놓지 않는다."""
    conn, snap = _conn(tmp_path)
    _seed_msft(conn, snap)

    obs = thesis_mod._observations(
        conn, {"subject": "MSFT", "metric": "free_cash_flow", "category": "financials"}, CUTOFF)
    assert obs[0] == ("2026-06-30T00:00:00+00:00", 19_639_000_000.0)
    assert len(obs) == 3


def test_annual_only_subject_stays_visible(tmp_path):
    """분기로 못박지 않는 이유 — DART(한국)·TSM(20-F)은 연간밖에 없다.
    못박았다면 이 종목들의 재무 가설이 영구히 판정 불가가 된다."""
    conn, snap = _conn(tmp_path)
    db_mod.upsert_fact(
        conn, "dart:005930.KS:revenue:20251231", snap, "2026-03-15T00:00:00+00:00",
        _fin("005930.KS", "revenue", "2025-12-31T00:00:00+00:00", 333_605_938_000_000.0, "annual"),
    )
    conn.commit()

    obs = thesis_mod._observations(
        conn, {"subject": "005930.KS", "metric": "revenue", "category": "financials"}, CUTOFF)
    assert obs == [("2025-12-31T00:00:00+00:00", 333_605_938_000_000.0)]
