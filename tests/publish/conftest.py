"""Fixtures for the ST3 (publish-and-schedule) acceptance tests.

Reports are built as real `Report` objects and serialised with the very
same `to_json()` the `report` CLI writes, so `site.py`/`obsidian.py` are
exercised against the exact bytes they will meet in `reports/` — not
against a hand-shaped dict that already agrees with whatever the reader
happens to expect.

Calendar facts go through `db.insert_raw_snapshot` + `db.upsert_fact`
(the pair `engine._persist` calls), never a raw SQL INSERT: the whole
point of the cutoff tests below is that the PIT read path is honoured,
and a fixture that side-steps the write path can't prove that.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_intel import db as db_mod
from market_intel.engine import _fact_id
from market_intel.models import FactCandidate, RawItem
from market_intel.reporting.cutoff import KST, cutoff_for
from market_intel.reporting.model import (
    CalendarRow,
    FactRow,
    Interpretation,
    MissingItem,
    Report,
)


def make_report(
    report_type: str = "morning",
    report_date: str = "2026-08-01",
    *,
    facts: list[FactRow] | None = None,
    events: list[CalendarRow] | None = None,
    late: bool = False,
    data_status: str = "source_verified",
    title: str | None = None,
) -> Report:
    d = date.fromisoformat(report_date)
    try:
        cutoff = cutoff_for(report_type, d)
    except ValueError:
        cutoff = datetime(d.year, d.month, d.day, 9, 0, tzinfo=KST)
    return Report(
        report_type=report_type,
        report_date=report_date,
        cutoff_kst=cutoff.isoformat(),
        cutoff_utc=db_mod.iso_utc(cutoff),
        generated_at=db_mod.iso_utc(cutoff),
        title=title or f"{report_type} 리포트",
        headline="KOSPI 3100(+0.5%) · S&P500 6000(+0.2%) · USD/KRW 1441원(-0.1%) · 미10Y 4.2%",
        data_status=data_status,
        facts=facts if facts is not None else [
            FactRow(
                label="NVDA 종가", value="120.00 USD", comparison="전일 대비 +1.0%",
                source_url="https://example.test/nvda", data_status=data_status,
                known_at=db_mod.iso_utc(cutoff), subject="NVDA", metric="price_close",
                raw_value=120.0,
            )
        ],
        market_reaction=[],
        events=events or [],
        schedule_changes=[],
        missing=[MissingItem(area="한국 수급", reason="KRX 인증벽", since=db_mod.iso_utc(cutoff),
                             gap_id="kr_flows:investor_flow")],
        interpretation=Interpretation(),
        meta={"late_generation": late, "no_facts_before_cutoff": False},
    )


def write_report(reports_root: Path, report: Report, stem: str | None = None) -> Path:
    from market_intel.reporting.build import stem_for

    stem = stem or stem_for(report)
    path = reports_root / report.report_type / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json(), encoding="utf-8")
    return path


@pytest.fixture
def reports_root(tmp_path) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    return root


@pytest.fixture
def docs_root(tmp_path) -> Path:
    return tmp_path / "docs"


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "market_intel.db")
    db_mod.init_db(db_path)
    c = db_mod.connect(db_path)
    yield c
    c.close()


def seed_calendar(conn, raw_dir: str, subject: str, year: int, dates: list[str],
                  known_at: str, extra: dict | None = None) -> str:
    """A B2-rev2 year-timetable calendar fact, written the real way."""
    fc = FactCandidate(
        raw_ref=f"{subject}:{year}", subject=subject, category="calendar",
        metric="scheduled_date", event_at=f"{year}-01-01T00:00:00+00:00",
        market="US", country="US", value_text=",".join(sorted(set(dates))),
        data_status="source_verified", safe_source_url="https://example.test/cal",
        extra=extra or {},
    )
    raw = RawItem(
        external_id=f"cal:{subject}:{year}", source_published_at=fc.event_at,
        safe_source_url=fc.safe_source_url, payload=json.dumps({"dates": dates}),
    )
    snapshot_id = db_mod.insert_raw_snapshot(conn, raw_dir, "policy_calendar", raw)
    fact_id = _fact_id("policy_calendar", fc)
    db_mod.upsert_fact(conn, fact_id, snapshot_id, known_at, fc)
    conn.commit()
    return fact_id
