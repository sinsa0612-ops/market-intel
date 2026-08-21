"""서브태스크 B 인수 테스트 — 한국 시장 폭(코스피·코스닥 전종목)이 리포트에
보이는가. spec `_org/20260804-krx-breadth/spec-b.md` §5 성공 기준의 3(차단선
준수)·§3-4(테스트 최소 목록: 역접 규칙, 맥락 부족 시 생략, 차단선 준수, 옛
JSON 하위호환, 한국 사실 없음)를 코드로 고정한다.

fixture는 `db.insert_raw_snapshot` + `db.upsert_fact`(conftest `seed_fact`)를
지난다 — `test_visual_readability.py`와 같은 이유: PIT 읽기 경로를 우회한
fixture로는 차단선 준수를 증명할 수 없다.
"""
from __future__ import annotations

from datetime import date

from market_intel import db as db_mod
from market_intel.models import FactCandidate
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import model as model_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod

from tests.reporting.conftest import seed_fact

REPORT_DATE = date(2026, 8, 3)
# close_delta 차단선은 그날 16:15 KST(=07:15Z) — krx 종가 데이터가 나오는
# 시점과 맞다(morning의 07:15 KST는 장이 열리기도 전이라 당일 값이 없다).
KNOWN_BEFORE = "2026-08-03T07:00:00+00:00"
KNOWN_AFTER = "2026-08-03T08:00:00+00:00"  # cutoff(07:15Z) 이후


def breadth_fc(subject: str, metric: str, event_at: str, value: float) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"krx:{subject}:{metric}:{event_at}", subject=subject, category="breadth",
        metric=metric, event_at=event_at, market="KR", country="KR",
        value_num=value, unit="issues" if metric.startswith("breadth_") else "%",
        data_status="source_verified",
    )


def _seed_day(conn, raw_dir, subject: str, day: str, adv: float, dec: float,
              index_pct: float | None, known_at: str = KNOWN_BEFORE) -> None:
    event_at = f"{day}T06:30:00+00:00"
    seed_fact(conn, raw_dir, "krx", breadth_fc(subject, "breadth_advancers", event_at, adv), known_at)
    seed_fact(conn, raw_dir, "krx", breadth_fc(subject, "breadth_decliners", event_at, dec), known_at)
    if index_pct is not None:
        seed_fact(conn, raw_dir, "krx",
                  breadth_fc(subject, "index_change_pct", event_at, index_pct), known_at)


def _open(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _report(conn, report_type="close_delta"):
    cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
    return build_mod.build_report(conn, report_type, REPORT_DATE, cutoff)


# --- ① 역접 규칙: 지수·다수 방향이 어긋날 때만 "인데" -----------------------

def test_contrarian_connector_only_when_index_and_majority_diverge(settings):
    conn = _open(settings)
    # 코스피: 지수는 내렸는데(-3.0%) 상승 종목이 더 많다 -> 역접("인데").
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=6.0, dec=4.0, index_pct=-3.0)
    # 코스닥: 지수도 오르고(+2.0%) 상승 종목도 더 많다 -> 같은 방향("·").
    _seed_day(conn, settings.raw_dir, "KOSDAQ", "2026-08-03", adv=7.0, dec=3.0, index_pct=2.0)
    report = _report(conn)
    conn.close()

    lines = report.breadth.splitlines()
    assert len(lines) == 3, report.breadth
    assert lines[0].startswith("관측기업")  # 첫 줄은 그대로 유지(spec §1)

    kospi_line = next(ln for ln in lines if ln.startswith("코스피"))
    kosdaq_line = next(ln for ln in lines if ln.startswith("코스닥"))

    assert kospi_line == "코스피 -3.00%(근사)인데 오른 종목 6 / 내린 종목 4 — 상승비율 60%", kospi_line
    assert kosdaq_line == "코스닥 +2.00%(근사) · 오른 종목 7 / 내린 종목 3 — 상승비율 70%", kosdaq_line
    # 표본(1일)이 최소 문맥 일수 미만이므로 백분위 절은 아예 없다.
    assert "중" not in kospi_line and "중" not in kosdaq_line


def test_no_contrarian_when_index_down_and_decliners_lead(settings):
    """지수도 내리고 내린 종목도 더 많으면 어긋난 게 아니다 — "인데" 금지.

    위 테스트는 "지수↓ + 오른 종목 多"만 덮는다. 이 갈래(둘 다 아래)를 안 덮으면
    역접 조건을 `index_pct < 0`만으로 잘못 구현해도 초록불이 된다."""
    conn = _open(settings)
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=2.0, dec=7.0, index_pct=-3.0)
    report = _report(conn)
    conn.close()

    kospi_line = next(ln for ln in report.breadth.splitlines() if ln.startswith("코스피"))
    assert "인데" not in kospi_line, kospi_line


def test_stale_breadth_is_dated_not_passed_off_as_today(settings):
    """수집이 며칠 실패해 최신 관측이 리포트 날짜보다 오래됐으면 **날짜를 밝힌다.**

    이게 없으면 2주 전 숫자가 아무 표시 없이 오늘 줄에 실린다(심사 실측).
    줄을 통째로 빼지는 않는다 — 아침 리포트는 정상적으로 전 거래일이 최신이라
    빼버리면 매일 한국 시장이 사라진다."""
    conn = _open(settings)
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-07-20", adv=900.0, dec=100.0, index_pct=3.5)
    report = _report(conn)  # REPORT_DATE = 2026-08-03
    conn.close()

    kospi_line = next(ln for ln in report.breadth.splitlines() if ln.startswith("코스피"))
    assert "7/20 기준" in kospi_line, kospi_line


def test_same_day_breadth_carries_no_date_label(settings):
    """반대로 최신 관측이 리포트 날짜와 같으면 날짜를 붙이지 않는다 —
    매 줄에 오늘 날짜가 붙으면 잡음만 늘고 §1 목표 형태와 달라진다."""
    conn = _open(settings)
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=6.0, dec=4.0, index_pct=-3.0)
    report = _report(conn)
    conn.close()

    kospi_line = next(ln for ln in report.breadth.splitlines() if ln.startswith("코스피"))
    assert "기준)" not in kospi_line, kospi_line


def test_no_contrarian_when_index_missing(settings):
    """지수 근사치가 없으면 방향을 비교할 수 없으니 역접을 쓰지 않는다
    (섣불리 어긋난다고 단정하지 않는다) — §3.3 미확인은 채우지 않는다는 원칙."""
    conn = _open(settings)
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=6.0, dec=4.0, index_pct=None)
    report = _report(conn)
    conn.close()

    kospi_line = next(ln for ln in report.breadth.splitlines() if ln.startswith("코스피"))
    assert "지수 근사치 미확인" in kospi_line, kospi_line
    assert "인데" not in kospi_line, kospi_line


# --- ② 맥락 부족(§2-3) -------------------------------------------------------

def _flat_history(n: int, ratio_adv: float, ratio_dec: float, *, start_year=2024, start_month=1) -> dict:
    """`n`개의 연속 날짜에 같은 상승비율을 심은 순수 딕셔너리(과거 표본)."""
    out = {}
    d = date(start_year, start_month, 1)
    from datetime import timedelta
    for i in range(n):
        day = (d + timedelta(days=i)).isoformat()
        out[day] = {"breadth_advancers": ratio_adv, "breadth_decliners": ratio_dec}
    return out


def test_percentile_omitted_below_min_history():
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS - 1  # 표본 1일 모자람
    by_date = _flat_history(n, 40.0, 60.0)
    today = "2026-06-01"
    by_date[today] = {"breadth_advancers": 70.0, "breadth_decliners": 30.0}

    ctx = build_mod._kr_breadth_context(by_date, today, 70.0)
    assert ctx == "", ctx


def test_percentile_shown_at_min_history():
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS  # 정확히 최소치
    by_date = _flat_history(n, 40.0, 60.0)
    today = "2026-06-01"
    by_date[today] = {"breadth_advancers": 70.0, "breadth_decliners": 30.0}

    ctx = build_mod._kr_breadth_context(by_date, today, 70.0)
    assert ctx != ""
    # 과거 표본 전부(40%)보다 오늘(70%)이 높으므로 "상위" 쪽.
    assert "상위" in ctx, ctx


def test_percentile_bucket_middle_and_bottom():
    # "중간": 오늘 값이 과거 표본의 중간 어딘가(25~75 백분위)에 있을 때.
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS
    by_date = {}
    d = date(2024, 1, 1)
    from datetime import timedelta
    for i in range(n):
        # 1%,2%,...,n% 고르게 분포.
        by_date[(d + timedelta(days=i)).isoformat()] = {
            "breadth_advancers": float(i + 1), "breadth_decliners": float(n - i)}
    today = "2026-06-01"
    by_date[today] = {"breadth_advancers": 50.0, "breadth_decliners": 50.0}
    ctx_mid = build_mod._kr_breadth_context(by_date, today, 50.0)
    assert "중간" in ctx_mid, ctx_mid

    by_date[today] = {"breadth_advancers": 5.0, "breadth_decliners": 95.0}
    ctx_low = build_mod._kr_breadth_context(by_date, today, 5.0)
    assert "하위" in ctx_low, ctx_low


# --- ③ 차단선 준수 (spec §2-4 / §5-3) ---------------------------------------

def test_cutoff_excludes_late_known_revision(settings):
    """같은 날의 사실이 차단선 이후에 알려진 개정치를 받아도(revision), 그
    리포트는 차단선 이전에 알려졌던 값만 봐야 한다 — `facts_as_of`가 지키는
    계약을 이 빌더가 실제로 상속하는지 실측한다."""
    conn = _open(settings)
    # 차단선 이전: 오른 종목이 더 많다(계약대로 60%).
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=6.0, dec=4.0, index_pct=-3.0,
              known_at=KNOWN_BEFORE)
    # 같은 날짜의 "정정"이 차단선 이후에 도착 — 완전히 다른 그림(20%)이지만
    # known_at이 차단선보다 늦으므로 절대 보이면 안 된다.
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=2.0, dec=8.0, index_pct=-30.0,
              known_at=KNOWN_AFTER)
    report = _report(conn)
    conn.close()

    kospi_line = next(ln for ln in report.breadth.splitlines() if ln.startswith("코스피"))
    assert "상승비율 60%" in kospi_line, kospi_line
    assert "오른 종목 6 / 내린 종목 4" in kospi_line, kospi_line
    assert "-30.00%" not in kospi_line and "20%" not in kospi_line


def test_cutoff_excludes_future_trading_day(settings):
    """차단선 이후에야 알려진 **다른 날짜**의 관측(예: 다음날 배치가 일찍
    돈 경우)이 "오늘"로 둔갑해서는 안 된다."""
    conn = _open(settings)
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=6.0, dec=4.0, index_pct=-3.0,
              known_at=KNOWN_BEFORE)
    # 2026-08-04치가 실수로 차단선 전에 알려졌다면 오늘로 잘못 읽힐 것이므로,
    # 일부러 차단선 **이후**에만 알려지게 심어 반증한다.
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-04", adv=999.0, dec=1.0, index_pct=50.0,
              known_at=KNOWN_AFTER)
    report = _report(conn)
    conn.close()

    kospi_line = next(ln for ln in report.breadth.splitlines() if ln.startswith("코스피"))
    assert "오른 종목 6 / 내린 종목 4" in kospi_line, kospi_line
    assert "999" not in kospi_line


def test_percentile_context_excludes_post_cutoff_history_day(settings):
    """차단선 이후에만 알려진 과거 날짜 하나가 백분위 표본에 섞이면 안 된다
    (후견지명 유출) — 그 하루가 섞이는 순간과 안 섞이는 순간을 직접 비교한다."""
    conn = _open(settings)
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS
    d = date(2024, 1, 1)
    from datetime import timedelta
    for i in range(n):
        day = (d + timedelta(days=i)).isoformat()
        _seed_day(conn, settings.raw_dir, "KOSPI", day, adv=40.0, dec=60.0, index_pct=0.1,
                  known_at=KNOWN_BEFORE)
    # 오늘.
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=90.0, dec=10.0, index_pct=5.0,
              known_at=KNOWN_BEFORE)
    report_without_late_day = _report(conn)

    # 표본을 하나 더 심되, known_at을 차단선 이후로 둔다 — 극단값(모두 상승)
    # 이라 섞이면 백분위가 눈에 띄게 바뀐다.
    late_day = (d + timedelta(days=n)).isoformat()
    _seed_day(conn, settings.raw_dir, "KOSPI", late_day, adv=100.0, dec=0.0, index_pct=9.0,
              known_at=KNOWN_AFTER)
    report_with_late_day_seeded_but_after_cutoff = _report(conn)
    conn.close()

    line_a = next(ln for ln in report_without_late_day.breadth.splitlines()
                  if ln.startswith("코스피"))
    line_b = next(ln for ln in report_with_late_day_seeded_but_after_cutoff.breadth.splitlines()
                  if ln.startswith("코스피"))
    assert line_a == line_b, (line_a, line_b)


# --- ④ 옛 리포트 JSON 하위호환 (spec §5-4) -----------------------------------

def test_old_report_json_without_kr_lines_still_loads():
    """이 서브태스크 이전에 만들어진 리포트 JSON은 `breadth`가 관측기업 한
    줄뿐이다. `Report.breadth`는 새 필드가 아니라 기존 `str` 필드를 그대로
    쓰므로(설계 판단, spec §3) 옛 JSON은 손댈 것 없이 그대로 읽혀야 한다."""
    legacy = model_mod.Report(
        report_type="close_delta", report_date="2026-07-28",
        breadth="관측기업 4/6개 상승 · 반도체·공급망 4/4",
    )
    restored = model_mod.Report.from_json(legacy.to_json())
    assert restored.breadth == "관측기업 4/6개 상승 · 반도체·공급망 4/4"
    assert "\n" not in restored.breadth
    render_html_mod.render_html(restored)  # must not raise
    render_md_mod.render_markdown(restored)  # must not raise


# --- ⑤ 한국 사실이 아예 없을 때 --------------------------------------------

def test_no_kr_lines_when_no_krx_facts(settings):
    conn = _open(settings)
    report = _report(conn)  # 아무 것도 seed하지 않음
    conn.close()

    assert "코스피" not in report.breadth
    assert "코스닥" not in report.breadth
    assert report.breadth == build_mod._breadth_line(report.sector_summary)


# --- ⑥ 렌더러: 줄바꿈이 살아 있어야 한다 -------------------------------------

def test_multiline_breadth_survives_both_renderers(settings):
    conn = _open(settings)
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=6.0, dec=4.0, index_pct=-3.0)
    _seed_day(conn, settings.raw_dir, "KOSDAQ", "2026-08-03", adv=7.0, dec=3.0, index_pct=2.0)
    report = _report(conn)
    conn.close()

    assert report.breadth.count("\n") == 2

    md = render_md_mod.render_markdown(report)
    # 원문 통째로 비교하지 않는다 — 마크다운은 여러 줄을 "하드 브레이크"(줄 끝
    # 공백 두 칸)로 잇기 때문에 `report.breadth`와 글자가 달라진다. 검사해야 할
    # 것은 "세 줄이 다 실렸는가"이지 "글자가 똑같은가"가 아니다.
    for line in report.breadth.split("\n"):
        assert line in md, line

    html_doc = render_html_mod.render_html(report)
    assert "코스피" in html_doc and "코스닥" in html_doc
    assert "<br>" in html_doc  # 세 줄이 한 <p>에 붙는다면 줄바꿈 마크업이 있어야 함


# --- ⑦ AI 해석 다이제스트에도 실린다(spec §3 항목3) --------------------------

def test_breadth_reaches_the_interpretation_digest(settings):
    from market_intel.interp import digest as digest_mod

    conn = _open(settings)
    _seed_day(conn, settings.raw_dir, "KOSPI", "2026-08-03", adv=6.0, dec=4.0, index_pct=-3.0)
    report = _report(conn)
    conn.close()

    text, _ = digest_mod.build(report)
    assert "코스피" in text
    assert "오른 종목 6 / 내린 종목 4" in text
