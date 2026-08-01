"""Fixture seeding for reporting/ tests: goes through the real write path
(`db.insert_raw_snapshot` + `db.upsert_fact`, the same two calls
`engine._persist` makes), never a hand-written SQL INSERT — a build.py bug
that reads the DB "shape" wrong would go undetected by a fixture that
side-steps the same code the real collectors use."""
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


def price_fc(symbol: str, market: str, country: str, event_at: str, value: float, unit: str = "USD") -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{symbol}:{event_at}", subject=symbol, category="price", metric="price_close",
        event_at=event_at, market=market, country=country, value_num=value, unit=unit,
        data_status="source_verified",
    )
