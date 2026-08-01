"""Secret-leak audit: prove no API key ever reaches the repo, the DB, the raw
snapshots or the logs.

Run it with `uv run python secret_leak_check.py` after a collect.

Design rules this script itself obeys (Prime Rule 8):
  * it reads .env, but never prints a value — only match COUNTS, so its own
    output cannot leak a secret even when it finds one;
  * secret values are piped to grep on STDIN, never passed as argv (argv is
    visible to `ps` and lands in shell history);
  * paths are derived from this file's location, never hardcoded.
"""
from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
DB_PATH = Path(__import__("os").environ.get("MI_DB_PATH", PROJECT_ROOT / "var" / "market_intel.db"))


def load_non_empty_values() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            pairs.append((key.strip(), value))
    return pairs


def grep_tree(value: str) -> list[str]:
    """Whole project tree, excluding .venv and .env itself. The pattern goes in
    on stdin (`-f -`) so it never appears in this process's argv."""
    proc = subprocess.run(
        ["grep", "-R", "--exclude-dir=.venv", "--exclude-dir=.git", "--exclude=.env",
         "-l", "-F", "-f", "-", str(PROJECT_ROOT)],
        input=value, capture_output=True, text=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def count_in_db_dump(value: str) -> int:
    if not DB_PATH.exists():
        return 0
    dump = subprocess.run(["sqlite3", str(DB_PATH), ".dump"], capture_output=True, text=True)
    return dump.stdout.count(value)


def count_in_raw_gz(value: str) -> tuple[int, list[str]]:
    """`grep -R` treats large raw snapshots (payload_path, gzip'd -- see
    db.py INLINE_LIMIT) as binary and skips them, and even without that
    skip, compressed bytes never contain a literal-text match of the
    plaintext key. Decompress every .gz under the project and check the
    actual plaintext (repair.md finding #8)."""
    hits = 0
    hit_files: list[str] = []
    for path in PROJECT_ROOT.rglob("*.gz"):
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        try:
            text = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
        except OSError:
            continue
        count = text.count(value)
        if count:
            hits += count
            hit_files.append(str(path))
    return hits, hit_files


def main() -> int:
    if not ENV_PATH.exists():
        print("no .env found — nothing to check")
        return 0

    pairs = load_non_empty_values()
    print(f"non-empty values found in .env: {[k for k, _ in pairs]}")
    print(f"db checked: {DB_PATH}")
    print()

    any_hit = False
    for key, value in pairs:
        hit_files = grep_tree(value)
        print(f"{key}: grep -R (tree, excl .env/.venv) hits = {len(hit_files)}")
        if hit_files:
            any_hit = True
            print(f"  files: {hit_files}")

        db_hits = count_in_db_dump(value)
        print(f"{key}: sqlite3 .dump hits = {db_hits}")
        if db_hits:
            any_hit = True

        gz_hits, gz_files = count_in_raw_gz(value)
        print(f"{key}: gzip raw-snapshot (decompressed) hits = {gz_hits}")
        if gz_hits:
            any_hit = True
            print(f"  files: {gz_files}")

    print()
    print("RESULT:", "LEAK DETECTED" if any_hit else "0 hits for all non-empty values in .env")
    return 1 if any_hit else 0


if __name__ == "__main__":
    sys.exit(main())
