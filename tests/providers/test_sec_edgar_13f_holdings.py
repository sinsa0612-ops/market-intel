"""13F 보유내역 파싱 — 실측(2026-08-04)으로 드러난 함정들.

여기서 지키는 것은 "파싱이 된다"가 아니라 **틀린 보유내역을 발행하지 않는다**
이다. 13F는 남의 돈이 어디 들어가 있는지를 말하는 문서라, 조용히 어긋난 숫자가
나가면 그것을 근거로 판단이 이뤄진다.
"""
from __future__ import annotations

import textwrap

from market_intel.providers import sec_edgar_13f as p

NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def info_table(*rows: str) -> str:
    return f'<informationTable xmlns="{NS}">{"".join(rows)}</informationTable>'


def row(cusip: str, value: str, shares: str, *, issuer="ACME CORP", unit="SH",
        put_call: str = "", other: str = "") -> str:
    pc = f"<putCall>{put_call}</putCall>" if put_call else ""
    om = f"<otherManager>{other}</otherManager>" if other else ""
    return textwrap.dedent(f"""
        <infoTable>
          <nameOfIssuer>{issuer}</nameOfIssuer><titleOfClass>COM</titleOfClass>
          <cusip>{cusip}</cusip><value>{value}</value>
          <shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt><sshPrnamtType>{unit}</sshPrnamtType></shrsOrPrnAmt>
          {pc}{om}
        </infoTable>""")


# --- 1) 같은 종목이 여러 줄로 쪼개져 온다 ------------------------------------

def test_same_cusip_split_across_submanagers_is_summed():
    """버크셔는 자회사 14곳을 대신 신고해서 ALLY 한 종목이 세 줄로 나뉜다
    (2026Q1 실측: 90줄). 합산하지 않으면 `_fact_id`가 같아 **마지막 줄만**
    남고 나머지 보유가 조용히 사라진다."""
    xml = info_table(
        row("02005N100", "498992850", "12719675", issuer="ALLY FINL INC", other="4"),
        row("02005N100", "109996016", "2803875", issuer="ALLY FINL INC", other="2,4,11"),
        row("02005N100", "165872286", "4228200", issuer="ALLY FINL INC", other="4,5"),
    )
    holdings = p.parse_information_table(xml)
    assert len(holdings) == 1
    h = holdings[0]
    assert h["value"] == 498992850 + 109996016 + 165872286
    assert h["amount"] == 12719675 + 2803875 + 4228200
    assert h["rows"] == 3, "몇 줄을 합쳤는지 남겨야 감사할 수 있다"


def test_shares_and_principal_are_never_summed_together():
    """주식 수(SH)와 원금액(PRN)은 더할 수 없는 양이다."""
    xml = info_table(row("111111111", "100", "10", unit="SH"),
                     row("111111111", "200", "20", unit="PRN"))
    holdings = p.parse_information_table(xml)
    assert {h["unit"] for h in holdings} == {"SH", "PRN"}
    assert len(holdings) == 2


def test_calls_and_puts_stay_apart():
    """콜과 풋은 방향이 반대라 합치면 순보유가 거짓이 된다."""
    xml = info_table(row("222222222", "100", "10"),
                     row("222222222", "300", "30", put_call="Put"))
    holdings = p.parse_information_table(xml)
    assert sorted(h["put_call"] for h in holdings) == ["", "Put"]


def test_unparseable_or_wrong_document_yields_nothing():
    """정보표가 아닌 XML을 정보표로 읽으면 없는 보유가 생긴다."""
    assert p.parse_information_table("not xml at all") == []
    assert p.parse_information_table("<edgarSubmission><formData/></edgarSubmission>") == []


def test_namespace_version_is_not_hardcoded():
    """스키마 판이 바뀌어도 읽혀야 한다 — 네임스페이스를 고정해 찾으면 판이
    하나 올라가는 순간 조용히 0건이 된다."""
    xml = info_table(row("333333333", "500", "5")).replace(NS, f"{NS}/2029")
    assert len(p.parse_information_table(xml)) == 1


# --- 2) 보유 시점 ------------------------------------------------------------

def test_cover_page_converts_the_us_date_and_carries_the_oracle():
    cover = p.parse_cover_page(
        '<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">'
        "<headerData><filerInfo><periodOfReport>03-31-2026</periodOfReport></filerInfo></headerData>"
        "<formData><summaryPage><tableEntryTotal>90</tableEntryTotal>"
        "<tableValueTotal>263095703570</tableValueTotal></summaryPage></formData>"
        "</edgarSubmission>")
    assert cover["period"] == "2026-03-31"
    assert cover["table_value_total"] == 263095703570
    assert cover["entry_total"] == 90


def test_unrecognised_period_is_blank_not_guessed():
    """보유 시점을 모르면 모른다고 해야 한다. 제출일로 대신하면 3월 말 보유가
    5월 15일 사실로 둔갑한다."""
    assert p._iso_period("03-31-2026") == "2026-03-31"
    for raw in ("2026-03-31", "", "31-03-26", "봄", "03/31/2026", "03-31-26"):
        assert p._iso_period(raw) == "", f"{raw!r}를 날짜로 지어냈다"


# --- 3) 금액 단위 ------------------------------------------------------------

def test_thousands_filer_is_detected_by_implied_price():
    """실측: Baupost는 천 달러 단위로 낸다(주당 환산가 중앙값 0.126달러).
    섞인 채로 실으면 버크셔 $263B 옆에 Baupost $5M이 나란히 선다."""
    baupost = [{"unit": "SH", "value": 649543, "amount": 3118754},
               {"unit": "SH", "value": 597208, "amount": 8080112},
               {"unit": "SH", "value": 393159, "amount": 2500000}]
    assert p.value_scale(baupost) == 1000.0


def test_dollar_filer_is_left_alone():
    berkshire = [{"unit": "SH", "value": 57843260493, "amount": 227917808},
                 {"unit": "SH", "value": 45859204536, "amount": 151610700}]
    assert p.value_scale(berkshire) == 1.0


def test_scale_defaults_to_one_when_it_cannot_be_judged():
    """모를 때 1000배 부풀리는 쪽으로 기울면 안 된다."""
    assert p.value_scale([]) == 1.0
    assert p.value_scale([{"unit": "PRN", "value": 1000, "amount": 0}]) == 1.0


def test_a_few_penny_stocks_do_not_flip_a_dollar_filer():
    """중앙값을 쓰는 이유 — 동전주가 섞여도 판정이 뒤집히면 안 된다."""
    mixed = [{"unit": "SH", "value": 5_000_000_000, "amount": 20_000_000},
             {"unit": "SH", "value": 3_000_000_000, "amount": 15_000_000},
             {"unit": "SH", "value": 100_000, "amount": 900_000},  # 0.11달러 동전주
             {"unit": "SH", "value": 80_000, "amount": 800_000}]
    assert p.value_scale(mixed) == 1.0
