"""SEC EDGAR — 13F-HR filing detection for the 5 tracked superinvestor
managers (spec §9.2 핵심 추적군, §9.4 step 1 only: "대상 운용사의 13F-HR 또는
13F-HR/A 제출을 감지한다"). Cover-page/holdings-table normalization, 대가
카드, 종목 카드 (§9.4 steps 2-7) are Stage 2 analysis, out of scope here.

CIKs for the 5 managers are resolved live via SEC's own company-name search
(browse-edgar, on the same whitelisted www.sec.gov host sec_edgar.py already
uses) instead of being hardcoded — a stale/wrong hand-typed CIK would
silently attribute filings to the wrong entity, and Prime Rule 8's
confidence gate says verify, don't fabricate. A name search can return
either a single matched company (its filings listed directly) or several
companies sharing the search term (each match nested, no filings) — the
latter is resolved by re-querying each candidate CIK directly and keeping
whichever one is still actually filing 13F-HR."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# spec §9.2 — subject slug, display name, company-search term (not a CIK).
TRACKED_MANAGERS = [
    ("berkshire_hathaway", "Berkshire Hathaway", "berkshire hathaway"),
    ("pershing_square", "Pershing Square Capital Management", "pershing square capital"),
    ("baupost_group", "Baupost Group", "baupost group"),
    ("lone_pine_capital", "Lone Pine Capital", "lone pine capital"),
    ("tci_fund_management", "TCI Fund Management", "tci fund management"),
]

MAX_AMBIGUOUS_CANDIDATES = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _search(client, search_term: str, cik: str | None = None):
    params = {
        "action": "getcompany", "type": "13F-HR", "dateb": "", "owner": "include",
        "count": "10", "output": "atom",
    }
    if cik:
        params["CIK"] = cik
    else:
        params["company"] = search_term
    return client.get(BROWSE_EDGAR_URL, params=params)


def _parse_filing_entries(root: ET.Element) -> list[dict]:
    """Single-company mode: each <entry> IS a filing (its <content> carries
    an <accession-number>). Most recent filing first."""
    out = []
    for entry in root.findall("a:entry", ATOM_NS):
        content = entry.find("a:content", ATOM_NS)
        if content is None:
            continue
        acc = content.findtext("a:accession-number", namespaces=ATOM_NS) or content.findtext("accession-number")
        if not acc:
            continue
        out.append({
            "accession_number": acc,
            "filing_date": content.findtext("a:filing-date", namespaces=ATOM_NS) or content.findtext("filing-date"),
            "filing_type": content.findtext("a:filing-type", namespaces=ATOM_NS) or content.findtext("filing-type"),
        })
    return out


def _parse_company_matches(root: ET.Element) -> list[str]:
    """Multi-company mode (ambiguous search term): each <entry> wraps a
    nested <company-info> with a CIK but no filing details."""
    ciks = []
    for entry in root.findall("a:entry", ATOM_NS):
        info = entry.find("a:content/a:company-info", ATOM_NS)
        if info is None:
            continue
        cik = info.findtext("a:cik", namespaces=ATOM_NS) or info.findtext("cik")
        if cik:
            ciks.append(cik.strip())
    return ciks


class Sec13fProvider:
    name = "sec_edgar_13f"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        # Same host whitelist / rate limit / User-Agent as sec_edgar (spec A6) —
        # this provider is a distinct workflow entry, not a distinct HTTP identity.
        client = ctx.http("sec_edgar")
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for subject, display_name, search_term in TRACKED_MANAGERS:
            resp, filings = self._resolve(client, display_name, search_term, missing)
            if resp is None:
                continue

            latest = filings[0]
            external_id = f"13f:{subject}:{latest['accession_number']}"
            raw_items.append(
                RawItem(
                    external_id=external_id, source_published_at=latest["filing_date"] or _now_iso(),
                    safe_source_url=client.safe_url(str(resp.request.url)), payload=resp.text,
                )
            )
            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=subject, category="13f_filing", metric="filing_event",
                    event_at=f"{latest['filing_date']}T00:00:00+00:00", market="US", country="US",
                    value_text=latest["accession_number"], unit="", publisher="SEC EDGAR",
                    data_status="source_verified",
                    extra={"form": latest["filing_type"], "manager": display_name},
                )
            )

        if not facts:
            return ProviderResult(status="NO_DATA", reason_code="empty_response", raw_items=raw_items, safe_detail=("; ".join(missing[:8]))[:400])
        status = "PARTIAL" if missing else "OK"
        return ProviderResult(
            status=status, reason_code=None, raw_items=raw_items, facts=facts,
            safe_detail=("; ".join(missing[:8]))[:400],
        )

    def _resolve(self, client, display_name: str, search_term: str, missing: list[str]):
        """Returns (response_holding_the_chosen_filing, filings) or
        (None, None) with a reason appended to `missing`."""
        try:
            resp = _search(client, search_term)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{display_name}:search_error:{exc.__class__.__name__}")
            return None, None
        if resp.status_code != 200:
            missing.append(f"{display_name}:search_http_{resp.status_code}")
            return None, None
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            missing.append(f"{display_name}:unparseable_response")
            return None, None

        filings = _parse_filing_entries(root)
        if filings:
            return resp, filings

        # Ambiguous name match: no direct filings, but possibly several
        # candidate companies. Re-query each candidate CIK directly and
        # keep the one with the most recent actual 13F-HR filing (the
        # live, currently-filing entity) rather than guessing from the name.
        candidates = _parse_company_matches(root)
        if not candidates:
            missing.append(f"{display_name}:no_match")
            return None, None

        best_resp, best_filings = None, None
        for cik in candidates[:MAX_AMBIGUOUS_CANDIDATES]:
            try:
                cresp = _search(client, search_term, cik=cik)
            except Exception:  # noqa: BLE001
                continue
            if cresp.status_code != 200:
                continue
            try:
                croot = ET.fromstring(cresp.text)
            except ET.ParseError:
                continue
            cfilings = _parse_filing_entries(croot)
            if cfilings and (best_filings is None or cfilings[0]["filing_date"] > best_filings[0]["filing_date"]):
                best_resp, best_filings = cresp, cfilings

        if best_resp is None:
            missing.append(f"{display_name}:no_recent_13f_among_{len(candidates)}_candidates")
            return None, None
        return best_resp, best_filings
