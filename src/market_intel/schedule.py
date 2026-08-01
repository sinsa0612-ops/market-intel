"""Calendar slot-key conventions (spec B2 rev2), forward listing,
deterministic change classification, and 13F deadline computation.

Calendar events are stored as ordinary `fact_revisions` rows — no new table
(spec B2: "새 테이블을 만들지 않는다").

**The unit of identity is a year's timetable, not a single release.** One
fact = "everything this source has published about year Y", so
`subject` carries no date at all (`fredrel:192`, `fomc`, `NVDA`),
`event_at` is 1 Jan of Y, and every scheduled date of that year lives in
`value_text` as a normalised CSV (`",".join(sorted(set(dates)))`).

Why it has to be this shape (rev1 tried the obvious alternative and broke):
the engine's `_fact_id` is `{provider}:{subject}:{metric}:{event_at[:10]}`
and a provider may never read the DB (spec A5), so fact identity must be a
pure function of one provider response. Under that constraint "a moved date
is a new revision of the same fact" and "two releases in the same month are
not the same fact" cannot both hold while identity is per-release: rev1
anchored on the month and JOLTS' twice-a-month schedule (9 of 26 months)
collapsed into one oscillating fact — false 연기 every day, +2 revisions per
collect, and the month's first date permanently missing from the calendar.
Moving the dates out of the identity and into the value dissolves the
conflict: both dates sit side by side in one value, a move is a value
change (= revision), an unchanged source is byte-identical (= no-op), and
inserting an unscheduled meeting shifts nothing.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db as db_mod

KST = ZoneInfo("Asia/Seoul")

CALENDAR_CATEGORY = "calendar"
SCHEDULED_DATE_METRIC = "scheduled_date"
RELEASE_CADENCE_METRIC = "release_cadence"
CONSENSUS_METRICS = {"consensus_eps", "consensus_revenue"}

# comparison_basis strings the calendar providers stamp on their facts
# (spec B2 rev2). Defined here, imported there, so the convention has one
# home instead of four drifting copies.
YEAR_TIMETABLE_BASIS = "연도 일정표(그 해에 공표된 예정일 전부)"
CADENCE_BASIS = "연간 발표 빈도(그 해 영업일 발표 건수)"

# Max |Δdays| at which a disappeared date and an appeared date are still
# read as the same release moving (spec B2 rev2 step 2). Beyond it, the new
# date is the next cycle rather than a delay of the old one.
MOVE_PAIRING_DAYS = 45

DENSITY_GUARD_RATIO = 0.6  # spec B3.3 (imported by fred_calendar)

# spec ST1 boundary: the Korean statistics release calendar is deliberately
# NOT implemented, so the gap is registered with the evidence of what was
# tried instead of being filled with a guess.
KR_MACRO_GAP_ID = "kr_macro_calendar:release_date"
KR_MACRO_GAP_REASON = (
    "ECOS OpenAPI에 캘린더 없음(재확인, 2026-08-01). kostat.go.kr은 mods.go.kr(국가데이터처)로 "
    "리다이렉트되며 시도한 5개 경로 어디에도 '공표일정' 문자열 없음. kosis.kr/openapi는 별도 키 필요(미보유). "
    "BOK 통계공표일정 페이지(/portal/stats/statsPublictSchdul/listCldr.do?menuNo=200775)는 200이고 "
    "74개 항목이 렌더되지만 월 이동 파라미터(sdate/edate)가 무시됨(2026-08 요청에 2025년 항목 반환) "
    "— 계약 미확정, 추정으로 채우지 않음."
)

# spec §제외한 것 (rev2 갱신): 취소된 일정은 연도 일정표 diff에서 짝 없는
# `removed`로 관측은 되지만 '취소'로 분류·표시하지는 않는다.
CANCELLATION_GAP_ID = "calendar:cancellation_detection"
CANCELLATION_GAP_REASON = (
    "부족유형: 분류 부족. rev2의 연도 일정표 diff에서 사라진 예정일은 짝 없는 removed로 "
    "관측 가능하고, 최신 revision에 없으므로 캘린더에서도 이미 자동으로 빠진다"
    "(rev1의 '관측 불가'에서 성격이 바뀜). 다만 그것이 취소인지 단순 미공표인지 "
    "구분할 근거가 소스에 없어 '취소'로 분류·보고하지 않는다 — 이번 범위 밖."
)

# spec B7 — fixed data_status display mapping (duplicated here rather than
# imported from reporting/, which does not exist yet in this run: ST1 does
# not depend on ST2's reporting/ package, and B7's mapping is a two-line
# constant, not shared logic).
DATA_STATUS_KO = {
    "source_verified": "원자료 확인",
    "reconstructed": "복원 완료",
    "partial": "부분 확인",
    "unverified": "미확인",
}


def serialize_dates(dates) -> str:
    """The normalised serialisation every calendar provider must use (spec
    B2 rev2): sorted, de-duplicated, `YYYY-MM-DD`, comma-joined, no spaces.
    Any other ordering makes an unchanged source look changed and appends a
    ghost revision on every collect."""
    return ",".join(sorted({d for d in dates if d}))


def _parse_extra(row) -> dict:
    raw = row["extra_json"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _dates_of(row) -> list[str]:
    """The timetable a revision carries, as ISO date strings."""
    out = []
    for chunk in (row["value_text"] or "").split(","):
        chunk = chunk.strip()
        try:
            out.append(date.fromisoformat(chunk).isoformat())
        except ValueError:
            continue
    return sorted(set(out))


def _display_name(subject: str, extra: dict) -> str:
    if subject == "fomc":
        return "FOMC"
    if subject == "bokmpc":
        return "한국은행 금융통화위원회"
    if subject.startswith("fredrel:"):
        return extra.get("release_name", subject)
    return f"{subject} 실적"  # bare symbol -> earnings calendar timetable


def _importance(subject: str, extra: dict) -> str:
    """§5.2 결정론 규칙 (spec B6) applied to forward-looking calendar rows.
    A and B are enumerated lists; everything else is C — scheduled earnings
    dates are in neither list, so they are C. Detected events (8-K 2.02,
    DART) and Core16 ±5%/±3-5% moves are graded by reporting/build.py (ST2)
    from actual fact data, not here — this only grades *scheduled* dates."""
    if subject in ("fomc", "bokmpc"):
        return "A"
    if subject.startswith("fredrel:"):
        name = extra.get("release_name", "")
        return "A" if name in ("Consumer Price Index", "Employment Situation") else "B"
    return "C"


def _kst_date(dt) -> date:
    return datetime.fromisoformat(db_mod.iso_utc(dt)).astimezone(KST).date()


def upcoming(conn, cutoff, days: int = 60) -> list[dict]:
    """Forward-looking calendar: every date of every year-timetable fact
    (point-in-time as of `cutoff`) expanded into its own row, plus computed
    13F deadlines, restricted to [today, today+days] where `today` is
    `cutoff`'s KST date.

    `release_cadence` facts are structurally excluded — they are filtered
    out by the metric filter, which is what keeps a genuine daily series
    (H.15, ~250 business days a year) from burying the calendar."""
    rows = db_mod.facts_as_of(conn, cutoff, category=CALENDAR_CATEGORY, metric=SCHEDULED_DATE_METRIC)
    today = _kst_date(cutoff)
    window_end = today + timedelta(days=days)

    out: list[dict] = []
    for row in rows:
        extra = _parse_extra(row)
        for scheduled in _dates_of(row):
            d = date.fromisoformat(scheduled)
            if not (today <= d <= window_end):
                continue
            out.append(
                {
                    "date": d.isoformat(), "importance": _importance(row["subject"], extra),
                    "country": row["country"] or "", "name": _display_name(row["subject"], extra),
                    "subject": row["subject"], "status": DATA_STATUS_KO.get(row["data_status"], row["data_status"] or ""),
                }
            )

    for deadline in thirteen_f_deadlines(today, n=4):
        if today <= date.fromisoformat(deadline) <= window_end:
            out.append(
                {
                    "date": deadline, "importance": "B", "country": "US", "name": "13F 제출 마감",
                    "subject": "", "status": DATA_STATUS_KO["reconstructed"],
                }
            )

    out.sort(key=lambda r: (r["date"], r["importance"], r["name"]))
    return out


def _pair_moves(removed: list[date], added: list[date]) -> list[tuple[date, date]]:
    """1:1 greedy pairing of disappeared and appeared dates within
    ±MOVE_PAIRING_DAYS, closest first (spec B2 rev2 step 2). Deterministic:
    ties break on the dates themselves, never on dict/set ordering."""
    candidates = sorted(
        (abs((a - r).days), r, a)
        for r in removed
        for a in added
        if abs((a - r).days) <= MOVE_PAIRING_DAYS
    )
    used_r: set[date] = set()
    used_a: set[date] = set()
    pairs: list[tuple[date, date]] = []
    for _delta, r, a in candidates:
        if r in used_r or a in used_a:
            continue
        used_r.add(r)
        used_a.add(a)
        pairs.append((r, a))
    return pairs


def _row(date_str: str, kind: str, name: str, old: str, new: str) -> dict:
    return {"date": date_str, "kind": kind, "name": name, "old": old, "new": new}


def changes(conn, since, days: int | None = None, today: date | None = None) -> list[dict]:
    """Deterministic calendar-change classification (spec B2 rev2, no AI).

    Diffs the date SET of a timetable fact's latest revision against its
    immediately preceding one. Because both sets are whole years, a delay,
    an advance, a next-quarter roll and an inserted unscheduled meeting are
    all just set differences — there are no ordinals to shift and no
    per-date slot that can be silently overwritten.

    `days`, when given, trims the output to dates inside [today, today+days]
    (spec B2 rev2), so the ~12-row 신규 burst of a newly published next-year
    timetable does not drown the screen. `today` exists for tests; it
    defaults to the current KST date.
    """
    since_str = db_mod.iso_utc(since)
    rows = conn.execute(
        "SELECT * FROM fact_revisions WHERE category=? ORDER BY fact_id ASC, revision_no ASC",
        (CALENDAR_CATEGORY,),
    ).fetchall()

    lineages: dict[str, list] = {}
    for row in rows:
        lineages.setdefault(row["fact_id"], []).append(row)

    # (subject, year) -> that timetable's dates, so a consensus revision can
    # be reported against the earnings date it belongs to instead of against
    # its own anchor (1 Jan), which would be a meaningless calendar row.
    schedule_dates: dict[tuple[str, str], list[str]] = {}
    for seq in lineages.values():
        latest = seq[-1]
        if latest["metric"] == SCHEDULED_DATE_METRIC:
            schedule_dates[(latest["subject"], (latest["event_at"] or "")[:4])] = _dates_of(latest)

    out: list[dict] = []
    for seq in lineages.values():
        latest = seq[-1]
        if latest["known_at"] < since_str:
            continue
        metric = latest["metric"]
        extra = _parse_extra(latest)
        name = _display_name(latest["subject"], extra)

        if metric in CONSENSUS_METRICS:
            if len(seq) < 2:
                continue  # a first consensus reading is not a revision
            sibling = schedule_dates.get((latest["subject"], (latest["event_at"] or "")[:4])) or []
            date_col = sibling[-1] if sibling else (latest["event_at"] or "")[:10]
            out.append(_row(date_col, "consensus_revised", name,
                            _fmt_num(seq[-2]["value_num"]), _fmt_num(latest["value_num"])))
            continue

        if metric != SCHEDULED_DATE_METRIC:
            continue  # release_cadence and friends are not calendar events

        new_dates = [date.fromisoformat(d) for d in _dates_of(latest)]
        if len(seq) == 1:
            # spec B2 rev2 step 5 — a brand-new timetable: every date is new.
            for d in new_dates:
                out.append(_row(d.isoformat(), "신규", name, "-", d.isoformat()))
            continue

        old_dates = [date.fromisoformat(d) for d in _dates_of(seq[-2])]
        added = sorted(set(new_dates) - set(old_dates))
        removed = sorted(set(old_dates) - set(new_dates))
        pairs = _pair_moves(removed, added)
        paired_added = {a for _r, a in pairs}

        for r, a in pairs:
            kind = "연기" if a > r else "앞당김"
            out.append(_row(a.isoformat(), kind, name, r.isoformat(), a.isoformat()))

        last_old = max(old_dates) if old_dates else None
        for a in added:
            if a in paired_added:
                continue
            if last_old is not None and a > last_old:
                # spec B2 rev2 step 3 — the rolling earnings slot moving to
                # the next quarter lands here, not in 연기.
                out.append(_row(a.isoformat(), "next_cycle", name, last_old.isoformat(), a.isoformat()))
            else:
                out.append(_row(a.isoformat(), "신규", name, "-", a.isoformat()))
        # Unpaired `removed` is deliberately not reported (spec B2 rev2
        # step 4 / §제외한 것: cancellation classification is out of scope).
        # It has already dropped out of the calendar, since `upcoming()`
        # reads the latest revision only.

    if days is not None:
        start = today or _kst_date(datetime.now(timezone.utc))
        end = start + timedelta(days=days)
        out = [r for r in out if r["date"] and start <= date.fromisoformat(r["date"]) <= end]

    out.sort(key=lambda r: (r["date"], r["kind"], r["name"]))
    return out


def _fmt_num(v) -> str:
    return "" if v is None else f"{v:g}"


def ensure_calendar_gaps(conn) -> None:
    """Registers the two structural calendar gaps this subtask deliberately
    does NOT implement (spec ST1 boundary + §제외한 것): the KR macro release
    calendar, and cancellation classification. INSERT OR IGNORE keyed by
    gap_id — idempotent, safe to call on every calendar collect."""
    now = db_mod.iso_utc()
    conn.executemany(
        "INSERT OR IGNORE INTO data_gaps(gap_id, subject, metric, detected_at, reason, status) "
        "VALUES (?,?,?,?,?,?)",
        [
            (KR_MACRO_GAP_ID, "kr_macro_calendar", "release_date", now, KR_MACRO_GAP_REASON, "보류"),
            (CANCELLATION_GAP_ID, "calendar", "cancellation_detection", now, CANCELLATION_GAP_REASON, "보류"),
        ],
    )
    conn.commit()


# --- 13F-HR deadlines (spec B4): pure calendar function, no network, no
# storage — a deterministic function can never produce a revision, so
# persisting its output would add no information. -------------------------

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last_day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _us_market_holidays(year: int) -> set[date]:
    """[ASSUMPTION] Fixed federal holidays that can plausibly fall inside a
    45-day post-quarter-end window; not a complete SEC holiday calendar,
    but the ones that matter for this window (spec B4: "휴일이면 익영업일")."""
    return {
        date(year, 1, 1), _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0), date(year, 6, 19), date(year, 7, 4),
        _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2), date(year, 11, 11),
        _nth_weekday(year, 11, 3, 4), date(year, 12, 25),
    }


def _next_business_day(d: date) -> date:
    holidays = _us_market_holidays(d.year) | _us_market_holidays(d.year + 1)
    while d.weekday() >= 5 or d in holidays:
        d += timedelta(days=1)
    return d


def thirteen_f_deadlines(today: date, n: int = 4) -> list[str]:
    """Next `n` 13F-HR deadlines (quarter end + 45 calendar days, rolled
    forward to the next business day) from `today` onward."""
    quarter_ends = []
    y = today.year - 1
    while len(quarter_ends) < n + 8:
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            quarter_ends.append(date(y, month, day))
        y += 1
    quarter_ends.sort()

    out: list[str] = []
    for qe in quarter_ends:
        deadline = _next_business_day(qe + timedelta(days=45))
        if deadline >= today:
            out.append(deadline.isoformat())
        if len(out) >= n:
            break
    return out
