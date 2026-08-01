"""ST3 acceptance tests — job runner: lock, catch-up, pipeline (spec B10)."""
from __future__ import annotations

import fcntl
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from market_intel import jobs as jobs_mod
from market_intel.reporting.cutoff import KST

from conftest import make_report, write_report


@pytest.fixture
def job_env(tmp_path, settings):
    """settings comes from the repo-root conftest (tmp-path db/raw/logs)."""
    from market_intel import db as db_mod

    db_mod.init_db(settings.db_path)
    return {
        "settings": settings,
        "reports_root": tmp_path / "reports",
        "docs_root": tmp_path / "docs",
        "vault_root": tmp_path / "vault",
    }


def run(job_env, name, **kw):
    calls = kw.pop("calls", [])

    def fake_collect(settings, workflow):
        calls.append(workflow)
        return True

    kw.setdefault("collect", fake_collect)
    return jobs_mod.run_job(
        job_env["settings"], name,
        reports_root=job_env["reports_root"], docs_root=job_env["docs_root"],
        vault_root=job_env["vault_root"], publish=False, **kw,
    )


# --- lock -----------------------------------------------------------------

def test_job_lock(job_env):
    """spec ST3 `test_job_lock` — the second concurrent run reports
    `already_running` and exits 0 (a skipped duplicate is not a failure)."""
    lock_path = jobs_mod.lock_path(job_env["settings"], "morning")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST))
        assert result["lock"] == "already_running"
        assert result["exit"] == 0
        assert all(v == "skip" for v in result["steps"].values()), result["steps"]
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_lock_released_after_run(job_env):
    run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST))
    result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 41, tzinfo=KST))
    assert result["lock"] == "acquired"


def test_no_shell_flock_used():
    """spec §Environment gotchas — macOS has no `flock(1)`; the lock must be
    Python's `fcntl.flock`, never a shelled-out binary."""
    src = (Path(jobs_mod.__file__)).read_text(encoding="utf-8")
    assert "fcntl.flock" in src
    assert '"flock"' not in src and "'flock'" not in src
    assert '"timeout"' not in src and "'timeout'" not in src


# --- catch-up -------------------------------------------------------------

def test_slot_dates_respects_job_weekdays(job_env):
    """`morning` runs Tue–Fri, `weekstart` only Mon — the catch-up must not
    invent a Monday morning report the schedule never asked for."""
    today = date(2026, 8, 7)
    morning = jobs_mod.slot_dates("morning", today)
    assert all(d.weekday() in (1, 2, 3, 4) for d in morning), morning
    weekstart = jobs_mod.slot_dates("weekstart", today)
    assert all(d.weekday() == 0 for d in weekstart), weekstart
    monthly = jobs_mod.slot_dates("monthly", date(2026, 8, 3))
    assert monthly == [date(2026, 8, 1)], monthly


def test_catchup_backfills(job_env):
    """spec ST3 `test_catchup_backfills` — a slot whose JSON is missing is
    regenerated **with that day's blackout**, and is labelled late."""
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    slots = jobs_mod.slot_dates("morning", now.date())
    past = [d for d in slots if d != now.date()]
    assert len(past) >= 2, past
    missing = past[0]
    for d in past[1:]:
        write_report(job_env["reports_root"], make_report("morning", d.isoformat()))

    result = run(job_env, "morning", now=now)
    assert result["catchup_generated"] == 1, result
    assert result["exit"] == 0

    path = job_env["reports_root"] / "morning" / f"{missing.isoformat()}.json"
    assert path.exists(), f"{missing} was not backfilled"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["meta"]["late_generation"] is True
    assert data["cutoff_kst"] == f"{missing.isoformat()}T07:15:00+09:00", data["cutoff_kst"]


def test_catchup_never_uses_today_cutoff(job_env):
    """The hindsight ban (spec B10): a backfilled report must not be able to
    see anything published after its own historical blackout."""
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    run(job_env, "morning", now=now)
    for path in (job_env["reports_root"] / "morning").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["cutoff_kst"].startswith(data["report_date"]), (
            f"{path.name}: cutoff {data['cutoff_kst']} is not that report_date's blackout")


def test_catchup_is_noop_when_present(job_env):
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    for d in jobs_mod.slot_dates("morning", now.date()):
        write_report(job_env["reports_root"], make_report("morning", d.isoformat()))
    result = run(job_env, "morning", now=now)
    assert result["catchup_generated"] == 0


# --- pipeline -------------------------------------------------------------

def test_report_job_does_not_collect(job_env):
    """Orchestrator decision 1 — a report job only reports. Collecting at
    07:40 would write facts its own 07:15 blackout is obliged to ignore;
    `collect-am` at 06:50 is what feeds it."""
    calls: list[str] = []
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    result = run(job_env, "morning", now=now, calls=calls)
    assert calls == [], calls
    assert result["steps"]["collect"] == "skip"
    assert result["steps"]["report"] == "ok"
    assert result["steps"]["site"] == "ok"
    assert result["steps"]["obsidian"] == "ok"
    assert result["steps"]["publish"] == "skip"  # publish=False
    assert result["exit"] == 0
    assert (job_env["docs_root"] / "index.html").exists()
    assert (job_env["vault_root"] / "2026" / "2026-08-07-morning.md").exists()


def test_collect_job_skips_report_step(job_env):
    calls: list[str] = []
    result = run(job_env, "collect-am", now=datetime(2026, 8, 7, 6, 50, tzinfo=KST), calls=calls)
    assert result["steps"]["collect"] == "ok"
    assert result["steps"]["report"] == "skip"
    assert result["exit"] == 0
    assert calls == ["morning", "calendar", "events"], calls


def test_step_failure_does_not_block_the_rest(job_env):
    """spec ST3 What #3 — 각 단계 실패는 다음 단계를 막지 않는다."""
    def boom(settings, workflow):
        raise RuntimeError("provider exploded")

    result = run(job_env, "collect-am", now=datetime(2026, 8, 7, 6, 50, tzinfo=KST), collect=boom)
    assert result["steps"]["collect"] == "fail"
    assert result["steps"]["site"] == "ok"
    assert result["steps"]["obsidian"] == "ok"
    assert result["exit"] == 0, "a dead source must not fail the job (spec B13)"


def test_every_job_name_has_a_launchd_template():
    templates = {p.name.split(".plist")[0].rsplit(".", 1)[-1]
                 for p in (Path(jobs_mod.__file__).resolve().parents[2]
                           / "launchd").glob("*.plist.template")}
    assert set(jobs_mod.JOBS) == templates, (set(jobs_mod.JOBS) ^ templates)
