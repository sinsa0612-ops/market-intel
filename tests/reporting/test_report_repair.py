"""Repair regression tests (judge.md P0 + "양쪽 다 틀린 것").

Every test here is behaviour-based on purpose: the pre-existing
`test_reports_never_touch_sql` grep gate cannot see the calendar leak at
all, because the offending SQL lives in `schedule.py`, outside
`reporting/`. A gate that inspects source text can only ever catch the
mistakes it was written for; these run the real build + render path and
assert on what a reader would actually see.

Fixtures go through `db.insert_raw_snapshot` + `db.upsert_fact`
(conftest.seed_fact) — the same two calls `engine._persist` makes.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime, timedelta, timezone

from market_intel import db as db_mod
from market_intel import schedule as schedule_mod
from market_intel.models import FactCandidate
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import model as model_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod

from conftest import seed_fact

REPORT_DATE = date(2026, 8, 1)
# spec B6: morning 차단선 = 당일 07:15 KST = 전날 22:15 UTC.
MORNING_CUTOFF_UTC = "2026-07-31T22:15:00+00:00"


def _timetable_fc(dates_csv: str, *, subject: str = "fredrel:999", year: int = 2026) -> FactCandidate:
    """One year-timetable calendar fact (spec B2 rev2): the dates live in
    `value_text`, so a moved release is a *revision of the same fact* —
    which is exactly what makes a post-cutoff move leakable."""
    return FactCandidate(
        raw_ref=f"cal-{subject}-{year}", subject=subject, category="calendar",
        metric="scheduled_date", event_at=f"{year}-01-01T00:00:00+00:00",
        market="US", country="US", value_text=dates_csv,
        comparison_basis=schedule_mod.YEAR_TIMETABLE_BASIS,
        data_status="source_verified", extra={"release_name": "Consumer Price Index"},
        safe_source_url="https://api.stlouisfed.org/fred/release/dates?api_key=***",
    )


def _conn_with_move(settings, tmp_path, name: str, second_known_at: str):
    """A timetable published before the cutoff (08-04) and then moved to
    08-06 at `second_known_at`."""
    db_path = str(tmp_path / f"{name}.db")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("2026-08-04"),
              known_at="2026-07-29T00:00:00+00:00")
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("2026-08-06"),
              known_at=second_known_at)
    return conn


# --- ① 차단선 이후에 알려진 일정 변경이 리포트로 새어 나온다 ---------------

def test_post_cutoff_schedule_change_never_reaches_the_report(settings, tmp_path):
    """judge.md 「양쪽 다 틀린 것」 1: the same report printed
    「다가오는 일정: 08-04」 and 「최근 변경: 08-04 → 08-06」 at once, where
    the move was only known 2h AFTER the blackout. That is the exact
    hindsight failure this system exists to prevent."""
    cutoff = cutoff_mod.cutoff_for("morning", REPORT_DATE)
    conn = _conn_with_move(settings, tmp_path, "leak", db_mod.iso_utc(cutoff + timedelta(hours=2)))

    report = build_mod.build_report(conn, "morning", REPORT_DATE, cutoff)
    conn.close()

    assert any(e.when == "2026-08-04" for e in report.events), \
        "the pre-cutoff schedule must still be visible"
    leaked = [c for c in report.schedule_changes if "2026-08-06" in f"{c.when} {c.name}"]
    assert not leaked, f"a calendar move known after the cutoff leaked into the report: {leaked}"

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    assert "2026-08-06" not in md, "the post-cutoff move reached the markdown a reader sees"
    assert "2026-08-06" not in html_doc, "the post-cutoff move reached the published HTML"
    assert "2026-08-04" in md and "2026-08-04" in html_doc


def test_schedule_change_cutoff_boundary_is_inclusive_to_the_second(settings, tmp_path):
    """cutoff−1s and cutoff exactly are inside; cutoff+1s is not."""
    cutoff = cutoff_mod.cutoff_for("morning", REPORT_DATE)
    cases = {
        "minus1s": (db_mod.iso_utc(cutoff - timedelta(seconds=1)), True),
        "exact": (db_mod.iso_utc(cutoff), True),
        "plus1s": (db_mod.iso_utc(cutoff + timedelta(seconds=1)), False),
    }
    for name, (known_at, should_be_visible) in cases.items():
        conn = _conn_with_move(settings, tmp_path, name, known_at)
        report = build_mod.build_report(conn, "morning", REPORT_DATE, cutoff)
        conn.close()
        moved = [c for c in report.schedule_changes if "2026-08-06" in f"{c.when} {c.name}"]
        assert bool(moved) is should_be_visible, f"{name}: known_at={known_at} moved={moved}"


def test_schedule_changes_cutoff_accepts_kst_and_utc_spellings(settings, tmp_path):
    """The same instant written as `+09:00` and as UTC must give the same
    answer — a KST-spelled cutoff compared lexicographically against UTC
    `known_at` values is how a blackout silently opens."""
    conn = _conn_with_move(settings, tmp_path, "spelling", "2026-08-01T00:15:00+00:00")
    since = "2026-07-01T00:00:00+00:00"
    kst = schedule_mod.changes(conn, since, "2026-08-01T07:15:00+09:00")
    utc = schedule_mod.changes(conn, since, MORNING_CUTOFF_UTC)
    aware = schedule_mod.changes(conn, since, cutoff_mod.cutoff_for("morning", REPORT_DATE))
    conn.close()
    assert kst == utc == aware
    assert not any(r["new"] == "2026-08-06" for r in kst), kst


def test_changes_window_anchors_on_the_cutoff_not_the_wall_clock(settings, tmp_path):
    """judge.md ②: `days` trimmed on the *real* today, so a backfilled or
    catch-up report silently produced an empty change list. The window must
    be anchored on the cutoff's own KST date."""
    db_path = str(tmp_path / "backfill.db")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("2026-07-24"),
              known_at="2026-07-10T00:00:00+00:00")
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("2026-07-26"),
              known_at="2026-07-15T00:00:00+00:00")

    rows = schedule_mod.changes(conn, "2026-07-01T00:00:00+00:00",
                                "2026-07-20T07:15:00+09:00", days=7)
    conn.close()
    assert [(r["date"], r["kind"]) for r in rows] == [("2026-07-26", "연기")], rows


def test_backfilled_report_still_carries_its_schedule_changes(settings, tmp_path):
    """The same regression one layer up: a report generated for a past date
    must contain the moves that were known before ITS cutoff."""
    db_path = str(tmp_path / "backfill_report.db")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("2026-07-24"),
              known_at="2026-07-10T00:00:00+00:00")
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("2026-07-26"),
              known_at="2026-07-15T00:00:00+00:00")

    past = date(2026, 7, 20)
    report = build_mod.build_report(conn, "morning", past, cutoff_mod.cutoff_for("morning", past))
    conn.close()
    assert [c.change for c in report.schedule_changes] == ["연기"], report.schedule_changes
    assert "2026-07-26" in render_md_mod.render_markdown(report)


# --- ② href 스킴 허용목록 (공개 GitHub Pages로 나가는 HTML) ---------------

_HREF_RE = re.compile(r'href="([^"]*)"')

BAD_URLS = [
    "javascript:alert(document.domain)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(2)",
    "java\tscript:alert(3)",
    "java\nscript:alert(4)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "/relative/path",
]


def _report_with_source_urls(settings, tmp_path, urls: list[str]):
    db_path = str(tmp_path / "urls.db")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    for i, url in enumerate(urls):
        fc = FactCandidate(
            raw_ref=f"xss-{i}", subject=f"SYM{i}", category="financials", metric="revenue",
            event_at="2026-06-30T00:00:00+00:00", market="US", country="US",
            value_num=float(i + 1), unit="USD", data_status="source_verified",
            comparison_basis="quarterly", safe_source_url=url,
        )
        seed_fact(conn, settings.raw_dir, "sec_edgar", fc, known_at="2026-08-01T00:00:00+00:00")
    cutoff = cutoff_mod.cutoff_for("quarterly", REPORT_DATE)
    report = build_mod.build_report(conn, "quarterly", REPORT_DATE, cutoff,
                                    subject=None)
    conn.close()
    return report


def test_html_href_only_ever_carries_http_schemes(settings, tmp_path):
    report = _report_with_source_urls(settings, tmp_path, BAD_URLS)
    # build_report filters financials to Core16; render the rows directly so
    # the renderer — the actual sink — is what is under test.
    report = dataclasses.replace(report, facts=[
        model_mod.FactRow(label=f"row{i}", value="1", comparison="", source_url=url,
                          data_status="source_verified", known_at="2026-08-01T00:00:00+00:00",
                          subject=f"SYM{i}", metric="revenue")
        for i, url in enumerate(BAD_URLS)
    ])
    html_doc = render_html_mod.render_html(report)
    hrefs = _HREF_RE.findall(html_doc)
    for href in hrefs:
        assert href.lower().startswith(("http://", "https://")), f"unsafe href emitted: {href!r}"
    lowered = html_doc.lower()
    for scheme in ("javascript:", "data:", "vbscript:"):
        assert f'href="{scheme}' not in lowered
    assert "href=\"javascript" not in lowered.replace(" ", "").replace("\t", "").replace("\n", "")
    # the URL is not silently dropped — it stays readable as text
    assert "alert(document.domain)" in html_doc


def test_markdown_link_only_ever_carries_http_schemes(settings, tmp_path):
    report = _report_with_source_urls(settings, tmp_path, BAD_URLS)
    report = dataclasses.replace(report, facts=[
        model_mod.FactRow(label=f"row{i}", value="1", comparison="", source_url=url,
                          data_status="source_verified", known_at="2026-08-01T00:00:00+00:00",
                          subject=f"SYM{i}", metric="revenue")
        for i, url in enumerate(BAD_URLS)
    ])
    md = render_md_mod.render_markdown(report)
    for link in re.findall(r"\[원자료\]\(([^)]*)\)", md):
        assert link.lower().startswith(("http://", "https://")), f"unsafe md link: {link!r}"


def test_http_urls_still_become_links(settings, tmp_path):
    url = "https://api.stlouisfed.org/fred/series?api_key=***"
    report = _report_with_source_urls(settings, tmp_path, [url])
    report = dataclasses.replace(report, facts=[
        model_mod.FactRow(label="ok", value="1", comparison="", source_url=url,
                          data_status="source_verified", known_at="2026-08-01T00:00:00+00:00",
                          subject="SYM0", metric="revenue")
    ])
    html_doc = render_html_mod.render_html(report)
    md = render_md_mod.render_markdown(report)
    assert 'href="https://api.stlouisfed.org/fred/series?api_key=***"' in html_doc.replace("&amp;", "&")
    assert f"[원자료]({url})" in md


# --- ③ 해석 4필드가 8종 × md/html 전부에서 살아남는다 --------------------

FILLED = model_mod.Interpretation(
    reading="테스트 당시 해석", counter_reading="테스트 반대 해석",
    thesis_impact="테스트 기존 가설 영향", next_check="테스트 다음 검증",
    generated_by="ai:test-model", generated_at="2026-08-01T08:00:00+00:00",
)


def test_interpretation_survives_every_type_in_both_renderers(settings):
    """judge.md ④: A dropped `reading`/`thesis_impact` in weekly_review and
    event — md AND html — and every test stayed green because only
    `morning` was ever checked."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for report_type in build_mod.TITLES:
        cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
        subject = "NVDA" if report_type == "event" else None
        report = build_mod.build_report(conn, report_type, REPORT_DATE, cutoff, subject=subject)
        filled = dataclasses.replace(report, interpretation=FILLED)
        for renderer, out in (
            ("md", render_md_mod.render_markdown(filled)),
            ("html", render_html_mod.render_html(filled)),
        ):
            where = f"{report_type}/{renderer}"
            for field_name in ("reading", "counter_reading", "thesis_impact", "next_check"):
                assert getattr(FILLED, field_name) in out, f"{where}: {field_name} dropped"
            assert "AI 해석 미생성" not in out, where
            assert "AI 자동판정 · ai:test-model" in out, where
    conn.close()


def test_every_type_renders_the_four_interpretation_headers(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for report_type in build_mod.TITLES:
        cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
        subject = "NVDA" if report_type == "event" else None
        report = build_mod.build_report(conn, report_type, REPORT_DATE, cutoff, subject=subject)
        md = render_md_mod.render_markdown(report)
        html_doc = render_html_mod.render_html(report)
        for header in ("당시 해석", "반대 해석", "기존 가설 영향", "다음 검증"):
            assert f"## {header}" in md, f"{report_type} md is missing '{header}'"
            assert f"<h2>{header}</h2>" in html_doc, f"{report_type} html is missing '{header}'"
    conn.close()


def test_spec_header_orders_are_preserved(settings):
    """§4.2 (일간 7개) · §6.2 (주간 5개) · §7.2 (이벤트 4개) must still come
    out in the specified order after the layout unification."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)

    def headers(report_type: str, subject=None) -> list[str]:
        cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
        report = build_mod.build_report(conn, report_type, REPORT_DATE, cutoff, subject=subject)
        md = render_md_mod.render_markdown(report)
        return [line[3:] for line in md.splitlines() if line.startswith("## ")]

    daily = headers("morning")
    assert daily[:7] == ["시장 한 줄", "핵심 사실", "시장 반응",
                         "당시 해석", "반대 해석", "기존 가설 영향", "다음 검증"]

    weekly = headers("weekly_review")
    assert weekly[:5] == ["이번 주 시장의 지배 변수", "자산·섹터 성과",
                          "다음 주에 뒤집힐 수 있는 변수", "내가 놓친 변수", "다음 주 검증할 가설"]

    event = headers("event", subject="NVDA")
    assert event[:4] == ["실제치·예상치·가이던스", "현금흐름과 투자",
                         "시장 반응과 반대 해석", "다음 분기 검증 조건"]
    conn.close()


def test_schedule_sections_are_rendered_for_every_type(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for report_type in build_mod.TITLES:
        cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
        subject = "NVDA" if report_type == "event" else None
        report = build_mod.build_report(conn, report_type, REPORT_DATE, cutoff, subject=subject)
        md = render_md_mod.render_markdown(report)
        html_doc = render_html_mod.render_html(report)
        for header in ("다가오는 일정", "최근 일정 변경"):
            assert f"## {header}" in md, f"{report_type} md is missing '{header}'"
            assert f"<h2>{header}</h2>" in html_doc, f"{report_type} html is missing '{header}'"
    conn.close()


# --- ⑤ 국면 라벨 · ⑥ 차단선 이전 fact 0건 --------------------------------

def test_regime_label_is_undecidable_without_inputs(settings):
    """judge.md 「양쪽 다 틀린 것」 3: with every macro input None the report
    still asserted 「성장 확대」 — in its own title. §3.3: 미확인은 추정으로
    채우지 않는다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    cutoff = cutoff_mod.cutoff_for("monthly", REPORT_DATE)
    report = build_mod.build_report(conn, "monthly", REPORT_DATE, cutoff)
    conn.close()

    assert report.meta["regime_label"] == build_mod.REGIME_UNDECIDABLE
    assert build_mod.REGIME_UNDECIDABLE in report.title
    assert any(m.gap_id == build_mod.REGIME_GAP_ID for m in report.missing)
    md = render_md_mod.render_markdown(report)
    assert build_mod.REGIME_UNDECIDABLE in md


def test_regime_label_is_decided_when_inputs_exist(settings, tmp_path):
    """The other half of the gate: with real inputs the deterministic rule
    still produces one of §6.3's labels (the fix must not blanket-refuse)."""
    db_path = str(tmp_path / "regime.db")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    cutoff = cutoff_mod.cutoff_for("monthly", REPORT_DATE)
    for event_at, cpi in (("2026-06-01T00:00:00+00:00", 300.0), ("2026-07-01T00:00:00+00:00", 303.0)):
        seed_fact(conn, settings.raw_dir, "fred", FactCandidate(
            raw_ref=f"cpi-{event_at}", subject="CPIAUCSL", category="macro", metric="value",
            event_at=event_at, market="US", country="US", value_num=cpi, unit="index",
            data_status="source_verified",
        ), known_at="2026-07-20T00:00:00+00:00")
    report = build_mod.build_report(conn, "monthly", REPORT_DATE, cutoff)
    conn.close()

    assert report.meta["regime_label"] == "인플레이션 재가속", report.meta["regime_rule"]
    assert not any(m.gap_id == build_mod.REGIME_GAP_ID for m in report.missing)


def test_zero_facts_before_cutoff_is_declared_as_missing(settings):
    """judge.md 「양쪽 다 틀린 것」 4 + [운영 이슈]: with collect running at
    07:20/07:40 and the morning blackout at 07:15, the report comes out
    empty every single day. Nothing may pass silently: the gap goes into
    `missing`, into `data_gaps`, and onto the face of the report."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    cutoff = cutoff_mod.cutoff_for("morning", REPORT_DATE)
    report = build_mod.build_report(conn, "morning", REPORT_DATE, cutoff)

    assert not report.facts and not report.market_reaction
    assert any(m.gap_id == build_mod.NO_FACTS_GAP_ID for m in report.missing), report.missing
    row = conn.execute("SELECT status FROM data_gaps WHERE gap_id=?",
                       (build_mod.NO_FACTS_GAP_ID,)).fetchone()
    assert row is not None and row["status"] == "제안"
    conn.close()

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    assert render_md_mod.EMPTY_REPORT_WARNING in md
    assert render_md_mod.EMPTY_REPORT_WARNING in html_doc


def test_no_facts_gap_disappears_once_a_pre_cutoff_fact_exists(settings, tmp_path):
    db_path = str(tmp_path / "hasfacts.db")
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    cutoff = cutoff_mod.cutoff_for("morning", REPORT_DATE)
    seed_fact(conn, settings.raw_dir, "fred", FactCandidate(
        raw_ref="unrate", subject="UNRATE", category="macro", metric="value",
        event_at="2026-07-01T00:00:00+00:00", market="US", country="US",
        value_num=4.1, unit="percent", data_status="source_verified",
    ), known_at=db_mod.iso_utc(cutoff - timedelta(hours=1)))
    report = build_mod.build_report(conn, "morning", REPORT_DATE, cutoff)
    conn.close()

    assert report.facts
    assert not any(m.gap_id == build_mod.NO_FACTS_GAP_ID for m in report.missing)
    assert render_md_mod.EMPTY_REPORT_WARNING not in render_md_mod.render_markdown(report)
