"""ST3 acceptance tests — job runner: lock, catch-up, pipeline (spec B10)."""
from __future__ import annotations

import fcntl
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from market_intel import jobs as jobs_mod
from market_intel.reporting.cutoff import KST

from tests.publish.conftest import make_report, write_report


@pytest.fixture
def job_env(tmp_path, settings):
    """settings comes from the repo-root conftest (tmp-path db/raw/logs)."""
    from market_intel import db as db_mod

    db_mod.init_db(settings.db_path)
    return {
        "settings": settings,
        "reports_root": tmp_path / "reports",
        "docs_root": tmp_path / "docs",
        "vault_root": tmp_path / "vault",
    }


def run(job_env, name, **kw):
    calls = kw.pop("calls", [])

    def fake_collect(settings, workflow):
        calls.append(workflow)
        return True

    kw.setdefault("collect", fake_collect)
    return jobs_mod.run_job(
        job_env["settings"], name,
        reports_root=job_env["reports_root"], docs_root=job_env["docs_root"],
        vault_root=job_env["vault_root"], publish=False, **kw,
    )


# --- lock -----------------------------------------------------------------

def test_job_lock(job_env):
    """spec ST3 `test_job_lock` — the second concurrent run reports
    `already_running` and exits 0 (a skipped duplicate is not a failure)."""
    lock_path = jobs_mod.lock_path(job_env["settings"], "morning")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST))
        assert result["lock"] == "already_running"
        assert result["exit"] == 0
        assert all(v == "skip" for v in result["steps"].values()), result["steps"]
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_lock_released_after_run(job_env):
    run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST))
    result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 41, tzinfo=KST))
    assert result["lock"] == "acquired"


def test_no_shell_flock_used():
    """spec §Environment gotchas — macOS has no `flock(1)`; the lock must be
    Python's `fcntl.flock`, never a shelled-out binary."""
    src = (Path(jobs_mod.__file__)).read_text(encoding="utf-8")
    assert "fcntl.flock" in src
    assert '"flock"' not in src and "'flock'" not in src
    assert '"timeout"' not in src and "'timeout'" not in src


# --- catch-up -------------------------------------------------------------

def test_slot_dates_respects_job_weekdays(job_env):
    """`morning` runs Tue–Fri, `weekly` only Mon — the catch-up must not
    invent a Monday morning report the schedule never asked for.

    `weekly` was the Saturday slot until 2026-08-20; it moved to Monday when
    it absorbed `week_start`. Its catch-up must move with it, or every run
    would look for a Saturday `weekly_review` that is no longer scheduled."""
    today = date(2026, 8, 7)
    morning = jobs_mod.slot_dates("morning", today)
    assert all(d.weekday() in (1, 2, 3, 4) for d in morning), morning
    weekly = jobs_mod.slot_dates("weekly", today)
    assert all(d.weekday() == 0 for d in weekly), weekly
    assert weekly, "월요일 슬롯이 최근 7일 안에 하나는 있어야 한다"
    monthly = jobs_mod.slot_dates("monthly", date(2026, 8, 3))
    assert monthly == [date(2026, 8, 1)], monthly


def test_catchup_backfills(job_env):
    """spec ST3 `test_catchup_backfills` — a slot whose JSON is missing is
    regenerated **with that day's blackout**, and is labelled late."""
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    slots = jobs_mod.slot_dates("morning", now.date())
    past = [d for d in slots if d != now.date()]
    assert len(past) >= 2, past
    missing = past[0]
    for d in past[1:]:
        write_report(job_env["reports_root"], make_report("morning", d.isoformat()))

    result = run(job_env, "morning", now=now)
    assert result["catchup_generated"] == 1, result
    assert result["exit"] == 0

    path = job_env["reports_root"] / "morning" / f"{missing.isoformat()}.json"
    assert path.exists(), f"{missing} was not backfilled"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["meta"]["late_generation"] is True
    assert data["cutoff_kst"] == f"{missing.isoformat()}T07:15:00+09:00", data["cutoff_kst"]


def test_catchup_never_uses_today_cutoff(job_env):
    """The hindsight ban (spec B10): a backfilled report must not be able to
    see anything published after its own historical blackout."""
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    run(job_env, "morning", now=now)
    for path in (job_env["reports_root"] / "morning").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["cutoff_kst"].startswith(data["report_date"]), (
            f"{path.name}: cutoff {data['cutoff_kst']} is not that report_date's blackout")


def test_catchup_is_noop_when_present(job_env):
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    for d in jobs_mod.slot_dates("morning", now.date()):
        write_report(job_env["reports_root"], make_report("morning", d.isoformat()))
    result = run(job_env, "morning", now=now)
    assert result["catchup_generated"] == 0


# --- pipeline -------------------------------------------------------------

def test_report_job_does_not_collect(job_env):
    """Orchestrator decision 1 — a report job only reports. Collecting at
    07:40 would write facts its own 07:15 blackout is obliged to ignore;
    `collect-am` at 06:50 is what feeds it."""
    calls: list[str] = []
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    result = run(job_env, "morning", now=now, calls=calls)
    assert calls == [], calls
    assert result["steps"]["collect"] == "skip"
    assert result["steps"]["report"] == "ok"
    assert result["steps"]["site"] == "ok"
    assert result["steps"]["obsidian"] == "ok"
    assert result["steps"]["publish"] == "skip"  # publish=False
    assert result["exit"] == 0
    assert (job_env["docs_root"] / "index.html").exists()
    assert (job_env["vault_root"] / "2026" / "2026-08-07-morning.md").exists()


def test_collect_job_skips_report_step(job_env):
    calls: list[str] = []
    result = run(job_env, "collect-am", now=datetime(2026, 8, 7, 6, 50, tzinfo=KST), calls=calls)
    assert result["steps"]["collect"] == "ok"
    assert result["steps"]["report"] == "skip"
    assert result["exit"] == 0
    assert calls == ["morning", "calendar", "events"], calls


def test_step_failure_does_not_block_the_rest(job_env):
    """spec ST3 What #3 — 각 단계 실패는 다음 단계를 막지 않는다."""
    def boom(settings, workflow):
        raise RuntimeError("provider exploded")

    result = run(job_env, "collect-am", now=datetime(2026, 8, 7, 6, 50, tzinfo=KST), collect=boom)
    assert result["steps"]["collect"] == "fail"
    assert result["steps"]["site"] == "ok"
    assert result["steps"]["obsidian"] == "ok"
    assert result["exit"] == 0, "a dead source must not fail the job (spec B13)"


def test_every_job_name_has_a_launchd_template():
    templates = {p.name.split(".plist")[0].rsplit(".", 1)[-1]
                 for p in (Path(jobs_mod.__file__).resolve().parents[2]
                           / "launchd").glob("*.plist.template")}
    assert set(jobs_mod.JOBS) == templates, (set(jobs_mod.JOBS) ^ templates)


# --- interpret step + job_runs (2단계-B ST3) --------------------------------

def _recorded(job_env) -> list[dict]:
    from market_intel import db as db_mod
    from market_intel.interp import store as store_mod

    conn = db_mod.connect(job_env["settings"].db_path)
    try:
        return store_mod.last_job_runs(conn)
    finally:
        conn.close()


def _fake_interpret(paths: list[Path], status: str = "ok"):
    def interpret(settings, conn, path):
        paths.append(Path(path))
        return {"status": status}
    return interpret


def test_steps_include_interpret_between_report_and_site(job_env):
    """spec ST3 What #2 — 출력 순서 collect report interpret site obsidian publish."""
    seen: list[Path] = []
    result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST),
                 interpret=_fake_interpret(seen))
    assert list(result["steps"]) == ["collect", "report", "interpret", "site", "obsidian", "publish"]
    assert result["steps"]["interpret"] == "ok"
    assert seen, "the interpret step never ran"


def test_collect_only_job_skips_interpret(job_env):
    result = run(job_env, "collect-am", now=datetime(2026, 8, 7, 6, 50, tzinfo=KST),
                 interpret=_fake_interpret([]))
    assert result["steps"]["interpret"] == "skip"


def test_job_run_row_is_recorded_with_ok_status(job_env):
    run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST),
        interpret=_fake_interpret([]))
    rows = _recorded(job_env)
    assert [r["job"] for r in rows] == ["morning"]
    row = rows[0]
    assert row["status"] == "ok", row
    assert row["finished_at"], "finish_job_run never ran"
    assert row["steps"]["interpret"] == "ok"


def test_job_run_status_is_fail_when_the_report_step_fails(job_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("build exploded")

    monkeypatch.setattr(jobs_mod, "generate_report", boom)
    result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST),
                 interpret=_fake_interpret([]))
    assert result["steps"]["report"] == "fail"
    assert _recorded(job_env)[0]["status"] == "fail"


def test_job_run_status_is_partial_when_only_interpret_fails(job_env):
    def boom(settings, conn, path):
        raise RuntimeError("ollama is down")

    result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST), interpret=boom)
    assert result["steps"]["interpret"] == "fail"
    assert _recorded(job_env)[0]["status"] == "partial"


def test_interpretation_failure_never_blocks_report_or_site(job_env):
    """spec SA-9 — 해석 생성 실패는 리포트 실패가 아니다. 리포트 파일과 사이트는
    어떤 경우에도 정상 발행되고 exit 0이다."""
    def boom(settings, conn, path):
        raise RuntimeError("ollama is down")

    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)
    result = run(job_env, "morning", now=now, interpret=boom)
    assert result["steps"]["report"] == "ok"
    assert result["steps"]["site"] == "ok"
    assert result["steps"]["obsidian"] == "ok"
    assert result["exit"] == 0
    assert (job_env["reports_root"] / "morning" / "2026-08-07.json").exists()
    assert (job_env["docs_root"] / "index.html").exists()


def test_transport_failure_status_marks_the_step_failed(job_env):
    """An `llm_unavailable` result is a failed step even though no exception
    was raised — spec ST3 성공기준: `interpret=fail site=ok … exit=0`."""
    result = run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST),
                 interpret=_fake_interpret([], status="llm_unavailable"))
    assert result["steps"]["interpret"] == "fail"
    assert result["steps"]["site"] == "ok"
    assert result["exit"] == 0


def test_interpret_budget_skips_the_oldest_but_never_today(job_env, monkeypatch):
    """spec ST3 What #2 — `MI_INTERP_MAX_PER_RUN`을 넘으면 오래된 것부터
    건너뛰고 건너뛴 수를 note에 남긴다. 오늘 리포트는 절대 건너뛰지 않는다."""
    monkeypatch.setenv("MI_INTERP_MAX_PER_RUN", "3")
    now = datetime(2026, 8, 7, 7, 40, tzinfo=KST)  # Fri: catch-up 08-04/05/06 + today
    seen: list[Path] = []
    result = run(job_env, "morning", now=now, interpret=_fake_interpret(seen))

    assert result["catchup_generated"] == 3
    stems = sorted(p.stem for p in seen)
    assert stems == ["2026-08-05", "2026-08-06", "2026-08-07"], stems
    assert "2026-08-07" in stems, "today's report must never be skipped"
    assert "interp_skipped=1" in _recorded(job_env)[0]["note"]


def test_interpret_budget_of_one_still_covers_today(job_env, monkeypatch):
    monkeypatch.setenv("MI_INTERP_MAX_PER_RUN", "1")
    seen: list[Path] = []
    run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST),
        interpret=_fake_interpret(seen))
    assert [p.stem for p in seen] == ["2026-08-07"]


def test_default_interpret_reads_each_reports_own_cutoff(job_env, monkeypatch):
    """정보 차단선(SA-10): 캐치업으로 방금 만든 리포트도 자기 차단선으로 해석된다."""
    cutoffs: list[str] = []
    real = jobs_mod.ops_mod.interpret_report

    def spy(conn, path, **kw):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cutoffs.append(data["cutoff_utc"])
        return real(conn, path, use_llm=False)

    monkeypatch.setattr(jobs_mod.ops_mod, "interpret_report", spy)
    monkeypatch.setenv("MI_INTERP_MAX_PER_RUN", "9")
    run(job_env, "morning", now=datetime(2026, 8, 7, 7, 40, tzinfo=KST), interpret=None)

    assert len(cutoffs) == 4, cutoffs
    for path in (job_env["reports_root"] / "morning").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["cutoff_utc"] in cutoffs


# --- publish 종료코드 -> job 성패 (final-review.md F6) ----------------------
#
# `exit 5`(미결재 소스가 앞서 푸시 거부)는 `PUBLISH_HARD_FAILURES`에 없어서
# job이 초록으로 끝났다. 이 상태는 사람이 손대기 전까지 스스로 풀리지 않는데
# ("오프라인이라 다음에 재시도"와 성질이 다르다) 그동안 공개 사이트는 조용히
# 갱신을 멈춘다. 그래서 5는 실패로 올리고, 사유를 운영 상태 페이지가 읽는
# `job_runs.note`에 남긴다.

def _repo_with_publish(tmp_path, name: str, script: str) -> Path:
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "publish.sh").write_text(script, encoding="utf-8")
    return root


def _run_with_publish(job_env, repo_root: Path, **kw):
    kw.setdefault("interpret", _fake_interpret([]))  # 실 LLM을 부르지 않는다
    return jobs_mod.run_job(
        job_env["settings"], "morning",
        reports_root=job_env["reports_root"], docs_root=job_env["docs_root"],
        vault_root=job_env["vault_root"], publish=True, repo_root=repo_root,
        now=datetime(2026, 8, 7, 7, 40, tzinfo=KST), **kw,
    )


def test_publish_push_refusal_fails_the_job(job_env, tmp_path):
    repo_root = _repo_with_publish(
        tmp_path, "repo5",
        "#!/bin/bash\n"
        "echo 'publish: refusing to push — unreviewed non-artefact changes are ahead' >&2\n"
        "exit 5\n",
    )
    result = _run_with_publish(job_env, repo_root)
    assert result["steps"]["publish"] == "fail"
    assert result["exit"] == 5, "푸시가 거부됐는데 job이 성공으로 끝났다"


def test_publish_push_refusal_says_why_on_the_status_page(job_env, tmp_path):
    """`job_runs.note`는 `docs/status.html`의 사유 칸에 그대로 실린다
    (`ops.status` -> `_status_page`) — 여기가 CEO가 이유를 읽는 유일한 자리다."""
    repo_root = _repo_with_publish(
        tmp_path, "repo5n", "#!/bin/bash\nexit 5\n")
    _run_with_publish(job_env, repo_root)
    note = _recorded(job_env)[0]["note"]
    assert "publish_blocked" in note, note
    assert "결재" in note, note


def test_offline_push_failure_still_keeps_the_job_green(job_env, tmp_path):
    """spec B11-5 — 오프라인은 스스로 풀리므로 실패로 올리지 않는다.
    이 대조군이 없으면 `PUBLISH_HARD_FAILURES`를 전체 코드로 넓혀도 초록이다."""
    repo_root = _repo_with_publish(
        tmp_path, "repo1", "#!/bin/bash\necho 'push failed (offline)' >&2\nexit 1\n")
    result = _run_with_publish(job_env, repo_root)
    assert result["steps"]["publish"] == "fail"
    assert result["exit"] == 0
    assert "publish_blocked" not in _recorded(job_env)[0]["note"]
