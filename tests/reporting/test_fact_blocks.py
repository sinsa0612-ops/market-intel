"""핵심 사실을 갈래별 모양으로 — 수급 막대 · 거시 카드 · 공시 요약.

한 표에 다 넣으면 실측 77행이 되고 아무것도 한눈에 안 들어온다는 CEO 지적
(2026-08-03). 여기서 지키는 것은 **줄어든 줄 수**가 아니라, 줄이면서 사실을
잃지 않았다는 것이다: 접힌 것은 펼칠 수 있어야 하고, 새 모양이 없는 갈래는
쓰던 표로 떨어져야 하며, 막대만 남고 문장이 사라지면 안 된다.
"""
from __future__ import annotations

from market_intel.reporting.model import FactRow
from market_intel.reporting.render_html import _block_html
from market_intel.reporting.render_md import (
    _block_md,
    fact_blocks,
    filing_summary,
    flow_groups,
    fmt_money,
)


def flow_row(subject: str, name: str, actor: str, value: float, *, shares: bool = False) -> FactRow:
    ko = {"individual": "개인", "institution": "기관", "foreign": "외국인"}[actor]
    unit = "주" if shares else "금액"
    metric = f"net_buy_{actor}" if shares else f"net_buy_{actor}_value"
    return FactRow(
        label=f"{name}({subject}) {ko} 순매수({unit})", value=f"{value:,.0f}",
        comparison="2026-08-03 · 한국투자증권 KIS", source_url="", data_status="source_verified",
        known_at="2026-08-03T07:00:00+00:00", subject=subject, metric=metric,
        raw_value=value, group="flow" if not shares else "",
    )


def macro_row(subject: str, label: str, value: str, delta: float | None) -> FactRow:
    return FactRow(label=label, value=value, comparison="", source_url="",
                   data_status="source_verified", known_at="2026-08-03T00:00:00+00:00",
                   subject=subject, metric="value", delta_pct=delta, group="macro")


def filing_row(subject: str, metric: str) -> FactRow:
    return FactRow(label=f"{subject} 공시", value="2026-07-29 제출", comparison="",
                   source_url="https://example.test/d", data_status="source_verified",
                   known_at="2026-07-30T00:00:00+00:00", subject=subject, metric=metric,
                   group="filing")


SAMSUNG = [
    flow_row("005930.KS", "삼성전자", "individual", 2_100_000_000_000.0),
    flow_row("005930.KS", "삼성전자", "institution", -1_220_000_000_000.0),
    flow_row("005930.KS", "삼성전자", "foreign", -945_500_000_000.0),
]


# --- 금액 표기 --------------------------------------------------------------

def test_fmt_money_is_readable_at_korean_scale():
    """2,174,506,000,000은 자릿수를 세야 읽힌다 — 그것이 CEO가 지적한 그 표다."""
    assert fmt_money(2_174_506_000_000.0, "KRW") == "2.2조 원"
    assert fmt_money(-450_060_000_000.0, "KRW") == "-4,501억 원"
    assert fmt_money(19_639_000_000.0, "USD") == "196.4억 달러"
    assert fmt_money(None, "KRW") == "미확인"
    # 조 미만은 억으로, 억 미만은 원 그대로 — 단위가 바뀌는 자리를 못박는다.
    assert fmt_money(999_900_000_000.0, "KRW") == "9,999억 원"
    assert fmt_money(1_000_000_000_000.0, "KRW") == "1.0조 원"


# --- 1) 수급 ----------------------------------------------------------------

def test_flow_groups_collapse_to_one_line_per_symbol():
    groups = flow_groups(SAMSUNG)
    assert len(groups) == 1
    g = groups[0]
    assert g["name"] == "삼성전자(005930.KS)"
    assert [a["label"] for a in g["actors"]] == ["개인", "기관", "외국인"]
    # 막대 안 금액은 절댓값 — 방향은 색과 문장이 말한다.
    assert all(not a["text"].startswith("-") for a in g["actors"])
    assert [a["buy"] for a in g["actors"]] == [True, False, False]


def test_flow_story_names_every_side_with_its_size():
    """막대는 흑백 출력·화면 낭독기에서 아무 말도 못 한다. 문장이 그 자리를
    대신하므로 규모가 빠지면 안 된다."""
    story = flow_groups(SAMSUNG)[0]["story"]
    assert "개인이 2.1조 원 담고" in story
    assert "기관·외국인이" in story and "던졌다" in story


def test_flow_sorts_biggest_first_and_folds_the_quiet_ones():
    small = [flow_row("105560.KS", "KB금융", "individual", 1_000_000_000.0),
             flow_row("105560.KS", "KB금융", "foreign", -1_000_000_000.0)]
    groups = flow_groups(SAMSUNG + small)
    assert [g["subject"] for g in groups] == ["005930.KS", "105560.KS"]
    assert groups[0]["quiet"] is False
    assert groups[1]["quiet"] is True
    assert "특이사항 없음" in groups[1]["story"]


def test_flow_bar_widths_are_proportional_and_labelled():
    html = _block_html({"kind": "flow", "rows": SAMSUNG})
    assert 'class="buy"' in html and 'class="sell"' in html
    # 개인 2.1조 / 합 4.265조 ≈ 0.4924
    assert "flex:0.49" in html
    assert "2.1조 원" in html
    # 색만으로 정보를 주지 않는다 — 문장이 같은 마크업 안에 있어야 한다.
    assert "담고" in html


def test_flow_shares_only_source_still_gets_a_table_not_a_bar():
    """금액 없이 주식 수만 주는 소스가 있다(걷어낸 pykrx가 그랬다). 막대 길이는
    서로 더할 수 있는 양이라는 뜻인데
    종목이 다른 주식 수는 더할 수 없다 — 그런 행은 쓰던 표로 떨어져야 한다."""
    shares = [flow_row("KOSPI", "코스피", "foreign", 123456.0, shares=True)]
    blocks = fact_blocks(shares)
    assert [b["kind"] for b in blocks] == ["facts"]
    assert blocks[0]["rows"] == shares


# --- 2) 거시 ----------------------------------------------------------------

def test_macro_cards_lead_with_rates_and_fold_the_rest():
    rows = [
        macro_row("CPIAUCSL", "미국 CPI", "332.57", -0.42),
        macro_row("PAYEMS", "미국 비농업고용", "158,984", 0.04),
        macro_row("RSAFS", "미국 소매판매", "768,553", 0.22),
        macro_row("INDPRO", "미국 산업생산지수", "102.64", 0.08),
        macro_row("PCEPI", "미국 PCE", "131.39", -0.11),
        macro_row("DGS10", "미국채 10년물 금리", "4.68 %", 0.21),
        macro_row("DGS2", "미국채 2년물 금리", "4.23 %", 0.24),
        macro_row("UNRATE", "미국 실업률", "4.20 %", -2.33),
        macro_row("T10Y2Y", "미 10Y-2Y 금리차", "0.47 %", 4.44),
    ]
    block = next(b for b in fact_blocks(rows) if b["kind"] == "macro_cards")
    assert [r.subject for r in block["rows"]][:3] == ["DGS10", "DGS2", "T10Y2Y"]
    assert len(block["rows"]) == 8 and len(block["rest"]) == 1
    # 접은 것은 반드시 펼칠 수 있어야 한다 — 사실이 사라지면 안 된다.
    html = _block_html(block)
    assert "<details>" in html and block["rest"][0].label in html


def test_macro_flat_says_no_change_instead_of_plus_zero():
    """기준금리처럼 몇 달째 그대로인 값이 매일 +0.00%를 찍으면 읽는 사람이
    그것도 움직인 값으로 훑게 된다."""
    block = {"kind": "macro_cards",
             "rows": [macro_row("base_rate", "한국 기준금리", "2.50 연%", 0.0)], "rest": []}
    html = _block_html(block)
    assert "변화 없음" in html and "+0.00%" not in html


# --- 3) 공시 ----------------------------------------------------------------

def test_filing_summary_answers_who_reported_earnings():
    rows = ([filing_row(s, "earnings_release_8k") for s in ("MSFT", "AMZN")]
            + [filing_row(s, "filing_event") for s in ("GOOGL", "META", "XOM")])
    s = filing_summary(rows)
    assert (s["total"], s["earnings_count"], s["other_count"]) == (5, 2, 3)
    assert s["earnings_subjects"] == ["MSFT", "AMZN"]

    html = _block_html({"kind": "filing_summary", "rows": rows})
    assert "공시 5건" in html and "MSFT · AMZN" in html
    assert "<details>" in html and "XOM" in html  # 전체는 접혀 있을 뿐 남아 있다


# --- 갈래 나누기 ------------------------------------------------------------

def test_fact_blocks_split_by_group_and_keep_everything():
    rows = SAMSUNG + [macro_row("DGS10", "미국채 10년물 금리", "4.68 %", 0.21),
                      filing_row("MSFT", "earnings_release_8k")]
    blocks = fact_blocks(rows)
    assert [b["kind"] for b in blocks] == ["flow", "macro_cards", "filing_summary"]
    counted = sum(len(b.get("rows", [])) + len(b.get("rest", [])) for b in blocks)
    assert counted == len(rows), "갈래로 나누면서 사실이 하나라도 사라지면 안 된다"


def test_ungrouped_facts_fall_back_to_the_old_table():
    """갈래가 없는 사실 — 재무·컨센서스, 그리고 이 필드가 없던 옛 리포트 JSON —
    은 지금까지 쓰던 표로 떨어져야 한다. 새 모양이 없다고 사라지면 안 된다."""
    legacy = FactRow(label="MSFT 매출", value="900.1억 달러", comparison="분기",
                     source_url="", data_status="source_verified", known_at="",
                     subject="MSFT", metric="revenue")
    assert legacy.group == ""
    blocks = fact_blocks([legacy])
    assert [b["kind"] for b in blocks] == ["facts"]
    assert "MSFT 매출" in _block_html(blocks[0])
    assert "MSFT 매출" in _block_md(blocks[0])


def test_markdown_keeps_the_same_structure_without_colour():
    """마크다운(Obsidian)에는 막대도 색도 없다. 그래도 **종목당 한 줄**이라는
    요점과 접기 규칙은 같아야 한다 — 두 형식이 다른 사실을 말하면 안 된다."""
    md = _block_md({"kind": "flow", "rows": SAMSUNG})
    assert md.count("\n") == 2  # 머리글 + 구분선 + 종목 한 줄
    assert "삼성전자" in md and "2.1조 원" in md and "담고" in md
