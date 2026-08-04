"""argparse CLI (spec A8): `market-intel init | collect | db stats`.
No TTY prompts anywhere — must run unattended under cron."""
from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime

from .config import load_settings
from .db import connect, init_db
from .engine import run_collect
from .http_client import configure_logging
from .schedule import ensure_calendar_gaps
from .universe import CORE16, UNIVERSE
from .workflows import WORKFLOWS

PROVIDER_ORDER = [
    "yfinance", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart",
    # spec B14 (ST1) — without these four, `db stats` reports the calendar
    # facts as if they did not exist.
    "fred_calendar", "earnings_calendar", "policy_calendar", "sec_8k_events",
]
STATUS_ORDER = ["source_verified", "partial", "reconstructed", "unverified"]

# spec B1 — the single point where ST1/ST2/ST3 each add a `schedule`/
# `report`/`publish` subcommand without three subtasks editing this file's
# same lines (worktree merge-hell prevention). Each module registers its
# own argparse subparser and handles its own subcommand; a module that
# doesn't exist yet (later worktrees) is skipped silently.
CLI_EXTENSIONS = [
    "market_intel.cli_schedule", "market_intel.cli_report", "market_intel.cli_publish",
    # 2단계-B: ST1/ST2는 자기 서브커맨드 모듈만 만들 수 있었고(이 파일은 ST3 소유)
    # 그 결과 `market-intel interpret`이 인도물 상태로 실행 불가능했다(judge.md 6-1).
    "market_intel.cli_thesis", "market_intel.cli_interpret", "market_intel.cli_ops",
    # 백필 ST1 — `market-intel backfill`. 등록하지 않으면 모듈만 존재하고
    # 명령은 `invalid choice: 'backfill'`로 죽는다(2단계-B judge.md 6-1과
    # 같은 실패). CEO 실행 단계(백필 spec §9)가 이 명령을 직접 친다.
    "market_intel.cli_backfill",
    # 수급 표본 목록 갱신 — `market-intel krx top-kospi`. 등록하지 않으면
    # 모듈만 있고 명령은 `invalid choice`로 죽는다(이 파일이 이미 두 번 겪은 실패).
    "market_intel.cli_krx",
]


def _extension_modules() -> list:
    mods = []
    # dict.fromkeys = 순서 유지 중복 제거. ST1/ST2의 테스트들이 자기 모듈을
    # `CLI_EXTENSIONS + [...]`로 덧붙여 등록하는데(그 시절엔 이 파일을 고칠 수
    # 없었다), 이제 기본 목록에도 들어 있으므로 그대로 두면 argparse가 같은
    # 서브커맨드를 두 번 등록하다 죽는다.
    for name in dict.fromkeys(CLI_EXTENSIONS):
        try:
            mods.append(importlib.import_module(name))
        except ImportError as exc:
            # Only "this extension isn't in this worktree yet" is skippable.
            # A broken import *inside* an existing module must not make its
            # subcommand vanish silently — that turns a typo into argparse's
            # "invalid choice: 'schedule'".
            if exc.name != name:
                raise
            continue
    return mods


def _parse_cutoff(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def cmd_init(settings) -> int:
    init_db(settings.db_path)
    print(f"initialized db at {settings.db_path}")
    return 0


def cmd_collect(settings, workflow: str, cutoff_str: str | None) -> int:
    # Imported here, not at module scope: a provider that writes to stdout at
    # import time would pollute the parseable output of `init`/`db stats`.
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
    if workflow in ("calendar", "all"):
        # spec ST1 boundary / §제외한 것 — what this stage deliberately does
        # not implement is registered on the write path rather than being
        # filled with a guess.
        conn = connect(settings.db_path)
        try:
            ensure_calendar_gaps(conn)
        finally:
            conn.close()
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
    # 라벨에 "core16"을 박아 두면 관측군이 늘 때(2026-08-03에 18개) 화면이
    # `core16_price_coverage=0/18`이라고 말한다 — 숫자와 이름이 서로 어긋난다.
    print(f"observed_price_coverage={covered}/{len(CORE16)}")
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

    for mod in _extension_modules():
        mod.register(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()

    for mod in _extension_modules():
        result = mod.dispatch(args, settings)
        if result is not None:
            return result

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
