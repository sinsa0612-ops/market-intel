"""`interpret` CLI tests (spec SA-11). Registered into `cli.py` only via the
monkeypatched `CLI_EXTENSIONS` list, the same technique spec's own ST1 test
guidance describes — this worktree does not modify `cli.py` (ST3's file)."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from market_intel import cli as cli_mod
from market_intel import cli_interpret as cli_interpret_mod
from market_intel import db as db_mod

from tests.interp.conftest import make_report


def run_cli(monkeypatch, tmp_path, capsys, argv):
    monkeypatch.setenv("MI_DB_PATH", str(tmp_path / "market_intel.db"))
    monkeypatch.setenv("MI_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setattr(cli_mod, "CLI_EXTENSIONS", cli_mod.CLI_EXTENSIONS + ["market_intel.cli_interpret"])
    code = cli_mod.main(argv)
    return code, capsys.readouterr().out


def _write_report(tmp_path) -> Path:
    report = make_report()
    path = tmp_path / "report.json"
    path.write_text(report.to_json(), encoding="utf-8")
    return path


def test_no_llm_dry_run_disabled_status_no_write(monkeypatch, tmp_path, capsys):
    path = _write_report(tmp_path)
    before = path.read_text(encoding="utf-8")
    code, out = run_cli(monkeypatch, tmp_path, capsys, ["interpret", "--file", str(path), "--no-llm", "--dry-run"])
    assert code == 0
    assert "interpret_status=disabled" in out
    assert path.read_text(encoding="utf-8") == before  # dry-run never writes


def test_no_llm_writes_back_to_input_file_by_default(monkeypatch, tmp_path, capsys):
    path = _write_report(tmp_path)
    code, out = run_cli(monkeypatch, tmp_path, capsys, ["interpret", "--file", str(path), "--no-llm"])
    assert code == 0
    assert f"json={path}" in out
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["meta"]["interpretation"]["status"] == "disabled"


def test_out_flag_writes_elsewhere_leaves_input_untouched(monkeypatch, tmp_path, capsys):
    path = _write_report(tmp_path)
    before = path.read_text(encoding="utf-8")
    out_path = tmp_path / "out.json"
    code, out = run_cli(
        monkeypatch, tmp_path, capsys,
        ["interpret", "--file", str(path), "--no-llm", "--out", str(out_path)],
    )
    assert code == 0
    assert path.read_text(encoding="utf-8") == before
    assert out_path.exists()
    assert f"json={out_path}" in out


def test_llm_unavailable_exit_0_and_missing_entry(monkeypatch, tmp_path, capsys):
    """The scenario spec's own success criteria script (1) checks (there run
    as `MI_LLM_HOST=http://127.0.0.1:1 uv run market-intel interpret ...`,
    a fresh subprocess — see result.md for that exact command's real output;
    `market_intel.interp.llm.DEFAULT_HOST` is a module-level constant read
    once at import, so an in-process `monkeypatch.setenv` here cannot
    reproduce the same cold-import path faithfully). Here the same
    unreachable-host failure is induced directly at the call site instead,
    to prove the CLI's own status/exit-code/`missing`-entry wiring): an
    unreachable host must exit 0 with interpret_status=llm_unavailable and a
    `missing` entry with area 'AI 해석'."""
    import market_intel.interp.llm as real_llm_mod

    def fake(*a, **k):
        raise real_llm_mod.LLMUnavailable("connection refused")

    monkeypatch.setattr(cli_interpret_mod.apply_mod.llm_mod, "generate_json", fake)

    path = _write_report(tmp_path)
    out_path = tmp_path / "mi-out.json"
    code, out = run_cli(
        monkeypatch, tmp_path, capsys,
        ["interpret", "--file", str(path), "--out", str(out_path)],
    )
    assert code == 0
    assert "interpret_status=llm_unavailable" in out
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["interpretation"]["reading"] == ""
    assert any(m["area"] == "AI 해석" for m in written["missing"])


def test_thesis_module_absent_no_thesis_line(monkeypatch, tmp_path, capsys):
    """A build without ST1's `interp/thesis.py` must degrade to no thesis
    line, not crash.

    Written when that was this worktree's natural state; ST1 is merged now,
    so absence is *simulated* rather than relied on. The contract still
    matters — `interpret` must not require the thesis layer to be present."""
    monkeypatch.setattr(cli_interpret_mod, "_load_thesis_module", lambda: None)
    path = _write_report(tmp_path)
    code, out = run_cli(monkeypatch, tmp_path, capsys, ["interpret", "--file", str(path), "--no-llm"])
    assert code == 0
    assert "thesis:" not in out


def test_thesis_module_present_prints_thesis_line_and_records_reviews(monkeypatch, tmp_path, capsys):
    """Simulates ST1's `interp/thesis.py` being merged in, via a fake module
    injected into `sys.modules` — proves the optional-import integration
    shape in `cli_interpret._thesis_summary` without needing ST1's real
    code (not present in this worktree)."""
    fake = types.ModuleType("market_intel.interp.thesis")
    fake.load_file = lambda path: [{"id": "ai_semi_1"}]

    def fake_review(conn, theses, cutoff, report_type, report_date):
        return [{"thesis_id": "ai_semi_1", "verdict": "유지", "changed": False}]

    fake.review = fake_review
    # ST4: `_thesis_summary` now calls `render_impact` with `report_date` and
    # the new `states=`/`introduced_on=` keywords (spec ST4 item 2 — the real
    # `thesis.render_impact` accepts them as default kwargs precisely so this
    # kind of stand-in module keeps working). The fake mirrors that surface;
    # its canned return text is unchanged.
    fake.render_impact = (
        lambda reviews, report_date="", states=None, introduced_on="":
        "가설 1건 판정 — 강화 0 · 유지 1 · 약화 0 · 무효 0 · 판정 불가 0."
    )
    fake.render_next_check_suffix = lambda reviews, report: "가설 재점검 2026-11-01"
    monkeypatch.setitem(sys.modules, "market_intel.interp.thesis", fake)

    path = _write_report(tmp_path)
    code, out = run_cli(monkeypatch, tmp_path, capsys, ["interpret", "--file", str(path), "--no-llm"])
    assert code == 0
    # spec SA-11's own example prints "판정불가=N" with no space (key=value
    # tokens must stay whitespace-splittable — see cli_interpret._thesis_line).
    assert "thesis: 강화=0 유지=1 약화=0 무효=0 판정불가=0 변화=0" in out

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["interpretation"]["thesis_impact"] != ""
    assert written["meta"]["interpretation"]["thesis_reviews"] == [
        {"thesis_id": "ai_semi_1", "verdict": "유지", "changed": False}
    ]


def test_thesis_line_verdict_key_has_no_embedded_space():
    """`판정 불가` (the SA-2 verdict enum value, WITH a space) must render as
    the bare key `판정불가` (NO space) — SA-11's own example line does this,
    and a `key=value` token containing a literal space would silently
    misparse under the whitespace-split B13 convention every other CLI
    command in this codebase relies on."""
    line = cli_interpret_mod._thesis_line(
        [{"thesis_id": "x", "verdict": "판정 불가", "changed": False}]
    )
    assert "판정불가=1" in line
    assert "판정 불가" not in line


def test_load_thesis_module_returns_none_when_absent(monkeypatch):
    """`_load_thesis_module()` degrades to `None` when the module is genuinely
    absent, rather than raising.

    ST1's `thesis.py` is merged now, so the absence is simulated by making
    `import_module` raise the exact ImportError shape a missing module
    produces (`exc.name == "market_intel.interp.thesis"`). That name is the
    whole point of the guard — see `_load_thesis_module`'s docstring."""
    def raise_absent(name):
        raise ImportError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(cli_interpret_mod.importlib, "import_module", raise_absent)
    assert cli_interpret_mod._load_thesis_module() is None


def test_load_thesis_module_finds_st1_module_when_present():
    """The state after the ST1 merge: the real module resolves."""
    mod = cli_interpret_mod._load_thesis_module()
    assert mod is not None
    assert mod.__name__ == "market_intel.interp.thesis"


def test_load_thesis_module_reraises_broken_import_inside_an_existing_module(monkeypatch):
    """spec's `cli.py._extension_modules` guard, mirrored here: only an
    ImportError whose OWN name is exactly `market_intel.interp.thesis` (the
    module genuinely absent) is swallowed. If a future merged `thesis.py`
    itself imports something broken, `importlib.import_module` raises an
    ImportError named after *that* dependency, not `...interp.thesis` — and
    that must propagate instead of silently reading as "thesis unavailable"."""

    def broken(name):
        assert name == "market_intel.interp.thesis"
        raise ImportError("thesis.py imports a missing dependency", name="some_other_dependency")

    monkeypatch.setattr(cli_interpret_mod.importlib, "import_module", broken)
    with pytest.raises(ImportError):
        cli_interpret_mod._load_thesis_module()


def test_type_date_path_resolution(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli_interpret_mod, "PROJECT_ROOT", tmp_path)
    report = make_report(report_type="morning", report_date="2026-08-01")
    report_path = tmp_path / "reports" / "morning" / "2026-08-01.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(report.to_json(), encoding="utf-8")

    code, out = run_cli(
        monkeypatch, tmp_path, capsys,
        ["interpret", "--type", "morning", "--date", "2026-08-01", "--no-llm"],
    )
    assert code == 0
    assert f"json={report_path}" in out
