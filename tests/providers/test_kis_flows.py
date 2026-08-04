"""한국투자증권 KIS — 국내주식 투자자별 매매동향 provider.

네트워크를 타지 않는다: `httpx.MockTransport`로 고정 응답을 준다.

이 파일이 지키는 계약 중 하나는 기능이 아니라 **안전장치**다: 같은 자격증명에
주문 API가 붙어 있으므로, 이 provider가 조회(quotations) 경로 밖으로 나가지
않는다는 것을 테스트로 못박는다. 이 프로젝트는 매매를 하지 않는다.

`from conftest import` 금지 — 헬퍼는 이 파일 안에 있다(검수서 F12).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from market_intel.config import Settings
from market_intel.http_client import SafeHttp
from market_intel.models import CollectContext
from market_intel.providers.kis_flows import KisFlowsProvider

FAKE_KEY, FAKE_SECRET = "FAKEAPPKEY123456", "FAKEAPPSECRET7890+/="

# 실제 응답에서 옮긴 모양 (2026-08-03, 삼성전자 20260731).
# `_pbmn` 계열은 **백만원 단위**다 — 원으로 읽으면 리포트가 "외국인 순매수
# 210만원"이라고 쓴다(실측: 8,359,011주 x 262,500원 ~= 2.19조원).
SAMPLE_ROW = {
    "stck_bsop_date": "20260731", "stck_clpr": "262500", "prdy_ctrt": "26.81",
    "acml_vol": "58478873", "acml_tr_pbmn": "14924241538350",
    "frgn_ntby_qty": "8359011", "prsn_ntby_qty": "-11681307", "orgn_ntby_qty": "3618959",
    "frgn_ntby_tr_pbmn": "2099936", "prsn_ntby_tr_pbmn": "-2958633",
    "orgn_ntby_tr_pbmn": "935049",
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(db_path=str(tmp_path / "t.db"), raw_dir=str(tmp_path / "raw"),
                 log_dir=str(tmp_path / "logs"))
    s.kis_app_key, s.kis_app_secret = FAKE_KEY, FAKE_SECRET
    return s


def _ctx(settings, handler, universe=None):
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=universe or [], logger=logging.getLogger("test"),
    )


def _handler(rows=None, *, token="TOKEN-ABC", capture=None):
    rows = SAMPLE_ROW if rows is None else rows

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if capture is not None:
            capture.append((request.method, url, dict(request.headers)))
        if "/oauth2/tokenP" in url:
            return httpx.Response(200, json={"access_token": token, "expires_in": 86400})
        if "investor-trade-by-stock-daily" in url:
            body = rows if isinstance(rows, list) else [rows]
            return httpx.Response(200, json={"rt_cd": "0", "msg1": "정상처리 되었습니다.",
                                             "output2": body})
        return httpx.Response(404, json={"rt_cd": "1", "msg1": "not found"})

    return handle


# --- 안전장치: 매매를 하지 않는다 --------------------------------------------

def test_provider_only_ever_calls_quotation_paths(settings):
    """같은 앱키에 주문 API가 붙어 있다. 이 프로젝트는 매매를 하지 않으므로
    조회 경로 밖으로 나가는 순간을 테스트가 잡는다."""
    calls: list = []
    meta = {"symbol": "005930.KS", "market": "KR", "country": "KR",
            "asset_type": "equity", "core16": True, "name": "Samsung", "name_ko": "삼성전자",
            "unit": None, "sector": "반도체·공급망"}
    KisFlowsProvider().collect(_ctx(settings, _handler(capture=calls), [meta]))

    assert calls, "요청이 한 건도 없다"
    for method, url, _h in calls:
        assert "/oauth2/tokenP" in url or "/uapi/domestic-stock/v1/quotations/" in url, (
            f"조회 경로가 아닌 곳을 불렀다: {method} {url}")
        assert "order" not in url.lower(), f"주문 경로: {url}"
        assert "trading" not in url.lower() or "investor-trade" in url, url


def test_order_paths_are_refused_even_if_asked(settings):
    """가드가 URL 문자열 검사에 그치지 않고 실제로 막는지."""
    from market_intel.providers import kis_flows

    with pytest.raises(ValueError, match="조회"):
        kis_flows._quotation_url("/uapi/domestic-stock/v1/trading/order-cash")


# --- 수급 사실 ---------------------------------------------------------------

def _facts(settings, rows=None, subject="005930.KS"):
    """-> (결과, 그 종목의 metric->fact).

    metric으로만 키를 잡으면 안 된다: provider는 관측 기업 외에 수급 표본
    20종목도 함께 도는데(`universe.KOSPI_FLOW_SAMPLE`), 가짜 응답이 모든
    종목에 같은 값을 주므로 마지막 종목이 앞의 것을 전부 덮어쓴다."""
    meta = {"symbol": "005930.KS", "market": "KR", "country": "KR",
            "asset_type": "equity", "core16": True, "name": "Samsung", "name_ko": "삼성전자",
            "unit": None, "sector": "반도체·공급망"}
    result = KisFlowsProvider().collect(_ctx(settings, _handler(rows), [meta]))
    return result, {f.metric: f for f in result.facts if f.subject == subject}


def test_net_buy_quantities_become_facts(settings):
    result, by_metric = _facts(settings)
    assert result.status in ("OK", "PARTIAL"), result.safe_detail
    assert by_metric["net_buy_foreign"].value_num == 8359011
    assert by_metric["net_buy_individual"].value_num == -11681307
    assert by_metric["net_buy_institution"].value_num == 3618959
    for m in ("net_buy_foreign", "net_buy_individual", "net_buy_institution"):
        fc = by_metric[m]
        assert fc.unit == "shares", f"{m} 단위가 주식 수가 아니다: {fc.unit}"
        assert fc.subject == "005930.KS"
        assert fc.category == "flow"
        assert fc.event_at == "2026-07-31T06:30:00+00:00", "한국 장 마감(15:30 KST)"


def test_amounts_are_converted_from_millions(settings):
    """`_pbmn`은 백만원 단위다. 원으로 읽으면 2.1조가 210만원이 된다."""
    _result, by_metric = _facts(settings)
    fc = by_metric["net_buy_foreign_value"]
    assert fc.value_num == 2099936 * 1_000_000, fc.value_num
    assert fc.unit == "KRW"


def test_no_credentials_never_touches_the_network(settings):
    settings.kis_app_key = ""
    called: list = []
    result = KisFlowsProvider().collect(_ctx(settings, lambda r: called.append(r) or httpx.Response(200)))
    assert result.status == "NO_DATA" and result.reason_code == "키없음", result.safe_detail
    assert called == []


def test_credentials_never_appear_in_stored_urls(settings):
    """저장되는 `safe_source_url`에 앱키·시크릿이 있으면 DB에 평문으로 남는다."""
    result, _ = _facts(settings)
    for item in result.raw_items:
        assert FAKE_KEY not in item.safe_source_url
        assert FAKE_SECRET not in item.safe_source_url
    for fc in result.facts:
        assert FAKE_KEY not in json.dumps(fc.extra or {}, ensure_ascii=False)


def test_error_response_is_reported_not_swallowed(settings):
    def handle(request: httpx.Request) -> httpx.Response:
        if "/oauth2/tokenP" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 86400})
        return httpx.Response(200, json={"rt_cd": "1", "msg1": "일시적 오류"})

    meta = {"symbol": "005930.KS", "market": "KR", "country": "KR", "asset_type": "equity",
            "core16": True, "name": "S", "name_ko": "삼성전자", "unit": None, "sector": "반도체·공급망"}
    result = KisFlowsProvider().collect(_ctx(settings, handle, [meta]))
    assert result.status in ("NO_DATA", "PARTIAL")
    assert "일시적 오류" in result.safe_detail or "rt_cd" in result.safe_detail, result.safe_detail


def test_token_is_fetched_once_for_many_symbols(settings):
    """토큰은 하루 유효다. 종목마다 새로 받으면 KIS가 호출 제한을 건다."""
    calls: list = []
    metas = [{"symbol": s, "market": "KR", "country": "KR", "asset_type": "equity",
              "core16": True, "name": s, "name_ko": s, "unit": None, "sector": "반도체·공급망"}
             for s in ("005930.KS", "000660.KS", "005380.KS")]
    KisFlowsProvider().collect(_ctx(settings, _handler(capture=calls), metas))
    token_calls = [u for _m, u, _h in calls if "/oauth2/tokenP" in u]
    assert len(token_calls) == 1, f"토큰을 {len(token_calls)}번 받았다"


def test_base_date_backs_off_before_the_investor_cutoff():
    """KIS는 당일 수급을 15:40 KST 전에는 안 준다 — `rt_cd=2 TIME LIMIT 00:00 ~ 15:40`
    (실측 2026-08-03). 이걸 안 막으면 아침 수집(06:50)이 매일 0건이 된다."""
    from market_intel.providers.kis_flows import _base_date

    # 06:50 KST = 전날 21:50 UTC -> 전날을 물어야 한다
    assert _base_date(datetime(2026, 8, 2, 21, 50, tzinfo=timezone.utc)) == "20260802"
    # 15:39 KST — 아직 이르다
    assert _base_date(datetime(2026, 8, 3, 6, 39, tzinfo=timezone.utc)) == "20260802"
    # 15:40 KST — 이제 당일치가 나온다
    assert _base_date(datetime(2026, 8, 3, 6, 40, tzinfo=timezone.utc)) == "20260803"
    # 16:15 KST 장마감 수집
    assert _base_date(datetime(2026, 8, 3, 7, 15, tzinfo=timezone.utc)) == "20260803"


def test_token_is_cached_on_disk_and_reused(settings, tmp_path):
    """KIS는 토큰 발급에 빈도 제한을 건다 — 실측 2026-08-03: 짧은 시간에 몇 번
    받았더니 발급이 HTTP 403으로 막혔다. 토큰은 24시간 유효하므로 재사용한다.
    안 그러면 수집이 며칠 뒤 통째로 멈춘다."""
    import os
    import stat

    from market_intel.providers import kis_flows

    calls: list = []
    meta = {"symbol": "005930.KS", "market": "KR", "country": "KR", "asset_type": "equity",
            "core16": True, "name": "S", "name_ko": "삼성전자", "unit": None, "sector": "반도체·공급망"}

    KisFlowsProvider().collect(_ctx(settings, _handler(capture=calls), [meta]))
    assert len([u for _m, u, _h in calls if "/oauth2/tokenP" in u]) == 1

    # 두 번째 수집은 캐시를 써서 발급을 건너뛴다
    calls.clear()
    KisFlowsProvider().collect(_ctx(settings, _handler(capture=calls), [meta]))
    assert [u for _m, u, _h in calls if "/oauth2/tokenP" in u] == [], "토큰을 또 받았다"

    cache = kis_flows._token_cache_path(settings)
    assert cache.exists()
    mode = stat.S_IMODE(os.stat(cache).st_mode)
    assert mode == 0o600, f"토큰 캐시 권한이 {oct(mode)} — 소유자만 읽어야 한다"


def test_expired_cache_is_not_reused(settings):
    """만료된 토큰을 그대로 쓰면 수집이 401로 죽는다."""
    import time

    from market_intel.providers import kis_flows

    kis_flows._write_cached_token(settings, "OLD", expires_in=1)
    path = kis_flows._token_cache_path(settings)
    path.write_text(json.dumps({"access_token": "OLD", "expires_at": time.time() - 5}),
                    encoding="utf-8")
    assert kis_flows._read_cached_token(settings) is None


def test_flow_sample_symbols_are_collected_too(settings):
    """시장 전체 수급을 주는 경로가 없어 코스피 상위 20종목 합으로 대신한다
    (`universe.KOSPI_FLOW_SAMPLE`). 그 종목들이 수집되지 않으면 합계 막대가
    관측 기업 5개만 더한 것이 되고, 화면은 그것을 '상위 20종목 합계'라고
    부른다 — 숫자가 조용히 틀린다."""
    from market_intel.universe import KOSPI_FLOW_SAMPLE_SYMBOLS

    result, _ = _facts(settings)
    collected = {f.subject for f in result.facts}
    missing = set(KOSPI_FLOW_SAMPLE_SYMBOLS) - collected
    assert not missing, f"표본에서 빠진 종목: {sorted(missing)}"
    # 관측 기업도 그대로 있어야 한다 — 표본이 관측을 대체하는 것이 아니다.
    assert "005930.KS" in collected
