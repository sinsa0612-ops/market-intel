"""ST3 success criterion 4(a): fake-key injection integration test routed
through all 7 registry slots, asserting zero secret leakage into the DB
(safe_source_url, raw payloads) or logs.

[ASSUMPTION] yfinance/pykrx never touch SafeHttp or any MI_* secret
(spec A6 exempts them — they manage their own internal HTTP sessions), so
this test swaps in trivial local stand-ins for those two slots to keep the
whole run offline/deterministic; their real implementations are already
exercised end-to-end by the E2E run in result.md. sec_edgar/fred/ecos/dart
run their REAL collect() through a shared MockTransport via engine's
`transport_factory` seam, so the actual SafeHttp masking path is what's
under test."""
import logging

import httpx

from market_intel import db as db_mod
from market_intel.engine import run_collect
from market_intel.http_client import configure_logging
from market_intel.models import CollectContext, FactCandidate, ProviderResult, RawItem
from market_intel.providers.dart import DartProvider
from market_intel.providers.ecos import EcosProvider
from market_intel.providers.fred import FredProvider
from market_intel.providers.sec_edgar import SecEdgarProvider
from market_intel.providers.sec_edgar_13f import Sec13fProvider

FAKE_FRED = "FAKEFRED_INTEG"
FAKE_ECOS = "FAKEECOS_INTEG"
FAKE_DART = "FAKEDART_INTEG"
FAKE_UA = "FAKEUSERAGENT_INTEG contact@example.com"
ALL_SECRETS = [FAKE_FRED, FAKE_ECOS, FAKE_DART, FAKE_UA]


class _BenignStandIn:
    def __init__(self, name):
        self.name = name

    def collect(self, ctx: CollectContext) -> ProviderResult:
        raw = RawItem(
            external_id=f"{self.name}:stub", source_published_at="2026-07-31",
            safe_source_url=f"https://example.test/{self.name}", payload="{}",
        )
        fact = FactCandidate(
            raw_ref=f"{self.name}:stub", subject="STUB", category="test", metric="value",
            event_at="2026-07-31T00:00:00+00:00", market="US", country="US", value_num=1.0, unit="",
        )
        return ProviderResult(status="OK", reason_code=None, raw_items=[raw], facts=[fact])


def _mock_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "stlouisfed.org" in url:
        return httpx.Response(200, json={"observations": [{"date": "2026-06-01", "value": "1.0"}]})
    if "ecos.bok.or.kr" in url:
        return httpx.Response(200, json={"StatisticSearch": {"row": [
            {"STAT_CODE": "722Y001", "ITEM_CODE1": "0101000", "UNIT_NAME": "%", "TIME": "202606", "DATA_VALUE": "1.0"}
        ]}})
    if "corpCode.xml" in url:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("CORPCODE.xml", "<result></result>")  # no matches -> honest empty
        return httpx.Response(200, content=buf.getvalue())
    if "opendart.fss.or.kr" in url:
        return httpx.Response(200, json={"status": "013", "message": "no data"})
    if "sec.gov" in url:
        return httpx.Response(200, json={})
    return httpx.Response(404)


def test_seven_provider_run_never_leaks_fake_secrets(settings, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    settings.fred_api_key = FAKE_FRED
    settings.ecos_api_key = FAKE_ECOS
    settings.dart_api_key = FAKE_DART
    settings.sec_user_agent = FAKE_UA
    # Mirrors what cli.py's `collect` command does in production: this is
    # what actually attaches SecretRedactingFilter (spec A6). Without it,
    # httpx's own request-URL logging would leak secrets to any OTHER
    # handler (e.g. pytest's caplog) that isn't ours.
    configure_logging(settings, log_dir=str(tmp_path / "logs"))

    db_mod.init_db(settings.db_path)
    registry = {
        "yfinance": _BenignStandIn("yfinance"),
        "pykrx": _BenignStandIn("pykrx"),
        "sec_edgar": SecEdgarProvider(),
        "sec_edgar_13f": Sec13fProvider(),
        "fred": FredProvider(),
        "ecos": EcosProvider(),
        "dart": DartProvider(),
    }

    result = run_collect(
        settings, [], registry, "all", None,
        transport_factory=lambda _pname: httpx.MockTransport(_mock_handler),
    )

    assert set(result["providers"]) == set(registry)

    conn = db_mod.connect(settings.db_path)
    urls = [r["safe_source_url"] or "" for r in conn.execute("SELECT safe_source_url FROM raw_snapshots")]
    inline_payloads = [r["payload_inline"] or "" for r in conn.execute("SELECT payload_inline FROM raw_snapshots")]
    conn.close()

    for secret in ALL_SECRETS:
        for url in urls:
            assert secret not in url, f"secret leaked into stored safe_source_url: {url}"
        for payload in inline_payloads:
            assert secret not in payload, "secret leaked into stored raw payload"
        assert secret not in caplog.text, "secret leaked into logs"
