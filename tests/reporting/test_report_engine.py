"""ST2 acceptance tests (spec `### ST2 report-engine`, "TDD 인수 테스트").

Every fixture goes through `db.insert_raw_snapshot` + `db.upsert_fact`
(conftest.py `seed_fact`) — the real write path, not a hand-seeded row —
so a build.py bug that assumes the wrong DB shape cannot hide behind a
fixture that already assumes the right one.
"""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from market_intel import db as db_mod
from market_intel import reporting
from market_intel.models import FactCandidate
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import model as model_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod

from conftest import price_fc, seed_fact


def test_cutoff_blackout(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    report_date = date(2026, 8, 1)
    cutoff = cutoff_mod.cutoff_for("morning", report_date)  # 2026-08-01T07:15+09:00

    stale_reading = FactCandidate(
        raw_ref="cpi-old", subject="CPIAUCSL", category="macro", metric="value",
        event_at="2026-07-01T00:00:00+00:00", market="US", country="US",
        value_num=310.0, unit="index", data_status="source_verified",
    )
    seed_fact(conn, settings.raw_dir, "fred", stale_reading, known_at="2026-07-05T00:00:00+00:00")

    # A revision known 1 minute AFTER the blackout must not leak in.
    late_reading = FactCandidate(
        raw_ref="cpi-late", subject="CPIAUCSL", category="macro", metric="value",
        event_at="2026-08-01T00:00:00+00:00", market="US", country="US",
        value_num=311.5, unit="index", data_status="source_verified",
    )
    late_known_at = db_mod.iso_utc(cutoff + timedelta(minutes=1))
    seed_fact(conn, settings.raw_dir, "fred", late_reading, known_at=late_known_at)

    report = build_mod.build_report(conn, "morning", report_date, cutoff)
    cpi_rows = [r for r in report.facts if r.subject == "CPIAUCSL"]
    assert len(cpi_rows) == 1
    assert cpi_rows[0].raw_value == 310.0, "a fact known after the cutoff leaked into the report"

    # A revision known 1 minute BEFORE the blackout must be visible.
    early_known_at = db_mod.iso_utc(cutoff - timedelta(minutes=1))
    on_time_reading = FactCandidate(
        raw_ref="unrate-ontime", subject="UNRATE", category="macro", metric="value",
        event_at="2026-07-01T00:00:00+00:00", market="US", country="US",
        value_num=4.1, unit="percent", data_status="source_verified",
    )
    seed_fact(conn, settings.raw_dir, "fred", on_time_reading, known_at=early_known_at)
    report2 = build_mod.build_report(conn, "morning", report_date, cutoff)
    assert any(r.subject == "UNRATE" for r in report2.facts)
    conn.close()


def test_reports_never_touch_sql(settings):
    reporting_dir = Path(reporting.__file__).parent
    py_files = list(reporting_dir.rglob("*.py"))
    assert py_files, "reporting/ package is empty"
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        assert "fact_revisions" not in text, f"{path} references fact_revisions directly"
        assert "SELECT " not in text, f"{path} contains a raw SQL SELECT"

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    calls = []
    original = db_mod.facts_as_of

    def _spy(c, cutoff, **filters):
        calls.append(filters)
        return original(c, cutoff, **filters)

    with mock.patch.object(db_mod, "facts_as_of", side_effect=_spy):
        report_date = date(2026, 8, 1)
        build_mod.build_report(conn, "morning", report_date, cutoff_mod.cutoff_for("morning", report_date))
    assert len(calls) >= 1, "build_report never called db.facts_as_of"
    conn.close()


def test_interpretation_empty_contract(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    report_date = date(2026, 8, 1)
    for report_type in build_mod.TITLES:
        cutoff = cutoff_mod.cutoff_for(report_type, report_date)
        subject = "NVDA" if report_type == "event" else None
        report = build_mod.build_report(conn, report_type, report_date, cutoff, subject=subject)
        interp = report.interpretation
        assert (interp.reading, interp.counter_reading, interp.thesis_impact, interp.next_check) == ("", "", "", "")
        assert interp.is_empty()
        md = render_md_mod.render_markdown(report)
        html_doc = render_html_mod.render_html(report)
        assert "AI 해석 미생성" in md, report_type
        assert "AI 해석 미생성" in html_doc, report_type
    conn.close()


def test_interpretation_filled_renders(settings):
    """The 2B extension point actually works: fill `Interpretation`, change
    nothing else, and the exact same renderer prints the text instead of
    the placeholder (spec B5 contract 2)."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    report_date = date(2026, 8, 1)
    cutoff = cutoff_mod.cutoff_for("morning", report_date)
    report = build_mod.build_report(conn, "morning", report_date, cutoff)

    filled = dataclasses.replace(
        report,
        interpretation=model_mod.Interpretation(
            reading="테스트 당시 해석", counter_reading="테스트 반대 해석",
            thesis_impact="유지", next_check="다음 CPI 발표 확인",
            generated_by="ai:test-model", generated_at="2026-08-01T08:00:00+00:00",
        ),
    )
    md = render_md_mod.render_markdown(filled)
    html_doc = render_html_mod.render_html(filled)

    assert "AI 해석 미생성" not in md
    assert "AI 해석 미생성" not in html_doc
    assert "AI 자동판정 · ai:test-model" in md
    assert "AI 자동판정 · ai:test-model" in html_doc
    assert "테스트 당시 해석" in md and "테스트 당시 해석" in html_doc
    conn.close()


def test_data_status_surfaced(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    lly_capex_2022 = FactCandidate(
        raw_ref="lly-capex-2022", subject="LLY", category="financials", metric="capex",
        event_at="2022-12-31T00:00:00+00:00", market="US", country="US",
        value_num=-2100000000.0, unit="USD", data_status="partial", comparison_basis="annual",
    )
    seed_fact(conn, settings.raw_dir, "sec_edgar", lly_capex_2022, known_at="2026-08-01T00:00:00+00:00")

    report_date = date(2026, 8, 1)
    cutoff = cutoff_mod.cutoff_for("quarterly", report_date)
    report = build_mod.build_report(conn, "quarterly", report_date, cutoff)

    lly_rows = [r for r in report.facts if r.subject == "LLY"]
    assert lly_rows, "seeded LLY capex fact did not reach the report"
    assert lly_rows[0].data_status == "partial"
    assert "2022" in lly_rows[0].value
    assert report.data_status in ("partial", "unverified"), report.data_status

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    # B7: "모든 FactRow는 자기 등급 배지를 갖는다" — assert on the ROW, not on
    # the document. The header line `데이터 상태: 부분 확인` alone satisfied a
    # document-wide `in` check, so deleting every row badge kept the suite
    # green (judge.md 변이 2).
    md_rows = [line for line in md.splitlines() if line.startswith("|") and "LLY" in line]
    assert md_rows, "the LLY row is missing from the markdown table"
    assert "부분 확인" in md_rows[0] and "2022" in md_rows[0], md_rows[0]

    html_rows = [chunk for chunk in html_doc.split("<tr>") if "LLY" in chunk]
    assert html_rows, "the LLY row is missing from the HTML table"
    assert "부분 확인" in html_rows[0] and "2022" in html_rows[0], html_rows[0]
    assert 'class="status-warn"' in html_rows[0], "B7 경고색 훅이 행에서 사라졌다"
    conn.close()


def test_no_kr_flows_still_builds(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    report_date = date(2026, 8, 1)
    cutoff = cutoff_mod.cutoff_for("close_delta", report_date)

    report = build_mod.build_report(conn, "close_delta", report_date, cutoff)  # must not raise

    assert any(m.gap_id == build_mod.KR_FLOW_GAP_ID for m in report.missing)
    row = conn.execute("SELECT status FROM data_gaps WHERE gap_id=?", (build_mod.KR_FLOW_GAP_ID,)).fetchone()
    assert row is not None and row["status"] == "제안"
    conn.close()


def test_kr_flows_appear_when_present(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    flow_fact = FactCandidate(
        raw_ref="krx-foreign-net-buy", subject="KOSPI", category="flow", metric="net_buy_foreign",
        event_at="2026-08-01T00:00:00+00:00", market="KR", country="KR",
        value_num=123456.0, unit="KRW", data_status="source_verified",
    )
    seed_fact(conn, settings.raw_dir, "kis", flow_fact, known_at="2026-08-01T07:00:00+00:00")

    report_date = date(2026, 8, 1)
    cutoff = cutoff_mod.cutoff_for("close_delta", report_date)
    report = build_mod.build_report(conn, "close_delta", report_date, cutoff)

    assert not any(m.gap_id == build_mod.KR_FLOW_GAP_ID for m in report.missing)
    assert any(r.subject == "KOSPI" and r.metric == "net_buy_foreign" for r in report.facts)
    conn.close()


def test_close_delta_3_to_5(settings):
    """spec B6: 3~5 items, absolute-move-rank order, KOSPI + USD/KRW always
    included. 6 movers are seeded (4 non-mandatory) so the cap of 5 must
    actually drop the single weakest one (JPM, +0.2%) rather than just
    happening to fit everything."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    known_at = "2026-08-01T06:00:00+00:00"
    # (symbol, market, country, prior_close, latest_close, unit)
    movers = [
        ("^KS11", "KR", "KR", 2500.0, 2550.0, "point"),   # +2.0%, mandatory
        ("KRW=X", "US", "US", 1440.0, 1450.0, "KRW"),     # +0.7%, mandatory
        ("NVDA", "US", "US", 100.0, 105.0, "USD"),        # +5.0%
        ("MSFT", "US", "US", 400.0, 380.0, "USD"),        # -5.0%
        ("AMZN", "US", "US", 200.0, 202.0, "USD"),        # +1.0%
        ("JPM", "US", "US", 150.0, 150.3, "USD"),         # +0.2%, weakest of all 6
    ]
    for symbol, market, country, prior, latest, unit in movers:
        seed_fact(conn, settings.raw_dir, "yfinance",
                   price_fc(symbol, market, country, "2026-07-31T06:30:00+00:00", prior, unit), known_at)
        seed_fact(conn, settings.raw_dir, "yfinance",
                   price_fc(symbol, market, country, "2026-08-01T06:30:00+00:00", latest, unit), known_at)

    report_date = date(2026, 8, 1)
    cutoff = cutoff_mod.cutoff_for("close_delta", report_date)
    report = build_mod.build_report(conn, "close_delta", report_date, cutoff)

    assert 3 <= len(report.market_reaction) <= 5, report.market_reaction
    subjects = {r.subject for r in report.market_reaction}
    assert "^KS11" in subjects and "KRW=X" in subjects
    assert "NVDA" in subjects and "MSFT" in subjects and "AMZN" in subjects
    assert "JPM" not in subjects, "the weakest mover (+0.2%) must be dropped once the 5-item cap is exceeded"
    conn.close()


def test_json_roundtrip(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    lly_capex_2022 = FactCandidate(
        raw_ref="lly-capex-2022", subject="LLY", category="financials", metric="capex",
        event_at="2022-12-31T00:00:00+00:00", market="US", country="US",
        value_num=-2100000000.0, unit="USD", data_status="partial", comparison_basis="annual",
        extra={"period_end": "2022-12-31"},
    )
    seed_fact(conn, settings.raw_dir, "sec_edgar", lly_capex_2022, known_at="2026-08-01T00:00:00+00:00")
    seed_fact(conn, settings.raw_dir, "yfinance",
               price_fc("^KS11", "KR", "KR", "2026-08-01T06:30:00+00:00", 2530.5, "point"),
               "2026-08-01T06:00:00+00:00")

    report_date = date(2026, 8, 1)
    cutoff = cutoff_mod.cutoff_for("morning", report_date)
    report = build_mod.build_report(conn, "morning", report_date, cutoff)

    restored = model_mod.Report.from_json(report.to_json())
    assert restored == report
    conn.close()
