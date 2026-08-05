"""토큰 캐시가 죽으면 **조용히 넘어가지 않는다.**

왜 이 파일이 있는가 (CEO 지적 2026-08-05): KIS가 "접근 토큰은 1일 1회 발급
원칙이며, 유효기간 내 잦은 발급 시 이용이 제한될 수 있습니다"라고 안내한다.
지금은 캐시 덕에 하루 한 번이지만, 캐시 파일을 못 쓰게 되면 수집할 때마다
새 토큰을 받아 **하루 6번**이 된다(06:50·07:40·13:00·15:50·16:15·22:00).
예전 코드는 `except OSError: pass`라 흔적조차 없어 **계정이 막힐 때까지
아무도 모르는** 구조였다.

캐시 실패가 수집을 막아서는 안 된다(캐시는 최적화다) — 기록만 남긴다.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from market_intel.models import CollectContext
from market_intel.providers import kis_flows
from market_intel.providers.kis_flows import KisFlowsProvider


def _ctx(settings, tmp_path, handler):
    from market_intel.http_client import SafeHttp

    def http(name: str):
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler))

    from datetime import datetime, timezone
    now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone.utc)
    return CollectContext(cutoff=now, now=now, settings=settings, http=http,
                          universe=[], logger=None)


def _handler(request: httpx.Request) -> httpx.Response:
    if "/oauth2/tokenP" in str(request.url):
        return httpx.Response(200, json={"access_token": "TESTTOKEN", "expires_in": 86400})
    return httpx.Response(200, json={"rt_cd": "0", "msg1": "ok", "output2": [
        {"stck_bsop_date": "20260804", "stck_clpr": "1000",
         "frgn_ntby_qty": "1", "prsn_ntby_qty": "-1", "orgn_ntby_qty": "0",
         "frgn_ntby_tr_pbmn": "1", "prsn_ntby_tr_pbmn": "-1", "orgn_ntby_tr_pbmn": "0"}]})


@pytest.fixture
def kis_settings(settings):
    settings.kis_app_key = "FAKEKEY"
    settings.kis_app_secret = "FAKESECRET"
    return settings


def test_cache_write_failure_is_reported_not_swallowed(kis_settings, tmp_path, monkeypatch):
    """캐시를 못 써도 수집은 되지만, 그 사실이 `safe_detail`에 남아야 한다."""
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(kis_flows.os, "open", boom)
    result = KisFlowsProvider().collect(_ctx(kis_settings, tmp_path, _handler))

    assert result.status in ("OK", "PARTIAL"), result.status  # 수집은 계속된다
    assert "token_cache_write_failed" in result.safe_detail, result.safe_detail
    # 토큰 값이 기록에 새면 안 된다.
    assert "TESTTOKEN" not in result.safe_detail


def test_fresh_issue_is_noted_so_it_can_be_matched_with_kis_alerts(kis_settings, tmp_path):
    """KIS는 발급 때마다 고객에게 알림을 보낸다. 왜 받았는지가 남아야 대조된다."""
    result = KisFlowsProvider().collect(_ctx(kis_settings, tmp_path, _handler))
    assert "token_issued" in result.safe_detail, result.safe_detail


def test_a_valid_cached_token_is_reused_without_reissuing(kis_settings, tmp_path):
    """캐시가 살아 있으면 발급하지 않는다 — 이것이 하루 1회를 지키는 장치다."""
    path = kis_flows._token_cache_path(kis_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": "CACHED", "expires_at": time.time() + 20 * 3600}),
                    encoding="utf-8")

    calls: list[str] = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _handler(request)

    result = KisFlowsProvider().collect(_ctx(kis_settings, tmp_path, counting))

    assert not any("/oauth2/tokenP" in u for u in calls), "캐시가 있는데 토큰을 또 받았다"
    assert "token_issued" not in result.safe_detail
