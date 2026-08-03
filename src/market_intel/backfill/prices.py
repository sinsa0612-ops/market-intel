"""주가 백필 (spec 백필 §5 ST2 항목 1).

라이브 `providers/yfinance_prices.py`와 **같은 계보**를 만든다: 같은 provider
이름(`"yfinance"`), 같은 `event_at` 도출(`_local_close_utc`), 같은 fact_id 규약
(`engine._fact_id`). 그래서 백필한 2년치와 매일 쌓이는 오늘치가 하나의 원장으로
이어진다 — 백필 전용 접두사를 붙이는 순간 계보가 갈라진다(S2).

라이브와 다른 점은 셋뿐이다:
  1. `data_status='reconstructed'` + `correction_reason='backfill:prices'` (S4)
  2. `upsert_fact`가 아니라 `ledger.append_vintage` — "값이 같으면 생략" 규칙이
     백필에서는 정확히 틀리다(S5).
  3. `price_after_hours`를 만들지 않는다. 시간외가는 조회 시점에만 존재하는
     값이라 과거 시점으로 복원할 방법이 없다(spec ST2 항목 1).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from .. import db as db_mod
from ..engine import _fact_id
from ..models import FactCandidate, RawItem
from ..providers.yfinance_prices import _local_close_utc, _num, _valid_bars
from ..universe import UNIVERSE
from . import BackfillResult
from .ledger import append_vintage

CORRECTION_REASON = "backfill:prices"

# 정해진 장 마감이 없는 자산군. 선물·환율은 거의 24시간 거래되므로 "그날 종가"는
# 우리가 붙인 마감 시각(주식 장 마감)에 확정되지 않고 계속 움직인다. 그 상태로
# `known_at = event_at`(S3)을 적용하면 **같은 known_at에 값이 다른 판**이 생겨
# "그 시점에 알려져 있던 값"이 둘이 되고, 원장이 답을 못 한다.
# 실측 2026-08-03: 재실행 2회로 `CL=F`(유가) 거래량과 `DX-Y.NYB`(달러지수) 종가가
# 각각 두 판이 됐다. 다음 세션 봉이 나온 뒤에야 확정된 것으로 본다.
CONTINUOUS_ASSET_TYPES = ("commodity", "fx", "rate")
SETTLE_LAG = timedelta(days=1)


def run(conn, settings, source: str, *, since: date, until: date,
        subjects=None, dry_run: bool = False) -> BackfillResult:
    metas = [m for m in UNIVERSE if subjects is None or m["symbol"] in subjects]
    if not metas:
        return BackfillResult(source=source, status="NO_DATA", reason_code="no_subjects")
    symbols = [m["symbol"] for m in metas]

    try:
        # 한 번에 받는다. 심볼마다 부르면 44회 왕복이 된다(spec ST2 항목 1).
        # `end`는 **배타적**이라 하루를 더해야 `until` 당일 봉이 들어온다 —
        # 명세 본문은 `end=until`이라고 적었지만 그대로 두면 마지막 거래일이
        # 조용히 빠진다. [ASSUMPTION] 명세의 의도는 until 포함이라고 읽었다.
        data = yf.download(
            symbols, start=since, end=until + timedelta(days=1), interval="1d",
            group_by="ticker", progress=False, auto_adjust=False, threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        return BackfillResult(source=source, status="ERROR", reason_code="network_error",
                              detail=f"{exc.__class__.__name__}: {exc}"[:300])
    if data is None or data.empty:
        return BackfillResult(source=source, status="NO_DATA", reason_code="empty_response")

    fetched = appended = skipped = 0
    missing: list[str] = []
    now_iso = db_mod.iso_utc()
    settled_before = db_mod.iso_utc(datetime.now(timezone.utc) - SETTLE_LAG)

    # 라이브는 `data[symbol] if len(symbols) > 1 else data`로 가른다. 여기서는
    # 컬럼 모양을 직접 본다 — `--subjects MSFT`로 한 종목만 돌릴 때 yfinance가
    # 심볼 레벨을 붙여 주면 개수로 가른 쪽은 `Close`를 못 찾아 **조용히 0건**이
    # 된다. 백필은 한 번 돌리고 마는 명령이라 0건이 조용하면 알아채기 어렵다.
    grouped = isinstance(data.columns, pd.MultiIndex)

    for meta in metas:
        symbol = meta["symbol"]
        try:
            frame = data[symbol] if grouped else data
        except (KeyError, TypeError):
            missing.append(symbol)
            continue
        bars = _valid_bars(frame)
        if not bars:
            missing.append(symbol)
            continue
        currency = "KRW" if meta["market"] == "KR" else "USD"

        for trade_date, series in bars:
            event_at = _local_close_utc(meta, trade_date)
            # 아직 마감하지 않은 세션은 건너뛴다. known_at=event_at 규칙(S3)은
            # "장이 닫히는 순간 종가가 확정된다"에 기대는데, 장중에 받은 값은
            # 그 시각에 알려져 있지 않았다 — 마감 시각이 아직 미래다.
            # 실측 2026-08-03: 이걸 안 막으니 한국 장중에 두 번 돌린 재실행이
            # **같은 known_at에 다른 값** 25행을 새로 넣었다(멱등성 깨짐 +
            # 어느 값이 그 시점의 진실인지 원장이 답할 수 없는 상태).
            if event_at > (settled_before if meta["asset_type"] in CONTINUOUS_ASSET_TYPES
                           else now_iso):
                continue
            date_str = trade_date.strftime("%Y-%m-%d")
            external_id = f"{symbol}:{date_str}"
            close = _num(series.get("Close"))
            volume = _num(series.get("Volume"))

            candidates = [FactCandidate(
                raw_ref=external_id, subject=symbol, category="price", metric="price_close",
                event_at=event_at, market=meta["market"], country=meta["country"],
                value_num=close, unit=meta.get("unit") or currency,
                publisher="Yahoo Finance", data_status="reconstructed",
                extra={"asset_type": meta["asset_type"]},
            )]
            # 지수·환율은 거래량 0을 자리표시자로 보낸다. 관측으로 저장하면
            # "그날 한 주도 안 거래됐다"는 거짓말이 된다(라이브와 같은 규칙).
            if volume is not None and volume > 0:
                candidates.append(FactCandidate(
                    raw_ref=external_id, subject=symbol, category="price", metric="volume",
                    event_at=event_at, market=meta["market"], country=meta["country"],
                    value_num=volume, unit="shares",
                    publisher="Yahoo Finance", data_status="reconstructed",
                ))

            fetched += len(candidates)
            if dry_run:
                continue

            snapshot_id = db_mod.insert_raw_snapshot(
                conn, settings.raw_dir, "yfinance",
                RawItem(
                    external_id=external_id, source_published_at=event_at,
                    safe_source_url=f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                    payload=json.dumps({
                        "symbol": symbol, "date": date_str,
                        "open": _num(series.get("Open")), "high": _num(series.get("High")),
                        "low": _num(series.get("Low")), "close": close, "volume": volume,
                    }, ensure_ascii=False),
                    fetch_status="ok",
                ),
            )
            for fc in candidates:
                # S3: 주가는 사후 수정되지 않는다 — 장 마감 시각이 곧 알려진 시각.
                if append_vintage(conn, _fact_id("yfinance", fc), snapshot_id,
                                  fc.event_at, fc, correction_reason=CORRECTION_REASON):
                    appended += 1
                else:
                    skipped += 1

    if not dry_run:
        conn.commit()
    if fetched == 0:
        return BackfillResult(source=source, status="NO_DATA", reason_code="empty_response",
                              detail="; ".join(missing[:8])[:300])
    return BackfillResult(
        source=source, status="PARTIAL" if missing else "OK",
        fetched=fetched, appended=appended, skipped_existing=skipped,
        detail="; ".join(missing[:8])[:300],
    )
