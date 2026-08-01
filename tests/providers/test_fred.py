"""ST3 acceptance tests for fred.py: (i) no key -> NO_DATA/키없음 + zero
HTTP calls, (ii) fake key + MockTransport fixture -> facts mapped
correctly, (iii) fake key absent from stored safe_url."""
from datetime import datetime, timezone

import httpx

from market_intel.models import CollectContext
from market_intel.providers.fred import SERIES, FredProvider

FAKE_KEY = "FAKEFREDKEY123"


def _ctx(settings, http_factory, universe=None):
    return CollectContext(
        cutoff=datetime.now(timezone.utc), now=datetime.now(timezone.utc),
        settings=settings, http=http_factory, universe=universe or [], logger=__import__("logging").getLogger("t"),
    )


def test_no_key_means_zero_http_calls(settings):
    settings.fred_api_key = ""

    def boom(_name):
        raise AssertionError("SafeHttp factory must not be invoked when the key is empty")

    result = FredProvider().collect(_ctx(settings, boom))
    assert result.status == "NO_DATA"
    assert result.reason_code == "키없음"
    assert result.raw_items == []
    assert result.facts == []


def test_fake_key_maps_observations_to_facts(settings):
    settings.fred_api_key = FAKE_KEY
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        series_id = request.url.params.get("series_id")
        return httpx.Response(
            200,
            json={
                "units": "lin", "output_type": 1,
                "observations": [
                    {"realtime_start": "2026-07-01", "realtime_end": "9999-12-31", "date": "2026-06-01", "value": "3.50"}
                ],
            },
        )

    def http_factory(name):
        from market_intel.http_client import SafeHttp
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler))

    result = FredProvider().collect(_ctx(settings, http_factory))

    assert result.status == "OK"
    assert len(calls) == len(SERIES)
    assert len(result.facts) == len(SERIES)
    subjects = {f.subject for f in result.facts}
    assert subjects == set(SERIES)
    f = result.facts[0]
    assert f.metric == "value"
    assert f.value_num == 3.50
    assert f.data_status == "source_verified"
    assert f.extra["realtime_start"] == "2026-07-01"


def test_fake_key_never_appears_in_stored_safe_url(settings, caplog):
    settings.fred_api_key = FAKE_KEY

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"observations": [{"date": "2026-06-01", "value": "1.0"}]})

    def http_factory(name):
        from market_intel.http_client import SafeHttp
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler))

    result = FredProvider().collect(_ctx(settings, http_factory))

    for raw in result.raw_items:
        assert FAKE_KEY not in raw.safe_source_url
    for fact in result.facts:
        assert FAKE_KEY not in (fact.extra.get("realtime_start") or "")
    assert FAKE_KEY not in caplog.text
