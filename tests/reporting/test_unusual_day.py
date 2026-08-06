"""서브태스크 인수 테스트 — "오늘 유별난 것" 블록(spec
`_org/20260806-report-visual/spec.md` §1①). CEO가 세 번째 같은 말을 했다:
"시각화로 변화에 집중하게 해달라." §2 정직성 규칙(재량 없음) 5가지를 코드로
고정한다.

fixture는 `db.insert_raw_snapshot` + `db.upsert_fact`(conftest `seed_fact`)를
지난다 — `test_kr_breadth.py`와 같은 이유: PIT 읽기 경로를 우회한 fixture로는
차단선 준수를 증명할 수 없다.
"""
from __future__ import annotations

import pytest
import pathlib
import json
from datetime import date, timedelta

from market_intel import db as db_mod
from market_intel.models import FactCandidate
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import model as model_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod

from conftest import price_fc, seed_fact

REPORT_DATE = date(2026, 8, 6)
KNOWN_BEFORE = "2026-08-06T06:30:00+00:00"  # close_delta 차단선(07:15Z) 이전


def breadth_fc(subject: str, metric: str, event_at: str, value: float) -> FactCandidate:
    return FactCandidate(
        raw_ref=f"krx:{subject}:{metric}:{event_at}", subject=subject, category="breadth",
        metric=metric, event_at=event_at, market="KR", country="KR",
        value_num=value, unit="issues" if metric.startswith("breadth_") else "%",
        data_status="source_verified",
    )


def _seed_day(conn, raw_dir, subject: str, day: str, adv: float, dec: float,
              known_at: str = KNOWN_BEFORE) -> None:
    event_at = f"{day}T06:30:00+00:00"
    seed_fact(conn, raw_dir, "krx", breadth_fc(subject, "breadth_advancers", event_at, adv), known_at)
    seed_fact(conn, raw_dir, "krx", breadth_fc(subject, "breadth_decliners", event_at, dec), known_at)


def _seed_flat_history(conn, raw_dir, subject: str, n: int, adv: float, dec: float,
                        *, start_year=2024, start_month=1) -> None:
    d = date(start_year, start_month, 1)
    for i in range(n):
        _seed_day(conn, raw_dir, subject, (d + timedelta(days=i)).isoformat(), adv, dec)


def _open(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _report(conn, report_type="close_delta"):
    cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
    return build_mod.build_report(conn, report_type, REPORT_DATE, cutoff)


def _headers(md: str) -> list[str]:
    return [line[3:] for line in md.splitlines() if line.startswith("## ")]


# --- ① 극단인 날: 백분위 문구 + 추이 그래프 ----------------------------------

def test_extreme_day_shows_percentile_and_marks_notable(settings):
    conn = _open(settings)
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS
    _seed_flat_history(conn, settings.raw_dir, "KOSPI", n, adv=40.0, dec=60.0)
    _seed_day(conn, settings.raw_dir, "KOSPI", REPORT_DATE.isoformat(), adv=90.0, dec=10.0)
    report = _report(conn)
    conn.close()

    u = report.unusual_day
    assert u.is_notable is True
    assert "상위" in u.headline, u.headline
    assert "코스피" in u.headline and "상승비율 90%" in u.headline
    # 분모를 지어내지 않는다 — "N종목 중"은 코스피 전체 종목 수(보합·거래없음 포함)와
    # 어긋나 독자가 계산하면 아래 상승비율과 안 맞는다(심사 2026-08-06).
    assert "오른 종목 90 / 내린 종목 10" in u.headline
    assert u.trend_label == "코스피 상승비율"
    assert len(u.trend_series) >= n
    assert u.trend_series[-1] == 90.0  # 마지막 점 = 오늘


def test_extreme_day_block_precedes_market_one_liner(settings):
    """spec §1: "## 시장 한 줄" **위**에 새 블록이 와야 한다."""
    conn = _open(settings)
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS
    _seed_flat_history(conn, settings.raw_dir, "KOSPI", n, adv=40.0, dec=60.0)
    _seed_day(conn, settings.raw_dir, "KOSPI", REPORT_DATE.isoformat(), adv=95.0, dec=5.0)
    report = _report(conn)
    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    conn.close()

    headers = _headers(md)
    assert "오늘 유별난 것" in headers
    assert headers.index("오늘 유별난 것") < headers.index("시장 한 줄")
    assert "<h2>오늘 유별난 것</h2>" in html_doc
    assert html_doc.index("<h2>오늘 유별난 것</h2>") < html_doc.index("<h2>시장 한 줄</h2>")
    # 추이 그래프 — 인라인 SVG, 오늘 점 강조.
    assert '<svg class="trend' in html_doc
    assert 'class="today"' in html_doc
    assert "<script" not in html_doc  # spec §3: CDN/스크립트 금지


# --- ② 유별나지 않은 날: 드라마를 지어내지 않는다(§2-1) ----------------------

def test_unremarkable_day_does_not_claim_a_percentile(settings):
    conn = _open(settings)
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS
    # 표본을 고르게 분포시켜(1%~n%) 오늘 50%가 정말 "중간"이 되게 한다.
    for i in range(n):
        d = date(2024, 1, 1) + timedelta(days=i)
        _seed_day(conn, settings.raw_dir, "KOSPI", d.isoformat(), adv=float(i + 1), dec=float(n - i))
    _seed_day(conn, settings.raw_dir, "KOSPI", REPORT_DATE.isoformat(), adv=50.0, dec=50.0)
    report = _report(conn)
    conn.close()

    u = report.unusual_day
    assert u.is_notable is False
    assert "유별난 날이 아닙니다" in u.headline
    assert "상위" not in u.headline and "하위" not in u.headline
    # 그래도 "무슨 일인가"는 사실대로 실린다.
    assert "코스피 오른 종목 50 / 내린 종목 50" in u.headline
    # 극단이 아니어도 추이 그래프(사실의 시각화)는 그대로 나온다 —
    # §2-1이 금지하는 것은 "상위 N%" 주장이지 그래프 자체가 아니다.
    assert len(u.trend_series) >= n


# --- ③ 표본 부족: 백분위 자체를 말하지 않는다(§2-2) --------------------------

def test_thin_sample_does_not_claim_percentile_either(settings):
    conn = _open(settings)
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS - 1  # 최소치보다 하루 모자람
    _seed_flat_history(conn, settings.raw_dir, "KOSPI", n, adv=90.0, dec=10.0)
    _seed_day(conn, settings.raw_dir, "KOSPI", REPORT_DATE.isoformat(), adv=90.0, dec=10.0)
    report = _report(conn)
    conn.close()

    u = report.unusual_day
    assert u.is_notable is False
    assert "표본이 부족" in u.headline
    assert "상위" not in u.headline and "하위" not in u.headline
    assert "유별난 날이 아닙니다" not in u.headline  # 안 유별난 게 아니라 "모른다"


# --- ④ 두 시장이 다 극단이면 둘 다 문구에 실린다 -----------------------------

def test_both_markets_extreme_are_both_named(settings):
    conn = _open(settings)
    n = build_mod._KR_BREADTH_MIN_HISTORY_DAYS
    _seed_flat_history(conn, settings.raw_dir, "KOSPI", n, adv=40.0, dec=60.0)
    _seed_flat_history(conn, settings.raw_dir, "KOSDAQ", n, adv=40.0, dec=60.0)
    _seed_day(conn, settings.raw_dir, "KOSPI", REPORT_DATE.isoformat(), adv=89.0, dec=11.0)
    _seed_day(conn, settings.raw_dir, "KOSDAQ", REPORT_DATE.isoformat(), adv=90.0, dec=10.0)
    report = _report(conn)
    conn.close()

    u = report.unusual_day
    assert u.is_notable is True
    assert "코스피" in u.headline and "코스닥" in u.headline
    assert u.headline.count("상위") == 2


# --- ⑤ 가장 크게 움직인 것 5개 -----------------------------------------------

def test_top_movers_ranked_by_absolute_move_capped_at_five(settings):
    conn = _open(settings)
    movers = [
        ("^KS11", "KR", "KR", "point", 4.0),
        ("^GSPC", "US", "US", "point", -1.0),
        ("GC=F", "US", "US", "USD", 5.19),
        ("^VIX", "US", "US", "point", -4.18),
        ("000660.KS", "KR", "KR", "KRW", 5.77),
        ("LLY", "US", "US", "USD", 4.86),
        ("MSFT", "US", "US", "USD", 0.2),
    ]
    for i, (symbol, market, country, unit, pct) in enumerate(movers):
        prev = 100.0
        latest = prev * (1 + pct / 100)
        seed_fact(conn, settings.raw_dir, "yfinance",
                  price_fc(symbol, market, country, "2026-08-05T20:00:00+00:00", prev, unit),
                  KNOWN_BEFORE)
        seed_fact(conn, settings.raw_dir, "yfinance",
                  price_fc(symbol, market, country, "2026-08-06T06:00:00+00:00", latest, unit),
                  KNOWN_BEFORE)
    report = _report(conn)
    conn.close()

    u = report.unusual_day
    assert len(u.top_movers) == 5
    deltas = [abs(r.delta_pct) for r in u.top_movers]
    assert deltas == sorted(deltas, reverse=True)
    assert u.top_movers[0].subject == "000660.KS"  # +5.77%가 제일 크다
    subjects = {r.subject for r in u.top_movers}
    assert "MSFT" not in subjects  # +0.2%는 5위 안에 못 든다


# --- ⑥ 옛 리포트 JSON 하위호환(spec §5-5) -----------------------------------

def test_old_report_json_without_unusual_day_still_loads():
    """**진짜 옛 아티팩트**를 읽는다 — `Report(...).to_json()` 왕복은 새 키가
    항상 들어가므로 하위호환을 하나도 검사하지 못한다(심사 2026-08-06:
    `d.get("unusual_day")`를 `d["unusual_day"]`로 바꿔도 전부 초록이었다).

    `reports/morning/2026-07-28.json`은 schema 2a.1로 발행된 실제 파일이고
    `unusual_day` 키가 없다. 이 파일이 사라지면 테스트는 건너뛴다 — 발행본을
    테스트가 붙잡아 두지는 않되, 있으면 반드시 검사한다."""
    legacy_path = (pathlib.Path(__file__).resolve().parents[2]
                   / "reports" / "morning" / "2026-07-28.json")
    if not legacy_path.exists():  # pragma: no cover - 발행본이 정리된 경우
        pytest.skip("옛 발행본이 없다")
    raw = legacy_path.read_text(encoding="utf-8")
    assert "unusual_day" not in json.loads(raw), "픽스처가 더 이상 '옛' 파일이 아니다"
    restored = model_mod.Report.from_json(raw)
    assert restored.unusual_day.is_notable is False
    assert restored.unusual_day.headline == ""
    assert restored.unusual_day.trend_series == []
    assert restored.unusual_day.top_movers == []
    md = render_md_mod.render_markdown(restored)  # must not raise
    html_doc = render_html_mod.render_html(restored)  # must not raise
    # 보여줄 내용이 없으면 헤딩 자체를 내지 않는다(§2-1과 같은 태도).
    assert "오늘 유별난 것" not in md
    assert "오늘 유별난 것" not in html_doc


# --- ⑦ 한국 시장 폭이 아예 없는 날: 이벤트를 지어내지 않는다 ------------------

def test_no_block_when_no_kr_breadth_and_no_movers(settings):
    conn = _open(settings)
    report = _report(conn)  # 아무 것도 seed하지 않음
    conn.close()

    u = report.unusual_day
    assert u.headline == ""
    assert u.is_notable is False
    md = render_md_mod.render_markdown(report)
    assert "오늘 유별난 것" not in md
