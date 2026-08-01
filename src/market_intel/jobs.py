"""Unattended job runner (spec B10) — locking, catch-up, pipeline.

**Timetable (orchestrator decision 1, which overrides B10's 6-job list).**
The ST2 judge reproduced the defect: the `morning` blackout is 07:15 but
the only collect ran at/after 07:20, so every morning report came out
structurally empty. The blackout is a fixed spec value and stays fixed;
what moves is collection. Collect jobs therefore run *before* the
blackout they feed, and are separate launchd entries from the report
jobs that consume them:

    06:50 Mon–Fri  collect-am    morning + calendar + events
    07:40 Mon      weekstart     report week_start   (blackout 07:15)
    07:40 Tue–Fri  morning       report morning      (blackout 07:15)
    15:50 Mon–Fri  collect-pm    close
    16:15 Mon–Fri  close         report close_delta  (blackout 16:15)
    08:00 Sat/1st  collect-full  all
    08:30 Sat      weekly        report weekly_review
    08:30 1st      monthly       report monthly
    13:00 & 22:00  eventwatch    events (공시·실적 감시)

Two consequences worth stating plainly:
  * a report job does **not** collect (collecting at 07:40 would only write
    facts its own 07:15 blackout must ignore), so its `collect=` step reads
    `skip` — B13's `<ok|fail>` gains a third, honest value;
  * `close`'s blackout equals its own run time, so `collect-pm` at 15:50 is
    what makes it non-empty.

**Locking** is `fcntl.flock(LOCK_EX|LOCK_NB)` on `var/locks/<job>.lock`
(spec B10) — macOS ships no `flock(1)`, so a shell lock would die with
`command not found` and every overlapping run would proceed silently.

**Catch-up** (spec B10): a job first re-checks its own last 7 days of
slots and regenerates any whose JSON is missing — using *that day's*
blackout, never now. Hindsight is the thing being prevented, so the
backfilled report is stamped `meta.late_generation=true` and the site
badges it 지연 생성 instead of passing it off as timely.
"""
from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db as db_mod
from . import obsidian as obsidian_mod
from . import site as site_mod
from .config import PROJECT_ROOT
from .reporting.build import build_report, stem_for
from .reporting.cutoff import KST, cutoff_for

CATCHUP_DAYS = 7  # spec B10 — 최근 7일치 자기 슬롯을 점검

# Python weekday(): Mon=0 … Sun=6.
_WEEKDAYS = "월화수목금토일"
MON_FRI = {0, 1, 2, 3, 4}

# collect: workflows to run · report: report type or None · the rest is the
# calendar this job occupies, used by `slot_dates` for catch-up.
JOBS: dict[str, dict] = {
    "collect-am": {"collect": ["morning", "calendar", "events"], "report": None,
                   "weekdays": MON_FRI},
    "weekstart": {"collect": [], "report": "week_start", "weekdays": {0}},
    "morning": {"collect": [], "report": "morning", "weekdays": {1, 2, 3, 4}},
    "collect-pm": {"collect": ["close"], "report": None, "weekdays": MON_FRI},
    "close": {"collect": [], "report": "close_delta", "weekdays": MON_FRI},
    "collect-full": {"collect": ["all"], "report": None, "weekdays": {5}, "monthday": 1},
    "weekly": {"collect": [], "report": "weekly_review", "weekdays": {5}},
    "monthly": {"collect": [], "report": "monthly", "monthday": 1},
    "eventwatch": {"collect": ["events"], "report": None},
}

PUBLISH_SCRIPT = "scripts/publish.sh"
# publish.sh exit codes that mean "a safety gate fired", as opposed to
# "the network was down". Only the former makes the job itself fail.
PUBLISH_HARD_FAILURES = {3, 4}


# --- lock -----------------------------------------------------------------

def lock_path(settings, name: str) -> Path:
    """`var/locks/<job>.lock`, derived from the DB location so a test (or a
    second checkout) with its own `MI_DB_PATH` gets its own locks instead of
    contending with the live installation."""
    return Path(settings.db_path).resolve().parent / "locks" / f"{name}.lock"


@contextmanager
def job_lock(settings, name: str):
    """Yields True when the lock was taken, False when another run holds it."""
    path = lock_path(settings, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# --- slots ----------------------------------------------------------------

def slot_dates(name: str, today: date, days: int = CATCHUP_DAYS) -> list[date]:
    """The dates in the last `days` (inclusive of `today`) on which this job
    was scheduled to run. A job that runs Tue–Fri must not backfill a Monday
    report the timetable never asked for."""
    spec = JOBS[name]
    weekdays = spec.get("weekdays")
    monthday = spec.get("monthday")
    out = []
    for back in range(days - 1, -1, -1):
        d = today - timedelta(days=back)
        if weekdays is None and monthday is None:
            out.append(d)
        elif weekdays is not None and d.weekday() in weekdays:
            out.append(d)
        elif monthday is not None and d.day == monthday:
            out.append(d)
    return out


# --- report generation ----------------------------------------------------

def report_path(reports_root: Path, report_type: str, report_date: date,
                stem: str | None = None) -> Path:
    if stem is None:
        stem = _stem_without_building(report_type, report_date)
    return reports_root / report_type / f"{stem}.json"


def _stem_without_building(report_type: str, report_date: date) -> str:
    """`stem_for` needs a built Report; catch-up needs to know the filename
    *before* deciding whether to spend a build. Same B6 table, keyed off the
    date alone — which is all the 5 scheduled types need."""
    d = report_date.isoformat()
    if report_type == "monthly":
        return d[:7]
    return d


def generate_report(conn, report_type: str, report_date: date, reports_root: Path,
                    late: bool = False) -> Path:
    cutoff = cutoff_for(report_type, report_date)
    report = build_report(conn, report_type, report_date, cutoff)
    report.meta["late_generation"] = late
    path = reports_root / report_type / f"{stem_for(report)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json(), encoding="utf-8")
    return path


# --- steps ----------------------------------------------------------------

def _default_collect(settings, workflow: str) -> bool:
    from .engine import run_collect
    from .http_client import configure_logging
    from .providers import PROVIDERS
    from .universe import UNIVERSE

    run_collect(settings, UNIVERSE, PROVIDERS, workflow, None,
                logger=configure_logging(settings))
    return True


def _run_publish(repo_root: Path) -> tuple[str, int]:
    script = repo_root / PUBLISH_SCRIPT
    if not script.exists():
        return "skip", 0
    proc = subprocess.run(["bash", str(script)], cwd=repo_root,
                          capture_output=True, text=True, check=False)
    for line in (proc.stdout + proc.stderr).splitlines():
        print(f"  publish| {line}")
    if proc.returncode == 0:
        return "ok", 0
    if proc.returncode in PUBLISH_HARD_FAILURES:
        return "fail", proc.returncode
    # Offline / no credentials: spec B11-5 — log it and keep the job green,
    # the next run retries by itself.
    return "fail", 0


def run_job(settings, name: str, *, publish: bool = True, now: datetime | None = None,
            reports_root: Path | None = None, docs_root: Path | None = None,
            vault_root: Path | None = None, collect=None,
            repo_root: Path | None = None) -> dict:
    """Run one job end to end. Never raises for a dead source: each step is
    isolated and a failure is recorded, not propagated (spec ST3 What #3)."""
    if name not in JOBS:
        raise ValueError(f"run_job: unknown job {name!r}")
    spec = JOBS[name]
    reports_root = Path(reports_root) if reports_root else PROJECT_ROOT / "reports"
    docs_root = Path(docs_root) if docs_root else PROJECT_ROOT / "docs"
    repo_root = Path(repo_root) if repo_root else PROJECT_ROOT
    collect = collect or _default_collect
    now = now or datetime.now(KST)
    today = now.astimezone(KST).date()

    steps = {k: "skip" for k in ("collect", "report", "site", "obsidian", "publish")}
    result = {"job": name, "lock": "already_running", "catchup_generated": 0,
              "steps": steps, "exit": 0}

    with job_lock(settings, name) as acquired:
        if not acquired:
            return result
        result["lock"] = "acquired"

        if spec["collect"]:
            ok = True
            for workflow in spec["collect"]:
                try:
                    collect(settings, workflow)
                except Exception as exc:  # a dead provider must not kill the job
                    print(f"  collect({workflow}) failed: {type(exc).__name__}")
                    ok = False
            steps["collect"] = "ok" if ok else "fail"

        db_mod.init_db(settings.db_path)
        conn = db_mod.connect(settings.db_path)
        try:
            if spec["report"]:
                try:
                    rtype = spec["report"]
                    for d in slot_dates(name, today):
                        if d == today:
                            continue
                        if report_path(reports_root, rtype, d).exists():
                            continue
                        generate_report(conn, rtype, d, reports_root, late=True)
                        result["catchup_generated"] += 1
                    generate_report(conn, rtype, today, reports_root, late=False)
                    steps["report"] = "ok"
                except Exception as exc:
                    print(f"  report failed: {type(exc).__name__}: {exc}")
                    steps["report"] = "fail"

            try:
                site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
                steps["site"] = "ok"
            except Exception as exc:
                print(f"  site failed: {type(exc).__name__}: {exc}")
                steps["site"] = "fail"
        finally:
            conn.close()

        try:
            obsidian_mod.sync(reports_root=reports_root, vault_root=vault_root)
            steps["obsidian"] = "ok"
        except Exception as exc:
            print(f"  obsidian failed: {type(exc).__name__}: {exc}")
            steps["obsidian"] = "fail"

        if publish:
            steps["publish"], result["exit"] = _run_publish(repo_root)

    return result
