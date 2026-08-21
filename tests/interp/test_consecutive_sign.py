"""부호 있는 흐름(순매수/순매도)의 "N구간 연속"은 단조 변화가 아니다.

왜 이 파일이 있는가 (2026-08-12, 실제로 죽어 있던 반증 조건):
`ai_semi_2`의 약화 조건 `hynix_foreign_5d_sell`("하이닉스 외국인 5일 연속
순매도")이 `consecutive/down`으로 적혀 있었는데, 그 종류는 **매 구간 값이
직전보다 작아질 것**(단조 감소)을 요구한다. 분기 재무("MSFT 영업이익 2분기
연속 증가")에는 그것이 옳은 뜻이지만, 순매수 금액처럼 부호가 있는 값에는
"연속 순매도" = **부호가 유지되는 것**이지 "매일 더 많이 파는 것"이 아니다.

실측(2026-08-11 차단선, SK하이닉스 외국인 순매수액):
    8/6 -1.68조 · 8/7 -667억 · 8/10 -4,088억 · 8/11 -2,998억
4거래일 내리 순매도(합계 -2.45조)인데, 8/11(-2,998억)이 8/10(-4,088억)보다
**덜** 팔았다는 이유로 연속이 1에서 끊겼다. 매일 1조씩 한 달을 팔아도 발화하지
않는 조건이었다 — 그 사이 외국인 이탈이라는 실제 사건이 지나갔다.

`consecutive`의 의미는 일부러 그대로 둔다(재무 조건들의 지문과 과거 판정이
흔들리지 않게). 이 파일은 새 종류 `consecutive_sign`의 계약을 못박는다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.interp import thesis as thesis_mod
from market_intel.models import FactCandidate


def _cutoff(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _flow_fc(event_at: str, value: float) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"000660.KS:{event_at}", subject="000660.KS", category="flow",
        metric="net_buy_foreign_value", event_at=event_at, market="KR", country="KR",
        value_num=value, unit="KRW", data_status="source_verified",
    )


def _atom(kind: str, periods: int, direction: str = "down") -> dict:
    return {"id": "a", "kind": kind, "category": "flow", "subject": "000660.KS",
            "metric": "net_buy_foreign_value", "direction": direction, "periods": periods}


def _seed(conn, raw_dir, series):
    from tests.interp.conftest import seed_fact

    for day, value in series:
        seed_fact(conn, raw_dir, "kis", _flow_fc(f"2026-08-{day}T06:30:00+00:00", value),
                  "2026-08-11T22:00:00+00:00")


# 실제로 발행 사고를 만든 그 숫자들(조 단위를 원으로).
_REAL_SERIES = [("06", -1_677_100_000_000.0), ("07", -66_700_000_000.0),
                ("10", -408_800_000_000.0), ("11", -299_800_000_000.0)]


def test_four_days_of_net_selling_is_four_consecutive(settings):
    """실측 재현: 4거래일 내리 순매도면 4구간 연속이다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed(conn, settings.raw_dir, _REAL_SERIES)

    status, detail = thesis_mod.evaluate_atom(
        conn, _atom("consecutive_sign", 4), _cutoff("2026-08-11T22:15:00+00:00"))
    assert status == "TRUE"
    assert detail["sum"] == pytest.approx(-2_452_400_000_000.0)


def test_the_old_kind_misses_the_same_series(settings):
    """⚠️ 회귀 감시 — 이것이 고친 결함 그 자체다.

    같은 4일 순매도를 옛 종류(`consecutive`)로 재면 거짓이 나온다. 이 테스트가
    깨진다면 누군가 `consecutive`의 의미를 부호 기준으로 바꾼 것이고, 그러면
    재무 조건들(MSFT 영업이익·EQIX 매출)의 뜻이 함께 뒤집힌다.
    """
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed(conn, settings.raw_dir, _REAL_SERIES)

    status, _ = thesis_mod.evaluate_atom(
        conn, _atom("consecutive", 3), _cutoff("2026-08-11T22:15:00+00:00"))
    assert status == "FALSE", "consecutive는 단조 변화를 재야 한다(의미를 바꾸지 말 것)"


def test_a_net_buy_day_breaks_the_streak(settings):
    """신호를 만들어내지 않는다: 중간에 순매수가 한 번 끼면 연속이 끊긴다.

    실측에서도 8/5에 +6,577억 순매수가 있어 5일 연속은 거짓이다 — 그래서 이
    수리는 "4일은 참, 5일은 거짓"이라는 정직한 상태를 만든다.
    """
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed(conn, settings.raw_dir, [("05", +657_700_000_000.0)] + _REAL_SERIES)

    cutoff = _cutoff("2026-08-11T22:15:00+00:00")
    assert thesis_mod.evaluate_atom(conn, _atom("consecutive_sign", 4), cutoff)[0] == "TRUE"
    assert thesis_mod.evaluate_atom(conn, _atom("consecutive_sign", 5), cutoff)[0] == "FALSE"


def test_zero_is_neither_direction(settings):
    """0은 순매수도 순매도도 아니다. 연속에 끼워 주면 끊긴 흐름을 안 끊긴 것으로 읽는다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed(conn, settings.raw_dir, [("10", 0.0), ("11", -299_800_000_000.0)])

    status, _ = thesis_mod.evaluate_atom(
        conn, _atom("consecutive_sign", 2), _cutoff("2026-08-11T22:15:00+00:00"))
    assert status == "FALSE"


def test_needs_periods_observations_not_periods_plus_one(settings):
    """단조 종류는 비교 기준점이 필요해 N+1개를 요구하지만, 부호 종류는 N개면 된다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed(conn, settings.raw_dir, [("10", -408_800_000_000.0), ("11", -299_800_000_000.0)])

    cutoff = _cutoff("2026-08-11T22:15:00+00:00")
    assert thesis_mod.evaluate_atom(conn, _atom("consecutive_sign", 2), cutoff)[0] == "TRUE"
    status, detail = thesis_mod.evaluate_atom(conn, _atom("consecutive_sign", 3), cutoff)
    assert status == "UNKNOWN"
    assert detail["required"] == 3 and detail["observed"] == 2


def test_unknown_direction_is_rejected_at_load():
    """새 종류도 방향 검증을 받는다 — `consecutive`와 같은 관문."""
    reasons: list[str] = []
    thesis_mod._validate_atom(
        {"kind": "consecutive_sign", "subject": "X", "metric": "m",
         "direction": "sideways", "periods": 2}, "where", reasons)
    assert reasons and "방향" in reasons[0]
