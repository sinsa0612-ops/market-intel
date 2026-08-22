"""Fake-key injection integration test routed through EVERY registry slot
of the `all` workflow — the 7 stage-1 providers plus ST1's 4 calendar/event
providers — asserting zero secret leakage into the DB (safe_source_url, raw
payloads) or logs.

This is the only net that catches a provider persisting a raw URL with
`api_key=<real key>` in it (spec B12/R2 — the repo is public and
`MI_SEC_USER_AGENT` is the CEO's real e-mail). It must therefore route the
REAL `collect()` of every provider that can touch a secret: an earlier
revision of this file weakened `assert set(result["providers"]) == set(registry)`
to a subset check and left the 4 new providers out, and a mutation that made
`fred_calendar` store the unmasked URL passed 97 tests unnoticed.

[ASSUMPTION] yfinance never touches SafeHttp or any MI_* secret (spec A6
exempts them — they manage their own internal HTTP sessions), so those two
slots use trivial local stand-ins. `earnings_calendar` is yfinance-based
too, but it runs its REAL collect() here against a patched `yf.Ticker`, so
whatever it persists is what production persists. Everything else runs
through a shared MockTransport via the engine's `transport_factory` seam,
so the actual SafeHttp masking path is what is under test."""
import logging
from datetime import date

import httpx

from market_intel import db as db_mod
from market_intel.engine import run_collect
from market_intel.http_client import configure_logging
from market_intel.models import CollectContext, FactCandidate, ProviderResult, RawItem
from market_intel.providers import earnings_calendar as ec_mod
from market_intel.providers.dart import DartProvider
from market_intel.providers.earnings_calendar import EarningsCalendarProvider
from market_intel.providers.ecos import EcosProvider
from market_intel.providers.fred import FredProvider
from market_intel.providers.fred_calendar import FredCalendarProvider
from market_intel.providers.kis_flows import KisFlowsProvider
from market_intel.providers.krx_breadth import KrxBreadthProvider
from market_intel.universe import UNIVERSE
from market_intel.providers.policy_calendar import PolicyCalendarProvider
from market_intel.providers.sec_8k_events import Sec8kEventsProvider
from market_intel.providers.sec_edgar import SecEdgarProvider
from market_intel.providers.sec_edgar_13f import Sec13fProvider

FAKE_FRED = "FAKEFRED_INTEG"
FAKE_ECOS = "FAKEECOS_INTEG"
FAKE_DART = "FAKEDART_INTEG"
FAKE_UA = "FAKEUSERAGENT_INTEG contact@example.com"
# KIS는 앱키·시크릿을 **헤더로** 보내고 토큰을 디스크에 캐시한다 — 새는 경로가
# 다른 provider와 달라서 이 그물에 반드시 걸려 있어야 한다.
FAKE_KIS_KEY = "FAKEKISKEY_INTEG"
FAKE_KIS_SECRET = "FAKEKISSECRET_INTEG+/="
# KRX도 KIS와 같이 키를 **헤더**(`AUTH_KEY`)로 보낸다. 쿼리스트링에 실리지
# 않는다는 이유로 그물에서 빼면, 로그·raw 스냅샷으로 새는 경로가 안 덮인다.
FAKE_KRX = "FAKEKRXKEY_INTEG"
ALL_SECRETS = [FAKE_FRED, FAKE_ECOS, FAKE_DART, FAKE_UA, FAKE_KIS_KEY, FAKE_KIS_SECRET, FAKE_KRX]


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


class _FakeTicker:
    """Offline stand-in for `yf.Ticker` so earnings_calendar's real collect()
    runs without a network call."""

    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def calendar(self):
        return {"Earnings Date": [date(2026, 8, 27)], "Earnings Average": 2.0, "Revenue Average": 3.0}


def _mock_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    # ST1 fred_calendar: a real allowlist hit, so both the /fred/releases and
    # the /fred/release/dates URLs (each carrying api_key) get persisted —
    # that is exactly what must come back masked.
    if "/fred/release/dates" in url:
        return httpx.Response(200, json={"release_dates": [
            {"release_id": 10, "date": "2026-08-12"}, {"release_id": 10, "date": "2026-09-11"},
        ]})
    if "/fred/releases" in url:
        return httpx.Response(200, json={"releases": [{"id": 10, "name": "Consumer Price Index"}]})
    if "stlouisfed.org" in url:
        return httpx.Response(200, json={"observations": [{"date": "2026-06-01", "value": "1.0"}]})
    if "ecos.bok.or.kr" in url:
        return httpx.Response(200, json={"StatisticSearch": {"row": [
            {"STAT_CODE": "722Y001", "ITEM_CODE1": "0101000", "UNIT_NAME": "%", "TIME": "202606", "DATA_VALUE": "1.0"}
        ]}})
    # ST1 policy_calendar: no meetings parsed -> honest NO_DATA, but the two
    # fetched URLs are still persisted as raw snapshots.
    if "federalreserve.gov" in url or "www.bok.or.kr" in url:
        return httpx.Response(200, text="<html></html>")
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
    if "/oauth2/tokenP" in url:
        return httpx.Response(200, json={"access_token": "FAKETOKEN", "expires_in": 86400})
    if "investor-trade-by-stock-daily" in url:
        return httpx.Response(200, json={"rt_cd": "0", "msg1": "ok", "output2": [
            {"stck_bsop_date": "20260731", "stck_clpr": "262500",
             "frgn_ntby_qty": "1", "prsn_ntby_qty": "-1", "orgn_ntby_qty": "0",
             "frgn_ntby_tr_pbmn": "1", "prsn_ntby_tr_pbmn": "-1", "orgn_ntby_tr_pbmn": "0"}]})
    # KRX 전종목: 첫 basDd에서 바로 응답해야 lookback이 멈추고 raw 스냅샷이
    # 남는다. 빈 응답을 주면 7일을 헛돌다 fact 0건으로 끝나고, 이 그물이
    # 초록이면서 krx를 하나도 안 덮게 된다(kis/빈 유니버스와 같은 실패 모드).
    if "bydd_trd" in url:
        return httpx.Response(200, json={"OutBlock_1": [
            {"BAS_DD": "20260731", "ISU_CD": "005930", "ISU_NM": "TESTCO", "MKT_NM": "KOSPI",
             "TDD_CLSPRC": "100", "CMPPREVDD_PRC": "10", "FLUC_RT": "11.11",
             "ACC_TRDVOL": "1000", "ACC_TRDVAL": "100000", "MKTCAP": "1000000"},
            {"BAS_DD": "20260731", "ISU_CD": "000660", "ISU_NM": "QUIETCO", "MKT_NM": "KOSPI",
             "TDD_CLSPRC": "50", "CMPPREVDD_PRC": "0", "FLUC_RT": "0.00",
             "ACC_TRDVOL": "0", "ACC_TRDVAL": "0", "MKTCAP": "500000"},
        ]})
    return httpx.Response(404)


def test_full_workflow_run_never_leaks_fake_secrets(settings, caplog, tmp_path, monkeypatch):
    caplog.set_level(logging.DEBUG)
    settings.fred_api_key = FAKE_FRED
    settings.ecos_api_key = FAKE_ECOS
    settings.dart_api_key = FAKE_DART
    settings.sec_user_agent = FAKE_UA
    settings.kis_app_key = FAKE_KIS_KEY
    settings.kis_app_secret = FAKE_KIS_SECRET
    settings.krx_api_key = FAKE_KRX
    # Mirrors what cli.py's `collect` command does in production: this is
    # what actually attaches SecretRedactingFilter (spec A6). Without it,
    # httpx's own request-URL logging would leak secrets to any OTHER
    # handler (e.g. pytest's caplog) that isn't ours.
    configure_logging(settings, log_dir=str(tmp_path / "logs"))
    monkeypatch.setattr(ec_mod.yf, "Ticker", _FakeTicker)

    db_mod.init_db(settings.db_path)
    registry = {
        "yfinance": _BenignStandIn("yfinance"),
        # 업종 ETF 보유비중(2026-08-21). 대역을 쓰는 이유는 `yfinance`와 같다 —
        # 이 그물이 재는 것은 **비밀이 새는가**이지 yfinance가 도는가가 아니고,
        # 진짜 공급자를 넣으면 시험이 네트워크에 매달린다.
        "yfinance_holdings": _BenignStandIn("yfinance_holdings"),
        "sec_edgar": SecEdgarProvider(),
        "sec_edgar_13f": Sec13fProvider(),
        "fred": FredProvider(),
        "ecos": EcosProvider(),
        "dart": DartProvider(),
        # ST1 additions to the "all" workflow (spec B14).
        "fred_calendar": FredCalendarProvider(),
        "earnings_calendar": EarningsCalendarProvider(),
        "policy_calendar": PolicyCalendarProvider(),
        "sec_8k_events": Sec8kEventsProvider(),
        "kis": KisFlowsProvider(),
        "krx": KrxBreadthProvider(),
    }

    # **빈 유니버스를 넘기면 안 된다.** 종목이 필요한 provider(kis)가 실제 코드에
    # 닿지 못하고 `no_subjects`로 끝나, 이 그물이 초록이면서 아무것도 안 덮는다.
    # 실측 2026-08-03: 이 자리가 `[]`일 때 KIS의 safe_source_url에 앱키를 일부러
    # 붙이는 변이를 주입해도 테스트가 통과했다.
    result = run_collect(
        settings, UNIVERSE, registry, "all", None,
        transport_factory=lambda _pname: httpx.MockTransport(_mock_handler),
    )

    # Equality, not a subset: a provider silently dropping out of the run is
    # exactly how this test stops covering it.
    assert set(result["providers"]) == set(registry)
    # ...and the fred_calendar leg must actually have fetched something,
    # otherwise there is no URL to check for leakage.
    assert result["providers"]["fred_calendar"]["facts_seen"] > 0
    # 같은 이유로 krx도 실제로 뭔가 가져왔어야 한다. 키가 비었거나 mock이
    # 빈 응답을 주면 provider가 조용히 NO_DATA로 끝나고, 그물은 초록인데
    # krx의 헤더 인증 경로는 하나도 안 덮인 상태가 된다.
    assert result["providers"]["krx"]["facts_seen"] > 0

    conn = db_mod.connect(settings.db_path)
    urls = [r["safe_source_url"] or "" for r in conn.execute("SELECT safe_source_url FROM raw_snapshots")]
    fact_urls = [r["safe_source_url"] or "" for r in conn.execute("SELECT safe_source_url FROM fact_revisions")]
    inline_payloads = [r["payload_inline"] or "" for r in conn.execute("SELECT payload_inline FROM raw_snapshots")]
    details = [r["safe_detail"] or "" for r in conn.execute("SELECT safe_detail FROM provider_runs")]
    conn.close()
    assert any("stlouisfed.org/fred/release/dates" in u for u in urls), "fred_calendar's URLs must be under test"

    for secret in ALL_SECRETS:
        for url in urls + fact_urls:
            assert secret not in url, f"secret leaked into a stored safe_source_url: {url}"
        for payload in inline_payloads:
            assert secret not in payload, "secret leaked into stored raw payload"
        for detail in details:
            assert secret not in detail, "secret leaked into a provider_runs safe_detail"
        assert secret not in caplog.text, "secret leaked into logs"
