"""가독성 요구 인수 테스트 — 등락 색상·화살표 / 업종 묶기·시장 폭 / 스파크라인.

CEO 요구: "올랐는지 내렸는지 한눈에 안 보인다. 색을 넣고 그래프를 붙여달라" +
"명세는 전체 시장을 보게 돼 있는데 왜 반도체만 보이나"(= 업종 축이 코드에
없었다). 명세 §12(Core 16 업종 구분표)·§6.1(업종/시장 폭)이 근거다.

모든 fixture는 `db.insert_raw_snapshot` + `db.upsert_fact`(conftest
`seed_fact`)를 지난다 — PIT 읽기 경로를 우회한 fixture로는 차단선 준수를
증명할 수 없기 때문이다.
"""
from __future__ import annotations

from datetime import date, timedelta

from market_intel import db as db_mod
from market_intel import universe as universe_mod
from market_intel.models import FactCandidate
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import model as model_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod

from conftest import price_fc, seed_fact

REPORT_DATE = date(2026, 8, 1)
KNOWN_BEFORE = "2026-07-31T22:00:00+00:00"  # morning 차단선(07-31T22:15Z) 이전

# 명세 §12 표 그대로. 이 상수가 곧 인수 기준이다 — 코드가 스스로를 채점하지
# 않도록 테스트 쪽에 사본을 둔다.
SPEC_SECTORS = {
    "MSFT": "플랫폼·AI 수익화", "GOOGL": "플랫폼·AI 수익화",
    "AMZN": "플랫폼·AI 수익화", "META": "플랫폼·AI 수익화",
    "NVDA": "반도체·공급망", "TSM": "반도체·공급망",
    "005930.KS": "반도체·공급망", "000660.KS": "반도체·공급망",
    "JPM": "금융·신용", "105560.KS": "금융·신용",
    "XOM": "전력·산업", "AEP": "전력·산업", "CAT": "전력·산업",
    "WMT": "소비·수출", "005380.KS": "소비·수출",
    "LLY": "헬스케어",
    # 비어 있던 두 축 (CEO 확정 2026-08-02, 구현 2026-08-03)
    "005490.KS": "소재·철강",
    "EQIX": "부동산·데이터센터",
}
# 명세 표 **밖에서** CEO 결정으로 더한 기업. 명세 목록과 따로 둔다 — 섞으면
# "명세가 정한 것"과 "우리가 더한 것"을 나중에 구별할 수 없다(업종 지수에서 같은
# 이유로 이미 분리했다, test_sector_indices.py).
#
# 2026-08-14: 미국 메모리가 관측군에 하나도 없어서 `ai_semi_2`·`ai_semi_3`(둘 다
# 한·미 메모리의 차이를 주장한다)이 비교 대상 없이 SOX와만 견주고 있었다. SOX에는
# 로직(엔비디아·TSMC)이 섞여 "메모리만의 재평가"를 가려낼 수 없다.
ADDED_SECTORS = {
    "MU": "반도체·공급망",
    "SNDK": "반도체·공급망",
}


def _seed_series(conn, raw_dir, symbol, closes, *, market="US", country="US",
                 unit="USD", known_at=KNOWN_BEFORE, start_day=27):
    """`closes`를 2026-07-{start_day}부터 하루 간격 종가로 심는다."""
    for i, value in enumerate(closes):
        event_at = f"2026-07-{start_day + i:02d}T20:00:00+00:00"
        seed_fact(conn, raw_dir, "yfinance",
                  price_fc(symbol, market, country, event_at, value, unit), known_at)


def _two_day(conn, raw_dir, symbol, prev, latest, **kw):
    _seed_series(conn, raw_dir, symbol, [prev, latest], start_day=30, **kw)


def _report(conn, report_type="morning"):
    cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
    return build_mod.build_report(conn, report_type, REPORT_DATE, cutoff)


def _open(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _row_chunk(html_doc: str, label: str) -> str:
    """표의 `<tr>` 한 조각. 히어로 카드에도 같은 이름이 나오므로 `<td`가 있는
    조각만 고른다."""
    chunks = [c for c in html_doc.split("<tr>") if label in c and "<td" in c]
    assert chunks, f"{label} 행을 표에서 찾지 못했다"
    return chunks[0]


# --- ① 업종 정보 (명세 §12) ------------------------------------------------

def test_universe_sector_matches_spec_table():
    """관측 기업은 §12의 축에 정확히 하나씩 배정되고, 지수·환율·원자재는
    기업이 아니므로 sector가 없다(None).

    축이 6개에서 8개가 된 것은 §12 표를 벗어난 CEO 확정 사항이다(2026-08-02):
    GICS 11업종에 대보면 소재·부동산이 0개 기업이라 그 두 업종에는 깊이 층
    (분기 재무·가설)이 통째로 없었다. 근거는 universe.py의 SECTORS 주석."""
    expected_sectors = {**SPEC_SECTORS, **ADDED_SECTORS}
    by_symbol = {m["symbol"]: m for m in universe_mod.UNIVERSE}
    for symbol, sector in expected_sectors.items():
        assert by_symbol[symbol]["sector"] == sector, symbol
    for m in universe_mod.UNIVERSE:
        if not m["core16"]:
            assert m["sector"] is None, f"{m['symbol']}는 Core 16이 아닌데 업종이 붙었다"
    assert list(universe_mod.SECTORS) == [
        "플랫폼·AI 수익화", "반도체·공급망", "금융·신용", "전력·산업", "소비·수출", "헬스케어",
        "소재·철강", "부동산·데이터센터",
    ]
    assert all(m["sector"] for m in universe_mod.CORE16), "축 없는 관측 기업이 있다"
    assert universe_mod.SECTOR_BY_SYMBOL == expected_sectors


# --- ② 업종 묶어보기 + 시장 폭 ---------------------------------------------

def test_sector_summary_counts_up_and_down(settings):
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "NVDA", 100.0, 105.0)      # +5.0
    _two_day(conn, settings.raw_dir, "TSM", 100.0, 101.0)       # +1.0
    _two_day(conn, settings.raw_dir, "WMT", 100.0, 98.0)        # -2.0
    _two_day(conn, settings.raw_dir, "005380.KS", 100.0, 99.0,
             market="KR", country="KR", unit="KRW")             # -1.0
    report = _report(conn)
    conn.close()

    by_sector = {s.sector: s for s in report.sector_summary}
    assert list(by_sector) == list(universe_mod.SECTORS), "6축이 모두, 순서대로 나와야 한다"

    semi = by_sector["반도체·공급망"]
    assert (semi.up, semi.down, semi.total) == (2, 0, 2)
    consumer = by_sector["소비·수출"]
    assert (consumer.up, consumer.down, consumer.total) == (0, 2, 2)
    health = by_sector["헬스케어"]
    assert health.total == 0 and health.median_pct is None, "관측 없는 축은 숫자를 지어내지 않는다"


def test_sector_representative_value_resists_one_outlier(settings):
    """평균이면 한 종목이 축 전체를 끌고 간다. 반도체 4종목이 +1/+1/+1/+30일
    때 평균은 +8.25%로 '반도체 전면 급등'처럼 보이지만 실제로 뛴 건 하나뿐이다.
    대표값은 중앙값이어야 한다."""
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "NVDA", 100.0, 101.0)
    _two_day(conn, settings.raw_dir, "TSM", 100.0, 101.0)
    _two_day(conn, settings.raw_dir, "005930.KS", 100.0, 101.0, market="KR", country="KR", unit="KRW")
    _two_day(conn, settings.raw_dir, "000660.KS", 100.0, 130.0, market="KR", country="KR", unit="KRW")
    report = _report(conn)
    conn.close()

    semi = {s.sector: s for s in report.sector_summary}["반도체·공급망"]
    assert semi.total == 4
    assert semi.median_pct == 1.0, f"중앙값이어야 한다(평균이면 8.25): {semi.median_pct}"


def test_small_sample_sectors_are_flagged(settings):
    """종목이 1~2개뿐인 축은 '업종 신호'가 아니라 개별 종목의 움직임이다."""
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "LLY", 100.0, 110.0)
    _two_day(conn, settings.raw_dir, "NVDA", 100.0, 101.0)
    _two_day(conn, settings.raw_dir, "TSM", 100.0, 101.0)
    _two_day(conn, settings.raw_dir, "005930.KS", 100.0, 101.0, market="KR", country="KR", unit="KRW")
    report = _report(conn)
    conn.close()

    by_sector = {s.sector: s for s in report.sector_summary}
    assert by_sector["헬스케어"].small_sample is True
    assert by_sector["반도체·공급망"].small_sample is False

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    assert "표본 적음" in md and "표본 적음" in html_doc
    assert render_md_mod.SECTOR_NOTE in md
    assert "중앙값" in html_doc


def test_breadth_line_answers_whole_market_or_narrow(settings):
    """§6.1 '시장 전체인가, 대형주 쏠림인가'에 한 줄로 답한다."""
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "NVDA", 100.0, 105.0)
    _two_day(conn, settings.raw_dir, "TSM", 100.0, 101.0)
    _two_day(conn, settings.raw_dir, "005930.KS", 100.0, 101.0, market="KR", country="KR", unit="KRW")
    _two_day(conn, settings.raw_dir, "000660.KS", 100.0, 101.0, market="KR", country="KR", unit="KRW")
    _two_day(conn, settings.raw_dir, "WMT", 100.0, 98.0)
    _two_day(conn, settings.raw_dir, "005380.KS", 100.0, 99.0, market="KR", country="KR", unit="KRW")
    report = _report(conn)
    conn.close()

    assert "관측기업 4/6개 상승" in report.breadth, report.breadth
    assert "반도체·공급망 4/4" in report.breadth, report.breadth
    assert "소비·수출 0/2" in report.breadth, report.breadth
    assert "헬스케어 관측 없음" in report.breadth, report.breadth

    assert report.breadth in render_md_mod.render_markdown(report)
    assert "관측기업 4/6개 상승" in render_html_mod.render_html(report)


def test_breadth_states_absence_when_nothing_observed(settings):
    conn = _open(settings)
    report = _report(conn)
    conn.close()
    assert "관측 없음" in report.breadth, report.breadth


# --- ③ 색상 + 화살표 --------------------------------------------------------

def test_direction_arrows_in_markdown(settings):
    """마크다운(Obsidian)에는 색을 못 넣으므로 ▲/▼가 방향을 진다."""
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "^KS11", 2500.0, 2550.0, market="KR", country="KR", unit="point")
    _two_day(conn, settings.raw_dir, "^VIX", 20.0, 18.0, unit="point")
    md = render_md_mod.render_markdown(_report(conn))
    conn.close()

    kospi = [ln for ln in md.splitlines() if ln.startswith("|") and "KOSPI" in ln]
    vix = [ln for ln in md.splitlines() if ln.startswith("|") and "VIX" in ln]
    assert kospi and "▲" in kospi[0] and "▼" not in kospi[0], kospi
    assert vix and "▼" in vix[0] and "▲" not in vix[0], vix


def test_direction_colour_and_arrow_in_html(settings):
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "^KS11", 2500.0, 2550.0, market="KR", country="KR", unit="point")
    _two_day(conn, settings.raw_dir, "^VIX", 20.0, 18.0, unit="point")
    html_doc = render_html_mod.render_html(_report(conn))
    conn.close()

    kospi = _row_chunk(html_doc, "KOSPI")
    vix = _row_chunk(html_doc, "VIX")
    assert 'class="chg up"' in kospi and "▲" in kospi, kospi
    assert 'class="chg down"' in vix and "▼" in vix, vix
    assert "down" not in kospi.split("<td")[3], "상승 행에 하락 색이 붙었다"
    assert "up" not in vix.split("<td")[3], "하락 행에 상승 색이 붙었다"


def test_colour_is_never_the_only_channel(settings):
    """흑백 인쇄·색각이상에서도 읽혀야 한다 — 색이 붙은 셀은 **하나도 빠짐없이**
    같은 뜻의 기호를 함께 달고 있어야 한다."""
    import re

    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "^KS11", 2500.0, 2550.0, market="KR", country="KR", unit="point")
    _two_day(conn, settings.raw_dir, "^VIX", 20.0, 18.0, unit="point")
    _two_day(conn, settings.raw_dir, "NVDA", 100.0, 105.0)
    html_doc = render_html_mod.render_html(_report(conn))
    conn.close()

    cells = re.findall(r'<td class="chg (up|down|flat)">(.*?)</td>', html_doc, re.S)
    assert cells, "등락 셀이 하나도 없다"
    marks = {"up": "▲", "down": "▼", "flat": "―"}
    for cls, inner in cells:
        assert marks[cls] in inner, f"색만 있고 기호가 없는 셀: {cls} {inner!r}"
    assert {c for c, _ in cells} >= {"up", "down"}


def test_legend_says_colour_is_direction_not_good_or_bad(settings):
    """환율 상승 = 원화 약세, VIX 하락 = 시장 안정. 색은 좋고 나쁨이 아니다."""
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "KRW=X", 1400.0, 1440.0, unit="KRW")
    report = _report(conn)
    conn.close()

    html_doc = render_html_mod.render_html(report)
    md = render_md_mod.render_markdown(report)
    assert "빨강" in html_doc and "파랑" in html_doc, "국내 관례 범례가 화면에 없다"
    assert "좋고 나쁨" in html_doc and "좋고 나쁨" in md
    assert "▲" in md and "▼" in md, "마크다운 범례도 두 기호를 모두 설명해야 한다"


def test_rows_without_a_measured_change_get_no_arrow(settings):
    """직전 값이 없어 등락을 모르는 행에 방향을 지어내지 않는다."""
    conn = _open(settings)
    _seed_series(conn, settings.raw_dir, "^KS11", [2500.0],
                 market="KR", country="KR", unit="point", start_day=31)
    report = _report(conn)
    conn.close()

    row = [r for r in report.market_reaction if r.subject == "^KS11"][0]
    assert row.delta_pct is None
    chunk = _row_chunk(render_html_mod.render_html(report), "KOSPI")
    assert "▲" not in chunk and "▼" not in chunk and "chg" not in chunk, chunk


def test_data_status_badge_survives_the_colour_work(settings):
    """색을 입히다 낡은 값 표시(부분 확인 + 기준일)가 묻히면 안 된다."""
    conn = _open(settings)
    for event_at, value in (("2026-07-30T20:00:00+00:00", 100.0), ("2026-07-31T20:00:00+00:00", 105.0)):
        fc = FactCandidate(
            raw_ref=f"nvda:{event_at}", subject="NVDA", category="price", metric="price_close",
            event_at=event_at, market="US", country="US", value_num=value, unit="USD",
            data_status="partial",
        )
        seed_fact(conn, settings.raw_dir, "yfinance", fc, KNOWN_BEFORE)
    report = _report(conn)
    conn.close()

    # `_row_chunk`(파일 위쪽 정의)의 "<td가 있는 조각만 고른다"는 규칙을 그대로
    # 쓴다 — spec 20260806-report-visual §1①-4("가장 크게 움직인 것 5개")가
    # 생기면서 NVIDIA가 "오늘 유별난 것" 블록에도 한 번 더 나오는데, 그 자리는
    # `<td>`도 상태 배지도 없는 별개 요약 줄이다. 걸러내지 않으면 진짜 사실
    # 표 행 대신 그 요약 줄이 골라진다.
    html_rows = [c for c in render_html_mod.render_html(report).split("<tr>")
                 if "NVIDIA" in c and "<td" in c]
    assert html_rows and "부분 확인" in html_rows[0] and 'class="status-warn"' in html_rows[0]
    assert 'class="chg up"' in html_rows[0], "배지와 등락색이 함께 살아 있어야 한다"
    # 마크다운도 같은 이유로 "오늘 유별난 것" 요약 표(| 종목 | 등락 |, 2칸)와
    # 사실 표(| 항목 | 수치 | 비교 | 원자료 |, 4칸)를 구별해야 한다.
    md_rows = [ln for ln in render_md_mod.render_markdown(report).splitlines()
               if ln.startswith("|") and "NVIDIA" in ln and ln.count("|") >= 5]
    assert md_rows and "부분 확인" in md_rows[0] and "▲" in md_rows[0]


# --- ④ 추이 그래프 (스파크라인) --------------------------------------------

def test_sparkline_is_inline_svg_without_scripts_or_cdn(settings):
    conn = _open(settings)
    _seed_series(conn, settings.raw_dir, "^KS11", [2400.0, 2450.0, 2430.0, 2500.0, 2550.0],
                 market="KR", country="KR", unit="point")
    report = _report(conn)
    conn.close()

    row = [r for r in report.market_reaction if r.subject == "^KS11"][0]
    assert row.series == [2400.0, 2450.0, 2430.0, 2500.0, 2550.0], "오래된 값 -> 최신 값 순"

    html_doc = render_html_mod.render_html(report)
    assert "<svg" in html_doc and "<path" in html_doc
    assert "<script" not in html_doc.lower()
    assert "http://" not in html_doc.replace("https://", "")
    assert "cdn." not in html_doc
    assert 'class="spark up"' in html_doc


def test_sparkline_never_uses_data_from_after_the_cutoff(settings):
    """PIT: 차단선 이후에 알려진 종가는 선에도 좌표에도 들어가면 안 된다."""
    conn = _open(settings)
    cutoff = cutoff_mod.cutoff_for("morning", REPORT_DATE)
    _seed_series(conn, settings.raw_dir, "^KS11", [2400.0, 2450.0, 2430.0, 2500.0],
                 market="KR", country="KR", unit="point")
    leaked = 9999.0
    seed_fact(conn, settings.raw_dir, "yfinance",
              price_fc("^KS11", "KR", "KR", "2026-08-01T06:30:00+00:00", leaked, "point"),
              db_mod.iso_utc(cutoff + timedelta(minutes=1)))

    report = build_mod.build_report(conn, "morning", REPORT_DATE, cutoff)
    conn.close()

    row = [r for r in report.market_reaction if r.subject == "^KS11"][0]
    assert leaked not in row.series, "차단선 이후 종가가 시계열에 들어왔다"
    assert row.series == [2400.0, 2450.0, 2430.0, 2500.0]
    assert len(row.series) == 4

    # 좌표로도 새면 안 된다: 선은 정확히 4개 점을 잇고, 누출값(9999)이 만들
    # y축 스케일(최고점이 3.0에 붙고 나머지가 바닥에 깔림)이 나오면 안 된다.
    import re

    svg = render_html_mod.sparkline_svg(row.series, "up")
    line = re.search(r'class="line" d="M([^"]+)"', svg).group(1)
    ys = [float(p.split(",")[1]) for p in line.split(" L")]
    assert len(ys) == 4, svg
    assert min(ys) == render_html_mod.SPARK_TOP and max(ys) == render_html_mod.SPARK_BOTTOM, ys
    assert ys[-1] == render_html_mod.SPARK_TOP, "최신값(2500)이 이 4개 중 최고여야 한다"


def test_sparkline_is_omitted_for_single_observation_series(settings):
    """거시지표는 관측이 1개씩이라 그래프가 안 나온다 — 억지로 그리지 않는다."""
    conn = _open(settings)
    macro = FactCandidate(
        raw_ref="cpi", subject="CPIAUCSL", category="macro", metric="value",
        event_at="2026-07-01T00:00:00+00:00", market="US", country="US",
        value_num=310.0, unit="index", data_status="source_verified",
    )
    seed_fact(conn, settings.raw_dir, "fred", macro, KNOWN_BEFORE)
    report = _report(conn)
    conn.close()

    cpi = [r for r in report.facts if r.subject == "CPIAUCSL"][0]
    assert cpi.series == []
    assert render_html_mod.sparkline_svg([], "up") == ""
    assert render_html_mod.sparkline_svg([1.0], "up") == ""

    html_doc = render_html_mod.render_html(report)
    facts_table = html_doc.split("핵심 사실")[1].split("</section>")[0]
    assert "추이" not in facts_table, "그릴 게 없는 표에 빈 추이 칸을 만들지 않는다"
    assert "<svg" not in facts_table


def test_sparkline_length_is_capped(settings):
    conn = _open(settings)
    _seed_series(conn, settings.raw_dir, "^KS11", [100.0 + i for i in range(10)],
                 market="KR", country="KR", unit="point", start_day=20)
    report = _report(conn)
    conn.close()
    row = [r for r in report.market_reaction if r.subject == "^KS11"][0]
    assert len(row.series) == build_mod.SPARKLINE_POINTS
    assert row.series[-1] == 109.0, "최근 구간을 남겨야 한다"


def test_flat_series_does_not_divide_by_zero(settings):
    svg = render_html_mod.sparkline_svg([100.0, 100.0, 100.0], "flat")
    assert "<path" in svg and "nan" not in svg.lower()


# --- 계약 보존 --------------------------------------------------------------

def test_new_fields_survive_the_json_roundtrip(settings):
    conn = _open(settings)
    _seed_series(conn, settings.raw_dir, "^KS11", [2400.0, 2450.0, 2500.0],
                 market="KR", country="KR", unit="point")
    _two_day(conn, settings.raw_dir, "NVDA", 100.0, 105.0)
    report = _report(conn)
    conn.close()

    restored = model_mod.Report.from_json(report.to_json())
    assert restored == report
    assert restored.sector_summary and restored.breadth
    assert any(r.series for r in restored.market_reaction)


def test_old_report_json_without_the_new_fields_still_loads():
    """이미 커밋된 리포트 JSON에는 새 필드가 없다. 사이트가 그것들로 무너지면
    안 된다(docs/는 매번 reports/ 전체에서 다시 만들어진다)."""
    legacy = model_mod.Report(
        report_type="morning", report_date="2026-07-28",
        facts=[model_mod.FactRow(label="a", value="1", comparison="", source_url="",
                                 data_status="partial", known_at="2026-07-28T00:00:00+00:00",
                                 subject="X", metric="price_close")],
    )
    raw = legacy.to_json()
    import json as _json
    d = _json.loads(raw)
    d.pop("breadth"), d.pop("sector_summary")
    for f in d["facts"]:
        f.pop("delta_pct"), f.pop("series")
    restored = model_mod.Report.from_json(_json.dumps(d, ensure_ascii=False))
    assert restored.breadth == "" and restored.sector_summary == []
    assert restored.facts[0].series == [] and restored.facts[0].delta_pct is None
    render_html_mod.render_html(restored)  # must not raise
    render_md_mod.render_markdown(restored)


def test_every_report_type_still_renders(settings):
    """8종 × md/html 전수 — 새 블록이 어느 타입에서도 터지지 않는다."""
    conn = _open(settings)
    _two_day(conn, settings.raw_dir, "NVDA", 100.0, 105.0)
    for report_type in build_mod.TITLES:
        cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
        subject = "NVDA" if report_type == "event" else None
        report = build_mod.build_report(conn, report_type, REPORT_DATE, cutoff, subject=subject)
        md = render_md_mod.render_markdown(report)
        html_doc = render_html_mod.render_html(report)
        assert "AI 해석 미생성" in md and "AI 해석 미생성" in html_doc, report_type
        assert report.sector_summary, report_type
    conn.close()
