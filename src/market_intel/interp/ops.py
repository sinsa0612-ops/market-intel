"""파이프라인 해석 단계 + 운영 상태 데이터 조립 (spec SA-9 · SA-12, ST3 소유).

이 모듈이 하는 일은 둘이다.

1. **`interpret_report()` — 파이프라인의 해석 단계.** `market-intel interpret`
   CLI(ST2)와 목적이 다르다. CLI는 사람이 파일 하나를 두고 즉석에서 돌리는
   도구(`--dry-run`/`--out`/`theses.json` 직접 읽기)이고, 이쪽은 **기록의
   정본**이다: `thesis_reviews`·`interpretations` 행을 남기고, 실패하면
   SA-9가 요구하는 `data_gaps` 행까지 등록한다. 가설 목록도 파일이 아니라
   DB(`store.list_theses`)에서 읽는다 — `thesis load`로 CEO가 명시적으로
   적재한 것이 그 시점의 정본이고, `thesis review` CLI도 같은 곳을 읽는다.
   (그래서 CLI가 이 함수를 부르도록 합치지는 않았다. ST2의 `cli_interpret`은
   자기 테스트가 파일 기반 경로와 내부 헬퍼에 묶여 있고, 그 파일은 이 서브태스크의
   소유가 아니다. 대신 CLI도 `record_outcome()`만 호출해 `interpretations`
   기록은 어느 경로로 돌든 남게 했다.)

2. **`status()` — SA-12의 4개 출처 조립.** `job_runs` / `provider_runs` +
   `collect_runs` / `interpretations` / `data_gaps`. 여기가 이 코드베이스에서
   **벽시계(`datetime.now`)를 쓰는 유일한 자리**다. 리포트·일정 페이지는
   정보 차단선(SA-10)을 지켜야 하지만, 운영 상태 페이지가 답하는 질문은
   "지금 이 시스템이 살아 있는가"이므로 기준이 벽시계인 것이 맞다
   (SA-12가 명시한 예외).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import db as db_mod
from ..reporting.cutoff import KST
from ..reporting.model import Report
from . import apply as apply_mod
from . import checks as checks_mod
from . import store as store_mod
from . import thesis as thesis_mod
from . import transitions as transitions_mod

INTERP_BUDGET_ENV = "MI_INTERP_MAX_PER_RUN"
DEFAULT_INTERP_BUDGET = 3

SAFE_DETAIL_MAX = 200  # SA-12 — fred_calendar의 safe_detail이 400자를 넘는다(실측)
OVERDUE_WINDOW_DAYS = 14  # 지연 판정을 위해 되짚어 보는 슬롯 창
THESIS_CHANGE_WINDOW_DAYS = 90  # SA-12/ST3 What #4 — `가설 변화` 목록의 기간

# 해석이 "돌긴 돌았다"고 볼 수 있는 status. 나머지(llm_unavailable/llm_timeout/
# bad_output/validation_failed)는 단계 실패로 센다 — 리포트는 그대로 나가지만
# (SA-9) 운영자는 해석이 며칠째 안 되고 있다는 사실을 알아야 한다.
INTERPRET_OK_STATUSES = ("ok", "partial")

STATE_OK = "정상"
STATE_PARTIAL = "일부 실패"
STATE_FAIL = "실패"
STATE_OVERDUE = "지연"
STATE_RUNNING = "실행 중"
STATE_NEVER = "기록 없음"

ALERT_STATES = (STATE_FAIL, STATE_PARTIAL, STATE_OVERDUE)


def interp_budget() -> int:
    """`MI_INTERP_MAX_PER_RUN` (기본 3). 캐치업이 밀렸을 때 한 번의 job이
    ollama를 몇 번까지 부를지 — 9b 기준 1건당 25~40초라 상한이 없으면 다음
    job과 겹친다(spec R6)."""
    raw = os.environ.get(INTERP_BUDGET_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INTERP_BUDGET
    return max(1, value)


# --- 해석 단계 --------------------------------------------------------------

def record_outcome(conn, report: Report, result: dict) -> str:
    """`apply.fill`의 결과를 DB에 남긴다: `interpretations` 한 행 + 실패면
    `data_gaps` 한 행(SA-9 2번째 겹). `apply.fill`은 DB에 쓰지 않는다고
    자기 docstring에 못박고 있으므로(호출자 책임) 그 자리가 여기다."""
    row = dict(result)
    row.update({
        "report_type": report.report_type,
        "report_date": report.report_date,
        "cutoff_utc": report.cutoff_utc,
    })
    interpretation_id = store_mod.record_interpretation(conn, row)

    # 해석 성적표(CEO 지시 2026-08-20) — 이 해석이 등록한 조건을 원장에 남긴다.
    # 만기가 지나면 규칙이 채점한다. **품질 미달 백엔드가 만든 조건은
    # `checks.register`가 스스로 버린다**(실측 근거는 그 모듈 주석).
    # 등록이 실패해도 해석 저장은 이미 끝났다 — 성적표가 리포트를 막지 않는다.
    try:
        checks_mod.register(
            conn, interpretation_id=interpretation_id, report_type=report.report_type,
            report_date=report.report_date, atoms=result.get("checks") or [],
            model=result.get("model"),
            # 리포트가 자기 차단선을 들고 있다 — 별도로 넘겨받지 않는다.
            # 호출자가 다른 시각을 줄 수 있으면 그 자리가 차단선을 두 번
            # 해석하는 구멍이 된다.
            cutoff=datetime.fromisoformat(report.cutoff_utc))
    except Exception as exc:  # noqa: BLE001
        print(f"  checks.register 실패(무시): {type(exc).__name__}")

    for item in report.missing:
        if not (item.gap_id or "").startswith("interp:"):
            continue
        # `reporting/build.py::_register_gap`과 같은 패턴 — 같은 사유가 반복돼도
        # 행은 하나로 유지된다.
        conn.execute(
            "INSERT OR IGNORE INTO data_gaps(gap_id, subject, metric, detected_at, reason, status) "
            "VALUES (?,?,?,?,?,?)",
            (item.gap_id, "interpretation", report.report_type, db_mod.iso_utc(), item.reason, "제안"),
        )
    conn.commit()
    return interpretation_id


def _record_reviews_idempotently(conn, reviews: list[dict]) -> int:
    """`store.record_reviews`를 한 행씩 호출하고 중복은 넘긴다.

    `thesis_reviews`에는 `UNIQUE(thesis_id, report_type, report_date, cutoff_utc)`가
    걸려 있는데 `store.record_reviews`는 평범한 INSERT라, 같은 리포트를 두 번
    해석하면(같은 날 job 재실행·수동 재생성) IntegrityError로 **해석이 LLM을
    부르기도 전에** 죽는다 — 실측으로 터진 것이다. 같은 차단선의 같은 가설
    판정은 정의상 같은 값이므로 두 번째 기록은 건너뛰는 것이 맞다.
    한 행씩 넣는 이유: 가설이 중간에 추가된 경우, 이미 기록된 가설 때문에
    새 가설의 판정까지 통째로 롤백되면 안 되기 때문이다.

    [ST1에 보고할 것] 근본 수리 위치는 `store.record_reviews`(INSERT OR IGNORE
    또는 UNIQUE 충돌 처리)다. ST3 경계상 `store.py`는 읽기 전용이라 호출부에서
    막았다."""
    recorded = 0
    for row in reviews:
        try:
            recorded += store_mod.record_reviews(conn, [row])
        except sqlite3.IntegrityError:
            conn.rollback()
    return recorded


def thesis_display_introduced_on(conn, pending_engine_version: str | None = None,
                                 report_date: str | None = None) -> str:
    """ST4 / 명세 §2 구현 4: 표시 도입일은 상수가 아니라 원장에서 뽑는다 — 현재
    엔진 버전 행 중 가장 이른 `created_at`(KST 날짜).

    원장에 그 버전 행이 아직 없으면(첫 실행 당일) `report_date`로 대체하지
    않는다는 원칙(final-review F4)은 그대로다 — 그런데 그 원칙을 지키는
    방식이 두 호출 경로를 실측으로 갈랐다: `ops.interpret_report`는 이번
    판정을 먼저 기록한 *뒤에* 묻기 때문에 원장에 방금 넣은 행이 바로 잡히지만,
    `market-intel interpret` CLI는 절대 기록하지 않으므로(그 리뷰는 자기
    docstring이 밝히듯 `record_reviews`를 부르지 않는다) 같은 배포 첫날 —
    하필 범례가 가장 필요한 그날 — CLI 산출물에만 범례가 빠졌다(final-review
    F4, CEO 결정 (가): 두 경로가 같은 산출물을 내야 한다).

    `pending_engine_version`: CLI 경로가 `transitions.pending_row_from_review`로
    이미 만들어 둔 오늘 판정의 `engine_version`(한 프로세스의 모든 가설이
    같은 `thesis.ENGINE_VERSION`을 찍으므로 하나만 넘기면 된다). 원장 조회가
    비어 있고 이 값이 현재 엔진 버전과 같으면 — 즉 오늘이 정말 그 버전의 첫
    실사용일이면 — "지금"을 KST 날짜로 돌린다. 이건 없는 값을 지어내는 게
    아니다: `ops` 경로가 기록 직후 읽어오는 실제 `created_at`도 결국 기록
    시점의 "지금"이므로, 이 폴백은 ops 경로가 실제로 얻을 값을 CLI 경로도
    미리 계산할 뿐이다. `report_date`는 여전히 쓰지 않는다 — CLI도 `--date`로
    과거 리포트를 다시 돌릴 수 있어(ops의 캐치업과 같은 함정) report_date를
    쓰면 도입일이 실제보다 앞당겨진다(위 경고가 여기서도 그대로 유효하다).

    `pending_engine_version`이 없거나 현재 엔진 버전과 다르면(가설이 없어
    `reviews`가 빈 경우 등) 빈 문자열 — 값이 없으면 지어내지 말고 호출자가
    범례를 생략하게 한다(`render_impact`/`site.py` 둘 다 빈 문자열이면
    범례 줄을 뺀다).

    `report_date`(F-A 수리, SA-10; **4차 검수 F-A 잔여분 수리**): 도입일
    *값*을 만드는 데는 여전히 안 쓴다(위 문단의 이유가 그대로 유효하다 —
    `report_date`를 값으로 쓰면 캐치업 때 도입일이 실제보다 앞당겨진다).
    계산된 `introduced`가 이 `report_date`보다 **뒤**면 빈 문자열을 돌려
    범례의 도입일 조각을 생략시킨다 — 그 리포트의 차단선에서는 아직 오지
    않은 날짜를 "도입됐다"고 쓸 수 없다(R8/R9 경고문 자체는 이 반환값과
    무관하게 `render_impact`가 항상 붙인다 — CEO 결정 (가), `_LEGEND_R89`).

    이전 구현은 원장에 `report_date`보다 뒤 날짜의 판정 행이 이미 "존재하는지"로
    캐치업 여부를 추정했다(`_has_later_recorded_report`, 시간순 역전 검사).
    그 방식은 **캐치업이 오래된 리포트부터 처리하는 동안**(`jobs.py`
    `paths[-budget:]`, oldest-first) 한 번도 걸리지 않는다 — 처리 중인 리포트가
    항상 그 시점 원장의 최신이라, "이 report_date보다 뒤 날짜가 원장에 있는가"가
    매번 거짓으로 나온다(실측: 2026-08-03 배치가 07-30·07-31 두 건에서
    "도입 2026-08-03"이라는 미래 도입일을 그대로 발행했다). `introduced` 값과
    `report_date`를 직접 비교하면 이 우회가 사라진다: 캐치업이든 정상 당일
    처리든, 방금 계산된 도입일이 지금 렌더링 중인 리포트의 날짜보다 뒤이기만
    하면 무조건 걸린다. `report_date`가 없으면(옛 호출부) 이 비교를 건너뛴다."""
    introduced = store_mod.engine_introduced_on(conn, thesis_mod.ENGINE_VERSION)
    if not introduced and pending_engine_version == thesis_mod.ENGINE_VERSION:
        introduced = datetime.fromisoformat(db_mod.iso_utc()).astimezone(KST).strftime("%Y-%m-%d")
    if not introduced:
        return ""
    if report_date and introduced > report_date:
        return ""
    return introduced


def interpret_report(conn, path, *, model: str | None = None, use_llm: bool = True) -> dict:
    """리포트 JSON 하나를 **그 리포트의 차단선으로** 해석해 제자리에 다시 쓴다.

    차단선의 유일한 출처는 `report.cutoff_utc`다(SA-10). 캐치업으로 오늘 만든
    지난주 리포트도 지난주의 눈으로 해석된다 — 벽시계를 쓰면 그 리포트가 볼 수
    없던 사실이 해석에 섞인다.
    """
    path = Path(path)
    report = Report.from_json(path.read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat(report.cutoff_utc)

    theses = store_mod.list_theses(conn)
    reviews = []
    if theses:
        reviews = thesis_mod.review(conn, theses, cutoff, report.report_type, report.report_date)
        _record_reviews_idempotently(conn, reviews)

    thesis_impact = ""
    if reviews:
        # 이 시점에는 위 `_record_reviews_idempotently`가 이미 오늘 행을
        # 기록했으므로 `pending`은 필요 없다 — 파생 뷰가 원장에서 바로 읽는다
        # (ST4 명세 §5 What #3, Risk R-1: `market-intel interpret`은 기록하지
        # 않으므로 그쪽만 `pending`이 필요하다).
        states = {
            r["thesis_id"]: transitions_mod.thesis_view(conn, r["thesis_id"], as_of=report.report_date)
            for r in reviews
        }
        introduced_on = thesis_display_introduced_on(conn, report_date=report.report_date)
        thesis_impact = thesis_mod.render_impact(
            reviews, report.report_date, states=states, introduced_on=introduced_on)
    next_check_suffix = thesis_mod.render_next_check_suffix(reviews, report) if reviews else ""

    report, result = apply_mod.fill(
        report, conn, cutoff=cutoff,
        thesis_impact=thesis_impact, thesis_engine_version=thesis_mod.ENGINE_VERSION,
        next_check_suffix=next_check_suffix,
        model=model, use_llm=use_llm,
    )
    if reviews:
        report.meta["interpretation"]["thesis_reviews"] = [
            {"thesis_id": r["thesis_id"], "verdict": r["verdict"], "changed": bool(r["changed"])}
            for r in reviews
        ]

    path.write_text(report.to_json(), encoding="utf-8")
    record_outcome(conn, report, result)
    return result


# --- 운영 상태 --------------------------------------------------------------

def _kst_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(KST).date()


def _last_success_date(conn, job: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(started_at) AS started_at FROM job_runs WHERE job=? AND status IN ('ok','partial')",
        (job,),
    ).fetchone()
    return _kst_date(row["started_at"] if row else None)


def _recording_since(conn) -> date | None:
    """`job_runs`에 기록이 시작된 날. 그 이전 슬롯은 늦은 것이 아니라 **모르는**
    것이다 — 이 테이블은 2단계-B에서 새로 생겼고, 그 전에도 job은 돌고 있었다.
    (실측: 라이브 DB를 복사해 상태를 뽑았더니 9개 중 8개가 `지연`으로 떴다.
    전부 거짓 경보였다.)"""
    row = conn.execute("SELECT MIN(started_at) AS started_at FROM job_runs").fetchone()
    return _kst_date(row["started_at"] if row else None)


def _overdue_slots(job: str, last_success: date | None, today: date,
                   since: date | None) -> int:
    """마지막 성공 이후로 **이미 지나간** 예정 슬롯 수(SA-12의 지연 판정).

    오늘치 슬롯은 세지 않는다 — 예정 시각(예: `close`는 16:15)이 아직 오지
    않았을 수 있고, 그걸 지연이라 부르면 매일 아침 거짓 경보가 뜬다.
    기록이 시작되기 전(`since`)의 슬롯도 세지 않는다."""
    from ..jobs import slot_dates

    if since is None:
        return 0
    return sum(
        1 for d in slot_dates(job, today, days=OVERDUE_WINDOW_DAYS)
        if d < today and d >= since and (last_success is None or d > last_success)
    )


def _job_state(run: dict | None, overdue: int) -> str:
    """한 단어 상태. 마지막 실행의 결과가 지연보다 우선한다 — 오늘 실패한 job을
    `지연`이라고만 적으면 더 급한 사실(방금 깨졌다)이 가려진다. 밀린 슬롯 수는
    표의 별도 칸(`overdue`)으로 항상 함께 보인다."""
    if run is None:
        return STATE_OVERDUE if overdue else STATE_NEVER
    state = {
        "fail": STATE_FAIL, "partial": STATE_PARTIAL, "running": STATE_RUNNING,
    }.get(run["status"])
    if state:
        return state
    return STATE_OVERDUE if overdue else STATE_OK


def _steps_text(steps: dict) -> str:
    order = ["collect", "report", "interpret", "site", "obsidian", "publish"]
    keys = [k for k in order if k in steps] + [k for k in steps if k not in order]
    return " ".join(f"{k}={steps[k]}" for k in keys)


def _collect_block(conn) -> dict | None:
    run = conn.execute("SELECT * FROM collect_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if run is None:
        return None
    rows = conn.execute(
        "SELECT provider, status, reason_code, safe_detail, item_count FROM provider_runs "
        "WHERE run_id=? ORDER BY started_at, provider",
        (run["run_id"],),
    ).fetchall()
    providers = [
        {
            "provider": r["provider"], "status": r["status"],
            "reason_code": r["reason_code"] or "",
            "safe_detail": (r["safe_detail"] or "")[:SAFE_DETAIL_MAX],
            "item_count": r["item_count"],
        }
        for r in rows
    ]
    return {
        "run_id": run["run_id"], "workflow": run["workflow"],
        "started_at": run["started_at"], "cutoff_at": run["cutoff_at"],
        "status": run["status"], "providers": providers,
    }


def _gaps(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT gap_id, subject, metric, detected_at, reason, status FROM data_gaps "
        "ORDER BY detected_at DESC, gap_id"
    ).fetchall()
    return [dict(r) for r in rows]


def status(conn, *, now: datetime | None = None) -> dict:
    """`docs/status.html`과 `market-intel ops status`가 함께 쓰는 상태 dict.

    `now`는 SA-12가 허용한 벽시계다(위 모듈 docstring 참고). 테스트는 고정
    시각을 주입한다."""
    from ..jobs import JOBS

    now = now or datetime.now(KST)
    now = now.astimezone(KST)
    today = now.date()

    runs = {r["job"]: r for r in store_mod.last_job_runs(conn)}
    since = _recording_since(conn)
    jobs_block = []
    alerts = []
    if since is None:
        alerts.append("자동 실행 기록이 아직 없다 — launchd 등록과 첫 실행을 확인해야 한다")
    for job in JOBS:
        run = runs.get(job)
        overdue = _overdue_slots(job, _last_success_date(conn, job), today, since)
        state = _job_state(run, overdue)
        jobs_block.append({
            "job": job,
            "last_started": run["started_at"] if run else None,
            "last_finished": run["finished_at"] if run else None,
            "status": run["status"] if run else None,
            "steps": run["steps"] if run else {},
            "steps_text": _steps_text(run["steps"]) if run else "",
            "note": (run["note"] or "") if run else "",
            "catchup_generated": run["catchup_generated"] if run else 0,
            "overdue": overdue,
            "state": state,
        })
        if state in ALERT_STATES or overdue:
            detail = f"{job} — {state}"
            if overdue:
                detail += f" (예정 실행 {overdue}회를 건너뛰었다)"
            alerts.append(detail)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_display": now.strftime("%Y-%m-%d %H:%M KST"),
        "jobs": jobs_block,
        "collect": _collect_block(conn),
        "interpretation": store_mod.last_interpretation(conn),
        "gaps": _gaps(conn),
        "alerts": alerts,
        "healthy": not alerts,
    }


# --- 가설 (사이트 게시용) ----------------------------------------------------

# 판정 → 그 판정을 만든 조건 묶음. `유지`는 어느 조건도 걸리지 않은 결과라
# 전용 묶음이 없다(아래에서 전체를 훑는다).
_VERDICT_GROUP = {"무효": "falsify", "약화": "weaken", "강화": "strengthen"}


def _review_reason(atoms_json: str | None, verdict: str = "") -> str:
    """그 판정을 **만든** 조건의 문장. `판정 불가`면 왜 판단할 수 없었는지
    (관측 부족) 말하는 UNKNOWN 조건의 문장을 고른다.

    묶음을 가리지 않고 첫 조건을 집으면 `강화` 가설 밑에 반증 조건 문장이
    근거처럼 붙는다 — 실측으로 그렇게 나왔고, 사람이 읽으면 정반대로 읽힌다."""
    if not atoms_json:
        return ""
    try:
        atoms = json.loads(atoms_json)
    except ValueError:
        return ""

    groups = ("falsify", "weaken", "strengthen")
    preferred = _VERDICT_GROUP.get(verdict)
    order = (preferred,) + tuple(g for g in groups if g != preferred) if preferred else groups

    def pick(group: str, wanted_status: str | None) -> str:
        for atom in atoms.get(group, []) or []:
            if wanted_status and atom.get("status") != wanted_status:
                continue
            message = (atom.get("detail") or {}).get("message")
            if message:
                return message
        return ""

    # 판정을 만든 조건 = 그 묶음에서 참인 조건. 판정 불가는 UNKNOWN이 이유다.
    wanted = "UNKNOWN" if verdict == "판정 불가" else "TRUE"
    for group in order:
        message = pick(group, wanted)
        if message:
            return message
    for group in order:
        message = pick(group, None)
        if message:
            return message
    return ""


def _last_review(conn, thesis_id: str) -> dict | None:
    row = conn.execute(
        "SELECT verdict, prev_verdict, changed, atoms_json, report_type, report_date, created_at "
        "FROM thesis_reviews WHERE thesis_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (thesis_id,),
    ).fetchone()
    return dict(row) if row else None


def _thesis_state_line(conn, thesis_id: str, review: dict | None) -> str:
    """ST4 / 명세 §3.2 — 사이트 가설 페이지의 "상태:" 줄. 판정을 만든 조건의
    파생 뷰(`transitions.thesis_view`)를 `thesis.state_fragment`에 넘긴다.
    `review["atoms_json"]`은 원장에 실제로 저장된 모양(`{id, status, detail}`
    평면 — `evals_by_group`의 중첩 `atom.id`가 아니다)이라 `row["atoms"]`로
    넘긴다 — `thesis._fired_atom_ids`가 두 모양을 모두 받는다."""
    if review is None:
        return ""
    row = {"verdict": review["verdict"], "atoms": json.loads(review["atoms_json"])}
    state = transitions_mod.thesis_view(conn, thesis_id)
    return thesis_mod.state_fragment(row, state)


def thesis_overview(conn) -> list[dict]:
    """5테마 × 슬롯 — 각 가설의 현재 판정과 그 사유. 가설이 없는 테마도
    빠지지 않는다(빈 테마는 화면에서 '가설 없음'으로 읽힌다)."""
    loaded: dict[str, list[dict]] = {theme: [] for theme in thesis_mod.THEME_LABELS}
    for t in store_mod.list_theses(conn):
        review = _last_review(conn, t["thesis_id"])
        loaded.setdefault(t["theme"], []).append({
            "thesis_id": t["thesis_id"],
            "slot": t["slot"],
            "statement": t["statement"],
            "leading_indicators": t["leading_indicators"],
            "next_check_date": t["next_check_date"],
            "verdict": review["verdict"] if review else "",
            "reason": _review_reason(review["atoms_json"], review["verdict"]) if review else "",
            "state_line": _thesis_state_line(conn, t["thesis_id"], review),
            "reviewed_at": review["report_date"] if review else "",
        })
    return [
        {"theme": theme, "label": thesis_mod.THEME_LABELS.get(theme, theme),
         "theses": sorted(items, key=lambda x: x["slot"])}
        for theme, items in loaded.items()
    ]


def thesis_changes(conn, *, now: datetime | None = None,
                   days: int = THESIS_CHANGE_WINDOW_DAYS) -> list[dict]:
    """판정이 뒤집힌 리뷰만, 최근 `days`일. 명세 §13.3이 "가격 알림보다 가설
    변화 알림을 우선한다"고 한 그 알림의 게시 형태다.

    기간 기준은 리뷰가 딸린 **리포트의 차단선**(`cutoff_utc`)이다 — 기록 시각이
    아니라. 캐치업으로 오늘 소급 생성된 지난주 리포트의 판정 변화는 지난주의
    변화지 오늘의 변화가 아니다."""
    now = (now or datetime.now(KST)).astimezone(KST)
    since = db_mod.iso_utc(now - timedelta(days=days))
    # `rules_changed=1`도 함께 싣는다: 기준을 바꾼 것은 판정이 뒤집힌 것만큼
    # 중요한 사건이다. 오히려 조용히 지나가면 안 되는 쪽이다 — 그 이후 판정은
    # 그 전 판정과 비교할 수 없기 때문이다(CEO 2026-08-04 "목표치 재설정").
    # `engine_changed=1`도 같은 이유로 싣는다 — 조건이 아니라 판정 엔진의
    # 뜻이 바뀐 경계이고, 그 전후 판정도 서로 비교할 수 없다(명세 §2).
    rows = conn.execute(
        "SELECT r.thesis_id, r.verdict, r.prev_verdict, r.report_type, r.report_date, "
        "r.cutoff_utc, r.rules_changed, r.engine_changed, t.statement FROM thesis_reviews r "
        "LEFT JOIN theses t ON t.thesis_id = r.thesis_id "
        "WHERE (r.changed=1 OR r.rules_changed=1 OR r.engine_changed=1) AND r.cutoff_utc >= ? "
        "ORDER BY r.cutoff_utc DESC, r.rowid DESC",
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]
