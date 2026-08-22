"""업종 ETF 보유종목 비중 수집기 — 오프라인 단위 시험.

이 수집기가 있는 이유는 `providers/yfinance_holdings.py` 모듈 주석에 있다. 요점:
사각지대 신고가 **단정 대신 계산**을 하려면 비중이 필요하고, 그 비중을 사람이
기억으로 적으면 그 자체가 환각 표면이 된다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pytest

from market_intel.models import CollectContext
from market_intel.providers import yfinance_holdings as mod


def _holdings(pairs) -> pd.DataFrame:
    """`funds_data.top_holdings`의 모양: 심볼이 색인, 비중은 **비율(0~1)**."""
    return pd.DataFrame(
        {"Name": [n for _s, n, _w in pairs], "Holding Percent": [w for _s, _n, w in pairs]},
        index=pd.Index([s for s, _n, _w in pairs], name="Symbol"),
    )


class _Funds:
    def __init__(self, frame): self.top_holdings = frame


class _Ticker:
    def __init__(self, frame): self.funds_data = _Funds(frame)


UNIVERSE = [
    {"symbol": "XLV", "asset_type": "sector_index", "market": "US", "country": "US"},
    {"symbol": "227540.KS", "asset_type": "sector_index", "market": "KR", "country": "KR"},
    {"symbol": "LLY", "asset_type": "equity", "market": "US", "country": "US"},
]


def _ctx(settings, universe=None):
    now = datetime.now(timezone.utc)
    return CollectContext(cutoff=now, now=now, settings=settings, http=lambda n: None,
                          universe=universe if universe is not None else UNIVERSE,
                          logger=logging.getLogger("test"))


def _patch(monkeypatch, table: dict):
    """`table`: 심볼 -> DataFrame | None | Exception."""
    def ticker(symbol):
        v = table.get(symbol)
        if isinstance(v, Exception):
            raise v
        return _Ticker(v)
    monkeypatch.setattr(mod.yf, "Ticker", ticker)


REAL = _holdings([("LLY", "Eli Lilly and Co", 0.154720), ("JNJ", "Johnson & Johnson", 0.105057)])


def test_weight_is_stored_as_percent_not_ratio(settings, monkeypatch):
    """원장의 `unit="percent"`가 그 뜻이다. 비율 그대로 넣으면 기여도가 100배
    작아지고, 사각지대가 통째로 과장된다."""
    _patch(monkeypatch, {"XLV": REAL, "227540.KS": None})
    facts = mod.YFinanceHoldingsProvider().collect(_ctx(settings)).facts
    lly = next(f for f in facts if f.subject == "XLV/LLY")
    assert lly.value_num == pytest.approx(15.472)
    assert lly.unit == "percent" and lly.metric == "holding_weight"


def test_subject_pairs_the_etf_with_the_holding(settings, monkeypatch):
    """`engine._fact_id`가 provider:종목:항목:날짜라, ETF만 쓰면 한 업종의 열
    종목이 같은 이름을 갖는다 (13F 보유내역이 쓰는 것과 같은 수법)."""
    _patch(monkeypatch, {"XLV": REAL, "227540.KS": None})
    facts = mod.YFinanceHoldingsProvider().collect(_ctx(settings)).facts
    assert {f.subject for f in facts} == {"XLV/LLY", "XLV/JNJ"}
    assert all(f.extra["etf"] == "XLV" for f in facts)


def test_an_etf_without_holdings_is_missing_not_zero(settings, monkeypatch):
    """한국 업종 ETF 14개가 전부 여기 해당한다(실측 2026-08-21). **결측이지
    "비중 0"이 아니다** — 0으로 새면 그 업종은 영원히 "우리가 아무것도 설명하지
    못한다"가 되고, 사각지대가 실제보다 크게 보고된다."""
    _patch(monkeypatch, {"XLV": REAL, "227540.KS": None})
    result = mod.YFinanceHoldingsProvider().collect(_ctx(settings))
    assert not [f for f in result.facts if f.subject.startswith("227540")]
    assert "227540.KS:no_holdings" in result.safe_detail
    assert result.status == "PARTIAL"


def test_one_etf_failing_does_not_take_the_rest(settings, monkeypatch):
    _patch(monkeypatch, {"XLV": REAL, "227540.KS": RuntimeError("boom")})
    result = mod.YFinanceHoldingsProvider().collect(_ctx(settings))
    assert [f.subject for f in result.facts] == ["XLV/LLY", "XLV/JNJ"]
    assert "holdings_error:RuntimeError" in result.safe_detail


def test_only_sector_indices_are_asked(settings, monkeypatch):
    """개별 기업에 보유내역을 묻지 않는다 — 호출만 낭비하고 답도 없다."""
    asked = []
    def ticker(symbol):
        asked.append(symbol)
        return _Ticker(REAL if symbol == "XLV" else None)
    monkeypatch.setattr(mod.yf, "Ticker", ticker)
    mod.YFinanceHoldingsProvider().collect(_ctx(settings))
    assert "LLY" not in asked and "XLV" in asked


def test_nothing_anywhere_is_no_data_not_ok(settings, monkeypatch):
    _patch(monkeypatch, {"XLV": None, "227540.KS": None})
    result = mod.YFinanceHoldingsProvider().collect(_ctx(settings))
    assert result.status == "NO_DATA" and not result.facts


def test_event_at_is_the_collection_day_not_a_claimed_as_of(settings, monkeypatch):
    """yfinance는 **언제 기준 비중인지 말해 주지 않는다.** 그래서 "우리가 그날
    관측한 비중"이라고만 주장한다 — 과거 어느 시점의 비중이었다고 하지 않는다.

    딸려오는 성질이 옳다: 지난 리포트를 다시 만들면 그 차단선에 비중 관측이
    없어 기여도가 계산되지 않고, 검출기는 계산 없이 말할 수 있는 것만 말한다."""
    _patch(monkeypatch, {"XLV": REAL, "227540.KS": None})
    facts = mod.YFinanceHoldingsProvider().collect(_ctx(settings)).facts
    today = datetime.now(timezone.utc).date().isoformat()
    assert all(f.event_at == f"{today}T00:00:00+00:00" for f in facts)


def test_a_broken_percent_column_is_skipped_not_guessed(settings, monkeypatch):
    _patch(monkeypatch, {"XLV": _holdings([("LLY", "Eli Lilly", None), ("JNJ", "J&J", 0.1)]),
                         "227540.KS": None})
    facts = mod.YFinanceHoldingsProvider().collect(_ctx(settings)).facts
    assert [f.subject for f in facts] == ["XLV/JNJ"]
