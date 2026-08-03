"""Workflow -> provider-name list (spec A8, fixed)."""
from __future__ import annotations

WORKFLOWS: dict[str, list[str]] = {
    # `kis`가 morning에도 있는 이유는 **재시도**다. 수급은 하루 한 번(15:50)
    # 받는데 그 한 번이 실패하면 그날치가 리포트에서 통째로 빈다 — 다음 기회가
    # 다음 날 15:50이기 때문이다. 실패는 실제로 일어난다(2026-08-03 실측: 토큰
    # 403, 조회 500, 그리고 KIS의 15:40 공개 경계까지 여유가 10분뿐이다).
    # 06:50에는 `_base_date()`가 전날로 물러서고 KIS가 그 날짜 기준 30거래일을
    # 주므로, 전날 실패분이 아침에 자동으로 메워진다. 비용은 호출 5번이다.
    "morning": ["yfinance", "sec_edgar", "sec_edgar_13f", "fred", "kis"],
    # close(15:50)가 당일치를 받는 자리다 — KIS는 15:40 이후에야 당일 수급을 준다.
    "close": ["yfinance", "ecos", "dart", "kis"],
    # spec B14 (ST1 addition) — morning/close are untouched (1단계 테스트가
    # 내용을 검사한다); calendar/events are new entries, also added to "all".
    "calendar": ["fred_calendar", "earnings_calendar", "policy_calendar"],
    "events": ["sec_8k_events"],
    "all": [
        "yfinance", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart",
        "fred_calendar", "earnings_calendar", "policy_calendar", "sec_8k_events", "kis",
    ],
}
