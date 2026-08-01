"""ST1 acceptance test 4: secret masking (query + path segments), host
whitelist, and cross-host redirect rejection."""
import logging

import httpx
import pytest

from market_intel.errors import HostRejected
from market_intel.http_client import (
    SafeHttp,
    SecretRedactingFilter,
    configure_logging,
    safe_url,
)

SECRET = "TESTSECRET123"


def test_safe_url_masks_query_param_by_name():
    url = "https://api.stlouisfed.org/fred/series/observations?series_id=CPIAUCSL&api_key=TESTSECRET123&file_type=json"
    masked = safe_url(url, secrets=[SECRET])
    assert SECRET not in masked
    assert "api_key=%2A%2A%2A" in masked or "api_key=***" in masked


def test_safe_url_masks_path_segment():
    # ECOS-style: the key rides in the URL *path*, not a query string.
    url = f"https://ecos.bok.or.kr/api/StatisticSearch/{SECRET}/json/kr/1/100/722Y001"
    masked = safe_url(url, secrets=[SECRET])
    assert SECRET not in masked
    assert "***" in masked


def test_safe_url_masks_named_param_even_if_value_unknown():
    url = "https://opendart.fss.or.kr/api/list.json?crtfc_key=whatever&corp_code=00126380"
    masked = safe_url(url, secrets=[])
    assert "crtfc_key=whatever" not in masked
    assert "crtfc_key=***" in masked or "crtfc_key=%2A%2A%2A" in masked


def test_safe_http_query_and_path_secrets_never_stored(settings):
    settings.fred_api_key = SECRET

    def handler(request: httpx.Request) -> httpx.Response:
        # the mock server sees the real secret; that's fine — the *client*
        # must never persist/log it unmasked.
        assert SECRET in str(request.url)
        return httpx.Response(200, json={"ok": True})

    client = SafeHttp("fred", settings, transport=httpx.MockTransport(handler))
    resp = client.get("https://api.stlouisfed.org/fred/series/observations", params={"api_key": SECRET, "series_id": "X"})
    stored_url = client.safe_url(str(resp.request.url))
    assert SECRET not in stored_url
    client.close()


def test_safe_http_rejects_non_whitelisted_host(settings):
    client = SafeHttp("fred", settings, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(HostRejected):
        client.get("https://evil.example.com/steal")
    client.close()


def test_safe_http_rejects_non_https(settings):
    client = SafeHttp("fred", settings, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(HostRejected):
        client.get("http://api.stlouisfed.org/fred/series/observations")
    client.close()


def test_safe_http_rejects_cross_host_redirect(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    client = SafeHttp("fred", settings, transport=httpx.MockTransport(handler))
    with pytest.raises(HostRejected):
        client.get("https://api.stlouisfed.org/fred/series/observations")
    client.close()


def test_secret_redacting_filter_scrubs_log_records():
    filt = SecretRedactingFilter([SECRET])
    record = logging.LogRecord("t", logging.INFO, __file__, 1, f"leaked: {SECRET}", (), None)
    filt.filter(record)
    assert SECRET not in record.getMessage()


# --- what actually lands on disk / on the wire --------------------------------
# (unit-level masking can be right while the production logging wiring still
#  leaks: third-party libraries log full request URLs themselves.)


def test_configure_logging_redacts_secrets_written_to_the_real_log_file(settings, tmp_path):
    settings.fred_api_key = SECRET
    settings.log_dir = str(tmp_path / "logs")

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    try:
        logger = configure_logging(settings)
        logger.info("fetched https://api.stlouisfed.org/fred/series?api_key=%s", SECRET)
        # a third-party logger (httpx logs request URLs at INFO) must be covered too
        logging.getLogger("httpx").info(
            "HTTP Request: GET https://api.stlouisfed.org/fred/series?api_key=%s \"200 OK\"", SECRET
        )
        for handler in root.handlers:
            handler.flush()

        log_files = list((tmp_path / "logs").glob("collect-*.log"))
        assert log_files, "configure_logging must create <log_dir>/collect-YYYYMMDD.log"
        content = log_files[0].read_text(encoding="utf-8")
        assert SECRET not in content
        assert content.count("***") >= 2
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        root.handlers = previous_handlers


def test_sec_edgar_user_agent_header_is_sent(settings):
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"ok": True})

    settings.sec_user_agent = "market-intel-test (test@example.invalid)"
    client = SafeHttp("sec_edgar", settings, transport=httpx.MockTransport(handler))
    client.get("https://www.sec.gov/files/company_tickers.json")
    client.close()
    assert captured["user_agent"] == "market-intel-test (test@example.invalid)"


def test_same_host_redirect_is_followed_exactly_once(settings):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://api.stlouisfed.org/fred/series/final"})
        return httpx.Response(200, json={"ok": True})

    client = SafeHttp("fred", settings, transport=httpx.MockTransport(handler))
    resp = client.get("https://api.stlouisfed.org/fred/series")
    client.close()
    assert resp.status_code == 200
    assert len(calls) == 2


def test_sec_edgar_rate_limit_respects_the_10_per_second_ceiling(settings):
    sec = SafeHttp("sec_edgar", settings, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    default = SafeHttp("fred", settings, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        assert sec._min_interval >= 0.1, "SEC EDGAR allows at most 10 requests/second"
        assert sec._min_interval < default._min_interval
    finally:
        sec.close()
        default.close()
