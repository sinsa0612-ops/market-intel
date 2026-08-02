"""ST3 acceptance tests — CLI output format (spec B13).

These parse the output the way jobs.py, the CEO and any future tooling
will: `key=value`. A cosmetic change to these lines is a contract break,
so it is asserted, not eyeballed.
"""
from __future__ import annotations

import re
import subprocess
import sys

import pytest

from market_intel import cli as cli_mod
from market_intel import cli_publish as cli_publish_mod
from market_intel import jobs as jobs_mod
from market_intel import obsidian as obsidian_mod
from market_intel import site as site_mod


def run_cli(monkeypatch, tmp_path, capsys, argv, **env):
    """Run the CLI fully sandboxed.

    `site build` / `job run` take their `reports/` and `docs/` roots from
    `PROJECT_ROOT` when the CLI does not override them — which is correct in
    production and destructive in a test: an un-patched run wipes the real
    `docs/` and writes catch-up reports into the real `reports/`. (Observed,
    not theorised: the first version of this file did exactly that.)
    """
    monkeypatch.setenv("MI_DB_PATH", str(tmp_path / "market_intel.db"))
    monkeypatch.setenv("MI_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("MI_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MI_OBSIDIAN_DIR", str(tmp_path / "vault"))
    for mod in (site_mod, jobs_mod, obsidian_mod):
        monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    code = cli_mod.main(argv)
    return code, capsys.readouterr().out


def test_sandbox_is_effective(monkeypatch, tmp_path, capsys):
    """The guard above must actually hold: nothing may be written under the
    real project root by a CLI test."""
    from market_intel.config import PROJECT_ROOT as REAL_ROOT

    before = sorted(p.name for p in (REAL_ROOT / "docs").glob("*")) \
        if (REAL_ROOT / "docs").exists() else None
    run_cli(monkeypatch, tmp_path, capsys, ["site", "build"])
    after = sorted(p.name for p in (REAL_ROOT / "docs").glob("*")) \
        if (REAL_ROOT / "docs").exists() else None
    assert before == after, "a CLI test wrote into the real docs/"
    assert (tmp_path / "docs" / "index.html").exists()


def test_extension_is_registered_via_the_hook():
    """spec B1 — ST3 reaches the CLI only through `CLI_EXTENSIONS`."""
    assert "market_intel.cli_publish" in cli_mod.CLI_EXTENSIONS
    parser = cli_mod.build_parser()
    for name in ("site", "obsidian", "job"):
        assert name in parser._subparsers._group_actions[0].choices


def test_site_build_output_format(monkeypatch, tmp_path, capsys):
    code, out = run_cli(monkeypatch, tmp_path, capsys, ["site", "build"])
    assert code == 0
    assert re.search(r"^site_pages=\d+ reports_indexed=\d+ latest=\S* out=/\S+$",
                     out.strip().splitlines()[-1]), out


def test_obsidian_sync_output_format(monkeypatch, tmp_path, capsys):
    code, out = run_cli(monkeypatch, tmp_path, capsys, ["obsidian", "sync"])
    assert code == 0
    assert re.search(r"^obsidian_written=\d+ vault=/\S+$", out.strip().splitlines()[0]), out
    assert str(tmp_path / "vault") in out


def test_obsidian_sync_since_is_parsed(monkeypatch, tmp_path, capsys):
    code, _ = run_cli(monkeypatch, tmp_path, capsys,
                      ["obsidian", "sync", "--since", "2026-07-01"])
    assert code == 0


def test_job_run_output_format(monkeypatch, tmp_path, capsys):
    """The B13 block, verbatim: 4 lines, in order."""
    calls = []
    monkeypatch.setattr(jobs_mod, "_default_collect",
                        lambda settings, workflow: calls.append(workflow))
    # 해석 단계도 collect와 같은 이유로 가짜다 — 진짜로 두면 이 형식 테스트가
    # 로컬 ollama에 의존하고 리포트 한 건당 25~40초씩 걸린다.
    monkeypatch.setattr(jobs_mod, "_default_interpret",
                        lambda settings, conn, path: {"status": "ok"})
    code, out = run_cli(monkeypatch, tmp_path, capsys,
                        ["job", "run", "--name", "morning", "--no-publish"])
    lines = out.strip().splitlines()
    assert code == 0
    assert re.match(r"^job=morning lock=(acquired|already_running)$", lines[0]), lines
    assert re.match(r"^catchup_generated=\d+$", lines[1]), lines
    # 2단계-B ST3가 `interpret`를 report와 site 사이에 끼워 넣었다(spec ST3
    # What #2). B13의 5단계 줄은 그 시점에 대체된 계약이다.
    assert re.match(
        r"^steps: collect=\w+ report=\w+ interpret=\w+ site=\w+ obsidian=\w+ publish=\w+$",
        lines[2]), lines
    assert lines[3] == "exit=0", lines


def test_job_run_rejects_unknown_job(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, tmp_path, capsys, ["job", "run", "--name", "nope"])


def test_no_tty_prompt_in_new_modules():
    """spec A8/B13 — these run unattended under launchd."""
    from pathlib import Path

    for mod in (cli_publish_mod, jobs_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "input(" not in src
        assert "getpass" not in src
