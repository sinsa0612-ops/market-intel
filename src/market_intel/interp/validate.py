"""Hallucination / banned-phrase / format validator (spec SA-5).

These six rules are not a from-scratch design: spec §사전 확인 사실 C ran a
prototype of exactly this ruleset against 17 hand-built adversarial sentences
and 32 *real* qwen3.5:9b/14b/27b-generated fields, and reports 17/17 and
31/32 respectively — the one rejection in the second set was a genuine LLM
error (a unit swap: "1.76%포인트" for what the report actually said was
"+1.76%"), not a false positive. Getting there took two corrections, both
recorded here so a future edit doesn't reintroduce them:

1. A first pass banned the raw substrings "매수"/"목표가"/etc. and blocked
   `외국인 매수세` and `금리 상단 목표가 3.75%` — both legitimate. Switched to
   the context-sensitive regexes below (rule 4), which only fire on
   trade-recommendation *phrasing*, not any sentence that happens to contain
   those characters.
2. A first pass only compared numeric tokens against the report's allowed
   numbers, and missed `12조 4천억원` — because 12 and 4 individually *do*
   appear somewhere in the report, just not as that combined magnitude.
   Added rule 3 (Korean magnitude words are banned outright in interpretation
   prose) on the grounds that the 4 generated reports never use `조`/`억`/`만`
   themselves (grep-verified, 0 hits) — so any appearance in interpretation
   text is by definition an ungrounded re-expression, not a citation.

`check()` intentionally does not short-circuit on the first violation: it
collects every rule that fired, because `apply.fill()` records the full
violation list in the repair prompt and in `meta["interpretation"]["violations"]`.
"""
from __future__ import annotations

import re
from typing import Any

# Rule 1 — format violations. Interpretation text is plain Korean prose; any
# of these mean either injected markup (rendered live by the un-escaped
# Obsidian markdown renderer — SA-13 trust boundary 2) or a raw URL that adds
# nothing over the report's already-normalized fact labels.
_FORMAT_MARKERS = ("<", ">", "](", "http://", "https://", "`")

# Rule 2 — length cap. Real outputs top out around 230 chars; 600 leaves
# generous room without allowing a runaway generation to dominate the report.
_MAX_LEN = 600

# Rule 3 — Korean order-of-magnitude words. 0 hits across all 4 generated
# report types (grep-verified) — the report layer never writes these, so an
# interpretation that does is re-expressing a number in a form nothing in the
# report actually states, which is exactly the failure mode this validator
# exists to catch even when the digits involved are individually real.
_KO_MAGNITUDE_RE = re.compile(r"\d[\d,.]*\s*[조억만]")

# Rule 4 — banned trade-recommendation / price-target / return-forecast
# phrasing (BRIEF rule 5, spec §1/§6.1). Context-sensitive regexes, not bare
# substrings — see correction #1 above for why.
_BANNED_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"목표\s*주가"), "목표주가"),
    (re.compile(r"적정\s*주가"), "적정주가"),
    (re.compile(r"투자\s*의견"), "투자의견"),
    (re.compile(r"비중\s*(확대|축소)"), "비중조절"),
    (re.compile(r"(매수|매도)\s*(하|해|할|한다|하라|권|추천|의견|타이밍|시점|구간|기회|시그널|신호)"), "매매권유"),
    (re.compile(r"사야|팔아야|담아야|비중을\s*(늘|줄)"), "매매권유"),
    (re.compile(r"손절|익절"), "매매권유"),
    (re.compile(r"목표\s*수익률|기대\s*수익률|예상\s*주가|주가\s*전망치"), "수익예측"),
    # Unit substitution — the one real error the empirical test caught (a 27b
    # output that restated "전일대비 +1.76%" as "1.76%포인트 상승"). Numeric
    # comparison alone cannot catch this because the digit itself is correct.
    (re.compile(r"%\s*포인트|%p\b|\bbp\b|퍼센트\s*포인트"), "단위변조"),
)

# Rule 5 — date tokens must be one of the report's own dates.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Rule 6 — numeric tokens. Not glued to a latin letter, so index-style
# identifiers (`KOSPI200`, `S&P500`, `Core16`) are skipped rather than
# treated as numbers to ground.
_NUM_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")
_UNIT_RE = re.compile(r"^\s*(%|％|퍼센트|원|달러|엔|위안|조|억|만|천|배|포인트|%[pP]|bp|pt)")
_SMALL_INT_EXEMPT_MAX = 12


def _numeric_tokens(text: str):
    """Yield (raw_token, value, decimal_places, followed_by_unit) for every
    numeric token in `text` that is a candidate for grounding."""
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        cleaned = tok.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        has_unit = bool(_UNIT_RE.match(text[m.end() : m.end() + 4]))
        yield tok, value, decimals, has_unit


def _as_texts(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(value)]


def allowed_numbers(report: dict) -> set[float]:
    """SA-5 rule 6's grounding set: headline + facts[]/market_reaction[]'s
    label/value/comparison/raw_value + events[]/schedule_changes[]'s
    when/name + report_date + cutoff_kst."""
    values: set[float] = set()
    texts: list[str] = list(_as_texts(report.get("headline")))
    for key in ("facts", "market_reaction"):
        for row in report.get(key) or []:
            texts += _as_texts(row.get("value"))
            texts += _as_texts(row.get("comparison"))
            texts += _as_texts(row.get("label"))
            raw = row.get("raw_value")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                values.add(float(raw))
    for key in ("events", "schedule_changes"):
        for row in report.get(key) or []:
            texts += _as_texts(row.get("when"))
            texts += _as_texts(row.get("name"))
    texts += _as_texts(report.get("report_date"))
    texts += _as_texts(report.get("cutoff_kst"))

    for text in texts:
        for _tok, value, _dec, _unit in _numeric_tokens(text):
            values.add(value)
    return values


def allowed_dates(report: dict) -> set[str]:
    """SA-5 rule 5's grounding set."""
    dates = {report.get("report_date") or "", (report.get("cutoff_kst") or "")[:10]}
    for key in ("events", "schedule_changes"):
        for row in report.get(key) or []:
            when = row.get("when") or ""
            dates.add(when[:10])
    dates.discard("")
    return dates


def check(report: dict, text: str) -> list[tuple[str, str]]:
    """Run all 6 SA-5 rules against `text`. Returns every violation found
    (kind, offending_token) — empty list means the field passes."""
    violations: list[tuple[str, str]] = []

    for marker in _FORMAT_MARKERS:
        if marker in text:
            violations.append(("format", marker))

    if len(text) > _MAX_LEN:
        violations.append(("length", str(len(text))))

    for m in _KO_MAGNITUDE_RE.finditer(text):
        violations.append(("ko_magnitude", m.group(0)))

    for pattern, name in _BANNED_PATTERNS:
        m = pattern.search(text)
        if m:
            violations.append((f"banned:{name}", m.group(0)))

    dates_ok = allowed_dates(report)
    for m in _DATE_RE.finditer(text):
        if m.group(0) not in dates_ok:
            violations.append(("date", m.group(0)))

    nums_ok = allowed_numbers(report)
    text_no_dates = _DATE_RE.sub(" ", text)
    for tok, value, decimals, has_unit in _numeric_tokens(text_no_dates):
        if decimals == 0 and abs(value) <= _SMALL_INT_EXEMPT_MAX and not has_unit:
            continue  # counts ("7종목", "2개 분기") need no grounding.
        if not any(round(allowed, decimals) == round(value, decimals) for allowed in nums_ok):
            violations.append(("num", tok))

    return violations


def _main(argv: list[str]) -> int:
    """`python -m market_intel.interp.validate <report.json>` — re-checks
    the 4 `Interpretation` fields a report file already carries against its
    own fact set, independent of whatever produced them. Exists for spec
    ST2's success-criteria step (2): fields the validator already accepted
    once (during `apply.fill`) must still validate clean on a second, cold
    pass — a field that only ever gets checked by the same code path that
    approved it in the first place is not really being checked."""
    import json
    import sys

    if len(argv) != 1:
        print("usage: python -m market_intel.interp.validate <report.json>", file=sys.stderr)
        return 2
    report = json.loads(open(argv[0], encoding="utf-8").read())
    interp = report.get("interpretation", {})
    ok = True
    for field in ("reading", "counter_reading", "thesis_impact", "next_check"):
        text = interp.get(field, "") or ""
        if not text:
            print(f"{field}: (empty, skipped)")
            continue
        violations = check(report, text)
        status = "PASS" if not violations else "FAIL"
        if violations:
            ok = False
        print(f"{field}: {status} {violations}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
