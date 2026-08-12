"""판정 옆에 **근거가 언제 것인지**를 적는다 (CEO 지시 2026-08-12).

왜 필요한가: 판정은 매일 다시 계산되지만 근거는 그렇지 않다. 2026-08-02~08-11
열흘 동안 8건의 가설 판정이 **한 번도 바뀌지 않았고**, 그중 "강화"는 두 종류가
섞여 있었다 —

  - 수준 조건: `DGS10 >= 4.5`는 금리가 그 위에 있는 한 매일 참이다.
  - 분기 재무: MSFT 영업이익 최신치가 2026-06-30이고 다음은 10월 말이다.
    그 사이 2.5개월 동안 시장이 무엇을 하든 같은 문장이 나온다.

CEO는 매일 "강화 5건"을 읽지만 그중 새로 알게 된 것은 0건이었다. 날짜를 함께
적으면 "오늘 새로 안 것"과 "석 달 전 사실의 재확인"이 화면에서 갈린다.
"""
from __future__ import annotations

from market_intel.interp import thesis as thesis_mod


def _review(verdict: str, group: str, atoms: list[tuple[str, str, str | None]]) -> dict:
    """(id, status, latest_at) 목록으로 판정 한 줄을 만든다."""
    evals = [{"atom": {"id": aid}, "status": status,
              "detail": {"message": f"{aid} 메시지",
                         **({"latest_at": at} if at else {})}}
             for aid, status, at in atoms]
    return {"thesis_id": "t1", "theme": "ai_semi", "slot": 1, "verdict": verdict,
            "evals_by_group": {group: evals}, "all_evals": evals,
            "evaluable_atoms": len(evals), "total_atoms": len(evals)}


def test_firing_verdict_carries_the_evidence_date():
    row = _review("강화", "strengthen", [("msft_oi_2q_up", "TRUE", "2026-06-30T00:00:00+00:00")])
    out = thesis_mod.render_impact([row], "2026-08-12")
    assert "근거 2026-06-30" in out, out


def test_today_is_said_in_words_not_as_a_date():
    """근거가 오늘 것이면 날짜 대신 "오늘"이라 적는다 — 리포트 날짜와 같은 숫자가
    두 번 나오면 읽는 사람이 무엇과 무엇을 비교해야 하는지 알기 어렵다."""
    row = _review("강화", "strengthen", [("dgs10_high", "TRUE", "2026-08-12T00:00:00+00:00")])
    out = thesis_mod.render_impact([row], "2026-08-12")
    assert "근거 오늘" in out and "근거 2026-08-12" not in out


def test_the_oldest_firing_atom_wins():
    """조건이 여럿 발화하면 **가장 묵은** 근거가 이 판정의 나이다.
    최신 것을 쓰면 오래된 근거가 새 근거 뒤에 숨는다."""
    row = _review("강화", "strengthen", [
        ("fresh", "TRUE", "2026-08-11T00:00:00+00:00"),
        ("stale", "TRUE", "2026-06-30T00:00:00+00:00"),
    ])
    out = thesis_mod.render_impact([row], "2026-08-12")
    assert "근거 2026-06-30" in out


def test_hold_verdicts_get_no_date():
    """⚠️ 실제로 났던 결함: `유지`는 **판정을 만든 조건이 없는** 상태인데도
    반증 그룹의 참인 원자를 주워 "유지 — 발화 조건 없음 (근거 8/11)"이라고
    적었다. 문서가 자기 자신에 대해 거짓을 말하는 문장이다."""
    row = _review("유지", "falsify", [("sox_60d_up", "TRUE", "2026-08-11T00:00:00+00:00")])
    out = thesis_mod.render_impact([row], "2026-08-12")
    assert "근거" not in out, out


def test_missing_dates_do_not_break_the_line():
    """옛 원장 행에는 `latest_at`이 없다. 날짜가 없으면 조용히 생략한다."""
    row = _review("강화", "strengthen", [("no_date", "TRUE", None)])
    out = thesis_mod.render_impact([row], "2026-08-12")
    assert "강화" in out and "근거" not in out


def test_report_date_is_optional():
    """옛 호출부(리포트 날짜를 안 주는 곳)가 깨지지 않아야 한다."""
    row = _review("강화", "strengthen", [("a", "TRUE", "2026-06-30T00:00:00+00:00")])
    assert "근거 2026-06-30" in thesis_mod.render_impact([row])
