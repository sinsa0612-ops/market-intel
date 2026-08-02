"""`ops status` CLI (spec SA-11/SA-12), wired into `cli.py` through the
`CLI_EXTENSIONS` hook (spec B1) — same shape as `cli_report.py`.

터미널에서 파이프라인이 살아 있는지 한 번에 보는 명령이다. 사이트의
`docs/status.html`과 **같은 `ops.status()` dict**를 읽으므로 둘이 다른 말을
할 수 없다. 출력은 B13의 `key=value` 형식, TTY 입력 없음.
"""
from __future__ import annotations

import json

from . import db as db_mod
from .interp import ops as ops_mod


def register(sub) -> None:
    p_ops = sub.add_parser("ops")
    ops_sub = p_ops.add_subparsers(dest="ops_command", required=True)
    p_status = ops_sub.add_parser("status")
    p_status.add_argument("--json", action="store_true", dest="as_json")


def dispatch(args, settings) -> int | None:
    if args.command != "ops":
        return None
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    try:
        if args.ops_command == "status":
            return _cmd_status(conn, args)
        return 1
    finally:
        conn.close()


def _cmd_status(conn, args) -> int:
    state = ops_mod.status(conn)
    if args.as_json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    print(f"generated_at={state['generated_at']} healthy={'yes' if state['healthy'] else 'no'} "
          f"alerts={len(state['alerts'])}")
    for job in state["jobs"]:
        print(
            f"{job['job']}: state={job['state']} status={job['status'] or '(없음)'} "
            f"last_run={job['last_started'] or '(없음)'} overdue={job['overdue']} "
            f"steps=[{job['steps_text']}]"
        )
    interp = state["interpretation"]
    if interp:
        print(f"last_interpretation={interp['status']} model={interp.get('model') or '(없음)'} "
              f"report={interp['report_type']}/{interp['report_date']} at={interp['created_at']}")
    else:
        print("last_interpretation=(없음)")

    collect = state["collect"]
    if collect:
        provs = " ".join(f"{p['provider']}={p['status']}({p['reason_code']})" for p in collect["providers"])
        print(f"last_collect: workflow={collect['workflow']} started={collect['started_at']} providers: {provs}")
    else:
        print("last_collect=(없음)")

    print(f"open_gaps={len(state['gaps'])}: " + " ".join(g["gap_id"] for g in state["gaps"]))
    for alert in state["alerts"]:
        print(f"alert: {alert}")
    return 0
