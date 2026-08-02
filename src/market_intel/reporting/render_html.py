"""HTML renderer — same `Report` in, HTML tags out instead of markdown
syntax. No markdown-to-HTML conversion anywhere (spec B0) and no template
engine (jinja2 etc. excluded, spec B0) — this is plain f-string HTML.

Section *markup* is independent from `render_md.py` (spec ST2 What #5:
"같은 Report에서 독립 렌더"); the section *layout* is deliberately not
duplicated — both renderers consume `render_md.sections()`, a public,
markup-free description of what goes where. Two hand-kept copies of the
layout is exactly what silently dropped two interpretation fields from
two report types in both formats at once (judge.md ④).

**Every externally-derived string is `html.escape()`d** (spec B12: release
names, company names, filing labels all originate from a public HTTP
response and are an XSS surface once they reach `docs/`). Escaping is not
enough for a URL — `html.escape` leaves `javascript:` intact — so hrefs
additionally go through `render_md.safe_href`'s scheme allowlist.

Produces a `<article>…</article>` fragment, not a full HTML document —
page chrome (`<head>`, nav, `style.css` link, `docs/index.html`'s "recent
20" card list) is `site.py`'s job (ST3, spec B8), which is why this
returns embeddable content rather than `<!doctype html>`.
"""
from __future__ import annotations

import html as html_mod

from .model import FactRow, Report, SectorSummary
from .render_md import (
    LEGEND_HTML,
    SECTOR_INDEX_NOTE,
    SECTOR_INDEX_TITLE,
    SECTOR_NOTE,
    SECTOR_TITLE,
    arrow,
    direction,
    fmt_pct,
    heading,
    safe_href,
    sections,
    status_ko,
)

# 스파크라인 좌표계(승인된 시안과 동일). 세로 3~28은 선이 위아래로 잘리지
# 않게 남긴 여백이다.
SPARK_W, SPARK_H = 110, 30
SPARK_TOP, SPARK_BOTTOM = 3.0, 28.0


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


def sparkline_svg(series: list[float], direction_class: str) -> str:
    """최근 종가 시계열 -> 인라인 SVG. 외부 라이브러리·CDN·<script>는 쓰지
    않는다(사이트 정책, spec B8).

    점이 2개 미만이면 **빈 문자열**을 낸다 — 거시지표처럼 관측이 1개뿐인
    계열에 억지로 선을 그으면 없는 추세를 그린 것이 된다.

    색은 인라인으로 넣지 않고 `.spark.up`/`.spark.down` 클래스 + `currentColor`
    에 맡긴다. 인라인 색은 다크모드 미디어쿼리를 그냥 빠져나간다.

    좌표는 전부 이 함수가 계산한 float이라 이스케이프 대상이 아니다(외부
    문자열이 SVG 속성으로 들어가는 경로가 없다).
    """
    points = [v for v in series if v is not None]
    if len(points) < 2:
        return ""
    lo, hi = min(points), max(points)
    span = hi - lo
    step = (SPARK_W - 2) / (len(points) - 1)
    mid = (SPARK_TOP + SPARK_BOTTOM) / 2
    coords = []
    for i, v in enumerate(points):
        x = 1 + i * step
        # 완전 평평한 계열(span == 0)은 0으로 나누지 않고 가운데 직선으로.
        y = mid if span == 0 else SPARK_TOP + (hi - v) / span * (SPARK_BOTTOM - SPARK_TOP)
        coords.append((x, y))
    line = " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    x0, xn = coords[0][0], coords[-1][0]
    area = f"M{x0:.1f},{SPARK_H} L{line} L{xn:.1f},{SPARK_H} Z"
    end_x, end_y = coords[-1]
    cls = f"spark {direction_class}".strip()
    return (
        f'<svg class="{_esc(cls)}" viewBox="0 0 {SPARK_W} {SPARK_H}" width="{SPARK_W}" '
        f'height="{SPARK_H}" aria-hidden="true">'
        f'<path class="area" d="{area}"/>'
        f'<path class="line" d="M{line}"/>'
        f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="2.2"/>'
        "</svg>"
    )


def _change_cell(delta_pct: float | None, text: str) -> str:
    """등락 셀: 색(.up/.down) + 화살표를 항상 함께. 색만 붙이면 흑백·색각이상
    에서 그 셀은 아무 말도 하지 않는다."""
    d = direction(delta_pct)
    if not d:
        return f"<td>{_esc(text)}</td>"
    return (f'<td class="chg {d}"><span class="arrow">{arrow(delta_pct)}</span>'
            f"{_esc(text)}</td>")


def _scroll(table: str) -> str:
    """좁은 화면(아이폰)에서 표가 페이지를 밀어내지 않도록 표만 가로
    스크롤시킨다 — `site.py`가 이미 운영 페이지에 쓰는 `.scroll` 관행."""
    return f'<div class="scroll">{table}</div>'


def _facts_table_html(rows: list[FactRow]) -> str:
    if not rows:
        return "<p>(해당 없음)</p>"
    # 그릴 시계열이 하나도 없는 표(거시지표·재무제표)에는 빈 '추이' 칸을
    # 만들지 않는다 — 375px 화면에서 빈 열은 순수한 손해다.
    has_spark = any(len(r.series) >= 2 for r in rows)
    spark_head = "<th>추이</th>" if has_spark else ""
    out = [
        "<table><thead><tr><th>항목</th><th>수치</th><th>비교</th>"
        f"{spark_head}<th>원자료</th></tr></thead><tbody>"
    ]
    for r in rows:
        badge = status_ko(r.data_status)
        cls = "status-warn" if r.data_status in ("partial", "unverified") else "status-ok"
        value_cell = f'{_esc(r.value)} <span class="{cls}">{_esc(badge)}</span>' if badge else _esc(r.value)
        href = safe_href(r.source_url)
        if href:
            src = f'<a href="{_esc(href)}" rel="noopener" target="_blank">원자료</a>'
        else:
            # Not a link: an unsupported scheme (javascript:, data:, …) or a
            # relative/blank value. Shown as text so nothing is silently lost.
            src = _esc(r.source_url) if r.source_url else "-"
        spark_cell = (f'<td class="sp">{sparkline_svg(r.series, direction(r.delta_pct))}</td>'
                      if has_spark else "")
        out.append(
            f"<tr><td>{_esc(r.label)}</td><td>{value_cell}</td>"
            f"{_change_cell(r.delta_pct, r.comparison)}{spark_cell}<td>{src}</td></tr>"
        )
    out.append("</tbody></table>")
    return _scroll("".join(out))


def _hero_html(rows: list[FactRow]) -> str:
    """"오늘 올랐나 내렸나"에 스크롤 없이 답하는 카드 줄. 좁은 화면에서는
    `flex-wrap`으로 접힌다(스타일은 site.py)."""
    if not rows:
        return ""
    cards = []
    for r in rows:
        d = direction(r.delta_pct)
        change = (f'<span class="arrow">{arrow(r.delta_pct)}</span>{_esc(r.comparison)}'
                  if d else _esc(r.comparison))
        cards.append(
            f'<div class="card"><div class="k">{_esc(r.label)}</div>'
            f'<div class="v">{_esc(r.value)}</div>'
            f'<div class="c {d}">{change}</div></div>'
        )
    return f'<div class="hero">{"".join(cards)}</div>'


def _sector_index_table_html(groups) -> str:
    """업종 지수 표(HTML). 시장별로 표를 나누고, 각 표는 등락률 내림차순이라
    맨 윗줄이 그날 주도 업종이다. 색·화살표·스파크라인 규약은 사실 표와 동일."""
    parts = [f'<p class="meta">{_esc(SECTOR_INDEX_NOTE)}</p>']
    if not groups:
        parts.append("<p>(관측 없음 — 차단선 이전에 알려진 업종 지수 종가가 없습니다)</p>")
        return "".join(parts)
    for market_label, rows in groups:
        has_spark = any(len(r.series) >= 2 for r in rows)
        spark_head = "<th>추이</th>" if has_spark else ""
        body = []
        for r in rows:
            badge = status_ko(r.data_status)
            cls = "status-warn" if r.data_status in ("partial", "unverified") else "status-ok"
            value_cell = (f'{_esc(r.value)} <span class="{cls}">{_esc(badge)}</span>'
                          if badge else _esc(r.value))
            href = safe_href(r.source_url)
            src = (f'<a href="{_esc(href)}" rel="noopener" target="_blank">원자료</a>'
                   if href else (_esc(r.source_url) if r.source_url else "-"))
            spark_cell = (f'<td class="sp">{sparkline_svg(r.series, direction(r.delta_pct))}</td>'
                          if has_spark else "")
            body.append(
                f"<tr><td>{_esc(r.label)}</td><td>{value_cell}</td>"
                f"{_change_cell(r.delta_pct, r.comparison)}{spark_cell}<td>{src}</td></tr>"
            )
        parts.append(f'<p class="group">{_esc(market_label)}</p>')
        parts.append(_scroll(
            "<table><thead><tr><th>업종</th><th>수치</th><th>등락</th>"
            f'{spark_head}<th>원자료</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
        ))
    return "".join(parts)


def _sector_table_html(rows: list[SectorSummary]) -> str:
    if not rows:
        return ""
    body = []
    for s in rows:
        if not s.total:
            body.append(f"<tr><td>{_esc(s.sector)}</td><td>-</td><td>-</td>"
                        "<td>관측 없음</td></tr>")
            continue
        count = f"{s.total}종목" + (" (표본 적음)" if s.small_sample else "")
        body.append(
            f"<tr><td>{_esc(s.sector)}</td>"
            f'<td>{s.up}<span class="up">↑</span> / {s.down}<span class="down">↓</span></td>'
            f"{_change_cell(s.median_pct, fmt_pct(s.median_pct))}"
            f"<td>{_esc(count)}</td></tr>"
        )
    table = ("<table><thead><tr><th>업종</th><th>상승/하락</th><th>중앙값</th>"
             f'<th>종목</th></tr></thead><tbody>{"".join(body)}</tbody></table>')
    return _scroll(table) + f'<p class="meta">{_esc(SECTOR_NOTE)}</p>'


def _calendar_table_html(columns: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p>(해당 없음)</p>"
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    return _scroll(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


def _block_html(block: dict) -> str:
    kind = block["kind"]
    if kind == "facts":
        return _facts_table_html(block["rows"])
    if kind == "hero":
        return _hero_html(block["rows"])
    if kind == "legend":
        return f'<p class="legend">{_esc(LEGEND_HTML)}</p>'
    if kind == "breadth":
        return f'<p class="breadth">{_esc(block["text"])}</p>' if block["text"] else ""
    if kind == "sector":
        return _sector_table_html(block["rows"])
    if kind == "sector_index":
        return _sector_index_table_html(block["groups"])
    if kind == "subheading":
        return f"<h3>{_esc(block['text'])}</h3>" if block["text"] else ""
    if kind == "calendar":
        return _calendar_table_html(block["columns"], block["rows"])
    if kind == "missing":
        if not block["items"]:
            return ""
        items = "".join(f"<li>결측: {_esc(m.area)} — {_esc(m.reason)}</li>" for m in block["items"])
        return f"<ul>{items}</ul>"
    if kind == "interp":
        if block["badge"]:
            return f'<p class="ai-badge">{_esc(block["badge"])}</p><p>{_esc(block["text"])}</p>'
        return f"<p>{_esc(block['text'])}</p>"
    return f"<p>{_esc(block['text'])}</p>"


def render_html(report: Report) -> str:
    head = heading(report)
    parts = [f"<h1>{_esc(head['title'])}</h1>"]
    parts += [f"<p>{_esc(line)}</p>" for line in head["meta"]]
    for header, blocks in sections(report):
        parts.append(f"<section><h2>{_esc(header)}</h2>")
        parts += [_block_html(block) for block in blocks]
        parts.append("</section>")
    body = "".join(parts)
    return f'<article class="mi-report" data-report-type="{_esc(report.report_type)}">{body}</article>'
