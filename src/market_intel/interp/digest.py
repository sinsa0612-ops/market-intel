"""Report -> LLM digest + F-index, and F-number -> (fact_id, revision_no)
evidence resolution (spec SA-6).

Deliberately does not touch `reporting/**`: it only reads the already-built
`Report` object's public fields, plus one more `db.facts_as_of` call (the
same read path `reporting/build.py` itself uses — spec B5 contract 3) to
resolve which PIT fact-table row(s) an F-number actually points at.
"""
from __future__ import annotations

import re

from .. import db as db_mod
from ..reporting.model import FactRow, Report

MAX_CHARS = 40_000
MAX_EVENTS = 12
_TRUNCATION_MARKER_RE = re.compile(r"\(이하 \d+건 생략\)")
# Not `\bF(\d+)\b`: Korean particles attach directly to the citation with no
# separator ("F1의", "F1을", "F1에서 ..."), and Python's `\w` (hence `\b`)
# treats Hangul as a word character — so a trailing `\b` never matches
# "F1의" at all (digit and 의 are both \w, no boundary between them). Guard
# the left side only (no letter/digit glued before the F) and cap the run of
# digits instead.
_FNUM_RE = re.compile(r"(?<![A-Za-z0-9])F(\d+)(?!\d)")


def _fact_line(n: int, row: FactRow) -> str:
    return f"F{n}. {row.label} = {row.value} | {row.comparison}"


def build(report: Report) -> tuple[str, dict[str, FactRow]]:
    """spec SA-6: facts[] first, then market_reaction[] (continuous
    numbering), then [결측], then [다가오는 일정] (max 12), headed by report
    type/date/cutoff/headline. Raw source URLs are never included — they
    cost prompt tokens and give the model nothing useful to interpret, and
    there is no reason to hand API-key-masked URLs to a local LLM at all."""
    findex: dict[str, FactRow] = {}
    fact_lines: list[str] = []
    for i, row in enumerate(list(report.facts) + list(report.market_reaction), start=1):
        key = f"F{i}"
        findex[key] = row
        fact_lines.append(_fact_line(i, row))

    header = "\n".join(
        [
            f"[리포트] {report.title} ({report.report_type})",
            f"날짜: {report.report_date}  차단선(KST): {report.cutoff_kst}  차단선(UTC): {report.cutoff_utc}",
            f"헤드라인: {report.headline}",
            # 서브태스크 B: 시장 폭(관측기업 + 한국 전체)을 AI 해석이 보는
            # 요약에 넣는다. 안 넣으면 화면은 "종목 폭은 평범"인데 해석은
            # "시장이 5% 빠졌다"고 쓰는 모순이 생긴다(spec §3).
            f"시장 폭: {report.breadth}",
            "",
            "[사실]",
        ]
    )

    missing_lines = [f"- {m.area}: {m.reason}" for m in report.missing] or ["(없음)"]
    events_lines = [f"- {e.when} {e.name}" for e in list(report.events)[:MAX_EVENTS]] or ["(없음)"]
    tail = "\n".join(["", "[결측]"] + missing_lines + ["", "[다가오는 일정]"] + events_lines)

    # Truncate from the tail of the fact list (not mid-line) if the digest
    # would exceed the 40,000-char safety valve. The real max observed is
    # quarterly's 7,610 chars, so this loop never runs in practice today —
    # it exists so a future universe expansion fails safely instead of
    # blowing past num_ctx silently.
    kept = fact_lines
    while True:
        body = "\n".join(kept) if kept else "(없음)"
        omitted = len(fact_lines) - len(kept)
        marker = f"\n(이하 {omitted}건 생략)" if omitted else ""
        text = f"{header}\n{body}{marker}\n{tail}"
        if len(text) <= MAX_CHARS or not kept:
            break
        kept = kept[:-1]

    return text, findex


def is_truncated(digest_text: str) -> bool:
    return bool(_TRUNCATION_MARKER_RE.search(digest_text))


def resolve_evidence(
    conn, cutoff, findex: dict[str, FactRow], texts: list[str]
) -> tuple[list[list], list[str]]:
    """spec SA-6 "근거 해소": scan `texts` for `F<n>` citations, resolve each
    to every matching `fact_` + `revisions` row via `(subject, metric, known_at)`
    (re-read `db.facts_as_of` at the same `cutoff` the report itself used —
    SA-10). Multi-match records all of them; an F-number the LLM invented
    (not in `findex`) or that can no longer be resolved goes to
    `evidence_unresolved` — that is not treated as a failure (SA-6)."""
    cited: set[str] = set()
    for text in texts:
        for m in _FNUM_RE.finditer(text):
            cited.add(f"F{m.group(1)}")
    if not cited:
        return [], []

    rows = db_mod.facts_as_of(conn, cutoff)
    index: dict[tuple[str, str, str], list] = {}
    for row in rows:
        index.setdefault((row["subject"], row["metric"], row["known_at"]), []).append(row)

    evidence: list[list] = []
    unresolved: list[str] = []
    for fnum in sorted(cited, key=lambda s: int(s[1:])):
        fact_row = findex.get(fnum)
        if fact_row is None:
            unresolved.append(fnum)
            continue
        matches = index.get((fact_row.subject, fact_row.metric, fact_row.known_at), [])
        if not matches:
            unresolved.append(fnum)
            continue
        for match in matches:
            evidence.append([fnum, match["fact_id"], match["revision_no"]])
    return evidence, unresolved
