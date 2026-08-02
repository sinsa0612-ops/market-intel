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


# --- spec B11 push guard: 미결재 소스가 앞서면 푸시 거부 (final-review.md F3) --
#
# 이 가드는 "미결재 소스 66개 파일이 첫 예약 발행에 실려 나갈 뻔한" 사고를 막으려
# 넣은 것인데(publish.sh 주석), 검수에서 테스트가 0건임이 드러났다: 변이
# `if [ -n "$ahead" ]` -> `if false`에 tests/publish/ 125건이 전원 통과했다.
# 아래 두 건은 베어 원격을 실제로 만들어 **양방향**을 본다 — 막아야 할 때 막고,
# 막지 말아야 할 때 발행한다. 어느 한쪽만 보면 `exit 5`를 상수로 만들어도,
# 가드를 지워도 초록이 된다.

@pytest.fixture
def repo_with_remote(repo, tmp_path):
    """`repo`에 진짜 베어 원격을 붙이고 첫 푸시까지 끝낸 상태."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True,
                   capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(bare))
    pushed = git(repo, "push", "-q", "-u", "origin", "main")
    assert pushed.returncode == 0, pushed.stderr
    return repo, bare


def remote_head(bare: Path) -> str:
    return subprocess.run(["git", "rev-parse", "main"], cwd=bare,
                          capture_output=True, text=True, check=True).stdout.strip()


def remote_files(bare: Path) -> list[str]:
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=bare,
                         capture_output=True, text=True, check=True).stdout
    return [l for l in out.splitlines() if l]


def test_publish_refuses_to_push_while_unreviewed_source_is_ahead(repo_with_remote):
    """(a) 미결재 소스 커밋이 원격보다 앞서 있으면 exit 5 · 원격 불변.
    리포트 커밋 자체는 로컬에 남아 사람이 결재 후 밀 수 있어야 한다."""
    repo, bare = repo_with_remote
    before = remote_head(bare)

    (repo / "src" / "app.py").write_text("print('unreviewed')\n", encoding="utf-8")
    git(repo, "add", "src/app.py")
    git(repo, "commit", "-q", "-m", "source change (unreviewed)")
    (repo / "reports" / "morning" / "2026-08-01.json").write_text("{}\n", encoding="utf-8")

    proc = run_publish(repo)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 5, out
    assert "refusing to push" in out, out
    assert "src/app.py" in out, out
    assert remote_head(bare) == before, "가드가 떴는데 원격이 움직였다"
    assert "reports/morning/2026-08-01.json" not in remote_files(bare)
    remote_app = subprocess.run(["git", "show", "main:src/app.py"], cwd=bare,
                                capture_output=True, text=True, check=True).stdout
    assert "unreviewed" not in remote_app, "미결재 소스 내용이 원격에 도달했다"

    committed = git(repo, "show", "--name-only", "--format=", "HEAD").stdout
    assert "reports/morning/2026-08-01.json" in committed, (
        "리포트는 로컬 커밋으로 남아 있어야 한다 (사람이 결재 후 푸시)")


def test_publish_pushes_the_report_when_nothing_unreviewed_is_ahead(repo_with_remote):
    """(b) 정상 상태에서는 리포트만 커밋·푸시된다 — 가드가 상시 거부로
    굳어버리면 사이트가 조용히 갱신을 멈춘다."""
    repo, bare = repo_with_remote
    before = remote_head(bare)

    (repo / "reports" / "morning" / "2026-08-01.json").write_text("{}\n", encoding="utf-8")
    proc = run_publish(repo)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "pushed" in proc.stdout, out
    assert remote_head(bare) != before, "정상인데 아무것도 푸시되지 않았다"
    assert "reports/morning/2026-08-01.json" in remote_files(bare)
    assert "src/app.py" in remote_files(bare)  # 첫 푸시분 그대로


def test_publish_guard_names_exit_5_in_its_own_contract():
    """`jobs.PUBLISH_HARD_FAILURES`가 5를 실패로 취급하려면(F6) 스크립트가
    실제로 5로 끝나야 한다 — 두 파일이 같은 숫자를 말하는지 못박는다."""
    from market_intel import jobs as jobs_mod

    text = (SCRIPTS / "publish.sh").read_text(encoding="utf-8")
    assert "exit 5" in text
    assert 5 in jobs_mod.PUBLISH_HARD_FAILURES
