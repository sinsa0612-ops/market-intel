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
