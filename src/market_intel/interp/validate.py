r"""Hallucination / banned-phrase / format validator (spec SA-5).

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

--- 2026-08-02 hardening (judge.md §6 「양쪽 다 틀린 것」) ---

The ST2 judge ran 50 adversarial cases plus 16 real ollama generations
against both variants and reproduced six holes that the shipped ruleset —
this one — left open. Each fix below is keyed to that report, and every one
of them is pinned by `tests/interp/test_validate_adversarial.py`:

- rule 0 (`cjk`): the local model is Chinese-trained and leaked `持仓`/`背后`
  into Korean prose in 2 of 8 real runs; both passed as `status=ok` and
  reached the published markdown. There was no charset rule at all.
- rule 4 (`bp`/`%p`): `\b` does not exist between a digit and `bp`, and
  Python's `\w` includes Hangul so it does not exist between `%p` and `다`
  either — the rule the spec §C added to catch the one real 27b error was
  dead in exactly the two spellings a model actually writes (`47bp`,
  `1.8%p다`). Replaced with explicit latin-boundary lookarounds.
- rule 6 token regex: the trailing `(?![A-Za-z0-9])` guard (there to skip
  `KOSPI200`-style identifiers) meant any number with a latin unit glued to
  it was never checked at all — `1442KRW`, `9.99pct`, `1.76pp`, `999M` all
  walked through. The guard is now one-sided (leading only, which is what
  identifier-shaped tokens actually need) and a glued latin suffix is itself
  a violation unless it is one the report's own vocabulary uses.
- rule 7 (`unit`, new): `원/달러 환율은 1,442.1달러다` and `미국 실업률은
  4.20원이다` both passed — the digits are real, only the unit was swapped,
  and nothing compared a token's unit against the unit the report attaches
  to that same number.
- rule 6 sign handling: comparing signed values literally (correct, and the
  reason the winning variant caught 3 sign flips the loser missed) also
  rejected `0.21% 하락` for a report that says `-0.21%` — a legitimate
  Korean rendering. Absolute-value restatements are now accepted *only*
  when the surrounding wording states the matching direction, which as a
  side effect catches the inverse hallucination (`0.21% 상승`).
"""
from __future__ import annotations

import re

# Rule 0 — CJK ideographs / kana. The interpretation is Korean prose for a
# Korean reader; `qwen3.5:9b` is a Chinese-trained model and leaks source-
# language tokens mid-sentence (judge.md §6-2: `持仓`, `背后` in 2 of 8 real
# runs, both published). Banned outright rather than filtered, because the
# ban costs nothing here: a full scan of everything this project can put in
# front of the model or the reader — `src/**`, `reports/**` (6 report types),
# `docs/**`, `theses/**`, and all 5.8MB / 304 raw snapshots of
# `var/market_intel.db` including the DART filing payloads — contains **zero**
# characters in these ranges (measured 2026-08-02; the corpus is Hangul +
# latin + `·`/`±` punctuation only). Same argument as rule 3: a character
# class the report layer never produces cannot be a citation.
_CJK_RE = re.compile(
    r"[々〆぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿]"
)

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
    # The `\b` spelling this rule shipped with was dead in both of the forms
    # a model actually writes (judge.md §6-3): `\b` needs a word/non-word
    # transition, and `47bp` has a digit on the left while `1.8%p다` has
    # Hangul on the right — Python's `\w` counts both as word characters.
    # Latin-only lookarounds are the boundary this rule actually meant.
    (
        re.compile(r"%\s*포인트|퍼센트\s*포인트|%\s*[pP](?![A-Za-z])|(?<![A-Za-z])bps?(?![A-Za-z])"),
        "단위변조",
    ),
)

# Rule 5 — date tokens must be one of the report's own dates.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Rule 6 — numeric tokens. The *leading* guard is what identifier-shaped
# tokens need (`KOSPI200`, `S&P500`, `Core16`, and the digest's own `F34`
# fact references all put the letters BEFORE the digits). The trailing guard
# this originally carried skipped the token entirely whenever a latin unit
# was glued to its right, which is where `1442KRW`/`9.99pct`/`999M` walked
# through unchecked (judge.md §6-4) — so the suffix is captured and judged
# by rule 6b instead of silencing the whole token.
_NUM_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?")
_LATIN_SUFFIX_RE = re.compile(r"^[A-Za-z]+")
_UNIT_RE = re.compile(r"^\s*(%|％|퍼센트|원|달러|엔|위안|조|억|만|천|배|포인트|%[pP]|bp|pt)")
_SMALL_INT_EXEMPT_MAX = 12

# Rule 6b — latin suffixes the report's own vocabulary uses, measured over
# every string the digest can show the model (headline/label/value/comparison
# /event name across all 6 report types, 2026-08-02): `미10Y`, `2Y`, `13F`,
# `earnings_release_8k`, `180d`/`181d`/`272d`. Every latin unit the report
# writes it writes with a separating space (`1,442.07 USD`, `6,595.45 point`,
# `158,984 lin`) — never glued — so a glued suffix outside this set is, like
# rule 3's `조/억/만`, a re-expression of a number in a form nothing in the
# report states. Deliberately case-sensitive: `999K` (thousands) must stay a
# violation even though `8k` is legal.
_ALLOWED_LATIN_SUFFIXES = frozenset({"Y", "F", "d", "k"})

# Rule 7 — unit contradiction. Only the *currency* classes are enforced,
# because that is the direction of the attack the judge reproduced (`1,442.1
# 달러` for 원, `4.20원` for a rate) and because the report's own macro units
# are FRED's opaque `lin`/`point` strings — enforcing percent against those
# would reject the entirely correct `실업률 4.20%`.
# Known cost, measured over 71 real generated fields (1 hit): the USD/KRW
# market_reaction row states the won rate as `1,442.07 USD` (yfinance quote
# currency), so a correct `1,442.07 원` is rejected too. The report attaches
# both currencies to the same rate, so no rule can block the judge's
# `1,442.1달러` and pass that one — see
# tests/interp/test_validate_adversarial.py::
# test_known_false_positive_usdkrw_row_unit_mislabel, which is a strict
# xfail pointing at the real fix (label that row in 원, in `reporting/**`).
_UNIT_CLASSES = {
    "%": "percent", "％": "percent", "퍼센트": "percent",
    "원": "krw", "KRW": "krw", "달러": "usd", "USD": "usd",
    "엔": "jpy", "JPY": "jpy", "위안": "cny", "CNY": "cny", "유로": "eur", "EUR": "eur",
}
_CURRENCY_CLASSES = frozenset({"krw", "usd", "jpy", "cny", "eur"})
_KNOWN_UNIT_RE = re.compile(r"^[ ]?(퍼센트|%|％|원|달러|엔|위안|유로|KRW|USD|JPY|CNY|EUR)")
# Anything else glued to the number (`4.20 lin`, `6,595.45 point`, `7종목`,
# `2026년`) still says "this magnitude is not money" — enough to contradict a
# currency, not enough to name a unit. A number followed by punctuation or a
# bracket (`KOSPI 6,595.45(+17.9%)`) carries no unit at all and stays
# permissive, so adding a unit to a bare number is never invented evidence.
_OTHER_UNIT_RE = re.compile(r"^[ ]?[A-Za-z가-힣]")

# Rule 6c — direction words, for absolute-value restatements of a negative
# report value (`-0.21%` written as `0.21% 하락`). The nearest one wins.
_DOWN_RE = re.compile(r"하락|하회|내렸|내려|내리|떨어|감소|급락|하향|약세|낙폭|밀렸|빠졌|마이너스|줄었|축소|둔화")
_UP_RE = re.compile(r"상승|상회|올랐|올라|오르|증가|급등|확대|강세|반등|뛰었|늘었|플러스")
_DIRECTION_WINDOW = 30


def _unit_class(after: str) -> str | None:
    """Classify what follows a numeric token: a named unit class, the
    catch-all `"other"`, or `None` when nothing is attached."""
    m = _KNOWN_UNIT_RE.match(after)
    if m:
        return _UNIT_CLASSES[m.group(1)]
    return "other" if _OTHER_UNIT_RE.match(after) else None


def _numeric_tokens(text: str):
    """Yield (match, raw_token, value, decimal_places, followed_by_unit,
    unit_class, latin_suffix) for every numeric token in `text`."""
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        cleaned = tok.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        trailing = text[m.end() : m.end() + 8]
        has_unit = bool(_UNIT_RE.match(trailing))
        latin = _LATIN_SUFFIX_RE.match(trailing)
        yield (
            m, tok, value, decimals, has_unit, _unit_class(trailing),
            latin.group(0) if latin else "",
        )


def _direction_near(text: str, start: int, end: int) -> str | None:
    """`"down"` / `"up"` / `None` — whichever direction word sits closest to
    the token within `_DIRECTION_WINDOW` characters on either side."""
    window_start = max(0, start - _DIRECTION_WINDOW)
    window = text[window_start : end + _DIRECTION_WINDOW]
    best: tuple[int, str] | None = None
    for regex, label in ((_DOWN_RE, "down"), (_UP_RE, "up")):
        for m in regex.finditer(window):
            pos = window_start + m.start()
            distance = start - pos if pos < start else pos - end
            if best is None or distance < best[0]:
                best = (distance, label)
    return best[1] if best else None


def _occurrences(report: dict) -> list[tuple[float, str | None]]:
    """Every number the report states, paired with the unit class written
    next to it — the grounding set of SA-5 rule 6, plus the unit dimension
    rule 7 needs. A row's `raw_value` inherits the unit of its own `value`
    string (`"1,442.07 USD"` -> usd), since that float IS that string."""
    found: list[tuple[float, str | None]] = []

    def scan(text: str) -> str | None:
        first: str | None = None
        for i, (_m, _tok, value, _dec, _has_unit, unit_cls, _latin) in enumerate(_numeric_tokens(text)):
            found.append((value, unit_cls))
            if i == 0:
                first = unit_cls
        return first

    scan(str(report.get("headline") or ""))
    for key in ("facts", "market_reaction"):
        for row in report.get(key) or []:
            row_unit = scan(str(row.get("value") or ""))
            scan(str(row.get("comparison") or ""))
            scan(str(row.get("label") or ""))
            raw = row.get("raw_value")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                found.append((float(raw), row_unit))
    for key in ("events", "schedule_changes"):
        for row in report.get(key) or []:
            scan(str(row.get("when") or ""))
            scan(str(row.get("name") or ""))
    scan(str(report.get("report_date") or ""))
    scan(str(report.get("cutoff_kst") or ""))
    return found


def allowed_numbers(report: dict) -> set[float]:
    """SA-5 rule 6's grounding set: headline + facts[]/market_reaction[]'s
    label/value/comparison/raw_value + events[]/schedule_changes[]'s
    when/name + report_date + cutoff_kst."""
    return {value for value, _unit in _occurrences(report)}


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

    seen_cjk: set[str] = set()
    for m in _CJK_RE.finditer(text):
        if m.group(0) not in seen_cjk:
            seen_cjk.add(m.group(0))
            violations.append(("cjk", m.group(0)))

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

    occurrences = _occurrences(report)
    nums_ok = {value for value, _unit in occurrences}
    # Blank the dates out *in place* (same length) so rule 6c's direction
    # window still lines up with the caller's original string.
    text_no_dates = _DATE_RE.sub(lambda m: " " * len(m.group(0)), text)
    for m, tok, value, decimals, has_unit, unit_cls, latin in _numeric_tokens(text_no_dates):
        if latin and latin not in _ALLOWED_LATIN_SUFFIXES:
            violations.append(("latin_unit", tok + latin))

        if decimals == 0 and abs(value) <= _SMALL_INT_EXEMPT_MAX and not has_unit:
            continue  # counts ("7종목", "2개 분기") need no grounding.

        if not any(round(allowed, decimals) == round(value, decimals) for allowed in nums_ok):
            signed = tok[0] in "+-"
            mirrored = any(round(allowed, decimals) == round(-value, decimals) for allowed in nums_ok)
            # `0.21% 하락` for a report that says `-0.21%` is correct Korean,
            # not a hallucination — but only when the sentence actually says
            # "down". An explicit sign is never reinterpreted: `+0.21%`
            # against `-0.21%` is the irreversible failure this whole
            # validator exists for.
            if not signed and mirrored and _direction_near(text_no_dates, m.start(), m.end()) == "down":
                continue
            violations.append(("sign" if (not signed and mirrored) else "num", tok))
            continue

        if unit_cls in _CURRENCY_CLASSES:
            # Only an exact-value citation pins down which occurrence is
            # being quoted, and therefore which unit belongs to it; a rounded
            # one (`1,442원` for 1,442.07) could be any of several, so it is
            # left alone. `None` = the report states that number bare, which
            # contradicts nothing.
            exact = {unit for value_, unit in occurrences if value_ == value}
            if exact and None not in exact and unit_cls not in exact:
                written = _KNOWN_UNIT_RE.match(text_no_dates[m.end() : m.end() + 8])
                violations.append(("unit", tok + (written.group(1) if written else "")))

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
