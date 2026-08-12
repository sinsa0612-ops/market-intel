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
    APPENDIX_SECTIONS,
    appendix_count,
    LEGEND_HTML,
    SECTOR_INDEX_NOTE,
    SECTOR_INDEX_TITLE,
    SECTOR_NOTE,
    SECTOR_TITLE,
    arrow,
    direction,
    PALE_BELOW,
    filing_summary,
    flow_groups,
    fmt_pct,
    heading,
    safe_href,
    sections,
    split_unchanged,
    status_ko,
)

NO_DATA_HTML = "<p>(해당 없음)</p>"

# 스파크라인 좌표계(승인된 시안과 동일). 세로 3~28은 선이 위아래로 잘리지
# 않게 남긴 여백이다.
SPARK_W, SPARK_H = 110, 30
SPARK_TOP, SPARK_BOTTOM = 3.0, 28.0


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


# --- 차트 (CEO 지시 2026-08-12) --------------------------------------------
#
# 스파크라인과 같은 규칙을 따른다: 외부 라이브러리·CDN·<script> 없이 인라인 SVG,
# 색은 인라인이 아니라 클래스에 맡긴다(인라인 색은 다크모드 미디어쿼리를 빠져나간다).
#
# 다만 스파크라인과 **다른 점이 하나** 있다. 스파크라인은 옆 칸의 숫자를 거드는
# 장식이라 `aria-hidden`이지만, 이 그림들은 그 자체가 정보다 — 그래서
# `role="img"` + `<title>`/`<desc>`를 달고, 그림 아래에 같은 내용을 문장으로도
# 싣는다(`ChartBlock.note`). 흑백 출력·화면 낭독기에서 그림만 사라지면 그 자리가
# 통째로 말을 못 하게 되기 때문이고, 이건 수급 막대에서 이미 정한 원칙이다.
CHART_W, CHART_H = 640, 190
CHART_PAD_L, CHART_PAD_R = 34.0, 8.0
CHART_PAD_T, CHART_PAD_B = 12.0, 26.0


def _chart_x(i: int, n: int) -> float:
    """i번째 눈금의 x 좌표. 눈금이 하나뿐이면 가운데에 둔다(0으로 나누지 않게)."""
    inner = CHART_W - CHART_PAD_L - CHART_PAD_R
    if n <= 1:
        return CHART_PAD_L + inner / 2
    return CHART_PAD_L + inner * i / (n - 1)


def _chart_scale(values: list[float], symmetric: bool) -> tuple[float, float]:
    """(lo, hi). `symmetric`이면 0을 가운데 두어 위/아래 길이를 견줄 수 있게 한다 —
    다이버징 막대에서 위아래 배율이 다르면 '더 큰 쪽'이 눈으로 뒤집힌다."""
    vals = [v for v in values if v is not None]
    if not vals:
        return 0.0, 1.0
    lo, hi = min(vals), max(vals)
    if symmetric:
        m = max(abs(lo), abs(hi)) or 1.0
        return -m, m
    if lo == hi:  # 완전히 평평한 계열
        return lo - 1.0, hi + 1.0
    return lo, hi


def _chart_y(v: float, lo: float, hi: float) -> float:
    span = (hi - lo) or 1.0
    return CHART_PAD_T + (hi - v) / span * (CHART_H - CHART_PAD_T - CHART_PAD_B)


def _date_ticks(dates: list[str]) -> str:
    """x축 눈금은 처음·가운데·끝 셋만 — 매일 찍으면 글자가 겹쳐 못 읽는다."""
    n = len(dates)
    if not n:
        return ""
    picks = {0, n // 2, n - 1}
    out = []
    for i in sorted(picks):
        label = dates[i][5:].replace("-", "/")  # 2026-08-03 -> 08/03
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        out.append(f'<text class="tick" x="{_chart_x(i, n):.1f}" y="{CHART_H - 8}" '
                   f'text-anchor="{anchor}">{_esc(label)}</text>')
    return "".join(out)


def _chart_frame(body: str, block, desc: str) -> str:
    title = f"{block.title} ({block.unit})" if block.unit else block.title
    return (
        f'<figure class="chart chart-{_esc(block.kind)}">'
        f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
        f'aria-labelledby="t-{_esc(block.kind)} d-{_esc(block.kind)}">'
        f'<title id="t-{_esc(block.kind)}">{_esc(title)}</title>'
        f'<desc id="d-{_esc(block.kind)}">{_esc(desc)}</desc>'
        f"{body}</svg>"
        + (f'<figcaption>{_esc(block.note)}</figcaption>' if block.note else "")
        + "</figure>"
    )


def _diverging_bars_svg(block) -> str:
    """0축 위/아래로 갈라지는 막대. `breadth`와 `flows`가 같은 그림을 쓴다.

    **부호를 색이 아니라 위치가 전달한다** — 흑백으로 뽑아도, 색을 못 보는
    사람에게도 위/아래는 남는다. 색은 거들 뿐이다.
    """
    n = len(block.dates)
    if not n or not block.series:
        return ""
    flat = [v for s in block.series for v in s.values if v is not None]
    if not flat:
        return ""
    lo, hi = _chart_scale(flat, symmetric=True)
    zero_y = _chart_y(0.0, lo, hi)

    group = len(block.series)
    inner = CHART_W - CHART_PAD_L - CHART_PAD_R
    slot = inner / max(n, 1)
    bar_w = max(1.5, min(9.0, slot / (group + 1)))

    parts = [f'<line class="axis" x1="{CHART_PAD_L}" y1="{zero_y:.1f}" '
             f'x2="{CHART_W - CHART_PAD_R}" y2="{zero_y:.1f}"/>']
    for si, s in enumerate(block.series):
        offset = (si - (group - 1) / 2) * bar_w
        for i, v in enumerate(s.values[:n]):
            if v is None:
                continue
            x = _chart_x(i, n) + offset - bar_w / 2
            y = _chart_y(v, lo, hi)
            top, height = (min(y, zero_y), abs(zero_y - y))
            parts.append(f'<rect class="s{si}" x="{x:.1f}" y="{top:.1f}" '
                         f'width="{bar_w:.1f}" height="{max(height, 0.6):.1f}"/>')
    for s in block.overlay:
        pts = [(_chart_x(i, n), _chart_y(v, lo, hi))
               for i, v in enumerate(s.values[:n]) if v is not None]
        # 겹치는 점은 **다른 배율의 값**이라 선으로 잇지 않는다 — 이으면 막대와
        # 같은 축에서 읽히는 두 번째 시계열처럼 보인다.
        parts += [f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="2.6"/>' for x, y in pts]
    parts.append(_date_ticks(block.dates))
    return "".join(parts)


def _rebased_lines_svg(block) -> str:
    """기준일=100 꺾은선. 계열마다 선 끝에 이름을 직접 붙인다 — 범례를 따로 두면
    눈이 그림과 범례 사이를 오가야 하고, 흑백에서는 어느 선이 누구인지 못 가린다."""
    n = len(block.dates)
    if n < 2 or not block.series:
        return ""
    flat = [v for s in block.series for v in s.values if v is not None]
    if not flat:
        return ""
    lo, hi = _chart_scale(flat + [100.0], symmetric=False)
    base_y = _chart_y(100.0, lo, hi)
    parts = [f'<line class="axis base" x1="{CHART_PAD_L}" y1="{base_y:.1f}" '
             f'x2="{CHART_W - CHART_PAD_R}" y2="{base_y:.1f}"/>',
             f'<text class="tick" x="{CHART_PAD_L - 4}" y="{base_y + 3:.1f}" '
             f'text-anchor="end">100</text>']
    for si, s in enumerate(block.series):
        pts = [(_chart_x(i, n), _chart_y(v, lo, hi))
               for i, v in enumerate(s.values[:n]) if v is not None]
        if len(pts) < 2:
            continue
        d = " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        # 선 종류를 계열마다 다르게 — 색을 못 쓰는 곳에서도 갈린다.
        parts.append(f'<path class="line s{si}" d="M{d}"/>')
        ex, ey = pts[-1]
        parts.append(f'<text class="lbl s{si}" x="{ex - 4:.1f}" y="{ey - 5:.1f}" '
                     f'text-anchor="end">{_esc(s.label)}</text>')
    parts.append(_date_ticks(block.dates))
    return "".join(parts)


def chart_svg(block) -> str:
    """`ChartBlock` -> 인라인 SVG. 그릴 것이 없으면 **빈 문자열**이다
    (`sparkline_svg`와 같은 관례 — 없는 추세를 그리지 않는다)."""
    if block.kind in ("breadth", "flows"):
        body = _diverging_bars_svg(block)
    elif block.kind == "rebased":
        body = _rebased_lines_svg(block)
    else:
        return ""
    if not body:
        return ""
    desc = block.note or block.title
    return _chart_frame(body, block, desc)


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


# "오늘 유별난 것" 추이 그래프 좌표계(spec 20260806-report-visual §1①-3).
# 스파크라인과 달리 y축을 0~100(상승비율 %) **고정**으로 둔다 — min/max로
# 늘리면 48%->52% 같은 잔물결도 꽉 찬 그래프가 되어 "오늘이 얼마나 먼지"를
# 실제보다 과장해서 보여준다.
TREND_W, TREND_H = 600, 140
TREND_TOP, TREND_BOTTOM = 10.0, 110.0


# 추세를 드러내는 이동평균 창. 20거래일 = 약 한 달 — 하루하루의 톱니는
# 지우고 국면 변화는 남긴다.
_TREND_MA_WINDOW = 20


def unusual_trend_svg(series: list[float]) -> str:
    """상승비율 추이(오래된 값 -> 최신 값) -> 인라인 SVG, 마지막 점(오늘)을
    강조한다. 점이 2개 미만이면 빈 문자열 — `sparkline_svg`와 같은 이유로
    없는 추세를 그리지 않는다. 외부 라이브러리·CDN·<script> 없음(spec §3,
    `tests/reporting/test_visual_readability.py`가 이 정책을 못박는다)."""
    points = [v for v in series if v is not None]
    if len(points) < 2:
        return ""
    step = (TREND_W - 2) / (len(points) - 1)

    def y_of(v: float) -> float:
        v = max(0.0, min(100.0, v))
        return TREND_BOTTOM - (v / 100.0) * (TREND_BOTTOM - TREND_TOP)

    coords = [(1 + i * step, y_of(v)) for i, v in enumerate(points)]
    line = " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)

    # **원시선만 그리면 오늘 점이 톱니에 묻힌다.** 484개 일별 점은 매일
    # 30~70%를 오가서 화면에서는 잡음 덩어리이고, 그 위에 찍은 오늘 점은
    # "높은 데 있네" 이상을 말해주지 못한다(심사 지적 2026-08-06). 원시선은
    # 옅게 깔고 **20일 이동평균을 굵게** 얹어 추세를 드러낸다 — 오늘 점이
    # 그 추세선 대비 어디인지가 CEO가 보려는 것이다.
    smooth = ""
    if len(points) >= _TREND_MA_WINDOW:
        ma = []
        for i in range(_TREND_MA_WINDOW - 1, len(points)):
            window = points[i - _TREND_MA_WINDOW + 1:i + 1]
            ma.append((1 + i * step, y_of(sum(window) / len(window))))
        smooth = '<path class="ma" d="M' + " L".join(f"{x:.1f},{y:.1f}" for x, y in ma) + '"/>'

    mid_y = y_of(50.0)
    end_x, end_y = coords[-1]
    today = points[-1]
    cls = "up" if today > 50 else ("down" if today < 50 else "flat")
    # y축 눈금: 0/50/100이 어디인지 없으면 세로 위치가 아무 뜻이 없다.
    axis = "".join(
        f'<text class="ax" x="{TREND_W - 2}" y="{y_of(v) + 3:.1f}">{v:g}</text>'
        for v in (100, 50, 0)
    )
    return (
        f'<svg class="trend {cls}" viewBox="0 0 {TREND_W} {TREND_H}" aria-hidden="true">'
        f'<line class="mid" x1="1" y1="{mid_y:.1f}" x2="{TREND_W - 1}" y2="{mid_y:.1f}"/>'
        f'<path class="line" d="M{line}"/>{smooth}{axis}'
        f'<circle class="today" cx="{end_x:.1f}" cy="{end_y:.1f}" r="6"/>'
        "</svg>"
    )


def _movers_html(rows: list[FactRow]) -> str:
    """오늘 가장 크게 움직인 것 — 이름 + 좌우 막대 + 값(spec §1①-4). 막대는
    가운데(0%)에서 위/아래 방향으로 자라며, 길이는 이 5개 중 가장 큰 값 대비
    비중이다. 색만으로 방향을 말하지 않는다 — 화살표·부호가 딸린 값이 항상
    같이 나간다(기존 규약과 동일, 같은 테스트가 검사)."""
    if not rows:
        return ""
    peak = max((abs(r.delta_pct) for r in rows if r.delta_pct is not None), default=0.0) or 1.0
    items = []
    for r in rows:
        d = direction(r.delta_pct)
        share = min(1.0, abs(r.delta_pct or 0.0) / peak)
        side = "pos" if (r.delta_pct or 0) >= 0 else "neg"
        bar = f'<span class="mv-bar {side}" style="width:{share * 50:.1f}%"></span>'
        # **비교 기준을 감추지 않는다.** 값만 찍으면 `3일 전 종가 대비 -8.95%`가
        # 화면에서 `-8.95%`가 되어 사흘치 하락이 하루치로 읽힌다(심사 실측
        # 2026-08-06, close_delta 2026-08-03의 삼성전자). 마크다운은 `comparison`을
        # 그대로 쓰는데 여기만 안 쓰고 있었다 — 명세가 경고한 "한쪽만 고치면
        # 다른 쪽이 조용히 옛 모양으로 남는다"가 그대로 일어난 자리다.
        value = f'<span class="arrow">{arrow(r.delta_pct)}</span>{_esc(r.comparison)}'
        items.append(
            f'<div class="mv-row"><span class="mv-label">{_esc(r.label)}</span>'
            f'<span class="mv-track">{bar}</span>'
            f'<span class="mv-val {d}">{value}</span></div>'
        )
    return f'<div class="movers">{"".join(items)}</div>'


def _unusual_day_html(block: dict) -> str:
    parts = []
    if block["headline"]:
        escaped = _esc(block["headline"]).replace("\n", "<br>")
        parts.append(f'<p class="unusual-headline">{escaped}</p>')
    series = block["trend_series"]
    svg = unusual_trend_svg(series)
    if svg:
        label = block["trend_label"] or "상승비율"
        parts.append(f'<p class="trend-caption">{_esc(label)} — 최근 {len(series)}거래일, 굵은 점이 오늘</p>')
        parts.append(svg)
    if block["top_movers"]:
        parts.append('<p class="movers-caption">오늘 가장 크게 움직인 것</p>')
        parts.append(_movers_html(block["top_movers"]))
    return f'<div class="unusual">{"".join(parts)}</div>' if parts else ""


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


def _facts_table_html_raw(rows: list[FactRow]) -> str:
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
        # 문서가 있으면 문서를 건다. `source_url`(수집 엔드포인트)은 클릭하면
        # JSON이 뜬다 — 감사용으로 리포트 JSON에는 남지만 화면 링크는 아니다.
        href = safe_href(r.doc_url) or safe_href(r.source_url)
        if href:
            text = "공시원문" if safe_href(r.doc_url) else "원자료"
            title = f' title="접수번호 {_esc(str(r.raw_value))}"' if r.doc_url and r.raw_value else ""
            src = f'<a href="{_esc(href)}"{title} rel="noopener" target="_blank">{text}</a>'
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


def _facts_table_html(rows: list[FactRow]) -> str:
    """spec 20260810-period-report §1③-1: 등락이 사실상 0인 행은
    `<details>`로 접는다(§2 규칙4 "접은 것은 접었다고 밝혀라" — 개수를 보이고
    펼칠 수 있게)."""
    if not rows:
        return "<p>(해당 없음)</p>"
    changed, unchanged = split_unchanged(rows)
    parts = []
    if changed:
        parts.append(_facts_table_html_raw(changed))
    elif not unchanged:
        return "<p>(해당 없음)</p>"
    if unchanged:
        parts.append(f"<details><summary>변화 없음 {len(unchanged)}건 펼치기</summary>"
                     f"{_facts_table_html_raw(unchanged)}</details>")
    return "".join(parts)


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
    맨 윗줄이 그날 주도 업종이다. 색·화살표·스파크라인 규약은 사실 표와 동일.

    spec 20260810-period-report §1③-4: 설명 문구는 그대로 두고 표만 접는다
    (`render_md._sector_index_table_md` 주석과 같은 이유 — "시장 반응"
    섹션의 실측 2,300px 중 이 표가 큰 몫이다)."""
    note = f'<p class="meta">{_esc(SECTOR_INDEX_NOTE)}</p>'
    if not groups:
        return note + "<p>(관측 없음 — 차단선 이전에 알려진 업종 지수 종가가 없습니다)</p>"
    n = sum(len(rows) for _, rows in groups)
    body_parts = []
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
        body_parts.append(f'<p class="group">{_esc(market_label)}</p>')
        body_parts.append(_scroll(
            "<table><thead><tr><th>업종</th><th>수치</th><th>등락</th>"
            f'{spark_head}<th>원자료</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
        ))
    return (note + f"<details><summary>업종 지수 {n}개 펼치기</summary>"
            f'{"".join(body_parts)}</details>')


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
    # spec 20260810-period-report §1③-4: 표만 접는다(이유는
    # `_sector_index_table_html` 주석과 같다), 설명 문구는 그대로 둔다.
    return (f"<details><summary>업종 묶음 {len(rows)}개 펼치기</summary>{_scroll(table)}</details>"
            f'<p class="meta">{_esc(SECTOR_NOTE)}</p>')


def _calendar_table_html(columns: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p>(해당 없음)</p>"
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    return _scroll(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


FLOW_LEGEND = ("막대 길이는 종목 안에서의 비중, 색이 진할수록 금액이 큽니다 "
               "(빨강 = 순매수 · 파랑 = 순매도, 화면에서 가장 큰 금액이 가장 진합니다). "
               "개인·기관·외국인의 순매수를 더하면 0이므로, 값 하나하나보다 "
               "어느 쪽이 사고 어느 쪽이 파는지가 정보입니다.")


def _flow_group_html(g: dict) -> str:
    out = ['<div class="flow">'
           f'<div class="name">{_esc(g["name"])}'
           f'<span class="story">{_esc(g["story"])}</span></div>']
    if g["quiet"]:
        out.append('<div class="bar"><span class="zero">움직임 작음</span></div>')
    else:
        # `--a`(진하기)는 인라인으로 나갈 수밖에 없지만 **색 자체는 인라인이
        # 아니다** — 스타일시트가 `--up`/`--down`과 섞으므로 다크모드 팔레트가
        # 그대로 적용된다(인라인 색은 prefers-color-scheme을 그냥 빠져나간다).
        bars = "".join(
            f'<span class="{"buy" if a["buy"] else "sell"}'
            f'{" pale" if a["intensity"] < PALE_BELOW else ""}" '
            f'style="flex:{abs(a["value"]) / g["total"]:.4f};--a:{a["intensity"]}">'
            f'{_esc(a["label"])} {_esc(a["text"])}</span>'
            for a in g["actors"] if a["value"]
        )
        out.append(f'<div class="bar">{bars}</div>')
    out.append("</div>")
    return "".join(out)


def _flow_html(groups: list[dict]) -> str:
    """수급 — 종목 하나에 막대 하나. 표에서는 안 보이던 "누가 사고 누가
    팔았나"가 이 모양에서는 첫눈에 읽힌다.

    막대는 장식이 아니라 데이터이므로 **문장이 항상 함께 나간다**: 흑백
    출력·화면 낭독기·색각이상에서 막대만 남으면 그 줄은 아무 말도 못 한다
    (색만으로 정보를 주지 않는다는 이 프로젝트의 규약과 같다).

    spec 20260810-period-report §1③-1: "움직임 작음"(`quiet`)도 안 움직인
    행과 같은 사정이다 — 접는다(§2 규칙4: 개수를 보이고 펼칠 수 있게)."""
    if not groups:
        return "<p>(해당 없음)</p>"
    active = [g for g in groups if not g["quiet"]]
    quiet = [g for g in groups if g["quiet"]]
    out = [f'<p class="legend">{_esc(FLOW_LEGEND)}</p>']
    out += [_flow_group_html(g) for g in active]
    if quiet:
        out.append(f'<details><summary>움직임 작음 {len(quiet)}건 펼치기</summary>'
                   f'{"".join(_flow_group_html(g) for g in quiet)}</details>')
    return "".join(out)


def _macro_cards_html(rows: list[FactRow], rest: list[FactRow]) -> str:
    """거시지표 — 값 하나짜리 관측이라 표의 다섯 칸 중 넷이 빈다. 카드가 맞다."""
    if not rows:
        return "<p>(해당 없음)</p>"
    cards = []
    for r in rows:
        d = direction(r.delta_pct)
        # 동결은 "― +0.00%"가 아니라 "― 변화 없음"이다. 기준금리처럼 몇 달째
        # 그대로인 값이 매일 소수점 둘째 자리까지 0을 찍으면, 읽는 사람은
        # 그것도 움직인 값으로 훑게 된다.
        # 단위는 리포트가 정해서 실어 보낸다(`FactRow.delta_unit`) — 금리·실업률은
        # `%p`다. 렌더러가 `unit` 문자열을 보고 추측하면 같은 판단이 두 곳에
        # 생기고, 한쪽만 고쳐지는 날 화면과 문구가 어긋난다.
        change = ({"": "―", "flat": "― 변화 없음"}.get(d)
                  or f"{arrow(r.delta_pct)} {fmt_pct(r.delta_pct, r.delta_unit)}")
        cards.append(f'<div class="card"><div class="k">{_esc(r.label)}</div>'
                     f'<div class="v">{_esc(r.value)}</div>'
                     f'<div class="c {d}">{_esc(change)}</div></div>')
    html = f'<div class="mgrid">{"".join(cards)}</div>'
    if rest:
        html += (f"<details><summary>나머지 거시지표 {len(rest)}개 펼치기</summary>"
                 f"{_facts_table_html(rest)}</details>")
    return html


def _filing_summary_html(rows: list[FactRow]) -> str:
    s = filing_summary(rows)
    if not s["total"]:
        return "<p>(해당 없음)</p>"
    line = (f'<p><strong>공시 {s["total"]}건</strong> — 실적 발표 {s["earnings_count"]} · '
            f'그 밖의 정기공시·13F {s["other_count"]}.')
    if s["earnings_subjects"]:
        line += f' 실적을 낸 곳: {_esc(" · ".join(s["earnings_subjects"]))}.'
    line += "</p>"
    return (line + f"<details><summary>공시 {s['total']}건 전체 펼치기</summary>"
            f"{_facts_table_html(rows)}</details>")


def _block_html(block: dict) -> str:
    kind = block["kind"]
    if kind == "chart":
        return chart_svg(block["block"])
    if kind == "unusual_day":
        return _unusual_day_html(block)
    if kind == "facts":
        return _facts_table_html(block["rows"])
    if kind == "flow":
        return _flow_html(flow_groups(block["rows"]))
    if kind == "macro_cards":
        return _macro_cards_html(block["rows"], block["rest"])
    if kind == "filing_summary":
        return _filing_summary_html(block["rows"])
    if kind == "hero":
        return _hero_html(block["rows"])
    if kind == "legend":
        return f'<p class="legend">{_esc(LEGEND_HTML)}</p>'
    if kind == "breadth":
        # 시장 폭이 여러 줄이면(관측기업 + 코스피 + 코스닥) 줄바꿈이 살아야
        # 한다 — `_esc` 다음에 개행만 `<br>`로 바꾼다(내용은 이미 이스케이프됐다).
        if not block["text"]:
            return ""
        escaped = _esc(block["text"]).replace("\n", "<br>")
        return f'<p class="breadth">{escaped}</p>'
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
        bodies = [b for b in (_block_html(block) for block in blocks) if b]
        # spec §1③-2 "부록을 맨 뒤로" — 크기가 아니라 접기로 대응한다
        # (render_md.APPENDIX_SECTIONS 주석 참조).
        if header in APPENDIX_SECTIONS and bodies and bodies != [NO_DATA_HTML]:
            combined = "".join(bodies)
            n = appendix_count(blocks)
            label = f"{header} {n}건 펼치기" if n else f"{header} 펼치기"
            parts.append(f"<details><summary>{_esc(label)}</summary>{combined}</details>")
        else:
            parts += bodies
        parts.append("</section>")
    body = "".join(parts)
    return f'<article class="mi-report" data-report-type="{_esc(report.report_type)}">{body}</article>'
