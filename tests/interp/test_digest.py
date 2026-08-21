"""SA-6 digest builder + evidence resolution tests."""
from __future__ import annotations

import dataclasses

from market_intel.interp import digest as digest_mod
from market_intel.reporting.model import CalendarRow, FactRow, MissingItem

from tests.interp.conftest import make_fact_row, make_report, macro_fc, price_fc, seed_fact


def test_build_header_includes_kr_market_breadth():
    """서브태스크 B spec §3 항목3: 시장 폭(관측기업 + 한국 전체)이 AI 해석이
    보는 다이제스트에도 실려야 한다. 안 실리면 화면은 "종목 폭은 평범"인데
    해석은 "시장이 5% 빠졌다"고 쓰는 모순이 생긴다."""
    report = dataclasses.replace(
        make_report(),
        breadth="관측기업 4/6개 상승 · 반도체·공급망 4/4\n"
                "코스피 -5.15%(근사)인데 오른 종목 455 / 내린 종목 419 — 상승비율 52%",
    )
    text, _ = digest_mod.build(report)
    assert "코스피 -5.15%(근사)인데 오른 종목 455 / 내린 종목 419 — 상승비율 52%" in text
    assert "시장 폭:" in text


def test_build_numbers_facts_then_market_reaction_continuously():
    facts = [make_fact_row("A", "1", "", subject="A", metric="m")]
    market_reaction = [
        make_fact_row("B", "2", "", subject="B", metric="m"),
        make_fact_row("C", "3", "", subject="C", metric="m"),
    ]
    report = make_report(facts=facts, market_reaction=market_reaction)
    text, findex = digest_mod.build(report)

    assert list(findex.keys()) == ["F1", "F2", "F3"]
    assert findex["F1"].subject == "A"
    assert findex["F2"].subject == "B"
    assert findex["F3"].subject == "C"
    assert "F1. A = 1 | " in text
    assert "F2. B = 2 | " in text
    assert "F3. C = 3 | " in text
    assert text.index("F1.") < text.index("F2.") < text.index("F3.")


def test_build_header_has_type_date_cutoff_headline():
    report = make_report(report_type="quarterly", report_date="2026-08-01",
                          cutoff_utc="2026-08-01T00:00:00+00:00", headline="헤드라인 문구")
    text, _ = digest_mod.build(report)
    assert "quarterly" in text
    assert "2026-08-01" in text
    assert "헤드라인 문구" in text


def test_build_missing_section():
    missing = [MissingItem(area="한국 수급", reason="KRX 인증벽", since="2026-08-01T00:00:00+00:00",
                            gap_id="kr_flows:net_buy")]
    report = make_report(facts=[], missing=missing)
    text, _ = digest_mod.build(report)
    assert "[결측]" in text
    assert "한국 수급" in text
    assert "KRX 인증벽" in text


def test_build_missing_section_empty_says_none():
    report = make_report(facts=[], missing=[])
    text, _ = digest_mod.build(report)
    assert "[결측]\n(없음)" in text


def test_build_events_capped_at_12():
    events = [
        CalendarRow(when=f"2026-09-{i:02d}", name=f"Event {i}", country="US", subject="X",
                    importance="A", change="", source_url="", data_status="확정")
        for i in range(1, 21)
    ]
    report = make_report(events=events)
    text, _ = digest_mod.build(report)
    assert text.count("Event ") == 12
    assert "Event 1\n" in text or text.endswith("Event 1")
    assert "Event 13" not in text


def test_build_no_source_urls_leaked():
    facts = [
        FactRow(
            label="A", value="1", comparison="", source_url="https://secret.example/api?key=SHOULD_NOT_LEAK",
            data_status="source_verified", known_at="2026-08-01T00:00:00+00:00", subject="A", metric="m",
        )
    ]
    report = make_report(facts=facts)
    text, _ = digest_mod.build(report)
    assert "secret.example" not in text
    assert "SHOULD_NOT_LEAK" not in text


def test_truncation_marker_and_is_truncated():
    # Build a report with enough fact lines to blow past MAX_CHARS.
    facts = [
        make_fact_row(f"항목{i}" * 20, "값" * 50, "비교" * 50, subject=f"S{i}", metric="m")
        for i in range(2000)
    ]
    report = make_report(facts=facts)
    text, findex = digest_mod.build(report)
    assert len(text) <= digest_mod.MAX_CHARS + 200  # small slack for the marker line itself
    assert digest_mod.is_truncated(text)
    assert len(findex) == 2000  # F-index still covers every original fact, even if the text dropped some


def test_not_truncated_for_normal_report():
    report = make_report()
    text, _ = digest_mod.build(report)
    assert not digest_mod.is_truncated(text)


def test_resolve_evidence_single_match(conn, raw_dir):
    fc = macro_fc("UNRATE", "2026-07-01T00:00:00+00:00", 4.2)
    known_at = "2026-08-01T00:00:00+00:00"
    fact_id = seed_fact(conn, raw_dir, "fred", fc, known_at)

    row = make_fact_row("미국 실업률", "4.20%", "", subject="UNRATE", metric="value", known_at=known_at)
    report = make_report(facts=[row])
    _text, findex = digest_mod.build(report)
    cutoff = "2026-08-02T00:00:00+00:00"

    evidence, unresolved = digest_mod.resolve_evidence(conn, cutoff, findex, ["F1의 실업률처럼 안정적이다."])
    assert unresolved == []
    assert len(evidence) == 1
    assert evidence[0][0] == "F1"
    assert evidence[0][1] == fact_id
    assert evidence[0][2] == 1  # first revision


def test_resolve_evidence_unresolved_for_invented_fnum(conn, raw_dir):
    row = make_fact_row("미국 실업률", "4.20%", "", subject="UNRATE", metric="value",
                        known_at="2026-08-01T00:00:00+00:00")
    report = make_report(facts=[row])
    _text, findex = digest_mod.build(report)

    evidence, unresolved = digest_mod.resolve_evidence(conn, "2026-08-02T00:00:00+00:00", findex, ["F99를 인용한다."])
    assert evidence == []
    assert unresolved == ["F99"]


def test_resolve_evidence_respects_cutoff(conn, raw_dir):
    """spec SA-10: evidence resolution must not resolve facts known after
    the report's own cutoff, even if the FactRow claims that known_at."""
    fc = macro_fc("UNRATE", "2026-07-01T00:00:00+00:00", 4.2)
    late_known_at = "2026-08-05T00:00:00+00:00"
    seed_fact(conn, raw_dir, "fred", fc, late_known_at)

    row = make_fact_row("미국 실업률", "4.20%", "", subject="UNRATE", metric="value", known_at=late_known_at)
    report = make_report(facts=[row])
    _text, findex = digest_mod.build(report)

    early_cutoff = "2026-08-01T00:00:00+00:00"  # before the fact was known
    evidence, unresolved = digest_mod.resolve_evidence(conn, early_cutoff, findex, ["F1을 본다."])
    assert evidence == []
    assert unresolved == ["F1"]


def test_resolve_evidence_no_citation_returns_empty(conn, raw_dir):
    report = make_report()
    _text, findex = digest_mod.build(report)
    evidence, unresolved = digest_mod.resolve_evidence(conn, "2026-08-02T00:00:00+00:00", findex, ["평범한 문장."])
    assert evidence == []
    assert unresolved == []
