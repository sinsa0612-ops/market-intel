"""Settings loaded from .env (python-dotenv). Never print/repr real secret
values — Prime Rule 8.

``find_dotenv()`` walks up from *this file's* location, so it locates the
project-root ``.env`` regardless of the caller's current working directory
(e.g. when this package is nested under an ``_org/.../exec-b/`` sandbox for
a paired execution, it still finds the real project's ``.env`` two levels
up — read-only, never written by this module).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(filename=".env", usecwd=False))

# <project root>/src/market_intel/config.py -> parents[2] is the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path(env_name: str, default_rel: str) -> str:
    """Storage locations default to the *project root*, never the process cwd.

    cron/launchd start the command from an arbitrary directory; a cwd-relative
    default would silently create a second, empty DB there and report
    "collected successfully, facts_total=0" forever. An explicit MI_* override
    is honoured verbatim (that is a deliberate operator choice).
    """
    override = os.environ.get(env_name, "")
    if override:
        return override
    return str(PROJECT_ROOT / default_rel)


def _mask(value: str) -> str:
    return "***" if value else ""


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _path("MI_DB_PATH", "var/market_intel.db"))
    raw_dir: str = field(default_factory=lambda: _path("MI_RAW_DIR", "var/raw"))
    log_dir: str = field(default_factory=lambda: _path("MI_LOG_DIR", "var/logs"))
    fred_api_key: str = field(default_factory=lambda: os.environ.get("MI_FRED_API_KEY", ""))
    ecos_api_key: str = field(default_factory=lambda: os.environ.get("MI_ECOS_API_KEY", ""))
    dart_api_key: str = field(default_factory=lambda: os.environ.get("MI_DART_API_KEY", ""))
    sec_user_agent: str = field(default_factory=lambda: os.environ.get("MI_SEC_USER_AGENT", ""))

    def __repr__(self) -> str:  # never leak secret values via repr/logging
        return (
            f"Settings(db_path={self.db_path!r}, raw_dir={self.raw_dir!r}, "
            f"log_dir={self.log_dir!r}, fred_api_key={_mask(self.fred_api_key)!r}, "
            f"ecos_api_key={_mask(self.ecos_api_key)!r}, dart_api_key={_mask(self.dart_api_key)!r}, "
            f"sec_user_agent={_mask(self.sec_user_agent)!r})"
        )

    def secret_values(self) -> list[str]:
        """All non-empty secret-bearing values, for masking/redaction."""
        return [
            v
            for v in (self.fred_api_key, self.ecos_api_key, self.dart_api_key, self.sec_user_agent)
            if v
        ]


def load_settings() -> Settings:
    return Settings()
