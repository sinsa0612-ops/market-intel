"""상대강도 조건 — "얼마나 빠졌나"가 아니라 "시장을 못 이겼나".

왜 이 종류가 생겼나 (2026-08-12 조건 감사): `ai_semi_1`("AI·반도체가 2030년까지
주도한다")의 반증·약화 조건이 둘 다 SOX의 **절대 낙폭**(-30% / -15%, 250거래일)
이었는데, 2년 이력 감사에서 **문턱 근접도 0%** — 근처에도 못 갔다. 즉 그 가설은
현재 데이터로 반증될 방법이 사실상 없었다.

주도력은 절대 낙폭이 아니라 **시장 대비**로 먼저 무너진다. 반도체가 시장과 같이
오르내리기만 해도 "주도한다"는 주장은 이미 약해진 것이다. 그래서 기존 문턱은
그대로 두고(골대를 옮기지 않는다) **다른 축**의 조건을 더했다 — 사외 고문 2인이
독립적으로 권한 방향이다.

효과는 실측으로 확인됐다: 같은 2년에서 절대 조건은 0회, 상대 조건(-10%p)은
**21일 발화**(마지막 2025-04-22). 죽은 자리에 살아 있는 감시자가 들어섰다.

⚠️ 문턱은 **오늘 값을 보기 전에** 문장의 뜻에서 정했다(한 분기 기준 -10%p 약화 /
-25%p 주도력 상실). 등록 후 재보니 오늘은 -2.82%p로 둘 다 미충족이다 — 결과를
보고 맞춘 것이 아니라는 증거다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.interp import thesis as thesis_mod
from market_intel.models import FactCandidate


def _cutoff() -> datetime:
    return datetime(2026, 8, 12, tzinfo=timezone.utc)


def _atom(**over) -> dict:
    base = {"id": "rs", "kind": "relative_change_pct", "category": "price",
            "subject": "^SOX", "benchmark": "^GSPC", "metric": "price_close",
            "op": "<=", "value": -10.0, "lookback": 2}
    base.update(over)
    return base


def _obs(*values) -> list[tuple[str, float]]:
    """최신순 관측열."""
    return [(f"2026-08-{10 - i:02d}T20:00:00+00:00", v) for i, v in enumerate(values)]


def test_lagging_the_benchmark_fires_even_when_both_rise():
    """⚠️ 이 종류의 존재 이유. **둘 다 올라도** 못 따라가면 주도력은 잃은 것이다 —
    절대 등락 조건은 이 경우를 영원히 못 잡는다."""
    subject = _obs(102.0, 101.0, 100.0)   # +2%
    bench = _obs(115.0, 107.0, 100.0)     # +15% -> 격차 -13%p
    status, detail = thesis_mod._evaluate_atom(_atom(), subject, _cutoff(), bench)
    assert status == "TRUE"
    assert detail["spread_pp"] == pytest.approx(-13.0, abs=0.01)


def test_absolute_condition_misses_the_same_case():
    """같은 데이터를 절대 낙폭 조건으로 재면 거짓이다 — 실제로 2년간 그랬다."""
    subject = _obs(102.0, 101.0, 100.0)
    absolute = {"id": "abs", "kind": "change_pct", "category": "price",
                "subject": "^SOX", "metric": "price_close",
                "op": "<=", "value": -10.0, "lookback": 2}
    status, _ = thesis_mod._evaluate_atom(absolute, subject, _cutoff())
    assert status == "FALSE"


def test_outperforming_does_not_fire():
    subject = _obs(120.0, 110.0, 100.0)   # +20%
    bench = _obs(105.0, 102.0, 100.0)     # +5% -> 격차 +15%p
    status, _ = thesis_mod._evaluate_atom(_atom(), subject, _cutoff(), bench)
    assert status == "FALSE"


def test_both_falling_together_does_not_fire():
    """시장이 통째로 빠진 날은 주도력 상실이 아니다 — 그건 시장 위험이다.
    이 구분이 없으면 급락장마다 모든 가설이 동시에 약화로 뒤집힌다."""
    subject = _obs(80.0, 90.0, 100.0)     # -20%
    bench = _obs(78.0, 88.0, 100.0)       # -22% -> 격차 +2%p
    status, detail = thesis_mod._evaluate_atom(_atom(), subject, _cutoff(), bench)
    assert status == "FALSE"
    assert detail["spread_pp"] > 0


def test_missing_benchmark_history_is_unknown_not_false():
    """기준 이력이 모자라면 "아니다"가 아니라 "모른다"다 — 모르는 것을 거짓으로
    처리하면 조건이 조용히 잠든다."""
    subject = _obs(102.0, 101.0, 100.0)
    status, _ = thesis_mod._evaluate_atom(_atom(), subject, _cutoff(), _obs(100.0))
    assert status == "UNKNOWN"

    status2, _ = thesis_mod._evaluate_atom(_atom(), subject, _cutoff(), None)
    assert status2 == "UNKNOWN"


def test_benchmark_is_required_at_load():
    """`benchmark`가 없으면 "무엇 대비"가 없다. 조용히 절대 등락으로 떨어지면
    문턱의 뜻이 통째로 달라진다(-10%p 초과미달 vs -10% 절대 하락)."""
    reasons: list[str] = []
    atom = _atom()
    del atom["benchmark"]
    thesis_mod._validate_atom(atom, "where", reasons)
    assert reasons and "benchmark" in reasons[0]


def test_audit_can_reach_the_new_kind():
    """새 종류를 더하면서 감사 도구의 합성 입력을 안 고치면, 그 종류로 쓴 조건이
    전부 '발화 불가'로 **잘못** 신고된다."""
    assert thesis_mod._reachable(_atom())
    assert thesis_mod._reachable(_atom(op=">=", value=10.0))


def test_audit_replays_relative_conditions_with_the_benchmark(settings):
    """감사의 소급이 기준 계열을 함께 되짚는지. 안 그러면 상대 조건은 언제나
    '발화 이력 없음'으로 나와 새로 넣은 감시자가 죽은 것처럼 보인다."""
    from conftest import seed_fact

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)

    def price(symbol, day, value):
        return FactCandidate(
            raw_ref=f"{symbol}:{day}", subject=symbol, category="price",
            metric="price_close", event_at=f"{day}T20:00:00+00:00", market="US",
            country="US", value_num=value, unit="USD", data_status="source_verified")

    for day, sox, spx in [("2026-08-06", 100.0, 100.0),
                          ("2026-08-07", 100.0, 108.0),
                          ("2026-08-10", 100.0, 115.0)]:
        seed_fact(conn, settings.raw_dir, "yfinance", price("^SOX", day, sox),
                  "2026-08-11T00:00:00+00:00")
        seed_fact(conn, settings.raw_dir, "yfinance", price("^GSPC", day, spx),
                  "2026-08-11T00:00:00+00:00")

    thesis = {"thesis_id": "t1", "theme": "ai_semi", "slot": 1,
              "conditions": {"weaken": [_atom()]}}
    rows = thesis_mod.audit_conditions(conn, [thesis], _cutoff())
    assert rows[0]["verdict"] == "ok", rows[0]
    assert rows[0]["last_fired"] == "2026-08-10"


def test_closest_approach_is_withheld_for_relative_conditions():
    """⚠️ 감사 도구가 **거짓 안심**을 주면 안 된다.

    상대 조건의 근접도를 대상 계열의 절대 변화율로 대신 재면 문턱보다 후하게
    나와서("시장이 올랐는데 나만 제자리"인 경우를 놓친다) 죽은 조건이 살아 있는
    것처럼 보인다. 정확히 못 잴 바에는 내지 않는다.
    """
    obs = _obs(102.0, 101.0, 100.0)
    assert thesis_mod._closest_approach(_atom(), obs) is None
