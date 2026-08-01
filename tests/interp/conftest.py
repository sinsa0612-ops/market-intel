"""Fixture seeding for interp/ tests: same pattern as tests/reporting/conftest.py
- goes through the real write path (`db.insert_raw_snapshot` + `db.upsert_fact`),
never a hand-written SQL INSERT, so the thesis engine is exercised against the
same DB shape the real collectors produce."""
from __future__ import annotations

from market_intel import db as db_mod
from market_intel.engine import _fact_id
from market_intel.models import FactCandidate, RawItem


def seed_fact(conn, raw_dir: str, provider: str, fc: FactCandidate, known_at: str) -> str:
    raw = RawItem(
        external_id=f"{provider}:{fc.subject}:{fc.metric}:{fc.event_at}",
        source_published_at=fc.event_at,
        safe_source_url=fc.safe_source_url or "https://example.test/data",
        payload="{}",
    )
    snapshot_id = db_mod.insert_raw_snapshot(conn, raw_dir, provider, raw)
    fact_id = _fact_id(provider, fc)
    db_mod.upsert_fact(conn, fact_id, snapshot_id, known_at, fc)
    conn.commit()
    return fact_id


def price_fc(symbol: str, event_at: str, value: float, market: str = "US", country: str = "US") -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{symbol}:{event_at}", subject=symbol, category="price", metric="price_close",
        event_at=event_at, market=market, country=country, value_num=value, unit="USD",
        data_status="source_verified",
    )


def macro_fc(subject: str, event_at: str, value: float) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{subject}:{event_at}", subject=subject, category="macro", metric="value",
        event_at=event_at, market="US", country="US", value_num=value, unit="index",
        data_status="source_verified",
    )


def fin_fc(subject: str, metric: str, event_at: str, value: float) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{subject}:{metric}:{event_at}", subject=subject, category="financials", metric=metric,
        event_at=event_at, market="US", country="US", value_num=value, unit="USD",
        data_status="source_verified",
    )
