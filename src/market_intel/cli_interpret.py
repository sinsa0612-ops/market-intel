"""`interpret` CLI (spec SA-11), wired into `cli.py` only through the
`CLI_EXTENSIONS` hook (spec B1/SA-1) — mirrors `cli_report.py`'s own pattern
of never being imported by name from `cli.py`.

This is the ONE place ST2 optionally imports `market_intel.interp.thesis`
(spec ST2 "무엇을 만드는가" item 6) — the same `exc.name` guard `cli.py`'s
`_extension_modules` already uses, so a worktree that does not have ST1's
`interp/thesis.py` merged in yet degrades to `thesis_impact=""` instead of
crashing every `interpret` call. [ASSUMPTION], flagged for the ST3
integrator: `thesis.review()` needs a `theses` list, and `interp/thesis.py`'s
own signature table (spec ST1) exposes `load_file(path) -> list[dict]` for
that — reading `theses/theses.json` fresh, not going through ST1's
`interp/store.py` (whose functions ST2 was never told to import here). This
keeps the file's only optional import to `interp.thesis`, per spec, but the
alternative reading (pull the current DB-loaded theses via `store.list_theses`
instead) is equally plausible and worth a second look once ST1 is merged."""
from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path

from . import db as db_mod
from .config import PROJECT_ROOT
from .interp import apply as apply_mod
from .interp import ops as ops_mod
from .interp import transitions as transitions_mod
from .reporting.model import Report, ThesisBoardRow

DEFAULT_THESES_PATH = PROJECT_ROOT / "theses" / "theses.json"


def register(sub) -> None:
    p = sub.add_parser("interpret")
    p.add_argument("--file", default=None)
    p.add_argument("--type", dest="report_type", default=None)
    p.add_argument("--date", default=None)
    p.add_argument("--stem", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--model", default=None)
    p.add_argument("--no-llm", action="store_true")


def dispatch(args, settings) -> int | None:
    if args.command != "interpret":
        return None
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    try:
        return _cmd_interpret(conn, settings, args)
    finally:
        conn.close()


def _resolve_path(args) -> Path:
    if args.file:
        return Path(args.file)
    if not args.report_type or not args.date:
        raise SystemExit("interpret: --file 이거나 --type과 --date가 필요하다")
    stem = args.stem or args.date
    return PROJECT_ROOT / "reports" / args.report_type / f"{stem}.json"


def _load_thesis_module():
    """Returns the `interp.thesis` module, or `None` if this worktree does
    not have it. Uses `importlib.import_module` (exactly `cli.py`'s own
    `_extension_modules` pattern) rather than `from .interp import thesis`:
    a plain relative `from...import` raises "cannot import name 'thesis'
    from 'market_intel.interp'" whose `exc.name` is `market_intel.interp`
    (the *package*, not `...interp.thesis`) — confirmed by hand against this
    worktree, where `interp/thesis.py` genuinely does not exist yet, so the
    `cli.py`-style `exc.name != name` guard would have misfired and hidden
    a real "the module import is broken" case instead of only "the module is
    absent". `importlib.import_module` raises with `exc.name` equal to the
    exact dotted name requested, which is the check this guard needs."""
    name = "market_intel.interp.thesis"
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        if exc.name != name:
            raise
        return None


def _thesis_summary(conn, report: Report, theses_path: Path):
    """-> (thesis_impact, next_check_suffix, reviews_summary|None, board).
    `reviews_summary` is `None` when thesis integration is unavailable/empty,
    else a list of `{"thesis_id","verdict","changed"}` dicts for the
    `meta["interpretation"]["thesis_reviews"]` mirror (SA-8's fixed shape) —
    ST2's `apply.fill` does not know about this key (it never imports
    `interp.thesis`), so it is added here, after `fill()` returns."""
    thesis_mod = _load_thesis_module()
    if thesis_mod is None:
        return "", "", None, []

    load_error_cls = getattr(thesis_mod, "ThesisLoadError", Exception)
    try:
        theses = thesis_mod.load_file(str(theses_path))
    except FileNotFoundError:
        return "", "", None, []
    except load_error_cls:
        return "", "", None, []
    if not theses:
        return "", "", []

    cutoff = datetime.fromisoformat(report.cutoff_utc)
    reviews = thesis_mod.review(conn, theses, cutoff, report.report_type, report.report_date)

    # ST4 (명세 §5 What #3 / Risk R-1): this path never calls
    # `store.record_reviews` (that is `ops.interpret_report`'s job — see this
    # module's own docstring), so today's review isn't in `thesis_reviews`
    # yet. Without handing it to `transitions.thesis_view` as `pending`, this
    # path's "지속"/"새 관측" counts would read one representative day behind
    # `ops.interpret_report`'s for the identical report.
    states = {}
    for r in reviews:
        thesis_id = r.get("thesis_id")
        if not thesis_id:
            continue
        pending = transitions_mod.pending_row_from_review(r)
        states[thesis_id] = transitions_mod.thesis_view(
            conn, thesis_id, pending=pending, as_of=report.report_date)
    # final-review F4 (CEO 결정 (가)): this path never records today's review
    # (see this module's own docstring), so on the engine's first-ever day the
    # ledger has no row yet to read an introduced-on date from. Passing this
    # call's own `engine_version` (every review row in `reviews` carries the
    # same one) lets `thesis_display_introduced_on` fall back to "now" instead
    # of silently omitting the legend that `ops.interpret_report` would show
    # for the identical report.
    pending_engine_version = reviews[0].get("engine_version") if reviews else None
    introduced_on = ops_mod.thesis_display_introduced_on(
        conn, pending_engine_version, report_date=report.report_date)

    thesis_impact = thesis_mod.render_impact(
        reviews, report.report_date, states=states, introduced_on=introduced_on)
    next_check_suffix = thesis_mod.render_next_check_suffix(reviews, report)
    reviews_summary = [
        {"thesis_id": r.get("thesis_id"), "verdict": r.get("verdict"), "changed": r.get("changed")}
        for r in reviews
    ]
    # `ThesisLoadError`·`ENGINE_VERSION`과 같은 관례 — 이 모듈은 **선택적으로**
    # 실려서 부분 구현일 수 있다(시험이 대역을 끼운다). 없으면 상태판만 비고
    # 산문은 그대로 나간다.
    make_board = getattr(thesis_mod, "board_rows", None)
    board = make_board(reviews, report.report_date, states=states) if make_board else []
    return thesis_impact, next_check_suffix, reviews_summary, board


_VERDICT_ORDER = ("강화", "유지", "약화", "무효", "판정 불가")


def _thesis_line(reviews_summary: list[dict]) -> str:
    """spec SA-11's literal example prints `판정불가=2` (no space) even
    though the verdict enum value itself is `판정 불가` (with a space, per
    SA-2's CHECK constraint) — this output is `key=value` tokens meant to be
    whitespace-split (B13 convention), so a space inside a bare key would
    silently break that parsing. Strip the space for display only; the
    verdict string stored anywhere else stays exactly as SA-2 fixed it."""
    counts = {v: 0 for v in _VERDICT_ORDER}
    changed = 0
    for r in reviews_summary:
        if r["verdict"] in counts:
            counts[r["verdict"]] += 1
        if r.get("changed"):
            changed += 1
    parts = " ".join(f"{v.replace(' ', '')}={counts[v]}" for v in _VERDICT_ORDER)
    return f"thesis: {parts} 변화={changed}"


def _cmd_interpret(conn, settings, args) -> int:
    path = _resolve_path(args)
    report = Report.from_json(path.read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat(report.cutoff_utc)

    thesis_impact, next_check_suffix, reviews_summary, board = _thesis_summary(
        conn, report, DEFAULT_THESES_PATH)

    # final-review F6: the "규칙 판정 · thesis/X" byline needs the judgment
    # engine's own version, not the LLM engine's — `_load_thesis_module()`
    # is the same lookup `_thesis_summary` already did internally.
    thesis_mod = _load_thesis_module()
    thesis_engine_version = getattr(thesis_mod, "ENGINE_VERSION", "") if thesis_mod else ""

    report, result = apply_mod.fill(
        report, conn, cutoff=cutoff,
        thesis_impact=thesis_impact, thesis_engine_version=thesis_engine_version,
        next_check_suffix=next_check_suffix,
        model=args.model, use_llm=not args.no_llm,
    )

    if reviews_summary is not None:
        report.meta["interpretation"]["thesis_reviews"] = reviews_summary
    # 파이프라인(`ops.interpret_report`)과 같은 표를 만든다 — 한쪽만 채우면
    # 손으로 돌린 해석의 리포트에서만 상태판이 빈다.
    report.thesis_board = [ThesisBoardRow(**r) for r in board]

    out_path = Path(args.out) if args.out else path
    if not args.dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.to_json(), encoding="utf-8")
        # ST3 통합: 손으로 돌린 해석도 `interpretations`(+실패 시 `data_gaps`)에
        # 남긴다. `apply.fill`은 DB에 쓰지 않는다고 자기 계약에 못박고 있고
        # (호출자 책임), 이 기록이 없으면 `docs/status.html`의 `마지막 AI 해석`이
        # 파이프라인 실행분만 보여 실제보다 낡은 상태를 말하게 된다.
        ops_mod.record_outcome(conn, report, result)

    print(
        f"interpret_status={result['status']} model={result['model']} "
        f"prompt={result['prompt_version']} attempts={result['attempts']} "
        f"elapsed_ms={result['elapsed_ms']}"
    )
    f = result["fields"]
    print(
        f"fields: reading={f['reading']} counter_reading={f['counter_reading']} "
        f"thesis_impact={f['thesis_impact']} next_check={f['next_check']}"
    )
    if reviews_summary is not None:
        print(_thesis_line(reviews_summary))
    print(f"json={out_path}")
    return 0
