"""Information-blackout cutoff — the single source of truth (spec B6):
`facts_as_of` gets exactly this value and nothing else computes one.

Fixed per report type (spec B6 table):
    morning / week_start   당일 07:15 KST
    close_delta             당일 16:15 KST
    weekly_review           당일 08:30 KST
    monthly                 당일 08:30 KST
    quarterly / annual / event   생성 시각 (cutoff = the moment the report is
                                  built, not a fixed clock time — there is no
                                  scheduled slot to blackout against)
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_FIXED_TIME = {
    "morning": time(7, 15),
    "week_start": time(7, 15),
    "close_delta": time(16, 15),
    "weekly_review": time(8, 30),
    "monthly": time(8, 30),
}
_GENERATION_TIME_TYPES = {"quarterly", "annual", "event"}


def cutoff_for(report_type: str, report_date_kst: date, now: datetime | None = None) -> datetime:
    """Returns a tz-aware KST datetime. `now` is a test seam for the 3 types
    whose cutoff is "generation time" rather than a fixed daily clock; it is
    never used, and never needed, for the 5 fixed-time types."""
    if report_type in _FIXED_TIME:
        t = _FIXED_TIME[report_type]
        return datetime(
            report_date_kst.year, report_date_kst.month, report_date_kst.day,
            t.hour, t.minute, tzinfo=KST,
        )
    if report_type in _GENERATION_TIME_TYPES:
        moment = now or datetime.now(timezone.utc)
        return moment.astimezone(KST)
    raise ValueError(f"cutoff_for: unknown report_type {report_type!r}")
