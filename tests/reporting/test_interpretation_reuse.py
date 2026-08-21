"""리포트를 다시 만들어도 AI 해석이 사라지지 않는다 — 단, 사실이 그대로일 때만.

왜 이 파일이 있는가 (CEO 지적 2026-08-05): 표기 하나 고쳐 달라고 할 때마다
리포트를 다시 만들게 되고, 그때마다 AI 해석이 통째로 사라졌다. 다시 쓰려면
LLM을 또 불러야 한다.

되살리는 판단은 **표시 형식이 아니라 데이터**로 한다. 그래야 오늘의 `%p` 수정처럼
화면 글자만 바뀐 경우에 멀쩡한 해석이 버려지지 않고, 반대로 수치가 바뀌면
옛 해석이 조용히 따라붙지 않는다.
"""
from __future__ import annotations

from datetime import date

from market_intel import db as db_mod
from market_intel.interp import digest as digest_mod
from market_intel.interp import store as store_mod
from market_intel.reporting import build as build_mod
from market_intel.reporting import cutoff as cutoff_mod
from market_intel.reporting.model import FactRow, Report
from tests.reporting.conftest import seed_fact

from market_intel.models import FactCandidate

REPORT_DATE = date(2026, 8, 3)
KNOWN = "2026-08-03T06:00:00+00:00"
TEXT = {
    "reading": "테스트 해석 본문",
    "counter_reading": "반대 해석",
    "thesis_impact": "가설 영향",
    "next_check": "다음 확인",
    "generated_by": "test-model",
    "generated_at": "2026-08-03T07:00:00+00:00",
}


def _open(settings):
    db_mod.init_db(settings.db_path)
    return db_mod.connect(settings.db_path)


def _report(conn):
    return build_mod.build_report(
        conn, "close_delta", REPORT_DATE, cutoff_mod.cutoff_for("close_delta", REPORT_DATE))


def _seed_one_fact(conn, raw_dir, value: float = 4.2) -> None:
    seed_fact(conn, raw_dir, "fred", FactCandidate(
        raw_ref="UNRATE:2026-07-31", subject="UNRATE", category="macro", metric="value",
        event_at="2026-07-31T00:00:00+00:00", market="US", country="US",
        value_num=value, unit="%", publisher="test"), KNOWN)


def _save(conn, report, status: str = "ok", text: dict | None = None) -> None:
    store_mod.record_interpretation(conn, {
        "report_type": report.report_type, "report_date": report.report_date,
        "cutoff_utc": report.cutoff_utc, "status": status,
        "facts_sha256": digest_mod.facts_fingerprint(report),
        "text": TEXT if text is None else text,
    })


# --- ① 같은 사실이면 되살린다 -------------------------------------------------

def test_interpretation_survives_a_rebuild(settings):
    conn = _open(settings)
    _seed_one_fact(conn, settings.raw_dir)
    _save(conn, _report(conn))
    again = _report(conn)
    conn.close()

    assert again.interpretation.reading == "테스트 해석 본문"
    assert again.interpretation.counter_reading == "반대 해석"


def test_restored_interpretation_keeps_its_original_timestamp(settings):
    """되살린 글에 오늘 시각을 찍으면 방금 쓴 해석처럼 보인다."""
    conn = _open(settings)
    _seed_one_fact(conn, settings.raw_dir)
    _save(conn, _report(conn))
    again = _report(conn)
    conn.close()

    assert again.interpretation.generated_at == TEXT["generated_at"]
    assert again.meta.get("interpretation_restored")  # 되살린 것임을 밝힌다


# --- ② 사실이 바뀌면 되살리지 않는다 ------------------------------------------

def test_changed_facts_do_not_get_the_old_interpretation(settings):
    """해석은 그때 그 사실을 보고 쓴 글이다. 바뀐 사실 위에 붙이면 거짓말이 된다."""
    conn = _open(settings)
    _seed_one_fact(conn, settings.raw_dir, value=4.2)
    _save(conn, _report(conn))

    # 같은 지표에 새 판(수정치)이 들어온다 -> 사실이 달라졌다
    seed_fact(conn, settings.raw_dir, "fred", FactCandidate(
        raw_ref="UNRATE:2026-07-31", subject="UNRATE", category="macro", metric="value",
        event_at="2026-07-31T00:00:00+00:00", market="US", country="US",
        value_num=9.9, unit="%", publisher="test"), "2026-08-03T06:30:00+00:00")
    again = _report(conn)
    conn.close()

    assert again.interpretation.is_empty(), again.interpretation


def test_failed_interpretation_is_never_restored(settings):
    """검증에 걸렸거나 LLM이 죽어 비었던 판을 되살리면 그때의 실패를 오늘
    리포트에 다시 붙이는 셈이다."""
    conn = _open(settings)
    _seed_one_fact(conn, settings.raw_dir)
    _save(conn, _report(conn), status="validation_failed")
    again = _report(conn)
    conn.close()

    assert again.interpretation.is_empty()


# --- ③ 표기만 바뀐 수정에서는 살아남는다 (이 기능의 존재 이유) ----------------

def test_display_only_change_keeps_the_same_fingerprint():
    """`%` -> `%p`처럼 화면 글자만 바뀐 수정은 사실을 바꾸지 않는다.
    지문이 표시 문자열을 물면 이런 수정마다 멀쩡한 해석이 버려진다."""
    def mk(comparison: str) -> Report:
        r = Report(report_type="close_delta", report_date="2026-08-03",
                   cutoff_utc="2026-08-03T07:15:00+00:00")
        r.facts = [FactRow(
            label="한국 기준금리", value="2.75 연%", comparison=comparison, source_url="",
            data_status="source_verified", known_at=KNOWN,
            subject="722Y001.0101000", metric="value", raw_value=2.75)]
        return r

    assert (digest_mod.facts_fingerprint(mk("직전 관측 대비 +10.00%"))
            == digest_mod.facts_fingerprint(mk("직전 관측 대비 +0.25%p")))


def test_value_change_moves_the_fingerprint():
    def mk(raw_value: float) -> Report:
        r = Report(report_type="close_delta", report_date="2026-08-03",
                   cutoff_utc="2026-08-03T07:15:00+00:00")
        r.facts = [FactRow(
            label="한국 기준금리", value="x", comparison="y", source_url="",
            data_status="source_verified", known_at=KNOWN,
            subject="722Y001.0101000", metric="value", raw_value=raw_value)]
        return r

    assert digest_mod.facts_fingerprint(mk(2.75)) != digest_mod.facts_fingerprint(mk(3.00))


def test_fingerprint_ignores_fact_ordering():
    """같은 사실 묶음이면 순서가 달라도 같은 지문이어야 한다 — 정렬이 바뀌었다고
    해석을 버릴 이유는 없다."""
    def mk(order: list[str]) -> Report:
        r = Report(report_type="close_delta", report_date="2026-08-03",
                   cutoff_utc="2026-08-03T07:15:00+00:00")
        r.facts = [FactRow(label=s, value="", comparison="", source_url="",
                           data_status="source_verified", known_at=KNOWN,
                           subject=s, metric="value", raw_value=1.0) for s in order]
        return r

    assert digest_mod.facts_fingerprint(mk(["A", "B"])) == digest_mod.facts_fingerprint(mk(["B", "A"]))


# --- ④ 원장이 없어도 리포트는 나온다 -----------------------------------------

def test_report_still_builds_when_the_ledger_lookup_fails(settings, monkeypatch):
    """"어떤 소스가 죽어도 리포트는 나온다" — 해석 복원이 리포트를 막으면 안 된다."""
    conn = _open(settings)
    _seed_one_fact(conn, settings.raw_dir)

    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(store_mod, "reusable_interpretation", boom)
    report = _report(conn)
    conn.close()

    assert report.facts  # 리포트는 정상적으로 나왔다
    assert report.interpretation.is_empty()


# --- ⑤ 이력(누가·언제·무엇을 근거로)도 함께 되살린다 -------------------------

def test_restored_interpretation_keeps_its_provenance(settings):
    """본문만 되살리면 어느 모델이 썼는지·검증에 걸린 게 있는지·근거가 무엇인지가
    사라진다. 실측 2026-08-05: 되살린 리포트에서 `meta.interpretation` 131줄이
    통째로 빠졌다. 감사 추적은 이 프로젝트의 핵심이다."""
    conn = _open(settings)
    _seed_one_fact(conn, settings.raw_dir)
    report = _report(conn)
    store_mod.record_interpretation(conn, {
        "report_type": report.report_type, "report_date": report.report_date,
        "cutoff_utc": report.cutoff_utc, "status": "ok",
        "facts_sha256": digest_mod.facts_fingerprint(report),
        "text": TEXT,
        "restorable_meta": {
            "model": "claude:haiku", "prompt_version": "interpretation_v2",
            "status": "ok", "violations": [], "evidence": {"F1": "..."},
        },
    })
    again = _report(conn)
    conn.close()

    meta = again.meta.get("interpretation")
    assert meta, "해석 이력이 사라졌다"
    assert meta["model"] == "claude:haiku"
    assert meta["prompt_version"] == "interpretation_v2"
    assert again.meta["interpretation_restored"]["meta_restored"] is True


def test_old_record_without_provenance_still_restores_the_text(settings):
    """이력을 안 남기던 시절의 판도 본문은 되살린다 — 다만 이력은 없다고 밝힌다."""
    conn = _open(settings)
    _seed_one_fact(conn, settings.raw_dir)
    report = _report(conn)
    store_mod.record_interpretation(conn, {
        "report_type": report.report_type, "report_date": report.report_date,
        "cutoff_utc": report.cutoff_utc, "status": "ok",
        "facts_sha256": digest_mod.facts_fingerprint(report),
        "text": TEXT,  # restorable_meta 없음
    })
    again = _report(conn)
    conn.close()

    assert again.interpretation.reading == "테스트 해석 본문"
    assert again.meta["interpretation_restored"]["meta_restored"] is False
