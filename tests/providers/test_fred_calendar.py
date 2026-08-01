"""Offline unit tests for fred_calendar.py: the year-timetable fact shape
(spec B2 rev2), the density guard + weekend discriminator (spec B3.3), the
fixed allowlist, and the no-key path."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from market_intel.http_client import SafeHttp
from market_intel.models import CollectContext
from market_intel.providers.fred_calendar import ALLOWED_RELEASE_NAMES, FredCalendarProvider

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
THIS_YEAR = NOW.year

RELEASES = {
    "releases": [
        {"id": 10, "name": "Consumer Price Index"},
        {"id": 101, "name": "FOMC Press Release"},  # not in the allowlist -> must never be fetched
    ]
}


def _make_ctx(settings, handler):
    return CollectContext(
        cutoff=NOW, now=NOW, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


def _handler_with_dates(dates_by_release_id: dict[int, list[str]], only_year: int = THIS_YEAR):
    """`/fred/release/dates` is queried once per year (spec B3.2); the
    fixture answers for `only_year` and returns nothing for the other."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.stlouisfed.org/fred/release/dates"):
            params = dict(request.url.params)
            rid = int(params["release_id"])
            year = int(params["realtime_start"][:4])
            dates = dates_by_release_id.get(rid, []) if year == only_year else []
            return httpx.Response(200, json={"release_dates": [{"release_id": rid, "date": d} for d in dates]})
        if url.startswith("https://api.stlouisfed.org/fred/releases"):
            return httpx.Response(200, json=RELEASES)
        return httpx.Response(404)

    return handler


def _all_days(year: int) -> list[str]:
    out, d = [], date(year, 1, 1)
    while d.year == year:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _weekdays(year: int) -> list[str]:
    return [d for d in _all_days(year) if date.fromisoformat(d).weekday() < 5]


def test_no_key_means_zero_http_calls(settings):
    settings.fred_api_key = ""

    def boom(_name):
        raise AssertionError("SafeHttp factory must not be invoked when the key is empty")

    result = FredCalendarProvider().collect(
        CollectContext(cutoff=NOW, now=NOW, settings=settings,
                        http=boom, universe=[], logger=logging.getLogger("test"))
    )
    assert result.status == "NO_DATA"
    assert result.reason_code == "키없음"
    assert result.facts == []


def test_queries_whole_calendar_years_not_a_sliding_window(settings):
    """spec B2 rev2: a sliding window would drop dates out the far end and
    mint a ghost revision every day. The query is the absolute year."""
    settings.fred_api_key = "FAKEKEY"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.stlouisfed.org/fred/release/dates"):
            p = dict(request.url.params)
            seen.append((p["realtime_start"], p["realtime_end"]))
            return httpx.Response(200, json={"release_dates": []})
        if url.startswith("https://api.stlouisfed.org/fred/releases"):
            return httpx.Response(200, json=RELEASES)
        return httpx.Response(404)

    FredCalendarProvider().collect(_make_ctx(settings, handler))
    assert seen == [
        (f"{THIS_YEAR}-01-01", f"{THIS_YEAR}-12-31"),
        (f"{THIS_YEAR + 1}-01-01", f"{THIS_YEAR + 1}-12-31"),
    ]


def test_daily_fill_is_discarded_and_reported(settings):
    """spec ST1 test_fred_daily_fill_guard: every calendar day incl. weekends
    == `include_release_dates_with_no_data` artefact -> discard, on the record."""
    settings.fred_api_key = "FAKEKEY"
    daily = _all_days(THIS_YEAR)
    result = FredCalendarProvider().collect(_make_ctx(settings, _handler_with_dates({10: daily})))
    assert [f for f in result.facts if f.subject == "fredrel:10"] == []
    assert f"daily_fill_discarded:{len(daily)}" in result.safe_detail


def test_real_daily_business_day_series_is_kept_as_cadence(settings):
    """spec B3.3 rev2: same density, zero weekend entries -> a genuine
    business-day series (H.15). Kept as one `release_cadence` fact instead of
    being thrown away with the artefacts."""
    settings.fred_api_key = "FAKEKEY"
    weekdays = _weekdays(THIS_YEAR)
    result = FredCalendarProvider().collect(_make_ctx(settings, _handler_with_dates({10: weekdays})))

    assert f"daily_series_kept:{len(weekdays)}" in result.safe_detail
    assert "daily_fill_discarded" not in result.safe_detail
    facts = [f for f in result.facts if f.subject == "fredrel:10"]
    assert len(facts) == 1
    cadence = facts[0]
    assert cadence.metric == "release_cadence"
    assert cadence.value_text == "daily_business_day"
    assert cadence.value_num == len(weekdays)
    assert cadence.data_status == "source_verified"
    assert cadence.event_at == f"{THIS_YEAR}-01-01T00:00:00+00:00"
    assert cadence.extra["weekend_count"] == 0
    assert cadence.extra["first"] == weekdays[0] and cadence.extra["last"] == weekdays[-1]


def test_sparse_dates_become_one_year_timetable_fact(settings):
    """spec B2 rev2: one fact per (release, year), all that year's dates in a
    normalised CSV, anchored on 1 Jan — NOT one fact per scheduled date."""
    settings.fred_api_key = "FAKEKEY"
    sparse = [f"{THIS_YEAR}-09-11", f"{THIS_YEAR}-08-12", f"{THIS_YEAR}-08-12", f"{THIS_YEAR}-10-14"]
    result = FredCalendarProvider().collect(_make_ctx(settings, _handler_with_dates({10: sparse})))
    cpi_facts = [f for f in result.facts if f.subject == "fredrel:10"]
    assert len(cpi_facts) == 1, "one timetable per year, not one fact per date"

    fact = cpi_facts[0]
    assert fact.event_at == f"{THIS_YEAR}-01-01T00:00:00+00:00", "anchor is the year the timetable covers"
    # sorted + de-duplicated + no spaces: an unstable serialisation would
    # append a ghost revision on every collect (spec B2 rev2 정규화 직렬화).
    assert fact.value_text == f"{THIS_YEAR}-08-12,{THIS_YEAR}-09-11,{THIS_YEAR}-10-14"
    assert fact.extra["dates"] == [f"{THIS_YEAR}-08-12", f"{THIS_YEAR}-09-11", f"{THIS_YEAR}-10-14"]
    assert fact.extra["release_id"] == 10 and fact.extra["release_name"] == "Consumer Price Index"
    assert fact.comparison_basis == "연도 일정표(그 해에 공표된 예정일 전부)"
    assert fact.data_status == "source_verified"
    assert fact.category == "calendar" and fact.metric == "scheduled_date"


def test_year_with_no_dates_creates_no_empty_fact(settings):
    """spec B2 rev2: 0 dates -> no fact at all (an empty-string fact would be
    a lie about what the source published)."""
    settings.fred_api_key = "FAKEKEY"
    result = FredCalendarProvider().collect(_make_ctx(settings, _handler_with_dates({10: []})))
    assert result.facts == []
    assert result.status == "NO_DATA"


def test_dates_outside_the_queried_year_never_pollute_a_timetable(settings):
    settings.fred_api_key = "FAKEKEY"
    dates = [f"{THIS_YEAR}-08-12", f"{THIS_YEAR - 1}-12-30"]
    result = FredCalendarProvider().collect(_make_ctx(settings, _handler_with_dates({10: dates})))
    fact = next(f for f in result.facts if f.subject == "fredrel:10")
    assert fact.value_text == f"{THIS_YEAR}-08-12"


def test_release_outside_allowlist_is_never_requested(settings):
    settings.fred_api_key = "FAKEKEY"
    seen_release_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.stlouisfed.org/fred/release/dates"):
            seen_release_ids.append(int(dict(request.url.params)["release_id"]))
            return httpx.Response(200, json={"release_dates": []})
        if url.startswith("https://api.stlouisfed.org/fred/releases"):
            return httpx.Response(200, json=RELEASES)
        return httpx.Response(404)

    FredCalendarProvider().collect(_make_ctx(settings, handler))
    assert 101 not in seen_release_ids, "FOMC Press Release (id 101) must never be fetched via fred_calendar (spec B3.4)"
    assert seen_release_ids.count(10) == 2, "one query per year"


def test_allowlist_has_the_eight_fixed_names():
    assert ALLOWED_RELEASE_NAMES == [
        "Consumer Price Index",
        "Employment Situation",
        "Personal Income and Outlays",
        "Gross Domestic Product",
        "Advance Monthly Sales for Retail and Food Services",
        "G.17 Industrial Production and Capacity Utilization",
        "Job Openings and Labor Turnover Survey",
        "H.15 Selected Interest Rates",
    ]
