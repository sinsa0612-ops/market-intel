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

from . import detail as detail_mod
from . import schedule as schedule_mod
from .config import PROJECT_ROOT
from .interp import ops as ops_mod
from .reporting.cutoff import KST
from .reporting.model import Report
from .reporting.render_html import render_html, sparkline_svg
from .reporting.render_md import safe_href, status_ko

SCHEDULE_WINDOW_DAYS = 60  # spec B8: 향후 60일 일정
CHANGES_WINDOW_DAYS = 7  # spec B8: 최근 7일 일정 변경
RECENT_CARDS = 20  # spec B8: index = 최신 리포트 본문 + 최근 20건 카드
BANNER_CHANGE_DAYS = 7  # index 배너에 띄우는 가설 변화의 기간(전체 목록은 가설 페이지)

TYPE_LABELS = {
    "morning": "모닝", "week_start": "주간 시작", "close_delta": "장마감 델타",
    "weekly_review": "주간 리뷰", "monthly": "월간", "quarterly": "분기",
    "annual": "연간", "event": "이벤트",
}

STYLE_CSS = """\
/* market-intel — single stylesheet (spec B8: no CDN, no external font). */
:root { --fg:#1b1f23; --muted:#6a737d; --line:#e1e4e8; --warn:#b26a00;
        --warn-bg:#fff6e5; --ok:#1a7f37; --ok-bg:#eaf6ec; --bg:#fff;
        --accent:#0b5fff; --accent-bg:#eef3ff; --th-bg:#f6f8fa;
        /* 등락 색은 국내 증시 관례를 따른다: 상승 = 빨강, 하락 = 파랑
           (미국식 초록/빨강과 반대). CEO가 한국 시장을 함께 보므로 여기서
           미국식을 쓰면 매일 방향을 거꾸로 읽게 된다. 흰 배경 대비 각각
           4.8:1 / 4.6:1 (WCAG AA 본문 기준 4.5:1 충족). */
        --up:#d92d20; --down:#1570ef; }
/* 아이폰 다크모드에서도 색 대비가 유지되어야 한다 — 밝은 배경용 빨강·파랑을
   어두운 배경에 그대로 쓰면 둘 다 뭉개진다. 이 팔레트는 #141821 대비
   6.4:1 / 7.7:1. */
@media (prefers-color-scheme: dark) {
  :root { --fg:#f0f2f5; --muted:#98a2b3; --line:#2a2f3a; --warn:#f0b357;
          --warn-bg:#3a2f16; --ok:#5bcc7d; --ok-bg:#15301d; --bg:#141821;
          --accent:#7aa7ff; --accent-bg:#1b2540; --th-bg:#1c212c;
          --up:#f97066; --down:#53b1fd; }
}
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
th { background:var(--th-bg); font-weight:600; }
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
/* 운영 상태(ST3) — 좁은 화면에서 표가 페이지를 밀어내지 않도록 표만 가로
   스크롤시킨다. 아이폰에서 status.html이 읽혀야 한다는 요구(사람 확인 4). */
.scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
.stamp { font-size:1.25rem; font-weight:600; margin:.2rem 0 .1rem; }
.banner { border-radius:4px; padding:.5rem .7rem; margin:.6rem 0; font-size:.95rem; }
.banner.ok { background:var(--ok-bg); color:var(--ok); }
.banner.warn { background:var(--warn-bg); color:var(--warn); font-weight:600; }
.banner.change { background:var(--accent-bg); color:var(--accent); }
.state-ok { color:var(--ok); }
.state-warn { color:var(--warn); background:var(--warn-bg); padding:0 .25rem;
              border-radius:3px; font-weight:600; }
.detail { color:var(--muted); font-size:.85rem; word-break:break-all; }

/* 가독성(등락 색·화살표·추이) — 마크업은 전부 reporting/render_html.py가
   만들고, 이 파일은 색과 배치만 정한다. 색이 여기 있는 이유: 인라인 색은
   위의 prefers-color-scheme 미디어쿼리를 그냥 빠져나간다. */
.up { color:var(--up); } .down { color:var(--down); }
.chg { text-align:right; font-variant-numeric:tabular-nums; font-weight:650;
       white-space:nowrap; }
.chg.up { color:var(--up); } .chg.down { color:var(--down); }
.chg.flat { color:var(--muted); }
.arrow { font-size:.7em; margin-right:.15em; }
/* 색만으로 정보를 주지 않는다는 규약을 여기서도 지킨다: 화살표는 장식이
   아니므로 어떤 화면 폭에서도 숨기지 않는다. */
.legend { color:var(--muted); font-size:.85rem; margin:.4rem 0 .8rem; }
.breadth { font-size:.95rem; margin:.6rem 0 .2rem; }
/* 업종 표가 둘(업종 지수 / Core 16 기업 묶음)이라 각 표에 제목이 붙는다.
   h2(섹션)보다 작고 본문보다 굵게 — 375px에서도 두 표가 구분돼야 한다. */
article.mi-report h3 { font-size:.98rem; margin:1.4rem 0 .1rem; }
article.mi-report p.group { font-size:.85rem; color:var(--muted); font-weight:600;
                            margin:.7rem 0 -.2rem; }
.hero { display:flex; gap:.5rem; flex-wrap:wrap; margin:.2rem 0 .6rem; }
.hero .card { flex:1 1 9rem; border:1px solid var(--line); border-radius:10px;
              padding:.6rem .8rem; }
.hero .k { font-size:.78rem; color:var(--muted); }
.hero .v { font-size:1.25rem; font-weight:700; margin-top:.15rem;
           font-variant-numeric:tabular-nums; }
.hero .c { font-size:.85rem; font-weight:600; margin-top:.1rem; }
.hero .c.up { color:var(--up); } .hero .c.down { color:var(--down); }
td.sp { width:7.5rem; text-align:right; }
.spark { display:block; margin-left:auto; color:var(--muted); }
.spark.up { color:var(--up); } .spark.down { color:var(--down); }
.spark .line { fill:none; stroke:currentColor; stroke-width:1.6; stroke-linejoin:round; }
.spark .area { fill:currentColor; opacity:.10; stroke:none; }
.spark circle { fill:currentColor; }

/* 차트 (CEO 지시 2026-08-12 "시각화가 부족하다").
   ⚠️ 부호는 색이 아니라 **0축 위/아래 위치**가 전달한다 — 흑백 출력에서도,
   색을 못 가리는 눈에도 남는다. 색은 거들 뿐이다. 그래서 계열 구분에도
   색만 쓰지 않고 선은 점선 패턴을, 막대는 채움 농도를 함께 바꾼다.
   `figcaption`은 장식이 아니라 그림과 같은 내용의 문장이다(낭독기·흑백용). */
.chart { margin:1rem 0; }
.chart svg { display:block; width:100%; height:auto; overflow:visible; }
.chart figcaption { color:var(--muted); font-size:.85rem; margin-top:.35rem; }
.chart .axis { stroke:var(--line); stroke-width:1; }
.chart .axis.base { stroke-dasharray:3 3; }
.chart .tick { fill:var(--muted); font-size:11px; }
/* 다이버징 막대: 위(0축 위)가 첫 계열, 아래가 둘째다. */
.chart-breadth rect.s0 { fill:var(--up); }
.chart-breadth rect.s1 { fill:var(--down); }
.chart-breadth .dot { fill:var(--fg); }
.chart-flows rect.s0 { fill:var(--down); }          /* 외국인 */
.chart-flows rect.s1 { fill:var(--muted); }         /* 기관 */
.chart-flows rect.s2 { fill:var(--up); }            /* 개인 */
/* 꺾은선: 색 + 선 패턴을 함께 바꿔 흑백에서도 갈린다. */
.chart-rebased .line { fill:none; stroke-width:1.8; stroke-linejoin:round; }
.chart-rebased .line.s0 { stroke:var(--fg); }
.chart-rebased .line.s1 { stroke:var(--up); stroke-dasharray:5 3; }
.chart-rebased .line.s2 { stroke:var(--down); stroke-dasharray:2 2; }
.chart-rebased .line.s3 { stroke:var(--muted); stroke-dasharray:8 3 2 3; }
.chart-rebased .lbl { font-size:11px; fill:currentColor; }
.chart-rebased .lbl.s0 { fill:var(--fg); }
.chart-rebased .lbl.s1 { fill:var(--up); }
.chart-rebased .lbl.s2 { fill:var(--down); }
.chart-rebased .lbl.s3 { fill:var(--muted); }

/* 수급 막대 — 종목 하나에 한 줄. 개인·기관·외국인의 순매수 합은 0이므로
   폭의 비율이 곧 "누가 사고 누가 팔았나"다. 색은 위의 등락 팔레트를 그대로
   쓴다(빨강 = 사는 쪽). 막대 안의 글자는 색 위에 얹히므로 --bg가 아니라
   고정 흰색이다 — 다크모드에서 배경색을 쓰면 빨강 위에 남색이 온다. */
.flow { margin:.9rem 0; }
.flow .name { font-weight:650; font-size:.95rem; }
.flow .story { color:var(--muted); font-size:.85rem; margin-left:.35rem; font-weight:400; }
.bar { display:flex; height:1.5rem; border-radius:4px; overflow:hidden; margin-top:.3rem;
       background:var(--th-bg); font-size:.72rem; }
.bar span { display:flex; align-items:center; justify-content:center; color:#fff;
            white-space:nowrap; overflow:hidden; min-width:0; }
/* 진하기 = 금액의 절대 크기(`--a`, 렌더러가 계산해 인라인으로 넣는다). 색 자체는
   여기서 --up/--down과 섞으므로 다크모드 팔레트가 그대로 적용된다 — 인라인 색은
   prefers-color-scheme을 그냥 빠져나간다.
   `background` 선언이 둘인 것은 폴백이다: color-mix를 모르는 브라우저는 앞줄의
   진한 색을 그대로 쓴다(정보가 사라지는 게 아니라 농담만 사라진다).
   섞는 상대가 `transparent`가 아니라 `--bg`인 이유: 투명하게 두면 아래 깔린
   트랙 색이 비쳐 옅은 칸끼리 서로 다른 색으로 보인다. */
.bar .buy  { background:var(--up); }
.bar .sell { background:var(--down); }
.bar .buy  { background:color-mix(in srgb, var(--up)   calc(var(--a, 1) * 100%), var(--bg)); }
.bar .sell { background:color-mix(in srgb, var(--down) calc(var(--a, 1) * 100%), var(--bg)); }
/* 옅은 칸 위의 흰 글씨는 읽히지 않는다. 임계값 아래는 렌더러가 `pale`을 붙이고
   글자를 본문색으로 돌린다 — 밝은 배경에서도 어두운 배경에서도 대비가 산다. */
.bar .pale { color:var(--fg); }
.bar .zero { background:var(--line); color:var(--muted); flex:1; }

/* 거시지표 카드 — 값 하나짜리 관측이라 표의 다섯 칸 중 넷이 빈다. */
.mgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(8.5rem,1fr)); gap:.5rem;
         margin:.5rem 0 .8rem; }
.mgrid .card { border:1px solid var(--line); border-radius:8px; padding:.5rem .6rem; }
.mgrid .k { font-size:.75rem; color:var(--muted); }
.mgrid .v { font-size:1.05rem; font-weight:700; font-variant-numeric:tabular-nums; }
.mgrid .c { font-size:.78rem; font-weight:600; color:var(--muted); }
.mgrid .c.up { color:var(--up); } .mgrid .c.down { color:var(--down); }

details > summary { cursor:pointer; font-weight:600; margin:.6rem 0; color:var(--accent); }

/* "오늘 유별난 것"(spec 20260806-report-visual §1①) — 상승비율 2년 추이 +
   오늘 점 강조, 가장 크게 움직인 것의 좌우 막대. 색·화살표 규약은 위와 같다. */
.unusual { margin:.3rem 0 1rem; }
.unusual-headline { font-size:.95rem; margin:.2rem 0 .6rem; }
.trend-caption, .movers-caption { color:var(--muted); font-size:.8rem; margin:.6rem 0 .2rem; }
.trend { display:block; width:100%; height:auto; margin:.2rem 0; color:var(--muted); }
.trend.up { color:var(--up); } .trend.down { color:var(--down); }
/* 원시선은 배경으로 깔고(옅게·가늘게), 20일 이동평균이 추세를 말한다.
   484개 일별 점을 같은 굵기로 그리면 톱니만 보이고 오늘 점이 묻힌다. */
.trend .line { fill:none; stroke:currentColor; stroke-width:1; opacity:.3; stroke-linejoin:round; }
.trend .ma { fill:none; stroke:currentColor; stroke-width:2.4; stroke-linejoin:round; }
.trend .mid { stroke:var(--line); stroke-width:1; stroke-dasharray:4 3; }
/* 오늘 점은 배경색 테두리를 둘러 선 위에서도 떨어져 보이게 한다. */
.trend .today { fill:currentColor; stroke:var(--bg); stroke-width:2; }
.trend .ax { fill:var(--muted); font-size:9px; text-anchor:end; }
.movers { margin:.2rem 0 .4rem; }
.mv-row { display:grid; grid-template-columns:6.5rem 1fr 4.5rem; align-items:center;
          gap:.5rem; font-size:.85rem; margin:.3rem 0; }
.mv-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.mv-track { position:relative; height:.7rem; background:var(--th-bg); border-radius:3px; }
.mv-track::after { content:""; position:absolute; left:50%; top:0; bottom:0; width:1px;
                    background:var(--line); }
.mv-bar { position:absolute; top:0; bottom:0; border-radius:3px; }
.mv-bar.pos { left:50%; background:var(--up); }
.mv-bar.neg { right:50%; background:var(--down); }
.mv-val { text-align:right; font-weight:650; font-variant-numeric:tabular-nums; white-space:nowrap; }
.mv-val.up { color:var(--up); } .mv-val.down { color:var(--down); }
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
        f'<a href="{up}detail.html">상세</a>'
        f'<a href="{up}theses.html">가설</a>'
        f'<a href="{up}status.html">상태</a>'
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


def _banner(state: dict, changes: list[dict]) -> str:
    """index 최상단 한 줄(spec ST3 What #4). CEO는 푸시 알림을 받지 않기로
    했으므로, 파이프라인이 죽었다는 사실과 가설 판정이 뒤집혔다는 사실이
    사이트에서 눈에 띄는 것이 유일한 전달 경로다."""
    if state["healthy"]:
        line = (f'<p class="banner ok">마지막 실행 정상 · {_esc(state["generated_at_display"])} 기준 '
                '<a href="status.html">운영 상태</a></p>')
    else:
        summary = " · ".join(state["alerts"][:3])
        more = f" 외 {len(state['alerts']) - 3}건" if len(state["alerts"]) > 3 else ""
        line = (f'<p class="banner warn">확인 필요 — {_esc(summary)}{_esc(more)} '
                '<a href="status.html">운영 상태 보기</a></p>')
    if changes:
        moves = " · ".join(
            f"{c['thesis_id']} {c['prev_verdict'] or '(없음)'} → {c['verdict']}" for c in changes[:3])
        line += (f'<p class="banner change">가설 변화 {len(changes)}건 — {_esc(moves)} '
                 '<a href="theses.html">가설 보기</a></p>')
    return line


def _index_page(entries: list[dict], banner: str = "") -> str:
    if not entries:
        return _page("market-intel", banner + "<p>아직 생성된 리포트가 없습니다.</p>", depth=0)
    latest = entries[0]
    # The latest body is rendered with report-page-relative links rewritten
    # to root-relative ones is *not* needed: render_html emits no internal
    # links at all, only absolute source anchors.
    cards = "".join(_card_li(e) for e in entries[:RECENT_CARDS])
    # spec 20260810-period-report §1③-2: "최근 보고서"는 실측 1,074px로 리포트
    # 본문보다 큰 링크 목록이었다(CEO: "읽을 내용이 아니라 링크 목록인데
    # 본문보다 크다"). 지우거나 옮기지 않고 접는다(§2 규칙4) — 순서는 이미
    # 본문 다음이라 그대로 두고, 크기만 접어서 줄인다.
    body = (
        banner
        + render_html(latest["report"])
        + f"<details><summary>최근 보고서 {min(len(entries), RECENT_CARDS)}건 펼치기</summary>"
        + f'<ul class="cards">{cards}</ul></details>'
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


# --- 운영 상태 / 가설 (2단계-B ST3) ----------------------------------------

def _kst_display(iso: str | None) -> str:
    """UTC 기록 시각을 화면용 KST 문자열로. 사장님이 보는 시간은 한국 시간이다."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone(KST).strftime("%m-%d %H:%M")
    except ValueError:
        return str(iso)


def _job_description(name: str) -> str:
    """이 job이 실제로 무엇을 하는지 사람 말로 — job 이름만 늘어놓으면
    비전공자에게는 아무 정보도 아니다."""
    from .jobs import JOBS

    spec = JOBS.get(name, {})
    parts = []
    if spec.get("collect"):
        parts.append("수집: " + ", ".join(spec["collect"]))
    if spec.get("report"):
        parts.append("리포트: " + TYPE_LABELS.get(spec["report"], spec["report"]))
    return " · ".join(parts) or "-"


def _state_cell(state: str) -> str:
    cls = "state-ok" if state == "정상" else "state-warn"
    return f'<span class="{cls}">{_esc(state)}</span>'


def _status_page(state: dict) -> str:
    parts = [
        "<h1>운영 상태</h1>",
        f'<p class="stamp">이 페이지 생성 시각: {_esc(state["generated_at_display"])}</p>',
        '<p class="meta">이 페이지는 자동 실행이 돌 때마다 다시 만들어집니다. '
        "위 시각이 며칠 전이라면 자동 실행 자체가 멈춘 것입니다.</p>",
    ]
    if state["healthy"]:
        parts.append('<p class="banner ok">지금 정상입니다 — 예정된 작업이 모두 제때 돌았습니다.</p>')
    else:
        items = "".join(f"<li>{_esc(a)}</li>" for a in state["alerts"])
        parts.append(f'<p class="banner warn">확인 필요 — {len(state["alerts"])}건</p><ul>{items}</ul>')

    rows = "".join(
        "<tr>"
        f"<td>{_esc(j['job'])}</td>"
        f"<td>{_esc(_job_description(j['job']))}</td>"
        f"<td>{_esc(_kst_display(j['last_started']))}</td>"
        f"<td>{_state_cell(j['state'])}</td>"
        f"<td>{_esc(j['overdue']) if j['overdue'] else '0'}</td>"
        f"<td class=\"detail\">{_esc(j['steps_text'] or '-')}</td>"
        f"<td class=\"detail\">{_esc(j['note'] or '-')}</td>"
        "</tr>"
        for j in state["jobs"]
    )
    parts.append(
        "<h2>자동 실행</h2>"
        '<div class="scroll"><table><thead><tr><th>작업</th><th>하는 일</th><th>마지막 실행</th>'
        "<th>결과</th><th>밀린 실행</th><th>단계</th><th>비고</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        # 이 페이지를 만드는 것이 그 job 자신이므로, 방금 그 job은 아직 끝나지
        # 않은 상태로 찍힌다. 설명이 없으면 매번 이상해 보인다.
        '<p class="meta">지금 이 페이지를 만든 작업은 `실행 중`으로 보입니다 — '
        "그 작업의 최종 결과는 다음 실행 때 반영됩니다. "
        "`기록 없음`은 이 화면이 생긴 뒤로 아직 한 번도 돌지 않았다는 뜻입니다.</p>"
    )

    collect = state["collect"]
    parts.append("<h2>마지막 수집</h2>")
    if collect:
        prows = "".join(
            "<tr>"
            f"<td>{_esc(p['provider'])}</td><td>{_esc(p['status'])}</td>"
            f"<td>{_esc(p['reason_code'] or '-')}</td>"
            f"<td class=\"detail\">{_esc(p['safe_detail'] or '-')}</td>"
            "</tr>"
            for p in collect["providers"]
        )
        parts.append(
            f'<p class="meta">{_esc(collect["workflow"])} · {_esc(_kst_display(collect["started_at"]))}</p>'
            '<div class="scroll"><table><thead><tr><th>제공자</th><th>상태</th><th>사유</th>'
            f"<th>상세</th></tr></thead><tbody>{prows}</tbody></table></div>"
        )
    else:
        parts.append("<p>(수집 기록 없음)</p>")

    interp = state["interpretation"]
    parts.append("<h2>마지막 AI 해석</h2>")
    if interp:
        fields = " ".join(f"{k}={v}" for k, v in (interp.get("fields") or {}).items())
        parts.append(
            f'<p>{_esc(interp["status"])} · {_esc(interp["report_type"])} '
            f'{_esc(interp["report_date"])} · {_esc(_kst_display(interp["created_at"]))}</p>'
            f'<p class="detail">모델 {_esc(interp.get("model") or "-")} · '
            f'{_esc(interp.get("prompt_version") or "-")} · 칸별 {_esc(fields or "-")}</p>'
        )
    else:
        parts.append("<p>(해석 기록 없음)</p>")

    parts.append("<h2>미해결 결측</h2>")
    if state["gaps"]:
        grows = "".join(
            "<tr>"
            f"<td>{_esc(g['gap_id'])}</td><td>{_esc(g['subject'] or '-')}</td>"
            f"<td class=\"detail\">{_esc(g['reason'] or '-')}</td><td>{_esc(g['status'] or '-')}</td>"
            "</tr>"
            for g in state["gaps"]
        )
        parts.append('<div class="scroll"><table><thead><tr><th>항목</th><th>대상</th>'
                     f"<th>사유</th><th>상태</th></tr></thead><tbody>{grows}</tbody></table></div>")
    else:
        parts.append("<p>(해당 없음)</p>")

    return _page("market-intel — 운영 상태", "".join(parts), depth=0)


def _theses_page(conn, now: datetime) -> str:
    overview = ops_mod.thesis_overview(conn)
    changes = ops_mod.thesis_changes(conn, now=now)
    introduced_on = ops_mod.thesis_display_introduced_on(conn)

    # final-review F4: 도입일을 원장에서 못 뽑으면(아직 그 엔진 버전 행이
    # 없으면) 문장 자체를 생략한다 — 벽시계 날짜로 대체하면 없는 도입일을
    # 지어내는 것이다. 지속일이 독립 증거가 아니라는 일반 안내는 도입일과
    # 무관하므로 그대로 남긴다.
    meta = (
        '가설 문장은 사람이 쓴 것이고, 판정은 코드가 규칙으로 매긴 것입니다. '
        "판정 불가는 오류가 아니라 아직 판단할 관측이 모이지 않았다는 뜻입니다. "
        "<strong>기준 변경</strong> 표시는 그 시점에 가설의 판정 기준 자체가 바뀌었다는 뜻이며, "
        "그 앞뒤 판정은 서로 비교할 수 없습니다. "
    )
    if introduced_on:
        meta += (
            f"진입일·지속·새 관측 표시는 {_esc(introduced_on)} 도입되었습니다 — 그 이전 판정의 "
            "'강화'는 다른 뜻입니다(매일 재충족을 포함). "
        )
    meta += (
        "지속일은 독립 증거의 수가 아닙니다 — "
        "월간·분기 지표는 다음 발표 전까지 증거 1개입니다."
    )

    parts = [
        "<h1>가설</h1>",
        f'<p class="meta">{meta}</p>',
        f"<h2>가설 변화 (최근 {ops_mod.THESIS_CHANGE_WINDOW_DAYS}일)</h2>",
    ]
    if changes:
        rows = "".join(
            "<tr>"
            f"<td>{_esc(c['report_date'])}</td><td>{_esc(c['thesis_id'])}</td>"
            f"<td>{_esc(c['prev_verdict'] or '(없음)')} → {_esc(c['verdict'])}"
            + ('<span class="badge-late">기준 변경</span>' if c.get("rules_changed") else "")
            + ('<span class="badge-late">표시 기준 변경</span>' if c.get("engine_changed") else "")
            + "</td>"
            f"<td class=\"detail\">{_esc(c['statement'] or '-')}</td>"
            "</tr>"
            for c in changes
        )
        parts.append('<div class="scroll"><table><thead><tr><th>일자</th><th>가설</th>'
                     f"<th>판정 변화</th><th>내용</th></tr></thead><tbody>{rows}</tbody></table></div>")
    else:
        parts.append("<p>(해당 없음 — 판정이 뒤집힌 가설이 없습니다.)</p>")

    for theme in overview:
        parts.append(f"<h2>{_esc(theme['label'])}</h2>")
        if not theme["theses"]:
            parts.append('<p class="meta">가설 없음</p>')
            continue
        for t in theme["theses"]:
            verdict = t["verdict"] or "판정 없음"
            cls = "state-ok" if verdict == "유지" or verdict == "강화" else "state-warn"
            indicators = ", ".join(t["leading_indicators"]) if t["leading_indicators"] else "-"
            state_line = (f'<p class="detail">상태: {_esc(t["state_line"])}</p>'
                         if t.get("state_line") else "")
            parts.append(
                f'<p><strong>{_esc(t["thesis_id"])}</strong> '
                f'<span class="{cls}">{_esc(verdict)}</span></p>'
                f"<p>{_esc(t['statement'])}</p>"
                f'<p class="detail">근거: {_esc(t["reason"] or "-")} · 선행 지표: {_esc(indicators)} '
                f'· 다음 점검일: {_esc(t["next_check_date"])}</p>'
                f"{state_line}"
            )
    return _page("market-intel — 가설", "".join(parts), depth=0)


# --- 상세 (기업 재무 / 거시지표 / 공시 / 13F) --------------------------------
#
# 리포트가 "오늘 무엇이 달라졌나"라면 상세는 "그동안 어떻게 움직였나"다. 백필로
# 재무 8분기·주가 2년·거시 3년이 들어오면서 처음으로 그릴 것이 생긴 층이고,
# 조회는 전부 `detail.py`가 — 즉 `facts_as_of(cutoff)`가 — 한다. 이 페이지들의
# 차단선도 리포트와 같은 `schedule_cutoff(entries)`다.

def _source_cell(url: str) -> str:
    """출처 링크. `html.escape`는 `javascript:`를 그대로 두므로 스킴 허용목록을
    한 번 더 통과시킨다 — 여기 들어오는 URL은 전부 외부 응답에서 온 것이다
    (render_html.py 규약과 같음)."""
    href = safe_href(url)
    if not href:
        return _esc(url) if url else "-"
    return f'<a href="{_esc(href)}" rel="noopener" target="_blank">원자료</a>'


def _no_cutoff_page(title: str, heading: str) -> str:
    """리포트가 하나도 없으면 정보차단선을 정할 수 없다. 벽시계로 물러나면
    어떤 리포트도 볼 수 없던 사실을 공개하게 되므로, 일정 페이지와 같은 규칙으로
    비운다."""
    body = (f"<h1>{_esc(heading)}</h1>"
            '<p class="meta">기준 시각: 없음 — 생성된 리포트가 없어 표시할 '
            "정보차단선을 정할 수 없습니다.</p>")
    return _page(title, body, depth=0)


def _cutoff_note(cutoff: datetime) -> str:
    return (f'<p class="meta">정보차단선: {_esc(cutoff.astimezone(KST).strftime("%Y-%m-%d %H:%M"))} KST '
            "— 이 시각까지 알려진 사실만 싣습니다.</p>")


def _company_page(conn, cutoff: datetime, subject: str) -> str:
    fin = detail_mod.company_financials(conn, cutoff, subject)
    rows = detail_mod.filings(conn, cutoff, subject=subject)
    name = detail_mod.name_ko(subject)

    parts = [f"<h1>{_esc(name)} <span class=\"meta\">({_esc(subject)})</span></h1>",
             _cutoff_note(cutoff), "<h2>재무 추이</h2>"]
    if not fin["metrics"]:
        parts.append('<p class="meta">아직 수집된 재무 항목이 없습니다.</p>')
    else:
        # 기간 길이는 항목마다 따로 적는다. 같은 회사 안에서도 매출은 분기,
        # 현금흐름은 연간만 오는 경우가 있어 표 하나에 "분기"라고 뭉뚱그리면
        # 그 자체가 거짓말이 된다.
        summary = "".join(
            "<tr>"
            f"<td>{_esc(detail_mod.METRIC_LABELS.get(m, m))}</td>"
            f"<td>{_esc(detail_mod.BASIS_LABELS.get(fin['bases'][m], fin['bases'][m]))}</td>"
            f"<td>{_esc(fin['periods'][0][m]['text'] if fin['periods'] else '—')}</td>"
            f'<td class="sp">{sparkline_svg(fin["series"][m], "")}</td>'
            "</tr>"
            for m in fin["metrics"]
        )
        parts.append('<div class="scroll"><table><thead><tr><th>항목</th><th>기간 기준</th>'
                     f"<th>최근값</th><th>추이</th></tr></thead><tbody>{summary}</tbody>"
                     "</table></div>")

        head = "".join(f"<th>{_esc(detail_mod.METRIC_LABELS.get(m, m))}</th>"
                       for m in fin["metrics"])
        body_rows = []
        for period in fin["periods"]:
            cells = []
            for m in fin["metrics"]:
                cell = period[m]
                mark = ' <span class="meta">(산출)</span>' if cell["derived"] else ""
                link = (f' {_source_cell(cell["source_url"])}') if cell["source_url"] else ""
                cells.append(f"<td>{_esc(cell['text'])}{mark}{link}</td>")
            body_rows.append(f"<tr><td>{_esc(period['period'])}</td>{''.join(cells)}</tr>")
        parts.append('<div class="scroll"><table><thead><tr><th>기간 종료일</th>'
                     f"{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>")
        parts.append('<p class="legend">(산출) = 원문에 그 기간 값이 없어 누적치를 '
                     "차분해 만든 값입니다. 나머지는 공시 원문 그대로입니다.</p>")

    parts.append("<h2>공시 이력</h2>")
    parts.append(_filing_table(rows, with_subject=False))
    parts.append('<p class="meta"><a href="../detail.html">← 상세</a></p>')
    return _page(f"market-intel — {name}", "".join(parts), depth=1)


def _macro_page(conn, cutoff: datetime, subject: str) -> str:
    data = detail_mod.macro_series(conn, cutoff, subject)
    name = data["label"]
    unit = data["unit"]

    parts = [f"<h1>{_esc(name)} <span class=\"meta\">({_esc(subject)})</span></h1>",
             _cutoff_note(cutoff)]
    if not data["observations"]:
        parts.append('<p class="meta">아직 수집된 관측이 없습니다.</p>')
    else:
        parts.append(f'<p class="breadth">추이 (최근 {len(data["series"])}개 관측) '
                     f'{sparkline_svg(data["series"], "")}</p>')
        body_rows = "".join(
            f"<tr><td>{_esc(o['event_at'])}</td>"
            f"<td>{_esc(f'{o['value']:,.2f}' if o['value'] is not None else '미확인')}"
            f"{_esc(' ' + unit if unit else '')}</td>"
            f"<td>{_source_cell(o['source_url'])}</td></tr>"
            for o in data["observations"]
        )
        parts.append('<div class="scroll"><table><thead><tr><th>기준일</th><th>값</th>'
                     f"<th>출처</th></tr></thead><tbody>{body_rows}</tbody></table></div>")
        parts.append(f'<p class="meta">표는 최근 {len(data["observations"])}개, '
                     f'차단선 이전 전체 관측은 {data["total"]}개입니다.</p>')
    parts.append('<p class="meta"><a href="../detail.html">← 상세</a></p>')
    return _page(f"market-intel — {name}", "".join(parts), depth=1)


def _filing_table(rows: list[dict], *, with_subject: bool) -> str:
    if not rows:
        return '<p class="meta">차단선 이전에 수집된 공시가 없습니다.</p>'
    subject_head = "<th>대상</th>" if with_subject else ""
    body = "".join(
        "<tr>"
        f"<td>{_esc(r['event_at'])}</td>"
        + (f"<td>{_esc(r['name'])}</td>" if with_subject else "")
        + f"<td>{_esc(r['form_label'])}</td>"
        f"<td>{_esc(r['item'] or '-')}</td>"
        f"<td class=\"detail\">{_esc(r['accession'] or '-')}</td>"
        f"<td>{_source_cell(r['source_url'])}</td>"
        "</tr>"
        for r in rows
    )
    return ('<div class="scroll"><table><thead><tr><th>일자</th>'
            f"{subject_head}<th>종류</th><th>항목</th><th>접수번호</th><th>출처</th>"
            f"</tr></thead><tbody>{body}</tbody></table></div>")


def _filings_page(conn, cutoff: datetime | None) -> str:
    if cutoff is None:
        return _no_cutoff_page("market-intel — 공시 이력", "공시 이력")
    rows = detail_mod.filings(conn, cutoff)
    parts = ["<h1>공시 이력</h1>", _cutoff_note(cutoff),
             '<p class="legend">정기공시(10-K·10-Q)·실적 수시공시(8-K)·기관 13F 제출을 '
             "한 줄로 모은 것입니다. 항목 칸의 숫자는 8-K의 사유 번호(2.02 = 실적 발표)입니다.</p>",
             _filing_table(rows, with_subject=True)]
    return _page("market-intel — 공시 이력", "".join(parts), depth=0)


def _holdings_page(conn, cutoff: datetime | None) -> str:
    if cutoff is None:
        return _no_cutoff_page("market-intel — 기관 보유", "기관(13F) 보유내역")
    groups = detail_mod.holdings_by_manager(conn, cutoff)
    parts = ["<h1>기관(13F) 보유내역</h1>", _cutoff_note(cutoff)]

    if not groups:
        # 빈 표를 "이 기관은 아무것도 안 들고 있다"로 읽히게 두면 안 된다.
        parts.append('<p class="banner warn">아직 읽어들인 보유내역이 없습니다 — '
                     "13F 제출은 감지했지만 보유 표를 아직 받지 못했습니다.</p>")
    else:
        # 13F는 **분기 말 기준을 45일 뒤에** 내는 서류다. 그 시차를 화면이
        # 말하지 않으면 독자는 이것을 지금 보유로 읽는다.
        parts.append('<p class="legend">13F는 분기 말 보유를 최대 45일 뒤에 신고합니다 — '
                     "각 표의 기준일은 <strong>그 분기 말</strong>이고 지금 보유가 아닙니다. "
                     "비중은 그 운용사 안에서의 몫입니다.</p>")
    for g in groups:
        rescaled = ('<span class="badge-late">천 달러 단위 신고 → 달러 환산</span>'
                    if g["rescaled"] else "")
        parts.append(f'<h2>{_esc(g["manager"])}</h2>'
                     f'<p class="meta">{_esc(g["period"])} 기준 · {len(g["holdings"])}종목 · '
                     f'합계 {_esc(g["total_text"])} {rescaled}</p>')
        rows_html = "".join(
            "<tr>"
            f'<td>{_esc(h["issuer"])}{" " + _esc(h["put_call"]) if h["put_call"] else ""}</td>'
            f'<td class="chg">{_esc(h["value_text"])}</td>'
            f'<td class="chg">{_esc(f"{h["weight"] * 100:.1f}%")}</td>'
            f'<td class="chg">{_esc(f"{h["amount"]:,.0f}" if h["amount"] is not None else "-")}'
            f'{" " + _esc(h["amount_unit"]) if h["amount"] is not None else ""}</td>'
            f'<td class="detail">{_esc(h["cusip"])}</td>'
            "</tr>"
            for h in g["holdings"]
        )
        parts.append('<div class="scroll"><table><thead><tr><th>종목</th><th>평가금액</th>'
                     f"<th>비중</th><th>수량</th><th>CUSIP</th></tr></thead>"
                     f"<tbody>{rows_html}</tbody></table></div>")
        parts.append(f'<p class="meta">{_source_cell(g["source_url"])}</p>')

    parts.append("<h2>제출 이력</h2>")
    parts.append(_filing_table(detail_mod.holdings_13f(conn, cutoff), with_subject=True))
    return _page("market-intel — 기관 보유", "".join(parts), depth=0)


def _detail_index_page(conn, cutoff: datetime | None, companies: dict[str, str],
                       macros: dict[str, str], macro_labels: dict[str, str]) -> str:
    if cutoff is None:
        return _no_cutoff_page("market-intel — 상세", "상세")
    parts = ["<h1>상세</h1>", _cutoff_note(cutoff),
             '<p class="legend">리포트가 "오늘 무엇이 달라졌나"라면 이쪽은 '
             '"그동안 어떻게 움직였나"입니다.</p>']

    parts.append("<h2>기업별 재무 추이</h2>")
    if companies:
        items = "".join(
            f'<li><a href="company/{_esc(slug)}.html">{_esc(detail_mod.name_ko(subject))}</a> '
            f'<span class="meta">{_esc(subject)}</span></li>'
            for subject, slug in sorted(companies.items(),
                                        key=lambda kv: detail_mod.name_ko(kv[0]))
        )
        parts.append(f'<ul class="cards">{items}</ul>')
    else:
        parts.append('<p class="meta">아직 재무·공시가 수집된 기업이 없습니다.</p>')

    parts.append("<h2>거시지표별</h2>")
    if macros:
        items = "".join(
            f'<li><a href="macro/{_esc(slug)}.html">{_esc(macro_labels.get(subject, subject))}</a> '
            f'<span class="meta">{_esc(subject)}</span></li>'
            for subject, slug in sorted(macros.items(),
                                        key=lambda kv: macro_labels.get(kv[0], kv[0]))
        )
        parts.append(f'<ul class="cards">{items}</ul>')
    else:
        parts.append('<p class="meta">아직 수집된 거시지표가 없습니다.</p>')

    parts.append("<h2>그 밖의 상세</h2>"
                 '<ul class="cards">'
                 '<li><a href="filings.html">공시 이력 타임라인</a></li>'
                 '<li><a href="holdings.html">기관(13F) 보유내역</a></li>'
                 "</ul>")
    return _page("market-intel — 상세", "".join(parts), depth=0)


# --- entry point ----------------------------------------------------------

def build_site(conn, reports_root: Path | None = None, docs_root: Path | None = None,
               now: datetime | None = None) -> dict:
    """Regenerate `docs/` in full. Returns the B13 `site build` counters.

    `now` is the wall clock **for the operational pages only** (`status.html`
    의 생성 시각·지연 판정, `theses.html`의 최근 90일 창) — spec SA-12가 명시한
    예외다. 리포트·일정 페이지의 기준선은 지금도 `schedule_cutoff()`, 즉 그
    리포트들의 정보차단선이며 이 인자는 거기에 닿지 않는다."""
    reports_root = Path(reports_root) if reports_root else PROJECT_ROOT / "reports"
    docs_root = Path(docs_root) if docs_root else PROJECT_ROOT / "docs"
    now = (now or datetime.now(KST)).astimezone(KST)

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

    ops_state = ops_mod.status(conn, now=now)
    banner = _banner(ops_state, ops_mod.thesis_changes(conn, now=now, days=BANNER_CHANGE_DAYS))
    (docs_root / "index.html").write_text(_index_page(entries, banner), encoding="utf-8")
    (docs_root / "archive.html").write_text(_archive_page(entries), encoding="utf-8")
    pages += 2

    cutoff = schedule_cutoff(entries)
    (docs_root / "schedule.html").write_text(_schedule_page(conn, cutoff), encoding="utf-8")
    (docs_root / "status.html").write_text(_status_page(ops_state), encoding="utf-8")
    (docs_root / "theses.html").write_text(_theses_page(conn, now), encoding="utf-8")
    pages += 3

    # 상세 층. 차단선이 없으면(리포트 0건) 개별 페이지는 만들지 않고 안내만
    # 남긴다 — 목록만 있고 링크가 전부 깨진 페이지보다 낫다.
    company_slugs = detail_mod.slug_map(detail_mod.companies(conn, cutoff)) if cutoff else {}
    macro_labels = detail_mod.macro_subjects(conn, cutoff) if cutoff else {}
    macro_slugs = detail_mod.slug_map(macro_labels)
    (docs_root / "detail.html").write_text(
        _detail_index_page(conn, cutoff, company_slugs, macro_slugs, macro_labels),
        encoding="utf-8")
    (docs_root / "filings.html").write_text(_filings_page(conn, cutoff), encoding="utf-8")
    (docs_root / "holdings.html").write_text(_holdings_page(conn, cutoff), encoding="utf-8")
    pages += 3

    for subject, name in company_slugs.items():
        out = docs_root / "company" / f"{name}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_company_page(conn, cutoff, subject), encoding="utf-8")
        pages += 1
    for subject, name in macro_slugs.items():
        out = docs_root / "macro" / f"{name}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_macro_page(conn, cutoff, subject), encoding="utf-8")
        pages += 1

    latest = f"{entries[0]['report'].report_type}/{entries[0]['stem']}" if entries else ""
    return {
        "pages": pages,
        "reports_indexed": len(entries),
        "latest": latest,
        "out": str(docs_root),
    }
