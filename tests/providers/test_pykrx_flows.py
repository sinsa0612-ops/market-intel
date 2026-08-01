"""Offline unit tests for the pykrx investor-flow provider (no network).

KRX currently gates the investor-flow endpoints behind a login (see HANDOFF),
so the live run reports NO_DATA. These tests pin both halves of that contract:
a blocked source must produce zero facts and an explicit reason — never a
placeholder value — and the mapping must be correct for the day the source
comes back.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pytest

from market_intel.models import CollectContext
from market_intel.providers import pykrx_flows as mod

TRADING_DAY = "20260731"


def _ctx(settings):
    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings, http=lambda name: None,
        universe=[], logger=logging.getLogger("test"),
    )


@pytest.fixture
def fixed_trading_day(monkeypatch):
    monkeypatch.setattr(mod, "_recent_trading_date", lambda: TRADING_DAY)


def test_krx_auth_wall_yields_no_data_and_no_invented_values(settings, fixed_trading_day, monkeypatch):
    def blocked(*args, **kwargs):
        raise KeyError("거래대금")  # what pykrx raises once KRX rejects the session

    monkeypatch.setattr(mod.stock, "get_market_net_purchases_of_equities", blocked)
    monkeypatch.setattr(mod.stock, "get_market_trading_value_by_investor", blocked)

    result = mod.PykrxProvider().collect(_ctx(settings))

    assert result.status == "NO_DATA"
    assert result.reason_code == "empty_response"
    assert result.facts == [], "a blocked source must never produce facts"
    assert "KRX" in result.safe_detail and "KeyError" in result.safe_detail


def test_empty_frame_is_reported_not_stored_as_zero(settings, fixed_trading_day, monkeypatch):
    monkeypatch.setattr(mod.stock, "get_market_net_purchases_of_equities", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(mod.stock, "get_market_trading_value_by_investor", lambda *a, **k: pd.DataFrame())

    result = mod.PykrxProvider().collect(_ctx(settings))

    assert result.status == "NO_DATA"
    assert result.facts == []
    assert "empty" in result.safe_detail


def test_investor_flows_map_to_facts_when_the_source_answers(settings, fixed_trading_day, monkeypatch):
    market_df = pd.DataFrame({"순매수거래대금": [100.0, -40.0]}, index=["005930", "000660"])
    ticker_df = pd.DataFrame(
        {"순매수": [11.0, 22.0, -33.0]}, index=["외국인", "기관합계", "개인"]
    )
    monkeypatch.setattr(mod.stock, "get_market_net_purchases_of_equities", lambda *a, **k: market_df)
    monkeypatch.setattr(mod.stock, "get_market_trading_value_by_investor", lambda *a, **k: ticker_df)

    result = mod.PykrxProvider().collect(_ctx(settings))

    assert result.status == "OK"
    kospi_foreign = [f for f in result.facts if f.subject == "KOSPI" and f.metric == "net_buy_foreign"]
    assert len(kospi_foreign) == 1
    assert kospi_foreign[0].value_num == 60.0  # summed across the market
    assert kospi_foreign[0].event_at == "2026-07-31T06:30:00+00:00"  # 15:30 KST -> UTC
    assert kospi_foreign[0].market == "KR" and kospi_foreign[0].unit == "KRW"

    samsung = [f for f in result.facts if f.subject == "005930.KS" and f.metric == "net_buy_institution"]
    assert len(samsung) == 1 and samsung[0].value_num == 22.0

    metrics = {f.metric for f in result.facts}
    assert metrics == {"net_buy_foreign", "net_buy_institution", "net_buy_individual"}
