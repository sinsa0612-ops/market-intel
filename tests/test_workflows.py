from market_intel.workflows import WORKFLOWS


def test_workflow_composition_is_fixed():
    # sec_edgar_13f added alongside sec_edgar (repair.md finding #4: 13F-HR
    # detection for the tracked managers was in scope but unimplemented).
    assert WORKFLOWS["morning"] == ["yfinance", "sec_edgar", "sec_edgar_13f", "fred"]
    # `kis`(한국 투자자별 수급)는 close에만 있다 — KIS가 당일 수급을 15:40 KST
    # 전에 주지 않으므로 morning(06:50)에 넣으면 매일 0건이다(2026-08-03 실측).
    assert WORKFLOWS["close"] == ["yfinance", "pykrx", "ecos", "dart", "kis"]
    # spec B14 (2A/ST1) — calendar/events are new workflow entries; "all"
    # gains their 4 providers but morning/close stay byte-for-byte the same.
    assert WORKFLOWS["calendar"] == ["fred_calendar", "earnings_calendar", "policy_calendar"]
    assert WORKFLOWS["events"] == ["sec_8k_events"]
    assert WORKFLOWS["all"] == [
        "yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart",
        "fred_calendar", "earnings_calendar", "policy_calendar", "sec_8k_events", "kis",
    ]
    assert set(WORKFLOWS["all"]) == (
        set(WORKFLOWS["morning"]) | set(WORKFLOWS["close"]) | set(WORKFLOWS["calendar"]) | set(WORKFLOWS["events"])
    )
