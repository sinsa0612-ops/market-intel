"""`backfill.krx_breadth` (spec 백필 §5, krx-breadth spec §5).

네트워크를 타지 않는다: `httpx.MockTransport`로 고정 응답을 준다.

고정하는 계약:
  - `known_at = event_at` (S3: 시장 폭도 종가처럼 마감 시각에 확정된다)
  - `fact_id`가 라이브 provider(`providers/krx_breadth.py`)와 **같은 계보**(S2)
  - `data_status='reconstructed'` + `correction_reason='backfill:krx_breadth'` (S4)
  - 두 번 돌리면 append 0 (멱등)
  - `--dry-run`은 DB에 한 줄도 쓰지 않는다
  - 휴장일(빈 응답)은 결측이 아니다 — missing에 쌓지 않고 그냥 건너뛴다
  - 연속 실패는 조용히 건너뛰지 않고 중단·보고한다

날짜는 전부 2026-01(과거로 확정된 시점)로 고정한다 — 실제 실행 시각과
무관하게 "미마감 세션 건너뛰기" 가드에 걸리지 않도록.

`from conftest import` 금지 — 헬퍼는 이 파일 안에 있다(검수서 F12).
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from market_intel import db as db_mod
from market_intel.backfill import krx_breadth as krx_bf_mod
from market_intel.config import Settings
from market_intel.http_client import SafeHttp


def _row(*, isu="000001", cmp_prev, fluc, vol, mktcap, trdval, bas_dd):
    return {
        "BAS_DD": bas_dd, "ISU_CD": isu, "ISU_NM": isu, "MKT_NM": "KOSPI",
        "SECT_TP_NM": "", "TDD_CLSPRC": "0", "CMPPREVDD_PRC": str(cmp_prev),
        "FLUC_RT": str(fluc), "TDD_OPNPRC": "0", "TDD_HGPRC": "0", "TDD_LWPRC": "0",
        "ACC_TRDVOL": str(vol), "ACC_TRDVAL": str(trdval), "MKTCAP": str(mktcap),
        "LIST_SHRS": "0",
    }


def _rows_for(bas_dd: str) -> list[dict]:
    return [
        _row(isu="000001", cmp_prev=50, fluc=25, vol=100, mktcap=200, trdval=1000, bas_dd=bas_dd),
        _row(isu="000002", cmp_prev=-10, fluc=-10, vol=100, mktcap=90, trdval=500, bas_dd=bas_dd),
    ]


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(db_path=str(tmp_path / "bf.db"), raw_dir=str(tmp_path / "raw"),
                 log_dir=str(tmp_path / "logs"))
    s.krx_api_key = "FAKEKRXKEY1234567890"
    return s


@pytest.fixture
def conn(settings):
    db_mod.init_db(settings.db_path)
    c = db_mod.connect(settings.db_path)
    yield c
    c.close()


def _handler(rows_by_date: dict[str, list[dict]], *, capture=None, always_fail=False):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        bas_dd = dict(request.url.params).get("basDd", "")
        if capture is not None:
            capture.append((url, bas_dd))
        if always_fail:
            return httpx.Response(500)
        if "stk_bydd_trd" in url or "ksq_bydd_trd" in url:
            return httpx.Response(200, json={"OutBlock_1": rows_by_date.get(bas_dd, [])})
        return httpx.Response(404)

    return handle


def _http(settings, handler):
    def http(name):
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0)

    return http


def _run(conn, settings, http, **kw):
    return krx_bf_mod.run(
        conn, settings, "krx_breadth",
        since=kw.pop("since", date(2026, 1, 5)), until=kw.pop("until", date(2026, 1, 5)),
        subjects=kw.pop("subjects", None), dry_run=kw.pop("dry_run", False), http=http,
    )


def test_no_credentials_never_touches_the_network(conn, settings):
    settings.krx_api_key = ""
    called: list = []
    http = lambda name: called.append(name) or SafeHttp(name, settings, rate=0)  # noqa: E731
    result = _run(conn, settings, http)
    assert result.status == "NO_DATA" and result.reason_code == "키없음"
    assert called == []


def test_known_at_equals_event_at_and_lineage_matches_live(conn, settings):
    """S3: known_at은 마감 시각 그대로. S2: fact_id는 라이브와 같은 계보."""
    rows_by_date = {"20260105": _rows_for("20260105")}
    http = _http(settings, _handler(rows_by_date))
    result = _run(conn, settings, http)
    assert result.status == "OK", result.detail
    assert result.appended == 14, result.detail  # 2시장 x 7 metric

    rows = list(conn.execute(
        "SELECT fact_id, subject, event_at, known_at, data_status, correction_reason "
        "FROM fact_revisions WHERE metric='breadth_advancers' ORDER BY subject"))
    assert [r["fact_id"] for r in rows] == [
        "krx:KOSDAQ:breadth_advancers:20260105", "krx:KOSPI:breadth_advancers:20260105",
    ], "라이브와 다른 fact_id를 쓰면 계보가 갈라진다"
    for r in rows:
        assert r["known_at"] == r["event_at"], "시장 폭은 사후 수정되지 않는다 (S3)"
        assert r["data_status"] == "reconstructed"
        assert r["correction_reason"] == "backfill:krx_breadth"
    assert rows[0]["event_at"] == "2026-01-05T06:30:00+00:00", rows[0]["event_at"]


def test_rerun_is_idempotent(conn, settings):
    rows_by_date = {"20260105": _rows_for("20260105")}
    http = _http(settings, _handler(rows_by_date))
    first = _run(conn, settings, http)
    second = _run(conn, settings, http)
    assert first.appended == 14, first.detail
    assert second.appended == 0, second.detail
    assert second.skipped_existing == 14, second.detail


def test_dry_run_writes_nothing(conn, settings):
    rows_by_date = {"20260105": _rows_for("20260105")}
    http = _http(settings, _handler(rows_by_date))
    result = _run(conn, settings, http, dry_run=True)
    assert result.fetched == 14, result.detail
    assert result.appended == 0
    assert conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM raw_snapshots").fetchone()["c"] == 0


def test_holiday_empty_response_is_skipped_not_counted_as_missing(conn, settings):
    """휴장일은 결측이 아니다(spec §5 마지막 줄) — missing에 쌓이면 안 된다."""
    rows_by_date = {"20260106": _rows_for("20260106")}  # 1/5는 휴장(빈 응답) 가정
    http = _http(settings, _handler(rows_by_date))
    result = _run(conn, settings, http, since=date(2026, 1, 5), until=date(2026, 1, 6))
    assert result.status == "OK", result.detail
    assert result.appended == 14  # 1/6 하루치만
    assert "20260105" not in (result.detail or "")


def test_subjects_filter_restricts_to_named_market(conn, settings):
    rows_by_date = {"20260105": _rows_for("20260105")}
    http = _http(settings, _handler(rows_by_date))
    result = _run(conn, settings, http, subjects=["KOSPI"])
    assert result.status == "OK", result.detail
    assert result.appended == 7  # KOSPI 7개 metric만
    subjects = {r["subject"] for r in conn.execute("SELECT DISTINCT subject FROM fact_revisions")}
    assert subjects == {"KOSPI"}


def test_consecutive_failures_abort_instead_of_silently_skipping(conn, settings):
    """spec §5: 연속 실패가 나면 조용히 건너뛰지 말고 중단·보고한다."""
    http = _http(settings, _handler({}, always_fail=True))
    result = _run(conn, settings, http, since=date(2026, 1, 5), until=date(2026, 1, 20))
    assert result.status == "ERROR", result.detail
    assert result.reason_code == "network_error"
    assert result.appended == 0
    assert str(krx_bf_mod.MAX_CONSECUTIVE_FAILURES) in result.detail
