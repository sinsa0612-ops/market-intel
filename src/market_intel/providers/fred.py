"""FRED (St. Louis Fed) — fixed macro series list (spec §6.3). Empty key
means zero HTTP calls: NO_DATA/키없음 is returned before ctx.http() is even
called. ISM PMI is explicitly out of scope: FRED's free API does not
publish it (see HANDOFF data-gap note)."""
from __future__ import annotations

import json

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

SERIES = [
    "CPIAUCSL", "PCEPI", "UNRATE", "PAYEMS", "FEDFUNDS",
    "DFEDTARU", "DGS10", "DGS2", "T10Y2Y", "RSAFS", "INDPRO",
]


class FredProvider:
    name = "fred"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        api_key = ctx.settings.fred_api_key
        if not api_key:
            return ProviderResult(status="NO_DATA", reason_code="키없음", raw_items=[], facts=[])

        client = ctx.http("fred")
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for series_id in SERIES:
            params = {
                "series_id": series_id, "api_key": api_key, "file_type": "json",
                "sort_order": "desc", "limit": 1,
            }
            try:
                resp = client.get(OBSERVATIONS_URL, params=params)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{series_id}:error:{exc.__class__.__name__}")
                continue
            if resp.status_code != 200:
                missing.append(f"{series_id}:http_{resp.status_code}")
                continue

            payload = resp.text
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                missing.append(f"{series_id}:bad_json")
                continue
            obs_list = data.get("observations", [])
            if not obs_list:
                missing.append(f"{series_id}:no_observations")
                continue
            obs = obs_list[0]
            date = obs.get("date", "")
            external_id = f"{series_id}:{date}"
            raw_items.append(
                RawItem(
                    external_id=external_id, source_published_at=date,
                    safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
                )
            )

            value_str = obs.get("value", ".")
            if value_str in (".", None, ""):
                missing.append(f"{series_id}:missing_value")
                continue
            try:
                value_num = float(value_str)
            except ValueError:
                missing.append(f"{series_id}:unparseable_value")
                continue

            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=series_id, category="macro", metric="value",
                    event_at=f"{date}T00:00:00+00:00", market="US", country="US",
                    value_num=value_num, unit=data.get("units", ""), publisher="FRED",
                    data_status="source_verified",
                    extra={
                        "realtime_start": obs.get("realtime_start"),
                        "realtime_end": obs.get("realtime_end"),
                        "output_type": data.get("output_type"),
                    },
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
