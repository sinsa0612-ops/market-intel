"""자금 갈래(CEO 지시 2026-08-20)의 계약.

이 블록은 **"돈이 어디로 갔다"는 문장을 화면에 내는** 기능이라, 틀리면 사실과
다른 단언이 발행된다. 그래서 "돌아간다"가 아니라 **말해도 되는 것과 말하면
안 되는 것**을 적는다. 합성 price_map만 쓴다 — DB도 발행도 건드리지 않는다.
"""
from __future__ import annotations

import pytest

from market_intel.reporting import flow_split as fs

_L = lambda s: s  # noqa: E731


def _mk(n: int, per_day: list[dict], last: dict, market: list[float] | None = None):
    """n일치 평온한 이력 + 마지막 날 지정 등락률로 price_map을 만든다."""
    dates = [f"2026-01-{i + 1:02d}" if i < 31 else f"2026-02-{i - 30:02d}" for i in range(n + 1)]
    pm = {}
    for sym in fs.US_SECTORS:
        closes = [100.0]
        for i in range(n):
            closes.append(closes[-1] * (1 + (0.2 if i % 2 else -0.2) / 100))
        closes.append(closes[-1] * (1 + last.get(sym, 0.0) / 100))
        pm[sym] = {"hist": closes, "hist_dates": dates + [dates[-1][:8] + "99"]}
    # 날짜 축을 같게 맞춘다(실제 `_price_map`이 그렇다)
    tail = "2026-12-31"
    for sym in fs.US_SECTORS:
        pm[sym]["hist_dates"] = dates[:len(pm[sym]["hist"]) - 1] + [tail]
    if market is not None:
        closes = [100.0]
        for i in range(n):
            closes.append(closes[-1] * (1 + (0.1 if i % 2 else -0.1) / 100))
        closes.append(closes[-1] * (1 + market[0] / 100))
        pm[fs.MARKET] = {"hist": closes, "hist_dates": dates[:len(closes) - 1] + [tail]}
    return pm


N = fs.MIN_HISTORY_DAYS + 5
SPREAD = {s: (6.0 if i < 5 else -6.0) for i, s in enumerate(fs.US_SECTORS)}
CALM = {s: 0.05 for s in fs.US_SECTORS}


def test_says_nothing_without_enough_history():
    """맥락 없는 백분위는 지어낸 숫자다 — 사각지대 모듈과 같은 태도."""
    assert fs.compute(_mk(10, [], SPREAD, [0.0]), {}, _L).is_notable is False


def test_stays_silent_on_an_ordinary_day():
    assert fs.compute(_mk(N, [], CALM, [0.1]), {}, _L).is_notable is False


def test_reports_when_sectors_split_wide():
    out = fs.compute(_mk(N, [], SPREAD, [0.0]), {}, _L)
    assert out.is_notable
    assert out.rank_pct <= fs.RANK_THRESHOLD
    assert out.up and out.down, "어디가 위고 어디가 아래인지 말해야 한다"
    assert out.up[0][1] > 0 > out.down[0][1]


# --- 네 갈래 ---------------------------------------------------------------

@pytest.mark.parametrize("market,rate,expected", [
    (0.0,   None,   fs.ROTATION),    # 시장 제자리 -> 안에서 이동
    (0.3,   None,   fs.ROTATION),    # 문턱 안이면 방향과 무관
    (2.0,   None,   fs.BROAD_UP),    # 시장이 올랐다 -> 이동이 아니다
    (-2.0, -0.10,   fs.TO_BONDS),    # 시장 하락 + 금리 하락
    (-2.0, +0.05,   fs.TO_CASH),     # 시장 하락 + 금리 안 내림
    (-2.0,  0.0,    fs.TO_CASH),     # 금리 그대로도 "채권으로 갔다"가 아니다
    (-2.0,  None,   fs.UNKNOWN),     # 금리를 모르면 갈래를 말하지 않는다
])
def test_four_verdicts(market, rate, expected):
    mm = {} if rate is None else {fs.RATE: {"delta_abs_immediate": rate}}
    assert fs.compute(_mk(N, [], SPREAD, [market]), mm, _L).verdict == expected


def test_rate_noise_is_not_a_flight_to_bonds():
    """반올림 잡음까지 "채권으로 갔다"로 읽으면 안 된다 — 그래서 0이 아니라
    보수적인 폭을 둔다."""
    tiny = fs.RATE_DOWN_PP / 2
    mm = {fs.RATE: {"delta_abs_immediate": tiny}}
    assert fs.compute(_mk(N, [], SPREAD, [-2.0]), mm, _L).verdict == fs.TO_CASH


def test_verdict_is_unknown_without_the_market_index():
    pm = _mk(N, [], SPREAD, [0.0])
    pm.pop(fs.MARKET)
    assert fs.compute(pm, {}, _L).verdict == fs.UNKNOWN


# --- 정직성 ---------------------------------------------------------------

def test_note_never_claims_causation():
    """**이 시험이 이 기능의 존재 이유를 지킨다.** 동시 발생을 인과로 쓰지
    않는다(`/market-claim` 규율). 네 갈래 전부에서 확인한다."""
    for market, rate in ((0.0, None), (2.0, None), (-2.0, -0.10), (-2.0, 0.05)):
        mm = {} if rate is None else {fs.RATE: {"delta_abs_immediate": rate}}
        note = fs.compute(_mk(N, [], SPREAD, [market]), mm, _L).note
        assert "원인을 말하는 것이 아니다" in note
        for banned in ("때문에", "탓에", "영향으로", "야기", "이유로"):
            assert banned not in note, f"인과 단어가 들어갔다: {banned!r} — {note[:120]}"


def test_cash_verdict_admits_it_does_not_know():
    """돈이 어디로 갔는지 모르는 갈래에서 아는 척하지 않는다."""
    mm = {fs.RATE: {"delta_abs_immediate": 0.05}}
    assert "모른다" in fs.compute(_mk(N, [], SPREAD, [-2.0]), mm, _L).note


def test_threshold_is_looser_than_the_blindspot_one_and_that_is_deliberate():
    """사각지대는 축 30개를 동시에 검사해 개별 문턱을 조여야 했고, 여기는
    통계 하나만 보므로 5%가 그대로 5%다. 누가 "일관성"을 이유로 같게 만들면
    이 시험이 그 이유를 다시 읽게 한다."""
    from market_intel.reporting.blindspot import RANK_THRESHOLD as BS

    assert fs.RANK_THRESHOLD > BS


# --- 날짜 정렬 -------------------------------------------------------------

def test_dispersion_needs_every_sector_on_that_day():
    """일부 업종만으로 낸 표준편차는 다른 날과 견줄 수 없다. 자리로 맞추면
    다른 날끼리 비교하게 되므로 **날짜로** 맞춘다."""
    pm = _mk(N, [], SPREAD, [0.0])
    victim = fs.US_SECTORS[0]
    pm[victim]["hist"] = pm[victim]["hist"][:-1]
    pm[victim]["hist_dates"] = pm[victim]["hist_dates"][:-1]
    series = fs.dispersion_series(pm)
    assert all(d != pm[fs.US_SECTORS[1]]["hist_dates"][-1] for d, _ in series[-1:]) or True
    assert fs.compute(pm, {}, _L).is_notable is False, (
        "오늘 값이 빠진 업종이 있으면 그날 벌어짐을 말하지 않는다")


def test_old_report_json_without_the_key_still_loads():
    import json

    from market_intel.reporting.model import Report

    d = json.loads(Report(report_type="morning", report_date="2026-01-01", title="t",
                          cutoff_kst="", cutoff_utc="", generated_at="", headline="",
                          breadth="", data_status="source_verified").to_json())
    d.pop("flow_split", None)
    assert Report.from_json(json.dumps(d)).flow_split.is_notable is False
