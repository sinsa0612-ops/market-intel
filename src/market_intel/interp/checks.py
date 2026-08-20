"""해석 성적표 — 해석이 **미리 등록한 조건**을 만기에 규칙이 채점한다.

CEO 지시(2026-08-20): *"이전 해석이 맞았는지 판단해야 우리가 왜 시장이 어떤
요인으로 인해 움직였는지 알 수 있잖아."*

## 무엇을 채점하고 무엇을 채점하지 않나

**산문을 채점하지 않는다.** 성적표는 *"이 해석이 맞았다"*가 아니라
**"이 해석이 등록한 조건 3개 중 2개가 맞았다"**라고만 말한다. 산문과 조건이
일치한다고 주장하지 않으므로 **한국어 산문을 조건으로 파싱하는 환각 표면**이
생기지 않는다 — `model.PriorInterpretation` 주석이 경고한 함정 (2)가 그것이다.

세 함정(같은 주석)에 대한 답:
  (1) 같은 LLM이 제 글을 평가하면 합리화한다
      -> **LLM은 채점에 안 낀다.** 등록은 칸 채우기, 채점은 `thesis.evaluate_atom`.
  (2) 산문 파싱은 새 환각 표면이다
      -> **산문을 안 읽는다.** 등록된 조건만 본다.
  (3) 오늘 옛 글을 읽고 "실은 이런 뜻이었다"고 정하는 건 골대 이동이다
      -> **append-only.** 등록된 조건은 수정 불가, 채점은 한 번뿐.

## 채점 엔진을 새로 만들지 않는다

`thesis.evaluate_atom(conn, atom, cutoff)`이 이미 조건 39개를 채점하는 범용
평가기이고 6종(`threshold`·`change_pct`·`relative_change_pct`·`consecutive`·
`consecutive_sign`·`stale`)을 지원한다. 실측(2026-08-20): 실제 발행된
「다음 검증」이 주장하는 것 대부분이 이 6종으로 표현된다.

## 품질 미달 백엔드는 등록하지 않는다

**실측(2026-08-20)**: 같은 산문을 조건으로 옮기게 했을 때
```
Codex(gpt-5.6-luna)  유효 2/2 · 환각 0 · 측정 불가한 주장은 스스로 폐기
ollama(qwen3.5:9b)   유효 0~1/5 · 러셀2000을 "나스닥100"이라 부르고
                      원문에 없는 주장을 생성
```
폴백은 실제로 일어난다(해석 55건 중 3건이 ollama). **그 조건으로 성적표를
쌓으면 성적표 자체가 오염된다** — 틀린 믿음에 인증서가 붙는 쪽이 채점을 안
하는 것보다 나쁘다는 CEO 지적이 여기에 그대로 적용된다.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from .. import db as db_mod
from . import store as store_mod
from . import thesis as thesis_mod

# 조건을 등록해도 되는 백엔드. 여기 없는 백엔드가 만든 조건은 **버린다**
# (위 모듈 주석의 실측 근거). `llm.py`의 접두어와 같은 값이다.
TRUSTED_MODEL_PREFIXES = ("gpt:", "claude:")

# 등록 시점부터 만기까지의 기본 거래일 수. 해석이 스스로 기한을 말하지 않으므로
# 여기서 정한다. 5거래일 = 약 한 주 — 일간 해석이 "다음에 확인하자"고 할 때
# 실제로 겨냥하는 창이다(실물 「다음 검증」이 드는 일정이 대개 1~2주 안이다).
DEFAULT_HORIZON_DAYS = 7

MAX_PER_INTERPRETATION = 4  # 한 해석이 등록할 수 있는 조건 수 상한


@dataclass
class Scorecard:
    """한 묶음의 성적. `unknown`은 실패가 아니라 **채점 불가**다 — 그 비율
    자체가 "우리 관측으로 검증되는 주장을 쓰고 있나"의 지표다."""
    total: int = 0
    true: int = 0
    false: int = 0
    unknown: int = 0

    @property
    def scored(self) -> int:
        return self.true + self.false

    @property
    def hit_rate(self) -> float | None:
        return (self.true / self.scored * 100) if self.scored else None


def is_trusted(model: str | None) -> bool:
    return bool(model) and model.startswith(TRUSTED_MODEL_PREFIXES)


def due_date_for(report_date: str, horizon_days: int = DEFAULT_HORIZON_DAYS) -> str:
    return (date.fromisoformat(report_date) + timedelta(days=horizon_days)).isoformat()


def _has_observations(conn, subject: str, metric: str, cutoff) -> bool:
    """차단선 시점에 이 (subject, metric) 관측을 볼 수 있었나.

    **없으면 등록하지 않는다.** 채점될 수 없는 조건을 쌓으면 성적표가 「채점
    불가」로만 차오르고, 그 비율이 "우리 관측으로 검증되는 주장을 쓰고 있나"를
    재는 지표라서 지표 자체가 망가진다.

    실측(2026-08-20 E2E): 모델이 `KOSPI`·`SOX`로 조건을 냈는데 원장의 실제
    subject는 `^KS11`·`^SOX`다 — 다이제스트가 **사람이 읽는 이름**만 보여주기
    때문이다. 프롬프트에 코드 목록을 주는 것으로 원인을 고쳤지만, 프롬프트는
    언제든 다시 어긋날 수 있으므로 여기서도 막는다.

    **원장을 직접 조회하지 않는다.** `interp/` 아래 어떤 파일도 `fact_revisions`에
    직접 닿으면 안 된다는 규칙이 있고(`test_interp_never_touches_fact_revisions_or_raw_sql`),
    `thesis._observations`가 그 정식 경로(`db.facts_as_of`)를 이미 감싸고 있다.
    차단선이 함께 걸리는 것이 오히려 옳다 — **리포트 시점에 볼 수 없던 대상에
    조건을 걸면 안 된다.**"""
    return bool(thesis_mod._observations(
        conn, {"subject": subject, "metric": metric}, cutoff))


def register(conn, *, interpretation_id: str, report_type: str, report_date: str,
             atoms: list[dict], model: str | None, cutoff, basis: dict | None = None,
             horizon_days: int = DEFAULT_HORIZON_DAYS) -> int:
    """조건을 등록한다. -> 실제로 등록된 개수.

    **품질 미달 백엔드면 0을 낸다** — 조용히 등록하지 않는다(위 주석).
    같은 (해석, 조건 id)는 UNIQUE라 재실행해도 늘지 않는다."""
    if not is_trusted(model):
        return 0
    due = due_date_for(report_date, horizon_days)
    now = db_mod.iso_utc()
    n = 0
    for atom in atoms[:MAX_PER_INTERPRETATION]:
        reasons: list[str] = []
        why = atom.get("why") or ""
        clean = {k: v for k, v in atom.items() if k != "why" and v is not None}
        atom_id = clean.pop("id", None)
        if not atom_id:
            continue
        thesis_mod._validate_atom(clean, atom_id, reasons)
        if reasons:
            continue  # 형식이 틀린 조건은 등록하지 않는다 — 채점할 수 없다
        if not _has_observations(conn, clean["subject"], clean["metric"], cutoff):
            continue  # 차단선 시점에 볼 수 없던 대상 — 영원히 채점 불가다(위 주석)
        if clean.get("benchmark") and not _has_observations(
                conn, clean["benchmark"], clean["metric"], cutoff):
            continue
        store_mod.insert_check(conn, {
            "check_id": f"chk_{uuid.uuid4().hex[:20]}",
            "interpretation_id": interpretation_id, "report_type": report_type,
            "report_date": report_date, "atom_id": atom_id,
            "atom_json": json.dumps(clean, ensure_ascii=False, sort_keys=True),
            "basis_json": json.dumps(basis or {}, ensure_ascii=False),
            "why": why, "due_date": due, "model": model, "registered_at": now})
        n += 1
    conn.commit()
    return n


def due(conn, as_of: str) -> list[dict]:
    """만기가 지났는데 아직 채점 안 된 조건들. `as_of`는 리포트의 날짜다 —
    **오늘 날짜가 아니다.** 과거 리포트를 다시 만들 때 미래를 당겨 쓰지 않기
    위해서다(차단선과 같은 이유)."""
    return store_mod.due_checks(conn, as_of)


def score_due(conn, as_of: str, cutoff) -> int:
    """만기 도래분을 채점한다. -> 채점한 개수.

    **채점은 한 번뿐이다.** `scored_at IS NULL`인 것만 고르고 한 번 채우면
    다시 건드리지 않는다 — 나중에 값이 달라졌다고 성적이 바뀌면 그건 골대
    이동이다."""
    n = 0
    for row in due(conn, as_of):
        atom = json.loads(row["atom_json"])
        atom.setdefault("id", row["atom_id"])
        try:
            status, detail = thesis_mod.evaluate_atom(conn, atom, cutoff)
        except Exception as exc:  # noqa: BLE001 — 조건 하나가 성적표를 막지 않는다
            status, detail = "UNKNOWN", {"message": f"채점 실패: {type(exc).__name__}"}
        store_mod.mark_check_scored(
            conn, row["check_id"], db_mod.iso_utc(), status,
            json.dumps(detail, ensure_ascii=False, default=str))
        n += 1
    conn.commit()
    return n


def scorecard(conn, *, report_type: str | None = None, subject: str | None = None) -> Scorecard:
    """누적 성적. `subject`로 좁히면 **변수별 적중률**이 된다 — CEO가 물은
    "어떤 변수를 짚었을 때 맞았나"가 이것이다."""
    card = Scorecard()
    for verdict, atom_json in store_mod.scored_checks(conn, report_type):
        if subject and json.loads(atom_json).get("subject") != subject:
            continue
        card.total += 1
        if verdict == "TRUE":
            card.true += 1
        elif verdict == "FALSE":
            card.false += 1
        else:
            card.unknown += 1
    return card
