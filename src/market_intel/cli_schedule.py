"""`schedule list` / `schedule changes` CLI (spec B13), wired into `cli.py`
only through the `CLI_EXTENSIONS` hook (spec B1) — `cli.py` never imports
this module by name."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db as db_mod
from . import schedule as schedule_mod


def register(sub) -> None:
    p_schedule = sub.add_parser("schedule")
    schedule_sub = p_schedule.add_subparsers(dest="schedule_command", required=True)

    p_list = schedule_sub.add_parser("list")
    p_list.add_argument("--days", type=int, default=60)

    p_changes = schedule_sub.add_parser("changes")
    p_changes.add_argument("--days", type=int, default=7)


def dispatch(args, settings) -> int | None:
    if args.command != "schedule":
        return None

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    try:
        if args.schedule_command == "list":
            return _cmd_list(conn, args.days)
        if args.schedule_command == "changes":
            return _cmd_changes(conn, args.days)
        return 1
    finally:
        conn.close()


def _cmd_list(conn, days: int) -> int:
    now = datetime.now(timezone.utc)
    rows = schedule_mod.upcoming(conn, now, days)
    print(f"upcoming_total={len(rows)} window_days={days}")
    for r in rows:
        subj = f" subject={r['subject']}" if r["subject"] else ""
        print(f"{r['date']} {r['importance']} {r['country']} {r['name']}{subj} status={r['status']}")
    return 0


def _cmd_changes(conn, days: int) -> int:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_iso = db_mod.iso_utc(since)
    # `days` is both the lookback for "what changed" and the forward window
    # for "which dates are worth showing" (spec B2 rev2) — without the
    # second half, the day a next-year timetable is first published prints
    # every one of its dates as 신규. `now` is this command's information
    # barrier: an interactive listing may see everything known *now*, and
    # nothing that is not yet known.
    rows = schedule_mod.changes(conn, since, now, days=days)
    print(f"changes_total={len(rows)} since={since_iso}")
    for r in rows:
        print(f"{r['date']} {r['kind']} {r['name']} {r['old']} -> {r['new']}")
    return 0
