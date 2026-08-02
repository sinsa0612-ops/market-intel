"""Thesis loader + deterministic verdict engine (spec §3.1 ③④, SA-7).

No LLM anywhere in this module. `evaluate_atom` reads facts exclusively
through `db.facts_as_of(conn, cutoff, ...)` (spec SA-10, BRIEF rule 9) — never
a raw SQL query against `fact_revisions` — so a thesis can never see a fact
that was not yet known as of its report's cutoff.

Verdict is one of 5 (spec §사전 확인 사실 D): 강화/유지/약화/무효/판정 불가.
`판정 불가` is not a bug — most theses will report it for months, because the
observation counts financials/macro providers actually deliver today (1-2 per
subject/metric) cannot support multi-period conditions yet. Printing 유지 in
that situation would be a fabricated "no news is good news" reading (spec
§3.3: 미확인은 추정으로 채우지 않고 확인 과제로 보류).
"""
from __future__ import annotations

import json
import operator
from datetime import datetime, timedelta

from .. import db as db_mod

ENGINE_VERSION = "2b.1"

THEMES = {"ai_semi", "power_energy", "fin_credit", "consumer_cycle", "policy_geo"}

THEME_LABELS = {
    "ai_semi": "AI·반도체",
    "power_energy": "전력·에너지",
    "fin_credit": "금융·신용",
    "consumer_cycle": "소비·경기",
    "policy_geo": "정책·지정학",
}

ATOM_KINDS = {"threshold", "change_pct", "consecutive", "stale"}
OPS = {
    ">": operator.gt, "<": operator.lt, ">=": operator.ge,
    "<=": operator.le, "==": operator.eq, "!=": operator.ne,
}
DIRECTIONS = {"up", "down"}

MAX_SLOT = 3
MAX_THESES_PER_THEME = 3


class ThesisLoadError(Exception):
    """가설 파일이 SA-7의 적재 거부 규칙 중 하나라도 어긴 경우. `.reasons`에
    사람이 읽을 수 있는 문장으로 위반 사유를 전부 담는다(부분 지적이 아니라
    파일 전체를 한 번에 훑어 모은 목록 — CEO가 한 번에 다 고칠 수 있도록)."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


# ---------------------------------------------------------------------------
# Loader (spec SA-7) — pure file/JSON validation, no DB access at all, so a
# rejected file can never leave a partial trace in the DB (the caller only
# calls store.replace_theses() after this returns successfully).
# ---------------------------------------------------------------------------

def _validate_atom(atom: dict, where: str, reasons: list[str]) -> None:
    kind = atom.get("kind")
    if kind not in ATOM_KINDS:
        reasons.append(f"{where}: 알 수 없는 조건 종류(kind)={kind!r} (허용: {sorted(ATOM_KINDS)})")
        return
    if not atom.get("subject") or not atom.get("metric"):
        reasons.append(f"{where}: subject/metric이 비어 있음")
    if kind in ("threshold", "change_pct"):
        op = atom.get("op")
        if op not in OPS:
            reasons.append(f"{where}: 알 수 없는 연산자(op)={op!r} (허용: {sorted(OPS)})")
        if "value" not in atom:
            reasons.append(f"{where}: value가 없음")
    if kind == "change_pct" and "lookback" not in atom:
        reasons.append(f"{where}: lookback이 없음")
    if kind == "consecutive":
        direction = atom.get("direction")
        if direction not in DIRECTIONS:
            reasons.append(f"{where}: 알 수 없는 방향(direction)={direction!r} (허용: {sorted(DIRECTIONS)})")
        if "periods" not in atom:
            reasons.append(f"{where}: periods가 없음")
    if kind == "stale" and "days" not in atom:
        reasons.append(f"{where}: days가 없음")


def _validate_thesis(theme: str, raw: dict, reasons: list[str]) -> dict | None:
    tid = raw.get("id")
    label = f"가설 {tid or '(id 없음)'}({theme})"
    ok = True

    if not tid:
        reasons.append(f"{label}: id가 없음")
        ok = False

    slot = raw.get("slot")
    if not isinstance(slot, int) or not (1 <= slot <= MAX_SLOT):
        reasons.append(f"{label}: slot={slot!r}이 1~{MAX_SLOT} 범위 밖")
        ok = False

    statement = raw.get("statement") or ""
    if not statement.strip():
        reasons.append(f"{label}: statement가 비어 있음")
        ok = False

    leading = raw.get("leading_indicators") or []
    if not leading:
        reasons.append(f"{label}: leading_indicators가 비어 있음 — 선행 지표 없는 가설은 확인 방법이 없다")
        ok = False

    next_check_date = raw.get("next_check_date")
    if not next_check_date:
        reasons.append(f"{label}: next_check_date가 없음")
        ok = False
    else:
        try:
            datetime.fromisoformat(next_check_date)
        except ValueError:
            reasons.append(f"{label}: next_check_date={next_check_date!r}가 ISO 날짜가 아님")
            ok = False

    conditions = raw.get("conditions") or {}
    falsify = conditions.get("falsify") or []
    weaken = conditions.get("weaken") or []
    strengthen = conditions.get("strengthen") or []

    if not falsify:
        reasons.append(f"{label}: 반증 조건(falsify)이 0개 — 명세 §2.2: 반증 조건 없는 가설은 가설이 아니다")
        ok = False

    for group_name, group in (("falsify", falsify), ("weaken", weaken), ("strengthen", strengthen)):
        for i, atom in enumerate(group):
            _validate_atom(atom, f"{label}.{group_name}[{i}]", reasons)

    if not ok:
        return None

    return {
        "thesis_id": tid, "theme": theme, "slot": slot, "statement": statement,
        "leading_indicators": list(leading), "next_check_date": next_check_date,
        "conditions": {"falsify": falsify, "weaken": weaken, "strengthen": strengthen},
    }


def load_file(path: str) -> list[dict]:
    """Loads and validates `theses/theses.json` (spec SA-7). Any violation ->
    `ThesisLoadError` listing every reason found, and no partial result — the
    caller must not call `store.replace_theses` unless this returns."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    reasons: list[str] = []
    themes_dict = data.get("themes") or {}

    unknown_theme_keys = set(themes_dict) - THEMES
    for key in sorted(unknown_theme_keys):
        reasons.append(f"알 수 없는 테마 키: {key!r} (허용: {sorted(THEMES)})")

    out: list[dict] = []
    for theme, theme_block in themes_dict.items():
        if theme not in THEMES:
            continue  # already reported above
        theses_list = (theme_block or {}).get("theses") or []
        if len(theses_list) > MAX_THESES_PER_THEME:
            reasons.append(
                f"테마 {theme}({THEME_LABELS.get(theme, theme)}) 가설이 {len(theses_list)}개 "
                f"— 테마당 최대 {MAX_THESES_PER_THEME}개"
            )

        seen_slots: dict[int, str] = {}
        for raw in theses_list:
            validated = _validate_thesis(theme, raw, reasons)
            slot = raw.get("slot")
            if isinstance(slot, int) and slot in seen_slots:
                reasons.append(
                    f"테마 {theme}: slot {slot}이 {seen_slots[slot]!r}와 {raw.get('id')!r}에서 중복"
                )
            elif isinstance(slot, int):
                seen_slots[slot] = raw.get("id")
            if validated is not None:
                out.append(validated)

    if reasons:
        raise ThesisLoadError(reasons)
    return out


# ---------------------------------------------------------------------------
# Atom evaluation (spec SA-7's evaluation table)
# ---------------------------------------------------------------------------

def _observations(conn, atom: dict, cutoff) -> list[tuple[str, float | None]]:
    filters = {"subject": atom["subject"], "metric": atom["metric"]}
    if atom.get("category"):
        filters["category"] = atom["category"]
    rows = db_mod.facts_as_of(conn, cutoff, **filters)
    obs = sorted(rows, key=lambda r: r["event_at"] or "", reverse=True)  # newest first
    return [(r["event_at"], r["value_num"]) for r in obs]


def _cmp(value: float, op: str, target: float) -> bool:
    return OPS[op](value, target)


def evaluate_atom(conn, atom: dict, cutoff) -> tuple[str, dict]:
    """-> ("TRUE"|"FALSE"|"UNKNOWN", detail). `detail` always carries a
    human-readable "message" plus enough structure (observed/required counts,
    latest values) for `render_impact` and for `thesis_reviews.evidence_json`."""
    kind = atom["kind"]
    subject, metric = atom["subject"], atom["metric"]
    obs = _observations(conn, atom, cutoff)

    if kind == "threshold":
        if not obs or obs[0][1] is None:
            return "UNKNOWN", {"observed": len(obs), "required": 1, "message": f"{subject} {metric} 관측 없음"}
        latest_at, latest_val = obs[0]
        ok = _cmp(latest_val, atom["op"], atom["value"])
        # A bare comparison reads as an assertion. Printed without its truth
        # value, a FALSE atom became the sentence "최신값 4.68 <= 3" — a false
        # statement published as the reason for a verdict (final-review F1).
        msg = (f"{subject} {metric} 최신값 {latest_val:g}"
               f" (조건 {atom['op']} {atom['value']:g} {'충족' if ok else '미충족'})")
        return ("TRUE" if ok else "FALSE"), {
            "observed": len(obs), "latest_value": latest_val, "latest_event_at": latest_at, "message": msg,
        }

    if kind == "change_pct":
        lookback = atom["lookback"]
        required = lookback + 1
        if len(obs) < required:
            return "UNKNOWN", {
                "observed": len(obs), "required": required,
                "message": f"{subject} {metric} 관측 부족({len(obs)}개, 필요 {required}개)",
            }
        latest_val = obs[0][1]
        base_val = obs[lookback][1]
        if latest_val is None or base_val is None or base_val == 0:
            return "UNKNOWN", {
                "observed": len(obs), "required": required,
                "message": f"{subject} {metric} 값 없음 또는 기준값 0",
            }
        pct = (latest_val - base_val) / abs(base_val) * 100
        ok = _cmp(pct, atom["op"], atom["value"])
        msg = (f"{subject} {metric} {lookback}구간 전 대비 {pct:.2f}%"
               f" (조건 {atom['op']} {atom['value']:g}% {'충족' if ok else '미충족'})")
        return ("TRUE" if ok else "FALSE"), {"observed": len(obs), "change_pct": pct, "message": msg}

    if kind == "consecutive":
        periods = atom["periods"]
        direction = atom["direction"]
        required = periods + 1
        if len(obs) < required:
            return "UNKNOWN", {
                "observed": len(obs), "required": required,
                "message": f"{subject} {metric} 관측 부족({len(obs)}개, 필요 {required}개)",
            }
        ok = True
        for i in range(periods):
            a, b = obs[i][1], obs[i + 1][1]
            if a is None or b is None:
                return "UNKNOWN", {
                    "observed": len(obs), "required": required,
                    "message": f"{subject} {metric} 구간에 결측값 있음",
                }
            step_ok = (a > b) if direction == "up" else (a < b)
            if not step_ok:
                ok = False
                break
        msg = f"{subject} {metric} 최근 {periods}구간 연속 {direction} {'확인' if ok else '아님'}"
        return ("TRUE" if ok else "FALSE"), {"observed": len(obs), "message": msg}

    if kind == "stale":
        days = atom["days"]
        if not obs:
            return "UNKNOWN", {"observed": 0, "required": 1, "message": f"{subject} {metric} 관측 없음"}
        latest_at = obs[0][0]
        cutoff_dt = datetime.fromisoformat(db_mod.iso_utc(cutoff))
        latest_dt = datetime.fromisoformat(db_mod.iso_utc(latest_at))
        threshold_dt = cutoff_dt - timedelta(days=days)
        ok = latest_dt < threshold_dt
        msg = f"{subject} {metric} 최신 관측 {latest_at[:10]}, 기준일 {days}일"
        return ("TRUE" if ok else "FALSE"), {"observed": len(obs), "latest_event_at": latest_at, "message": msg}

    raise ValueError(f"evaluate_atom: unknown atom kind {kind!r}")  # pragma: no cover — load_file already rejects this


# ---------------------------------------------------------------------------
# Verdict (spec SA-7's 5-step, order fixed)
# ---------------------------------------------------------------------------

def _eval_group(conn, atoms: list[dict], cutoff) -> list[dict]:
    out = []
    for atom in atoms:
        status, detail = evaluate_atom(conn, atom, cutoff)
        out.append({"atom": atom, "status": status, "detail": detail})
    return out


def review(conn, theses: list[dict], cutoff, report_type: str, report_date: str) -> list[dict]:
    """Evaluates every thesis's conditions as of `cutoff` and returns rows
    ready for `store.record_reviews` (already carries serialized
    atoms_json/evidence_json) plus display fields for `render_impact`/
    `render_next_check_suffix`. Import `store` lazily to avoid a cycle since
    `store` never needs to import this module."""
    from . import store as store_mod

    cutoff_str = db_mod.iso_utc(cutoff)
    results = []
    for th in theses:
        conditions = th["conditions"]
        falsify_ev = _eval_group(conn, conditions.get("falsify", []), cutoff)
        weaken_ev = _eval_group(conn, conditions.get("weaken", []), cutoff)
        strengthen_ev = _eval_group(conn, conditions.get("strengthen", []), cutoff)
        all_ev = falsify_ev + weaken_ev + strengthen_ev

        if all_ev and all(e["status"] == "UNKNOWN" for e in all_ev):
            verdict = "판정 불가"
        elif falsify_ev and all(e["status"] == "TRUE" for e in falsify_ev):
            verdict = "무효"
        elif any(e["status"] == "TRUE" for e in weaken_ev):
            verdict = "약화"
        elif any(e["status"] == "TRUE" for e in strengthen_ev):
            verdict = "강화"
        else:
            verdict = "유지"

        prev_verdict = store_mod.last_verdict(conn, th["thesis_id"])
        changed = 1 if (prev_verdict is not None and prev_verdict != verdict) else 0

        evaluable = sum(1 for e in all_ev if e["status"] != "UNKNOWN")
        atoms_payload = {
            "falsify": [{"id": e["atom"].get("id"), "status": e["status"], "detail": e["detail"]} for e in falsify_ev],
            "weaken": [{"id": e["atom"].get("id"), "status": e["status"], "detail": e["detail"]} for e in weaken_ev],
            "strengthen": [{"id": e["atom"].get("id"), "status": e["status"], "detail": e["detail"]} for e in strengthen_ev],
        }
        evidence_payload = [
            {"subject": e["atom"]["subject"], "metric": e["atom"]["metric"], "status": e["status"], **e["detail"]}
            for e in all_ev
        ]

        results.append(
            {
                "thesis_id": th["thesis_id"], "theme": th["theme"], "slot": th["slot"],
                "statement": th["statement"], "next_check_date": th.get("next_check_date"),
                "report_type": report_type, "report_date": report_date, "cutoff_utc": cutoff_str,
                "verdict": verdict, "prev_verdict": prev_verdict, "changed": changed,
                "atoms": atoms_payload, "atoms_json": json.dumps(atoms_payload, ensure_ascii=False),
                "evidence_json": json.dumps(evidence_payload, ensure_ascii=False),
                "total_atoms": len(all_ev), "evaluable_atoms": evaluable,
                "all_evals": falsify_ev + weaken_ev + strengthen_ev,
                # Which group each verdict came from, so the printed reason can
                # cite the atom that actually decided it. `all_evals` alone
                # loses that: it is a flat concatenation, and reading the first
                # entry off it published "강화 — DGS10 최신값 4.68 <= 3" — the
                # verdict was right but the sentence cited a falsify condition
                # that had NOT fired, stated as though it had (final-review F1).
                "evals_by_group": {
                    "falsify": falsify_ev, "weaken": weaken_ev, "strengthen": strengthen_ev,
                },
            }
        )
    return results


# ---------------------------------------------------------------------------
# Rendering (spec SA-8 — fixed template shape for Interpretation.thesis_impact
# and the next_check suffix)
# ---------------------------------------------------------------------------

_VERDICT_GROUP = {"무효": "falsify", "약화": "weaken", "강화": "strengthen"}


def _first_reason(row: dict) -> str:
    """The condition that actually produced this verdict — never merely the
    first one in the list.

    A verdict is caused by a TRUE atom in exactly one group: 무효 by falsify,
    약화 by weaken, 강화 by strengthen. Reading `all_evals[0]` instead printed
    whichever atom happened to come first, which is always a falsify atom, and
    printed it whether it was true or false: the published line read
    "강화 — DGS10 value 최신값 4.68 <= 3" (final-review F1). The verdict was
    correct; the sentence was arithmetically false."""
    group = _VERDICT_GROUP.get(row["verdict"])
    evals = (row.get("evals_by_group") or {}).get(group, []) if group else []
    fired = [e for e in evals if e["status"] == "TRUE"]
    if fired:
        return " · ".join(e["detail"]["message"] for e in fired)
    # No group matched (or the row predates evals_by_group): fall back to any
    # atom that is actually true, never to one that is false or unknown.
    for e in row.get("all_evals", []):
        if e["status"] == "TRUE":
            return e["detail"]["message"]
    return "근거 없음"


def render_impact(reviews: list[dict]) -> str:
    if not reviews:
        return ""
    counts = {"강화": 0, "유지": 0, "약화": 0, "무효": 0, "판정 불가": 0}
    for r in reviews:
        counts[r["verdict"]] += 1
    header = (
        f"가설 {len(reviews)}건 판정 — 강화 {counts['강화']} · 유지 {counts['유지']} · "
        f"약화 {counts['약화']} · 무효 {counts['무효']} · 판정 불가 {counts['판정 불가']}."
    )
    lines = [header]
    for r in reviews:
        label = THEME_LABELS.get(r["theme"], r["theme"])
        if r["verdict"] == "판정 불가":
            reason = r["all_evals"][0]["detail"]["message"] if r["all_evals"] else "근거 없음"
            reason = f"관측 부족({reason})"
        elif r["verdict"] == "유지":
            reason = f"발화 조건 없음(평가 가능 {r['evaluable_atoms']}/{r['total_atoms']})"
        else:
            reason = _first_reason(r)
        lines.append(f"[{label} #{r['slot']}] {r['verdict']} — {reason}.")
    return "\n".join(lines)


def render_next_check_suffix(reviews: list[dict], report=None) -> str:
    """SA-8: "확인 일정: 2026-08-07 Employment Situation(A) · ... · 가설 재점검
    2026-11-01" — this returns everything *after* the "확인 일정: " prefix
    (the prefix itself is `apply.py`'s / ST2's job to prepend).

    [ASSUMPTION] the spec's own example shows 2 upcoming A급 calendar events;
    it does not pin an exact selection count, so this picks the 2 nearest
    A-importance events from `report.events` when a report is supplied."""
    parts: list[str] = []
    if report is not None:
        events = [e for e in getattr(report, "events", []) if getattr(e, "importance", None) == "A"]
        for e in events[:2]:
            when = (e.when or "")[:10]
            parts.append(f"{when} {e.name}({e.importance})")

    if reviews:
        earliest = min(r["next_check_date"] for r in reviews if r.get("next_check_date"))
        parts.append(f"가설 재점검 {earliest}")

    return " · ".join(parts)
