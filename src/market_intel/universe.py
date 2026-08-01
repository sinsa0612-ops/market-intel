"""Observed universe — symbols fixed by spec A9. Do not add symbols outside
this list (Core 16 boundary is enforced by convention, not code, per the
task's "Core 16 밖 종목 금지" boundary)."""
from __future__ import annotations


def _sym(
    symbol: str, market: str, country: str, asset_type: str, name: str,
    core16: bool = False, unit: str | None = None,
) -> dict:
    """`unit` overrides the exchange currency for instruments that are not
    quoted in money. An index is points, a yield is percent, and KRW=X is
    won-per-dollar (not dollars) — labelling any of them with the listing
    currency makes a report say "US 10Y at 4.2 USD". None = use the
    exchange currency reported by the price source."""
    return {
        "symbol": symbol,
        "market": market,
        "country": country,
        "asset_type": asset_type,
        "name": name,
        "core16": core16,
        "unit": unit,
    }


UNIVERSE: list[dict] = [
    _sym("MSFT", "US", "US", "equity", "Microsoft", True),
    _sym("GOOGL", "US", "US", "equity", "Alphabet", True),
    _sym("AMZN", "US", "US", "equity", "Amazon", True),
    _sym("META", "US", "US", "equity", "Meta Platforms", True),
    _sym("NVDA", "US", "US", "equity", "NVIDIA", True),
    _sym("TSM", "US", "TW", "equity", "TSMC ADR", True),
    _sym("005930.KS", "KR", "KR", "equity", "Samsung Electronics", True),
    _sym("000660.KS", "KR", "KR", "equity", "SK Hynix", True),
    _sym("JPM", "US", "US", "equity", "JPMorgan Chase", True),
    _sym("105560.KS", "KR", "KR", "equity", "KB Financial Group", True),
    _sym("XOM", "US", "US", "equity", "Exxon Mobil", True),
    _sym("AEP", "US", "US", "equity", "American Electric Power", True),
    _sym("CAT", "US", "US", "equity", "Caterpillar", True),
    _sym("WMT", "US", "US", "equity", "Walmart", True),
    _sym("005380.KS", "KR", "KR", "equity", "Hyundai Motor", True),
    _sym("LLY", "US", "US", "equity", "Eli Lilly", True),
    _sym("^KS11", "KR", "KR", "index", "KOSPI", unit="point"),
    _sym("^KQ11", "KR", "KR", "index", "KOSDAQ", unit="point"),
    _sym("^GSPC", "US", "US", "index", "S&P 500", unit="point"),
    _sym("^IXIC", "US", "US", "index", "Nasdaq Composite", unit="point"),
    _sym("^SOX", "US", "US", "index", "PHLX Semiconductor", unit="point"),
    _sym("^VIX", "US", "US", "index", "CBOE VIX", unit="point"),
    _sym("^TNX", "US", "US", "rate", "US 10Y Treasury Yield", unit="percent"),
    _sym("KRW=X", "US", "US", "fx", "USD/KRW", unit="KRW"),
    _sym("DX-Y.NYB", "US", "US", "fx", "US Dollar Index", unit="point"),
    _sym("CL=F", "US", "US", "commodity", "WTI Crude Oil"),
    _sym("GC=F", "US", "US", "commodity", "Gold"),
    _sym("HG=F", "US", "US", "commodity", "Copper"),
]

CORE16: list[dict] = [m for m in UNIVERSE if m["core16"]]
CORE16_SYMBOLS: list[str] = [m["symbol"] for m in CORE16]

# Korean Core 4 (subset of Core16) — used by pykrx investor-flow and DART filings.
KR_CORE4_SYMBOLS: list[str] = ["005930.KS", "000660.KS", "105560.KS", "005380.KS"]
