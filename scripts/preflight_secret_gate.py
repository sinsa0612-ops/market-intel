#!/usr/bin/env python3
"""Hard secret gate for the *public* artefacts (spec B12).

This repository is public and `MI_SEC_USER_AGENT` carries the CEO's real
email address. Before anything under `reports/` or `docs/` can be
committed, every non-empty secret value must be absent from all of it.
One hit aborts the publish.

Rules this script obeys about its own behaviour (Prime Rule 8):

  * it never prints a secret value — only the env var **name** and the
    offending file path, so its own output cannot leak what it caught;
  * secrets are never passed as argv (visible in `ps`) — the scan is done
    in-process;
  * paths derive from this file's location, never a hardcoded home dir.

Stdlib only, and it imports `market_intel` only opportunistically, so it
runs identically under `uv run python`, a bare `python3` from launchd, or
from inside a throw-away test repo that has no virtualenv at all.

    python3 scripts/preflight_secret_gate.py [--root DIR] [SUBDIR …]

Exit codes: 0 = clean, 1 = a secret was found, 2 = bad usage.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_SUBDIRS = ("reports", "docs")
# Values shorter than this are not secrets, they are coincidences — a
# 3-char key would match half the site and turn the gate into noise.
MIN_SECRET_LENGTH = 8


def parse_env_file(path: Path) -> dict[str, str]:
    """`KEY=value` pairs with a non-empty value. Deliberately not
    python-dotenv: this must work with no third-party packages installed."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            out[key.strip()] = value
    return out


def settings_secrets() -> dict[str, str]:
    """`Settings.secret_values()` (spec B12), when the package is importable.
    Catches values supplied through the real environment rather than a
    `.env` file."""
    try:
        from market_intel.config import load_settings
    except Exception:
        return {}
    try:
        s = load_settings()
    except Exception:
        return {}
    named = {
        "MI_FRED_API_KEY": s.fred_api_key,
        "MI_ECOS_API_KEY": s.ecos_api_key,
        "MI_DART_API_KEY": s.dart_api_key,
        "MI_SEC_USER_AGENT": s.sec_user_agent,
    }
    return {k: v for k, v in named.items() if v and v in s.secret_values()}


def collect_secrets(root: Path) -> dict[str, str]:
    secrets = dict(parse_env_file(root / ".env"))
    for key, value in settings_secrets().items():
        secrets.setdefault(key, value)
    return {k: v for k, v in secrets.items() if len(v) >= MIN_SECRET_LENGTH}


def scan(root: Path, subdirs: tuple[str, ...], secrets: dict[str, str]) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for sub in subdirs:
        base = root / sub
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for key, value in secrets.items():
                if value in text:
                    hits.append((key, str(path.relative_to(root))))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="secret gate for public artefacts")
    parser.add_argument("--root", default=None)
    parser.add_argument("subdirs", nargs="*", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    subdirs = tuple(args.subdirs) if args.subdirs else DEFAULT_SUBDIRS

    secrets = collect_secrets(root)
    scanned = [s for s in subdirs if (root / s).exists()]
    print(f"secret gate: root={root} scanning={list(scanned)} "
          f"secrets_checked={sorted(secrets)}")

    hits = scan(root, subdirs, secrets)
    if hits:
        print("SECRET LEAK — publish aborted:", file=sys.stderr)
        for key, rel in hits:
            # name + path only; the value itself is never echoed.
            print(f"  {key} found in {rel}", file=sys.stderr)
        return 1
    print("secret gate: OK (0 hits)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
