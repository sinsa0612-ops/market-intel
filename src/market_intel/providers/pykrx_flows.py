"""pykrx — KOSPI/KOSDAQ investor net-buy flows (market-level and Korean
Core 4 tickers). pykrx wraps KRX's own scraping endpoints internally and is
exempt from SafeHttp per spec A6.

[ASSUMPTION] As of this run, KRX's investor-composition endpoints
(get_market_net_purchases_of_equities / get_market_trading_value_by_investor)
return an empty/non-JSON body for anonymous requests in this environment —
pykrx logs "KRX 로그인 실패" at import time and the calls raise/return empty.
Plain OHLCV endpoints (no investor breakdown) still work. This looks like a
KRX-side auth wall unrelated to any of the three key-bearing providers in
scope here, so it is reported honestly as PARTIAL/NO_DATA rather than
papered over — see HANDOFF for the data-gap note."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pykrx import stock

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from ..universe import KR_CORE_SYMBOLS

KR_TZ = ZoneInfo("Asia/Seoul")
MARKETS = ["KOSPI", "KOSDAQ"]
INVESTOR_METRIC = {"외국인": "net_buy_foreign", "기관합계": "net_buy_institution", "개인": "net_buy_individual"}


def _recent_trading_date() -> str:
    """Best-effort recent business day: pykrx's own helper depends on the
    same (currently broken) calendar endpoint, so fall back to scanning
    OHLCV for the last populated date within a week."""
    now_kst = datetime.now(KR_TZ)
    for back in range(0, 8):
        d = (now_kst - timedelta(days=back)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv_by_date(d, d, "005930")
        if df is not None and not df.empty:
            return d
    return now_kst.strftime("%Y%m%d")


def _event_at(date_str: str) -> str:
    local = datetime.strptime(date_str, "%Y%m%d").replace(hour=15, minute=30, tzinfo=KR_TZ)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


class PykrxProvider:
    name = "pykrx"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        try:
            date_str = _recent_trading_date()
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(status="ERROR", reason_code="network_error", safe_detail=str(exc)[:300])

        event_at = _event_at(date_str)
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        failures: list[str] = []

        for market in MARKETS:
            for investor, metric in INVESTOR_METRIC.items():
                try:
                    df = stock.get_market_net_purchases_of_equities(date_str, date_str, market, investor)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{market}:{investor}:{exc.__class__.__name__}")
                    continue
                if df is None or df.empty or "순매수거래대금" not in getattr(df, "columns", []):
                    failures.append(f"{market}:{investor}:empty")
                    continue
                total = float(df["순매수거래대금"].sum())
                external_id = f"market:{market}:{investor}:{date_str}"
                raw_items.append(
                    RawItem(
                        external_id=external_id, source_published_at=event_at,
                        safe_source_url=f"https://data.krx.co.kr/investor-flow/{market}",
                        payload=json.dumps({"market": market, "investor": investor, "date": date_str, "total": total}),
                    )
                )
                facts.append(
                    FactCandidate(
                        raw_ref=external_id, subject=market, category="flow", metric=metric,
                        event_at=event_at, market="KR", country="KR", value_num=total, unit="KRW",
                        publisher="KRX", data_status="source_verified",
                    )
                )

        for ticker in KR_CORE_SYMBOLS:
            code = ticker.split(".")[0]
            for investor, metric in INVESTOR_METRIC.items():
                try:
                    df = stock.get_market_trading_value_by_investor(date_str, date_str, code)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{code}:{investor}:{exc.__class__.__name__}")
                    continue
                if df is None or df.empty or investor not in getattr(df, "index", []):
                    failures.append(f"{code}:{investor}:empty")
                    continue
                value = float(df.loc[investor].get("순매수", 0))
                external_id = f"ticker:{code}:{investor}:{date_str}"
                raw_items.append(
                    RawItem(
                        external_id=external_id, source_published_at=event_at,
                        safe_source_url=f"https://data.krx.co.kr/investor-flow/{code}",
                        payload=json.dumps({"ticker": code, "investor": investor, "date": date_str, "value": value}),
                    )
                )
                facts.append(
                    FactCandidate(
                        raw_ref=external_id, subject=ticker, category="flow", metric=metric,
                        event_at=event_at, market="KR", country="KR", value_num=value, unit="KRW",
                        publisher="KRX", data_status="source_verified",
                    )
                )

        if not facts:
            detail = "investor-flow endpoints returned empty for all markets/tickers (KRX auth wall — see HANDOFF); " + "; ".join(failures[:6])
            return ProviderResult(status="NO_DATA", reason_code="empty_response", raw_items=raw_items, safe_detail=detail[:400])

        status = "PARTIAL" if failures else "OK"
        safe_detail = ("some flows unavailable: " + "; ".join(failures[:6])) if failures else ""
        return ProviderResult(status=status, reason_code=None, raw_items=raw_items, facts=facts, safe_detail=safe_detail[:400])
