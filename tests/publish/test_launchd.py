"""ST3 acceptance tests — launchd plist templates (spec B10 + the
orchestrator's decision 1 timetable).

Decision 1 moved collection *ahead* of the blackout: without it the
07:15 morning blackout can never see the 07:20 collect and the report
comes out empty every single day (reproduced by the ST2 judge). These
tests pin the resulting timetable so it cannot silently drift back.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

LAUNCHD_DIR = Path(__file__).resolve().parents[2] / "launchd"

# job -> the StartCalendarInterval entries it must declare, KST local time.
# launchd Weekday: 0/7=Sun, 1=Mon … 6=Sat.
EXPECTED = {
    "collect-am":   [{"Weekday": w, "Hour": 6, "Minute": 50} for w in (1, 2, 3, 4, 5)],
    "morning":      [{"Weekday": w, "Hour": 7, "Minute": 40} for w in (2, 3, 4, 5)],
    "collect-pm":   [{"Weekday": w, "Hour": 15, "Minute": 50} for w in (1, 2, 3, 4, 5)],
    "close":        [{"Weekday": w, "Hour": 16, "Minute": 15} for w in (1, 2, 3, 4, 5)],
    "collect-full": [{"Weekday": 6, "Hour": 8, "Minute": 0}, {"Day": 1, "Hour": 8, "Minute": 0}],
    # 2026-08-20: 토 08:30 -> 월 07:40. `weekly_review`가 `week_start`를 흡수해
    # 월요일 아침 한 장이 됐다(`weekstart` job/plist는 삭제).
    "weekly":       [{"Weekday": 1, "Hour": 7, "Minute": 40}],
    "monthly":      [{"Day": 1, "Hour": 8, "Minute": 30}],
    "eventwatch":   [{"Hour": 13, "Minute": 0}, {"Hour": 22, "Minute": 0}],
}


INSTALL_ROOT = "/opt/market-intel-test"


def load(job: str) -> dict:
    """Parse the template **as installed** — i.e. after the same
    `sed s#<REPO_ROOT>#…#g` the CEO runs. `<REPO_ROOT>` is deliberately
    kept as literal text (so that one sed command works verbatim), which
    means the raw template is not well-formed XML on its own; what has to
    parse is the substituted result, because that is the file
    `launchctl bootstrap` reads."""
    path = LAUNCHD_DIR / f"com.kangtaeklee.market-intel.{job}.plist.template"
    text = path.read_text(encoding="utf-8").replace("<REPO_ROOT>", INSTALL_ROOT)
    return plistlib.loads(text.encode("utf-8"))


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_template_exists_and_parses(job):
    data = load(job)
    assert data["Label"] == f"com.kangtaeklee.market-intel.{job}", data["Label"]


def _normalise(entries) -> list[list[tuple]]:
    entries = [entries] if isinstance(entries, dict) else entries
    return sorted(sorted(d.items()) for d in entries)


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_schedule_matches_decision_1(job):
    data = load(job)
    assert _normalise(data["StartCalendarInterval"]) == _normalise(EXPECTED[job]), job


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_no_machine_specific_absolute_paths(job):
    """spec B10 — 머신별 절대경로를 커밋된 파일에 하드코딩하지 않는다."""
    raw = (LAUNCHD_DIR / f"com.kangtaeklee.market-intel.{job}.plist.template").read_text(
        encoding="utf-8")
    assert "<REPO_ROOT>" in raw
    assert "/Users/" not in raw, "a real home directory leaked into a committed template"


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_run_at_load_false_and_runs_the_job_script(job):
    data = load(job)
    assert data.get("RunAtLoad") is False, "spec B10: RunAtLoad=false"
    args = data["ProgramArguments"]
    assert args[0] == "/bin/zsh"
    assert args[1] == f"{INSTALL_ROOT}/scripts/run_job.sh"
    assert args[2] == job
    assert data["WorkingDirectory"] == INSTALL_ROOT
    assert "var/logs/" in data["StandardOutPath"]
    assert "var/logs/" in data["StandardErrorPath"]


def test_collection_precedes_every_blackout():
    """The whole point of decision 1: for each report job, the collect job
    feeding it must finish *before* that report's blackout."""
    from market_intel.reporting.cutoff import _FIXED_TIME

    feeds = {"morning": "collect-am", "weekly": "collect-am", "close": "collect-pm"}
    report_type = {"morning": "morning", "weekly": "weekly_review", "close": "close_delta"}
    for job, collector in feeds.items():
        blackout = _FIXED_TIME[report_type[job]]
        c = EXPECTED[collector][0]
        assert (c["Hour"], c["Minute"]) < (blackout.hour, blackout.minute), (
            f"{collector} starts at {c['Hour']}:{c['Minute']} but {job}'s blackout is {blackout}")
