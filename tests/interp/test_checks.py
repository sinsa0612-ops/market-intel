"""해석 성적표(`interp/checks.py`)의 계약.

이 기능은 **"이 해석이 맞았다"에 가까운 문장을 만들어 내는** 자리라, 규율이
무너지면 틀린 믿음에 인증서가 붙는다. 그래서 "돌아간다"가 아니라 **지켜야 할
네 가지**를 적는다: 산문을 안 읽는다 · LLM이 채점에 안 낀다 · 골대를 안 옮긴다 ·
품질 미달 백엔드의 조건은 안 쌓는다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from market_intel import db as db_mod
from market_intel.interp import checks


@pytest.fixture()
def conn(tmp_path):
    p = str(tmp_path / "t.db")
    db_mod.init_db(p)
    c = db_mod.connect(p)
    c.execute(
        "INSERT INTO interpretations(interpretation_id, report_type, report_date, cutoff_utc,"
        " status, fields_json, engine_version, created_at, model) "
        "VALUES ('i1','morning','2026-03-02','2026-03-02T07:15:00+00:00','ok','{}','x',"
        "'2026-03-02T07:20:00+00:00','gpt:gpt-5.6-luna')")
    c.commit()
    # 조건 대상이 원장에 **존재하기만** 하게 해 둔다(등록 전제). 값은 등록
    # 시점 이전 것이라 채점에는 쓰이지 않는다 — 채점용 관측은 각 시험이 깐다.
    for subj in ("DGS2", "DGS10"):
        _seed(c, subj, "value", "2026-02-01T00:00:00+00:00", 1.0)
    return c


GOOD = {"id": "a1", "kind": "threshold", "subject": "DGS2", "metric": "value",
        "op": ">", "value": 3.75, "why": "정책금리 상단보다 높은지"}


def _seed(conn, subject, metric, event_at, value):
    """원장에 관측 하나. **등록의 전제조건이다** — 원장에 없는 대상은 채점될 수
    없으므로 `register`가 거부한다."""
    conn.execute(
        "INSERT INTO fact_revisions(fact_id, revision_no, known_at, event_at, subject,"
        " category, metric, value_num, comparison_basis, data_status) "
        "VALUES (?,1,?,?,?,?,?,?,'','source_verified')",
        (f"{subject}:{metric}:{event_at}", event_at, event_at, subject,
         "macro" if metric == "value" else "price", metric, value))
    conn.commit()


# 등록 시점의 차단선. 픽스처가 깔아 둔 관측(2026-02-01)보다 뒤라서 대상이
# "그때 보였다"가 성립한다.
REG_CUTOFF = datetime(2026, 3, 2, 7, 15, tzinfo=timezone.utc)


def _reg(conn, atoms, model="gpt:gpt-5.6-luna"):
    return checks.register(conn, interpretation_id="i1", report_type="morning",
                           report_date="2026-03-02", atoms=atoms, model=model,
                           cutoff=REG_CUTOFF)


# --- 품질 미달 백엔드 --------------------------------------------------------

@pytest.mark.parametrize("model", ["qwen3.5:9b", "", None, "llama3"])
def test_untrusted_backend_registers_nothing(conn, model):
    """실측(2026-08-20): ollama는 러셀2000을 "나스닥100"이라 부르고 원문에 없는
    주장을 만들었다. 그 조건으로 성적표를 쌓으면 성적표가 오염된다."""
    assert _reg(conn, [GOOD], model=model) == 0
    assert conn.execute("SELECT COUNT(*) FROM interpretation_checks").fetchone()[0] == 0


@pytest.mark.parametrize("model", ["gpt:gpt-5.6-luna", "claude:haiku"])
def test_trusted_backend_registers(conn, model):
    assert _reg(conn, [GOOD], model=model) == 1


# --- 등록 규율 --------------------------------------------------------------

def test_malformed_atom_is_not_registered(conn):
    """형식이 틀린 조건은 채점할 수 없다 — 등록해 두면 영원히 UNKNOWN으로
    남아 "채점 불가율"을 거짓으로 부풀린다.

    **subject는 원장에 있는 것을 쓴다.** 없는 것을 쓰면 존재 가드에 먼저 걸려
    형식 가드가 없어도 이 시험이 통과한다 — 실제로 첫 판이 그렇게 쓰여 있었고
    "형식 검사를 지운다" 변이가 살아남았다(2026-08-20 변이 검사)."""
    bad = {"id": "a2", "kind": "consecutive", "subject": "DGS2", "metric": "value"}
    assert _reg(conn, [bad]) == 0, "direction/periods가 없는 조건은 채점할 수 없다"


def test_atom_without_id_is_skipped(conn):
    assert _reg(conn, [{k: v for k, v in GOOD.items() if k != "id"}]) == 0


def test_subject_invisible_at_the_cutoff_is_refused(conn):
    """차단선 **이후**에야 생긴 대상에는 조건을 걸 수 없다 — 리포트를 쓸 때
    보이지도 않던 것을 근거로 삼는 셈이기 때문이다."""
    _seed(conn, "LATECOMER", "value", "2026-06-01T00:00:00+00:00", 1.0)
    assert _reg(conn, [dict(GOOD, id="l1", subject="LATECOMER")]) == 0


def test_subject_not_in_the_ledger_is_refused(conn):
    """**실측(2026-08-20 E2E)에서 나온 결함이다.** 모델이 `KOSPI`·`SOX`로 조건을
    냈는데 원장의 실제 subject는 `^KS11`·`^SOX`였다 — 다이제스트가 사람이 읽는
    이름만 보여주기 때문이다. 등록되면 영원히 「채점 불가」로 남아 성적표를
    무용지물로 만든다. 프롬프트에 코드 목록을 주는 것으로 원인은 고쳤지만,
    프롬프트는 다시 어긋날 수 있으므로 여기서도 막는다."""
    assert _reg(conn, [dict(GOOD, id="x1", subject="KOSPI", metric="price_close")]) == 0
    assert _reg(conn, [dict(GOOD, id="x2", subject="SOX", metric="price_close")]) == 0


def test_benchmark_must_also_exist(conn):
    """상대강도 조건은 비교 대상이 없으면 역시 채점할 수 없다."""
    atom = {"id": "r1", "kind": "relative_change_pct", "subject": "DGS2", "metric": "value",
            "op": "<=", "value": -5.0, "lookback": 10, "benchmark": "NOPE", "why": "x"}
    assert _reg(conn, [atom]) == 0


def test_registration_is_idempotent(conn):
    """같은 해석을 다시 채워도 조건이 늘지 않는다 — 재실행이 성적표를 부풀리면
    적중률이 표본 수로 조작된다."""
    assert _reg(conn, [GOOD]) == 1
    _reg(conn, [GOOD])
    assert conn.execute("SELECT COUNT(*) FROM interpretation_checks").fetchone()[0] == 1


def test_caps_how_many_one_interpretation_can_register(conn):
    atoms = [dict(GOOD, id=f"a{i}") for i in range(checks.MAX_PER_INTERPRETATION + 3)]
    _reg(conn, atoms)
    n = conn.execute("SELECT COUNT(*) FROM interpretation_checks").fetchone()[0]
    assert n == checks.MAX_PER_INTERPRETATION


# --- 채점 -------------------------------------------------------------------

CUTOFF = datetime(2026, 3, 20, 7, 15, tzinfo=timezone.utc)


def test_nothing_is_scored_before_its_due_date(conn):
    """만기 전에 채점하면 "지금 맞나"를 묻는 것이지 "그때 건 예측이 맞았나"가
    아니다."""
    _reg(conn, [GOOD])
    assert checks.due(conn, "2026-03-05") == []
    assert checks.score_due(conn, "2026-03-05", CUTOFF) == 0


def test_scores_after_due_date(conn):
    _reg(conn, [GOOD])
    _seed(conn, "DGS2", "value", "2026-03-15T00:00:00+00:00", 4.10)
    assert checks.score_due(conn, "2026-03-20", CUTOFF) == 1
    row = conn.execute("SELECT verdict, scored_at FROM interpretation_checks").fetchone()
    assert row["verdict"] == "TRUE" and row["scored_at"]


def test_false_is_recorded_as_false_not_dropped(conn):
    """틀린 것을 지우면 성적표가 자기 자랑이 된다."""
    _reg(conn, [GOOD])
    _seed(conn, "DGS2", "value", "2026-03-15T00:00:00+00:00", 3.00)
    checks.score_due(conn, "2026-03-20", CUTOFF)
    assert conn.execute("SELECT verdict FROM interpretation_checks").fetchone()[0] == "FALSE"


# 원장에 대상은 있지만 **관측 수가 모자라** 판정할 수 없는 조건. 등록은 되고
# 채점은 UNKNOWN이 된다 — "우리가 못 보는 것"의 정직한 모양이다.
UNSCORABLE = {"id": "u1", "kind": "consecutive", "subject": "DGS10", "metric": "value",
              "direction": "up", "periods": 8, "why": "8구간 연속 상승인지"}


def test_missing_observation_is_unknown_not_false(conn):
    """관측이 없는 것과 틀린 것은 다르다. 섞으면 "우리가 못 보는 것"이
    "해석이 틀린 것"으로 둔갑한다."""
    assert _reg(conn, [UNSCORABLE]) == 1
    checks.score_due(conn, "2026-03-20", CUTOFF)
    assert conn.execute("SELECT verdict FROM interpretation_checks").fetchone()[0] == "UNKNOWN"


def test_scoring_happens_once_and_never_changes(conn):
    """**골대 이동 방지.** 나중에 값이 달라졌다고 성적이 바뀌면 안 된다."""
    _reg(conn, [GOOD])
    _seed(conn, "DGS2", "value", "2026-03-15T00:00:00+00:00", 4.10)
    checks.score_due(conn, "2026-03-20", CUTOFF)
    first = conn.execute("SELECT verdict, scored_at FROM interpretation_checks").fetchone()
    _seed(conn, "DGS2", "value", "2026-03-19T00:00:00+00:00", 1.00)  # 이제 거짓
    assert checks.score_due(conn, "2026-03-25", CUTOFF) == 0, "두 번째 채점은 없다"
    again = conn.execute("SELECT verdict, scored_at FROM interpretation_checks").fetchone()
    assert (again["verdict"], again["scored_at"]) == (first["verdict"], first["scored_at"])


def test_the_update_carries_a_second_lock_against_rescoring(conn):
    """**이중 잠금의 존재를 못박는다.**

    채점이 두 번 일어나면 나중 값으로 성적이 바뀌고, 그것이 골대 이동이다.
    1차 방어는 `due()`의 `scored_at IS NULL` 필터라 UPDATE의 같은 조건은
    **기능적으로 도달할 수 없다** — 그래서 그 조건을 지워도 시험이 전부
    통과했다(변이 검사 2026-08-20에서 실제로 살아남았다).

    도달 불가능한 가드라고 지우면 `due()`가 바뀌는 날 조용히 뚫린다. 그래서
    코드에 그 조건이 있는지를 직접 본다 — 이 저장소가 `fcntl.flock` 사용을
    소스 문자열로 못박아 둔 것과 같은 방식이다(`tests/publish/test_jobs.py`)."""
    from pathlib import Path

    from market_intel.interp import store as store_mod

    src = Path(store_mod.__file__).read_text(encoding="utf-8")
    assert "WHERE check_id=? AND scored_at IS NULL" in src, (
        "UPDATE의 두 번째 잠금이 사라졌다 — `due_checks`가 바뀌면 재채점이 뚫린다")


def test_one_broken_atom_does_not_block_the_rest(conn, monkeypatch):
    """조건 하나가 채점 중 터져도 나머지는 채점된다 — 성적표 하나가 리포트
    파이프라인을 멈추면 안 된다. 등록 단계에서 이미 형식·존재를 거르므로 이
    경로는 좀처럼 안 밟히지만, 밟히는 날 조용히 전체가 멈추면 곤란하다."""
    from market_intel.interp import thesis as thesis_mod

    _reg(conn, [GOOD, dict(GOOD, id="a9", subject="DGS10")])
    _seed(conn, "DGS2", "value", "2026-03-15T00:00:00+00:00", 4.10)
    real = thesis_mod.evaluate_atom

    def boom(c, atom, cutoff):
        if atom.get("subject") == "DGS10":
            raise RuntimeError("일부러 터뜨림")
        return real(c, atom, cutoff)

    monkeypatch.setattr(checks.thesis_mod, "evaluate_atom", boom)
    assert checks.score_due(conn, "2026-03-20", CUTOFF) == 2
    verdicts = {r[0] for r in conn.execute("SELECT verdict FROM interpretation_checks")}
    assert verdicts == {"TRUE", "UNKNOWN"}


# --- 성적표 -----------------------------------------------------------------

def test_scorecard_separates_unknown_from_wrong(conn):
    """채점 불가는 실패가 아니다. 그 비율 자체가 "우리 관측으로 검증되는
    주장을 쓰고 있나"의 지표다."""
    _reg(conn, [GOOD, UNSCORABLE])
    _seed(conn, "DGS2", "value", "2026-03-15T00:00:00+00:00", 4.10)
    checks.score_due(conn, "2026-03-20", CUTOFF)
    card = checks.scorecard(conn)
    assert (card.total, card.true, card.unknown) == (2, 1, 1)
    assert card.scored == 1 and card.hit_rate == 100.0


def test_scorecard_can_narrow_to_one_subject(conn):
    """CEO 질문 — "어떤 변수를 짚었을 때 맞았나"가 이것이다."""
    _reg(conn, [GOOD, dict(GOOD, id="a2", subject="DGS10")])
    _seed(conn, "DGS2", "value", "2026-03-15T00:00:00+00:00", 4.10)
    _seed(conn, "DGS10", "value", "2026-03-15T00:00:00+00:00", 1.00)
    checks.score_due(conn, "2026-03-20", CUTOFF)
    assert checks.scorecard(conn, subject="DGS2").hit_rate == 100.0
    assert checks.scorecard(conn, subject="DGS10").hit_rate == 0.0


def test_hit_rate_is_none_when_nothing_was_scorable(conn):
    """0/0을 0%로 쓰면 "다 틀렸다"로 읽힌다."""
    assert checks.scorecard(conn).hit_rate is None


# --- 산문을 읽지 않는다 -------------------------------------------------------

def test_module_never_reads_interpretation_prose():
    """**이 시험이 이 모듈의 존재 이유를 지킨다.** 산문을 읽는 순간 파싱이 되고,
    파싱은 새 환각 표면이다(`model.PriorInterpretation` 주석의 함정 (2))."""
    from pathlib import Path

    src = Path(checks.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]  # 모듈 docstring 제외
    for banned in ("reading", "counter_reading", "next_check", "text_json", "fields_json"):
        assert banned not in body, f"산문 필드를 건드린다: {banned!r}"
