"""ST3 — `backfill.financials` (spec 백필 §5 ST3).

네트워크를 타지 않는다(오라클 테스트 하나만 예외, `@pytest.mark.network`):
`httpx.MockTransport`로 고정 응답을 준다.

고정하는 계약:
  - 라이브와 같은 계보(S2): provider 이름 `"sec_edgar"`, `providers/sec_edgar.py`의
    `CONCEPT_CANDIDATES`/`US_CORE_TICKERS`/`PREDECESSOR_CIK`를 재사용(복사 금지).
  - **직접 보고된 개별 분기(80~100일)는 전부 fact로**, `known_at=filed`. 같은
    end에 filed/val 조합이 여럿이면 각각 별도 revision(재작성 이력 보존).
  - **누적치 차분**: 같은 (taxonomy,concept,start) 안에서 인접한 누적 기간만
    차분하고, 결과 기간이 80~100일일 때만 채택. 직접 보고된 분기가 있으면
    파생하지 않는다(concept 안 가림). known_at = max(양쪽 filed).
  - `free_cash_flow = operating_cash_flow - capex`, 기간이 정확히 일치할 때만.
  - `data_status`: **전부 `reconstructed`** (명세 S4.1 "백필이 append하는 모든
    revision"). 처음 구현은 직접 보고분을 `source_verified`로 넣었는데, 그러면
    §9의 사람 확인 단계("복원 완료 배지가 라이브와 구분돼 보이는가")가 무의미해진다.
    `reconstructed`는 "값을 우리가 계산했다"가 아니라 "그 시점의 정보차단선을 지켜
    재구성했다"는 뜻이고, 직접 보고분도 그렇다. 파생 여부는 `extra.formula`로 구분한다.
    `correction_reason='backfill:financials'`.

`test_derived_matches_reported`는 실제 SEC EDGAR 네트워크를 쓴다 — 오라클이
실재 데이터에만 있기 때문이다(모듈 docstring 참조). 나머지는 전부 오프라인.

`from conftest import` 금지 — 헬퍼는 이 파일 안에 있다(`test_pit_barrier.py`/
`test_macro.py`와 같은 관례, 과거 사고 재발 방지).
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from market_intel import db as db_mod
from market_intel.backfill import financials as fin_mod
from market_intel.config import Settings
from market_intel.http_client import SafeHttp

SINCE, UNTIL = date(2024, 1, 1), date(2025, 12, 31)


# --------------------------------------------------------------------------
# 픽스처 / 헬퍼
# --------------------------------------------------------------------------
@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "bf.db"), raw_dir=str(tmp_path / "raw"),
                    log_dir=str(tmp_path / "logs"), sec_user_agent="test-agent contact@example.com")


@pytest.fixture
def conn(settings):
    db_mod.init_db(settings.db_path)
    c = db_mod.connect(settings.db_path)
    yield c
    c.close()


def _unit(start: str, end: str, val: float, filed: str, form: str = "10-Q") -> dict:
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def _companyfacts(entries_by_concept: dict[tuple[str, str], list[dict]]) -> dict:
    facts: dict = {}
    for (taxonomy, concept), units in entries_by_concept.items():
        facts.setdefault(taxonomy, {})[concept] = {"units": {"USD": units}}
    return {"facts": facts}


def _sec_ctx(settings, tickers_map: dict[str, int], companyfacts_by_cik: dict[int, dict],
             *, capture: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if capture is not None:
            capture.append(url)
        if "company_tickers.json" in url:
            body = {str(i): {"cik_str": cik, "ticker": t, "title": t}
                    for i, (t, cik) in enumerate(tickers_map.items())}
            return httpx.Response(200, json=body)
        if "companyfacts" in url:
            for cik, payload in companyfacts_by_cik.items():
                if f"CIK{cik:010d}.json" in url:
                    return httpx.Response(200, json=payload)
            return httpx.Response(404, json={})
        return httpx.Response(404, json={})

    def http(name):
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0)
    return http


def _run(conn, settings, http, **kw):
    return fin_mod.run(conn, settings, "financials", since=kw.pop("since", SINCE),
                       until=kw.pop("until", UNTIL), subjects=kw.pop("subjects", ["MSFT"]),
                       dry_run=kw.pop("dry_run", False), http=http)


def _rows(conn, subject="MSFT", metric=None):
    q = "SELECT * FROM fact_revisions WHERE subject=?"
    params = [subject]
    if metric:
        q += " AND metric=?"
        params.append(metric)
    q += " ORDER BY event_at, known_at"
    return list(conn.execute(q, params))


MSFT_CIK = 789019


# --------------------------------------------------------------------------
# 직접 보고된 개별 분기
# --------------------------------------------------------------------------
def test_direct_quarters_become_facts_with_known_at_filed(conn, settings):
    data = _companyfacts({
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): [
            _unit("2024-01-01", "2024-03-31", 1000, "2024-04-25"),
            _unit("2024-04-01", "2024-06-30", 1100, "2024-07-25"),
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http)
    # PARTIAL이 맞다 — 이 픽스처는 revenue만 준다. 나머지 3개 지표는
    # concept_not_found로 보고돼야 한다(조용히 빠지면 안 된다).
    assert result.status == "PARTIAL", result.line()
    assert result.appended == 2, result.line()
    assert "operating_income:concept_not_found" in result.detail, result.detail

    rows = _rows(conn, metric="revenue")
    assert [r["fact_id"] for r in rows] == [
        "sec_edgar:MSFT:revenue:20240331", "sec_edgar:MSFT:revenue:20240630",
    ]
    assert rows[0]["known_at"] == "2024-04-25T00:00:00+00:00"
    assert rows[0]["event_at"] == "2024-03-31T00:00:00+00:00"
    assert rows[0]["data_status"] == "reconstructed"  # S4.1 — 백필은 전부 복원 완료
    assert "formula" not in json.loads(rows[0]["extra_json"]), "직접 보고분에 차분식이 붙었다"
    assert rows[0]["correction_reason"] == "backfill:financials"
    assert rows[0]["comparison_basis"] == "quarterly"


def test_restated_quarter_becomes_a_separate_revision(conn, settings):
    """같은 end에 filed/val 조합이 여럿 -> 각각 별도 revision(재작성 이력 보존)."""
    data = _companyfacts({
        ("us-gaap", "OperatingIncomeLoss"): [
            _unit("2024-01-01", "2024-03-31", 200, "2024-04-25"),
            _unit("2024-01-01", "2024-03-31", 210, "2025-04-25"),  # 다음 해 비교치로 재게재
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http)
    assert result.appended == 2, result.line()
    rows = _rows(conn, metric="operating_income")
    assert len(rows) == 2
    assert {r["fact_id"] for r in rows} == {"sec_edgar:MSFT:operating_income:20240331"}
    assert sorted(r["value_num"] for r in rows) == [200, 210]
    assert sorted(r["known_at"] for r in rows) == [
        "2024-04-25T00:00:00+00:00", "2025-04-25T00:00:00+00:00",
    ]


# --------------------------------------------------------------------------
# 누적치 차분
# --------------------------------------------------------------------------
def test_cumulative_diff_derives_the_missing_quarter(conn, settings):
    data = _companyfacts({
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): [
            _unit("2024-01-01", "2024-03-31", 300, "2024-04-25"),  # Q1 직접
            _unit("2024-01-01", "2024-06-30", 700, "2024-07-25"),  # H1 누적
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http)
    assert result.appended == 2, result.line()  # Q1(직접) + Q2(파생)

    rows = {r["fact_id"]: r for r in _rows(conn, metric="operating_cash_flow")}
    q1 = rows["sec_edgar:MSFT:operating_cash_flow:20240331"]
    q2 = rows["sec_edgar:MSFT:operating_cash_flow:20240630"]
    assert q1["data_status"] == "reconstructed" and q1["value_num"] == 300
    assert q2["data_status"] == "reconstructed" and q2["value_num"] == 400  # 700-300
    assert q2["known_at"] == "2024-07-25T00:00:00+00:00"  # max(filed Q1, filed H1)
    assert q2["correction_reason"] == "backfill:financials"

    extra = json.loads(q2["extra_json"])
    assert extra["formula"]
    assert extra["long_val"] == 700 and extra["short_val"] == 300
    assert extra["long_filed"] == "2024-07-25" and extra["short_filed"] == "2024-04-25"


def test_direct_quarter_wins_over_derivation_even_if_derived_would_differ(conn, settings):
    """직접 보고된 분기가 있으면 파생하지 않는다 — concept을 가리지 않고
    검사한다(태그 이동이 조용히 숫자를 오염시키는 것을 막는다)."""
    data = _companyfacts({
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): [
            _unit("2024-01-01", "2024-03-31", 300, "2024-04-25"),   # Q1
            _unit("2024-04-01", "2024-06-30", 999, "2024-07-25"),   # Q2 직접(파생과 다른 값)
            _unit("2024-01-01", "2024-06-30", 700, "2024-07-25"),   # H1 누적(파생하면 400이 됨)
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http)
    rows = _rows(conn, metric="operating_cash_flow")
    q2_rows = [r for r in rows if r["fact_id"] == "sec_edgar:MSFT:operating_cash_flow:20240630"]
    assert len(q2_rows) == 1, "직접치가 있는데 파생치가 별도 revision으로 또 들어갔다"
    assert q2_rows[0]["value_num"] == 999
    assert q2_rows[0]["data_status"] == "reconstructed"


def test_fiscal_year_boundary_never_crosses(conn, settings):
    """서로 다른 회계연도(=서로 다른 누적 시작일)는 절대 섞여 차분되지 않는다.
    두 회계연도 모두 Q1+H1을 갖게 해서 각자 Q2를 파생시키고, **자기 연도의
    Q1로만** 파생됐는지 확인한다 — 옆 연도 Q1과 섞였으면 값이 달라진다.
    `fy`/`fp` 필드가 아니라 날짜(start)로 회계연도 경계를 잡는다는 설계의
    직접 증거."""
    data = _companyfacts({
        ("us-gaap", "OperatingIncomeLoss"): [
            _unit("2024-01-01", "2024-03-31", 100, "2024-04-25"),   # FY2024 Q1
            _unit("2024-01-01", "2024-06-30", 300, "2024-07-25"),   # FY2024 H1 -> Q2=200
            _unit("2025-01-01", "2025-03-31", 150, "2025-04-25"),   # FY2025 Q1 (다른 값)
            _unit("2025-01-01", "2025-06-30", 500, "2025-07-25"),   # FY2025 H1 -> Q2=350
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    _run(conn, settings, http, until=date(2025, 12, 31))
    rows = {r["fact_id"]: r["value_num"] for r in _rows(conn, metric="operating_income")}
    assert rows["sec_edgar:MSFT:operating_income:20240630"] == 200, (
        "FY2024 Q2가 옆 연도 값과 섞였다: " + str(rows)
    )
    assert rows["sec_edgar:MSFT:operating_income:20250630"] == 350, (
        "FY2025 Q2가 옆 연도 값과 섞였다: " + str(rows)
    )


def test_missing_short_cumulative_does_not_fabricate_a_quarter(conn, settings):
    """짧은 쪽 누적치(반기·9개월)가 없으면 Q1과 연간만 남는다. 그 둘을 그대로
    빼면 9개월짜리 값이 '분기'로 둔갑한다 — duration 필터가 이를 막는다."""
    data = _companyfacts({
        ("us-gaap", "Revenues"): [
            _unit("2024-01-01", "2024-03-31", 300, "2024-04-25", form="10-Q"),
            _unit("2024-01-01", "2024-12-31", 1300, "2025-02-01", form="10-K"),
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http)
    rows = _rows(conn, metric="revenue")
    assert len(rows) == 1, "9개월짜리 값이 분기로 잘못 파생됐다"
    assert rows[0]["value_num"] == 300


def test_tag_migration_does_not_diff_across_concepts(conn, settings):
    """회사가 태그를 갈아탄 경계에서는 차분하지 않는다(서로 다른 concept이라
    같은 그룹에 들어가지 않는다)."""
    data = _companyfacts({
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): [
            _unit("2024-01-01", "2024-03-31", 300, "2024-04-25"),
        ],
        ("us-gaap", "Revenues"): [
            _unit("2024-01-01", "2024-06-30", 700, "2024-07-25"),
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http)
    rows = _rows(conn, metric="revenue")
    # 새 태그의 Q1(직접) 하나만 있어야 한다. 옛 태그의 H1을 새 태그의 Q1과
    # 섞어 파생한 Q2(400)가 있으면 태그 이동을 무시한 것이다.
    assert len(rows) == 1
    assert rows[0]["value_num"] == 300
    assert not any(r["value_num"] == 400 for r in rows)


# --------------------------------------------------------------------------
# free_cash_flow
# --------------------------------------------------------------------------
def test_fcf_only_when_period_matches_exactly(conn, settings):
    data = _companyfacts({
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"): [
            _unit("2024-01-01", "2024-03-31", 500, "2024-04-25"),
            _unit("2024-04-01", "2024-06-30", 600, "2024-07-25"),  # capex 짝이 없다
        ],
        ("us-gaap", "PaymentsToAcquireProductiveAssets"): [
            _unit("2024-01-01", "2024-03-31", 100, "2024-04-25"),
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http)
    fcf_rows = _rows(conn, metric="free_cash_flow")
    assert len(fcf_rows) == 1
    assert fcf_rows[0]["value_num"] == 400  # 500-100
    assert fcf_rows[0]["event_at"] == "2024-03-31T00:00:00+00:00"
    assert fcf_rows[0]["data_status"] == "reconstructed"
    # 사유는 개수가 아니라 **어느 기간에 무엇이 없었는지**여야 한다. 대칭차집합
    # 개수만 세면 창 밖·비분기 기간까지 뭉뚱그려져 아무 정보가 없다.
    assert "기간불일치" in result.detail, result.detail
    assert "capex_없음" in result.detail, result.detail
    assert "2024-04-01~2024-06-30" in result.detail, result.detail


# --------------------------------------------------------------------------
# 계보 / CIK / 폴백 / 멱등 / dry-run
# --------------------------------------------------------------------------
def test_cik_is_resolved_live_not_hardcoded(conn, settings):
    """company_tickers.json을 실제로 호출해서 CIK를 얻는다 — 하드코딩이면
    임의의 CIK(555555)로도 데이터를 찾을 수 없다."""
    urls: list[str] = []
    weird_cik = 555555
    data = _companyfacts({
        ("us-gaap", "Revenues"): [_unit("2024-01-01", "2024-03-31", 42, "2024-04-25")],
    })
    http = _sec_ctx(settings, {"MSFT": weird_cik}, {weird_cik: data}, capture=urls)
    result = _run(conn, settings, http)
    assert result.appended >= 1, result.line()
    assert any("company_tickers.json" in u for u in urls)
    assert any(f"CIK{weird_cik:010d}.json" in u for u in urls)


def test_predecessor_cik_fallback_for_xom(conn, settings):
    """XOM의 현재 CIK가 정기보고서를 하나도 안 낸(신설 지주사) 경우
    PREDECESSOR_CIK로 대체하고 어느 entity에서 왔는지 남긴다."""
    from market_intel.providers.sec_edgar import PREDECESSOR_CIK
    xom_new_cik = 2115436
    xom_old_cik = PREDECESSOR_CIK["XOM"]
    empty = {"facts": {}}
    real = _companyfacts({
        ("us-gaap", "Revenues"): [_unit("2024-01-01", "2024-03-31", 9000, "2024-04-25")],
    })
    http = _sec_ctx(settings, {"XOM": xom_new_cik},
                    {xom_new_cik: empty, xom_old_cik: real})
    result = _run(conn, settings, http, subjects=["XOM"])
    assert result.appended >= 1, result.line()
    rows = _rows(conn, subject="XOM", metric="revenue")
    assert len(rows) == 1 and rows[0]["value_num"] == 9000
    extra = json.loads(rows[0]["extra_json"])
    assert extra["reporting_entity_cik"] == xom_old_cik
    assert extra["reporting_entity_note"] == "predecessor registrant"


def test_rerun_is_idempotent(conn, settings):
    data = _companyfacts({
        ("us-gaap", "Revenues"): [_unit("2024-01-01", "2024-03-31", 42, "2024-04-25")],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    first = _run(conn, settings, http)
    second = _run(conn, settings, http)
    assert first.appended >= 1, first.line()
    assert second.appended == 0, second.line()
    assert second.skipped_existing >= 1


def test_dry_run_writes_nothing(conn, settings):
    data = _companyfacts({
        ("us-gaap", "Revenues"): [_unit("2024-01-01", "2024-03-31", 42, "2024-04-25")],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http, dry_run=True)
    assert result.fetched >= 1 and result.appended == 0, result.line()
    assert conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"] == 0


def test_since_until_window_limits_what_is_emitted(conn, settings):
    data = _companyfacts({
        ("us-gaap", "Revenues"): [
            _unit("2023-01-01", "2023-03-31", 10, "2023-04-25"),  # 창 밖
            _unit("2024-01-01", "2024-03-31", 20, "2024-04-25"),  # 창 안
        ],
    })
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK}, {MSFT_CIK: data})
    result = _run(conn, settings, http, since=date(2024, 1, 1), until=date(2024, 12, 31))
    rows = _rows(conn, metric="revenue")
    assert len(rows) == 1 and rows[0]["value_num"] == 20


def test_subjects_filter_limits_tickers(conn, settings):
    data_msft = _companyfacts({
        ("us-gaap", "Revenues"): [_unit("2024-01-01", "2024-03-31", 1, "2024-04-25")],
    })
    data_nvda = _companyfacts({
        ("us-gaap", "Revenues"): [_unit("2024-01-01", "2024-03-31", 2, "2024-04-25")],
    })
    from market_intel.providers.sec_edgar import US_CORE_TICKERS
    assert "NVDA" in US_CORE_TICKERS
    nvda_cik = 1045810
    http = _sec_ctx(settings, {"MSFT": MSFT_CIK, "NVDA": nvda_cik},
                    {MSFT_CIK: data_msft, nvda_cik: data_nvda})
    _run(conn, settings, http, subjects=["MSFT"])
    subjects_in_db = {r["subject"] for r in conn.execute("SELECT DISTINCT subject FROM fact_revisions")}
    assert subjects_in_db == {"MSFT"}


# --------------------------------------------------------------------------
# ★ 오라클 — 실제 SEC 데이터 (spec ST3-(d))
# --------------------------------------------------------------------------
@pytest.mark.network
def test_derived_matches_reported():
    """MSFT는 현금흐름표 항목을 개별 분기로도, 누적으로도 낸다(spec §2.3 실측
    표) — 그래서 "직접 보고된 개별 분기"와 "차분으로 파생한 값"이 둘 다 있는
    (subject,metric,period_end)가 실재한다. 모듈 docstring/`explore_oracle.py`에
    적힌 대로 operating_income은 2016 회계연도 재작성 때문에 제외한다(진짜
    재무제표 재작성이지 알고리즘 결함이 아니다) — revenue/operating_cash_flow/
    capex 세 지표만 오라클로 쓴다."""
    resp = httpx.get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{MSFT_CIK:010d}.json",
        headers={"User-Agent": "market-intel-test test@example.com"}, timeout=20,
    )
    assert resp.status_code == 200, resp.status_code
    data = resp.json()

    pairs = 0
    for metric in ("revenue", "operating_cash_flow", "capex"):
        pool = fin_mod.pool_entries(data, metric)
        direct_ends = fin_mod._direct_end_set(pool)
        direct = fin_mod.direct_quarter_entries(pool)
        # 오라클 비교는 개념(concept)을 맞춰야 공평하다 — 태그가 다르면 서로
        # 다른 것을 재는 것이므로 알고리즘 검증이 아니라 태그 불일치 검증이
        # 된다. 그래서 direct도 (concept, end)로 인덱싱한다.
        direct_by_concept_end: dict[tuple[str, str, str], float] = {}
        for u in direct:
            key = (u["taxonomy"], u["concept"], u["end"])
            cur = direct_by_concept_end.get(key)
            if cur is None:
                direct_by_concept_end[key] = u["val"]

        # direct_ends를 비워서(파생 함수의 "직접 있으면 파생 안 함" 스킵을
        # 끄고) 수학 자체가 맞는지 순수하게 검증한다 — 이게 오라클 대조다.
        derived = fin_mod.derive_quarter_entries(pool, direct_ends=set())
        for d in derived:
            key = (d["taxonomy"], d["concept"], d["end"])
            if key not in direct_by_concept_end:
                continue
            reported = direct_by_concept_end[key]
            if reported == 0:
                continue
            err = abs(d["val"] - reported) / abs(reported)
            assert err < 0.005, (
                f"{metric} {key}: direct={reported} derived={d['val']} err={err:.4%}"
            )
            pairs += 1

    assert pairs >= 20, f"검사쌍이 부족하다: {pairs}"


# --- 태그 교차 충돌 (블라인드 심사가 양쪽 공통 HIGH로 잡은 것) ---------------
#
# `engine._fact_id`는 concept을 안 쓴다(`provider:subject:metric:YYYYMMDD`).
# 그래서 서로 다른 XBRL 태그가 **같은 기간**을 **같은 문서**로 보고하면
# fact_id도 known_at도 같은 revision이 둘 생기고, `facts_as_of`가
# (known_at, revision_no)로 고르므로 **삽입 순서**가 승자를 정한다.
#
# 실측(AEP revenue 2025-06-30, 둘 다 filed=2025-07-30 form=10-Q):
#   rank0 RevenueFromContractWithCustomerExcludingAssessedTax = 5,055,400,000
#   rank1 Revenues                                            = 5,086,900,000
# 백필은 rank1을, 라이브 `sec_edgar._period_key`는 rank0을 고른다 —
# **같은 분기가 경로에 따라 3,150만 달러 다르다.** 원장이 append-only라
# 잘못 넣으면 되돌릴 수 없다.
#
# 같은 버그가 라이브에서 한 번 잡힌 적이 있다("previously order-dependent —
# repair.md finding #1"). 백필 계층에서 그대로 재발했다.

def _aep_like_pool():
    """AEP 실데이터의 모양 그대로 — 두 태그, 같은 기간, 같은 filed."""
    return [
        {"taxonomy": "us-gaap", "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
         "start": "2025-04-01", "end": "2025-06-30", "val": 5_055_400_000.0, "filed": "2025-07-30"},
        {"taxonomy": "us-gaap", "concept": "Revenues",
         "start": "2025-04-01", "end": "2025-06-30", "val": 5_086_900_000.0, "filed": "2025-07-30"},
    ]


def test_same_period_same_filing_yields_one_variant_not_two():
    """같은 fact_id·같은 known_at에 값이 다른 판이 둘 생기면 원장이 '그 시점에
    알려져 있던 값'에 답할 수 없다."""
    from market_intel.backfill import financials as F

    variants, _current = F.build_metric_facts(_aep_like_pool(), "revenue")
    keys = [(v["end"], v["known_at"]) for v in variants]
    assert len(keys) == len(set(keys)), f"같은 (기간, known_at)에 판이 둘: {variants}"


def test_the_winner_matches_the_live_provider_rule():
    """라이브와 다른 값을 고르면 같은 분기가 수집 경로에 따라 달라진다."""
    from market_intel.backfill import financials as F

    variants, _ = F.build_metric_facts(_aep_like_pool(), "revenue")
    assert len(variants) == 1
    assert variants[0]["val"] == 5_055_400_000.0, (
        f"라이브(_period_key)는 rank0을 고른다. 백필이 고른 값: {variants[0]['val']:,}")
    assert "RevenueFromContract" in variants[0]["extra"]["xbrl_concept"]


def test_shortest_period_wins_before_concept_rank():
    """라이브 규칙의 1순위는 기간 길이다 — 같은 end에 분기치와 누적치가 함께
    올라오면 분기치를 골라야 한다(누적치를 분기로 싣는 것이 더 큰 오류)."""
    from market_intel.backfill import financials as F

    pool = [
        # rank0인데 180일 누적
        {"taxonomy": "us-gaap", "concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
         "start": "2025-01-01", "end": "2025-06-30", "val": 10_470_500_000.0, "filed": "2025-07-30"},
        # rank1인데 90일 분기
        {"taxonomy": "us-gaap", "concept": "Revenues",
         "start": "2025-04-01", "end": "2025-06-30", "val": 5_086_900_000.0, "filed": "2025-07-30"},
    ]
    variants, _ = F.build_metric_facts(pool, "revenue")
    ends = [v for v in variants if v["end"] == "2025-06-30"]
    assert len(ends) == 1, ends
    assert ends[0]["val"] == 5_086_900_000.0, "누적치가 분기 자리에 실렸다"


def test_genuine_restatements_still_produce_separate_revisions():
    """고치려는 것은 '같은 시점의 모호함'이지 재작성 이력이 아니다 —
    filed가 다르면 그때 알려져 있던 값이 서로 다른 것이므로 둘 다 남아야 한다."""
    from market_intel.backfill import financials as F

    pool = [
        {"taxonomy": "us-gaap", "concept": "Revenues", "start": "2025-04-01",
         "end": "2025-06-30", "val": 100.0, "filed": "2025-07-30"},
        {"taxonomy": "us-gaap", "concept": "Revenues", "start": "2025-04-01",
         "end": "2025-06-30", "val": 110.0, "filed": "2025-11-01"},   # 재작성
    ]
    variants, _ = F.build_metric_facts(pool, "revenue")
    assert sorted(v["val"] for v in variants) == [100.0, 110.0], variants
    assert len({v["known_at"] for v in variants}) == 2
