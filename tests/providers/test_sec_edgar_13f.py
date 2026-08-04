"""Offline unit tests for 13F-HR filing detection (repair.md finding #4).

Response shapes are modeled on real SEC browse-edgar atom output verified
live against https://www.sec.gov/cgi-bin/browse-edgar during this repair
(single-company mode for Berkshire/Pershing Square/Lone Pine/TCI, and
genuine multi-company ambiguity for "baupost group" -> two CIKs, one
inactive since 2010, one still filing)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pytest

from market_intel.http_client import SafeHttp
from market_intel.models import CollectContext
from market_intel.providers.sec_edgar_13f import TRACKED_MANAGERS, Sec13fProvider

ATOM_HEADER = '<?xml version="1.0" encoding="ISO-8859-1" ?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
ATOM_FOOTER = "</feed>"


def _single_company_atom(cik: str, filings: list[tuple[str, str, str]]) -> str:
    """filings: list of (accession_number, filing_date, filing_type), most
    recent first -- matches the real feed's own ordering."""
    entries = "".join(
        f"""
        <entry>
          <content type="text/xml">
            <accession-number>{acc}</accession-number>
            <filing-date>{date}</filing-date>
            <filing-type>{ftype}</filing-type>
            <filing-href>https://www.sec.gov/Archives/edgar/data/{cik.lstrip("0")}/{acc.replace("-", "")}/{acc}-index.htm</filing-href>
          </content>
        </entry>"""
        for acc, date, ftype in filings
    )
    return f"""{ATOM_HEADER}
      <company-info><cik>{cik}</cik></company-info>{entries}
    {ATOM_FOOTER}"""


def _multi_company_atom(ciks: list[str]) -> str:
    entries = "".join(
        f"""
        <entry>
          <content type="text/xml">
            <company-info><cik>{cik}</cik></company-info>
          </content>
        </entry>"""
        for cik in ciks
    )
    return f"{ATOM_HEADER}{entries}{ATOM_FOOTER}"


def _empty_atom() -> str:
    return f"{ATOM_HEADER}{ATOM_FOOTER}"


BERKSHIRE_ATOM = _single_company_atom(
    "0001067983",
    [("0001193125-26-226661", "2026-05-15", "13F-HR")],
)
PERSHING_ATOM = _single_company_atom("0001336528", [("0000000000-26-000001", "2026-05-15", "13F-HR")])
LONE_PINE_ATOM = _single_company_atom("0001061165", [("0000000000-26-000002", "2026-05-15", "13F-HR")])
TCI_ATOM = _single_company_atom("0001647251", [("0000000000-26-000003", "2026-05-15", "13F-HR")])

# Baupost: name search is ambiguous (real SEC behavior) -- one CIK inactive
# since 2010, one still filing. Its own by-CIK atom is single-company mode.
BAUPOST_SEARCH_ATOM = _multi_company_atom(["0001054420", "0001061768"])
BAUPOST_INACTIVE_ATOM = _empty_atom()  # 0001054420: no 13F-HR entries at all
BAUPOST_ACTIVE_ATOM = _single_company_atom("0001061768", [("0000000000-26-000004", "2026-05-15", "13F-HR")])


def _make_ctx(settings, handler) -> CollectContext:
    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


# 문서 디렉터리 응답. 실측 구조 그대로다: index.json이 파일 목록을 주고,
# 정보표 파일 이름은 제출마다 다르며(`53405.xml`·`infotable.xml`…), 표지
# `primary_doc.xml`이 기준일과 표 합계를 들고 있다.
INDEX_JSON = ('{"directory": {"item": ['
              '{"name": "primary_doc.xml"}, {"name": "53405.xml"}, {"name": "note.txt"}]}}')
INFO_TABLE = (
    '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
    "<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>"
    "<cusip>037833100</cusip><value>600000000</value>"
    "<shrsOrPrnAmt><sshPrnamt>3000000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
    "</infoTable>"
    "<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>"
    "<cusip>037833100</cusip><value>400000000</value>"
    "<shrsOrPrnAmt><sshPrnamt>2000000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
    "</infoTable></informationTable>")
PRIMARY_DOC = (
    '<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">'
    "<headerData><filerInfo><periodOfReport>03-31-2026</periodOfReport></filerInfo></headerData>"
    "<formData><summaryPage><tableEntryTotal>2</tableEntryTotal>"
    "<tableValueTotal>1000000000</tableValueTotal></summaryPage></formData></edgarSubmission>")


def _route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/index.json"):
        return httpx.Response(200, text=INDEX_JSON)
    if path.endswith("/primary_doc.xml"):
        return httpx.Response(200, text=PRIMARY_DOC)
    if path.endswith("/53405.xml"):
        return httpx.Response(200, text=INFO_TABLE)
    if path.endswith("/note.txt"):
        return httpx.Response(200, text="not xml")

    params = dict(request.url.params)
    cik = params.get("CIK")
    company = params.get("company")

    if cik == "0001067983":
        return httpx.Response(200, text=BERKSHIRE_ATOM)
    if cik == "0001336528" or company == "pershing square capital":
        return httpx.Response(200, text=PERSHING_ATOM)
    if cik == "0001061165" or company == "lone pine capital":
        return httpx.Response(200, text=LONE_PINE_ATOM)
    if cik == "0001647251" or company == "tci fund management":
        return httpx.Response(200, text=TCI_ATOM)
    if company == "berkshire hathaway":
        return httpx.Response(200, text=BERKSHIRE_ATOM)
    if company == "baupost group":
        return httpx.Response(200, text=BAUPOST_SEARCH_ATOM)
    if cik == "0001054420":
        return httpx.Response(200, text=BAUPOST_INACTIVE_ATOM)
    if cik == "0001061768":
        return httpx.Response(200, text=BAUPOST_ACTIVE_ATOM)
    return httpx.Response(404, text="not found")


@pytest.fixture
def result(settings):
    return Sec13fProvider().collect(_make_ctx(settings, _route))


def test_all_five_tracked_managers_are_detected(result):
    filings = {f.subject for f in result.facts if f.category == "13f_filing"}
    assert filings == {slug for slug, _, _ in TRACKED_MANAGERS}
    assert result.status == "OK", result.safe_detail


def test_holdings_are_read_from_the_filing_directory(result):
    """제출 감지에서 멈추지 않고 보유 표까지 읽는다. 정보표 파일 이름은 제출마다
    다르므로(`53405.xml`) 이름이 아니라 루트 태그로 고른 것이 여기서 증명된다."""
    values = [f for f in result.facts
              if f.category == "13f_holding" and f.metric == "holding_value"]
    assert {f.subject for f in values} == {
        f"{slug}/037833100" for slug, _, _ in TRACKED_MANAGERS}

    berkshire = next(f for f in values if f.subject.startswith("berkshire"))
    # 같은 종목 두 줄이 합산돼야 한다 — 안 하면 마지막 줄만 남는다.
    assert berkshire.value_num == 1_000_000_000
    assert berkshire.extra["merged_rows"] == 2
    assert berkshire.extra["issuer"] == "APPLE INC"
    # 보유 시점은 분기 말(periodOfReport)이지 제출일(05-15)이 아니다.
    assert berkshire.event_at.startswith("2026-03-31")
    assert berkshire.extra["period_of_report"] == "2026-03-31"

    amounts = [f for f in result.facts if f.metric == "holding_amount"]
    assert next(f for f in amounts if f.subject.startswith("berkshire")).value_num == 5_000_000


def test_a_manager_whose_table_disagrees_with_its_own_total_is_reported_missing(settings):
    """문서가 적어 둔 표 합계와 우리가 더한 값이 어긋나면 종목을 놓쳤거나
    중복해 더한 것이다 — 틀린 보유내역을 내느니 결측으로 신고한다."""
    bad_primary = PRIMARY_DOC.replace("<tableValueTotal>1000000000<", "<tableValueTotal>9999999999<")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/primary_doc.xml"):
            return httpx.Response(200, text=bad_primary)
        return _route(request)

    result = Sec13fProvider().collect(_make_ctx(settings, route))
    assert not [f for f in result.facts if f.category == "13f_holding"]
    assert "value_total_mismatch" in result.safe_detail
    # 보유내역을 못 읽었다고 제출 감지까지 잃으면 안 된다.
    assert len([f for f in result.facts if f.category == "13f_filing"]) == 5


def test_holdings_are_skipped_when_the_period_is_unknown(settings):
    """보유 시점을 모르면 실을 수 없다 — 제출일로 대신하면 3월 말 보유가
    5월 15일 사실로 둔갑한다."""
    no_period = PRIMARY_DOC.replace("<periodOfReport>03-31-2026</periodOfReport>", "")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/primary_doc.xml"):
            return httpx.Response(200, text=no_period)
        return _route(request)

    result = Sec13fProvider().collect(_make_ctx(settings, route))
    assert not [f for f in result.facts if f.category == "13f_holding"]
    assert "no_period_of_report" in result.safe_detail


def test_single_company_match_uses_most_recent_filing(result):
    berkshire = next(f for f in result.facts if f.subject == "berkshire_hathaway")
    assert berkshire.value_text == "0001193125-26-226661"
    assert berkshire.extra["form"] == "13F-HR"
    assert berkshire.event_at.startswith("2026-05-15")
    assert berkshire.category == "13f_filing"
    assert berkshire.metric == "filing_event"
    assert berkshire.data_status == "source_verified"


def test_ambiguous_name_match_resolves_to_the_still_active_entity(result):
    """Two CIKs share the 'baupost group' search term -- the inactive one
    (no 13F-HR filings since 2010) must never be picked over the one that
    is still actually filing."""
    baupost = next(f for f in result.facts if f.subject == "baupost_group")
    assert baupost.value_text == "0000000000-26-000004"
    assert baupost.event_at.startswith("2026-05-15")


def test_no_recent_filing_is_reported_not_fabricated(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_empty_atom())

    result = Sec13fProvider().collect(_make_ctx(settings, handler))
    assert result.status == "NO_DATA"
    assert not result.facts
    assert "no_match" in result.safe_detail


def test_secret_free_source_urls_are_stored(result):
    for item in result.raw_items:
        assert item.safe_source_url.startswith("https://")
