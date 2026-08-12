"""가설 조건이 **실제로 발화할 수 있는가**를 검사하는 장치 (CEO 지시 2026-08-12).

왜 이 파일이 있는가: 2026-08-12에 반증 조건 하나(`hynix_foreign_5d_sell`)가
사실상 발화 불가능한 상태로 발견됐는데, **그것을 찾은 것은 검사 장치가 아니라
우연이었다.** 우연이 QA를 대신하는 한 나머지 31개 조건이 살아 있는지는 아무도
모른다. 사외 고문 2인이 독립적으로 이 검사를 첫 순위로 꼽았다.

검사가 두 갈래인 이유가 이 파일의 요점이다:
  - 도달 가능성만 보면 그 버그를 **못 잡는다.** 단조 감소도 논리적으로는
    가능하기 때문이다(매일 더 많이 파는 일이 현실에서 없을 뿐).
  - 그래서 실이력 소급이 본체이고, 도달 가능성은 연산자 오타처럼 어떤
    데이터로도 참이 될 수 없는 조건을 잡는 값싼 그물이다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.interp import thesis as thesis_mod
from market_intel.models import FactCandidate


def _cutoff() -> datetime:
    return datetime(2026, 8, 12, tzinfo=timezone.utc)


def _flow_fc(day: str, value: float) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"X:{day}", subject="000660.KS", category="flow",
        metric="net_buy_foreign_value", event_at=f"{day}T06:30:00+00:00",
        market="KR", country="KR", value_num=value, unit="KRW",
        data_status="source_verified")


def _thesis(atom: dict, group: str = "weaken") -> dict:
    return {"thesis_id": "t1", "theme": "ai_semi", "slot": 1,
            "conditions": {group: [atom]}}


_FLOW_ATOM = {"id": "a", "category": "flow", "subject": "000660.KS",
              "metric": "net_buy_foreign_value", "direction": "down", "periods": 4}


def _seed_steady_selling(conn, raw_dir):
    """4거래일 내리 순매도 — 다만 **가속하지는 않는다**(실측 8/6~8/11 모양)."""
    from conftest import seed_fact

    for day, value in [("2026-08-06", -1_677_100_000_000.0),
                       ("2026-08-07", -66_700_000_000.0),
                       ("2026-08-10", -408_800_000_000.0),
                       ("2026-08-11", -299_800_000_000.0)]:
        seed_fact(conn, raw_dir, "kis", _flow_fc(day, value), "2026-08-11T22:00:00+00:00")


def test_audit_catches_the_condition_that_was_actually_dead(settings):
    """⚠️ 이 파일의 존재 이유. 옛 모양(`consecutive`)을 실제 데이터에 대고
    돌리면 "한 번도 발화한 적 없음"이 나와야 한다 — 4일 내리 순매도가 있었는데도."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_steady_selling(conn, settings.raw_dir)

    dead = dict(_FLOW_ATOM, kind="consecutive")
    rows = thesis_mod.audit_conditions(conn, [_thesis(dead)], _cutoff())
    assert rows[0]["verdict"] == "never_fired", rows[0]
    assert rows[0]["observations"] == 4, "관측은 있었다 — 조건이 못 잡은 것이다"


def test_the_repaired_condition_passes_the_same_audit(settings):
    """수리한 종류(`consecutive_sign`)는 같은 데이터에서 발화 이력이 잡힌다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_steady_selling(conn, settings.raw_dir)

    fixed = dict(_FLOW_ATOM, kind="consecutive_sign")
    rows = thesis_mod.audit_conditions(conn, [_thesis(fixed)], _cutoff())
    assert rows[0]["verdict"] == "ok"
    assert rows[0]["fired_days"] >= 1
    assert rows[0]["last_fired"] == "2026-08-11"


def test_reachability_alone_would_have_missed_it():
    """도달 가능성만으로는 그 버그를 못 잡는다는 것을 증거로 남긴다.

    이 단언이 깨진다면 누군가 검사를 도달 가능성 하나로 줄여도 된다고 오해할
    수 있다 — 실이력 소급이 왜 본체인지가 여기 적혀 있다.
    """
    dead = dict(_FLOW_ATOM, kind="consecutive")
    assert thesis_mod._reachable(dead) is True, "단조 감소는 논리적으로는 가능하다"


def test_unreachable_condition_is_flagged(settings):
    """어떤 데이터로도 참이 될 수 없는 조건(방향이 뒤집힌 연산자 등)은 버그다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)

    broken = {"id": "b", "kind": "threshold", "category": "flow",
              "subject": "000660.KS", "metric": "net_buy_foreign_value",
              "op": "잘못된연산자", "value": 1.0}
    rows = thesis_mod.audit_conditions(conn, [_thesis(broken)], _cutoff())
    assert rows[0]["verdict"] == "unreachable"


def test_boundary_conditions_are_reachable_despite_float_error():
    """⚠️ 이 감사 도구가 처음 낸 답은 **오탐 2건**이었다.

    `>=` 경계를 정확히 겨냥해 합성값을 만들었더니 `100 * 1.15 =
    114.99999999999999`이라 변화율이 14.999...%로 나와 멀쩡한 조건 둘이
    "발화 불가"로 신고됐다. 도구가 없는 결함을 만들어 내면 있는 결함보다
    나쁘다 — 아무도 그 도구를 안 믿게 된다.
    """
    for value, op in ((15.0, ">="), (-30.0, "<="), (0.0, ">=")):
        atom = {"id": "c", "kind": "change_pct", "category": "price",
                "subject": "X", "metric": "price_close",
                "op": op, "value": value, "lookback": 60}
        assert thesis_mod._reachable(atom), f"{op} {value} 가 발화 불가로 잘못 나온다"


@pytest.mark.parametrize("kind,extra", [
    ("threshold", {"op": ">=", "value": 4.5}),
    ("change_pct", {"op": "<=", "value": -30.0, "lookback": 60}),
    ("consecutive", {"direction": "up", "periods": 2}),
    ("consecutive_sign", {"direction": "down", "periods": 3}),
    ("stale", {"days": 30}),
])
def test_every_supported_kind_is_reachable(kind, extra):
    """지원하는 모든 조건 종류가 합성 입력으로 참이 될 수 있어야 한다.
    새 종류를 더하면서 `_synthetic_obs`를 안 고치면 그 종류로 쓴 조건이
    전부 '발화 불가'로 잘못 신고된다 — 여기서 먼저 걸린다."""
    atom = {"id": "d", "kind": kind, "category": "macro",
            "subject": "X", "metric": "value", **extra}
    assert thesis_mod._reachable(atom), f"{kind} 를 참으로 만드는 합성 관측이 없다"


def test_closest_approach_separates_live_sentinels_from_decoration(settings):
    """"발화한 적 없음"만으로는 판단이 안 된다 — 반증 조건은 가설이 맞으면
    안 울리는 게 정상이다. 문턱까지의 거리가 그것을 갈라 준다."""
    from conftest import seed_fact

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for day, value in [("2026-08-10", 4.4), ("2026-08-11", 4.45)]:
        seed_fact(conn, settings.raw_dir, "fred",
                  FactCandidate(raw_ref=f"D:{day}", subject="DGS10", category="macro",
                                metric="value", event_at=f"{day}T00:00:00+00:00",
                                market="US", country="US", value_num=value,
                                unit="percent", data_status="source_verified"),
                  "2026-08-11T22:00:00+00:00")

    near = {"id": "n", "kind": "threshold", "category": "macro", "subject": "DGS10",
            "metric": "value", "op": ">=", "value": 5.0}
    far = dict(near, id="f", value=100.0)

    rows = {r["atom_id"]: r for r in thesis_mod.audit_conditions(
        conn, [_thesis(near, "falsify"), {"thesis_id": "t2", "theme": "ai_semi", "slot": 2,
                                          "conditions": {"falsify": [far]}}], _cutoff())}
    assert rows["n"]["verdict"] == "never_fired"
    assert rows["n"]["closest"] == pytest.approx(0.89, abs=0.02), "5.0 문턱에 4.45까지 갔다"
    assert rows["f"]["closest"] < 0.05, "100 문턱은 근처에도 못 갔다 — 사실상 장식"


def test_fired_conditions_get_no_closest(settings):
    """이미 발화한 적이 있으면 거리는 뜻이 없다 — 도달했으니까."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_steady_selling(conn, settings.raw_dir)

    rows = thesis_mod.audit_conditions(
        conn, [_thesis(dict(_FLOW_ATOM, kind="consecutive_sign"))], _cutoff())
    assert rows[0]["verdict"] == "ok"
    assert rows[0]["closest"] is None
