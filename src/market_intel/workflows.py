"""Workflow -> provider-name list (spec A8, fixed)."""
from __future__ import annotations

WORKFLOWS: dict[str, list[str]] = {
    "morning": ["yfinance", "sec_edgar", "sec_edgar_13f", "fred"],
    "close": ["yfinance", "pykrx", "ecos", "dart"],
    "all": ["yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart"],
}
