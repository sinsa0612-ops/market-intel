"""Markdown renderer + **the single layout description both renderers
share** — the `Report` -> §4.2/§6.2/§7.2 formats. No markdown *parsing*
library is used anywhere (spec B0); this only ever emits.

`sections(report)` is the format-agnostic layout: an ordered list of
`(header, [block, …])`. This module turns each block into markdown,
`render_html.py` turns the very same blocks into HTML, and neither parses
the other's output (spec ST2 What #5 is about markup independence, not
about duplicating the *layout* twice). It exists because the previous
per-type render functions — 6 here plus 6 there, each naming the
`Interpretation` fields itself — dropped `당시 해석` and `기존 가설 영향`
from `weekly_review` and `event` in both renderers at once, while every
test stayed green because only `morning` was ever checked (judge.md ④).
With one layout, a 2B field is added in one place and a per-type omission
is structurally impossible.

Per-type section choice (design freedom this subtask has — spec ST2 What
#4 leaves the exact template mapping open beyond §4.2/§6.2/§7.2):
  morning / week_start / close_delta -> §4.2 (일간 공통 포맷), the 7 headers
      in the order the ST2 success criteria pin down word for word.
  weekly_review                      -> §6.2's 5 headers verbatim, in order.
  monthly                            -> no template exists in the source doc
      (§6.3 only gives an indicator table + a regime label) — built here as
      a regime headline + indicator table + asset performance.
  quarterly / annual                 -> a Core16-wide rollup (no doc
      template — these are portfolio-wide reviews, not the single-company
      event §7.2 describes).
  event                              -> §7.2's 4 headers verbatim.
Every type then gets the same 4-part interpretation block (already the
§4.2 tail for daily types, appended after the type's own headers for the
others, so §6.2/§7.2 header order is untouched) and the two calendar
sections. That uniformity IS the 2B contract: whatever 2B fills is
rendered by all 8 types in both formats.

The interpretation block is per-field, not gated by `is_empty()` as a
single switch: each of the 4 `Interpretation` fields independently prints
"AI 해석 미생성" when blank, or an "AI 자동판정 · {generated_by}" badge
followed by the text when 2B has filled it in (spec B5 contract 2).
"""
from __future__ import annotations

import re

from .model import DATA_STATUS_KO, FactRow, Report

# judge.md 「양쪽 다 틀린 것」 4 + [운영 이슈]: an empty report must say so on
# its own face, not only in `missing`.
EMPTY_REPORT_WARNING = (
    "주의: 차단선 이전에 알려진 사실이 0건이라 이 리포트는 비어 있습니다 "
    "(수집이 차단선보다 늦었을 가능성 — 결측 항목 참조)."
)

# spec B12: this markup goes to a public GitHub Pages site. `html.escape`
# neutralises tags, NOT url schemes — `href="javascript:alert(1)"` survives
# escaping untouched (judge.md P0, measured). Anything whose scheme is not
# http/https is therefore never turned into a link; it is printed as text so
# the information is still visible and auditable.
ALLOWED_URL_SCHEMES = ("http://", "https://")
# Browsers ignore control characters and spaces inside a URL, so
# "java\tscript:" and "  javascript:" are live schemes. Strip them before
# deciding, and link the stripped form (what a browser would resolve).
_URL_IGNORED_CHARS = re.compile(r"[\x00-\x20\x7f]")


def safe_href(url) -> str | None:
    """The URL to put in an href/markdown link, or None if it must not be a
    link at all."""
    if not url:
        return None
    cleaned = _URL_IGNORED_CHARS.sub("", str(url))
    return cleaned if cleaned.lower().startswith(ALLOWED_URL_SCHEMES) else None


def status_ko(status: str) -> str:
    """spec B7 fixed display mapping (public: render_html.py uses it too)."""
    return DATA_STATUS_KO.get(status, status or "")


# --- 등락 방향 (색·화살표) -------------------------------------------------
# 국내 증시 관례: 상승 = 빨강, 하락 = 파랑 (미국식 초록/빨강과 반대). 색은
# `site.py`의 스타일시트가 `.up`/`.down` 클래스에 입히고, 여기서는 **방향만**
# 정한다. 방향은 화살표로도 반드시 나가야 한다 — 색만으로 정보를 주면 흑백
# 출력과 색각이상에서 그 행은 아무 말도 하지 않고, 마크다운(Obsidian)에는
# 애초에 색이 없다.
UP_MARK, DOWN_MARK, FLAT_MARK = "▲", "▼", "―"

# 색은 방향이지 좋고 나쁨이 아니다: 환율 상승은 원화 약세고, VIX 하락은
# 시장 안정이다. 형식마다 문장이 다르므로(마크다운에는 색이 없다) 렌더러가
# 각자 쓴다.
LEGEND_HTML = ("빨강 ▲ 상승 · 파랑 ▼ 하락 (국내 증시 관례) — "
               "색은 방향일 뿐 좋고 나쁨이 아닙니다. 환율 상승은 원화 약세, VIX 하락은 시장 안정입니다.")
LEGEND_MD = ("▲ 상승 · ▼ 하락 — 방향 표시일 뿐 좋고 나쁨이 아닙니다. "
             "환율 상승은 원화 약세, VIX 하락은 시장 안정입니다.")

SECTOR_NOTE = ("업종 대표값은 평균이 아니라 중앙값입니다 — 한 종목이 크게 튀면 평균은 축 전체가 "
               "움직인 것처럼 보이기 때문입니다. 종목이 1~2개인 축(표본 적음)은 업종 신호가 아니라 "
               "그 개별 종목의 움직임으로 읽으십시오.")


def direction(delta_pct: float | None) -> str:
    """'up' | 'down' | 'flat' | '' — 마지막은 '등락을 모른다'(직전 값 없음).
    모르는 것에 방향을 지어내지 않는다."""
    if delta_pct is None:
        return ""
    if delta_pct > 0:
        return "up"
    if delta_pct < 0:
        return "down"
    return "flat"


def arrow(delta_pct: float | None) -> str:
    return {"up": UP_MARK, "down": DOWN_MARK, "flat": FLAT_MARK}.get(direction(delta_pct), "")


def fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


# --- layout (shared with render_html.py) ---------------------------------

def heading(report: Report) -> dict:
    """The document head both renderers emit: one title + meta lines."""
    if report.report_type == "event":
        head = {"title": f"{report.title} | {report.report_date}",
                "meta": [f"발표 시각: {report.cutoff_kst}"]}
    else:
        head = {"title": f"{report.report_date} [{report.title}]",
                "meta": [f"기준 시각: {report.cutoff_kst}",
                         f"데이터 상태: {status_ko(report.data_status)}"]}
    if report.meta.get("no_facts_before_cutoff"):
        head["meta"].append(EMPTY_REPORT_WARNING)
    return head


def _facts(rows: list[FactRow]) -> dict:
    return {"kind": "facts", "rows": rows}


def _text(text: str) -> dict:
    return {"kind": "text", "text": text}


def _missing(report: Report) -> dict:
    return {"kind": "missing", "items": report.missing}


# 히어로 카드에 올릴 4가지 — 국내 지수, 미국 지수, 반도체, 환율. "오늘 시장이
# 올랐나 내렸나"에 스크롤 없이 답하는 최소 조합이다(승인된 시안 기준).
HERO_SYMBOLS = ("^KS11", "^GSPC", "^SOX", "KRW=X")


def _hero(report: Report) -> dict:
    by_subject = {r.subject: r for r in report.market_reaction}
    rows = [by_subject[s] for s in HERO_SYMBOLS if s in by_subject]
    return {"kind": "hero", "rows": rows}


def _breadth(report: Report) -> dict:
    return {"kind": "breadth", "text": report.breadth}


def _sector(report: Report) -> dict:
    return {"kind": "sector", "rows": report.sector_summary}


def _market_blocks(report: Report) -> list[dict]:
    """"시장 반응" 계열 섹션의 본문: 개별 종목 표 + 시장 폭 한 줄 + 업종 요약.
    5개 리포트 타입이 같은 조합을 쓰므로 한 곳에서만 만든다 — 타입마다 손으로
    베낀 레이아웃이 예전에 두 타입의 해석 칸을 동시에 떨어뜨린 적이 있다."""
    return [_facts(report.market_reaction), _breadth(report), _sector(report)]


def _interp(report: Report, field_name: str) -> dict:
    """spec B5 contract 2, per field: blank -> the literal placeholder,
    filled -> the AI badge + the text."""
    text = getattr(report.interpretation, field_name)
    if not text:
        return {"kind": "interp", "badge": "", "text": "AI 해석 미생성"}
    return {"kind": "interp",
            "badge": f"AI 자동판정 · {report.interpretation.generated_by or 'ai:unknown'}",
            "text": text}


_CASHFLOW_METRICS = ("operating_cash_flow", "capex", "free_cash_flow")

INTERPRETATION_HEADERS = (
    ("당시 해석", "reading"), ("반대 해석", "counter_reading"),
    ("기존 가설 영향", "thesis_impact"), ("다음 검증", "next_check"),
)


def _lead_sections(report: Report) -> list[tuple[str, list[dict]]]:
    """The type-specific head of the layout, before the universal tail."""
    rt = report.report_type
    if rt == "weekly_review":  # §6.2, 5 headers in order
        return [
            ("이번 주 시장의 지배 변수", [_text(report.headline), _facts(report.facts), _missing(report)]),
            ("자산·섹터 성과", _market_blocks(report)),
            ("다음 주에 뒤집힐 수 있는 변수", [_interp(report, "counter_reading")]),
            ("내가 놓친 변수", [_missing(report)]),
            ("다음 주 검증할 가설", [_interp(report, "next_check")]),
        ]
    if rt == "event":  # §7.2, 4 headers in order
        cashflow_rows = [r for r in report.facts if r.metric in _CASHFLOW_METRICS]
        return [
            ("실제치·예상치·가이던스", [_facts(report.facts), _missing(report)]),
            ("현금흐름과 투자", [_facts(cashflow_rows)]),
            ("시장 반응과 반대 해석",
             _market_blocks(report) + [_interp(report, "counter_reading")]),
            ("다음 분기 검증 조건", [_interp(report, "next_check")]),
        ]
    if rt == "monthly":
        return [
            (f"월간 거시 체제: {report.meta.get('regime_label', '')}", [_text(report.headline)]),
            ("핵심 지표", [_facts(report.facts), _missing(report)]),
            ("자산 성과", _market_blocks(report)),
        ]
    if rt in ("quarterly", "annual"):
        return [
            ("핵심 사실", [_facts(report.facts), _missing(report)]),
            ("시장 반응", _market_blocks(report)),
        ]
    # morning / week_start / close_delta — §4.2's first 3 headers.
    return [
        ("시장 한 줄", [_text(report.headline)]),
        ("핵심 사실", [_facts(report.facts), _missing(report)]),
        ("시장 반응", _market_blocks(report)),
    ]


def sections(report: Report) -> list[tuple[str, list[dict]]]:
    """The single layout description shared by render_markdown/render_html."""
    out = _lead_sections(report)
    # 히어로 카드 + 색 범례는 **첫 섹션 안쪽에** 얹는다. 앞에 새 섹션을 만들면
    # §4.2/§6.2/§7.2가 못박은 머리말 순서가 밀린다(그 순서는 테스트가 고정).
    if out:
        header, blocks = out[0]
        out[0] = (header, [_hero(report), {"kind": "legend"}] + blocks)
    out += [(header, [_interp(report, field)]) for header, field in INTERPRETATION_HEADERS]
    out.append((
        "다가오는 일정",
        [{"kind": "calendar", "columns": ["일자", "중요도", "국가", "이름", "상태"],
          "rows": [[e.when, e.importance, e.country, e.name, e.data_status] for e in report.events]}],
    ))
    out.append((
        "최근 일정 변경",
        [{"kind": "calendar", "columns": ["일자", "구분", "이름"],
          "rows": [[c.when, c.change, c.name] for c in report.schedule_changes]}],
    ))
    return out


# --- markdown emission ----------------------------------------------------

def _facts_table_md(rows: list[FactRow]) -> str:
    if not rows:
        return "(해당 없음)"
    lines = ["| 항목 | 수치 | 비교 | 원자료 |", "|---|---:|---|---|"]
    for r in rows:
        badge = status_ko(r.data_status)
        value_cell = f"{r.value} · {badge}" if badge else r.value
        href = safe_href(r.source_url)
        src = f"[원자료]({href})" if href else (r.source_url or "-")
        # 마크다운에는 색이 없으므로 방향은 화살표가 진다.
        mark = arrow(r.delta_pct)
        comparison = f"{mark} {r.comparison}".strip() if mark else r.comparison
        lines.append(f"| {r.label} | {value_cell} | {comparison} | {src} |")
    return "\n".join(lines)


def _sector_table_md(rows) -> str:
    if not rows:
        return ""
    lines = ["| 업종 | 상승/하락 | 중앙값 | 종목 |", "|---|---|---:|---|"]
    for s in rows:
        if not s.total:
            lines.append(f"| {s.sector} | - | - | 관측 없음 |")
            continue
        count = f"{s.total}종목" + (" (표본 적음)" if s.small_sample else "")
        mark = arrow(s.median_pct)
        median = f"{mark} {fmt_pct(s.median_pct)}".strip() if mark else fmt_pct(s.median_pct)
        lines.append(f"| {s.sector} | {s.up}↑ / {s.down}↓ | {median} | {count} |")
    return "\n".join(lines) + f"\n\n{SECTOR_NOTE}"


def _calendar_table_md(columns: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(해당 없음)"
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _block_md(block: dict) -> str:
    kind = block["kind"]
    if kind == "facts":
        return _facts_table_md(block["rows"])
    if kind == "hero":
        # 카드 배치는 화면(HTML)만의 것이다. 마크다운에는 바로 아래에
        # `headline`이 같은 4개 숫자를 이미 한 줄로 싣고 있으므로, 여기서
        # 또 찍으면 Obsidian 노트에는 같은 값이 두 번 나온다.
        return ""
    if kind == "legend":
        return LEGEND_MD
    if kind == "breadth":
        return block["text"]
    if kind == "sector":
        return _sector_table_md(block["rows"])
    if kind == "calendar":
        return _calendar_table_md(block["columns"], block["rows"])
    if kind == "missing":
        if not block["items"]:
            return ""
        return "\n".join(f"- 결측: {m.area} — {m.reason}" for m in block["items"])
    if kind == "interp":
        return f"{block['badge']}\n\n{block['text']}" if block["badge"] else block["text"]
    return block["text"]


def _frontmatter(report: Report) -> str:
    """A minimal, spec-B9-compatible Obsidian frontmatter block (date /
    cutoff / status / ai_interpretation / base tags). Subject wikilinks and
    market/country tags are content-shaped decisions the sync step (ST3
    `obsidian.py`) makes when it has the whole vault's conventions in view;
    this only emits the fields `Report` itself already knows."""
    status_tag = status_ko(report.data_status).replace(" ", "")
    tags = ["market-intel", f"report/{report.report_type}", f"status/{status_tag}"]
    lines = [
        "---",
        "project: market-intel",
        f"type: {report.report_type}",
        f"date: {report.report_date}",
        f'cutoff_kst: "{report.cutoff_kst}"',
        f"data_status: {status_ko(report.data_status)}",
        f"ai_interpretation: {'true' if not report.interpretation.is_empty() else 'false'}",
        f"tags: [{', '.join(tags)}]",
        "---",
    ]
    return "\n".join(lines) + "\n"


def render_markdown(report: Report, obsidian_frontmatter: bool = False) -> str:
    head = heading(report)
    parts = [f"# {head['title']}"] + head["meta"] + [""]
    for header, blocks in sections(report):
        parts += [f"## {header}", ""]
        for block in blocks:
            body = _block_md(block)
            if body:
                parts += [body, ""]
    body = "\n".join(parts) + "\n"
    return (_frontmatter(report) + "\n" + body) if obsidian_frontmatter else body
