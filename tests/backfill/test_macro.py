"""ST2 — `backfill.macro` (spec 백필 §5 ST2 항목 2·3).

네트워크를 타지 않는다: `httpx.MockTransport`로 고정 응답을 준다.

고정하는 계약:
  - **미국(FRED)**: `known_at = 응답 행의 realtime_start` (S3 — ALFRED가 "이 값이
    유효해진 날"을 준다). `value == "."`은 건너뛴다. `realtime_start=1776-07-04`
    같은 전체-vintage 요청을 보내지 않는다 — 일간 계열에서 HTTP 400으로 죽는다
    (spec §2.3 반증 5). 그래서 `realtime_start`를 백필 시작일로 묶는다.
  - **한국(ECOS)**: 개정되지 않는 두 계열만. `data_status='partial'`(빈티지를
    제공하지 않으므로 `reconstructed`라고 말할 수 없다). 수출 두 계열은
    호출하지 않고 `data_gaps`에 등록한다.
  - 둘 다 라이브와 같은 계보(S2): provider 이름 `"fred"`/`"ecos"`, 같은 subject
    규약, 같은 `event_at` 도출.

`from conftest import` 금지 — 헬퍼는 이 파일 안에 있다(검수서 F12).
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from market_intel import db as db_mod
from market_intel.backfill import macro as macro_mod
from market_intel.config import Settings
from market_intel.http_client import SafeHttp

SINCE, UNTIL = date(2023, 8, 1), date(2026, 8, 1)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "bf.db"), raw_dir=str(tmp_path / "raw"),
                    log_dir=str(tmp_path / "logs"))


@pytest.fixture
def conn(settings):
    db_mod.init_db(settings.db_path)
    c = db_mod.connect(settings.db_path)
    yield c
    c.close()


def _observations(rows: list[dict]) -> dict:
    return {"observations": rows}


def _obs(date_str: str, value: str, realtime_start: str) -> dict:
    return {"date": date_str, "value": value,
            "realtime_start": realtime_start, "realtime_end": "9999-12-31"}


def _fred_ctx(settings, rows, *, unit="%", capture=None):
    """`/fred/series/observations` -> rows, `/fred/series` -> units_short."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if capture is not None:
            capture.append(url)
        if "/fred/series/observations" in url:
            return httpx.Response(200, json=_observations(rows))
        if "/fred/series" in url:
            return httpx.Response(200, json={"seriess": [{"units_short": unit}]})
        return httpx.Response(404, json={})

    def http(name):
        return SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0)

    return http


def _run_us(conn, settings, http, **kw):
    return macro_mod.run(conn, settings, "macro_us", since=SINCE, until=UNTIL,
                         subjects=kw.pop("subjects", ["UNRATE"]),
                         dry_run=kw.pop("dry_run", False), http=http)


# --- 미국 (FRED) ------------------------------------------------------------

def test_known_at_comes_from_realtime_start(conn, settings):
    """S3: FRED는 "이 값이 언제 유효해졌는지"를 준다. 관측일이 아니라 그 날짜가
    known_at이다 — 관측일을 쓰면 T+2 발표 지연만큼 후견지명이 생긴다."""
    http = _fred_ctx(settings, [
        _obs("2026-06-01", "4.2", "2026-07-03"),
        _obs("2026-07-01", "4.3", "2026-08-01"),
    ])
    result = _run_us(conn, settings, http)
    assert result.status == "OK", result.line()
    assert result.appended == 2, result.line()

    rows = list(conn.execute(
        "SELECT fact_id, event_at, known_at, value_num, unit, data_status, correction_reason "
        "FROM fact_revisions ORDER BY event_at"))
    assert [r["fact_id"] for r in rows] == [
        "fred:UNRATE:value:20260601", "fred:UNRATE:value:20260701",
    ], "라이브와 다른 fact_id를 쓰면 계보가 갈라진다"
    assert rows[0]["known_at"] == "2026-07-03T00:00:00+00:00", rows[0]["known_at"]
    assert rows[0]["event_at"] == "2026-06-01T00:00:00+00:00"
    assert rows[0]["known_at"] > rows[0]["event_at"], "발표 지연이 데이터에 남아야 한다"
    assert rows[0]["unit"] == "%", "`lin`(변환 방식)이 아니라 실제 단위여야 한다"
    assert rows[0]["data_status"] == "reconstructed"
    assert rows[0]["correction_reason"] == "backfill:macro_us"


def test_missing_value_dot_is_skipped(conn, settings):
    """FRED는 결측을 `.`로 준다. float()에 넣으면 죽고, 0으로 채우면 거짓말이다."""
    http = _fred_ctx(settings, [
        _obs("2026-06-01", ".", "2026-07-03"),
        _obs("2026-07-01", "4.3", "2026-08-01"),
    ])
    result = _run_us(conn, settings, http)
    assert result.appended == 1, result.line()
    assert conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"] == 1


def test_realtime_start_is_bounded_by_since_not_1776(conn, settings):
    """spec §2.3 반증 5: 전체 vintage를 요구하면 일간 계열이 HTTP 400으로 죽는다
    (2000 vintage 상한). `realtime_start`를 백필 시작일로 묶는다."""
    urls: list[str] = []
    http = _fred_ctx(settings, [_obs("2026-07-01", "4.3", "2026-08-01")], capture=urls)
    _run_us(conn, settings, http)

    obs_urls = [u for u in urls if "/observations" in u]
    assert obs_urls, urls
    assert "1776-07-04" not in obs_urls[0], obs_urls[0]
    assert f"realtime_start={SINCE.isoformat()}" in obs_urls[0], obs_urls[0]
    assert f"observation_start={SINCE.isoformat()}" in obs_urls[0], obs_urls[0]
    assert f"realtime_end={UNTIL.isoformat()}" in obs_urls[0], obs_urls[0]


def test_same_observation_with_two_vintages_keeps_both(conn, settings):
    """개정은 덮어쓰기가 아니다 — 그때 알려져 있던 값이 원장에 남아야 한다."""
    http = _fred_ctx(settings, [
        _obs("2026-06-01", "4.2", "2026-07-03"),
        _obs("2026-06-01", "4.1", "2026-07-31"),   # 개정판
    ])
    result = _run_us(conn, settings, http)
    assert result.appended == 2, result.line()
    rows = list(conn.execute(
        "SELECT known_at, value_num FROM fact_revisions "
        "WHERE fact_id='fred:UNRATE:value:20260601' ORDER BY known_at"))
    assert [r["value_num"] for r in rows] == [4.2, 4.1]


def test_us_rerun_is_idempotent(conn, settings):
    http = _fred_ctx(settings, [_obs("2026-07-01", "4.3", "2026-08-01")])
    first = _run_us(conn, settings, http)
    second = _run_us(conn, settings, http)
    assert (first.appended, second.appended) == (1, 0), (first.line(), second.line())
    assert second.skipped_existing == 1


def test_us_dry_run_writes_nothing(conn, settings):
    http = _fred_ctx(settings, [_obs("2026-07-01", "4.3", "2026-08-01")])
    result = _run_us(conn, settings, http, dry_run=True)
    assert result.fetched == 1 and result.appended == 0, result.line()
    assert conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"] == 0


def test_no_fred_key_never_touches_the_network(conn, settings):
    """키가 없으면 호출조차 하지 않고 이유를 남긴다(라이브 provider와 같은 규율)."""
    settings.fred_api_key = ""
    called = []
    result = macro_mod.run(conn, settings, "macro_us", since=SINCE, until=UNTIL,
                           subjects=None, dry_run=False,
                           http=lambda name: called.append(name))
    assert result.status == "NO_DATA" and result.reason_code == "키없음", result.line()
    assert called == []


# --- 한국 (ECOS) ------------------------------------------------------------

def _ecos_ctx(settings, rows=None, capture=None):
    """ECOS는 계열마다 주기가 다르다 — 월간은 `202606`, 일간은 `20260630`.
    한쪽 형식을 두 계열에 다 주면 실제와 다른 것을 재게 된다."""
    monthly = [{"TIME": "202606", "DATA_VALUE": "2.5", "UNIT_NAME": "%", "STAT_NAME": "기준금리"}]
    daily = [{"TIME": "20260630", "DATA_VALUE": "1436.6", "UNIT_NAME": "KRW", "STAT_NAME": "원/달러"}]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if capture is not None:
            capture.append(url)
        picked = rows if rows is not None else (monthly if "/M/" in url else daily)
        return httpx.Response(200, json={"StatisticSearch": {"row": picked}})

    return lambda name: SafeHttp(name, settings, transport=httpx.MockTransport(handler), rate=0)


def _run_kr(conn, settings, http, **kw):
    return macro_mod.run(conn, settings, "macro_kr", since=SINCE, until=UNTIL,
                         subjects=kw.pop("subjects", None),
                         dry_run=kw.pop("dry_run", False), http=http)


def test_kr_only_the_two_unrevised_series_are_backfilled(conn, settings):
    """ECOS는 빈티지를 제공하지 않는다(§2.4 실측). 개정되는 계열은 "그때 알 수
    있었던 값"을 복원할 방법이 없으므로 손대지 않는다."""
    urls: list[str] = []
    http = _ecos_ctx(settings, capture=urls)
    result = _run_kr(conn, settings, http)

    assert result.status == "PARTIAL", "한계가 결과에 드러나야 한다: " + result.line()
    assert "빈티지" in result.detail, result.detail
    subjects = {r["subject"] for r in conn.execute("SELECT DISTINCT subject FROM fact_revisions")}
    assert subjects == {"722Y001.0101000", "731Y001.0000001"}, subjects
    assert not any("901Y011" in u for u in urls), "수출 계열을 호출하면 안 된다"


def test_kr_status_is_partial_not_reconstructed(conn, settings):
    """`reconstructed`는 "정보 차단선을 지켜 재구성 완료"라는 뜻이다. ECOS는
    known_at을 API가 아니라 계열 성질로 정하므로 그렇게 말할 수 없다."""
    http = _ecos_ctx(settings)
    _run_kr(conn, settings, http)
    statuses = {r["data_status"] for r in conn.execute("SELECT DISTINCT data_status FROM fact_revisions")}
    assert statuses == {"partial"}, statuses
    reasons = {r["correction_reason"] for r in conn.execute(
        "SELECT DISTINCT correction_reason FROM fact_revisions")}
    assert reasons == {"backfill:macro_kr"}, reasons


def test_kr_export_series_are_registered_as_a_data_gap(conn, settings):
    """조용히 빼면 "한국 수출은 원래 안 본다"로 읽힌다. 갭으로 남긴다."""
    http = _ecos_ctx(settings)
    _run_kr(conn, settings, http)
    gaps = {r["gap_id"]: r["reason"] for r in conn.execute("SELECT gap_id, reason FROM data_gaps")}
    assert any("exports" in g for g in gaps), gaps
    assert any("빈티지" in r for r in gaps.values()), gaps


def test_kr_known_at_is_never_earlier_than_the_observation(conn, settings):
    """보수적 추정의 방향: 실제보다 **늦게** 알려진 것으로 잡는다. 그래야
    후견지명이 절대 생기지 않는다(S3)."""
    http = _ecos_ctx(settings)
    _run_kr(conn, settings, http)
    for row in conn.execute("SELECT event_at, known_at FROM fact_revisions"):
        assert row["known_at"] >= row["event_at"], dict(row)


def test_no_ecos_key_never_touches_the_network(conn, settings):
    settings.ecos_api_key = ""
    called = []
    result = macro_mod.run(conn, settings, "macro_kr", since=SINCE, until=UNTIL,
                           subjects=None, dry_run=False,
                           http=lambda name: called.append(name))
    assert result.status == "NO_DATA" and result.reason_code == "키없음", result.line()
    assert called == []
