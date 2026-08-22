"""수급 표에 **매수금·매도금**을 싣는 방식의 계약 (CEO 지시 2026-08-21).

## 왜 필요했나

리포트는 순매수만 실었다. 그런데 순매수는 **거래 규모를 지운다**:

    SK하이닉스 2026-08-21 개인 순매수 -1.26조
      -> 실제로는 3.10조를 팔면서 1.84조를 샀다.

같은 -1.26조라도 "조용히 1.26조어치 빠져나갔다"와 "5조가 오가는 중에 1.26조가
남았다"는 완전히 다른 이야기이고, 순매수 하나로는 어느 쪽인지 알 수 없다.

## 왜 제 줄로 싣지 않나

종목당 6줄이 더 붙으면 2026-08-03에 겨우 77행에서 줄인 표가 다시 불어난다.
그래서 **표본 합계의 순매수 줄 옆에 붙인다** — 줄은 안 늘고 규모는 보인다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from market_intel import db as db_mod
from market_intel.providers.kis_flows import GROSS_BY_ACTOR
from market_intel.reporting import build as build_mod

DAY = "2026-08-21"
CUTOFF = datetime(2026, 8, 22, 7, 15, tzinfo=timezone.utc)


def _conn(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _seed(conn, subject, metric, value, day=DAY):
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision_no, known_at, event_at, subject,"
        " category, metric, value_num, unit, publisher, comparison_basis, data_status) "
        "VALUES (?,1,?,?,?,'flow',?,?,'KRW','한국투자증권 KIS','','source_verified')",
        (f"{subject}:{metric}:{day}", f"{day}T06:30:00+00:00", f"{day}T06:30:00+00:00",
         subject, metric, value))
    conn.commit()


def _sample_symbol() -> str:
    from market_intel.universe import KOSPI_FLOW_SAMPLE_SYMBOLS
    return KOSPI_FLOW_SAMPLE_SYMBOLS[0]


def _seed_actor(conn, subject, actor, net, buy=None, sell=None):
    _seed(conn, subject, f"net_buy_{actor}_value", net)
    if buy is not None:
        _seed(conn, subject, GROSS_BY_ACTOR[actor][0], buy)
    if sell is not None:
        _seed(conn, subject, GROSS_BY_ACTOR[actor][1], sell)


def _rows(conn):
    rows, _missing = build_mod._kr_flows(conn, CUTOFF)
    return rows


def test_gross_rides_along_the_net_line_instead_of_making_its_own(settings):
    """줄 수가 늘지 않는 것이 이 설계의 핵심이다."""
    conn = _conn(settings)
    sym = _sample_symbol()
    _seed_actor(conn, sym, "individual", net=-1_000, buy=4_000, sell=5_000)

    rows = _rows(conn)
    labels = [r.label for r in rows]
    assert not any("매수금" in l or "매도금" in l for l in labels), "총액이 제 줄을 만들었다"
    assert not any(r.metric in {m for pair in GROSS_BY_ACTOR.values() for m in pair}
                   for r in rows), "총액 지표가 표에 그대로 실렸다"

    sample = [r for r in rows if r.subject == build_mod._FLOW_SAMPLE_SUBJECT]
    assert len(sample) == 1
    assert "매수" in sample[0].comparison and "매도" in sample[0].comparison


def test_the_numbers_on_that_line_are_the_gross_amounts(settings):
    conn = _conn(settings)
    sym = _sample_symbol()
    _seed_actor(conn, sym, "individual",
                net=-1_260_000_000_000, buy=1_840_000_000_000, sell=3_100_000_000_000)

    line = next(r for r in _rows(conn) if r.subject == build_mod._FLOW_SAMPLE_SUBJECT)
    assert "매수 1.8조 원" in line.comparison
    assert "매도 3.1조 원" in line.comparison


def test_a_missing_gross_says_nothing_rather_than_zero(settings):
    """**결측을 사실로 승격하지 않는다.** 0으로 채우면 "그날 아무도 안 샀다"가
    되는데, KIS가 그 칸을 안 준 날과 정말 0인 날은 다르다."""
    conn = _conn(settings)
    sym = _sample_symbol()
    _seed_actor(conn, sym, "individual", net=-1_000)   # 총액 없음

    line = next(r for r in _rows(conn) if r.subject == build_mod._FLOW_SAMPLE_SUBJECT)
    assert "매수" not in line.comparison and "매도" not in line.comparison
    assert "0" not in line.comparison.split("·")[-1]


def test_half_a_pair_is_not_enough(settings):
    """매수만 있고 매도가 없으면 "얼마 팔았나"에 답할 수 없다 — 반쪽을 싣느니
    침묵한다."""
    conn = _conn(settings)
    sym = _sample_symbol()
    _seed_actor(conn, sym, "individual", net=-1_000, buy=4_000)

    line = next(r for r in _rows(conn) if r.subject == build_mod._FLOW_SAMPLE_SUBJECT)
    assert "매수" not in line.comparison


def test_each_actor_gets_its_own_gross_not_another_actors(settings):
    conn = _conn(settings)
    sym = _sample_symbol()
    _seed_actor(conn, sym, "individual", net=-1_000, buy=1_000_000_000_000, sell=2_000_000_000_000)
    _seed_actor(conn, sym, "foreign", net=+500, buy=9_000_000_000_000, sell=8_000_000_000_000)

    lines = {r.label: r.comparison for r in _rows(conn)
             if r.subject == build_mod._FLOW_SAMPLE_SUBJECT}
    개인 = next(v for k, v in lines.items() if "개인" in k)
    외국인 = next(v for k, v in lines.items() if "외국인" in k)
    assert "매수 1.0조 원" in 개인 and "매도 2.0조 원" in 개인
    assert "매수 9.0조 원" in 외국인 and "매도 8.0조 원" in 외국인


def test_per_stock_rows_stay_free_of_gross(settings):
    """관측 기업(표본에 접히지 않는 종목)의 개별 줄에도 총액이 새면 안 된다."""
    conn = _conn(settings)
    from market_intel.universe import KR_CORE_SYMBOLS

    core = KR_CORE_SYMBOLS[0]
    _seed_actor(conn, core, "individual", net=-1_000, buy=4_000, sell=5_000)
    per_stock = [r for r in _rows(conn) if r.subject == core]
    assert per_stock, "관측 기업 줄이 있어야 한다"
    assert all("net_buy" in r.metric for r in per_stock), [r.metric for r in per_stock]


# --- 항등식: 주체별 순매수의 합은 0이다 (CEO 지시 2026-08-22) -------------------

def _gap_rows(conn):
    return list(conn.execute(
        "SELECT gap_id, reason FROM data_gaps WHERE gap_id=?",
        (build_mod.KR_FLOW_BALANCE_GAP_ID,)))


def test_a_balanced_day_raises_no_gap(settings):
    """네 주체가 다 있으면 합이 0이고, 조용해야 한다."""
    conn = _conn(settings)
    sym = _sample_symbol()
    for actor, net in (("individual", -1_000_000_000), ("foreign", 400_000_000),
                       ("institution", 300_000_000), ("etc", 300_000_000)):
        _seed_actor(conn, sym, actor, net=net)
    _rows(conn)
    assert not _gap_rows(conn)


def test_an_unbalanced_day_is_reported_not_swallowed(settings):
    """**이 검사가 없어서 결함이 조용히 발행됐다.** 셋만 싣던 시절 SK하이닉스에서
    -1조1,299억이 남았는데 아무도 몰랐다 — 그 1조는 어디로도 가지 않았고, 네
    번째 주체가 사간 것이었다."""
    conn = _conn(settings)
    sym = _sample_symbol()
    _seed_actor(conn, sym, "individual", net=-1_129_900_000_000)
    _seed_actor(conn, sym, "foreign", net=13_800_000_000)
    _seed_actor(conn, sym, "institution", net=111_400_000_000)
    # 기타를 일부러 빼 둔다 = 옛 상태 그대로.
    _rows(conn)
    gaps = _gap_rows(conn)
    assert gaps, "합이 0이 아닌데 아무 말도 하지 않았다"
    assert "0이 아니다" in gaps[0][1] and "억원" in gaps[0][1]


def test_rounding_noise_is_not_reported_as_a_defect(settings):
    """원본이 백만원 단위로 반올림해 주므로 잔차는 원래 조금 생긴다(실측 최대
    0.5억). 그걸 매일 신고하면 신고 자체가 배경 소음이 된다."""
    conn = _conn(settings)
    sym = _sample_symbol()
    _seed_actor(conn, sym, "individual", net=-1_000_000_000)
    _seed_actor(conn, sym, "foreign", net=400_000_000)
    _seed_actor(conn, sym, "institution", net=300_000_000)
    _seed_actor(conn, sym, "etc", net=350_000_000)   # 5,000만원 어긋남
    _rows(conn)
    assert not _gap_rows(conn)


def test_etc_is_drawn_in_the_flow_chart_too(settings):
    """막대가 좌우로 맞으려면 네 번째 주체가 그림에도 있어야 한다."""
    from market_intel.reporting.render_md import FLOW_ACTORS, flow_groups

    assert "etc" in dict(FLOW_ACTORS)
    conn = _conn(settings)
    sym = _sample_symbol()
    for actor, net in (("individual", -1_129_900_000_000), ("foreign", 13_800_000_000),
                       ("institution", 111_400_000_000), ("etc", 1_004_700_000_000)):
        _seed_actor(conn, sym, actor, net=net)
    groups = flow_groups([r for r in _rows(conn) if r.group == "flow"])
    assert groups, "수급 그림에 줄이 있어야 한다"
    actors = {a["label"] for g in groups for a in g["actors"]}
    assert "기타" in actors, f"기타가 그림에 없다: {actors}"


def test_the_stock_name_is_stripped_for_the_etc_row_too(settings):
    """라벨 정규식에 기타를 안 넣으면 그림의 종목 이름이 "삼성전자(005930.KS)
    기타 순매수(금액)"로 남는다 — 다른 주체와 다른 이름이 되어 한 줄로 안 묶인다."""
    from market_intel.reporting.render_md import flow_groups

    conn = _conn(settings)
    sym = _sample_symbol()
    for actor, net in (("individual", -1_000_000_000_000), ("etc", 1_000_000_000_000)):
        _seed_actor(conn, sym, actor, net=net)
    groups = flow_groups([r for r in _rows(conn) if r.group == "flow"])
    # 표본이면서 관측 기업인 종목이라 합계 줄과 개별 줄 둘 다 나온다 — 그게
    # 정상이고, 이 시험이 보는 것은 **이름이 깨끗한가**다.
    names = [g["name"] for g in groups]
    assert names, "수급 그림에 줄이 있어야 한다"
    for name in names:
        assert "순매수" not in name and "기타" not in name, f"주체가 이름에 남았다: {name}"
