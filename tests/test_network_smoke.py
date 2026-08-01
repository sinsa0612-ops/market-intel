"""Real-network smoke tests (spec A10) — the three no-key providers plus
the CLI's own end-to-end path. Slower; excluded by `-m "not network"`."""
import subprocess
import sys

import pytest

from market_intel import db as db_mod
from market_intel.engine import run_collect
from market_intel.providers.sec_edgar import SecEdgarProvider
from market_intel.providers.yfinance_prices import YFinanceProvider
from market_intel.universe import CORE16
from market_intel.models import CollectContext


@pytest.mark.network
def test_yfinance_gets_core16_close_prices(settings):
    import logging

    ctx = CollectContext(
        cutoff=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        settings=settings, http=lambda name: None, universe=CORE16, logger=logging.getLogger("t"),
    )
    result = YFinanceProvider().collect(ctx)
    assert result.status in ("OK", "PARTIAL")
    subjects = {f.subject for f in result.facts if f.metric == "price_close"}
    covered = sum(1 for s in CORE16 if s["symbol"] in subjects)
    assert covered == 16, f"expected all 16 Core symbols to have a close price, got {covered}"


@pytest.mark.network
def test_sec_edgar_resolves_msft_and_detects_filing(settings):
    import logging
    from datetime import datetime, timezone

    from market_intel.http_client import SafeHttp

    ctx = CollectContext(
        cutoff=datetime.now(timezone.utc), now=datetime.now(timezone.utc), settings=settings,
        http=lambda name: SafeHttp(name, settings), universe=CORE16, logger=logging.getLogger("t"),
    )
    result = SecEdgarProvider().collect(ctx)
    assert result.status in ("OK", "PARTIAL")
    msft_filings = [f for f in result.facts if f.subject == "MSFT" and f.metric == "filing_event"]
    assert len(msft_filings) == 1


@pytest.mark.network
def test_full_morning_workflow_reaches_facts_and_core16_coverage(settings):
    db_mod.init_db(settings.db_path)
    from market_intel.providers import PROVIDERS

    result = run_collect(settings, CORE16, PROVIDERS, "morning", None)
    assert result["providers"]["yfinance"]["status"] in ("OK", "PARTIAL")

    conn = db_mod.connect(settings.db_path)
    total = conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"]
    conn.close()
    assert total >= 50  # morning alone (yfinance+sec_edgar); full 'all' workflow clears 100+ (see result.md)
