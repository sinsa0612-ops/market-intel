"""값이 이미 퍼센트인 지표는 변화를 **퍼센트포인트(%p)**로 말한다.

왜 이 파일이 있는가 (CEO 지적 2026-08-05): 한국 기준금리가 2.50 -> 2.75가
됐을 때 리포트가 `직전 관측 대비 +10.00%`라고 썼다. 산술은 맞지만(2.75/2.50),
금리를 그렇게 말하는 곳은 없고 읽는 사람은 금리가 10%대가 된 줄로 읽는다.

더 위험한 것은 실업률이었다 — 4.3 -> 4.2를 `-2.33%`로 쓰면 그럴듯해서 그냥
믿고 넘어간다(실제로는 0.1%p). 금리차(10Y-2Y)는 0을 넘나들 수 있어 상대
변화율 자체가 성립하지 않는다.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.universe import UNIVERSE
from conftest import seed_fact

from market_intel.models import FactCandidate

REPORT_DATE = date(2026, 8, 3)
KNOWN = "2026-08-03T06:00:00+00:00"


def macro_fact(subject: str, event_at: str, value: float, unit: str) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"{subject}:{event_at[:10]}", subject=subject, category="macro",
        metric="value", event_at=event_at, market="US", country="US",
        value_num=value, unit=unit, publisher="test",
    )


def _open(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _report(conn):
    cutoff = cutoff_mod.cutoff_for("close_delta", REPORT_DATE)
    return build_mod.build_report(conn, "close_delta", REPORT_DATE, cutoff)


def _seed_two(conn, raw_dir, subject: str, unit: str, prev: float, latest: float) -> None:
    seed_fact(conn, raw_dir, "fred",
              macro_fact(subject, "2026-06-30T00:00:00+00:00", prev, unit), KNOWN)
    seed_fact(conn, raw_dir, "fred",
              macro_fact(subject, "2026-07-31T00:00:00+00:00", latest, unit), KNOWN)


def _macro_row(report, subject: str):
    return next(r for r in report.facts if r.subject == subject)


# --- ① 퍼센트 값 지표는 %p ---------------------------------------------------

@pytest.mark.parametrize("unit", ["%", "연%", "percent"])
def test_percent_valued_metric_reports_percentage_points(settings, unit):
    """2.50 -> 2.75는 `+0.25%p`이지 `+10.00%`가 아니다."""
    conn = _open(settings)
    _seed_two(conn, settings.raw_dir, "TESTRATE", unit, 2.50, 2.75)
    report = _report(conn)
    conn.close()

    row = _macro_row(report, "TESTRATE")
    assert "+0.25%p" in row.comparison, row.comparison
    assert "10.00%" not in row.comparison, row.comparison
    assert row.delta_unit == "%p"
    assert row.delta_pct == pytest.approx(0.25)


def test_unemployment_small_move_is_not_inflated(settings):
    """실업률 4.3 -> 4.2는 `-0.10%p`다. `-2.33%`는 그럴듯해서 더 위험하다."""
    conn = _open(settings)
    _seed_two(conn, settings.raw_dir, "UNRATE", "%", 4.3, 4.2)
    report = _report(conn)
    conn.close()

    assert "-0.10%p" in _macro_row(report, "UNRATE").comparison


def test_spread_crossing_zero_is_expressible(settings):
    """금리차는 0을 지난다. 상대 변화율은 그 순간 무한대로 튀거나 부호가
    뒤집히지만, 퍼센트포인트는 언제나 말이 된다."""
    conn = _open(settings)
    _seed_two(conn, settings.raw_dir, "T10Y2Y", "%", -0.10, 0.15)
    report = _report(conn)
    conn.close()

    row = _macro_row(report, "T10Y2Y")
    assert "+0.25%p" in row.comparison, row.comparison


# --- ② 진짜 비율 변화는 그대로 % --------------------------------------------

def test_non_percent_metric_keeps_relative_percent(settings):
    """환율·지수·고용자수는 값이 퍼센트가 아니므로 상대 변화율이 맞다."""
    conn = _open(settings)
    _seed_two(conn, settings.raw_dir, "TESTFX", "원", 1000.0, 1010.0)
    report = _report(conn)
    conn.close()

    row = _macro_row(report, "TESTFX")
    assert "+1.00%" in row.comparison and "%p" not in row.comparison, row.comparison
    assert row.delta_unit == "%"


# --- ③ 화면(HTML 카드)도 같은 단위를 쓴다 ------------------------------------

def test_html_macro_card_shows_percentage_points(settings):
    """카드는 `comparison` 문자열이 아니라 `delta_pct` 값을 직접 찍는다 —
    문구만 고치고 이쪽을 두면 사이트에는 계속 `+10.00%`가 나간다."""
    conn = _open(settings)
    _seed_two(conn, settings.raw_dir, "TESTRATE", "%", 2.50, 2.75)
    report = _report(conn)
    conn.close()

    html = render_html_mod.render_html(report)
    assert "+0.25%p" in html
    assert "+10.00%" not in html


# --- ④ 단위 표기가 새로 생기면 알아채야 한다 ---------------------------------

def test_no_percent_valued_unit_is_left_out_of_the_set():
    """universe에 `unit`이 퍼센트 계열인데 `_PERCENT_VALUED_UNITS`에 없는 표기가
    생기면 그 종목은 조용히 예전처럼 상대 변화율로 나간다. 여기서 잡는다.

    (거시지표 쪽 단위는 provider가 소스 문자열을 그대로 싣기 때문에 코드로
    전수할 수 없다 — 그래서 universe만 검사하고, 소스 단위는 위 ①의
    parametrize가 실제로 쓰이는 세 표기를 고정한다.)"""
    suspicious = {
        (m["symbol"], m.get("unit"))
        for m in UNIVERSE
        if m.get("asset_type") == "rate"
        and (m.get("unit") or "") not in build_mod._PERCENT_VALUED_UNITS
    }
    assert not suspicious, f"금리 종목인데 단위가 %p 집합에 없다: {suspicious}"


# --- ⑤ 옛 리포트 JSON 하위호환 ----------------------------------------------

def test_old_report_json_without_delta_unit_still_loads():
    """`delta_unit`이 없던 옛 artefact 하나가 사이트 전체를 죽이면 안 된다."""
    from market_intel.reporting.model import Report

    old = Report(report_type="close_delta", report_date="2026-07-01").to_json()
    import json
    d = json.loads(old)
    d["facts"] = [{
        "label": "미국 실업률", "value": "4.20 %", "comparison": "직전 관측 대비 -2.33%",
        "source_url": "", "data_status": "source_verified", "known_at": KNOWN,
        "subject": "UNRATE", "metric": "value", "delta_pct": -2.33,
    }]
    restored = Report.from_json(json.dumps(d))
    assert restored.facts[0].delta_unit == "%"  # 기본값으로 읽힌다
