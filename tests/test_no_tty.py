"""ST1 acceptance test 5: no interactive prompts anywhere in src/ — the
collector must run unattended under cron."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
FORBIDDEN = [
    re.compile(r"\binput\s*\("),
    re.compile(r"\bgetpass\b"),
    re.compile(r"click\.confirm"),
]


def test_no_tty_prompts_in_source():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            if pattern.search(text):
                offenders.append(f"{path}: {pattern.pattern}")
    assert not offenders, f"TTY-prompting code found: {offenders}"
