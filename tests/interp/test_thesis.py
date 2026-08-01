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
