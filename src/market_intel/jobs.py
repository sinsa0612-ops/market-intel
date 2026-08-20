"""Unattended job runner (spec B10) — locking, catch-up, pipeline.

**Timetable (orchestrator decision 1, which overrides B10's 6-job list).**
The ST2 judge reproduced the defect: the `morning` blackout is 07:15 but
the only collect ran at/after 07:20, so every morning report came out
structurally empty. The blackout is a fixed spec value and stays fixed;
what moves is collection. Collect jobs therefore run *before* the
blackout they feed, and are separate launchd entries from the report
jobs that consume them:

    06:50 Mon–Fri  collect-am    morning + calendar + events
    07:40 Mon      weekly        report weekly_review (blackout 07:15)
    07:40 Tue–Fri  morning       report morning       (blackout 07:15)
    15:50 Mon–Fri  collect-pm    close
    16:15 Mon–Fri  close         report close_delta   (blackout 16:15)
    08:00 Sat/1st  collect-full  all
    08:30 1st      monthly       report monthly
    13:00 & 22:00  eventwatch    events (공시·실적 감시)

`weekly_review` was two reports until 2026-08-20: a Saturday retrospective
plus a Monday `week_start` briefing. The CEO merged them into the Monday
slot — one report that looks back at last week and forward at this one.
The merge also fixes a mismatch: `week_start` was a *daily*-family report
(lookback=1, 전일대비), so a report titled "주간 시작" compared against
Friday. As `weekly_review` it gets the 5-거래일 window its title claims.
Saturday now runs `collect-full` only, which is what `monthly` needs.

Two consequences worth stating plainly:
  * a report job does **not** collect (collecting at 07:40 would only write
    facts its own 07:15 blackout must ignore), so its `collect=` step reads
    `skip` — B13's `<ok|fail>` gains a third, honest value;
  * `close`'s blackout equals its own run time, so `collect-pm` at 15:50 is
    what makes it non-empty.

**Locking** is `fcntl.flock(LOCK_EX|LOCK_NB)` on `var/locks/<job>.lock`
(spec B10) — macOS ships no `flock(1)`, so a shell lock would die with
`command not found` and every overlapping run would proceed silently.

**Catch-up** (spec B10): a job first re-checks its own last 7 days of
slots and regenerates any whose JSON is missing — using *that day's*
blackout, never now. Hindsight is the thing being prevented, so the
backfilled report is stamped `meta.late_generation=true` and the site
badges it 지연 생성 instead of passing it off as timely.
"""
from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db as db_mod
from . import obsidian as obsidian_mod
from . import site as site_mod
from .config import PROJECT_ROOT
from .interp import ops as ops_mod
from .interp import store as store_mod
from .reporting.build import build_report, stem_for
from .reporting.cutoff import KST, cutoff_for

CATCHUP_DAYS = 7  # spec B10 — 최근 7일치 자기 슬롯을 점검

# Python weekday(): Mon=0 … Sun=6.
_WEEKDAYS = "월화수목금토일"
MON_FRI = {0, 1, 2, 3, 4}

# collect: workflows to run · report: report type or None · the rest is the
# calendar this job occupies, used by `slot_dates` for catch-up.
JOBS: dict[str, dict] = {
    "collect-am": {"collect": ["morning", "calendar", "events"], "report": None,
                   "weekdays": MON_FRI},
    "morning": {"collect": [], "report": "morning", "weekdays": {1, 2, 3, 4}},
    "collect-pm": {"collect": ["close"], "report": None, "weekdays": MON_FRI},
    "close": {"collect": [], "report": "close_delta", "weekdays": MON_FRI},
    "collect-full": {"collect": ["all"], "report": None, "weekdays": {5}, "monthday": 1},
    "weekly": {"collect": [], "report": "weekly_review", "weekdays": {0}},
    "monthly": {"collect": [], "report": "monthly", "monthday": 1},
    "eventwatch": {"collect": ["events"], "report": None},
}

PUBLISH_SCRIPT = "scripts/publish.sh"
# publish.sh exit codes that mean "a safety gate fired", as opposed to
# "the network was down". Only the former makes the job itself fail.
#
# 5(미결재 소스가 원격보다 앞서 푸시 거부)가 빠져 있어서, 가드가 매 실행마다
# 푸시를 거부하는 동안에도 job은 초록이었다(final-review.md F6). 이 상태는
# 오프라인과 성질이 다르다 — 사람이 결재해 밀기 전까지 **스스로 풀리지 않고**,
# 그동안 공개 사이트는 조용히 갱신을 멈춘다. 알림 없이 며칠이 지나는 것이
# 이 시스템에서 가장 조용한 실패라 5를 실패로 올린다.
PUBLISH_BLOCKED_EXIT = 5
PUBLISH_HARD_FAILURES = {3, 4, PUBLISH_BLOCKED_EXIT}
# `job_runs.note`에 남기는 사유. 운영 상태 페이지(`docs/status.html`)의 사유
# 칸이 이 문자열을 그대로 싣는다(`interp/ops.py:status` -> `site._status_page`).
PUBLISH_BLOCKED_NOTE = (
    "publish_blocked=미결재 소스가 원격보다 앞서 있어 푸시를 거부했다 — "
    "사람이 그 커밋을 결재해 밀기 전까지 사이트는 갱신되지 않는다"
)


# --- lock -----------------------------------------------------------------

def lock_path(settings, name: str) -> Path:
    """`var/locks/<job>.lock`, derived from the DB location so a test (or a
    second checkout) with its own `MI_DB_PATH` gets its own locks instead of
    contending with the live installation."""
    return Path(settings.db_path).resolve().parent / "locks" / f"{name}.lock"


@contextmanager
def job_lock(settings, name: str):
    """Yields True when the lock was taken, False when another run holds it."""
    path = lock_path(settings, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# --- slots ----------------------------------------------------------------

def slot_dates(name: str, today: date, days: int = CATCHUP_DAYS) -> list[date]:
    """The dates in the last `days` (inclusive of `today`) on which this job
    was scheduled to run. A job that runs Tue–Fri must not backfill a Monday
    report the timetable never asked for."""
    spec = JOBS[name]
    weekdays = spec.get("weekdays")
    monthday = spec.get("monthday")
    out = []
    for back in range(days - 1, -1, -1):
        d = today - timedelta(days=back)
        if weekdays is None and monthday is None:
            out.append(d)
        elif weekdays is not None and d.weekday() in weekdays:
            out.append(d)
        elif monthday is not None and d.day == monthday:
            out.append(d)
    return out


# --- report generation ----------------------------------------------------

def report_path(reports_root: Path, report_type: str, report_date: date,
                stem: str | None = None) -> Path:
    if stem is None:
        stem = _stem_without_building(report_type, report_date)
    return reports_root / report_type / f"{stem}.json"


def _stem_without_building(report_type: str, report_date: date) -> str:
    """`stem_for` needs a built Report; catch-up needs to know the filename
    *before* deciding whether to spend a build. Same B6 table, keyed off the
    date alone — which is all the 5 scheduled types need."""
    d = report_date.isoformat()
    if report_type == "monthly":
        return d[:7]
    return d


def generate_report(conn, report_type: str, report_date: date, reports_root: Path,
                    late: bool = False) -> Path:
    cutoff = cutoff_for(report_type, report_date)
    report = build_report(conn, report_type, report_date, cutoff)
    report.meta["late_generation"] = late
    path = reports_root / report_type / f"{stem_for(report)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json(), encoding="utf-8")
    return path


# --- steps ----------------------------------------------------------------

def _default_collect(settings, workflow: str) -> bool:
    from .engine import run_collect
    from .http_client import configure_logging
    from .providers import PROVIDERS
    from .universe import UNIVERSE

    run_collect(settings, UNIVERSE, PROVIDERS, workflow, None,
                logger=configure_logging(settings))
    return True


def _default_interpret(settings, conn, path: Path) -> dict:
    """해석 단계의 기본 구현. `settings`는 쓰지 않지만 `collect` 훅과 같은
    자리를 지켜 테스트가 같은 방식으로 가짜를 주입할 수 있게 둔다(모델·호스트는
    `interp/llm.py`가 `MI_LLM_*`에서 직접 읽는다)."""
    return ops_mod.interpret_report(conn, path)


def _select_for_interpretation(paths: list[Path], budget: int) -> tuple[list[Path], int]:
    """예산을 넘으면 **오래된 것부터** 버린다(spec ST3 What #2). `paths`는
    생성 순서(캐치업 과거분 → 오늘)이므로 뒤에서 자르면 오늘 리포트는 절대
    빠지지 않는다 — 오늘 것을 건너뛰면 그날 사이트에 해석이 통째로 없어진다."""
    if len(paths) <= budget:
        return paths, 0
    return paths[-budget:], len(paths) - budget


def _interpret_step(settings, conn, paths: list[Path], interpret) -> tuple[str, int]:
    """-> (step status, 예산 때문에 건너뛴 건수).

    해석 실패는 job을 죽이지 않는다(spec SA-9): 리포트 파일은 이미 디스크에
    있고, 사이트는 빈 해석 칸을 `AI 해석 미생성`으로 찍는다."""
    if not paths:
        return "skip", 0
    targets, skipped = _select_for_interpretation(paths, ops_mod.interp_budget())
    ok = True
    for path in targets:
        try:
            result = interpret(settings, conn, path) or {}
        except Exception as exc:  # ollama가 죽어도 다음 단계로 간다
            print(f"  interpret({path.name}) failed: {type(exc).__name__}: {exc}")
            ok = False
            continue
        status = result.get("status")
        if status not in ops_mod.INTERPRET_OK_STATUSES:
            # 성공은 조용히 — B13의 `job run` 출력 블록은 4줄 계약이다.
            print(f"  interpret({path.name}) status={status}")
            ok = False
    return ("ok" if ok else "fail"), skipped


def _job_status(steps: dict) -> str:
    """spec ST3 What #2 — `report=fail`이면 `fail`, 그 외 `fail`이 하나라도
    있으면 `partial`, 없으면 `ok`."""
    if steps.get("report") == "fail":
        return "fail"
    if any(v == "fail" for v in steps.values()):
        return "partial"
    return "ok"


def _run_publish(repo_root: Path) -> tuple[str, int]:
    script = repo_root / PUBLISH_SCRIPT
    if not script.exists():
        return "skip", 0
    proc = subprocess.run(["bash", str(script)], cwd=repo_root,
                          capture_output=True, text=True, check=False)
    for line in (proc.stdout + proc.stderr).splitlines():
        print(f"  publish| {line}")
    if proc.returncode == 0:
        return "ok", 0
    if proc.returncode in PUBLISH_HARD_FAILURES:
        return "fail", proc.returncode
    # Offline / no credentials: spec B11-5 — log it and keep the job green,
    # the next run retries by itself.
    return "fail", 0


def run_job(settings, name: str, *, publish: bool = True, now: datetime | None = None,
            reports_root: Path | None = None, docs_root: Path | None = None,
            vault_root: Path | None = None, collect=None, interpret=None,
            repo_root: Path | None = None) -> dict:
    """Run one job end to end. Never raises for a dead source: each step is
    isolated and a failure is recorded, not propagated (spec ST3 What #3)."""
    if name not in JOBS:
        raise ValueError(f"run_job: unknown job {name!r}")
    spec = JOBS[name]
    reports_root = Path(reports_root) if reports_root else PROJECT_ROOT / "reports"
    docs_root = Path(docs_root) if docs_root else PROJECT_ROOT / "docs"
    repo_root = Path(repo_root) if repo_root else PROJECT_ROOT
    collect = collect or _default_collect
    interpret = interpret or _default_interpret
    now = now or datetime.now(KST)
    today = now.astimezone(KST).date()

    steps = {k: "skip" for k in ("collect", "report", "interpret", "site", "obsidian", "publish")}
    result = {"job": name, "lock": "already_running", "catchup_generated": 0,
              "steps": steps, "exit": 0}

    with job_lock(settings, name) as acquired:
        if not acquired:
            return result
        result["lock"] = "acquired"

        if spec["collect"]:
            ok = True
            for workflow in spec["collect"]:
                try:
                    collect(settings, workflow)
                except Exception as exc:  # a dead provider must not kill the job
                    print(f"  collect({workflow}) failed: {type(exc).__name__}")
                    ok = False
            steps["collect"] = "ok" if ok else "fail"

        db_mod.init_db(settings.db_path)
        conn = db_mod.connect(settings.db_path)
        job_run_id = store_mod.start_job_run(conn, name)
        notes: list[str] = []
        try:
            # 이번 실행이 만든 리포트들 — 캐치업 과거분이 앞, 오늘이 마지막.
            # 해석 단계가 예산을 넘길 때 어느 것을 버릴지 이 순서로 정한다.
            generated: list[Path] = []
            if spec["report"]:
                try:
                    rtype = spec["report"]
                    for d in slot_dates(name, today):
                        if d == today:
                            continue
                        if report_path(reports_root, rtype, d).exists():
                            continue
                        generated.append(generate_report(conn, rtype, d, reports_root, late=True))
                        result["catchup_generated"] += 1
                    generated.append(generate_report(conn, rtype, today, reports_root, late=False))
                    steps["report"] = "ok"
                except Exception as exc:
                    print(f"  report failed: {type(exc).__name__}: {exc}")
                    steps["report"] = "fail"

            # report와 site 사이 — 사이트가 해석이 채워진 JSON을 읽도록.
            steps["interpret"], skipped = _interpret_step(settings, conn, generated, interpret)
            if skipped:
                notes.append(f"interp_skipped={skipped}")

            try:
                site_mod.build_site(conn, reports_root=reports_root, docs_root=docs_root, now=now)
                steps["site"] = "ok"
            except Exception as exc:
                print(f"  site failed: {type(exc).__name__}: {exc}")
                steps["site"] = "fail"
        finally:
            conn.close()

        try:
            obsidian_mod.sync(reports_root=reports_root, vault_root=vault_root)
            steps["obsidian"] = "ok"
        except Exception as exc:
            print(f"  obsidian failed: {type(exc).__name__}: {exc}")
            steps["obsidian"] = "fail"

        if publish:
            steps["publish"], result["exit"] = _run_publish(repo_root)
            if result["exit"] == PUBLISH_BLOCKED_EXIT:
                notes.append(PUBLISH_BLOCKED_NOTE)

        # 마지막에 한 번 더 여는 이유: publish는 외부 프로세스(git)를 돌리므로
        # 그 동안 DB 커넥션을 붙들고 있지 않는다(위 블록이 이미 그 형태였다).
        conn = db_mod.connect(settings.db_path)
        try:
            store_mod.finish_job_run(
                conn, job_run_id, steps, _job_status(steps),
                catchup=bool(result["catchup_generated"]), note=" ".join(notes),
            )
        finally:
            conn.close()

    return result
