"""재무 백필 — 제출일(filed) 기준 + 누적치 차분 (spec 백필 §5 ST3).

라이브 `providers/sec_edgar.py`와 같은 계보(S2)를 쓴다: provider 이름
`"sec_edgar"`, 같은 fact_id 규약(`engine._fact_id`), 같은 CONCEPT_CANDIDATES
(재사용, 복사 금지), 같은 CIK 라이브 해석 + PREDECESSOR_CIK 폴백.

라이브와 다른 점:
  1. 라이브는 지표당 "가장 최근 period" **하나만** 고른다(`_period_key`로
     최댓값 하나). 백필은 그 회사가 그 지표를 낸 적 있는 **모든 period**를
     원한다 — 그래서 후보 concept을 전부 풀링하고, 개별 분기는 전부 fact로
     만든다.
  2. `data_status='reconstructed'` + `correction_reason='backfill:financials'`
     (S4), `upsert_fact`가 아니라 `ledger.append_vintage`(S5).
  3. **누적치 차분으로 개별 분기를 파생한다.** 현금흐름표 항목(영업현금흐름·
     capex)은 미국 GAAP 관행상 10-Q에 반기/9개월/연간 누적으로 잡히는 경우가
     많다(NVDA·GOOGL·AMZN 실측, spec §2.3 반증 6). 차분 없이는 분기 현금흐름이
     2년에 2건뿐이다.

## 누적치 차분 규칙 — 무엇을, 왜 (result.md 요약과 동일)

**그룹 키 = `(taxonomy, concept, start)`.** "start가 같다"가 "같은 회계연도"의
대용이다: 회계연도가 다르면 누적 기간의 시작일도 다르기 때문에, 날짜 자체가
회계연도 경계를 결정한다. `fy`/`fp` XBRL 필드는 **쓰지 않는다** — 실측으로
신뢰할 수 없음을 확인했다: 같은 기간(`2019-10-01~2019-12-31`)이 한 회사
안에서 `fp=Q2(2020)`로도 `fp=Q2(2021)`로도 찍힌다(다음 해 10-Q가 비교연도로
그 분기를 다시 실어서 재게재하기 때문). 날짜 기반 그룹핑은 이 재게재에
영향받지 않는다.

**그룹 안에서 "바로 인접한" 누적 기간만 차분한다** (전체 쌍이 아니라). 같은
그룹 안에서 end를 오름차순 정렬해 이웃한 두 항목만 뺀다 — Q1·반기·9개월·연간이
있으면 (반기-Q1)·(9개월-반기)·(연간-9개월) 세 개만 만든다. **차분 결과의
기간 길이가 80~100일(분기)이 아니면 버린다** — 짧은 쪽 누적치가 없어서(예:
Q1과 연간만 있고 반기·9개월이 없음) 인접 항목이 실제로는 9개월 간격이 되는
경우, 그걸 "분기"라고 우기지 않는다(스펙 표현 그대로: "그럴듯하게 틀린
숫자"). 이게 "짧은 쪽 누적치 부재"의 처리 방법이다.

**태그 이동 처리:** 그룹 키에 concept이 들어가므로, 회사가 XBRL 태그를
갈아탄 경계에서는 두 concept의 데이터가 다른 그룹으로 갈라져 차분되지
않는다(그 분기는 커버리지에서 빠지지만, 서로 다른 태그의 값을 섞어 차분하는
것보다 안전하다 — 태그 이동이 종종 정의 변경을 동반하기 때문이다,
`sec_edgar.py`의 capex 태그 주석 참조). **직접 보고된 분기가 있으면 파생하지
않는다** 판정은 concept을 가리지 않고 전체 풀에서 확인한다 — 다른 태그가 이미
그 period_end를 직접 보고했는데 우리가 별개 태그로 파생해 덮으면 태그 이동이
조용히 숫자를 오염시킨다.

**같은 (start,end)에 filed가 여럿이면 가장 이른 값을 diff 입력으로 쓴다.**
이 프로젝트는 "그때 알 수 있었던 값"이 전부다 — 두 입력이 **처음** 동시에
알려진 시점이 진짜 known_at이다. 나중에 어느 한쪽이 재작성되면(값이 달라지면)
다음 백필 실행이 그 새 값으로 **또 다른** derived revision을 추가한다
(append-only, 덮어쓰지 않는다) — 이번 실행 범위에서 재작성 감지 자동화는
하지 않는다([ASSUMPTION], result.md).

**오라클 검증(§ST3-(d)):** 실제 MSFT companyfacts로 이 알고리즘을 미리
검증했다 — revenue 28쌍/operating_cash_flow 29쌍/capex 36쌍, 전부 오차 0%.
operating_income은 42쌍 중 1개(2016-06-30)가 191% 오차인데, 이는 MSFT의
2016 회계연도 불연속영업(노키아) 재분류로 인한 **진짜 재무제표 재작성**이지
차분 알고리즘의 결함이 아니다(재현: `scratchpad/explore_oracle.py`) — 그래서
`test_derived_matches_reported`는 revenue/operating_cash_flow/capex 세
지표만 오라클로 쓴다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .. import db as db_mod
from ..engine import _fact_id
from ..http_client import SafeHttp
from ..models import FactCandidate, RawItem
from ..providers.sec_edgar import (
    ANNUAL_QUARTERLY_FORMS,
    COMPANYFACTS_URL,
    CONCEPT_CANDIDATES,
    PREDECESSOR_CIK,
    TICKERS_URL,
    US_CORE_TICKERS,
    _comparison_basis,
    _duration_days,
    _num,
)
from ..universe import UNIVERSE
from . import BackfillResult
from .ledger import append_vintage

CORRECTION_REASON = "backfill:financials"

QUARTER_MIN_DAYS = 80
QUARTER_MAX_DAYS = 100

# 라이브 `ANNUAL_QUARTERLY_FORMS`(10-Q/10-K/20-F/6-K)에 두 개를 더한다.
# 라이브는 "가장 최근 하나"만 골라 쓰므로 이 폭이 필요 없었지만, 백필은
# 재작성 이력 전체를 원장에 남기는 것이 목적이다(spec ST3 항목 2). 실측
# (`scratchpad/debug_financials2.py` 계열)으로 두 form이 실재 회계 재작성
# 문서임을 확인했다:
#   - `10-Q/A`: 분기보고서 정정 — 형식상 재작성이다. 제외할 이유가 없다.
#   - `8-K`: 신규 세그먼트 구조 등으로 과거 분기를 재분류해 통째로 다시
#     공시하는 문서로도 쓰인다(실측: CAT가 2026-03-26 8-K로 2024~2025년
#     10개 분기 매출을 재분류해 다시 냈다 — 원래 10-Q 수치와 별개의 진짜
#     재작성). `ANNUAL_QUARTERLY_FORMS`를 고치지 않고(재사용 원칙, 라이브의
#     "최신 1개" 선택에는 이 폭이 필요 없다) 백필 전용 상수를 따로 둔다.
BACKFILL_FORMS = frozenset(ANNUAL_QUARTERLY_FORMS) | {"10-Q/A", "8-K"}


def _is_quarterly(u: dict) -> bool:
    d = _duration_days(u)
    return d is not None and QUARTER_MIN_DAYS <= d <= QUARTER_MAX_DAYS


def pool_entries(data: dict, metric: str) -> list[dict]:
    """metric의 모든 concept 후보를 풀링한다(라이브처럼 "가장 최근 하나"를
    고르지 않는다 — 백필은 그 회사가 낸 적 있는 모든 기간을 원한다).

    반환 항목: `{taxonomy, concept, start, end, val, filed}`."""
    out: list[dict] = []
    for taxonomy, concept in CONCEPT_CANDIDATES[metric]:
        node = data.get("facts", {}).get(taxonomy, {}).get(concept)
        if not node:
            continue
        for u in node.get("units", {}).get("USD", []):
            if (u.get("form") in BACKFILL_FORMS and u.get("val") is not None
                    and u.get("end") and u.get("start") and u.get("filed")):
                out.append({
                    "taxonomy": taxonomy, "concept": concept,
                    "start": u["start"], "end": u["end"],
                    "val": _num(u["val"]), "filed": u["filed"],
                    # 어느 문서에서 온 값인지 남긴다. 폼을 8-K/10-Q/A까지 넓혔으므로
                    # (10-K/10-Q만 보던 라이브와 다르다) 사후에 출처 종류를 알
                    # 방법이 없으면 곤란하다 — 8-K 첨부에서 온 값과 정기보고서
                    # 값을 구분할 수 없게 된다.
                    "form": u.get("form"), "fy": u.get("fy"), "fp": u.get("fp"),
                })
    return out


def direct_quarter_entries(pool: list[dict]) -> list[dict]:
    """기간 80~100일인 항목 전부. 같은 end에 filed/val 조합이 여러 개면
    **전부** 포함한다 — 재작성 이력을 원장에 그대로 남긴다(spec ST3 항목 2:
    "각각 별도 revision")."""
    return [u for u in pool if _is_quarterly(u)]


def _direct_end_set(pool: list[dict]) -> set[str]:
    return {u["end"] for u in pool if _is_quarterly(u)}


def derive_quarter_entries(pool: list[dict], direct_ends: set[str] | None = None) -> list[dict]:
    """모듈 docstring의 차분 규칙을 그대로 구현. `direct_ends`를 넘기지
    않으면 `pool` 자체에서 계산한다(단일 호출 편의용 — 실제 run()은 미리
    계산해 넘겨 같은 값을 두 번 스캔하지 않는다)."""
    if direct_ends is None:
        direct_ends = _direct_end_set(pool)

    groups: dict[tuple[str, str, str], dict[str, dict]] = {}
    for u in pool:
        key = (u["taxonomy"], u["concept"], u["start"])
        bucket = groups.setdefault(key, {})
        cur = bucket.get(u["end"])
        if cur is None or u["filed"] < cur["filed"]:  # 가장 이른 filed를 diff 입력으로
            bucket[u["end"]] = u

    out: list[dict] = []
    for (taxonomy, concept, _start), by_end in groups.items():
        ends = sorted(by_end)
        for i in range(len(ends) - 1):
            short, long = by_end[ends[i]], by_end[ends[i + 1]]
            if long["end"] in direct_ends:
                continue  # 직접 보고된 분기가 있으면 파생하지 않는다
            d_start = date.fromisoformat(short["end"]) + timedelta(days=1)
            d_end = date.fromisoformat(long["end"])
            if not (QUARTER_MIN_DAYS <= (d_end - d_start).days <= QUARTER_MAX_DAYS):
                continue  # 짧은 쪽 누적치 부재 등으로 사이 간격이 분기가 아님
            out.append({
                "taxonomy": taxonomy, "concept": concept,
                "start": d_start.isoformat(), "end": d_end.isoformat(),
                "val": long["val"] - short["val"],
                "known_at_basis": max(short["filed"], long["filed"]),
                "long": long, "short": short,
            })
    return out


def build_metric_facts(pool: list[dict], metric: str) -> tuple[list[dict], dict]:
    """-> (variants, current_by_period).

    `variants`: 이 metric에 대해 append할 모든 revision 후보 —
      `{start, end, val, known_at, is_derived, extra}`.
    `current_by_period`: `(start,end) -> 가장 늦게 알려진(known_at 최대) variant`
      — free_cash_flow가 operating_cash_flow/capex를 기간 정확히 일치시켜
      결합할 때 쓴다(그 시점 "현재" 값을 결합하는 것이지, 과거 재작성 각각을
      전부 다시 결합하지 않는다)."""
    direct_ends = _direct_end_set(pool)
    variants: list[dict] = []

    for u in direct_quarter_entries(pool):
        variants.append({
            "start": u["start"], "end": u["end"], "val": u["val"],
            "known_at": f"{u['filed']}T00:00:00+00:00", "is_derived": False,
            "extra": {
                "xbrl_concept": f"{u['taxonomy']}:{u['concept']}",
                "period_start": u["start"], "period_end": u["end"], "filed": u["filed"],
                "form": u.get("form"), "fy": u.get("fy"), "fp": u.get("fp"),
            },
        })

    for d in derive_quarter_entries(pool, direct_ends):
        variants.append({
            "start": d["start"], "end": d["end"], "val": d["val"],
            "known_at": f"{d['known_at_basis']}T00:00:00+00:00", "is_derived": True,
            "extra": {
                "formula": "누적치 차분: 긴 누적기간 값 - 짧은 누적기간 값 (같은 XBRL 태그, 같은 시작일)",
                "xbrl_concept": f"{d['taxonomy']}:{d['concept']}",
                "period_start": d["start"], "period_end": d["end"],
                "long_period_end": d["long"]["end"], "long_val": d["long"]["val"],
                "long_filed": d["long"]["filed"],
                "short_period_end": d["short"]["end"], "short_val": d["short"]["val"],
                "short_filed": d["short"]["filed"],
                "form": d["long"].get("form"), "fy": d["long"].get("fy"),
                "fp": d["long"].get("fp"),
            },
        })

    variants = _resolve_tag_collisions(variants, metric)

    current: dict[tuple[str, str], dict] = {}
    for v in variants:
        key = (v["start"], v["end"])
        cur = current.get(key)
        if cur is None or v["known_at"] > cur["known_at"]:
            current[key] = v
    return variants, current


def _concept_rank(metric: str, xbrl_concept: str) -> int:
    """`CONCEPT_CANDIDATES`에서의 선호 순위(낮을수록 우선). 없으면 맨 뒤."""
    for i, (taxonomy, concept) in enumerate(CONCEPT_CANDIDATES[metric]):
        if xbrl_concept == f"{taxonomy}:{concept}":
            return i
    return len(CONCEPT_CANDIDATES[metric])


def _resolve_tag_collisions(variants: list[dict], metric: str) -> list[dict]:
    """같은 `(기간 종료일, known_at)`에 판이 둘 이상이면 하나만 남긴다.

    **왜 필요한가.** `engine._fact_id`는 concept을 쓰지 않는다
    (`provider:subject:metric:YYYYMMDD`). 그래서 서로 다른 XBRL 태그가 같은
    기간을 **같은 문서로** 보고하면 fact_id도 known_at도 같은 revision이 둘
    생기고, `facts_as_of`는 `(known_at, revision_no)`로 고르므로 **삽입 순서**가
    승자를 정한다. 원장이 "그 시점에 알려져 있던 값"에 답하지 못하는 상태다.

    실측(AEP revenue 2025-06-30, 둘 다 `filed=2025-07-30` `form=10-Q`):
      rank0 `RevenueFromContractWithCustomerExcludingAssessedTax` = 5,055,400,000
      rank1 `Revenues`                                            = 5,086,900,000
    백필은 rank1을, 라이브 `sec_edgar._period_key`는 rank0을 골랐다 — 같은 분기가
    수집 경로에 따라 3,150만 달러 달랐다. 원장은 append-only라 잘못 넣으면
    되돌릴 수 없다. 같은 버그가 라이브에서 한 번 잡힌 적이 있다
    (`sec_edgar.py`: "previously order-dependent — repair.md finding #1").

    **선택 규칙은 라이브와 글자 그대로 같다**(`_period_key`): 기간이 짧은 쪽 →
    concept 선호 순위 → 늦게 제출된 쪽. 두 경로가 다른 규칙을 쓰면 그 자체가
    같은 분기를 두 값으로 만드는 원인이 된다.

    **재작성은 건드리지 않는다.** `filed`가 다르면 known_at이 달라 서로 다른
    판이고, 그때 알려져 있던 값이 실제로 달랐던 것이므로 둘 다 남는다.
    """
    best: dict[tuple[str, str], dict] = {}
    for v in variants:
        key = (v["end"], v["known_at"])
        prior = best.get(key)
        if prior is None or _collision_key(v, metric) > _collision_key(prior, metric):
            best[key] = v
    return list(best.values())


def _collision_key(v: dict, metric: str) -> tuple:
    """`sec_edgar._period_key`와 같은 정렬 기준(max를 취한다)."""
    days = _duration_days({"start": v["start"], "end": v["end"]})
    concept = str(v["extra"].get("xbrl_concept", ""))
    return (-(days if days is not None else 10**6),
            -_concept_rank(metric, concept),
            v["extra"].get("filed", ""))


def build_fcf_variants(ocf_current: dict, capex_current: dict) -> tuple[list[dict], int]:
    """`free_cash_flow = operating_cash_flow - capex`, 기간이 **정확히**
    일치할 때만(spec ST3 항목 4) — `current_by_period`가 이미 (start,end)로
    키가 잡혀 있으므로 교집합 자체가 정확한 기간 일치다.

    -> (variants, 기간 불일치로 건너뛴 개수)."""
    out: list[dict] = []
    for key, ocf in ocf_current.items():
        capex = capex_current.get(key)
        if capex is None:
            continue
        start, end = key
        out.append({
            "start": start, "end": end, "val": ocf["val"] - capex["val"],
            "known_at": max(ocf["known_at"], capex["known_at"]), "is_derived": True,
            "extra": {
                "formula": "operating_cash_flow - capex",
                "period_start": start, "period_end": end,
                "ocf": ocf["val"], "ocf_known_at": ocf["known_at"],
                "capex": capex["val"], "capex_known_at": capex["known_at"],
            },
        })
    # 대칭차집합 **개수**만 세면 창 밖 기간·비분기 기간까지 뭉뚱그려져 사실상
    # 아무 정보가 없는 숫자가 된다. 어느 쪽이 없어서 못 만들었는지 기간별로 남긴다
    # — 그래야 "CAPEX를 안 내는 회사"와 "기간이 어긋난 회사"를 구분할 수 있다.
    only_ocf = sorted(set(ocf_current) - set(capex_current))
    only_capex = sorted(set(capex_current) - set(ocf_current))
    reasons = [f"{s}~{e}:capex_없음" for s, e in only_ocf[-3:]]
    reasons += [f"{s}~{e}:ocf_없음" for s, e in only_capex[-3:]]
    return out, reasons


def _emit(conn, snapshot_id, ticker: str, meta: dict, metric: str, variants: list[dict],
          since: date, until: date, dry_run: bool, reporting_entity_cik: int | None,
          safe_url: str = ""):
    fetched = appended = skipped = 0
    for v in variants:
        end_date = date.fromisoformat(v["end"])
        if not (since <= end_date <= until):
            continue
        extra = dict(v["extra"])
        if reporting_entity_cik is not None:
            extra["reporting_entity_cik"] = reporting_entity_cik
            extra["reporting_entity_note"] = "predecessor registrant"
        fc = FactCandidate(
            raw_ref=f"{ticker}:companyfacts", subject=ticker, category="financials", metric=metric,
            event_at=f"{v['end']}T00:00:00+00:00", market=meta["market"], country=meta["country"],
            value_num=v["val"], unit="USD",
            comparison_basis=_comparison_basis(_duration_days({"start": v["start"], "end": v["end"]})),
            publisher="SEC EDGAR (derived)" if v["is_derived"] else "SEC EDGAR",
            # 명세 S4.1: 백필이 append하는 **모든** revision은 `reconstructed`다.
            # 직접 보고분이라고 `source_verified`를 주면 §9의 사람 확인 단계
            # ("복원 완료 배지가 라이브와 구분돼 보이는가")가 무의미해진다.
            # `reconstructed`는 "값을 우리가 계산했다"가 아니라 "그 시점의
            # 정보차단선을 지켜 재구성했다"는 뜻이다 — 직접 보고분도 그렇다.
            # 파생인지 여부는 `extra.formula` 유무로 여전히 구분된다.
            data_status="reconstructed",
            extra=extra,
        )
        # 이 한 줄이 없으면 리포트 표의 '원자료' 칸이 빈칸으로 렌더된다
        # (실측: 백필 재무 576행 전부 NULL -> 표시 20행 중 10행 링크 없음).
        fc.safe_source_url = safe_url
        fetched += 1
        if dry_run:
            continue
        if append_vintage(conn, _fact_id("sec_edgar", fc), snapshot_id, v["known_at"], fc,
                          correction_reason=CORRECTION_REASON):
            appended += 1
        else:
            skipped += 1
    return fetched, appended, skipped


def _process_company(conn, settings, client, ticker: str, cik: int, meta: dict,
                      since: date, until: date, dry_run: bool,
                      reporting_entity_cik: int | None = None):
    """-> stats dict, 또는 이 CIK에 재무 데이터가 전혀 없으면 None(호출부가
    PREDECESSOR_CIK 폴백을 시도하는 신호)."""
    url = COMPANYFACTS_URL.format(cik=cik)
    try:
        resp = client.get(url)
    except Exception as exc:  # noqa: BLE001
        return {"fetched": 0, "appended": 0, "skipped": 0,
                "missing": [f"{ticker}:companyfacts_error:{exc.__class__.__name__}"]}
    if resp.status_code != 200:
        return {"fetched": 0, "appended": 0, "skipped": 0,
                "missing": [f"{ticker}:companyfacts_http_{resp.status_code}"]}

    payload = resp.text
    data = json.loads(payload)
    pools = {metric: pool_entries(data, metric) for metric in CONCEPT_CANDIDATES}
    if not any(pools.values()):
        return None  # 폴백 신호 (XOM류: 신설 지주사가 아직 정기보고서가 없음)

    external_id = f"{ticker}:companyfacts"
    safe_url = client.safe_url(str(resp.request.url))
    snapshot_id = None
    if not dry_run:
        snapshot_id = db_mod.insert_raw_snapshot(
            conn, settings.raw_dir, "sec_edgar",
            RawItem(external_id=external_id, source_published_at=db_mod.iso_utc(),
                    safe_source_url=safe_url, payload=payload, fetch_status="ok"),
        )

    fetched = appended = skipped = 0
    missing: list[str] = []
    current_by_metric: dict[str, dict] = {}

    for metric, pool in pools.items():
        if not pool:
            missing.append(f"{ticker}:{metric}:concept_not_found")
            current_by_metric[metric] = {}
            continue
        variants, current = build_metric_facts(pool, metric)
        current_by_metric[metric] = current
        f, a, s = _emit(conn, snapshot_id, ticker, meta, metric, variants, since, until,
                        dry_run, reporting_entity_cik, safe_url)
        fetched += f
        appended += a
        skipped += s

    fcf_variants, mismatch_reasons = build_fcf_variants(
        current_by_metric.get("operating_cash_flow", {}), current_by_metric.get("capex", {}),
    )
    f, a, s = _emit(conn, snapshot_id, ticker, meta, "free_cash_flow", fcf_variants,
                    since, until, dry_run, reporting_entity_cik, safe_url)
    fetched += f
    appended += a
    skipped += s
    if mismatch_reasons:
        missing.append(f"{ticker}:free_cash_flow:기간불일치 " + ", ".join(mismatch_reasons))

    return {"fetched": fetched, "appended": appended, "skipped": skipped, "missing": missing}


def run(conn, settings, source: str, *, since: date, until: date,
        subjects=None, dry_run: bool = False, http=None) -> BackfillResult:
    """`http`는 테스트가 `MockTransport`를 밀어 넣는 자리다(macro.py와 같은
    관례). 레지스트리 계약(`SOURCES`)은 이 인자를 주지 않으므로 기본값이
    실제 클라이언트다."""
    http = http or (lambda name: SafeHttp(name, settings))
    client = http("sec_edgar")

    tickers = [t for t in US_CORE_TICKERS if subjects is None or t in subjects]
    if not tickers:
        return BackfillResult(source=source, status="NO_DATA", reason_code="no_subjects")

    try:
        resp = client.get(TICKERS_URL)
    except Exception as exc:  # noqa: BLE001
        return BackfillResult(source=source, status="ERROR", reason_code="network_error",
                              detail=f"{exc.__class__.__name__}: {exc}"[:300])
    if resp.status_code != 200:
        return BackfillResult(source=source, status="ERROR", reason_code="host_rejected",
                              detail=f"tickers http {resp.status_code}")
    by_ticker = {v["ticker"]: v for v in json.loads(resp.text).values()}

    fetched = appended = skipped = 0
    missing: list[str] = []

    for ticker in tickers:
        meta = next(m for m in UNIVERSE if m["symbol"] == ticker)
        entry = by_ticker.get(ticker)
        if entry is None:
            missing.append(f"{ticker}:cik_not_found")
            continue
        cik = int(entry["cik_str"])

        stat = _process_company(conn, settings, client, ticker, cik, meta, since, until, dry_run)
        if stat is None and ticker in PREDECESSOR_CIK:
            # 이 티커의 현재 CIK에는 정기보고서가 없다(예: XOM 지주회사 개편,
            # spec §5 항목 1 / `providers/sec_edgar.py` PREDECESSOR_CIK 주석).
            # 실제 재무 이력을 쥔 이전 등록자로 대체하고 어느 entity에서 온
            # 값인지 extra에 남긴다(_emit의 reporting_entity_cik).
            stat = _process_company(conn, settings, client, ticker, PREDECESSOR_CIK[ticker], meta,
                                    since, until, dry_run,
                                    reporting_entity_cik=PREDECESSOR_CIK[ticker])
        if stat is None:
            missing.append(f"{ticker}:companyfacts_no_data")
            continue

        fetched += stat["fetched"]
        appended += stat["appended"]
        skipped += stat["skipped"]
        missing.extend(stat["missing"])

    if not dry_run:
        conn.commit()
    if fetched == 0:
        return BackfillResult(source=source, status="NO_DATA", reason_code="empty_response",
                              detail="; ".join(missing[:8])[:300])
    return BackfillResult(
        source=source, status="PARTIAL" if missing else "OK",
        fetched=fetched, appended=appended, skipped_existing=skipped,
        detail="; ".join(missing[:8])[:300],
    )
