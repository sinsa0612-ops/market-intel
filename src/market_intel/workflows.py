"""Workflow -> provider-name list (spec A8, fixed)."""
from __future__ import annotations

WORKFLOWS: dict[str, list[str]] = {
    "morning": ["yfinance", "sec_edgar", "sec_edgar_13f", "fred"],
    # `kis`(한국 투자자별 수급)가 close에만 있는 이유: KIS는 당일 수급을
    # 15:40 KST 전에 주지 않는다. collect-pm은 16:15라 당일치가 나오고, 다음 날
    # 아침 리포트(차단선 07:15)는 그 값을 known_at 기준으로 정상적으로 본다 —
    # morning에 또 넣으면 같은 값을 하루 두 번 받을 뿐이다.
    "close": ["yfinance", "pykrx", "ecos", "dart", "kis"],
    # spec B14 (ST1 addition) — morning/close are untouched (1단계 테스트가
    # 내용을 검사한다); calendar/events are new entries, also added to "all".
    "calendar": ["fred_calendar", "earnings_calendar", "policy_calendar"],
    "events": ["sec_8k_events"],
    "all": [
        "yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart",
        "fred_calendar", "earnings_calendar", "policy_calendar", "sec_8k_events", "kis",
    ],
}
