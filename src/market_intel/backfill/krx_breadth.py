"""KRX 전종목 -> 시장 폭(breadth) 백필 (spec 백필 §5).

라이브 `providers/krx_breadth.py`와 **같은 계보**를 만든다: 같은 provider 이름
(`"krx"`), 같은 `event_at` 도출(`_event_at`), 같은 metric 7개(`_compute`).
라이브와 다른 점은 프라이스 백필(`backfill/prices.py`)과 같은 셋뿐이다:

  1. `data_status='reconstructed'` + `correction_reason='backfill:krx_breadth'`
  2. `upsert_fact`가 아니라 `ledger.append_vintage`
  3. `known_at = event_at` — 종가처럼 장 마감 시각에 확정되는 값이다(S3).

라이브와 다른 점이 하나 더 있다: **lookback을 하지 않는다.** since~until을
하루씩 그대로 순회하며 그 날짜를 요청한다. 빈 응답은 휴장일(오류 아님)로
건너뛴다 — `missing`에 쌓지 않는다(spec §5 마지막 줄).

호출량이 크다(490거래일 x 2시장 ~= 980회, spec §5). KRX 호출 제한은
미확인이므로 연속 실패를 감시해 **조용히 건너뛰지 않고 중단**한다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .. import db as db_mod
from ..engine import _fact_id
from ..http_client import SafeHttp
from ..models import FactCandidate, RawItem
from ..providers.krx_breadth import MARKETS, PUBLISHER, _compute, _event_at, _fetch
from . import BackfillResult
from .ledger import append_vintage

CORRECTION_REASON = "backfill:krx_breadth"

# spec §5: "연속 실패가 나면 중단하고 보고한다" — 정확한 임계값은 명세에 없다.
# [ASSUMPTION] 5회 연속 HTTP/네트워크 실패(휴장일의 빈 응답은 실패로 세지
# 않는다)를 KRX 쪽 이상 신호로 보고 중단한다. 재실행하면 append_vintage의
# 멱등성 키가 이미 들어간 날을 건너뛰므로 중단 지점부터 이어진다.
MAX_CONSECUTIVE_FAILURES = 5


def _default_http(settings):
    return lambda name: SafeHttp(name, settings)


def run(conn, settings, source: str, *, since: date, until: date,
        subjects=None, dry_run: bool = False, http=None) -> BackfillResult:
    if not settings.krx_api_key:
        # 키가 없으면 네트워크를 두드리지 않는다(라이브 provider와 같은 규율).
        return BackfillResult(source=source, status="NO_DATA", reason_code="키없음")

    markets = [(s, u) for s, u in MARKETS if subjects is None or s in subjects]
    if not markets:
        return BackfillResult(source=source, status="NO_DATA", reason_code="no_subjects")

    http = http or _default_http(settings)
    client = http("krx")

    fetched = appended = skipped = 0
    missing: list[str] = []
    consecutive_failures = 0
    now_iso = db_mod.iso_utc()
    aborted = False

    day = since
    while day <= until and not aborted:
        bas_dd = day.strftime("%Y%m%d")
        for subject, url in markets:
            try:
                rows, safe_url = _fetch(client, settings.krx_api_key, url, bas_dd)
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                missing.append(f"{subject}:{bas_dd}:{exc.__class__.__name__}:{exc}"[:160])
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    break
                continue
            consecutive_failures = 0
            if not rows:
                continue  # 휴장일 — 결측이 아니다(spec §5 마지막 줄), missing에 안 쌓는다

            # 날짜는 요청한 basDd가 아니라 응답의 BAS_DD를 쓴다 — 라이브와 같은 규칙(spec §3-4).
            bas_dds = {str(r.get("BAS_DD")) for r in rows}
            if len(bas_dds) != 1:
                missing.append(f"{subject}:{bas_dd}:mixed_BAS_DD:{sorted(bas_dds)}"[:160])
                continue
            date_str = bas_dds.pop()
            event_at = _event_at(date_str)

            # 미래·미마감 세션은 건너뛴다(`backfill/prices.py`의 `now_iso` 가드와 같은 규율).
            if event_at > now_iso:
                continue

            external_id = f"krx:{subject}:{date_str}"
            snapshot_id = None
            if not dry_run:
                snapshot_id = db_mod.insert_raw_snapshot(
                    conn, settings.raw_dir, "krx",
                    RawItem(external_id=external_id, source_published_at=event_at,
                            safe_source_url=safe_url,
                            payload=json.dumps(rows, ensure_ascii=False), fetch_status="ok"),
                )

            for metric, (value, unit, extra) in _compute(rows).items():
                if value is None:
                    continue
                fetched += 1
                if dry_run:
                    continue
                fc = FactCandidate(
                    raw_ref=external_id, subject=subject, category="breadth", metric=metric,
                    event_at=event_at, market="KR", country="KR",
                    value_num=value, unit=unit, publisher=PUBLISHER,
                    data_status="reconstructed", extra=extra,
                )
                fc.safe_source_url = safe_url
                if append_vintage(conn, _fact_id("krx", fc), snapshot_id, event_at, fc,
                                  correction_reason=CORRECTION_REASON):
                    appended += 1
                else:
                    skipped += 1
        day += timedelta(days=1)

    if not dry_run:
        conn.commit()

    if aborted:
        return BackfillResult(
            source=source, status="ERROR", reason_code="network_error",
            fetched=fetched, appended=appended, skipped_existing=skipped,
            detail=(f"{MAX_CONSECUTIVE_FAILURES}회 연속 실패로 중단({day.strftime('%Y%m%d')} 이전까지 처리). "
                    + "; ".join(missing[-8:]))[:400],
        )
    if fetched == 0:
        return BackfillResult(source=source, status="NO_DATA", reason_code="empty_response",
                              detail="; ".join(missing[:8])[:300])
    return BackfillResult(
        source=source, status="PARTIAL" if missing else "OK",
        fetched=fetched, appended=appended, skipped_existing=skipped,
        detail="; ".join(missing[:8])[:300],
    )
