"""원장의 보유비중을 사각지대 검출기가 쓸 모양으로 꺼내오는 계약.

`yfinance_holdings`가 `subject="XLV/LLY"` 꼴로 쌓아 둔 것을 `{업종: {종목: %}}`로
가른다. 여기서 틀리기 쉬운 곳이 둘이고, 둘 다 **조용히** 틀린다:

1. `facts_as_of`는 **정렬하지 않는다.** `fact_id`에 날짜가 들어가므로 같은
   (업종, 종목)의 날짜별 비중이 전부 돌아온다 — 명시로 최신순을 고르지 않으면
   어느 날의 비중이 뽑힐지가 SQLite의 행 순서에 달린다.
2. 차단선. 오늘 안 비중으로 지난주를 설명하면 안 된다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from market_intel import db as db_mod
from market_intel.reporting import build as build_mod


def _conn(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _seed(conn, etf, holding, day, pct, known_at=None):
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision_no, known_at, event_at, subject,"
        " category, metric, value_num, unit, comparison_basis, data_status) "
        "VALUES (?,1,?,?,?,'etf_holding','holding_weight',?,'percent','','source_verified')",
        (f"{etf}/{holding}:holding_weight:{day}", known_at or f"{day}T00:00:00+00:00",
         f"{day}T00:00:00+00:00", f"{etf}/{holding}", pct))
    conn.commit()


def _at(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T23:59:59+00:00").astimezone(timezone.utc)


def test_the_newest_weight_wins_not_whatever_sqlite_returns_first(settings):
    """**정렬을 안 하면 여기가 조용히 틀린다.** 옛 비중을 먼저 넣어 두면 행
    순서가 옛것부터가 되기 쉬우므로, 정렬이 빠졌을 때 실제로 빨간불이 난다."""
    conn = _conn(settings)
    _seed(conn, "XLV", "LLY", "2026-08-01", 9.0)
    _seed(conn, "XLV", "LLY", "2026-08-19", 15.472)
    assert build_mod._sector_weights(conn, _at("2026-08-20")) == {"XLV": {"LLY": 15.472}}


def test_a_weight_learned_after_the_cutoff_is_not_used(settings):
    """오늘 안 비중으로 지난주를 설명하면 차단선을 어긴다 — 가격·사실과 같은 규율."""
    conn = _conn(settings)
    _seed(conn, "XLV", "LLY", "2026-08-19", 15.472)
    assert build_mod._sector_weights(conn, _at("2026-08-10")) == {}


def test_several_holdings_and_several_sectors_are_kept_apart(settings):
    conn = _conn(settings)
    _seed(conn, "XLV", "LLY", "2026-08-19", 15.4)
    _seed(conn, "XLV", "JNJ", "2026-08-19", 10.5)
    _seed(conn, "XLK", "MSFT", "2026-08-19", 12.0)
    got = build_mod._sector_weights(conn, _at("2026-08-20"))
    assert got == {"XLV": {"LLY": 15.4, "JNJ": 10.5}, "XLK": {"MSFT": 12.0}}


def test_a_subject_without_the_pair_shape_is_skipped(settings):
    """`subject`에 슬래시가 없으면 어느 업종의 비중인지 알 수 없다. 버린다."""
    conn = _conn(settings)
    _seed(conn, "XLV", "LLY", "2026-08-19", 15.4)
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision_no, known_at, event_at, subject,"
        " category, metric, value_num, unit, comparison_basis, data_status) "
        "VALUES ('bad:holding_weight:2026-08-19',1,'2026-08-19T00:00:00+00:00',"
        "'2026-08-19T00:00:00+00:00','NOSLASH','etf_holding','holding_weight',5.0,"
        "'percent','','source_verified')")
    conn.commit()
    assert build_mod._sector_weights(conn, _at("2026-08-20")) == {"XLV": {"LLY": 15.4}}


def test_a_broken_ledger_read_does_not_break_the_report(settings, monkeypatch):
    """`_prior_interpretation`·`_scorecard`와 같은 원칙 — 비중이 없으면 신고가
    계산 없이 말할 수 있는 것만 말한다. 리포트는 그대로 나간다."""
    conn = _conn(settings)
    monkeypatch.setattr(db_mod, "facts_as_of",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert build_mod._sector_weights(conn, _at("2026-08-20")) == {}
