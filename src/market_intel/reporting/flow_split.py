"""자금 갈래 — **오늘 돈이 갈렸나, 갈렸다면 어디로 보이나.**

CEO 지시(2026-08-20): *"궁극적인 목표 중 하나가 기술이나 돈 수급이 다른 쪽으로
쏠리는 걸 포착하는 것"* · *"어디로인지 알 수 있어?"*

## 무엇을 재나

미국 업종 11개(GICS 전부)의 **같은 날 등락률이 서로 얼마나 벌어졌나**를 잰다.
벌어짐이 작으면 시장 전체가 한 방향으로 움직인 것이고, 크면 **돈이 한쪽에서
다른 쪽으로 갔다**는 뜻이다. 그 벌어짐이 자기 이력에서 유별난 날만 신고한다.

## 어디로 갔나 — 네 갈래

시장 전체(S&P500)와 미국채 10년물을 함께 보면 갈래가 나뉜다.

```
시장이 거의 안 움직였다        -> 주식 안에서 이동      (진짜 로테이션)
시장이 올랐다                 -> 시장 전체 상승 + 차등  (이동이 아니라 강도 차)
시장이 빠졌고 금리도 내렸다      -> 주식 밖으로 (채권 쪽)
시장이 빠졌는데 금리는 안 내렸다  -> 주식 밖으로 (현금/불명)
```

**실측이 중요한 사실 하나를 알려준다**(1,412거래일): 벌어짐이 상위 2%인 29일 중
**「주식 안에서 이동」은 2일뿐**이었다. 나머지는 시장 전체가 움직이며 업종별
강도가 달랐던 것이다. 즉 사람들이 "쏠림"이라 부르는 현상은 생각보다 훨씬 드물다.
이 블록의 값어치의 절반은 **"오늘은 이동이 아니다"라고 말해 주는 것**에 있다.

## ⚠️ 이것은 인과가 아니다

전부 **같은 날 함께 일어난 일**을 적은 것이다. "채권 쪽"은 금리가 내렸다는
뜻이지 **그 돈이 그 주식에서 나왔다는 증거가 아니다** — 타임라인이 하나뿐이라
그건 원리적으로 확인할 수 없다. 문구가 매번 그 사실을 밝힌다
(`/market-claim` 규율: 동시 발생을 인과로 쓰지 않는다).

## 문턱이 사각지대(2%)와 다른 이유

여기는 **통계 하나**(벌어짐)만 검사하므로 상위 5%가 그대로 5%다. 사각지대는
축 30개를 **동시에** 검사해서 개별 문턱을 조여야 했다(그쪽 주석 참조).
상위 5% = 연 12.7번(한 달에 한 번꼴)이고, 상위 2%로 조이면 연 5.2번까지
줄어 「주식 안에서 이동」 표본이 1,412일 중 2건밖에 안 남는다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# GICS 11개 섹터 전부. **미국만 본다** — 한국은 업종 ETF가 시장을 빠짐없이
# 나누지 못하고(유통·소비·통신이 비어 있다) 일간 국채 지표도 없어서 "주식
# 밖으로" 갈래를 만들 수 없다. 반쪽짜리 판정을 내느니 미국만 말한다.
US_SECTORS: tuple[str, ...] = (
    "XLK", "XLV", "XLF", "XLE", "XLI", "XLU", "XLP", "XLY", "XLB", "XLRE", "XLC",
)
MARKET = "^GSPC"      # 시장 전체
RATE = "DGS10"        # 미국채 10년물 (거시 — `delta_abs_immediate`가 %p 변화)

RANK_THRESHOLD = 5    # 벌어짐이 자기 이력 상위 몇 % 안일 때 신고하나
MIN_HISTORY_DAYS = 60  # 백분위를 말하려면 최소 이 정도는 있어야 한다
# 시장이 "거의 안 움직였다"고 볼 폭. 이 안이면 위아래 어느 쪽도 아니므로
# 업종이 갈린 것은 시장 전체 때문이 아니라 **안에서 옮겨간 것**이다.
FLAT_MARKET_PCT = 0.5
# 금리가 "내렸다"고 볼 폭(%p). 하루 변동의 중앙값 근처에서 잡은 보수적인 선이다
# — 0으로 두면 반올림 잡음까지 "채권으로 갔다"가 된다. [ASSUMPTION]
RATE_DOWN_PP = -0.02
TOP_N = 3             # 위/아래로 몇 개씩 보일까

ROTATION = "주식 안에서 이동"
TO_BONDS = "주식 밖으로 (채권 쪽)"
TO_CASH = "주식 밖으로 (현금·불명)"
BROAD_UP = "시장 전체 상승 + 업종별 강도 차"
UNKNOWN = "판정 불가"


@dataclass
class FlowSplit:
    """판단은 전부 여기서 끝내고 렌더러는 문장만 싣는다 — `UnusualDayBlock`·
    `BlindSpotRow`와 같은 관례다."""
    is_notable: bool = False
    dispersion: float = 0.0        # 오늘 업종 간 등락률 표준편차(%p)
    rank_pct: float = 0.0          # 자기 이력에서 상위 몇 %
    market_pct: float | None = None   # S&P500 등락률
    rate_change_pp: float | None = None  # 10년물 변화(%p)
    verdict: str = ""
    up: list[tuple[str, float]] = field(default_factory=list)
    down: list[tuple[str, float]] = field(default_factory=list)
    note: str = ""


def _daily_returns(closes_asc: list[float]) -> list[float]:
    return [(b - a) / a * 100 for a, b in zip(closes_asc, closes_asc[1:]) if a]


def dispersion_series(price_map: dict) -> list[tuple[str, float]]:
    """(날짜, 그날 업종 간 등락률 표준편차) 오름차순.

    **날짜로 맞춘다.** `hist`는 종목마다 거래일 수가 달라 값만 늘어놓고 자리로
    맞추면 다른 날끼리 비교하게 된다(`hist_dates`가 존재하는 이유와 같다).
    11개가 전부 있는 날만 센다 — 일부만으로 낸 표준편차는 다른 날과 견줄 수
    없기 때문이다."""
    by_date: dict[str, dict[str, float]] = {}
    for sym in US_SECTORS:
        info = price_map.get(sym) or {}
        hist, dates = info.get("hist") or [], info.get("hist_dates") or []
        if len(hist) != len(dates) or len(hist) < 2:
            continue
        for d, r in zip(dates[1:], _daily_returns(hist)):
            by_date.setdefault(d, {})[sym] = r
    out = []
    for d in sorted(by_date):
        vals = by_date[d]
        if len(vals) == len(US_SECTORS):
            out.append((d, statistics.pstdev(vals.values())))
    return out


def compute(price_map: dict, macro_map: dict, label_of) -> FlowSplit:
    """`price_map`/`macro_map`은 리포트가 이미 차단선을 걸어 만든 것이다 —
    이 함수는 DB를 읽지 않는다(사각지대 모듈과 같은 원칙)."""
    series = dispersion_series(price_map)
    if len(series) < MIN_HISTORY_DAYS + 1:
        return FlowSplit()
    today_date, today = series[-1]
    past = [v for _, v in series[:-1]]
    rank = sum(1 for v in past if v >= today) / len(past) * 100
    if rank > RANK_THRESHOLD:
        return FlowSplit(dispersion=today, rank_pct=round(rank, 1))

    moves = []
    for sym in US_SECTORS:
        info = price_map.get(sym) or {}
        hist, dates = info.get("hist") or [], info.get("hist_dates") or []
        if len(hist) == len(dates) and dates and dates[-1] == today_date and len(hist) >= 2:
            rs = _daily_returns(hist)
            if rs:
                moves.append((label_of(sym), rs[-1]))
    moves.sort(key=lambda t: -t[1])
    up = [m for m in moves[:TOP_N] if m[1] > 0]
    down = [m for m in moves[-TOP_N:] if m[1] < 0][::-1]

    mkt_info = price_map.get(MARKET) or {}
    mkt_hist, mkt_dates = mkt_info.get("hist") or [], mkt_info.get("hist_dates") or []
    market = None
    if len(mkt_hist) == len(mkt_dates) and mkt_dates and mkt_dates[-1] == today_date:
        rs = _daily_returns(mkt_hist)
        market = rs[-1] if rs else None
    rate = (macro_map.get(RATE) or {}).get("delta_abs_immediate")

    if market is None:
        verdict = UNKNOWN
    elif abs(market) < FLAT_MARKET_PCT:
        verdict = ROTATION
    elif market > 0:
        verdict = BROAD_UP
    elif rate is None:
        verdict = UNKNOWN
    else:
        verdict = TO_BONDS if rate <= RATE_DOWN_PP else TO_CASH

    return FlowSplit(
        is_notable=True, dispersion=round(today, 2), rank_pct=round(rank, 1),
        market_pct=None if market is None else round(market, 2),
        rate_change_pp=None if rate is None else round(rate, 3),
        verdict=verdict, up=up, down=down,
        note=_note(today, rank, market, rate, verdict, up, down),
    )


def _note(disp, rank, market, rate, verdict, up, down) -> str:
    head = (f"미국 업종 11개의 등락이 {disp:.2f}%p 벌어졌다 "
            f"(자기 이력 상위 {rank:.1f}%) — 시장 전체가 한 방향으로 움직인 것이 "
            f"아니라 **갈렸다**.")
    where = []
    if up:
        where.append("위로 " + " · ".join(f"{n} {v:+.1f}%" for n, v in up))
    if down:
        where.append("아래로 " + " · ".join(f"{n} {v:+.1f}%" for n, v in down))
    mid = ("  " + " / ".join(where) + ".") if where else ""
    if verdict == ROTATION:
        tail = (f" S&P500이 {market:+.2f}%로 거의 제자리라, 시장이 통째로 움직인 것이 "
                f"아니라 **주식 안에서 옮겨간** 모양이다.")
    elif verdict == BROAD_UP:
        tail = (f" S&P500이 {market:+.2f}%로 함께 올랐다 — **돈이 옮겨간 것이 아니라 "
                f"업종별 강도가 달랐던 것**에 가깝다.")
    elif verdict == TO_BONDS:
        tail = (f" S&P500 {market:+.2f}%, 미국채 10년물 {rate:+.3f}%p — 주식이 빠지는 "
                f"동안 금리도 내렸다(채권값 상승).")
    elif verdict == TO_CASH:
        tail = (f" S&P500 {market:+.2f}%인데 미국채 10년물은 {rate:+.3f}%p로 내리지 "
                f"않았다 — 주식에서 빠진 돈이 어디로 갔는지 이 리포트는 모른다.")
    else:
        tail = " 시장 전체나 금리 값이 없어 갈래는 판정하지 못한다."
    return head + mid + tail + " (같은 날 함께 일어난 일이지 원인을 말하는 것이 아니다.)"
