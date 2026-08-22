"""사각지대 신고(CEO 지시 2026-08-20)의 계약.

이 기능은 **화면이 사실과 다른 문장을 내는 종류**라, 누가 그 단언을 적어 두지
않으면 시험 1000개가 통과해도 안 잡힌다(2026-08-19 마일스톤의 교훈). 그래서
여기서는 "돌아간다"가 아니라 **무엇을 말해도 되고 무엇을 말하면 안 되는지**를
적는다.

합성 `price_map`만 쓴다 — DB도, 리포트 발행도 건드리지 않는다(저장소 `reports/`
를 덮어쓰는 사고를 원천 차단).
"""
from __future__ import annotations

import pytest

from market_intel.reporting import blindspot as bs


def _hist(n: int, step: float = 0.5, last: float | None = None) -> list[float]:
    """평온한 종가 이력 n개(+마지막 날 등락). `last`가 등락률(%)이다."""
    closes = [100.0]
    for i in range(n):
        closes.append(closes[-1] * (1 + (step if i % 2 else -step) / 100))
    if last is not None:
        closes.append(closes[-1] * (1 + last / 100))
    return closes


def _pm(sector: str, sector_last: float, watched: dict[str, float] | None = None) -> dict:
    out = {sector: {"hist": _hist(300, last=sector_last), "delta_pct": sector_last}}
    for sym, last in (watched or {}).items():
        out[sym] = {"hist": _hist(300, last=last), "delta_pct": last}
    return out


def _volatile_hist(n: int, last: float) -> list[float]:
    """**변동이 큰** 종가 이력. 개별주를 흉내 낸다.

    이게 없으면 비중 관련 시험이 겨냥한 코드에 **닿지 못한다**: `_pm`의 평온한
    이력에서는 4% 하루가 무조건 자기 이력 상위 0%라, 기여도를 따지기 전에
    `_is_unusual(관측기업)` 가드가 먼저 걸어 신고 자체가 사라진다.

    현실이 정확히 이 모양이라서 중요하다 — LLY는 +4.46%가 자기 이력 상위 3.19%라
    "평소 범위"로 판정됐다(문턱 2%). 개별주가 ETF보다 원래 크게 움직인다는
    사실이 곧 오탐의 원인이었으므로, 시험 이력도 그래야 한다."""
    closes = [100.0]
    for i in range(n):
        closes.append(closes[-1] * (1 + ((i % 21) - 10) * 0.8 / 100))
    closes.append(closes[-1] * (1 + last / 100))
    return closes


def _pm_vol(sector: str, sector_last: float, watched: dict[str, float]) -> dict:
    out = {sector: {"hist": _hist(300, last=sector_last), "delta_pct": sector_last}}
    for sym, last in watched.items():
        out[sym] = {"hist": _volatile_hist(300, last), "delta_pct": last}
    return out


_L = lambda s: s  # noqa: E731 - 라벨은 심볼 그대로


# --- 문턱 -----------------------------------------------------------------

def test_threshold_is_deliberately_tighter_than_the_unusual_day_block():
    """2%와 5%가 다른 것은 **의도**다 — 업종 19개에 5%를 걸면 하루 평균 1.2건이
    정의상 보장되어 신고가 배경 소음이 된다(실측 58%의 날에 신고). 누군가
    "일관성"을 이유로 둘을 같게 만들면 이 시험이 그 이유를 다시 읽게 한다."""
    from market_intel.reporting.build import _UNUSUAL_DAY_RANK_THRESHOLD

    assert bs.RANK_THRESHOLD < _UNUSUAL_DAY_RANK_THRESHOLD, (
        "업종 수가 많으므로 개별 문턱은 「오늘 유별난 것」보다 조여야 한다")


def test_says_nothing_without_enough_history():
    """표본이 모자라면 백분위를 **아예 말하지 않는다** — 맥락 없는 순위는
    지어낸 숫자다(`_KR_BREADTH_MIN_HISTORY_DAYS`와 같은 태도)."""
    assert bs.top_rank_pct(_hist(bs.MIN_HISTORY_DAYS - 5, last=50.0)) is None


def test_today_is_excluded_from_its_own_population():
    """오늘을 모집단에 넣으면 자기 자신을 밀어 올려 백분위가 낙관적으로 나온다.

    **이력을 최소 길이로 잡는 것이 이 시험의 요점이다.** 300일짜리로 재면 오늘
    한 칸의 무게가 1/300=0.33%라 웬만한 허용오차 안에 숨는다 — 실제로 첫 판이
    그렇게 쓰여 있었고, "오늘을 포함시키는" 변이가 통과했다. 여기서는 오늘이
    이력 전체에서 가장 큰 값이므로 정답이 **정확히 0.0**이고, 포함시키면
    1/n(>1%)이 되어 반드시 깨진다."""
    n = bs.MIN_HISTORY_DAYS + 1
    wild = _hist(n, last=50.0)
    assert bs.top_rank_pct(wild) == 0.0
    assert bs.top_rank_pct(_hist(n, last=0.1)) > 0.0


# --- 신고 조건 -------------------------------------------------------------

SECTOR_WITH_WATCH = "XLK"
WATCHED = bs.SECTOR_WATCH[SECTOR_WITH_WATCH][0]


def test_reports_when_sector_is_unusual_but_our_companies_are_quiet():
    rows = bs.detect(_pm(SECTOR_WITH_WATCH, 30.0, {WATCHED: 0.1}), _L)
    assert [r.sector_symbol for r in rows] == [SECTOR_WITH_WATCH]
    assert WATCHED in rows[0].note
    assert "평소 범위" in rows[0].note
    assert rows[0].explained_pp is None, "비중을 안 줬으면 계산했다고 하면 안 된다"


# --- 단정 금지 (2026-08-21에 고친 오탐) --------------------------------------

def test_never_claims_the_mover_was_something_we_do_not_watch():
    """**이 시스템이 낸 유일한 사각지대 신고 1건이 이 문장 때문에 틀렸다.**

    2026-08-20 발행: *"헬스케어 +3.51% — 우리가 보는 Eli Lilly은(는) 평소
    범위였다. 움직인 것은 우리가 관측하지 않는 종목이다."*

    실제로는 LLY가 +4.46%였고 XLV의 15.47%라 업종 움직임의 약 20%를 **그 종목이**
    만들었다. 검출기는 각 종목을 자기 이력과만 견주는데(상대적 질문), 개별주는
    ETF보다 원래 크게 움직여서 같은 문턱을 대면 거의 항상 "평소 범위"로 나온다 —
    그 위에 저 단정을 얹으면 **구조적으로 틀린 문장**이 된다.

    관측이 아니라 추론인 문장은 쓰지 않는다. 비중을 알면 숫자로, 모르면 침묵.
    """
    for weights in (None, {SECTOR_WITH_WATCH: {WATCHED: 20.0}}):
        rows = bs.detect(_pm(SECTOR_WITH_WATCH, 30.0, {WATCHED: 0.1}), _L, weights)
        for r in rows:
            assert "관측하지 않는 종목이다" not in r.note, "근거 없는 단정이 돌아왔다"


def test_says_it_cannot_tell_when_weights_are_unknown():
    """한국 업종 ETF는 보유내역을 주지 않는다(실측: 14개 전부). **모른다를
    없다로 승격하지 않는다** — 못 가린다고 말한다."""
    rows = bs.detect(_pm(SECTOR_WITH_WATCH, 30.0, {WATCHED: 0.1}), _L, weights={})
    assert "비중을 알 수 없어" in rows[0].note


def test_splits_the_move_into_explained_and_unexplained():
    """비중을 알면 단정 대신 **분해**한다."""
    pm = _pm_vol(SECTOR_WITH_WATCH, 10.0, {WATCHED: 4.0})
    assert not bs._is_unusual(pm[WATCHED]["hist"]), "픽스처가 기여도 코드에 닿아야 한다"
    rows = bs.detect(pm, _L, weights={SECTOR_WITH_WATCH: {WATCHED: 25.0}})
    assert rows, "설명된 몫이 10%면 여전히 사각지대다"
    assert rows[0].explained_pp == pytest.approx(1.0)   # 0.25 * 4.0
    assert "+1.00%p" in rows[0].note and "10%" in rows[0].note
    assert "+9.00%p" in rows[0].note, "남은 몫도 숫자로 말해야 한다"


def test_stays_silent_when_our_companies_explain_most_of_the_move():
    """관측 기업이 절반 넘게 설명하면 그날의 이야기는 "우리가 못 보는 곳"이
    아니다. 자기 이력 기준으로는 조용했더라도 마찬가지다 — 그 판정이 바로
    오탐을 만든 상대적 질문이기 때문이다."""
    pm = _pm_vol(SECTOR_WITH_WATCH, 10.0, {WATCHED: 6.0})
    assert not bs._is_unusual(pm[WATCHED]["hist"]), "픽스처가 기여도 코드에 닿아야 한다"
    assert bs.detect(pm, _L, weights={SECTOR_WITH_WATCH: {WATCHED: 90.0}}) == []  # 5.4%p = 54%


def test_a_watched_company_moving_against_the_sector_does_not_count_as_explaining():
    """부호가 반대면 설명한 것이 아니라 **거스른** 것이다. 절댓값으로 재면
    업종을 끌어내린 종목이 업종 상승을 "설명"하게 된다."""
    pm = _pm_vol(SECTOR_WITH_WATCH, 10.0, {WATCHED: -6.0})
    assert not bs._is_unusual(pm[WATCHED]["hist"]), "픽스처가 기여도 코드에 닿아야 한다"
    rows = bs.detect(pm, _L, weights={SECTOR_WITH_WATCH: {WATCHED: 90.0}})
    assert rows, "반대로 움직인 기업이 신고를 지우면 안 된다"
    assert rows[0].explained_pp == pytest.approx(-5.4)


def test_unknown_weight_for_one_company_does_not_become_zero():
    """비중을 모르는 기업은 계산에서 빠진다 — 0으로 치면 "설명 못 한 몫"이
    부풀려지고, 그만큼 사각지대가 과장된다."""
    pm = _pm(SECTOR_WITH_WATCH, 10.0, {WATCHED: 4.0})
    assert bs.explained_pp(pm, {}, [WATCHED]) is None
    assert bs.explained_pp(pm, {WATCHED: 25.0}, [WATCHED]) == pytest.approx(1.0)


def test_stays_silent_when_our_companies_moved_too():
    """우리 기업도 같이 크게 움직였으면 사각지대가 아니다 — 우리가 보는 것으로
    설명이 되는 날이다."""
    assert bs.detect(_pm(SECTOR_WITH_WATCH, 30.0, {WATCHED: 30.0}), _L) == []


def test_stays_silent_when_the_sector_itself_is_ordinary():
    assert bs.detect(_pm(SECTOR_WITH_WATCH, 0.1, {WATCHED: 0.1}), _L) == []


def test_stays_silent_when_our_companies_have_no_price_at_all():
    """비교할 값이 없으면 "조용했다"고 말할 수 없다 — 없는 것을 조용함으로
    읽으면 결측이 사실로 승격된다."""
    assert bs.detect(_pm(SECTOR_WITH_WATCH, 30.0), _L) == []


# --- 구조적 사실 vs 그날의 사건 ----------------------------------------------

def test_unwatched_sectors_are_never_a_daily_alert():
    """관측 기업이 아예 없는 업종은 **상시 사실**이라 당일 신고에 오르지 않는다.
    실측: 매일 신고하면 신고의 69%가 이 종류가 되어 나머지를 덮는다.

    업종 하나가 아니라 **관측 없는 업종 전부를 한꺼번에** 극단으로 움직여 놓고
    본다. 하나만 보면 다른 이유(값 없음)로 조용한 것과 구별이 안 된다."""
    unwatched = [s for s, w in bs.SECTOR_WATCH.items() if not w]
    pm = {}
    for i, sector in enumerate(unwatched):
        pm.update(_pm(sector, 40.0 + i))
    assert bs.detect(pm, _L) == []


def test_a_watched_sector_in_the_same_batch_still_reports():
    """앞 시험이 "그냥 아무것도 안 낸다"로 통과하지 않게 하는 짝 시험 —
    같은 묶음에 관측 있는 업종을 하나 섞으면 그것만 나와야 한다."""
    pm = {}
    for i, sector in enumerate(s for s, w in bs.SECTOR_WATCH.items() if not w):
        pm.update(_pm(sector, 40.0 + i))
    pm.update(_pm(SECTOR_WITH_WATCH, 30.0, {WATCHED: 0.1}))
    assert [r.sector_symbol for r in bs.detect(pm, _L)] == [SECTOR_WITH_WATCH]


def test_unwatched_sectors_are_always_disclosed():
    """대신 목록으로는 **항상** 나온다. 빠지면 리포트가 자기 눈먼 자리를 말하지
    않는 리포트가 된다."""
    listed = set(bs.unwatched_sectors(_L))
    expected = {s for s, w in bs.SECTOR_WATCH.items() if not w}
    assert listed == expected
    assert listed, "관측 기업이 없는 업종이 하나도 없다면 이 기능의 전제가 바뀐 것이다"


def test_every_watched_symbol_is_a_real_universe_member():
    """매핑에 오타가 나면 그 업종은 영원히 "관측 없음"처럼 조용해진다 —
    조용한 실패라 눈으로는 안 잡힌다."""
    from market_intel.universe import UNIVERSE

    known = {m["symbol"] for m in UNIVERSE}
    watched = {s for v in bs.SECTOR_WATCH.values() for s in v}
    assert watched <= known, f"유니버스에 없는 심볼: {sorted(watched - known)}"
    assert set(bs.SECTOR_WATCH) <= known, (
        f"유니버스에 없는 업종 지수: {sorted(set(bs.SECTOR_WATCH) - known)}")


def test_mapping_covers_every_sector_index_we_collect():
    """업종 지수를 새로 추가하고 매핑을 안 적으면 그 업종은 사각지대 판정에서
    조용히 빠진다. 개수를 박지 않고 **집합으로** 비교한다."""
    from market_intel.universe import SECTOR_INDEX_SYMBOLS

    assert set(SECTOR_INDEX_SYMBOLS) <= set(bs.SECTOR_WATCH), (
        f"매핑이 없는 업종 지수: {sorted(set(SECTOR_INDEX_SYMBOLS) - set(bs.SECTOR_WATCH))}")


def test_non_sector_axes_are_declared_not_smuggled():
    """업종 지수가 **아닌** 축(규모 축 등)도 여기서 함께 본다. 다만 조용히
    끼워 넣으면 안 된다 — 축이 하나 늘 때마다 다중비교가 늘어 문턱의 뜻이
    바뀌기 때문이다(문턱을 2%로 조인 이유가 바로 그것이다)."""
    from market_intel.universe import SECTOR_INDEX_SYMBOLS

    extra = set(bs.SECTOR_WATCH) - set(SECTOR_INDEX_SYMBOLS)
    assert extra == bs.NON_SECTOR_AXES, (
        f"선언되지 않은 축: {sorted(extra - bs.NON_SECTOR_AXES)} / "
        f"선언만 되고 없는 축: {sorted(bs.NON_SECTOR_AXES - extra)}")


def test_caps_how_many_rows_one_report_can_carry():
    pm = {}
    watched_sectors = [s for s, w in bs.SECTOR_WATCH.items() if w]
    for i, sector in enumerate(watched_sectors):
        pm.update(_pm(sector, 30.0 + i, {bs.SECTOR_WATCH[sector][0]: 0.1}))
    rows = bs.detect(pm, _L)
    assert len(rows) == bs.MAX_ROWS < len(watched_sectors)
    assert [abs(r.delta_pct) for r in rows] == sorted(
        (abs(r.delta_pct) for r in rows), reverse=True), "큰 것부터 실어야 한다"


# --- 옛 리포트 -------------------------------------------------------------

def test_old_report_json_without_the_new_keys_still_loads():
    """`site build`는 이 필드가 생기기 전에 쓰인 JSON을 전부 다시 읽는다.
    하나가 죽으면 사이트 전체가 안 올라간다."""
    from market_intel.reporting.model import Report

    old = Report(report_type="morning", report_date="2026-01-01", title="t",
                 cutoff_kst="", cutoff_utc="", generated_at="", headline="",
                 breadth="", data_status="source_verified").to_json()
    import json
    d = json.loads(old)
    d.pop("blind_spots", None)
    d.pop("unwatched_sectors", None)
    r = Report.from_json(json.dumps(d))
    assert r.blind_spots == [] and r.unwatched_sectors == []


def test_the_2026_08_20_false_alert_cannot_happen_again():
    """**실제 사건 재현.** 이 시스템이 낸 유일한 사각지대 신고가 틀렸던 그 날이다.

    원장 실측(2026-08-19 미국 종가):
        XLV +3.51% -> 자기 이력 상위 0.21% -> 유별남 (문턱 2%)
        LLY +4.46% -> 자기 이력 상위 3.19% -> 문턱 미달 = "평소 범위"
        LLY 비중 15.47% -> 기여도 +0.69%p = 업종 +3.51%p의 20%

    즉 **업종을 움직인 것의 5분의 1이 우리가 보는 그 종목이었다.** 그런데 발행된
    문장은 "움직인 것은 우리가 관측하지 않는 종목이다"였다.

    DB를 읽지 않는다(이 파일의 규칙) — 위 두 성질(업종은 유별남 · 관측 기업은
    자기 기준 평소 범위)을 합성 이력으로 그대로 만든다.
    """
    pm = _pm_vol("XLV", 3.51, {"LLY": 4.46})
    assert bs._is_unusual(pm["XLV"]["hist"]), "업종은 유별나야 한다"
    assert not bs._is_unusual(pm["LLY"]["hist"]), "관측 기업은 자기 기준 평소 범위여야 한다"

    rows = bs.detect(pm, _L, weights={"XLV": {"LLY": 15.472}})
    assert len(rows) == 1
    note = rows[0].note
    assert "관측하지 않는 종목이다" not in note, "그 단정이 이 사건을 만들었다"
    assert "+0.69%p" in note and "20%" in note, "우리 몫을 숫자로 말해야 한다"
    assert "+2.82%p" in note, "남은 몫도 숫자로 말해야 한다"
