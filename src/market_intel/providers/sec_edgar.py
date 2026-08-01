"""SEC EDGAR — CIK resolution (never hardcoded; resolved live from
company_tickers.json), recent 10-Q/10-K/20-F filing detection, and a
best-effort XBRL financial snapshot (revenue, operating income, operating
cash flow, capex -> derived FCF) for the US Core equities (TSM uses 20-F/
6-K and IFRS tags; best-effort, honestly marked partial if concepts are
missing). 13F fund-filing detection is out of scope for this run (not
required by this task's success criteria) — see HANDOFF."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from ..universe import UNIVERSE

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

US_CORE_TICKERS = [m["symbol"] for m in UNIVERSE if m["core16"] and m["market"] == "US" and m["asset_type"] == "equity"]

ANNUAL_QUARTERLY_FORMS = {"10-Q", "10-K", "20-F", "6-K"}

# Concept candidates tried in order; first with recent 10-Q/10-K(/20-F/6-K)
# data wins. us-gaap first, ifrs-full as a TSM fallback.
# Successor registrants that carry the ticker but not the financial history.
# XOM: ExxonMobil reorganized under a holding company around 2026-07-01 —
# SEC's own company_tickers.json maps XOM to the new holdco (CIK 2115436),
# which files 8-Ks but has no periodic report yet, while the operating
# company (CIK 34088, 25-NSE filed 2026-07-02) holds every historical
# figure. Filing detection stays on the ticker's current CIK; only the
# financial-statement lookup falls back, and every fact it produces records
# the entity it came from.
PREDECESSOR_CIK: dict[str, int] = {"XOM": 34088}

CONCEPT_CANDIDATES: dict[str, list[tuple[str, str]]] = {
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("ifrs-full", "Revenue"),
    ],
    "operating_income": [
        ("us-gaap", "OperatingIncomeLoss"),
        ("ifrs-full", "ProfitLossFromOperatingActivities"),
    ],
    "operating_cash_flow": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ],
    # `PaymentsToAcquireProductiveAssets` is the tag AMZN, NVDA and AEP
    # actually report cash capex under today; the PP&E tag is the one they
    # abandoned (AMZN's stops at 2017-03-31). Deliberately NOT a candidate:
    # `CapitalExpendituresIncurredButNotYetPaid` is accrued-but-unpaid capex,
    # not cash out the door — feeding it into FCF = operating cash flow -
    # capex would silently corrupt the cash-flow axis.
    "capex": [
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipment"),
    ],
}


# A company that stopped using an XBRL tag years ago still has data under it.
# Picking that up and publishing it as "the latest quarter" is how a 2014
# number ends up labelled 원자료확인. Anything whose period ends more than two
# quarters before the company's own most recent periodic filing is kept (the
# event date and the value are both real) but downgraded to 부분확인/partial and
# reported in safe_detail.
STALE_AFTER_DAYS = 200


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _days_behind(period_end: str, reference_date: str | None) -> int | None:
    if not reference_date or not period_end:
        return None
    try:
        return (date.fromisoformat(reference_date[:10]) - date.fromisoformat(period_end[:10])).days
    except ValueError:
        return None


def _is_stale(period_end: str, reference_date: str | None) -> bool:
    behind = _days_behind(period_end, reference_date)
    return behind is not None and behind > STALE_AFTER_DAYS


def _duration_days(u: dict) -> int | None:
    start, end = u.get("start"), u.get("end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _comparison_basis(duration_days: int | None) -> str:
    """Label the reporting period length so a metric is never silently
    mixed across companies with different bases (a quarter vs a full
    fiscal year) with no way to tell which is which."""
    if duration_days is None:
        return "unknown"
    if 80 <= duration_days <= 100:
        return "quarterly"
    if 350 <= duration_days <= 380:
        return "annual"
    return f"{duration_days}d"


class SecEdgarProvider:
    name = "sec_edgar"

    def collect(self, ctx: CollectContext) -> ProviderResult:
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

            filing_facts, filing_raw, filing_missing, filing_date = self._filing(client, cik, ticker, meta)
            facts.extend(filing_facts)
            raw_items.extend(filing_raw)
            missing.extend(filing_missing)

            fin_facts, fin_raw, fin_missing = self._financials(client, cik, ticker, meta, filing_date)
            if not fin_facts and ticker in PREDECESSOR_CIK:
                # The ticker now points at a successor registrant that has not
                # filed a periodic report yet, so it has no XBRL history at
                # all. Fall back to the predecessor that actually reported the
                # numbers, and stamp which entity they came from — silently
                # dropping a Core 16 company's financials is worse, and
                # silently blending two entities without saying so is worse still.
                prev_cik = PREDECESSOR_CIK[ticker]
                fin_facts, fin_raw, prev_missing = self._financials(
                    client, prev_cik, ticker, meta, filing_date
                )
                for f in fin_facts:
                    f.extra = {**(f.extra or {}), "reporting_entity_cik": prev_cik,
                               "reporting_entity_note": "predecessor registrant"}
                missing.extend(fin_missing if not fin_facts else prev_missing)
            else:
                missing.extend(fin_missing)
            facts.extend(fin_facts)
            raw_items.extend(fin_raw)

        if not facts:
            return ProviderResult(status="NO_DATA", reason_code="empty_response", raw_items=raw_items)

        status = "PARTIAL" if missing else "OK"
        return ProviderResult(
            status=status, reason_code=None, raw_items=raw_items, facts=facts,
            safe_detail=("; ".join(missing[:8]))[:400],
        )

    def _filing(self, client, cik: int, ticker: str, meta: dict):
        """Returns (facts, raw_items, missing, latest_filing_date | None).

        The filing date is the reference point the financial snapshot uses to
        judge whether an XBRL concept's data is current or long abandoned."""
        url = SUBMISSIONS_URL.format(cik=cik)
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            return [], [], [f"{ticker}:submissions_error:{exc.__class__.__name__}"], None
        if resp.status_code != 200:
            return [], [], [f"{ticker}:submissions_http_{resp.status_code}"], None

        payload = resp.text
        sub = json.loads(payload)
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])

        target_forms = {"20-F", "6-K"} if ticker == "TSM" else {"10-Q", "10-K"}
        for form, date, acc in zip(forms, dates, accs):
            if form in target_forms:
                external_id = f"{ticker}:filing:{acc}"
                raw = RawItem(
                    external_id=external_id, source_published_at=date,
                    safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
                )
                fact = FactCandidate(
                    raw_ref=external_id, subject=ticker, category="filing", metric="filing_event",
                    event_at=f"{date}T00:00:00+00:00", market=meta["market"], country=meta["country"],
                    value_text=acc, unit="", publisher="SEC EDGAR", data_status="source_verified",
                    extra={"form": form},
                )
                return [fact], [raw], [], date
        return [], [], [f"{ticker}:no_recent_filing"], None

    def _financials(self, client, cik: int, ticker: str, meta: dict, reference_date: str | None = None):
        url = COMPANYFACTS_URL.format(cik=cik)
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            return [], [], [f"{ticker}:companyfacts_error:{exc.__class__.__name__}"]
        if resp.status_code != 200:
            return [], [], [f"{ticker}:companyfacts_http_{resp.status_code}"]

        payload = resp.text
        data = json.loads(payload)
        external_id = f"{ticker}:companyfacts"
        raw = RawItem(
            external_id=external_id, source_published_at=_now_iso(),
            safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
        )

        facts_by_metric: dict[str, dict] = {}
        missing: list[str] = []
        for metric, candidates in CONCEPT_CANDIDATES.items():
            # Pool every candidate concept before choosing, rather than
            # stopping at the first one that holds any data at all. Companies
            # migrate between XBRL concepts, and the abandoned concept keeps
            # its last-reported values forever: NVDA's older `Revenues` still
            # carries FY2022 while current filings report under
            # `RevenueFromContractWithCustomerExcludingAssessedTax`. First-hit
            # selection locked onto the dead concept and produced a 4-year-old
            # revenue that sat next to other companies' current quarter.
            pool: list[tuple[int, str, dict]] = []
            for rank, (taxonomy, concept) in enumerate(candidates):
                node = data.get("facts", {}).get(taxonomy, {}).get(concept)
                if not node:
                    continue
                pool.extend(
                    (rank, f"{taxonomy}:{concept}", u)
                    for u in node.get("units", {}).get("USD", [])
                    if u.get("form") in ANNUAL_QUARTERLY_FORMS and u.get("val") is not None and u.get("end")
                )
            if not pool:
                missing.append(f"{ticker}:{metric}:concept_not_found")
                continue

            # Deterministic, period-aware pick: the most recent period end
            # wins; when several entries share that end (SEC posts a
            # discrete-quarter figure and a YTD/cumulative duplicate at the
            # same end date), the shortest duration wins so the result
            # reflects the single reported period instead of whichever order
            # the source array happened to list them in (previously
            # order-dependent -- repair.md finding #1). Concept preference
            # order only breaks a remaining tie, and `filed` breaks the last.
            def _period_key(item: tuple[int, str, dict]) -> tuple:
                rank, _concept, u = item
                d = _duration_days(u)
                return (u["end"], -(d if d is not None else 10**6), -rank, u.get("filed", ""))

            _rank, used_concept, found = max(pool, key=_period_key)
            facts_by_metric[metric] = {"entry": found, "concept": used_concept}

        out_facts: list[FactCandidate] = []
        for metric, info in facts_by_metric.items():
            entry = info["entry"]
            event_at = f"{entry['end']}T00:00:00+00:00"
            duration_days = _duration_days(entry)
            stale = _is_stale(entry["end"], reference_date)
            if stale:
                missing.append(f"{ticker}:{metric}:stale_period:{entry['end']}")
            extra = {
                "xbrl_concept": info["concept"], "form": entry.get("form"),
                "fy": entry.get("fy"), "fp": entry.get("fp"),
                "period_start": entry.get("start"), "period_end": entry["end"],
            }
            if stale:
                extra["stale_vs_filing_date"] = reference_date
                extra["days_behind_latest_filing"] = _days_behind(entry["end"], reference_date)
            out_facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=ticker, category="financials", metric=metric,
                    event_at=event_at, market=meta["market"], country=meta["country"],
                    value_num=_num(entry["val"]), unit="USD",
                    comparison_basis=_comparison_basis(duration_days),
                    publisher="SEC EDGAR",
                    data_status="partial" if stale else "source_verified",
                    extra=extra,
                )
            )

        if "operating_cash_flow" in facts_by_metric and "capex" in facts_by_metric:
            ocf = facts_by_metric["operating_cash_flow"]["entry"]
            capex = facts_by_metric["capex"]["entry"]
            if ocf["end"] != capex["end"]:
                # Subtracting figures from different periods produces a number
                # that means nothing. Skip it and say so.
                missing.append(f"{ticker}:free_cash_flow:period_mismatch_skipped")
            elif _is_stale(ocf["end"], reference_date):
                missing.append(f"{ticker}:free_cash_flow:stale_period_skipped:{ocf['end']}")
            else:
                fcf = _num(ocf["val"]) - _num(capex["val"])
                out_facts.append(
                    FactCandidate(
                        raw_ref=external_id, subject=ticker, category="financials", metric="free_cash_flow",
                        event_at=f"{ocf['end']}T00:00:00+00:00", market=meta["market"], country=meta["country"],
                        value_num=fcf, unit="USD",
                        comparison_basis=_comparison_basis(_duration_days(ocf)),
                        publisher="SEC EDGAR (derived)",
                        # derived, not observed -> 복원완료 (spec §3.3)
                        data_status="reconstructed",
                        extra={
                            "formula": "operating_cash_flow - capex", "ocf": ocf["val"], "capex": capex["val"],
                            "period_start": ocf.get("start"), "period_end": ocf["end"],
                        },
                    )
                )

        return out_facts, [raw], missing


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
