"""Offline unit tests for the pykrx investor-flow provider (no network).

KRX currently gates the investor-flow endpoints behind a login (see HANDOFF),
so the live run reports NO_DATA. These tests pin both halves of that contract:
a blocked source must produce zero facts and an explicit reason — never a
placeholder value — and the mapping must be correct for the day the source
comes back.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pytest

from market_intel.models import CollectContext
from market_intel.providers import pykrx_flows as mod

TRADING_DAY = "20260731"


def _ctx(settings):
    now = datetime.now(timezone.utc)
    return CollectContext(
        cutoff=now, now=now, settings=settings, http=lambda name: None,
        universe=[], logger=logging.getLogger("test"),
    )


@pytest.fixture
def fixed_trading_day(monkeypatch):
    monkeypatch.setattr(mod, "_recent_trading_date", lambda: TRADING_DAY)


def test_krx_auth_wall_yields_no_data_and_no_invented_values(settings, fixed_trading_day, monkeypatch):
    def blocked(*args, **kwargs):
        raise KeyError("거래대금")  # what pykrx raises once KRX rejects the session

    monkeypatch.setattr(mod.stock, "get_market_net_purchases_of_equities", blocked)
    monkeypatch.setattr(mod.stock, "get_market_trading_value_by_investor", blocked)

    result = mod.PykrxProvider().collect(_ctx(settings))

    assert result.status == "NO_DATA"
    assert result.reason_code == "empty_response"
    assert result.facts == [], "a blocked source must never produce facts"
    assert "KRX" in result.safe_detail and "KeyError" in result.safe_detail


def test_empty_frame_is_reported_not_stored_as_zero(settings, fixed_trading_day, monkeypatch):
    monkeypatch.setattr(mod.stock, "get_market_net_purchases_of_equities", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(mod.stock, "get_market_trading_value_by_investor", lambda *a, **k: pd.DataFrame())

    result = mod.PykrxProvider().collect(_ctx(settings))

    assert result.status == "NO_DATA"
    assert result.facts == []
    assert "empty" in result.safe_detail


def test_investor_flows_map_to_facts_when_the_source_answers(settings, fixed_trading_day, monkeypatch):
    market_df = pd.DataFrame({"순매수거래대금": [100.0, -40.0]}, index=["005930", "000660"])
    ticker_df = pd.DataFrame(
        {"순매수": [11.0, 22.0, -33.0]}, index=["외국인", "기관합계", "개인"]
    )
    monkeypatch.setattr(mod.stock, "get_market_net_purchases_of_equities", lambda *a, **k: market_df)
    monkeypatch.setattr(mod.stock, "get_market_trading_value_by_investor", lambda *a, **k: ticker_df)

    result = mod.PykrxProvider().collect(_ctx(settings))

    assert result.status == "OK"
    kospi_foreign = [f for f in result.facts if f.subject == "KOSPI" and f.metric == "net_buy_foreign"]
    assert len(kospi_foreign) == 1
    assert kospi_foreign[0].value_num == 60.0  # summed across the market
    assert kospi_foreign[0].event_at == "2026-07-31T06:30:00+00:00"  # 15:30 KST -> UTC
    assert kospi_foreign[0].market == "KR" and kospi_foreign[0].unit == "KRW"

    samsung = [f for f in result.facts if f.subject == "005930.KS" and f.metric == "net_buy_institution"]
    assert len(samsung) == 1 and samsung[0].value_num == 22.0

    metrics = {f.metric for f in result.facts}
    assert metrics == {"net_buy_foreign", "net_buy_institution", "net_buy_individual"}


# --- 자격증명이 로그로 새지 않는가 (2026-08-03) -----------------------------
#
# KRX가 투자자별 수급 화면을 회원 전용으로 바꿔서(익명 요청에 `LOGOUT`), pykrx에
# `KRX_ID`/`KRX_PW`를 줘야 수급이 들어온다. 그런데 pykrx는 **import 시점에**
# 로그인하면서 `  로그인 ID: <아이디>`를 stdout에 찍는다 — 자격증명을 넣는 순간부터
# 매 수집마다 `var/logs/job-*.log`에 계정 아이디가 평문으로 쌓인다.
# 실측 2026-08-03: 자격증명이 없는 상태에서도 그 배너가 이미 로그 파일에 있었다.

def test_pykrx_banner_never_reaches_stdout():
    """`cli.py`가 이미 "pykrx 배너가 파싱 가능한 출력을 오염시킨다"고 적어 둔
    그 배너다. 이제는 오염 문제만이 아니라 자격증명 문제이기도 하다."""
    import contextlib
    import importlib
    import io

    from market_intel.providers import pykrx_flows

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        importlib.reload(pykrx_flows)
    assert buf.getvalue() == "", f"pykrx 배너가 stdout으로 샜다: {buf.getvalue()!r}"


def test_login_id_is_masked_in_whatever_we_log(monkeypatch):
    """배너를 버리지는 않는다 — 로그인 성공/실패는 운영자가 알아야 한다.
    다만 아이디·비밀번호는 가린다."""
    from market_intel.providers import pykrx_flows

    monkeypatch.setenv("KRX_ID", "myaccount")
    monkeypatch.setenv("KRX_PW", "s3cret!")
    masked = pykrx_flows._mask_credentials(
        "  로그인 ID: myaccount / pw=s3cret! / KRX 로그인 완료.")
    assert "myaccount" not in masked, masked
    assert "s3cret!" not in masked, masked
    assert masked.count("***") == 2, masked
    assert "KRX 로그인 완료" in masked, "가리느라 운영 정보까지 지우면 안 된다"


def test_credentials_are_in_the_secret_scan_list(monkeypatch):
    """`secret_leak_check.py`가 `.env`의 모든 값을 훑는 근거가 `secret_values()`다.
    여기 없으면 DB·로그·저장된 URL에 새어도 아무도 모른다."""
    from market_intel.config import Settings

    monkeypatch.setenv("KRX_ID", "myaccount")
    monkeypatch.setenv("KRX_PW", "s3cret!")
    s = Settings()
    assert "myaccount" in s.secret_values()
    assert "s3cret!" in s.secret_values()
    assert "myaccount" not in repr(s) and "s3cret!" not in repr(s), repr(s)
