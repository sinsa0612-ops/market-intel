"""Regression: default DB/raw/log paths must be anchored at the project root,
not at the caller's current working directory.

A cwd-relative default is the classic cron failure: launchd/cron runs the
command from `/` or the user's home, the collector silently creates a fresh
empty DB there, and every run afterwards reports "success, facts_total=0".
"""
from __future__ import annotations

import os
from pathlib import Path

from market_intel.config import PROJECT_ROOT, Settings, load_settings

MODULE_ROOT = Path(__file__).resolve().parents[1]


def _clear_path_env(monkeypatch):
    for key in ("MI_DB_PATH", "MI_RAW_DIR", "MI_LOG_DIR"):
        monkeypatch.delenv(key, raising=False)


def test_project_root_is_the_repository_root():
    assert PROJECT_ROOT == MODULE_ROOT


def test_default_paths_are_absolute_and_under_project_root(monkeypatch, tmp_path):
    _clear_path_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # simulate cron running from an unrelated cwd

    settings = load_settings()

    for path in (settings.db_path, settings.raw_dir, settings.log_dir):
        assert os.path.isabs(path), f"{path!r} must be absolute"
        assert Path(path).is_relative_to(PROJECT_ROOT), f"{path!r} must live under the project root"

    assert settings.db_path == str(PROJECT_ROOT / "var" / "market_intel.db")
    # and nothing was created in the foreign cwd just by loading settings
    assert list(tmp_path.iterdir()) == []


def test_explicit_env_override_still_wins(monkeypatch, tmp_path):
    _clear_path_env(monkeypatch)
    monkeypatch.setenv("MI_DB_PATH", str(tmp_path / "custom.db"))

    assert load_settings().db_path == str(tmp_path / "custom.db")


def test_explicit_constructor_paths_still_win(tmp_path):
    settings = Settings(db_path=str(tmp_path / "x.db"))
    assert settings.db_path == str(tmp_path / "x.db")
