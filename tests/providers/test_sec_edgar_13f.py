"""Offline unit tests for 13F-HR filing detection (repair.md finding #4).

Response shapes are modeled on real SEC browse-edgar atom output verified
live against https://www.sec.gov/cgi-bin/browse-edgar during this repair
(single-company mode for Berkshire/Pershing Square/Lone Pine/TCI, and
genuine multi-company ambiguity for "baupost group" -> two CIKs, one
inactive since 2010, one still filing)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pytest

from market_intel.http_client import SafeHttp
from market_intel.models import CollectContext
from market_intel.providers.sec_edgar_13f import TRACKED_MANAGERS, Sec13fProvider

ATOM_HEADER = '<?xml version="1.0" encoding="ISO-8859-1" ?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
ATOM_FOOTER = "</feed>"


def _single_company_atom(cik: str, filings: list[tuple[str, str, str]]) -> str:
    """filings: list of (accession_number, filing_date, filing_type), most
    recent first -- matches the real feed's own ordering."""
    entries = "".join(
        f"""
        <entry>
          <content type="text/xml">
            <accession-number>{acc}</accession-number>
            <filing-date>{date}</filing-date>
            <filing-type>{ftype}</filing-type>
          </content>
        </entry>"""
        for acc, date, ftype in filings
    )
    return f"""{ATOM_HEADER}
      <company-info><cik>{cik}</cik></company-info>{entries}
    {ATOM_FOOTER}"""


def _multi_company_atom(ciks: list[str]) -> str:
    entries = "".join(
        f"""
        <entry>
          <content type="text/xml">
            <company-info><cik>{cik}</cik></company-info>
          </content>
        </entry>"""
        for cik in ciks
    )
    return f"{ATOM_HEADER}{entries}{ATOM_FOOTER}"


def _empty_atom() -> str:
    return f"{ATOM_HEADER}{ATOM_FOOTER}"


BERKSHIRE_ATOM = _single_company_atom(
    "0001067983",
    [("0001193125-26-226661", "2026-05-15", "13F-HR")],
)
PERSHING_ATOM = _single_company_atom("0001336528", [("0000000000-26-000001", "2026-05-15", "13F-HR")])
LONE_PINE_ATOM = _single_company_atom("0001061165", [("0000000000-26-000002", "2026-05-15", "13F-HR")])
TCI_ATOM = _single_company_atom("0001647251", [("0000000000-26-000003", "2026-05-15", "13F-HR")])

# Baupost: name search is ambiguous (real SEC behavior) -- one CIK inactive
# since 2010, one still filing. Its own by-CIK atom is single-company mode.
BAUPOST_SEARCH_ATOM = _multi_company_atom(["0001054420", "0001061768"])
BAUPOST_INACTIVE_ATOM = _empty_atom()  # 0001054420: no 13F-HR entries at all
BAUPOST_ACTIVE_ATOM = _single_company_atom("0001061768", [("0000000000-26-000004", "2026-05-15", "13F-HR")])


def _make_ctx(settings, handler) -> CollectContext:
    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


def _route(request: httpx.Request) -> httpx.Response:
    params = dict(request.url.params)
    cik = params.get("CIK")
    company = params.get("company")

    if cik == "0001067983":
        return httpx.Response(200, text=BERKSHIRE_ATOM)
    if cik == "0001336528" or company == "pershing square capital":
        return httpx.Response(200, text=PERSHING_ATOM)
    if cik == "0001061165" or company == "lone pine capital":
        return httpx.Response(200, text=LONE_PINE_ATOM)
    if cik == "0001647251" or company == "tci fund management":
        return httpx.Response(200, text=TCI_ATOM)
    if company == "berkshire hathaway":
        return httpx.Response(200, text=BERKSHIRE_ATOM)
    if company == "baupost group":
        return httpx.Response(200, text=BAUPOST_SEARCH_ATOM)
    if cik == "0001054420":
        return httpx.Response(200, text=BAUPOST_INACTIVE_ATOM)
    if cik == "0001061768":
        return httpx.Response(200, text=BAUPOST_ACTIVE_ATOM)
    return httpx.Response(404, text="not found")


@pytest.fixture
def result(settings):
    return Sec13fProvider().collect(_make_ctx(settings, _route))


def test_all_five_tracked_managers_are_detected(result):
    subjects = {f.subject for f in result.facts}
    assert subjects == {slug for slug, _, _ in TRACKED_MANAGERS}
    assert result.status == "OK"


def test_single_company_match_uses_most_recent_filing(result):
    berkshire = next(f for f in result.facts if f.subject == "berkshire_hathaway")
    assert berkshire.value_text == "0001193125-26-226661"
    assert berkshire.extra["form"] == "13F-HR"
    assert berkshire.event_at.startswith("2026-05-15")
    assert berkshire.category == "13f_filing"
    assert berkshire.metric == "filing_event"
    assert berkshire.data_status == "source_verified"


def test_ambiguous_name_match_resolves_to_the_still_active_entity(result):
    """Two CIKs share the 'baupost group' search term -- the inactive one
    (no 13F-HR filings since 2010) must never be picked over the one that
    is still actually filing."""
    baupost = next(f for f in result.facts if f.subject == "baupost_group")
    assert baupost.value_text == "0000000000-26-000004"
    assert baupost.event_at.startswith("2026-05-15")


def test_no_recent_filing_is_reported_not_fabricated(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_empty_atom())

    result = Sec13fProvider().collect(_make_ctx(settings, handler))
    assert result.status == "NO_DATA"
    assert not result.facts
    assert "no_match" in result.safe_detail


def test_secret_free_source_urls_are_stored(result):
    for item in result.raw_items:
        assert item.safe_source_url.startswith("https://")
