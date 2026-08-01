"""Offline unit tests for the yfinance provider (no network).

Pins the two things the live smoke test cannot check cheaply:
  * every valid trading day in the downloaded window becomes a fact
    (PIT history depth — spec ST2 asks for the last 5 trading days),
  * exchange-local close times are converted to UTC per market.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pytest

from market_intel.models import CollectContext
from market_intel.providers import yfinance_prices as mod

DATES = pd.to_datetime(["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"])

UNIVERSE = [
    {"symbol": "MSFT", "market": "US", "country": "US", "asset_type": "equity", "name": "Microsoft", "core16": True},
    {"symbol": "005930.KS", "market": "KR", "country": "KR", "asset_type": "equity", "name": "Samsung", "core16": True},
    {"symbol": "^GSPC", "market": "US", "country": "US", "asset_type": "index", "name": "S&P 500", "core16": False},
]


def _frame(closes, volumes):
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": volumes,
        },
        index=DATES,
    )


@pytest.fixture
def fake_download(monkeypatch):
    data = pd.concat(
        {
            "MSFT": _frame([100.0, 101.0, 102.0, 103.0, 104.0], [10, 11, 12, 13, 14]),
            # one hole in the middle: a NaN close must be skipped, not stored as 0
            "005930.KS": _frame([70000.0, float("nan"), 72000.0, 73000.0, 74000.0], [1, 2, 3, 4, 5]),
            # indices report volume 0 — meaningless, must not become a fact
            "^GSPC": _frame([5000.0, 5010.0, 5020.0, 5030.0, 5040.0], [0, 0, 0, 0, 0]),
        },
        axis=1,
    )
    monkeypatch.setattr(mod.yf, "download", lambda *a, **k: data)
    return data


@pytest.fixture
def fake_ticker(monkeypatch):
    class _T:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            if self.symbol == "MSFT":
                return {"postMarketPrice": 105.5, "postMarketTime": 1785700800}
            return {}

    monkeypatch.setattr(mod.yf, "Ticker", _T)


def _ctx(settings):
    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings, http=lambda name: None,
        universe=UNIVERSE, logger=logging.getLogger("test"),
    )


def test_every_valid_trading_day_becomes_a_fact(settings, fake_download, fake_ticker):
    result = mod.YFinanceProvider().collect(_ctx(settings))

    msft_closes = [f for f in result.facts if f.subject == "MSFT" and f.metric == "price_close"]
    assert len(msft_closes) == 5, "all 5 downloaded trading days must be persisted, not just the latest"
    assert [f.value_num for f in msft_closes] == [100.0, 101.0, 102.0, 103.0, 104.0]

    samsung_closes = [f for f in result.facts if f.subject == "005930.KS" and f.metric == "price_close"]
    assert len(samsung_closes) == 4, "the NaN close day must be skipped, not stored"


def test_raw_snapshot_per_symbol_and_day(settings, fake_download, fake_ticker):
    result = mod.YFinanceProvider().collect(_ctx(settings))
    ids = {r.external_id for r in result.raw_items}
    assert "MSFT:2026-07-27" in ids and "MSFT:2026-07-31" in ids
    assert "005930.KS:2026-07-28" not in ids, "no snapshot for a day with no usable close"


def test_close_event_time_is_exchange_local_converted_to_utc(settings, fake_download, fake_ticker):
    result = mod.YFinanceProvider().collect(_ctx(settings))
    msft = next(f for f in result.facts if f.subject == "MSFT" and f.metric == "price_close" and f.event_at.startswith("2026-07-31"))
    samsung = next(f for f in result.facts if f.subject == "005930.KS" and f.metric == "price_close" and f.event_at.startswith("2026-07-31"))
    assert msft.event_at == "2026-07-31T20:00:00+00:00"      # 16:00 America/New_York (EDT)
    assert samsung.event_at == "2026-07-31T06:30:00+00:00"   # 15:30 Asia/Seoul


def test_zero_volume_is_not_stored(settings, fake_download, fake_ticker):
    result = mod.YFinanceProvider().collect(_ctx(settings))
    assert not [f for f in result.facts if f.subject == "^GSPC" and f.metric == "volume"]
    assert len([f for f in result.facts if f.subject == "MSFT" and f.metric == "volume"]) == 5


def test_after_hours_is_recorded_once_for_the_latest_session(settings, fake_download, fake_ticker):
    result = mod.YFinanceProvider().collect(_ctx(settings))
    ah = [f for f in result.facts if f.metric == "price_after_hours"]
    assert len(ah) == 1
    assert ah[0].subject == "MSFT"
    assert ah[0].session_label == "after_hours"
    assert ah[0].value_num == 105.5


def test_empty_download_is_no_data_not_a_crash(settings, monkeypatch):
    monkeypatch.setattr(mod.yf, "download", lambda *a, **k: pd.DataFrame())
    result = mod.YFinanceProvider().collect(_ctx(settings))
    assert result.status == "NO_DATA"
    assert result.reason_code == "empty_response"
    assert result.facts == []
