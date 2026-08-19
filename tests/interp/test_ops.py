"""ST3 — `interp/ops.py`: the pipeline's interpretation step and the
operational-status assembly (spec SA-9, SA-12).

Two contracts are under test here and nothing else:

1. `interpret_report()` is the *pipeline* entry point. Unlike the ad-hoc
   `market-intel interpret` CLI (ST2), it is the system of record: it writes
   the `interpretations` mirror row, the `thesis_reviews` rows and the
   `data_gaps` entry SA-9 requires — and it reads every report through that
   report's own `cutoff_utc`, never a wall clock (SA-10).
2. `status()` assembles exactly SA-12's four sources plus the overdue
   judgement, which is the one place in this codebase allowed to look at the
   wall clock (the page answers "is this system alive *now*").
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from market_intel import db as db_mod
from market_intel.interp import llm as llm_mod
from market_intel.interp import ops as ops_mod
from market_intel.interp import store as store_mod
from market_intel.reporting.cutoff import KST

from conftest import make_report


def write_report(tmp_path, report, name="report.json"):
    path = tmp_path / name
    path.write_text(report.to_json(), encoding="utf-8")
    return path


# --- interpret_report -------------------------------------------------------

def test_interpret_report_records_interpretation_row(conn, tmp_path):
    path = write_report(tmp_path, make_report())
    result = ops_mod.interpret_report(conn, path, use_llm=False)

    assert result["status"] == "disabled"
    row = store_mod.last_interpretation(conn)
    assert row is not None
    assert row["status"] == "disabled"
    assert row["report_type"] == "weekly_review"
    assert row["report_date"] == "2026-08-01"


def test_interpret_report_writes_the_report_back(conn, tmp_path):
    path = write_report(tmp_path, make_report())
    ops_mod.interpret_report(conn, path, use_llm=False)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["meta"]["interpretation"]["status"] == "disabled"


def test_interpret_report_registers_a_data_gap_on_llm_failure(conn, tmp_path, monkeypatch):
    """spec SA-9 layer 2 — the `missing` entry gets a matching `data_gaps`
    row (`INSERT OR IGNORE`, the existing `_register_gap` pattern)."""
    def dead(*a, **k):
        raise llm_mod.LLMUnavailable("connection refused")

    monkeypatch.setattr(ops_mod.apply_mod.llm_mod, "generate_json", dead)
    path = write_report(tmp_path, make_report())
    result = ops_mod.interpret_report(conn, path)

    assert result["status"] == "llm_unavailable"
    gaps = {r["gap_id"] for r in conn.execute("SELECT gap_id FROM data_gaps")}
    assert "interp:llm_unavailable" in gaps
    written = json.loads(path.read_text(encoding="utf-8"))
    assert any(m["area"] == "AI 해석" for m in written["missing"])


def test_interpret_report_uses_the_reports_own_cutoff_not_now(conn, tmp_path, monkeypatch):
    """spec SA-10 — a catch-up report generated today must still be
    interpreted as of its own historical blackout."""
    seen = {}

    real_fill = ops_mod.apply_mod.fill

    def spy(report, c, *, cutoff, **kw):
        seen["cutoff"] = cutoff
        return real_fill(report, c, cutoff=cutoff, **kw)

    monkeypatch.setattr(ops_mod.apply_mod, "fill", spy)
    report = make_report(report_date="2026-07-20", cutoff_utc="2026-07-20T22:15:00+00:00")
    path = write_report(tmp_path, report)
    ops_mod.interpret_report(conn, path, use_llm=False)

    assert seen["cutoff"] == datetime.fromisoformat("2026-07-20T22:15:00+00:00")


def test_interpret_report_records_thesis_reviews(conn, tmp_path):
    """가설 판정은 파이프라인이 기록한다 — 기록이 없으면 `changed`(가설 변화)를
    영영 계산할 수 없다(첫 판정에는 prev_verdict가 없기 때문)."""
    theses = [{
        "thesis_id": "ai_semi_1", "theme": "ai_semi", "slot": 1,
        "statement": "테스트 가설", "leading_indicators": ["NVDA price_close"],
        "next_check_date": "2026-11-01",
        "conditions": {"falsify": [
            {"id": "nvda_3d_down", "kind": "consecutive", "category": "price",
             "subject": "NVDA", "metric": "price_close", "direction": "down", "periods": 3}
        ], "weaken": [], "strengthen": []},
    }]
    store_mod.replace_theses(conn, theses, "sha")

    path = write_report(tmp_path, make_report())
    ops_mod.interpret_report(conn, path, use_llm=False)

    rows = conn.execute("SELECT thesis_id, verdict FROM thesis_reviews").fetchall()
    assert [r["thesis_id"] for r in rows] == ["ai_semi_1"]
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["meta"]["interpretation"]["thesis_reviews"][0]["thesis_id"] == "ai_semi_1"


def test_interpreting_the_same_report_twice_does_not_blow_up(conn, tmp_path):
    """실측으로 터진 것: `thesis_reviews`에는 (가설, 리포트, 차단선) UNIQUE 제약이
    있는데 `store.record_reviews`는 그냥 INSERT다. 같은 날 job이 두 번 돌면
    IntegrityError가 나서 **해석이 LLM을 부르기도 전에** 실패했다. 같은 차단선의
    같은 가설 판정은 정의상 같은 값이므로, 두 번째 기록은 건너뛰는 것이 맞다."""
    theses = [{
        "thesis_id": "ai_semi_1", "theme": "ai_semi", "slot": 1,
        "statement": "테스트 가설", "leading_indicators": [],
        "next_check_date": "2026-11-01",
        "conditions": {"falsify": [
            {"id": "nvda_3d_down", "kind": "consecutive", "category": "price",
             "subject": "NVDA", "metric": "price_close", "direction": "down", "periods": 3}
        ], "weaken": [], "strengthen": []},
    }]
    store_mod.replace_theses(conn, theses, "sha")
    path = write_report(tmp_path, make_report())

    first = ops_mod.interpret_report(conn, path, use_llm=False)
    second = ops_mod.interpret_report(conn, path, use_llm=False)

    assert first["status"] == "disabled" and second["status"] == "disabled"
    rows = conn.execute("SELECT COUNT(*) c FROM thesis_reviews").fetchone()["c"]
    assert rows == 1, "같은 차단선의 판정이 두 번 기록됐다"


# --- status -----------------------------------------------------------------

def _finish(conn, job, status, steps=None, note="", started_at=None):
    job_run_id = store_mod.start_job_run(conn, job)
    store_mod.finish_job_run(conn, job_run_id, steps or {"report": "ok"}, status, note=note)
    if started_at is not None:
        conn.execute("UPDATE job_runs SET started_at=?, finished_at=? WHERE job_run_id=?",
                     (started_at, started_at, job_run_id))
        conn.commit()
    return job_run_id


def test_status_lists_every_job_even_when_never_run(conn):
    from market_intel.jobs import JOBS

    st = ops_mod.status(conn, now=datetime(2026, 8, 3, 9, 0, tzinfo=KST))
    assert [j["job"] for j in st["jobs"]] == list(JOBS)
    assert all(j["state"] == "기록 없음" for j in st["jobs"] if j["last_started"] is None)


def test_status_with_no_history_at_all_says_so_instead_of_crying_wolf(conn):
    """기록이 통째로 없을 때(=이 기능을 막 배포했거나 launchd 등록이 안 됐을 때)
    9개 job을 전부 `지연`이라고 적으면 첫 화면이 거짓 경보로 가득 찬다. 실제로
    이 맥의 라이브 DB로 돌려 보고 고친 것 — 8건이 그렇게 떴다. 모르는 것은
    모른다고 적고, 대신 '기록이 없다'는 사실 자체를 한 줄로 경고한다."""
    st = ops_mod.status(conn, now=datetime(2026, 8, 14, 9, 0, tzinfo=KST))
    assert all(j["overdue"] == 0 for j in st["jobs"]), st["jobs"]
    assert all(j["state"] == "기록 없음" for j in st["jobs"])
    assert st["healthy"] is False
    assert any("기록" in a for a in st["alerts"]), st["alerts"]


def test_status_does_not_blame_a_job_for_slots_before_recording_began(conn):
    """기록이 시작되기 전의 슬롯은 '늦은' 것이 아니라 '모르는' 것이다."""
    now = datetime(2026, 8, 14, 9, 0, tzinfo=KST)
    _finish(conn, "weekly", "ok", started_at=db_mod.iso_utc(now))
    st = ops_mod.status(conn, now=now)
    morning = next(j for j in st["jobs"] if j["job"] == "morning")
    assert morning["overdue"] == 0, morning
    assert morning["state"] == "기록 없음"


def test_status_flags_a_job_that_stopped_after_recording_began(conn):
    """반대로, 기록이 시작된 뒤로 한 번도 안 돈 job은 진짜 `지연`이다."""
    now = datetime(2026, 8, 14, 9, 0, tzinfo=KST)
    _finish(conn, "weekly", "ok", started_at=db_mod.iso_utc(now - timedelta(days=10)))
    st = ops_mod.status(conn, now=now)
    morning = next(j for j in st["jobs"] if j["job"] == "morning")
    assert morning["overdue"] >= 1, morning
    assert morning["state"] == "지연"
    assert st["healthy"] is False


def test_status_marks_a_job_that_has_not_run_for_days_as_overdue(conn):
    """spec SA-12 — 마지막 성공 이후 지나간 예정 슬롯이 1개 이상이면 `지연`.
    CEO가 사이트만 보고도 '며칠째 죽어 있다'를 알아야 한다."""
    now = datetime(2026, 8, 14, 9, 0, tzinfo=KST)  # Friday
    ten_days_ago = db_mod.iso_utc(now - timedelta(days=10))
    _finish(conn, "morning", "ok", started_at=ten_days_ago)

    st = ops_mod.status(conn, now=now)
    morning = next(j for j in st["jobs"] if j["job"] == "morning")
    assert morning["overdue"] >= 1, morning
    assert morning["state"] == "지연"
    assert st["healthy"] is False
    assert any("morning" in a for a in st["alerts"])


def test_status_today_slot_alone_is_not_overdue(conn):
    """오늘치 슬롯은 아직 예정 시각이 안 왔을 수 있으므로 지연으로 세지 않는다."""
    now = datetime(2026, 8, 14, 9, 0, tzinfo=KST)  # Friday
    yesterday = db_mod.iso_utc(now - timedelta(days=1))
    _finish(conn, "morning", "ok", started_at=yesterday)

    st = ops_mod.status(conn, now=now)
    morning = next(j for j in st["jobs"] if j["job"] == "morning")
    assert morning["overdue"] == 0, morning
    assert morning["state"] == "정상"


def test_status_reports_failed_and_partial_runs(conn):
    now = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    _finish(conn, "weekly", "fail", steps={"report": "fail"}, started_at=db_mod.iso_utc(now))
    _finish(conn, "close", "partial", steps={"interpret": "fail"}, started_at=db_mod.iso_utc(now))

    st = ops_mod.status(conn, now=now)
    weekly = next(j for j in st["jobs"] if j["job"] == "weekly")
    close = next(j for j in st["jobs"] if j["job"] == "close")
    # 마지막 실행 결과가 지연보다 우선한다 — 방금 깨진 job을 `지연`으로만
    # 적으면 더 급한 사실이 가려진다(밀린 슬롯 수는 overdue 칸에 그대로 있다).
    assert weekly["state"] == "실패"
    assert close["state"] == "일부 실패"
    assert st["healthy"] is False


def test_status_carries_providers_gaps_and_last_interpretation(conn, tmp_path):
    now = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    conn.execute("INSERT INTO collect_runs(run_id, workflow, cutoff_at, started_at, finished_at, status)"
                 " VALUES ('r1','morning',?,?,?,'done')",
                 (db_mod.iso_utc(now), db_mod.iso_utc(now), db_mod.iso_utc(now)))
    conn.execute("INSERT INTO provider_runs(provider_run_id, run_id, provider, started_at, finished_at,"
                 " item_count, status, reason_code, safe_detail) VALUES ('p1','r1','fred',?,?,3,'PARTIAL','http_429',?)",
                 (db_mod.iso_utc(now), db_mod.iso_utc(now), "x" * 400))
    conn.execute("INSERT OR IGNORE INTO data_gaps(gap_id, subject, metric, detected_at, reason, status)"
                 " VALUES ('kr_flows:net_buy','kr_flows','net_buy',?,'KRX 인증벽','제안')", (db_mod.iso_utc(now),))
    conn.commit()

    path = write_report(tmp_path, make_report())
    ops_mod.interpret_report(conn, path, use_llm=False)

    st = ops_mod.status(conn, now=now)
    assert st["collect"]["workflow"] == "morning"
    provider = st["collect"]["providers"][0]
    assert provider["provider"] == "fred"
    assert len(provider["safe_detail"]) <= ops_mod.SAFE_DETAIL_MAX
    assert st["interpretation"]["status"] == "disabled"
    assert [g["gap_id"] for g in st["gaps"]] == ["kr_flows:net_buy"]


def test_status_never_reports_a_failed_run_as_healthy(conn):
    """변이 검사 대상 — 실패를 성공으로 표시하면 이 단언이 깨진다."""
    now = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    _finish(conn, "weekly", "fail", steps={"report": "fail"}, started_at=db_mod.iso_utc(now))
    st = ops_mod.status(conn, now=now)
    weekly = next(j for j in st["jobs"] if j["job"] == "weekly")
    assert weekly["state"] != "정상"
    assert st["healthy"] is False


def test_thesis_overview_reason_matches_the_verdict(conn):
    """판정을 만든 조건의 문장을 보여야 한다. 첫 조건을 무조건 집으면 `강화`
    가설 밑에 반증 조건("… 아님")이 근거랍시고 붙어 사람이 읽고 혼란스러워진다
    (실측: fin_credit_1이 그렇게 나왔다)."""
    store_mod.replace_theses(conn, [{
        "thesis_id": "t1", "theme": "ai_semi", "slot": 1, "statement": "s",
        "conditions": {"falsify": [], "weaken": [], "strengthen": []},
        "leading_indicators": [], "next_check_date": "2026-11-01",
    }], "sha")
    atoms = json.dumps({
        "falsify": [{"id": "f1", "status": "FALSE", "detail": {"message": "반증 조건 문장"}}],
        "weaken": [],
        "strengthen": [{"id": "s1", "status": "TRUE", "detail": {"message": "강화 조건 문장"}}],
    }, ensure_ascii=False)
    store_mod.record_reviews(conn, [{
        "thesis_id": "t1", "report_type": "morning", "report_date": "2026-08-01",
        "cutoff_utc": db_mod.iso_utc(datetime(2026, 8, 1, 9, 0, tzinfo=KST)),
        "verdict": "강화", "prev_verdict": None, "changed": 0,
        "atoms_json": atoms, "evidence_json": "[]",
    }])

    overview = ops_mod.thesis_overview(conn)
    thesis = next(t for theme in overview for t in theme["theses"])
    assert thesis["reason"] == "강화 조건 문장", thesis["reason"]


def test_thesis_changes_returns_recent_verdict_flips(conn, tmp_path):
    now = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    store_mod.record_reviews(conn, [{
        "thesis_id": "ai_semi_1", "report_type": "morning", "report_date": "2026-08-01",
        "cutoff_utc": db_mod.iso_utc(now), "verdict": "약화", "prev_verdict": "유지",
        "changed": 1, "atoms_json": "{}", "evidence_json": "[]",
    }])
    changes = ops_mod.thesis_changes(conn, now=now)
    assert len(changes) == 1
    assert changes[0]["verdict"] == "약화" and changes[0]["prev_verdict"] == "유지"


def test_thesis_changes_ignores_old_flips(conn):
    old = datetime(2026, 1, 1, 9, 0, tzinfo=KST)
    now = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    store_mod.record_reviews(conn, [{
        "thesis_id": "ai_semi_1", "report_type": "morning", "report_date": "2026-01-01",
        "cutoff_utc": db_mod.iso_utc(old), "verdict": "약화", "prev_verdict": "유지",
        "changed": 1, "atoms_json": "{}", "evidence_json": "[]",
    }])
    assert ops_mod.thesis_changes(conn, now=now) == []


def test_thesis_changes_also_returns_engine_semantics_boundaries(conn):
    """ST3: `engine_changed=1`도 판정이 뒤집힌 것만큼 중요한 사건이다(명세
    §2) — verdict은 그대로여도(`changed=0`) 실려야 한다. `rules_changed=1`의
    기존 계약과 완전히 대칭."""
    now = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
    store_mod.record_reviews(conn, [{
        "thesis_id": "ai_semi_1", "report_type": "morning", "report_date": "2026-08-01",
        "cutoff_utc": db_mod.iso_utc(now), "verdict": "강화", "prev_verdict": "강화",
        "changed": 0, "atoms_json": "{}", "evidence_json": "[]",
        "engine_version": "2b.3", "engine_changed": 1,
    }])
    changes = ops_mod.thesis_changes(conn, now=now)
    assert len(changes) == 1
    assert changes[0]["engine_changed"] == 1
