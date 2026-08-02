"""`backfill` CLI (spec 백필 §4 S8), wired into `cli.py` through the
`CLI_EXTENSIONS` hook (spec B1) — same shape as `cli_ops.py`/`cli_report.py`.

무인 실행: 프롬프트 없음. 종료코드 `0`=OK/NO_DATA, `1`=PARTIAL·ERROR,
`2`=인자 오류(`cli.py:82`의 기존 관례와 동일).

**재개 방식 = 그냥 다시 실행한다.** `ledger.append_vintage`의 멱등성 키가 이미
들어간 행을 전부 건너뛴다. 별도 체크포인트 파일이나 상태 테이블은 두지 않는다
(spec S8 — 전체 실행이 60초 미만이고 소스별 HTTP 호출이 1~16회뿐이다).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone

from . import db as db_mod
from .backfill import SOURCE_ORDER, run_source


def register(sub) -> None:
    p = sub.add_parser("backfill")
    p.add_argument("--source", required=True, choices=[*SOURCE_ORDER, "all"])
    p.add_argument("--since", default=None, help="YYYY-MM-DD (기본: 소스별)")
    p.add_argument("--until", default=None, help="YYYY-MM-DD (기본: 오늘)")
    p.add_argument("--subjects", default=None, help="SYM[,SYM,...]")
    p.add_argument("--dry-run", action="store_true", dest="dry_run")
    p.add_argument("--json", action="store_true", dest="as_json")


def dispatch(args, settings) -> int | None:
    if args.command != "backfill":
        return None
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    try:
        return _cmd_backfill(conn, settings, args)
    finally:
        conn.close()


def _minus_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 2월 29일
        return day.replace(year=day.year - years, day=28)


def _default_since(source: str, today: date) -> date:
    """spec S8: prices=오늘−2년, macro_*=오늘−3년, financials=오늘−730일."""
    if source == "prices":
        return _minus_years(today, 2)
    if source in ("macro_us", "macro_kr"):
        return _minus_years(today, 3)
    return today - timedelta(days=730)


def _cmd_backfill(conn, settings, args) -> int:
    today = datetime.now(timezone.utc).date()
    try:
        until = date.fromisoformat(args.until) if args.until else today
        since_override = date.fromisoformat(args.since) if args.since else None
    except ValueError as exc:
        # 잘못된 날짜 하나에 raw traceback을 뱉지 않는다(`cli.py`의 선례).
        print(f"error: invalid date argument: {exc}", file=sys.stderr)
        return 2
    if since_override and since_override > until:
        print(f"error: --since {since_override} is after --until {until}", file=sys.stderr)
        return 2

    subjects = None
    if args.subjects:
        subjects = [s.strip() for s in args.subjects.split(",") if s.strip()]
        if not subjects:
            print("error: --subjects is empty", file=sys.stderr)
            return 2

    sources = list(SOURCE_ORDER) if args.source == "all" else [args.source]
    results = [
        run_source(
            conn, settings, source,
            since=since_override or _default_since(source, today),
            until=until, subjects=subjects, dry_run=args.dry_run,
        )
        for source in sources
    ]

    if args.as_json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(result.line())

    # 부분 실패가 조용히 성공으로 보고되면 안 된다(spec R8).
    return 1 if any(r.status in ("PARTIAL", "ERROR") for r in results) else 0
