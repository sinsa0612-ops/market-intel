"""리포트가 자기 입으로 쓴 `%p`를 인용한 해석문은 반려하지 않는다.

왜 이 파일이 있는가 (실측 2026-08-05): 금리·실업률 표기를 `%`에서 `%p`로 바꾼
날, 그날 마감 리포트의 `reading`이 통째로 반려됐다 — 검증기의 `단위변조` 규칙이
`%p`를 무조건 막고 있었기 때문이다. AI는 리포트에 적힌 `+0.25%p`를 그대로
옮겼을 뿐인데 변조로 몰린 것이다.

규칙 자체는 지운 게 아니다. 그 규칙은 모델이 `+1.76%`를 `1.76%포인트`로 바꿔 쓴
실제 사고 때문에 생겼고, 그 사고는 여전히 막아야 한다.
"""
from __future__ import annotations

from market_intel.interp import validate as v

REPORT = {
    "headline": "미10Y 4.63%(-0.1%p) · 달러/원 1,429.4원(-0.4%)",
    "breadth": "코스피 -5.15%(근사)인데 오른 종목 455 / 내린 종목 419",
    "facts": [
        {"label": "한국 기준금리", "value": "2.75 연%", "comparison": "직전 관측 대비 +0.25%p"},
        {"label": "미국 실업률", "value": "4.20 %", "comparison": "직전 관측 대비 -0.10%p"},
        {"label": "미국 CPI", "value": "332.57", "comparison": "직전 관측 대비 -0.42%"},
    ],
    "market_reaction": [],
}


def _unit_violations(text: str) -> list:
    return [x for x in v.check(REPORT, text) if x[0] == "banned:단위변조"]


# --- ① 리포트에 있는 %p 인용은 통과 ------------------------------------------

def test_quoting_a_percentage_point_from_the_report_passes():
    assert not _unit_violations("한국 기준금리가 +0.25%p 올랐습니다.")


def test_quoting_from_the_headline_passes():
    assert not _unit_violations("미10Y가 -0.1%p 내렸습니다.")


def test_spacing_difference_does_not_reject_a_quote():
    """리포트는 `+0.25%p`, 해석문은 `+0.25 %p` — 공백 하나로 반려되면 안 된다."""
    assert not _unit_violations("기준금리가 +0.25 %p 올랐습니다.")


# --- ② 변조는 여전히 걸린다 (규칙이 지키려던 것) ------------------------------

def test_a_number_the_report_never_wrote_is_still_tampering():
    """리포트에 %p가 있다고 아무 숫자에나 붙이면 규칙이 무력해진다."""
    assert _unit_violations("기준금리가 +3.00%p 올랐습니다.")


def test_percent_restated_as_percentage_point_is_still_tampering():
    """이 규칙이 생긴 원래 사고 — `-0.42%`를 `0.42%포인트`로 바꿔 쓴 것."""
    assert _unit_violations("미국 CPI가 0.42%포인트 내렸습니다.")


def test_hangul_percentage_point_spelling_is_still_tampering():
    assert _unit_violations("기준금리가 0.25퍼센트 포인트 올랐습니다.")


def test_basis_points_are_still_tampering():
    """`bp`는 리포트가 쓰지 않는 표기다 — 모델이 스스로 환산한 것이다."""
    assert _unit_violations("기준금리가 25bp 올랐습니다.")


# --- ③ 리포트가 %p를 아예 안 쓰면 예전 그대로 --------------------------------

def test_report_without_any_percentage_point_rejects_all_of_them():
    plain = {"headline": "KOSPI 6,347(+1.4%)", "breadth": "",
             "facts": [{"label": "미국 CPI", "value": "332.57",
                        "comparison": "직전 관측 대비 -0.42%"}],
             "market_reaction": []}
    assert [x for x in v.check(plain, "CPI가 0.42%p 내렸습니다.") if x[0] == "banned:단위변조"]
