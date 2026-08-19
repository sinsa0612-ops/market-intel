"""ST4 — 표시 교체: 리포트/사이트가 파생 뷰(`interp/transitions.py`)를 실제로
소비하는 자리 (spec §5 §3 "화면에 나가는 것").

Three things this file exists to prove that no other test file proves:

1. **Risk R-1 (spec's own "가장 먼저 칠 것")**: `ops.interpret_report` (records
   the review, then reads the derived view back) and `market-intel interpret`
   / `cli_interpret._thesis_summary` (never records — see that module's own
   docstring) must show the SAME "지속"/"새 관측" numbers for the identical
   report. Without `transitions.thesis_view(..., pending=...)` the CLI path
   would read one representative day behind the pipeline's.
2. R9's guard, structurally: a duration fragment never appears without a
   "새 관측" fragment next to it.
3. The legend (§3.1) carries a real `introduced_on` date, and `render_impact`
   degrades gracefully (no state fragment, no crash) when it isn't given one.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from market_intel import cli_interpret as cli_interpret_mod
from market_intel import db as db_mod
from market_intel.interp import ops as ops_mod
from market_intel.interp import store as store_mod
from market_intel.interp import thesis as thesis_mod
from market_intel.interp import transitions as tr_mod
from market_intel.reporting.cutoff import KST

from conftest import macro_fc, make_report, seed_fact

# --- shared fixture: one real threshold thesis, one real macro series -------

_THESIS = {
    "thesis_id": "fin_credit_1", "theme": "fin_credit", "slot": 1,
    "statement": "미 10년물 금리가 높은 수준을 유지한다",
    "leading_indicators": ["DGS10 value"], "next_check_date": "2026-11-01",
    "conditions": {
        "falsify": [{"id": "dgs10_collapse", "kind": "threshold", "category": "macro",
                     "subject": "DGS10", "metric": "value", "op": "<=", "value": -100.0}],
        "weaken": [],
        "strengthen": [{"id": "dgs10_high", "kind": "threshold", "category": "macro",
                        "subject": "DGS10", "metric": "value", "op": ">=", "value": 4.5}],
    },
}

# 4차 검수 F-A 잔여분 수리: `record_reviews`가 찍는 `created_at`은 벽시계
# (`db_mod.iso_utc()`)이고 이 테스트는 그것을 얼리지 않는다 — `engine_introduced_on`은
# 그러므로 이 테스트가 실제로 실행되는 "오늘"의 KST 날짜를 돌려준다. 픽스처의
# `report_date`(_NEW_DAY)가 달력에 못박힌 과거 날짜였다면, 올바른 가드
# (`도입일 > report_date`면 범례의 날짜 조각을 생략)가 그 값을 정직하게 "미래"로
# 판정해 범례를 지운다 — 결함이 아니라 이 픽스처가 실행 시점과 무관한 상수를
# 썼기 때문이다(4차 검수 F-A 잔여분 지적). 실행 시점의 실제 오늘을 기준으로 삼는다.
_TODAY = datetime.now(KST).date()
_HISTORY_DATES = [(_TODAY - timedelta(days=n)).isoformat() for n in (3, 2, 1)]
_NEW_DAY = _TODAY.isoformat()


def _theses_json_payload(theses: list[dict]) -> dict:
    themes: dict[str, dict] = {}
    for t in theses:
        themes.setdefault(t["theme"], {"theses": []})["theses"].append({
            "id": t["thesis_id"], "slot": t["slot"], "statement": t["statement"],
            "leading_indicators": t["leading_indicators"], "next_check_date": t["next_check_date"],
            "conditions": t["conditions"],
        })
    return {"themes": themes}


def _seed(conn, raw_dir: str) -> None:
    store_mod.replace_theses(conn, [_THESIS], "sha")
    seed_fact(conn, raw_dir, "fred",
              macro_fc("DGS10", "2026-06-01T00:00:00+00:00", 4.7),
              "2026-06-02T00:00:00+00:00")


def _record_history(conn, dates: list[str]) -> None:
    """Plays back `dates` through the real `thesis.review()` + `record_reviews`
    — the same two calls the pipeline makes every morning — so both sides of
    the R-1 comparison start from an identical, realistically-produced ledger."""
    for d in dates:
        cutoff = datetime.fromisoformat(f"{d}T22:00:00+00:00")
        reviews = thesis_mod.review(conn, store_mod.list_theses(conn), cutoff, "morning", d)
        store_mod.record_reviews(conn, reviews)


def test_pipeline_and_cli_paths_agree_on_duration_and_new_observations(tmp_path):
    """Spec Risk R-1, the single highest-impact item in the milestone: the
    two call sites must not disagree by one representative day."""
    # --- path A: ops.interpret_report (records first, then reads the ledger back) ---
    db_a = str(tmp_path / "a" / "market_intel.db")
    raw_a = str(tmp_path / "a" / "raw")
    Path(raw_a).mkdir(parents=True)
    db_mod.init_db(db_a)
    conn_a = db_mod.connect(db_a)
    _seed(conn_a, raw_a)
    _record_history(conn_a, _HISTORY_DATES)

    report_a = make_report(report_type="morning", report_date=_NEW_DAY,
                           cutoff_utc=f"{_NEW_DAY}T22:00:00+00:00")
    path_a = tmp_path / "report_a.json"
    path_a.write_text(report_a.to_json(), encoding="utf-8")
    ops_mod.interpret_report(conn_a, path_a, use_llm=False)
    impact_a = json.loads(path_a.read_text(encoding="utf-8"))["interpretation"]["thesis_impact"]

    # --- path B: market-intel interpret / cli_interpret._thesis_summary (never records) ---
    db_b = str(tmp_path / "b" / "market_intel.db")
    raw_b = str(tmp_path / "b" / "raw")
    Path(raw_b).mkdir(parents=True)
    db_mod.init_db(db_b)
    conn_b = db_mod.connect(db_b)
    _seed(conn_b, raw_b)
    _record_history(conn_b, _HISTORY_DATES)
    theses_path = tmp_path / "theses.json"
    theses_path.write_text(json.dumps(_theses_json_payload([_THESIS]), ensure_ascii=False),
                           encoding="utf-8")

    report_b = make_report(report_type="morning", report_date=_NEW_DAY,
                           cutoff_utc=f"{_NEW_DAY}T22:00:00+00:00")
    impact_b, _next_check, _summary = cli_interpret_mod._thesis_summary(conn_b, report_b, theses_path)

    assert impact_a and impact_b
    # 4 recorded days total (3 history + today) — both paths must count the
    # same. Before `pending` merging, path B stopped at "지속 3판정일".
    assert "지속 4판정일" in impact_a
    assert "지속 4판정일" in impact_b, (
        "market-intel interpret 경로가 파이프라인보다 하루 뒤처졌다 (spec Risk R-1)\n"
        f"ops:  {impact_a!r}\ncli:  {impact_b!r}"
    )
    # Same underlying fact throughout (one 2026-06-01 print, never revised),
    # and the ledger's very first day can never count as a new observation
    # (R7 bullet 1 — no predecessor to compare against) — so both paths must
    # agree there have been zero new observations since the ledger began.
    assert "새 관측 없음(이 구간)" in impact_a
    assert "새 관측 없음(이 구간)" in impact_b, (
        "두 경로의 새 관측 판단이 갈렸다\n"
        f"ops:  {impact_a!r}\ncli:  {impact_b!r}"
    )
    # final-review F4 (CEO 결정 (가)): both paths write to the same report JSON,
    # so the legend must not depend on which command happened to run it.
    assert "도입" in impact_a and "도입" in impact_b, (
        "두 경로 중 한쪽에 범례가 빠졌다 (final-review F4)\n"
        f"ops:  {impact_a!r}\ncli:  {impact_b!r}"
    )


def test_historical_report_never_cites_a_date_after_its_own_report_date(tmp_path):
    """F-A (3차 검수 회귀, SA-10): re-interpreting a PAST `report_date` — the
    entire reason `market-intel interpret --file <old report>` exists, and
    what catch-up backfill does every time it falls behind — must not let the
    display layer see representative days recorded *after* that date. Before
    this fix, `transitions.thesis_view` read the ledger's whole history with
    no upper bound, so a 2026-08-01 report re-interpreted after the ledger
    had moved on to 2026-08-19 cited "마지막 2026-08-19" and a legend
    "도입 2026-08-19" — both dates its own cutoff could not have known about.
    Covers both call sites F-A hit: `cli_interpret._thesis_summary` (never
    records) and `ops.interpret_report` (records first, reads back)."""
    db_path = str(tmp_path / "market_intel.db")
    raw_dir = str(tmp_path / "raw")
    Path(raw_dir).mkdir(parents=True)
    db_mod.init_db(db_path)
    conn = db_mod.connect(db_path)
    _seed(conn, raw_dir)

    # Ledger runs well past the historical report date below — the exact
    # shape of the CEO's repro (catch-up recorded ahead of what it is now
    # asked to re-interpret). This test only asserts *absence* (no later date,
    # no legend), so unlike `_HISTORY_DATES`/`_NEW_DAY` above it does not need
    # to track the real wall-clock "today" — a fixed, permanently-past
    # sequence is deliberately kept self-contained here rather than reusing
    # those relative constants (which now resolve to "today", not "08-01").
    historical_date = "2026-08-01"
    later_dates = ["2026-08-02", "2026-08-03", "2026-08-04", "2026-08-10", "2026-08-19"]
    all_dates = [historical_date] + later_dates

    # A fresh DGS10 print every day keeps the atom's streak/새 관측 counters
    # moving across the whole window — the real shape the CEO's repro hit
    # (DFII10: "새 관측 5건, 마지막 2026-08-19"). `_seed`'s single frozen fact
    # above never produces a new observation, so without this the as_of-cap
    # regression would not actually show up in the rendered text.
    for d in all_dates:
        seed_fact(conn, raw_dir, "fred",
                  macro_fc("DGS10", f"{d}T00:00:00+00:00", 4.7),
                  f"{d}T12:00:00+00:00")

    _record_history(conn, all_dates)

    theses_path = tmp_path / "theses.json"
    theses_path.write_text(json.dumps(_theses_json_payload([_THESIS]), ensure_ascii=False),
                           encoding="utf-8")

    # historical_date already set above ("2026-08-01") — the report under re-interpretation
    report = make_report(report_type="morning", report_date=historical_date,
                         cutoff_utc=f"{historical_date}T22:00:00+00:00")

    # --- path B: cli_interpret._thesis_summary (never records) ---
    impact_cli, _next_check, _summary = cli_interpret_mod._thesis_summary(conn, report, theses_path)
    assert impact_cli
    for later_date in later_dates:
        assert later_date not in impact_cli, (
            f"cli_interpret 경로가 과거 리포트({historical_date})에서 차단선 이후 날짜"
            f"({later_date})를 인용했다\n{impact_cli!r}"
        )
    # The legend's own introduced-on date is subject to the same cap: this
    # engine version's ledger rows were all created "now" (test run time),
    # long after 2026-08-01, so the legend must be omitted rather than
    # print a future introduction date.
    assert "도입" not in impact_cli, (
        f"과거 리포트가 자기 날짜보다 미래인 범례 도입일을 보여줬다\n{impact_cli!r}"
    )

    # --- path A: ops.interpret_report (records first, then reads the ledger back) ---
    path = tmp_path / "report.json"
    path.write_text(report.to_json(), encoding="utf-8")
    ops_mod.interpret_report(conn, path, use_llm=False)
    impact_ops = json.loads(path.read_text(encoding="utf-8"))["interpretation"]["thesis_impact"]
    assert impact_ops
    for later_date in later_dates:
        assert later_date not in impact_ops, (
            f"ops.interpret_report 경로가 과거 리포트({historical_date})에서 차단선 이후 날짜"
            f"({later_date})를 인용했다\n{impact_ops!r}"
        )
    assert "도입" not in impact_ops, (
        f"ops.interpret_report 경로가 과거 리포트에서 미래 범례 도입일을 보여줬다\n{impact_ops!r}"
    )


def test_pipeline_and_cli_paths_agree_on_the_introduced_on_legend_on_first_use(tmp_path):
    """final-review F4 (CEO 결정 (가)) — the deploy-day case, distinct from the
    R-1 test above: this one has ZERO prior ledger history, so it is the very
    first time this engine version is ever judged. `ops.interpret_report`
    records first then reads the ledger back and gets a real `introduced_on`
    from that just-inserted row. `market-intel interpret` never records (see
    `cli_interpret`'s own docstring) — before this fix it read an empty
    ledger and silently dropped the legend on exactly the day it mattered
    most (실측: 범례 있음 vs 범례 없음, 같은 리포트). Both paths must show the
    legend, with the same date, for the identical report."""
    # 위 `_TODAY`/`_NEW_DAY`와 같은 이유: 이 테스트도 wall clock을 얼리지 않으므로
    # "배포 첫날"은 실행 시점의 실제 오늘이어야 도입일 가드를 정직하게 통과한다.
    first_day = _NEW_DAY

    # --- path A: ops.interpret_report, no history at all ---
    db_a = str(tmp_path / "a" / "market_intel.db")
    raw_a = str(tmp_path / "a" / "raw")
    Path(raw_a).mkdir(parents=True)
    db_mod.init_db(db_a)
    conn_a = db_mod.connect(db_a)
    _seed(conn_a, raw_a)

    report_a = make_report(report_type="morning", report_date=first_day,
                           cutoff_utc=f"{first_day}T22:00:00+00:00")
    path_a = tmp_path / "report_a.json"
    path_a.write_text(report_a.to_json(), encoding="utf-8")
    ops_mod.interpret_report(conn_a, path_a, use_llm=False)
    impact_a = json.loads(path_a.read_text(encoding="utf-8"))["interpretation"]["thesis_impact"]

    # --- path B: market-intel interpret / _thesis_summary, same no history ---
    db_b = str(tmp_path / "b" / "market_intel.db")
    raw_b = str(tmp_path / "b" / "raw")
    Path(raw_b).mkdir(parents=True)
    db_mod.init_db(db_b)
    conn_b = db_mod.connect(db_b)
    _seed(conn_b, raw_b)
    theses_path = tmp_path / "theses.json"
    theses_path.write_text(json.dumps(_theses_json_payload([_THESIS]), ensure_ascii=False),
                           encoding="utf-8")

    report_b = make_report(report_type="morning", report_date=first_day,
                           cutoff_utc=f"{first_day}T22:00:00+00:00")
    impact_b, _next_check, _summary = cli_interpret_mod._thesis_summary(conn_b, report_b, theses_path)

    assert "도입" in impact_a, f"ops 경로에 범례가 없다 (배포 첫날): {impact_a!r}"
    assert "도입" in impact_b, (
        "market-intel interpret 경로가 배포 첫날 범례를 빠뜨렸다 (final-review F4)\n"
        f"ops: {impact_a!r}\ncli: {impact_b!r}"
    )
    date_a = re.search(r"표시는 (\S+) 도입", impact_a).group(1)
    date_b = re.search(r"표시는 (\S+) 도입", impact_b).group(1)
    assert date_a == date_b, f"두 경로의 도입일이 다르다: ops={date_a!r} cli={date_b!r}"


# --- R9's guard, structurally: duration never appears without new-obs ------

def _fragment_for(**run_overrides) -> str:
    run = tr_mod.Run(**{
        "thesis_id": "t1", "atom_id": "a", "status": "TRUE", "entry_date": "2026-08-01",
        "left_censored": False, "last_report_date": "2026-08-10", "duration_days": 1,
        "new_observation_count": 1, "last_new_observation_date": "2026-08-01",
        "unknown_observation_days": 0, "has_data_gap": False, "restart_reason": None,
        "streak_since": None,
        **run_overrides,
    })
    return thesis_mod._run_fragment(run, "2026-01-01")


def test_r9_a_duration_fragment_never_appears_without_a_new_observation_count():
    """R9, executable: '지속일을 혼자 쓰지 마라. 항상 새 관측 건수를 함께
    적는다.' Every shape a Run can take — long-lived, freshly entered,
    zero-new-obs, unknown-mixed, left-censored — must carry '새 관측'
    whenever it carries a duration fragment ('지속 N판정일' or '오늘 새로
    충족')."""
    cases = [
        dict(duration_days=1, new_observation_count=1),
        dict(duration_days=28, new_observation_count=1),
        dict(duration_days=18, new_observation_count=0),
        dict(duration_days=18, new_observation_count=5, unknown_observation_days=2),
        dict(duration_days=505, left_censored=True, entry_date=None),
    ]
    for overrides in cases:
        frag = _fragment_for(**overrides)
        has_duration = ("지속 " in frag) or ("오늘 새로 충족" in frag)
        assert has_duration, f"지속 조각 자체가 없다: {frag!r}"
        assert "새 관측" in frag, f"R9 위반 — 지속만 있고 새 관측이 없다: {frag!r}"


# --- 4차 검수 F3: state_fragment의 검증 공백 ---------------------------------

def test_state_fragment_refuses_a_run_whose_ledger_status_disagrees_with_the_verdict():
    """4차 검수 F3: `thesis.state_fragment`의 `if current.status == "TRUE":`
    가드(`thesis.py`, `state_fragment` 안)가 유일한 방어선이다 — 오늘의 판정
    (`row`)이 어떤 원자를 "발화(TRUE)"로 인용해 '강화' 판정을 냈는데, 파생 뷰
    (`state`, `transitions.thesis_view`)가 그 **같은 원자·같은 as_of 구간**의
    최신 런(`runs[-1]`)을 FALSE로 본다면 두 소스가 불일치하는 것이다. 그 경우
    지속/신규관측 조각을 인쇄하면 "강화 — ... (진입 ... 지속 5판정일 ...)"처럼
    지금 거짓인 조건을 마치 지속되고 있는 것처럼 적어 리포트 자신과 모순되는
    문장을 만든다. 가드는 이런 불일치를 만나면 조각을 아예 생략해야 한다 —
    없는 지속을 지어내는 대신 조용히 접는다.

    이 가드를 지우면(`if current.status == "TRUE": candidates.append(current)`를
    무조건 append로 바꾸면) `min(candidates, ...)`가 이 FALSE 런을 그대로
    골라 `_run_fragment`를 호출하므로 이 테스트가 RED가 된다."""
    row = _minimal_row("강화")  # 발화 원자 id "a", group "strengthen" (verdict=강화)
    disagreeing_run = tr_mod.Run(
        thesis_id="t1", atom_id="a", status="FALSE", entry_date="2026-08-01",
        left_censored=False, last_report_date="2026-08-10", duration_days=5,
        new_observation_count=1, last_new_observation_date="2026-08-01",
        unknown_observation_days=0, has_data_gap=False, restart_reason=None,
        streak_since=None,
    )
    state = {"atoms": {"a": [disagreeing_run]}, "ledger_start": "2026-01-01"}
    frag = thesis_mod.state_fragment(row, state)
    assert frag == "", (
        f"원장 최신 런이 FALSE인데 지속 조각을 냈다 (4차 검수 F3): {frag!r}"
    )


# --- final-review F1/F2/F3: "오늘 새로 충족"과 "미상"의 진짜 조건 -------------

def test_f1_a_forced_restart_never_claims_the_condition_is_newly_met():
    """A month-old TRUE condition that R5.2/R5.3 forcibly restarts (rules or
    engine version changed) must not read as "오늘 새로 충족" — the condition
    itself never changed, only the run's bookkeeping did. duration_days==1
    here purely because the restart opened a new run today."""
    frag = _fragment_for(duration_days=1, restart_reason="의미 변경", new_observation_count=0)
    assert "오늘 새로 충족" not in frag, f"강제 재시작인데 '오늘 새로 충족'이라고 적었다: {frag!r}"
    assert "지속 1판정일" in frag


def test_f2_left_censored_never_claims_the_condition_is_newly_met():
    """R11 좌측 절단(원장 첫 행)도 duration_days==1일 수 있다 — 그 경우도 "오늘
    새로 충족"이 아니다(기록 시작 이전부터의 상태를 오늘 새로 생겼다고 적으면
    같은 문장 안에서 자기모순이다)."""
    frag = _fragment_for(duration_days=1, left_censored=True, entry_date=None,
                         new_observation_count=0)
    assert "오늘 새로 충족" not in frag, f"좌측 절단인데 '오늘 새로 충족'이라고 적었다: {frag!r}"
    assert "지속 1판정일" in frag
    assert "기록 시작" in frag


def test_f1_forced_restart_actually_renders_its_reason_label():
    """F-B (3차 검수 무검증 지적): the test above only asserts that a forced
    restart does NOT say '오늘 새로 충족' — it never checks that the entry
    fragment's restart-reason label (`_RESTART_LABELS`) is the thing that
    actually explains "지속 1판정일" to the reader. Deleting `_RESTART_LABELS`
    entirely left every existing test green (검수관 변이 m15): a month-old TRUE
    condition would then read as a bare '진입 2026-08-20 · 지속 1판정일' —
    indistinguishable from having just started, which is exactly the defect
    F1 was supposed to fix. Covers both restart reasons R5.2/R5.3 define."""
    frag_rules = _fragment_for(duration_days=1, restart_reason=tr_mod.RESTART_RULES_CHANGED,
                               new_observation_count=0)
    assert "기준 변경 후 재시작" in frag_rules, f"기준 변경 재시작 라벨이 안 보인다: {frag_rules!r}"

    frag_engine = _fragment_for(duration_days=1, restart_reason=tr_mod.RESTART_ENGINE_CHANGED,
                                new_observation_count=0)
    assert "의미 변경 후 재시작" in frag_engine, f"의미 변경 재시작 라벨이 안 보인다: {frag_engine!r}"


def test_f1_a_real_state_transition_still_says_newly_met():
    """진짜 상태 전이(재시작도 좌측 절단도 아님)는 여전히 "오늘 새로 충족"이어야
    한다 — F1 수리가 R9의 원래 규칙까지 지워버리면 안 된다."""
    frag = _fragment_for(duration_days=1, restart_reason=None, left_censored=False,
                         new_observation_count=1)
    assert "오늘 새로 충족" in frag


def test_f3_unknown_only_run_says_unknown_not_a_bare_zero():
    """R7 — 알려진 새 관측이 0건이고 일부가 미상이면 "0건 이상(...)"으로 아무
    말도 안 하는 문장 대신 "미상"이라고 말해야 한다."""
    frag = _fragment_for(duration_days=14, new_observation_count=0, unknown_observation_days=14)
    assert "미상" in frag
    assert "0건 이상" not in frag


# --- legend / introduced_on -------------------------------------------------

def _minimal_row(verdict="강화"):
    ev = [{"atom": {"id": "a"}, "status": "TRUE", "detail": {"message": "m"}}]
    return {"thesis_id": "t1", "theme": "fin_credit", "slot": 1, "verdict": verdict,
            "evals_by_group": {"strengthen": ev}, "all_evals": ev,
            "evaluable_atoms": 1, "total_atoms": 1}


def test_legend_carries_a_real_introduced_on_date_and_the_r9_sentence():
    out = thesis_mod.render_impact([_minimal_row()], "2026-08-12", introduced_on="2026-08-19")
    assert "2026-08-19" in out
    assert "지속일은 독립 증거의 수가 아니다" in out  # 요구사항 7, R9의 문장 형태


def test_legend_is_omitted_without_an_introduced_on():
    """빈 문자열이면 범례의 도입일 조각만 뺀다 — 도입일을 모르는데 도입됐다고
    적는 것은 없는 사실을 지어내는 것이다. R8/R9 경고문(요구사항 7)은 도입일과
    무관하므로 이 경우에도 남는다(4차 검수 F-A 잔여분 수리, CEO 결정 (가) —
    이전에는 도입일 조각이 빠지면 R8/R9 경고문까지 함께 사라졌다)."""
    out = thesis_mod.render_impact([_minimal_row()], "2026-08-12")
    assert "도입" not in out
    assert "지속일은 독립 증거의 수가 아니다" in out, (
        f"도입일이 없다고 R8/R9 경고문까지 함께 사라졌다: {out!r}"
    )


def test_render_impact_one_arg_call_still_works():
    """옛 호출부(리포트 날짜도, 상태도, 도입일도 없는 곳)가 깨지지 않아야
    한다 — ST4 성공 기준."""
    out = thesis_mod.render_impact([_minimal_row()])
    assert "강화" in out


def test_no_state_omits_the_fragment_but_keeps_the_evidence_date():
    row = _minimal_row()
    row["evals_by_group"]["strengthen"][0]["detail"]["latest_at"] = "2026-08-01T00:00:00+00:00"
    out = thesis_mod.render_impact([row], "2026-08-12")  # states=None
    assert "근거 2026-08-01" in out
    # `state`가 None이면 per-atom 진입/지속/새 관측 조각(`_run_fragment`, 항상
    # "지속 {n}판정일" 형태)이 없어야 한다 — 문자열 "지속"만 보면 4차 검수
    # F-A 잔여분 수리 이후 항상 붙는 R8/R9 범례("지속일은 독립 증거의 수가
    # 아니다")의 "지속일"과 오검출로 충돌하므로 트레일링 공백까지 맞춘다.
    assert "지속 " not in out and "새 관측" not in out
