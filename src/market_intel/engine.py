"""Runs a workflow: calls each provider, isolates its failures, and
persists whatever it returns through db.py (providers never touch the DB
directly — spec A5)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from . import db as db_mod
from .http_client import SafeHttp
from .models import CollectContext, ProviderResult
from .workflows import WORKFLOWS


def _fact_id(provider: str, fc) -> str:
    date_str = fc.event_at[:10].replace("-", "") if fc.event_at else "00000000"
    suffix = ":ah" if fc.session_label == "after_hours" else ""
    return f"{provider}:{fc.subject}:{fc.metric}:{date_str}{suffix}"


def _persist(conn, raw_dir: str, provider_name: str, result: ProviderResult) -> tuple[int, int]:
    """Returns (facts_seen, facts_appended)."""
    ref_map: dict[str, tuple[str, str]] = {}
    for item in result.raw_items:
        snapshot_id = db_mod.insert_raw_snapshot(conn, raw_dir, provider_name, item)
        ref_map[item.external_id] = (snapshot_id, item.safe_source_url)

    known_at = db_mod.iso_utc()
    appended = 0
    for fc in result.facts:
        snapshot_id, safe_url = ref_map.get(fc.raw_ref, (None, ""))
        fc.safe_source_url = safe_url
        fact_id = _fact_id(provider_name, fc)
        if db_mod.upsert_fact(conn, fact_id, snapshot_id, known_at, fc):
            appended += 1
    conn.commit()
    return len(result.facts), appended


def run_collect(
    settings,
    universe,
    providers_registry: dict,
    workflow: str,
    cutoff: datetime | None = None,
    logger: logging.Logger | None = None,
    transport_factory=None,
) -> dict:
    """`transport_factory`, if given, is `provider_name -> httpx.BaseTransport`
    and is threaded into each provider's SafeHttp — a test-only seam (e.g.
    httpx.MockTransport) for exercising the real SafeHttp/secret-masking
    path across every provider without hitting the network. Production
    callers omit it and get the real network transport."""
    if workflow not in WORKFLOWS:
        raise ValueError(f"unknown workflow: {workflow!r} (choices: {list(WORKFLOWS)})")

    logger = logger or logging.getLogger("market_intel")
    now = datetime.now(timezone.utc)
    cutoff = cutoff or now

    conn = db_mod.connect(settings.db_path)
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO collect_runs(run_id, workflow, cutoff_at, started_at, finished_at, status) VALUES (?,?,?,?,?,?)",
        (run_id, workflow, db_mod.iso_utc(cutoff), db_mod.iso_utc(now), None, "running"),
    )
    conn.commit()

    summary: dict[str, dict] = {}
    for name in WORKFLOWS[workflow]:
        provider = providers_registry.get(name)
        p_started = db_mod.iso_utc()

        if provider is None:
            status, reason_code, safe_detail = "NO_DATA", "미구현", ""
            facts_seen = facts_appended = 0
        else:
            def http_factory(pname: str = name) -> SafeHttp:
                transport = transport_factory(pname) if transport_factory else None
                return SafeHttp(pname, settings, transport=transport)

            ctx = CollectContext(
                cutoff=cutoff, now=now, settings=settings, http=http_factory,
                universe=universe, logger=logger,
            )
            try:
                result = provider.collect(ctx)
                status, reason_code, safe_detail = result.status, result.reason_code, result.safe_detail
                facts_seen, facts_appended = _persist(conn, settings.raw_dir, name, result)
            except Exception as exc:  # noqa: BLE001 - provider failures must not abort the run
                logger.exception("provider %s raised", name)
                # Do NOT default to "network_error": we do not know that it was
                # the network. reason_code stays empty and the real exception
                # class goes into safe_detail (Prime Rule 7 — report faithfully).
                status, reason_code = "ERROR", None
                safe_detail = f"{exc.__class__.__name__}: {exc}"[:300]
                facts_seen = facts_appended = 0

        conn.execute(
            """INSERT INTO provider_runs
               (provider_run_id, run_id, provider, started_at, finished_at, item_count, status, reason_code, safe_detail)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), run_id, name, p_started, db_mod.iso_utc(), facts_seen, status, reason_code, safe_detail),
        )
        conn.commit()
        summary[name] = {
            "status": status, "reason_code": reason_code,
            "facts_seen": facts_seen, "facts_appended": facts_appended,
        }

    conn.execute(
        "UPDATE collect_runs SET finished_at=?, status=? WHERE run_id=?",
        (db_mod.iso_utc(), "done", run_id),
    )
    conn.commit()
    conn.close()
    return {"run_id": run_id, "workflow": workflow, "cutoff": db_mod.iso_utc(cutoff), "providers": summary}
