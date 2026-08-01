"""argparse CLI (spec A8): `market-intel init | collect | db stats`.
No TTY prompts anywhere — must run unattended under cron."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from .config import load_settings
from .db import connect, init_db
from .engine import run_collect
from .http_client import configure_logging
from .universe import CORE16, UNIVERSE
from .workflows import WORKFLOWS

PROVIDER_ORDER = ["yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart"]
STATUS_ORDER = ["source_verified", "partial", "reconstructed", "unverified"]


def _parse_cutoff(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def cmd_init(settings) -> int:
    init_db(settings.db_path)
    print(f"initialized db at {settings.db_path}")
    return 0


def cmd_collect(settings, workflow: str, cutoff_str: str | None) -> int:
    # Imported here, not at module scope: pykrx writes a banner to stdout at
    # import time, which would pollute the parseable output of `init`/`db stats`.
    from .providers import PROVIDERS

    try:
        cutoff = _parse_cutoff(cutoff_str)
    except ValueError as exc:
        # A raw traceback here still exits non-zero (cron can detect the
        # failure either way), but it also dumps an unhandled ValueError to
        # stderr for a simple bad-input case (repair.md finding #7).
        print(f"error: invalid --cutoff value {cutoff_str!r}: {exc}", file=sys.stderr)
        return 2
    logger = configure_logging(settings)
    result = run_collect(settings, UNIVERSE, PROVIDERS, workflow, cutoff, logger=logger)
    print(f"run_id={result['run_id']} workflow={result['workflow']} cutoff={result['cutoff']}")
    for name, info in result["providers"].items():
        print(
            f"  {name}: status={info['status']} reason={info['reason_code']} "
            f"facts_seen={info['facts_seen']} facts_appended={info['facts_appended']}"
        )
    return 0


def cmd_db_stats(settings) -> int:
    # `db stats` on a not-yet-initialized DB should report an empty database,
    # not a traceback; init_db is idempotent and never drops anything.
    init_db(settings.db_path)
    conn = connect(settings.db_path)
    total = conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"]
    print(f"facts_total={total}")

    by_provider = {p: 0 for p in PROVIDER_ORDER}
    for row in conn.execute("SELECT fact_id FROM fact_revisions"):
        provider = row["fact_id"].split(":", 1)[0]
        by_provider[provider] = by_provider.get(provider, 0) + 1
    print("facts_by_provider: " + " ".join(f"{p}={by_provider.get(p, 0)}" for p in PROVIDER_ORDER))

    by_status = {s: 0 for s in STATUS_ORDER}
    for row in conn.execute("SELECT data_status FROM fact_revisions"):
        if row["data_status"] in by_status:
            by_status[row["data_status"]] += 1
    print("facts_by_status: " + " ".join(f"{s}={by_status[s]}" for s in STATUS_ORDER))

    covered = 0
    coverage_lines = []
    for sym in CORE16:
        row = conn.execute(
            "SELECT event_at FROM fact_revisions WHERE subject=? AND metric='price_close' "
            "ORDER BY event_at DESC LIMIT 1",
            (sym["symbol"],),
        ).fetchone()
        if row and row["event_at"]:
            covered += 1
            coverage_lines.append(f"{sym['symbol']} latest_close_event_date={row['event_at'][:10].replace('-', '')}")
        else:
            coverage_lines.append(f"{sym['symbol']} latest_close_event_date=NONE")
    print(f"core16_price_coverage={covered}/{len(CORE16)}")
    for line in coverage_lines:
        print(line)

    last_run = conn.execute("SELECT * FROM collect_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if last_run:
        provs = conn.execute(
            "SELECT provider, status, reason_code FROM provider_runs WHERE run_id=?",
            (last_run["run_id"],),
        ).fetchall()
        provs_str = " ".join(f"{p['provider']}={p['status']}({p['reason_code'] or ''})" for p in provs)
        print(f"last_run: workflow={last_run['workflow']} cutoff={last_run['cutoff_at']} providers: {provs_str}")
    else:
        print("last_run: none")

    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-intel")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--workflow", required=True, choices=list(WORKFLOWS))
    p_collect.add_argument("--cutoff", default=None)

    p_db = sub.add_parser("db")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("stats")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command == "init":
        return cmd_init(settings)
    if args.command == "collect":
        return cmd_collect(settings, args.workflow, args.cutoff)
    if args.command == "db" and args.db_command == "stats":
        return cmd_db_stats(settings)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
