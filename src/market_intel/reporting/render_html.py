"""HTML renderer — same `Report` in, HTML tags out instead of markdown
syntax. No markdown-to-HTML conversion anywhere (spec B0) and no template
engine (jinja2 etc. excluded, spec B0) — this is plain f-string HTML.

Section *markup* is independent from `render_md.py` (spec ST2 What #5:
"같은 Report에서 독립 렌더"); the section *layout* is deliberately not
duplicated — both renderers consume `render_md.sections()`, a public,
markup-free description of what goes where. Two hand-kept copies of the
layout is exactly what silently dropped two interpretation fields from
two report types in both formats at once (judge.md ④).

**Every externally-derived string is `html.escape()`d** (spec B12: release
names, company names, filing labels all originate from a public HTTP
response and are an XSS surface once they reach `docs/`). Escaping is not
enough for a URL — `html.escape` leaves `javascript:` intact — so hrefs
additionally go through `render_md.safe_href`'s scheme allowlist.

Produces a `<article>…</article>` fragment, not a full HTML document —
page chrome (`<head>`, nav, `style.css` link, `docs/index.html`'s "recent
20" card list) is `site.py`'s job (ST3, spec B8), which is why this
returns embeddable content rather than `<!doctype html>`.
"""
from __future__ import annotations

import html as html_mod

from .model import FactRow, Report
from .render_md import heading, safe_href, sections, status_ko


def _esc(value) -> str:
    return html_mod.escape(str(value), quote=True)


def _facts_table_html(rows: list[FactRow]) -> str:
    if not rows:
        return "<p>(해당 없음)</p>"
    out = [
        '<table><thead><tr><th>항목</th><th>수치</th><th>비교</th><th>원자료</th></tr></thead><tbody>'
    ]
    for r in rows:
        badge = status_ko(r.data_status)
        cls = "status-warn" if r.data_status in ("partial", "unverified") else "status-ok"
        value_cell = f'{_esc(r.value)} <span class="{cls}">{_esc(badge)}</span>' if badge else _esc(r.value)
        href = safe_href(r.source_url)
        if href:
            src = f'<a href="{_esc(href)}" rel="noopener" target="_blank">원자료</a>'
        else:
            # Not a link: an unsupported scheme (javascript:, data:, …) or a
            # relative/blank value. Shown as text so nothing is silently lost.
            src = _esc(r.source_url) if r.source_url else "-"
        out.append(f"<tr><td>{_esc(r.label)}</td><td>{value_cell}</td><td>{_esc(r.comparison)}</td><td>{src}</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _calendar_table_html(columns: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p>(해당 없음)</p>"
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _block_html(block: dict) -> str:
    kind = block["kind"]
    if kind == "facts":
        return _facts_table_html(block["rows"])
    if kind == "calendar":
        return _calendar_table_html(block["columns"], block["rows"])
    if kind == "missing":
        if not block["items"]:
            return ""
        items = "".join(f"<li>결측: {_esc(m.area)} — {_esc(m.reason)}</li>" for m in block["items"])
        return f"<ul>{items}</ul>"
    if kind == "interp":
        if block["badge"]:
            return f'<p class="ai-badge">{_esc(block["badge"])}</p><p>{_esc(block["text"])}</p>'
        return f"<p>{_esc(block['text'])}</p>"
    return f"<p>{_esc(block['text'])}</p>"


def render_html(report: Report) -> str:
    head = heading(report)
    parts = [f"<h1>{_esc(head['title'])}</h1>"]
    parts += [f"<p>{_esc(line)}</p>" for line in head["meta"]]
    for header, blocks in sections(report):
        parts.append(f"<section><h2>{_esc(header)}</h2>")
        parts += [_block_html(block) for block in blocks]
        parts.append("</section>")
    body = "".join(parts)
    return f'<article class="mi-report" data-report-type="{_esc(report.report_type)}">{body}</article>'
