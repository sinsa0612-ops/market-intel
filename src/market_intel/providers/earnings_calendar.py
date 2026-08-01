"""Earnings calendar — Core 16 scheduled earnings dates + consensus, via
`yfinance.Ticker(sym).calendar` (NOT `.get_earnings_dates()`, which needs
`lxml` — adding a dependency is forbidden, spec B0). `.calendar` was
smoke-tested 16/16 on Core 16 including the `.KS` tickers (spec 사전 확인
사실). Announcement time (BMO/AMC) is not in this payload, so every fact
here is honestly `partial` with `extra.time_unknown=true` instead of
implying a time we don't have.

Whether a date shift is a same-quarter delay or the *next* quarter's cycle
(a >45-day jump — real behaviour once the previous date has passed, e.g.
XOM's `.calendar` rolling straight to the next quarter) is not decided
here: this provider just reports whatever `.calendar` says, filed under the
year that date falls in (spec B2 rev2 — the anchor is the year, so a
same-year roll is a revision of one fact and `schedule.changes()` reads the
Δ63일 jump as next_cycle rather than a delay)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import yfinance as yf

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from ..schedule import YEAR_TIMETABLE_BASIS, serialize_dates
from ..universe import CORE16

PUBLISHER = "Yahoo Finance (yfinance)"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _year_anchor(d: date) -> str:
    return datetime(d.year, 1, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")


def _currency(meta: dict) -> str:
    """`.calendar` reports consensus EPS/revenue as bare numbers with no
    currency, so it comes from where the symbol is listed — the same
    convention universe.py's `unit` field encodes (None = the exchange's
    currency). 삼성전자's 14,322 EPS is won; labelling it USD would publish
    "$14,322 예상 EPS"."""
    return "KRW" if meta["market"] == "KR" else "USD"


def _first_earnings_date(cal) -> date | None:
    val = cal.get("Earnings Date") if cal else None
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class EarningsCalendarProvider:
    name = "earnings_calendar"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for meta in CORE16:
            symbol = meta["symbol"]
            try:
                cal = yf.Ticker(symbol).calendar
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{symbol}:error:{exc.__class__.__name__}")
                continue
            earnings_date = _first_earnings_date(cal)
            if earnings_date is None:
                missing.append(f"{symbol}:no_earnings_date")
                continue

            date_str = earnings_date.isoformat()
            event_at = _year_anchor(earnings_date)
            currency = _currency(meta)
            external_id = f"{symbol}:calendar:{date_str}"
            payload = json.dumps({k: str(v) for k, v in (cal or {}).items()}, ensure_ascii=False)
            raw_items.append(
                RawItem(external_id=external_id, source_published_at=_now_iso(), safe_source_url="", payload=payload)
            )

            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=symbol, category="calendar", metric="scheduled_date",
                    event_at=event_at, market=meta["market"], country=meta["country"],
                    value_text=serialize_dates([date_str]), comparison_basis=YEAR_TIMETABLE_BASIS,
                    publisher=PUBLISHER, data_status="partial",
                    extra={"time_unknown": True, "dates": [date_str]},
                )
            )

            # The consensus facts deliberately carry no date in `extra`:
            # `upsert_fact` only compares value/unit/status, so a date parked
            # there would go stale the moment the earnings date moved without
            # the estimate moving (spec B2 rev2).
            eps = _num(cal.get("Earnings Average")) if cal else None
            if eps is not None:
                facts.append(
                    FactCandidate(
                        raw_ref=external_id, subject=symbol, category="calendar", metric="consensus_eps",
                        event_at=event_at, market=meta["market"], country=meta["country"],
                        value_num=eps, unit=f"{currency}/share", comparison_basis=YEAR_TIMETABLE_BASIS,
                        publisher=PUBLISHER, data_status="partial", extra={"time_unknown": True},
                    )
                )

            revenue = _num(cal.get("Revenue Average")) if cal else None
            if revenue is not None:
                facts.append(
                    FactCandidate(
                        raw_ref=external_id, subject=symbol, category="calendar", metric="consensus_revenue",
                        event_at=event_at, market=meta["market"], country=meta["country"],
                        value_num=revenue, unit=currency, comparison_basis=YEAR_TIMETABLE_BASIS,
                        publisher=PUBLISHER, data_status="partial", extra={"time_unknown": True},
                    )
                )

        if not facts:
            return ProviderResult(
                status="NO_DATA", reason_code="empty_response", raw_items=raw_items,
                safe_detail=("; ".join(missing[:8]))[:400],
            )
        status = "PARTIAL" if missing else "OK"
        return ProviderResult(
            status=status, reason_code=None, raw_items=raw_items, facts=facts,
            safe_detail=("; ".join(missing[:8]))[:400],
        )
