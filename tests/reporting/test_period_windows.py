"""spec 20260810-period-report — 주기별 변화량(§1①) · 변동성(§1②) ·
가독성 접기(§1③) 인수 테스트.

CEO 지적(§0): "주간·월간·분기 보고서도 변화량은 주간·월간·분기·연간이어야
하지 않나. 지금은 전일대비를 쓰다 보니 일일보고서랑 다른 게 없다." 근본
원인은 `_price_map`/`_macro_map`이 리포트 종류를 몰랐던 것이다 — 이 파일은
그 수정이 실제로 적용됐는지, 그리고 §2의 정직성 규칙(기간을 지어내지
않는다·표본 부족이면 말하지 않는다)이 지켜지는지를 검사한다.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from market_intel import db as db_mod
from market_intel import schedule as schedule_mod
from market_intel.models import FactCandidate
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod

from tests.reporting.conftest import price_fc, seed_fact

REPORT_DATE = date(2026, 8, 9)
KNOWN_BEFORE = "2026-08-08T22:00:00+00:00"  # weekly_review 차단선(08-09 08:30 KST) 이전


def _open(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _seed_daily_closes(conn, raw_dir, symbol, closes_asc, *, end_day: date,
                       known_at=KNOWN_BEFORE, market="KR", country="KR", unit="point"):
    """`closes_asc`(오래된 -> 최신)를 `end_day`로 끝나는 연속된 날짜에 심는다."""
    start = end_day - timedelta(days=len(closes_asc) - 1)
    for i, value in enumerate(closes_asc):
        day = start + timedelta(days=i)
        seed_fact(conn, raw_dir, "yfinance",
                  price_fc(symbol, market, country, f"{day.isoformat()}T20:00:00+00:00", value, unit),
                  known_at)


def _report(conn, report_type: str, report_date: date = REPORT_DATE):
    cutoff = cutoff_mod.cutoff_for(report_type, report_date)
    return build_mod.build_report(conn, report_type, report_date, cutoff)


def _kospi_row(report):
    return next(r for r in report.market_reaction if r.subject == "^KS11")


# --- ①-a 가격: 주간 리뷰는 5거래일 전과 비교한다 ----------------------------

def test_weekly_review_compares_five_trading_days_back_not_yesterday(settings):
    """CEO 예시와 같은 모양: 전일 변화는 거의 0인데 1주 변화는 크다."""
    conn = _open(settings)
    closes = [3000.0, 3000.0, 3000.0, 3000.0, 3000.0,  # 5거래일 전(=rs[5])까지
              2850.0, 2849.0, 2848.0, 2847.0, 2846.0]   # 최근 5거래일, 최신=2846.0
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", closes, end_day=date(2026, 8, 8))
    report = _report(conn, "weekly_review")
    conn.close()

    row = _kospi_row(report)
    weekly_delta = (2846.0 - 3000.0) / 3000.0 * 100
    assert row.delta_pct == pytest.approx(weekly_delta, abs=0.01)
    assert "1주 전 대비" in row.comparison, row.comparison
    # "그 주기 + 전일" 병기 금지(CEO 확정) — 전일 문구가 같이 나오면 안 된다.
    assert "전일대비" not in row.comparison, row.comparison

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    assert "1주 전 대비" in md and "1주 전 대비" in html_doc


def test_morning_report_still_compares_to_the_previous_trading_day(settings):
    """§5-4: 일간 리포트는 안 바뀐다 — 같은 데이터로 morning을 만들면 여전히
    직전 거래일 비교다."""
    conn = _open(settings)
    closes = [3000.0, 3000.0, 3000.0, 3000.0, 3000.0, 2850.0, 2849.0, 2848.0, 2847.0, 2846.0]
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", closes, end_day=date(2026, 8, 8))
    # morning 차단선은 report_date 07:15 KST — 08-09 아침에 08-08 종가를 본다.
    report = _report(conn, "morning")
    conn.close()

    row = _kospi_row(report)
    daily_delta = (2846.0 - 2847.0) / 2847.0 * 100
    assert row.delta_pct == pytest.approx(daily_delta, abs=0.01)
    assert "전일대비" in row.comparison, row.comparison
    assert "1주 전" not in row.comparison, row.comparison


def test_quarterly_review_falls_back_to_the_honest_gap_when_history_is_short(settings):
    """§2 규칙1: 63거래일 전 관측이 없으면(여기서는 10거래일치만 있다) 있는
    것 중 가장 가까운 것을 쓰되, "1분기 전"이라고 지어내지 않고 실제 간격을
    밝힌다."""
    conn = _open(settings)
    closes = [3000.0 + i for i in range(10)]  # 10거래일치뿐 — 63에 한참 못 미친다
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", closes, end_day=date(2026, 8, 8))
    report = _report(conn, "quarterly")
    conn.close()

    row = _kospi_row(report)
    assert "1분기 전" not in row.comparison, row.comparison
    assert "일 전 종가 대비" in row.comparison, row.comparison


# --- ①-b 거시: 발표 주기가 다른 지표도 정직하게 --------------------------

def _macro_fc(subject: str, event_at: str, value: float, unit: str = "%") -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{subject}:{event_at}", subject=subject, category="macro", metric="value",
        event_at=event_at, market="US", country="US", value_num=value, unit=unit,
        data_status="source_verified",
    )


def _macro_row(report, subject: str):
    return next(r for r in report.facts if r.subject == subject)


def test_weekly_review_macro_daily_series_gets_the_period_label(settings):
    """DGS10처럼 매일 관측되는 거시지표는 "1주 전" 라벨을 정직하게 쓸 수 있다."""
    conn = _open(settings)
    start = date(2026, 8, 8) - timedelta(days=9)
    for i in range(10):
        day = start + timedelta(days=i)
        seed_fact(conn, settings.raw_dir, "fred",
                  _macro_fc("DGS10", f"{day.isoformat()}T00:00:00+00:00", 4.0 + i * 0.01),
                  KNOWN_BEFORE)
    report = _report(conn, "weekly_review")
    conn.close()

    row = _macro_row(report, "DGS10")
    assert "1주 전 관측 대비" in row.comparison, row.comparison


def test_weekly_review_macro_monthly_series_does_not_lie_about_being_weekly(settings):
    """CPI는 한 달에 한 번 발표된다. 직전 관측이 실제로는 35일 전인데
    "1주 전 관측 대비"라고 쓰면 거짓말이다(§2 규칙1) — 실제 간격을 밝혀야
    한다."""
    conn = _open(settings)
    seed_fact(conn, settings.raw_dir, "fred",
              _macro_fc("CPIAUCSL", "2026-07-04T00:00:00+00:00", 310.0, unit="index"),
              KNOWN_BEFORE)
    seed_fact(conn, settings.raw_dir, "fred",
              _macro_fc("CPIAUCSL", "2026-08-08T00:00:00+00:00", 312.0, unit="index"),
              KNOWN_BEFORE)
    report = _report(conn, "weekly_review")
    conn.close()

    row = _macro_row(report, "CPIAUCSL")
    assert "1주 전" not in row.comparison, row.comparison
    assert "35일 전 관측 대비" in row.comparison, row.comparison


def test_daily_macro_comparison_wording_is_unchanged(settings):
    """일간 리포트 타입(주기 라벨 없음)은 기존 문구("직전 관측 대비")를
    간격과 무관하게 그대로 유지한다 — `test_macro_percentage_point.py`가
    이미 이 문구를 무조건 기대하고 있어 회귀시키면 안 된다."""
    conn = _open(settings)
    seed_fact(conn, settings.raw_dir, "fred",
              _macro_fc("CPIAUCSL", "2026-07-04T00:00:00+00:00", 310.0, unit="index"),
              KNOWN_BEFORE)
    seed_fact(conn, settings.raw_dir, "fred",
              _macro_fc("CPIAUCSL", "2026-08-08T00:00:00+00:00", 312.0, unit="index"),
              KNOWN_BEFORE)
    report = _report(conn, "close_delta", date(2026, 8, 9))
    conn.close()

    row = _macro_row(report, "CPIAUCSL")
    assert row.comparison.startswith("직전 관측 대비"), row.comparison


# --- ② 변동성 "평소 대비 몇 배" -------------------------------------------

def test_volatility_ratio_pure_function():
    """공식 확인: 그 기간 표준편차 ÷ 전체 기간 표준편차. 마지막 5개 구간이
    나머지보다 훨씬 크게 흔들리면 비율이 1보다 뚜렷이 커야 한다."""
    closes = [3000.0]
    for i in range(64):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    for i in range(5):
        closes.append(closes[-1] * (1.03 if i % 2 == 0 else 1 / 1.03))
    ratio = build_mod._volatility_ratio(closes, window=5)
    assert ratio is not None
    assert ratio > 1.5, ratio


def test_volatility_ratio_needs_a_baseline_of_60_trading_days():
    """§2 규칙2: 기준 표본이 60거래일 미만이면 None(문구를 아예 뺀다)."""
    closes = [3000.0 + i for i in range(30)]
    assert build_mod._volatility_ratio(closes, window=5) is None


def test_volatility_ratio_needs_at_least_three_period_observations():
    closes = [3000.0 + i for i in range(120)]
    assert build_mod._volatility_ratio(closes, window=1) is None  # 일간 lookback


def test_weekly_review_headline_shows_volatility_when_sample_is_enough(settings):
    conn = _open(settings)
    closes = [3000.0]
    for i in range(64):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    for i in range(5):
        closes.append(closes[-1] * (1.03 if i % 2 == 0 else 1 / 1.03))
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", closes, end_day=date(2026, 8, 8))
    report = _report(conn, "weekly_review")
    conn.close()
    assert "흔들림 평소의" in report.headline, report.headline


def test_daily_report_never_shows_volatility(settings):
    """일간 리포트는 lookback=1이라 기간 관측이 1개뿐 — 변동성 문구가 절대
    나오지 않는다(별도 분기 없이 표본 부족 규칙에서 자동으로 빠진다)."""
    conn = _open(settings)
    closes = [3000.0]
    for i in range(64):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    for i in range(5):
        closes.append(closes[-1] * (1.03 if i % 2 == 0 else 1 / 1.03))
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", closes, end_day=date(2026, 8, 8))
    report = _report(conn, "morning")
    conn.close()
    assert "흔들림" not in report.headline, report.headline


def test_short_history_does_not_claim_volatility(settings):
    """이 리포지토리 실측(10거래일치)처럼 표본이 짧으면 주간 리뷰라도
    변동성을 말하지 않는다."""
    conn = _open(settings)
    closes = [3000.0, 3000.0, 3000.0, 3000.0, 3000.0, 2850.0, 2849.0, 2848.0, 2847.0, 2846.0]
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", closes, end_day=date(2026, 8, 8))
    report = _report(conn, "weekly_review")
    conn.close()
    assert "흔들림" not in report.headline, report.headline


# --- ③-1 안 움직인 항목 접기 ------------------------------------------------

def test_unchanged_rows_are_folded_and_disclosed(settings):
    conn = _open(settings)
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", [2500.0, 2550.0],
                       end_day=date(2026, 8, 8))
    # 거의 안 움직인 종목 — 0.05% 문턱 아래.
    _seed_daily_closes(conn, settings.raw_dir, "^VIX", [20.000, 20.005],
                       end_day=date(2026, 8, 8), market="US", country="US", unit="point")
    report = _report(conn, "morning")
    conn.close()

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    assert "변화 없음 1건" in md, md
    assert "변화 없음 1건 펼치기" in html_doc, html_doc
    # 정보를 지우지 않는다(§2 규칙4) — 접힌 행도 여전히 어딘가에 있다.
    assert "VIX" in md and "VIX" in html_doc
    assert "<details>" in html_doc


def test_rows_with_unknown_change_are_not_folded(settings):
    """delta_pct가 None인(등락을 모르는) 행은 "안 움직였다"가 아니다 —
    접지 않는다."""
    conn = _open(settings)
    _seed_daily_closes(conn, settings.raw_dir, "^KS11", [2500.0],
                       end_day=date(2026, 8, 8))
    report = _report(conn, "morning")
    conn.close()
    md = render_md_mod.render_markdown(report)
    assert "변화 없음" not in md, md


# --- ③-2 부록 접기 (최근 일정 변경) ---------------------------------------

def _timetable_fc(subject: str, dates_csv: str) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"cal-{subject}", subject=subject, category="calendar",
        metric="scheduled_date", event_at="2026-01-01T00:00:00+00:00",
        market="US", country="US", value_text=dates_csv,
        comparison_basis=schedule_mod.YEAR_TIMETABLE_BASIS,
        data_status="source_verified", extra={"release_name": "Consumer Price Index"},
    )


def test_recent_schedule_changes_are_folded_but_upcoming_schedule_is_not(settings):
    conn = _open(settings)
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("fredrel:999", "2026-08-04"),
              known_at="2026-07-29T00:00:00+00:00")
    seed_fact(conn, settings.raw_dir, "fred_calendar", _timetable_fc("fredrel:999", "2026-08-06"),
              known_at="2026-08-01T00:00:00+00:00")
    report = _report(conn, "morning", date(2026, 8, 2))
    conn.close()

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    assert "최근 일정 변경" in md and "최근 일정 변경" in html_doc

    # md: "최근 일정 변경" 헤딩 다음에 `<details>`가 바로 나온다.
    after = md.split("## 최근 일정 변경", 1)[1]
    assert after.lstrip().startswith("<details>"), after[:120]
    upcoming = md.split("## 다가오는 일정", 1)[1].split("## 최근 일정 변경", 1)[0]
    assert "<details>" not in upcoming, upcoming

    assert '<h2>최근 일정 변경</h2><details>' in html_doc.replace("\n", ""), html_doc


# --- 표시 창이 월간 국면(§6.3) 판정을 건드리면 안 된다 -----------------------

def test_display_window_does_not_change_the_regime_inputs(settings):
    """`build.py` 상단 주석이 못박은 §6.3 규칙 — 국면 판정은 **직전 관측** 대비
    값을 쓰며 "synthetic 30-day lookback이 아니다".

    표시 창(월간=21거래일)을 그대로 판정에 흘리면 화면만 바꾸려던 변경이 판정을
    조용히 뒤집는다. 실측(심사 2026-08-10): `dxy_delta_pct`가 -0.21 -> -1.57로
    바뀌어 문턱(-1.0)을 새로 넘었고 **어떤 테스트도 잡지 못했다.**
    """
    conn = _open(settings)
    # DX-Y.NYB: 직전 관측 대비는 -0.2%(문턱 밖)지만, 21거래일 전 대비는 -5%(문턱 안).
    day = date(2026, 5, 1)
    price = 100.0
    for i in range(30):
        d = day + timedelta(days=i)
        seed_fact(conn, settings.raw_dir, "yfinance",
                  price_fc("DX-Y.NYB", "US", "US", f"{d.isoformat()}T20:00:00+00:00",
                           price, "point"),
                  KNOWN_BEFORE)
        price *= 0.998  # 매일 -0.2%씩 -> 21거래일이면 약 -4%
    report = build_mod.build_report(
        conn, "monthly", REPORT_DATE, cutoff_mod.cutoff_for("monthly", REPORT_DATE))
    conn.close()

    rule = report.meta.get("regime_rule") or ""
    assert "dxy_delta_pct=" in rule, rule
    dxy = float(rule.split("dxy_delta_pct=")[1].split()[0].rstrip("->").strip())
    # 직전 관측 대비(-0.2%)여야 한다. 21거래일 창(-4%)이 새면 문턱(-1.0)을 넘는다.
    assert -0.5 < dxy < 0, f"국면 입력에 표시 창이 샜다: dxy={dxy}"
