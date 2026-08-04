"""KRX 전종목 -> 한국 시장 폭(breadth) provider 오프라인 테스트.

네트워크를 타지 않는다: `httpx.MockTransport`로 고정 응답을 준다.

`from conftest import` 금지 — 헬퍼는 이 파일 안에 있다(검수서 F12 관례,
`test_kis_flows.py`와 동일 모양).
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
from market_intel.providers.krx_breadth import KrxBreadthProvider, _compute

FAKE_KEY = "FAKEKRXKEY1234567890"


def _row(*, isu="000000", cmp_prev, fluc, vol, mktcap, trdval, mkt="KOSPI", bas_dd="20260803"):
    """spec §1의 필드 15개 모양을 흉내낸다(계산에 안 쓰는 필드는 자리만 채운다)."""
    return {
        "BAS_DD": bas_dd, "ISU_CD": isu, "ISU_NM": isu, "MKT_NM": mkt,
        "SECT_TP_NM": "", "TDD_CLSPRC": "0", "CMPPREVDD_PRC": str(cmp_prev),
        "FLUC_RT": str(fluc), "TDD_OPNPRC": "0", "TDD_HGPRC": "0", "TDD_LWPRC": "0",
        "ACC_TRDVOL": str(vol), "ACC_TRDVAL": str(trdval), "MKTCAP": str(mktcap),
        "LIST_SHRS": "0",
    }


# 5종목 표본 — 상승 1 · 하락 2(그중 1은 FLUC_RT<=-100) · 보합(거래됨) 1 · 거래없음 1.
# 손으로 검산 가능한 값만 쓴다:
#   median(traded fluc) = median([25,-10,0,-100]) = (-10+0)/2 = -5.0
#   cap 역산(제외 종목 빼고): now=200+90+50+40=380, prev=160+100+50+40=350
#     -> (380-350)/350*100 = 8.571428...%
#   turnover = 1000+500+300+0+10 = 1810
SAMPLE_ROWS = [
    _row(isu="000001", cmp_prev=50, fluc=25, vol=100, mktcap=200, trdval=1000),   # advancer
    _row(isu="000002", cmp_prev=-10, fluc=-10, vol=100, mktcap=90, trdval=500),   # decliner
    _row(isu="000003", cmp_prev=0, fluc=0, vol=100, mktcap=50, trdval=300),       # unchanged(거래됨)
    _row(isu="000004", cmp_prev=0, fluc=0, vol=0, mktcap=40, trdval=0),           # 거래없음
    _row(isu="000005", cmp_prev=-999, fluc=-100, vol=1, mktcap=5, trdval=10),     # 하락, cap계산 제외
]


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(db_path=str(tmp_path / "t.db"), raw_dir=str(tmp_path / "raw"),
                 log_dir=str(tmp_path / "logs"))
    s.krx_api_key = FAKE_KEY
    return s


def _ctx(settings, handler):
    # 2026-08-03 14:00 KST (장중) — 당일치가 비어 있는 상황을 그대로 재현.
    now = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


def _handler(kospi_rows=None, kosdaq_rows=None, *, capture=None, kosdaq_status=200):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if capture is not None:
            capture.append((request.method, url, dict(request.headers)))
        if "ksq_bydd_trd" in url:
            if kosdaq_status != 200:
                return httpx.Response(kosdaq_status)
            return httpx.Response(200, json={"OutBlock_1": kosdaq_rows if kosdaq_rows is not None else []})
        if "stk_bydd_trd" in url:
            return httpx.Response(200, json={"OutBlock_1": kospi_rows if kospi_rows is not None else []})
        return httpx.Response(404)

    return handle


def _by_date_handler(rows_by_date: dict[str, list[dict]], *, capture=None):
    """`basDd`에 따라 다른 응답 — lookback이 실제로 날짜를 거슬러 가는지 검증."""

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        bas_dd = dict(request.url.params).get("basDd", "")
        if capture is not None:
            capture.append((url, bas_dd))
        if "stk_bydd_trd" in url or "ksq_bydd_trd" in url:
            return httpx.Response(200, json={"OutBlock_1": rows_by_date.get(bas_dd, [])})
        return httpx.Response(404)

    return handle


# --- 계산 로직 (순수 함수 단위 테스트) ---------------------------------------

def test_no_trade_issues_are_split_from_unchanged_and_sum_identity_holds():
    """spec §3-1: 거래량 0은 보합이 아니라 별도 칸. 항등식은 항상 성립해야 한다."""
    metrics = _compute(SAMPLE_ROWS)
    advancers = metrics["breadth_advancers"][0]
    decliners = metrics["breadth_decliners"][0]
    unchanged = metrics["breadth_unchanged"][0]
    no_trade = metrics["breadth_no_trade"][0]

    assert (advancers, decliners, unchanged, no_trade) == (1, 2, 1, 1)
    assert advancers + decliners + unchanged + no_trade == len(SAMPLE_ROWS)


def test_median_uses_only_traded_issues():
    """spec §3-2: 거래 없는 종목의 0.00%를 섞으면 중앙값이 0쪽으로 끌린다.

    거래없음 행(FLUC_RT=0.00)을 포함하면 median([25,-10,0,0,-100]) = 0.0이
    나온다 — 분리했을 때의 -5.0과 다르다. 이 테스트는 -5.0을 요구한다."""
    metrics = _compute(SAMPLE_ROWS)
    assert metrics["breadth_median_change_pct"][0] == pytest.approx(-5.0)


def test_cap_weighted_index_excludes_fluc_at_or_below_minus_100():
    """spec §3-3: FLUC_RT<=-100은 역산 불가라 분자·분모 양쪽에서 뺀다.

    포함시키면 0으로 나누거나 부호가 뒤집혀 8.571...%와 다른 값이 나온다."""
    metrics = _compute(SAMPLE_ROWS)
    index_change_pct, unit, extra = metrics["index_change_pct"]
    assert index_change_pct == pytest.approx(30 / 350 * 100, rel=1e-9)
    assert unit == "%"
    assert extra["method"] == "cap_weighted_reconstructed"
    assert extra["excluded_count"] == 1, "FLUC_RT=-100인 종목 1건이 빠졌어야 한다"


def test_no_trade_issues_still_count_toward_cap_weighted_denominator():
    """거래없음 종목도 "전일과 같은 시총"으로 지수 분모에 들어간다 — 아예
    빼면 그 종목의 시총 비중이 사라져 지수가 왜곡된다."""
    metrics = _compute(SAMPLE_ROWS)
    # SAMPLE_ROWS 중 거래없음(mktcap=40)을 뺀 4종목만 남으면 분모가 350-40=310이 된다.
    without_no_trade = [r for r in SAMPLE_ROWS if r["ISU_CD"] != "000004"]
    without_metrics = _compute(without_no_trade)
    assert metrics["index_change_pct"][0] != without_metrics["index_change_pct"][0]


def test_turnover_sums_all_rows_including_no_trade():
    metrics = _compute(SAMPLE_ROWS)
    value, unit, _extra = metrics["turnover_value"]
    assert value == pytest.approx(1810.0)
    assert unit == "KRW"


def test_median_is_none_when_no_traded_issues():
    all_no_trade = [_row(isu="1", cmp_prev=0, fluc=0, vol=0, mktcap=10, trdval=0)]
    metrics = _compute(all_no_trade)
    assert metrics["breadth_median_change_pct"][0] is None


# --- provider.collect() 통합 테스트 ------------------------------------------

def test_no_credentials_never_touches_the_network(settings):
    settings.krx_api_key = ""
    called: list = []
    result = KrxBreadthProvider().collect(
        _ctx(settings, lambda r: called.append(r) or httpx.Response(200)))
    assert result.status == "NO_DATA" and result.reason_code == "키없음", result.safe_detail
    assert called == []


def test_holiday_empty_response_is_no_data_not_a_crash(settings):
    """휴장일은 오류가 아니라 빈 OutBlock_1(spec §1). lookback을 다 써도
    비어 있으면 NO_DATA/empty_response로 보고한다(조용히 죽지 않는다)."""
    result = KrxBreadthProvider().collect(_ctx(settings, _handler([], [])))
    assert result.status == "NO_DATA", result.safe_detail
    assert result.reason_code == "empty_response"
    assert "KOSPI" in result.safe_detail and "KOSDAQ" in result.safe_detail


def test_full_collect_reproduces_breadth_facts_for_both_markets(settings):
    result = KrxBreadthProvider().collect(_ctx(settings, _handler(SAMPLE_ROWS, SAMPLE_ROWS)))
    assert result.status == "OK", result.safe_detail
    assert len(result.facts) == 14, f"2시장 x 7 metric != {len(result.facts)}"

    by_subject_metric = {(f.subject, f.metric): f for f in result.facts}
    for subject in ("KOSPI", "KOSDAQ"):
        assert by_subject_metric[(subject, "breadth_advancers")].value_num == 1
        assert by_subject_metric[(subject, "breadth_decliners")].value_num == 2
        assert by_subject_metric[(subject, "breadth_unchanged")].value_num == 1
        assert by_subject_metric[(subject, "breadth_no_trade")].value_num == 1
        assert by_subject_metric[(subject, "breadth_median_change_pct")].value_num == pytest.approx(-5.0)
        assert by_subject_metric[(subject, "turnover_value")].value_num == pytest.approx(1810.0)
        idx = by_subject_metric[(subject, "index_change_pct")]
        assert idx.value_num == pytest.approx(30 / 350 * 100, rel=1e-9)
        assert idx.extra["method"] == "cap_weighted_reconstructed"
        assert idx.category == "breadth"
        assert idx.market == "KR" and idx.country == "KR"
        assert idx.publisher == "한국거래소 KRX"
        # _handler는 요청 날짜와 무관하게 SAMPLE_ROWS를 즉시 돌려주므로 lookback
        # 없이 1차 시도(0803)에서 바로 잡힌다 — 날짜는 응답의 BAS_DD 필드
        # (spec §3-4) 그대로 20260803이다.
        assert idx.event_at == "2026-08-03T06:30:00+00:00", idx.event_at


def test_no_individual_stock_facts_are_ever_stored(settings):
    """CEO 결정: 개별 종목은 fact로 저장하지 않는다. subject는 KOSPI/KOSDAQ뿐."""
    result = KrxBreadthProvider().collect(_ctx(settings, _handler(SAMPLE_ROWS, SAMPLE_ROWS)))
    subjects = {f.subject for f in result.facts}
    assert subjects == {"KOSPI", "KOSDAQ"}, subjects


def test_raw_payload_keeps_the_full_untouched_array(settings):
    """원문은 가공 없이 전 종목 배열 그대로 raw_snapshot에 남는다(spec §2)."""
    result = KrxBreadthProvider().collect(_ctx(settings, _handler(SAMPLE_ROWS, SAMPLE_ROWS)))
    kospi_item = next(i for i in result.raw_items if i.external_id.startswith("krx:KOSPI:"))
    assert json.loads(kospi_item.payload) == SAMPLE_ROWS


def test_one_market_failure_yields_partial_and_the_other_still_publishes(settings):
    """spec §4: 시장 하나가 실패해도 나머지는 발행한다."""
    result = KrxBreadthProvider().collect(
        _ctx(settings, _handler(SAMPLE_ROWS, None, kosdaq_status=500)))
    assert result.status == "PARTIAL", result.safe_detail
    subjects = {f.subject for f in result.facts}
    assert subjects == {"KOSPI"}
    assert "KOSDAQ" in result.safe_detail


def test_lookback_backs_off_to_the_last_trading_day(settings):
    """장중에는 당일치가 없다 — 오늘부터 하루씩 거슬러 첫 비어 있지 않은
    응답을 쓴다(spec §4). 오늘(0803)은 비고 어제(0802)에만 데이터가 있다."""
    calls: list = []
    rows_0802 = [dict(r, BAS_DD="20260802") for r in SAMPLE_ROWS]
    rows_by_date = {"20260802": rows_0802}
    result = KrxBreadthProvider().collect(_ctx(settings, _by_date_handler(rows_by_date, capture=calls)))
    assert result.status == "OK", result.safe_detail
    kospi_dates = {bd for url, bd in calls if "stk_bydd_trd" in url}
    assert kospi_dates == {"20260803", "20260802"}, "0803(빈 응답) 이후 0802 하나만 더 시도해야 한다"
    for f in result.facts:
        assert f.event_at == "2026-08-02T06:30:00+00:00", f.event_at


def test_credentials_never_appear_in_stored_urls(settings):
    result = KrxBreadthProvider().collect(_ctx(settings, _handler(SAMPLE_ROWS, SAMPLE_ROWS)))
    for item in result.raw_items:
        assert FAKE_KEY not in item.safe_source_url
