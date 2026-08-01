"""Offline unit tests for policy_calendar.py, replayed against frozen real
snapshots (spec ST1 test_policy_calendar_parse): FOMC 2026 must parse to
exactly the 8 known meetings, BOK 2026 to exactly the 8 known meetings, a
requested year with fewer than 6 parsed meetings must yield no facts for
that year rather than a guessed calendar, and each source must survive the
other one failing (spec ST1 What #3, both directions)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from market_intel.http_client import SafeHttp
from market_intel.models import CollectContext
from market_intel.providers.policy_calendar import (
    MIN_MEETINGS_FOR_A_YEAR,
    PolicyCalendarProvider,
    parse_bok_meetings,
    parse_fomc_meetings,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FOMC_HTML = (FIXTURES / "fomccalendars.htm").read_text()
BOK_2026_HTML = (FIXTURES / "bok_listYear_2026.htm").read_text()
BOK_2027_HTML = (FIXTURES / "bok_listYear_2027.htm").read_text()

EXPECTED_FOMC_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
EXPECTED_BOK_2026 = [
    "2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28",
    "2026-07-16", "2026-08-27", "2026-10-22", "2026-11-26",
]

TRUNCATED_FOMC_HTML = (
    '<div class="panel"><div class="panel-heading"><h4><a id="1">2026 FOMC Meetings</a></h4></div>'
    '<div class="fomc-meeting__month"><strong>January</strong></div>'
    '<div class="fomc-meeting__date">27-28</div>'
    '<div class="fomc-meeting__month"><strong>March</strong></div>'
    '<div class="fomc-meeting__date">17-18</div></div>'
)


def _ctx(settings, handler, now=datetime(2026, 8, 1, tzinfo=timezone.utc)):
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


def _handler(fomc_response, bok_2026_response=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "federalreserve.gov" in url:
            return fomc_response
        if "bok.or.kr" in url:
            py = dict(request.url.params).get("pYear")
            if py == "2026":
                return bok_2026_response or httpx.Response(200, text=BOK_2026_HTML)
            return httpx.Response(200, text=BOK_2027_HTML)
        return httpx.Response(404)

    return handler


def test_parse_fomc_2026_matches_known_meetings():
    assert parse_fomc_meetings(FOMC_HTML, 2026) == EXPECTED_FOMC_2026


def test_parse_bok_2026_matches_known_meetings():
    assert parse_bok_meetings(BOK_2026_HTML, 2026) == EXPECTED_BOK_2026


def test_parse_bok_2027_is_honestly_empty_not_guessed():
    # bok.or.kr genuinely returns 0 rows for a not-yet-published year.
    assert parse_bok_meetings(BOK_2027_HTML, 2027) == []


def test_year_timetable_fact_shape(settings):
    """spec B2 rev2: one fact per (source, year) whose value is the whole
    year's meeting list — no ordinal in the subject, so an unscheduled
    meeting cannot shift every later slot (judge §③)."""
    result = PolicyCalendarProvider().collect(_ctx(settings, _handler(httpx.Response(200, text=FOMC_HTML))))
    fomc = [f for f in result.facts if f.subject == "fomc" and f.event_at.startswith("2026")]
    assert len(fomc) == 1
    assert fomc[0].value_text == ",".join(EXPECTED_FOMC_2026)
    assert fomc[0].event_at == "2026-01-01T00:00:00+00:00"
    assert fomc[0].comparison_basis == "연도 일정표(그 해에 공표된 예정일 전부)"
    assert fomc[0].extra["dates"] == EXPECTED_FOMC_2026
    assert fomc[0].metric == "scheduled_date" and fomc[0].category == "calendar"

    bok = [f for f in result.facts if f.subject == "bokmpc"]
    assert len(bok) == 1, "2027 is not published yet -> no fact for it"
    assert bok[0].value_text == ",".join(EXPECTED_BOK_2026)
    assert bok[0].country == "KR"


def test_below_minimum_year_is_not_published_as_facts(settings):
    """A year with < MIN_MEETINGS_FOR_A_YEAR parsed meetings must produce no
    facts for that year, never a partial guess — checked on BOTH sources."""
    result = PolicyCalendarProvider().collect(_ctx(settings, _handler(httpx.Response(200, text=FOMC_HTML))))
    assert [f for f in result.facts if f.subject == "bokmpc" and f.event_at.startswith("2027")] == []
    assert "bokmpc:2027:parsed_0_below_min" in result.safe_detail
    assert result.status == "PARTIAL"

    truncated = PolicyCalendarProvider().collect(
        _ctx(settings, _handler(httpx.Response(200, text=TRUNCATED_FOMC_HTML)))
    )
    assert [f for f in truncated.facts if f.subject == "fomc"] == [], "a partially parsed FOMC page must publish nothing"
    assert "fomc:2026:parsed_2_below_min" in truncated.safe_detail


def test_fomc_source_failure_does_not_block_bok(settings):
    result = PolicyCalendarProvider().collect(_ctx(settings, _handler(httpx.Response(500))))
    assert not [f for f in result.facts if f.subject == "fomc"]
    assert len([f for f in result.facts if f.subject == "bokmpc"]) == 1
    assert "fomc:http_500" in result.safe_detail
    assert result.status == "PARTIAL"


def test_bok_source_failure_does_not_block_fomc(settings):
    result = PolicyCalendarProvider().collect(
        _ctx(settings, _handler(httpx.Response(200, text=FOMC_HTML), bok_2026_response=httpx.Response(503)))
    )
    assert not [f for f in result.facts if f.subject == "bokmpc"]
    assert [f.value_text for f in result.facts if f.subject == "fomc" and f.event_at.startswith("2026")] == [
        ",".join(EXPECTED_FOMC_2026)
    ]
    assert "bokmpc:2026:http_503" in result.safe_detail
    assert result.status == "PARTIAL"


def test_min_meetings_threshold_is_six():
    assert MIN_MEETINGS_FOR_A_YEAR == 6
