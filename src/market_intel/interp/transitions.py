"""State-transition derived view (spec §5).

ST1 pinned the pre-registered rules document and locked its hash (see
`RULES_SHA256` below, checked by `tests/interp/test_transition_rules_frozen.py`).
This module (ST2) is the engine that reads `thesis_reviews` — and *only*
`thesis_reviews`, per rules-doc R0 — and turns the daily verdict rows into
the run/transition view: `최초진입일 · 지속 · 새 관측 건수`, replacing the old
"강화 N회" counter that fired on every `TRUE → TRUE` day. See
`theses/transition_rules_v1.md` for the frozen rules text (R0–R12); each
function/branch below cites the rule it implements.

This module never writes to `thesis_reviews` and never re-reads
`fact_revisions` — recomputing history from today's facts would be a
retroactive rewrite of past verdicts, which is exactly what the rules doc
(R0, R11) forbids. It also never runs SQL itself: all 2B table access is
`store.py`'s job alone (spec SA-1), so this module reads through
`store.reviews_for_thesis` and nothing else.

R5's third run-breaking condition is "그 행의 `engine_changed = 1`". Until ST3,
`thesis_reviews` had no such column, so this module derived it from
`engine_version` changing between representative days. ST3 added the real
column (`thesis_reviews.engine_changed`, computed by `thesis.review()` the
same way `rules_changed` is — spec §2 "지문/엔진버전 함정의 해법") and wired
`store.record_reviews` to persist it, so this module now reads that column
directly through `store.reviews_for_thesis` instead of re-deriving it. The
old derivation was a stopgap documented as exactly that; keeping it after the
real column existed would have let two independently-computed "did the
engine change" answers drift apart.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from . import store as store_mod

RULES_VERSION = "tv1"
RULES_PATH = "theses/transition_rules_v1.md"
RULES_SHA256 = "6d1bec67305056f5bd4a12284f0491a5304200983ff05a71e5a2c1390fcb9047"

# R3: an atom's status is TRUE/FALSE/UNKNOWN, or "없음" when the day's
# atoms_json doesn't carry that atom id at all.
STATUS_NONE = "없음"

# R5's two forced-restart reasons (there are only ever these two labels).
RESTART_RULES_CHANGED = "기준 변경"
RESTART_ENGINE_CHANGED = "의미 변경"


@dataclass(frozen=True)
class AtomDay:
    """One representative day's reading for one atom (R2 + R3 + R7)."""

    report_date: str
    status: str  # TRUE | FALSE | UNKNOWN | STATUS_NONE
    latest_at: str | None  # detail.latest_at; None if absent (atom missing, or field missing — R7)
    streak_since: str | None  # detail.streak.since — R11 reference field only, never an entry_date
    is_new_observation: bool | None  # True/False, or None = 미상 (R7) or N/A (ledger's first row, R7 bullet 1)
    has_predecessor: bool  # False only for the ledger's very first representative day
    rules_changed: bool  # this day's row.rules_changed (R5.2)
    engine_changed: bool  # this day's row.engine_changed (R5.3, real column since ST3)


@dataclass(frozen=True)
class Run:
    """A maximal same-status run (R5), with its display fields (R6/R7/R9)."""

    thesis_id: str
    atom_id: str
    status: str
    entry_date: str | None  # None when left_censored (R11)
    left_censored: bool
    last_report_date: str
    duration_days: int  # count of representative (판정)일, not calendar days (R6)
    new_observation_count: int  # R7/R9
    last_new_observation_date: str | None  # R7
    unknown_observation_days: int  # days with a predecessor whose new-observation status was 미상 (R7 bullet 2)
    has_data_gap: bool  # R10: any adjacent pair inside the run >=4 calendar days apart
    restart_reason: str | None  # None, or RESTART_RULES_CHANGED / RESTART_ENGINE_CHANGED (R5.2/.3)
    streak_since: str | None  # R11: detail.streak.since as of the run's last day — data-only reference,
    # never used as entry_date (it moves backward on backfills) and omitted from default display.


def _merge_pending(rows: list[dict], pending: dict | None) -> list[dict]:
    """ST4 item 3 / spec Risk R-1: `ops.interpret_report` records this
    cutoff's review *before* asking for the derived view, so by the time it
    calls in, `store.reviews_for_thesis` already has the row and `pending`
    is None. `market-intel interpret` (`cli_interpret._thesis_summary`)
    computes the same review but never persists it — without this merge it
    would show one representative day *less* than the pipeline path for the
    exact same report, which is the "하루 어긋난 숫자" R-1 warns about.

    `pending` must already be in the shape `store.reviews_for_thesis` rows
    are (see `pending_row_from_review` below) — a raw `report_date` string
    key, not a re-parse. If a row for that date is already in the ledger
    (e.g. a rerun after the pipeline already recorded it), the recorded row
    wins — R2 already defines "the representative row for a date" as
    whichever `thesis_reviews` row is there; `pending` only fills a gap, it
    never shadows the ledger."""
    if pending is None:
        return rows
    if any(r["report_date"] == pending["report_date"] for r in rows):
        return rows
    return rows + [pending]


def _representative_rows(conn: sqlite3.Connection, thesis_id: str,
                         pending: dict | None = None,
                         as_of: str | None = None) -> list[dict]:
    """R0 + R1 + R2: read only `thesis_reviews` (via `store.reviews_for_thesis`,
    SA-1's single accessor for the 2B tables), already in R1 order
    (`cutoff_utc` then `rowid`); collapse same-`report_date` rows to the last
    one in that order (R2). Relies on `thesis_reviews` being append-only
    (db.py trigger), so `rowid` insertion order is a stable, permanent
    tie-break.

    R1's `cutoff_utc` order decides *which* row represents a date (R2) — that
    part is untouched. R4 separately defines a transition as being between
    "날짜순으로 인접한 두 대표일" (date-order-adjacent representative days), so
    the days handed to the rest of this module must be laid out in
    `report_date` order, not in the order their representative row happened
    to be recorded. A late-arriving (backfilled) row for an earlier date can
    have a *later* `cutoff_utc` than an already-recorded later date, and
    without this sort the earlier date would be appended after the later one
    — a same-thesis timeline whose entry dates go backwards. This final sort
    is a display-order concern, not a change to R1's tie-break rule.

    `pending` (ST4): an in-memory review row not yet in the ledger,
    merged in via `_merge_pending` above.

    `as_of` (F-A fix — information cutoff, SA-10): the ledger holds every
    representative day ever recorded, including ones recorded *after* the
    report this call is building the view for (catch-up backfills a report
    for a past date while the ledger's head has since moved on). Without this
    cap, a historical `market-intel interpret --file <past report>` cited
    dates and streak counts from *after* its own cutoff — the exact thing
    SA-10 forbids. `report_date` strings compare lexicographically as dates
    (`YYYY-MM-DD`), so a plain string `<=` is enough. `pending`'s date always
    equals the report's own `report_date` (the caller passes both from the
    same report), so it is never dropped by this filter — that is how a
    same-day report keeps seeing its own not-yet-recorded verdict."""
    rows = store_mod.reviews_for_thesis(conn, thesis_id)
    rows = _merge_pending(rows, pending)
    if as_of is not None:
        rows = [r for r in rows if r["report_date"] <= as_of]
    by_date: dict[str, dict] = {}
    for row in rows:
        by_date[row["report_date"]] = row  # last row for a date wins (R2)
    return sorted(by_date.values(), key=lambda r: r["report_date"])


def pending_row_from_review(review_row: dict) -> dict | None:
    """Adapts one of `thesis.review()`'s in-memory output rows into the shape
    `store.reviews_for_thesis` returns, for `pending=` above. `thesis.review()`
    rows carry `atoms` (already a parsed dict — the same payload `atoms_json`
    serializes) rather than a JSON string, so this is a field rename/select,
    not a re-parse.

    Returns None when the row doesn't carry what this needs (`report_date`/
    `atoms`) — e.g. a stand-in review row from a test double for a thesis
    module ST4 doesn't own — so callers can degrade to `pending=None`
    (today's state simply isn't shown yet) instead of crashing on a shape
    they don't control."""
    if "report_date" not in review_row or "atoms" not in review_row:
        return None
    return {
        "report_date": review_row["report_date"],
        "atoms_json": review_row["atoms"],
        "rules_changed": bool(review_row.get("rules_changed", 0)),
        "engine_version": review_row.get("engine_version"),
        "engine_changed": bool(review_row.get("engine_changed", 0)),
        "verdict": review_row.get("verdict", ""),
    }


def _atom_ids(rep_rows: list[dict]) -> list[str]:
    """Every atom id ever seen for this thesis, across all atoms_json
    categories and all representative days — a mid-ledger thesis edit can
    add/drop an atom, and R4 requires "없음 → TRUE" to be a real transition,
    so an atom's timeline must span the whole ledger, not just the days it
    happens to appear on."""
    ids: set[str] = set()
    for row in rep_rows:
        for items in row["atoms_json"].values():
            for atom in items:
                ids.add(atom["id"])
    return sorted(ids)


def _atom_reading(row: dict, atom_id: str) -> tuple[str, str | None, str | None]:
    """(status, latest_at, streak_since) for one atom on one representative
    row. Status is STATUS_NONE (R3 "없음") if the id isn't present in any
    category that day. `streak_since` is `detail.streak.since` (R11's
    reference field, absent on rows before it existed)."""
    for items in row["atoms_json"].values():
        for atom in items:
            if atom["id"] == atom_id:
                detail = atom.get("detail", {})
                return atom["status"], detail.get("latest_at"), detail.get("streak", {}).get("since")
    return STATUS_NONE, None, None


def _calendar_gap_days(d1: str, d2: str) -> int:
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


def atom_timeline(conn: sqlite3.Connection, thesis_id: str, atom_id: str,
                  pending: dict | None = None, as_of: str | None = None) -> list[AtomDay]:
    """Day-by-day reading for one atom across the thesis's whole ledger.
    `Run`s (below) are built by grouping this timeline; exposed separately
    because R7's "미상" is a per-day fact a caller may want without waiting
    for run aggregation. `pending`: see `_merge_pending`. `as_of`: see
    `_representative_rows`."""
    rep_rows = _representative_rows(conn, thesis_id, pending, as_of)
    out: list[AtomDay] = []
    prev_latest_at: str | None = None
    for i, row in enumerate(rep_rows):
        status, latest_at, streak_since = _atom_reading(row, atom_id)
        if i == 0:
            # R7 bullet 1: the ledger's first row has no predecessor to
            # compare against, so "new observation" is undecidable — and
            # for R11 the same index is what "left-censored" keys off.
            is_new = None
        elif latest_at is None or prev_latest_at is None:
            is_new = None  # R7 bullet 2: 미상 — either side is missing detail.latest_at
        else:
            is_new = latest_at != prev_latest_at
        out.append(
            AtomDay(
                report_date=row["report_date"],
                status=status,
                latest_at=latest_at,
                streak_since=streak_since,
                is_new_observation=is_new,
                has_predecessor=i > 0,
                rules_changed=bool(row["rules_changed"]),
                engine_changed=bool(row["engine_changed"]),
            )
        )
        prev_latest_at = latest_at
    return out


def _build_runs(thesis_id: str, atom_id: str, timeline: list[AtomDay]) -> list[Run]:
    if not timeline:
        return []

    # R5: split the timeline into maximal runs. A new run starts at index 0
    # (always) or wherever status changed (R4/R5.1), rules_changed fired
    # (R5.2), or engine_changed fired (R5.3) — even with no status change.
    run_starts = [0]
    for i in range(1, len(timeline)):
        prev, cur = timeline[i - 1], timeline[i]
        if cur.status != prev.status or cur.rules_changed or cur.engine_changed:
            run_starts.append(i)

    runs: list[Run] = []
    for k, start in enumerate(run_starts):
        end = (run_starts[k + 1] - 1) if k + 1 < len(run_starts) else len(timeline) - 1
        days = timeline[start:end + 1]
        first = days[0]

        restart_reason = None
        if start > 0:
            reasons = []
            if first.rules_changed:
                reasons.append(RESTART_RULES_CHANGED)
            if first.engine_changed:
                reasons.append(RESTART_ENGINE_CHANGED)
            restart_reason = " · ".join(reasons) if reasons else None

        has_gap = any(
            _calendar_gap_days(days[i - 1].report_date, days[i].report_date) >= 4
            for i in range(1, len(days))
        )

        new_days = [d for d in days if d.is_new_observation is True]
        # R7 bullet 1's "그 날은 세지 않는다" (ledger's first row) is a plain
        # exclusion, not the "미상" this field reports — only count days that
        # *had* a predecessor to compare against but still couldn't be told.
        unknown_days = [d for d in days if d.is_new_observation is None and d.has_predecessor]

        runs.append(
            Run(
                thesis_id=thesis_id,
                atom_id=atom_id,
                status=first.status,
                entry_date=None if start == 0 else first.report_date,
                left_censored=(start == 0),
                last_report_date=days[-1].report_date,
                duration_days=len(days),
                new_observation_count=len(new_days),
                last_new_observation_date=new_days[-1].report_date if new_days else None,
                unknown_observation_days=len(unknown_days),
                has_data_gap=has_gap,
                restart_reason=restart_reason,
                streak_since=days[-1].streak_since,  # R11: freshest known value, never entry_date
            )
        )
    return runs


def atom_runs(conn: sqlite3.Connection, thesis_id: str, atom_id: str,
              pending: dict | None = None, as_of: str | None = None) -> list[Run]:
    """The full run history for one (thesis_id, atom_id), oldest first.
    `pending`: see `_merge_pending`. `as_of`: see `_representative_rows`."""
    return _build_runs(thesis_id, atom_id, atom_timeline(conn, thesis_id, atom_id, pending, as_of))


def all_atom_runs(conn: sqlite3.Connection, thesis_id: str,
                  pending: dict | None = None, as_of: str | None = None) -> dict[str, list[Run]]:
    """Run history for every atom this thesis has ever carried (R0-scoped:
    reads only `thesis_reviews`). `pending`: see `_merge_pending`. `as_of`:
    see `_representative_rows`."""
    rep_rows = _representative_rows(conn, thesis_id, pending, as_of)
    return {atom_id: atom_runs(conn, thesis_id, atom_id, pending, as_of) for atom_id in _atom_ids(rep_rows)}


def ledger_start(conn: sqlite3.Connection, thesis_id: str,
                 pending: dict | None = None, as_of: str | None = None) -> str | None:
    """R11: the ledger's first representative day for this thesis — what a
    left-censored Run's display needs to build "기록 시작(YYYY-MM-DD)
    이전부터" without a second, separate query of the raw ledger. None only
    when the thesis has no reviews at all. `pending`: see `_merge_pending`
    (only matters here for a thesis with zero recorded rows yet — its first
    review, still unrecorded, is then the ledger's start). `as_of`: see
    `_representative_rows`."""
    rep_rows = _representative_rows(conn, thesis_id, pending, as_of)
    return rep_rows[0]["report_date"] if rep_rows else None


@dataclass(frozen=True)
class VerdictDay:
    """One representative day's verdict reading. The "강화" badge the display
    shows is a per-verdict fact, not a per-atom one — R4/R5's same-state-run
    machinery applies unchanged here, just keyed on the row's own `verdict`
    column instead of one atom's status."""

    report_date: str
    verdict: str  # thesis_reviews.verdict — always one of the 5 values (NOT NULL CHECK)
    rules_changed: bool  # R5.2
    engine_changed: bool  # R5.3, real column (same as AtomDay's) since ST3


def verdict_timeline(conn: sqlite3.Connection, thesis_id: str,
                     pending: dict | None = None, as_of: str | None = None) -> list[VerdictDay]:
    """Day-by-day verdict reading across the thesis's whole ledger — same
    representative-day rule (R0-R2) as `atom_timeline`, reading the row's own
    `verdict` instead of one atom's status. `pending`: see `_merge_pending`.
    `as_of`: see `_representative_rows`."""
    rep_rows = _representative_rows(conn, thesis_id, pending, as_of)
    out: list[VerdictDay] = []
    for row in rep_rows:
        out.append(
            VerdictDay(
                report_date=row["report_date"],
                verdict=row["verdict"],
                rules_changed=bool(row["rules_changed"]),
                engine_changed=bool(row["engine_changed"]),
            )
        )
    return out


@dataclass(frozen=True)
class VerdictRun:
    """A maximal same-verdict run — the granularity the "강화" badge actually
    displays at, not the atom. Deliberately carries no
    new_observation_count / last_new_observation_date /
    unknown_observation_days: R7 defines "신규관측" per atom
    (`detail.latest_at`) only. A verdict-level version would have to
    aggregate several atoms' `latest_at` into one date (e.g. by taking the
    oldest, as `min()` would) — the rules doc never registers that
    aggregation, so doing it here would be inventing a rule outside R0-R12,
    which R12 reserves for a new `transition_rules_v2.md`. Any caller
    wanting a "신규관측" figure for a verdict run must therefore treat it as
    미상 (unknown), never compute one from this dataclass."""

    thesis_id: str
    verdict: str
    entry_date: str | None  # None when left_censored (R11)
    left_censored: bool
    last_report_date: str
    duration_days: int  # count of representative (판정)일, not calendar days (R6)
    has_data_gap: bool  # R10: any adjacent pair inside the run >=4 calendar days apart
    restart_reason: str | None  # None, or RESTART_RULES_CHANGED / RESTART_ENGINE_CHANGED (R5.2/.3)


def _build_verdict_runs(thesis_id: str, timeline: list[VerdictDay]) -> list[VerdictRun]:
    """Same run-splitting logic as `_build_runs` (R4/R5), keyed on `verdict`
    instead of atom `status`, and without R7/R9's new-observation bookkeeping
    (see `VerdictRun`'s docstring for why)."""
    if not timeline:
        return []

    run_starts = [0]
    for i in range(1, len(timeline)):
        prev, cur = timeline[i - 1], timeline[i]
        if cur.verdict != prev.verdict or cur.rules_changed or cur.engine_changed:
            run_starts.append(i)

    runs: list[VerdictRun] = []
    for k, start in enumerate(run_starts):
        end = (run_starts[k + 1] - 1) if k + 1 < len(run_starts) else len(timeline) - 1
        days = timeline[start:end + 1]
        first = days[0]

        restart_reason = None
        if start > 0:
            reasons = []
            if first.rules_changed:
                reasons.append(RESTART_RULES_CHANGED)
            if first.engine_changed:
                reasons.append(RESTART_ENGINE_CHANGED)
            restart_reason = " · ".join(reasons) if reasons else None

        has_gap = any(
            _calendar_gap_days(days[i - 1].report_date, days[i].report_date) >= 4
            for i in range(1, len(days))
        )

        runs.append(
            VerdictRun(
                thesis_id=thesis_id,
                verdict=first.verdict,
                entry_date=None if start == 0 else first.report_date,
                left_censored=(start == 0),
                last_report_date=days[-1].report_date,
                duration_days=len(days),
                has_data_gap=has_gap,
                restart_reason=restart_reason,
            )
        )
    return runs


def verdict_runs(conn: sqlite3.Connection, thesis_id: str,
                 pending: dict | None = None, as_of: str | None = None) -> list[VerdictRun]:
    """The full verdict-run history for one thesis, oldest first — the
    granularity the "강화" badge actually displays at (R4's "TRUE → TRUE는
    전이가 아니다" applies the same way to "강화 → 강화"). `pending`: see
    `_merge_pending`. `as_of`: see `_representative_rows`."""
    return _build_verdict_runs(thesis_id, verdict_timeline(conn, thesis_id, pending, as_of))


def thesis_view(conn: sqlite3.Connection, thesis_id: str, pending: dict | None = None,
               as_of: str | None = None) -> dict:
    """Bundles the whole derived view for one thesis: R11's `ledger_start`,
    the atom-level run history (`all_atom_runs`), and the verdict-level run
    history (`verdict_runs`) — one call for what a display layer needs,
    instead of three separate queries against the ledger.

    `pending` (ST4 item 3 / Risk R-1): an in-memory review row for this
    thesis that hasn't been written to `thesis_reviews` yet, in the shape
    `pending_row_from_review` produces. `ops.interpret_report` records first
    and passes None (the row is already the ledger's latest); the `market-intel
    interpret` CLI path never records, so it passes the row it just computed
    — otherwise its "지속"/"새 관측" counts would read one representative day
    behind the pipeline's for the identical report.

    `as_of` (F-A fix, SA-10): both call sites pass their own report's
    `report_date` here — the display layer's only source of "how far this
    report is allowed to see" is the report itself, never the wall clock or
    the ledger's actual head. See `_representative_rows` for why."""
    return {
        "ledger_start": ledger_start(conn, thesis_id, pending, as_of),
        "atoms": all_atom_runs(conn, thesis_id, pending, as_of),
        "verdict": verdict_runs(conn, thesis_id, pending, as_of),
    }
