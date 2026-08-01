"""Static site generator (spec B8) — `reports/**/*.json` in, `docs/` out.

Three rules shape everything here:

1. **`reports/` is the only input.** `docs/` is deleted and rebuilt in full
   on every run (spec B8), so there is no partial-update state to manage
   and a report that disappears cannot leave a stale public page behind.
2. **The report body is rendered by ST2's `render_html`, never by this
   file.** Assembling report markup here would route around that module's
   `html.escape` + URL-scheme allowlist — the exact defect that nearly put
   a `javascript:` href on a public site one stage earlier. This module
   only produces *chrome* (head, nav, cards, tables of links), and every
   externally-derived string it puts in that chrome is escaped here.
3. **The schedule page reads the calendar as of the latest report's
   blackout, never `datetime.now()`** (orchestrator decision 2). The
   calendar layer has its own PIT barrier — `schedule.upcoming` through
   `facts_as_of`, `schedule.changes` through a mandatory `cutoff` — and
   handing it a wall clock publishes information the report itself was
   not allowed to see.

No CDN, no external font, no `<script>` anywhere (spec B8): one hand-written
stylesheet, everything else is plain HTML with relative links so the site
works under a repository sub-path on GitHub Pages.
"""
from __future__ import annotations

import html as html_mod
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import schedule as schedule_mod
from .config import PROJECT_ROOT
from .reporting.model import Report
from .reporting.render_html import render_html
from .reporting.render_md import status_ko

SCHEDULE_WINDOW_DAYS = 60  # spec B8: 향후 60일 일정
CHANGES_WINDOW_DAYS = 7  # spec B8: 최근 7일 일정 변경
RECENT_CARDS = 20  # spec B8: index = 최신 리포트 본문 + 최근 20건 카드

TYPE_LABELS = {
    "morning": "모닝", "week_start": "주간 시작", "close_delta": "장마감 델타",
    "weekly_review": "주간 리뷰", "monthly": "월간", "quarterly": "분기",
    "annual": "연간", "event": "이벤트",
}

STYLE_CSS = """\
/* market-intel — single stylesheet (spec B8: no CDN, no external font). */
:root { --fg:#1b1f23; --muted:#6a737d; --line:#e1e4e8; --warn:#b26a00;
        --warn-bg:#fff6e5; --ok:#1a7f37; --bg:#fff; --accent:#0b5fff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); line-height:1.65;
       font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo",
                    "Malgun Gothic", sans-serif; }
.wrap { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
header.site { border-bottom:1px solid var(--line); margin-bottom:1.5rem; padding-bottom:.75rem; }
header.site h1 { font-size:1.1rem; margin:0 0 .35rem; }
nav a { margin-right:1rem; color:var(--accent); text-decoration:none; }
nav a:hover { text-decoration:underline; }
h1,h2,h3 { line-height:1.3; }
h2 { font-size:1.05rem; margin-top:2rem; border-bottom:1px solid var(--line); padding-bottom:.3rem; }
table { border-collapse:collapse; width:100%; margin:.5rem 0 1rem; font-size:.92rem; }
th,td { border:1px solid var(--line); padding:.35rem .5rem; text-align:left; vertical-align:top; }
th { background:#f6f8fa; font-weight:600; }
.status-ok { color:var(--ok); font-size:.8rem; white-space:nowrap; }
.status-warn { color:var(--warn); background:var(--warn-bg); font-size:.8rem;
               padding:0 .25rem; border-radius:3px; white-space:nowrap; }
.badge-late { color:var(--warn); background:var(--warn-bg); font-size:.75rem;
              padding:0 .3rem; border-radius:3px; margin-left:.4rem; }
.ai-badge { color:var(--muted); font-size:.85rem; margin:.2rem 0; }
ul.cards { list-style:none; padding:0; }
ul.cards li { border-bottom:1px solid var(--line); padding:.5rem 0; font-size:.95rem; }
ul.cards li a { color:var(--accent); text-decoration:none; }
ul.cards li a:hover { text-decoration:underline; }
.meta { color:var(--muted); font-size:.85rem; }
footer.site { margin-top:3rem; padding-top:1rem; border-top:1px solid var(--line);
              color:var(--muted); font-size:.8rem; }
"""


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


def _page(title: str, body: str, depth: int) -> str:
    """`depth` = how many directories deep the page sits under `docs/`, so
    every asset/nav link stays relative (GitHub Pages serves this project
    from a repository sub-path, where a leading `/` points at the wrong
    origin root)."""
    up = "../" * depth
    return (
        "<!doctype html>\n"
        '<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f'<link rel="stylesheet" href="{up}style.css"></head><body><div class="wrap">'
        '<header class="site"><h1>market-intel</h1><nav>'
        f'<a href="{up}index.html">최신</a>'
        f'<a href="{up}archive.html">전체 보고서</a>'
        f'<a href="{up}schedule.html">일정</a>'
        "</nav></header>"
        f"{body}"
        '<footer class="site">사실 계층만 자동 생성 · 해석은 별도 단계에서 채웁니다.</footer>'
        "</div></body></html>\n"
    )


def _status_badge(report: Report) -> str:
    cls = "status-warn" if report.data_status in ("partial", "unverified") else "status-ok"
    return f'<span class="{cls}">{_esc(status_ko(report.data_status))}</span>'


def _card_li(entry: dict) -> str:
    report = entry["report"]
    late = ' <span class="badge-late">지연 생성</span>' if report.meta.get("late_generation") else ""
    label = TYPE_LABELS.get(report.report_type, report.report_type)
    return (
        f'<li><a href="{_esc(entry["href"])}">{_esc(report.report_date)} · {_esc(label)}</a> '
        f'{_status_badge(report)}{late}</li>'
    )


# --- loading --------------------------------------------------------------

def load_reports(reports_root: Path) -> list[dict]:
    """Every report JSON, newest first. A file that is not a readable report
    is skipped with its name reported rather than aborting the build — the
    site must still go up when one artefact is corrupt."""
    entries: list[dict] = []
    for path in sorted(reports_root.glob("*/*.json")):
        try:
            report = Report.from_json(path.read_text(encoding="utf-8"))
        except (ValueError, TypeError, KeyError):
            continue
        stem = path.stem
        entries.append({
            "report": report,
            "stem": stem,
            "href": f"reports/{report.report_type}/{stem}.html",
            "out": Path("reports") / report.report_type / f"{stem}.html",
        })
    # Same-date reports are ranked by blackout, not by when the file happened
    # to be written: the later blackout is the one that saw more of the day.
    # Ordering by `generated_at` put whichever report was rendered last on the
    # front page — so a backfilled 07:15 morning report displaced the 16:15
    # close, and the site opened on the emptier of the two.
    entries.sort(
        key=lambda e: (e["report"].report_date, e["report"].cutoff_kst,
                       e["report"].generated_at, e["stem"]),
        reverse=True,
    )
    return entries


# --- pages ----------------------------------------------------------------

def _report_page(entry: dict) -> str:
    report = entry["report"]
    body = (
        '<p class="meta"><a href="../../archive.html">← 전체 보고서</a></p>'
        + render_html(report)
    )
    title = f"{report.report_date} {TYPE_LABELS.get(report.report_type, report.report_type)}"
    return _page(title, body, depth=2)


def _index_page(entries: list[dict]) -> str:
    if not entries:
        return _page("market-intel", "<p>아직 생성된 리포트가 없습니다.</p>", depth=0)
    latest = entries[0]
    # The latest body is rendered with report-page-relative links rewritten
    # to root-relative ones is *not* needed: render_html emits no internal
    # links at all, only absolute source anchors.
    cards = "".join(_card_li(e) for e in entries[:RECENT_CARDS])
    body = (
        render_html(latest["report"])
        + "<h2>최근 보고서</h2>"
        + f'<ul class="cards">{cards}</ul>'
        + f'<p class="meta">전체 {len(entries)}건 — <a href="archive.html">과거 보고서 전체 보기</a></p>'
    )
    return _page("market-intel — 최신", body, depth=0)


def _archive_page(entries: list[dict]) -> str:
    """spec B8 — 연 → 월 → type 그룹, 링크 전부. This is the page that has to
    make *every* past report reachable (CEO requirement), so it is grouped
    but never truncated."""
    tree: dict[str, dict[str, dict[str, list[dict]]]] = {}
    for e in entries:
        d = e["report"].report_date
        year, month = d[:4], d[5:7]
        tree.setdefault(year, {}).setdefault(month, {}).setdefault(e["report"].report_type, []).append(e)

    parts = ["<h1>전체 보고서</h1>", f'<p class="meta">총 {len(entries)}건</p>']
    if not entries:
        parts.append("<p>아직 생성된 리포트가 없습니다.</p>")
    for year in sorted(tree, reverse=True):
        parts.append(f"<h2>{_esc(year)}년</h2>")
        for month in sorted(tree[year], reverse=True):
            parts.append(f"<h3>{_esc(month)}월</h3>")
            for rtype in sorted(tree[year][month]):
                label = TYPE_LABELS.get(rtype, rtype)
                items = "".join(_card_li(e) for e in tree[year][month][rtype])
                parts.append(f'<p class="meta">{_esc(label)}</p><ul class="cards">{items}</ul>')
    return _page("market-intel — 전체 보고서", "".join(parts), depth=0)


def schedule_cutoff(entries: list[dict]) -> datetime | None:
    """The information barrier for `docs/schedule.html`: the **latest
    blackout among all published reports**, never the wall clock.

    Why the maximum rather than the newest report's own cutoff: every one of
    those reports is already on this site, so publishing the calendar as of
    the latest of their blackouts reveals nothing that is not public
    already — while keying off `entries[0]` alone would blank the schedule
    page whenever the most *recently generated* report happens to be an
    early-blackout one (observed: a 07:15 morning report regenerated after a
    16:15 close_delta emptied the whole page).

    What it must never become is `datetime.now()`. `schedule.changes()`
    takes its cutoff as a mandatory argument precisely because a wall clock
    there published a move that only the future knew.
    """
    cutoffs = [datetime.fromisoformat(e["report"].cutoff_utc)
               for e in entries if e["report"].cutoff_utc]
    return max(cutoffs) if cutoffs else None


def _schedule_page(conn, cutoff: datetime | None) -> str:
    if cutoff is None:
        # No report means no defensible information barrier. Saying so beats
        # falling back to the wall clock, which would publish calendar facts
        # no report was ever allowed to see.
        body = (
            "<h1>일정</h1>"
            '<p class="meta">기준 시각: 없음 — 생성된 리포트가 없어 일정을 표시할 '
            "정보차단선을 정할 수 없습니다.</p>"
        )
        return _page("market-intel — 일정", body, depth=0)

    upcoming = schedule_mod.upcoming(conn, cutoff, SCHEDULE_WINDOW_DAYS)
    changes = schedule_mod.changes(
        conn, cutoff - timedelta(days=CHANGES_WINDOW_DAYS), cutoff, days=CHANGES_WINDOW_DAYS)

    parts = ["<h1>일정</h1>",
             f'<p class="meta">기준 시각: {_esc(cutoff.isoformat())} '
             "(최신 리포트의 정보차단선)</p>",
             f"<h2>향후 {SCHEDULE_WINDOW_DAYS}일</h2>"]
    if upcoming:
        rows = "".join(
            "<tr>" + "".join(
                f"<td>{_esc(r[k])}</td>"
                for k in ("date", "importance", "country", "name", "status")
            ) + "</tr>"
            for r in upcoming
        )
        parts.append("<table><thead><tr><th>일자</th><th>중요도</th><th>국가</th>"
                     f"<th>이름</th><th>상태</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        parts.append("<p>(해당 없음)</p>")

    parts.append(f"<h2>최근 {CHANGES_WINDOW_DAYS}일 일정 변경</h2>")
    if changes:
        rows = "".join(
            "<tr>" + "".join(
                f"<td>{_esc(r[k])}</td>" for k in ("date", "kind", "name", "old", "new")
            ) + "</tr>"
            for r in changes
        )
        parts.append("<table><thead><tr><th>일자</th><th>구분</th><th>이름</th>"
                     f"<th>이전</th><th>이후</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        parts.append("<p>(해당 없음)</p>")
    return _page("market-intel — 일정", "".join(parts), depth=0)


# --- entry point ----------------------------------------------------------

def build_site(conn, reports_root: Path | None = None, docs_root: Path | None = None) -> dict:
    """Regenerate `docs/` in full. Returns the B13 `site build` counters."""
    reports_root = Path(reports_root) if reports_root else PROJECT_ROOT / "reports"
    docs_root = Path(docs_root) if docs_root else PROJECT_ROOT / "docs"

    entries = load_reports(reports_root) if reports_root.exists() else []

    if docs_root.exists():
        shutil.rmtree(docs_root)
    docs_root.mkdir(parents=True)

    (docs_root / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    # spec §Environment gotchas: without .nojekyll, GitHub Pages swallows any
    # path starting with `_`.
    (docs_root / ".nojekyll").write_text("", encoding="utf-8")

    pages = 0
    for entry in entries:
        out = docs_root / entry["out"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_report_page(entry), encoding="utf-8")
        pages += 1

    (docs_root / "index.html").write_text(_index_page(entries), encoding="utf-8")
    (docs_root / "archive.html").write_text(_archive_page(entries), encoding="utf-8")
    pages += 2

    cutoff = schedule_cutoff(entries)
    (docs_root / "schedule.html").write_text(_schedule_page(conn, cutoff), encoding="utf-8")
    pages += 1

    latest = f"{entries[0]['report'].report_type}/{entries[0]['stem']}" if entries else ""
    return {
        "pages": pages,
        "reports_indexed": len(entries),
        "latest": latest,
        "out": str(docs_root),
    }
