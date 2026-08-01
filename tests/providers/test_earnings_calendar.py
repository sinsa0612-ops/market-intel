"""Offline unit tests for earnings_calendar.py (yfinance `.calendar`, not
`.get_earnings_dates()` — spec B0/ST1 What #2)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pytest

from market_intel.models import CollectContext
from market_intel.providers import earnings_calendar as mod
from market_intel.universe import CORE16

CORE16_SYMBOLS = [m["symbol"] for m in CORE16]


class _FakeTicker:
    def __init__(self, symbol, calendars):
        self.symbol = symbol
        self._calendars = calendars

    @property
    def calendar(self):
        return self._calendars.get(self.symbol, {})


def _ctx(settings):
    now = datetime.now(timezone.utc)
    return CollectContext(cutoff=now, now=now, settings=settings, http=lambda name: None, universe=[], logger=logging.getLogger("t"))


def test_maps_earnings_date_and_consensus(settings, monkeypatch):
    calendars = {
        sym: {"Earnings Date": [date(2026, 8, 27)], "Earnings Average": 2.08225, "Revenue Average": 91821907760}
        for sym in CORE16_SYMBOLS
    }
    monkeypatch.setattr(mod.yf, "Ticker", lambda s: _FakeTicker(s, calendars))

    result = mod.EarningsCalendarProvider().collect(_ctx(settings))
    nvda = [f for f in result.facts if f.subject == "NVDA"]
    by_metric = {f.metric: f for f in nvda}

    assert by_metric["scheduled_date"].value_text == "2026-08-27"
    assert by_metric["scheduled_date"].event_at == "2026-01-01T00:00:00+00:00", "anchor is the year, not the date"
    assert by_metric["scheduled_date"].comparison_basis == "연도 일정표(그 해에 공표된 예정일 전부)"
    assert by_metric["scheduled_date"].data_status == "partial"
    assert by_metric["scheduled_date"].extra["time_unknown"] is True
    assert by_metric["consensus_eps"].value_num == 2.08225
    assert by_metric["consensus_revenue"].value_num == 91821907760
    assert result.status == "OK"


def test_consensus_unit_follows_the_listing_market(settings, monkeypatch):
    """The consensus numbers arrive from `.calendar` without a currency, so
    it comes from the symbol's market the same way universe.py's `unit`
    convention resolves an instrument's unit — a KR issuer's EPS is won.
    Storing 삼성전자's 14,322 as USD would publish "$14,322 EPS"."""
    calendars = {
        sym: {"Earnings Date": [date(2026, 10, 28)], "Earnings Average": 14322.95, "Revenue Average": 2.1e14}
        for sym in CORE16_SYMBOLS
    }
    monkeypatch.setattr(mod.yf, "Ticker", lambda s: _FakeTicker(s, calendars))
    result = mod.EarningsCalendarProvider().collect(_ctx(settings))
    units = {(f.subject, f.metric): f.unit for f in result.facts}

    assert units[("005930.KS", "consensus_eps")] == "KRW/share"
    assert units[("005930.KS", "consensus_revenue")] == "KRW"
    assert units[("NVDA", "consensus_eps")] == "USD/share"
    assert units[("NVDA", "consensus_revenue")] == "USD"
    # TSM is a US-listed ADR (market=US) even though country=TW.
    assert units[("TSM", "consensus_eps")] == "USD/share"


def test_consensus_facts_carry_no_date_in_extra(settings, monkeypatch):
    """spec B2 rev2: `extra` may only hold things derived from the stored
    value. A scheduled date parked in a consensus fact's extra would go
    stale silently — upsert_fact compares value/unit/status, never extra."""
    calendars = {"NVDA": {"Earnings Date": [date(2026, 8, 27)], "Earnings Average": 2.0, "Revenue Average": 1.0}}
    monkeypatch.setattr(mod.yf, "Ticker", lambda s: _FakeTicker(s, calendars))
    result = mod.EarningsCalendarProvider().collect(_ctx(settings))
    consensus = [f for f in result.facts if f.metric.startswith("consensus_")]
    assert consensus
    for fact in consensus:
        assert "scheduled_date" not in fact.extra


def test_earnings_date_as_list_takes_first_element(settings, monkeypatch):
    calendars = {"NVDA": {"Earnings Date": [date(2026, 8, 27), date(2026, 11, 25)]}}
    monkeypatch.setattr(mod.yf, "Ticker", lambda s: _FakeTicker(s, calendars))
    result = mod.EarningsCalendarProvider().collect(_ctx(settings))
    nvda_dates = [f for f in result.facts if f.subject == "NVDA" and f.metric == "scheduled_date"]
    assert len(nvda_dates) == 1
    assert nvda_dates[0].value_text == "2026-08-27"


def test_missing_calendar_is_reported_not_guessed(settings, monkeypatch):
    monkeypatch.setattr(mod.yf, "Ticker", lambda s: _FakeTicker(s, {}))
    result = mod.EarningsCalendarProvider().collect(_ctx(settings))
    assert result.facts == []
    assert result.status == "NO_DATA"
    assert "no_earnings_date" in result.safe_detail


def test_ticker_error_is_isolated_per_symbol(settings, monkeypatch):
    class _Boom:
        def __init__(self, s):
            self.s = s

        @property
        def calendar(self):
            if self.s == "NVDA":
                raise RuntimeError("rate limited")
            return {"Earnings Date": [date(2026, 8, 27)]}

    monkeypatch.setattr(mod.yf, "Ticker", _Boom)
    result = mod.EarningsCalendarProvider().collect(_ctx(settings))
    assert "NVDA:error:RuntimeError" in result.safe_detail
    assert not [f for f in result.facts if f.subject == "NVDA"]
    assert result.status == "PARTIAL"
