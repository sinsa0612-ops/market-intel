"""ST2 — `backfill.prices` (spec 백필 §5 ST2 항목 1).

네트워크를 타지 않는다: `yf.download`를 갈아끼워 고정 프레임을 준다.

고정하는 계약:
  - `known_at = event_at` (S3: 주가는 사후 수정되지 않는다)
  - `fact_id`가 라이브 provider와 **같은 계보**다 (S2)
  - `data_status='reconstructed'` + `correction_reason='backfill:prices'` (S4)
  - volume 0은 사실이 아니다 — 지수·환율의 자리표시자다 (라이브와 동일 규칙)
  - 두 번 돌리면 append 0 (멱등)
  - `--dry-run`은 DB에 한 줄도 쓰지 않는다

`from conftest import` 금지 — 헬퍼는 이 파일 안에 있다(검수서 F12).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from market_intel import db as db_mod
from market_intel.backfill import prices as prices_mod
from market_intel.config import Settings

SINCE, UNTIL = date(2026, 7, 29), date(2026, 7, 31)


def _frame(closes: dict[str, float], volumes: dict[str, float]) -> pd.DataFrame:
    idx = pd.to_datetime(sorted(closes))
    return pd.DataFrame(
        {"Close": [closes[d.strftime("%Y-%m-%d")] for d in idx],
         "Open": [closes[d.strftime("%Y-%m-%d")] for d in idx],
         "High": [closes[d.strftime("%Y-%m-%d")] for d in idx],
         "Low": [closes[d.strftime("%Y-%m-%d")] for d in idx],
         "Volume": [volumes[d.strftime("%Y-%m-%d")] for d in idx]},
        index=idx,
    )


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


@pytest.fixture
def fake_download(monkeypatch):
    """`yf.download(...)`의 다중심볼 반환 모양(컬럼 최상위가 심볼)을 흉내낸다."""
    calls = []

    def _install(frames: dict[str, pd.DataFrame]):
        def fake(symbols, **kwargs):
            calls.append({"symbols": list(symbols), **kwargs})
            return pd.concat(frames, axis=1)
        monkeypatch.setattr(prices_mod.yf, "download", fake)
        return calls

    return _install


def _run(conn, settings, **kw):
    return prices_mod.run(conn, settings, "prices", since=SINCE, until=UNTIL,
                          subjects=kw.pop("subjects", ["MSFT"]), dry_run=kw.pop("dry_run", False))


def test_known_at_equals_event_at_and_lineage_matches_live(conn, settings, fake_download):
    """S3: 주가의 known_at은 장 마감 시각 그대로. S2: fact_id는 라이브와 같은 계보."""
    fake_download({"MSFT": _frame({"2026-07-30": 451.10, "2026-07-31": 464.72},
                                  {"2026-07-30": 1000.0, "2026-07-31": 2000.0})})
    result = _run(conn, settings)
    assert result.status == "OK", result.line()
    assert result.appended == 4, result.line()  # 종가 2 + 거래량 2

    rows = list(conn.execute(
        "SELECT fact_id, event_at, known_at, data_status, correction_reason, value_num "
        "FROM fact_revisions WHERE metric='price_close' ORDER BY event_at"))
    assert [r["fact_id"] for r in rows] == [
        "yfinance:MSFT:price_close:20260730", "yfinance:MSFT:price_close:20260731",
    ], "라이브와 다른 fact_id를 쓰면 계보가 갈라진다"
    for r in rows:
        assert r["known_at"] == r["event_at"], "주가는 사후 수정되지 않는다 (S3)"
        assert r["data_status"] == "reconstructed"
        assert r["correction_reason"] == "backfill:prices"
    # 미국 장 마감 16:00 ET -> UTC 20:00
    assert rows[-1]["event_at"].endswith("T20:00:00+00:00"), rows[-1]["event_at"]
    assert rows[-1]["value_num"] == pytest.approx(464.72)


def test_zero_volume_is_not_a_fact(conn, settings, fake_download):
    """지수·환율은 거래량 0을 자리표시자로 보낸다 — 관측으로 저장하면 거짓말이다."""
    fake_download({"^KS11": _frame({"2026-07-30": 5593.56, "2026-07-31": 6595.45},
                                   {"2026-07-30": 0.0, "2026-07-31": 0.0})})
    result = prices_mod.run(conn, settings, "prices", since=SINCE, until=UNTIL,
                            subjects=["^KS11"], dry_run=False)
    assert result.appended == 2, result.line()
    assert conn.execute("SELECT COUNT(*) c FROM fact_revisions WHERE metric='volume'").fetchone()["c"] == 0


def test_korean_close_uses_the_seoul_session(conn, settings, fake_download):
    """한국 종목의 마감은 15:30 KST = 06:30 UTC (라이브 `_local_close_utc` 재사용)."""
    fake_download({"005930.KS": _frame({"2026-07-31": 262500.0}, {"2026-07-31": 100.0})})
    prices_mod.run(conn, settings, "prices", since=SINCE, until=UNTIL,
                   subjects=["005930.KS"], dry_run=False)
    row = conn.execute("SELECT event_at, unit FROM fact_revisions WHERE metric='price_close'").fetchone()
    assert row["event_at"] == "2026-07-31T06:30:00+00:00", row["event_at"]
    assert row["unit"] == "KRW"


def test_rerun_is_idempotent(conn, settings, fake_download):
    frames = {"MSFT": _frame({"2026-07-31": 464.72}, {"2026-07-31": 2000.0})}
    fake_download(frames)
    first = _run(conn, settings)
    second = _run(conn, settings)
    assert first.appended == 2
    assert second.appended == 0, second.line()
    assert second.skipped_existing == 2, second.line()


def test_dry_run_writes_nothing(conn, settings, fake_download):
    fake_download({"MSFT": _frame({"2026-07-31": 464.72}, {"2026-07-31": 2000.0})})
    result = _run(conn, settings, dry_run=True)
    assert result.fetched == 2, result.line()
    assert result.appended == 0
    assert conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM raw_snapshots").fetchone()["c"] == 0


def test_download_is_called_once_for_all_symbols(conn, settings, fake_download):
    """심볼마다 부르면 44회 왕복이 된다 — 한 번에 받는다(spec ST2 항목 1)."""
    calls = fake_download({
        "MSFT": _frame({"2026-07-31": 464.72}, {"2026-07-31": 2000.0}),
        "NVDA": _frame({"2026-07-31": 180.0}, {"2026-07-31": 3000.0}),
    })
    prices_mod.run(conn, settings, "prices", since=SINCE, until=UNTIL,
                   subjects=["MSFT", "NVDA"], dry_run=False)
    assert len(calls) == 1, f"yf.download가 {len(calls)}번 불렸다"
    assert calls[0]["interval"] == "1d"
    assert calls[0]["auto_adjust"] is False
    # `end`는 배타적이다 — until 당일 봉을 받으려면 하루를 더해야 한다.
    assert calls[0]["end"] > UNTIL, calls[0]["end"]


def test_empty_download_is_no_data_not_a_crash(conn, settings, monkeypatch):
    monkeypatch.setattr(prices_mod.yf, "download", lambda symbols, **kw: pd.DataFrame())
    result = _run(conn, settings)
    assert result.status == "NO_DATA", result.line()
    assert result.appended == 0
