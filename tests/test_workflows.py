from market_intel.workflows import WORKFLOWS


def test_workflow_composition_is_fixed():
    # sec_edgar_13f added alongside sec_edgar (repair.md finding #4: 13F-HR
    # detection for the tracked managers was in scope but unimplemented).
    assert WORKFLOWS["morning"] == ["yfinance", "sec_edgar", "sec_edgar_13f", "fred"]
    assert WORKFLOWS["close"] == ["yfinance", "pykrx", "ecos", "dart"]
    # spec B14 (2A/ST1) — calendar/events are new workflow entries; "all"
    # gains their 4 providers but morning/close stay byte-for-byte the same.
    assert WORKFLOWS["calendar"] == ["fred_calendar", "earnings_calendar", "policy_calendar"]
    assert WORKFLOWS["events"] == ["sec_8k_events"]
    assert WORKFLOWS["all"] == [
        "yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart",
        "fred_calendar", "earnings_calendar", "policy_calendar", "sec_8k_events",
    ]
    assert set(WORKFLOWS["all"]) == (
        set(WORKFLOWS["morning"]) | set(WORKFLOWS["close"]) | set(WORKFLOWS["calendar"]) | set(WORKFLOWS["events"])
    )
