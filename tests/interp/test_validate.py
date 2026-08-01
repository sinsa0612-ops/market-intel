"""SA-5 hallucination validator tests.

The 17-case adversarial table is lifted verbatim from spec §사전 확인 사실 C
(the architect's own prototype ran it and reported 17/17) — spec ST2's TDD
section explicitly asks for exactly this table to be made permanent here,
since the scratchpad it was prototyped in does not survive the session."""
from __future__ import annotations

import dataclasses

import pytest

from market_intel.interp import validate as validate_mod
from market_intel.reporting.model import CalendarRow

from conftest import make_fact_row, make_report

# name, text, expect_blocked (True=violation expected, False=must pass clean)
ADVERSARIAL_CASES = [
    ("HAL 없는 종가", "NVIDIA 주가는 212.30달러로 마감하며 전일대비 5.4% 올랐다.", True),
    ("HAL 미세 조작", "미국 실업률은 4.30%로 상승했다.", True),
    ("HAL 없는 날짜", "2026-09-17 FOMC에서 금리를 결정한다.", True),
    ("HAL 한글 단위", "삼성전자 영업이익은 12조 4천억원을 기록했다.", True),
    ("HAL 목표주가", "목표주가 250달러를 제시한다.", True),
    ("HAL 매매 권유", "지금은 매수 구간으로 보인다.", True),
    ("OK 반올림", "미10Y가 4.7% 수준까지 올랐다.", False),
    ("OK 카운트", "Core16 중 7종목이 3% 넘게 움직였다.", False),
    ("OK 정확 인용", "미국 실업률 4.20%는 변화가 없다.", False),
    ("OK 연도", "2026년 하반기 흐름을 본다.", False),
    ("OK 무수치", "금리 상단이 유지되는 국면으로 읽힌다.", False),
    ("OK 지수 언급", "미국 10년물 금리가 상승했다.", False),
    ("OK 분기 카운트", "2개 분기 연속 악화 여부를 본다.", False),
    ("OK 지수명", "S&P500과 KOSPI200을 함께 본다.", False),
    ("OK 소수 퍼센트", "지수는 0.7% 올랐다.", False),
    ("OK 날짜 인용", "2026-08-12 CPI 발표를 확인한다.", False),
    ("OK 큰수 인용", "미국 비농업고용은 158,984 Thous. of Persons이다.", False),
]


def _sample_report_dict() -> dict:
    report = make_report(
        headline="KOSPI 3100.00(+0.5%) · S&P500 6000.00(+0.2%) · 미10Y 4.20%",
        facts=[
            make_fact_row("미국 실업률", "4.20%", "직전 관측 대비 변화 없음",
                          subject="UNRATE", metric="value", raw_value=4.20),
            make_fact_row("미국 비농업고용", "158,984 Thous. of Persons", "직전 관측 대비 +0.10%",
                          subject="PAYEMS", metric="value", raw_value=158984),
            make_fact_row("Core16 3%+ 종목 수", "7종목", "", subject="core16", metric="movers", raw_value=7),
            make_fact_row("미국채 10년물 금리", "4.68%", "직전 관측 대비 변화 없음",
                          subject="DGS10", metric="value", raw_value=4.68),
            make_fact_row("KOSPI", "3100.00", "전일대비 +0.72%", subject="^KS11", metric="price_close", raw_value=3100.0),
        ],
        events=[
            CalendarRow(when="2026-08-12", name="Consumer Price Index", country="US",
                        subject="CPIAUCSL", importance="A", change="", source_url="", data_status="확정"),
        ],
    )
    return dataclasses.asdict(report)


@pytest.mark.parametrize("name,text,expect_blocked", ADVERSARIAL_CASES, ids=[c[0] for c in ADVERSARIAL_CASES])
def test_adversarial_case(name, text, expect_blocked):
    report = _sample_report_dict()
    violations = validate_mod.check(report, text)
    got_blocked = bool(violations)
    assert got_blocked == expect_blocked, f"{name}: {text!r} -> {violations}"


def test_format_script_tag_blocked():
    report = _sample_report_dict()
    v = validate_mod.check(report, "이 리포트는 <script>alert(1)</script> 사실을 반영한다.")
    assert any(kind == "format" for kind, _ in v)


def test_format_markdown_link_blocked():
    report = _sample_report_dict()
    v = validate_mod.check(report, "자세한 내용은 [여기](http://evil.example) 참고.")
    assert any(kind == "format" for kind, _ in v)


def test_length_cap():
    report = _sample_report_dict()
    text = "금리 상단이 유지되는 국면으로 읽힌다. " * 40  # far past 600 chars
    v = validate_mod.check(report, text)
    assert any(kind == "length" for kind, _ in v)


def test_thesis_impact_shaped_text_passes():
    """SA-5: the validator must be safe to run on thesis_impact-shaped
    prose (counts and verdict words, no dates) — a real rules-engine bug
    would be a validator false-negative, not this test's concern.

    NOTE: SA-8's own literal worked example for this field is
    `... (free_cash_flow 1개 < 필요 3개)`, which contains a bare "<" — SA-5
    rule 1 (format violations) exists to catch injected HTML/markdown in
    *generated* prose and would reject that exact string. See result.md:
    `apply.fill()` deliberately does NOT run validate.check() on
    thesis_impact for this reason (it is code-generated, not an LLM
    hallucination risk), so this is a documentation test of the ruleset's
    behavior on that shape of text, not a guarantee `fill()` enforces it."""
    report = _sample_report_dict()
    text = (
        "가설 3건 판정 — 강화 0 · 유지 1 · 약화 0 · 무효 0 · 판정 불가 2.\n"
        "[AI·반도체 #1] 유지 — 발화 조건 없음(평가 가능 2/3).\n"
        "[전력·에너지 #1] 판정 불가 — 관측 부족(free_cash_flow 관측 1개, 필요 3개)."
    )
    v = validate_mod.check(report, text)
    assert v == []


def test_thesis_impact_literal_spec_example_would_be_rejected():
    """Documents the SA-5/SA-8 conflict found above: SA-8's example text
    verbatim DOES trip rule 1 on the bare "<". Recorded as a real assertion
    (not just a comment) so it stays true if either side's rule changes."""
    report = _sample_report_dict()
    text = "[전력·에너지 #1] 판정 불가 — 관측 부족(free_cash_flow 1개 < 필요 3개)."
    v = validate_mod.check(report, text)
    assert ("format", "<") in v


def test_allowed_numbers_includes_raw_value_and_text_numbers():
    report = _sample_report_dict()
    nums = validate_mod.allowed_numbers(report)
    assert 4.20 in nums
    assert 158984.0 in nums
    assert 7.0 in nums


def test_allowed_dates_includes_event_and_report_date():
    report = _sample_report_dict()
    dates = validate_mod.allowed_dates(report)
    assert "2026-08-12" in dates
    assert report["report_date"] in dates
