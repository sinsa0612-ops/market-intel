"""관측 정렬 — 두 관측을 비교할 때 **어느 것이 먼저 알려졌는지**를 코드가 강제한다.

사전등록 규칙: `theses/alignment_rules_v1.md` (동결, 해시로 잠김).
이 모듈은 그 문서의 R1~R4를 코드로 옮긴 것이고, 그 이상을 하지 않는다.

## 왜 있나

2026-08-20 실측. 미국채 10년물과 코스피를 같은 날짜로 묶어 방향별 분할표를
냈더니 "금리↑일 때 코스피 하락 43.9% / 금리↓일 때 47.6%"(차이 3.7%p, 사실상
무관)가 나왔다. 시차를 맞추자 "51.2% / 40.8%"(차이 10.4%p, 뚜렷한 관계)로
**결론이 정반대로 뒤집혔다.** 미국 장이 한국 장보다 늦게 닫는다는 것 하나를
놓친 결과다.

**틀린 쪽이 더 그럴듯해 보였다.** 그래서 정렬을 매번 판단하지 않고 여기를
거치게 한다. 교차시장 비교는 전부 `aligned()`를 통과해야 한다.
"""
from __future__ import annotations

from typing import Iterator

# 규칙 문서의 sha256. 문서가 한 글자라도 바뀌면 시험이 빨개진다 —
# `transition_rules_v1.md`와 같은 장치다.
RULES_SHA256 = "fbafed3d6abd93baca498e90e1884764ddabcc38ebf5cc3a249f882eadf70036"
RULES_PATH = "theses/alignment_rules_v1.md"

# R1 — 같은 달력일 안에서 값이 확정되는 순서. 클수록 늦다.
# 미국 장 마감은 KST 다음날 새벽이므로 한국보다 항상 늦다.
SESSION_ORDER: dict[str, int] = {"KR": 0, "US": 1}

# R3 — 관측 종류마다 값이 담긴 칸이 다르다. 다른 칸으로 조회하면 오류가 아니라
# **빈 결과**가 나오고, 빈 결과는 "관계 없음"처럼 보인다.
METRIC_BY_CATEGORY: dict[str, str] = {
    "price": "price_close",
    "macro": "value",
}


class AlignmentError(ValueError):
    """정렬을 정할 수 없을 때. **조용히 하나를 고르지 않는다** — 빈 결과나
    임의 선택이 곧 오보가 되는 자리라 반드시 터뜨린다."""


def _describe(conn, subject: str) -> tuple[str, str]:
    """(country, metric). 원장에 실제로 있는 값에서만 나온다 — 별도 등록부를
    두면 원장과 어긋나는 순간 조용히 틀린다(R4)."""
    rows = conn.execute(
        "SELECT DISTINCT country, category FROM fact_revisions "
        "WHERE subject=? AND value_num IS NOT NULL", (subject,)).fetchall()
    if not rows:
        raise AlignmentError(f"원장에 관측이 없는 subject: {subject!r}")
    countries = {r[0] for r in rows if r[0]}
    categories = {r[1] for r in rows if r[1]}
    if len(countries) != 1:
        raise AlignmentError(
            f"{subject!r}의 관측에 나라가 섞여 있다({sorted(countries)}) — 정렬을 정할 수 없다")
    country = countries.pop()
    if country not in SESSION_ORDER:
        raise AlignmentError(f"{subject!r}의 나라 {country!r}에 세션 순서가 없다(R1)")
    known = categories & set(METRIC_BY_CATEGORY)
    if len(known) != 1:
        raise AlignmentError(
            f"{subject!r}의 종류를 하나로 정할 수 없다({sorted(categories)}) — "
            f"정렬 가능한 종류는 {sorted(METRIC_BY_CATEGORY)}뿐이다(R3)")
    return country, METRIC_BY_CATEGORY[known.pop()]


def series(conn, subject: str) -> dict[str, float]:
    """날짜 -> 값. **칸 이름을 호출자가 고르지 않는다**(R3) — 그 자유가
    2026-08-20에 금리를 통째로 빈칸으로 만들었다."""
    _, metric = _describe(conn, subject)
    out: dict[str, float] = {}
    for row in conn.execute(
            "SELECT substr(event_at,1,10) AS d, value_num, revision_no "
            "FROM fact_revisions WHERE subject=? AND metric=? AND value_num IS NOT NULL "
            "ORDER BY d, revision_no", (subject, metric)):
        out[row[0]] = row[1]  # 같은 날 여러 개정이면 마지막(최신 개정)이 이긴다
    return out


def lag_days(cause_country: str, effect_country: str) -> int:
    """R2 — 원인을 며칠 앞으로 당겨야 하나. 1이면 `원인 D-1` ↔ `결과 D`."""
    for c in (cause_country, effect_country):
        if c not in SESSION_ORDER:
            raise AlignmentError(f"세션 순서가 없는 나라: {c!r}")
    return 1 if SESSION_ORDER[cause_country] >= SESSION_ORDER[effect_country] else 0


def aligned(conn, cause: str, effect: str) -> list[tuple[str, float, float, float]]:
    """`(결과의 날짜, 원인의 변화, 결과의 변화율%, 원인의 값)` 목록.

    원인의 변화는 **그 관측 자신의 직전 관측 대비**다 — 금리는 %p, 가격은
    절대차. 결과는 등락률(%)이다. 이 비대칭이 의도적인 이유: 원인 쪽은 "올랐나
    내렸나"만 쓰이고(방향별 분할표), 결과 쪽은 크기가 쓰인다.

    R2가 정한 시차를 **여기서만** 적용한다. 호출자가 날짜를 직접 맞추지 못하게
    하는 것이 이 함수의 존재 이유다."""
    cause_country, _ = _describe(conn, cause)
    effect_country, _ = _describe(conn, effect)
    lag = lag_days(cause_country, effect_country)

    cs, es = series(conn, cause), series(conn, effect)
    cdays, edays = sorted(cs), sorted(es)
    cchg = {cdays[i]: cs[cdays[i]] - cs[cdays[i - 1]] for i in range(1, len(cdays))}

    out = []
    for i in range(1, len(edays)):
        prev, cur = edays[i - 1], edays[i]
        if not es[prev]:
            continue
        # 시차만큼 뒤로 물러선 시점에서 **가장 최근에 알려진** 원인의 변화.
        # 거래일이 어긋나는 날(한쪽만 휴장)에도 미래를 당겨 쓰지 않는다.
        limit = prev if lag else cur
        known = [d for d in cdays if d <= limit and d in cchg]
        if not known:
            continue
        d = known[-1]
        out.append((cur, cchg[d], (es[cur] / es[prev] - 1) * 100, cs[d]))
    return out


def direction_table(pairs: list[tuple[str, float, float, float]],
                    min_move: float = 0.0) -> dict:
    """CEO가 2026-08-20에 제안한 방향별 분할표. **빈도·크기·꼬리를 나눠 낸다** —
    실측에서 빈도 차이(10%p)는 컸는데 낙폭 차이(0.12%p)는 작았다. 하나로 뭉쳐
    평균만 내면 그 구분이 사라진다."""
    def side(sel: list[tuple]) -> dict:
        ks = [k for _, _, k, _ in sel]
        if not ks:
            return {"days": 0}
        down = [k for k in ks if k < 0]
        return {
            "days": len(ks),
            "down_share": len(down) / len(ks) * 100,
            "mean_drop": (sum(down) / len(down)) if down else None,
            "tail_share": sum(1 for k in ks if k <= -1.0) / len(ks) * 100,
            "mean": sum(ks) / len(ks),
        }
    return {
        "up": side([p for p in pairs if p[1] >= min_move and p[1] > 0]),
        "down": side([p for p in pairs if p[1] <= -min_move and p[1] < 0]),
        "min_move": min_move,
    }


def iter_subjects(conn) -> Iterator[str]:
    """정렬 가능한 subject 전부 — 진단 명령이 목록을 보일 때 쓴다."""
    cats = ",".join("?" * len(METRIC_BY_CATEGORY))
    for row in conn.execute(
            f"SELECT DISTINCT subject FROM fact_revisions WHERE category IN ({cats}) "
            "ORDER BY subject", tuple(METRIC_BY_CATEGORY)):
        yield row[0]
