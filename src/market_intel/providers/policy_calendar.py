"""Policy calendar — FOMC decision days (federalreserve.gov HTML) and Bank
of Korea Monetary Policy Committee decision days (bok.or.kr), fetched and
parsed independently: one source failing must never take the other down
(spec ST1 What #3). Both are `re`-only scrapes (spec B0 forbids
BeautifulSoup/lxml), anchored on structure confirmed by a live fetch on
2026-08-01 — see `tests/fixtures/fomccalendars.htm` and
`tests/fixtures/bok_listYear_*.htm` for the frozen snapshots the offline
tests replay.

Neither source is ever guessed at: fewer than `MIN_MEETINGS_FOR_A_YEAR`
parsed meetings for a requested year is treated as "can't trust this parse"
(site restructured, or — for bok.or.kr's `pYear` one year ahead — the
schedule genuinely isn't published yet) and returns nothing for that year
rather than a partial/wrong calendar (spec ST1 test_policy_calendar_parse).
FOMC is sourced ONLY here, never through fred_calendar.py's FRED release
scan (spec B3.4 — that path is a data trap, not a source)."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem
from ..schedule import YEAR_TIMETABLE_BASIS, serialize_dates

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BOK_URL = "https://www.bok.or.kr/portal/singl/crncyPolicyDrcMtg/listYear.do"

MIN_MEETINGS_FOR_A_YEAR = 6

FOMC_YEAR_HEADER_RE = re.compile(r'<h4>\s*<a id="\d+">(\d{4}) FOMC Meetings</a>\s*</h4>')
FOMC_MONTH_RE = re.compile(r'fomc-meeting__month[^>]*><strong>([A-Za-z]+)</strong>')
FOMC_DATE_RE = re.compile(r'fomc-meeting__date[^>]*>([^<]+)<')
BOK_DATE_RE = re.compile(r'(\d{2})월\s*(\d{2})일\(([월화수목금토일])\)')

MONTH_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _year_anchor(year: int) -> str:
    return datetime(year, 1, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")


def parse_fomc_meetings(html: str, year: int) -> list[str]:
    """ISO end-dates for FOMC meetings in `year`. [] if the year's `<h4>`
    section isn't found, or a meeting's date field doesn't parse cleanly —
    this never falls back to guessing."""
    headers = list(FOMC_YEAR_HEADER_RE.finditer(html))
    segment = None
    for i, m in enumerate(headers):
        if int(m.group(1)) == year:
            end = headers[i + 1].start() if i + 1 < len(headers) else len(html)
            segment = html[m.end():end]
            break
    if segment is None:
        return []

    out = []
    for month_name, date_field in zip(FOMC_MONTH_RE.findall(segment), FOMC_DATE_RE.findall(segment)):
        month_num = MONTH_NUM.get(month_name)
        if month_num is None:
            continue
        field = date_field.strip().rstrip("*")
        m = re.match(r"^(\d{1,2})-(\d{1,2})$", field)
        if not m:
            continue
        try:
            out.append(date(year, month_num, int(m.group(2))).isoformat())
        except ValueError:
            continue
    return sorted(out)


def parse_bok_meetings(html: str, year: int) -> list[str]:
    """ISO dates for BOK 금융통화위원회 meetings in `year`. The page is
    already scoped to that year via the `pYear` query param."""
    out = set()
    for mm, dd, _weekday in BOK_DATE_RE.findall(html):
        try:
            out.add(date(year, int(mm), int(dd)).isoformat())
        except ValueError:
            continue
    return sorted(out)


class PolicyCalendarProvider:
    name = "policy_calendar"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        client = ctx.http("policy_calendar")
        years = [ctx.now.year, ctx.now.year + 1]

        fomc_facts, fomc_raw, fomc_missing = self._fomc(client, years)
        bok_facts, bok_raw, bok_missing = self._bok(client, years)

        facts = fomc_facts + bok_facts
        raw_items = fomc_raw + bok_raw
        missing = fomc_missing + bok_missing

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

    def _fomc(self, client, years: list[int]):
        try:
            resp = client.get(FOMC_URL)
        except Exception as exc:  # noqa: BLE001
            return [], [], [f"fomc:error:{exc.__class__.__name__}"]
        if resp.status_code != 200:
            return [], [], [f"fomc:http_{resp.status_code}"]

        payload = resp.text
        raw_items = [
            RawItem(
                external_id="fomc:calendar", source_published_at=_now_iso(),
                safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
            )
        ]

        facts: list[FactCandidate] = []
        missing: list[str] = []
        for year in years:
            meetings = parse_fomc_meetings(payload, year)
            if len(meetings) < MIN_MEETINGS_FOR_A_YEAR:
                missing.append(f"fomc:{year}:parsed_{len(meetings)}_below_min")
                continue
            # spec B2 rev2: one fact per year, subject carries no ordinal.
            # An unscheduled meeting inserted mid-year is then a single added
            # date, not an off-by-one shift of every later slot (judge §③).
            facts.append(
                FactCandidate(
                    raw_ref="fomc:calendar", subject="fomc", category="calendar", metric="scheduled_date",
                    event_at=_year_anchor(year), market="US", country="US",
                    value_text=serialize_dates(meetings), comparison_basis=YEAR_TIMETABLE_BASIS,
                    publisher="Federal Reserve", data_status="source_verified",
                    extra={"dates": meetings},
                )
            )
        return facts, raw_items, missing

    def _bok(self, client, years: list[int]):
        facts: list[FactCandidate] = []
        raw_items: list[RawItem] = []
        missing: list[str] = []

        for year in years:
            try:
                resp = client.get(BOK_URL, params={"mtgSe": "A", "menuNo": "200755", "pYear": str(year)})
            except Exception as exc:  # noqa: BLE001
                missing.append(f"bokmpc:{year}:error:{exc.__class__.__name__}")
                continue
            if resp.status_code != 200:
                missing.append(f"bokmpc:{year}:http_{resp.status_code}")
                continue

            payload = resp.text
            external_id = f"bokmpc:calendar:{year}"
            raw_items.append(
                RawItem(
                    external_id=external_id, source_published_at=_now_iso(),
                    safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
                )
            )
            meetings = parse_bok_meetings(payload, year)
            if len(meetings) < MIN_MEETINGS_FOR_A_YEAR:
                # bok.or.kr honestly returns 0 rows for a not-yet-published
                # year (e.g. next year, before BOK announces it) — that is
                # not an error, just "not yet available", but we still
                # refuse to publish a fact for it instead of guessing.
                missing.append(f"bokmpc:{year}:parsed_{len(meetings)}_below_min")
                continue
            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject="bokmpc", category="calendar", metric="scheduled_date",
                    event_at=_year_anchor(year), market="KR", country="KR",
                    value_text=serialize_dates(meetings), comparison_basis=YEAR_TIMETABLE_BASIS,
                    publisher="한국은행", data_status="source_verified",
                    extra={"dates": meetings},
                )
            )
        return facts, raw_items, missing
