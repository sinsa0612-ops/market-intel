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
from datetime import date, datetime, timedelta

from .. import db as db_mod
from .. import schedule as schedule_mod
from ..universe import CORE16_SYMBOLS, UNIVERSE
from .cutoff import KST
from .model import CalendarRow, FactRow, Interpretation, MissingItem, Report, worst_data_status

_UNIVERSE_BY_SYMBOL = {m["symbol"]: m for m in UNIVERSE}

# spec §6.1 "지수/위험선호/실물 변수" — everything that is not a Core16
# equity but still belongs in the daily "시장의 체온" snapshot.
_MARKET_REACTION_SYMBOLS = [
    "^KS11", "^KQ11", "^GSPC", "^IXIC", "^SOX", "^VIX", "^TNX",
    "KRW=X", "DX-Y.NYB", "CL=F", "GC=F", "HG=F",
]
_CORE16_MOVE_THRESHOLD = 3.0  # spec §6.1 "Core 16 중 하루 3% 이상 움직인 기업만"
_CLOSE_DELTA_MANDATORY = {"^KS11", "KRW=X"}  # spec B6 close_delta 선정 규칙

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


def _row_from_fact(row, label: str, comparison: str) -> FactRow:
    return FactRow(
        label=label, value=_fmt_value_with_asof(row), comparison=comparison,
        source_url=row["safe_source_url"] or "", data_status=row["data_status"] or "unverified",
        known_at=row["known_at"], subject=row["subject"], metric=row["metric"],
        raw_value=row["value_num"] if row["value_num"] is not None else row["value_text"],
    )


# --- prices / market reaction -------------------------------------------

def _price_map(conn, cutoff) -> dict[str, dict]:
    """subject -> {latest, prev, delta_pct} using every price_close fact
    known as of `cutoff` (spec A5-compliant: one `facts_as_of` call, no
    per-symbol filter, grouped in Python — one fact_id exists per trading
    day per symbol, so a subject filter alone would not collapse to
    "today's close")."""
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
        out[subj] = {"latest": latest, "prev": prev, "delta_pct": delta}
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


def _market_reaction_row(subj: str, info: dict) -> FactRow:
    row = info["latest"]
    meta = _UNIVERSE_BY_SYMBOL.get(subj, {})
    label = meta.get("name", subj)
    # "전일대비" is only true when the two observations really are adjacent
    # sessions. After a holiday, a collection outage, or a backfill the
    # previous close can be many days back, and calling that "전일" states a
    # fact the data does not support (final-review.md F4). Name the actual
    # gap instead; one trading day keeps the familiar wording.
    comparison = "전일 비교 불가(직전 종가 없음)"
    if info["delta_pct"] is not None:
        gap = _session_gap_days(row, info.get("prev"))
        if gap is None:
            comparison = f"직전 종가 대비 {info['delta_pct']:+.2f}%"
        elif gap <= 1:
            comparison = f"전일대비 {info['delta_pct']:+.2f}%"
        else:
            comparison = f"{gap}일 전 종가 대비 {info['delta_pct']:+.2f}%"
    return FactRow(
        label=label, value=_fmt_price_value(row), comparison=comparison,
        source_url=row["safe_source_url"] or "", data_status=row["data_status"] or "unverified",
        known_at=row["known_at"], subject=subj, metric=row["metric"], raw_value=row["value_num"],
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
    krw = part("KRW=X", "USD/KRW", lambda v: f"{v:,.1f}원")
    us10y = part("^TNX", "미10Y", lambda v: f"{v:.2f}%")
    n_movers = sum(
        1 for s in CORE16_SYMBOLS
        if price_map.get(s) and price_map[s]["delta_pct"] is not None
        and abs(price_map[s]["delta_pct"]) >= _CORE16_MOVE_THRESHOLD
    )
    n_missing = sum(1 for s in CORE16_SYMBOLS if s not in price_map)
    return f"{kospi} · {spx} · {krw} · {us10y} — Core16 중 ±3% 이상 {n_movers}종목, 결측 {n_missing}건"


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
        out.append(_row_from_fact(info["latest"], label, comparison))
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
    name = (meta or {}).get("name")
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


def _filing_facts(conn, cutoff, since: str | None = None) -> list[FactRow]:
    rows = list(db_mod.facts_as_of(conn, cutoff, category="filing"))
    rows += list(db_mod.facts_as_of(conn, cutoff, category="13f_filing"))
    rows += list(db_mod.facts_as_of(conn, cutoff, category="event", metric="earnings_release_8k"))
    out = []
    for row in rows:
        if since is not None and (row["known_at"] or "") < since:
            continue
        label = f"{_subject_name(row['subject'])} · {row['metric']}"
        out.append(_row_from_fact(row, label, row["publisher"] or ""))
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
    }

    return Report(
        report_type=report_type,
        report_date=report_date.isoformat(),
        cutoff_kst=cutoff.astimezone(KST).isoformat(),
        cutoff_utc=db_mod.iso_utc(cutoff),
        generated_at=db_mod.iso_utc(),
        title=title,
        headline=headline,
        data_status=status,
        facts=facts,
        market_reaction=market_reaction,
        events=events,
        schedule_changes=schedule_changes,
        missing=missing,
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
