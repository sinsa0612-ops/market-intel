"""관측 정렬(`align.py`)의 계약 — 사전등록 규칙 `theses/alignment_rules_v1.md`.

이 모듈이 존재하는 이유가 하나뿐이라 시험도 거기 집중한다: **정렬이 틀리면
결론이 정반대로 나오는데, 틀린 쪽이 더 그럴듯해 보인다.** 그래서 "돌아간다"가
아니라 "틀린 정렬을 쓰면 반드시 깨진다"를 적는다.

메모리 DB만 쓴다 — 운영 원장도, 저장소 `reports/`도 건드리지 않는다.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from market_intel import align

REPO = Path(__file__).resolve().parents[2]


def _db(rows) -> sqlite3.Connection:
    """rows: (subject, country, category, metric, date, value)"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE fact_revisions (subject TEXT, country TEXT, category TEXT,"
                 " metric TEXT, event_at TEXT, value_num REAL, revision_no INTEGER)")
    conn.executemany(
        "INSERT INTO fact_revisions VALUES (?,?,?,?,?,?,1)",
        [(s, c, cat, m, f"{d}T00:00:00+00:00", v) for s, c, cat, m, d, v in rows])
    return conn


def _days(n: int, start: int = 1) -> list[str]:
    return [f"2026-03-{start + i:02d}" for i in range(n)]


# --- 동결 ------------------------------------------------------------------

def test_rules_document_is_frozen():
    """규칙을 고치려면 v2를 새로 등록해야 한다 — 조용한 수정을 막는다
    (`transition_rules_v1.md`와 같은 장치)."""
    text = (REPO / align.RULES_PATH).read_bytes()
    assert hashlib.sha256(text).hexdigest() == align.RULES_SHA256


# --- R1/R2 시차 -------------------------------------------------------------

def test_us_cause_is_lagged_for_a_korean_effect():
    """R2 검산. 미국 장이 늦게 닫으므로 미국 D일 값은 한국 D+1일에야 쓸 수 있다.
    **2026-08-20에 이걸 놓쳐 결론이 정반대로 나왔다.**"""
    assert align.lag_days("US", "KR") == 1


def test_korean_cause_is_not_lagged_for_a_us_effect():
    """반대 방향은 시차가 없다 — 한국 마감은 미국 마감 전에 이미 알려져 있다.
    양쪽에 같은 시차를 거는 것은 한쪽을 반드시 틀리게 한다."""
    assert align.lag_days("KR", "US") == 0


@pytest.mark.parametrize("country", sorted(align.SESSION_ORDER))
def test_same_session_cause_still_comes_first(country):
    """같은 시각에 확정된 두 값 사이에서 한쪽을 원인이라 부르려면 먼저 있어야 한다."""
    assert align.lag_days(country, country) == 1


def test_unknown_country_raises_instead_of_guessing():
    with pytest.raises(align.AlignmentError):
        align.lag_days("JP", "KR")


# --- R3 지표 이름 -----------------------------------------------------------

def test_macro_is_read_from_its_own_column_not_the_price_column():
    """2026-08-20 실측 사고의 회귀 시험. 거시 지표를 `price_close`로 조회하면
    오류가 아니라 **빈 결과**가 나오고, 빈 결과는 "관계 없음"으로 읽힌다.
    호출자가 칸 이름을 고를 수 없어야 이 사고가 구조적으로 불가능해진다."""
    d = _days(3)
    conn = _db([("DGS10", "US", "macro", "value", d[i], 4.0 + i) for i in range(3)])
    assert align.series(conn, "DGS10") == {d[0]: 4.0, d[1]: 5.0, d[2]: 6.0}


def test_unknown_subject_raises_instead_of_returning_empty():
    """빈 결과를 조용히 돌려주면 "그 관계는 없다"로 읽힌다. 없는 것과 모르는
    것은 다르다."""
    with pytest.raises(align.AlignmentError):
        align.series(_db([]), "NOPE")


def test_mixed_country_subject_raises():
    """한 subject의 관측에 나라가 섞이면 시차를 정할 수 없다 — 조용히 하나를
    고르면 그 선택이 결론을 뒤집는다."""
    d = _days(2)
    conn = _db([("X", "US", "price", "price_close", d[0], 1.0),
                ("X", "KR", "price", "price_close", d[1], 2.0)])
    with pytest.raises(align.AlignmentError):
        align.series(conn, "X")


# --- 정렬 본체 --------------------------------------------------------------

def _cross_market_db():
    """미국 원인 / 한국 결과. 원인은 **하루씩 밀려** 결과에 나타나도록 만든다:
    미국이 D일에 오르면 한국은 D+1일에 내린다."""
    d = _days(8)
    rows = []
    cause = [4.0, 4.1, 4.0, 4.1, 4.0, 4.1, 4.0, 4.1]      # 올랐다/내렸다 반복
    effect = [100, 100, 99, 100, 99, 100, 99, 100]        # 하루 뒤 반대로 움직인다
    for i, day in enumerate(d):
        rows.append(("US10Y", "US", "macro", "value", day, cause[i]))
        rows.append(("KOSPI", "KR", "price", "price_close", day, float(effect[i])))
    return _db(rows), d


def test_aligned_pairs_the_cause_with_the_next_day_effect():
    conn, d = _cross_market_db()
    pairs = align.aligned(conn, "US10Y", "KOSPI")
    # 결과 날짜마다 쓰인 원인 값이 **직전 날까지** 알려진 것이어야 한다.
    for day, change, _, _ in pairs:
        assert day in d
    ups = [p for p in pairs if p[1] > 0]
    downs = [p for p in pairs if p[1] < 0]
    assert ups and downs, "양쪽 방향이 다 나와야 분할표를 만들 수 있다"
    # 설계상 금리가 오른 다음날은 내리고, 내린 다음날은 오른다.
    assert all(p[2] < 0 for p in ups), [p for p in ups if p[2] >= 0]
    assert all(p[2] > 0 for p in downs), [p for p in downs if p[2] <= 0]


def test_same_day_pairing_would_reach_the_opposite_conclusion():
    """**이 시험이 이 모듈의 존재 이유다.** 같은 데이터를 같은 날로 묶으면
    정반대 결론이 나온다. 정렬이 취향 문제가 아니라는 증거를 코드에 남긴다."""
    conn, _ = _cross_market_db()
    aligned = align.aligned(conn, "US10Y", "KOSPI")
    cs, es = align.series(conn, "US10Y"), align.series(conn, "KOSPI")
    days = sorted(set(cs) & set(es))
    same_day = [((cs[days[i]] - cs[days[i - 1]]),
                 (es[days[i]] / es[days[i - 1]] - 1) * 100) for i in range(1, len(days))]

    def down_share(rows):
        ups = [r for r in rows if r[0] > 0]
        return sum(1 for r in ups if r[1] < 0) / len(ups) * 100

    assert down_share([(p[1], p[2]) for p in aligned]) == 100.0
    assert down_share(same_day) == 0.0, "같은 날 묶음은 정반대를 말한다"


def test_never_uses_a_cause_observation_from_the_future():
    """차단선의 축소판 — 결과가 확정되기 전에 알려져 있던 원인만 쓴다."""
    conn, d = _cross_market_db()
    cs = align.series(conn, "US10Y")
    by_value = {}
    for day, value in cs.items():
        by_value.setdefault(value, []).append(day)
    for effect_day, _, _, cause_value in align.aligned(conn, "US10Y", "KOSPI"):
        used = [x for x in by_value[cause_value] if x < effect_day]
        assert used, f"{effect_day}에 쓰인 원인 값이 그 날 이후 것이다"


# --- 분할표 ---------------------------------------------------------------

def test_direction_table_separates_frequency_from_size():
    """빈도와 크기를 뭉쳐 평균만 내면 구분이 사라진다 — 실측에서 빈도 차이는
    10%p였는데 낙폭 차이는 0.12%p였다."""
    pairs = [("d", +1.0, -2.0, 0), ("d", +1.0, -2.0, 0), ("d", +1.0, +1.0, 0),
             ("d", -1.0, -0.5, 0), ("d", -1.0, +1.0, 0), ("d", -1.0, +1.0, 0)]
    t = align.direction_table(pairs)
    assert t["up"]["down_share"] == pytest.approx(66.67, abs=0.01)
    assert t["down"]["down_share"] == pytest.approx(33.33, abs=0.01)
    assert t["up"]["mean_drop"] == pytest.approx(-2.0)
    assert t["down"]["mean_drop"] == pytest.approx(-0.5)
    assert t["up"]["tail_share"] == pytest.approx(66.67, abs=0.01)   # 1% 이상 급락
    assert t["down"]["tail_share"] == 0.0
