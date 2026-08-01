"""CLI acceptance tests for `thesis load|list|review` (spec SA-11) and the
CLI_EXTENSIONS hook (spec B1) — `cli.py` is never edited by this subtask;
registration is proven the same way `test_cli_schedule.py` proves it for
`schedule`: monkeypatch `cli.CLI_EXTENSIONS`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_intel import cli
from market_intel import db as db_mod

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def _register_thesis_extension(monkeypatch):
    monkeypatch.setattr(cli, "CLI_EXTENSIONS", cli.CLI_EXTENSIONS + ["market_intel.cli_thesis"])


def _use_settings_env(monkeypatch, settings) -> None:
    monkeypatch.setenv("MI_DB_PATH", settings.db_path)
    monkeypatch.setenv("MI_RAW_DIR", settings.raw_dir)
    monkeypatch.setenv("MI_LOG_DIR", settings.log_dir)


def _valid_file(tmp_path) -> str:
    data = {
        "schema_version": "thesis.1",
        "themes": {
            "ai_semi": {
                "label": "AI·반도체",
                "theses": [
                    {
                        "id": "ai_semi_1", "slot": 1, "statement": "테스트 가설",
                        "leading_indicators": ["DGS10 value"], "next_check_date": "2026-12-01",
                        "conditions": {
                            "falsify": [{"id": "f1", "kind": "threshold", "category": "macro",
                                          "subject": "DGS10", "metric": "value", "op": ">=", "value": 999.0}],
                            "weaken": [], "strengthen": [],
                        },
                    }
                ],
            },
            "power_energy": {"label": "전력·에너지", "theses": []},
            "fin_credit": {"label": "금융·신용", "theses": []},
            "consumer_cycle": {"label": "소비·경기", "theses": []},
            "policy_geo": {"label": "정책·지정학", "theses": []},
        },
    }
    p = tmp_path / "theses.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_thesis_subcommands_are_registered_via_the_extension_hook():
    parser = cli.build_parser()
    args = parser.parse_args(["thesis", "load", "--file", "x.json", "--check"])
    assert args.command == "thesis" and args.thesis_command == "load"
    assert args.file == "x.json" and args.check is True

    args2 = parser.parse_args(["thesis", "list"])
    assert args2.thesis_command == "list"

    args3 = parser.parse_args(["thesis", "review", "--file", "r.json", "--dry-run"])
    assert args3.thesis_command == "review" and args3.dry_run is True


def test_thesis_load_then_list(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    path = _valid_file(tmp_path)

    rc = cli.main(["thesis", "load", "--file", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "theses_loaded=1" in out and "themes=5" in out and "rejected=0" in out

    rc2 = cli.main(["thesis", "list"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "theses_total=1" in out2
    assert "ai_semi_1" in out2


def test_thesis_load_rejects_and_exits_2(settings, capsys, monkeypatch):
    _use_settings_env(monkeypatch, settings)
    bad = str(FIXTURES / "theses_no_falsify.json")
    rc = cli.main(["thesis", "load", "--file", bad])
    out = capsys.readouterr().out
    assert rc == 2
    assert "thesis_load_error=" in out


def test_thesis_load_rejection_does_not_clobber_existing_load(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    good = _valid_file(tmp_path)
    cli.main(["thesis", "load", "--file", good])
    capsys.readouterr()

    bad = str(FIXTURES / "theses_no_falsify.json")
    rc = cli.main(["thesis", "load", "--file", bad])
    assert rc == 2
    capsys.readouterr()

    cli.main(["thesis", "list"])
    out = capsys.readouterr().out
    assert "theses_total=1" in out  # unchanged, no partial load


def test_thesis_load_check_does_not_write_db(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    path = _valid_file(tmp_path)
    rc = cli.main(["thesis", "load", "--file", path, "--check"])
    assert rc == 0
    capsys.readouterr()
    cli.main(["thesis", "list"])
    out = capsys.readouterr().out
    assert "theses_total=0" in out


def test_thesis_review_prints_verdicts_and_summary(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    path = _valid_file(tmp_path)
    cli.main(["thesis", "load", "--file", path])
    capsys.readouterr()

    report = {
        "schema_version": "2a.1", "report_type": "morning", "report_date": "2026-08-01",
        "cutoff_kst": "2026-08-01T08:00:00+09:00", "cutoff_utc": "2026-07-31T23:00:00+00:00",
        "generated_at": "2026-08-01T00:00:00+00:00", "title": "t", "headline": "h", "data_status": "unverified",
        "facts": [], "market_reaction": [], "events": [], "schedule_changes": [], "missing": [],
        "interpretation": {"reading": "", "counter_reading": "", "thesis_impact": "", "next_check": "",
                            "generated_by": "", "generated_at": ""},
        "meta": {},
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    rc = cli.main(["thesis", "review", "--file", str(report_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "verdict=판정 불가 thesis_id=ai_semi_1" in out
    assert "thesis: 강화=0 유지=0 약화=0 무효=0 판정불가=1 변화=0" in out


def test_thesis_review_dry_run_does_not_write_reviews(settings, capsys, monkeypatch, tmp_path):
    _use_settings_env(monkeypatch, settings)
    path = _valid_file(tmp_path)
    cli.main(["thesis", "load", "--file", path])
    capsys.readouterr()

    report = {
        "schema_version": "2a.1", "report_type": "morning", "report_date": "2026-08-01",
        "cutoff_kst": "2026-08-01T08:00:00+09:00", "cutoff_utc": "2026-07-31T23:00:00+00:00",
        "generated_at": "2026-08-01T00:00:00+00:00", "title": "t", "headline": "h", "data_status": "unverified",
        "facts": [], "market_reaction": [], "events": [], "schedule_changes": [], "missing": [],
        "interpretation": {"reading": "", "counter_reading": "", "thesis_impact": "", "next_check": "",
                            "generated_by": "", "generated_at": ""},
        "meta": {},
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    cli.main(["thesis", "review", "--file", str(report_path), "--dry-run"])
    capsys.readouterr()

    conn = db_mod.connect(settings.db_path)
    n = conn.execute("SELECT COUNT(*) c FROM thesis_reviews").fetchone()["c"]
    conn.close()
    assert n == 0
