"""해석 성적표 — 등록한 조건을 만기에 채점하고 화면에 싣는다 (E 증분 3).

왜 이 파일이 있는가: 증분 1+2로 조건이 **쌓이기만** 했다. 채점을 부르는 곳이
없었고, 성적은 어디에도 안 실렸다 — 등록만 되는 시스템은 아무것도 검증하지 않는다.

이 파일이 지키는 계약 넷:
  1. **채점과 표시가 한 자리다.** 채점을 해석 단계에 두면 화면 값은 build에서
     이미 확정된 뒤라 리포트가 하루 묵은 성적을 싣는다.
  2. **`as_of`는 리포트 날짜다.** 벽시계가 아니다 — 과거 리포트를 다시 만들 때
     미래를 당겨 쓰면 차단선을 어기는 것과 같다.
  3. **채점 불가는 오답이 아니다.** 적중률에서 빼고 따로 센다. 그 비율 자체가
     "우리 관측으로 검증되는 주장을 쓰고 있나"의 지표다.
  4. **산문이 맞았다고 말하지 않는다.** 화면이 말할 수 있는 것은 "등록한 조건
     3개 중 2개가 맞았다"까지다(`interp/checks.py` 모듈 주석).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from market_intel import db as db_mod
from market_intel.interp import checks as checks_mod
from market_intel.reporting import build as build_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod
from market_intel.reporting.model import InterpretationScorecard, ScorecardRow

REG_CUTOFF = datetime(2026, 3, 2, 7, 15, tzinfo=timezone.utc)


def _conn(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    conn.execute(
        "INSERT INTO interpretations(interpretation_id, report_type, report_date, cutoff_utc,"
        " status, fields_json, engine_version, created_at, model) "
        "VALUES ('i1','morning','2026-03-02','2026-03-02T07:15:00+00:00','ok','{}','x',"
        "'2026-03-02T07:20:00+00:00','gpt:gpt-5.6-luna')")
    conn.commit()
    return conn


def _seed(conn, subject, metric, event_at, value):
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision_no, known_at, event_at, subject,"
        " category, metric, value_num, comparison_basis, data_status) "
        "VALUES (?,1,?,?,?,?,?,?,'','source_verified')",
        (f"{subject}:{metric}:{event_at}", event_at, event_at, subject,
         "macro" if metric == "value" else "price", metric, value))
    conn.commit()


def _seed_pair(conn, subject, before, after):
    """등록 전 관측 하나(등록의 전제 — 원장에 대상이 있어야 한다)와 **등록 뒤**
    관측 하나. 뒤엣것이 없으면 채점은 UNKNOWN이다
    (`checks._require_new_observation`) — 실제 운영도 이 모양이다."""
    _seed(conn, subject, "value", "2026-03-01T00:00:00+00:00", before)
    _seed(conn, subject, "value", "2026-03-08T00:00:00+00:00", after)


def _atom(atom_id: str, subject: str, op: str, value: float) -> dict:
    return {"id": atom_id, "kind": "threshold", "subject": subject, "metric": "value",
            "op": op, "value": value}


def _register(conn, atoms, report_date="2026-03-02"):
    return checks_mod.register(
        conn, interpretation_id="i1", report_type="morning", report_date=report_date,
        atoms=atoms, model="gpt:gpt-5.6-luna", cutoff=REG_CUTOFF)


# --- 계약 1·2: 채점을 부르는 곳이 build이고, 기준 날짜는 리포트 날짜다 ---------

def test_building_a_report_scores_what_is_due(settings):
    """**이것이 증분 3의 존재 이유다.** 증분 1+2에서는 이 호출이 없어 조건이
    영원히 채점되지 않았다."""
    conn = _conn(settings)
    _seed_pair(conn, "DGS2", 4.5, 4.6)
    assert _register(conn, [_atom("a1", "DGS2", ">", 3.75)]) == 1

    # 만기(등록일 +7일 = 2026-03-09)가 지난 리포트를 만든다.
    card = build_mod._scorecard(
        conn, datetime(2026, 3, 10, 7, 15, tzinfo=timezone.utc), date(2026, 3, 10))

    assert card.scored == 1 and card.true == 1, "만기 도래분이 채점돼야 한다"
    assert card.rows and card.rows[0].subject == "DGS2"


def test_a_report_dated_before_the_due_date_scores_nothing(settings):
    """`as_of`가 벽시계면 지난주 리포트를 오늘 다시 만들 때 그 리포트가 볼 수
    없던 미래로 채점된다 — 차단선을 어기는 것과 같다."""
    conn = _conn(settings)
    _seed(conn, "DGS2", "value", "2026-03-01T00:00:00+00:00", 4.5)
    _register(conn, [_atom("a1", "DGS2", ">", 3.75)])

    card = build_mod._scorecard(
        conn, datetime(2026, 3, 5, 7, 15, tzinfo=timezone.utc), date(2026, 3, 5))

    assert card.scored == 0 and not card.rows
    assert card.pending == 1 and card.next_due == "2026-03-09"


def test_the_empty_scorecard_says_why_it_is_empty(settings):
    """등록은 되는데 만기가 안 온 것과, **등록 자체가 안 되고 있는 것**은 화면에서
    구별돼야 한다. 둘 다 빈 표로 보이면 배관이 끊긴 날을 아무도 모른다."""
    conn = _conn(settings)
    nothing = build_mod._scorecard(
        conn, datetime(2026, 3, 5, 7, 15, tzinfo=timezone.utc), date(2026, 3, 5))
    assert "아직 없습니다" in nothing.unavailable and "등록" in nothing.unavailable

    _seed(conn, "DGS2", "value", "2026-03-01T00:00:00+00:00", 4.5)
    _register(conn, [_atom("a1", "DGS2", ">", 3.75)])
    waiting = build_mod._scorecard(
        conn, datetime(2026, 3, 5, 7, 15, tzinfo=timezone.utc), date(2026, 3, 5))
    assert "2026-03-09" in waiting.unavailable, "언제부터 성적이 생기는지 말해야 한다"


def test_a_broken_scorecard_does_not_break_the_report(settings, monkeypatch):
    """`_prior_interpretation`과 같은 원칙 — 성적표가 리포트를 막지 않는다."""
    conn = _conn(settings)
    monkeypatch.setattr(checks_mod, "score_due",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    card = build_mod._scorecard(
        conn, datetime(2026, 3, 10, 7, 15, tzinfo=timezone.utc), date(2026, 3, 10))
    assert card.unavailable and not card.rows


# --- 계약 3: 채점 불가는 오답이 아니다 ----------------------------------------

# 원장에 대상은 있지만 **관측 수가 모자라** 판정할 수 없는 조건 — 등록은 되고
# 채점은 UNKNOWN이 된다(`tests/interp/test_checks.py::UNSCORABLE`과 같은 모양).
UNSCORABLE = {"id": "u1", "kind": "consecutive", "subject": "DGS10", "metric": "value",
              "direction": "up", "periods": 8}


def test_unscorable_is_counted_apart_from_wrong(settings):
    """채점 불가를 오답에 합치면 "우리 관측으로 검증되는 주장을 쓰고 있나"라는
    지표가 사라진다. 세 갈래(맞음·틀림·채점 불가)가 **동시에** 있는 판이라야
    합치는 변이가 잡힌다 — 채점 불가가 0인 판으로는 그 가드에 닿지 못한다."""
    conn = _conn(settings)
    _seed_pair(conn, "DGS2", 4.5, 4.6)
    _seed_pair(conn, "DGS10", 4.9, 4.8)
    assert _register(conn, [_atom("a1", "DGS2", ">", 3.75),
                            _atom("a2", "DGS10", ">", 99.0), UNSCORABLE]) == 3

    card = build_mod._scorecard(
        conn, datetime(2026, 3, 10, 7, 15, tzinfo=timezone.utc), date(2026, 3, 10))

    assert (card.true, card.false, card.unknown) == (1, 1, 1)
    assert card.scored == 2, "맞음+틀림만 적중률의 분모다 — 채점 불가는 빠진다"
    dgs10 = next(r for r in card.rows if r.subject == "DGS10")
    assert (dgs10.false, dgs10.unknown) == (1, 1), "변수별 줄에서도 갈라져야 한다"


def test_hit_rate_column_is_a_dash_not_zero_when_nothing_was_scored():
    """0%는 "다 틀렸다"이고 여기는 "잰 적이 없다"다 — 정반대를 같은 글자로 쓰면 안 된다."""
    assert render_md_mod.scorecard_rate(
        ScorecardRow(scored=0, true=0, false=0, unknown=3)) == "—"
    assert render_md_mod.scorecard_rate(
        ScorecardRow(scored=4, true=1, false=3)) == "25%"


def test_unscorable_rate_is_shown_with_its_meaning():
    """(c) 채점 불가율 — CEO가 요구한 "우리 관측으로 검증되는 주장을 쓰고 있나"."""
    notes = render_md_mod.scorecard_notes(
        InterpretationScorecard(scored=8, true=5, false=3, unknown=2))
    assert any("채점 불가 2건" in n and "20%" in n for n in notes)


# --- 계약 4: 산문이 맞았다고 말하지 않는다 -------------------------------------

def test_neither_renderer_claims_the_prose_was_right():
    """`PriorInterpretation`이 정한 것과 모순되지 않게 하는 선이다 — 채점 대상은
    **해석이 스스로 등록한 숫자 조건**이지 산문이 아니다."""
    card = InterpretationScorecard(
        rows=[ScorecardRow(label="KOSPI", subject="^KS11", scored=3, true=2, false=1)],
        scored=3, true=2, false=1)
    for text in (render_md_mod._scorecard_md(card), render_html_mod._scorecard_html(card)):
        assert "산문이 맞았는지를 말하지 않습니다" in text
        for forbidden in ("해석이 맞았", "해석이 틀렸", "예측 적중"):
            assert forbidden not in text, f"판정 문구 금지: {forbidden}"
    assert "**" not in render_html_mod._scorecard_html(card), "마크다운 문법이 HTML로 샜다"


def test_scorecard_is_inside_the_interpretation_section(settings):
    """성적표가 별도 섹션으로 빠지면 아무도 안 본다 — 지난 해석 대조와 같은 자리다."""
    from market_intel.reporting.model import Report

    report = Report(report_type="morning", report_date="2026-03-10")
    report.scorecard = InterpretationScorecard(
        rows=[ScorecardRow(label="KOSPI", subject="^KS11", scored=3, true=2, false=1)],
        scored=3, true=2, false=1)
    sections = dict(render_md_mod.sections(report))
    kinds = [b.get("kind") for b in sections["해석"]]
    assert "scorecard" in kinds
    assert kinds.index("scorecard") > kinds.index("prior"), "사례 다음에 누계다"


# --- 변수별 성적 (b) -----------------------------------------------------------

def test_scorecard_splits_by_variable_most_used_first(settings):
    """CEO가 물은 "어떤 변수를 짚었을 때 맞았나". 한 번 짚고 만 변수보다 열두 번
    짚은 변수가 먼저 읽혀야 한다."""
    conn = _conn(settings)
    _seed_pair(conn, "DGS2", 4.5, 4.6)
    _seed_pair(conn, "DGS10", 4.9, 4.8)
    _register(conn, [_atom("a1", "DGS2", ">", 3.75), _atom("a2", "DGS2", "<", 9.0),
                     _atom("a3", "DGS10", ">", 3.0)])
    checks_mod.score_due(conn, "2026-03-10",
                         datetime(2026, 3, 10, 7, 15, tzinfo=timezone.utc))

    by_subject = dict(checks_mod.scorecard_by_subject(conn))
    assert by_subject["DGS2"].scored == 2 and by_subject["DGS10"].scored == 1
    assert [s for s, _c in checks_mod.scorecard_by_subject(conn)][0] == "DGS2"


def test_variable_rows_use_the_ledger_code_as_the_identity(settings):
    """이름표는 바뀔 수 있고 원장 코드는 안 바뀐다. 줄의 정체는 코드 쪽이다."""
    conn = _conn(settings)
    _seed_pair(conn, "DGS2", 4.5, 4.6)
    _register(conn, [_atom("a1", "DGS2", ">", 3.75)])
    card = build_mod._scorecard(
        conn, datetime(2026, 3, 10, 7, 15, tzinfo=timezone.utc), date(2026, 3, 10))
    assert card.rows[0].subject == "DGS2"
    assert card.rows[0].label, "사람이 읽는 이름도 함께 있어야 한다"


def test_pending_reports_the_earliest_due_date(settings):
    conn = _conn(settings)
    _seed(conn, "DGS2", "value", "2026-03-01T00:00:00+00:00", 4.5)
    _register(conn, [_atom("a1", "DGS2", ">", 3.75)], report_date="2026-03-02")
    _register(conn, [_atom("b1", "DGS2", "<", 9.0)], report_date="2026-03-05")
    assert checks_mod.pending(conn) == (2, "2026-03-09")


# --- 옛 리포트 JSON ------------------------------------------------------------

def test_old_report_json_without_the_key_still_loads():
    """`site build`는 이 필드가 생기기 전에 쓰인 JSON까지 전부 다시 렌더한다."""
    from market_intel.reporting.model import Report

    raw = json.loads(Report(report_type="morning", report_date="2026-03-10").to_json())
    del raw["scorecard"]
    loaded = Report.from_json(json.dumps(raw))
    assert loaded.scorecard.rows == [] and loaded.scorecard.unavailable == ""


def test_old_reports_do_not_grow_an_empty_subheading():
    """`site build`는 **모든** 리포트 JSON을 매번 다시 렌더한다. 이 필드가 생기기
    전에 쓰인 것들에 빈 소제목이 붙으면, 새 코드가 옛 발행물 20여 건을 조용히
    바꿔 놓는 셈이 된다."""
    from market_intel.reporting.model import Report

    raw = json.loads(Report(report_type="morning", report_date="2026-03-10").to_json())
    del raw["scorecard"]
    old = Report.from_json(json.dumps(raw))
    assert "등록한 조건의 성적" not in render_md_mod.render_markdown(old)

    fresh = Report.from_json(json.dumps(raw))
    fresh.scorecard = InterpretationScorecard(unavailable="해석이 등록한 조건이 아직 없습니다.")
    assert "등록한 조건의 성적" in render_md_mod.render_markdown(fresh), \
        "새 리포트는 성적이 없어도 왜 없는지를 싣는다"
