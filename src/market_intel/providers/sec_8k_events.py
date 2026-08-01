"""SEC EDGAR — 8-K Item 2.02 (earnings-release) detection for the US Core
12 (spec ST1 What #4). sec_edgar.py's own submissions fetch only looks at
10-Q/10-K/20-F/6-K (`ANNUAL_QUARTERLY_FORMS`) — it never sees 8-Ks, so this
is new collection, not a duplicate of sec_edgar.py's periodic-filing fact.
Confirmed live 2026-08-01: `data.sec.gov/submissions/CIK*.json`'s `recent`
block carries an `items` array parallel to `form`, a comma-joined string
like `"2.02,9.01"` for 8-Ks — that is what is matched against here.

Reuses sec_edgar.py's CIK resolution (`company_tickers.json`) and Core-12
ticker list read-only (import, not modification — spec boundary forbids
editing sec_edgar.py)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from ..universe import UNIVERSE
from .sec_edgar import SUBMISSIONS_URL, TICKERS_URL, US_CORE_TICKERS

ITEM_EARNINGS_RELEASE = "2.02"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Sec8kEventsProvider:
    name = "sec_8k_events"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        # Same host whitelist / rate limit / User-Agent identity as
        # sec_edgar.py — same pattern sec_edgar_13f.py already uses.
        # SafeHttp only attaches the SEC-required User-Agent header when
        # provider=="sec_edgar" (http_client.py); using this provider's own
        # name here gets a live 403 from data.sec.gov (confirmed by running
        # `collect --workflow events` against the real network — see
        # result.md). The "sec_8k_events" HOST_WHITELIST entry (spec B12)
        # is kept for a future direct caller but isn't exercised by this path.
        client = ctx.http("sec_edgar")
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        try:
            resp = client.get(TICKERS_URL)
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(status="ERROR", reason_code="network_error", safe_detail=str(exc)[:300])
        if resp.status_code != 200:
            return ProviderResult(status="ERROR", reason_code="host_rejected", safe_detail=f"tickers http {resp.status_code}")

        tickers_payload = resp.text
        by_ticker = {v["ticker"]: v for v in json.loads(tickers_payload).values()}
        raw_items.append(
            RawItem(
                external_id="company_tickers", source_published_at=_now_iso(),
                safe_source_url=client.safe_url(str(resp.request.url)), payload=tickers_payload,
            )
        )

        for ticker in US_CORE_TICKERS:
            entry = by_ticker.get(ticker)
            if entry is None:
                missing.append(f"{ticker}:cik_not_found")
                continue
            cik = int(entry["cik_str"])
            meta = next(m for m in UNIVERSE if m["symbol"] == ticker)

            url = SUBMISSIONS_URL.format(cik=cik)
            try:
                sresp = client.get(url)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{ticker}:submissions_error:{exc.__class__.__name__}")
                continue
            if sresp.status_code != 200:
                missing.append(f"{ticker}:submissions_http_{sresp.status_code}")
                continue

            payload = sresp.text
            sub = json.loads(payload)
            recent = sub.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accs = recent.get("accessionNumber", [])
            items = recent.get("items", [])

            found = None
            for form, filing_date, acc, item_str in zip(forms, dates, accs, items):
                if form == "8-K" and ITEM_EARNINGS_RELEASE in (item_str or "").split(","):
                    found = (filing_date, acc)
                    break

            if found is None:
                missing.append(f"{ticker}:no_8k_2.02")
                continue

            filing_date, acc = found
            external_id = f"{ticker}:8k:{acc}"
            raw_items.append(
                RawItem(
                    external_id=external_id, source_published_at=filing_date,
                    safe_source_url=client.safe_url(str(sresp.request.url)), payload=payload,
                )
            )
            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=ticker, category="event", metric="earnings_release_8k",
                    event_at=f"{filing_date}T00:00:00+00:00", market=meta["market"], country=meta["country"],
                    value_text=acc, unit="", publisher="SEC EDGAR", data_status="source_verified",
                    extra={"item": ITEM_EARNINGS_RELEASE, "form": "8-K"},
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
