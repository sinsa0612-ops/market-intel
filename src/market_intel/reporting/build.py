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
    KOSPI_FLOW_SAMPLE_SYMBOLS,
    KR_CORE_SYMBOLS,
    SECTOR_BY_SYMBOL,
    SECTOR_INDEX_SYMBOLS,
    SECTORS,
    UNIVERSE,
)
from .cutoff import KST
from .model import (
    CalendarRow,
    ChartBlock,
    ChartSeries,
    FactRow,
    Interpretation,
    MissingItem,
    Report,
    SectorIndexRow,
    SectorSummary,
    UnusualDayBlock,
    worst_data_status,
)
# 표시 형식은 렌더러 계층이 정한다(`render_md`가 두 렌더러의 공용 서식 자리).
# 여기서 쓰는 이유: 자릿수 12~15개짜리 금액은 **리포트 JSON 단계에서** 읽을 수
# 있는 형태여야 한다. 그 문자열이 곧 LLM에게 가는 다이제스트 줄이기 때문이다
# (`interp/digest.py`가 `row.value`를 그대로 싣는다) — 원본 숫자를 넘기면
# 모델이 스스로 "2조"로 바꿔 쓰다가 검증기 rule 3에 걸린다(실측 3회).
from .render_md import fmt_money

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

# spec 20260810-period-report §1① — 리포트 종류별 비교 창(거래일 기준).
# "그 주기만" — 병기하지 않는다(CEO 확정). morning/close_delta/week_start/
# event는 표에 없다 = 지금 그대로 직전 거래일(코드 기본값 lookback=1).
#
# 가격(`_price_map`)은 이 숫자를 그대로 **몇 번째 관측 뒤**의 인덱스로 쓴다 —
# "가격 fact는 심볼당 거래일 하나"라 인덱스 거리가 곧 거래일 거리다(모듈
# docstring). 거시(`_macro_map`)는 인덱스가 아니라 아래 `_PERIOD_CALENDAR_DAYS`
# (달력일 목표)를 쓴다 — 이유는 그 상수 옆 주석 참조.
_PERIOD_LOOKBACK = {"weekly_review": 5, "monthly": 21, "quarterly": 63, "annual": 252}
_PERIOD_LABEL = {"weekly_review": "1주", "monthly": "1개월", "quarterly": "1분기", "annual": "1년"}

# 거시지표는 매일 관측되지 않는다(CPI는 월 1회). "5거래일 전"을 그대로 인덱스로
# 적용하면 CPI에서는 다섯 번째 이전 발표(약 5개월 전)를 가리켜 버려 "1주 전"이라는
# 이름표가 거짓말이 된다. 그래서 거시는 달력일 목표(1주=7일 등)로 바꿔 그 날짜에
# 가장 가까운(그러나 넘지 않는) 관측을 찾는다 — 그 결과 월간 지표는 주간 리뷰에서
# 자연히 "직전 관측"(기존과 동일)으로 떨어지고, 월간 리뷰에서는 자연히 "그 달의
# 직전 발표"가 된다. `_PERIOD_TOLERANCE`는 그 관측이 실제로 그 주기를 대표할 만큼
# 가까운지 판단한다 — 아니면 §2 규칙1대로 실제 날짜·간격을 그대로 보인다.
_PERIOD_CALENDAR_DAYS = {"weekly_review": 7, "monthly": 30, "quarterly": 91, "annual": 365}
# [ASSUMPTION] CEO가 정확한 배수를 정하지 않았다 — 발표 주기(월간지표 약 30일)가
# 목표 안에 넉넉히 들어오도록 잡은 보수적인 선이다.
_PERIOD_TOLERANCE = 1.5

# spec §1② 변동성 "평소 대비 몇 배". 예시가 전부 KOSPI라 대표 지표 하나만
# 보인다(§1②: "어디에 몇 개를 보일지는 네 판단" — 모든 행에 붙이지 않는다).
# [ASSUMPTION] 다른 지표로 넓히는 결정은 CEO 확인 없이 내리지 않는다.
_VOLATILITY_SUBJECT = "^KS11"
# §2 규칙2: 기간 관측 3개 미만이거나 기준 표본이 60거래일 미만이면 변동성을
# 아예 말하지 않는다(기존 `_KR_BREADTH_MIN_HISTORY_DAYS`와 같은 태도).
_VOL_MIN_PERIOD_OBS = 3
_VOL_MIN_BASELINE_DAYS = 60
# n<=2인 축은 '업종'이 아니라 개별 종목이다(§12 기준 금융·신용 2, 소비·수출 2,
# 헬스케어 1). 대표값을 지우지는 않되 표본이 적다는 사실을 함께 싣는다.
_SMALL_SAMPLE_MAX = 2

_MACRO_LABELS = {
    "CPIAUCSL": "미국 CPI", "PCEPI": "미국 PCE", "UNRATE": "미국 실업률",
    "PAYEMS": "미국 비농업고용", "FEDFUNDS": "연방기금금리(실효)", "DFEDTARU": "연방기금금리 상단(목표)",
    "DGS10": "미국채 10년물 금리", "DGS2": "미국채 2년물 금리", "T10Y2Y": "미 10Y-2Y 금리차",
    "RSAFS": "미국 소매판매", "INDPRO": "미국 산업생산지수",
    # 2026-08-12 추가(providers/fred.py의 같은 날 주석 참조). 이름은 **무엇을
    # 재는지**로 짓는다 — `DFII10`을 "미국채 10년물 실질금리"라 쓰면 명목
    # 10년물과 화면에서 헷갈린다. 둘은 매일 나란히 실리고 오늘도 4.72 vs 2.43으로
    # 1.5%p 넘게 벌어져 있다.
    "DFII10": "미 10년물 실질금리(TIPS)", "T10YIE": "미 10년 기대인플레",
    "BAMLH0A0HYM2": "미 하이일드 신용스프레드", "DTWEXBGS": "달러지수(광의)",
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
    "차단선 이전에 알려진 한국 수급(개인·외국인·기관 순매수) fact가 0건이다. 수급은 "
    "한국투자증권 KIS가 주는데(KRX는 익명 경로를 막았고 오픈API에 수급 엔드포인트가 없다 — "
    "2026-08-03 실측), 그 수집이 실패했거나 차단선보다 늦게 돌았을 수 있다. 수급 fact가 "
    "하나라도 들어오면 이 리포트 빌더는 코드 수정 없이 수급 섹션을 채운다."
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

def _with_asof(base: str, row) -> str:
    """불완전·미확인 값에는 **언제 기준인지**를 붙인다. 이 꼬리표가 없으면
    몇 년 전 값이 오늘 값처럼 읽힌다(`test_data_status_surfaced`가 지킨다).
    조/억으로 줄여 쓰는 자리에서도 같은 꼬리표가 따라와야 하므로 서식과
    분리해 둔다."""
    if row["data_status"] in ("partial", "unverified") and row["event_at"]:
        return f"{base} (기준일 {row['event_at'][:10]})"
    return base


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
    return _with_asof(base, row)


def _row_from_fact(
    row, label: str, comparison: str, delta_pct: float | None = None,
    value: str | None = None, doc_url: str = "", group: str = "",
    delta_unit: str = "%",
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
        delta_pct=delta_pct, doc_url=doc_url, group=group, delta_unit=delta_unit,
    )


# --- prices / market reaction -------------------------------------------

# 값 자체가 이미 퍼센트인 지표의 단위. 이런 값의 변화는 **퍼센트포인트**로
# 말해야 한다 — 자세한 이유는 `model.FactRow.delta_unit` 주석 참조.
# 문자열 집합으로 가르는 이유: 어느 소스가 어떤 단위 표기를 쓰는지는 provider가
# 정하고(FRED `%`, ECOS `연%`, universe `percent`), 여기서는 그 표기를 그대로
# 받아 판단만 한다. 새 단위 표기가 생기면 이 집합에 없어서 예전처럼 상대
# 변화율로 나온다 — 조용히 틀리지 않게 `tests/reporting/test_macro_percentage_point.py`가
# DB와 universe에 실제로 들어 있는 단위 목록을 훑어 빠진 것이 없는지 본다.
_PERCENT_VALUED_UNITS = frozenset({"%", "연%", "percent", "Percent"})


def _price_map(conn, cutoff, lookback: int = 1) -> dict[str, dict]:
    """subject -> {latest, prev, delta_pct, series, hist} using every
    price_close fact known as of `cutoff` (spec A5-compliant: one
    `facts_as_of` call, no per-symbol filter, grouped in Python — one
    fact_id exists per trading day per symbol, so a subject filter alone
    would not collapse to "today's close").

    `lookback` (spec 20260810-period-report §1①) is how many trading days
    back `prev` should be — 1 (기본값) for the daily-cadence report types,
    5/21/63/252 for weekly/monthly/quarterly/annual. Since one row = one
    trading day, this is a plain index offset into `rs`. When history is
    shorter than `lookback` (early collection days), `prev` falls back to
    the oldest observation on hand and `exact_lookback` is False so the
    caller can honestly disclose the real gap instead of the period name
    (spec §2 규칙1 — "5거래일 전 관측이 없으면 있는 것 중 가장 가까운 것을
    쓰되 실제 날짜·간격을 밝힌다").

    `series` (추이 그래프의 입력) and `hist`(변동성 계산의 입력, spec §1②)
    are both built from **these same rows**, i.e. from the one
    `facts_as_of(cutoff)` read, so both inherit the report's information
    barrier instead of needing a second PIT rule of their own. A close known
    after the blackout is not in `rows` and therefore cannot be in either.
    `hist` is never trimmed (unlike `series`) and never leaves this module —
    it is not a `Report`/`FactRow` field, only an input to
    `_volatility_ratio`, so it does not bloat the JSON the way a full
    history field would."""
    rows = db_mod.facts_as_of(conn, cutoff, metric="price_close")
    by_subject: dict[str, list] = {}
    for r in rows:
        by_subject.setdefault(r["subject"], []).append(r)
    out: dict[str, dict] = {}
    for subj, rs in by_subject.items():
        rs.sort(key=lambda r: r["event_at"] or "", reverse=True)
        latest = rs[0]
        exact_lookback = len(rs) > lookback
        if exact_lookback:
            prev = rs[lookback]
        elif len(rs) > 1:
            prev = rs[-1]
        else:
            prev = None
        delta = None
        if prev is not None and prev["value_num"] not in (None, 0) and latest["value_num"] is not None:
            delta = (latest["value_num"] - prev["value_num"]) / prev["value_num"] * 100
        series = [r["value_num"] for r in reversed(rs[:SPARKLINE_POINTS])
                  if r["value_num"] is not None]
        hist = [r["value_num"] for r in reversed(rs) if r["value_num"] is not None]
        # `^TNX`(미 10년물)처럼 **시세 자체가 퍼센트**인 종목은 변화를 퍼센트
        # 포인트로 말해야 한다 — 아래 `_price_delta`가 이 값을 골라 쓴다.
        # 여기서 미리 계산해 두는 이유는 `prev`가 0일 때 상대 변화율만 None이
        # 되고 절대 변화는 멀쩡한 경우가 있기 때문이다(금리는 0을 지날 수 있다).
        delta_abs = None
        if prev is not None and prev["value_num"] is not None and latest["value_num"] is not None:
            delta_abs = latest["value_num"] - prev["value_num"]
        # **월간 국면 규칙(§6.3)이 쓰는 값은 표시 창과 무관하게 "직전 관측"이다.**
        # 표시 창(21거래일 등)을 그대로 국면 판정에 흘리면, 화면만 바꾸려던 변경이
        # 판정 자체를 조용히 바꾼다 — 이 파일 상단 주석이 "synthetic 30-day
        # lookback이 아니다"라고 못박은 규칙이다. 실측(심사 2026-08-10):
        # `dxy_delta_pct`가 -0.21 -> -1.57로 바뀌어 문턱(-1.0)을 새로 넘었다.
        prev_immediate = rs[1] if len(rs) > 1 else None
        delta_immediate = None
        if (prev_immediate is not None and prev_immediate["value_num"] not in (None, 0)
                and latest["value_num"] is not None):
            delta_immediate = ((latest["value_num"] - prev_immediate["value_num"])
                               / prev_immediate["value_num"] * 100)
        # `hist`와 짝이 되는 날짜. 리베이스 차트는 여러 종목을 **같은 x축**에
        # 세워야 하는데(그래야 "같은 날 A는 오르고 B는 내렸다"가 보인다), 값만
        # 있으면 종목마다 거래일 수가 달라 축을 맞출 수 없다. 한국·미국 시장은
        # 휴장일이 달라 실제로 어긋난다.
        hist_dates = [(r["event_at"] or "")[:10] for r in reversed(rs)
                      if r["value_num"] is not None]
        out[subj] = {"latest": latest, "prev": prev, "delta_pct": delta,
                     "delta_pct_immediate": delta_immediate,
                     "delta_abs": delta_abs, "series": series, "hist": hist,
                     "hist_dates": hist_dates,
                     "exact_lookback": exact_lookback}
    return out


def _daily_pct_returns(closes_asc: list[float]) -> list[float]:
    """오래된 -> 최신 종가 목록에서 일간 등락률(%) 목록. 0으로 나누지 않는다."""
    return [(b - a) / a * 100 for a, b in zip(closes_asc, closes_asc[1:]) if a]


def _volatility_ratio(closes_asc: list[float], window: int) -> float | None:
    """spec §1② "그 기간 일간 등락률의 표준편차 ÷ 가용 전체 기간 일간 등락률의
    표준편차". 표본이 모자라면(§2 규칙2) None — 호출부는 그 경우 문구를 아예
    뺀다. `window`는 `_PERIOD_LOOKBACK`과 같은 값이므로, 일간 리포트 타입
    (lookback=1)은 기간 관측이 1개뿐이라 항상 None이 된다 — 별도 분기 없이
    "일간 리포트에는 변동성 문구가 없다"가 저절로 성립한다."""
    returns = _daily_pct_returns(closes_asc)
    if len(returns) < _VOL_MIN_BASELINE_DAYS:
        return None
    period = returns[-window:] if window > 0 else []
    if len(period) < _VOL_MIN_PERIOD_OBS:
        return None
    baseline_sd = statistics.stdev(returns)
    if not baseline_sd:
        return None
    return statistics.stdev(period) / baseline_sd


def _price_delta(subject: str, info: dict) -> tuple[float | None, str]:
    """-> (화면에 찍을 등락값, 그 단위). 시세가 퍼센트인 종목(`unit=percent`,
    지금은 `^TNX` 하나)은 퍼센트포인트다.

    이걸 안 나누면 같은 미국 10년물 금리를 리포트가 두 군데서 다르게 말한다 —
    거시 카드(FRED `DGS10`)는 `+0.07%p`, 머리줄(yfinance `^TNX`)은 `+1.8%`.
    같은 사실이 두 숫자로 보이면 어느 쪽이 맞는지 독자가 알 수 없다."""
    meta = _UNIVERSE_BY_SYMBOL.get(subject) or {}
    if (meta.get("unit") or "") in _PERCENT_VALUED_UNITS:
        return info.get("delta_abs"), "%p"
    return info.get("delta_pct"), "%"


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


def _comparison_text(info: dict, subject: str = "", report_type: str = "") -> str:
    """"전일대비 +1.2%" 같은 비교 문구. 리포트 타입에 주기 라벨이 있고
    (spec §1① `_PERIOD_LABEL`) `_price_map`이 그 거래일 수만큼 실제로 뒤로
    갈 수 있었으면("exact_lookback") "1주 전 대비"/"1분기 전 대비"처럼 그
    주기로만 말한다("그 주기 + 전일" 병기 금지, CEO 확정) — 정확히 spec §0의
    참고 예시 문구("1주 전 대비 -5.10%")와 같은 형태다.

    "전일대비" is only true when the two observations really are adjacent
    sessions. After a holiday, a collection outage, or a backfill the
    previous close can be many days back, and calling that "전일" states a
    fact the data does not support (final-review.md F4). Name the actual
    gap instead; one trading day keeps the familiar wording. 주기 라벨을 쓸
    수 없을 때(데이터가 그만큼 없을 때)도 이 규칙 그대로 떨어진다 —
    §2 규칙1 "있는 것 중 가장 가까운 것을 쓰되 실제 날짜·간격을 밝힌다".

    `subject`를 받는 이유는 단위 때문이다 — 시세가 퍼센트인 종목(`^TNX`)은
    `%`가 아니라 `%p`로 말한다(`_price_delta` 주석)."""
    delta, unit = _price_delta(subject, info)
    if delta is None:
        return "전일 비교 불가(직전 종가 없음)"
    period_label = _PERIOD_LABEL.get(report_type)
    if period_label and info.get("exact_lookback"):
        return f"{period_label} 전 대비 {delta:+.2f}{unit}"
    gap = _session_gap_days(info["latest"], info.get("prev"))
    if gap is None:
        return f"직전 종가 대비 {delta:+.2f}{unit}"
    if gap <= 1:
        return f"전일대비 {delta:+.2f}{unit}"
    return f"{gap}일 전 종가 대비 {delta:+.2f}{unit}"


def _market_reaction_row(subj: str, info: dict, report_type: str = "") -> FactRow:
    row = info["latest"]
    meta = _UNIVERSE_BY_SYMBOL.get(subj, {})
    label = meta.get("name_ko") or meta.get("name", subj)
    comparison = _comparison_text(info, subj, report_type)
    delta, delta_unit = _price_delta(subj, info)
    return FactRow(
        label=label, value=_fmt_price_value(row), comparison=comparison,
        source_url=row["safe_source_url"] or "", data_status=row["data_status"] or "unverified",
        known_at=row["known_at"], subject=subj, metric=row["metric"], raw_value=row["value_num"],
        delta_pct=delta, series=list(info.get("series") or []), delta_unit=delta_unit,
    )


def _market_reaction(price_map: dict, report_type: str = "") -> list[FactRow]:
    rows = [_market_reaction_row(s, price_map[s], report_type)
            for s in _MARKET_REACTION_SYMBOLS if s in price_map]
    for s in CORE16_SYMBOLS:
        info = price_map.get(s)
        if info and info["delta_pct"] is not None and abs(info["delta_pct"]) >= _CORE16_MOVE_THRESHOLD:
            rows.append(_market_reaction_row(s, info, report_type))
    return rows


def _close_delta_rows(price_map: dict, report_type: str = "close_delta") -> list[FactRow]:
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
    return [_market_reaction_row(s, i, report_type) for s, i in selected]


def _headline(price_map: dict, vol_ratio: float | None = None) -> str:
    """spec B6 fixed template. A missing piece becomes the literal word
    '미확인' in place of its value+% fragment (never omitted, never blank —
    [ASSUMPTION]: the label prefix is kept so the reader still knows which
    indicator is missing, e.g. 'KOSPI 미확인').

    `vol_ratio`(spec 20260810-period-report §1②)가 있으면 한 줄 끝에 이어
    붙인다 — 모든 행이 아니라 요약 한 곳에만 보이라는 CEO 확정("모든 행에
    붙이지 마라")을 지키는 자리다."""
    def part(symbol: str, prefix: str, fmt) -> str:
        info = price_map.get(symbol)
        if not info or info["latest"]["value_num"] is None:
            return f"{prefix} 미확인"
        # 금리(^TNX)는 %p다 — 안 그러면 머리줄이 "미10Y 4.75%(+1.5%)"가 되어
        # 같은 금리를 거시 카드(`+0.07%p`)와 다르게 말한다(`_price_delta`).
        pct, unit = _price_delta(symbol, info)
        pct_str = f"{pct:+.1f}{unit}" if pct is not None else "미확인"
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
    # "Core16"이라고 박아 두면 관측군이 늘 때마다 화면이 거짓말을 한다(실제로
    # 2026-08-03에 18개가 됐다). 개수를 세어 쓴다 — 다음에 또 늘어도 맞는다.
    line = (f"{kospi} · {spx} · {krw} · {us10y} — 관측기업 {len(CORE16_SYMBOLS)}곳 중 "
            f"±3% 이상 {n_movers}종목, 결측 {n_missing}건")
    if vol_ratio is not None:
        line += f" · 흔들림 평소의 {vol_ratio:.1f}배"
    return line


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


def _sector_index_rows(price_map: dict, report_type: str = "") -> list[SectorIndexRow]:
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
            comparison=_comparison_text(info, symbol, report_type),
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
        return "관측기업 등락 관측 없음 — 시장 폭을 계산할 종가가 차단선 이전에 없습니다."
    up = sum(s.up for s in summaries)
    parts = [
        f"{s.sector} {s.up}/{s.total}" if s.total else f"{s.sector} 관측 없음"
        for s in summaries
    ]
    return f"관측기업 {up}/{observed}개 상승 · " + " · ".join(parts)


# --- 한국 시장 폭 (서브태스크 B, spec §1·§2) --------------------------------
#
# `_breadth_line`(위)은 관측기업(Core 16) 등락만 본다 — CEO 지적(2026-08-04):
# 그것은 우리 관측군이지 "한국 시장"이 아니다. 여기서는 `providers/krx_breadth.py`
# 가 채운 코스피·코스닥 **전종목** 집계(§0 목적)를 한 줄씩 덧붙인다.
#
# 정직성 규칙(§2, 재량 없음)을 코드로:
#   1. `index_change_pct`는 전종목 시총 역산 근사치이므로 항상 "(근사)"를 붙인다
#      (절대 "지수"라고만 쓰지 않는다).
#   2. 지수 방향과 상승/하락 종목 수 다수 방향이 실제로 어긋날 때만 "…인데"로
#      잇는다(`_kr_breadth_market_line`의 `contrarian` 분기). 같은 방향이면
#      "·"로 나열만 한다.
#   3. 2년 백분위 문맥은 표본이 모자라면 아예 쓰지 않는다
#      (`_KR_BREADTH_MIN_HISTORY_DAYS`, 근거는 그 상수 옆 주석).
#   4. 값은 전부 `db.facts_as_of(cutoff)` 한 번의 결과에서만 나온다 — 별도
#      SELECT가 없으므로 차단선 이후 관측이 섞일 수 없다.

_KR_BREADTH_LABELS = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}

# 백분위를 낼 때 요구하는 최소 과거 관측(오늘 제외) 일수. 60거래일(약 3개월)
# 미만이면 하루 추가될 때마다 백분위가 크게 흔들려("이번 주 안에서 최고"류의
# 착시) "2년 중 X%"라는 문장이 실제로 갖지 않은 정밀도를 주장하게 된다.
# [ASSUMPTION] 정확한 임계값은 CEO가 정하지 않았다 — 분기(60거래일)를 문맥이
# 안정되기 시작하는 최소선으로 잡았다. 표본이 60일 미만이면 percentile 절
# 전체를 생략한다(§2-3).
_KR_BREADTH_MIN_HISTORY_DAYS = 60


def _kr_breadth_history(conn, cutoff, subject: str) -> dict[str, dict[str, float]]:
    """subject(KOSPI/KOSDAQ)의 날짜별 {metric: value} — **`facts_as_of(cutoff)`
    한 번의 결과만** 쓴다(§2 규칙4). 날짜별로만 묶는다, 그 이상 가공하지 않는다."""
    rows = db_mod.facts_as_of(conn, cutoff, category="breadth", subject=subject)
    by_date: dict[str, dict[str, float]] = {}
    for r in rows:
        day = (r["event_at"] or "")[:10]
        if not day or r["value_num"] is None:
            continue
        by_date.setdefault(day, {})[r["metric"]] = r["value_num"]
    return by_date


def _kr_up_ratio(day_metrics: dict[str, float]) -> float | None:
    adv, dec = day_metrics.get("breadth_advancers"), day_metrics.get("breadth_decliners")
    if adv is None or dec is None or adv + dec <= 0:
        return None
    return adv / (adv + dec) * 100


def _kr_breadth_span_label(dates: list[str]) -> str:
    """과거 표본이 실제로 걸쳐 있는 기간을 사람이 읽는 말로("2년"/"5개월").
    이 근거로 현재 완료된 2년 백필(spec 헤더: 483거래일)에서는 자연히 "2년"이
    나오고, 데이터가 아직 짧을 때는 그 실제 기간을 말한다 — "2년"을 하드코딩해
    실제보다 긴 문맥을 주장하지 않는다."""
    span_days = (date.fromisoformat(max(dates)) - date.fromisoformat(min(dates))).days
    years = span_days / 365.25
    if years >= 1.5:
        return f"{round(years)}년"
    return f"{max(1, round(span_days / 30))}개월"


def _kr_breadth_rank(by_date: dict[str, dict], today: str, today_ratio: float) -> tuple[float, str] | None:
    """오늘을 뺀 과거 표본에서 오늘 상승비율의 순위(0~100 백분위)와 표본이
    걸친 기간 표기. 표본이 `_KR_BREADTH_MIN_HISTORY_DAYS` 미만이면
    None(§2-3 — 맥락 없으면 백분위를 아예 말하지 않는다).

    `_kr_breadth_context`(문구)와 `_unusual_day_market`(오늘 유별난 것 블록)
    양쪽이 이 계산을 공유한다 — 백분위 판단이 두 곳에 따로 생기면 어느 날
    "상위 2%"와 "중간"이 동시에 나올 수 있다."""
    hist_dates = [d for d in by_date if d != today]
    hist_ratios = {d: _kr_up_ratio(by_date[d]) for d in hist_dates}
    hist_ratios = {d: r for d, r in hist_ratios.items() if r is not None}
    if len(hist_ratios) < _KR_BREADTH_MIN_HISTORY_DAYS:
        return None
    vals = sorted(hist_ratios.values())
    rank = sum(1 for v in vals if v <= today_ratio) / len(vals) * 100
    span = _kr_breadth_span_label(list(hist_ratios.keys()))
    return rank, span


def _kr_breadth_context(by_date: dict[str, dict], today: str, today_ratio: float) -> str:
    """오늘을 뺀 과거 표본에서 오늘 상승비율의 위치. 표본이
    `_KR_BREADTH_MIN_HISTORY_DAYS` 미만이면 빈 문자열(§2-3 — 맥락 없으면
    그 절을 아예 쓰지 않는다)."""
    info = _kr_breadth_rank(by_date, today, today_ratio)
    if info is None:
        return ""
    rank, span = info
    if 25 <= rank <= 75:
        return f"{span} 중 중간"
    # 역대 최고/최저(rank 100/0)는 반올림하면 "상위 0%"가 되어 "아무도 없다"는
    # 말처럼 읽힌다 — 최소 1%로 바닥을 둔다.
    if rank > 75:
        return f"{span} 중 상위 {max(1, round(100 - rank))}%"
    return f"{span} 중 하위 {max(1, round(rank))}%"


def _kr_breadth_market_line(by_date: dict[str, dict], subject: str,
                            report_date: date) -> str | None:
    """`by_date`가 비어 있거나 오늘치에 등락 종목 수가 없으면 그 시장은 아예
    낸 줄이 없다(None) — 없는 관측을 지어내지 않는다.

    **가장 최근 관측이 리포트 날짜가 아니면 그 날짜를 이름표에 박는다.** 이게
    없으면 KRX 수집이 며칠 실패했을 때 2주 전 숫자가 날짜 표기 없이 오늘 줄에
    실린다(심사 실측: 7/20 자료만 있는 상태로 8/3 리포트를 만들었더니
    `코스피 +3.50%(근사) · 오른 종목 900 …`이 그대로 나왔다). 아침 리포트처럼
    **정상적으로** 전 거래일이 최신인 경우도 있으므로 줄을 빼지는 않는다 —
    빼면 매일 아침 한국 시장이 통째로 사라진다. 날짜를 밝히는 쪽이 맞다."""
    if not by_date:
        return None
    today = max(by_date)
    metrics = by_date[today]
    adv, dec = metrics.get("breadth_advancers"), metrics.get("breadth_decliners")
    if adv is None or dec is None:
        return None
    idx = metrics.get("index_change_pct")

    label = _KR_BREADTH_LABELS[subject]
    if today != report_date.isoformat():
        label += f"({date.fromisoformat(today):%-m/%-d} 기준)"
    # 규칙1: "지수"라고 쓰지 않는다 — 전종목 시총 역산 근사치임을 항상 드러낸다.
    idx_part = f"{idx:+.2f}%(근사)" if idx is not None else "지수 근사치 미확인"

    up_ratio = _kr_up_ratio(metrics)
    if up_ratio is None:
        tail = "상승비율 계산 불가(등락 종목 없음)"
    else:
        ctx = _kr_breadth_context(by_date, today, up_ratio)
        tail = f"상승비율 {up_ratio:.0f}%" + (f", {ctx}" if ctx else "")

    counts = f"오른 종목 {adv:,.0f} / 내린 종목 {dec:,.0f}"

    # 규칙2: 지수 방향과 다수 종목 방향이 실제로 어긋날 때만 역접.
    idx_sign = None if idx is None else (1 if idx > 0 else (-1 if idx < 0 else 0))
    maj_sign = 1 if adv > dec else (-1 if dec > adv else 0)
    contrarian = idx_sign not in (None, 0) and maj_sign != 0 and idx_sign != maj_sign

    if contrarian:
        return f"{label} {idx_part}인데 {counts} — {tail}"
    return f"{label} {idx_part} · {counts} — {tail}"


def _kr_breadth_lines(by_date_map: dict[str, dict], report_date: date) -> list[str]:
    """§1 목표 형태의 2·3번째 줄. 시장별로 독립 계산 — 한쪽 provider가
    실패해도 다른 쪽은 그대로 나온다.

    `by_date_map`은 호출부(`build_report`)가 `_kr_breadth_history(conn, cutoff,
    subject)`로 시장마다 한 번씩만 미리 읽어 온 것이다 — "오늘 유별난 것"
    블록(`_unusual_day_block`)도 같은 결과를 쓰므로, 여기서 다시 조회하면
    같은 cutoff를 두 번 읽는 낭비가 생긴다."""
    lines = []
    for subject in ("KOSPI", "KOSDAQ"):
        line = _kr_breadth_market_line(by_date_map[subject], subject, report_date)
        if line:
            lines.append(line)
    return lines


# --- "오늘 유별난 것" (spec 20260806-report-visual §1①) ---------------------
#
# CEO가 세 번째 같은 말을 했다: "시각화로 변화에 집중하게 해달라." 483거래일
# 중 손꼽는 전면 상승/하락장이 리포트 맨 아래 문장 한 줄에 묻혀 있던 문제
# (spec §0)를 고치는 자리 — 코스피/코스닥 상승비율의 2년 분포 속 오늘 위치를
# 맨 위로 끌어올린다. §2 정직성 규칙 5가지가 전부 여기서 코드가 된다.

# 상/하위 몇 %부터 "유별난 날"이라 부를지. CEO가 예로 든 상위 2%·상위 1%는
# 이 문턱 훨씬 안이다. 정확한 값은 CEO가 정하지 않았다 —
# `_KR_BREADTH_MIN_HISTORY_DAYS`처럼 "이 정도는 확실히 드문 날"이라 부를 수
# 있는 보수적인 선을 잡았다. [ASSUMPTION]
_UNUSUAL_DAY_RANK_THRESHOLD = 5
_UNUSUAL_DAY_TOP_N = 5  # spec §1①-4: 가장 크게 움직인 것 5개


def _unusual_day_market(by_date: dict[str, dict], subject: str, report_date: date) -> dict | None:
    """subject(KOSPI/KOSDAQ) 하루치 재료. 오늘(또는 가장 최근 관측일) 상승비율을
    계산할 수 없으면 None — §2-1 "없는 사건을 지어내지 않는다"가 여기서부터
    시작된다: 이 함수가 None을 내면 그 시장은 블록에 아예 나오지 않는다."""
    if not by_date:
        return None
    today = report_date.isoformat()
    # `_kr_breadth_market_line`과 같은 이유로 최신 관측일이 리포트 날짜와
    # 다를 수 있다(수집이 며칠 밀렸을 때) — 그 날짜를 기준으로 삼는다.
    latest_day = today if today in by_date else max(by_date)
    ratio = _kr_up_ratio(by_date[latest_day])
    if ratio is None:
        return None
    adv, dec = by_date[latest_day]["breadth_advancers"], by_date[latest_day]["breadth_decliners"]
    rank_info = _kr_breadth_rank(by_date, latest_day, ratio)
    is_extreme, percentile_text, rank = False, "", None
    if rank_info is not None:
        rank, span = rank_info
        if rank >= 100 - _UNUSUAL_DAY_RANK_THRESHOLD:
            is_extreme, percentile_text = True, f"{span} 중 상위 {max(1, round(100 - rank))}%"
        elif rank <= _UNUSUAL_DAY_RANK_THRESHOLD:
            is_extreme, percentile_text = True, f"{span} 중 하위 {max(1, round(rank))}%"
    # 추이 그래프의 입력 — 그래프 자체는 백분위 주장이 아니므로(선을 그릴 뿐
    # "상위 N%"라고 말하지 않는다) §2-2(표본 부족 시 백분위 생략) 밖이다.
    dated = sorted((d, _kr_up_ratio(m)) for d, m in by_date.items() if _kr_up_ratio(m) is not None)
    series = [v for _, v in dated]
    return {
        "label": _KR_BREADTH_LABELS[subject], "ratio": ratio, "adv": adv, "dec": dec,
        "rank": rank, "is_extreme": is_extreme, "percentile_text": percentile_text,
        "stale": latest_day != today, "as_of": latest_day, "series": series,
    }


def _unusual_day_what_line(m: dict) -> str:
    """"코스피 오른 종목 788 / 내린 종목 96 (상승비율 89%)" — 극단 여부와
    무관하게 항상 말할 수 있는 순수 사실(spec §1①-2). 날짜가 밀렸으면 그
    날짜를 밝힌다(`_kr_breadth_market_line`과 같은 이유).

    **"N종목 중 M개 상승"이라고 쓰지 않는다.** 상승비율의 분모는 거래된 종목
    중 오르거나 내린 것(adv+dec)이라, 코스피 전체 종목 수(943)와 다르다
    (보합 31 + 거래없음 28 제외). 어느 쪽 숫자를 분모로 적든 한쪽이 거짓말이
    된다 — 943을 쓰면 788/943=84%라 아래 줄의 89%와 어긋나고, 884를 쓰면
    "코스피가 884종목?"으로 읽힌다(심사 2026-08-06, 두 안이 각각 이 두 절벽에
    떨어졌다). 그래서 **분모를 만들지 않고 오른/내린 종목 수를 그대로** 쓴다."""
    label = m["label"]
    if m["stale"]:
        label += f"({date.fromisoformat(m['as_of']):%-m/%-d} 기준)"
    return f"{label} 오른 종목 {m['adv']:,.0f} / 내린 종목 {m['dec']:,.0f}"


def _unusual_day_headline(markets: list[dict]) -> tuple[bool, str]:
    """1줄(들)로 "얼마나 드문 날인가" + "무슨 일인가"를 답한다. 극단인
    시장이 하나도 없으면 **정직하게 그렇다고 말한다** — 표본이 없어서
    모르는 것과, 표본이 있는데 안 유별난 것은 다른 문장이어야 한다(§2-1·
    §2-2가 요구하는 구별)."""
    if not markets:
        return False, ""
    what = " · ".join(_unusual_day_what_line(m) for m in markets)
    extreme = [m for m in markets if m["is_extreme"]]
    if extreme:
        how = " · ".join(
            f"{m['label']} 상승비율 {m['ratio']:.0f}% — {m['percentile_text']}" for m in extreme)
        return True, f"{how}\n{what}"
    known = [m for m in markets if m["rank"] is not None]
    if known:
        return False, f"오늘은 유별난 날이 아닙니다.\n{what}"
    return False, f"표본이 부족해 오늘이 얼마나 드문 날인지는 아직 말할 수 없습니다.\n{what}"


def _unusual_day_primary(markets: list[dict]) -> dict | None:
    """추이 그래프에 쓸 시장 하나를 고른다. 백분위를 아는 시장 중 중앙(50%)
    에서 가장 먼 쪽 — 그게 "오늘 유별난 것"의 주인공이다. 아무 시장도 표본이
    충분치 않으면 그냥 표본이 더 긴 쪽을 보여준다(그래프는 백분위 주장이
    아니므로 §2-2 제약 밖 — `_unusual_day_market` 주석 참조)."""
    if not markets:
        return None
    known = [m for m in markets if m["rank"] is not None]
    if known:
        return max(known, key=lambda m: abs(m["rank"] - 50))
    return max(markets, key=lambda m: len(m["series"]))


def _top_movers(price_map: dict, report_type: str = "", n: int = _UNUSUAL_DAY_TOP_N) -> list[FactRow]:
    """spec §1①-4: 오늘 가장 크게 움직인 n개(관측기업 + 지수·금리·환율·원자재).
    `_close_delta_rows`와 후보군은 같지만 KOSPI/USD 필수 포함 규칙은 없다 —
    close_delta의 고정 5종목 규칙이 아니라 순수 "무엇이 컸나"이기 때문이다."""
    candidates = [
        (s, price_map[s]) for s in (_MARKET_REACTION_SYMBOLS + CORE16_SYMBOLS)
        if s in price_map and price_map[s]["delta_pct"] is not None
    ]
    ranked = sorted(candidates, key=lambda t: -abs(t[1]["delta_pct"]))
    seen: set[str] = set()
    out: list[FactRow] = []
    for s, info in ranked:
        if s in seen:
            continue
        seen.add(s)
        out.append(_market_reaction_row(s, info, report_type))
        if len(out) >= n:
            break
    return out


# --- 차트 (CEO 지시 2026-08-12: "시각화가 충분하지 않다") --------------------
#
# 판단은 전부 여기서 끝낸다. 렌더러는 좌표만 계산한다(`ChartBlock` 주석 참조).
# 그림 종류 선택의 근거는 사외 고문 2인의 독립 자문(2026-08-12)이다 — 파이·도넛은
# 쓰지 않는다: 이 리포트의 핵심 수치인 순매수는 **부호가 있어** 조각으로 못 나누고,
# 하루치 구성만 보여 주는 그림은 매일 읽는 문서에서 날짜 비교가 안 된다.
CHART_DAYS = 20  # 한 달치 거래일. 더 늘리면 막대가 실처럼 가늘어져 못 읽는다.


def _chart_days(dates: list[str]) -> list[str]:
    return sorted(dates)[-CHART_DAYS:]


def _breadth_chart(by_date_map: dict[str, dict]) -> ChartBlock | None:
    """상승/하락 종목 수를 0축 위아래로, 지수 등락률을 점으로 겹친다.

    이 그림이 첫째인 이유: 지수와 종목 다수가 **갈라지는 날**을 다른 어떤 표도
    보여주지 못한다. 실측 2026-08-03 코스피는 지수 -5.1%인데 오른 종목 455 /
    내린 종목 419였다 — 대형주가 지수를 끌어내린 날이고, 숫자 표로는 그 구조가
    눈에 안 들어온다(CEO 지적 2026-08-03 "표로 보니 한눈에 안 들어온다").
    """
    hist = by_date_map.get("KOSPI") or {}
    dates = _chart_days([d for d, m in hist.items()
                         if m.get("breadth_advancers") is not None])
    if len(dates) < 2:
        return None
    adv = [hist[d].get("breadth_advancers") for d in dates]
    dec = [(-hist[d]["breadth_decliners"]) if hist[d].get("breadth_decliners") is not None
           else None for d in dates]
    idx = [hist[d].get("index_change_pct") for d in dates]

    # 설명 문장은 **그림이 없는 곳(마크다운·낭독기)에서 그림을 대신한다.**
    # 가장 최근에 "지수와 종목 다수가 어긋난 날"을 짚어 준다 — 그림에서 눈이
    # 먼저 가는 곳이 그 자리이기 때문이다.
    note = f"코스피 상승·하락 종목 수(막대)와 지수 등락률(점), 최근 {len(dates)}거래일."
    for d in reversed(dates):
        m = hist[d]
        a, b, chg = m.get("breadth_advancers"), m.get("breadth_decliners"), m.get("index_change_pct")
        if a is None or b is None or chg is None:
            continue
        if (chg < 0 and a > b) or (chg > 0 and b > a):
            note += (f" 가장 최근 어긋난 날은 {d[5:].replace('-', '/')}로, "
                     f"지수 {chg:+.1f}%인데 오른 종목 {a:,.0f} / 내린 종목 {b:,.0f}였다.")
            break
    return ChartBlock(
        kind="breadth", title="코스피 시장 폭과 지수", dates=dates, unit="종목",
        series=[ChartSeries(label="오른 종목", values=adv),
                ChartSeries(label="내린 종목", values=dec)],
        overlay=[ChartSeries(label="지수 등락률", values=idx)],
        note=note)


def _flows_chart(conn, cutoff) -> ChartBlock | None:
    """투자자 주체별 순매수(+)/순매도(-)를 0축 위아래로.

    표는 "오늘 누가 샀나"에 답하고, 이 그림은 "**며칠째** 그러고 있나"에 답한다.
    수급은 합이 0이라(한쪽이 사면 다른 쪽이 판다) 방향보다 **지속**이 정보다.
    """
    rows = db_mod.facts_as_of(conn, cutoff, category="flow")
    metrics = {"net_buy_foreign_value": "외국인",
               "net_buy_institution_value": "기관",
               "net_buy_individual_value": "개인"}
    by_date: dict[str, dict[str, float]] = {}
    for r in rows:
        metric = r["metric"] or ""
        if metric not in metrics or r["value_num"] is None:
            continue
        day = (r["event_at"] or "")[:10]
        if not day:
            continue
        # 종목별 값을 합산한다 — 이 그림이 답하는 것은 개별 종목이 아니라
        # 표본 전체에서 주체들이 어느 쪽에 서 있는가다.
        by_date.setdefault(day, {}).setdefault(metric, 0.0)
        by_date[day][metric] += r["value_num"]
    dates = _chart_days(list(by_date))
    if len(dates) < 2:
        return None
    series = [ChartSeries(label=label,
                          values=[(by_date[d].get(m, 0.0) / 1e12) for d in dates])
              for m, label in metrics.items()]
    totals = {s.label: sum(v for v in s.values if v is not None) for s in series}
    parts = " · ".join(f"{k} {v:+.1f}조" for k, v in totals.items())
    return ChartBlock(
        kind="flows", title="투자자별 순매수 (코스피 상위 20종목 표본)",
        dates=dates, unit="조 원", series=series,
        note=(f"양(+)이 순매수, 음(-)이 순매도. 최근 {len(dates)}거래일 합계 — {parts}. "
              "시장 전체가 아니라 표본이다."))


# 기준=100 꺾은선에 올릴 계열. 한국 메모리와 미국 반도체를 같은 축에 세우는 것이
# 요점이다 — 2026-08-11 기준 60거래일에서 SOX는 +0.2%인데 SK하이닉스는 -27.7%였다.
_REBASE_SUBJECTS = (("^KS11", "코스피"), ("^KQ11", "코스닥"),
                    ("^SOX", "미 반도체"), ("000660.KS", "SK하이닉스"))


def _rebased_chart(price_map: dict) -> ChartBlock | None:
    """여러 시장을 기준일=100으로 맞춰 겹쳐 그린다.

    지수는 단위가 제각각이라(코스피 6,358 · 코스닥 858 · SOX 12,098) 그대로
    겹치면 큰 숫자만 보인다. 100으로 맞추면 **같은 기간에 누가 얼마나 갔는지**가
    비로소 비교된다.

    날짜 축은 **한국·미국 공통 거래일의 교집합**이다. 휴장일이 서로 달라 한쪽
    기준으로 세우면 없는 날의 값을 앞 값으로 끌어다 쓰게 되고, 그건 관측하지
    않은 값을 그리는 것이다.
    """
    have = [(sym, label) for sym, label in _REBASE_SUBJECTS
            if (price_map.get(sym) or {}).get("hist_dates")]
    if len(have) < 2:
        return None
    common = set.intersection(*(set(price_map[s]["hist_dates"]) for s, _ in have))
    dates = _chart_days(sorted(common))
    if len(dates) < 2:
        return None

    series = []
    for sym, label in have:
        info = price_map[sym]
        by_day = dict(zip(info["hist_dates"], info["hist"]))
        base = by_day.get(dates[0])
        if not base:
            continue
        series.append(ChartSeries(label=label,
                                  values=[by_day[d] / base * 100 for d in dates]))
    if len(series) < 2:
        return None
    moves = " · ".join(f"{s.label} {s.values[-1] - 100:+.1f}%" for s in series)
    return ChartBlock(
        kind="rebased", title=f"{dates[0]} = 100 기준 상대 추이",
        dates=dates, unit="=100", series=series,
        note=(f"같은 기간 상대 성적 — {moves}. "
              f"관측 {len(dates)}거래일로는 추세가 아니라 이 구간의 기록이다."))


def _charts(conn, cutoff, by_date_map: dict[str, dict], price_map: dict) -> list[ChartBlock]:
    """그릴 수 있는 것만 싣는다. 데이터가 모자라면 그 그림은 아예 없다 —
    빈 축이나 점 하나짜리 그림은 정보가 아니라 잡음이다(`sparkline_svg`와 같은 관례)."""
    blocks = [_breadth_chart(by_date_map), _flows_chart(conn, cutoff), _rebased_chart(price_map)]
    return [b for b in blocks if b is not None]


def _unusual_day_block(by_date_map: dict[str, dict], report_date: date, price_map: dict,
                       report_type: str = "") -> UnusualDayBlock:
    markets = [_unusual_day_market(by_date_map.get(s, {}), s, report_date)
               for s in ("KOSPI", "KOSDAQ")]
    markets = [m for m in markets if m is not None]
    is_notable, headline = _unusual_day_headline(markets)
    primary = _unusual_day_primary(markets)
    return UnusualDayBlock(
        is_notable=is_notable,
        headline=headline,
        trend_label=f"{primary['label']} 상승비율" if primary else "",
        trend_series=primary["series"] if primary else [],
        top_movers=_top_movers(price_map, report_type),
    )


# --- macro ----------------------------------------------------------------

def _event_date(row) -> date | None:
    try:
        return date.fromisoformat((row["event_at"] or "")[:10])
    except (ValueError, TypeError, IndexError, KeyError):
        return None


def _macro_map(conn, cutoff, report_type: str = "") -> dict[str, dict]:
    """spec 20260810-period-report §1① — `_price_map`과 같은 결함("_macro_map도
    같다", §0)을 고친다. 거시는 인덱스가 아니라 **달력일 목표**로 "그 주기에
    맞는" 관측을 찾는다(`_PERIOD_CALENDAR_DAYS` 주석 참조) — 발표 주기가
    지표마다 달라서 가격처럼 "N번째 이전 관측"을 그대로 쓰면 월간 지표에서
    "1주 전"이 실제로는 5개월 전을 가리키는 거짓말이 된다.

    `period_match`는 그렇게 찾은 관측이 실제로 그 주기를 대표할 만큼 목표에
    가까운지(`_PERIOD_TOLERANCE`)를 담아 두어, 호출부(`_macro_comparison_text`)가
    라벨을 쓸지 실제 날짜·간격을 밝힐지(§2 규칙1)를 다시 계산하지 않고
    고른다."""
    calendar_days = _PERIOD_CALENDAR_DAYS.get(report_type)
    rows = db_mod.facts_as_of(conn, cutoff, category="macro", metric="value")
    by_subject: dict[str, list] = {}
    for r in rows:
        by_subject.setdefault(r["subject"], []).append(r)
    out: dict[str, dict] = {}
    for subj, rs in by_subject.items():
        rs.sort(key=lambda r: r["event_at"] or "", reverse=True)
        latest = rs[0]
        prev = rs[1] if len(rs) > 1 else None
        period_match = False
        if calendar_days and len(rs) > 1:
            latest_date = _event_date(latest)
            target = (latest_date - timedelta(days=calendar_days)) if latest_date else None
            candidate = None
            if target is not None:
                candidate = next(
                    (r for r in rs[1:] if (d := _event_date(r)) is not None and d <= target), None)
            if candidate is not None:
                prev = candidate
                gap = (latest_date - _event_date(candidate)).days
                period_match = gap <= calendar_days * _PERIOD_TOLERANCE
            else:
                # §2 규칙1: 목표만큼 뒤로 갈 관측이 없으면 있는 것 중 가장
                # 가까운(=가장 오래된) 것을 쓰고, 라벨 대신 실제 간격을 밝힌다.
                prev = rs[-1]
                period_match = False
        delta_abs = delta_pct = None
        if prev is not None and latest["value_num"] is not None and prev["value_num"] is not None:
            delta_abs = latest["value_num"] - prev["value_num"]
            if prev["value_num"] != 0:
                delta_pct = delta_abs / prev["value_num"] * 100
        # 가격 쪽과 같은 이유로 직전 관측 대비 값을 따로 보존한다(§6.3 국면 규칙).
        prev_immediate = rs[1] if len(rs) > 1 else None
        delta_abs_immediate = delta_pct_immediate = None
        if prev_immediate is not None and latest["value_num"] is not None \
                and prev_immediate["value_num"] is not None:
            delta_abs_immediate = latest["value_num"] - prev_immediate["value_num"]
            if prev_immediate["value_num"] != 0:
                delta_pct_immediate = delta_abs_immediate / prev_immediate["value_num"] * 100
        out[subj] = {"latest": latest, "prev": prev, "delta_abs": delta_abs, "delta_pct": delta_pct,
                     "delta_abs_immediate": delta_abs_immediate,
                     "delta_pct_immediate": delta_pct_immediate,
                     "period_match": period_match}
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


def _macro_comparison_text(info: dict, is_pp: bool, report_type: str) -> str:
    """일간 리포트 타입(주기 라벨 없음)은 기존 문구를 **그대로** 유지한다
    (`test_macro_percentage_point.py`가 "직전 관측 대비"를 무조건 기대한다 —
    거시는 원래도 "전일" 개념이 없어 그 문구에 날짜 간격을 붙인 적이 없다).
    주기 라벨이 있는 리포트(weekly_review 등)에서만 §1① 규칙을 적용한다."""
    delta = info["delta_abs"] if is_pp else info["delta_pct"]
    unit = "%p" if is_pp else "%"
    if delta is None:
        return "직전 관측 없음"
    period_label = _PERIOD_LABEL.get(report_type)
    if not period_label:
        return f"직전 관측 대비 {delta:+.2f}{unit}"
    if info.get("period_match"):
        return f"{period_label} 전 관측 대비 {delta:+.2f}{unit}"
    gap = _session_gap_days(info["latest"], info.get("prev"))
    if gap is None:
        return f"직전 관측 대비 {delta:+.2f}{unit}"
    return f"{gap}일 전 관측 대비 {delta:+.2f}{unit}"


def _macro_facts(mmap: dict, report_type: str = "") -> list[FactRow]:
    out = []
    for subj, info in mmap.items():
        label = _macro_label(subj, info["latest"])
        # 금리·실업률처럼 값이 이미 퍼센트면 변화는 퍼센트포인트(%p)로 말한다.
        # 상대 변화율을 쓰면 "한국 기준금리 +10.00%"(실제 2.50->2.75)처럼
        # 읽는 사람이 금리 수준을 오해한다(CEO 지적 2026-08-05).
        is_pp = (info["latest"]["unit"] or "") in _PERCENT_VALUED_UNITS
        delta = info["delta_abs"] if is_pp else info["delta_pct"]
        unit = "%p" if is_pp else "%"
        comparison = _macro_comparison_text(info, is_pp, report_type)
        # 거시지표는 관측이 1개씩이라 시계열 그래프는 나오지 않는다(series 없음).
        # 방향(화살표·색)은 직전 관측이 있을 때만 붙는다.
        out.append(_row_from_fact(info["latest"], label, comparison,
                                  delta_pct=delta, group="macro", delta_unit=unit))
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
    by_key: dict[tuple[str, str], list] = {}
    for row in rows:
        if subjects is not None and row["subject"] not in subjects:
            continue
        by_key.setdefault((row["subject"], row["metric"]), []).append(row)

    out = []
    for (subject, metric), group in by_key.items():
        # 백필 표시 가드(spec 백필 §5 ST3 항목 5): (subject,metric)당 최근
        # period_end 2개만 표에 올린다. 백필 후 재무 fact가 수백 개가 되어
        # 분기 리포트를 읽을 수 없게 되기 때문이다(기준선 quarterly
        # facts_rows=95). §7.3 질문이 전부 전분기 대비라 2개면 답이 된다.
        # 8분기 추이를 버리는 게 아니다 — DB에 그대로 있고 가설 판정과 상세
        # 페이지가 쓴다. 표는 읽히는 것이 목적이다.
        # (수급 표에 적용한 것과 같은 규약 — `_kr_flows` 참조.)
        group.sort(key=lambda r: r["event_at"] or "", reverse=True)
        for row in group[:2]:
            label = f"{_subject_name(subject)} {_FINANCIALS_LABELS.get(metric, metric)}"
            basis = _COMPARISON_BASIS_KO.get(row["comparison_basis"], row["comparison_basis"] or "")
            # 재무도 자릿수가 12~15개다(삼성전자 매출 333,605,938,000,000).
            # 상세 페이지가 이미 `333.6조 원`으로 쓰므로 같은 함수를 쓴다 —
            # 두 화면이 같은 금액을 다른 자릿수로 쓰면 대조가 안 된다.
            out.append(_row_from_fact(row, label, basis, group="financials",
                                      value=_with_asof(
                                          fmt_money(row["value_num"], row["unit"] or ""), row)))
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
            value=_filing_value(row, ref), doc_url=_filing_doc_url(row), group="filing",
        ))
    out.sort(key=lambda r: r.known_at, reverse=True)
    return out


def _consensus_facts(conn, cutoff, subject: str) -> list[FactRow]:
    rows = db_mod.facts_as_of(conn, cutoff, category="calendar", subject=subject)
    out = []
    for row in rows:
        if row["metric"] == "consensus_eps":
            out.append(_row_from_fact(row, "컨센서스 EPS", "", group="consensus"))
        elif row["metric"] == "consensus_revenue":
            out.append(_row_from_fact(row, "컨센서스 매출", "", group="consensus"))
    return out


def _register_gap(conn, gap_id: str, subject: str, metric: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO data_gaps(gap_id, subject, metric, detected_at, reason, status) "
        "VALUES (?,?,?,?,?,?)",
        (gap_id, subject, metric, db_mod.iso_utc(), reason, "제안"),
    )
    conn.commit()


# 투자자별 수급 항목명. 수량 라벨이 남아 있는 것은 DB와 상세 페이지가 여전히
# 두 단위를 다 쓰기 때문이다 — 리포트 표에는 금액만 실린다(`_kr_flows` 참조).
_FLOW_LABELS = {
    "net_buy_foreign": "외국인 순매수(주)",
    "net_buy_institution": "기관 순매수(주)",
    "net_buy_individual": "개인 순매수(주)",
    "net_buy_foreign_value": "외국인 순매수(금액)",
    "net_buy_institution_value": "기관 순매수(금액)",
    "net_buy_individual_value": "개인 순매수(금액)",
}


_KR_CORE_SET = set(KR_CORE_SYMBOLS)
_FLOW_SAMPLE_SET = set(KOSPI_FLOW_SAMPLE_SYMBOLS)
_FLOW_SAMPLE_SUBJECT = "KOSPI_TOP20"
# 화면에 "시장"이라고 쓰지 않는다. 시장 전체 수급을 주는 경로가 없어서 상위
# 20종목 합으로 대신하는 것이고(`universe.KOSPI_FLOW_SAMPLE`), 시장이라고 쓰는
# 순간 나중에 "코스피 수급"으로 읽고 판단하게 된다.
_FLOW_SAMPLE_LABEL = f"코스피 상위 {len(KOSPI_FLOW_SAMPLE_SYMBOLS)}종목 합계(표본)"


def _folded_into_sample_total(subject: str) -> bool:
    """이 종목의 개별 줄을 빼도 되는가 — **합계 막대가 이미 보여주는가**.

    관측 기업(Core 16의 한국 상장분)은 표본에도 들어 있지만 개별로도 본다.
    표본에만 있는 종목이 접히는 대상이다."""
    return subject in _FLOW_SAMPLE_SET and subject not in _KR_CORE_SET


def _kr_flow_sample_total(latest: dict) -> list[FactRow]:
    """표본 종목의 순매수를 주체별로 더해 **막대 한 줄**을 만든다.

    개별 종목 20줄 대신 합계 1줄인 이유는 호출부 주석 참조. 같은 날짜의 값만
    더한다 — 종목마다 마지막 거래일이 다를 수 있는데(거래정지 등) 섞어 더하면
    "그날 시장"이 아니라 여러 날의 잡탕이 된다.
    """
    by_metric: dict[str, list] = {}
    for (subject, metric), row in latest.items():
        if subject not in KOSPI_FLOW_SAMPLE_SYMBOLS or not metric.endswith("_value"):
            continue
        by_metric.setdefault(metric, []).append(row)
    if not by_metric:
        return []

    # 가장 많은 종목이 공유하는 최근 날짜 하나로 못박는다.
    day = max((r["event_at"] or "")[:10]
              for rows in by_metric.values() for r in rows)

    out: list[FactRow] = []
    for metric, rows in by_metric.items():
        same_day = [r for r in rows if (r["event_at"] or "")[:10] == day]
        if not same_day:
            continue
        total = sum(r["value_num"] or 0.0 for r in same_day)
        who = _FLOW_LABELS.get(metric, metric).replace("(금액)", "").strip()
        sample = same_day[0]
        out.append(FactRow(
            label=f"{_FLOW_SAMPLE_LABEL} {who}",
            value=fmt_money(total, "KRW"),
            comparison=f"{day} · {sample['publisher'] or ''} · {len(same_day)}종목 합"
                       .strip(" ·"),
            source_url=sample["safe_source_url"] or "",
            data_status=sample["data_status"] or "source_verified",
            known_at=sample["known_at"], subject=_FLOW_SAMPLE_SUBJECT, metric=metric,
            raw_value=total, group="flow",
        ))
    return out


def _kr_flows(conn, cutoff) -> tuple[list[FactRow], list[MissingItem]]:
    """spec B7/R7: KR investor flows are 0 facts today; the report must
    still build, with the gap named in both `missing` and `data_gaps`
    (test_no_kr_flows_still_builds). The moment a flow fact exists, this
    same code fills the section and the gap stops being re-registered as
    outstanding (test_kr_flows_appear_when_present) — the branch is on
    fact presence, not a feature flag."""
    rows = db_mod.facts_as_of(conn, cutoff, category="flow")
    # **같은 매매를 두 단위로 쓴 행은 하나만 싣는다.** KIS는 주체별로 금액과
    # 주식 수를 같이 주는데, 표에서는 행을 두 배로 늘리기만 한다(실측
    # 2026-08-03: 30행 중 15행이 주식 수). 리포트가 답하는 질문은 "누가 얼마어치
    # 사고 팔았나"이고 그 답은 금액이다 (CEO 결정 2026-08-03). 버리는 게 아니라
    # DB에 그대로 있고 상세 페이지가 쓴다.
    #
    # 종목별로 판단하는 이유: 금액을 주지 않는 소스가 있을 수 있다(걷어낸
    # pykrx가 그랬다). 무조건 금액만 남기면 그런 소스의 수급이 통째로 사라지고
    # 리포트가 "한국 수급 결측"이라고 신고한다 — 데이터가 있는데 없다고 말하는
    # 것이다(테스트 `test_kr_flows_appear_when_present`가 이것을 잡는다).
    priced = {r["subject"] for r in rows if (r["metric"] or "").endswith("_value")}
    rows = [r for r in rows
            if (r["metric"] or "").endswith("_value") or r["subject"] not in priced]
    if rows:
        # **표시 가드.** KIS는 한 번 호출에 30거래일을 준다. 그대로 실으면 리포트
        # 하나에 900행(5종목 x 30일 x 6지표)이 들어가고 같은 라벨이 30번 반복돼
        # 아무도 못 읽는다(실측 2026-08-03). 표는 "오늘 누가 사고 누가 팔았나"에
        # 답하는 자리이므로 **(종목, 지표)당 가장 최근 하루**만 싣는다.
        # 나머지 29일은 버리는 게 아니라 DB에 그대로 있고, 가설 판정과 상세
        # 페이지가 그것을 쓴다 — 표는 읽히는 것이 목적이다.
        latest: dict[tuple, object] = {}
        for r in rows:
            key = (r["subject"], r["metric"])
            prev = latest.get(key)
            if prev is None or (r["event_at"] or "") > (prev["event_at"] or ""):
                latest[key] = r
        # `005930.KS net_buy_foreign`이 아니라 `삼성전자(005930.KS) 외국인 순매수`.
        # 공시 행에서 겪은 것과 같은 문제다 — 기계 항목명을 그대로 화면에 실으면
        # 읽는 사람이 무슨 숫자인지 알 수 없다(CEO 지적 2026-08-02·08-03).
        out = [
            _row_from_fact(
                r, f"{_subject_name(r['subject'])} {_FLOW_LABELS.get(r['metric'], r['metric'])}",
                f"{(r['event_at'] or '')[:10]} · {r['publisher'] or ''}".strip(" ·"),
                # 금액 행만 조/억으로 줄인다. 주식 수는 `1,368,737 shares`가
                # 이미 읽히고, 통화가 아니라 수량이라 조/억이 붙으면 틀린다.
                value=(_with_asof(fmt_money(r["value_num"], r["unit"] or ""), r)
                       if (r["metric"] or "").endswith("_value") else None),
                # 막대는 **금액 행에만** 붙인다. 막대 길이는 서로 더할 수 있는
                # 양이라는 뜻인데, 주식 수는 종목이 다르면 더할 수도 비교할
                # 수도 없다. 갈래가 없는 행은 지금까지 쓰던 표로 떨어진다.
                group="flow" if (r["metric"] or "").endswith("_value") else "",
            )
            for r in latest.values()
            # **표본 전용 종목만** 개별 막대에서 뺀다 — 20개를 다 그리면 어제 겨우
            # 77행에서 줄인 핵심 사실이 다시 20줄이 된다. 그 종목들은 합계 막대와
            # 상세 페이지가 대신 보여준다.
            #
            # 화이트리스트(`in _KR_CORE_SET`)로 거르지 않는 이유: 그러면 관측
            # 기업도 표본도 아닌 수급 사실이 **합계에도 안 들어가고 개별 줄에도
            # 없어 통째로 사라진다**. 새 소스가 다른 subject로 수급을 주기
            # 시작하면 아무도 모르게 리포트에서 빠지는 것이다
            # (`test_kr_flows_appear_when_present`가 이것을 잡았다).
            # 빼는 것은 "합계가 이미 보여주는 것"뿐이다.
            if not _folded_into_sample_total(r["subject"])
        ]
        out = _kr_flow_sample_total(latest) + out
        out.sort(key=lambda x: (x.subject != _FLOW_SAMPLE_SUBJECT, x.subject, x.label))
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
    """**표시 창이 아니라 `*_immediate`(직전 관측)를 읽는다.** 이 파일 상단
    주석이 못박은 §6.3 규칙 — "not a synthetic 30-day lookback" — 을 표시
    변경으로부터 지키기 위해서다. 화면을 21거래일 창으로 바꾸면서 여기까지
    같이 바뀌면, 리포트 모양만 손보려던 변경이 국면 판정을 조용히 뒤집는다."""
    cpi = mmap.get("CPIAUCSL", {}).get("delta_pct_immediate")
    pce = mmap.get("PCEPI", {}).get("delta_pct_immediate")
    unrate = mmap.get("UNRATE", {}).get("delta_abs_immediate")
    dgs10 = mmap.get("DGS10", {}).get("delta_abs_immediate")
    dxy = price_map.get("DX-Y.NYB", {}).get("delta_pct_immediate")

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
    # spec 20260810-period-report §1① — 리포트 종류별 비교 창(거래일 기준).
    # morning/close_delta/week_start/event는 표에 없으므로 기본값 1(지금
    # 그대로 직전 거래일)로 떨어진다.
    lookback = _PERIOD_LOOKBACK.get(report_type, 1)
    price_map = _price_map(conn, cutoff, lookback=lookback)
    mmap = _macro_map(conn, cutoff, report_type=report_type)
    macro_facts = _macro_facts(mmap, report_type)
    flow_facts, flow_missing = _kr_flows(conn, cutoff)
    filing_facts = _filing_facts(conn, cutoff)
    market_reaction_all = _market_reaction(price_map, report_type)
    # spec §1② "평소 대비 몇 배" — KOSPI 하나만(모든 행에 붙이지 않는다).
    # 일간 리포트는 lookback=1이라 기간 관측이 1개뿐이므로 `_volatility_ratio`가
    # 자동으로 None을 낸다 — 별도 분기 없이 daily는 문구가 안 생긴다.
    vol_ratio = _volatility_ratio((price_map.get(_VOLATILITY_SUBJECT) or {}).get("hist") or [], lookback)
    headline = _headline(price_map, vol_ratio)
    sector_summary = _sector_summary(price_map)
    sector_index = _sector_index_rows(price_map, report_type)
    # 코스피·코스닥 시장 폭은 한 번만 읽어(`_kr_breadth_history`) 기존 시장
    # 폭 줄과 "오늘 유별난 것" 블록 양쪽이 나눠 쓴다 — 같은 cutoff를 두 번
    # 읽지 않는다.
    kr_breadth_by_date = {s: _kr_breadth_history(conn, cutoff, s) for s in ("KOSPI", "KOSDAQ")}
    # 첫 줄은 관측기업(Core 16), 이어지는 줄은 한국 시장 전체(§1) — 없으면
    # (한국 사실이 아예 없을 때) 첫 줄만 남는다.
    breadth = "\n".join(
        [_breadth_line(sector_summary)] + _kr_breadth_lines(kr_breadth_by_date, report_date))
    unusual_day = _unusual_day_block(kr_breadth_by_date, report_date, price_map, report_type)
    # 그림은 이미 읽어 둔 것들(`kr_breadth_by_date`·`price_map`)과 흐름 사실
    # 하나만 더 읽어 만든다 — 같은 cutoff를 다시 읽지 않으므로 차단선이 어긋날
    # 여지가 없다.
    charts = _charts(conn, cutoff, kr_breadth_by_date, price_map)
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
        market_reaction = _close_delta_rows(price_map, report_type)
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
            market_reaction = [_market_reaction_row(subj, price_map[subj], report_type)]
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

    report = Report(
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
        unusual_day=unusual_day,
        charts=charts,
        interpretation=Interpretation(),
        meta=meta,
    )
    _restore_interpretation(conn, report)
    return report


def _restore_interpretation(conn, report: Report) -> None:
    """리포트를 다시 만들 때 **같은 사실 위에서 쓰인** 해석이 원장에 있으면
    되살린다(CEO 지적 2026-08-05 — 수정 요청 때마다 해석이 사라진다).

    지문이 다르면(= 사실이 바뀌었으면) 되살리지 않는다. 해석은 그때 그 사실을
    보고 쓴 글이라, 바뀐 사실 위에 옛 글을 붙이면 리포트가 거짓말을 한다.
    그 경우는 지금까지처럼 빈 채로 두고 `interpret`가 새로 쓴다.

    **이 함수는 리포트를 못 만들게 하지 않는다.** 원장이 없거나 옛 스키마여서
    조회가 실패해도 해석만 비고 리포트는 그대로 나온다 — 이 프로젝트의
    "어떤 소스가 죽어도 리포트는 나온다" 원칙.
    """
    from ..interp import digest as digest_mod  # 순환 import 회피(interp가 build를 읽는다)
    from ..interp import store as store_mod

    try:
        saved = store_mod.reusable_interpretation(
            conn, report.report_type, report.report_date, report.cutoff_utc,
            digest_mod.facts_fingerprint(report),
        )
    except Exception:  # noqa: BLE001 - 원장 조회 실패가 리포트를 막지 않는다
        return
    if not saved:
        return
    text = saved.get("text") or {}
    report.interpretation = Interpretation(
        reading=text.get("reading", ""),
        counter_reading=text.get("counter_reading", ""),
        thesis_impact=text.get("thesis_impact", ""),
        next_check=text.get("next_check", ""),
        generated_by=text.get("generated_by", ""),
        # **처음 쓰인 시각을 그대로 지킨다.** 되살린 글에 오늘 시각을 찍으면
        # 방금 쓴 해석처럼 보인다.
        generated_at=text.get("generated_at", ""),
    )
    # 본문만 되살리고 이력을 두면 "누가 언제 무엇을 근거로 썼는지"가 사라진다
    # (실측 2026-08-05: `meta.interpretation` 131줄이 통째로 빠졌다).
    # 옛 판(이력을 안 남기던 시절)은 `restorable_meta`가 없으므로 그대로 둔다.
    restored_meta = saved.get("restorable_meta")
    if restored_meta:
        report.meta["interpretation"] = restored_meta
    report.meta["interpretation_restored"] = {
        "interpretation_id": saved.get("interpretation_id"),
        "created_at": saved.get("created_at"),
        "facts_sha256": saved.get("facts_sha256"),
        # 이력까지 되살렸는지 — 옛 판이면 본문만 살아난다.
        "meta_restored": bool(restored_meta),
    }


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
