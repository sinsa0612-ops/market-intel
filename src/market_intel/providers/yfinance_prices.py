"""yfinance — price_close/volume for the full 28-symbol universe, plus
price_after_hours for US Core equities where Yahoo exposes it.

yfinance manages its own HTTP session/cookies internally, so per spec A6
it is exempt from SafeHttp (direct-API-call rule doesn't apply to it)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from ..universe import UNIVERSE

US_TZ = ZoneInfo("America/New_York")
KR_TZ = ZoneInfo("Asia/Seoul")
US_CLOSE_LOCAL = (16, 0)
KR_CLOSE_LOCAL = (15, 30)

# US individual equities only (spec: after-hours attempted for US Core 12,
# not indices/fx/commodities and not .KS tickers).
US_CORE_EQUITIES = [
    m["symbol"] for m in UNIVERSE if m["core16"] and m["market"] == "US" and m["asset_type"] == "equity"
]


def _local_close_utc(symbol_meta: dict, trade_date: pd.Timestamp) -> str:
    tz, (hh, mm) = (KR_TZ, KR_CLOSE_LOCAL) if symbol_meta["market"] == "KR" else (US_TZ, US_CLOSE_LOCAL)
    local_dt = datetime(trade_date.year, trade_date.month, trade_date.day, hh, mm, tzinfo=tz)
    return local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _valid_bars(frame: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Series]]:
    """Every downloaded trading day that has a usable close, oldest first.

    All of them become facts: the point of a PIT store is history depth, and
    a day with no close is skipped rather than stored as a zero."""
    if frame is None or frame.empty:
        return []
    close = frame.get("Close")
    if close is None:
        return []
    valid = close.dropna()
    return [(idx, frame.loc[idx]) for idx in valid.index]


class YFinanceProvider:
    name = "yfinance"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        symbols = [m["symbol"] for m in ctx.universe]
        meta_by_symbol = {m["symbol"]: m for m in ctx.universe}

        try:
            data = yf.download(
                symbols, period="5d", interval="1d", group_by="ticker",
                progress=False, auto_adjust=False, threads=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(status="ERROR", reason_code="network_error", safe_detail=str(exc)[:300])

        if data is None or data.empty:
            return ProviderResult(status="NO_DATA", reason_code="empty_response")

        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for symbol in symbols:
            meta = meta_by_symbol[symbol]
            try:
                frame = data[symbol] if len(symbols) > 1 else data
            except (KeyError, TypeError):
                missing.append(symbol)
                continue

            bars = _valid_bars(frame)
            if not bars:
                missing.append(symbol)
                continue
            currency = "KRW" if meta["market"] == "KR" else "USD"

            for trade_date, series in bars:
                event_at = _local_close_utc(meta, trade_date)
                date_str = trade_date.strftime("%Y-%m-%d")
                external_id = f"{symbol}:{date_str}"

                payload = json.dumps(
                    {
                        "symbol": symbol, "date": date_str,
                        "open": _num(series.get("Open")), "high": _num(series.get("High")),
                        "low": _num(series.get("Low")), "close": _num(series.get("Close")),
                        "volume": _num(series.get("Volume")),
                    },
                    ensure_ascii=False,
                )
                raw_items.append(
                    RawItem(
                        external_id=external_id,
                        source_published_at=event_at,
                        safe_source_url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                        payload=payload,
                        fetch_status="ok",
                    )
                )
                facts.append(
                    FactCandidate(
                        raw_ref=external_id, subject=symbol, category="price", metric="price_close",
                        event_at=event_at, market=meta["market"], country=meta["country"],
                        value_num=_num(series.get("Close")), unit=meta.get("unit") or currency,
                        publisher="Yahoo Finance", data_status="source_verified",
                        extra={"asset_type": meta["asset_type"]},
                    )
                )
                volume = _num(series.get("Volume"))
                # Indices/FX report a 0 volume — a placeholder, not an observation.
                if volume is not None and volume > 0:
                    facts.append(
                        FactCandidate(
                            raw_ref=external_id, subject=symbol, category="price", metric="volume",
                            event_at=event_at, market=meta["market"], country=meta["country"],
                            value_num=volume, unit="shares",
                            publisher="Yahoo Finance", data_status="source_verified",
                        )
                    )

            if symbol in US_CORE_EQUITIES:
                latest_date_str = bars[-1][0].strftime("%Y-%m-%d")
                ah_fact, ah_raw = self._after_hours(symbol, meta, latest_date_str)
                if ah_fact is not None:
                    raw_items.append(ah_raw)
                    facts.append(ah_fact)
                else:
                    missing.append(f"{symbol}:after_hours")

        if not facts:
            return ProviderResult(status="NO_DATA", reason_code="empty_response", raw_items=raw_items)

        status = "PARTIAL" if missing else "OK"
        safe_detail = f"missing: {', '.join(missing)}" if missing else ""
        return ProviderResult(status=status, reason_code=None, raw_items=raw_items, facts=facts, safe_detail=safe_detail)

    def _after_hours(self, symbol: str, meta: dict, date_str: str):
        try:
            info = yf.Ticker(symbol).info
        except Exception:  # noqa: BLE001
            return None, None
        price = info.get("postMarketPrice")
        ts = info.get("postMarketTime")
        if price is None or ts is None:
            return None, None
        event_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
        external_id = f"{symbol}:{date_str}:ah"
        raw = RawItem(
            external_id=external_id,
            source_published_at=event_at,
            safe_source_url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            payload=json.dumps({"symbol": symbol, "postMarketPrice": price, "postMarketTime": ts}),
            fetch_status="ok",
        )
        fact = FactCandidate(
            raw_ref=external_id, subject=symbol, category="price", metric="price_after_hours",
            event_at=event_at, market=meta["market"], country=meta["country"],
            value_num=float(price), unit="USD", publisher="Yahoo Finance",
            data_status="source_verified", session_label="after_hours",
        )
        return fact, raw


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f
