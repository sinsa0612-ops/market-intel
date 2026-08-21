"""ST3 acceptance tests — static site (spec §ST3 What #1, B8).

The site is the only artefact that becomes *public*, so these tests are
written adversarially: they check what must NOT be in `docs/` at least as
hard as what must.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_intel import site as site_mod
from market_intel.reporting import render_md
from market_intel.reporting.model import CalendarRow, FactRow

from tests.publish.conftest import make_report, seed_calendar, write_report


def _html_files(docs_root: Path) -> list[Path]:
    return sorted(docs_root.rglob("*.html"))


def test_site_builds_b8_layout(reports_root, docs_root, conn, tmp_path):
    write_report(reports_root, make_report("morning", "2026-08-01"))
    result = site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    for rel in ("index.html", "archive.html", "schedule.html", "style.css", ".nojekyll"):
        assert (docs_root / rel).exists(), f"spec B8 requires docs/{rel}"
    assert (docs_root / "reports" / "morning" / "2026-08-01.html").exists()
    assert result["reports_indexed"] == 1
    assert result["latest"] == "morning/2026-08-01"


def test_site_indexes_all(reports_root, docs_root, conn):
    """spec ST3 `test_site_indexes_all` — 12 reports over several types and
    years, every one reachable from archive.html."""
    stems = []
    for i in range(6):
        d = f"2026-07-{i + 1:02d}"
        write_report(reports_root, make_report("morning", d))
        stems.append(("morning", d))
    for i in range(3):
        d = f"2025-06-{i + 1:02d}"
        write_report(reports_root, make_report("close_delta", d))
        stems.append(("close_delta", d))
    for i in range(3):
        d = f"2026-0{i + 1}-01"
        write_report(reports_root, make_report("monthly", d))
        stems.append(("monthly", d[:7]))

    result = site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    assert result["reports_indexed"] == 12

    archive = (docs_root / "archive.html").read_text(encoding="utf-8")
    for rtype, stem in stems:
        href = f"reports/{rtype}/{stem}.html"
        assert href in archive, f"{href} missing from archive.html"
        assert (docs_root / "reports" / rtype / f"{stem}.html").exists()


def test_index_lists_past_reports(reports_root, docs_root, conn):
    """CEO requirement: past reports must be reachable from the front page,
    not only from a separate archive page."""
    for i in range(25):
        write_report(reports_root, make_report("morning", f"2026-07-{i + 1:02d}"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    index = (docs_root / "index.html").read_text(encoding="utf-8")
    assert "archive.html" in index
    links = set(re.findall(r'href="(reports/[^"]+\.html)"', index))
    assert len(links) >= 20, f"index.html should card-list the recent 20 (got {len(links)})"


def test_html_escapes(reports_root, docs_root, conn):
    """spec ST3 `test_html_escapes` — an externally-derived name carrying a
    script tag must never reach the public site as markup."""
    evil = "<script>alert(1)</script>"
    rep = make_report("morning", "2026-08-01", facts=[
        FactRow(label=evil, value="1", comparison="", source_url="https://example.test/x",
                data_status="partial", known_at="2026-07-31T22:00:00+00:00",
                subject="X", metric="value")
    ])
    rep.title = evil
    write_report(reports_root, rep)
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    for path in _html_files(docs_root):
        text = path.read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in text, f"raw script tag in {path}"
    page = (docs_root / "reports" / "morning" / "2026-08-01.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in page


def test_no_external_assets(reports_root, docs_root, conn):
    """spec ST3 `test_no_external_assets` — no CDN/font/script pulled from
    the network. Only source anchors with rel="noopener" may point out."""
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    for path in _html_files(docs_root):
        text = path.read_text(encoding="utf-8")
        assert "<script" not in text.lower(), f"inline/external JS in {path} (spec B8)"
        for m in re.finditer(r'src="(https?://[^"]+)"', text):
            pytest.fail(f"external asset {m.group(1)} in {path}")
        for m in re.finditer(r'<[^>]*href="(https?://[^"]+)"[^>]*>', text):
            tag = m.group(0)
            assert tag.startswith("<a "), f"external href on a non-anchor tag in {path}: {tag}"
            assert 'rel="noopener"' in tag, f"external anchor without rel=noopener in {path}"


def test_no_javascript_scheme_href(reports_root, docs_root, conn):
    """A `javascript:` source_url must not become a live href — the ST2
    renderer's scheme allowlist has to survive site assembly."""
    rep = make_report("morning", "2026-08-01", facts=[
        FactRow(label="fact", value="1", comparison="", source_url="javascript:alert(1)",
                data_status="partial", known_at="2026-07-31T22:00:00+00:00",
                subject="X", metric="value")
    ])
    write_report(reports_root, rep)
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    for path in _html_files(docs_root):
        text = path.read_text(encoding="utf-8").lower()
        assert 'href="javascript:' not in text, f"javascript: href in {path}"


def test_data_status_badge_and_asof(reports_root, docs_root, conn):
    """spec B7 — a `partial` fact is shown with 부분 확인 + a warning class."""
    rep = make_report("morning", "2026-08-01", data_status="partial")
    write_report(reports_root, rep)
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    page = (docs_root / "reports" / "morning" / "2026-08-01.html").read_text(encoding="utf-8")
    assert "부분 확인" in page
    assert "status-warn" in page


def test_late_generation_badge(reports_root, docs_root, conn):
    """spec B10 — a catch-up report is labelled 지연 생성 on its site card."""
    write_report(reports_root, make_report("morning", "2026-08-01", late=True))
    write_report(reports_root, make_report("morning", "2026-08-02", late=False))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    archive = (docs_root / "archive.html").read_text(encoding="utf-8")
    assert "지연 생성" in archive


def test_interpretation_placeholder_on_site(reports_root, docs_root, conn):
    """해석이 하나도 없으면 **네 칸 모두** 비었다고 말해야 한다 — 조용히 빈
    칸으로 두면 독자는 그날 할 말이 없었던 것으로 읽는다.

    반대 해석만 문구가 다르다: 당시 해석이 없으면 반박할 대상 자체가 없다는
    뜻이라 사정이 다르고, 같은 문구로 쓰면 "이 문단이 검증에 걸렸다"와
    구별되지 않는다(CEO 지적 2026-08-04)."""
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    page = (docs_root / "reports" / "morning" / "2026-08-01.html").read_text(encoding="utf-8")
    assert page.count(render_md.NO_INTERP) >= 3
    assert render_md.NO_COUNTER_WITHOUT_READING in page


def test_site_regenerated_in_full(reports_root, docs_root, conn):
    """spec B8 — `docs/` is rebuilt from scratch every time, so a report
    that disappears from `reports/` does not leave a stale public page."""
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    stale = docs_root / "reports" / "morning" / "2026-08-01.html"
    assert stale.exists()

    (reports_root / "morning" / "2026-08-01.json").unlink()
    write_report(reports_root, make_report("morning", "2026-08-02"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    assert not stale.exists(), "docs/ kept a page whose report JSON is gone"


def test_schedule_page_uses_report_cutoff_not_wall_clock(reports_root, docs_root, conn, tmp_path):
    """**The cutoff-leak regression (orchestrator decision 2).**

    A calendar fact that only became known *after* the latest report's
    blackout must not appear on the public schedule page. If `site.py`
    passes `datetime.now()` to `schedule.upcoming/changes` instead of that
    report's cutoff, the post-blackout date leaks straight onto the site —
    which is exactly what happened one stage earlier.
    """
    raw_dir = str(tmp_path / "raw")
    # Latest blackout among published reports = morning 2026-08-01 07:15 KST
    # (= 2026-07-31T22:15Z). The older report must not raise or lower it.
    write_report(reports_root, make_report("morning", "2026-08-01"))
    write_report(reports_root, make_report("morning", "2026-07-31"))

    before = "2026-07-31T22:00:00+00:00"   # known before the blackout
    after = "2026-07-31T23:00:00+00:00"    # known after it
    seed_calendar(conn, raw_dir, "fomc", 2026, ["2026-08-05"], before)
    seed_calendar(conn, raw_dir, "bokmpc", 2026, ["2026-08-27"], after)

    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    page = (docs_root / "schedule.html").read_text(encoding="utf-8")
    assert "2026-08-05" in page, "a pre-cutoff scheduled date should be published"
    assert "2026-08-27" not in page, "post-cutoff calendar fact leaked onto the public site"


def test_schedule_cutoff_is_the_latest_published_blackout(reports_root, docs_root, conn, tmp_path):
    """A later-generated but earlier-blackout report must not shrink the
    schedule page back to the earlier barrier."""
    raw_dir = str(tmp_path / "raw")
    write_report(reports_root, make_report("close_delta", "2026-08-01"))  # 16:15 KST
    write_report(reports_root, make_report("morning", "2026-08-01"))      # 07:15 KST

    seed_calendar(conn, raw_dir, "bokmpc", 2026, ["2026-08-27"], "2026-08-01T05:00:00+00:00")
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    page = (docs_root / "schedule.html").read_text(encoding="utf-8")
    assert "2026-08-27" in page, (
        "a fact inside the close_delta blackout was hidden because a morning "
        "report happened to be written last")


def test_schedule_changes_respect_the_cutoff(reports_root, docs_root, conn, tmp_path):
    """`schedule.changes()` takes a **mandatory** cutoff. A schedule move
    that only became known after the blackout must not be published — the
    site once printed 「다가오는 일정: 08-04」 and 「변경: 08-04 → 08-06」 side
    by side, the move being something only the future knew."""
    raw_dir = str(tmp_path / "raw")
    write_report(reports_root, make_report("morning", "2026-08-01"))  # 07-31T22:15Z

    seed_calendar(conn, raw_dir, "fomc", 2026, ["2026-08-05"], "2026-07-31T22:00:00+00:00")
    seed_calendar(conn, raw_dir, "fomc", 2026, ["2026-08-06"], "2026-07-31T23:00:00+00:00")

    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    page = (docs_root / "schedule.html").read_text(encoding="utf-8")
    assert "2026-08-05" in page
    assert "2026-08-06" not in page, "a post-blackout schedule move leaked onto the site"
    assert "연기" not in page


def test_schedule_page_without_reports_states_it(reports_root, docs_root, conn):
    """With no report there is no honest cutoff, so the page says so rather
    than silently falling back to the wall clock."""
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    page = (docs_root / "schedule.html").read_text(encoding="utf-8")
    assert "기준 시각" in page
