"""ECOS (Bank of Korea) — base rate, USD/KRW FX, exports, semiconductor
exports. ECOS's StatisticSearch API puts the API key in the URL *path*
(not a query param) — this is the provider SafeHttp's path-segment masking
(spec A6) exists for.

Statistic-code status, checked against the real ECOS API with a live key on
2026-08-01 (synthesis run):
  * base_rate (722Y001/0101000)   — CONFIRMED, returns 기준금리 (연%).
  * usd_krw_fx (731Y001/0000001)  — CONFIRMED, returns 원/달러 매매기준율.
  * exports_total (901Y011/*AA)          — WRONG, ECOS answers INFO-200 (no rows).
  * semiconductor_exports (901Y011/*AAA1) — WRONG, same INFO-200.
The two wrong codes are left in place *and reported as a miss* on every run
(`provider_runs.safe_detail`) rather than silently dropped or replaced with a
guess; re-derive them via ECOS's StatisticTableList/StatisticItemList API and
replace them (see HANDOFF.md).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
KR_TZ = ZoneInfo("Asia/Seoul")

# stat_code 검증 상태는 모듈 docstring 참조 (2026-08-01 실키 확인).
STAT_CODES: dict[str, dict] = {
    "base_rate": {
        "stat_code": "722Y001", "item_code1": "0101000", "freq": "M", "lookback_days": 200,
        "unit_hint": "%",
        "note": "실키 확인 완료(2026-08-01): 한국은행 기준금리",
    },
    "usd_krw_fx": {
        "stat_code": "731Y001", "item_code1": "0000001", "freq": "D", "lookback_days": 30,
        "unit_hint": "KRW",
        "note": "실키 확인 완료(2026-08-01): 원/달러 매매기준율",
    },
    "exports_total": {
        "stat_code": "901Y011", "item_code1": "*AA", "freq": "M", "lookback_days": 200,
        "unit_hint": "USD",
        "note": "코드 오답 확인(2026-08-01 실키): INFO-200(no rows). StatisticTableList로 재도출 필요",
    },
    "semiconductor_exports": {
        "stat_code": "901Y011", "item_code1": "*AAA1", "freq": "M", "lookback_days": 200,
        "unit_hint": "USD",
        "note": "코드 오답 확인(2026-08-01 실키): INFO-200(no rows). StatisticTableList로 재도출 필요",
    },
}


def _period_bounds(freq: str, now_kst: datetime, lookback_days: int) -> tuple[str, str]:
    start_dt = now_kst - timedelta(days=lookback_days)
    if freq == "M":
        return start_dt.strftime("%Y%m"), now_kst.strftime("%Y%m")
    return start_dt.strftime("%Y%m%d"), now_kst.strftime("%Y%m%d")


def _time_to_event_at(time_str: str, freq: str) -> str:
    if freq == "M":
        local = datetime.strptime(time_str, "%Y%m").replace(day=1, tzinfo=KR_TZ)
    else:
        local = datetime.strptime(time_str, "%Y%m%d").replace(tzinfo=KR_TZ)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


class EcosProvider:
    name = "ecos"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        api_key = ctx.settings.ecos_api_key
        if not api_key:
            return ProviderResult(status="NO_DATA", reason_code="키없음", raw_items=[], facts=[])

        client = ctx.http("ecos")
        now_kst = datetime.now(KR_TZ)
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for key, spec in STAT_CODES.items():
            start, end = _period_bounds(spec["freq"], now_kst, spec["lookback_days"])
            # Key rides in the URL path: .../StatisticSearch/{key}/json/kr/1/100/{stat}/{freq}/{start}/{end}/{item}
            url = (
                f"{BASE_URL}/{api_key}/json/kr/1/100/"
                f"{spec['stat_code']}/{spec['freq']}/{start}/{end}/{spec['item_code1']}"
            )
            try:
                resp = client.get(url)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{key}:error:{exc.__class__.__name__}")
                continue
            if resp.status_code != 200:
                missing.append(f"{key}:http_{resp.status_code}")
                continue

            payload = resp.text
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                missing.append(f"{key}:bad_json")
                continue

            rows = data.get("StatisticSearch", {}).get("row", [])
            if not rows:
                err = data.get("RESULT", {})
                missing.append(f"{key}:no_rows:{err.get('CODE', '')}")
                continue
            row = rows[-1]  # rows are chronological; last = most recent in range

            time_str = row.get("TIME", "")
            external_id = f"{key}:{time_str}"
            raw_items.append(
                RawItem(
                    external_id=external_id, source_published_at=time_str,
                    safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
                )
            )

            value_str = row.get("DATA_VALUE")
            try:
                value_num = float(value_str)
            except (TypeError, ValueError):
                missing.append(f"{key}:unparseable_value")
                continue

            subject = f"{spec['stat_code']}.{spec['item_code1']}"
            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=subject, category="macro", metric="value",
                    event_at=_time_to_event_at(time_str, spec["freq"]), market="KR", country="KR",
                    value_num=value_num, unit=row.get("UNIT_NAME") or spec["unit_hint"],
                    publisher="ECOS(BOK)", data_status="source_verified",
                    extra={"logical_key": key, "stat_name": row.get("STAT_NAME"), "assumption_note": spec["note"]},
                )
            )

        if not facts:
            return ProviderResult(
                status="NO_DATA", reason_code="empty_response", raw_items=raw_items,
                safe_detail="; ".join(missing[:8])[:400],
            )
        status = "PARTIAL" if missing else "OK"
        return ProviderResult(
            status=status, reason_code=None, raw_items=raw_items, facts=facts,
            safe_detail="; ".join(missing[:8])[:400],
        )
