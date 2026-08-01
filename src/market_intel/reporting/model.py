"""Report data model (spec B5) — the 2B extension point.

`Interpretation` is always constructed empty by this stage (2A). Nothing in
this module, `build.py`, or either renderer ever fills `reading` /
`counter_reading` / `thesis_impact` / `next_check` with generated text — 2B
does that later, out of process, by handing the renderers the very same
`Report` object with only `interpretation` different. The 3 contracts this
protects (spec B5):

1. Renderers take a `Report` and nothing else.
2. `interpretation.is_empty()` → renderers print the literal string
   "AI 해석 미생성" in each of the 4 body sections; filled → an
   "AI 자동판정 · {generated_by}" badge instead.
3. Facts reach this model only through `db.facts_as_of` (build.py's job,
   not this file's — this file has no DB import at all).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "2a.1"


@dataclass
class FactRow:
    label: str
    value: str
    comparison: str
    source_url: str
    data_status: str
    known_at: str
    subject: str
    metric: str
    raw_value: Any = None


@dataclass
class CalendarRow:
    when: str
    name: str
    country: str
    subject: str
    importance: str  # 'A'|'B'|'C' (spec 5.2)
    change: str  # ''|'신규'|'앞당김'|'연기'|'next_cycle'|'consensus_revised'
    source_url: str
    data_status: str


@dataclass
class MissingItem:
    area: str
    reason: str
    since: str
    gap_id: str


@dataclass
class Interpretation:
    reading: str = ""
    counter_reading: str = ""
    thesis_impact: str = ""
    next_check: str = ""
    generated_by: str = ""
    generated_at: str = ""

    def is_empty(self) -> bool:
        return not (self.reading or self.counter_reading or self.thesis_impact or self.next_check)


@dataclass
class Report:
    schema_version: str = SCHEMA_VERSION
    report_type: str = ""
    report_date: str = ""
    cutoff_kst: str = ""
    cutoff_utc: str = ""
    generated_at: str = ""
    title: str = ""
    headline: str = ""
    data_status: str = ""
    facts: list[FactRow] = field(default_factory=list)
    market_reaction: list[FactRow] = field(default_factory=list)
    events: list[CalendarRow] = field(default_factory=list)
    schedule_changes: list[CalendarRow] = field(default_factory=list)
    missing: list[MissingItem] = field(default_factory=list)
    interpretation: Interpretation = field(default_factory=Interpretation)
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Report":
        d = json.loads(raw)
        d["facts"] = [FactRow(**f) for f in d["facts"]]
        d["market_reaction"] = [FactRow(**f) for f in d["market_reaction"]]
        d["events"] = [CalendarRow(**e) for e in d["events"]]
        d["schedule_changes"] = [CalendarRow(**e) for e in d["schedule_changes"]]
        d["missing"] = [MissingItem(**m) for m in d["missing"]]
        d["interpretation"] = Interpretation(**d["interpretation"])
        return cls(**d)


# spec B7 — fixed data_status display mapping. Reused (not re-defined) from
# schedule.py (ST1), which already established this exact table for the
# CLI's `status=` output; duplicating the 4-entry dict here would just be a
# second copy to drift out of sync.
from ..schedule import DATA_STATUS_KO  # noqa: E402

# spec B6 — data_status rank for "리포트 전체 등급 = 구성 fact 중 최악".
DATA_STATUS_RANK = {"source_verified": 0, "reconstructed": 1, "partial": 2, "unverified": 3}
DATA_STATUS_RANK_REVERSE = {v: k for k, v in DATA_STATUS_RANK.items()}


def worst_data_status(rows: list[FactRow]) -> str:
    """spec B6: report-wide grade = worst grade among its constituent facts.
    An empty report (no facts collected at all) is graded 'unverified' —
    nothing here has been confirmed, so it must not claim the best grade by
    default (spec §3.3: 미확인 = "추정으로 채우지 않고 확인 과제로 보류")."""
    if not rows:
        return "unverified"
    worst = max(DATA_STATUS_RANK.get(r.data_status, DATA_STATUS_RANK["unverified"]) for r in rows)
    return DATA_STATUS_RANK_REVERSE[worst]
