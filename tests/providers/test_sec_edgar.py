"""Offline unit tests for the SEC EDGAR provider (httpx.MockTransport).

Covers the two data-honesty rules the live run cannot assert deterministically:
  * an XBRL concept a company abandoned years ago must not be published as a
    current figure with `source_verified` — it is downgraded to `partial`
    (부분확인) and the reason is recorded,
  * a derived figure (FCF = OCF − CAPEX) is `reconstructed` (복원완료), and is
    only derived when both legs cover the same period.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from market_intel.http_client import SafeHttp
from market_intel.models import CollectContext
from market_intel.providers.sec_edgar import SecEdgarProvider

CIK = 789019
FILING_DATE = "2026-04-30"

TICKERS = {"0": {"cik_str": CIK, "ticker": "MSFT", "title": "MICROSOFT CORP"}}

SUBMISSIONS = {
    "cik": str(CIK),
    "filings": {
        "recent": {
            "form": ["8-K", "10-Q", "10-K"],
            "filingDate": ["2026-05-02", FILING_DATE, "2025-07-30"],
            "accessionNumber": ["0000000000-26-000001", "0000789019-26-000042", "0000789019-25-000077"],
        }
    },
}


def _unit(end: str, val: float, form: str = "10-Q") -> dict:
    return {"end": end, "val": val, "form": form, "fy": int(end[:4]), "fp": "Q3"}


def _companyfacts(capex_end: str = "2026-03-31") -> dict:
    return {
        "cik": CIK,
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [_unit("2025-12-31", 80_000.0), _unit("2026-03-31", 82_886.0)]}
                },
                # abandoned tag: latest datapoint is 12 years old
                "OperatingIncomeLoss": {"units": {"USD": [_unit("2014-06-30", 6_000.0)]}},
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {"USD": [_unit("2026-03-31", 40_000.0)]}
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {"USD": [_unit(capex_end, 15_000.0)]}
                },
            }
        },
    }


def _make_ctx(settings, capex_end: str = "2026-03-31") -> CollectContext:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json=TICKERS)
        if "/submissions/" in url:
            return httpx.Response(200, json=SUBMISSIONS)
        if "/companyfacts/" in url:
            return httpx.Response(200, json=_companyfacts(capex_end))
        return httpx.Response(404, json={"error": "not found"})

    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


@pytest.fixture
def facts(settings):
    result = SecEdgarProvider().collect(_make_ctx(settings))
    return result, {f.metric: f for f in result.facts if f.subject == "MSFT"}


def test_filing_event_uses_the_most_recent_periodic_report(facts):
    _, by_metric = facts
    assert by_metric["filing_event"].value_text == "0000789019-26-000042"
    assert by_metric["filing_event"].extra["form"] == "10-Q"
    assert by_metric["filing_event"].event_at.startswith(FILING_DATE)


def test_current_period_figure_is_source_verified(facts):
    _, by_metric = facts
    revenue = by_metric["revenue"]
    assert revenue.value_num == 82_886.0
    assert revenue.event_at.startswith("2026-03-31")
    assert revenue.data_status == "source_verified"


def test_stale_concept_is_downgraded_and_reported(facts):
    result, by_metric = facts
    op = by_metric["operating_income"]
    assert op.event_at.startswith("2014-06-30")
    assert op.data_status == "partial", "a 12-year-old datapoint cannot be 원자료확인"
    assert "MSFT:operating_income:stale_period:2014-06-30" in result.safe_detail
    assert result.status == "PARTIAL"


def test_derived_fcf_is_reconstructed(facts):
    _, by_metric = facts
    fcf = by_metric["free_cash_flow"]
    assert fcf.value_num == 25_000.0
    assert fcf.data_status == "reconstructed", "a derived value is 복원완료, never 원자료확인"
    assert fcf.publisher.endswith("(derived)")


def test_no_fcf_when_the_two_legs_cover_different_periods(settings):
    result = SecEdgarProvider().collect(_make_ctx(settings, capex_end="2017-03-31"))
    assert not [f for f in result.facts if f.metric == "free_cash_flow"]
    assert "period_mismatch" in result.safe_detail


def test_missing_ticker_is_reported_not_guessed(facts):
    result, _ = facts
    assert "NVDA:cik_not_found" in result.safe_detail or "cik_not_found" in result.safe_detail
    assert not [f for f in result.facts if f.subject == "NVDA"]


def test_secret_free_source_urls_are_stored(facts):
    result, _ = facts
    for item in result.raw_items:
        assert item.safe_source_url.startswith("https://")
        assert json.loads(item.payload) is not None


# --- repair.md finding #1: same-`end` candidates must be picked
# deterministically (never by array order) and the chosen period's length
# must be labeled via comparison_basis, never left blank. -----------------

def _unit_with_period(start: str, end: str, val: float, form: str = "10-Q") -> dict:
    return {"start": start, "end": end, "val": val, "form": form, "fy": int(end[:4]), "fp": "Q2"}


def _companyfacts_amzn_style(order: str) -> dict:
    """SEC posts a discrete 3-month figure and a 6-month YTD duplicate at
    the identical `end` date within the same 10-Q. `order` controls which
    one the source array lists last, reproducing the reviewer's repro
    where flipping the array order flipped the selected value."""
    quarter = _unit_with_period("2026-04-01", "2026-06-30", 200_606.0)
    ytd = _unit_with_period("2026-01-01", "2026-06-30", 382_125.0)
    units = [quarter, ytd] if order == "quarter_first" else [ytd, quarter]
    return {
        "cik": CIK,
        "facts": {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": units}}}},
    }


def _make_ctx_amzn_style(settings, order: str) -> CollectContext:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json=TICKERS)
        if "/submissions/" in url:
            return httpx.Response(200, json=SUBMISSIONS)
        if "/companyfacts/" in url:
            return httpx.Response(200, json=_companyfacts_amzn_style(order))
        return httpx.Response(404, json={"error": "not found"})

    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )


def test_same_end_pick_is_independent_of_source_array_order(settings):
    result_a = SecEdgarProvider().collect(_make_ctx_amzn_style(settings, "quarter_first"))
    result_b = SecEdgarProvider().collect(_make_ctx_amzn_style(settings, "ytd_first"))

    rev_a = next(f for f in result_a.facts if f.subject == "MSFT" and f.metric == "revenue")
    rev_b = next(f for f in result_b.facts if f.subject == "MSFT" and f.metric == "revenue")

    assert rev_a.value_num == rev_b.value_num == 200_606.0, (
        "the discrete-quarter figure must win regardless of array order, "
        "not whichever entry the source array happens to list last"
    )
    assert rev_a.comparison_basis == "quarterly"


def test_annual_period_is_labeled_in_comparison_basis(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json=TICKERS)
        if "/submissions/" in url:
            return httpx.Response(200, json=SUBMISSIONS)
        if "/companyfacts/" in url:
            fy_unit = _unit_with_period("2025-07-01", "2026-06-30", 331_839.0, form="10-K")
            return httpx.Response(
                200,
                json={
                    "cik": CIK,
                    "facts": {"us-gaap": {"Revenues": {"units": {"USD": [fy_unit]}}}},
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    now = datetime.now(timezone.utc)
    ctx = CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )
    result = SecEdgarProvider().collect(ctx)
    revenue = next(f for f in result.facts if f.subject == "MSFT" and f.metric == "revenue")
    assert revenue.value_num == 331_839.0
    assert revenue.comparison_basis == "annual", (
        "an FY figure must be labeled 'annual' so it is never silently "
        "compared against another company's quarterly figure"
    )


# --- capex 태그: EQIX·LLY가 쓰는 현행 태그 (2026-08-03) ----------------------

def test_other_ppe_tag_is_a_capex_candidate():
    """`PaymentsToAcquireOtherPropertyPlantAndEquipment`가 후보에 있어야 한다.

    2026-08-01에는 이름의 "Other" 때문에 "자본지출 전체가 아닐 수 있다"며 뺐고,
    그 결과 LLY capex가 2022-09-30에 멈춰 있었다. EQIX가 관측군에 들어오며 같은
    이유로 FCF가 0건이 되자 추측 대신 SEC 원자료를 전수 비교했다 — 두 태그를 함께
    보고한 기간에서 EQIX는 2015-12-31 이후 42기간 전부 동일, LLY는 3기간 전부
    동일, 나머지 10개사는 태그 자체가 없다. 근거는 sec_edgar.py의 주석.

    수리 결과(실측): EQIX capex 2026-03-31 -> 2026-06-30 · FCF 0건 -> 생성,
    LLY capex 2022-09-30 -> 2026-03-31 · FCF 0건 -> 생성. 나머지는 불변.
    """
    from market_intel.providers.sec_edgar import CONCEPT_CANDIDATES

    tags = [tag for _ns, tag in CONCEPT_CANDIDATES["capex"]]
    assert "PaymentsToAcquireOtherPropertyPlantAndEquipment" in tags, tags
    # 미지급 발생액은 현금흐름이 아니다 — FCF에 섞이면 조용히 오염된다.
    assert "CapitalExpendituresIncurredButNotYetPaid" not in tags


def test_capex_picks_the_newest_period_across_candidate_tags(settings):
    """회사가 태그를 갈아탄 경우 **최신 기간**이 이긴다 — 후보 순서가 아니라.

    EQIX가 정확히 이 모양이다: 옛 태그는 2026-03-31에서 멈추고 현행 태그가
    2026-06-30을 낸다. 순서로 골랐다면 3월 값이 최신인 척 실렸을 것이다."""
    facts = _companyfacts()
    facts["facts"]["us-gaap"]["PaymentsToAcquireProductiveAssets"] = {
        "units": {"USD": [_unit("2026-03-31", 11_000.0)]}
    }
    facts["facts"]["us-gaap"]["PaymentsToAcquireOtherPropertyPlantAndEquipment"] = {
        "units": {"USD": [_unit("2026-06-30", 22_000.0)]}
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json=TICKERS)
        if "/submissions/" in url:
            return httpx.Response(200, json=SUBMISSIONS)
        if "/companyfacts/" in url:
            return httpx.Response(200, json=facts)
        return httpx.Response(404, json={"error": "not found"})

    now = datetime.now(timezone.utc)
    ctx = CollectContext(
        cutoff=now, now=now, settings=settings,
        http=lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0),
        universe=[], logger=logging.getLogger("test"),
    )
    result = SecEdgarProvider().collect(ctx)

    capex = [f for f in result.facts if f.metric == "capex"]
    assert capex, "capex fact가 없다"
    assert capex[0].event_at.startswith("2026-06-30"), capex[0].event_at
    assert capex[0].value_num == 22_000.0
