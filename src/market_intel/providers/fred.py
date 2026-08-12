"""FRED (St. Louis Fed) — fixed macro series list (spec §6.3). Empty key
means zero HTTP calls: NO_DATA/키없음 is returned before ctx.http() is even
called. ISM PMI is explicitly out of scope: FRED's free API does not
publish it (see HANDOFF data-gap note)."""
from __future__ import annotations

import json

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES_URL = "https://api.stlouisfed.org/fred/series"

SERIES = [
    "CPIAUCSL", "PCEPI", "UNRATE", "PAYEMS", "FEDFUNDS",
    "DFEDTARU", "DGS10", "DGS2", "T10Y2Y", "RSAFS", "INDPRO",
    # --- 2026-08-12 추가 ------------------------------------------------
    #
    # **계산하지 않고 받아온다.** 원래는 실질금리를 `DGS10 - CPI 전년비`로 파생해
    # 만들려 했는데, 그 근사가 **부호까지 틀렸다**: 4.72 - 3.73 = 0.99%로 "완화
    # 쪽"이 나왔지만 시장이 매기는 실질금리(DFII10)는 같은 날 2.43%로 **2년 중
    # 최고(백분위 100%)**였다. 원인은 CPI 전년비가 지나간 12개월을 보는 값이고
    # 시장은 앞으로의 기대인플레(T10YIE 2.27%)로 실질금리를 매기기 때문이다.
    # 4.72 - 2.27 ~= 2.45%로 DFII10과 맞는다.
    #
    # 받아오는 쪽이 나은 이유가 셋이다: (1) 계산이 아니라 관측이라 CPI냐 PCE냐
    # 같은 **자유 파라미터가 0**이다(다중검정 위험이 늘지 않는다), (2) 월간 34
    # 관측 제약을 우회한다 — 전부 일간이고 2003년까지 있다, (3) 발표 지연이 없다.
    #
    # 사외 고문 2인 독립 자문(2026-08-12)에서 나온 제안이고, 5개 ID 전부 FRED에
    # 실재를 직접 확인하고 넣었다.
    "DFII10",         # 10년 TIPS 실질금리 — "물가를 감안해도 긴축인가"
    "T10YIE",         # 10년 기대인플레 — "시장은 물가 재가속을 믿는가"(후행 CPI로는 불가)
    # `금융·신용` 테마에 **신용 데이터가 하나도 없었다** — 국채금리는 신용이 아니다.
    # 이 스프레드가 벌어지는 것이 "고금리가 실물로 번진다"의 첫 신호다.
    "BAMLH0A0HYM2",   # ICE BofA 미국 하이일드 OAS
    # 원/달러만으로는 "원화가 약한 것"과 "달러가 강한 것"을 분해할 수 없다.
    # 실측 2026-08: 달러지수는 2년 중 하위 11%(약세)인데 원/달러는 1,415다.
    "DTWEXBGS",       # 광의 달러지수
]


class FredProvider:
    name = "fred"

    def __init__(self) -> None:
        self._unit_cache: dict[str, str] = {}

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

            unit, unit_note = self._unit_of(client, series_id, api_key)
            if unit_note:
                missing.append(unit_note)

            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=series_id, category="macro", metric="value",
                    event_at=f"{date}T00:00:00+00:00", market="US", country="US",
                    value_num=value_num, unit=unit, publisher="FRED",
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

    def _unit_of(self, client, series_id: str, api_key: str) -> tuple[str, str]:
        """The measurement unit, from FRED's own series metadata.

        The observations response carries `units`, but that is the requested
        *transformation* ("lin" = no transformation), not what the number is
        measured in — publishing it made reports read "미국 CPI 332.57 lin".
        `/fred/series` returns `units_short` ("%", "Index 1982-1984=100",
        "Thous. of Persons"), which is what the value actually is. Unknown
        stays empty rather than guessed: a wrong unit is worse than none.

        Returns (unit, missing_note); the note is empty on success."""
        cached = self._unit_cache.get(series_id)
        if cached is not None:
            return cached, ""
        params = {"series_id": series_id, "api_key": api_key, "file_type": "json"}
        try:
            resp = client.get(SERIES_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            return "", f"{series_id}:unit_lookup_error:{exc.__class__.__name__}"
        if resp.status_code != 200:
            return "", f"{series_id}:unit_lookup_http_{resp.status_code}"
        try:
            entries = json.loads(resp.text).get("seriess", [])
        except json.JSONDecodeError:
            return "", f"{series_id}:unit_lookup_bad_json"
        if not entries:
            return "", f"{series_id}:unit_lookup_empty"
        unit = (entries[0].get("units_short") or "").strip()
        self._unit_cache[series_id] = unit
        return unit, "" if unit else f"{series_id}:unit_unknown"
