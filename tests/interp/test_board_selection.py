"""가설 상태판이 **여러 조건 중 무엇을 가리키는가** (CEO 지적 2026-08-27 후속).

왜 별도 파일인가: 파이프라인 시험은 발화 조건이 하나뿐이라 "여럿 중 고른다"는
규칙 자체에 닿지 못한다. 실제로 변이 검사에서 두 건이 그렇게 살아남았다 —
`min`을 `max`로 바꿔도, 근거를 가장 오래된 것 대신 최신으로 바꿔도 시험이 전부
초록이었다. **조건 두 개가 동시에 발화한 판**이라야 그 가드에 닿는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from market_intel.interp import thesis as th


@dataclass
class _Run:
    """`transitions.Run`의 덕타이핑 대역 — `_run_fragment`가 그 타입을 import하지
    않고 필드만 본다고 자기 주석에 못박고 있으므로, 여기서도 필드만 맞춘다."""
    atom_id: str
    status: str = "TRUE"
    duration_days: int = 1
    left_censored: bool = False
    entry_date: str | None = "2026-08-01"
    restart_reason: str | None = None
    new_observation_count: int = 1
    last_new_observation_date: str | None = "2026-08-27"
    unknown_observation_days: int = 0


def _row(fired: list[tuple[str, str]], verdict: str = "강화") -> dict:
    """`fired`: (원자 id, 근거 날짜). 둘 다 참으로 발화한 판을 만든다."""
    group = th._VERDICT_GROUP[verdict]
    return {
        "thesis_id": "t1", "theme": "ai_semi", "slot": 1, "verdict": verdict,
        "evals_by_group": {group: [
            {"atom": {"id": aid}, "status": "TRUE", "detail": {"latest_at": f"{day}T00:00:00+00:00"}}
            for aid, day in fired]},
    }


def _state(runs: list[_Run]) -> dict:
    return {"atoms": {r.atom_id: [r] for r in runs}, "ledger_start": "2026-07-01"}


def test_the_most_recent_crossing_wins_not_the_oldest():
    """**판정을 바꾼 사건**이 가장 최근에 넘은 조건이다. 나머지는 이미 참이던
    상태라 오늘의 소식이 아니다. 가장 오래된 것을 고르면 표가 "20일째"라고
    말하는데 실제로 오늘 바뀐 것은 3일짜리 조건이다."""
    row = _row([("a_old", "2026-08-01"), ("a_new", "2026-08-26")])
    state = _state([_Run("a_old", duration_days=20), _Run("a_new", duration_days=3)])

    run = th._current_run(row, state)
    assert run.atom_id == "a_new" and run.duration_days == 3

    board = th.board_rows([row], "2026-08-27", states={"t1": state})
    assert board[0]["duration_days"] == 3, "표가 산문과 다른 구간을 가리킨다"


def test_the_oldest_basis_wins_not_the_newest():
    """조건이 여럿이면 **그 전부가 참이어야** 판정이 선다. 가장 묵은 근거가 이
    판정의 나이다 — 최신을 쓰면 두 달 된 근거가 어제 근거 뒤에 숨고, 상태판이
    "강화 · 근거 최근"이라고 말하게 된다."""
    row = _row([("a_old", "2026-06-30"), ("a_new", "2026-08-26")])
    assert th._basis_date(row) == "2026-06-30"

    board = th.board_rows([row], "2026-08-27", states={"t1": _state([_Run("a_old"), _Run("a_new")])})
    assert board[0]["basis_date"] == "2026-06-30"
    assert board[0]["basis_age_days"] == 58, "묵은 근거가 숨었다"


def test_a_condition_that_is_no_longer_true_is_not_the_reason():
    """지금 거짓인 조건이 판정을 만들었다고 말하면 안 된다."""
    row = _row([("a_true", "2026-08-26")])
    state = _state([_Run("a_true", duration_days=3), _Run("a_false", status="FALSE", duration_days=1)])
    assert th._current_run(row, state).atom_id == "a_true"


def test_hold_has_no_run_and_no_basis():
    """유지·판정 불가는 판정을 만든 조건 자체가 없다 — 아무 참인 원자나 주워
    날짜를 붙이면 "유지 · 근거 8/11"처럼 자기 자신에 대해 거짓말하는 표가 된다
    (`_evidence_note` 주석의 실제 사고와 같은 함정)."""
    row = {"thesis_id": "t1", "theme": "ai_semi", "slot": 2, "verdict": "유지",
           "evals_by_group": {"strengthen": [
               {"atom": {"id": "a1"}, "status": "TRUE",
                "detail": {"latest_at": "2026-08-11T00:00:00+00:00"}}]}}
    assert th._current_run(row, _state([_Run("a1")])) is None
    assert th._basis_date(row) == ""

    board = th.board_rows([row], "2026-08-27", states={"t1": _state([_Run("a1")])})
    assert board[0]["duration_days"] is None and board[0]["basis_date"] == ""


def test_a_left_censored_run_keeps_its_days_and_is_marked():
    row = _row([("a1", "2026-08-26")])
    state = _state([_Run("a1", duration_days=20, left_censored=True, entry_date=None)])
    board = th.board_rows([row], "2026-08-27", states={"t1": state})
    assert board[0]["duration_days"] == 20
    assert board[0]["duration_at_least"] is True


def test_without_a_derived_view_the_board_says_nothing_about_duration():
    """없는 정보를 지어내지 않는다 — `state`가 없으면 일수 칸은 비운다."""
    board = th.board_rows([_row([("a1", "2026-08-26")])], "2026-08-27", states=None)
    assert board[0]["duration_days"] is None
    assert board[0]["basis_date"] == "2026-08-26", "근거는 파생 뷰 없이도 안다"
