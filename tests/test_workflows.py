from market_intel.workflows import WORKFLOWS


def test_workflow_composition_is_fixed():
    # sec_edgar_13f added alongside sec_edgar (repair.md finding #4: 13F-HR
    # detection for the tracked managers was in scope but unimplemented).
    # `kis`가 morning에도 있는 것은 재시도 목적이다(중복이 아니라 복구) —
    # 수급을 하루 한 번만 받으면 그 한 번이 실패한 날은 리포트에서 통째로 빈다.
    # `krx`(전종목 시장 폭)도 같은 이유로 morning/close 둘 다에 있다
    # (krx-breadth spec §4).
    assert WORKFLOWS["morning"] == ["yfinance", "sec_edgar", "sec_edgar_13f", "fred", "kis", "krx"]
    assert WORKFLOWS["close"] == ["yfinance", "ecos", "dart", "kis", "krx"]
    # spec B14 (2A/ST1) — calendar/events are new workflow entries; "all"
    # gains their 4 providers but morning/close stay byte-for-byte the same.
    assert WORKFLOWS["calendar"] == ["fred_calendar", "earnings_calendar", "policy_calendar"]
    # `yfinance_holdings`(2026-08-21) — 업종 ETF 상위 보유종목 비중. events에
    # 붙인 이유는 이 시험이 지키려는 것이 **morning/close의 불변**이기 때문이다:
    # 그 둘은 위에서 한 바이트도 안 바뀐 채 그대로다.
    assert WORKFLOWS["events"] == ["sec_8k_events", "yfinance_holdings"]
    assert WORKFLOWS["all"] == [
        "yfinance", "yfinance_holdings", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart",
        "fred_calendar", "earnings_calendar", "policy_calendar", "sec_8k_events", "kis", "krx",
    ]
    assert set(WORKFLOWS["all"]) == (
        set(WORKFLOWS["morning"]) | set(WORKFLOWS["close"]) | set(WORKFLOWS["calendar"]) | set(WORKFLOWS["events"])
    )
