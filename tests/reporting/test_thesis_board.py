"""가설 상태판 — 산문 9줄 대신 표 한 장 (CEO 지적 2026-08-27).

*"강화 유지 같은 지표가 한눈에 보이지 않아."*

맞았다. 판정은 「해석」 섹션 안쪽 **269번째 줄**에 산문으로만 있었고, 그 앞에
핵심 사실 125줄이 먼저 왔다. "지금 무슨 상태인가"를 알려면 스크롤해서 문장 아홉
줄을 읽어야 했다.

이 파일이 지키는 계약 넷:
  1. **산문을 대체하지 않는다.** 같은 판정을 같은 선택 규칙으로 요약만 한다 —
     두 곳이 따로 고르면 표는 "3일째"인데 문장은 "20일째"인 날이 온다.
  2. **머리말 순서를 밀지 않는다.** 새 `## ` 섹션이 아니라 첫 섹션 안쪽이다.
  3. **`—`와 `0`은 다르다.** 조건이 없는 것과 오늘 시작한 것은 정반대다.
  4. **근거의 나이를 말한다.** "강화"라도 근거가 두 달 전이면 오늘 새로 안 것이
     아니다(실측 2026-08-27: 강화 4건 중 2건이 그랬다).
"""
from __future__ import annotations

from market_intel.reporting import render_html as html_mod
from market_intel.reporting import render_md as md
from market_intel.reporting.model import Report, ThesisBoardRow


def _row(**kw) -> ThesisBoardRow:
    base = dict(label="AI·반도체 #1", verdict="강화", changed=False, prev_verdict="",
                duration_days=5, duration_at_least=False,
                basis_date="2026-08-26", basis_age_days=1)
    base.update(kw)
    return ThesisBoardRow(**base)


def _report(rows) -> Report:
    r = Report(report_type="morning", report_date="2026-08-27")
    r.thesis_board = rows
    return r


# --- 계약 2: 자리 ------------------------------------------------------------

def test_the_board_sits_inside_the_first_section_not_as_a_new_one():
    """`## ` 하나를 더 만들면 `test_cli_report`가 못박은 머리말 순서가 밀린다."""
    sections = md.sections(_report([_row()]))
    headers = [h for h, _ in sections]
    assert headers[:1] == ["시장 한 줄"], headers[:3]
    kinds = [b.get("kind") for b in dict(sections)["시장 한 줄"]]
    assert "thesis_board" in kinds, kinds


def test_the_board_is_near_the_top_of_the_report():
    """맨 아래에 있으면 못 본다 — 그게 이 작업의 이유다."""
    text = md.render_markdown(_report([_row()]))
    assert "가설 상태" in text
    assert text.index("가설 상태") < text.index("## 핵심 사실")


def test_no_board_no_heading():
    """옛 리포트 JSON과 해석 실패한 날에는 빈 표를 내지 않는다."""
    assert "가설 상태" not in md.render_markdown(_report([]))


# --- 계약 3: 모른다와 없다와 0은 다르다 ----------------------------------------

def test_a_thesis_with_no_firing_condition_shows_a_dash_not_zero():
    """유지·판정 불가는 **판정을 만든 조건이 없다**. 0일째라고 적으면 "오늘
    시작했다"가 되어 정반대를 말한다."""
    assert md.thesis_board_days(_row(verdict="유지", duration_days=None)) == "—"
    assert md.thesis_board_days(_row(duration_days=0)) == "0일째"


def test_a_left_censored_run_says_at_least():
    """원장 기록 시작 이전부터 이어진 구간은 **일수는 알지만 그 전을 모른다**.
    산문도 "지속 20판정일"이라고 쓴다 — 표만 "—"로 비우면 같은 판정을 두 곳이
    다르게 말한다."""
    assert md.thesis_board_days(_row(duration_days=20, duration_at_least=True)) == "20일째+"
    assert md.thesis_board_days(_row(duration_days=20, duration_at_least=False)) == "20일째"


# --- 계약 4: 근거의 나이 ------------------------------------------------------

def test_an_old_basis_is_labelled_with_its_age():
    """실측(2026-08-27): 강화 4건 중 2건의 근거가 두 달 전이었다. 상태만 보면
    매일 무언가 좋아지는 것처럼 읽힌다."""
    lines = md.thesis_board_lines([_row(basis_date="2026-06-30", basis_age_days=58)])
    assert "2026-06-30 (58일 전)" in lines[-1]


def test_a_fresh_basis_is_not_shouted_about():
    lines = md.thesis_board_lines([_row(basis_date="2026-08-26", basis_age_days=1)])
    assert "(최근)" in lines[-1] and "일 전" not in lines[-1]


def test_the_note_counts_stale_bases(): 
    note = md.thesis_board_note([
        _row(basis_age_days=58), _row(basis_age_days=57), _row(basis_age_days=1)])
    assert "묵은 것 2건" in note


def test_the_note_says_a_state_is_not_an_event():
    """실측(2026-08-21): 판정 244건 중 상태가 바뀐 것은 4건(1.6%)뿐이다.
    이 문장이 없으면 매일 뜨는 "강화"가 매일의 사건으로 읽힌다."""
    note = md.thesis_board_note([_row()])
    assert "오늘의 사건이 아니라" in note
    assert "바뀐 가설 없음" in note


def test_a_changed_thesis_names_what_it_was():
    lines = md.thesis_board_lines([_row(verdict="약화", changed=True, prev_verdict="유지")])
    assert "오늘 유지→약화" in lines[-1]
    assert "바뀐 가설 1건" in md.thesis_board_note([_row(changed=True, prev_verdict="유지")])


# --- 두 렌더러가 같은 말을 한다 -------------------------------------------------

def test_both_renderers_agree():
    rows = [_row(verdict="약화", changed=True, prev_verdict="유지",
                 duration_days=3, basis_date="2026-06-30", basis_age_days=58)]
    text = md._thesis_board_md(rows)
    doc = html_mod._thesis_board_html(rows)
    for token in ("약화", "3일째", "2026-06-30 (58일 전)", "오늘 유지→약화"):
        assert token in text, f"md에 없다: {token}"
        assert token in doc, f"html에 없다: {token}"
    assert "**" not in doc, "마크다운 문법이 HTML로 샜다"
