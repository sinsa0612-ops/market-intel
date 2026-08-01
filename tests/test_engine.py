"""ST1 acceptance tests 2 & 3: idempotent collect, provider failure
isolation."""
from datetime import datetime, timezone

from market_intel import db as db_mod
from market_intel.engine import run_collect
from market_intel.models import CollectContext, FactCandidate, ProviderResult, RawItem


class _GoodProvider:
    name = "goodprov"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        raw = RawItem(
            external_id="MSFT:20260731", source_published_at="2026-07-31",
            safe_source_url="https://example.test/MSFT", payload='{"close": 100}',
        )
        fact = FactCandidate(
            raw_ref="MSFT:20260731", subject="MSFT", category="price", metric="price_close",
            event_at="2026-07-31T20:00:00+00:00", market="US", country="US",
            value_num=100.0, unit="USD",
        )
        return ProviderResult(status="OK", reason_code=None, raw_items=[raw], facts=[fact])


class _ExplodingProvider:
    name = "badprov"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        raise RuntimeError("simulated provider failure")


def _fake_workflows(monkeypatch, names):
    monkeypatch.setitem(__import__("market_intel.workflows", fromlist=["WORKFLOWS"]).WORKFLOWS, "test", names)


def test_idempotent_collect(settings, monkeypatch):
    _fake_workflows(monkeypatch, ["goodprov"])
    db_mod.init_db(settings.db_path)
    registry = {"goodprov": _GoodProvider()}

    now = datetime.now(timezone.utc)
    run_collect(settings, [], registry, "test", now)
    conn = db_mod.connect(settings.db_path)
    facts_after_1 = conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"]
    snapshots_after_1 = conn.execute("SELECT COUNT(*) c FROM raw_snapshots").fetchone()["c"]
    conn.close()

    run_collect(settings, [], registry, "test", now)
    conn = db_mod.connect(settings.db_path)
    facts_after_2 = conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"]
    snapshots_after_2 = conn.execute("SELECT COUNT(*) c FROM raw_snapshots").fetchone()["c"]
    conn.close()

    assert facts_after_1 > 0
    assert facts_after_2 == facts_after_1, "second identical collect must add 0 new fact revisions"
    assert snapshots_after_2 == snapshots_after_1, "second identical collect must add 0 new raw snapshots"


def test_provider_isolation(settings, monkeypatch):
    _fake_workflows(monkeypatch, ["goodprov", "badprov"])
    db_mod.init_db(settings.db_path)
    registry = {"goodprov": _GoodProvider(), "badprov": _ExplodingProvider()}

    result = run_collect(settings, [], registry, "test", datetime.now(timezone.utc))

    assert result["providers"]["goodprov"]["status"] == "OK"
    assert result["providers"]["badprov"]["status"] == "ERROR"

    conn = db_mod.connect(settings.db_path)
    facts = conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"]
    provider_runs = conn.execute(
        "SELECT provider, status FROM provider_runs WHERE run_id=?", (result["run_id"],)
    ).fetchall()
    conn.close()

    assert facts == 1, "the good provider's fact must still be persisted"
    statuses = {r["provider"]: r["status"] for r in provider_runs}
    assert statuses == {"goodprov": "OK", "badprov": "ERROR"}


def test_unregistered_provider_is_no_data_not_implemented(settings, monkeypatch):
    _fake_workflows(monkeypatch, ["ghost"])
    db_mod.init_db(settings.db_path)

    result = run_collect(settings, [], {}, "test", datetime.now(timezone.utc))

    assert result["providers"]["ghost"]["status"] == "NO_DATA"
    assert result["providers"]["ghost"]["reason_code"] == "미구현"
