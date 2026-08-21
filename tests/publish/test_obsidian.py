"""ST3 acceptance tests — Obsidian sync (spec §ST3 What #2, B9)."""
from __future__ import annotations

from pathlib import Path

from market_intel import obsidian as obsidian_mod
from market_intel.reporting.model import FactRow

from tests.publish.conftest import make_report, write_report


def test_path_convention(reports_root, tmp_path):
    """spec B9 — `<vault>/<YYYY>/<stem>-<type>.md`."""
    write_report(reports_root, make_report("morning", "2026-08-01"))
    vault = tmp_path / "vault"
    result = obsidian_mod.sync(reports_root=reports_root, vault_root=vault)
    assert result["written"] == 1
    assert (vault / "2026" / "2026-08-01-morning.md").exists()


def test_frontmatter_b9_key_order(reports_root, tmp_path):
    """spec B9 pins the frontmatter keys *and their order*."""
    rep = make_report("morning", "2026-08-01", data_status="partial")
    write_report(reports_root, rep)
    vault = tmp_path / "vault"
    obsidian_mod.sync(reports_root=reports_root, vault_root=vault)

    text = (vault / "2026" / "2026-08-01-morning.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    block = text.split("---\n")[1]
    keys = [line.split(":", 1)[0] for line in block.splitlines() if line and not line.startswith(" ")]
    assert keys == [
        "project", "type", "date", "cutoff_kst", "data_status",
        "ai_interpretation", "tags", "subjects",
    ], keys
    assert "project: market-intel" in block
    assert "type: morning" in block
    assert "date: 2026-08-01" in block
    assert 'cutoff_kst: "2026-08-01T07:15:00+09:00"' in block
    assert "data_status: 부분 확인" in block
    assert "ai_interpretation: false" in block
    assert "market-intel" in block and "report/morning" in block and "status/부분확인" in block


def test_frontmatter_has_market_tags_and_subject_links(reports_root, tmp_path):
    """spec B9 — the knowledge-graph payload: market/* tags and `[[…]]`
    subjects. Without these the vault is a pile of flat notes."""
    rep = make_report("morning", "2026-08-01", facts=[
        FactRow(label="NVDA 종가", value="120", comparison="", source_url="https://example.test/n",
                data_status="source_verified", known_at="2026-07-31T22:00:00+00:00",
                subject="NVDA", metric="price_close"),
        FactRow(label="삼성전자 종가", value="70000", comparison="",
                source_url="https://example.test/s", data_status="source_verified",
                known_at="2026-07-31T22:00:00+00:00", subject="005930.KS", metric="price_close"),
    ])
    write_report(reports_root, rep)
    vault = tmp_path / "vault"
    obsidian_mod.sync(reports_root=reports_root, vault_root=vault)
    text = (vault / "2026" / "2026-08-01-morning.md").read_text(encoding="utf-8")

    assert "market/US" in text
    assert "market/KR" in text
    # The graph node is the canonical universe name; the alias keeps the
    # note's visible text identical to the report (see obsidian._wikilink —
    # deliberate deviation from B9's illustrative `[[NVDA]]`).
    assert '"[[NVIDIA]]"' in text
    assert '"[[Samsung Electronics]]"' in text
    assert "[[NVIDIA|NVDA]]" in text


def test_wikilink_only_on_first_occurrence(reports_root, tmp_path):
    """spec B9 — 본문 안 종목 *첫 등장*에만 위키링크. Linking every mention
    turns the note into `[[NVDA]] [[NVDA]] [[NVDA]]`."""
    rep = make_report("morning", "2026-08-01", facts=[
        FactRow(label="NVDA 종가", value="120", comparison="NVDA 전일 대비 +1%",
                source_url="https://example.test/n", data_status="source_verified",
                known_at="2026-07-31T22:00:00+00:00", subject="NVDA", metric="price_close"),
    ])
    write_report(reports_root, rep)
    vault = tmp_path / "vault"
    obsidian_mod.sync(reports_root=reports_root, vault_root=vault)
    body = (vault / "2026" / "2026-08-01-morning.md").read_text(encoding="utf-8").split("---\n", 2)[2]
    assert body.count("[[NVIDIA|NVDA]]") == 1, body
    assert body.count("[[") == 1, "a second mention was linked too"
    assert body.count("NVDA") == 2, "the report's own wording must be preserved"


def test_no_double_frontmatter(reports_root, tmp_path):
    write_report(reports_root, make_report("morning", "2026-08-01"))
    vault = tmp_path / "vault"
    obsidian_mod.sync(reports_root=reports_root, vault_root=vault)
    text = (vault / "2026" / "2026-08-01-morning.md").read_text(encoding="utf-8")
    assert text.count("\nproject: market-intel") == 1
    assert text.count("ai_interpretation:") == 1


def test_overwrite_same_stem(reports_root, tmp_path):
    """spec B9 — 동기화는 덮어쓰기."""
    write_report(reports_root, make_report("morning", "2026-08-01"))
    vault = tmp_path / "vault"
    obsidian_mod.sync(reports_root=reports_root, vault_root=vault)
    target = vault / "2026" / "2026-08-01-morning.md"
    target.write_text("STALE", encoding="utf-8")
    obsidian_mod.sync(reports_root=reports_root, vault_root=vault)
    assert "STALE" not in target.read_text(encoding="utf-8")


def test_vault_root_notes_untouched(reports_root, tmp_path):
    """spec B9 — 볼트 루트의 기존 데일리 노트는 절대 건드리지 않는다."""
    vault = tmp_path / "Pensieve"
    (vault).mkdir()
    daily = vault / "2026-07-15.md"
    daily.write_text("CEO의 데일리 노트", encoding="utf-8")
    write_report(reports_root, make_report("morning", "2026-08-01"))
    obsidian_mod.sync(reports_root=reports_root, vault_root=vault / "market-intel")
    assert daily.read_text(encoding="utf-8") == "CEO의 데일리 노트"
    assert sorted(p.name for p in vault.iterdir()) == ["2026-07-15.md", "market-intel"]


def test_missing_vault_is_created(reports_root, tmp_path):
    write_report(reports_root, make_report("morning", "2026-08-01"))
    vault = tmp_path / "does" / "not" / "exist"
    result = obsidian_mod.sync(reports_root=reports_root, vault_root=vault)
    assert result["written"] == 1
    assert vault.exists()


def test_since_filter(reports_root, tmp_path):
    from datetime import date

    write_report(reports_root, make_report("morning", "2026-07-01"))
    write_report(reports_root, make_report("morning", "2026-08-01"))
    vault = tmp_path / "vault"
    result = obsidian_mod.sync(reports_root=reports_root, vault_root=vault,
                               since=date(2026, 7, 15))
    assert result["written"] == 1
    assert (vault / "2026" / "2026-08-01-morning.md").exists()
    assert not (vault / "2026" / "2026-07-01-morning.md").exists()


def test_vault_dir_env_override(monkeypatch, tmp_path):
    """spec B12 — `MI_OBSIDIAN_DIR` is the one new env var; default is
    `~/Pensieve/market-intel`."""
    monkeypatch.delenv("MI_OBSIDIAN_DIR", raising=False)
    assert obsidian_mod.vault_dir() == Path.home() / "Pensieve" / "market-intel"
    monkeypatch.setenv("MI_OBSIDIAN_DIR", str(tmp_path / "elsewhere"))
    assert obsidian_mod.vault_dir() == tmp_path / "elsewhere"
