"""Unattended-execution smoke test through a real subprocess.

In-process `cli.main()` calls cannot prove what cron needs to know: that the
installed entry point starts, exits 0, and never blocks on a TTY. These run
the module as its own process with stdin closed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALL_PROVIDER_NAMES = ("yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart")


def _run_cli(args: list[str], tmp_path: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MI_DB_PATH"] = str(tmp_path / "market_intel.db")
    env["MI_RAW_DIR"] = str(tmp_path / "raw")
    env["MI_LOG_DIR"] = str(tmp_path / "logs")
    return subprocess.run(
        [sys.executable, "-m", "market_intel.cli", *args],
        cwd=str(cwd or PROJECT_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,  # any prompt would hang/EOF-crash instead of passing
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_init_then_db_stats_exit_zero_with_no_stdin(tmp_path):
    init_result = _run_cli(["init"], tmp_path)
    assert init_result.returncode == 0, init_result.stderr

    stats_result = _run_cli(["db", "stats"], tmp_path)
    assert stats_result.returncode == 0, stats_result.stderr
    out = stats_result.stdout
    assert "facts_total=0" in out
    assert "facts_by_provider:" in out
    assert "facts_by_status:" in out
    assert "core16_price_coverage=0/16" in out
    assert "last_run: none" in out


def test_db_stats_before_init_does_not_crash(tmp_path):
    result = _run_cli(["db", "stats"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "facts_total=0" in result.stdout
    assert "Traceback" not in result.stderr


def test_unknown_workflow_is_rejected_without_prompting(tmp_path):
    result = _run_cli(["collect", "--workflow", "nonsense"], tmp_path)
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_invalid_cutoff_is_rejected_without_a_raw_traceback(tmp_path):
    """repair.md finding #7: a bad --cutoff must fail cleanly (cron can
    already detect the non-zero exit) without dumping an unhandled
    ValueError/Traceback to stderr."""
    init_result = _run_cli(["init"], tmp_path)
    assert init_result.returncode == 0, init_result.stderr

    result = _run_cli(["collect", "--workflow", "morning", "--cutoff", "not-a-date"], tmp_path)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "invalid --cutoff value" in result.stderr


def test_runs_from_a_foreign_cwd_without_creating_files_there(tmp_path):
    foreign = tmp_path / "foreign_cwd"
    foreign.mkdir()
    db_dir = tmp_path / "state"
    db_dir.mkdir()

    result = _run_cli(["init"], db_dir, cwd=foreign)
    assert result.returncode == 0, result.stderr
    assert list(foreign.iterdir()) == [], "cron-style foreign cwd must stay untouched"
