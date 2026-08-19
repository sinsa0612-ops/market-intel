"""ST1 acceptance tests (spec `_org/20260801-mi-interp/spec.md` §SHARED ARCHITECTURE
SA-2/SA-7, `### ST1`) for the deterministic thesis/verdict engine.

No LLM anywhere in this file or in the code it exercises. Every seeded fact
goes through the real write path (`db.insert_raw_snapshot` + `db.upsert_fact`,
via `conftest.seed_fact`) — never a hand-written SQL INSERT — and every read
goes through `db.facts_as_of` (checked explicitly in
`test_evaluate_atom_never_bypasses_facts_as_of`).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.interp import store as store_mod
from market_intel.interp import thesis as thesis_mod

from conftest import fin_fc, macro_fc, price_fc, seed_fact

UTC = timezone.utc


def _cutoff(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# 4 atom kinds x TRUE / FALSE / UNKNOWN
# ---------------------------------------------------------------------------

def test_threshold_true(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    atom = {"id": "a", "kind": "threshold", "category": "macro", "subject": "DGS10", "metric": "value", "op": ">=", "value": 4.5}
    status, detail = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "TRUE"
    assert "message" in detail


def test_threshold_false(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    atom = {"id": "a", "kind": "threshold", "category": "macro", "subject": "DGS10", "metric": "value", "op": ">=", "value": 5.0}
    status, _ = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "FALSE"


def test_threshold_unknown_no_observation(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    atom = {"id": "a", "kind": "threshold", "category": "macro", "subject": "DGS10", "metric": "value", "op": ">=", "value": 4.5}
    status, detail = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "UNKNOWN"
    assert detail["observed"] == 0


def test_change_pct_true(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "sec_edgar", fin_fc("NVDA", "revenue", "2025-04-26T00:00:00+00:00", 26914000000.0), "2025-05-01T00:00:00+00:00")
    seed_fact(conn, settings.raw_dir, "sec_edgar", fin_fc("NVDA", "revenue", "2026-04-26T00:00:00+00:00", 81615000000.0), "2026-05-01T00:00:00+00:00")
    atom = {"id": "a", "kind": "change_pct", "category": "financials", "subject": "NVDA", "metric": "revenue", "op": ">=", "value": 20.0, "lookback": 1}
    status, detail = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "TRUE"
    assert detail["change_pct"] == pytest.approx(203.19, rel=0.01)


def test_change_pct_false(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "sec_edgar", fin_fc("MSFT", "revenue", "2025-06-30T00:00:00+00:00", 300000000000.0), "2025-07-01T00:00:00+00:00")
    seed_fact(conn, settings.raw_dir, "sec_edgar", fin_fc("MSFT", "revenue", "2026-06-30T00:00:00+00:00", 305000000000.0), "2026-07-01T00:00:00+00:00")
    atom = {"id": "a", "kind": "change_pct", "category": "financials", "subject": "MSFT", "metric": "revenue", "op": ">=", "value": 20.0, "lookback": 1}
    status, _ = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "FALSE"


def test_change_pct_unknown_not_enough_observations(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "sec_edgar", fin_fc("MSFT", "free_cash_flow", "2026-06-30T00:00:00+00:00", 66987000000.0), "2026-08-01T02:00:00+00:00")
    atom = {"id": "a", "kind": "change_pct", "category": "financials", "subject": "MSFT", "metric": "free_cash_flow", "op": ">=", "value": 10.0, "lookback": 1}
    status, detail = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-02T00:00:00+00:00"))
    assert status == "UNKNOWN"
    assert detail["observed"] == 1 and detail["required"] == 2


def test_consecutive_true(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for d, v in [("2026-07-29", 190.0), ("2026-07-30", 195.0), ("2026-07-31", 200.0)]:
        seed_fact(conn, settings.raw_dir, "yfinance", price_fc("NVDA", f"{d}T20:00:00+00:00", v), "2026-08-01T02:00:00+00:00")
    atom = {"id": "a", "kind": "consecutive", "category": "price", "subject": "NVDA", "metric": "price_close", "direction": "up", "periods": 2}
    status, detail = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T12:00:00+00:00"))
    assert status == "TRUE"


def test_consecutive_false(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    for d, v in [("2026-07-29", 190.0), ("2026-07-30", 195.0), ("2026-07-31", 192.0)]:
        seed_fact(conn, settings.raw_dir, "yfinance", price_fc("NVDA", f"{d}T20:00:00+00:00", v), "2026-08-01T02:00:00+00:00")
    atom = {"id": "a", "kind": "consecutive", "category": "price", "subject": "NVDA", "metric": "price_close", "direction": "up", "periods": 2}
    status, _ = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T12:00:00+00:00"))
    assert status == "FALSE"


def test_consecutive_unknown_when_observations_equal_periods():
    """`periods=2` requires periods+1=3 observations; exactly 2 must be UNKNOWN
    (spec ST1 TDD 순서: "consecutive는 관측 2개일 때 periods=2가 UNKNOWN인지 반드시 확인")."""
    import tempfile
    from market_intel.config import Settings
    with tempfile.TemporaryDirectory() as td:
        settings = Settings(db_path=f"{td}/mi.db", raw_dir=f"{td}/raw", log_dir=f"{td}/logs",
                             fred_api_key="", ecos_api_key="", dart_api_key="", sec_user_agent="t")
        db_mod.init_db(settings.db_path)
        conn = db_mod.connect(settings.db_path)
        for d, v in [("2026-07-30", 195.0), ("2026-07-31", 200.0)]:
            seed_fact(conn, settings.raw_dir, "yfinance", price_fc("NVDA", f"{d}T20:00:00+00:00", v), "2026-08-01T02:00:00+00:00")
        atom = {"id": "a", "kind": "consecutive", "category": "price", "subject": "NVDA", "metric": "price_close", "direction": "up", "periods": 2}
        status, detail = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T12:00:00+00:00"))
        assert status == "UNKNOWN"
        assert detail["observed"] == 2 and detail["required"] == 3


def test_stale_true(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "sec_edgar", fin_fc("TSM", "revenue", "2020-07-26T00:00:00+00:00", 1.0), "2020-08-01T00:00:00+00:00")
    atom = {"id": "a", "kind": "stale", "category": "financials", "subject": "TSM", "metric": "revenue", "days": 200}
    status, _ = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "TRUE"


def test_stale_false(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "sec_edgar", fin_fc("TSM", "revenue", "2026-07-26T00:00:00+00:00", 1.0), "2026-07-27T00:00:00+00:00")
    atom = {"id": "a", "kind": "stale", "category": "financials", "subject": "TSM", "metric": "revenue", "days": 200}
    status, _ = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "FALSE"


def test_stale_unknown_no_observation(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    atom = {"id": "a", "kind": "stale", "category": "financials", "subject": "TSM", "metric": "revenue", "days": 200}
    status, detail = thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert status == "UNKNOWN"
    assert detail["observed"] == 0


def test_evaluate_atom_never_bypasses_facts_as_of(settings, monkeypatch):
    """정보 차단선(BRIEF 규칙 9): evaluate_atom must read facts exclusively
    through db.facts_as_of. If that function is never called, evaluate_atom
    could only be reading raw_ref/SQL directly — a blackout violation."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")

    calls = []
    real = db_mod.facts_as_of

    def _spy(conn_, cutoff, **filters):
        calls.append((cutoff, filters))
        return real(conn_, cutoff, **filters)

    monkeypatch.setattr(thesis_mod.db_mod, "facts_as_of", _spy)
    atom = {"id": "a", "kind": "threshold", "category": "macro", "subject": "DGS10", "metric": "value", "op": ">=", "value": 4.5}
    thesis_mod.evaluate_atom(conn, atom, _cutoff("2026-08-01T00:00:00+00:00"))
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Verdict order (SA-7's 5-step table)
# ---------------------------------------------------------------------------

def _thesis(thesis_id="t1", theme="ai_semi", slot=1, falsify=None, weaken=None, strengthen=None):
    return {
        "thesis_id": thesis_id, "theme": theme, "slot": slot,
        "statement": "test thesis", "leading_indicators": ["DGS10 value"],
        "next_check_date": "2026-12-01",
        "conditions": {"falsify": falsify or [], "weaken": weaken or [], "strengthen": strengthen or []},
    }


def _threshold_atom(aid, value, op=">="):
    return {"id": aid, "kind": "threshold", "category": "macro", "subject": "DGS10", "metric": "value", "op": op, "value": value}


def test_verdict_all_unknown_is_undecidable(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    th = _thesis(falsify=[_threshold_atom("f1", 999.0)])  # no DGS10 fact seeded -> UNKNOWN
    results = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert results[0]["verdict"] == "판정 불가"


def test_verdict_falsify_all_true_is_void(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    th = _thesis(falsify=[_threshold_atom("f1", 4.0, ">=")])  # 4.68 >= 4.0 True
    results = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert results[0]["verdict"] == "무효"


def test_verdict_falsify_partially_unknown_is_not_void(settings):
    """`falsify`의 원자 일부가 UNKNOWN이면 전부 TRUE가 아니므로 무효가 아니다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    known_true = _threshold_atom("f1", 4.0, ">=")  # TRUE
    unknown = {"id": "f2", "kind": "threshold", "category": "macro", "subject": "UNRATE", "metric": "value", "op": ">=", "value": 3.0}  # not seeded -> UNKNOWN
    th = _thesis(falsify=[known_true, unknown])
    results = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert results[0]["verdict"] != "무효"
    assert results[0]["verdict"] == "유지"  # nothing else fires


def test_verdict_weaken_true(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    th = _thesis(
        falsify=[_threshold_atom("f1", 100.0, ">=")],  # False
        weaken=[_threshold_atom("w1", 4.0, ">=")],  # True
    )
    results = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert results[0]["verdict"] == "약화"


def test_verdict_strengthen_true(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    th = _thesis(
        falsify=[_threshold_atom("f1", 100.0, ">=")],  # False
        weaken=[_threshold_atom("w1", 100.0, ">=")],  # False
        strengthen=[_threshold_atom("s1", 4.0, ">=")],  # True
    )
    results = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert results[0]["verdict"] == "강화"


def test_verdict_maintained_when_nothing_fires(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    th = _thesis(
        falsify=[_threshold_atom("f1", 100.0, ">=")],  # False
        weaken=[_threshold_atom("w1", 100.0, ">=")],  # False
        strengthen=[_threshold_atom("s1", 100.0, ">=")],  # False
    )
    results = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert results[0]["verdict"] == "유지"


def test_verdict_weaken_beats_strengthen_when_both_true(settings):
    """SA-7 순서: weaken 검사(3)가 strengthen 검사(4)보다 먼저다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    th = _thesis(
        falsify=[_threshold_atom("f1", 100.0, ">=")],  # False
        weaken=[_threshold_atom("w1", 4.0, ">=")],  # True
        strengthen=[_threshold_atom("s1", 4.0, ">=")],  # True
    )
    results = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert results[0]["verdict"] == "약화"


def test_review_records_prev_verdict_and_changed(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    th = _thesis(thesis_id="t1", strengthen=[_threshold_atom("s1", 4.0, ">=")], falsify=[_threshold_atom("f1", 100.0, ">=")])

    r1 = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert r1[0]["prev_verdict"] is None
    assert r1[0]["changed"] == 0
    store_mod.record_reviews(conn, r1)

    # Now weaken fires instead -> verdict flips.
    th2 = _thesis(thesis_id="t1", weaken=[_threshold_atom("w1", 4.0, ">=")], falsify=[_threshold_atom("f1", 100.0, ">=")])
    r2 = thesis_mod.review(conn, [th2], _cutoff("2026-08-02T00:00:00+00:00"), "morning", "2026-08-02")
    assert r2[0]["prev_verdict"] == "강화"
    assert r2[0]["verdict"] == "약화"
    assert r2[0]["changed"] == 1


# ---------------------------------------------------------------------------
# Information barrier (cutoff)
# ---------------------------------------------------------------------------

def test_review_respects_cutoff_blackout(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    # Known only AFTER the early cutoff.
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-08-05T00:00:00+00:00")
    th = _thesis(strengthen=[_threshold_atom("s1", 4.0, ">=")], falsify=[_threshold_atom("f1", 100.0, ">=")])
    early = thesis_mod.review(conn, [th], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    assert early[0]["verdict"] == "판정 불가"  # fact not known yet
    late = thesis_mod.review(conn, [th], _cutoff("2026-08-06T00:00:00+00:00"), "morning", "2026-08-06")
    assert late[0]["verdict"] == "강화"


# ---------------------------------------------------------------------------
# theses/theses.json loader — rejection rules (SA-7)
# ---------------------------------------------------------------------------

def _valid_thesis_dict(id_="ai_semi_1", slot=1, falsify_kind="threshold", **overrides):
    d = {
        "id": id_, "slot": slot,
        "statement": "테스트 가설",
        "leading_indicators": ["DGS10 value"],
        "next_check_date": "2026-12-01",
        "conditions": {
            "falsify": [{"id": "f1", "kind": "threshold", "category": "macro", "subject": "DGS10", "metric": "value", "op": ">=", "value": 100.0}],
            "weaken": [],
            "strengthen": [],
        },
    }
    d.update(overrides)
    return d


def _valid_file(themes_overrides=None):
    themes = {
        "ai_semi": {"label": "AI·반도체", "theses": [_valid_thesis_dict()]},
        "power_energy": {"label": "전력·에너지", "theses": []},
        "fin_credit": {"label": "금융·신용", "theses": []},
        "consumer_cycle": {"label": "소비·경기", "theses": []},
        "policy_geo": {"label": "정책·지정학", "theses": []},
    }
    if themes_overrides:
        themes.update(themes_overrides)
    return {"schema_version": "thesis.1", "themes": themes}


def _write(tmp_path, data, name="theses.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_load_file_accepts_valid_file(tmp_path):
    path = _write(tmp_path, _valid_file())
    loaded = thesis_mod.load_file(path)
    assert len(loaded) == 1
    assert loaded[0]["theme"] == "ai_semi" and loaded[0]["slot"] == 1


def test_load_rejects_no_falsify(tmp_path):
    bad = _valid_thesis_dict()
    bad["conditions"]["falsify"] = []
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_empty_leading_indicators(tmp_path):
    bad = _valid_thesis_dict()
    bad["leading_indicators"] = []
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_missing_next_check_date(tmp_path):
    bad = _valid_thesis_dict()
    del bad["next_check_date"]
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_non_iso_next_check_date(tmp_path):
    bad = _valid_thesis_dict()
    bad["next_check_date"] = "다음 달 언젠가"
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_slot_out_of_range(tmp_path):
    bad = _valid_thesis_dict(slot=4)
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_duplicate_slot_in_same_theme(tmp_path):
    a = _valid_thesis_dict(id_="ai_semi_1", slot=1)
    b = _valid_thesis_dict(id_="ai_semi_2", slot=1)
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [a, b]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_unknown_theme_key(tmp_path):
    data = _valid_file()
    data["themes"]["not_a_real_theme"] = {"label": "가짜", "theses": [_valid_thesis_dict()]}
    path = _write(tmp_path, data)
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_unknown_atom_kind(tmp_path):
    bad = _valid_thesis_dict()
    bad["conditions"]["falsify"] = [{"id": "f1", "kind": "moon_phase", "category": "macro", "subject": "DGS10", "metric": "value"}]
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_unknown_op(tmp_path):
    bad = _valid_thesis_dict()
    bad["conditions"]["falsify"] = [{"id": "f1", "kind": "threshold", "category": "macro", "subject": "DGS10", "metric": "value", "op": "~=", "value": 1.0}]
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_unknown_direction(tmp_path):
    bad = _valid_thesis_dict()
    bad["conditions"]["falsify"] = [{"id": "f1", "kind": "consecutive", "category": "price", "subject": "NVDA", "metric": "price_close", "direction": "sideways", "periods": 2}]
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_rejects_theme_with_4_or_more_theses(tmp_path):
    theses4 = [_valid_thesis_dict(id_=f"ai_semi_{i}", slot=min(i, 3)) for i in range(1, 5)]
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": theses4}}))
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(path)


def test_load_error_lists_all_reasons_in_human_language(tmp_path):
    bad = _valid_thesis_dict()
    bad["conditions"]["falsify"] = []
    bad["leading_indicators"] = []
    path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}))
    with pytest.raises(thesis_mod.ThesisLoadError) as exc_info:
        thesis_mod.load_file(path)
    reasons = exc_info.value.reasons
    assert len(reasons) >= 2
    assert all(isinstance(r, str) and r for r in reasons)


def test_load_rejection_leaves_db_unchanged(settings, tmp_path):
    """전부 적재 실패, DB 무변화 (부분 적재 금지)."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    good_path = _write(tmp_path, _valid_file(), name="good.json")
    good = thesis_mod.load_file(good_path)
    store_mod.replace_theses(conn, good, "sha-good")
    before = store_mod.list_theses(conn)
    assert len(before) == 1

    bad = _valid_thesis_dict()
    bad["conditions"]["falsify"] = []
    bad_path = _write(tmp_path, _valid_file({"ai_semi": {"label": "AI·반도체", "theses": [bad]}}), name="bad.json")
    with pytest.raises(thesis_mod.ThesisLoadError):
        thesis_mod.load_file(bad_path)
        # (load_file itself never touches the DB; this asserts the CLI-level
        #  contract that a raised ThesisLoadError must precede any DB write.)

    after = store_mod.list_theses(conn)
    assert after == before


# ---------------------------------------------------------------------------
# Schema-level cap enforcement (spec: "16번째가 물리적으로 안 들어가야 한다")
# ---------------------------------------------------------------------------

THEMES5 = ["ai_semi", "power_energy", "fin_credit", "consumer_cycle", "policy_geo"]


def test_schema_physically_rejects_16th_thesis_row(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    now = db_mod.iso_utc()
    n = 0
    for theme in THEMES5:
        for slot in (1, 2, 3):
            conn.execute(
                "INSERT INTO theses(thesis_id, theme, slot, statement, conditions_json, "
                "leading_indicators, next_check_date, source_sha256, loaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{theme}_{slot}", theme, slot, "s", "{}", "[]", "2026-12-01", "sha", now),
            )
            n += 1
    conn.commit()
    assert n == 15

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO theses(thesis_id, theme, slot, statement, conditions_json, "
            "leading_indicators, next_check_date, source_sha256, loaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("ai_semi_extra", "ai_semi", 1, "s", "{}", "[]", "2026-12-01", "sha", now),
        )


def test_schema_rejects_theme_outside_the_5(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    now = db_mod.iso_utc()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO theses(thesis_id, theme, slot, statement, conditions_json, "
            "leading_indicators, next_check_date, source_sha256, loaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("x", "crypto_meme", 1, "s", "{}", "[]", "2026-12-01", "sha", now),
        )


def test_schema_rejects_slot_outside_1_3(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    now = db_mod.iso_utc()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO theses(thesis_id, theme, slot, statement, conditions_json, "
            "leading_indicators, next_check_date, source_sha256, loaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("x", "ai_semi", 4, "s", "{}", "[]", "2026-12-01", "sha", now),
        )


# ---------------------------------------------------------------------------
# append-only thesis_reviews
# ---------------------------------------------------------------------------

def test_thesis_reviews_is_append_only(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    now = db_mod.iso_utc()
    conn.execute(
        "INSERT INTO thesis_reviews(review_id, thesis_id, report_type, report_date, cutoff_utc, "
        "verdict, prev_verdict, changed, atoms_json, evidence_json, engine_version, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "t1", "morning", "2026-08-01", "2026-08-01T00:00:00+00:00", "유지", None, 0, "{}", "{}", "2b.1", now),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE thesis_reviews SET verdict='강화' WHERE review_id='r1'")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM thesis_reviews WHERE review_id='r1'")


# ---------------------------------------------------------------------------
# render_impact / render_next_check_suffix
# ---------------------------------------------------------------------------

def test_render_impact_summarizes_counts_and_reasons(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    seed_fact(conn, settings.raw_dir, "fred", macro_fc("DGS10", "2026-07-30T00:00:00+00:00", 4.68), "2026-07-30T12:00:00+00:00")
    th_ok = _thesis(thesis_id="t1", theme="ai_semi", slot=1, strengthen=[_threshold_atom("s1", 4.0, ">=")], falsify=[_threshold_atom("f1", 100.0, ">=")])
    th_unknown = _thesis(thesis_id="t2", theme="power_energy", slot=1,
                          falsify=[{"id": "f2", "kind": "threshold", "category": "financials", "subject": "AEP", "metric": "free_cash_flow", "op": ">=", "value": 0.0}])
    results = thesis_mod.review(conn, [th_ok, th_unknown], _cutoff("2026-08-01T00:00:00+00:00"), "morning", "2026-08-01")
    text = thesis_mod.render_impact(results)
    assert text.startswith("가설 2건 판정")
    assert "강화 1" in text and "판정 불가 1" in text
    assert "[AI·반도체 #1]" in text and "강화" in text
    assert "[전력·에너지 #1]" in text and "판정 불가" in text


def test_render_impact_empty_when_no_theses():
    assert thesis_mod.render_impact([]) == ""


def test_render_next_check_suffix_includes_reviewed_theses_date():
    results = [{"thesis_id": "t1", "theme": "ai_semi", "slot": 1, "verdict": "유지", "next_check_date": "2026-11-01"}]
    text = thesis_mod.render_next_check_suffix(results, report=None)
    assert "2026-11-01" in text


# --- final-review.md F1: 판정과 사유의 정합 -------------------------------

def _f1_thesis(tid, *, falsify, weaken=None, strengthen=None):
    return {
        "thesis_id": tid, "theme": "fin_credit", "slot": 1,
        "statement": "테스트", "leading_indicators": ["X value"],
        "next_check_date": "2026-12-01",
        "conditions": {"falsify": falsify, "weaken": weaken or [], "strengthen": strengthen or []},
    }


def _f1_atom(op, value, aid):
    return {"id": aid, "kind": "threshold", "subject": "X", "metric": "value",
            "category": "macro", "op": op, "value": value}


def test_reason_cites_the_condition_that_actually_fired(conn, raw_dir):
    """사유는 판정을 만든 조건이어야 한다 — 목록의 첫 조건이 아니라.

    발행된 실물이 `강화 — DGS10 value 최신값 4.68 <= 3` 이었다(final-review F1).
    판정(강화)은 옳았지만 인용된 것은 **발동하지 않은 반증 조건**이었고,
    4.68 <= 3 은 산술적으로 거짓인 문장이 매일 리포트에 실린 것이다.
    """
    from market_intel.interp import thesis as T

    seed_fact(conn, raw_dir, "fred", macro_fc("X", "2026-07-01T00:00:00+00:00", 4.68),
              "2026-07-02T00:00:00+00:00")
    cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)

    # falsify(<=3)는 거짓, strengthen(>=4.5)은 참 -> 판정은 강화
    reviews = T.review(
        conn,
        [_f1_thesis("t1", falsify=[_f1_atom("<=", 3, "f1")], strengthen=[_f1_atom(">=", 4.5, "s1")])],
        cutoff, "morning", "2026-08-01",
    )
    assert reviews[0]["verdict"] == "강화"
    text = T.render_impact(reviews)

    # 발동한 조건(>= 4.5)이 인용되고, 발동하지 않은 반증 조건은 인용되지 않는다
    assert ">= 4.5" in text
    assert "<= 3" not in text, f"발동하지 않은 조건이 사유로 실렸다: {text}"


def test_comparison_never_printed_without_its_truth_value(conn, raw_dir):
    """비교식은 충족/미충족 없이 단정문으로 찍히면 안 된다.

    `최신값 4.68 <= 3` 처럼 참·거짓 표시 없는 비교식은 그 자체로 거짓 진술이
    된다. 조건이 거짓인 원자가 어떤 경로로든 출력되더라도 문장은 참이어야 한다.
    """
    from market_intel.interp import thesis as T

    seed_fact(conn, raw_dir, "fred", macro_fc("X", "2026-07-01T00:00:00+00:00", 4.68),
              "2026-07-02T00:00:00+00:00")
    cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)

    reviews = T.review(
        conn, [_f1_thesis("t2", falsify=[_f1_atom("<=", 3, "f1")])], cutoff, "morning", "2026-08-01",
    )
    messages = [e["detail"]["message"] for e in reviews[0]["all_evals"]]
    for msg in messages:
        if "<=" in msg or ">=" in msg:
            assert "충족" in msg or "미충족" in msg, f"참·거짓 표시 없는 비교식: {msg}"


# --- 목표치 재설정: 골대가 움직였다는 사실을 남긴다 (CEO 2026-08-04) --------
#
# 가설은 살아 있는 문서라 기준이 바뀐다. 그런데 기준을 바꾸면 그 전후 판정은
# 서로 비교할 수 없는 것이 되고, 기록에 판(版)을 안 남기면 원장이 "강화 → 강화"만
# 보여준다 — 가설이 맞아서인지 골대를 옮겨서인지 구별할 수 없다.

def _fin_thesis(strengthen_at: float = 4.5) -> dict:
    conditions = {
        "falsify": [{"id": "low", "kind": "threshold", "category": "macro",
                     "subject": "DGS10", "metric": "value", "op": "<=", "value": 3.0}],
        "weaken": [],
        "strengthen": [{"id": "high", "kind": "threshold", "category": "macro",
                        "subject": "DGS10", "metric": "value", "op": ">=", "value": strengthen_at}],
    }
    return {
        "thesis_id": "fin_1", "theme": "fin_credit", "slot": 1,
        "statement": "고금리 국면이 유지된다.", "conditions": conditions,
        "leading_indicators": ["DGS10 value"], "next_check_date": "2026-12-01",
        "rules_sha256": store_mod.rules_fingerprint("고금리 국면이 유지된다.", conditions),
    }


def _seed_dgs10(conn, raw_dir, value: float):
    seed_fact(conn, raw_dir, "fred", macro_fc("DGS10", "2026-08-01T00:00:00+00:00", value),
              "2026-08-02T00:00:00+00:00")


def test_fingerprint_ignores_key_order_but_not_the_threshold():
    """파일에서 키 순서만 바뀐 것을 '기준 변경'으로 세면 매번 경고가 뜬다.
    반대로 숫자가 바뀌면 반드시 달라져야 한다."""
    a = {"falsify": [{"kind": "threshold", "subject": "DGS10", "op": "<=", "value": 3.0}]}
    b = {"falsify": [{"value": 3.0, "op": "<=", "subject": "DGS10", "kind": "threshold"}]}
    c = {"falsify": [{"kind": "threshold", "subject": "DGS10", "op": "<=", "value": 3.5}]}
    same = store_mod.rules_fingerprint("문장", a)
    assert same == store_mod.rules_fingerprint("문장", b)
    assert same != store_mod.rules_fingerprint("문장", c)
    # 조건이 같아도 주장이 바뀌면 다른 가설이다 — 골대를 옮기는 흔한 방식이다.
    assert same != store_mod.rules_fingerprint("다른 문장", a)


def test_moving_the_goalpost_is_flagged_once_not_forever(settings):
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_dgs10(conn, settings.raw_dir, 4.75)

    def run(thesis, day):
        rows = thesis_mod.review(conn, [thesis], _cutoff(f"2026-08-{day}T12:00:00+00:00"),
                                 "morning", f"2026-08-{day}")
        store_mod.record_reviews(conn, rows)
        return rows[0]

    first = run(_fin_thesis(4.5), "10")
    assert first["verdict"] == "강화" and first["rules_changed"] == 0

    moved = run(_fin_thesis(4.0), "11")          # 골대를 옮긴다
    assert moved["verdict"] == "강화", "판정은 그대로인데"
    assert moved["changed"] == 0, "판정 변화로는 안 잡힌다 — 그래서 별도 표시가 필요하다"
    assert moved["rules_changed"] == 1, "기준이 바뀐 사실은 잡혀야 한다"

    again = run(_fin_thesis(4.0), "12")          # 같은 기준으로 하루 더
    assert again["rules_changed"] == 0, "한 번만 표시된다 — 계속 뜨면 경고가 무뎌진다"


def test_the_version_that_produced_each_verdict_is_recorded(settings):
    """나중에 '이 판정은 어떤 기준으로 나왔나'에 답할 수 있어야 한다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_dgs10(conn, settings.raw_dir, 4.75)

    v1, v2 = _fin_thesis(4.5), _fin_thesis(4.0)
    for thesis, day in ((v1, "10"), (v2, "11")):
        store_mod.record_reviews(conn, thesis_mod.review(
            conn, [thesis], _cutoff(f"2026-08-{day}T12:00:00+00:00"), "morning", f"2026-08-{day}"))

    stamped = [r["rules_sha256"] for r in conn.execute(
        "SELECT rules_sha256 FROM thesis_reviews WHERE thesis_id='fin_1' ORDER BY rowid")]
    assert stamped == [v1["rules_sha256"], v2["rules_sha256"]]
    assert len(set(stamped)) == 2


def test_same_second_reviews_do_not_scramble_the_previous_one(settings):
    """`created_at`은 초 단위다. 한 번에 여러 판정을 기록하면 값이 같아지고,
    uuid로 동점을 풀면 **입력 순서와 무관한 행**이 직전으로 뽑힌다.

    실측(2026-08-04): v1 -> v2 -> v2 를 같은 초에 넣었더니 세 번째까지
    '기준이 바뀐 뒤 첫 판정'으로 떴다 — 두 번째 행이 아니라 첫 번째 행이
    직전으로 뽑혔기 때문이다. 지문이 서로 다른 이력이 있어야 이 뒤섞임이
    드러나므로, 기준을 한 번 바꾼 뒤 같은 기준으로 여러 번 더 돌린다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_dgs10(conn, settings.raw_dir, 4.75)

    plan = [(_fin_thesis(4.5), "10"), (_fin_thesis(4.0), "11"),
            (_fin_thesis(4.0), "12"), (_fin_thesis(4.0), "13")]
    flags = []
    for thesis, day in plan:
        rows = thesis_mod.review(conn, [thesis], _cutoff(f"2026-08-{day}T12:00:00+00:00"),
                                 "morning", f"2026-08-{day}")
        store_mod.record_reviews(conn, rows)
        flags.append(rows[0]["rules_changed"])

    assert flags == [0, 1, 0, 0], (
        "기준을 바꾼 그 한 번만 표시돼야 한다 — 계속 뜨면 경고가 무뎌지고, "
        f"직전 판정을 잘못 고르고 있다는 뜻이다: {flags}")


# --- ST3: 엔진 의미 버전 — 판정 코드의 뜻이 바뀐 경계를 남긴다 (명세 §2) ------
#
# `rules_changed`(위)와 완전히 대칭이지만 원인이 다르다: `rules_changed`는
# 조건(theses.json)이 바뀐 것이고, `engine_changed`는 조건은 그대로인데
# 판정을 만드는 **엔진 코드의 뜻**이 바뀐 것이다. `rules_fingerprint`는
# statement·conditions만 보므로 이 변화를 못 잡는다 — 2026-08-12
# detail.latest_at/streak 도입이 실제로 그렇게 흔적 없이 지나갔다(명세 §Problem).

def test_engine_semantics_registry_documents_the_current_version():
    """ENGINE_SEMANTICS에 현재 ENGINE_VERSION 키가 없으면 실패한다(§2 "이
    해법이 약속하지 않는 것" (b): 사람이 버전을 올렸는데 레지스트리에 뜻을
    안 남기는 것을 잡는 마지막 방어선)."""
    assert thesis_mod.ENGINE_VERSION in thesis_mod.ENGINE_SEMANTICS
    assert thesis_mod.ENGINE_SEMANTICS[thesis_mod.ENGINE_VERSION]  # 빈 문자열도 금지


def test_engine_version_change_is_flagged_once_not_forever(settings):
    """`test_moving_the_goalpost_is_flagged_once_not_forever`(위)의 엔진판.
    ENGINE_VERSION은 모듈 상수라 한 프로세스 안의 `review()` 호출만으로는
    버전을 못 바꾸므로, 옛 버전의 첫 행은 실제 운영 원장의 과거 값("2b.1")을
    직접 기록해 재현한다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_dgs10(conn, settings.raw_dir, 4.75)
    thesis = _fin_thesis(4.5)  # 조건은 세 판정 내내 그대로 — rules_changed는 0이어야 한다

    old_row = dict(thesis_mod.review(
        conn, [thesis], _cutoff("2026-08-10T12:00:00+00:00"), "morning", "2026-08-10")[0])
    old_row["engine_version"], old_row["engine_changed"] = "2b.1", 0  # 실제 운영 원장의 옛 값
    store_mod.record_reviews(conn, [old_row])

    def run(day):
        rows = thesis_mod.review(conn, [thesis], _cutoff(f"2026-08-{day}T12:00:00+00:00"),
                                 "morning", f"2026-08-{day}")
        store_mod.record_reviews(conn, rows)
        return rows[0]

    first_at_new_version = run("11")
    assert first_at_new_version["engine_version"] == thesis_mod.ENGINE_VERSION
    assert first_at_new_version["engine_changed"] == 1, "엔진 버전이 바뀐 뒤 첫 판정은 잡혀야 한다"
    assert first_at_new_version["rules_changed"] == 0, "조건은 안 바뀌었다 — rules_changed와 섞이면 안 된다"

    again = run("12")
    assert again["engine_changed"] == 0, "한 번만 표시된다 — 계속 뜨면 경고가 무뎌진다"


def test_stored_engine_changed_column_matches_what_review_computed(settings):
    """`test_the_version_that_produced_each_verdict_is_recorded`(rules_sha256
    쪽)의 engine판. `thesis.review()`가 만든 dict의 값을 확인하는 것만으로는
    `store.record_reviews`가 그 값을 실제로 DB에 박는지 증명하지 못한다 —
    두 단계가 분리돼 있어서, record_reviews가 값을 흘려도 review()의 반환
    dict는 여전히 옳게 보인다. 원장(`thesis_reviews`)을 직접 읽어야 한다."""
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed_dgs10(conn, settings.raw_dir, 4.75)
    thesis = _fin_thesis(4.5)

    old_row = dict(thesis_mod.review(
        conn, [thesis], _cutoff("2026-08-10T12:00:00+00:00"), "morning", "2026-08-10")[0])
    old_row["engine_version"], old_row["engine_changed"] = "2b.1", 0
    store_mod.record_reviews(conn, [old_row])

    rows = thesis_mod.review(conn, [thesis], _cutoff("2026-08-11T12:00:00+00:00"), "morning", "2026-08-11")
    store_mod.record_reviews(conn, rows)

    stamped = [(r["engine_version"], r["engine_changed"]) for r in conn.execute(
        "SELECT engine_version, engine_changed FROM thesis_reviews WHERE thesis_id='fin_1' ORDER BY rowid")]
    assert stamped == [("2b.1", 0), (thesis_mod.ENGINE_VERSION, 1)], stamped
