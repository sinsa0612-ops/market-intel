"""FRED calendar — forward release-date schedule for a fixed allowlist of
macro releases (spec B3). Three traps this file exists to dodge:
  * `/fred/release/dates` without an explicit realtime window defaults to
    `1776-07-04..9999-12-31` and returns only past dates,
  * `include_release_dates_with_no_data=true` makes releases *without* a
    real publication calendar (FOMC, Coinbase Cryptocurrencies) return every
    calendar day in the window — a density guard discards those,
  * ...but a genuine business-day series (H.15, 250 dates a year) trips the
    same density threshold. The weekend count separates them: the
    daily-fill artefact fills weekends too (FOMC 2026: 365 dates, 104 of
    them weekends), H.15 never does (250 dates, 0 weekends — both measured
    live 2026-08-01). A real daily series is kept as one `release_cadence`
    fact instead of being thrown away with the garbage (spec B3.3 rev2).
FOMC itself is never sourced here — see policy_calendar.py.

Storage shape (spec B2 rev2 — the part that must never regress): one fact
per (release, year), `subject=fredrel:{id}` with no date in it, `event_at`
= 1 Jan of the year the timetable covers, and every date of that year in
`value_text` as a normalised CSV. Anchoring on the month instead (rev1)
collapses same-month double releases — JOLTS publishes twice a month in 9
of 26 months — into one oscillating fact.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from ..schedule import (
    CADENCE_BASIS,
    DENSITY_GUARD_RATIO,
    YEAR_TIMETABLE_BASIS,
    serialize_dates,
)

RELEASES_URL = "https://api.stlouisfed.org/fred/releases"
RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"

# spec B3 — fixed allowlist, resolved by NAME every run (never hardcode the
# id; ids are only used inside tests as a regression fixture).
ALLOWED_RELEASE_NAMES = [
    "Consumer Price Index",
    "Employment Situation",
    "Personal Income and Outlays",
    "Gross Domestic Product",
    "Advance Monthly Sales for Retail and Food Services",
    "G.17 Industrial Production and Capacity Utilization",
    "Job Openings and Labor Turnover Survey",
    "H.15 Selected Interest Rates",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _year_anchor(year: int) -> str:
    return datetime(year, 1, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")


def _days_in_year(year: int) -> int:
    return (date(year, 12, 31) - date(year, 1, 1)).days + 1


def _weekend_count(dates: list[str]) -> int:
    return sum(1 for d in dates if date.fromisoformat(d).weekday() >= 5)


class FredCalendarProvider:
    name = "fred_calendar"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        api_key = ctx.settings.fred_api_key
        if not api_key:
            return ProviderResult(status="NO_DATA", reason_code="키없음", raw_items=[], facts=[])

        client = ctx.http("fred_calendar")
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        notes: list[str] = []    # density decisions — always kept in safe_detail
        missing: list[str] = []  # actual gaps — these make the run PARTIAL

        try:
            resp = client.get(RELEASES_URL, params={"api_key": api_key, "file_type": "json"})
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(status="ERROR", reason_code="network_error", safe_detail=str(exc)[:300])
        if resp.status_code != 200:
            return ProviderResult(status="ERROR", reason_code="host_rejected", safe_detail=f"releases http {resp.status_code}")

        releases_payload = resp.text
        raw_items.append(
            RawItem(
                external_id="releases", source_published_at=_now_iso(),
                safe_source_url=client.safe_url(str(resp.request.url)), payload=releases_payload,
            )
        )
        by_name = {r["name"]: r["id"] for r in json.loads(releases_payload).get("releases", [])}

        # spec B2 rev2: absolute calendar years, never a sliding "today+N"
        # window — a window that slides drops dates out the far end and mints
        # a ghost revision every single day.
        years = [ctx.now.year, ctx.now.year + 1]

        for name in ALLOWED_RELEASE_NAMES:
            release_id = by_name.get(name)
            if release_id is None:
                missing.append(f"{name}:release_id_not_found")
                continue

            for year in years:
                params = {
                    "release_id": release_id, "api_key": api_key, "file_type": "json",
                    "realtime_start": f"{year}-01-01", "realtime_end": f"{year}-12-31",
                    "include_release_dates_with_no_data": "true", "sort_order": "asc",
                }
                try:
                    dresp = client.get(RELEASE_DATES_URL, params=params)
                except Exception as exc:  # noqa: BLE001
                    missing.append(f"{name}:{year}:error:{exc.__class__.__name__}")
                    continue
                if dresp.status_code != 200:
                    missing.append(f"{name}:{year}:http_{dresp.status_code}")
                    continue

                payload = dresp.text
                external_id = f"release_dates:{release_id}:{year}"
                raw_items.append(
                    RawItem(
                        external_id=external_id, source_published_at=_now_iso(),
                        safe_source_url=client.safe_url(str(dresp.request.url)), payload=payload,
                    )
                )

                dates = sorted({
                    d["date"] for d in json.loads(payload).get("release_dates", [])
                    if d.get("date", "").startswith(f"{year}-")
                })
                if not dates:
                    # An empty year is honest ("not published yet"), not an
                    # error — but never a fact with an empty timetable.
                    missing.append(f"{name}:{year}:no_dates")
                    continue

                n = len(dates)
                if (n / _days_in_year(year)) >= DENSITY_GUARD_RATIO:
                    weekend = _weekend_count(dates)
                    if weekend > 0:
                        # No real publication calendar: `include_release_dates_with_no_data`
                        # filled every day of the year (spec B3.3 — FOMC 365/104).
                        missing.append(f"{name}:{year}:daily_fill_discarded:{n}")
                        continue
                    # Weekday-only at this density = a genuine business-day
                    # series. Keep the fact of its cadence, but never as
                    # calendar rows (250 rows would bury everything else).
                    notes.append(f"{name}:{year}:daily_series_kept:{n}")
                    facts.append(
                        FactCandidate(
                            raw_ref=external_id, subject=f"fredrel:{release_id}", category="calendar",
                            metric="release_cadence", event_at=_year_anchor(year), market="US", country="US",
                            value_num=float(n), value_text="daily_business_day",
                            comparison_basis=CADENCE_BASIS, publisher="FRED", data_status="source_verified",
                            extra={
                                "release_id": release_id, "release_name": name,
                                "weekend_count": weekend, "first": dates[0], "last": dates[-1],
                            },
                        )
                    )
                    continue

                facts.append(
                    FactCandidate(
                        raw_ref=external_id, subject=f"fredrel:{release_id}", category="calendar",
                        metric="scheduled_date", event_at=_year_anchor(year), market="US", country="US",
                        value_text=serialize_dates(dates), comparison_basis=YEAR_TIMETABLE_BASIS,
                        publisher="FRED", data_status="source_verified",
                        extra={"release_id": release_id, "release_name": name, "dates": dates},
                    )
                )

        safe_detail = ("; ".join(notes[:8] + missing[:8]))[:400]
        if not facts:
            return ProviderResult(
                status="NO_DATA", reason_code="empty_response", raw_items=raw_items, safe_detail=safe_detail,
            )
        return ProviderResult(
            status="PARTIAL" if missing else "OK", reason_code=None, raw_items=raw_items,
            facts=facts, safe_detail=safe_detail,
        )
