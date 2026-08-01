"""Workflow -> provider-name list (spec A8, fixed)."""
from __future__ import annotations

WORKFLOWS: dict[str, list[str]] = {
    "morning": ["yfinance", "sec_edgar", "sec_edgar_13f", "fred"],
    "close": ["yfinance", "pykrx", "ecos", "dart"],
    # spec B14 (ST1 addition) — morning/close are untouched (1단계 테스트가
    # 내용을 검사한다); calendar/events are new entries, also added to "all".
    "calendar": ["fred_calendar", "earnings_calendar", "policy_calendar"],
    "events": ["sec_8k_events"],
    "all": [
        "yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart",
        "fred_calendar", "earnings_calendar", "policy_calendar", "sec_8k_events",
    ],
}
