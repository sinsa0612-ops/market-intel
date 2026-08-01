"""Fixtures for the 2단계-B (`interp/`) tests — ST1's thesis engine and ST2's
interpretation generator share this directory.

Merged from the two subtasks' own conftests. `price_fc`/`macro_fc` keep ST2's
`unit=` parameter (ST1 passed the same defaults positionally, so the wider
signature is compatible) and ST1's `market`/`country` overrides, which the
thesis tests need for Korean symbols.

Seeding goes through the real write path (`db.insert_raw_snapshot` +
`db.upsert_fact`), never a hand-written SQL INSERT, so both engines are
exercised against the same DB shape the real collectors produce.
"""
from __future__ import annotations

import pytest

from market_intel import db as db_mod
from market_intel.engine import _fact_id
from market_intel.models import FactCandidate, RawItem
from market_intel.reporting.model import CalendarRow, FactRow, Interpretation, MissingItem, Report


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "market_intel.db")
    db_mod.init_db(db_path)
    c = db_mod.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def raw_dir(tmp_path) -> str:
    d = tmp_path / "raw"
    d.mkdir()
    return str(d)


def seed_fact(conn, raw_dir: str, provider: str, fc: FactCandidate, known_at: str) -> str:
    """Real write path (`insert_raw_snapshot` + `upsert_fact`), same as
    `tests/reporting/conftest.py` — never a hand-written SQL INSERT."""
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


def price_fc(
    symbol: str, event_at: str, value: float, unit: str = "USD",
    market: str = "US", country: str = "US",
) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{symbol}:{event_at}", subject=symbol, category="price", metric="price_close",
        event_at=event_at, market=market, country=country, value_num=value, unit=unit,
        data_status="source_verified",
    )


def macro_fc(
    subject: str, event_at: str, value: float, unit: str = "percent",
    market: str = "US", country: str = "US",
) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{subject}:{event_at}", subject=subject, category="macro", metric="value",
        event_at=event_at, market=market, country=country, value_num=value, unit=unit,
        data_status="source_verified",
    )


def fin_fc(
    subject: str, metric: str, event_at: str, value: float,
    market: str = "US", country: str = "US",
) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{subject}:{metric}:{event_at}", subject=subject, category="financials",
        metric=metric, event_at=event_at, market=market, country=country, value_num=value,
        unit="USD", data_status="source_verified",
    )


def make_fact_row(
    label: str, value: str, comparison: str, *,
    subject: str = "TEST", metric: str = "value", known_at: str = "2026-08-01T00:00:00+00:00",
    raw_value: float | None = None, data_status: str = "source_verified",
) -> FactRow:
    return FactRow(
        label=label, value=value, comparison=comparison, source_url="https://example.test/x",
        data_status=data_status, known_at=known_at, subject=subject, metric=metric, raw_value=raw_value,
    )


def make_report(
    *,
    report_type: str = "weekly_review",
    report_date: str = "2026-08-01",
    cutoff_utc: str = "2026-08-01T22:15:00+00:00",
    headline: str = "KOSPI 3100.00(+0.5%) · S&P500 6000.00(+0.2%)",
    facts: list[FactRow] | None = None,
    market_reaction: list[FactRow] | None = None,
    events: list[CalendarRow] | None = None,
    schedule_changes: list[CalendarRow] | None = None,
    missing: list[MissingItem] | None = None,
) -> Report:
    return Report(
        report_type=report_type,
        report_date=report_date,
        cutoff_kst=cutoff_utc,
        cutoff_utc=cutoff_utc,
        generated_at=cutoff_utc,
        title="테스트 리포트",
        headline=headline,
        data_status="source_verified",
        facts=facts if facts is not None else [
            make_fact_row("미국 실업률", "4.20%", "직전 관측 대비 +0.00%",
                          subject="UNRATE", metric="value", known_at=cutoff_utc, raw_value=4.20),
        ],
        market_reaction=market_reaction if market_reaction is not None else [],
        events=events if events is not None else [],
        schedule_changes=schedule_changes if schedule_changes is not None else [],
        missing=missing if missing is not None else [],
        interpretation=Interpretation(),
        meta={},
    )
