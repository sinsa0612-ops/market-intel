"""`thesis load|list|review` CLI (spec SA-11), wired into `cli.py` only
through the `CLI_EXTENSIONS` hook (spec B1) — `cli.py` is never edited by
this subtask; tests register this module the same way
`test_cli_schedule.py` does.

No TTY prompts anywhere. Output is the parseable `key=value` B13 format.
"""
from __future__ import annotations

import hashlib
import json

from .config import PROJECT_ROOT
from . import db as db_mod
from .interp import store as store_mod
from .interp import thesis as thesis_mod
from .reporting.model import Report

DEFAULT_THESES_FILE = str(PROJECT_ROOT / "theses" / "theses.json")

_VERDICTS = ["강화", "유지", "약화", "무효", "판정 불가"]


def register(sub) -> None:
    p_thesis = sub.add_parser("thesis")
    thesis_sub = p_thesis.add_subparsers(dest="thesis_command", required=True)

    p_load = thesis_sub.add_parser("load")
    p_load.add_argument("--file", default=None)
    p_load.add_argument("--check", action="store_true")

    thesis_sub.add_parser("list")

    p_review = thesis_sub.add_parser("review")
    p_review.add_argument("--file", required=True)
    p_review.add_argument("--dry-run", action="store_true")

    p_audit = thesis_sub.add_parser("audit")
    p_audit.add_argument("--json", action="store_true")


def dispatch(args, settings) -> int | None:
    if args.command != "thesis":
        return None

    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    try:
        if args.thesis_command == "load":
            return _cmd_load(conn, args)
        if args.thesis_command == "list":
            return _cmd_list(conn)
        if args.thesis_command == "review":
            return _cmd_review(conn, args)
        if args.thesis_command == "audit":
            return _cmd_audit(conn, args)
        return 1
    finally:
        conn.close()


def _cmd_load(conn, args) -> int:
    file_path = args.file or DEFAULT_THESES_FILE
    try:
        theses = thesis_mod.load_file(file_path)
    except thesis_mod.ThesisLoadError as exc:
        for reason in exc.reasons:
            print(f"thesis_load_error={reason}")
        return 2

    with open(file_path, encoding="utf-8") as f:
        raw_text = f.read()
    theme_count = len(json.loads(raw_text).get("themes", {}))
    source_sha256 = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    if not args.check:
        store_mod.replace_theses(conn, theses, source_sha256)

    print(f"theses_loaded={len(theses)} themes={theme_count} slots_used={len(theses)} rejected=0")
    return 0


def _cmd_list(conn) -> int:
    theses = store_mod.list_theses(conn)
    print(f"theses_total={len(theses)}")
    for t in theses:
        label = thesis_mod.THEME_LABELS.get(t["theme"], t["theme"])
        last = store_mod.last_verdict(conn, t["thesis_id"]) or "(없음)"
        print(
            f"[{label} #{t['slot']}] {t['thesis_id']} next_check={t['next_check_date']} last_verdict={last}"
        )
    return 0


def _cmd_audit(conn, args) -> int:
    """`thesis audit` — 조건 하나하나가 **실제로 발화할 수 있는가**를 묻는다.

    왜 이 명령이 있는가: 2026-08-12에 반증 조건 하나가 사실상 발화 불가능한
    상태로 발견됐는데, 그것을 찾은 것은 검사 장치가 아니라 우연이었다. 우연이
    QA를 대신하는 한 나머지 조건이 살아 있는지는 아무도 모른다.

    종료코드: `unreachable`(어떤 데이터로도 참이 될 수 없음)이 하나라도 있으면
    2 — 그건 버그다. `never_fired`만 있으면 0이다. 반증 조건이 안 울리는 것은
    가설이 맞으면 정상이라, 실패로 처리하면 매번 빨간불이 되어 아무도 안 본다.
    대신 문턱 근접도를 함께 찍어 사람이 판단하게 한다.
    """
    from datetime import datetime, timezone

    theses = store_mod.list_theses(conn)
    if not theses:
        print("thesis_audit: 적재된 가설이 없습니다")
        return 0
    rows = thesis_mod.audit_conditions(conn, theses, datetime.now(timezone.utc))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            near = "" if r["closest"] is None else f" closest={r['closest'] * 100:.0f}%"
            fired = (f" fired_days={r['fired_days']} last_fired={r['last_fired']}"
                     if r["fired_days"] else "")
            print(f"audit={r['verdict']} thesis_id={r['thesis_id']} group={r['group']} "
                  f"atom={r['atom_id']} kind={r['kind']} obs={r['observations']}{fired}{near}")

    counts = {"ok": 0, "never_fired": 0, "unreachable": 0}
    for r in rows:
        counts[r["verdict"]] += 1
    print(f"thesis_audit: 조건={len(rows)} 발화이력있음={counts['ok']} "
          f"발화이력없음={counts['never_fired']} 발화불가={counts['unreachable']}")
    return 2 if counts["unreachable"] else 0


def _cmd_review(conn, args) -> int:
    with open(args.file, encoding="utf-8") as f:
        report = Report.from_json(f.read())

    theses = store_mod.list_theses(conn)
    results = thesis_mod.review(conn, theses, report.cutoff_utc, report.report_type, report.report_date)

    if not args.dry_run:
        store_mod.record_reviews(conn, results)

    for r in results:
        print(
            f"verdict={r['verdict']} thesis_id={r['thesis_id']} theme={r['theme']} slot={r['slot']} "
            f"changed={r['changed']} prev_verdict={r['prev_verdict'] or '(없음)'}"
        )

    counts = {v: 0 for v in _VERDICTS}
    for r in results:
        counts[r["verdict"]] += 1
    changed_total = sum(r["changed"] for r in results)
    print(
        f"thesis: 강화={counts['강화']} 유지={counts['유지']} 약화={counts['약화']} "
        f"무효={counts['무효']} 판정불가={counts['판정 불가']} 변화={changed_total}"
    )
    return 0
