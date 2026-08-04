"""KRX 전종목 일별매매정보 -> 한국 시장 폭(breadth) provider.

리포트가 지금 "시장 폭"이라고 부르는 것은 관측기업 16개 중 몇 개가 올랐나였다.
그건 시장이 아니라 우리 관측군이다. 여기서는 코스피·코스닥 **전종목**을 세어
"지수는 빠졌는데 종목 대부분은 올랐다" 같은 쏠림/착시를 잡는다.

## 함정 세 가지 (실측으로 잡았다, spec §1·§3)

1. **거래량 0 종목을 보합에 섞으면 안 된다.** `ACC_TRDVOL == 0`인 종목은
   `FLUC_RT`가 0.00으로 오지만 진짜 보합(거래됐는데 가격이 안 바뀜)이 아니다.
   섞으면 "시장이 조용했다"는 없는 신호가 생긴다(실측 8/3: 분리 전 코스피
   보합 69 = 진짜 보합 43 + 거래없음 26, 코스닥 182 = 89 + 93).
2. **중앙값은 거래된 종목만.** 거래 없는 종목의 0.00%를 섞으면 중앙값이 0
   쪽으로 끌린다(실측 8/3 코스닥: 섞으면 1.07%, 분리하면 1.25%).
3. **시총가중 등락률은 전일 시총 역산**(`MKTCAP_i / (1 + FLUC_RT_i/100)`)이다.
   `FLUC_RT_i <= -100`이면 역산이 수학적으로 불가능(0 또는 음수 나눗셈)하므로
   그 종목만 분자·분모 양쪽에서 뺀다. 조용히 0 취급하면 안 된다.

개별 종목은 fact로 남기지 않는다(CEO 결정 2026-08-04) — 원문 전종목 배열이
raw_snapshot에 그대로 남으므로 나중에 언제든 다시 계산할 수 있다.
"""
from __future__ import annotations

import json
import statistics
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from .kis_flows import _event_at, _num

KR_TZ = ZoneInfo("Asia/Seoul")

KOSPI_URL = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
KOSDAQ_URL = "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd"
MARKETS: tuple[tuple[str, str], ...] = (("KOSPI", KOSPI_URL), ("KOSDAQ", KOSDAQ_URL))

# 당일치는 장중에 항상 비어 있다(spec §1 실측: 11:35에 basDd=오늘 조회 시
# 0행). 그래서 오늘부터 하루씩 거슬러 올라간다 — `cli_krx.py`의
# `MAX_LOOKBACK_DAYS` 패턴과 같은 값이다(주말+공휴일이 7일을 넘는 경우는
# 없다).
MAX_LOOKBACK_DAYS = 7

PUBLISHER = "한국거래소 KRX"


def _fetch(client, api_key: str, url: str, bas_dd: str) -> tuple[list[dict], str]:
    """-> (그 날짜 전종목 배열, safe_url). HTTP 실패는 예외로 올라간다."""
    resp = client.get(url, params={"basDd": bas_dd}, headers={"AUTH_KEY": api_key})
    if resp.status_code != 200:
        raise RuntimeError(f"http {resp.status_code}")
    body = json.loads(resp.text) or {}
    rows = body.get("OutBlock_1") or []
    return rows, client.safe_url(str(resp.request.url))


def _fetch_with_lookback(client, api_key: str, url: str, start_date) -> tuple[list[dict], str]:
    """오늘부터 최대 `MAX_LOOKBACK_DAYS`일 거슬러 올라가며 첫 비어 있지 않은
    응답을 쓴다. 끝까지 비어 있으면 ([], 마지막으로 시도한 safe_url)을 돌려준다
    — 호출자가 "휴장일이 아니라 못 찾음"으로 구분해 보고한다."""
    safe_url = url
    day = start_date
    for _ in range(MAX_LOOKBACK_DAYS):
        bas_dd = day.strftime("%Y%m%d")
        rows, safe_url = _fetch(client, api_key, url, bas_dd)
        if rows:
            return rows, safe_url
        day -= timedelta(days=1)
    return [], safe_url


def _compute(rows: list[dict]) -> dict[str, tuple[float | None, str, dict]]:
    """전종목 배열 -> {metric: (value, unit, extra)}. metric 7개가 전부다
    (spec §2) — 추가·삭제 금지.

    분류 우선순위(합계 항등식 `advancers+decliners+unchanged+no_trade==len(rows)`
    이 항상 성립하도록 모든 행을 정확히 한 칸에 넣는다):
      1. 거래량이 0이거나 파싱 불가 -> no_trade
      2. [ASSUMPTION] CMPPREVDD_PRC가 파싱 불가(실측에서는 없었다. 15개
         필드가 항상 채워져 온다) -> 방향을 알 수 없으므로 no_trade와 같은
         칸으로 접는다. 항등식을 깨느니 보수적으로 합친다.
      3. CMPPREVDD_PRC 부호로 advancers/decliners/unchanged
    """
    advancers = decliners = unchanged = no_trade = 0
    traded_flucs: list[float] = []
    turnover = 0.0
    cap_now = 0.0
    cap_prev = 0.0
    excluded = 0

    for row in rows:
        vol = _num(row.get("ACC_TRDVOL"))
        cmp_prev = _num(row.get("CMPPREVDD_PRC"))
        fluc = _num(row.get("FLUC_RT"))
        mktcap = _num(row.get("MKTCAP"))
        trdval = _num(row.get("ACC_TRDVAL"))

        if trdval is not None:
            turnover += trdval

        if vol is None or vol == 0:
            no_trade += 1
        elif cmp_prev is None:
            no_trade += 1
        elif cmp_prev > 0:
            advancers += 1
            if fluc is not None:
                traded_flucs.append(fluc)
        elif cmp_prev < 0:
            decliners += 1
            if fluc is not None:
                traded_flucs.append(fluc)
        else:
            unchanged += 1
            if fluc is not None:
                traded_flucs.append(fluc)

        # 시총가중 역산은 거래 여부와 무관하게 전 종목이 대상이다 — 거래
        # 없는 종목도 전일과 같은 시총으로 지수에 기여한다(spec §3-3).
        if mktcap is not None and fluc is not None and fluc > -100:
            cap_now += mktcap
            cap_prev += mktcap / (1 + fluc / 100)
        else:
            excluded += 1

    median = statistics.median(traded_flucs) if traded_flucs else None
    index_change_pct = ((cap_now - cap_prev) / cap_prev * 100) if cap_prev else None

    return {
        "breadth_advancers": (float(advancers), "issues", {}),
        "breadth_decliners": (float(decliners), "issues", {}),
        "breadth_unchanged": (float(unchanged), "issues", {}),
        "breadth_no_trade": (float(no_trade), "issues", {}),
        "breadth_median_change_pct": (median, "%", {}),
        "index_change_pct": (index_change_pct, "%", {
            "method": "cap_weighted_reconstructed", "excluded_count": excluded,
        }),
        "turnover_value": (turnover, "KRW", {}),
    }


class KrxBreadthProvider:
    name = "krx"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        settings = ctx.settings
        if not settings.krx_api_key:
            # 키가 없으면 네트워크를 두드리지 않는다(다른 provider와 같은 규율).
            return ProviderResult(status="NO_DATA", reason_code="키없음")

        start_date = ctx.now.astimezone(KR_TZ).date()
        client = ctx.http("krx")

        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for subject, url in MARKETS:
            try:
                rows, safe_url = _fetch_with_lookback(
                    client, settings.krx_api_key, url, start_date)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{subject}:{exc.__class__.__name__}:{exc}"[:160])
                continue
            if not rows:
                missing.append(f"{subject}:no_data_in_lookback")
                continue

            # 날짜는 요청한 basDd가 아니라 응답이 말한 BAS_DD를 쓴다(spec §3-4).
            # 한 응답 안에 BAS_DD가 섞여 있으면 그 시장은 발행하지 않는다.
            bas_dds = {str(r.get("BAS_DD")) for r in rows}
            if len(bas_dds) != 1:
                missing.append(f"{subject}:mixed_BAS_DD:{sorted(bas_dds)}"[:160])
                continue
            date_str = bas_dds.pop()

            external_id = f"krx:{subject}:{date_str}"
            event_at = _event_at(date_str)
            raw_items.append(RawItem(
                external_id=external_id, source_published_at=event_at,
                safe_source_url=safe_url,
                payload=json.dumps(rows, ensure_ascii=False), fetch_status="ok"))

            for metric, (value, unit, extra) in _compute(rows).items():
                if value is None:
                    continue
                facts.append(FactCandidate(
                    raw_ref=external_id, subject=subject, category="breadth", metric=metric,
                    event_at=event_at, market="KR", country="KR",
                    value_num=value, unit=unit, publisher=PUBLISHER,
                    data_status="source_verified", extra=extra,
                ))

        if not facts:
            return ProviderResult(status="NO_DATA", reason_code="empty_response",
                                  raw_items=raw_items,
                                  safe_detail="; ".join(missing[:8])[:400])
        return ProviderResult(
            status="PARTIAL" if missing else "OK", reason_code=None,
            raw_items=raw_items, facts=facts,
            safe_detail="; ".join(missing[:8])[:400])
