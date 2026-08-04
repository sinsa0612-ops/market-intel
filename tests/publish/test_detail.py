"""상세 페이지 (기업 재무 · 거시지표 · 공시 타임라인 · 기관 13F).

site.py의 다른 테스트와 같은 자세로 쓴다: 나와야 할 것보다 **나오면 안 되는
것**을 더 세게 본다. 상세 페이지는 리포트와 같은 공개물이고, 여기 실리는
문자열(회사명·공시 종류·출처 URL)은 전부 외부 HTTP 응답에서 온 것이다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from market_intel import db as db_mod
from market_intel import detail as detail_mod
from market_intel import site as site_mod
from market_intel.engine import _fact_id
from market_intel.models import FactCandidate, RawItem

from conftest import make_report, write_report

CUTOFF = "2026-08-05T00:00:00+00:00"


def seed(conn, raw_dir, provider, fc, known_at):
    raw = RawItem(external_id=fc.raw_ref, source_published_at=fc.event_at,
                  safe_source_url=fc.safe_source_url, payload="{}")
    snapshot_id = db_mod.insert_raw_snapshot(conn, raw_dir, provider, raw)
    db_mod.upsert_fact(conn, _fact_id(provider, fc), snapshot_id, known_at, fc)
    conn.commit()


def fin(subject, metric, event_date, value, basis, *, unit="USD", url="https://example.test/f"):
    return FactCandidate(
        raw_ref=f"{subject}:{metric}:{event_date}", subject=subject, category="financials",
        metric=metric, event_at=f"{event_date}T00:00:00+00:00", market="US", country="US",
        value_num=value, unit=unit, comparison_basis=basis, data_status="source_verified",
        safe_source_url=url,
    )


def filing(subject, event_date, category, form, *, url="https://example.test/d", extra=None):
    return FactCandidate(
        raw_ref=f"{subject}:{category}:{event_date}", subject=subject, category=category,
        metric="filing_event", event_at=f"{event_date}T00:00:00+00:00", market="US",
        country="US", value_text="0001-26-1", data_status="source_verified",
        safe_source_url=url, extra=extra if extra is not None else {"form": form},
    )


def macro(subject, event_date, value, *, url="https://example.test/m", extra=None):
    return FactCandidate(
        raw_ref=f"{subject}:{event_date}", subject=subject, category="macro", metric="value",
        event_at=f"{event_date}T00:00:00+00:00", market="US", country="US", value_num=value,
        unit="%", data_status="source_verified", safe_source_url=url, extra=extra or {},
    )


# --- 슬러그 (경로 조립) ------------------------------------------------------

@pytest.mark.parametrize("subject", [
    "../../etc/passwd", "..", "/absolute", "a/b/c", "^KS11", "005930.KS", "",
])
def test_slug_never_escapes_one_path_segment(subject):
    """subject는 DB에서 오는 외부 유래 문자열이다. 슬러그가 경로 구분자나
    상위 이동을 남기면 `docs/` 밖으로 파일을 쓸 수 있다."""
    s = detail_mod.slug(subject)
    assert s and "/" not in s and "\\" not in s
    assert ".." not in s
    assert Path(s).name == s


def test_slug_map_resolves_collisions():
    """`^KS11`과 `-KS11-`은 같은 슬러그로 접힌다 — 접힌 채로 두면 페이지 하나가
    다른 페이지를 덮어써 조용히 사라진다."""
    mapping = detail_mod.slug_map(["^KS11", "-KS11-", "005930.KS"])
    assert len(set(mapping.values())) == 3
    assert mapping["005930.KS"] == "005930-KS"


# --- 1) 기업별 재무 추이 -----------------------------------------------------

def test_financials_use_one_period_length(conn, tmp_path):
    """1년치 누적이 마지막 분기 자리를 먹으면 안 된다 — 표와 그래프가 동시에
    거짓말을 한다(실측 2026-08-03, MSFT). `test_period_basis_collision.py`가
    조회 계층에서 잡는 것과 같은 결함을 화면 입력에서 다시 막는다."""
    raw_dir = str(tmp_path / "raw")
    for date_str, value in [("2025-12-31", 5.0e9), ("2026-03-31", 15.0e9), ("2026-06-30", 19.6e9)]:
        seed(conn, raw_dir, "sec_edgar",
             fin("MSFT", "free_cash_flow", date_str, value, "quarterly"), f"{date_str}T00:00:00+00:00")
    seed(conn, raw_dir, "sec_edgar",
         fin("MSFT", "free_cash_flow", "2026-06-30", 66.9e9, "annual"), "2026-08-01T00:00:00+00:00")

    data = detail_mod.company_financials(conn, CUTOFF, "MSFT")
    assert data["bases"]["free_cash_flow"] == "quarterly"
    assert data["periods"][0]["period"] == "2026-06-30"
    assert "196.0억 달러" in data["periods"][0]["free_cash_flow"]["text"]
    assert len(data["series"]["free_cash_flow"]) == 3


def test_financials_keep_annual_only_subjects(conn, tmp_path):
    """DART(한국)·TSM은 연간밖에 없다. 분기로 못박았다면 이 회사들의 상세
    페이지가 통째로 비어버린다."""
    seed(conn, str(tmp_path / "raw"), "dart",
         fin("005930.KS", "revenue", "2025-12-31", 333.6e12, "annual", unit="KRW"),
         "2026-03-15T00:00:00+00:00")
    data = detail_mod.company_financials(conn, CUTOFF, "005930.KS")
    assert data["bases"]["revenue"] == "annual"
    assert data["periods"][0]["revenue"]["text"] == "333.6조 원"


def test_financials_respect_the_information_barrier(conn, tmp_path):
    """차단선 이후에 알게 된 값은 상세 페이지에도 실리면 안 된다."""
    raw_dir = str(tmp_path / "raw")
    seed(conn, raw_dir, "sec_edgar", fin("MSFT", "revenue", "2026-03-31", 80e9, "quarterly"),
         "2026-04-29T00:00:00+00:00")
    seed(conn, raw_dir, "sec_edgar", fin("MSFT", "revenue", "2026-06-30", 90e9, "quarterly"),
         "2026-08-10T00:00:00+00:00")  # 차단선(08-05) 이후

    data = detail_mod.company_financials(conn, CUTOFF, "MSFT")
    assert [p["period"] for p in data["periods"]] == ["2026-03-31"]


@pytest.mark.parametrize("value,unit,expected", [
    (333_605_938_000_000.0, "KRW", "333.6조 원"),
    (43_600_000_000.0, "KRW", "436억 원"),
    (19_639_000_000.0, "USD", "196.4억 달러"),
    (-1_050_000_000.0, "USD", "-10.5억 달러"),
    (None, "USD", "미확인"),
])
def test_fmt_money(value, unit, expected):
    assert detail_mod.fmt_money(value, unit) == expected


# --- 3) 공시 이력 / 4) 13F ---------------------------------------------------

def test_filings_merge_three_kinds_newest_first(conn, tmp_path):
    raw_dir = str(tmp_path / "raw")
    seed(conn, raw_dir, "sec_edgar", filing("MSFT", "2026-07-29", "filing", "10-K"),
         "2026-07-30T00:00:00+00:00")
    seed(conn, raw_dir, "sec_edgar",
         filing("MSFT", "2026-07-25", "event", "8-K", extra={"form": "8-K", "item": "2.02"}),
         "2026-07-26T00:00:00+00:00")
    seed(conn, raw_dir, "sec_edgar",
         filing("berkshire_hathaway", "2026-05-15", "13f_filing", "13F-HR",
                extra={"form": "13F-HR", "manager": "Berkshire Hathaway"}),
         "2026-05-16T00:00:00+00:00")

    rows = detail_mod.filings(conn, CUTOFF)
    assert [r["event_at"] for r in rows] == ["2026-07-29", "2026-07-25", "2026-05-15"]
    assert rows[0]["form_label"] == "연간보고서(10-K)"
    assert rows[1]["item"] == "실적·재무상태 발표(항목 2.02)"
    assert rows[2]["name"] == "Berkshire Hathaway"

    assert detail_mod.filings(conn, CUTOFF, subject="MSFT") == rows[:2]
    assert [r["category"] for r in detail_mod.holdings_13f(conn, CUTOFF)] == ["13f_filing"]


# --- 페이지 생성 -------------------------------------------------------------

@pytest.fixture
def built(conn, tmp_path, reports_root, docs_root):
    raw_dir = str(tmp_path / "raw")
    seed(conn, raw_dir, "sec_edgar", fin("MSFT", "revenue", "2026-06-30", 90e9, "quarterly"),
         "2026-07-30T00:00:00+00:00")
    seed(conn, raw_dir, "sec_edgar", filing("MSFT", "2026-07-29", "filing", "10-K"),
         "2026-07-30T00:00:00+00:00")
    seed(conn, raw_dir, "sec_edgar",
         filing("berkshire_hathaway", "2026-05-15", "13f_filing", "13F-HR",
                extra={"form": "13F-HR", "manager": "Berkshire Hathaway"}),
         "2026-05-16T00:00:00+00:00")
    seed(conn, raw_dir, "fred", macro("DGS10", "2026-07-30", 4.68), "2026-07-31T00:00:00+00:00")
    write_report(reports_root, make_report("morning", "2026-08-01"))
    result = site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    return result, docs_root


def test_detail_pages_are_generated_and_linked(built):
    result, docs_root = built
    for rel in ("detail.html", "filings.html", "holdings.html",
                "company/MSFT.html", "macro/DGS10.html"):
        assert (docs_root / rel).exists(), f"상세 페이지 누락: {rel}"

    index = (docs_root / "detail.html").read_text(encoding="utf-8")
    assert 'href="company/MSFT.html"' in index
    assert 'href="macro/DGS10.html"' in index
    # 모든 페이지에서 상세로 갈 수 있어야 한다.
    assert '>상세</a>' in (docs_root / "index.html").read_text(encoding="utf-8")


def test_company_page_marks_derived_values(conn, tmp_path, reports_root, docs_root):
    """누적치를 차분해 만든 값은 공시 원문 그대로가 아니다 — 그 사실이 표에
    보여야 한다."""
    fc = fin("MSFT", "free_cash_flow", "2026-06-30", 19.6e9, "quarterly")
    fc.extra = {"formula": "operating_cash_flow - capex"}
    seed(conn, str(tmp_path / "raw"), "sec_edgar", fc, "2026-07-30T00:00:00+00:00")
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    html = (docs_root / "company" / "MSFT.html").read_text(encoding="utf-8")
    assert "(산출)" in html


def test_detail_pages_never_emit_a_javascript_href(conn, tmp_path, reports_root, docs_root):
    """`html.escape`는 `javascript:`를 그대로 둔다. 출처 URL은 외부 응답에서
    오므로 스킴 허용목록을 통과해야 하고, 막힌 값은 링크가 아니라 글자로
    남아야 한다(조용히 사라지면 감사 추적이 끊긴다)."""
    raw_dir = str(tmp_path / "raw")
    seed(conn, raw_dir, "sec_edgar",
         fin("MSFT", "revenue", "2026-06-30", 90e9, "quarterly", url="javascript:alert(1)"),
         "2026-07-30T00:00:00+00:00")
    seed(conn, raw_dir, "sec_edgar",
         filing("MSFT", "2026-07-29", "filing", "10-K", url="javascript:alert(2)"),
         "2026-07-30T00:00:00+00:00")
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    for path in docs_root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for chunk in text.split('href="')[1:]:
            href = chunk.split('"')[0]
            assert not href.lower().lstrip().startswith(("javascript:", "data:", "vbscript:")), (
                f"{path.name}에 실행 가능한 href가 실렸다: {href!r}")

    # 막힌 URL은 링크가 아니라 **글자로** 남아야 한다 — 조용히 지우면 그 행의
    # 출처가 무엇이었는지 감사할 수 없다.
    company = (docs_root / "company" / "MSFT.html").read_text(encoding="utf-8")
    assert "javascript:alert(1)" in company


def test_detail_index_without_any_report_states_it_has_no_barrier(conn, reports_root, docs_root):
    """리포트가 0건이면 차단선을 정할 수 없다. 벽시계로 물러나면 어떤 리포트도
    볼 수 없던 사실을 공개하게 되므로, 일정 페이지와 같은 규칙으로 비운다."""
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    for rel in ("detail.html", "filings.html", "holdings.html"):
        assert "정보차단선을 정할 수 없습니다" in (docs_root / rel).read_text(encoding="utf-8")
    assert not (docs_root / "company").exists()


def test_macro_page_resolves_ecos_codes(conn, tmp_path, reports_root, docs_root):
    """ECOS는 `722Y001.0101000`으로 사실을 키잉한다. 화면에 코드가 그대로
    나오면 CEO가 읽을 수 없다 — 리포트가 쓰는 이름표 규칙을 그대로 쓴다."""
    seed(conn, str(tmp_path / "raw"), "ecos",
         macro("722Y001.0101000", "2026-08-02", 2.5,
               extra={"logical_key": "base_rate", "stat_name": "한국은행 기준금리"}),
         "2026-08-03T00:00:00+00:00")
    write_report(reports_root, make_report("morning", "2026-08-04"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    index = (docs_root / "detail.html").read_text(encoding="utf-8")
    assert "한국 기준금리" in index
    page = (docs_root / "macro" / "722Y001-0101000.html").read_text(encoding="utf-8")
    assert "한국 기준금리" in page


# --- 4) 기관 13F 보유내역 (2026-08-04) ---------------------------------------

def holding(manager, cusip, issuer, value, shares, *, metric="holding_value",
            period="2026-03-31", scale=None):
    extra = {"manager": manager, "issuer": issuer, "cusip": cusip,
             "amount_type": "SH", "period_of_report": period, "merged_rows": 1}
    if scale:
        extra["value_scale"] = scale
    return FactCandidate(
        raw_ref=f"13f:{manager}:{cusip}", subject=f"{manager}/{cusip}",
        category="13f_holding", metric=metric,
        # 보유 시점은 분기 말이다 — 알게 된 시점(known_at)과 다르다.
        event_at=f"{period}T00:00:00+00:00", market="US", country="US",
        value_num=value if metric == "holding_value" else shares,
        unit="USD" if metric == "holding_value" else "SH",
        data_status="source_verified", safe_source_url="https://example.test/13f",
        extra=extra,
    )


def _seed_holdings(conn, raw_dir):
    for metric in ("holding_value", "holding_amount"):
        seed(conn, raw_dir, "sec_edgar_13f",
             holding("Berkshire Hathaway", "037833100", "APPLE INC",
                     57_843_260_493.0, 227_917_808.0, metric=metric),
             "2026-05-16T00:00:00+00:00")
        seed(conn, raw_dir, "sec_edgar_13f",
             holding("Berkshire Hathaway", "025816109", "AMERICAN EXPRESS CO",
                     45_859_204_536.0, 151_610_700.0, metric=metric),
             "2026-05-16T00:00:00+00:00")
        seed(conn, raw_dir, "sec_edgar_13f",
             holding("Baupost Group", "023135106", "AMAZON COM INC",
                     649_543_000.0, 3_118_754.0, metric=metric, scale=1000.0),
             "2026-05-16T00:00:00+00:00")


def test_holdings_group_by_manager_biggest_first(conn, tmp_path):
    _seed_holdings(conn, str(tmp_path / "raw"))
    groups = detail_mod.holdings_by_manager(conn, CUTOFF)

    assert [g["manager"] for g in groups] == ["Berkshire Hathaway", "Baupost Group"]
    berk = groups[0]
    assert berk["period"] == "2026-03-31"
    assert [h["issuer"] for h in berk["holdings"]] == ["APPLE INC", "AMERICAN EXPRESS CO"]
    assert berk["holdings"][0]["amount"] == 227_917_808.0
    # 비중은 그 운용사 안에서의 몫이다 — 규모가 100배 차이 나는 운용사끼리
    # 전체 대비로 재면 작은 쪽은 전부 0%가 된다.
    assert 0.55 < berk["holdings"][0]["weight"] < 0.57
    assert abs(sum(h["weight"] for h in berk["holdings"]) - 1.0) < 1e-9


def test_rescaled_filing_is_flagged_so_the_number_is_auditable(conn, tmp_path):
    """천 달러 단위 신고를 달러로 바꿔 실었다는 사실이 화면에 남아야 한다."""
    _seed_holdings(conn, str(tmp_path / "raw"))
    groups = {g["manager"]: g for g in detail_mod.holdings_by_manager(conn, CUTOFF)}
    assert groups["Baupost Group"]["rescaled"] is True
    assert groups["Berkshire Hathaway"]["rescaled"] is False


def test_holdings_respect_the_information_barrier(conn, tmp_path, reports_root, docs_root):
    """13F는 분기 말 보유를 45일 뒤에 낸다. 우리가 그것을 **알기 전** 차단선의
    리포트가 그 보유를 아는 것처럼 보이면 안 된다."""
    _seed_holdings(conn, str(tmp_path / "raw"))  # known_at = 2026-05-16
    assert detail_mod.holdings_by_manager(conn, "2026-05-15T00:00:00+00:00") == []
    assert detail_mod.holdings_by_manager(conn, "2026-05-17T00:00:00+00:00")


def test_holdings_page_shows_the_table_and_says_it_is_not_today(conn, tmp_path,
                                                                reports_root, docs_root):
    _seed_holdings(conn, str(tmp_path / "raw"))
    seed(conn, str(tmp_path / "raw"),
         "sec_edgar_13f",
         filing("berkshire_hathaway", "2026-05-15", "13f_filing", "13F-HR",
                extra={"form": "13F-HR", "manager": "Berkshire Hathaway"}),
         "2026-05-16T00:00:00+00:00")
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)

    html = (docs_root / "holdings.html").read_text(encoding="utf-8")
    assert "APPLE INC" in html and "2026-03-31" in html
    # 보유 표가 생겼다고 제출 이력이 사라지면 안 된다 — 서로 다른 질문에 답한다.
    assert "0001-26-1" in html
    # 45일 시차를 말하지 않으면 독자는 이것을 지금 보유로 읽는다.
    assert "45일" in html
    assert "천 달러 단위 신고" in html, "환산한 사실이 화면에 남아야 한다"
    # 옛 배너(보유내역 없음)가 표와 함께 남아 있으면 서로 모순된다.
    assert "보유 종목 표는 아직 없습니다" not in html


def test_holdings_page_says_so_when_nothing_was_parsed(conn, reports_root, docs_root):
    """빈 표를 '이 기관은 아무것도 안 들고 있다'로 읽히게 두면 안 된다."""
    write_report(reports_root, make_report("morning", "2026-08-01"))
    site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root)
    html = (docs_root / "holdings.html").read_text(encoding="utf-8")
    assert "아직 읽어들인 보유내역이 없습니다" in html
