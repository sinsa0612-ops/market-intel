"""ST3 acceptance tests — `publish.sh` path guard (spec B11) and the
pre-commit secret gate (spec B12).

Every one of these runs against a throw-away git repo built in `tmp_path`.
Nothing here ever touches the real repository's index, and nothing here
ever pushes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "scripts"

FAKE_SECRET = "market-intel-collector ceo-fake@example.invalid"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False,
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    for name in ("publish.sh", "preflight_secret_gate.py"):
        shutil.copy2(SCRIPTS / name, r / "scripts" / name)
    (r / "reports" / "morning").mkdir(parents=True)
    (r / "docs").mkdir()
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (r / ".gitignore").write_text(".env\n", encoding="utf-8")

    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@example.invalid")
    git(r, "config", "user.name", "test")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def run_publish(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        ["bash", "scripts/publish.sh", *args],
        cwd=repo, capture_output=True, text=True, env=full_env, check=False,
    )


def staged(repo: Path) -> list[str]:
    return [l for l in git(repo, "diff", "--cached", "--name-only").stdout.splitlines() if l]


# --- spec B11: path guard -------------------------------------------------

def test_publish_path_guard(repo):
    """spec ST3 `test_publish_path_guard` — a source file already sitting in
    the index must abort the publish and leave the index empty."""
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "docs" / "index.html").write_text("<p>ok</p>\n", encoding="utf-8")
    git(repo, "add", "src/app.py")
    assert "src/app.py" in staged(repo)

    proc = run_publish(repo, "--dry-run")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert staged(repo) == [], "index was not reset after the guard fired"
    assert "src/app.py" in (proc.stdout + proc.stderr)


def test_publish_stages_only_reports_and_docs(repo):
    (repo / "docs" / "index.html").write_text("<p>ok</p>\n", encoding="utf-8")
    (repo / "reports" / "morning" / "2026-08-01.json").write_text("{}\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")

    proc = run_publish(repo, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "docs/index.html" in proc.stdout
    assert "reports/morning/2026-08-01.json" in proc.stdout
    assert "src/app.py" not in proc.stdout
    assert staged(repo) == [], "--dry-run must leave the index as it found it"


def test_publish_empty_stage_is_silent_success(repo):
    proc = run_publish(repo, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "nothing" in proc.stdout.lower()


def test_publish_refuses_non_main_branch(repo):
    """spec B11-6 — 브랜치가 main이 아니면 아무것도 하지 않는다."""
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "docs" / "index.html").write_text("<p>ok</p>\n", encoding="utf-8")
    proc = run_publish(repo, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert staged(repo) == []
    assert "main" in proc.stdout


def test_publish_never_pushes_in_dry_run(repo):
    """No remote is configured; a dry run that tried to push would fail
    loudly. It must not even reach that point."""
    (repo / "docs" / "index.html").write_text("<p>ok</p>\n", encoding="utf-8")
    proc = run_publish(repo, "--dry-run")
    assert proc.returncode == 0
    log = git(repo, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 1, "dry run created a commit"


def test_publish_script_uses_no_flock_or_timeout_binaries():
    """spec §Environment gotchas — macOS ships neither `flock(1)` nor
    `timeout(1)`; a script calling them dies with `command not found`."""
    for name in ("publish.sh", "run_job.sh"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        for banned in ("flock ", "timeout "):
            assert banned not in text, f"{name} calls the missing binary {banned.strip()}"


# --- spec B12: secret gate ------------------------------------------------

def run_gate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "scripts/preflight_secret_gate.py", *args],
        cwd=repo, capture_output=True, text=True, check=False,
    )


def test_secret_gate_blocks(repo):
    """spec ST3 `test_secret_gate_blocks` — a real `.env` value present in a
    public artefact aborts, and the value itself is never echoed."""
    (repo / ".env").write_text(f"MI_SEC_USER_AGENT={FAKE_SECRET}\n", encoding="utf-8")
    (repo / "docs" / "x.html").write_text(
        f"<p>collected by {FAKE_SECRET}</p>\n", encoding="utf-8")

    proc = run_gate(repo)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "MI_SEC_USER_AGENT" in out
    assert "docs/x.html" in out
    assert FAKE_SECRET not in out, "the gate leaked the secret it was guarding"
    assert "example.invalid" not in out


def test_secret_gate_passes_clean_tree(repo):
    (repo / ".env").write_text(f"MI_SEC_USER_AGENT={FAKE_SECRET}\n", encoding="utf-8")
    (repo / "docs" / "x.html").write_text("<p>clean</p>\n", encoding="utf-8")
    proc = run_gate(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_secret_gate_ignores_empty_env_values(repo):
    """An empty value must not turn into a substring that matches everything."""
    (repo / ".env").write_text("MI_FRED_API_KEY=\nMI_SEC_USER_AGENT=\n", encoding="utf-8")
    (repo / "docs" / "x.html").write_text("<p>anything at all</p>\n", encoding="utf-8")
    proc = run_gate(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_publish_aborts_when_gate_fires(repo):
    """spec B12 — the gate is wired *into* publish, not merely available."""
    (repo / ".env").write_text(f"MI_SEC_USER_AGENT={FAKE_SECRET}\n", encoding="utf-8")
    (repo / "docs" / "x.html").write_text(f"<p>{FAKE_SECRET}</p>\n", encoding="utf-8")

    proc = run_publish(repo, "--dry-run")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert staged(repo) == [], "index left staged after the secret gate fired"
    assert FAKE_SECRET not in out
