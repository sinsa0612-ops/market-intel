"""Information-blackout cutoff — the single source of truth (spec B6):
`facts_as_of` gets exactly this value and nothing else computes one.

Fixed per report type (spec B6 table):
    morning / weekly_review 당일 07:15 KST
    close_delta             당일 16:15 KST
    monthly                 당일 08:30 KST
    quarterly / annual / event   생성 시각 (cutoff = the moment the report is
                                  built, not a fixed clock time — there is no
                                  scheduled slot to blackout against)
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# `weekly_review` moved 08:30 -> 07:15 with the 2026-08-20 merge (it absorbed
# `week_start` and now runs Monday 07:40, not Saturday 08:30). A blackout must
# never sit *after* the run that produces the report: at 08:30 the 07:40 report
# would claim it had seen the whole morning, and a later catch-up rebuild of the
# same date would legitimately see 07:40–08:30 facts and produce different text
# for the same stamped blackout. 07:15 is the Monday-morning family's value and
# `collect-am` (06:50) still precedes it, so nothing is lost.
_FIXED_TIME = {
    "morning": time(7, 15),
    "close_delta": time(16, 15),
    "weekly_review": time(7, 15),
    "monthly": time(8, 30),
}
_GENERATION_TIME_TYPES = {"quarterly", "annual", "event"}


def cutoff_for(report_type: str, report_date_kst: date, now: datetime | None = None) -> datetime:
    """Returns a tz-aware KST datetime. `now` is a test seam for the 3 types
    whose cutoff is "generation time" rather than a fixed daily clock; it is
    never used, and never needed, for the 4 fixed-time types."""
    if report_type in _FIXED_TIME:
        t = _FIXED_TIME[report_type]
        return datetime(
            report_date_kst.year, report_date_kst.month, report_date_kst.day,
            t.hour, t.minute, tzinfo=KST,
        )
    if report_type in _GENERATION_TIME_TYPES:
        # Second resolution, matching the fixed-time types. The raw
        # `datetime.now()` printed a blackout of "18:40:19.353639+09:00" on
        # the published page, where every other report shows a clean time —
        # sub-second precision means nothing for an information barrier.
        moment = now or datetime.now(timezone.utc)
        return moment.astimezone(KST).replace(microsecond=0)
    raise ValueError(f"cutoff_for: unknown report_type {report_type!r}")
