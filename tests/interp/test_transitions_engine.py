"""ST2 — the transition-rules derived-view engine (`interp/transitions.py`).

Every test here cites the rule (R0-R12, `theses/transition_rules_v1.md`) it
guards. The engine's whole reason to exist is the bug in the brief: a
condition that stays TRUE for N days must not look like N "강화" events —
`test_true_true_streak_is_one_transition_and_duration_only_grows` is the
direct regression test for that (spec acceptance criterion: "동일 월간 관측이
28일 반복돼도 전이는 1건, 지속일만 증가").

Rows are seeded two ways:
  - `_seed` (raw INSERT, same pattern as `test_thesis.py`'s append-only test)
    when a test needs to control `engine_version`/`rules_changed`/
    `engine_changed`/duplicate `cutoff_utc` directly.
  - `store_mod.record_reviews` for the "does this survive the real write
    path" sanity checks.
This file never touches `fact_revisions` and never UPDATEs/DELETEs a
`thesis_reviews` row — append-only in, derived view out.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from market_intel import db as db_mod
from market_intel.interp import store as store_mod
from market_intel.interp import transitions as tr

# Pinned snapshot of two real ledger histories (consumer_cycle_1/retail_1y_up,
# fin_credit_1/dgs10_high) used by the real-data regression tests below. Not
# a live database path: a small, repo-committed JSON fixture, so those tests
# run unconditionally in any environment instead of silently skipping when a
# machine-local DB copy is missing (see `_seed_real_data_fixture`).
_REAL_DATA_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "transitions_real_data.json"


# --- seeding helpers ---------------------------------------------------------

def _atoms_json(atom_id: str | None, status: str | None, latest_at: str | None = None,
                 category: str = "strengthen", extra_detail: dict | None = None) -> str:
    """One atom's atoms_json for one row. `atom_id=None` produces a row where
    the atom is simply absent (R3 "없음"). `extra_detail` merges in fields
    besides `latest_at` — e.g. `latest_event_at`, the field R7 explicitly
    forbids treating as a substitute for `latest_at`."""
    if atom_id is None:
        return json.dumps({category: []}, ensure_ascii=False)
    detail = dict(extra_detail) if extra_detail else {}
    if latest_at is not None:
        detail["latest_at"] = latest_at
    return json.dumps({category: [{"id": atom_id, "status": status, "detail": detail}]}, ensure_ascii=False)


def _seed(conn: sqlite3.Connection, *, thesis_id: str, report_date: str, cutoff_utc: str,
          atoms_json: str, rules_sha256: str = "rs1", rules_changed: int = 0,
          engine_version: str = "2b.1", engine_changed: int = 0,
          report_type: str = "morning", verdict: str = "강화") -> None:
    now = db_mod.iso_utc()
    conn.execute(
        "INSERT INTO thesis_reviews(review_id, thesis_id, report_type, report_date, cutoff_utc, "
        "verdict, prev_verdict, changed, atoms_json, evidence_json, engine_version, created_at, "
        "rules_sha256, rules_changed, engine_changed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"r_{uuid.uuid4().hex[:16]}", thesis_id, report_type, report_date, cutoff_utc,
         verdict, None, 0, atoms_json, "{}", engine_version, now, rules_sha256, rules_changed, engine_changed),
    )
    conn.commit()


def _seed_real_data_fixture(conn: sqlite3.Connection, thesis_id: str) -> None:
    """Seeds one thesis's pinned real-ledger snapshot (`_REAL_DATA_FIXTURE`)
    into a fresh local DB via the same `_seed` helper every other test in
    this file uses. Frozen at the time this engine was picked so the
    real-data regression numbers stay reproducible without depending on a
    machine-local database copy (Prime Rule 8: the version this replaces
    hardcoded a scratchpad session path and `skipif`'d when it was absent —
    deleting that file silently made the test that proves this engine beat
    the alternative vanish instead of fail)."""
    with open(_REAL_DATA_FIXTURE, encoding="utf-8") as f:
        fixture = json.load(f)[thesis_id]
    atom_id = fixture["atom_id"]
    for row in fixture["rows"]:
        _seed(
            conn, thesis_id=thesis_id, report_date=row["report_date"], cutoff_utc=row["cutoff_utc"],
            atoms_json=_atoms_json(atom_id, row["status"], latest_at=row["latest_at"]),
            rules_changed=row["rules_changed"], engine_version=row["engine_version"],
        )


# --- R4 / R6 / R9: the core bug fix -----------------------------------------

def test_true_true_streak_is_one_transition_and_duration_only_grows(conn):
    """The acceptance criterion, verbatim: a monthly atom that reads TRUE off
    the *same* observation for 28 straight representative days must show as
    ONE transition (into TRUE) with duration climbing to 28 — never 28
    separate 'strengthen' events (R4: 'TRUE → TRUE는 전이가 아니다. 이것이 이
    작업의 핵심이다')."""
    thesis_id = "t_monthly"
    # Day 0: FALSE (baseline). Day 1: flips TRUE on a fresh monthly print.
    # Days 2-28: the same print repeated on 27 more daily judgments — no new
    # data arrives, but the naive "did it read TRUE today" counter that
    # caused the bug would still fire 27 more times.
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01",
          atoms_json=_atoms_json("retail_1y_up", "FALSE", latest_at="2026-05-01"), cutoff_utc="2026-07-01T00:00:00+00:00")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02",
          atoms_json=_atoms_json("retail_1y_up", "TRUE", latest_at="2026-06-01"), cutoff_utc="2026-07-02T00:00:00+00:00")
    for i in range(2, 29):  # 2026-07-03 .. 2026-07-29 (27 more days, same print)
        _seed(conn, thesis_id=thesis_id, report_date=f"2026-07-{i + 1:02d}",
              atoms_json=_atoms_json("retail_1y_up", "TRUE", latest_at="2026-06-01"),
              cutoff_utc=f"2026-07-{i + 1:02d}T00:00:00+00:00")

    timeline = tr.atom_timeline(conn, thesis_id, "retail_1y_up")
    assert len(timeline) == 29  # 1 FALSE day + 28 TRUE days

    transitions = sum(1 for i in range(1, len(timeline)) if timeline[i].status != timeline[i - 1].status)
    assert transitions == 1  # only FALSE->TRUE; none of the 27 repeat days is a transition

    runs = tr.atom_runs(conn, thesis_id, "retail_1y_up")
    assert [r.status for r in runs] == ["FALSE", "TRUE"]
    true_run = runs[1]
    assert true_run.duration_days == 28  # 지속 grows with representative days, not with re-affirmations
    assert true_run.entry_date == "2026-07-02"
    assert true_run.new_observation_count == 1  # the one real monthly print, not 28
    assert true_run.last_new_observation_date == "2026-07-02"


def test_daily_atom_racks_up_many_new_observations_in_the_same_run(conn):
    """Contrast case: a daily-cadence atom staying TRUE for the same span
    should show many new observations (R9: duration alone must never be read
    as independent confirmations — pairing it with new_observation_count is
    what tells daily and monthly apart)."""
    thesis_id = "t_daily"
    for i in range(10):
        _seed(conn, thesis_id=thesis_id, report_date=f"2026-07-{i + 1:02d}",
              atoms_json=_atoms_json("dgs10_high", "TRUE", latest_at=f"2026-06-{i + 1:02d}"),
              cutoff_utc=f"2026-07-{i + 1:02d}T00:00:00+00:00")
    runs = tr.atom_runs(conn, thesis_id, "dgs10_high")
    assert len(runs) == 1
    assert runs[0].duration_days == 10
    assert runs[0].new_observation_count == 9  # day 0 excluded (R7 bullet 1), the other 9 all bring new prints


# --- R4: UNKNOWN is its own state, not glued to the surrounding TRUE run ----

def test_true_unknown_true_is_two_transitions_and_two_runs(conn):
    """R4: 'TRUE → UNKNOWN → TRUE는 전이 2건이며 구간이 끊긴다' — an explicit
    anti-goal-post-move clause. UNKNOWN must never be silently absorbed into
    the surrounding TRUE run."""
    thesis_id = "t_gap_unknown"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "UNKNOWN"))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-03", cutoff_utc="2026-07-03T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert [(r.status, r.duration_days) for r in runs] == [("TRUE", 1), ("UNKNOWN", 1), ("TRUE", 1)]
    timeline = tr.atom_timeline(conn, thesis_id, "a1")
    transitions = sum(1 for i in range(1, len(timeline)) if timeline[i].status != timeline[i - 1].status)
    assert transitions == 2


# --- R3 / R4: an atom that simply isn't in atoms_json yet -------------------

def test_absent_atom_is_none_state_and_none_to_true_is_a_transition(conn):
    """R3: no `id` in that day's atoms_json => status '없음', not FALSE/UNKNOWN.
    R4: '없음 → TRUE도 전이 1건이다.'"""
    thesis_id = "t_new_condition"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json(None, None))  # condition doesn't exist yet
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("new_cond", "TRUE", latest_at="2026-06-01"), rules_changed=1)

    timeline = tr.atom_timeline(conn, thesis_id, "new_cond")
    assert timeline[0].status == tr.STATUS_NONE
    assert timeline[1].status == "TRUE"
    runs = tr.atom_runs(conn, thesis_id, "new_cond")
    assert [r.status for r in runs] == [tr.STATUS_NONE, "TRUE"]


# --- R2: a day with two reports collapses to the last row -------------------

def test_two_reports_same_day_counts_as_one_representative_day(conn):
    """R2: 'R1 정렬에서 마지막 행'을 대표로 삼고, 나머지는 무시한다. Two reports
    on one calendar day must not double the run's duration."""
    thesis_id = "t_two_reports"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T07:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), report_type="close_delta")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T22:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), report_type="morning")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T07:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), report_type="close_delta")

    timeline = tr.atom_timeline(conn, thesis_id, "a1")
    assert len(timeline) == 2  # not 3 — the two 07-01 rows collapse to one representative day
    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert runs[0].duration_days == 2


def test_representative_row_is_the_later_one_when_they_disagree(conn):
    """Same R2 rule, made observable: if the two same-day reports disagree,
    the later one (by R1 order) is what counts."""
    thesis_id = "t_two_reports_disagree"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T07:00:00+00:00",
          atoms_json=_atoms_json("a1", "FALSE"), report_type="close_delta")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T22:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), report_type="morning")

    timeline = tr.atom_timeline(conn, thesis_id, "a1")
    assert len(timeline) == 1
    assert timeline[0].status == "TRUE"  # the 22:00 row, not the 07:00 one


# --- R1: rowid tie-break when cutoff_utc ties -------------------------------

def test_rowid_breaks_ties_on_equal_cutoff_utc(conn):
    """R1: 'created_at은 초 단위라 동률이 실제로 발생한다' — same for cutoff_utc
    in this synthetic case. Two rows on the same day with identical
    cutoff_utc must resolve by insertion order (rowid), not be silently
    ambiguous."""
    thesis_id = "t_tie"
    same_cutoff = "2026-07-01T07:00:00+00:00"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc=same_cutoff,
          atoms_json=_atoms_json("a1", "FALSE"), report_type="close_delta")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc=same_cutoff,
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), report_type="morning")

    timeline = tr.atom_timeline(conn, thesis_id, "a1")
    assert len(timeline) == 1
    assert timeline[0].status == "TRUE"  # inserted second => higher rowid => wins R1's tie-break


# --- ST4 item 3 / spec Risk R-1: pending merge ------------------------------
#
# Not part of R0-R12 (those govern the ledger; `pending` is about a caller
# that hasn't written to the ledger yet). `ops.interpret_report` records the
# review before it asks for the derived view, so by the time it calls in,
# `store.reviews_for_thesis` already has today's row and it passes
# `pending=None`. `market-intel interpret` (`cli_interpret._thesis_summary`)
# computes the identical review but never persists it — without this merge
# its "지속"/"새 관측" numbers would read one representative day behind the
# pipeline's for the exact same report, which is the "하루 어긋난 숫자" R-1
# calls the single highest-impact risk in this milestone.

def test_pending_row_fills_in_todays_unrecorded_representative_day(conn):
    thesis_id = "t_pending"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))

    without_pending = tr.atom_runs(conn, thesis_id, "a1")
    assert without_pending[0].duration_days == 1

    pending = tr.pending_row_from_review({
        "report_date": "2026-07-02", "verdict": "강화",
        "atoms": json.loads(_atoms_json("a1", "TRUE", latest_at="2026-06-01")),
        "rules_changed": 0, "engine_version": "2b.3", "engine_changed": 0,
    })
    with_pending = tr.atom_runs(conn, thesis_id, "a1", pending=pending)
    assert with_pending[0].duration_days == 2, "pending 미병합 — 두 경로가 하루 어긋난다"

    # Calling twice with the same pending (a caller may rebuild it per
    # request) must not double-count the day.
    twice = tr.atom_runs(conn, thesis_id, "a1", pending=pending)
    assert twice[0].duration_days == 2


def test_pending_yields_to_an_already_recorded_row_for_the_same_date(conn):
    """If the pending row's date is already in the ledger (a rerun after the
    pipeline already recorded it), the recorded row wins — pending only
    fills a gap, it never shadows the ledger."""
    thesis_id = "t_pending_recorded_already"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "FALSE"))

    # A stale pending guess for 07-02 that disagrees with what was recorded.
    pending = tr.pending_row_from_review({
        "report_date": "2026-07-02", "verdict": "강화",
        "atoms": json.loads(_atoms_json("a1", "TRUE", latest_at="2026-06-01")),
        "rules_changed": 0, "engine_version": "2b.3", "engine_changed": 0,
    })
    runs = tr.atom_runs(conn, thesis_id, "a1", pending=pending)
    assert [r.status for r in runs] == ["TRUE", "FALSE"], "기록된 행이 있는데 pending이 덮어썼다"


def test_pending_row_from_review_degrades_to_none_on_an_unrecognized_shape():
    """A caller (e.g. a test double standing in for a thesis module ST4
    doesn't own) may hand back a review row missing `report_date`/`atoms` —
    this must degrade to None (pending=None: today's state simply isn't
    shown yet) rather than raise, so a stand-in module doesn't crash the
    whole render."""
    assert tr.pending_row_from_review({"thesis_id": "x", "verdict": "유지"}) is None


def test_thesis_view_accepts_pending_too(conn):
    """`thesis_view` (the one-call bundle display layers actually use)
    threads `pending` down to all three of its parts."""
    thesis_id = "t_pending_thesis_view"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    pending = tr.pending_row_from_review({
        "report_date": "2026-07-02", "verdict": "강화",
        "atoms": json.loads(_atoms_json("a1", "TRUE", latest_at="2026-06-01")),
        "rules_changed": 0, "engine_version": "2b.3", "engine_changed": 0,
    })
    view = tr.thesis_view(conn, thesis_id, pending=pending)
    assert view["atoms"]["a1"][0].duration_days == 2
    assert view["verdict"][0].duration_days == 2


# --- R4/R1: representative days must be date-ordered, not recording-order --

def test_backfilled_earlier_report_date_does_not_reverse_the_timeline(conn):
    """Regression: a late-arriving (backfilled) row for an EARLIER
    `report_date` can carry a LATER `cutoff_utc` than an already-recorded
    later date. R1's `cutoff_utc`/`rowid` order decides which row
    *represents* a given day (R2) — it is not license to lay the
    representative days out in recording order. R4 defines a transition as
    being between '날짜순으로 인접한 두 대표일' (date-order-adjacent days), so
    the final sequence handed to run-building must be `report_date`-ordered.

    Without that final sort, this thesis's second run would show
    `entry_date == '2026-07-01'` immediately after a first run whose own
    last day is '2026-07-10' — time running backwards inside one thesis's
    timeline. This is the exact shape of the real bug (measured: entry date
    07-01 landed after the preceding section's 07-10)."""
    thesis_id = "t_backfill_reorder"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-10", cutoff_utc="2026-07-10T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    # Backfilled after the fact: report_date is BEFORE 07-10, but it was
    # recorded — and so cutoff_utc-ordered — AFTER it.
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-08-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "FALSE"))

    timeline = tr.atom_timeline(conn, thesis_id, "a1")
    assert [d.report_date for d in timeline] == ["2026-07-01", "2026-07-10"]  # date order, not cutoff order

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert [(r.status, r.left_censored, r.entry_date) for r in runs] == [
        ("FALSE", True, None),
        ("TRUE", False, "2026-07-10"),
    ]


# --- R5.2: rules_changed forces a new run without a status change ----------

def test_rules_changed_forces_new_run_even_when_status_is_unchanged(conn):
    """R5 condition 2 + the §7 enforcement note: a goal-post move (threshold
    edited) must not let the old and new run merge into one long TRUE streak,
    even though the observed status didn't change."""
    thesis_id = "t_rules_changed"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), rules_sha256="v1")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), rules_sha256="v2", rules_changed=1)
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-03", cutoff_utc="2026-07-03T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), rules_sha256="v2")

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert [(r.status, r.duration_days, r.restart_reason) for r in runs] == [
        ("TRUE", 1, None),
        ("TRUE", 2, tr.RESTART_RULES_CHANGED),
    ]


# --- R5.3: engine_changed forces a new run -----------------------------------

def test_engine_version_change_forces_new_run(conn):
    """R5 condition 3 ('그 행의 engine_changed = 1', §4 해법). Until ST3 this
    column didn't exist and the engine derived it from `engine_version`
    changing between representative days; ST3 added the real column
    (`thesis.review()` computes it exactly like `rules_changed`) and this
    module now reads it directly (see module docstring) — so the test seeds
    `engine_changed` itself, mirroring `test_rules_changed_forces_new_run_...`
    above rather than relying on an `engine_version` diff."""
    thesis_id = "t_engine_changed"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), engine_version="2b.1")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), engine_version="2b.2", engine_changed=1)
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-03", cutoff_utc="2026-07-03T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), engine_version="2b.2")

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert [(r.status, r.duration_days, r.restart_reason) for r in runs] == [
        ("TRUE", 1, None),
        ("TRUE", 2, tr.RESTART_ENGINE_CHANGED),
    ]


# --- R7: new-observation determination --------------------------------------

def test_first_ledger_row_is_not_counted_as_new_or_unknown(conn):
    """R7 bullet 1: the ledger's first row has no predecessor, so it's simply
    excluded — not counted as new, and (fixed after an initial hand-check
    against real data) not folded into 'unknown' either, since 'unknown'
    specifically means 'a predecessor exists but its latest_at is missing'."""
    thesis_id = "t_first_row"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert runs[0].new_observation_count == 0
    assert runs[0].unknown_observation_days == 0
    assert runs[0].last_new_observation_date is None


def test_missing_latest_at_on_either_side_is_unknown_not_false(conn):
    """R7 bullet 2: pre-2026-08-12-style rows carry no `detail.latest_at` at
    all. Missing on either side of the comparison must show as 미상
    (unknown_observation_days), never silently counted as 'not new'."""
    thesis_id = "t_missing_latest_at"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE"))  # no latest_at (old-style row)
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE"))  # still no latest_at
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-03", cutoff_utc="2026-07-03T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))  # field newly appears
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-04", cutoff_utc="2026-07-04T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))  # same value, both sides present

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert len(runs) == 1
    run = runs[0]
    assert run.duration_days == 4
    # day2 (both None): 미상. day3 (prev None, this present): 미상. day4 (both present, equal): not new.
    assert run.unknown_observation_days == 2
    assert run.new_observation_count == 0


def test_latest_event_at_is_never_a_substitute_for_latest_at(conn):
    """R7 bullet 2, the forbidden-substitution clause verbatim: '일부 조건
    종류에만 있는 latest_event_at으로 대체하지 않는다.' This test is synthetic
    (no real-data dependency) so it still catches the violation even in an
    environment where the real-data tests above are skipped or their fixture
    is unavailable — the two real-data assertions this repair pass just
    fixed were previously the ONLY thing guarding this rule; delete them and
    the same violation would pass green again.

    Both rows carry `latest_event_at` (present on the DGS10-style atoms in
    real data) and never `latest_at`. `latest_event_at` changes between the
    two days — if the engine substituted it for `latest_at`, this would
    wrongly read as a new observation. Per R7 it must instead be 미상,
    because `latest_at` itself is absent on both sides."""
    thesis_id = "t_forbidden_substitute"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", extra_detail={"latest_event_at": "2026-06-01T00:00:00+00:00"}))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", extra_detail={"latest_event_at": "2026-06-30T00:00:00+00:00"}))

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert len(runs) == 1
    run = runs[0]
    assert run.new_observation_count == 0  # NOT 1 — latest_event_at changing must not count
    assert run.last_new_observation_date is None
    assert run.unknown_observation_days == 1  # day 2: 미상, since latest_at is absent on both sides


# --- R10: recorded gaps flag the run but never break it ---------------------

def test_weekend_gap_does_not_flag_but_four_plus_day_gap_does(conn):
    """R10: '금->월은 3일이므로 정상 주말은 걸리지 않는다.' A gap must not break
    the run either way (R10: '공백은 구간을 끊지 않는다')."""
    thesis_id = "t_gap"
    # Fri 07-03 -> Mon 07-06: 3 calendar days, a normal weekend, no flag.
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-03", cutoff_utc="2026-07-03T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-06", cutoff_utc="2026-07-06T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert len(runs) == 1
    assert runs[0].has_data_gap is False
    assert runs[0].duration_days == 2  # the run is not broken by the gap

    # Now extend past a holiday: 07-06 -> 07-11 is 5 calendar days.
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-11", cutoff_utc="2026-07-11T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert len(runs) == 1  # still one run
    assert runs[0].duration_days == 3
    assert runs[0].has_data_gap is True


# --- R11: left-censoring -----------------------------------------------------

def test_left_censored_only_on_the_ledgers_first_run(conn):
    """R11: only the run that starts on the ledger's very first representative
    day is left-censored (entry_date hidden — display owes '기록 시작 이전부터'
    instead, which is out of this engine's scope, but the flag it needs is
    not). A later run, even of the same status, must show a real entry_date."""
    thesis_id = "t_left_censor"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "FALSE"))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-03", cutoff_utc="2026-07-03T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-15"))

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert runs[0].status == "TRUE" and runs[0].left_censored is True and runs[0].entry_date is None
    assert runs[1].status == "FALSE" and runs[1].left_censored is False and runs[1].entry_date == "2026-07-02"
    assert runs[2].status == "TRUE" and runs[2].left_censored is False and runs[2].entry_date == "2026-07-03"


# --- R0: reads only thesis_reviews, never fact_revisions --------------------

def test_engine_module_never_references_fact_revisions():
    """R0's boundary, structurally enforced (belt-and-suspenders alongside
    the package-wide `test_apply.py::test_interp_never_touches_fact_revisions_or_raw_sql`,
    which already covers this file — this test pins it locally too so the
    guarantee survives even if that structural test ever moves). Reuses that
    test's own docstring-stripping helper: the module docstring here
    *explains* the boundary in prose ("never re-reads fact_revisions"), and a
    promise not to do the thing must not itself read as doing the thing."""
    from test_apply import _code_without_docstrings

    code = _code_without_docstrings(Path(tr.__file__))
    assert "fact_revisions" not in code
    assert "SELECT " not in code  # all SQL lives in store.py (SA-1); this module only calls store_mod


# --- real-data acceptance check (pinned fixture, tests/fixtures/transitions_real_data.json) --

def test_real_data_retail_1y_up_is_one_run_with_one_new_observation(conn):
    """The exact case from the brief: 소매판매(월간) read TRUE for 14 straight
    representative days with only 1 real new print underneath it."""
    _seed_real_data_fixture(conn, "consumer_cycle_1")
    runs = tr.atom_runs(conn, "consumer_cycle_1", "retail_1y_up")
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "TRUE"
    assert run.duration_days == 14
    assert run.new_observation_count == 1  # not 14 "reinforcements"
    assert run.last_new_observation_date == "2026-08-15"
    assert run.left_censored is True  # this thesis's ledger starts already TRUE
    # 7 days are genuinely 미상: detail.latest_at doesn't exist yet on either
    # side (field starts appearing partway through the fixture). Hand-
    # verified against the raw atoms_json before pinning this fixture.
    assert run.unknown_observation_days == 7


def test_real_data_dgs10_high_is_one_run_with_several_new_observations(conn):
    """The other case from the brief: 미 10년물 read TRUE for 18 straight days.
    Unlike the monthly retail case, DGS10 is a daily series, so several of
    those days DO carry a genuinely new print — the derived view must show
    that distinction (5 new observations out of 18 days), which a flat
    '강화 18회' counter erases."""
    _seed_real_data_fixture(conn, "fin_credit_1")
    runs = tr.atom_runs(conn, "fin_credit_1", "dgs10_high")
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "TRUE"
    assert run.duration_days == 18
    assert run.new_observation_count == 5
    assert 0 < run.new_observation_count < run.duration_days  # neither "no news" nor "every day is new"
    assert run.left_censored is True
    # 11 days are 미상 for the same reason as above.
    assert run.unknown_observation_days == 11


# --- sanity: the engine also works fed through the real write path ---------

# --- R11: streak_since is carried as a reference field, never an entry_date -

def test_streak_since_is_carried_on_run_but_never_used_as_entry_date(conn):
    """R11: 'detail.streak.since는 최초진입일로 쓰지 않는다... 참고 필드로
    데이터에는 남기되 화면 기본 표시에서는 뺀다.' The ledger-based `entry_date`
    must stay the first representative day's `report_date`; `streak_since`
    rides along on the Run as a separate, data-only field (a backfill can
    move it backward — R11's krw_weak example — without touching
    `entry_date`)."""
    thesis_id = "t_streak_since"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-30", cutoff_utc="2026-07-30T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01",
                                  extra_detail={"streak": {"since": "2025-09-25"}}))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-31", cutoff_utc="2026-07-31T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01",
                                  extra_detail={"streak": {"since": "2025-09-18"}}))  # backfill moved it earlier

    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert len(runs) == 1
    run = runs[0]
    assert run.entry_date is None and run.left_censored is True  # ledger-based, untouched by the backfill
    assert run.streak_since == "2025-09-18"  # reference field: the run's last known value


def test_streak_since_absent_is_none_not_a_missing_field_error(conn):
    """Rows recorded before `detail.streak` existed must not raise or fabricate
    a value — `streak_since` is simply None."""
    thesis_id = "t_streak_since_absent"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert runs[0].streak_since is None


# --- transplanted from st2-a: ledger_start + verdict-level RunView ----------

def test_ledger_start_is_the_earliest_representative_day(conn):
    """R11's '기록 시작(YYYY-MM-DD)' string needs the ledger's first
    representative day; `ledger_start` exposes it so a display doesn't have
    to re-query the raw ledger separately from calling `atom_runs`."""
    thesis_id = "t_ledger_start"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-15", cutoff_utc="2026-07-15T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-16", cutoff_utc="2026-07-16T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))
    assert tr.ledger_start(conn, thesis_id) == "2026-07-15"


def test_ledger_start_is_none_for_a_thesis_with_no_reviews(conn):
    assert tr.ledger_start(conn, "t_never_reviewed") is None


def test_verdict_true_true_streak_is_one_run_not_n_reinforcements(conn):
    """The same R4 bug the whole engine exists to fix, but at the granularity
    the "강화" badge actually displays at: the thesis's own `verdict` column,
    not one atom's status. A verdict repeated for 5 straight representative
    days must be ONE run with duration climbing to 5."""
    thesis_id = "t_verdict_streak"
    for i in range(5):
        _seed(conn, thesis_id=thesis_id, report_date=f"2026-07-{i + 1:02d}",
              cutoff_utc=f"2026-07-{i + 1:02d}T00:00:00+00:00",
              atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), verdict="강화")

    runs = tr.verdict_runs(conn, thesis_id)
    assert len(runs) == 1
    assert runs[0].verdict == "강화"
    assert runs[0].duration_days == 5
    assert runs[0].left_censored is True


def test_verdict_runs_split_on_verdict_change_and_rules_changed(conn):
    """Verdict runs split on a verdict change (R4/R5.1) and on rules_changed
    (R5.2), the same way atom runs do."""
    thesis_id = "t_verdict_split"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), rules_sha256="v1", verdict="유지")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-02", cutoff_utc="2026-07-02T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), rules_sha256="v1", verdict="강화")
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-03", cutoff_utc="2026-07-03T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"), rules_sha256="v2", rules_changed=1,
          verdict="강화")

    runs = tr.verdict_runs(conn, thesis_id)
    assert [(r.verdict, r.duration_days, r.restart_reason) for r in runs] == [
        ("유지", 1, None),
        ("강화", 1, None),
        ("강화", 1, tr.RESTART_RULES_CHANGED),
    ]


def test_verdict_run_has_no_new_observation_fields():
    """⚠ Guard against re-porting st2-a's `_verdict_latest_at` min()
    aggregation: R7 only defines '신규관측' per atom. `VerdictRun` must not
    carry a new_observation_count/last_new_observation_date field at all —
    inventing one at verdict granularity would be an undocumented rule,
    which R12 reserves for a registered `transition_rules_v2.md`."""
    field_names = {f for f in tr.VerdictRun.__dataclass_fields__}
    assert "new_observation_count" not in field_names
    assert "last_new_observation_date" not in field_names


def test_thesis_view_bundles_ledger_start_atoms_and_verdict(conn):
    """`thesis_view` is the one-call shape a display layer needs: ledger_start
    (R11) plus both run granularities, instead of three separate calls."""
    thesis_id = "t_thesis_view"
    _seed(conn, thesis_id=thesis_id, report_date="2026-07-01", cutoff_utc="2026-07-01T00:00:00+00:00",
          atoms_json=_atoms_json("a1", "TRUE", latest_at="2026-06-01"))

    view = tr.thesis_view(conn, thesis_id)
    assert view["ledger_start"] == "2026-07-01"
    assert set(view["atoms"]) == {"a1"}
    assert view["atoms"]["a1"][0].entry_date is None  # left-censored, same Run objects as atom_runs
    assert view["verdict"][0].verdict == "강화"  # _seed's default verdict


def test_engine_reads_rows_written_through_record_reviews(conn):
    """Not just hand-seeded rows — the engine must also work on rows written
    the normal way (`thesis.review()` -> `store.record_reviews`)."""
    thesis_id = "t_real_write_path"
    row = {
        "thesis_id": thesis_id, "report_type": "morning", "report_date": "2026-07-01",
        "cutoff_utc": "2026-07-01T00:00:00+00:00", "verdict": "강화", "prev_verdict": None, "changed": 1,
        "atoms_json": _atoms_json("a1", "TRUE", latest_at="2026-06-01"), "evidence_json": "{}",
        "rules_sha256": "rs1", "rules_changed": 0,
    }
    store_mod.record_reviews(conn, [row])
    runs = tr.atom_runs(conn, thesis_id, "a1")
    assert len(runs) == 1
    assert runs[0].status == "TRUE"
    assert runs[0].duration_days == 1
