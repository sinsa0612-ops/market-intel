"""`site build` / `obsidian sync` / `job run` CLI (spec B13), wired into
`cli.py` only through the `CLI_EXTENSIONS` hook (spec B1) — this module
never touches `cli.py`, and `cli.py` never imports it by name.

Output is the parseable B13 format: one `key=value` line set per command,
no TTY input anywhere, and exit code 0 unless a safety gate fired.
"""
from __future__ import annotations

from datetime import date

from . import db as db_mod
from . import jobs as jobs_mod
from . import obsidian as obsidian_mod
from . import site as site_mod


def register(sub) -> None:
    p_site = sub.add_parser("site")
    site_sub = p_site.add_subparsers(dest="site_command", required=True)
    site_sub.add_parser("build")

    p_obsidian = sub.add_parser("obsidian")
    obsidian_sub = p_obsidian.add_subparsers(dest="obsidian_command", required=True)
    p_sync = obsidian_sub.add_parser("sync")
    p_sync.add_argument("--since", default=None)

    p_job = sub.add_parser("job")
    job_sub = p_job.add_subparsers(dest="job_command", required=True)
    p_run = job_sub.add_parser("run")
    p_run.add_argument("--name", required=True, choices=sorted(jobs_mod.JOBS))
    p_run.add_argument("--no-publish", action="store_true")


def dispatch(args, settings) -> int | None:
    if args.command == "site" and args.site_command == "build":
        return _cmd_site_build(settings)
    if args.command == "obsidian" and args.obsidian_command == "sync":
        return _cmd_obsidian_sync(args)
    if args.command == "job" and args.job_command == "run":
        return _cmd_job_run(settings, args)
    return None


def _cmd_site_build(settings) -> int:
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    try:
        result = site_mod.build_site(conn)
    finally:
        conn.close()
    print(
        f"site_pages={result['pages']} reports_indexed={result['reports_indexed']} "
        f"latest={result['latest']} out={result['out']}"
    )
    return 0


def _cmd_obsidian_sync(args) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    result = obsidian_mod.sync(since=since)
    print(f"obsidian_written={result['written']} vault={result['vault']}")
    if result["failed"]:
        # Never silent: a vault that could not be written is stated, but the
        # exit code stays 0 (spec ST3 What #2 — 실패해도 종료코드 0).
        print(f"obsidian_failed={len(result['failed'])}")
    return 0


def _cmd_job_run(settings, args) -> int:
    result = jobs_mod.run_job(settings, args.name, publish=not args.no_publish)
    steps = result["steps"]
    print(f"job={result['job']} lock={result['lock']}")
    print(f"catchup_generated={result['catchup_generated']}")
    print(
        "steps: "
        # 2단계-B ST3: `interpret`가 report와 site 사이에 들어간다(spec ST3
        # What #2의 출력 순서). B13의 원래 5단계 줄을 그대로 두면 해석이 며칠째
        # 실패해도 `job run` 출력에는 아무 흔적이 없다.
        + " ".join(f"{k}={steps[k]}" for k in
                   ("collect", "report", "interpret", "site", "obsidian", "publish"))
    )
    print(f"exit={result['exit']}")
    return result["exit"]
