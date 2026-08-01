"""Offline unit tests for sec_8k_events.py — 8-K Item 2.02 detection, the
one thing sec_edgar.py's own ANNUAL_QUARTERLY_FORMS filter never sees."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pytest

from market_intel.http_client import SafeHttp
from market_intel.models import CollectContext
from market_intel.providers.sec_8k_events import Sec8kEventsProvider

CIK = 789019
TICKERS = {"0": {"cik_str": CIK, "ticker": "MSFT", "title": "MICROSOFT CORP"}}

SUBMISSIONS = {
    "cik": str(CIK),
    "filings": {
        "recent": {
            "form": ["8-K", "8-K", "10-Q"],
            "filingDate": ["2026-07-29", "2026-06-05", "2026-04-30"],
            "accessionNumber": ["0001193125-26-323632", "0001193125-26-258667", "0000789019-26-000042"],
            "items": ["2.02,9.01", "5.02", ""],
        }
    },
}


def _make_ctx(settings, handler):
    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("company_tickers.json"):
        return httpx.Response(200, json=TICKERS)
    if "/submissions/" in url:
        return httpx.Response(200, json=SUBMISSIONS)
    return httpx.Response(404)


def test_detects_8k_with_item_2_02(settings):
    result = Sec8kEventsProvider().collect(_make_ctx(settings, _handler))
    msft = [f for f in result.facts if f.subject == "MSFT"]
    assert len(msft) == 1
    fact = msft[0]
    assert fact.category == "event" and fact.metric == "earnings_release_8k"
    assert fact.value_text == "0001193125-26-323632"
    assert fact.event_at == "2026-07-29T00:00:00+00:00"
    assert fact.data_status == "source_verified"
    assert fact.extra["item"] == "2.02"


def test_8k_without_item_2_02_is_ignored(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json=TICKERS)
        if "/submissions/" in url:
            subs = {
                "cik": str(CIK),
                "filings": {"recent": {
                    "form": ["8-K"], "filingDate": ["2026-06-05"],
                    "accessionNumber": ["0001193125-26-258667"], "items": ["5.02"],
                }},
            }
            return httpx.Response(200, json=subs)
        return httpx.Response(404)

    result = Sec8kEventsProvider().collect(_make_ctx(settings, handler))
    assert not [f for f in result.facts if f.subject == "MSFT"]
    assert "MSFT:no_8k_2.02" in result.safe_detail


def test_missing_ticker_is_reported_not_guessed(settings):
    result = Sec8kEventsProvider().collect(_make_ctx(settings, _handler))
    assert "cik_not_found" in result.safe_detail
    assert not [f for f in result.facts if f.subject == "NVDA"]
