"""상세 페이지의 조회 계층 — 리포트가 "오늘 무엇이 달라졌나"라면 이쪽은
"이 종목/지표는 그동안 어떻게 움직였나"다.

세 가지 규약을 site.py와 공유한다:

1. **읽기는 전부 `db.facts_as_of(conn, cutoff, ...)`를 통한다.** 상세 페이지도
   공개물이므로 리포트와 같은 정보차단선을 쓴다 — 차단선은 `site.schedule_cutoff`,
   즉 이미 공개된 리포트들의 차단선 중 가장 늦은 것이고 벽시계가 아니다.
2. **이 모듈은 HTML을 만들지 않는다.** 마크업은 site.py가, 이스케이프도 거기서
   한다(site.py 규약 2).
3. **재무 시계열은 기간 길이 하나로 통일한다.** 1년치 누적과 한 분기가 같은
   fact_id를 공유하므로(`db._IDENTITY_FILTERS`), 통일하지 않으면 마지막 분기만
   3~4배 치솟은 가짜 그래프가 나온다. 규칙은 가설 엔진과 같은 것을 쓴다
   (`interp.thesis._dominant_basis`) — 화면과 판정이 다른 수를 보면 안 된다.
"""
from __future__ import annotations

import json
import re

from . import db as db_mod
from .interp.thesis import _dominant_basis
# 이름표는 리포트가 이미 푼 문제다 — 거시지표는 FRED가 `CPIAUCSL`로, ECOS가
# `722Y001.0101000`으로 키잉하고 읽을 이름은 extra에 있으며, 공시는 SEC가
# `form`("10-Q")을, DART가 `report_name`("분기보고서 (2026.03)")을 준다.
# 여기서 표를 다시 만들면 두 화면이 같은 것을 다른 이름으로 부르게 되므로
# 리포트의 표와 함수를 그대로 쓴다.
from .reporting.build import _FORM_LABELS, _ITEM_8K_LABELS, _macro_label
from .universe import UNIVERSE

# CEO 확정 4개 섹션 중 "기업별 재무 추이(매출·영업이익·FCF)"가 지목한 세 항목.
# 순서가 곧 표의 열 순서다(위에서 아래로 손익 → 현금).
FINANCIAL_METRICS: tuple[str, ...] = ("revenue", "operating_income", "free_cash_flow")
METRIC_LABELS = {"revenue": "매출", "operating_income": "영업이익", "free_cash_flow": "FCF"}
BASIS_LABELS = {"quarterly": "분기", "annual": "연간", "180d": "반기 누적", "": "기간 미상"}

# 공시 이력에 들어가는 세 갈래. `filing`=정기공시, `event`=실적 8-K,
# `13f_filing`=기관 보유내역 제출.
FILING_CATEGORIES: tuple[str, ...] = ("filing", "event", "13f_filing")


# 표에 싣는 최근 관측 개수. 재무는 2년치 분기(8)를 넘겨 받되 화면은 12까지,
# 거시는 3년치 일별이 5,000행까지 있으므로 잘라야 한다 — 전 구간은 스파크라인이
# 대신 보여준다.
FINANCIAL_ROWS = 12
MACRO_ROWS = 24
SPARKLINE_POINTS = 24

_NAME_KO = {m["symbol"]: m["name_ko"] for m in UNIVERSE}

# 파일 이름으로 쓸 수 있는 글자만 남긴다. subject는 DB에서 오는 외부 유래
# 문자열이라(`^KS11`, `005930.KS`, `berkshire_hathaway`) 경로 조립에 그대로
# 쓰면 `../`가 섞일 수 있다. 허용 목록 방식이라 무엇이 들어와도 한 조각으로
# 남는다.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def slug(subject: str) -> str:
    """`^KS11` -> `KS11`, `005930.KS` -> `005930-KS`. 서로 다른 subject가 같은
    슬러그로 접히면 페이지 하나가 다른 페이지를 덮어쓰므로, 호출부는
    `slug_map()`으로 충돌을 해소한 것을 쓴다."""
    return _UNSAFE.sub("-", subject).strip("-") or "x"


def slug_map(subjects) -> dict[str, str]:
    """subject -> 충돌 없는 슬러그. 같은 슬러그로 접히는 것이 있으면 뒤에
    일련번호를 붙인다. subject를 정렬해 돌므로 같은 입력이면 늘 같은 결과다."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for subject in sorted(subjects):
        base = slug(subject)
        candidate, n = base, 2
        while candidate in taken:
            candidate, n = f"{base}-{n}", n + 1
        taken.add(candidate)
        out[subject] = candidate
    return out


def name_ko(subject: str, row=None) -> str:
    """관측군에 있는 종목은 그 한국어 이름, 거시지표는 `_macro_label`이 푸는
    이름. `row`가 없으면(기업·기관) 심볼 표를 보고, 있으면 거시지표 규칙까지
    간다."""
    if subject in _NAME_KO:
        return _NAME_KO[subject]
    if row is not None:
        return _macro_label(subject, row)
    return subject


def _extra(row) -> dict:
    try:
        return json.loads(row["extra_json"] or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def fmt_money(value: float | None, unit: str) -> str:
    """재무 숫자는 자릿수가 12~15개라 원본 그대로는 읽히지 않는다(삼성전자 매출
    333,605,938,000,000). 통화 단위에 맞춰 조/억으로 줄인다."""
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


# --- 1) 기업별 재무 추이 ----------------------------------------------------

def company_financials(conn, cutoff, subject: str) -> dict:
    """-> {"basis": "quarterly", "periods": [{"period": "2026-06-30",
    "revenue": {...}, ...}], "metrics": [...]}. 기간은 최신이 먼저다.

    항목마다 기간 길이를 따로 정한다: 한 회사 안에서도 SEC가 매출은 분기로,
    현금흐름은 누적으로만 주는 경우가 있다."""
    by_metric: dict[str, list] = {}
    for metric in FINANCIAL_METRICS:
        rows = db_mod.facts_as_of(conn, cutoff, subject=subject,
                                  category="financials", metric=metric)
        basis = _dominant_basis(rows)
        if basis is None:
            continue
        rows = db_mod.facts_as_of(conn, cutoff, subject=subject, category="financials",
                                  metric=metric, comparison_basis=basis)
        by_metric[metric] = sorted(rows, key=lambda r: r["event_at"] or "", reverse=True)

    metrics = [m for m in FINANCIAL_METRICS if by_metric.get(m)]
    if not metrics:
        return {"periods": [], "metrics": [], "bases": {}, "series": {}}

    periods = sorted({r["event_at"][:10] for rs in by_metric.values() for r in rs}, reverse=True)
    table = []
    for period in periods[:FINANCIAL_ROWS]:
        cell: dict = {"period": period}
        for metric in metrics:
            row = next((r for r in by_metric[metric] if r["event_at"][:10] == period), None)
            cell[metric] = {
                "text": fmt_money(row["value_num"], row["unit"] or "") if row else "—",
                "source_url": (row["safe_source_url"] or "") if row else "",
                "known_at": row["known_at"] if row else "",
                "data_status": (row["data_status"] or "") if row else "",
                "derived": bool(row and _extra(row).get("formula")),
            } if row else {"text": "—", "source_url": "", "known_at": "",
                           "data_status": "", "derived": False}
        table.append(cell)

    # 스파크라인은 오래된 것부터 — 표와 방향이 반대다.
    series = {
        m: [r["value_num"] for r in reversed(by_metric[m][:SPARKLINE_POINTS])
            if r["value_num"] is not None]
        for m in metrics
    }
    bases = {m: _dominant_basis(by_metric[m]) or "" for m in metrics}
    return {"periods": table, "metrics": metrics, "bases": bases, "series": series}


def companies(conn, cutoff) -> list[str]:
    """상세 페이지를 가질 기업 — 재무나 공시가 하나라도 있는 종목. 관측군에
    있으나 아직 아무것도 안 들어온 종목은 빈 페이지를 만들지 않는다."""
    found = {r["subject"] for r in db_mod.facts_as_of(conn, cutoff, category="financials")}
    for category in ("filing", "event"):
        found |= {r["subject"] for r in db_mod.facts_as_of(conn, cutoff, category=category)}
    return sorted(found)


# --- 2) 거시지표별 ----------------------------------------------------------

def macro_series(conn, cutoff, subject: str) -> dict:
    rows = db_mod.facts_as_of(conn, cutoff, category="macro", subject=subject)
    rows = sorted(rows, key=lambda r: r["event_at"] or "", reverse=True)
    if not rows:
        return {"observations": [], "series": [], "unit": "", "total": 0, "label": subject}
    unit = rows[0]["unit"] or ""
    label = name_ko(subject, rows[0])
    observations = [{
        "event_at": r["event_at"][:10],
        "value": r["value_num"],
        "source_url": r["safe_source_url"] or "",
        "known_at": r["known_at"],
    } for r in rows[:MACRO_ROWS]]
    series = [r["value_num"] for r in reversed(rows[:SPARKLINE_POINTS])
              if r["value_num"] is not None]
    return {"observations": observations, "series": series, "unit": unit,
            "total": len(rows), "label": label}


def macro_subjects(conn, cutoff) -> dict[str, str]:
    """subject -> 화면에 쓸 이름. ECOS 코드는 여기서 풀린다."""
    latest: dict[str, object] = {}
    for row in db_mod.facts_as_of(conn, cutoff, category="macro"):
        subject = row["subject"]
        if subject not in latest or (row["event_at"] or "") > (latest[subject]["event_at"] or ""):
            latest[subject] = row
    return {s: name_ko(s, row) for s, row in sorted(latest.items())}


# --- 3) 공시 이력 타임라인 --------------------------------------------------

def _item_label(raw: str) -> str:
    """8-K 항목 코드는 그 자체로는 접수번호만큼이나 아무 말도 하지 않는다 —
    `2.02` -> `실적·재무상태 발표(항목 2.02)`. 모르는 코드는 코드로 둔다."""
    items = [i.strip() for i in str(raw or "").split(",") if i.strip()]
    return " · ".join(
        f"{_ITEM_8K_LABELS[i]}(항목 {i})" if i in _ITEM_8K_LABELS else f"항목 {i}"
        for i in items
    )


def filings(conn, cutoff, subject: str | None = None) -> list[dict]:
    """정기공시·실적 8-K·13F 제출을 한 줄로 합친 타임라인, 최신이 먼저."""
    out: list[dict] = []
    for category in FILING_CATEGORIES:
        for row in db_mod.facts_as_of(conn, cutoff, category=category):
            if subject is not None and row["subject"] != subject:
                continue
            extra = _extra(row)
            form = extra.get("form", "")
            # DART는 `form`을 주지 않고 보고서명을 준다 — 그것까지 못 보면 한국
            # 공시가 전부 "-"로 나온다(실측 2026-08-03: 삼성전자 분기보고서).
            report_name = str(extra.get("report_name") or "").strip()
            out.append({
                "event_at": (row["event_at"] or "")[:10],
                "subject": row["subject"],
                "name": extra.get("manager") or name_ko(row["subject"]),
                "category": category,
                "form": form,
                "form_label": _FORM_LABELS.get(form) or form or report_name or "-",
                "item": _item_label(extra.get("item", "")),
                "accession": row["value_text"] or "",
                "source_url": row["safe_source_url"] or "",
                "known_at": row["known_at"],
            })
    out.sort(key=lambda r: (r["event_at"], r["subject"]), reverse=True)
    return out


# --- 4) 기관(13F) 보유내역 --------------------------------------------------

def holdings_13f(conn, cutoff) -> list[dict]:
    """지금 파이프라인은 **13F가 제출됐다는 사실만** 감지하고 보유내역 표는
    읽지 않는다. 그래서 이 함수가 돌려주는 것은 보유내역이 아니라 제출 이력이며,
    화면도 그렇게 말해야 한다 — 빈 표를 "보유 없음"으로 읽히게 두면 안 된다."""
    rows = filings(conn, cutoff)
    return [r for r in rows if r["category"] == "13f_filing"]
