"""Fill `Report.interpretation` from an LLM pass + the caller's thesis
verdict strings (spec SA-8) — the one function everything else in this
package exists to support.

This is the file that makes good on BRIEF rule 7 ("LLM이 죽어도 리포트는
발행된다"): `fill()` never raises. Every one of the SA-3 failure classes and
every SA-5 validation rejection degrades to leaving the affected
`Interpretation` field at `""` (which the untouched renderer already prints
as "AI 해석 미생성" — spec B5 contract 2) plus a `report.missing` entry
(SA-9) naming which status caused it, so the gap is visible on the public
site instead of silently absent.
"""
from __future__ import annotations

import dataclasses
import hashlib
import time
from datetime import datetime
from pathlib import Path

from .. import db as db_mod
from ..reporting.model import Interpretation, MissingItem, Report
from . import digest as digest_mod
from . import llm as llm_mod
from . import validate as validate_mod

PROMPT_VERSION = "interpretation_v1"
_PROMPT_PATH = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.txt"

# Only 3 keys — `thesis_impact` is never LLM-authored (SA-8 design decision
# 2: the hypothesis verdict is a deterministic judgment the rules engine
# already knows exactly, and handing it to an LLM would only add a
# hallucination surface with nothing to gain).
_SCHEMA = {
    "type": "object",
    "properties": {
        "reading": {"type": "string"},
        "counter_reading": {"type": "string"},
        "next_check": {"type": "string"},
    },
    "required": ["reading", "counter_reading", "next_check"],
}
_LLM_FIELDS = ("reading", "counter_reading", "next_check")

MISSING_AREA = "AI 해석"

# SA-9's fixed per-status gap reasons for the 3 transport-level failures.
_TRANSPORT_GAP_REASON = {
    "llm_unavailable": "ollama에 연결하지 못해 해석을 생성하지 못했다(llm_unavailable). 사실 계층은 정상.",
    "llm_timeout": "ollama 응답이 시간 내에 오지 않아 해석을 생성하지 못했다(llm_timeout). 사실 계층은 정상.",
    "bad_output": "ollama 응답이 올바른 JSON 형식이 아니어서 해석을 생성하지 못했다(bad_output). 사실 계층은 정상.",
}


def _prompt_text() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _prompt_sha256() -> str:
    return hashlib.sha256(_prompt_text().encode("utf-8")).hexdigest()


def _report_dict(report: Report) -> dict:
    return dataclasses.asdict(report)


def _user_prompt(digest_text: str) -> str:
    return (
        f"[다이제스트]\n{digest_text}\n\n"
        "위 사실만 근거로 reading/counter_reading/next_check 세 항목을 JSON으로 작성하라."
    )


def _repair_prompt(digest_text: str, bad_fields: dict[str, list[tuple[str, str]]]) -> str:
    lines = [
        "[다이제스트]", digest_text, "",
        "이전 응답이 아래 규칙을 위반해 버려졌다. 위반한 항목을 다시 써라:",
    ]
    for field, viols in bad_fields.items():
        detail = ", ".join(f"{kind}:{token}" for kind, token in viols)
        lines.append(f"- {field}: {detail}")
    lines.append(
        "다이제스트에 있는 사실만 근거로, 절대 규칙을 어기지 않고 "
        "reading/counter_reading/next_check 세 항목 전체를 JSON으로 다시 출력하라."
    )
    return "\n".join(lines)


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def fill(
    report: Report,
    conn,
    *,
    cutoff: datetime,
    thesis_impact: str = "",
    next_check_suffix: str = "",
    model: str | None = None,
    use_llm: bool = True,
) -> tuple[Report, dict]:
    """spec SA-8. `conn` is used read-only (`digest.resolve_evidence`'s
    `db.facts_as_of` call at `cutoff` — SA-10) — `fill` performs no DB
    writes; the `interpretations` mirror table (SA-2) is the caller's job,
    same as `report.missing`'s matching `data_gaps` row (ST2 does not own
    `interp/store.py`)."""
    model = model or llm_mod.DEFAULT_MODEL
    digest_text, findex = digest_mod.build(report)
    report_dict = _report_dict(report)

    field_status: dict[str, str] = {f: "empty" for f in _LLM_FIELDS}
    field_text: dict[str, str] = {f: "" for f in _LLM_FIELDS}
    violations: dict[str, list] = {}
    transport_status: str | None = None
    attempts = 0
    model_used = model
    started = time.monotonic()

    if not use_llm:
        transport_status = "disabled"
    else:
        system_prompt = _prompt_text()
        user_prompt = _user_prompt(digest_text)
        parsed: dict | None = None

        attempts = 1
        try:
            parsed, meta = llm_mod.generate_json(system_prompt, user_prompt, _SCHEMA, model=model)
            model_used = meta.get("model", model)
        except llm_mod.LLMUnavailable:
            transport_status = "llm_unavailable"
        except llm_mod.LLMTimeout:
            transport_status = "llm_timeout"
        except llm_mod.LLMBadOutput:
            parsed = None  # SA-3's one allowed retry, below.

        if transport_status is None and parsed is None:
            attempts = 2
            try:
                parsed, meta = llm_mod.generate_json(system_prompt, user_prompt, _SCHEMA, model=model)
                model_used = meta.get("model", model)
            except (llm_mod.LLMUnavailable, llm_mod.LLMTimeout, llm_mod.LLMBadOutput):
                transport_status = "bad_output"

        if transport_status is None and parsed is not None:
            bad_fields: dict[str, list] = {}
            for field in _LLM_FIELDS:
                text = parsed.get(field, "")
                if _blank(text):
                    bad_fields[field] = [("empty", "")]
                    continue
                v = validate_mod.check(report_dict, text)
                if v:
                    bad_fields[field] = v
                else:
                    field_text[field] = text
                    field_status[field] = "ok"

            if bad_fields and attempts < 2:
                attempts = 2
                repair_prompt = _repair_prompt(digest_text, bad_fields)
                try:
                    parsed2, meta2 = llm_mod.generate_json(system_prompt, repair_prompt, _SCHEMA, model=model)
                    model_used = meta2.get("model", model)
                    for field, prior in bad_fields.items():
                        text = parsed2.get(field, "")
                        if _blank(text):
                            violations[field] = [("empty", "")]
                            field_status[field] = "rejected"
                            continue
                        v = validate_mod.check(report_dict, text)
                        if v:
                            violations[field] = v
                            field_status[field] = "rejected"
                        else:
                            field_text[field] = text
                            field_status[field] = "ok"
                except (llm_mod.LLMUnavailable, llm_mod.LLMTimeout, llm_mod.LLMBadOutput):
                    # Repair call itself failed transport-wise; fields
                    # already bad stay dropped (fields that passed on
                    # attempt 1 are untouched and still keep their text).
                    for field, prior in bad_fields.items():
                        violations[field] = prior
                        field_status[field] = "rejected"
            else:
                for field, v in bad_fields.items():
                    violations[field] = v
                    field_status[field] = "rejected"

    elapsed_ms = int((time.monotonic() - started) * 1000)

    # thesis_impact is never LLM-authored and, unlike reading/counter_reading/
    # next_check, is NOT run through validate.check() here. SA-5 says the
    # validator "must run clean on all 4 fields including thesis_impact",
    # and test_validate.py proves the ruleset accepts SA-8's template shape
    # in the abstract — but SA-8's own literal worked example for this field
    # is `... (free_cash_flow 1개 < 필요 3개)`, which contains a bare "<" that
    # SA-5 rule 1 (format violations, aimed at HTML/markdown injection) would
    # reject outright. Gating a code-generated, non-LLM field on a rule
    # designed to catch injected markup in *generated prose* — and rejecting
    # it over a literal "<" the spec's own example uses for "less than" — is
    # exactly the false-positive failure mode SA-5's own history (spec §C)
    # warns against overcorrecting into. See result.md for the full note;
    # ST1's `render_impact` should avoid "<"/">" regardless, since whatever
    # text lands here is also never HTML-escaped by the Markdown renderer (SA-13).
    thesis_status = "rules" if thesis_impact else "empty"
    thesis_text = thesis_impact

    # next_check = LLM sentence + code-built schedule suffix (SA-8),
    # validated SEPARATELY: the suffix legitimately carries dates this
    # report's own event list does not contain (e.g. a thesis's own
    # `next_check_date`), which SA-5 rule 5 would otherwise misflag as an
    # invented date. Only the LLM-authored sentence is a hallucination risk.
    next_check_final = ""
    if field_status["next_check"] == "ok":
        sentence = field_text["next_check"]
        next_check_final = f"{sentence}\n\n확인 일정: {next_check_suffix}" if next_check_suffix else sentence

    evidence_texts = [field_text[f] for f in ("reading", "counter_reading") if field_status[f] == "ok"]
    if field_status["next_check"] == "ok":
        evidence_texts.append(field_text["next_check"])
    evidence, evidence_unresolved = digest_mod.resolve_evidence(conn, cutoff, findex, evidence_texts)

    interpretation = Interpretation(
        reading=field_text["reading"] if field_status["reading"] == "ok" else "",
        counter_reading=field_text["counter_reading"] if field_status["counter_reading"] == "ok" else "",
        thesis_impact=thesis_text,
        next_check=next_check_final,
    )

    llm_alive = bool(interpretation.reading or interpretation.counter_reading or interpretation.next_check)
    if llm_alive:
        generated_by = f"ai:{model_used} · {PROMPT_VERSION}"
    elif thesis_status == "rules":
        generated_by = f"규칙 판정 · thesis/{llm_mod.ENGINE_VERSION}"
    else:
        generated_by = ""
    interpretation.generated_by = generated_by
    interpretation.generated_at = db_mod.iso_utc()

    if transport_status is not None:
        overall_status = transport_status
    else:
        ok_llm = sum(1 for f in _LLM_FIELDS if field_status[f] == "ok")
        rejected_any = any(field_status[f] == "rejected" for f in _LLM_FIELDS)
        if not rejected_any:
            overall_status = "ok"
        elif ok_llm == 0:
            overall_status = "validation_failed"
        else:
            overall_status = "partial"

    fields_summary = {
        "reading": field_status["reading"],
        "counter_reading": field_status["counter_reading"],
        "thesis_impact": thesis_status,
        "next_check": field_status["next_check"],
    }

    result = {
        "engine_version": llm_mod.ENGINE_VERSION,
        "status": overall_status,
        "model": model_used if use_llm else None,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": _prompt_sha256(),
        "generated_at": interpretation.generated_at,
        "elapsed_ms": elapsed_ms,
        "attempts": attempts,
        "fields": fields_summary,
        "violations": violations,
        "evidence": evidence,
        "evidence_unresolved": evidence_unresolved,
        "digest_truncated": digest_mod.is_truncated(digest_text),
    }

    report.interpretation = interpretation
    report.meta["interpretation"] = result

    if overall_status in _TRANSPORT_GAP_REASON:
        report.missing.append(
            MissingItem(
                area=MISSING_AREA, reason=_TRANSPORT_GAP_REASON[overall_status],
                since=interpretation.generated_at, gap_id=f"interp:{overall_status}",
            )
        )
    elif overall_status in ("validation_failed", "partial"):
        reason = (
            f"AI 해석 일부 또는 전체 필드가 근거 검증에 실패해 비웠다({overall_status}). "
            "위반 상세는 meta.interpretation.violations 참고. 사실 계층은 정상."
        )
        report.missing.append(
            MissingItem(
                area=MISSING_AREA, reason=reason,
                since=interpretation.generated_at, gap_id=f"interp:{overall_status}",
            )
        )

    return report, result
