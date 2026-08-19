""""오늘 새로 넘었나, 505일째인가" — 판정 옆에 신규성을 밝힌다.

왜 이 파일이 있는가 (CEO 지시 2026-08-12): 수준 조건은 상태가 유지되는 한 매일
참이라 매일 "강화"를 찍는다. 실측 —

    DGS10 >= 4.5      25회째 지속(2026-07-07부터)
    환율 >= 1400      212회째 지속(2025-09-25부터)
    DFII10 >= 1.05    505+회째 지속(2024-08-01부터, 데이터 전 구간)

CEO는 매일 "강화 6건"을 읽지만 그중 **새로 알게 된 것은 0건**이다. 확증편향이
심리가 아니라 조건 설계로 제조되는 자리다.

⚠️ **판정은 바꾸지 않는다.** "지금 긴축적이다"는 참이고 그 정보를 버리면 안 되며,
바꾸면 과거 판정과의 비교도 끊긴다. 대신 그 옆에 지속 횟수를 적어 독자가 news와
state를 구별하게 한다. (사외 고문의 처방이 여기서 갈렸다 — GPT는 판정 강등,
fable은 메타데이터. 정보를 버리지 않는 쪽을 골랐다.)

**2026-08-19에 표시가 전이 뷰로 교체됨** (spec §5, `theses/transition_rules_v1.md`) —
"N회째 지속"은 "진입일 · 지속 · 새 관측"으로 바뀌었다. 이 파일이 지키는 계약은
**"지속을 신규성으로 읽히게 하지 않는다"**이지 특정 문구가 아니다: 아래 네 테스트
(`test_persisting_state_says_how_long` / `test_new_crossing_says_so_plainly` /
`test_capped_streak_is_marked` / `test_the_most_recent_crossing_wins_when_several_fire`)는
그 새 표기로 같은 의도를 지키게 다시 썼다 — `thesis_mod._streak`가 만드는 옛
`detail.streak`가 아니라 `transitions.Run`(파생 뷰)을 직접 구성해 `render_impact`의
`states=` 인자로 넘긴다. 아래 두 테스트(판정 불변·성능 계약)는 신규성 층의 축이
아니라 판정 엔진 자체의 계약이라 그대로 둔다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.interp import thesis as thesis_mod
from market_intel.interp import transitions as tr_mod
from market_intel.models import FactCandidate


def _cutoff() -> datetime:
    return datetime(2026, 8, 12, tzinfo=timezone.utc)


_LEVEL_ATOM = {"id": "a", "kind": "threshold", "category": "macro",
               "subject": "DGS10", "metric": "value", "op": ">=", "value": 4.5}


def _obs(*values) -> list[tuple[str, float]]:
    """최신순 관측열. 날짜는 하루씩 거슬러 올라간다."""
    return [(f"2026-08-{11 - i:02d}T00:00:00+00:00", v) for i, v in enumerate(values)]


def test_streak_counts_consecutive_true_observations():
    # 최신 3개가 문턱 위, 그 앞은 아래 -> 3회째
    obs = _obs(4.7, 4.6, 4.55, 4.4, 4.3)
    s = thesis_mod._streak(_LEVEL_ATOM, obs, _cutoff())
    assert s["periods"] == 3
    assert s["since"] == "2026-08-09"


def test_new_today_is_a_streak_of_one():
    obs = _obs(4.7, 4.4, 4.3)
    assert thesis_mod._streak(_LEVEL_ATOM, obs, _cutoff())["periods"] == 1


def test_false_condition_has_no_streak():
    assert thesis_mod._streak(_LEVEL_ATOM, _obs(4.1, 4.0), _cutoff()) == {}


def _row(verdict: str, group: str, atom_id: str = "a", latest_at: str = "2026-08-10T00:00:00+00:00"):
    detail = {"message": "DGS10 최신값 4.72", "latest_at": latest_at}
    ev = [{"atom": {"id": atom_id}, "status": "TRUE", "detail": detail}]
    return {"thesis_id": "t1", "theme": "fin_credit", "slot": 1, "verdict": verdict,
            "evals_by_group": {group: ev}, "all_evals": ev,
            "evaluable_atoms": 1, "total_atoms": 1}


def _run(**overrides) -> tr_mod.Run:
    """`transitions.Run`(파생 뷰) 값을 손으로 구성한다 — 이 파일은 `_streak`의
    옛 `detail.streak`가 아니라 이 값을 `render_impact`의 `states=`로 넘긴다."""
    fields = dict(
        thesis_id="t1", atom_id="a", status="TRUE", entry_date="2026-08-01",
        left_censored=False, last_report_date="2026-08-12", duration_days=1,
        new_observation_count=1, last_new_observation_date="2026-08-01",
        unknown_observation_days=0, has_data_gap=False, restart_reason=None,
        streak_since=None,
    )
    fields.update(overrides)
    return tr_mod.Run(**fields)


def _states(ledger_start: str, **atom_runs: tr_mod.Run) -> dict:
    return {"t1": {"ledger_start": ledger_start,
                   "atoms": {atom_id: [run] for atom_id, run in atom_runs.items()},
                   "verdict": []}}


def test_persisting_state_says_how_long():
    """옛 "212회째 지속, 2025-09-25부터"가 지키던 의도 — **지속 기간과 새 관측
    건수가 함께 보인다** — 를 새 표기(R6/R9)로 지킨다."""
    run = _run(entry_date="2025-09-25", duration_days=212, new_observation_count=1,
               last_new_observation_date="2026-08-01")
    out = thesis_mod.render_impact([_row("강화", "strengthen")], "2026-08-12",
                                   states=_states("2025-09-25", a=run))
    assert "진입 2025-09-25" in out and "지속 212판정일" in out
    assert "회째" not in out, "옛 문구가 남아 있다"
    assert "새 관측" in out, "R9: 지속일 옆에 항상 새 관측 건수가 있어야 한다"


def test_new_crossing_says_so_plainly():
    run = _run(entry_date="2026-08-12", duration_days=1, new_observation_count=1,
               last_new_observation_date="2026-08-12")
    out = thesis_mod.render_impact([_row("강화", "strengthen")], "2026-08-12",
                                   states=_states("2025-09-25", a=run))
    assert "오늘 새로 충족" in out
    assert "회째" not in out and "판정일" not in out


def test_capped_streak_is_marked():
    """옛 "505+회째"(되짚기 상한, 정밀도 없음)가 지키던 의도 — **정확한 진입일을
    모르면 정밀한 척하지 않는다** — 는 이제 R11 좌측 절단으로 표현된다: 원장
    시작일 이전부터라고만 말하고, 가짜 진입일이나 가짜 지속 횟수를 만들지 않는다."""
    run = _run(entry_date=None, left_censored=True, duration_days=505,
               new_observation_count=1, last_new_observation_date="2026-08-01")
    out = thesis_mod.render_impact([_row("강화", "strengthen")], "2026-08-12",
                                   states=_states("2024-08-01", a=run))
    assert "기록 시작 2024-08-01 이전부터" in out
    assert "505+회째" not in out and "회째" not in out
    assert "진입 " not in out, "좌측 절단인데 없는 진입일을 지어냈다"


def test_the_most_recent_crossing_wins_when_several_fire():
    """여럿이 발화하면 **가장 최근에 넘은 것**을 말한다 — 판정을 바꾼 사건이
    그것이고, 나머지는 이미 참이던 상태다. (새 엔진에서는 "가장 최근에 넘은
    것" = 지속 판정일이 가장 짧은 구간.)"""
    ev = [
        {"atom": {"id": "a"}, "status": "TRUE",
         "detail": {"message": "old", "latest_at": "2026-08-10T00:00:00+00:00"}},
        {"atom": {"id": "b"}, "status": "TRUE",
         "detail": {"message": "new", "latest_at": "2026-08-10T00:00:00+00:00"}},
    ]
    row = {"thesis_id": "t1", "theme": "fin_credit", "slot": 1, "verdict": "강화",
           "evals_by_group": {"strengthen": ev}, "all_evals": ev,
           "evaluable_atoms": 2, "total_atoms": 2}
    old_run = _run(atom_id="a", entry_date="2024-10-01", duration_days=400,
                   new_observation_count=1, last_new_observation_date="2024-10-01")
    new_run = _run(atom_id="b", entry_date="2026-08-08", duration_days=3,
                   new_observation_count=1, last_new_observation_date="2026-08-08")
    states = {"t1": {"ledger_start": "2020-01-01",
                     "atoms": {"a": [old_run], "b": [new_run]}, "verdict": []}}
    out = thesis_mod.render_impact([row], "2026-08-12", states=states)
    assert "지속 3판정일" in out and "400" not in out


def test_verdict_is_untouched_by_the_novelty_layer(settings):
    """⚠️ 가장 중요한 계약. 신규성은 **표시**일 뿐 판정을 바꾸지 않는다 —
    바꾸면 "지금 긴축적이다"라는 참인 정보가 사라지고 과거 판정과 비교도 끊긴다."""
    from conftest import seed_fact

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for day, v in [("2026-08-05", 4.7), ("2026-08-06", 4.8), ("2026-08-07", 4.75)]:
        seed_fact(conn, settings.raw_dir, "fred",
                  FactCandidate(raw_ref=f"D:{day}", subject="DGS10", category="macro",
                                metric="value", event_at=f"{day}T00:00:00+00:00",
                                market="US", country="US", value_num=v, unit="percent",
                                data_status="source_verified"),
                  "2026-08-11T00:00:00+00:00")

    thesis = {"thesis_id": "t1", "theme": "fin_credit", "slot": 1,
              "statement": "s", "leading_indicators": ["DGS10 value"],
              "next_check_date": "2026-11-01",
              "conditions": {"falsify": [dict(_LEVEL_ATOM, id="f", op="<=", value=1.0)],
                             "weaken": [], "strengthen": [dict(_LEVEL_ATOM)]}}
    rows = thesis_mod.review(conn, [thesis], _cutoff(), "morning", "2026-08-12")
    assert rows[0]["verdict"] == "강화", "신규성 층이 판정을 건드리면 안 된다"

    fired = rows[0]["evals_by_group"]["strengthen"][0]
    assert fired["detail"]["streak"]["periods"] == 3


def test_streak_is_not_computed_for_false_atoms(settings, monkeypatch):
    """거짓 조건의 "지속"은 화면에 안 나가므로 되짚을 이유가 없다 — 조건 39개를
    전부 되짚으면 리포트 생성이 느려진다.

    ⚠️ **결과가 비었는지가 아니라 호출됐는지를 본다.** 거짓 조건의 `_streak`은
    어차피 빈 dict를 내므로, 결과만 확인하면 거짓에도 되짚는 변이를 그대로
    통과시킨다(실제로 첫 판이 그랬다 — 변이 주입에서 초록이었다). 계약은
    "결과를 안 담는다"가 아니라 "일을 안 한다"이다.
    """
    from conftest import seed_fact

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for day, v in [("2026-08-05", 3.0), ("2026-08-06", 3.1)]:
        seed_fact(conn, settings.raw_dir, "fred",
                  FactCandidate(raw_ref=f"D:{day}", subject="DGS10", category="macro",
                                metric="value", event_at=f"{day}T00:00:00+00:00",
                                market="US", country="US", value_num=v, unit="percent",
                                data_status="source_verified"),
                  "2026-08-11T00:00:00+00:00")

    calls: list[dict] = []
    real = thesis_mod._streak
    monkeypatch.setattr(thesis_mod, "_streak",
                        lambda atom, *a, **k: (calls.append(atom), real(atom, *a, **k))[1])

    evals = thesis_mod._eval_group(conn, [dict(_LEVEL_ATOM)], _cutoff())
    assert evals[0]["status"] == "FALSE"
    assert not calls, "거짓 조건인데 되짚었다 — 조건이 늘수록 리포트가 느려진다"
    assert "streak" not in evals[0]["detail"]
