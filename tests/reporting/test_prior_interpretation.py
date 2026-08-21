"""지난 해석 대조 — 판정하지 않고 나란히 놓는다 (CEO 지시 2026-08-13).

왜 이 파일이 있는가: 해석과 반대 해석을 매일 내놓고 **아무도 채점하지 않았다.**
그 글은 검증되지 않은 채 42건이 쌓였다.

그런데 채점에는 함정이 셋이다 —
  (1) 같은 LLM이 제 글을 평가하면 합리화한다,
  (2) 한국어 산문을 조건으로 파싱하는 것은 새 환각 표면이다,
  (3) 오늘 옛 글을 읽고 "실은 이런 뜻이었다"고 정하는 것은 골대 이동이다.

그래서 **판정을 하지 않는다.** 원장의 `evidence_json`이 F-번호를 (종목, 지표)로
기계적으로 풀어 주므로, 그 값이 그 뒤 어떻게 됐는지만 나란히 보여 준다. LLM도
파싱도 개입하지 않고 사후 재해석 여지가 없다 — 누가 맞았는지는 읽는 사람이 본다.

비교 대상은 **같은 종류의 직전 리포트**다: 일간은 전날, 주간은 지난주, 월간은 전월.
리포트 주기와 비교 창이 어긋나면 안 된다는 원칙은 2026-08-10에 가격 비교에서 이미
한 번 겪었다(주간 리포트가 전일대비를 쓰고 있었다).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.models import FactCandidate
from market_intel.reporting import build as build_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod
from market_intel.reporting.model import PriorClaimRow, PriorInterpretation, Report


def _cutoff(s: str = "2026-08-12T07:15:00+00:00") -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _flow(day: str, value: float, subject: str = "000660.KS",
          metric: str = "net_buy_foreign_value") -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{subject}:{day}", subject=subject, category="flow", metric=metric,
        event_at=f"{day}T06:30:00+00:00", market="KR", country="KR",
        value_num=value, unit="KRW", data_status="source_verified")


def _record(conn, report_type: str, report_date: str, evidence, text: dict,
            status: str = "ok") -> None:
    """원장에 해석 한 건. `store.record_interpretation`을 거치지 않고 직접 넣는 이유는
    이 테스트가 보려는 것이 **조회 계약**이지 기록 경로가 아니기 때문이다."""
    conn.execute(
        "INSERT INTO interpretations(interpretation_id, report_type, report_date, cutoff_utc,"
        " status, model, prompt_version, prompt_sha256, fields_json, violations_json,"
        " evidence_json, attempts, elapsed_ms, engine_version, created_at, facts_sha256, text_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{report_type}-{report_date}", report_type, report_date,
         f"{report_date}T07:15:00+00:00", status, "test", "v3", "x", "{}", None,
         json.dumps(evidence), 1, 10, "t", f"{report_date}T08:00:00+00:00", "",
         json.dumps({"text": text})),
    )
    conn.commit()


def _seed_pair(conn, raw_dir):
    """8/11에 순매도, 8/12에 순매수로 전환 — 실측 그대로의 모양."""
    from conftest import seed_fact

    seed_fact(conn, raw_dir, "kis", _flow("2026-08-11", -299_800_000_000.0),
              "2026-08-11T22:00:00+00:00")
    seed_fact(conn, raw_dir, "kis", _flow("2026-08-12", 845_100_000_000.0),
              "2026-08-12T06:40:00+00:00")


def test_prior_shows_then_and_now_for_cited_facts(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_pair(conn, settings.raw_dir)
    _record(conn, "close_delta", "2026-08-11",
            [["F6", "kis:000660.KS:net_buy_foreign_value:20260811", 1]],
            {"reading": "외국인이 지지했다", "counter_reading": "종목별 재배치다"})

    p = build_mod._prior_interpretation(conn, _cutoff(), "close_delta", date(2026, 8, 12))
    assert p.unavailable == ""
    assert p.report_date == "2026-08-11"
    assert p.reading == "외국인이 지지했다" and p.counter_reading == "종목별 재배치다"
    assert len(p.rows) == 1
    assert p.rows[0].change_ko == "음수 → 양수로 전환"


def test_same_report_type_only(settings):
    """⚠️ 비교 주기의 핵심. 주간 리포트가 어제 일간과 대조되면 "일주일 뒤 어떻게
    됐나"에 답하지 못한다 — 2026-08-10에 가격 비교에서 겪은 것과 같은 실수다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_pair(conn, settings.raw_dir)
    ev = [["F6", "kis:000660.KS:net_buy_foreign_value:20260811", 1]]
    _record(conn, "close_delta", "2026-08-11", ev, {"reading": "일간 것"})
    _record(conn, "weekly_review", "2026-08-08", ev, {"reading": "주간 것"})

    daily = build_mod._prior_interpretation(conn, _cutoff(), "close_delta", date(2026, 8, 12))
    weekly = build_mod._prior_interpretation(conn, _cutoff(), "weekly_review", date(2026, 8, 15))
    assert daily.reading == "일간 것"
    assert weekly.reading == "주간 것", "주간은 지난주 주간과 대조돼야 한다"


def test_failed_interpretations_are_not_compared(settings):
    """검증에 걸려 비었던 판은 **대조할 주장 자체가 없다.**"""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_pair(conn, settings.raw_dir)
    _record(conn, "close_delta", "2026-08-11",
            [["F6", "kis:000660.KS:net_buy_foreign_value:20260811", 1]],
            {"reading": "걸린 글"}, status="partial")

    p = build_mod._prior_interpretation(conn, _cutoff(), "close_delta", date(2026, 8, 12))
    assert p.unavailable and not p.rows


def test_no_new_observation_means_nothing_to_compare(settings):
    """그 뒤로 새 관측이 없으면 "그때 -> 지금"이 성립하지 않는다. 같은 값을 두 번
    쓰면 아무 일도 없었는데 대조한 것처럼 보인다."""
    from conftest import seed_fact

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "kis", _flow("2026-08-11", -299_800_000_000.0),
              "2026-08-11T22:00:00+00:00")
    _record(conn, "close_delta", "2026-08-11",
            [["F6", "kis:000660.KS:net_buy_foreign_value:20260811", 1]], {"reading": "x"})

    p = build_mod._prior_interpretation(conn, _cutoff(), "close_delta", date(2026, 8, 12))
    assert p.rows == [] and "새 관측" in p.unavailable


def test_missing_prior_does_not_break_the_report(settings):
    """첫 리포트에는 대조할 것이 없다. 그것 때문에 리포트가 안 나오면 안 된다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    p = build_mod._prior_interpretation(conn, _cutoff(), "close_delta", date(2026, 8, 12))
    assert p.unavailable and p.rows == []


def test_ledger_failure_does_not_break_the_report():
    """원장 조회가 죽어도 대조만 비고 리포트는 나온다 — 이 프로젝트의
    "어떤 소스가 죽어도 리포트는 나온다" 원칙(`_restore_interpretation`과 같다)."""
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("ledger down")

    p = build_mod._prior_interpretation(Broken(), _cutoff(), "close_delta", date(2026, 8, 12))
    assert p.unavailable and p.rows == []


def test_filing_events_are_skipped(settings):
    """공시 감지는 값이 없어 "그때 -> 지금"이 성립하지 않는다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_pair(conn, settings.raw_dir)
    _record(conn, "close_delta", "2026-08-11", [
        ["F1", "sec:MSFT:filing_event:20260811", 1],
        ["F6", "kis:000660.KS:net_buy_foreign_value:20260811", 1],
    ], {"reading": "x"})

    p = build_mod._prior_interpretation(conn, _cutoff(), "close_delta", date(2026, 8, 12))
    assert [r.metric_ko for r in p.rows] == ["외국인 순매수(금액)"]


def test_row_count_is_capped(settings):
    """⚠️ 한 해석이 F-번호를 수십 개 인용한다. 전부 실으면 표가 다시 77행이 된다 —
    2026-08-03에 CEO가 "표로 보니 한눈에 안 들어온다"고 지적한 그 자리다."""
    from conftest import seed_fact

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    ev = []
    for i in range(12):
        subj = f"00{i:04d}.KS"
        for day, v in (("2026-08-11", 1e9 * (i + 1)), ("2026-08-12", 9e9 * (i + 1))):
            seed_fact(conn, settings.raw_dir, "kis", _flow(day, v, subject=subj),
                      f"{day}T06:40:00+00:00")
        ev.append([f"F{i}", f"kis:{subj}:net_buy_foreign_value:20260811", 1])
    _record(conn, "close_delta", "2026-08-11", ev, {"reading": "x"})

    p = build_mod._prior_interpretation(conn, _cutoff(), "close_delta", date(2026, 8, 12))
    assert len(p.rows) == build_mod._PRIOR_MAX_ROWS
    # 가장 크게 움직인 것부터 남는다 — 아무거나 여섯 개가 아니다.
    assert "0011.KS" in p.rows[0].label


def _report_with_prior() -> Report:
    return Report(
        report_type="close_delta", report_date="2026-08-12",
        prior=PriorInterpretation(
            report_type="close_delta", report_date="2026-08-11",
            reading="F3의 외국인이 지지했다", counter_reading="F4의 종목별 재배치다",
            rows=[PriorClaimRow(label="SK하이닉스", metric_ko="외국인 순매수(금액)",
                                then_value="-2,998억 원", now_value="8,451억 원",
                                change_ko="음수 → 양수로 전환")]))


def test_both_renderers_show_the_comparison():
    report = _report_with_prior()
    md = render_md_mod.render_markdown(report)
    html = render_html_mod.render_html(report)
    for out in (md, html):
        assert "지난 해석 대조" in out
        assert "8,451억 원" in out and "음수 → 양수로 전환" in out


_BANNED_VERDICT = ("맞았", "틀렸", "적중", "빗나", "정확했", "오판", "성공", "실패")


@pytest.mark.parametrize("then_v,now_v", [
    (-3.0, 8.0), (8.0, -3.0), (1.0, 5.0), (5.0, 1.0), (2.0, 2.0), (0.0, -1.0),
])
def test_change_wording_is_description_not_verdict(then_v, now_v):
    """⚠️ 이 기능의 핵심 계약 — **코드 경로를 직접 친다.**

    첫 판은 손으로 만든 픽스처만 렌더링해 봤는데, 그러면 `_prior_change_ko`에
    "해석이 맞았다"를 심는 변이가 **이 테스트를 통과한다**(실제로 통과했다).
    계약은 "화면에 판정 문구가 없다"가 아니라 **"판정 문구를 만들어 내지 않는다"**다.
    """
    out = build_mod._prior_change_ko(then_v, now_v)
    assert out, "빈 문자열이면 표의 마지막 칸이 통째로 사라진다"
    for banned in _BANNED_VERDICT:
        assert banned not in out, f"판정 문구 '{banned}'를 코드가 만들었다: {out!r}"


def test_no_verdict_wording_in_the_rendered_report():
    """렌더링 경로에도 같은 금지를 건다(위 테스트가 생성기, 이것이 화면)."""
    md = render_md_mod.render_markdown(_report_with_prior())
    for banned in _BANNED_VERDICT:
        assert banned not in md, f"판정 문구 '{banned}'가 화면에 나갔다"


def test_prior_text_is_stripped_of_fact_numbers():
    """옛 해석도 화면에서는 F-번호를 걷어낸다 — 새 해석과 같은 규칙이다."""
    md = render_md_mod.render_markdown(_report_with_prior())
    assert "F3" not in md and "F4" not in md
    assert "외국인이 지지했다" in md


def test_old_reports_without_prior_still_load():
    """`site build`는 `reports/`의 모든 JSON에서 사이트를 다시 만든다. 이 필드가
    없던 옛 리포트 하나가 사이트 전체를 무너뜨리면 안 된다."""
    old = Report(report_type="morning", report_date="2026-07-28")
    d = json.loads(old.to_json())
    d.pop("prior")
    restored = Report.from_json(json.dumps(d, ensure_ascii=False))
    assert restored.prior.rows == [] and restored.prior.unavailable == ""
    render_md_mod.render_markdown(restored)
    render_html_mod.render_html(restored)


def test_old_reports_do_not_grow_an_empty_subheading():
    """`prior`가 생기기 전(2026-08-13 이전)에 쓰인 리포트 33건이 매 `site build`
    마다 **아래가 빈 「지난 해석 대조」 소제목**을 달고 다시 발행되고 있었다.

    새 리포트는 대조가 없어도 `_prior_interpretation`이 **왜 없는지**를 채우므로
    (직전 해석 없음 / 새 관측 없음) 소제목이 사라지지 않는다 — 그 구별이 이
    시험의 핵심이다. 사유까지 같이 숨기면 "대조를 안 하고 있다"가 화면에서
    사라진다.
    """
    from market_intel.reporting import render_md as md

    old = Report(report_type="morning", report_date="2026-08-01")
    assert not old.prior.rows and not old.prior.unavailable
    assert "지난 해석 대조" not in md.render_markdown(old)

    fresh = Report(report_type="morning", report_date="2026-08-21")
    fresh.prior = PriorInterpretation(unavailable="대조할 지난 해석이 아직 없습니다.")
    text = md.render_markdown(fresh)
    assert "지난 해석 대조" in text and "대조할 지난 해석이 아직 없습니다" in text
