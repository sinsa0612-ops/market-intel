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
    # 2026-08-14 추가 — **미국 메모리가 관측군에 하나도 없었다.**
    #
    # `ai_semi_2`("AI의 실물 증거는 한국 메모리")와 `ai_semi_3`("한국 메모리가 미국 AI
    # 서사와 따로 재평가된다")은 둘 다 한·미 메모리의 **차이**를 주장하는데, 정작 비교
    # 대상인 미국 메모리가 없어서 삼성·하이닉스를 SOX(반도체 전반)와만 견줬다. SOX에는
    # 엔비디아·TSMC 같은 로직이 섞여 있어 "메모리만의 재평가"를 가릴 수 없다.
    #
    # 계기가 된 것은 CEO가 가져온 외부 서사(2026-08-14, 샌디스크 주주환원)였는데, 그때
    # 샌디스크가 관측군에 없어 **검증 자체가 불가능했다.** 같은 질문이 또 온다.
    #
    # 둘을 함께 넣는 이유: SNDK만 넣으면 다음에 "마이크론은?"이 나온다. MU는 DRAM·NAND를
    # 다 하는 미국 유일 종합 메모리라 삼성·하이닉스의 정면 비교 대상이고, SNDK는 NAND
    # 전업이라 스토리지 축을 따로 읽는다.
    #
    # ⚠️ 딸려오는 비용(의도한 것): `US_CORE_TICKERS`가 자동으로 이 둘을 포함하므로
    # SEC 재무·공시가 매 수집에 따라붙고, 분기 실적 읽을 것이 2건 는다. 2026-08-02에
    # EQIX·POSCO를 넣을 때 CEO가 같은 비용을 "의도된 것"으로 판단한 전례를 따른다.
    _sym("MU", "US", "US", "equity", "Micron Technology", True, sector="반도체·공급망",
         name_ko="마이크론"),
    _sym("SNDK", "US", "US", "equity", "SanDisk", True, sector="반도체·공급망",
         name_ko="샌디스크"),
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
    # --- 테마 축 (2026-08-20, CEO 지시) --------------------------------
    #
    # **`sector_index`가 아니라 `index`다 — 의도적이다.** 업종 지수 표는
    # GICS 11개처럼 **겹치지 않고 시장을 빠짐없이 나누는** 분류만 실어야 한다.
    # 테마는 서로 겹치므로(IBM은 양자·소프트웨어·IT에 동시에 들어간다) 그 표에
    # 섞으면 같은 사건이 여러 줄로 신고된다. 여기서는 시장 반응 표에 실린다.
    #
    # 고른 기준은 **"이 축이 크게 움직인 날 중, 기존 축이 전부 조용했던 날의
    # 비율"**이다(682거래일 실측). 상관계수보다 이쪽이 직접적이다 —
    # 비교 기준은 이미 쓰는 SOX 26% · 러셀2000 19%.
    #   IGV  42%  거래대금 425M$  반도체와 상관 0.50  <- 후보 중 최고
    #   ITA  38%  거래대금  79M$  산업재와  상관 0.80
    #   BOTZ 16%  거래대금  29M$  XLK와    상관 0.84  <- 로봇 ETF 4개가 전부
    #        13~16%였다. 고르기의 문제가 아니라 로봇 축 자체가 기존 축과 겹친다.
    #
    # 넣지 않은 것: AIQ(XLK와 0.94, 거의 재탕) · QTUM(SOX와 0.88) ·
    # ARKX·UFO(순수 우주지만 거래대금 1~2M$로 너무 얇다 — 거래가 없어 튄 가격이
    # "유별난 날"로 잡힌다). **그래서 순수 우주 테마는 여전히 못 본다.**
    _sym("IGV", "US", "US", "index", "iShares Expanded Tech-Software ETF", name_ko="소프트웨어·플랫폼"),
    _sym("ITA", "US", "US", "index", "iShares U.S. Aerospace & Defense ETF", name_ko="항공우주·방산"),
    _sym("BOTZ", "US", "US", "index", "Global X Robotics & AI ETF", name_ko="로보틱스·AI"),
    _sym("091160.KS", "KR", "KR", "sector_index", "KODEX Semiconductor", name_ko="KODEX 반도체"),
    _sym("227540.KS", "KR", "KR", "sector_index", "TIGER Health Care", name_ko="TIGER 헬스케어"),
    _sym("117460.KS", "KR", "KR", "sector_index", "KODEX Energy & Chemicals", name_ko="KODEX 에너지화학"),
    _sym("102970.KS", "KR", "KR", "sector_index", "KODEX Securities", name_ko="KODEX 증권"),
    _sym("117680.KS", "KR", "KR", "sector_index", "KODEX Steel", name_ko="KODEX 철강"),
    # 2026-08-12 추가 — 한국 업종 계기의 공백을 메운다.
    #
    # 왜: CEO의 목표는 "그 시기 가장 주도적인 섹터"를 재는 것인데, 계기가 없는
    # 업종은 아무리 앞서도 **영원히 안 보인다.** 그런데 이 셋의 대표 종목
    # (한화에어로스페이스·HD현대중공업·두산에너빌리티)은 이미 코스피 상위 20
    # 수급 표본에 들어 있어 **수급은 매일 수집되는데 업종으로는 못 읽는** 상태였다.
    #
    # 티커는 추측하지 않고 두드려 확인했다. 실제로 두 번 틀렸다 — `385600.KS`는
    # 방산이 아니라 2차전지, `102960.KS`는 기계장비가 아니라 조선이었다. 영문명이
    # 모호한 `449450.KS`("ARIRANG Defensive")는 방어주인지 방산인지 이름으로는
    # 갈리지 않아 **일간 수익률 상관으로 확인**했다(1년): 한화에어로스페이스와
    # 0.83인데 KOSPI와는 0.52 — 방산이 맞다. 조선 0.88(HD현대중공업), 전력설비
    # 0.71(두산에너빌리티)도 같은 방법으로 확인했다.
    #
    # 같은 업종 후보가 여럿일 때는 **거래량**으로 골랐다. 계기는 그 업종을 faithful
    # 하게 따라가야 하는데 거래가 얇으면 지수가 아니라 그 ETF의 사정을 재게 된다
    # (조선: 466920 일평균 169만주 vs 102960 1.1만주).
    _sym("449450.KS", "KR", "KR", "sector_index", "PLUS K-Defense", name_ko="방산"),
    _sym("466920.KS", "KR", "KR", "sector_index", "SOL Shipbuilding Top3 Plus", name_ko="조선"),
    _sym("487240.KS", "KR", "KR", "sector_index", "KODEX AI Power Core Facilities", name_ko="AI전력설비"),
    # --- 2026-08-20 추가 (CEO 승인) ------------------------------------
    #
    # **한국 업종 커버리지를 메운다.** 미국은 SPDR 11개가 GICS 11개 섹터를 전부
    # 덮어 섹터 수준에서 이미 100%인데, 한국은 테마 ETF 8개뿐이라 운수장비·건설·
    # 은행·IT·2차전지·미디어가 통째로 비어 있었다. 그 업종이 아무리 크게
    # 움직여도 이 시스템은 그 사실조차 말할 수 없었다.
    #
    # 공식 경로(KRX 오픈API `idx/kospi_dd_trd`, 코스피 업종지수 21개)는 실측 결과
    # **401 Unauthorized**다 — 지금 KRX 키에 「주식」 권한만 있고 「지수」 권한이
    # 없다(같은 키로 `sto/stk_bydd_trd`는 200·942행 정상). 지수 서비스를 추가
    # 신청하면 이 ETF들을 지수로 갈아탈 수 있다.
    #
    # **거래대금으로 걸렀다.** 얇은 ETF는 거래가 없어 튄 가격이 「유별난 날」로
    # 잡혀 사각지대 탐지기가 거짓 경보를 낸다. 기존 최저치(TIGER 헬스케어
    # 일평균 9.4억)를 기준선으로 삼아 그 아래는 넣지 않았다 — 그래서 유통/소비
    # 축은 여전히 빈다(TIGER 200 생활소비재 0.7억, 경기소비재 2.3억).
    # 통신도 비운다(TIGER 방송통신은 관측이 1일뿐).
    _sym("139260.KS", "KR", "KR", "sector_index", "TIGER 200 IT", name_ko="IT"),
    _sym("091180.KS", "KR", "KR", "sector_index", "KODEX Autos", name_ko="자동차"),
    _sym("091170.KS", "KR", "KR", "sector_index", "KODEX Banks", name_ko="은행"),
    _sym("117700.KS", "KR", "KR", "sector_index", "KODEX Construction", name_ko="건설"),
    _sym("228810.KS", "KR", "KR", "sector_index", "TIGER Media & Contents", name_ko="미디어"),
    _sym("266370.KS", "KR", "KR", "sector_index", "KODEX Secondary Battery", name_ko="2차전지"),
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

# --- 한국 수급 표본 ---------------------------------------------------------
#
# **시장 전체 수급은 못 구한다.** 2026-08-04까지 두드려 본 경로가 전부 막혔다:
# pykrx는 KRX 회원 전용으로 잠겼고, KRX 오픈API에는 수급 엔드포인트가 없으며,
# KIS의 시장 단위 엔드포인트(`inquire-investor-daily-by-market`)는 지수 시세는
# 주지만 **투자자 필드가 300거래일 내내 전부 0**이다(이 계정 미제공).
# 종목별 수급만 나온다.
#
# 그래서 "시장 전체" 대신 **시가총액 상위 20종목의 합**을 쓴다(CEO 결정
# 2026-08-04). 코스피 전체 시가총액의 69.5%를 덮으므로 방향을 읽기에는 충분하지만
# **시장 전체가 아니다** — 화면에서도 "표본"이라고 쓴다. 시장이라고 쓰는 순간
# 나중에 "코스피 수급"으로 읽고 판단하게 된다.
#
# 우선주는 뺀다(CEO 결정): 같은 회사가 두 번 세어지고, 우선주는 투자자 구성이
# 보통주와 달라 방향을 흐린다. 거르는 기준은 **종목코드 끝자리**다 — 보통주는
# `0`으로 끝난다(실측: 코스피 943종목 중 833개). 이름으로 `우`를 찾으면
# `CJ4우(전환)`·`아모레퍼시픽홀딩스3우C`처럼 접미사가 붙은 3종목을 놓친다.
#
# **손으로 적은 목록이 아니다.** `market-intel krx top-kospi`가 KRX 전종목
# 시세(`MKTCAP`)에서 뽑아 이 블록을 그대로 출력한다. 순위는 천천히 바뀌므로
# 분기마다 다시 돌려 갱신하면 된다 — 갱신하면 git diff에 남는다.
#
# 기준일 2026-08-03 · 코스피 보통주 833개 중 상위 20 · 시총 비중 69.5%
KOSPI_FLOW_SAMPLE: tuple[tuple[str, str], ...] = (
    ("005930.KS", "삼성전자"),
    ("000660.KS", "SK하이닉스"),
    ("402340.KS", "SK스퀘어"),
    ("009150.KS", "삼성전기"),
    ("005380.KS", "현대차"),
    ("373220.KS", "LG에너지솔루션"),
    ("207940.KS", "삼성바이오로직스"),
    ("105560.KS", "KB금융"),
    ("032830.KS", "삼성생명"),
    ("028260.KS", "삼성물산"),
    ("000270.KS", "기아"),
    ("329180.KS", "HD현대중공업"),
    ("055550.KS", "신한지주"),
    ("012450.KS", "한화에어로스페이스"),
    ("012330.KS", "현대모비스"),
    ("034020.KS", "두산에너빌리티"),
    ("068270.KS", "셀트리온"),
    ("034730.KS", "SK"),
    ("086790.KS", "하나금융지주"),
    ("035420.KS", "NAVER"),
)
KOSPI_FLOW_SAMPLE_AS_OF = "2026-08-03"
KOSPI_FLOW_SAMPLE_COVERAGE_PCT = 69.5
KOSPI_FLOW_SAMPLE_NAMES: dict[str, str] = dict(KOSPI_FLOW_SAMPLE)
KOSPI_FLOW_SAMPLE_SYMBOLS: list[str] = [s for s, _ in KOSPI_FLOW_SAMPLE]
