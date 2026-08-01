"""ST3 acceptance tests for dart.py: corp_code resolved from a mocked
corpCode.xml zip, list.json filing detection, fnlttSinglAcntAll.json
financial mapping, and crtfc_key masking (query-param name match)."""
import io
import zipfile
from datetime import datetime, timezone

import httpx

from market_intel.models import CollectContext
from market_intel.providers.dart import KR_CORE4, DartProvider

FAKE_KEY = "FAKEDARTKEY789"

CORP_CODE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><stock_code>005930</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00164779</corp_code><corp_name>SK하이닉스</corp_name><stock_code>000660</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00164742</corp_code><corp_name>KB금융</corp_name><stock_code>105560</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00164780</corp_code><corp_name>현대차</corp_name><stock_code>005380</stock_code><modify_date>20260101</modify_date></list>
  <list><corp_code>00999999</corp_code><corp_name>기타회사</corp_name><stock_code></stock_code><modify_date>20260101</modify_date></list>
</result>"""


def _corp_code_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", CORP_CODE_XML)
    return buf.getvalue()


def _ctx(settings, http_factory):
    return CollectContext(
        cutoff=datetime.now(timezone.utc), now=datetime.now(timezone.utc),
        settings=settings, http=http_factory, universe=[], logger=__import__("logging").getLogger("t"),
    )


def test_no_key_means_zero_http_calls(settings):
    settings.dart_api_key = ""

    def boom(_name):
        raise AssertionError("SafeHttp factory must not be invoked when the key is empty")

    result = DartProvider().collect(_ctx(settings, boom))
    assert result.status == "NO_DATA"
    assert result.reason_code == "키없음"
    assert result.raw_items == [] and result.facts == []


SAMSUNG_CORP_CODE = "00126380"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "corpCode.xml" in url:
        return httpx.Response(200, content=_corp_code_zip_bytes(), headers={"content-type": "application/zip"})

    corp_code = request.url.params.get("corp_code")
    if "list.json" in url:
        if corp_code != SAMSUNG_CORP_CODE:
            return httpx.Response(200, json={"status": "013", "message": "조회된 데이터가 없습니다"})
        return httpx.Response(
            200,
            json={
                "status": "000", "message": "정상",
                "list": [
                    {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930",
                     "report_nm": "분기보고서 (2026.06)", "rcept_no": "20260814000123", "rcept_dt": "20260814"}
                ],
            },
        )
    if "fnlttSinglAcntAll.json" in url:
        if corp_code != SAMSUNG_CORP_CODE:
            return httpx.Response(200, json={"status": "013", "message": "조회된 데이터가 없습니다"})
        return httpx.Response(
            200,
            json={
                "status": "000", "message": "정상",
                "list": [
                    {"sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "300,870,900,000,000"},
                    {"sj_div": "IS", "account_nm": "영업이익", "thstrm_amount": "6,566,700,000,000"},
                ],
            },
        )
    return httpx.Response(404)


def test_fake_key_maps_filing_and_financial_facts(settings):
    settings.dart_api_key = FAKE_KEY

    def http_factory(name):
        from market_intel.http_client import SafeHttp
        return SafeHttp(name, settings, transport=httpx.MockTransport(_handler))

    result = DartProvider().collect(_ctx(settings, http_factory))

    assert result.status == "PARTIAL"  # only Samsung has fixture data; other 3 core companies miss
    subjects = {f.subject for f in result.facts}
    assert "005930.KS" in subjects
    metrics = {(f.subject, f.metric) for f in result.facts}
    assert ("005930.KS", "filing_event") in metrics
    assert ("005930.KS", "revenue") in metrics
    assert ("005930.KS", "operating_income") in metrics

    revenue_fact = next(f for f in result.facts if f.subject == "005930.KS" and f.metric == "revenue")
    assert revenue_fact.value_num == 300870900000000.0
    assert revenue_fact.unit == "KRW"
    assert revenue_fact.data_status == "source_verified"


def test_fake_key_never_appears_in_stored_safe_url(settings, caplog):
    settings.dart_api_key = FAKE_KEY

    def http_factory(name):
        from market_intel.http_client import SafeHttp
        return SafeHttp(name, settings, transport=httpx.MockTransport(_handler))

    result = DartProvider().collect(_ctx(settings, http_factory))

    for raw in result.raw_items:
        assert FAKE_KEY not in raw.safe_source_url
    assert FAKE_KEY not in caplog.text


def test_corp_code_never_hardcoded_only_kr_core4_constant():
    # KR_CORE4 lists the 4 target companies; corp_code values themselves
    # must come only from the runtime XML resolution (never literals here).
    assert set(KR_CORE4.keys()) == {"005930", "000660", "105560", "005380"}
    for stock_code, (subject, _name) in KR_CORE4.items():
        assert subject == f"{stock_code}.KS"


def test_filing_event_time_is_valid_iso8601(settings):
    """DART returns rcept_dt as YYYYMMDD; storing it verbatim produced the
    invalid timestamp `20260814T00:00:00+00:00` and a mangled fact_id."""
    settings.dart_api_key = FAKE_KEY

    def http_factory(name):
        from market_intel.http_client import SafeHttp
        return SafeHttp(name, settings, transport=httpx.MockTransport(_handler))

    result = DartProvider().collect(_ctx(settings, http_factory))

    filing = next(f for f in result.facts if f.metric == "filing_event")
    assert filing.event_at == "2026-08-14T00:00:00+00:00"
    datetime.fromisoformat(filing.event_at)  # must parse
