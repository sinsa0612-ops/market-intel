"""ST3 (2단계-B) acceptance tests — `docs/status.html`, `docs/theses.html`
and the index banner (spec SA-12, ST3 What #4, final-review F3).

The CEO gets no push notifications: **the site is the only way a dead
pipeline becomes visible**. So these tests are written from that angle — a
page that renders a failure as success, or that goes stale without saying so,
is the defect being hunted here, not a cosmetic issue.

Everything public also stays adversarial about injection: `provider_runs.
safe_detail` is external-HTTP-derived text and lands on a public page.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from market_intel import db as db_mod
from market_intel import site as site_mod
from market_intel.interp import ops as ops_mod
from market_intel.interp import store as store_mod
from market_intel.interp import thesis as thesis_mod
from market_intel.jobs import JOBS
from market_intel.reporting.cutoff import KST

from tests.publish.conftest import make_report, write_report

NOW = datetime(2026, 8, 14, 9, 0, tzinfo=KST)  # Friday


def build(conn, reports_root, docs_root, now=NOW):
    return site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root, now=now)


def seed_job_run(conn, job, status, steps=None, note="", started_at=None):
    job_run_id = store_mod.start_job_run(conn, job)
    store_mod.finish_job_run(conn, job_run_id, steps or {"report": "ok"}, status, note=note)
    if started_at is not None:
        conn.execute("UPDATE job_runs SET started_at=?, finished_at=? WHERE job_run_id=?",
                     (started_at, started_at, job_run_id))
        conn.commit()


def seed_provider_run(conn, provider, status, reason_code, safe_detail, at=NOW):
    conn.execute("INSERT OR IGNORE INTO collect_runs(run_id, workflow, cutoff_at, started_at,"
                 " finished_at, status) VALUES ('r1','morning',?,?,?,'done')",
                 (db_mod.iso_utc(at), db_mod.iso_utc(at), db_mod.iso_utc(at)))
    conn.execute("INSERT INTO provider_runs(provider_run_id, run_id, provider, started_at,"
                 " finished_at, item_count, status, reason_code, safe_detail)"
                 " VALUES (?,'r1',?,?,?,0,?,?,?)",
                 (f"p_{provider}", provider, db_mod.iso_utc(at), db_mod.iso_utc(at),
                  status, reason_code, safe_detail))
    conn.commit()


# --- status.html ------------------------------------------------------------

def test_status_page_is_generated_and_linked(reports_root, docs_root, conn):
    write_report(reports_root, make_report("morning", "2026-08-14"))
    build(conn, reports_root, docs_root)

    status_html = (docs_root / "status.html").read_text(encoding="utf-8")
    index_html = (docs_root / "index.html").read_text(encoding="utf-8")
    assert (docs_root / "status.html").exists()
    assert 'href="status.html"' in index_html, "상단 메뉴에서 상태 페이지로 갈 수 없다"
    assert 'href="theses.html"' in index_html
    # A report page sits two directories deep — its nav must still resolve.
    report_html = (docs_root / "reports" / "morning" / "2026-08-14.html").read_text(encoding="utf-8")
    assert 'href="../../status.html"' in report_html


def test_status_page_shows_its_own_generation_time(reports_root, docs_root, conn):
    """spec SA-12 — 페이지 자체가 낡으면 그 날짜가 곧 신호다."""
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "생성 시각" in html
    assert "2026-08-14" in html


def test_status_page_lists_every_job(reports_root, docs_root, conn):
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    for job in JOBS:
        assert job in html, f"{job} is missing from the status page"


def test_status_page_shows_last_run_result_and_reason(reports_root, docs_root, conn):
    seed_job_run(conn, "weekly", "fail", steps={"report": "fail", "site": "ok"},
                 note="report 단계 예외", started_at=db_mod.iso_utc(NOW))
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "실패" in html
    assert "report 단계 예외" in html
    assert "report=fail" in html


def test_status_page_marks_a_days_dead_job_as_overdue(reports_root, docs_root, conn):
    """며칠째 수집이 죽어 있으면 화면에서 즉시 보여야 한다 (final-review F3)."""
    seed_job_run(conn, "morning", "ok", started_at=db_mod.iso_utc(NOW - timedelta(days=10)))
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "지연" in html
    assert "확인 필요" in html


def test_status_page_says_it_is_healthy_when_everything_ran(reports_root, docs_root, conn):
    for job in JOBS:
        seed_job_run(conn, job, "ok", started_at=db_mod.iso_utc(NOW))
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "정상" in html
    assert "확인 필요" not in html


def test_status_page_shows_provider_results(reports_root, docs_root, conn):
    seed_provider_run(conn, "fred", "PARTIAL", "http_429", "요청이 너무 잦습니다")
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "fred" in html and "http_429" in html and "요청이 너무 잦습니다" in html


def test_status_page_truncates_long_provider_detail(reports_root, docs_root, conn):
    seed_provider_run(conn, "fred_calendar", "PARTIAL", "parse_error", "가" * 400)
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "가" * 200 in html
    assert "가" * 260 not in html, "safe_detail must be truncated (SA-12)"


def test_status_page_escapes_provider_detail(reports_root, docs_root, conn):
    """`safe_detail` is external-HTTP-derived and lands on a public page."""
    seed_provider_run(conn, "dart", "PARTIAL", "bad_html", "<script>alert(1)</script>")
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_status_page_shows_last_interpretation_and_gaps(reports_root, docs_root, conn):
    store_mod.record_interpretation(conn, {
        "report_type": "morning", "report_date": "2026-08-14",
        "cutoff_utc": db_mod.iso_utc(NOW), "status": "llm_unavailable",
        "model": "qwen3.5:9b", "prompt_version": "interpretation_v1", "prompt_sha256": "x",
        "fields": {"reading": "empty"}, "violations": None, "evidence": None,
        "attempts": 1, "elapsed_ms": 12,
    })
    conn.execute("INSERT OR IGNORE INTO data_gaps(gap_id, subject, metric, detected_at, reason,"
                 " status) VALUES ('kr_flows:net_buy','kr_flows','net_buy',?,'KRX 인증벽','제안')",
                 (db_mod.iso_utc(NOW),))
    conn.commit()
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "llm_unavailable" in html
    assert "kr_flows:net_buy" in html and "KRX 인증벽" in html


def test_status_page_has_no_script_tag_and_no_javascript_url(reports_root, docs_root, conn):
    seed_provider_run(conn, "ecos", "PARTIAL", "bad", "javascript:alert(1)")
    build(conn, reports_root, docs_root)
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "<script" not in html
    assert 'href="javascript:' not in html


def test_status_page_table_can_scroll_on_a_narrow_screen(reports_root, docs_root, conn):
    """Human check 4 (아이폰/좁은 창) — 표가 가로로 넘치지 않고 읽힌다."""
    build(conn, reports_root, docs_root)
    css = (docs_root / "style.css").read_text(encoding="utf-8")
    html = (docs_root / "status.html").read_text(encoding="utf-8")
    assert "overflow-x" in css
    assert 'class="scroll"' in html


# --- theses.html ------------------------------------------------------------

def seed_thesis(conn, thesis_id="ai_semi_1", theme="ai_semi", slot=1):
    store_mod.replace_theses(conn, [{
        "thesis_id": thesis_id, "theme": theme, "slot": slot,
        "statement": "AI 반도체 수요는 2026년에도 견조하다",
        "conditions": {"falsify": [], "weaken": [], "strengthen": []},
        "leading_indicators": ["NVDA price_close"],
        "next_check_date": "2026-11-01",
    }], "sha256")


def test_theses_page_shows_every_theme_and_the_current_verdict(reports_root, docs_root, conn):
    seed_thesis(conn)
    store_mod.record_reviews(conn, [{
        "thesis_id": "ai_semi_1", "report_type": "morning", "report_date": "2026-08-14",
        "cutoff_utc": db_mod.iso_utc(NOW), "verdict": "유지", "prev_verdict": None,
        "changed": 0, "atoms_json": '{"falsify": []}', "evidence_json": "[]",
    }])
    build(conn, reports_root, docs_root)
    html = (docs_root / "theses.html").read_text(encoding="utf-8")
    assert "AI 반도체 수요는 2026년에도 견조하다" in html
    assert "유지" in html
    for label in ("AI·반도체", "전력·에너지", "금융·신용", "소비·경기", "정책·지정학"):
        assert label in html, f"{label} 테마가 빠졌다"
    assert "가설 없음" in html, "빈 테마는 '가설 없음'이라고 말해야 한다"


def test_theses_page_publishes_verdict_changes(reports_root, docs_root, conn):
    """명세 §13.3 — 가격 알림보다 가설 변화 알림을 우선한다. CEO는 푸시 알림을
    받지 않으므로 그 형태가 사이트 게시다."""
    seed_thesis(conn)
    store_mod.record_reviews(conn, [{
        "thesis_id": "ai_semi_1", "report_type": "morning", "report_date": "2026-08-14",
        "cutoff_utc": db_mod.iso_utc(NOW), "verdict": "약화", "prev_verdict": "유지",
        "changed": 1, "atoms_json": '{"falsify": []}', "evidence_json": "[]",
    }])
    build(conn, reports_root, docs_root)
    html = (docs_root / "theses.html").read_text(encoding="utf-8")
    assert "가설 변화" in html
    assert "유지 → 약화" in html, "변화 목록에 이전→이후가 보이지 않는다"
    assert "ai_semi_1" in html


def test_theses_page_marks_an_engine_change_with_its_own_badge(reports_root, docs_root, conn):
    """final-review N1 — F5와 정확히 같은 종류의 결함(표시 경로에 테스트 0건)을
    수리하며 새로 넣은 배지였다: `표시 기준 변경` span을 통째로 지워도 publish
    161개 전부 초록이었다. `engine_changed=1` 행이 `기준 변경`(rules_changed)과
    구별되는 자기만의 배지로 렌더되는지 끝까지(진짜 `build_site`로) 확인한다."""
    seed_thesis(conn)
    store_mod.record_reviews(conn, [{
        "thesis_id": "ai_semi_1", "report_type": "morning", "report_date": "2026-08-14",
        "cutoff_utc": db_mod.iso_utc(NOW), "verdict": "유지", "prev_verdict": "유지",
        "changed": 0, "atoms_json": '{"falsify": []}', "evidence_json": "[]",
        "engine_changed": 1,
    }])
    build(conn, reports_root, docs_root)
    html = (docs_root / "theses.html").read_text(encoding="utf-8")
    assert '<span class="badge-late">표시 기준 변경</span>' in html
    assert '<span class="badge-late">기준 변경</span>' not in html, (
        "rules_changed는 세우지 않았는데 그 배지(기준 변경)가 떴다 — "
        "engine_changed 배지(표시 기준 변경)와 뒤섞였다"
    )


def test_theses_page_escapes_the_statement(reports_root, docs_root, conn):
    store_mod.replace_theses(conn, [{
        "thesis_id": "ai_semi_1", "theme": "ai_semi", "slot": 1,
        "statement": "<script>alert(1)</script>",
        "conditions": {"falsify": [], "weaken": [], "strengthen": []},
        "leading_indicators": [], "next_check_date": "2026-11-01",
    }], "sha")
    build(conn, reports_root, docs_root)
    html = (docs_root / "theses.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


# --- ST4: 상태 줄 / 범례 (final-review F5 — 이 표시 경로에 테스트가 0건이었다) --
#
# 다섯 자리 각각 지운 변이가 초록으로 남는 것이 확인됐던 결함이다: site.py의
# "상태:" 출력 블록, `ops.thesis_overview`의 `state_line` 필드, site.py의
# 범례 문장, R10 "기록 공백 포함" 표기, R7 "미상" 분기. 아래 테스트는 실제
# 판정 원장(`thesis_reviews`)을 채우고 `site.build_site`까지 끝까지 돌려
# 다섯 자리를 각각 검사한다 — mock 없이 진짜 렌더 경로.

def seed_review(conn, *, thesis_id, report_date, atoms_json, verdict="강화",
                cutoff_utc=None, engine_version=None, rules_changed=0, engine_changed=0):
    store_mod.record_reviews(conn, [{
        "thesis_id": thesis_id, "report_type": "morning", "report_date": report_date,
        "cutoff_utc": cutoff_utc or f"{report_date}T22:00:00+00:00",
        "verdict": verdict, "prev_verdict": None, "changed": 0,
        "atoms_json": json.dumps(atoms_json, ensure_ascii=False), "evidence_json": "[]",
        "engine_version": engine_version or thesis_mod.ENGINE_VERSION,
        "rules_changed": rules_changed, "engine_changed": engine_changed,
    }])


def test_theses_page_shows_the_state_line_and_legend(reports_root, docs_root, conn):
    """mutant 1 (site.py 상태 줄 삭제), mutant 2 (ops.thesis_overview의
    state_line 필드 삭제), mutant 3 (site.py 범례 문장 삭제)을 함께 잡는다."""
    seed_thesis(conn, thesis_id="ai_semi_1")
    seed_review(conn, thesis_id="ai_semi_1", report_date="2026-08-01", atoms_json={
        "falsify": [], "weaken": [],
        "strengthen": [{"id": "nvda_2d_up", "status": "TRUE",
                        "detail": {"latest_at": "2026-08-01T00:00:00+00:00"}}],
    })

    # mutant 2: the field must survive on the ops-layer dict, not just happen
    # to show up on the page some other way.
    overview = ops_mod.thesis_overview(conn)
    row = next(t for theme in overview for t in theme["theses"] if t["thesis_id"] == "ai_semi_1")
    assert row["state_line"], "ops.thesis_overview()의 state_line이 비었다"
    assert "새 관측" in row["state_line"]

    build(conn, reports_root, docs_root)
    html = (docs_root / "theses.html").read_text(encoding="utf-8")

    # mutant 1: the computed fragment must actually reach the page, not just
    # a hardcoded "상태:" label.
    assert "상태:" in html
    assert row["state_line"] in html

    # mutant 3: the legend sentence.
    assert "도입되었습니다" in html
    assert "지속일은 독립 증거의 수가 아닙니다" in html


def test_theses_page_state_line_marks_a_data_gap(reports_root, docs_root, conn):
    """R10 — mutant 4 (`· 기록 공백 포함` 표기 제거). 같은 원자가 TRUE로 이어지는
    구간 안에서 대표일 간격이 5일(>=4)이면 공백 표기가 붙어야 한다."""
    seed_thesis(conn, thesis_id="fin_credit_1", theme="fin_credit")
    atom = lambda d: {  # noqa: E731
        "falsify": [], "weaken": [],
        "strengthen": [{"id": "dgs10_high", "status": "TRUE", "detail": {"latest_at": d}}],
    }
    seed_review(conn, thesis_id="fin_credit_1", report_date="2026-07-10",
               atoms_json=atom("2026-07-10T00:00:00+00:00"))
    seed_review(conn, thesis_id="fin_credit_1", report_date="2026-07-16",
               atoms_json=atom("2026-07-16T00:00:00+00:00"))

    build(conn, reports_root, docs_root)
    html = (docs_root / "theses.html").read_text(encoding="utf-8")
    assert "기록 공백 포함" in html


def test_theses_page_state_line_shows_unknown_new_observation(reports_root, docs_root, conn):
    """R7 — mutant 5 (미상 분기 제거). `detail.latest_at`이 양쪽 다 없는
    대표일이 이어지면 새 관측을 셀 수 없으니 "미상"이라고 말해야 한다 — 조용히
    "0건"으로 뭉개면 안 된다."""
    seed_thesis(conn, thesis_id="consumer_cycle_1", theme="consumer_cycle")
    atom_no_latest = {
        "falsify": [], "weaken": [],
        "strengthen": [{"id": "retail_1y_up", "status": "TRUE", "detail": {}}],
    }
    seed_review(conn, thesis_id="consumer_cycle_1", report_date="2026-07-01", atoms_json=atom_no_latest)
    seed_review(conn, thesis_id="consumer_cycle_1", report_date="2026-07-02", atoms_json=atom_no_latest)

    build(conn, reports_root, docs_root)
    html = (docs_root / "theses.html").read_text(encoding="utf-8")
    assert "새 관측 미상" in html


# --- index banner -----------------------------------------------------------

def test_index_banner_is_quiet_when_healthy(reports_root, docs_root, conn):
    write_report(reports_root, make_report("morning", "2026-08-14"))
    for job in JOBS:
        seed_job_run(conn, job, "ok", started_at=db_mod.iso_utc(NOW))
    build(conn, reports_root, docs_root)
    html = (docs_root / "index.html").read_text(encoding="utf-8")
    assert "확인 필요" not in html
    assert "마지막 실행" in html


def test_index_banner_shouts_when_a_job_failed(reports_root, docs_root, conn):
    write_report(reports_root, make_report("morning", "2026-08-14"))
    seed_job_run(conn, "collect-am", "fail", steps={"collect": "fail"},
                 started_at=db_mod.iso_utc(NOW))
    build(conn, reports_root, docs_root)
    html = (docs_root / "index.html").read_text(encoding="utf-8")
    assert "확인 필요" in html
    assert "collect-am" in html
    assert 'class="banner warn"' in html


def test_index_banner_announces_thesis_changes(reports_root, docs_root, conn):
    write_report(reports_root, make_report("morning", "2026-08-14"))
    seed_thesis(conn)
    store_mod.record_reviews(conn, [{
        "thesis_id": "ai_semi_1", "report_type": "morning", "report_date": "2026-08-14",
        "cutoff_utc": db_mod.iso_utc(NOW), "verdict": "약화", "prev_verdict": "유지",
        "changed": 1, "atoms_json": "{}", "evidence_json": "[]",
    }])
    build(conn, reports_root, docs_root)
    html = (docs_root / "index.html").read_text(encoding="utf-8")
    assert "가설 변화" in html
    assert 'href="theses.html"' in html


# --- the wall-clock boundary stays where it belongs -------------------------

def test_schedule_page_still_uses_the_report_cutoff_not_the_build_clock(
        reports_root, docs_root, conn, tmp_path):
    """2단계-A에서 실제로 터졌던 결함: 사이트가 `schedule.changes()`를 부를 때
    벽시계를 넘기면 리포트가 볼 수 없던 일정을 공개한다. `now=`가 생긴 뒤에도
    일정 페이지의 기준선은 여전히 그 리포트의 차단선이어야 한다."""
    report = make_report("morning", "2026-08-01")
    write_report(reports_root, report)
    build(conn, reports_root, docs_root, now=NOW)
    html = (docs_root / "schedule.html").read_text(encoding="utf-8")
    # 향후 일정 표에는 미래 날짜가 나오는 것이 정상이다. 검사 대상은 **기준선**:
    # 그 값이 리포트의 차단선이어야 하고 빌드 시각이어서는 안 된다.
    assert f"기준 시각: {report.cutoff_utc}" in html
    assert NOW.isoformat(timespec="seconds") not in html, "일정 페이지가 벽시계를 기준으로 잡았다"
