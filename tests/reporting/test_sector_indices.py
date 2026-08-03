"""업종 지수(sector_index) 인수 테스트 — 시장 전체를 업종으로 덮는다.

CEO 질문(2026-08-02): "6개 영역으로 전체 시장을 분류하기에 충분한가, 헬스케어
1종목이면 충분한가". 답은 아니다 — 명세 §12의 6축·16기업에는 소재·부동산이
통째로 없고, 헬스케어는 릴리 1종목이라 업종이 아니라 그 회사 이야기다.

그래서 층을 나눈다: Core 16(개별 기업)은 **깊이**를, 새 업종 지수 16개는
**폭**(어느 업종이 주도하나)을 맡는다. 이 파일이 지키는 경계는 하나다 —
**업종 지수는 Core 16 집계에 절대 섞이지 않는다.** 섞이면 "Core 16 중 4/6개
상승" 같은 문장이 거짓이 된다.

fixture는 `db.insert_raw_snapshot` + `db.upsert_fact`(실제 쓰기 경로)를 지난다.
전체 실행 시 형제 폴더의 conftest가 잡히는 사고를 막으려고 conftest를 import
하지 않고 이 파일 안에서 직접 심는다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from market_intel import db as db_mod
from market_intel import universe as universe_mod
from market_intel.engine import _fact_id
from market_intel.models import FactCandidate, RawItem
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting import model as model_mod
from market_intel.reporting import render_html as render_html_mod
from market_intel.reporting import render_md as render_md_mod

REPORT_DATE = date(2026, 8, 1)
KNOWN_BEFORE = "2026-07-31T22:00:00+00:00"  # morning 차단선(07-31T22:15Z) 이전

# 과제가 지정한 16개. 코드가 스스로를 채점하지 않도록 테스트 쪽에 사본을 둔다.
SPEC_US_SECTOR_INDICES = {
    "XLK": "정보기술", "XLV": "헬스케어", "XLF": "금융", "XLE": "에너지",
    "XLI": "산업재", "XLU": "유틸리티", "XLP": "필수소비재", "XLY": "경기소비재",
    "XLB": "소재", "XLRE": "부동산", "XLC": "커뮤니케이션",
}
SPEC_KR_SECTOR_INDICES = {
    "091160.KS": "KODEX 반도체", "227540.KS": "TIGER 헬스케어",
    "117460.KS": "KODEX 에너지화학", "102970.KS": "KODEX 증권",
    "117680.KS": "KODEX 철강",
}


def _seed(conn, raw_dir, symbol, market, country, event_at, value, unit):
    fc = FactCandidate(
        raw_ref=f"{symbol}:{event_at}", subject=symbol, category="price", metric="price_close",
        event_at=event_at, market=market, country=country, value_num=value, unit=unit,
        safe_source_url="https://example.test/data", data_status="source_verified",
    )
    raw = RawItem(
        external_id=f"yfinance:{symbol}:price_close:{event_at}",
        source_published_at=event_at, safe_source_url="https://example.test/data", payload="{}",
    )
    snapshot_id = db_mod.insert_raw_snapshot(conn, raw_dir, "yfinance", raw)
    db_mod.upsert_fact(conn, _fact_id("yfinance", fc), snapshot_id, KNOWN_BEFORE, fc)
    conn.commit()


def _series(conn, raw_dir, symbol, closes, *, market="US", country="US", unit="USD", start_day=30):
    for i, value in enumerate(closes):
        _seed(conn, raw_dir, symbol, market, country,
              f"2026-07-{start_day + i:02d}T20:00:00+00:00", value, unit)


def _kr(conn, raw_dir, symbol, closes, **kw):
    _series(conn, raw_dir, symbol, closes, market="KR", country="KR", unit="KRW", **kw)


def _open(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _report(conn, report_type="morning"):
    cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
    return build_mod.build_report(conn, report_type, REPORT_DATE, cutoff)


def _seed_core16_four_of_six_up(conn, raw_dir):
    """`test_visual_readability.test_breadth_line...`과 **같은** 씨앗 —
    업종 지수를 아무리 넣어도 이 6종목이 만드는 문장은 변하지 않아야 한다."""
    _series(conn, raw_dir, "NVDA", [100.0, 105.0])
    _series(conn, raw_dir, "TSM", [100.0, 101.0])
    _kr(conn, raw_dir, "005930.KS", [100.0, 101.0])
    _kr(conn, raw_dir, "000660.KS", [100.0, 101.0])
    _series(conn, raw_dir, "WMT", [100.0, 98.0])
    _kr(conn, raw_dir, "005380.KS", [100.0, 99.0])


def _seed_all_sector_indices(conn, raw_dir, *, us_up=True):
    """16개 업종 지수 전부에 종가를 심는다(전부 상승 또는 전부 하락)."""
    latest = 110.0 if us_up else 90.0
    for symbol in SPEC_US_SECTOR_INDICES:
        _series(conn, raw_dir, symbol, [100.0, latest])
    for symbol in SPEC_KR_SECTOR_INDICES:
        _kr(conn, raw_dir, symbol, [100.0, latest])


# --- ① 유니버스 등록 --------------------------------------------------------

def test_all_16_sector_indices_are_registered():
    by_symbol = {m["symbol"]: m for m in universe_mod.UNIVERSE}
    for symbol, name_ko in {**SPEC_US_SECTOR_INDICES, **SPEC_KR_SECTOR_INDICES}.items():
        meta = by_symbol[symbol]
        assert meta["asset_type"] == "sector_index", symbol
        assert meta["name_ko"] == name_ko, symbol
        assert meta["core16"] is False, symbol
        assert meta["sector"] is None, f"{symbol}에 sector를 주면 Core 16 집계에 섞인다"
    for symbol in SPEC_US_SECTOR_INDICES:
        assert by_symbol[symbol]["market"] == "US" and by_symbol[symbol]["country"] == "US"
    for symbol in SPEC_KR_SECTOR_INDICES:
        assert by_symbol[symbol]["market"] == "KR" and by_symbol[symbol]["country"] == "KR"


def test_sector_indices_never_join_core16():
    """관측 기업군과 업종 지수는 서로의 집계에 절대 섞이지 않는다.

    기업 수는 여기서 못박지 않는다 — 2026-08-03에 EQIX·POSCO홀딩스가 들어와
    18개가 됐고(비어 있던 소재·부동산 축), 이 파일이 지키려는 계약은 "몇 개냐"가
    아니라 "두 집계가 서로에게 닿지 않는다"이다."""
    assert len(universe_mod.CORE16_SYMBOLS) == len(universe_mod.CORE16)
    assert set(universe_mod.CORE16_SYMBOLS).isdisjoint(
        set(SPEC_US_SECTOR_INDICES) | set(SPEC_KR_SECTOR_INDICES))
    assert set(universe_mod.SECTOR_BY_SYMBOL).isdisjoint(
        set(SPEC_US_SECTOR_INDICES) | set(SPEC_KR_SECTOR_INDICES))
    assert len(universe_mod.SECTOR_INDEX_SYMBOLS) == 16


def test_collect_universe_carries_the_sector_indices():
    """수집은 유니버스 전체를 provider에 넘기는 구조다(cli.collect →
    run_collect(UNIVERSE)). 유니버스에 있으면 수집된다는 계약을 고정한다."""
    from market_intel.providers.yfinance_prices import US_CORE_EQUITIES

    symbols = {m["symbol"] for m in universe_mod.UNIVERSE}
    assert set(SPEC_US_SECTOR_INDICES) | set(SPEC_KR_SECTOR_INDICES) <= symbols
    # after-hours는 US Core 기업만 — ETF까지 매 심볼 `Ticker().info`를 때리면
    # 아침 수집이 16번 더 느려진다.
    assert set(US_CORE_EQUITIES).isdisjoint(set(SPEC_US_SECTOR_INDICES))


# --- ② Core 16 집계 무오염 (회귀 고정) --------------------------------------

def test_core16_breadth_is_untouched_by_sector_indices(settings):
    """이 파일의 존재 이유. 업종 지수 16개가 전부 +10%로 뜨는 날에도
    "관측기업 4/6개 상승"은 그대로여야 한다."""
    conn = _open(settings)
    _seed_core16_four_of_six_up(conn, settings.raw_dir)
    _seed_all_sector_indices(conn, settings.raw_dir, us_up=True)
    report = _report(conn)
    conn.close()

    assert "관측기업 4/6개 상승" in report.breadth, report.breadth
    assert "반도체·공급망 4/4" in report.breadth, report.breadth
    assert "소비·수출 0/2" in report.breadth, report.breadth
    assert "헬스케어 관측 없음" in report.breadth, report.breadth

    totals = {s.sector: s.total for s in report.sector_summary}
    assert totals == {"플랫폼·AI 수익화": 0, "반도체·공급망": 4, "금융·신용": 0,
                      "전력·산업": 0, "소비·수출": 2, "헬스케어": 0,
                      "소재·철강": 0, "부동산·데이터센터": 0}
    assert sum(totals.values()) == 6, "업종 지수가 업종 표에 섞였다"


def test_core16_headline_counts_are_untouched_by_sector_indices(settings):
    """헤드라인의 "±3% 이상 N종목 / 결측 M건"도 관측 기업만 센다."""
    conn = _open(settings)
    _seed_core16_four_of_six_up(conn, settings.raw_dir)
    _seed_all_sector_indices(conn, settings.raw_dir, us_up=True)
    report = _report(conn)
    conn.close()

    expected = f"관측기업 {len(universe_mod.CORE16_SYMBOLS)}곳 중 ±3% 이상 1종목, 결측 12건"
    assert expected in report.headline, report.headline


def test_sector_indices_do_not_enter_the_main_market_reaction_table(settings):
    """업종 지수는 자기 표에서만 보인다 — 시장 반응 표에도 실으면 CEO는 같은
    16줄을 두 번 읽는다."""
    conn = _open(settings)
    _series(conn, settings.raw_dir, "^KS11", [2500.0, 2550.0], market="KR", country="KR", unit="point")
    _seed_all_sector_indices(conn, settings.raw_dir, us_up=True)
    report = _report(conn)
    conn.close()

    main_subjects = {r.subject for r in report.market_reaction}
    assert "^KS11" in main_subjects
    assert main_subjects.isdisjoint(set(SPEC_US_SECTOR_INDICES) | set(SPEC_KR_SECTOR_INDICES))


def test_hero_cards_and_headline_are_untouched(settings):
    """맨 위 요약 카드 8개와 "시장 한 줄"은 건드리지 않는다 — 업종 지수 16개가
    거기 끼면 요약이 아니게 된다."""
    conn = _open(settings)
    _series(conn, settings.raw_dir, "^KS11", [2500.0, 2550.0], market="KR", country="KR", unit="point")
    _seed_all_sector_indices(conn, settings.raw_dir, us_up=True)
    report = _report(conn)
    conn.close()

    assert render_md_mod.HERO_SYMBOLS == (
        "^KS11", "^KQ11", "^GSPC", "^IXIC", "^DJI", "^RUT", "^SOX", "KRW=X")
    hero = render_md_mod._hero(report)
    assert {r.subject for r in hero["rows"]} <= set(render_md_mod.HERO_SYMBOLS)
    for symbol, name_ko in SPEC_US_SECTOR_INDICES.items():
        assert symbol not in report.headline and name_ko not in report.headline


# --- ③ 업종 지수 표 ---------------------------------------------------------

def test_sector_index_rows_are_split_by_market_and_sorted_by_return(settings):
    conn = _open(settings)
    _series(conn, settings.raw_dir, "XLK", [100.0, 103.0])   # +3%
    _series(conn, settings.raw_dir, "XLE", [100.0, 99.0])    # -1%
    _series(conn, settings.raw_dir, "XLV", [100.0, 101.0])   # +1%
    _kr(conn, settings.raw_dir, "091160.KS", [100.0, 105.0])  # +5%
    _kr(conn, settings.raw_dir, "117680.KS", [100.0, 97.0])   # -3%
    report = _report(conn)
    conn.close()

    us = [r for r in report.sector_index if r.market == "US"]
    kr = [r for r in report.sector_index if r.market == "KR"]
    assert [r.subject for r in us] == ["XLK", "XLV", "XLE"], "등락률 내림차순이어야 한다"
    assert [r.subject for r in kr] == ["091160.KS", "117680.KS"]
    assert [r.label for r in us] == ["정보기술", "헬스케어", "에너지"]
    assert us[0].delta_pct == 3.0 and kr[-1].delta_pct == -3.0
    assert us[0].data_status == "source_verified"
    assert us[0].source_url.startswith("https://")


def test_sector_index_table_renders_in_both_formats(settings):
    """호출부 계약: 헬퍼가 아니라 실제 렌더러(render_markdown/render_html)가
    두 표를 **함께** 낸다."""
    conn = _open(settings)
    _seed_core16_four_of_six_up(conn, settings.raw_dir)
    _seed_all_sector_indices(conn, settings.raw_dir, us_up=True)
    report = _report(conn)
    conn.close()

    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)

    for text in (md, html_doc):
        assert render_md_mod.SECTOR_INDEX_TITLE in text
        assert render_md_mod.SECTOR_TITLE in text, "기업 묶음 표를 없애면 안 된다"
        assert render_md_mod.SECTOR_INDEX_NOTE in text, "두 표의 관계를 한 줄로 설명해야 한다"
        assert "미국" in text and "한국" in text
        for name_ko in SPEC_US_SECTOR_INDICES.values():
            assert name_ko in text, name_ko
        for name_ko in SPEC_KR_SECTOR_INDICES.values():
            assert name_ko in text, name_ko
        # 기업 묶음 표의 6축도 그대로 살아 있다.
        for axis in universe_mod.SECTORS:
            assert axis in text, axis


def test_sector_index_note_is_readable_by_a_non_expert():
    """비전공자가 읽을 수 있어야 한다 — 두 표가 각각 무엇인지 말한다."""
    note = render_md_mod.SECTOR_INDEX_NOTE
    assert "시장 전체" in note and str(len(universe_mod.CORE16_SYMBOLS)) in note
    assert len(note) <= 200, "한 줄 설명이어야 한다"


def test_sector_index_direction_uses_colour_and_arrow(settings):
    """색·화살표 규약은 기존 그대로 — 상승 ▲(up), 하락 ▼(down)."""
    conn = _open(settings)
    _series(conn, settings.raw_dir, "XLK", [100.0, 103.0])
    _series(conn, settings.raw_dir, "XLE", [100.0, 97.0])
    report = _report(conn)
    conn.close()

    html_doc = render_html_mod.render_html(report)
    up_row = [c for c in html_doc.split("<tr>") if "정보기술" in c and "<td" in c][0]
    down_row = [c for c in html_doc.split("<tr>") if "에너지" in c and "<td" in c][0]
    assert 'class="chg up"' in up_row and "▲" in up_row, up_row
    assert 'class="chg down"' in down_row and "▼" in down_row, down_row
    assert "down" not in up_row.split('class="chg')[1].split(">")[0]

    md = render_md_mod.render_markdown(report)
    md_up = [ln for ln in md.splitlines() if ln.startswith("|") and "정보기술" in ln][0]
    md_down = [ln for ln in md.splitlines() if ln.startswith("|") and "에너지" in ln][0]
    assert "▲" in md_up and "▼" not in md_up, md_up
    assert "▼" in md_down and "▲" not in md_down, md_down


def test_sector_index_sparkline_never_crosses_the_cutoff(settings):
    """정보 차단선: 스파크라인 시계열도 리포트 차단선 이전만."""
    conn = _open(settings)
    cutoff = cutoff_mod.cutoff_for("morning", REPORT_DATE)
    _series(conn, settings.raw_dir, "XLK", [100.0, 101.0, 102.0, 103.0], start_day=28)
    leaked = 9999.0
    fc = FactCandidate(
        raw_ref="XLK:leak", subject="XLK", category="price", metric="price_close",
        event_at="2026-08-01T06:30:00+00:00", market="US", country="US",
        value_num=leaked, unit="USD", safe_source_url="https://example.test/data",
        data_status="source_verified",
    )
    raw = RawItem(external_id="yfinance:XLK:leak", source_published_at=fc.event_at,
                  safe_source_url="https://example.test/data", payload="{}")
    snapshot_id = db_mod.insert_raw_snapshot(conn, settings.raw_dir, "yfinance", raw)
    db_mod.upsert_fact(conn, _fact_id("yfinance", fc), snapshot_id,
                       db_mod.iso_utc(cutoff + timedelta(minutes=1)), fc)
    conn.commit()

    report = build_mod.build_report(conn, "morning", REPORT_DATE, cutoff)
    conn.close()

    row = [r for r in report.sector_index if r.subject == "XLK"][0]
    assert leaked not in row.series
    assert row.series == [100.0, 101.0, 102.0, 103.0]

    svg = render_html_mod.sparkline_svg(row.series, "up")
    ys = [float(p.split(",")[1])
          for p in re.search(r'class="line" d="M([^"]+)"', svg).group(1).split(" L")]
    assert len(ys) == 4, svg


def test_sector_index_table_appears_in_every_market_reaction_report_type(settings):
    """8종 전수 — 시장 반응 블록을 쓰는 타입이면 업종 지수 표도 나온다."""
    conn = _open(settings)
    _seed_all_sector_indices(conn, settings.raw_dir, us_up=True)
    for report_type in build_mod.TITLES:
        cutoff = cutoff_mod.cutoff_for(report_type, REPORT_DATE)
        subject = "NVDA" if report_type == "event" else None
        report = build_mod.build_report(conn, report_type, REPORT_DATE, cutoff, subject=subject)
        assert report.sector_index, report_type
        md = render_md_mod.render_markdown(report)
        html_doc = render_html_mod.render_html(report)
        assert render_md_mod.SECTOR_INDEX_TITLE in md, report_type
        assert render_md_mod.SECTOR_INDEX_TITLE in html_doc, report_type
    conn.close()


def test_missing_sector_index_data_is_shown_as_absence_not_invented(settings):
    """관측이 없으면 표는 비되, 화면에서 조용히 사라지지 않는다."""
    conn = _open(settings)
    _series(conn, settings.raw_dir, "^KS11", [2500.0, 2550.0], market="KR", country="KR", unit="point")
    report = _report(conn)
    conn.close()

    assert report.sector_index == []
    md = render_md_mod.render_markdown(report)
    html_doc = render_html_mod.render_html(report)
    assert render_md_mod.SECTOR_INDEX_TITLE in md and render_md_mod.SECTOR_INDEX_TITLE in html_doc
    assert "관측 없음" in md and "관측 없음" in html_doc


# --- ④ 계약 보존 ------------------------------------------------------------

def test_sector_index_survives_the_json_roundtrip(settings):
    conn = _open(settings)
    _series(conn, settings.raw_dir, "XLK", [100.0, 101.0, 103.0])
    _kr(conn, settings.raw_dir, "091160.KS", [100.0, 105.0])
    report = _report(conn)
    conn.close()

    restored = model_mod.Report.from_json(report.to_json())
    assert restored == report
    assert restored.sector_index and restored.sector_index[0].series


def test_old_report_json_without_sector_index_still_loads():
    """이미 커밋된 리포트 JSON에는 이 필드가 없다. docs/는 매번 reports/
    전체에서 다시 만들어지므로, 옛 JSON 하나가 사이트를 무너뜨리면 안 된다."""
    import json as _json

    legacy = model_mod.Report(report_type="morning", report_date="2026-07-28")
    d = _json.loads(legacy.to_json())
    d.pop("sector_index")
    restored = model_mod.Report.from_json(_json.dumps(d, ensure_ascii=False))
    assert restored.sector_index == []
    render_html_mod.render_html(restored)  # must not raise
    render_md_mod.render_markdown(restored)
