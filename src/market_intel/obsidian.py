"""Obsidian vault sync (spec B9) — `reports/**/*.json` -> markdown notes in
`~/Pensieve/market-intel/` (overridable with `MI_OBSIDIAN_DIR`).

The body is ST2's `render_markdown`; this module adds the two things that
turn a pile of flat notes into a graph and that only make sense with the
whole vault in view (which is exactly why `render_md._frontmatter` leaves
them out):

  * the full spec-B9 frontmatter — fixed key order, `market/*` tags derived
    from the universe, and a `subjects:` list of `[[wikilinks]]`;
  * inline `[[…]]` on each subject's **first** mention in the body, so
    Obsidian's graph view links the note to the entity note (which is never
    created here — Obsidian graphs unresolved links just fine, and writing
    stub notes into the CEO's vault is not this stage's business).

Nothing outside `<vault>/<YYYY>/` is ever written, read or deleted: the
vault root holds the CEO's own daily notes.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from .config import PROJECT_ROOT
from .reporting.model import Report
from .reporting.render_md import render_markdown, status_ko
from .universe import UNIVERSE

DEFAULT_VAULT = Path.home() / "Pensieve" / "market-intel"

_SYMBOL_META = {m["symbol"]: m for m in UNIVERSE}

# Calendar subjects are not symbols; they get their entity name from the
# same table `schedule._display_name` uses, so the vault and the site call
# the same thing by the same name.
_CALENDAR_NAMES = {"fomc": "FOMC", "bokmpc": "한국은행 금융통화위원회"}
_CALENDAR_COUNTRIES = {"fomc": "US", "bokmpc": "KR"}

# A markdown link target `](https://…/NVDA)` must never be turned into
# `](https://…/[[NVDA]])`.
_LINK_TARGET = re.compile(r"\]\([^)]*\)")


def vault_dir() -> Path:
    """spec B12 — `MI_OBSIDIAN_DIR` is the single new env var, optional."""
    override = os.environ.get("MI_OBSIDIAN_DIR", "")
    return Path(override).expanduser() if override else DEFAULT_VAULT


def _entity(subject: str) -> tuple[str, str] | None:
    """(display name, country) for a subject, or None if it is not an entity
    this vault links (an unknown subject is left as plain text rather than
    guessed into a graph node)."""
    if subject in _SYMBOL_META:
        meta = _SYMBOL_META[subject]
        return meta["name"], meta["country"]
    if subject in _CALENDAR_NAMES:
        return _CALENDAR_NAMES[subject], _CALENDAR_COUNTRIES[subject]
    return None


def _subjects_and_markets(report: Report) -> tuple[list[tuple[str, str]], list[str]]:
    """Ordered, de-duplicated (subject token, display name) pairs plus the
    `market/*` tag values they imply."""
    seen: dict[str, str] = {}
    markets: list[str] = []
    rows = list(report.facts) + list(report.market_reaction)
    tokens = [r.subject for r in rows]
    tokens += [e.subject for e in list(report.events) + list(report.schedule_changes)]
    countries = {e.country for e in list(report.events) + list(report.schedule_changes) if e.country}

    for token in tokens:
        if not token or token in seen:
            continue
        ent = _entity(token)
        if ent is None:
            continue
        name, country = ent
        seen[token] = name
        if country and country not in markets:
            markets.append(country)
    for country in sorted(countries):
        if country not in markets:
            markets.append(country)
    return list(seen.items()), markets


def _frontmatter(report: Report, subjects: list[tuple[str, str]], markets: list[str]) -> str:
    """spec B9 — fixed key order. Anything reordered here silently changes
    how the vault's Dataview/Properties views read every note."""
    tags = ["market-intel", f"report/{report.report_type}",
            f"status/{status_ko(report.data_status).replace(' ', '')}"]
    tags += [f"market/{c}" for c in markets]
    subject_links = ", ".join(f'"[[{name}]]"' for _token, name in subjects)
    lines = [
        "---",
        "project: market-intel",
        f"type: {report.report_type}",
        f"date: {report.report_date}",
        f'cutoff_kst: "{report.cutoff_kst}"',
        f"data_status: {status_ko(report.data_status)}",
        f"ai_interpretation: {'false' if report.interpretation.is_empty() else 'true'}",
        f"tags: [{', '.join(tags)}]",
        f"subjects: [{subject_links}]",
        "---",
    ]
    return "\n".join(lines) + "\n"


def _wikilink(token: str, name: str) -> str:
    """The link to emit for a mention of `token`.

    The graph node is always the **canonical universe name** (`NVIDIA`,
    `Samsung Electronics`, `FOMC`), so one entity is one node however the
    report happened to spell it. Where the report's own text differs from
    that name, Obsidian's alias form `[[NVIDIA|NVDA]]` keeps the rendered
    note byte-identical to the report — a bare `[[NVIDIA]]` would make the
    vault say "NVIDIA 종가" where the JSON and the site both say
    "NVDA 종가", i.e. the archive would no longer agree with itself.

    (spec B9 illustrates `["[[NVDA]]", "[[삼성전자]]"]` — ticker for one and
    a Korean name for another. That example cannot be followed literally
    without inventing a second name table; `universe.py` is the codebase's
    single source of entity names, so it wins. Deviation is deliberate.)
    """
    return f"[[{name}]]" if token == name else f"[[{name}|{token}]]"


def _link_first(text: str, token: str, name: str) -> str:
    """Wikilink the first mention of `token` that is neither already inside
    a `[[…]]` nor inside a markdown link target."""
    spans = [m.span() for m in _LINK_TARGET.finditer(text)]
    pattern = re.compile(r"(?<!\[)" + re.escape(token) + r"(?!\])")
    for m in pattern.finditer(text):
        if any(start <= m.start() < end for start, end in spans):
            continue
        return f"{text[:m.start()]}{_wikilink(token, name)}{text[m.end():]}"
    return text


def _wikilink_body(body: str, subjects: list[tuple[str, str]]) -> str:
    for token, name in subjects:
        # Prefer the ticker (that is what report labels actually contain);
        # fall back to the display name for reports that spell it out.
        linked = _link_first(body, token, name)
        if linked == body and name != token:
            linked = _link_first(body, name, name)
        body = linked
    return body


def note_path(vault_root: Path, report: Report, stem: str) -> Path:
    """spec B9 — `<vault>/<YYYY>/<stem>-<type>.md`."""
    return vault_root / report.report_date[:4] / f"{stem}-{report.report_type}.md"


def render_note(report: Report) -> str:
    subjects, markets = _subjects_and_markets(report)
    body = render_markdown(report, obsidian_frontmatter=False)
    return _frontmatter(report, subjects, markets) + "\n" + _wikilink_body(body, subjects)


def sync(reports_root: Path | None = None, vault_root: Path | None = None,
         since: date | None = None) -> dict:
    """Write one note per report JSON (overwriting, spec B9). Never raises:
    a vault that cannot be written is reported in the counters so the job
    still exits 0 (spec ST3 What #2)."""
    reports_root = Path(reports_root) if reports_root else PROJECT_ROOT / "reports"
    vault_root = Path(vault_root) if vault_root else vault_dir()

    written = 0
    failed: list[str] = []
    if not reports_root.exists():
        return {"written": 0, "vault": str(vault_root), "failed": failed}

    for path in sorted(reports_root.glob("*/*.json")):
        try:
            report = Report.from_json(path.read_text(encoding="utf-8"))
        except (ValueError, TypeError, KeyError):
            failed.append(str(path))
            continue
        if since is not None and report.report_date < since.isoformat():
            continue
        target = note_path(vault_root, report, path.stem)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_note(report), encoding="utf-8")
        except OSError:
            failed.append(str(target))
            continue
        written += 1

    return {"written": written, "vault": str(vault_root), "failed": failed}
