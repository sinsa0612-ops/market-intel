"""CLI acceptance test for `report` (spec B13 output format) and the
`CLI_EXTENSIONS` hook (spec B1) wiring it into `cli.py` without `cli.py`
naming this module directly.

`cli_report.PROJECT_ROOT` is monkeypatched to `tmp_path` in every test that
actually writes a JSON file: the real `PROJECT_ROOT` (spec B12 — no env
override exists for `reports/`, by design, since it's a committed path) is
this worktree's own `reports/` directory, and a test suite must not leave
files behind in it on every run."""
from __future__ import annotations

from market_intel import cli
from market_intel import cli_report
from market_intel import db as db_mod


def _use_settings_env(monkeypatch, settings) -> None:
    monkeypatch.setenv("MI_DB_PATH", settings.db_path)
    monkeypatch.setenv("MI_RAW_DIR", settings.raw_dir)
    monkeypatch.setenv("MI_LOG_DIR", settings.log_dir)


def test_report_subcommand_is_registered_via_the_extension_hook():
    parser = cli.build_parser()
    args = parser.parse_args(["report", "--type", "morning", "--date", "2026-08-01"])
    assert args.command == "report"
    assert args.report_type == "morning"
    assert args.date == "2026-08-01"
    assert args.stdout is False


def test_report_writes_json_and_prints_b13_summary(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    monkeypatch.setattr(cli_report, "PROJECT_ROOT", tmp_path)

    rc = cli.main(["report", "--type", "morning", "--date", "2026-08-01"])
    out = capsys.readouterr().out
    assert rc == 0

    lines = out.splitlines()
    assert lines[0] == "report_type=morning report_date=2026-08-01 cutoff_kst=2026-08-01T07:15:00+09:00"
    assert lines[1].startswith("data_status=")
    assert "facts=" in lines[1] and "market_reaction=" in lines[1]
    assert lines[2] == "interpretation=absent"
    assert lines[3].startswith("json=")

    out_path = tmp_path / "reports" / "morning" / "2026-08-01.json"
    assert out_path.exists()
    assert lines[3] == f"json={out_path}"


def test_report_stdout_flag_prints_markdown_in_daily_header_order(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    monkeypatch.setattr(cli_report, "PROJECT_ROOT", tmp_path)

    rc = cli.main(["report", "--type", "morning", "--date", "2026-08-01", "--stdout"])
    out = capsys.readouterr().out
    assert rc == 0

    headers = [line for line in out.splitlines() if line.startswith("## ")]
    assert headers[:7] == [
        "## 시장 한 줄", "## 핵심 사실", "## 시장 반응",
        "## 당시 해석", "## 반대 해석", "## 기존 가설 영향", "## 다음 검증",
    ]
    assert "AI 해석 미생성" in out

    # --stdout keeps the B13 summary too: ST3's jobs.py/site build parse this
    # output, and dropping `json=`/`data_status=` the moment --stdout is added
    # silently breaks that contract (judge.md B13 항목).
    lines = out.splitlines()
    assert lines[0] == "report_type=morning report_date=2026-08-01 cutoff_kst=2026-08-01T07:15:00+09:00"
    assert lines[1].startswith("data_status=") and "market_reaction=" in lines[1]
    assert lines[2] == "interpretation=absent"
    assert lines[3] == f"json={tmp_path / 'reports' / 'morning' / '2026-08-01.json'}"
    assert lines[4].startswith("# 2026-08-01 ["), lines[4]


def test_report_explicit_cutoff_overrides_computed_blackout(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    monkeypatch.setattr(cli_report, "PROJECT_ROOT", tmp_path)

    rc = cli.main([
        "report", "--type", "morning", "--date", "2026-08-01",
        "--cutoff", "2026-08-01T09:00:00+09:00",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cutoff_kst=2026-08-01T09:00:00+09:00" in out
