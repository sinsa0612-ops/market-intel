"""`report` CLI (spec B13), wired into `cli.py` only through the
`CLI_EXTENSIONS` hook (spec B1) — `cli.py` never imports this module by
name, and this module never touches `cli.py`."""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT
from . import db as db_mod
from .reporting import build as build_mod
from .reporting import cutoff as cutoff_mod
from .reporting import render_md as render_md_mod
from .reporting.model import DATA_STATUS_KO


def register(sub) -> None:
    p_report = sub.add_parser("report")
    p_report.add_argument("--type", required=True, dest="report_type", choices=list(build_mod.TITLES))
    p_report.add_argument("--date", default=None)
    p_report.add_argument("--cutoff", default=None)
    p_report.add_argument("--stdout", action="store_true")
    # event-type-only knobs (spec B13's documented flags cover the 7 other
    # types; `event` additionally needs to know *which* company and what to
    # call the file — spec B6's stem table requires a slug for it).
    p_report.add_argument("--subject", default=None)
    p_report.add_argument("--slug", default=None)


def dispatch(args, settings) -> int | None:
    if args.command != "report":
        return None
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    try:
        return _cmd_report(conn, settings, args)
    finally:
        conn.close()


def _parse_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(timezone.utc).astimezone(cutoff_mod.KST).date()


def _parse_cutoff(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cmd_report(conn, settings, args) -> int:
    report_date = _parse_date(args.date)
    # An explicit --cutoff always wins over the computed blackout (spec
    # B13) — this is the backfill/catchup seam ST3's jobs.py needs.
    cutoff = _parse_cutoff(args.cutoff) or cutoff_mod.cutoff_for(args.report_type, report_date)

    # **차단선이 아직 오지 않았으면 만들지 않는다.**
    #
    # 리포트 첫 줄은 "이 시각까지 알려진 사실만 싣습니다"라고 약속한다. 그
    # 시각이 미래면 지킬 수 없는 약속이고, 장이 닫히기도 전에 "장마감 델타"가
    # 발행된다(실측 2026-08-05: 차단선 16:15인 마감 리포트가 12:34에 만들어져
    # 세 번 발행됐다 — 아무것도 막지 않았다).
    #
    # 지난 날짜를 뒤늦게 만드는 캐치업은 그대로 통과한다(차단선이 과거다).
    # 같은 규율이 백필에도 있다 — `backfill/prices.py`는 아직 마감하지 않은
    # 세션을 건너뛴다.
    now = datetime.now(timezone.utc)
    if cutoff > now:
        local = cutoff.astimezone(cutoff_mod.KST)
        print(
            f"error: 차단선이 아직 오지 않았다 — {local:%Y-%m-%d %H:%M} KST "
            f"(지금 {now.astimezone(cutoff_mod.KST):%H:%M}). "
            f"그 시각 이후에 만들어야 리포트의 '이 시각까지 알려진 사실만' 약속이 참이 된다.",
            file=sys.stderr,
        )
        return 2

    report = build_mod.build_report(conn, args.report_type, report_date, cutoff, subject=args.subject)

    stem = build_mod.stem_for(report, slug=args.slug)
    out_path = PROJECT_ROOT / "reports" / args.report_type / f"{stem}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report.to_json(), encoding="utf-8")

    print(f"report_type={report.report_type} report_date={report.report_date} cutoff_kst={report.cutoff_kst}")
    print(
        f"data_status={DATA_STATUS_KO.get(report.data_status, report.data_status)} "
        f"facts={len(report.facts)} market_reaction={len(report.market_reaction)} "
        f"events={len(report.events)} schedule_changes={len(report.schedule_changes)} "
        f"missing={len(report.missing)}"
    )
    print(f"interpretation={'absent' if report.interpretation.is_empty() else 'present'}")
    print(f"json={out_path}")
    # `--stdout` appends the markdown *after* the B13 summary instead of
    # replacing it: ST3's jobs.py/site build parse `json=`/`data_status=`
    # from this output, and dropping them the moment --stdout is passed
    # breaks that contract silently (judge.md B13 항목).
    if args.stdout:
        print(render_md_mod.render_markdown(report), end="")
    return 0
