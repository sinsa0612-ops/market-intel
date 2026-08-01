"""ST1 acceptance tests for schedule.py + the slot-key contract (spec B2
rev2) that the calendar providers must honour.

rev2 changed the unit of identity: a calendar fact is no longer "one
scheduled release" but "the whole year's schedule as published by that
source", `subject` carrying no date at all and `event_at` anchored on
1 Jan of the year the timetable covers. The dates live in `value_text` as a
normalised CSV. That is what makes a same-month double release survive
(both dates sit side by side in one value) while a date *move* is still a
revision of the same fact_id.

Every test here runs through the real engine (`run_collect` with
`transport_factory` / a patched yfinance `Ticker`), never hand-seeded DB
rows: rev1's judgement showed a hand-seeded test passes even when the
provider violates the anchor contract outright.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from market_intel import db as db_mod
from market_intel import schedule as schedule_mod
from market_intel.engine import run_collect
from market_intel.providers.earnings_calendar import EarningsCalendarProvider
from market_intel.providers.fred_calendar import FredCalendarProvider
from market_intel.providers.policy_calendar import PolicyCalendarProvider

RELEASE_DATES_PREFIX = "https://api.stlouisfed.org/fred/release/dates"
RELEASES_PREFIX = "https://api.stlouisfed.org/fred/releases"

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _this_year() -> int:
    return datetime.now(timezone.utc).year


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _fred_handler(dates_for, releases: dict):
    """MockTransport handler answering FRED's two endpoints. `dates_for(rid,
    year)` decides what the year query returns, mirroring the real API's
    `realtime_start={Y}-01-01&realtime_end={Y}-12-31` contract (spec B3.2)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(RELEASE_DATES_PREFIX):
            params = dict(request.url.params)
            rid = int(params["release_id"])
            year = int(params["realtime_start"][:4])
            dates = dates_for(rid, year)
            return httpx.Response(200, json={"release_dates": [{"release_id": rid, "date": d} for d in dates]})
        if url.startswith(RELEASES_PREFIX):
            return httpx.Response(200, json=releases)
        return httpx.Response(404)

    return handler


def _collect_fred(settings, dates_for, releases):
    return run_collect(
        settings, [], {"fred_calendar": FredCalendarProvider()}, "calendar", None,
        transport_factory=lambda _p: httpx.MockTransport(_fred_handler(dates_for, releases)),
    )


CPI_RELEASES = {"releases": [{"id": 10, "name": "Consumer Price Index"}]}
JOLTS_RELEASES = {"releases": [{"id": 192, "name": "Job Openings and Labor Turnover Survey"}]}


def test_slot_key_stable_across_move(settings):
    """spec ST1 test_slot_key_stable_across_move: moving a scheduled date
    08-12 -> 08-19 across two collects must append revision 2 to the SAME
    fact_id (anchored on 1 Jan of the timetable's year), and
    schedule.changes() must report exactly one 연기."""
    settings.fred_api_key = "FAKEFREDKEY"
    db_mod.init_db(settings.db_path)
    y = _this_year()

    _collect_fred(settings, lambda rid, year: [f"{y}-08-12"] if year == y else [], CPI_RELEASES)
    _collect_fred(settings, lambda rid, year: [f"{y}-08-19"] if year == y else [], CPI_RELEASES)

    conn = db_mod.connect(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM fact_revisions WHERE subject='fredrel:10' ORDER BY revision_no"
    ).fetchall()
    assert len(rows) == 2, "a date move must be 2 revisions of 1 fact, not 2 separate facts"
    assert rows[0]["fact_id"] == rows[1]["fact_id"] == f"fred_calendar:fredrel:10:scheduled_date:{y}0101"
    assert rows[0]["revision_no"] == 1 and rows[1]["revision_no"] == 2
    assert rows[1]["supersedes_revision"] == 1
    assert rows[0]["value_text"] == f"{y}-08-12" and rows[1]["value_text"] == f"{y}-08-19"
    assert rows[0]["comparison_basis"] == schedule_mod.YEAR_TIMETABLE_BASIS

    result = schedule_mod.changes(conn, since="2020-01-01T00:00:00+00:00")
    conn.close()

    delayed = [c for c in result if c["kind"] == "연기"]
    assert len(delayed) == 1, result
    assert delayed[0]["old"] == f"{y}-08-12" and delayed[0]["new"] == f"{y}-08-19"


def _double_release_dates() -> list[str]:
    """Four future dates inside ONE calendar year, two of them in the same
    month — the real FRED JOLTS shape (2026-08-04, 09-01, 09-29, 11-03,
    fetched live 2026-08-01), expressed relative to today so the test does
    not rot. If the natural months would straddle a year boundary the whole
    block moves to next January, keeping "one year's timetable" intact."""
    today = datetime.now(timezone.utc).date()
    base = _add_months(date(today.year, today.month, 1), 1)
    if _add_months(base, 3).year != base.year:
        base = date(today.year + 1, 1, 1)
    m1 = _add_months(base, 1)
    return [
        (base + timedelta(days=3)).isoformat(),
        m1.isoformat(),
        (m1 + timedelta(days=27)).isoformat(),
        (_add_months(base, 3) + timedelta(days=2)).isoformat(),
    ]


def test_same_month_double_release(settings):
    """spec ST1 test_same_month_double_release (rev2) — the case that killed
    rev1: JOLTS publishes twice in the same month. Both dates must show up
    as their own calendar rows, neither may be reported as a delay of the
    other, and re-collecting unchanged input must append nothing."""
    settings.fred_api_key = "FAKEFREDKEY"
    db_mod.init_db(settings.db_path)
    dates = _double_release_dates()
    year = int(dates[0][:4])
    dates_for = lambda rid, y: dates if y == year else []  # noqa: E731

    first = _collect_fred(settings, dates_for, JOLTS_RELEASES)
    assert first["providers"]["fred_calendar"]["facts_appended"] == 1, "one year timetable == one fact"

    conn = db_mod.connect(settings.db_path)

    # 4. storage shape: one fact, all four dates in a normalised CSV
    rows = conn.execute("SELECT * FROM fact_revisions WHERE subject='fredrel:192'").fetchall()
    assert len(rows) == 1
    assert rows[0]["fact_id"] == f"fred_calendar:fredrel:192:scheduled_date:{year}0101"
    assert rows[0]["value_text"] == ",".join(dates)

    # 1. both same-month dates survive as their own calendar rows
    cutoff = datetime.now(timezone.utc)
    upcoming = schedule_mod.upcoming(conn, cutoff, days=400)
    jolts = [r for r in upcoming if r["subject"] == "fredrel:192"]
    assert [r["date"] for r in jolts] == dates, jolts
    same_month = [r for r in jolts if r["date"][:7] == dates[1][:7]]
    assert len(same_month) == 2, "both releases of the doubled month must appear"

    # 2. no false 연기/앞당김 between the two dates of that month
    changed = schedule_mod.changes(conn, since="2020-01-01T00:00:00+00:00")
    assert [c for c in changed if c["kind"] in ("연기", "앞당김")] == []
    before = conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"]
    conn.close()

    # 3. idempotency: same input again -> zero appended revisions
    second = _collect_fred(settings, dates_for, JOLTS_RELEASES)
    assert second["providers"]["fred_calendar"]["facts_appended"] == 0

    conn = db_mod.connect(settings.db_path)
    after = conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"]
    conn.close()
    assert after == before, "a second identical collect must not append any revision"


def test_fred_real_daily_series_is_kept_but_never_floods_the_calendar(settings):
    """spec ST1 test_fred_real_daily_series_kept (rev2): H.15 publishes every
    business day (250/yr, weekend count 0). The density guard alone would
    discard it as daily-fill garbage; the weekend discriminator keeps it as
    a single `release_cadence` fact that never becomes a calendar row."""
    settings.fred_api_key = "FAKEFREDKEY"
    db_mod.init_db(settings.db_path)
    y = _this_year()
    weekdays = []
    d = date(y, 1, 1)
    while d.year == y:
        if d.weekday() < 5:
            weekdays.append(d.isoformat())
        d += timedelta(days=1)

    _collect_fred(
        settings,
        lambda rid, year: weekdays if year == y else [],
        {"releases": [{"id": 18, "name": "H.15 Selected Interest Rates"}]},
    )

    conn = db_mod.connect(settings.db_path)
    detail = conn.execute(
        "SELECT safe_detail FROM provider_runs WHERE provider='fred_calendar' ORDER BY started_at DESC LIMIT 1"
    ).fetchone()["safe_detail"]
    assert f"daily_series_kept:{len(weekdays)}" in detail
    assert "daily_fill_discarded" not in detail

    rows = conn.execute("SELECT * FROM fact_revisions WHERE subject='fredrel:18'").fetchall()
    assert len(rows) == 1
    assert rows[0]["metric"] == "release_cadence"
    assert rows[0]["value_text"] == "daily_business_day"
    assert rows[0]["value_num"] == len(weekdays)

    upcoming = schedule_mod.upcoming(conn, datetime.now(timezone.utc), days=400)
    conn.close()
    assert [r for r in upcoming if r["subject"] == "fredrel:18"] == [], "a daily series must not flood the calendar"


def _earnings_ticker_factory(date_by_symbol: dict[str, str]):
    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def calendar(self):
            d = date_by_symbol.get(self.symbol)
            if d is None:
                return {}
            return {"Earnings Date": [date.fromisoformat(d)], "Earnings Average": 1.23, "Revenue Average": 999.0}

    return _Ticker


def test_earnings_next_cycle(settings, monkeypatch):
    """spec ST1 test_earnings_next_cycle: 08-27 -> 10-29 (Δ63일) is the next
    quarter's cycle, not a delay — the jump is past the ±45일 pairing
    window, so the old date drops out and the new one is classified
    next_cycle."""
    from market_intel.providers import earnings_calendar as ec_mod

    db_mod.init_db(settings.db_path)
    registry = {"earnings_calendar": EarningsCalendarProvider()}
    y = _this_year()

    monkeypatch.setattr(ec_mod.yf, "Ticker", _earnings_ticker_factory({"NVDA": f"{y}-08-27"}))
    run_collect(settings, [], registry, "calendar", None, transport_factory=lambda _p: None)
    monkeypatch.setattr(ec_mod.yf, "Ticker", _earnings_ticker_factory({"NVDA": f"{y}-10-29"}))
    run_collect(settings, [], registry, "calendar", None, transport_factory=lambda _p: None)

    conn = db_mod.connect(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM fact_revisions WHERE subject='NVDA' AND metric='scheduled_date' ORDER BY revision_no"
    ).fetchall()
    assert len(rows) == 2, "same-year moves stay one fact"
    assert rows[0]["fact_id"] == rows[1]["fact_id"] == f"earnings_calendar:NVDA:scheduled_date:{y}0101"

    result = schedule_mod.changes(conn, since="2020-01-01T00:00:00+00:00")
    conn.close()
    nvda = [c for c in result if c["name"] == "NVDA 실적"]
    assert any(c["kind"] == "next_cycle" for c in nvda), nvda
    assert not any(c["kind"] == "연기" for c in nvda), nvda


def test_earnings_consensus_currency_follows_the_listing_market(settings, monkeypatch):
    """A Korean issuer's consensus EPS is won, not dollars — 삼성전자's
    14,322 stored as USD would be published as "$14,322 EPS" by ST2."""
    from market_intel.providers import earnings_calendar as ec_mod

    db_mod.init_db(settings.db_path)
    y = _this_year()
    monkeypatch.setattr(
        ec_mod.yf, "Ticker",
        _earnings_ticker_factory({"005930.KS": f"{y}-10-28", "NVDA": f"{y}-08-27"}),
    )
    run_collect(settings, [], {"earnings_calendar": EarningsCalendarProvider()}, "calendar", None,
                transport_factory=lambda _p: None)

    conn = db_mod.connect(settings.db_path)
    units = dict(
        conn.execute(
            "SELECT subject || '|' || metric AS k, unit FROM fact_revisions WHERE metric LIKE 'consensus_%'"
        ).fetchall()
    )
    conn.close()
    assert units["005930.KS|consensus_eps"] == "KRW/share"
    assert units["005930.KS|consensus_revenue"] == "KRW"
    assert units["NVDA|consensus_eps"] == "USD/share"
    assert units["NVDA|consensus_revenue"] == "USD"


FOMC_HTML = (FIXTURES / "fomccalendars.htm").read_text()
BOK_2026_HTML = (FIXTURES / "bok_listYear_2026.htm").read_text()
BOK_2027_HTML = (FIXTURES / "bok_listYear_2027.htm").read_text()

_UNSCHEDULED_BLOCK = (
    '<div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>October</strong></div>'
    '<div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">5-6</div>'
)


def _fomc_html_with_unscheduled_meeting() -> str:
    """Insert an unscheduled Oct 5-6 meeting in the MIDDLE of the 2026 panel
    (after the April block) — rev1's `fomc:{YYYY}:{n}` ordinal key shifted
    every later meeting by one and produced a wall of false 연기."""
    marker = '<div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>April</strong></div>'
    idx = FOMC_HTML.index(marker)
    return FOMC_HTML[:idx] + _UNSCHEDULED_BLOCK + FOMC_HTML[idx:]


def _policy_handler(fomc_html: str):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "federalreserve.gov" in url:
            return httpx.Response(200, text=fomc_html)
        if "bok.or.kr" in url:
            py = dict(request.url.params).get("pYear")
            return httpx.Response(200, text=BOK_2026_HTML if py == "2026" else BOK_2027_HTML)
        return httpx.Response(404)

    return handler


def test_policy_unscheduled_meeting_no_ordinal_shift(settings):
    """spec ST1 test_policy_unscheduled_meeting_no_ordinal_shift (rev2,
    judge §③): inserting an unscheduled FOMC meeting must produce exactly
    one 신규 and zero 연기 — the year-timetable key has no ordinals to shift."""
    db_mod.init_db(settings.db_path)
    registry = {"policy_calendar": PolicyCalendarProvider()}

    run_collect(settings, [], registry, "calendar", None,
                transport_factory=lambda _p: httpx.MockTransport(_policy_handler(FOMC_HTML)))
    run_collect(settings, [], registry, "calendar", None,
                transport_factory=lambda _p: httpx.MockTransport(
                    _policy_handler(_fomc_html_with_unscheduled_meeting())))

    conn = db_mod.connect(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM fact_revisions WHERE subject='fomc' AND event_at LIKE '2026%' ORDER BY revision_no"
    ).fetchall()
    assert [r["revision_no"] for r in rows] == [1, 2], "one timetable fact per year, two revisions"

    result = schedule_mod.changes(conn, since="2020-01-01T00:00:00+00:00")
    conn.close()
    fomc = [c for c in result if c["name"] == "FOMC" and c["date"] == "2026-10-06"]
    assert [c["kind"] for c in fomc] == ["신규"], result
    assert [c for c in result if c["name"] == "FOMC" and c["kind"] == "연기"] == []


def test_no_new_tables():
    """spec ST1 test_no_new_tables: calendar facts live in fact_revisions —
    db.SCHEMA must not gain a new table."""
    import re

    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db_mod.SCHEMA))
    assert tables == {"raw_snapshots", "fact_revisions", "collect_runs", "provider_runs", "data_gaps", "label_revisions"}


def test_serialize_dates_normalisation_is_stable():
    """spec B2 rev2 정규화 직렬화 — sorted, de-duplicated, no spaces. An
    unstable serialisation makes an unchanged source look changed, which is
    a ghost revision on every single collect."""
    assert schedule_mod.serialize_dates(["2026-09-11", "2026-08-12", "2026-08-12"]) == "2026-08-12,2026-09-11"
    assert schedule_mod.serialize_dates(["2026-08-12"]) == "2026-08-12"
    assert schedule_mod.serialize_dates([]) == ""


def _seed_timetable(conn, raw_dir, provider, subject, dates, known_at, year, metric="scheduled_date", **kw):
    """Seeds ONE year-timetable fact the way a provider would. Used only by
    the pure schedule.py read-path tests below; every provider-contract test
    in this file goes through the real provider instead."""
    from market_intel.models import FactCandidate, RawItem

    raw = RawItem(external_id=subject, source_published_at=known_at, safe_source_url="https://example.test", payload="{}")
    snap_id = db_mod.insert_raw_snapshot(conn, raw_dir, provider, raw)
    fc = FactCandidate(
        raw_ref=subject, subject=subject, category="calendar", metric=metric,
        event_at=f"{year}-01-01T00:00:00+00:00", market=kw.pop("market", "US"), country=kw.pop("country", "US"),
        value_text=",".join(dates), comparison_basis=schedule_mod.YEAR_TIMETABLE_BASIS,
        data_status=kw.pop("data_status", "source_verified"), extra={"dates": list(dates)}, **kw,
    )
    db_mod.upsert_fact(conn, f"{provider}:{subject}:{metric}:{year}0101", snap_id, known_at, fc)
    conn.commit()


def test_only_scheduled_date_facts_become_calendar_rows(settings):
    """The calendar is defined by the METRIC, not by whether a value happens
    to parse as dates: a `release_cadence` fact is a statement about how
    often something publishes, never a row on the calendar (spec B3.3)."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_timetable(
        conn, settings.raw_dir, "fred_calendar", "fredrel:18", ["2026-08-03", "2026-08-04"],
        "2026-08-01T00:00:00+00:00", 2026, metric="release_cadence",
    )
    rows = schedule_mod.upcoming(conn, datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc), days=60)
    conn.close()
    assert [r for r in rows if r["subject"] == "fredrel:18"] == []


def test_upcoming_expands_the_timetable_and_merges_13f_deadlines(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_timetable(
        conn, settings.raw_dir, "policy_calendar", "fomc",
        ["2026-09-16", "2026-10-28", "2026-12-09"], "2026-08-01T00:00:00+00:00", 2026,
    )

    cutoff = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = schedule_mod.upcoming(conn, cutoff, days=60)
    conn.close()

    fomc = [r for r in rows if r["name"] == "FOMC"]
    assert [r["date"] for r in fomc] == ["2026-09-16"], "only in-window dates of the timetable"
    assert fomc[0]["importance"] == "A" and fomc[0]["status"] == "원자료 확인"
    assert any(r["name"] == "13F 제출 마감" for r in rows), "13F deadlines must be merged into the listing"


def test_earnings_rows_are_importance_c(settings):
    """spec B6: A/B are enumerated, 실적 예정일 is not in either list -> C."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_timetable(
        conn, settings.raw_dir, "earnings_calendar", "NVDA", ["2026-08-27"],
        "2026-08-01T00:00:00+00:00", 2026, data_status="partial",
    )
    rows = schedule_mod.upcoming(conn, datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc), days=60)
    conn.close()
    nvda = next(r for r in rows if r["subject"] == "NVDA")
    assert nvda["importance"] == "C"
    assert nvda["name"] == "NVDA 실적"


def test_changes_reports_a_brand_new_timetable_as_added(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_timetable(
        conn, settings.raw_dir, "policy_calendar", "bokmpc", ["2026-08-27", "2026-10-22"],
        "2026-08-01T00:00:00+00:00", 2026, market="KR", country="KR",
    )
    result = schedule_mod.changes(conn, since="2026-07-01T00:00:00+00:00")
    conn.close()
    assert [c["kind"] for c in result] == ["신규", "신규"]
    assert [c["date"] for c in result] == ["2026-08-27", "2026-10-22"]
    assert result[0]["name"] == "한국은행 금융통화위원회"
    assert result[0]["old"] == "-" and result[0]["new"] == "2026-08-27"


def test_changes_window_filters_to_the_forward_days(settings):
    """spec B2 rev2: `--days` trims the output to dates inside the window, so
    a freshly published next-year timetable does not print 12 rows."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_timetable(
        conn, settings.raw_dir, "policy_calendar", "bokmpc", ["2026-08-03", "2026-11-26"],
        "2026-08-01T00:00:00+00:00", 2026, market="KR", country="KR",
    )
    result = schedule_mod.changes(
        conn, since="2026-07-01T00:00:00+00:00", days=7,
        today=date(2026, 8, 1),
    )
    conn.close()
    assert [c["date"] for c in result] == ["2026-08-03"]


def test_thirteen_f_deadlines_are_business_days_and_forward_only():
    today = date(2026, 8, 1)
    deadlines = schedule_mod.thirteen_f_deadlines(today, n=4)
    assert len(deadlines) == 4
    for d in deadlines:
        parsed = date.fromisoformat(d)
        assert parsed >= today
        assert parsed.weekday() < 5, f"{d} falls on a weekend"
    assert deadlines == sorted(deadlines)
