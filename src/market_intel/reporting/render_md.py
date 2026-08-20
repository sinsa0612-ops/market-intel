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
  morning / close_delta -> §4.2 (일간 공통 포맷), the 7 headers in the
      order the ST2 success criteria pin down word for word. `week_start`
      is retired (merged into `weekly_review`, 2026-08-20) but still
      renders here: its 4 published JSONs are re-rendered on every site
      build and must keep looking exactly as they did when published.
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

from ..universe import CORE16_SYMBOLS
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

# 업종 표가 두 개인 이유를 CEO가 한 줄로 알 수 있어야 한다. 위 표는 시장을
# 재는 자(업종 ETF), 아래 표는 내가 들여다보는 기업들이다.
SECTOR_INDEX_TITLE = "업종 지수 — 시장 전체가 업종별로 어떻게 움직였나"
SECTOR_TITLE = "업종 묶음 — 내가 관측하는 기업들은 어땠나"
# 따옴표는 쓰지 않는다: HTML 렌더러가 `_esc(quote=True)`로 &#x27;로 바꿔버려
# 같은 문장이 두 형식에서 다른 글자가 된다(테스트가 잡았다).
# 기업 수는 세어서 쓴다 — "16곳"이라고 박아 두면 관측군이 늘 때마다(2026-08-03에
# 18곳이 됐다) 화면이 조용히 거짓말을 한다.
SECTOR_INDEX_NOTE = ("이 표는 업종별 대표 ETF 한 종목으로 시장 전체가 업종별로 어떻게 움직였는지 봅니다. "
                     f"아래 업종 묶음 표는 그와 별개로, 제가 매일 들여다보는 기업 {len(CORE16_SYMBOLS)}곳만 "
                     "묶은 것입니다.")
# 업종 지수 표의 시장 구분 표기. 값은 `SectorIndexRow.market`.
MARKET_LABELS = {"US": "미국", "KR": "한국"}


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


def fmt_pct(value: float | None, unit: str = "%") -> str:
    """`unit`은 값이 이미 퍼센트인 지표에서 `%p`가 된다(`FactRow.delta_unit`).
    기본값을 `%`로 둬서 기존 호출부(업종 중앙값 등)는 그대로 동작한다."""
    return "-" if value is None else f"{value:+.2f}{unit}"


def fmt_money(value: float | None, unit: str) -> str:
    """자릿수가 12~15개인 금액은 원본 그대로면 읽히지 않는다 — 삼성전자 매출
    333,605,938,000,000, 수급 2,174,506,000,000 KRW. 통화에 맞춰 조/억으로
    줄인다. (상세 페이지도 같은 함수를 쓴다 — 두 화면이 같은 금액을 다른
    자릿수로 쓰면 대조가 안 된다.)"""
    if value is None:
        return "미확인"
    sign = "-" if value < 0 else ""
    v = abs(value)
    if unit == "KRW":
        if v >= 1e12:
            return f"{sign}{v / 1e12:,.1f}조 원"
        if v >= 1e8:
            return f"{sign}{v / 1e8:,.0f}억 원"
        return f"{sign}{v:,.0f}원"
    if unit == "USD":
        if v >= 1e8:
            return f"{sign}{v / 1e8:,.1f}억 달러"
        if v >= 1e6:
            return f"{sign}{v / 1e6:,.1f}백만 달러"
        return f"{sign}{v:,.0f} USD"
    return f"{sign}{v:,.0f} {unit}".rstrip()


# --- 수급 묶기 (두 렌더러 공용) --------------------------------------------
# 순매수는 개인+기관+외국인의 합이 0이다. 그래서 값 하나하나가 아니라
# **어느 쪽이 사고 어느 쪽이 파는가**가 정보다. 종목당 세 행을 따로 싣던
# 표는 그 구조를 보여줄 수 없었다.
FLOW_ACTORS: tuple[tuple[str, str], ...] = (
    ("individual", "개인"), ("institution", "기관"), ("foreign", "외국인"),
)
# 이 금액 아래는 "움직임 작음"으로 접는다. 막대를 그려도 픽셀이 안 나오고,
# 매일 5종목 중 3종목이 이 구간이라 그리면 오히려 큰 것이 안 보인다.
FLOW_QUIET_KRW = 1e11  # 1,000억


# 색 진하기의 아래·위 끝. 0까지 내려가면 옅은 칸이 배경과 구별되지 않아 "그
# 주체는 아무것도 안 했다"로 읽히므로 바닥을 둔다. `PALE_BELOW` 아래로는 글자를
# 흰색이 아니라 본문색으로 쓴다 — 옅은 분홍 위의 흰 글씨는 읽히지 않는다.
INTENSITY_FLOOR = 0.42
PALE_BELOW = 0.62


def _intensity(value: float, peak: float) -> float:
    """그 화면에서 가장 큰 금액 대비 이 금액의 진하기(0.42~1.0)."""
    if not peak:
        return 1.0
    share = abs(value) / peak
    return round(INTENSITY_FLOOR + (1.0 - INTENSITY_FLOOR) * share, 3)


def flow_groups(rows: list[FactRow]) -> list[dict]:
    """수급 FactRow들을 **종목 하나에 한 줄**로 묶는다. 큰 종목이 위로.

    반환: `{name, actors: [{label, value, text, buy}], total, quiet, story}`.
    `story`는 그 줄을 한 문장으로 읽은 것이다 — 막대만 있으면 흑백 출력과
    화면 낭독기에서 아무 말도 하지 않는다.
    """
    by_subject: dict[str, dict] = {}
    for row in rows:
        actor = next((key for key, _ in FLOW_ACTORS if f"net_buy_{key}" in row.metric), None)
        if actor is None or not isinstance(row.raw_value, (int, float)):
            continue
        entry = by_subject.setdefault(row.subject, {"name": "", "values": {}})
        entry["values"][actor] = float(row.raw_value)
        # 라벨은 "삼성전자(005930.KS) 개인 순매수(금액)" — 주체 부분을 떼면
        # 종목 이름이 남는다. build.py가 만든 그 라벨을 다시 만들지 않는다.
        entry["name"] = re.sub(r"\s*(개인|기관|외국인)\s*순매수.*$", "", row.label).strip()

    # 색의 진하기는 **금액의 절대 크기**다(CEO 요청 2026-08-04): 많이 샀으면
    # 진한 빨강, 조금 샀으면 옅은 빨강, 많이 팔았으면 진한 파랑, 조금 팔았으면
    # 옅은 파랑. 기준은 그 화면에서 가장 큰 한 주체의 금액이다 — 막대 **폭**은
    # 종목 안에서의 비중이라 종목끼리 비교가 안 되는데, 진하기가 그 자리를
    # 메운다("삼성전자 개인 2.1조"와 "기관 4,560억"이 폭은 비슷해도 진하기가
    # 다르다). 색만으로 정보를 주지는 않는다 — 금액은 막대 안에 글자로도,
    # 옆 문장에도 그대로 있다.
    peak = max((abs(v) for e in by_subject.values() for v in e["values"].values()), default=0.0)

    out = []
    for subject, entry in by_subject.items():
        values = entry["values"]
        total = sum(abs(v) for v in values.values())
        # 표시 금액은 **절댓값**이다. 방향은 색과 문장이 이미 말하므로 막대
        # 안의 빼기 기호는 "파랑 막대인데 마이너스"라고 두 번 말하면서 좁은
        # 칸의 글자만 밀어낸다. 부호가 필요한 곳(정렬·문장)은 `value`를 쓴다.
        actors = [
            {"label": label, "value": values.get(key, 0.0),
             "text": fmt_money(abs(values.get(key, 0.0)), "KRW"), "buy": values.get(key, 0.0) > 0,
             "intensity": _intensity(values.get(key, 0.0), peak)}
            for key, label in FLOW_ACTORS if key in values
        ]
        out.append({
            "subject": subject, "name": entry["name"] or subject, "actors": actors,
            "total": total, "quiet": total < FLOW_QUIET_KRW,
            "story": _flow_story(actors, total),
        })
    out.sort(key=lambda g: g["total"], reverse=True)
    return out


def _flow_story(actors: list[dict], total: float) -> str:
    """"개인이 2.2조 담고, 외국인이 1.8조 던졌다" — 막대가 말하는 것을 문장으로."""
    if total < FLOW_QUIET_KRW:
        return f"특이사항 없음 ({fmt_money(total, 'KRW')} 규모)"
    buyers = sorted([a for a in actors if a["buy"]], key=lambda a: -a["value"])
    sellers = sorted([a for a in actors if not a["buy"]], key=lambda a: a["value"])
    # 양쪽 다 **이름을 다 부르고 합계를 쓴다**. 이 한 줄이 막대를 대신해야
    # 하므로(흑백 출력·화면 낭독기) 규모가 빠지면 안 되고, "외 1" 같은 축약은
    # 한국어로 읽히지 않는다. 큰 쪽이 앞에 온다.
    # 세 주체 이름이 모두 받침으로 끝나므로(개인·기관·외국인) 조사는 늘 '이'다.
    def side(group: list[dict], verb: str) -> str:
        who = "·".join(a["label"] for a in group)
        return f"{who}이 {fmt_money(sum(abs(a['value']) for a in group), 'KRW')} {verb}"

    parts = []
    if buyers:
        parts.append(side(buyers, "담고"))
    if sellers:
        parts.append(side(sellers, "던졌다"))
    return ", ".join(parts) if parts else "-"


# spec 20260810-period-report §1③-2 "부록을 맨 뒤로". 두 절 모두 이미
# 레이아웃(`sections`) 맨 뒤에 있다 — "최근 일정 변경"은 8종 공통 꼬리
# 섹션의 마지막이고, "최근 보고서"는 `site.py`가 리포트 본문 다음에 붙인다.
# CEO 지적은 순서가 아니라 **크기**다("링크 목록인데 본문보다 크다") —
# 그래서 여기서는 옮기지 않고 접는다. "다가오는 일정"은 뺀다: 그건 앞으로
# 볼 것이라 펼쳐 둘 값어치가 있다.
APPENDIX_SECTIONS = {"최근 일정 변경"}


def appendix_count(blocks: list[dict]) -> int:
    """접은 부록 안에 몇 건이 들어 있는지. **개수를 밝히지 않으면 접은 것이
    아니라 사라진 것으로 읽힌다**(spec §2 규칙4 — 다른 접기들은 이미 개수를
    보이는데 부록만 빠져 있었다, 심사 2026-08-10 P2)."""
    return sum(len(b.get("rows") or ()) for b in blocks)
NO_DATA_MD = "(해당 없음)"

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


# 한 화면에 몇 줄까지 펼쳐 둘 것인가. 거시지표는 금리·환율이 앞에 오도록
# 정렬한 뒤 이 개수까지만 카드로 내고 나머지는 접는다.
MACRO_CARDS = 8
# 이 순서대로 앞에 세운다. CEO가 매일 먼저 보는 것은 금리와 환율이고, 물가·
# 고용은 월 1회 갱신이라 매일 화면 앞자리를 차지할 이유가 없다.
MACRO_PRIORITY = (
    "DGS10", "DGS2", "T10Y2Y", "usd_krw_fx", "731Y001.0000001",
    "base_rate", "722Y001.0101000", "DFEDTARU", "FEDFUNDS",
    "UNRATE", "CPIAUCSL", "PCEPI", "PAYEMS", "RSAFS", "INDPRO",
)


EARNINGS_METRIC = "earnings_release_8k"


def filing_summary(rows: list[FactRow]) -> dict:
    """공시 34건을 매일 다 읽지는 않는다. 한 줄로 답하고 전체는 접는다.

    한 줄이 답하는 것은 **실적을 발표한 곳이 어디인가**다 — 정기공시 제출은
    일정대로 일어나는 일이라 셀 값이지 읽을 값이 아니다."""
    earnings = [r for r in rows if r.metric == EARNINGS_METRIC]
    others = [r for r in rows if r.metric != EARNINGS_METRIC]
    return {
        "total": len(rows),
        "earnings_count": len(earnings),
        "other_count": len(others),
        # 종목 코드로 적는다 — 라벨에서 회사명을 떼어내려면 공시 종류 문자열을
        # 되파싱해야 하고, 그 규칙이 바뀌면 요약이 조용히 깨진다. 전체 이름은
        # 바로 아래 펼침 표에 그대로 있다.
        "earnings_subjects": [r.subject for r in earnings],
    }


def _macro_sort_key(row: FactRow) -> tuple:
    try:
        return (MACRO_PRIORITY.index(row.subject), "")
    except ValueError:
        return (len(MACRO_PRIORITY), row.label)


def fact_blocks(rows: list[FactRow]) -> list[dict]:
    """`핵심 사실`의 본문 — 갈래마다 **다른 모양**으로 낸다.

    한 표에 다 넣으면 실측 77행이 되고(수급 30 · 공시 34 · 거시 13) 아무것도
    한눈에 안 들어온다는 CEO 지적(2026-08-03). 세 갈래는 묻는 질문이 다르므로
    답하는 모양도 다르다:

    - **수급**은 "누가 사고 누가 팔았나" — 순매수 합은 0이라 방향이 전부다.
      종목 하나에 한 줄, 주체 셋을 한 막대에 나란히 놓으면 읽을 것이 없다.
    - **거시**는 "지금 몇인가" — 값 하나짜리 관측이라 표의 5개 칸 중 4개가
      낭비다. 카드가 맞다.
    - **공시**는 "무슨 일이 있었나" — 34건을 매일 다 읽지 않는다. 요약 한 줄에
      답하고 전체는 접는다.

    갈래가 없는 사실(옛 리포트 JSON, 재무·컨센서스)은 지금까지 쓰던 표로
    떨어진다 — 새 모양이 없다고 사실이 사라지면 안 된다.
    """
    by_group: dict[str, list[FactRow]] = {}
    for row in rows:
        by_group.setdefault(row.group or "", []).append(row)

    out: list[dict] = []
    if by_group.get("flow"):
        out.append({"kind": "flow", "rows": by_group.pop("flow")})
    if by_group.get("macro"):
        macro = sorted(by_group.pop("macro"), key=_macro_sort_key)
        out.append({"kind": "macro_cards", "rows": macro[:MACRO_CARDS],
                    "rest": macro[MACRO_CARDS:]})
    if by_group.get("filing"):
        out.append({"kind": "filing_summary", "rows": by_group.pop("filing")})
    rest = [r for group in by_group.values() for r in group]
    if rest:
        out.append(_facts(rest))
    return out or [_facts([])]


def _text(text: str) -> dict:
    return {"kind": "text", "text": text}


def _missing(report: Report) -> dict:
    return {"kind": "missing", "items": report.missing}


# 히어로 카드 — "오늘 시장이 올랐나 내렸나"에 스크롤 없이 답하는 조합.
#
# 처음엔 4개(KOSPI·S&P 500·SOX·달러/원)였는데, CEO가 "지수를 좀더 다양하게
# 보면 안 되나"라고 했다. 코스닥·나스닥은 이미 수집·표시되고 있었지만 카드에
# 없어서 안 보였던 것이다(2026-08-02). 국내 2 + 미국 4 + 반도체 + 환율로 넓힌다.
# 러셀2000은 중소형주라, 대형주가 끌어올린 장인지 시장 전체가 오른 장인지를
# S&P·나스닥만으로는 알 수 없는 자리를 메운다.
HERO_SYMBOLS = (
    "^KS11", "^KQ11",            # 국내
    "^GSPC", "^IXIC", "^DJI", "^RUT",  # 미국 (대형·기술·전통·중소형)
    "^SOX", "KRW=X",             # 반도체 · 환율
)


def _hero(report: Report) -> dict:
    by_subject = {r.subject: r for r in report.market_reaction}
    rows = [by_subject[s] for s in HERO_SYMBOLS if s in by_subject]
    return {"kind": "hero", "rows": rows}


def _breadth(report: Report) -> dict:
    return {"kind": "breadth", "text": report.breadth}


def _sector(report: Report) -> dict:
    return {"kind": "sector", "rows": report.sector_summary}


def _sector_index(report: Report) -> dict:
    """업종 지수 표를 시장(미국/한국)별로 묶는다. 순서는 행이 들고 온 순서 —
    `build.py`가 이미 시장별·등락률 내림차순으로 정렬해서 넘긴다."""
    groups: list[tuple[str, list]] = []
    for row in report.sector_index:
        label = MARKET_LABELS.get(row.market, row.market)
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(row)
    return {"kind": "sector_index", "groups": groups}


def _subheading(text: str) -> dict:
    """표 제목(h3/###). 표가 둘이 되면서 어느 표가 무엇인지 이름이 필요해졌다.
    제목을 표 렌더러 안이 아니라 **레이아웃에** 두면 제목과 본문의 순서를 여기
    한 곳에서 정할 수 있다(시장 폭이 제목 아래에서 머릿줄로 올라간 것도 이 덕분이다
    — `_market_blocks` 참조).
    빈 문자열이면 아무것도 내지 않는다(딸린 내용이 없는 제목은 안 낸다)."""
    return {"kind": "subheading", "text": text}


def _market_blocks(report: Report) -> list[dict]:
    """"시장 반응" 계열 섹션의 본문: 개별 종목 표 + 업종 지수 표 + 업종 묶음 표
    (시장 폭 한 줄 포함). 5개 리포트 타입이 같은 조합을 쓰므로 한 곳에서만
    만든다 — 타입마다 손으로 베낀 레이아웃이 예전에 두 타입의 해석 칸을 동시에
    떨어뜨린 적이 있다.

    순서는 넓은 것 → 좁은 것이다: 시장 폭 요약(코스피·코스닥 전종목 + 관측기업)
    다음에 개별 종목, 업종 지수, 업종 묶음. 두 업종 표는 붙어 있어야 비교가 된다.

    **시장 폭은 섹션 머릿줄이다 — 어느 표에도 딸리지 않는다.** 예전에는 관측기업
    한 줄뿐이라 `업종 묶음` 제목 아래에 뒀는데, 코스피·코스닥 전종목 2,763개 줄이
    붙으면서 그 자리가 거짓 이름표가 됐다(전종목은 "내가 관측하는 기업"이 아니다).
    세 줄 모두 자기가 무엇인지 밝히므로(`관측기업`/`코스피`/`코스닥`) 표 사이에
    떠서 소속을 알 수 없던 옛 문제도 없다. CEO 결정 2026-08-04."""
    return [
        _breadth(report),
        # 그림은 표보다 **앞에** 온다. 표는 "오늘 얼마"를 답하고 그림은 "며칠째
        # 어떤 모양인가"를 답하는데, 뒤에 두면 표 77행을 지나야 도달한다 —
        # CEO 지적(2026-08-03 "표로 보니 한눈에 안 들어온다")이 가리킨 자리다.
        *(_chart(c) for c in report.charts),
        _facts(report.market_reaction),
        _flow_split(report),
        _subheading(SECTOR_INDEX_TITLE), _sector_index(report), _blindspot(report),
        # 옛 리포트 JSON에는 sector_summary가 없다 — 딸린 표가 없으면 제목도 안 낸다.
        _subheading(SECTOR_TITLE if report.sector_summary else ""),
        _sector(report),
    ]


def _flow_split(report: Report) -> dict:
    """자금 갈래 블록(CEO 지시 2026-08-20). 업종 지수 표 **바로 앞**에 온다 —
    "오늘 돈이 갈렸다"가 요약이고 그 아래 표가 증거다. 유별나지 않은 날은
    `is_notable=False`라 아무것도 안 낸다(빈 제목을 만들지 않는 §2-1의 태도)."""
    return {"kind": "flow_split", "block": report.flow_split}


def _blindspot(report: Report) -> dict:
    """사각지대 신고 블록(CEO 지시 2026-08-20). 업종 지수 표 **바로 아래**에
    붙는다 — 그 표가 "어느 업종이 움직였나"를 보여준 직후가 "그런데 그 움직임을
    우리가 설명할 수 있나"가 가장 궁금한 자리다.

    판단은 `blindspot.detect`가 끝냈고 여기서는 옮기기만 한다(`_unusual_day`·
    `_chart`와 같은 원칙)."""
    return {"kind": "blindspot", "rows": report.blind_spots,
            "unwatched": report.unwatched_sectors}


def _prior(report: Report) -> dict:
    """지난 해석 대조 블록. 판단은 `build.py`가 끝냈으므로 옮기기만 한다."""
    return {"kind": "prior", "prior": report.prior}


def _chart(block) -> dict:
    """`ChartBlock` -> 렌더러 공용 블록. `_unusual_day`와 같은 원칙 — 판단은
    `build.py`가 끝냈고 여기서는 옮기기만 한다."""
    return {"kind": "chart", "block": block}


def _unusual_day(report: Report) -> dict:
    """spec 20260806-report-visual §1① — "오늘 유별난 것" 블록. 판단은 전부
    `build.py`가 끝냈으므로(`Report.unusual_day`) 여기서는 그 필드를 블록
    딕셔너리로 옮기기만 한다."""
    u = report.unusual_day
    return {
        "kind": "unusual_day", "headline": u.headline,
        "trend_label": u.trend_label, "trend_series": u.trend_series,
        "top_movers": u.top_movers,
    }


NO_INTERP = "AI 해석 미생성"
# 반대 해석만 비어 있는 것과, 반박할 대상 자체가 없는 것은 다른 사정이다.
# 앞의 것은 그 문단이 검증에 걸렸다는 뜻이고, 뒤의 것은 애초에 실을 수 없다는
# 뜻이다 — 같은 문구로 쓰면 독자가 구별할 수 없다(CEO 지적 2026-08-04).
NO_COUNTER_WITHOUT_READING = "당시 해석이 실리지 않아 반대 해석도 싣지 않습니다 (반박할 대상이 없습니다)."


# --- F-번호 화면 제거 (CEO 지적 2026-08-12: "F67 이런 표기가 알아보기 힘들다") ---
#
# ⚠️ **지우는 자리는 화면뿐이다.** F-번호는 표시용 장식이 아니라 환각 검증기의
# 접지 장치다 — 규칙 8(`attribution`)이 "F45 뒤의 주체가 정말 F45의 주체인가"를,
# 규칙 9(`citation_num`)가 "인용에 붙은 숫자가 그 인용의 숫자인가"를 F-번호로
# 대조한다. 이 둘이 잡아낸 실제 발행 사고가 있다(F45(KOSPI)에 삼성전자 등락률
# 26.81%를 붙여 내보낸 2026-08-03 주간 브리핑). 그래서 **생성·검증·저장은 전부
# F-번호가 붙은 원문 그대로** 두고, 렌더러가 화면에 낼 때만 걷어낸다:
#
#   생성 -> 검증기(F-번호 봄) -> 원장·리포트 JSON(F-번호 보존) -> 화면(제거)
#
# 원장이 원문을 그대로 갖고 있으므로 "이 문장이 무슨 사실을 인용했나"는 언제든
# 되짚을 수 있다. 여기서 원문을 지우면 그 감사 추적이 끊긴다 — 하지 말 것.
#
# 인용 묶음의 실제 모양(발행본 실측): `F73의` · `F60과 F69에서` · `(F1, F2, F3, F72)` ·
# `F20-F21의` · `F12에 따르면` · **`F56부터 F58까지는`**.
#
# 두 가지가 실제로 물렸다:
#  - 범위 표기(`-`)를 빼먹으면 `F20-F21의 금리` 가 `-금리`로 남는다(시제품에서 발견).
#  - `A부터 B까지` 는 **조사가 번호마다 붙어** 묶음 정규식만으로는 안 잡힌다. 빠뜨리면
#    `F56부터 F58까지는 …` 이 `부터 까지는 …` 이 된다(2026-08-12 발행문에서 실제로 났다).
#    그래서 이 형태를 **먼저** 통째로 지운다.
_FNUM_RANGE_RE = re.compile(r"F\d+\s*부터\s*F\d+\s*까지(?:는|도|의|를|을|와|과)?\s*")
_FNUM_RUN = r"F\d+(?:\s*(?:,|·|/|-|~|–|—|와|과|및|그리고)\s*F\d+)*"
# 인용에 바로 붙는 조사·연결어. **긴 것부터** — `에`가 먼저 먹으면 `따르면`이 남는다.
_FNUM_TAIL = (r"(?:에\s*따르면|에\s*기록된|에\s*기재된|에\s*나타난|에\s*따라|"
              r"에서\s*보이는|에서는|에서도|에서|에는|에도|에|의|은|는|이|가|을|를|도|"
              r"부터|까지|와|과|처럼|같이|기준)?")
_FNUM_PAREN_RE = re.compile(r"\s*[(（]\s*" + _FNUM_RUN + r"\s*[)）]")
_FNUM_INLINE_RE = re.compile(_FNUM_RUN + r"\s*" + _FNUM_TAIL + r"\s*")


def strip_fact_numbers(text: str) -> str:
    """화면에 낼 때만 F-번호를 걷어낸다. 저장된 원문에는 절대 쓰지 않는다(위 주석).

    괄호 묶음을 먼저 지운다 — 인라인 규칙이 먼저 돌면 괄호 안이 비어 `()`만 남는다.
    """
    if not text:
        return text
    out = _FNUM_PAREN_RE.sub("", text)
    out = _FNUM_RANGE_RE.sub("", out)
    out = _FNUM_INLINE_RE.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.·])", r"\1", out)
    return out.strip()


def _interp(report: Report, field_name: str) -> dict:
    """spec B5 contract 2, per field: blank -> the literal placeholder,
    filled -> the AI badge + the text.

    반대 해석은 예외다: 당시 해석이 비어 있으면 **왜** 비었는지를 말한다.
    이 사정은 리포트 자체에서 읽히므로(두 칸이 다 비었다) 새 필드가 필요 없다."""
    text = getattr(report.interpretation, field_name)
    if not text:
        if field_name == "counter_reading" and not report.interpretation.reading:
            return {"kind": "interp", "badge": "", "text": NO_COUNTER_WITHOUT_READING}
        return {"kind": "interp", "badge": "", "text": NO_INTERP}
    return {"kind": "interp",
            "badge": f"AI 자동판정 · {report.interpretation.generated_by or 'ai:unknown'}",
            "text": strip_fact_numbers(text)}


_CASHFLOW_METRICS = ("operating_cash_flow", "capex", "free_cash_flow")

INTERPRETATION_HEADERS = (
    ("당시 해석", "reading"), ("반대 해석", "counter_reading"),
    ("기존 가설 영향", "thesis_impact"), ("다음 검증", "next_check"),
)


def _lead_sections(report: Report) -> list[tuple[str, list[dict]]]:
    """The type-specific head of the layout, before the universal tail."""
    rt = report.report_type
    if rt == "weekly_review":  # §6.2's 5 headers, re-anchored 2026-08-20
        # §6.2 wrote these headers for a **Saturday** report, where "이번 주" is
        # the week that just ended and "다음 주" is the one starting Monday. The
        # merge moved publication to Monday 07:40, which shifts both by a week:
        # the retrospective content would have sat under "이번 주", and the
        # forward-looking content under "다음 주" — pointing past the week whose
        # calendar the report itself prints. Verified on a real 2026-08-17 build:
        # every fact read "1주 전 관측 대비" (last week) while 다가오는 일정
        # listed 08-18 onward (this week). The headers are re-anchored to the
        # report's own date, which is printed at the top of every report:
        #     지난주 = the week the facts cover · 이번 주 = the week ahead.
        # Cost, stated plainly: the 3 archived Saturday reports (08-01/08/15) are
        # re-rendered with these headers and read one week off. 3 archived pages
        # versus every Monday from here on.
        return [
            ("지난주 시장의 지배 변수", [_text(report.headline), *fact_blocks(report.facts), _missing(report)]),
            ("자산·섹터 성과", _market_blocks(report)),
            ("이번 주에 뒤집힐 수 있는 변수", [_interp(report, "counter_reading")]),
            ("내가 놓친 변수", [_missing(report)]),
            ("이번 주 검증할 가설", [_interp(report, "next_check")]),
        ]
    if rt == "event":  # §7.2, 4 headers in order
        cashflow_rows = [r for r in report.facts if r.metric in _CASHFLOW_METRICS]
        return [
            ("실제치·예상치·가이던스", [*fact_blocks(report.facts), _missing(report)]),
            ("현금흐름과 투자", [_facts(cashflow_rows)]),
            ("시장 반응과 반대 해석",
             _market_blocks(report) + [_interp(report, "counter_reading")]),
            ("다음 분기 검증 조건", [_interp(report, "next_check")]),
        ]
    if rt == "monthly":
        return [
            (f"월간 거시 체제: {report.meta.get('regime_label', '')}", [_text(report.headline)]),
            ("핵심 지표", [*fact_blocks(report.facts), _missing(report)]),
            ("자산 성과", _market_blocks(report)),
        ]
    if rt in ("quarterly", "annual"):
        # spec 20260810-period-report §1③-3 "양식 통일": 다른 7종은 모두 맨
        # 앞에 헤드라인 요약 한 줄을 보이는데(morning류의 "시장 한 줄",
        # weekly_review/monthly는 첫 섹션에 접혀 있다) quarterly/annual만
        # 없었다 — 그 기간 KOSPI 등락(§1①)과 흔들림(§1②)이 요약되는 자리라
        # 특히 이 두 타입에서 빠져 있던 것이 아쉽다.
        return [
            ("시장 한 줄", [_text(report.headline)]),
            ("핵심 사실", [*fact_blocks(report.facts), _missing(report)]),
            ("시장 반응", _market_blocks(report)),
        ]
    # morning / close_delta (+ retired week_start) — §4.2's first 3 headers.
    return [
        ("시장 한 줄", [_text(report.headline)]),
        ("핵심 사실", [*fact_blocks(report.facts), _missing(report)]),
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
    # spec 20260806-report-visual §1① — "오늘 유별난 것"은 `## 시장 한 줄`
    # **위**에 얹는다(그 헤딩이 있는 타입 = 일간 3종뿐). 위 hero/legend
    # 부착 다음에 끼워 넣는 이유: 그 로직이 "첫 섹션"을 그 타입의 머리
    # 요약으로 보고 있어서, 여기서 앞에 꽂으면 하이라이트 카드가 이 새
    # 섹션으로 끌려간다. 보여줄 것이 없으면(옛 JSON, 한국 시장 폭 자체가
    # 없는 날) 빈 헤딩을 내지 않는다 — §2-1과 같은 태도.
    if report.report_type in ("morning", "week_start", "close_delta") and (
            report.unusual_day.headline or report.unusual_day.top_movers):
        out.insert(0, ("오늘 유별난 것", [_unusual_day(report)]))
    # spec 20260806-report-visual §1② — 당시 해석·반대 해석·기존 가설 영향·
    # 다음 검증, 4개의 `## ` 섹션이 각각 제목+문단으로 세로 공간을 썼다.
    # 내용은 한 글자도 지우지 않는다 — 한 섹션 안에 `### ` 소제목 4개로
    # 묶어 배치만 좁힌다(h2 4번 -> h2 1번 + h3 4번).
    interp_parts: list[dict] = []
    for label, field_name in INTERPRETATION_HEADERS:
        interp_parts += [_subheading(label), _interp(report, field_name)]
    # 지난 해석 대조는 **해석 섹션 안에, 새 해석 뒤에** 붙는다(CEO 지시 2026-08-13).
    # 앞에 두면 오늘 읽을 것보다 어제 것이 먼저 오고, 별도 섹션으로 빼면 아무도
    # 안 본다 — 새 해석을 읽은 직후가 "지난번엔 뭐라 했더라"가 가장 궁금한 자리다.
    interp_parts += [_subheading("지난 해석 대조"), _prior(report)]
    out.append(("해석", interp_parts))
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


# --- 안 움직인 행 접기 (spec 20260810-period-report §1③-1, 두 렌더러 공용) ---
#
# CEO: "8화면 -> 3~4화면." 실측(8/6 기준) 32행 중 6행이 등락 사실상 0이었다.
# 그 행들을 지우지 않고(§2 규칙4 "정보를 지우지는 마라") "변화 없음 N건"으로
# 접어 펼쳐 볼 수 있게 한다.
#
# [ASSUMPTION] CEO가 정확한 문턱을 정하지 않았다. 화면에 소수 둘째 자리까지
# 찍히는 값이 반올림해도 "0.00%"로 보이는 선(0.005% 미만)은 너무 좁아 거의
# 아무것도 접지 못한다 — 반대로 너무 넓으면 진짜 움직인 행이 접힌다. 0.05%는
# "화면에서 0.0X% 대로 보이되 정말 작다"고 부를 수 있는 보수적인 선이다.
NO_CHANGE_THRESHOLD = 0.05  # % 또는 %p 단위(FactRow.delta_unit과 무관하게 같은 문턱)


def _is_unchanged(row: FactRow) -> bool:
    return row.delta_pct is not None and abs(row.delta_pct) < NO_CHANGE_THRESHOLD


def split_unchanged(rows: list[FactRow]) -> tuple[list[FactRow], list[FactRow]]:
    """(움직인 행, 안 움직인 행). 후자는 호출부가 접어서 낸다. 등락을 아예
    모르는 행(delta_pct=None)은 "안 움직였다"가 아니라 "모른다"이므로 접지
    않는다 — 다른 사정이다(§2 규칙4는 접은 사실을 밝히라는 것이지, 모르는
    것을 접으라는 게 아니다)."""
    changed = [r for r in rows if not _is_unchanged(r)]
    unchanged = [r for r in rows if _is_unchanged(r)]
    return changed, unchanged


# --- markdown emission ----------------------------------------------------

def _facts_table_md_raw(rows: list[FactRow]) -> str:
    if not rows:
        return "(해당 없음)"
    lines = ["| 항목 | 수치 | 비교 | 원자료 |", "|---|---:|---|---|"]
    for r in rows:
        badge = status_ko(r.data_status)
        value_cell = f"{r.value} · {badge}" if badge else r.value
        # 문서가 있으면 문서를 건다(render_html과 같은 규약).
        doc = safe_href(r.doc_url)
        href = doc or safe_href(r.source_url)
        src = f"[{'공시원문' if doc else '원자료'}]({href})" if href else (r.source_url or "-")
        # 마크다운에는 색이 없으므로 방향은 화살표가 진다.
        mark = arrow(r.delta_pct)
        comparison = f"{mark} {r.comparison}".strip() if mark else r.comparison
        lines.append(f"| {r.label} | {value_cell} | {comparison} | {src} |")
    return "\n".join(lines)


def _facts_table_md(rows: list[FactRow]) -> str:
    """§1③-1 접기 + 기존 표. 접은 부분은 markdown 안에 raw `<details>`로 낸다
    — Obsidian의 live preview는 CommonMark 원문 안의 raw HTML 블록을 그대로
    렌더링하고(공식 Help: "HTML은 문서 안에서 그대로 렌더된다"), `<details>`는
    커뮤니티에서 접이 노트를 만들 때 실제로 쓰는 관용구다. 블록 앞뒤로 빈 줄을
    둬야 그 안의 마크다운 표가 계속 표로 파싱된다(HTML 블록 규칙)."""
    if not rows:
        return "(해당 없음)"
    changed, unchanged = split_unchanged(rows)
    parts = []
    if changed:
        parts.append(_facts_table_md_raw(changed))
    elif not unchanged:
        return "(해당 없음)"
    if unchanged:
        parts.append(
            f"<details><summary>변화 없음 {len(unchanged)}건 펼치기</summary>\n\n"
            f"{_facts_table_md_raw(unchanged)}\n\n</details>"
        )
    return "\n\n".join(parts)


def _sector_index_table_md(groups) -> str:
    """업종 지수 표(마크다운). 관측이 0이어도 제목과 "관측 없음"은 낸다 —
    표가 조용히 사라지면 독자는 그날 업종이 안 움직였다고 읽는다(§3.3).

    spec 20260810-period-report §1③-4: "시장 반응" 섹션이 실측 2,300px로
    페이지에서 가장 컸다(개별 종목 표 + 이 업종 지수 표 + 업종 묶음 표 3개가
    한 섹션에 쌓인다). 업종 지수는 개별 종목표를 이미 본 다음에 보는 두 번째
    질문("업종별로는?")이라 부록에 가깝다 — 설명 문구는 그대로 보이고 표만
    접는다(§2 규칙4)."""
    parts = [SECTOR_INDEX_NOTE, ""]
    if not groups:
        parts.append("(관측 없음 — 차단선 이전에 알려진 업종 지수 종가가 없습니다)")
        return "\n".join(parts)
    n = sum(len(rows) for _, rows in groups)
    body_parts = []
    for market_label, rows in groups:
        body_parts += [f"**{market_label}**", "", "| 업종 | 수치 | 등락 | 원자료 |", "|---|---:|---|---|"]
        for r in rows:
            badge = status_ko(r.data_status)
            value_cell = f"{r.value} · {badge}" if badge else r.value
            href = safe_href(r.source_url)
            src = f"[원자료]({href})" if href else (r.source_url or "-")
            mark = arrow(r.delta_pct)
            change = f"{mark} {r.comparison}".strip() if mark else r.comparison
            body_parts.append(f"| {r.label} | {value_cell} | {change} | {src} |")
        body_parts.append("")
    body = "\n".join(body_parts).rstrip()
    parts.append(f"<details><summary>업종 지수 {n}개 펼치기</summary>\n\n{body}\n\n</details>")
    return "\n".join(parts)


def _sector_table_md(rows) -> str:
    """업종 묶음 표(마크다운). 표만 접는다 — 이유는 `_sector_index_table_md`
    주석과 같다."""
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
    table = "\n".join(lines)
    return (f"<details><summary>업종 묶음 {len(rows)}개 펼치기</summary>\n\n{table}\n\n</details>"
            f"\n\n{SECTOR_NOTE}")


def _calendar_table_md(columns: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(해당 없음)"
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _flow_table_md_raw(groups: list[dict]) -> str:
    lines = ["| 종목 | 개인 | 기관 | 외국인 | 한 줄 |", "|---|---:|---:|---:|---|"]
    for g in groups:
        cells = {a["label"]: a["text"] for a in g["actors"]}
        lines.append(
            f"| {g['name']} | {cells.get('개인', '-')} | {cells.get('기관', '-')} "
            f"| {cells.get('외국인', '-')} | {g['story']} |"
        )
    return "\n".join(lines)


def _flow_md(groups: list[dict]) -> str:
    """마크다운에는 막대도 색도 없다. 대신 **종목당 한 줄**이라는 요점은
    그대로 살린다 — 표를 종목 하나에 한 행으로 접고, 그 옆에 문장을 둔다.

    spec 20260810-period-report §1③-1: "움직임 작음"도 안 움직인 행과 같은
    사정이다 — 접는다(`render_html._flow_html`과 같은 판단)."""
    if not groups:
        return "(해당 없음)"
    active = [g for g in groups if not g["quiet"]]
    quiet = [g for g in groups if g["quiet"]]
    parts = []
    if active:
        parts.append(_flow_table_md_raw(active))
    if quiet:
        parts.append(
            f"<details><summary>움직임 작음 {len(quiet)}건 펼치기</summary>\n\n"
            f"{_flow_table_md_raw(quiet)}\n\n</details>"
        )
    return "\n\n".join(parts) if parts else "(해당 없음)"


def _macro_cards_md(rows: list[FactRow], rest: list[FactRow]) -> str:
    """카드는 HTML만의 모양이라 마크다운은 표로 낸다. 다만 **접는 규칙은
    같다** — 앞자리 지표만 싣고 나머지는 아래에 이어 붙인다."""
    if not rows:
        return "(해당 없음)"
    out = _facts_table_md(rows)
    if rest:
        out += f"\n\n_그 밖의 거시지표 {len(rest)}개_\n\n" + _facts_table_md(rest)
    return out


def _filing_summary_md(rows: list[FactRow]) -> str:
    s = filing_summary(rows)
    if not s["total"]:
        return "(해당 없음)"
    line = (f"**공시 {s['total']}건** — 실적 발표 {s['earnings_count']} · "
            f"그 밖의 정기공시·13F {s['other_count']}.")
    if s["earnings_subjects"]:
        line += f" 실적을 낸 곳: {' · '.join(s['earnings_subjects'])}."
    return f"{line}\n\n{_facts_table_md(rows)}"


def _unusual_day_md(block: dict) -> str:
    """추이 그래프는 화면(HTML)만의 것이다(`hero`와 같은 이유 — 마크다운에는
    SVG를 넣지 않는다, spec §3). 대신 추이의 관측 구간을 한 줄로 적어, 마크다운만
    읽는 사람도 오늘 값이 어느 범위 안의 어디인지 알 수 있게 한다.
    문구와 상위 5개는 표로 그대로 낸다."""
    parts = []
    if block["headline"]:
        parts.append("  \n".join(block["headline"].split("\n")))
    series = [v for v in (block.get("trend_series") or []) if v is not None]
    if len(series) >= 2 and block.get("trend_label"):
        parts.append(f"{block['trend_label']}: 오늘 {series[-1]:.0f}% "
                     f"(관측 구간 {min(series):.0f}%~{max(series):.0f}%)")
    movers = block["top_movers"]
    if movers:
        # 표 제목은 "종목"이 아니다 — 금·VIX·지수가 함께 들어온다.
        lines = ["| 가장 크게 움직인 것 | 등락 |", "|---|---:|"]
        for r in movers:
            mark = arrow(r.delta_pct)
            change = f"{mark} {r.comparison}".strip() if mark else r.comparison
            lines.append(f"| {r.label} | {change} |")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _chart_md(block) -> str:
    """마크다운에는 SVG를 넣지 않는다(spec §3, `_unusual_day_md`와 같은 규칙).
    대신 **같은 정보를 문장으로** 싣는다 — 그림이 없는 곳에서 정보까지 사라지면
    옵시디언으로만 읽는 사람은 그 사실 자체를 모르게 된다."""
    if not block.title or not block.note:
        return ""
    return f"**{block.title}** — {block.note}"


def _prior_md(p) -> str:
    """지난 해석이 무엇을 말했고 그 사실들이 그 뒤 어떻게 됐나.

    **판정 문구를 쓰지 않는다** — "맞았다/틀렸다"는 읽는 사람의 몫이다. 코드가
    대신 판정하면 오늘 옛 글을 읽고 뜻을 정하는 것(사후 재해석)이 된다.
    """
    if p.unavailable:
        return f"*{p.unavailable}*"
    if not p.rows:
        return ""
    head = f"**{p.report_date} {p.report_type} 해석이 인용한 사실들이 그 뒤 어떻게 됐나**"
    lines = ["| 대상 | 항목 | 그때 | 지금 | |", "|---|---|---:|---:|---|"]
    for r in p.rows:
        lines.append(f"| {r.label} | {r.metric_ko} | {r.then_value} | {r.now_value} | {r.change_ko} |")
    said = []
    if p.reading:
        said.append(f"> **그때 당시 해석** — {strip_fact_numbers(p.reading)}")
    if p.counter_reading:
        said.append(f"> **그때 반대 해석** — {strip_fact_numbers(p.counter_reading)}")
    return "\n\n".join([head, "\n".join(lines), *said])


def _blindspot_md(block: dict) -> str:
    """신고가 있으면 신고를, 없어도 **관측하지 않는 업종 목록은 항상** 싣는다.

    그 목록이 매일 같은 값이라 지루해 보이지만, 빠지면 리포트가 자기 눈먼 자리를
    말하지 않는 리포트가 된다 — 위 업종 지수 표에서 그 업종이 +8%인 것을 보고도
    "우리는 이 업종을 안 본다"는 사실은 어디에도 안 적히게 된다."""
    parts = []
    for r in block["rows"]:
        parts.append(f"- ⚠️ {r.note}")
    if block["unwatched"]:
        parts.append(
            # "업종"이라 못박지 않는다 — 규모 축(러셀2000 소형주)처럼 업종이
            # 아닌 것이 섞이기 때문이다.
            f"- 우리가 보는 기업이 없는 곳: {' · '.join(block['unwatched'])} — "
            f"여기가 움직인 이유는 이 리포트가 말할 수 없습니다.")
    return "\n".join(parts)


def _block_md(block: dict) -> str:
    kind = block["kind"]
    if kind == "prior":
        return _prior_md(block["prior"])
    if kind == "chart":
        return _chart_md(block["block"])
    if kind == "unusual_day":
        return _unusual_day_md(block)
    if kind == "facts":
        return _facts_table_md(block["rows"])
    if kind == "flow":
        return _flow_md(flow_groups(block["rows"]))
    if kind == "macro_cards":
        return _macro_cards_md(block["rows"], block["rest"])
    if kind == "filing_summary":
        return _filing_summary_md(block["rows"])
    if kind == "hero":
        # 카드 배치는 화면(HTML)만의 것이다. 마크다운에는 바로 아래에
        # `headline`이 같은 4개 숫자를 이미 한 줄로 싣고 있으므로, 여기서
        # 또 찍으면 Obsidian 노트에는 같은 값이 두 번 나온다.
        return ""
    if kind == "legend":
        return LEGEND_MD
    if kind == "breadth":
        # `report.breadth`가 여러 줄이면(관측기업 줄 + 코스피/코스닥 줄,
        # spec-b subtask B) 마크다운 한 문단 안에서 줄바꿈이 살아야 한다 —
        # 빈 줄로 나누면 별도 문단이 되어 시각적으로 너무 벌어진다. 마크다운의
        # "하드 브레이크"(줄 끝 공백 두 칸)로 잇는다. 옛 한 줄짜리 텍스트는
        # split이 원소 하나짜리 리스트를 주므로 그대로 통과한다.
        return "  \n".join(block["text"].split("\n"))
    if kind == "sector":
        return _sector_table_md(block["rows"])
    if kind == "sector_index":
        return _sector_index_table_md(block["groups"])
    if kind == "blindspot":
        return _blindspot_md(block)
    if kind == "flow_split":
        b = block["block"]
        return f"> 💧 **자금 갈래 — {b.verdict}**\n>\n> {b.note}" if b.is_notable else ""
    if kind == "subheading":
        return f"### {block['text']}" if block["text"] else ""
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
        bodies = [b for b in (_block_md(block) for block in blocks) if b]
        if header in APPENDIX_SECTIONS and bodies and bodies != [NO_DATA_MD]:
            combined = "\n\n".join(bodies)
            n = appendix_count(blocks)
            label = f"{header} {n}건 펼치기" if n else f"{header} 펼치기"
            parts += [f"<details><summary>{label}</summary>\n\n{combined}\n\n</details>", ""]
        else:
            for body in bodies:
                parts += [body, ""]
    body = "\n".join(parts) + "\n"
    return (_frontmatter(report) + "\n" + body) if obsidian_frontmatter else body
