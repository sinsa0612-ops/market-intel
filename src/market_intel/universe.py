"""Observed universe — symbols fixed by spec A9. Do not add symbols outside
this list (Core 16 boundary is enforced by convention, not code, per the
task's "Core 16 밖 종목 금지" boundary)."""
from __future__ import annotations


# spec §12 "Core 16: 시장 관측 기업군" — the six axes of the table, in the
# order the document lists them. These are the spec's own words: nothing is
# invented here and nothing is merged. §6.1 asks 업종 to answer "시장 폭과
# 순환은 어떤가", which is only answerable if every observed company sits on
# exactly one of these axes.
#
# 마지막 두 축은 §12 표 밖이다 (CEO 확정 2026-08-02, 구현 2026-08-03). 근거:
# 세계 표준 분류(GICS 11업종)에 §12의 16개 기업을 대보면 **소재와 부동산이
# 통째로 0개**였다. 업종 지수 16개를 따로 붙여 "폭"은 덮었지만, 그 두 업종에는
# 분기 재무·가설 검증이 걸리는 **기업이 하나도 없어** 깊이 층이 비어 있었다.
# §12 운영규칙("연간: 대표성을 잃은 기업·지표 교체 검토")과 §14("3개월 후 조정")가
# 관측군을 고정이 아니라 갱신 대상으로 두고 있으므로 표를 늘리는 것 자체는
# 명세 안이다. 축 이름이 GICS의 `소재`/`부동산`이 아니라 복합어인 것은 나머지
# 여섯 축과 같은 규칙을 따랐기 때문이다 — §12 표의 셋째 칸("관측할 시장 신호")을
# 이름에 담는다.
SECTORS: tuple[str, ...] = (
    "플랫폼·AI 수익화", "반도체·공급망", "금융·신용", "전력·산업", "소비·수출", "헬스케어",
    "소재·철강", "부동산·데이터센터",
)


def _sym(
    symbol: str, market: str, country: str, asset_type: str, name: str,
    core16: bool = False, unit: str | None = None, sector: str | None = None,
    name_ko: str | None = None,
) -> dict:
    """`unit` overrides the exchange currency for instruments that are not
    quoted in money. An index is points, a yield is percent, and KRW=X is
    won-per-dollar (not dollars) — labelling any of them with the listing
    currency makes a report say "US 10Y at 4.2 USD". None = use the
    exchange currency reported by the price source.

    `sector` is the spec §12 axis and only Core 16 companies have one: an
    index, an FX pair or a commodity is not a 업종, and giving KOSPI a sector
    would put the market's own benchmark inside the breadth count that is
    supposed to measure it. The same holds for the `sector_index` ETFs added
    below — they *measure* a sector, they do not *belong* to one."""
    if sector is not None and sector not in SECTORS:
        raise ValueError(f"universe: {symbol} has a sector outside spec §12: {sector!r}")
    if sector is not None and not core16:
        raise ValueError(f"universe: {symbol} is not Core 16 but carries a sector")
    return {
        "symbol": symbol,
        "market": market,
        "country": country,
        "asset_type": asset_type,
        "name": name,
        # 명세 §6.1(지수·위험선호·실물)과 §12(Core 16)가 이미 쓰는 한국어 표기.
        # 새로 지어낸 이름이 아니라 그 문서의 표기를 그대로 옮긴 것이고,
        # 없는 항목은 원문 영문명을 그대로 쓴다.
        "name_ko": name_ko or name,
        "core16": core16,
        "unit": unit,
        "sector": sector,
    }


UNIVERSE: list[dict] = [
    _sym("MSFT", "US", "US", "equity", "Microsoft", True, sector="플랫폼·AI 수익화"),
    _sym("GOOGL", "US", "US", "equity", "Alphabet", True, sector="플랫폼·AI 수익화"),
    _sym("AMZN", "US", "US", "equity", "Amazon", True, sector="플랫폼·AI 수익화"),
    _sym("META", "US", "US", "equity", "Meta Platforms", True, sector="플랫폼·AI 수익화"),
    _sym("NVDA", "US", "US", "equity", "NVIDIA", True, sector="반도체·공급망"),
    _sym("TSM", "US", "TW", "equity", "TSMC ADR", True, sector="반도체·공급망"),
    _sym("005930.KS", "KR", "KR", "equity", "Samsung Electronics", True, sector="반도체·공급망", name_ko="삼성전자"),
    _sym("000660.KS", "KR", "KR", "equity", "SK Hynix", True, sector="반도체·공급망", name_ko="SK하이닉스"),
    _sym("JPM", "US", "US", "equity", "JPMorgan Chase", True, sector="금융·신용"),
    _sym("105560.KS", "KR", "KR", "equity", "KB Financial Group", True, sector="금융·신용", name_ko="KB금융"),
    _sym("XOM", "US", "US", "equity", "Exxon Mobil", True, sector="전력·산업"),
    _sym("AEP", "US", "US", "equity", "American Electric Power", True, sector="전력·산업"),
    _sym("CAT", "US", "US", "equity", "Caterpillar", True, sector="전력·산업"),
    _sym("WMT", "US", "US", "equity", "Walmart", True, sector="소비·수출"),
    _sym("005380.KS", "KR", "KR", "equity", "Hyundai Motor", True, sector="소비·수출", name_ko="현대차"),
    _sym("LLY", "US", "US", "equity", "Eli Lilly", True, sector="헬스케어"),
    # --- 비어 있던 두 업종 (CEO 확정 2026-08-02, 구현 2026-08-03) -----------
    # 고른 기준은 시가총액이 아니라 §12 표의 셋째 칸("관측할 시장 신호")이다.
    # EQIX: §12가 AEP를 넣은 이유가 "데이터센터 전력수요"인데 EQIX는 그
    #   데이터센터 자체다. NVDA·MSFT의 AI CAPEX가 실제 건물·전력으로 가는지
    #   교차 확인이 된다 — 리츠 한 종목이 아니라 AI 서사의 실물 끝단이다.
    # 005490.KS: §12가 중시하는 "한국 수출·중국 경기"를 철강 수요로 직접 읽고,
    #   이미 추적 중인 CAT(건설·광산)·구리 가격과 짝이 맞는다. 한국 기업이
    #   반도체·금융·자동차에 몰려 있던 공백도 함께 메운다.
    _sym("EQIX", "US", "US", "equity", "Equinix", True, sector="부동산·데이터센터"),
    _sym("005490.KS", "KR", "KR", "equity", "POSCO Holdings", True, sector="소재·철강", name_ko="POSCO홀딩스"),
    _sym("^KS11", "KR", "KR", "index", "KOSPI", unit="point", name_ko="KOSPI"),
    _sym("^KQ11", "KR", "KR", "index", "KOSDAQ", unit="point", name_ko="KOSDAQ"),
    _sym("^GSPC", "US", "US", "index", "S&P 500", unit="point", name_ko="S&P 500"),
    _sym("^IXIC", "US", "US", "index", "Nasdaq Composite", unit="point", name_ko="나스닥"),
    # CEO 요청(2026-08-02)으로 추가. 명세 §6.1의 지수 목록(KOSPI/KOSDAQ/
    # S&P 500/Nasdaq/SOX)을 넓힌 것이며, §12 운영규칙의 "연간: 대표성을 잃은
    # 기업·지표 교체 검토"가 관측군을 고정이 아니라 갱신 대상으로 두고 있다.
    # 러셀2000은 중소형주라 대형주 중심인 S&P·나스닥이 못 보여주는 시장 폭을
    # 드러낸다. 닛케이·항셍은 데이터는 오지만 §14의 범위(미국·한국) 밖이라 뺐다.
    _sym("^DJI", "US", "US", "index", "Dow Jones Industrial Average", unit="point", name_ko="다우존스"),
    _sym("^RUT", "US", "US", "index", "Russell 2000", unit="point", name_ko="러셀2000"),
    _sym("^SOX", "US", "US", "index", "PHLX Semiconductor", unit="point", name_ko="SOX(필라델피아 반도체)"),
    _sym("^VIX", "US", "US", "index", "CBOE VIX", unit="point", name_ko="VIX"),
    _sym("^TNX", "US", "US", "rate", "US 10Y Treasury Yield", unit="percent", name_ko="미 10년물"),
    _sym("KRW=X", "US", "US", "fx", "USD/KRW", unit="KRW", name_ko="달러/원"),
    _sym("DX-Y.NYB", "US", "US", "fx", "US Dollar Index", unit="point", name_ko="달러지수"),
    _sym("CL=F", "US", "US", "commodity", "WTI Crude Oil", name_ko="유가"),
    _sym("GC=F", "US", "US", "commodity", "Gold", name_ko="금"),
    _sym("HG=F", "US", "US", "commodity", "Copper", name_ko="구리"),
    # --- 업종 지수 (CEO 확정, 2026-08-02) ---------------------------------
    # Core 16의 6축은 §12가 고른 **관측 기업군**이지 시장의 업종 지도가 아니다:
    # 세계 표준 분류(GICS 11업종)에 대보면 소재(화학·철강)와 부동산(리츠)이
    # 통째로 비어 있고, 헬스케어는 릴리 1종목이라 "헬스케어 상승"이 실은 그
    # 회사 이야기다. 그래서 층을 나눈다 — Core 16은 **깊이**(분기 재무·가설
    # 검증), 업종 지수는 **폭**(어느 업종이 주도하나, 순환).
    #
    # §14가 제한한 것은 기업 Core 16이지 지수가 아니고, §6.1의 업종 행이
    # 요구하는 질문("시장 폭과 순환은 어떤가")·§12 운영규칙의 "월간: 섹터 상대
    # 강도"는 지금 코드가 답하지 못하고 있었다.
    #
    # `sector`가 None인 것은 실수가 아니다: 이것들은 업종을 **측정하는 계기**지
    # 업종에 **속하는 종목**이 아니다. sector를 주면 Core 16 시장 폭 집계에
    # 섞여 들어가 자기 자신을 세게 되고, "Core 16 중 4/6개 상승"이 거짓이 된다.
    _sym("XLK", "US", "US", "sector_index", "Technology Select Sector SPDR", name_ko="정보기술"),
    _sym("XLV", "US", "US", "sector_index", "Health Care Select Sector SPDR", name_ko="헬스케어"),
    _sym("XLF", "US", "US", "sector_index", "Financial Select Sector SPDR", name_ko="금융"),
    _sym("XLE", "US", "US", "sector_index", "Energy Select Sector SPDR", name_ko="에너지"),
    _sym("XLI", "US", "US", "sector_index", "Industrial Select Sector SPDR", name_ko="산업재"),
    _sym("XLU", "US", "US", "sector_index", "Utilities Select Sector SPDR", name_ko="유틸리티"),
    _sym("XLP", "US", "US", "sector_index", "Consumer Staples Select Sector SPDR", name_ko="필수소비재"),
    _sym("XLY", "US", "US", "sector_index", "Consumer Discretionary Select Sector SPDR", name_ko="경기소비재"),
    _sym("XLB", "US", "US", "sector_index", "Materials Select Sector SPDR", name_ko="소재"),
    _sym("XLRE", "US", "US", "sector_index", "Real Estate Select Sector SPDR", name_ko="부동산"),
    _sym("XLC", "US", "US", "sector_index", "Communication Services Select Sector SPDR", name_ko="커뮤니케이션"),
    _sym("091160.KS", "KR", "KR", "sector_index", "KODEX Semiconductor", name_ko="KODEX 반도체"),
    _sym("227540.KS", "KR", "KR", "sector_index", "TIGER Health Care", name_ko="TIGER 헬스케어"),
    _sym("117460.KS", "KR", "KR", "sector_index", "KODEX Energy & Chemicals", name_ko="KODEX 에너지화학"),
    _sym("102970.KS", "KR", "KR", "sector_index", "KODEX Securities", name_ko="KODEX 증권"),
    _sym("117680.KS", "KR", "KR", "sector_index", "KODEX Steel", name_ko="KODEX 철강"),
]

# 이름의 "16"은 역사다 — 2026-08-03에 EQIX·POSCO홀딩스가 들어와 **18개**다.
# 상수 이름을 바꾸지 않은 것은 src/tests 33곳에 걸쳐 있어 기능 변경과 섞으면
# 리뷰가 불가능해지기 때문이고, 대신 **사람에게 보이는 문구는 전부 개수를
# 계산해서 쓴다**(`_breadth_line`/`_headline`) — 다음에 종목이 늘어도 화면의
# 숫자가 다시 거짓말을 하지 않는다.
CORE16: list[dict] = [m for m in UNIVERSE if m["core16"]]
CORE16_SYMBOLS: list[str] = [m["symbol"] for m in CORE16]
SECTOR_BY_SYMBOL: dict[str, str] = {m["symbol"]: m["sector"] for m in CORE16 if m["sector"]}
SECTOR_INDEX_SYMBOLS: list[str] = [m["symbol"] for m in UNIVERSE if m["asset_type"] == "sector_index"]

# 관측 기업 중 한국 상장분 — KIS 수급과 DART 공시·재무가 이 목록을 돈다.
KR_CORE_SYMBOLS: list[str] = [
    "005930.KS", "000660.KS", "105560.KS", "005380.KS", "005490.KS",
]
