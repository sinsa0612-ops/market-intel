"""Report assembly (spec B6/B7): report_type -> `Report`.

**Facts reach this module only through `db.facts_as_of`** (spec B5 contract
3, enforced by `test_reports_never_touch_sql`: neither the PIT fact
table's name nor a raw SQL query keyword may appear anywhere below
`reporting/`). The one raw SQL statement in this file is an `INSERT OR
IGNORE INTO data_gaps` — a write, not a read of the PIT store, and the
same pattern `schedule.py` already uses for `ensure_calendar_gaps`.

Design choices made here (this subtask is "open" per spec — these are the
calls the architect left to ST2, not things the spec pinned):

- **facts vs market_reaction split**: `facts` = what happened (macro
  releases, filings, corporate events, KR investor flows) — the trigger
  layer. `market_reaction` = how prices moved in response — the numeric
  layer B5 already carved out of "시장 반응" ("시장 반응 = 숫자이므로 사실
  계층"). This mirrors §2.1's fact/interpretation split one level down.
- **"기준일" for stale facts (spec B7)**: `FactRow` has no `event_at`
  field (B5's shape is fixed) so the "기준일 {event_at 날짜}" annotation
  the spec requires for `partial`/`unverified` facts is appended directly
  onto `FactRow.value` at build time (`_fmt_value_with_asof`) rather than
  computed by the renderer. The renderer then only needs the data_status
  badge — value and badge always travel together.
- **CalendarRow.data_status**: `schedule.upcoming()` already returns an
  ST1-owned, pre-translated Korean string (`status=`) — not the raw
  `source_verified`/... enum `FactRow.data_status` carries. That return
  shape is frozen (spec ST1: "반환 형태는 rev1과 동일하게 유지"), so
  `CalendarRow.data_status` here is that Korean string verbatim, unlike
  `FactRow.data_status` which carries the raw enum for the renderer to map.
- **CalendarRow has no old/new fields** (B5's shape is fixed): a
  `schedule.changes()` row's old→new move is folded into `CalendarRow.name`
  (`"{name} {old} → {new}"`), mirroring the CLI's own
  `<name> <old> -> <new>` output (spec B13).
- **업종·시장 폭·추이 (가독성 요구)**: 업종은 명세 §12 표를 `universe.py`가
  들고, 여기서는 묶기만 한다(`_sector_summary`). 대표값은 평균이 아니라
  중앙값이고 근거는 `model.SectorSummary`에 있다. 추이 그래프의 시계열은
  **리포트 JSON에 담아** 내보낸다 — 사이트가 DB를 직접 뒤져 시계열을 만들면
  그 순간 리포트의 정보차단선 밖으로 나가고, "리포트가 정본이고 사이트는
  렌더러"라는 구조도 깨진다. `_price_map`이 쓰는 단 한 번의
  `facts_as_of(cutoff)` 결과에서 그대로 잘라내므로 PIT 규칙이 하나로 유지된다.
- **업종 표가 둘인 이유**: `sector_index`(업종 지수 ETF 16개)는 "시장 전체가
  업종별로 어떻게 움직였나"에, `sector_summary`(Core 16 기업 묶음)는 "내가
  관측하는 16개 기업은 어땠나"에 답한다. 서로 다른 질문이라 합치면 둘 다
  잃는다. **두 집계는 입력이 겹치지 않는다** — 지수 쪽은
  `SECTOR_INDEX_SYMBOLS`만, 기업 쪽은 `CORE16_SYMBOLS`만 본다.
- **monthly regime rule** (§6.3, spec B6): uses each series' latest stored
  observation vs. the one immediately before it in the PIT store. For the
  monthly FRED series (CPI/PCE/UNRATE) that already IS month-over-month
  (they only publish once a month); for the two daily series (DGS10,
  T10Y2Y) and DXY it is whatever cadence has actually been collected so
  far — honest given the data on hand, not a synthetic 30-day lookback.
  Every input value is written to `meta["regime_rule"]` so the choice is
  auditable. The label set (§6.3) has 6 entries; "지정학·공급충격" needs
  qualitative signals this stage doesn't collect (§8, deliberately out of
  scope) and is therefore never reachable by this deterministic rule —
  that is a scope boundary, not a bug.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, timedelta

from .. import db as db_mod
from .. import schedule as schedule_mod
from ..universe import (
    CORE16_SYMBOLS,
    SECTOR_BY_SYMBOL,
    SECTOR_INDEX_SYMBOLS,
    SECTORS,
    UNIVERSE,
)
from .cutoff import KST
from .model import (
    CalendarRow,
    FactRow,
    Interpretation,
    MissingItem,
    Report,
    SectorIndexRow,
    SectorSummary,
    worst_data_status,
)

_UNIVERSE_BY_SYMBOL = {m["symbol"]: m for m in UNIVERSE}

# spec §6.1 "지수/위험선호/실물 변수" — everything that is not a Core16
# equity but still belongs in the daily "시장의 체온" snapshot.
#
# **Derived from the universe, never hand-listed.** This was a second copy of
# the symbol list, so adding an index to `universe.py` collected it but never
# showed it — the failure mode is silent, and it is how a tracked index goes
# missing from the report nobody notices. Declaration order in `universe.py`
# is the display order (지수 → 위험선호 → 실물).
#
# `sector_index`가 이 목록에 **일부러 없다**: 업종 지수 16개는 자기 표
# (`_sector_index_rows`)에서 등락률 순으로 보이므로, 여기에도 넣으면 CEO가 같은
# 16줄을 한 섹션 안에서 두 번 읽는다. 새 asset_type을 이 조건에 넣고 싶어질
# 때를 대비해 남긴다 — 그 중복은 테스트가 잡는다
# (test_sector_indices_do_not_enter_the_main_market_reaction_table).
_MARKET_REACTION_SYMBOLS = [
    m["symbol"] for m in UNIVERSE
    if not m["core16"] and m["asset_type"] in ("index", "rate", "fx", "commodity")
]
_CORE16_MOVE_THRESHOLD = 3.0  # spec §6.1 "Core 16 중 하루 3% 이상 움직인 기업만"
_CLOSE_DELTA_MANDATORY = {"^KS11", "KRW=X"}  # spec B6 close_delta 선정 규칙
# 스파크라인에 담을 종가 개수(최근 것부터). 현재 수집분이 심볼당 5~6일치라
# 6이면 사실상 "있는 만큼 전부"이고, 수집이 쌓여도 리포트 JSON이 무한정
# 커지지 않는다.
SPARKLINE_POINTS = 6
# n<=2인 축은 '업종'이 아니라 개별 종목이다(§12 기준 금융·신용 2, 소비·수출 2,
# 헬스케어 1). 대표값을 지우지는 않되 표본이 적다는 사실을 함께 싣는다.
_SMALL_SAMPLE_MAX = 2

_MACRO_LABELS = {
    "CPIAUCSL": "미국 CPI", "PCEPI": "미국 PCE", "UNRATE": "미국 실업률",
    "PAYEMS": "미국 비농업고용", "FEDFUNDS": "연방기금금리(실효)", "DFEDTARU": "연방기금금리 상단(목표)",
    "DGS10": "미국채 10년물 금리", "DGS2": "미국채 2년물 금리", "T10Y2Y": "미 10Y-2Y 금리차",
    "RSAFS": "미국 소매판매", "INDPRO": "미국 산업생산지수",
    "base_rate": "한국 기준금리", "usd_krw_fx": "원/달러 환율(ECOS)",
    "exports_total": "한국 수출(총액)", "semiconductor_exports": "한국 반도체 수출",
}
_FINANCIALS_LABELS = {
    "revenue": "매출", "operating_income": "영업이익", "operating_cash_flow": "영업현금흐름",
    "capex": "CAPEX", "free_cash_flow": "FCF",
}
_COMPARISON_BASIS_KO = {"quarterly": "분기", "annual": "연간", "unknown": "기간 미상"}

# spec B7 / judge.md 「양쪽 다 틀린 것」 4 + [운영 이슈]: "차단선 이전에 알려진
# fact가 0건"은 그 자체가 §11 레지스터에 올라가야 할 결측이다. 현재 시간표
# (collect 07:20~07:40 vs morning 차단선 07:15)에서 morning 리포트는 매일 빈
# 채로 나오는데, 두 구현 다 종료코드 0으로 조용히 배포했다. 스케줄 시각 자체는
# ST3 소관이므로 여기서는 고치지 않고 **드러내기만** 한다.
NO_FACTS_GAP_ID = "report:no_facts_before_cutoff"
NO_FACTS_AREA = "차단선 이전 사실 0건"
NO_FACTS_REASON = (
    "이 리포트의 차단선 이전에 알려진(known_at <= cutoff) fact가 0건이라 사실·시장반응 표가 "
    "모두 비었다. 수집 job이 차단선보다 늦게 도는지(B10 시간표 vs B6 차단선) 확인이 필요하다. "
    "빈 리포트를 조용히 배포하지 않기 위해 결측으로 신고한다."
)

KR_FLOW_GAP_ID = "kr_flows:net_buy"
KR_FLOW_GAP_REASON = (
    "pykrx 수급 provider가 0건을 반환함(NO_DATA/empty_response, KRX 인증벽으로 investor-flow "
    "엔드포인트가 전 시장/전 종목에 대해 빈 응답 — 2026-08-01 실측, 1단계 spec 참조). KRX 키가 "
    "도입되어 provider가 fact를 내면 이 리포트 빌더는 코드 수정 없이 수급 섹션을 채운다."
)

TITLES = {
    "morning": "모닝", "week_start": "주간 시작 브리핑", "close_delta": "장마감 델타",
    "weekly_review": "주간 리뷰", "monthly": "월간 거시 체제", "quarterly": "분기 실적 리뷰",
    "annual": "연간 리뷰", "event": "실적 이벤트",
}
_EVENTS_WINDOW_DAYS = {
    "morning": 14, "week_start": 14, "close_delta": 14, "weekly_review": 30,
    "monthly": 45, "quarterly": 90, "annual": 120, "event": 30,
}


# --- generic fact -> FactRow -------------------------------------------

def _fmt_value_with_asof(row) -> str:
    val = row["value_num"]
    unit = row["unit"] or ""
    if val is not None:
        if unit == "percent":
            base = f"{val:.2f}%"
        elif unit and abs(val) >= 1000:
            base = f"{val:,.0f} {unit}"
        elif unit:
            base = f"{val:,.2f} {unit}"
        elif abs(val) >= 1000:
            base = f"{val:,.0f}"
        else:
            base = f"{val:,.4g}"
    else:
        base = row["value_text"] or "미확인"
    if row["data_status"] in ("partial", "unverified") and row["event_at"]:
        base = f"{base} (기준일 {row['event_at'][:10]})"
    return base


def _row_from_fact(
    row, label: str, comparison: str, delta_pct: float | None = None,
    value: str | None = None, doc_url: str = "",
) -> FactRow:
    """`value`는 표시 문자열을 직접 지정할 때만 쓴다(공시 행처럼 `value_text`가
    수치가 아닌 경우). 기본은 값+기준일 조합인 `_fmt_value_with_asof`.

    `doc_url`은 사람이 읽는 문서 주소다. `source_url`(수집 엔드포인트)을
    덮어쓰지 않고 나란히 싣는다 — 감사 추적은 그대로 두고 링크만 쓸모 있게."""
    return FactRow(
        label=label, value=value if value is not None else _fmt_value_with_asof(row),
        comparison=comparison,
        source_url=row["safe_source_url"] or "", data_status=row["data_status"] or "unverified",
        known_at=row["known_at"], subject=row["subject"], metric=row["metric"],
        raw_value=row["value_num"] if row["value_num"] is not None else row["value_text"],
        delta_pct=delta_pct, doc_url=doc_url,
    )


# --- prices / market reaction -------------------------------------------

def _price_map(conn, cutoff) -> dict[str, dict]:
    """subject -> {latest, prev, delta_pct, series} using every price_close
    fact known as of `cutoff` (spec A5-compliant: one `facts_as_of` call, no
    per-symbol filter, grouped in Python — one fact_id exists per trading
    day per symbol, so a subject filter alone would not collapse to
    "today's close").

    `series` (추이 그래프의 입력) is built from **these same rows**, i.e. from
    the one `facts_as_of(cutoff)` read, so the sparkline inherits the report's
    information barrier instead of needing a second PIT rule of its own. A
    close known after the blackout is not in `rows` and therefore cannot be
    in `series`."""
    rows = db_mod.facts_as_of(conn, cutoff, metric="price_close")
    by_subject: dict[str, list] = {}
    for r in rows:
        by_subject.setdefault(r["subject"], []).append(r)
    out: dict[str, dict] = {}
    for subj, rs in by_subject.items():
        rs.sort(key=lambda r: r["event_at"] or "", reverse=True)
        latest, prev = rs[0], (rs[1] if len(rs) > 1 else None)
        delta = None
        if prev is not None and prev["value_num"] not in (None, 0) and latest["value_num"] is not None:
            delta = (latest["value_num"] - prev["value_num"]) / prev["value_num"] * 100
        series = [r["value_num"] for r in reversed(rs[:SPARKLINE_POINTS])
                  if r["value_num"] is not None]
        out[subj] = {"latest": latest, "prev": prev, "delta_pct": delta, "series": series}
    return out


def _fmt_price_value(row) -> str:
    v = row["value_num"]
    if v is None:
        return row["value_text"] or "미확인"
    unit = row["unit"] or ""
    if unit == "percent":
        return f"{v:.2f}%"
    if unit == "KRW":
        return f"{v:,.1f}원"
    if unit == "point":
        # An index is quoted in points by convention; printing the word made
        # the card read "6,595.45 point" and pushed the value onto two lines
        # on a phone. The unit still travels in the fact — this is display.
        return f"{v:,.2f}"
    if unit:
        return f"{v:,.2f} {unit}"
    return f"{v:,.2f}"


def _session_gap_days(latest, prev) -> int | None:
    """Calendar days between the two compared closes, or None if unknowable."""
    if prev is None:
        return None
    try:
        a = date.fromisoformat((latest["event_at"] or "")[:10])
        b = date.fromisoformat((prev["event_at"] or "")[:10])
    except (ValueError, TypeError, IndexError, KeyError):
        return None
    return abs((a - b).days)


def _comparison_text(info: dict) -> str:
    """"전일대비 +1.2%" 같은 비교 문구.

    "전일대비" is only true when the two observations really are adjacent
    sessions. After a holiday, a collection outage, or a backfill the
    previous close can be many days back, and calling that "전일" states a
    fact the data does not support (final-review.md F4). Name the actual
    gap instead; one trading day keeps the familiar wording."""
    if info["delta_pct"] is None:
        return "전일 비교 불가(직전 종가 없음)"
    gap = _session_gap_days(info["latest"], info.get("prev"))
    if gap is None:
        return f"직전 종가 대비 {info['delta_pct']:+.2f}%"
    if gap <= 1:
        return f"전일대비 {info['delta_pct']:+.2f}%"
    return f"{gap}일 전 종가 대비 {info['delta_pct']:+.2f}%"


def _market_reaction_row(subj: str, info: dict) -> FactRow:
    row = info["latest"]
    meta = _UNIVERSE_BY_SYMBOL.get(subj, {})
    label = meta.get("name_ko") or meta.get("name", subj)
    comparison = _comparison_text(info)
    return FactRow(
        label=label, value=_fmt_price_value(row), comparison=comparison,
        source_url=row["safe_source_url"] or "", data_status=row["data_status"] or "unverified",
        known_at=row["known_at"], subject=subj, metric=row["metric"], raw_value=row["value_num"],
        delta_pct=info["delta_pct"], series=list(info.get("series") or []),
    )


def _market_reaction(price_map: dict) -> list[FactRow]:
    rows = [_market_reaction_row(s, price_map[s]) for s in _MARKET_REACTION_SYMBOLS if s in price_map]
    for s in CORE16_SYMBOLS:
        info = price_map.get(s)
        if info and info["delta_pct"] is not None and abs(info["delta_pct"]) >= _CORE16_MOVE_THRESHOLD:
            rows.append(_market_reaction_row(s, info))
    return rows


def _close_delta_rows(price_map: dict) -> list[FactRow]:
    """spec B6: 3~5 items, absolute-move-rank order, KOSPI + USD/KRW always
    included when their data exists."""
    candidates = [
        (s, price_map[s]) for s in (_MARKET_REACTION_SYMBOLS + CORE16_SYMBOLS)
        if s in price_map and price_map[s]["delta_pct"] is not None
    ]
    mandatory = [(s, i) for s, i in candidates if s in _CLOSE_DELTA_MANDATORY]
    rest = sorted(
        ((s, i) for s, i in candidates if s not in _CLOSE_DELTA_MANDATORY),
        key=lambda t: -abs(t[1]["delta_pct"]),
    )
    ordered, seen, selected = mandatory + rest, set(), []
    for s, i in ordered:
        if s in seen:
            continue
        seen.add(s)
        selected.append((s, i))
        if len(selected) >= 5:
            break
    return [_market_reaction_row(s, i) for s, i in selected]


def _headline(price_map: dict) -> str:
    """spec B6 fixed template. A missing piece becomes the literal word
    '미확인' in place of its value+% fragment (never omitted, never blank —
    [ASSUMPTION]: the label prefix is kept so the reader still knows which
    indicator is missing, e.g. 'KOSPI 미확인')."""
    def part(symbol: str, prefix: str, fmt) -> str:
        info = price_map.get(symbol)
        if not info or info["latest"]["value_num"] is None:
            return f"{prefix} 미확인"
        pct = info["delta_pct"]
        pct_str = f"{pct:+.1f}%" if pct is not None else "미확인"
        return f"{prefix} {fmt(info['latest']['value_num'])}({pct_str})"

    kospi = part("^KS11", "KOSPI", lambda v: f"{v:,.2f}")
    spx = part("^GSPC", "S&P500", lambda v: f"{v:,.2f}")
    krw = part("KRW=X", "달러/원", lambda v: f"{v:,.1f}원")
    us10y = part("^TNX", "미10Y", lambda v: f"{v:.2f}%")
    n_movers = sum(
        1 for s in CORE16_SYMBOLS
        if price_map.get(s) and price_map[s]["delta_pct"] is not None
        and abs(price_map[s]["delta_pct"]) >= _CORE16_MOVE_THRESHOLD
    )
    n_missing = sum(1 for s in CORE16_SYMBOLS if s not in price_map)
    return f"{kospi} · {spx} · {krw} · {us10y} — Core16 중 ±3% 이상 {n_movers}종목, 결측 {n_missing}건"


# --- 업종·시장 폭 (spec §12 표 + §6.1 "시장 폭과 순환은 어떤가") -----------

def _sector_summary(price_map: dict) -> list[SectorSummary]:
    """spec §12의 6축 전부를 항상, 표의 순서대로 낸다.

    관측이 0인 축도 빼지 않는다: "헬스케어 관측 없음"은 노이즈가 아니라
    결측이고, 축이 조용히 사라지면 독자는 시장 폭을 실제보다 넓게 읽는다
    (§3.3 미확인은 채우지 않고 드러낸다).

    대표값은 **평균이 아니라 중앙값**이다 — 근거는 `SectorSummary` 참조.
    """
    out: list[SectorSummary] = []
    for sector in SECTORS:
        deltas = [
            price_map[s]["delta_pct"]
            for s in CORE16_SYMBOLS
            if SECTOR_BY_SYMBOL.get(s) == sector
            and s in price_map and price_map[s]["delta_pct"] is not None
        ]
        out.append(SectorSummary(
            sector=sector,
            up=sum(1 for d in deltas if d > 0),
            down=sum(1 for d in deltas if d < 0),
            flat=sum(1 for d in deltas if d == 0),
            total=len(deltas),
            median_pct=statistics.median(deltas) if deltas else None,
            small_sample=0 < len(deltas) <= _SMALL_SAMPLE_MAX,
        ))
    return out


def _sector_index_rows(price_map: dict) -> list[SectorIndexRow]:
    """업종 지수 표: 어느 업종이 오르고 내렸나, 등락률 순으로.

    **Core 16 집계와 완전히 분리된 계산이다.** 여기 쓰이는 심볼은
    `SECTOR_INDEX_SYMBOLS`뿐이고, `_sector_summary`/`_breadth_line`/`_headline`
    은 `CORE16_SYMBOLS`만 본다 — 두 집계가 서로의 입력에 닿지 않는다는 것이
    "Core 16 중 4/6개 상승"이 계속 참인 이유다(회귀 테스트:
    tests/reporting/test_sector_indices.py).

    시장(미국/한국)별로 나눠 각각 등락률 내림차순. 등락을 모르는 행(직전 종가
    없음)은 순위를 매길 수 없으므로 그 시장의 맨 뒤로 보낸다 — 0%로 취급해서
    중간에 끼워 넣으면 모르는 것을 안다고 말하는 셈이다."""
    by_market: dict[str, list[SectorIndexRow]] = {}
    for symbol in SECTOR_INDEX_SYMBOLS:
        meta = _UNIVERSE_BY_SYMBOL[symbol]
        info = price_map.get(symbol)
        if info is None:
            continue
        row = info["latest"]
        by_market.setdefault(meta["market"], []).append(SectorIndexRow(
            subject=symbol,
            label=meta.get("name_ko") or meta.get("name", symbol),
            market=meta["market"],
            value=_fmt_price_value(row),
            comparison=_comparison_text(info),
            delta_pct=info["delta_pct"],
            series=list(info.get("series") or []),
            source_url=row["safe_source_url"] or "",
            data_status=row["data_status"] or "unverified",
        ))
    out: list[SectorIndexRow] = []
    for market in dict.fromkeys(_UNIVERSE_BY_SYMBOL[s]["market"] for s in SECTOR_INDEX_SYMBOLS):
        rows = by_market.get(market, [])
        rows.sort(key=lambda r: (r.delta_pct is None, -(r.delta_pct or 0.0)))
        out += rows
    return out


def _breadth_line(summaries: list[SectorSummary]) -> str:
    """§6.1 "시장 전체인가, 대형주 쏠림인가"에 한 줄로 답한다: Core 16 전체의
    상승 비율 + 축별 상승 비율. 한 축만 4/4이고 나머지가 0/n이면 그 한 줄이
    이미 "반도체 쏠림"이라고 말한다."""
    observed = sum(s.total for s in summaries)
    if not observed:
        return "Core 16 등락 관측 없음 — 시장 폭을 계산할 종가가 차단선 이전에 없습니다."
    up = sum(s.up for s in summaries)
    parts = [
        f"{s.sector} {s.up}/{s.total}" if s.total else f"{s.sector} 관측 없음"
        for s in summaries
    ]
    return f"Core 16 중 {up}/{observed}개 상승 · " + " · ".join(parts)


# --- macro ----------------------------------------------------------------

def _macro_map(conn, cutoff) -> dict[str, dict]:
    rows = db_mod.facts_as_of(conn, cutoff, category="macro", metric="value")
    by_subject: dict[str, list] = {}
    for r in rows:
        by_subject.setdefault(r["subject"], []).append(r)
    out: dict[str, dict] = {}
    for subj, rs in by_subject.items():
        rs.sort(key=lambda r: r["event_at"] or "", reverse=True)
        latest, prev = rs[0], (rs[1] if len(rs) > 1 else None)
        delta_abs = delta_pct = None
        if prev is not None and latest["value_num"] is not None and prev["value_num"] is not None:
            delta_abs = latest["value_num"] - prev["value_num"]
            if prev["value_num"] != 0:
                delta_pct = delta_abs / prev["value_num"] * 100
        out[subj] = {"latest": latest, "prev": prev, "delta_abs": delta_abs, "delta_pct": delta_pct}
    return out


def _macro_label(subj: str, latest) -> str:
    """FRED keys its facts by the series id (`CPIAUCSL`), but ECOS keys them by
    the statistic/item code pair (`722Y001.0101000`) and carries the readable
    name in `extra.logical_key` / `extra.stat_name`. Looking up only by subject
    left every Korean macro row printed as its raw code — "722Y001.0101000
    2.50 연%" instead of "한국 기준금리". Fall through subject → logical_key →
    the source's own statistic name before giving up and showing the code."""
    if subj in _MACRO_LABELS:
        return _MACRO_LABELS[subj]
    try:
        extra = json.loads(latest["extra_json"] or "{}")
    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
        return subj
    key = extra.get("logical_key")
    if key and key in _MACRO_LABELS:
        return _MACRO_LABELS[key]
    stat_name = (extra.get("stat_name") or "").strip()
    return stat_name or subj


def _macro_facts(mmap: dict) -> list[FactRow]:
    out = []
    for subj, info in mmap.items():
        label = _macro_label(subj, info["latest"])
        comparison = f"직전 관측 대비 {info['delta_pct']:+.2f}%" if info["delta_pct"] is not None else "직전 관측 없음"
        # 거시지표는 관측이 1개씩이라 시계열 그래프는 나오지 않는다(series 없음).
        # 방향(화살표·색)은 직전 관측이 있을 때만 붙는다.
        out.append(_row_from_fact(info["latest"], label, comparison, delta_pct=info["delta_pct"]))
    out.sort(key=lambda r: r.subject)
    return out


# --- financials / filings / flows -----------------------------------------

def _subject_name(subject: str) -> str:
    """Company name with its ticker, so a Korean listing is readable.

    Price rows already resolve names through the universe, but financial and
    filing rows printed the bare symbol — the published site showed
    "000660.KS 영업이익" where a reader needs "SK Hynix(000660.KS)".
    Unknown symbols (fund managers in 13F rows, for instance) pass through
    unchanged rather than being invented."""
    meta = _UNIVERSE_BY_SYMBOL.get(subject)
    name = (meta or {}).get("name_ko") or (meta or {}).get("name")
    return f"{name}({subject})" if name else subject


def _financials_facts(conn, cutoff, subjects: list[str] | None = None) -> list[FactRow]:
    rows = db_mod.facts_as_of(conn, cutoff, category="financials")
    out = []
    for row in rows:
        if subjects is not None and row["subject"] not in subjects:
            continue
        label = f"{_subject_name(row['subject'])} {_FINANCIALS_LABELS.get(row['metric'], row['metric'])}"
        basis = _COMPARISON_BASIS_KO.get(row["comparison_basis"], row["comparison_basis"] or "")
        out.append(_row_from_fact(row, label, basis))
    out.sort(key=lambda r: (r.subject, r.metric))
    return out


_FORM_LABELS = {
    "10-K": "연간보고서(10-K)", "10-Q": "분기보고서(10-Q)", "8-K": "수시공시(8-K)",
    "6-K": "외국기업 수시보고(6-K)", "20-F": "외국기업 연차보고(20-F)",
    "13F-HR": "13F 보유내역", "13F-HR/A": "13F 보유내역(정정)",
}
_FILING_METRIC_LABELS = {
    "earnings_release_8k": "실적 발표 공시",
    "filing_event": "정기공시 제출",
}


def _extra(row) -> dict:
    try:
        return (json.loads(row["extra_json"] or "{}") or {})
    except (json.JSONDecodeError, TypeError):
        return {}


def _filing_kind(row) -> str:
    """"AMZN · earnings_release_8k" 대신 "Amazon(AMZN) 실적 발표 공시(8-K)"."""
    base = _FILING_METRIC_LABELS.get(row["metric"], row["metric"])
    form = _extra(row).get("form")
    if row["metric"] == "filing_event" and form:
        return _FORM_LABELS.get(form, f"{base}({form})")
    if form and form not in base:
        return f"{base}({form})"
    return base


# SEC 8-K 항목 코드 -> 그 공시가 무엇을 알리는 것인지. 지금 수집기가 감지하는
# 것은 2.02 하나뿐이지만(sec_8k_events.ITEM_EARNINGS_RELEASE), 코드만 찍히면
# `항목 2.02`는 접수번호와 똑같이 읽는 사람에게 아무 말도 하지 않는다.
_ITEM_8K_LABELS = {
    "1.01": "주요 계약 체결", "2.01": "자산 취득·처분", "2.02": "실적·재무상태 발표",
    "5.02": "임원 변동", "7.01": "기업 설명자료", "8.01": "기타 주요 사항",
    "9.01": "재무제표·첨부",
}


def _filing_detail(row) -> str:
    """그 공시가 **무슨 문서인지** 한 마디로. 없으면 빈 문자열."""
    extra = _extra(row)
    report_name = str(extra.get("report_name") or "").strip()  # DART가 주는 보고서명
    if report_name:
        return report_name
    items = [i.strip() for i in str(extra.get("item") or "").split(",") if i.strip()]
    named = [f"{_ITEM_8K_LABELS[i]}(항목 {i})" for i in items if i in _ITEM_8K_LABELS]
    # 13F는 라벨이 이미 `Berkshire Hathaway 13F 보유내역`이라 덧붙일 말이 없다.
    return " · ".join(named)


def _days_ago(when: str, ref) -> str:
    """제출일 옆의 경과일. NVDA의 2026-05-20자 8-K가 사흘 전 것과 나란히
    서 있어도 어느 쪽이 오늘 뉴스인지 한눈에 갈리게 하는 유일한 표시다."""
    try:
        days = (ref - date.fromisoformat(when)).days
    except (TypeError, ValueError):
        return ""
    if days < 0:
        return ""
    return "오늘" if days == 0 else f"{days}일 전"


def _filing_doc_url(row) -> str:
    """접수번호 하나로 **사람이 읽는 공시 문서**를 연다.

    `source_url`은 수집 엔드포인트(`data.sec.gov/submissions/CIK*.json`,
    `opendart.fss.or.kr/api/list.json`)라서 클릭하면 JSON 원문이 뜬다.
    실측 2026-08-03: EDGAR의 `Archives` 경로는 URL의 CIK 자리를 무시하고
    접수번호만으로 문서를 찾아 준다(일부러 999999를 넣어도 200 + 올바른
    Berkshire 13F). 그래서 접수번호 앞 10자리(제출 대행사 CIK)를 그대로 써도
    되고, 별도 CIK 저장이 없는 13F 행까지 소급 적용된다.
    DART는 접수번호(rcpNo)가 곧 뷰어 주소다."""
    acc = (row["value_text"] or "").strip()
    if re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc):  # SEC
        return (f"https://www.sec.gov/Archives/edgar/data/{int(acc[:10])}/"
                f"{acc.replace('-', '')}/{acc}-index.htm")
    if re.fullmatch(r"\d{14}", acc):  # DART 접수번호
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={acc}"
    return ""


def _filing_value(row, ref=None) -> str:
    """공시 행의 '수치' 칸.

    1차(2026-08-02): 접수번호가 금액인 것처럼 실리던 문제를 고쳐 제출일을 앞에
    세웠다. 2차(CEO 지적 2026-08-03): 제출일과 접수번호만 있으니 **그 회사에
    대해 알 수 있는 게 아무것도 없다**. 그래서 (a) 경과일을 붙여 오늘 뉴스와
    두 달 묵은 공시를 구분하고, (b) 이미 수집해 놓고 화면에 쓰지 않던 문서
    설명(DART `report_name`, 8-K 항목명)을 싣는다. 접수번호는 사람이 쓰는
    정보가 아니므로 이 칸에서 빼고, 리포트 JSON의 `raw_value`·DB·`doc_url`
    링크에 그대로 남긴다(감사 추적 유지)."""
    when = (row["event_at"] or "")[:10]
    parts = []
    if when:
        ago = _days_ago(when, ref) if ref is not None else ""
        parts.append(f"{when} 제출({ago})" if ago else f"{when} 제출")
    detail = _filing_detail(row)
    if detail:
        parts.append(detail)
    return " · ".join(parts) or (row["value_text"] or "미확인")


def _filing_facts(conn, cutoff, since: str | None = None) -> list[FactRow]:
    rows = list(db_mod.facts_as_of(conn, cutoff, category="filing"))
    rows += list(db_mod.facts_as_of(conn, cutoff, category="13f_filing"))
    rows += list(db_mod.facts_as_of(conn, cutoff, category="event", metric="earnings_release_8k"))
    ref = cutoff.date() if hasattr(cutoff, "date") else None
    out = []
    for row in rows:
        if since is not None and (row["known_at"] or "") < since:
            continue
        # 13F 행의 주체는 유니버스에 없는 운용사 슬러그(`berkshire_hathaway`)라
        # `_subject_name`이 그대로 흘려보낸다. 수집기가 이미 표시명을 넣어 뒀다.
        who = _extra(row).get("manager") or _subject_name(row["subject"])
        label = f"{who} {_filing_kind(row)}"
        out.append(_row_from_fact(
            row, label, row["publisher"] or "",
            value=_filing_value(row, ref), doc_url=_filing_doc_url(row),
        ))
    out.sort(key=lambda r: r.known_at, reverse=True)
    return out


def _consensus_facts(conn, cutoff, subject: str) -> list[FactRow]:
    rows = db_mod.facts_as_of(conn, cutoff, category="calendar", subject=subject)
    out = []
    for row in rows:
        if row["metric"] == "consensus_eps":
            out.append(_row_from_fact(row, "컨센서스 EPS", ""))
        elif row["metric"] == "consensus_revenue":
            out.append(_row_from_fact(row, "컨센서스 매출", ""))
    return out


def _register_gap(conn, gap_id: str, subject: str, metric: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO data_gaps(gap_id, subject, metric, detected_at, reason, status) "
        "VALUES (?,?,?,?,?,?)",
        (gap_id, subject, metric, db_mod.iso_utc(), reason, "제안"),
    )
    conn.commit()


def _kr_flows(conn, cutoff) -> tuple[list[FactRow], list[MissingItem]]:
    """spec B7/R7: KR investor flows are 0 facts today; the report must
    still build, with the gap named in both `missing` and `data_gaps`
    (test_no_kr_flows_still_builds). The moment a flow fact exists, this
    same code fills the section and the gap stops being re-registered as
    outstanding (test_kr_flows_appear_when_present) — the branch is on
    fact presence, not a feature flag."""
    rows = db_mod.facts_as_of(conn, cutoff, category="flow")
    if rows:
        out = [_row_from_fact(r, f"{r['subject']} {r['metric']}", "") for r in rows]
        return out, []
    _register_gap(conn, KR_FLOW_GAP_ID, "kr_flows", "net_buy", KR_FLOW_GAP_REASON)
    missing = [MissingItem(area="한국 수급(외국인/기관/개인)", reason=KR_FLOW_GAP_REASON,
                            since=db_mod.iso_utc(), gap_id=KR_FLOW_GAP_ID)]
    return [], missing


# --- calendar (spec ST1's schedule.py, read-only from here) --------------

def _calendar_source_url_map(conn, cutoff) -> dict[str, str]:
    rows = db_mod.facts_as_of(conn, cutoff, category="calendar", metric="scheduled_date")
    return {r["subject"]: (r["safe_source_url"] or "") for r in rows}


def _events_rows(conn, cutoff, days: int) -> list[CalendarRow]:
    src = _calendar_source_url_map(conn, cutoff)
    return [
        CalendarRow(
            when=r["date"], name=r["name"], country=r["country"], subject=r["subject"],
            importance=r["importance"], change="", source_url=src.get(r["subject"], ""),
            data_status=r["status"],
        )
        for r in schedule_mod.upcoming(conn, cutoff, days=days)
    ]


def _schedule_change_rows(conn, cutoff, days: int) -> list[CalendarRow]:
    since = db_mod.iso_utc(cutoff - timedelta(days=days))
    out = []
    # `cutoff` is passed straight through: `schedule.changes` applies the
    # information barrier itself (known_at <= cutoff) and derives its window
    # anchor from it, so this caller has no workaround left to reinvent.
    for r in schedule_mod.changes(conn, since, cutoff, days=days):
        # CalendarRow has no old/new fields (B5 shape is fixed) — folded
        # into `name`, mirroring the CLI's own `<name> <old> -> <new>`.
        name = f"{r['name']} {r['old']} → {r['new']}" if r["old"] != "-" else f"{r['name']} → {r['new']}"
        out.append(CalendarRow(when=r["date"], name=name, country="", subject="",
                                importance="", change=r["kind"], source_url="", data_status=""))
    return out


# --- monthly regime label (spec B6, §6.3) ---------------------------------

_REGIME_LABELS = (
    "성장 확대", "성장 둔화·디스인플레이션", "인플레이션 재가속",
    "침체 우려", "유동성 완화",
)  # "지정학·공급충격" needs qualitative input this stage does not collect.

# Not one of §6.3's six labels: it is the honest absence of one. With every
# input None the rule below falls through to its "else" branch and asserts
# 「성장 확대」 — a macro regime declared from zero observations, printed in
# the report title (judge.md 「양쪽 다 틀린 것」 3). §3.3: 미확인은 추정으로
# 채우지 않고 확인 과제로 보류한다.
REGIME_UNDECIDABLE = "판정 불가(입력 부족)"
REGIME_GAP_ID = "monthly_regime:inputs"
REGIME_GAP_REASON = (
    "월간 국면 판정에 쓰는 입력(CPI·PCE 전월비, 실업률 변화, DGS10 변화, DXY 변화) 중 "
    "차단선 이전에 알려진 값이 하나도 없어 §6.3 라벨을 결정하지 않았다. 추정으로 채우지 않는다(§3.3)."
)


def _regime_label(mmap: dict, price_map: dict) -> tuple[str, str]:
    cpi = mmap.get("CPIAUCSL", {}).get("delta_pct")
    pce = mmap.get("PCEPI", {}).get("delta_pct")
    unrate = mmap.get("UNRATE", {}).get("delta_abs")
    dgs10 = mmap.get("DGS10", {}).get("delta_abs")
    dxy = price_map.get("DX-Y.NYB", {}).get("delta_pct")

    inputs = f"cpi_mom_pct={cpi} pce_mom_pct={pce} unrate_delta_pp={unrate} dgs10_delta_pp={dgs10} dxy_delta_pct={dxy}"
    if all(v is None for v in (cpi, pce, unrate, dgs10, dxy)):
        return REGIME_UNDECIDABLE, f"{inputs} -> {REGIME_UNDECIDABLE} (유효 입력 0개)"

    inflation_up = (cpi is not None and cpi > 0.3) or (pce is not None and pce > 0.3)
    inflation_down = (cpi is None or cpi <= 0.1) and (pce is None or pce <= 0.1)
    labor_weak = unrate is not None and unrate >= 0.2
    yields_down = dgs10 is not None and dgs10 <= -0.15
    dollar_down = dxy is not None and dxy <= -1.0

    if inflation_up and not labor_weak:
        label = "인플레이션 재가속"
    elif labor_weak and inflation_down:
        label = "성장 둔화·디스인플레이션"
    elif labor_weak:
        label = "침체 우려"
    elif yields_down and dollar_down:
        label = "유동성 완화"
    else:
        label = "성장 확대"

    return label, f"{inputs} -> {label}"


# --- top-level dispatch ----------------------------------------------------

def build_report(
    conn, report_type: str, report_date: date, cutoff: datetime,
    subject: str | None = None,
) -> Report:
    """Assemble a `Report` for `report_type` as of `cutoff` (spec B6/B7).
    Never raises for missing data — absence goes into `missing` +
    `data_gaps`, per the task's "어떤 소스가 죽어도 리포트는 나온다" intent."""
    price_map = _price_map(conn, cutoff)
    mmap = _macro_map(conn, cutoff)
    macro_facts = _macro_facts(mmap)
    flow_facts, flow_missing = _kr_flows(conn, cutoff)
    filing_facts = _filing_facts(conn, cutoff)
    market_reaction_all = _market_reaction(price_map)
    headline = _headline(price_map)
    sector_summary = _sector_summary(price_map)
    sector_index = _sector_index_rows(price_map)
    breadth = _breadth_line(sector_summary)
    win = _EVENTS_WINDOW_DAYS[report_type]
    events = _events_rows(conn, cutoff, win)
    schedule_changes = _schedule_change_rows(conn, cutoff, win)

    meta: dict = {
        "generated_by": "reporting/build.py (2A)", "late_generation": False,
        "no_facts_before_cutoff": False,
    }

    if report_type in ("morning", "week_start"):
        facts = macro_facts + filing_facts + flow_facts
        market_reaction = market_reaction_all
    elif report_type == "close_delta":
        facts = flow_facts + macro_facts + filing_facts
        market_reaction = _close_delta_rows(price_map)
    elif report_type == "weekly_review":
        since = db_mod.iso_utc(cutoff - timedelta(days=7))
        facts = macro_facts + _filing_facts(conn, cutoff, since=since) + flow_facts
        market_reaction = market_reaction_all
    elif report_type == "monthly":
        regime, rule_note = _regime_label(mmap, price_map)
        facts = macro_facts + flow_facts
        market_reaction = market_reaction_all
        meta["regime_label"] = regime
        meta["regime_rule"] = rule_note
    elif report_type in ("quarterly", "annual"):
        facts = _financials_facts(conn, cutoff, subjects=CORE16_SYMBOLS) + filing_facts + flow_facts
        market_reaction = market_reaction_all
    elif report_type == "event":
        subj = subject or ""
        facts = _financials_facts(conn, cutoff, subjects=[subj]) + _consensus_facts(conn, cutoff, subj)
        market_reaction = [r for r in market_reaction_all if r.subject == subj]
        if not market_reaction and subj in price_map:
            market_reaction = [_market_reaction_row(subj, price_map[subj])]
    else:
        raise ValueError(f"build_report: unknown report_type {report_type!r}")

    missing = list(flow_missing)
    if report_type == "monthly" and meta["regime_label"] == REGIME_UNDECIDABLE:
        _register_gap(conn, REGIME_GAP_ID, "monthly_regime", "inputs", REGIME_GAP_REASON)
        missing.append(MissingItem(area="월간 국면 판정 입력", reason=REGIME_GAP_REASON,
                                   since=db_mod.iso_utc(), gap_id=REGIME_GAP_ID))

    all_rows = facts + market_reaction
    if not all_rows:
        _register_gap(conn, NO_FACTS_GAP_ID, "report", "facts_before_cutoff", NO_FACTS_REASON)
        missing.append(MissingItem(area=NO_FACTS_AREA, reason=NO_FACTS_REASON,
                                   since=db_mod.iso_utc(cutoff), gap_id=NO_FACTS_GAP_ID))
        meta["no_facts_before_cutoff"] = True

    status = worst_data_status(all_rows)
    title = TITLES.get(report_type, report_type)
    if report_type == "event":
        title = f"{title} · {subject or ''}"
    if report_type == "monthly":
        title = f"{title} · {meta['regime_label']}"

    meta["counts"] = {
        "facts": len(facts), "market_reaction": len(market_reaction),
        "events": len(events), "schedule_changes": len(schedule_changes), "missing": len(missing),
        "sector_index": len(sector_index),
    }

    return Report(
        report_type=report_type,
        report_date=report_date.isoformat(),
        cutoff_kst=cutoff.astimezone(KST).isoformat(),
        cutoff_utc=db_mod.iso_utc(cutoff),
        generated_at=db_mod.iso_utc(),
        title=title,
        headline=headline,
        breadth=breadth,
        data_status=status,
        facts=facts,
        market_reaction=market_reaction,
        events=events,
        schedule_changes=schedule_changes,
        missing=missing,
        sector_summary=sector_summary,
        sector_index=sector_index,
        interpretation=Interpretation(),
        meta=meta,
    )


def stem_for(report: Report, slug: str | None = None) -> str:
    """spec B6 file-stem table."""
    d = report.report_date
    if report.report_type in ("morning", "week_start", "close_delta", "weekly_review"):
        return d
    if report.report_type == "monthly":
        return d[:7]
    if report.report_type == "quarterly":
        y, m = int(d[:4]), int(d[5:7])
        return f"{y}Q{(m - 1) // 3 + 1}"
    if report.report_type == "annual":
        return d[:4]
    if report.report_type == "event":
        dt = datetime.fromisoformat(report.cutoff_kst)
        return f"{dt.strftime('%Y-%m-%d-%H%M')}-{slug or 'event'}"
    raise ValueError(f"stem_for: unknown report_type {report.report_type!r}")
