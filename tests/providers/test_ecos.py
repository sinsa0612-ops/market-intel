"""ST3 acceptance tests for ecos.py — including the path-segment key
masking gotcha (spec A6/A9): ECOS puts the key in the URL path, not a
query string, so a naive query-only masker would leak it."""
from datetime import datetime, timezone

import httpx

from market_intel.models import CollectContext
from market_intel.providers.ecos import STAT_CODES, EcosProvider

FAKE_KEY = "FAKEECOSKEY456"


def _ctx(settings, http_factory):
    return CollectContext(
        cutoff=datetime.now(timezone.utc), now=datetime.now(timezone.utc),
        settings=settings, http=http_factory, universe=[], logger=__import__("logging").getLogger("t"),
    )


def test_no_key_means_zero_http_calls(settings):
    settings.ecos_api_key = ""

    def boom(_name):
        raise AssertionError("SafeHttp factory must not be invoked when the key is empty")

    result = EcosProvider().collect(_ctx(settings, boom))
    assert result.status == "NO_DATA"
    assert result.reason_code == "키없음"
    assert result.raw_items == [] and result.facts == []


def test_key_rides_in_url_path_and_gets_used(settings):
    settings.ecos_api_key = FAKE_KEY
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        # confirm the key really is a path segment, not a query param —
        # this is the exact shape SafeHttp's path masking exists to catch.
        assert FAKE_KEY in request.url.path
        assert FAKE_KEY not in (request.url.query.decode() if request.url.query else "")
        # TIME format must match the requested freq (M -> YYYYMM, D -> YYYYMMDD),
        # same as the real ECOS API.
        freq_segment = request.url.path.split("/")[9]  # .../{key}/json/kr/1/100/{stat}/{freq}/{start}/{end}/{item}
        time_value = "202606" if freq_segment == "M" else "20260731"
        return httpx.Response(
            200,
            json={"StatisticSearch": {"row": [
                {"STAT_CODE": "722Y001", "STAT_NAME": "test", "ITEM_CODE1": "0101000",
                 "UNIT_NAME": "%", "TIME": time_value, "DATA_VALUE": "3.50"},
            ]}},
        )

    def http_factory(name):
        from market_intel.http_client import SafeHttp
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler))

    result = EcosProvider().collect(_ctx(settings, http_factory))

    assert len(seen_urls) == len(STAT_CODES)
    assert result.status == "OK"
    assert len(result.facts) == len(STAT_CODES)
    fact = result.facts[0]
    assert fact.subject == "722Y001.0101000"
    assert fact.value_num == 3.50
    assert fact.metric == "value"
    # every fact carries the provenance/verification note of its statistic code
    # (codes confirmed live on 2026-08-01; the two unverified ones say so).
    assert fact.extra["logical_key"] == "base_rate"
    assert fact.extra["assumption_note"]
    assert all(spec["note"] for spec in STAT_CODES.values())


def test_fake_key_never_appears_in_stored_safe_url_or_snapshot(settings, caplog):
    settings.ecos_api_key = FAKE_KEY

    def handler(request: httpx.Request) -> httpx.Response:
        freq_segment = request.url.path.split("/")[9]
        time_value = "202606" if freq_segment == "M" else "20260731"
        return httpx.Response(
            200,
            json={"StatisticSearch": {"row": [
                {"STAT_CODE": "722Y001", "ITEM_CODE1": "0101000", "UNIT_NAME": "%", "TIME": time_value, "DATA_VALUE": "3.50"}
            ]}},
        )

    def http_factory(name):
        from market_intel.http_client import SafeHttp
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler))

    result = EcosProvider().collect(_ctx(settings, http_factory))

    for raw in result.raw_items:
        assert FAKE_KEY not in raw.safe_source_url
        # the raw payload is the server's literal JSON body (no key in it,
        # since ECOS echoes the key nowhere in the response) — but assert
        # anyway as a belt-and-suspenders check.
        assert FAKE_KEY not in (raw.payload if isinstance(raw.payload, str) else raw.payload.decode())
    assert FAKE_KEY not in caplog.text
