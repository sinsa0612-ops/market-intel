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

ENGINE_VERSION = "2b.3"

# 사람이 읽는 "무엇이 언제 바뀌었는가" 기록 (명세 §2 "지문/엔진버전 함정의 해법").
# `"2b.2"` 항목은 **소급 표시가 아니다** — 그 구간의 행에는 실제로 `"2b.1"`이
# 찍혀 있다(2026-08-12 이전엔 `store.record_reviews`가 이 모듈이 아니라
# `store.ENGINE_VERSION`을 찍었고, 그 값도 계속 `"2b.1"`이었다). 이 레지스트리는
# 그 판정 코드가 실제로 무엇을 뜻했는지의 인간용 각주다.
ENGINE_SEMANTICS = {
    "2b.1": "판정 5종. 근거 날짜·지속 표시 없음 (~2026-08-11).",
    "2b.2": "근거 날짜(latest_at)와 연속 관측 수(streak)를 detail에 기록 (2026-08-12~).",
    "2b.3": "상태 전이 파생 뷰 도입. '강화 N회' 대신 진입일·지속·신규관측 표시 (transition_rules_v1).",
}

THEMES = {"ai_semi", "power_energy", "fin_credit", "consumer_cycle", "policy_geo"}

THEME_LABELS = {
    "ai_semi": "AI·반도체",
    "power_energy": "전력·에너지",
    "fin_credit": "금융·신용",
    "consumer_cycle": "소비·경기",
    "policy_geo": "정책·지정학",
}

# `consecutive` = 값이 **매 구간 전보다 강해지는가**(단조 변화). 분기 재무에는 이것이
# 옳다 — "MSFT 영업이익 2분기 연속 증가"는 매 분기가 직전 분기보다 커야 참이다.
#
# `consecutive_sign` = 값의 **부호가 N구간 유지되는가**. 부호 있는 일간 흐름(순매수/
# 순매도)에는 이쪽이 옳다. 2026-08-12에 이 종류가 없어서 실제로 사고가 났다:
# `hynix_foreign_5d_sell`("하이닉스 외국인 5일 연속 순매도")이 `consecutive/down`으로
# 적혀 있어 코드가 "매일 전날보다 **더 많이** 팔 것"을 요구했다. 실측 8/6 -1.68조 ·
# 8/7 -667억 · 8/10 -4,088억 · 8/11 -2,998억 — 4거래일 연속 순매도(-2.45조)인데
# 8/11이 8/10보다 덜 팔았다는 이유로 연속이 1에서 끊겼다. 매일 1조씩 한 달을 팔아도
# 발화하지 않는, 사실상 죽은 반증 조건이었다.
#
# 기존 `consecutive`의 의미는 **일부러 그대로 둔다**: 재무에 걸린 조건들(MSFT 영업이익,
# EQIX 매출, 소매판매)은 단조 변화가 맞는 뜻이고, 의미를 바꾸면 그 가설들의 지문과
# 과거 판정이 통째로 흔들린다. 새 종류를 더해 흐름 조건만 옮기면 파급이 그 한 줄이다.
#
# `relative_change_pct` = 같은 기간 **기준 지수 대비** 초과/미달(%p). 2026-08-12
# 감사에서 `ai_semi_1`의 반증·약화 조건(SOX 절대 -30%/-15%)이 2년 내내 문턱
# 근접도 0%로 나온 뒤 더했다 — 주도력은 절대 낙폭이 아니라 시장 대비로 먼저
# 무너진다. `benchmark`(비교 대상 subject)를 함께 적는다.
ATOM_KINDS = {"threshold", "change_pct", "consecutive", "consecutive_sign",
              "relative_change_pct", "stale"}
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
    if kind in ("threshold", "change_pct", "relative_change_pct"):
        op = atom.get("op")
        if op not in OPS:
            reasons.append(f"{where}: 알 수 없는 연산자(op)={op!r} (허용: {sorted(OPS)})")
        if "value" not in atom:
            reasons.append(f"{where}: value가 없음")
    if kind in ("change_pct", "relative_change_pct") and "lookback" not in atom:
        reasons.append(f"{where}: lookback이 없음")
    if kind == "relative_change_pct" and not atom.get("benchmark"):
        # 기준이 없으면 "무엇 대비"가 없다 — 조용히 절대 등락으로 떨어지면
        # 문턱의 뜻이 통째로 달라진다(-10%p 초과미달 vs -10% 절대 하락).
        reasons.append(f"{where}: benchmark가 없음(상대강도 조건은 비교 대상이 필수)")
    if kind in ("consecutive", "consecutive_sign"):
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

def _dominant_basis(rows) -> str | None:
    """재무 관측을 **하나의 기간 길이**로 통일한다 — 가장 많은 기간을 덮는 쪽,
    같으면 짧은 쪽(분기).

    재무 atom은 전부 구간을 이어서 읽는다("최근 2구간 연속 down"). 기간 길이가
    섞이면 그 비교는 성립하지 않는다: 1년치 누적 하나가 분기 자리에 들어오면
    직전 분기 대비 3~4배로 뛰어 **없던 방향이 생긴다**. 실측(2026-08-03)으로
    MSFT free_cash_flow의 최신 관측이 66,987,000,000(연간)이었고 그 앞은
    15,803,000,000(분기)였다 — 같은 fact_id를 공유하는 탓이다(`db._IDENTITY_FILTERS`).

    quarterly로 못박지 않는 이유: DART가 주는 한국 기업 재무와 TSM(20-F)은
    **연간밖에 없다**. 못박았다면 그 기업들의 가설이 영구히 판정 불가가 된다
    (실측: 005930.KS·000660.KS·005380.KS·005490.KS·105560.KS·TSM·JPM revenue).
    `None`은 관측이 없다는 뜻이고, 그때는 다시 읽지 않는다."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["comparison_basis"] or ""] = counts.get(r["comparison_basis"] or "", 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda b: (counts[b], b == "quarterly"))


def _observations(conn, atom: dict, cutoff) -> list[tuple[str, float | None]]:
    filters = {"subject": atom["subject"], "metric": atom["metric"]}
    if atom.get("category"):
        filters["category"] = atom["category"]
    rows = db_mod.facts_as_of(conn, cutoff, **filters)
    if filters.get("category") == "financials":
        basis = _dominant_basis(rows)
        if basis is not None:
            rows = db_mod.facts_as_of(conn, cutoff, **filters, comparison_basis=basis)
    obs = sorted(rows, key=lambda r: r["event_at"] or "", reverse=True)  # newest first
    return [(r["event_at"], r["value_num"]) for r in obs]


def _cmp(value: float, op: str, target: float) -> bool:
    return OPS[op](value, target)


def evaluate_atom(conn, atom: dict, cutoff) -> tuple[str, dict]:
    """-> ("TRUE"|"FALSE"|"UNKNOWN", detail).

    `_evaluate_atom`이 실제 판정을 하고, 이 함수는 거기에 **근거의 날짜**를
    붙인다(CEO 지시 2026-08-12). 분기마다 손으로 넣지 않고 한 곳에서 감싸는
    이유는, 판정 경로가 다섯 갈래인데 한 갈래만 빠뜨려도 그 가설만 조용히
    날짜 없이 발행되기 때문이다.

    왜 날짜가 필요한가: 판정은 매일 다시 계산되지만 근거는 그렇지 않다.
    `ai_semi_1`의 근거인 MSFT 영업이익은 최신치가 2026-06-30이고 다음 값은
    10월 말에 온다 — 그 사이 2.5개월 동안 시장이 무엇을 하든 매일 똑같은
    "강화"가 나온다. 날짜를 같이 보여 주면 CEO가 "오늘 새로 안 것"과 "석 달
    전 사실의 재확인"을 구별할 수 있다.

    관측은 **여기서 한 번만 읽어** 안쪽으로 넘긴다. 날짜를 붙이겠다고 한 번 더
    읽으면 같은 차단선을 두 번 조회하게 되고, 그 불변식을 지키는 테스트가
    있다(`test_evaluate_atom_never_bypasses_facts_as_of` — 실제로 이 변경에서
    걸렸다). 읽기가 하나여야 판정과 날짜가 **같은 관측 집합**에서 나온다.
    """
    obs = _observations(conn, atom, cutoff)
    # 상대강도 조건만 두 번 읽는다 — 두 종목을 견주는 것이 정의이기 때문이다.
    # 나머지는 한 번이고, 그 불변식은 위 주석의 테스트가 지킨다.
    benchmark_obs = None
    if atom.get("benchmark"):
        bench_atom = dict(atom, subject=atom["benchmark"])
        benchmark_obs = _observations(conn, bench_atom, cutoff)
    verdict, detail = _evaluate_atom(atom, obs, cutoff, benchmark_obs)
    if obs and obs[0][0]:
        detail.setdefault("latest_at", obs[0][0])
    return verdict, detail


def _evaluate_atom(atom: dict, obs: list[tuple[str, float | None]], cutoff,
                   benchmark_obs: list[tuple[str, float | None]] | None = None) -> tuple[str, dict]:
    """`detail` always carries a human-readable "message" plus enough structure
    (observed/required counts, latest values) for `render_impact` and for
    `thesis_reviews.evidence_json`."""
    kind = atom["kind"]
    subject, metric = atom["subject"], atom["metric"]

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

    if kind == "relative_change_pct":
        # 상대강도: 같은 기간 **기준 지수보다 얼마나 더/덜 갔는가**(%p).
        #
        # 왜 필요했나(2026-08-12 감사): 기존 조건은 전부 단일 종목의 절대
        # 등락이라 "얼마나 빠졌나"만 물었다. 그 결과 `ai_semi_1`의 반증·약화
        # 조건(SOX -30%/-15%)이 2년 내내 **문턱 근접도 0%** — 근처에도 못 갔다.
        # 주도력은 절대 낙폭이 아니라 **시장 대비**로 먼저 무너진다. 반도체가
        # 시장과 같이 오르내리기만 해도 "주도한다"는 주장은 이미 약해진 것이다.
        lookback = atom["lookback"]
        required = lookback + 1
        if len(obs) < required or len(benchmark_obs or []) < required:
            return "UNKNOWN", {
                "observed": min(len(obs), len(benchmark_obs or [])), "required": required,
                "message": (f"{subject} 또는 기준 {atom.get('benchmark')} 관측 부족"
                            f"({len(obs)}/{len(benchmark_obs or [])}개, 필요 {required}개)"),
            }
        pairs = []
        for series in (obs, benchmark_obs):
            latest_val, base_val = series[0][1], series[lookback][1]
            if latest_val is None or base_val is None or base_val == 0:
                return "UNKNOWN", {
                    "observed": len(obs), "required": required,
                    "message": f"{subject} 또는 기준 지수 값 없음 또는 기준값 0",
                }
            pairs.append((latest_val - base_val) / abs(base_val) * 100)
        subject_pct, bench_pct = pairs
        spread = subject_pct - bench_pct
        ok = _cmp(spread, atom["op"], atom["value"])
        msg = (f"{subject} {lookback}구간 {subject_pct:+.2f}% vs 기준 "
               f"{atom.get('benchmark')} {bench_pct:+.2f}% -> 격차 {spread:+.2f}%p"
               f" (조건 {atom['op']} {atom['value']:g}%p {'충족' if ok else '미충족'})")
        return ("TRUE" if ok else "FALSE"), {
            "observed": len(obs), "change_pct": subject_pct,
            "benchmark_change_pct": bench_pct, "spread_pp": spread, "message": msg}

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

    if kind == "consecutive_sign":
        # 부호 유지. `consecutive`와 달리 **직전 값과 비교하지 않으므로** 기준점이
        # 필요 없다 — N구간이면 관측 N개로 충분하다(단조 쪽은 N+1개가 필요하다).
        periods = atom["periods"]
        direction = atom["direction"]
        if len(obs) < periods:
            return "UNKNOWN", {
                "observed": len(obs), "required": periods,
                "message": f"{subject} {metric} 관측 부족({len(obs)}개, 필요 {periods}개)",
            }
        window = obs[:periods]
        if any(v is None for _at, v in window):
            return "UNKNOWN", {
                "observed": len(obs), "required": periods,
                "message": f"{subject} {metric} 구간에 결측값 있음",
            }
        # 0은 어느 쪽 부호도 아니다 — 순매수도 순매도도 아닌 날을 "연속"에
        # 끼워 주면 흐름이 끊긴 것을 끊기지 않은 것으로 읽는다.
        ok = all((v > 0) if direction == "up" else (v < 0) for _at, v in window)
        word = "순매수" if direction == "up" else "순매도"
        total = sum(v for _at, v in window)
        msg = (f"{subject} {metric} 최근 {periods}구간 연속 {word} "
               f"{'확인' if ok else '아님'}"
               + (f"(합계 {total:,.0f})" if ok else ""))
        return ("TRUE" if ok else "FALSE"), {
            "observed": len(obs), "sum": total, "message": msg}

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

# 지속 일수를 되짚을 때 걸어 볼 최대 관측 수. 2년 백필이 ~500관측이므로 그보다
# 넉넉히 잡되 무한정 걷지는 않는다 — 넘어가면 "이상"으로 표시한다.
_STREAK_SCAN_LIMIT = 800


def _streak(atom: dict, obs: list[tuple[str, float | None]], cutoff,
            benchmark_obs: list[tuple[str, float | None]] | None = None) -> dict:
    """이 조건이 **몇 관측째 연속으로 참인가**, 그리고 처음 참이 된 날.

    왜 필요한가 (CEO 지시 2026-08-12): 수준 조건은 상태가 유지되는 한 매일 참이라
    매일 "강화"를 찍는다. 실측 — `DGS10 >= 4.5`는 19/19회, `환율 >= 1400`은 13/13회,
    새로 넣은 `DFII10 >= 1.05`는 **505/505일** 전부 강화였다. CEO는 매일 "강화 6건"을
    읽지만 그중 **새로 알게 된 것은 0건**이다. 확증편향이 심리가 아니라 조건 설계로
    제조되는 자리다.

    **판정은 바꾸지 않는다.** "지금 긴축적이다"는 참이고 그 정보를 버리면 안 된다 —
    바꾸면 과거 판정과 비교도 끊긴다. 대신 그 옆에 "오늘 새로 넘었나, 505일째인가"를
    적어 독자가 news와 state를 구별하게 한다(사외 고문 2인의 처방이 여기서 갈렸는데,
    GPT는 판정 강등, fable은 메타데이터를 권했다 — 정보를 버리지 않는 쪽을 골랐다).

    되짚기는 관측 단위이지 달력 단위가 아니다. 일간 계열이면 거래일, 분기 재무면
    분기다 — 그래서 반환 필드 이름도 `periods`다.
    """
    if not obs:
        return {}
    bench_all = benchmark_obs
    n = 0
    first_at = ""
    for i in range(min(len(obs), _STREAK_SCAN_LIMIT)):
        window = obs[i:]
        bench_window = None
        if bench_all is not None:
            at = obs[i][0]
            bench_window = [(a, v) for a, v in bench_all if a and a <= at]
            if not bench_window:
                break
        try:
            status, _ = _evaluate_atom(atom, window, cutoff, bench_window)
        except (KeyError, ValueError, TypeError):
            break
        if status != "TRUE":
            break
        n += 1
        first_at = obs[i][0]
    if n == 0:
        return {}
    return {"periods": n, "since": (first_at or "")[:10],
            "capped": n >= min(len(obs), _STREAK_SCAN_LIMIT)}


def _eval_group(conn, atoms: list[dict], cutoff) -> list[dict]:
    out = []
    for atom in atoms:
        status, detail = evaluate_atom(conn, atom, cutoff)
        if status == "TRUE":
            # 참인 조건에만 계산한다 — 거짓 조건의 "지속"은 화면에 안 나가므로
            # 되짚을 이유가 없고, 조건 39개를 전부 되짚으면 리포트가 느려진다.
            obs = _observations(conn, atom, cutoff)
            bench = (_observations(conn, dict(atom, subject=atom["benchmark"]), cutoff)
                     if atom.get("benchmark") else None)
            streak = _streak(atom, obs, cutoff, bench)
            if streak:
                detail["streak"] = streak
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

        # 이 판정을 만든 가설 판의 지문. 직전 판정과 다르면 **기준이 바뀐 뒤의
        # 첫 판정**이므로, 그 전후 판정은 서로 비교할 수 없다. 기록해 두지
        # 않으면 원장이 "강화 → 강화"만 보여주고 골대가 움직인 사실이 사라진다
        # (CEO 2026-08-04 "목표치 재설정" 논의).
        rules_sha256 = th.get("rules_sha256") or ""
        prev_rules = store_mod.last_rules_sha256(conn, th["thesis_id"])
        rules_changed = 1 if (prev_rules and rules_sha256 and prev_rules != rules_sha256) else 0

        # `rules_changed`와 완전히 대칭: 조건이 아니라 **엔진 코드의 뜻**이 바뀐
        # 경계를 잡는다(명세 §2 "지문/엔진버전 함정의 해법"). `rules_fingerprint`는
        # statement·conditions만 보므로 판정 의미를 코드에서 바꿔도 지문은 안
        # 바뀐다 — 2026-08-12 detail.latest_at/streak 도입이 그렇게 흔적 없이
        # 지나갔다. 첫 판정(prev_engine=None)은 0.
        prev_engine = store_mod.last_engine_version(conn, th["thesis_id"])
        engine_changed = 1 if (prev_engine and prev_engine != ENGINE_VERSION) else 0

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
                "rules_sha256": rules_sha256, "rules_changed": rules_changed,
                "engine_version": ENGINE_VERSION, "engine_changed": engine_changed,
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


def _fired_atom_ids(row: dict, group: str) -> list[str]:
    """That group's TRUE atom ids, from whichever shape `row` carries them in
    — the in-memory `evals_by_group` shape `thesis.review()` returns
    (`{"atom": {"id": ...}, "status": ..., "detail": ...}`, used by
    `render_impact`'s callers straight off `review()`) or the persisted
    `atoms_json` shape `store.reviews_for_thesis`/`transitions.py` use
    (`{"id": ..., "status": ..., "detail": ...}`, flat — no nested `atom`
    key). `ops.thesis_overview` reads the persisted shape back off the
    ledger, so both must work here."""
    items = (row.get("evals_by_group") or {}).get(group)
    if items is not None:
        return [(e.get("atom") or {}).get("id") for e in items if e.get("status") == "TRUE" and (e.get("atom") or {}).get("id")]
    atoms = (row.get("atoms") or {}).get(group, [])
    return [a.get("id") for a in atoms if a.get("status") == "TRUE" and a.get("id")]


_RESTART_LABELS = {
    "기준 변경": "기준 변경 후 재시작",
    "의미 변경": "의미 변경 후 재시작",
}


def _run_fragment(run, ledger_start: str | None) -> str:
    """R6/R7/R9/R10/R11의 표시 형태 하나. `run`은 `transitions.Run`(또는
    같은 필드를 가진 값)이다 — 이 함수는 그 타입을 import하지 않고 덕타이핑만
    한다(엔진↔렌더러 분리, transitions.py를 새 의존으로 끌어오지 않는다)."""
    if run.left_censored:
        entry = f"기록 시작 {ledger_start} 이전부터" if ledger_start else "기록 시작 이전부터"
    else:
        entry = f"진입 {run.entry_date}"
        if run.restart_reason:
            reasons = " · ".join(
                _RESTART_LABELS.get(r, r) for r in run.restart_reason.split(" · "))
            entry += f"({reasons})"

    # R9: n=1이면 "지속 1판정일"이 아니라 "오늘 새로 충족" — 지속 조각이
    # 새 관측 조각과 나란히 있어야 한다는 규칙 자체는 여전히 지킨다(아래에서
    # new_obs 조각을 이어 붙인다).
    #
    # final-review F1/F2: duration_days<=1은 "오늘 조건이 새로 충족됐다"를
    # 뜻하지 않는다 — R5.2/R5.3(rules_changed/engine_changed)이 조건이 그대로여도
    # 구간을 강제 종료시키고, R11 좌측 절단도 구간을 1일짜리로 시작할 수 있다.
    # "오늘 새로 충족"은 원장 안에서 실제로 관측된 상태 전이(거짓/미지/없음 →
    # 참, R4)일 때만 쓴다 — 재시작이나 좌측 절단으로 열린 구간은 "지속
    # 1판정일"로 적고, entry 조각의 재시작 사유 표기가 이유를 말한다.
    is_real_transition = not run.left_censored and run.restart_reason is None
    duration = ("오늘 새로 충족" if run.duration_days <= 1 and is_real_transition
                else f"지속 {run.duration_days}판정일")

    # R7/R9: 지속일 옆에는 항상 새 관측 건수를 함께 적는다. 화면 문구는
    # "새 관측"이다 — "독립 증거"라고 쓰지 않는다(R8, 코드가 독립성을 판별할
    # 수 없다).
    #
    # final-review F3: `unknown_observation_days>0`이면서 `new_observation_count
    # ==0`이면 알려진 새 관측이 하나도 없다는 뜻이므로 R7이 요구하는 "미상"을
    # 그대로 낸다 — 건수 0을 "0건 이상"으로 감싸 아무 말도 안 하는 문장을
    # 만들지 않는다. 일부만 미상이면(새 관측이 이미 몇 건 확인됐으면) 그 건수를
    # 그대로 적고 "일부 판정일 미상"이라고만 말한다 — 왜 미상인지(원장에 그
    # 필드가 없던 시기인지, 그날 원자 자체가 없었는지)는 확인한 적이 없으므로
    # 특정 원인을 주장하지 않는다.
    if run.unknown_observation_days > 0 and run.new_observation_count == 0:
        new_obs = "새 관측 미상"
    elif run.unknown_observation_days > 0:
        new_obs = f"새 관측 {run.new_observation_count}건 이상(그 외 일부 판정일 미상)"
    elif run.new_observation_count == 0:
        new_obs = "새 관측 없음(이 구간)"
    else:
        last = f", 마지막 {run.last_new_observation_date}" if run.last_new_observation_date else ""
        new_obs = f"새 관측 {run.new_observation_count}건{last}"

    frag = f"{entry} · {duration} · {new_obs}"
    if run.has_data_gap:  # R10
        frag += " · 기록 공백 포함"
    return frag


def state_fragment(row: dict, state: dict | None) -> str:
    """그 판정을 만든 원자들 중 파생 뷰가 있는 것을 골라 진입·지속·새 관측
    조각을 만든다(R6/R7/R9/R11). `state`는 `transitions.thesis_view()`의
    반환값(또는 None — 파생 뷰가 없으면 빈 문자열, 없는 정보를 지어내지 않는다).

    여러 원자가 발화했으면 **가장 최근에 넘은 것**을 고른다(지속 판정일이
    가장 짧은 구간) — 옛 `_streak` 기반 로직의 "여럿이 발화하면 가장 최근에
    넘은 것을 말한다"와 같은 선택 규칙이다. 판정을 바꾼 사건이 그것이고,
    나머지는 이미 참이던 상태이기 때문이다."""
    if state is None:
        return ""
    group = _VERDICT_GROUP.get(row.get("verdict"))
    if not group:
        return ""
    atoms = state.get("atoms") or {}
    candidates = []
    for atom_id in _fired_atom_ids(row, group):
        runs = atoms.get(atom_id)
        if not runs:
            continue
        current = runs[-1]
        if current.status == "TRUE":
            candidates.append(current)
    if not candidates:
        return ""
    run = min(candidates, key=lambda r: (r.duration_days, r.atom_id))
    return _run_fragment(run, state.get("ledger_start"))


def _evidence_note(row: dict, report_date: str = "", state: dict | None = None) -> str:
    """판정을 만든 근거가 **언제 것인지**, 그리고(있으면) **얼마나 새 소식인지**
    한 마디로. 없으면 빈 문자열.

    `state`가 없으면(예: 옛 호출부, 파생 뷰를 못 만드는 상황) 상태 조각
    자체를 생략한다 — 없는 정보를 지어내지 않는다(ST4 성공 기준). `근거
    {date}` 조각은 `state`와 무관하게 그대로 유지한다.

    근거 날짜는 발화한 원자들의 `latest_at` 중 **가장 오래된 것**을 쓴다:
    조건이 여럿이면 판정은 그 전부가 참이어야 서므로, 가장 묵은 근거가 이
    판정의 나이다. 최신 것을 쓰면 오래된 근거가 새 근거 뒤에 숨는다.
    """
    group = _VERDICT_GROUP.get(row["verdict"])
    if not group:
        # 유지·판정 불가에는 **판정을 만든 조건이 없다.** 여기서 아무 참인 원자나
        # 주워 날짜를 붙이면 "유지 — 발화 조건 없음 (근거 8/11)"처럼 자기 자신에
        # 대해 거짓말하는 문장이 된다(실제로 ai_semi_3에서 나왔다: 반증 그룹의
        # `sox_60d_up`을 근거로 집었다).
        return ""
    evals = (row.get("evals_by_group") or {}).get(group, [])
    fired = [e for e in evals if e["status"] == "TRUE"]

    parts = []
    # **새 소식인가, 이어지는 상태인가.** 이것이 앞에 온다 — 독자가 먼저 알아야 할
    # 것은 "오늘 뭐가 달라졌나"이지 근거의 날짜가 아니다.
    frag = state_fragment(row, state)
    if frag:
        parts.append(frag)

    dates = sorted(
        str(e["detail"]["latest_at"])[:10]
        for e in fired if (e.get("detail") or {}).get("latest_at"))
    if dates:
        oldest = dates[0]
        parts.append("근거 오늘" if (report_date and oldest == report_date[:10])
                     else f"근거 {oldest}")
    return " · ".join(parts)


# --- 조건 발화 감사 (CEO 지시 2026-08-12) ----------------------------------
#
# 왜 이 코드가 있는가: 2026-08-12에 반증 조건 하나가 **발화 자체가 사실상
# 불가능한 상태**로 발견됐다(`hynix_foreign_5d_sell` — 단조 감소를 요구해
# "매일 전날보다 더 많이 팔 것"이 됐다). 그런데 그것을 찾은 것은 검사 장치가
# 아니라 **우연**이었다. 우연이 QA를 대신하는 한, 나머지 조건 중 몇 개가 더
# 죽어 있는지 아무도 모른다 — 사외 고문 2인이 독립적으로 이 검사를 첫손에
# 꼽았다(2026-08-12).
#
# 검사는 두 갈래이고, **둘 다 필요하다**:
#
#  1. 도달 가능성(`_reachable`) — 합성 관측을 넣어 이 조건이 논리적으로 TRUE가
#     될 수 있는지. 연산자 오타나 방향 뒤집힘처럼 **어떤 데이터로도 참이 될 수
#     없는** 조건을 잡는다. 값싸고 데이터가 필요 없다.
#  2. 실이력 소급(`_replay`) — 실제 관측 이력을 되짚어 이 조건이 **한 번이라도
#     발화한 적이 있는지**. 오늘 잡은 그 버그는 (1)로는 안 잡힌다: 단조 감소도
#     논리적으로는 가능하기 때문이다. 현실에서 안 일어날 뿐이다. 그래서 이쪽이
#     본체다.
#
# ⚠️ 소급은 **오늘의 개정본으로 과거를 되짚는다.** 그날 알려져 있던 값이 아니라
# 지금 아는 값을 쓴다 — 즉 이것은 과거 판정의 재현이 아니라 "이 조건이 이런
# 모양의 데이터에서 발화할 수 있는가"에 대한 답이다. 리포트 계층의 차단선
# 규칙과는 목적이 다르므로 섞어 쓰지 말 것.

_AUDIT_BASE = "2026-01-01T00:00:00+00:00"


def _synthetic_obs(atom: dict) -> list[tuple[str, float]] | None:
    """이 조건을 TRUE로 만들도록 **설계된** 관측열. 만들 수 없으면 None."""
    kind = atom["kind"]
    day = lambda i: f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00"  # noqa: E731

    if kind == "threshold":
        op, target = atom["op"], float(atom["value"])
        probe = {">": target + 1, ">=": target, "<": target - 1, "<=": target,
                 "==": target, "!=": target + 1}.get(op)
        return None if probe is None else [(day(0), probe)]

    if kind == "change_pct":
        op, target, lb = atom["op"], float(atom["value"]), int(atom["lookback"])
        # ⚠️ 경계값을 **정확히** 겨냥하면 안 된다. 판정은 `latest`에서 변화율을
        # 다시 계산하는데 `100 * 1.15 = 114.99999999999999`이라 14.999...%가 나와
        # `>= 15`를 못 넘긴다. 그러면 멀쩡한 조건이 "발화 불가"로 잘못 신고된다
        # (이 감사를 처음 돌렸을 때 실제로 2건이 그렇게 나왔다). 만족하는 쪽으로
        # 아주 조금 밀어 넣는다 — 도달 가능성만 보면 되므로 경계에 붙을 이유가 없다.
        nudge = max(abs(target), 1.0) * 1e-6
        pct = {">": target + 1, ">=": target + nudge, "<": target - 1,
               "<=": target - nudge, "==": target, "!=": target + 1}.get(op)
        if pct is None:
            return None
        base = 100.0
        latest = base * (1 + pct / 100)
        # newest first; 사이 값은 판정에 안 쓰이지만 개수는 맞춰야 한다.
        return [(day(0), latest)] + [(day(i), base) for i in range(1, lb + 1)]

    if kind == "relative_change_pct":
        # 기준은 제자리에 두고 대상만 움직여 격차를 만든다.
        op, target, lb = atom["op"], float(atom["value"]), int(atom["lookback"])
        nudge = max(abs(target), 1.0) * 1e-6
        spread = {">": target + 1, ">=": target + nudge, "<": target - 1,
                  "<=": target - nudge, "==": target, "!=": target + 1}.get(op)
        if spread is None:
            return None
        base = 100.0
        return [(day(0), base * (1 + spread / 100))] + [(day(i), base) for i in range(1, lb + 1)]

    if kind in ("consecutive", "consecutive_sign"):
        periods, direction = int(atom["periods"]), atom["direction"]
        n = periods + 1 if kind == "consecutive" else periods
        if kind == "consecutive":
            # newest first, 매 구간 직전보다 강해지도록.
            vals = [float(n - i) for i in range(n)] if direction == "up" \
                else [float(i) for i in range(n)]
        else:
            vals = [1.0] * n if direction == "up" else [-1.0] * n
        return [(day(i), v) for i, v in enumerate(vals)]

    if kind == "stale":
        return [("2020-01-01T00:00:00+00:00", 1.0)]
    return None


def _flat_benchmark(atom: dict, obs: list) -> list | None:
    """상대강도 조건의 짝. 기준을 **제자리(변화 0%)**로 두면 격차가 곧 대상의
    변화율이 되어, `_synthetic_obs`가 만든 값이 그대로 문턱을 겨냥한다."""
    if not atom.get("benchmark"):
        return None
    return [(at, 100.0) for at, _v in obs]


def _reachable(atom: dict) -> bool:
    obs = _synthetic_obs(atom)
    if obs is None:
        return False
    cutoff = datetime.fromisoformat(_AUDIT_BASE) + timedelta(days=365 * 3)
    try:
        status, _ = _evaluate_atom(atom, obs, cutoff, _flat_benchmark(atom, obs))
    except (KeyError, ValueError, TypeError):
        return False
    return status == "TRUE"


def _replay(atom: dict, obs_all: list[tuple[str, float | None]],
            bench_all: list[tuple[str, float | None]] | None = None) -> list[str]:
    """관측 이력을 하루씩 되짚으며 TRUE였던 날짜들. `obs_all`은 최신순.

    상대강도 조건은 기준 계열도 **같은 날짜로 잘라** 넘긴다 — 개수로 자르면
    거래일이 어긋난 두 시장(한국·미국)에서 다른 날을 견주게 된다.
    """
    bench_by_date = {at: v for at, v in (bench_all or []) if at}
    fired: list[str] = []
    for i, (at, _v) in enumerate(obs_all):
        if not at:
            continue
        window = obs_all[i:]
        bench_window = None
        if bench_all is not None:
            bench_window = [(a, v) for a, v in bench_all if a and a <= at]
            if not bench_window:
                continue
        try:
            status, _ = _evaluate_atom(
                atom, window, datetime.fromisoformat(db_mod.iso_utc(at)), bench_window)
        except (KeyError, ValueError, TypeError):
            continue
        if status == "TRUE":
            fired.append(at[:10])
    return fired


def _closest_approach(atom: dict, obs: list[tuple[str, float | None]]) -> float | None:
    """한 번도 발화하지 않은 조건이 **문턱에 얼마나 가까이 갔는가**(0~1).

    이 숫자가 없으면 "발화한 적 없음"을 읽고 아무 판단도 할 수 없다. 반증
    조건이 안 울리는 것은 가설이 맞으면 **정상**이기 때문이다. 갈라 주는 것은
    거리다 — 문턱의 90%까지 갔던 조건은 살아 있는 감시자이고, 20%에서 멈춘
    조건은 사실상 장식이다(고문 표현으로 "조기 경보가 아니라 부고장").

    수준·변화율 조건에만 뜻이 있다. 연속성 조건은 "몇 구간까지 이어졌나"가
    거리인데 그건 별개 계산이라 여기서는 None을 낸다.
    """
    kind = atom["kind"]
    vals = [v for _at, v in obs if v is not None]
    if not vals:
        return None
    op = atom.get("op")
    if kind == "threshold":
        target = float(atom["value"])
        best = max(vals) if op in (">", ">=") else min(vals) if op in ("<", "<=") else None
        if best is None or target == 0:
            return None
        return max(0.0, min(1.0, best / target if op in (">", ">=") else target / best))
    if kind in ("change_pct", "relative_change_pct"):
        lb = int(atom["lookback"])
        if len(vals) <= lb:
            return None
        target = float(atom["value"])
        pcts = [(vals[i] - vals[i + lb]) / abs(vals[i + lb]) * 100
                for i in range(len(vals) - lb) if vals[i + lb]]
        if kind == "relative_change_pct":
            # 기준 대비 격차를 정확히 재려면 두 계열이 필요한데 여기에는 대상만
            # 있다. 절대 변화율로 대신 재면 **문턱보다 후하게** 나와("시장이
            # 올랐는데 나만 제자리"인 경우를 놓친다) 죽은 조건을 살아 있는 것처럼
            # 보이게 한다 — 감사 도구가 거짓 안심을 주는 쪽이라 아예 내지 않는다.
            return None
        if not pcts or target == 0:
            return None
        best = max(pcts) if op in (">", ">=") else min(pcts)
        # 부호가 같을 때만 비율이 뜻을 가진다(목표 -30%에 관측 +5%면 0이다).
        if (target > 0) != (best > 0):
            return 0.0
        return max(0.0, min(1.0, best / target))
    return None


def audit_conditions(conn, theses: list[dict], cutoff) -> list[dict]:
    """조건 하나마다 한 줄. `verdict`는 셋 중 하나다:

      `unreachable` — 어떤 데이터로도 참이 될 수 없다. **버그로 보고 고쳐라.**
      `never_fired` — 논리적으로는 가능하나 보유 이력에서 한 번도 발화하지
                      않았다. 반증·약화 조건이 여기 있으면 그 가설은 사실상
                      반증 불가능하다(오늘 고친 그 조건이 이 자리였다).
      `ok`          — 이력에서 실제로 발화한 적이 있다.
    """
    rows: list[dict] = []
    for th in theses:
        for group, atoms in (th.get("conditions") or {}).items():
            for atom in atoms:
                obs = _observations(conn, atom, cutoff)
                bench = (_observations(conn, dict(atom, subject=atom["benchmark"]), cutoff)
                         if atom.get("benchmark") else None)
                reachable = _reachable(atom)
                fired = _replay(atom, obs, bench) if reachable else []
                if not reachable:
                    verdict = "unreachable"
                elif not fired:
                    verdict = "never_fired"
                else:
                    verdict = "ok"
                rows.append({
                    "thesis_id": th["thesis_id"], "group": group,
                    "atom_id": atom.get("id", ""), "kind": atom["kind"],
                    "subject": atom["subject"], "metric": atom["metric"],
                    "verdict": verdict, "observations": len(obs),
                    "fired_days": len(fired),
                    "first_fired": fired[-1] if fired else "",
                    "last_fired": fired[0] if fired else "",
                    "closest": None if fired else _closest_approach(atom, obs),
                })
    return rows


# R9의 집행 형태 — 화면에 항상 붙는 범례(명세 §3.1). "지속을 새 관측 없이 읽지
# 마라"는 요구사항 7을, "그 이상을 약속하지 않는다"는 R8을 문장으로 고정한다.
#
# 4차 검수 F-A 잔여분 수리(CEO 결정 (가)): 이 줄은 사실 두 조각이다. (a) 도입일
# 조각은 그 리포트 시점에 아직 없던 표시를 "도입됐다"고 주장하므로 캐치업이면
# 생략해야 한다(`ops.thesis_display_introduced_on`이 `introduced_on`을 빈
# 문자열로 돌려 이 조각을 뺀다) — 그런데 예전 구현은 (a)가 빠질 때 (b)까지
# 통째로 사라졌다. (b) R8/R9 경고문은 도입일과 무관한 일반 안내이므로 항상
# 붙는다(site.py `_theses_page`의 meta 문단이 이미 같은 분할을 쓰고 있다 —
# 이 함수도 그 패턴을 따른다).
_LEGEND_INTRODUCED = (
    "진입일·지속·새 관측 표시는 {introduced_on} 도입. 그 이전 판정의 '강화'는 "
    "다른 뜻이다(매일 재충족을 포함). "
)
_LEGEND_R89 = (
    "지속일은 독립 증거의 수가 아니다 — 월간·분기 지표는 다음 발표 전까지 증거 1개다."
)


def render_impact(reviews: list[dict], report_date: str = "",
                  states: dict | None = None, introduced_on: str = "") -> str:
    """`report_date`를 주면 근거가 그날 것인 판정은 "근거 오늘"로 적는다.
    안 주면 날짜를 그대로 쓴다 — 옛 호출부가 깨지지 않게 기본값을 둔다.

    `states`(ST4): `{thesis_id: transitions.thesis_view()의 반환값}`. 없거나
    그 가설의 항목이 없으면 진입·지속·새 관측 조각을 생략한다(`_evidence_note`
    참조) — "근거 {date}" 조각은 그와 무관하게 유지된다.

    `introduced_on`(ST4, 명세 §2 구현 4): 있을 때만 맨 끝 범례 줄에 도입일
    조각을 붙인다. 빈 문자열이면 그 조각만 뺀다 — 도입일을 모르는데
    "도입됨"이라고 적는 것은 없는 사실을 지어내는 것이다. R8/R9 경고문
    (`_LEGEND_R89`)은 도입일과 무관하므로 `reviews`가 있는 한 항상 붙는다
    (4차 검수 F-A 잔여분 수리, CEO 결정 (가) — 이전에는 `introduced_on`이
    비면 경고문까지 함께 사라졌다)."""
    if not reviews:
        return ""
    counts = {"강화": 0, "유지": 0, "약화": 0, "무효": 0, "판정 불가": 0}
    for r in reviews:
        counts[r["verdict"]] += 1

    # **사건을 앞세우고 상태를 뒤로 내린다** (CEO 지적 2026-08-21).
    #
    # 2026-08-20에 상세 줄의 "매일 강화"를 진입일·지속·새 관측으로 바꿨는데,
    # 요약 줄이 같은 결함을 그대로 반복하고 있었다 — 판정은 **상태**이지
    # 오늘의 사건이 아닌데 "강화 7"을 맨 앞에 놓으면 매일 무언가 좋아진 것처럼
    # 읽힌다. 실측: 판정 244건 중 상태가 바뀐 것은 **4건(1.6%)**이고, 나머지
    # 98.4%는 안 바뀐 상태를 다시 읽은 것이다.
    #
    # "바뀐 것 없음"이 매일 나오는 것은 괜찮다 — 그것이 **사실**이기 때문이다.
    # 문제는 아무 일도 없는 날에 "강화"라고 쓰는 것이었다.
    #
    # `changed`는 `review()`가 이미 계산해 원장에 남기는 값이다(직전 판정과
    # 다를 때만 1). 새로 세지 않고 그대로 쓴다.
    moved = [r for r in reviews if r.get("changed")]
    if moved:
        detail = " · ".join(
            f"{THEME_LABELS.get(r['theme'], r['theme'])} #{r['slot']} "
            f"{r['prev_verdict']} → {r['verdict']}" for r in moved)
        event = f"**상태가 바뀐 가설 {len(moved)}건** — {detail}"
    else:
        event = "상태가 바뀐 가설 없음"
    header = (
        f"가설 {len(reviews)}건 판정 — {event}. "
        f"(현재 상태: 강화 {counts['강화']} · 유지 {counts['유지']} · "
        f"약화 {counts['약화']} · 무효 {counts['무효']} · 판정 불가 {counts['판정 불가']})"
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
        state = (states or {}).get(r["thesis_id"])
        note = _evidence_note(r, report_date, state)
        suffix = f" ({note})" if note else ""
        lines.append(f"[{label} #{r['slot']}] {r['verdict']} — {reason}{suffix}.")
    legend = _LEGEND_INTRODUCED.format(introduced_on=introduced_on) if introduced_on else ""
    lines.append(f"※ {legend}{_LEGEND_R89}")
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
