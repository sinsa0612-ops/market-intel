"""SA-8 `apply.fill()` tests: the 4 failure classes, partial success,
evidence resolution, the information barrier, and the boundary rules
(no DB writes, no `reporting/**`/`fact_revisions` access anywhere in
`interp/`)."""
from __future__ import annotations

from datetime import datetime, timezone
import ast
from pathlib import Path

import pytest

from market_intel import interp as interp_pkg
from market_intel.interp import apply as apply_mod
from market_intel.interp import llm as llm_mod
from market_intel.interp import thesis as thesis_mod

from tests.interp.conftest import macro_fc, make_fact_row, make_report, seed_fact

_OK_FIELDS = {
    "reading": "F1의 실업률은 4.20%로 안정적이다. 고용시장은 완만한 흐름을 보인다.",
    "counter_reading": "다만 표본이 한 달치라 추세 반전 신호로 보기는 이르다.",
    "next_check": "다음 실업률 발표에서 4.20%를 유의미하게 벗어나면 해석이 갈린다.",
}


def _patch_generate(monkeypatch, side_effect):
    calls = []

    def fake(system, user, schema, *, model=llm_mod.DEFAULT_MODEL, host=llm_mod.DEFAULT_HOST, timeout_s=llm_mod.DEFAULT_TIMEOUT):
        calls.append({"system": system, "user": user, "model": model})
        result = side_effect(len(calls))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(apply_mod.llm_mod, "generate_json", fake)
    return calls


def _cutoff(report):
    return datetime.fromisoformat(report.cutoff_utc)


# --- 4 failure classes, never raising -------------------------------------

def test_llm_unavailable_never_raises_and_empties_fields(monkeypatch, conn):
    report = make_report()
    _patch_generate(monkeypatch, lambda n: llm_mod.LLMUnavailable("connection refused"))
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["status"] == "llm_unavailable"
    assert report.interpretation.reading == ""
    assert report.interpretation.counter_reading == ""
    assert report.interpretation.next_check == ""
    assert result["attempts"] == 1
    assert any(m.area == "AI 해석" and m.gap_id == "interp:llm_unavailable" for m in report.missing)


def test_llm_timeout_never_raises_no_retry(monkeypatch, conn):
    report = make_report()
    calls = _patch_generate(monkeypatch, lambda n: llm_mod.LLMTimeout("timed out"))
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["status"] == "llm_timeout"
    assert len(calls) == 1  # SA-3: timeout is never retried
    assert result["attempts"] == 1
    assert report.interpretation.is_empty() or (
        not report.interpretation.reading and not report.interpretation.counter_reading
    )


def test_bad_output_retries_once_then_gives_up(monkeypatch, conn):
    report = make_report()
    calls = _patch_generate(monkeypatch, lambda n: llm_mod.LLMBadOutput("not json"))
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["status"] == "bad_output"
    assert len(calls) == 2  # SA-3: bad_output gets exactly one retry
    assert result["attempts"] == 2
    assert report.interpretation.reading == ""


def test_bad_output_recovers_on_retry(monkeypatch, conn):
    report = make_report()

    def side_effect(n):
        if n == 1:
            return llm_mod.LLMBadOutput("garbled")
        return (dict(_OK_FIELDS), {"model": "qwen3.5:9b"})

    calls = _patch_generate(monkeypatch, side_effect)
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["status"] == "ok"
    assert len(calls) == 2
    assert report.interpretation.reading != ""


def test_validation_failure_all_fields_rejected_both_attempts(monkeypatch, conn):
    bad = {"reading": "목표주가 250달러를 제시한다.", "counter_reading": "매수 구간이다.",
           "next_check": "지금이 매수 타이밍이다."}
    calls = _patch_generate(monkeypatch, lambda n: (dict(bad), {"model": "qwen3.5:9b"}))
    report = make_report()
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["status"] == "validation_failed"
    assert len(calls) == 2  # attempt 1 + 1 repair retry
    assert report.interpretation.reading == ""
    assert report.interpretation.counter_reading == ""
    assert report.interpretation.next_check == ""
    assert set(result["violations"]) == {"reading", "counter_reading", "next_check"}


# --- partial success: 1 field bad, 3 fields kept --------------------------

def test_partial_success_one_field_rejected_others_kept(monkeypatch, conn):
    first = dict(_OK_FIELDS)
    first["counter_reading"] = "목표주가 250달러를 제시한다."  # the one bad field

    def side_effect(n):
        if n == 1:
            return (first, {"model": "qwen3.5:9b"})
        # repair attempt: still bad (LLM keeps insisting)
        return ({"reading": "무관", "counter_reading": "여전히 매수 구간이다.", "next_check": "무관"},
                {"model": "qwen3.5:9b"})

    calls = _patch_generate(monkeypatch, side_effect)
    report = make_report()
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert len(calls) == 2
    assert result["status"] == "partial"
    assert report.interpretation.reading == _OK_FIELDS["reading"]
    assert report.interpretation.counter_reading == ""  # dropped
    assert "next_check" in report.interpretation.next_check or report.interpretation.next_check != ""
    assert result["fields"]["counter_reading"] == "rejected"
    assert result["fields"]["reading"] == "ok"
    assert any(m.gap_id == "interp:partial" for m in report.missing)


def test_repair_recovers_the_bad_field(monkeypatch, conn):
    first = dict(_OK_FIELDS)
    first["counter_reading"] = "목표주가 250달러를 제시한다."

    def side_effect(n):
        if n == 1:
            return (first, {"model": "qwen3.5:9b"})
        fixed = dict(_OK_FIELDS)
        return (fixed, {"model": "qwen3.5:9b"})

    calls = _patch_generate(monkeypatch, side_effect)
    report = make_report()
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert len(calls) == 2
    assert result["status"] == "ok"
    assert report.interpretation.counter_reading == _OK_FIELDS["counter_reading"]


# --- success path + evidence resolution + generated_by --------------------

def test_success_sets_generated_by_and_interpretation(monkeypatch, conn):
    _patch_generate(monkeypatch, lambda n: (dict(_OK_FIELDS), {"model": "qwen3.5:9b"}))
    report = make_report()
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report), model="qwen3.5:9b")

    assert result["status"] == "ok"
    assert report.interpretation.generated_by == f"ai:qwen3.5:9b · {apply_mod.PROMPT_VERSION}"
    assert report.interpretation.generated_at
    # 문자열을 손으로 박아 두는 이유: 프롬프트를 갈아끼우는 것은 발행물의 성격을
    # 바꾸는 결정이라 테스트가 한 번 막아 세워야 한다. v2 -> v3는 2026-08-12,
    # "F-번호를 빼도 말이 되게 쓸 것"(규칙 1-1)을 넣으면서 올렸다.
    assert result["prompt_version"] == apply_mod.PROMPT_VERSION == "interpretation_v4"
    assert result["prompt_sha256"]


def test_evidence_resolved_for_cited_fnum(monkeypatch, conn, raw_dir):
    fc = macro_fc("UNRATE", "2026-07-01T00:00:00+00:00", 4.2)
    known_at = "2026-08-01T00:00:00+00:00"
    fact_id = seed_fact(conn, raw_dir, "fred", fc, known_at)

    row = make_fact_row("미국 실업률", "4.20%", "", subject="UNRATE", metric="value", known_at=known_at)
    report = make_report(facts=[row], cutoff_utc="2026-08-02T00:00:00+00:00")
    _patch_generate(monkeypatch, lambda n: (dict(_OK_FIELDS), {"model": "qwen3.5:9b"}))

    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))
    assert result["status"] == "ok"
    evidence_fact_ids = {e[1] for e in result["evidence"]}
    assert fact_id in evidence_fact_ids


def test_information_barrier_excludes_late_facts(monkeypatch, conn, raw_dir):
    """spec SA-10: `fill` must not resolve evidence for facts known after
    the cutoff it was given, even though the digest text is built from the
    already-cutoff-respecting `report.facts`."""
    fc = macro_fc("UNRATE", "2026-07-01T00:00:00+00:00", 4.2)
    late_known_at = "2026-08-05T00:00:00+00:00"
    seed_fact(conn, raw_dir, "fred", fc, late_known_at)

    row = make_fact_row("미국 실업률", "4.20%", "", subject="UNRATE", metric="value", known_at=late_known_at)
    report = make_report(facts=[row], cutoff_utc="2026-08-02T00:00:00+00:00")
    fields = dict(_OK_FIELDS)
    fields["reading"] = "F1을 인용한다."
    _patch_generate(monkeypatch, lambda n: (fields, {"model": "qwen3.5:9b"}))

    early_cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)  # before the fact was known
    report, result = apply_mod.fill(report, conn, cutoff=early_cutoff)
    assert result["evidence"] == []
    assert "F1" in result["evidence_unresolved"]


# --- disabled / thesis-only path -------------------------------------------

def test_use_llm_false_is_disabled_status(conn):
    report = make_report()
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report), use_llm=False)
    assert result["status"] == "disabled"
    assert result["attempts"] == 0
    assert report.interpretation.reading == ""


def test_thesis_impact_alone_fills_field_and_generated_by(conn):
    """final-review F6: the byline must carry the *judgment* engine's own
    version (thesis/2b.3), not the LLM engine's (2b.2). A bare
    `startswith("규칙 판정")` — and worse, calling `fill()` without even
    passing `thesis_engine_version` — let a reversion to the wrong engine's
    version through 991-green; both are pinned down here."""
    report = make_report()
    report, result = apply_mod.fill(
        report, conn, cutoff=_cutoff(report), use_llm=False,
        thesis_impact="가설 1건 판정 — 강화 0 · 유지 1 · 약화 0 · 무효 0 · 판정 불가 0.",
        next_check_suffix="확인 일정: 가설 재점검 2026-11-01",
        thesis_engine_version=thesis_mod.ENGINE_VERSION,
    )
    assert report.interpretation.thesis_impact != ""
    assert thesis_mod.ENGINE_VERSION != llm_mod.ENGINE_VERSION, (
        "두 상수가 우연히 같아지면 아래 단언이 무의미해진다"
    )
    assert report.interpretation.generated_by == f"규칙 판정 · thesis/{thesis_mod.ENGINE_VERSION}"
    assert result["fields"]["thesis_impact"] == "rules"


def test_next_check_suffix_appended_only_when_llm_field_ok(monkeypatch, conn):
    _patch_generate(monkeypatch, lambda n: (dict(_OK_FIELDS), {"model": "qwen3.5:9b"}))
    report = make_report()
    report, result = apply_mod.fill(
        report, conn, cutoff=_cutoff(report), next_check_suffix="확인 일정: 가설 재점검 2026-11-01",
    )
    assert result["status"] == "ok"
    assert "확인 일정: 가설 재점검 2026-11-01" in report.interpretation.next_check
    assert _OK_FIELDS["next_check"] in report.interpretation.next_check


def test_never_raises_on_any_of_the_4_failure_paths(monkeypatch, conn):
    report = make_report()
    for exc in (llm_mod.LLMUnavailable("x"), llm_mod.LLMTimeout("x"), llm_mod.LLMBadOutput("x")):
        _patch_generate(monkeypatch, lambda n, exc=exc: exc)
        r2, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))
        assert result["status"] in ("llm_unavailable", "llm_timeout", "bad_output")


# --- re-running on the same report is idempotent ---------------------------

def test_missing_entries_do_not_accumulate_across_reruns(monkeypatch, conn):
    """judge.md §6-7: ST3's job pipeline writes the interpretation back into
    the report file in place, so a catch-up or a retry re-runs `fill()` on a
    report that already carries an `interp:*` gap — and the public site's
    결측 list grew one identical line every time (3 runs -> 3 entries)."""
    report = make_report()
    _patch_generate(monkeypatch, lambda n: llm_mod.LLMUnavailable("connection refused"))
    for _ in range(3):
        report, _result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    interp_gaps = [m for m in report.missing if m.gap_id.startswith("interp:")]
    assert len(interp_gaps) == 1, [m.gap_id for m in report.missing]


def test_rerun_does_not_drop_the_report_own_missing_items(monkeypatch, conn):
    """The de-duplication must only reach `fill()`'s own `interp:*` rows —
    the fact layer's gaps belong to the report, not to this stage."""
    from market_intel.reporting.model import MissingItem

    report = make_report()
    report.missing.append(MissingItem(
        area="한국 수급", reason="수급 provider가 0건을 반환함", since="2026-08-01T00:00:00+00:00",
        gap_id="flows:kr_net_buy",
    ))
    _patch_generate(monkeypatch, lambda n: llm_mod.LLMUnavailable("x"))
    for _ in range(2):
        report, _result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert [m.gap_id for m in report.missing] == ["flows:kr_net_buy", "interp:llm_unavailable"]


def test_rerun_replaces_a_stale_status_gap(monkeypatch, conn):
    """A retry that succeeds must clear the previous run's failure line
    instead of leaving 사장님 with a stale '해석 미생성' next to a filled field."""
    report = make_report()
    _patch_generate(monkeypatch, lambda n: llm_mod.LLMUnavailable("x"))
    report, _ = apply_mod.fill(report, conn, cutoff=_cutoff(report))
    assert any(m.gap_id == "interp:llm_unavailable" for m in report.missing)

    _patch_generate(monkeypatch, lambda n: (dict(_OK_FIELDS), {"model": "qwen3.5:9b"}))
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["status"] == "ok"
    assert [m.gap_id for m in report.missing if m.gap_id.startswith("interp:")] == []


# --- structural boundary tests ---------------------------------------------

def _code_without_docstrings(path: Path) -> str:
    """Executable source only — comments and docstrings removed.

    Scanning raw file text made the guard fire on `thesis.py`'s module
    docstring, which says it reads facts through `db.facts_as_of` and *never*
    a raw query against `fact_revisions`. A prose promise not to do the thing
    must not read as doing the thing, or the guard trains people to delete
    the explanation instead of the violation."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
    return ast.unparse(tree)


def test_interp_never_touches_fact_revisions_or_raw_sql():
    interp_dir = Path(interp_pkg.__file__).parent
    py_files = [p for p in interp_dir.rglob("*.py")]
    assert py_files
    for path in py_files:
        code = _code_without_docstrings(path)
        # The real invariant (BRIEF rule 1): nothing under interp/ may read or
        # write the append-only fact ledger. Holds for every file, store.py
        # included — the interpretation layer reads facts only through
        # `db.facts_as_of`.
        assert "fact_revisions" not in code, f"{path} references fact_revisions directly"
        assert "raw_snapshots" not in code, f"{path} references raw_snapshots directly"
        assert "upsert_fact" not in code, f"{path} writes facts directly"
        assert "insert_raw_snapshot" not in code, f"{path} writes raw snapshots directly"
        # `store.py` is 2B's own DB access layer (spec SA-1); SQL against the
        # 2B tables (theses / thesis_reviews / interpretations / job_runs) is
        # its job. Every other module stays SQL-free so the interpretation
        # path cannot grow a second way to reach the database.
        # `ops.py`(ST3)는 SA-12가 지정한 운영 상태 출처 — `provider_runs`/
        # `collect_runs`/`data_gaps` — 를 읽는데, 이 셋은 2B 테이블이 아니어서
        # `store.py`에 접근자가 없고 `store.py`는 ST1 소유라 ST3가 고칠 수 없다.
        # 사실 원장 금지(위 4줄)는 `ops.py`에도 그대로 적용된다.
        if path.name not in ("store.py", "ops.py"):
            assert "SELECT " not in code, f"{path} contains a raw SQL SELECT"


def test_apply_and_digest_never_import_reporting_build_or_renderers():
    for path in (Path(apply_mod.__file__),):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("reporting.build", "reporting import build", "render_md", "render_html"):
            assert forbidden not in text, f"{path} imports a reporting/** module ST2 must not touch"


def test_fill_does_not_import_interp_thesis_or_store():
    """ST2 boundary: `apply.py` must not import `interp.thesis`/`interp.store`
    (worktree independence — those are ST1's files, and `fill()`'s
    thesis_impact/next_check_suffix are plain string parameters)."""
    text = Path(apply_mod.__file__).read_text(encoding="utf-8")
    assert "interp.thesis" not in text
    assert "interp.store" not in text
    assert "from . import thesis" not in text
    assert "from . import store" not in text

# --- 반대 해석은 당시 해석에 딸린 글이다 (CEO 지적 2026-08-04) ---------------

def _payload(**over):
    """`_patch_generate`의 fake가 그대로 돌려줄 (parsed, meta) 쌍."""
    base = dict(_OK_FIELDS)
    base.update(over)
    return base, {"model": "claude:haiku"}


def test_counter_reading_is_withheld_when_reading_is_not_published(monkeypatch, conn):
    """당시 해석이 반려됐는데 반대 해석만 나가면, 독자는 화면에 없는 주장을
    반박하는 문단을 읽는다(실측 2026-08-04 morning). 두 문단은 한 번의 생성에서
    같은 추론으로 나오므로, 당시 해석이 근거 불충분이면 반대 해석도 같은 전제를
    물려받았을 뿐 우연히 안 걸린 것일 수 있다."""
    report = make_report()
    # 당시 해석에만 리포트에 없는 숫자를 넣어 반려시킨다.
    _patch_generate(monkeypatch, lambda n: _payload(reading="KOSPI는 9,999.99를 기록했다."))
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["fields"]["reading"] == "rejected"
    assert result["fields"]["counter_reading"] == "withheld", "반박할 대상이 없으면 보류한다"
    assert report.interpretation.reading == ""
    assert report.interpretation.counter_reading == ""


def test_counter_reading_survives_when_reading_is_published(monkeypatch, conn):
    """딸린 글이라는 이유로 멀쩡한 반대 해석까지 버리면 안 된다."""
    report = make_report()
    _patch_generate(monkeypatch, lambda n: _payload())
    report, result = apply_mod.fill(report, conn, cutoff=_cutoff(report))

    assert result["fields"]["reading"] == "ok"
    assert result["fields"]["counter_reading"] == "ok"
    assert report.interpretation.counter_reading


def test_empty_counter_reading_says_why_it_is_empty():
    """'AI 해석 미생성'과 '반박할 대상이 없다'는 다른 사정이다 — 같은 문구로
    쓰면 독자가 구별할 수 없다."""
    from market_intel.reporting import render_md as md

    both_empty = make_report()
    both_empty.interpretation.reading = ""
    both_empty.interpretation.counter_reading = ""
    assert md._interp(both_empty, "counter_reading")["text"] == md.NO_COUNTER_WITHOUT_READING
    assert md._interp(both_empty, "reading")["text"] == md.NO_INTERP

    reading_only = make_report()
    reading_only.interpretation.reading = "국내 증시는 약세였다."
    reading_only.interpretation.counter_reading = ""
    assert md._interp(reading_only, "counter_reading")["text"] == md.NO_INTERP
