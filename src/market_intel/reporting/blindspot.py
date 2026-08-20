"""사각지대 신고 — 우리가 **안 보는 곳**에서 일어난 일을 리포트가 스스로 밝힌다.

CEO 질문(2026-08-20 브레인스토밍 23번): *"우리가 관측하지 않는 기업으로 인해
변동된 시장 흐름은 어떻게 해석할 것인가?"* (예: IBM 양자컴퓨팅, 모더나 암백신 3상)

관측 기업은 20곳이고 업종 지수는 19개다. 업종 지수는 **그 업종 전체**를 담으므로,
업종이 크게 움직였는데 그 업종의 우리 관측 기업이 조용하면 **움직인 것은 우리가
안 보는 종목**이다. 그 사실을 리포트가 신고하게 만드는 것이 이 모듈이다.

왜 중요한가 — 신고하지 않으면 해석이 **엉뚱한 것을 원인으로 지목**한다. 진짜
원인은 관측 밖에 있는데, 마침 같이 움직인 우리 관측치가 원인 자리에 앉는다.
그리고 그 해석을 나중에 채점하면 **틀린 믿음에 "맞음" 도장**이 찍힌다 — 채점을
안 하는 것보다 나쁘다(CEO 지적, 브레인스토밍 91번).

## 문턱을 하나만 쓴다

**절대 문턱은 쓸 수 없다.** 실측(2021-01~2026-08, 한국 업종 지수 8개 8,840일):
평소 하루 등락 중앙값이 AI전력설비 2.10% · TIGER 헬스케어 0.95%로 **2배 넘게**
차이 난다. "3% 이상이면 큰 움직임"으로 자르면 한쪽은 거의 매일 걸리고 한쪽은
거의 안 걸린다.

그래서 **자기 이력 대비 백분위**를 쓰고, 그 값은 이 프로젝트가 「오늘 유별난 것」
에서 이미 쓰는 상위 5%(`build._UNUSUAL_DAY_RANK_THRESHOLD`)와 **같은 값**이다 —
기준이 한 프로젝트 안에서 두 개가 되면 어느 날 화면이 서로 어긋난다.

그리고 **양쪽에 같은 문턱을 적용**한다: 업종도 상위 5%, 관측 기업도 상위 5%.
"업종은 유별난데 우리 기업은 유별나지 않다"가 사각지대의 정의다. 이렇게 하면
"조용했다"를 정하는 **두 번째 자의적인 숫자가 필요 없다**.
"""
from __future__ import annotations

from dataclasses import dataclass

# 백분위를 낼 때 요구하는 최소 과거 관측일. 표본이 모자라면 아무 말도 하지
# 않는다 — `build._KR_BREADTH_MIN_HISTORY_DAYS`와 같은 값·같은 태도다
# (맥락이 없으면 백분위를 아예 말하지 않는다).
MIN_HISTORY_DAYS = 60

# 상위 몇 %부터 "유별나다"고 부를지.
#
# 「오늘 유별난 것」(`build._UNUSUAL_DAY_RANK_THRESHOLD`)은 5%인데 여기는 2%다.
# **일부러 다르다** — 저쪽은 시장 2개(코스피·코스닥)를 보고 여기는 업종 19개를
# 본다. 같은 5%를 19개에 걸면 **하루 평균 1.2건이 정의상 보장**되고(실측: 최근
# 250거래일 중 58%의 날에 신고가 떴다) 신고가 배경 소음이 된다. 다중비교를
# 감안해 개별 문턱을 조인 것이지 숫자를 맞춘 것이 아니다.
#
# 2%를 고른 근거(2021-01~2026-08 전 이력, 업종 19개):
#   문턱 5% -> 실제 발화율 평균 6.36%   문턱 2% -> 평균 2.94%
# 둘 다 명목에 붙어 있으므로 백분위 자체는 잘 잡힌다. 문제는 개수였다.
RANK_THRESHOLD = 2

# 하루에 실을 신고의 최대 건수. 아주 험한 날 업종이 우르르 걸려 리포트가
# 사각지대 목록으로 덮이는 것을 막는다(「오늘 유별난 것」의 top-5와 같은 관례).
MAX_ROWS = 3

# 업종 지수 -> **그 업종에서 우리가 관측하는 기업**.
#
# ⚠️ 이것은 ETF의 실제 구성종목 목록이 아니다. "이 업종이 움직였을 때 우리가
# 그 업종을 대표해 보고 있는 기업이 무엇인가"라는 **이 프로젝트의 판단**이고,
# 그래서 코드에 명시로 적는다 — 추론하면 조용히 틀린다.
#
# 빈 리스트 = 그 업종에 관측 기업이 **아예 없다**. 실측 결과 한국 업종 지수
# 8개 중 6개가 이 상태다. 이것 자체가 이 시스템의 가장 큰 사각지대이고,
# 리포트가 그 업종이 크게 움직인 날 그 사실을 밝힌다.
SECTOR_WATCH: dict[str, list[str]] = {
    # --- 한국 ---
    "091160.KS": ["005930.KS", "000660.KS"],   # KODEX 반도체 <- 삼성전자·SK하이닉스
    "117680.KS": ["005490.KS"],                # KODEX 철강 <- POSCO홀딩스
    "227540.KS": [],                           # TIGER 헬스케어 — 한국 헬스케어 관측 0곳
    "117460.KS": [],                           # KODEX 에너지화학 — 관측 0곳
    "102970.KS": [],                           # KODEX 증권 — KB금융은 은행지주라 다른 업종
    "449450.KS": [],                           # 방산 — 관측 0곳
    "466920.KS": [],                           # 조선 — 관측 0곳
    "487240.KS": [],                           # AI전력설비 — 관측 0곳 (AEP는 미국)
    # --- 미국 ---
    "XLK":  ["MSFT", "NVDA", "MU"],            # 정보기술
    "XLC":  ["GOOGL", "META"],                 # 커뮤니케이션
    "XLY":  ["AMZN"],                          # 경기소비재
    "XLP":  ["WMT"],                           # 필수소비재
    "XLV":  ["LLY"],                           # 헬스케어
    "XLF":  ["JPM"],                           # 금융
    "XLE":  ["XOM"],                           # 에너지
    "XLI":  ["CAT"],                           # 산업재
    "XLU":  ["AEP"],                           # 유틸리티
    "XLRE": ["EQIX"],                          # 부동산·데이터센터
    "XLB":  [],                                # 소재 — 미국 소재 관측 0곳
}

@dataclass
class BlindSpotRow:
    """사각지대 한 줄. 판단은 전부 여기서 끝내고 렌더러는 문장만 싣는다 —
    `UnusualDayBlock`·`ChartBlock`과 같은 관례다."""
    sector_label: str = ""      # 사람이 읽는 업종 이름
    sector_symbol: str = ""     # 업종 지수 심볼
    delta_pct: float = 0.0      # 그 업종의 오늘 등락률
    rank_pct: float = 0.0       # 자기 이력에서 오늘 |등락|의 백분위(상위 몇 %)
    watched_ko: str = ""        # 그 업종에서 우리가 보는 기업(없으면 빈 문자열)
    note: str = ""              # 완성된 한 줄 설명


def _daily_returns(closes_asc: list[float]) -> list[float]:
    """오름차순 종가 -> 일간 등락률(%). 0 이하 종가는 건너뛴다(나눗셈 보호)."""
    out = []
    for i in range(1, len(closes_asc)):
        prev = closes_asc[i - 1]
        if prev:
            out.append((closes_asc[i] - prev) / prev * 100)
    return out


def top_rank_pct(hist_asc: list[float]) -> float | None:
    """마지막 등락률의 |크기|가 자기 이력에서 상위 몇 %인지.

    1.0을 내면 "상위 1%"(= 가장 드문 쪽). 표본이 `MIN_HISTORY_DAYS` 미만이면
    None — 맥락 없이 백분위를 말하지 않는다.

    오늘(마지막) 값은 **모집단에서 뺀다**. 넣으면 표본이 짧을수록 오늘이
    자기 자신을 밀어 올려 백분위가 낙관적으로 나온다."""
    returns = _daily_returns(hist_asc)
    if len(returns) < MIN_HISTORY_DAYS + 1:
        return None
    today = abs(returns[-1])
    past = [abs(r) for r in returns[:-1]]
    bigger = sum(1 for r in past if r >= today)
    return bigger / len(past) * 100


def _is_unusual(hist_asc: list[float]) -> bool:
    rank = top_rank_pct(hist_asc)
    return rank is not None and rank <= RANK_THRESHOLD


def detect(price_map: dict, label_of) -> list[BlindSpotRow]:
    """오늘 사각지대를 찾는다.

    `price_map`은 `build._price_map`의 결과(차단선을 이미 통과한 값들)이고,
    `label_of(symbol) -> str`는 사람이 읽는 이름을 주는 함수다. **이 함수는
    DB를 읽지 않는다** — 리포트의 정보 차단선을 두 번 해석할 여지를 없앤다.

    등락률이 큰 업종부터 낸다."""
    rows: list[BlindSpotRow] = []
    for sector, watched in SECTOR_WATCH.items():
        info = price_map.get(sector)
        if not info:
            continue
        hist = info.get("hist") or []
        if not _is_unusual(hist):
            continue
        delta = info.get("delta_pct")
        if delta is None:
            continue
        present = [s for s in watched if s in price_map]
        if not present:
            # 비교할 값이 없으면 "조용했다"고 말할 수 없다 — 없는 것을 조용함으로
            # 읽으면 결측이 사실로 승격된다. 관측 기업이 **아예 없는** 업종도
            # 여기서 걸린다(`watched`가 비었으니 `present`도 비었다). 그쪽은
            # 상시 사실이라 당일 신고가 아니라 `unwatched_sectors`가 맡는다.
            continue
        if any(_is_unusual(price_map[s].get("hist") or []) for s in present):
            continue  # 우리 기업도 같이 크게 움직였다 = 사각지대가 아니다
        rank = top_rank_pct(hist)
        label = label_of(sector)
        names = " · ".join(label_of(s) for s in present)
        rows.append(BlindSpotRow(
            sector_label=label, sector_symbol=sector, delta_pct=delta,
            rank_pct=round(rank, 1), watched_ko=names,
            note=(f"{label} {delta:+.2f}% (자기 이력 상위 {rank:.1f}%) — 이 업종에서 "
                  f"우리가 보는 {names}은(는) 평소 범위였다. 움직인 것은 우리가 "
                  f"관측하지 않는 종목이다."),
        ))
    rows.sort(key=lambda r: -abs(r.delta_pct))
    return rows[:MAX_ROWS]


def unwatched_sectors(label_of) -> list[str]:
    """관측 기업이 **하나도 없는** 업종의 이름들.

    이건 그날의 사건이 아니라 **상시 사실**이라 매일 신고하지 않는다 — 실측하니
    당일 신고의 69%(500거래일 219건 중 151건)가 이 종류였고, 같은 구조적 사실을
    매일 다시 말하는 것이 신고 전체를 배경 소음으로 만들고 있었다. 대신 리포트에
    **고정 한 줄**로 항상 싣는다. 그 업종이 오늘 얼마나 움직였는지는 「업종 지수」
    표에 이미 다 나오므로, 두 개를 나란히 보면 정보 손실이 없다."""
    return [label_of(s) for s, watched in SECTOR_WATCH.items() if not watched]
