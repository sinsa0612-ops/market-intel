"""ST3 — the 2B subcommands must be *real console commands* (judge.md 6-1).

`cli_thesis.py`/`cli_interpret.py` were delivered by ST1/ST2 as importable
modules that no one could actually run: their subtasks were forbidden from
touching `cli.py`, so `market-intel interpret` died with
`invalid choice: 'interpret'`. Registration is ST3's job and this file is the
regression net for it — an in-process parser check (fast, exact) plus real
subprocesses (what launchd and the CEO actually invoke: an in-process
`cli.main()` call cannot prove the installed entry point starts at all).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from market_intel import cli as cli_mod

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_EXTENSIONS = (
    "market_intel.cli_schedule",
    "market_intel.cli_report",
    "market_intel.cli_publish",
    "market_intel.cli_thesis",
    "market_intel.cli_interpret",
    "market_intel.cli_ops",
    "market_intel.cli_backfill",
)


def _run_cli(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MI_DB_PATH"] = str(tmp_path / "market_intel.db")
    env["MI_RAW_DIR"] = str(tmp_path / "raw")
    env["MI_LOG_DIR"] = str(tmp_path / "logs")
    return subprocess.run(
        [sys.executable, "-m", "market_intel.cli", *args],
        cwd=str(PROJECT_ROOT), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=180,
    )


def test_cli_extensions_registers_every_subcommand_module():
    assert list(cli_mod.CLI_EXTENSIONS) == list(EXPECTED_EXTENSIONS), cli_mod.CLI_EXTENSIONS


def test_parser_accepts_the_2b_subcommands():
    parser = cli_mod.build_parser()
    assert parser.parse_args(["interpret", "--file", "x.json"]).command == "interpret"
    assert parser.parse_args(["thesis", "list"]).thesis_command == "list"
    assert parser.parse_args(["ops", "status"]).ops_command == "status"


def test_subprocess_thesis_list_is_a_real_console_command(tmp_path):
    result = _run_cli(["thesis", "list"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "theses_total=" in result.stdout
    assert "invalid choice" not in result.stderr


def test_subprocess_ops_status_json_is_a_real_console_command(tmp_path):
    result = _run_cli(["ops", "status", "--json"], tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "jobs" in payload and "generated_at" in payload


def test_subprocess_interpret_is_a_real_console_command(tmp_path):
    """`market-intel interpret --no-llm` on a real report file: exit 0 and the
    SA-11 output shape. This is the exact command judge.md 6-1 could not run."""
    from market_intel.reporting.model import Interpretation, Report

    report = Report(
        report_type="weekly_review", report_date="2026-08-01",
        cutoff_kst="2026-08-01T22:15:00+09:00", cutoff_utc="2026-08-01T13:15:00+00:00",
        generated_at="2026-08-01T13:15:00+00:00", title="테스트", headline="테스트",
        data_status="source_verified", facts=[], market_reaction=[], events=[],
        schedule_changes=[], missing=[], interpretation=Interpretation(), meta={},
    )
    path = tmp_path / "report.json"
    path.write_text(report.to_json(), encoding="utf-8")

    result = _run_cli(["interpret", "--file", str(path), "--no-llm"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "interpret_status=disabled" in result.stdout
    assert "invalid choice" not in result.stderr

    # 손으로 돌린 해석도 `interpretations`에 남아야 한다. 남지 않으면
    # `docs/status.html`의 `마지막 AI 해석`이 실제보다 낡은 값을 보여준다 —
    # CEO의 유일한 감지 수단이 사이트인데 그 화면이 거짓말을 하게 된다.
    import sqlite3

    conn = sqlite3.connect(tmp_path / "market_intel.db")
    try:
        rows = conn.execute("SELECT status, report_type FROM interpretations").fetchall()
    finally:
        conn.close()
    assert rows == [("disabled", "weekly_review")], rows


def test_interpret_dry_run_records_nothing(tmp_path):
    from market_intel.reporting.model import Interpretation, Report

    report = Report(
        report_type="weekly_review", report_date="2026-08-01",
        cutoff_kst="2026-08-01T22:15:00+09:00", cutoff_utc="2026-08-01T13:15:00+00:00",
        generated_at="2026-08-01T13:15:00+00:00", title="테스트", headline="테스트",
        data_status="source_verified", facts=[], market_reaction=[], events=[],
        schedule_changes=[], missing=[], interpretation=Interpretation(), meta={},
    )
    path = tmp_path / "report.json"
    path.write_text(report.to_json(), encoding="utf-8")

    result = _run_cli(["interpret", "--file", str(path), "--no-llm", "--dry-run"], tmp_path)
    assert result.returncode == 0, result.stderr

    import sqlite3

    conn = sqlite3.connect(tmp_path / "market_intel.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM interpretations").fetchone()[0] == 0
    finally:
        conn.close()
