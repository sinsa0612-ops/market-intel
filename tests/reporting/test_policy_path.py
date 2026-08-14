"""시장이 반영한 금리 경로 = 2년물 − 정책금리 상단 (CEO 지시 2026-08-14).

왜 이 파일이 있는가: CEO가 "금리인상 횟수가 2회에서 1회로 줄 것"이라는 외부 서사를
가져왔는데, 그 원천인 **연방기금 선물은 무료 API가 없다.** 그래서 "몇 회"는 못 준다.

대신 2년물은 앞으로 2년의 정책 경로를 가격에 담으므로, 정책금리와의 격차가 시장이
인상/인하 중 어느 쪽을 얼마나 반영했는지를 말한다(사외 고문 fable 제안, 2026-08-12).

⚠️ **이것은 "인상 N회"가 아니다.** 선물이 주는 확률 분포와 국채 금리차는 다른
물건이고, 화면 문구가 전자로 읽히면 없는 정밀도를 주장하는 것이다 — 그 경계를
이 파일이 지킨다.

새 데이터가 아니라 **이미 매일 받는 두 값의 차이**라 수집도 백필도 없고, 조건으로도
쓰지 않는다(2026-08-12 조건 39개 동결).
"""
from __future__ import annotations

import pytest

from market_intel.reporting import build as build_mod


def _mmap(y2: float | None, pol: float | None) -> dict:
    def row(v):
        return None if v is None else {
            "value_num": v, "unit": "percent", "event_at": "2026-08-13T00:00:00+00:00",
            "known_at": "2026-08-14T00:00:00+00:00", "subject": "X", "metric": "value",
            "data_status": "source_verified", "safe_source_url": "", "extra_json": "{}",
            "comparison_basis": "", "category": "macro",
        }
    out = {}
    if y2 is not None:
        out["DGS2"] = {"latest": row(y2), "delta_abs": None, "delta_pct": None}
    if pol is not None:
        out["DFEDTARU"] = {"latest": row(pol), "delta_abs": None, "delta_pct": None}
    return out


def test_gap_and_direction():
    """실측 2026-08-13: 2년물 4.20 vs 정책금리 3.75 -> +0.45%p, 인상 쪽."""
    r = build_mod._policy_path_row(_mmap(4.20, 3.75))
    assert r is not None
    assert "+0.45%p" in r.comparison and "인상 쪽" in r.comparison
    # 원 숫자를 함께 적는다 — 격차만 보이면 어디서 온 값인지 되짚을 수 없다.
    assert "4.20%" in r.comparison and "3.75%" in r.comparison


@pytest.mark.parametrize("y2,pol,expect", [
    (4.20, 3.75, "인상 쪽"),
    (3.40, 3.75, "인하 쪽"),
    (3.80, 3.75, "중립 근처"),   # 격차 +0.05 — 문턱(0.10) 안
    (3.70, 3.75, "중립 근처"),
])
def test_direction_wording(y2, pol, expect):
    assert expect in build_mod._policy_path_row(_mmap(y2, pol)).comparison


def test_never_claims_a_hike_count():
    """⚠️ 이 파일의 핵심 계약. 국채 금리차는 "인상 몇 회"를 말할 수 없다 —
    그렇게 쓰면 선물이 주는 정밀도를 가진 척하는 것이다."""
    r = build_mod._policy_path_row(_mmap(4.20, 3.75))
    for banned in ("회", "번", "차례"):
        assert banned not in r.label, f"라벨이 횟수를 주장한다: {r.label}"
    assert "금리 경로" in r.label and "2년물" in r.label


@pytest.mark.parametrize("y2,pol", [(None, 3.75), (4.20, None), (None, None)])
def test_missing_input_yields_no_row(y2, pol):
    """둘 중 하나가 없으면 격차가 성립하지 않는다. 한쪽만으로 만들어 내지 않는다."""
    assert build_mod._policy_path_row(_mmap(y2, pol)) is None


def test_none_value_yields_no_row():
    m = _mmap(4.20, 3.75)
    m["DGS2"]["latest"]["value_num"] = None
    assert build_mod._policy_path_row(m) is None


def test_row_carries_no_delta():
    """이 행은 **수준의 차이**이지 변화가 아니다. delta를 채우면 화살표·색이 붙어
    "격차가 늘었다/줄었다"로 읽히는데, 그 계산은 하지 않았다."""
    r = build_mod._policy_path_row(_mmap(4.20, 3.75))
    assert r.delta_pct is None
