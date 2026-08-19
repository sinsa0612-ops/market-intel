"""ST1 — the pre-registered transition rules (`theses/transition_rules_v1.md`)
are frozen by CEO sign-off (spec ST0). This test is the lock: any edit to the
document, even a single character, must turn this red. Changing the rules is
not a v1 edit — it is registering `transition_rules_v2.md` (spec §1 R12).
"""
from __future__ import annotations

import hashlib

from market_intel.config import PROJECT_ROOT
from market_intel.interp.transitions import RULES_PATH, RULES_SHA256


def test_transition_rules_v1_hash_is_frozen():
    path = PROJECT_ROOT / RULES_PATH
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == RULES_SHA256, (
        "규칙 문서가 동결됐다. 바꾸려면 v2를 등록하라 "
        f"(expected sha256={RULES_SHA256}, actual sha256={actual}, path={path})"
    )
