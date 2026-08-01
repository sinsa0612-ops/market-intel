"""CLI acceptance test for `schedule list` / `schedule changes` (spec B13
output format) and the CLI_EXTENSIONS hook (spec B1) that wires them into
`cli.py` without `cli.py` naming this module directly."""
from __future__ import annotations

from datetime import datetime, timezone

from market_intel import cli
from market_intel import db as db_mod
from market_intel.models import FactCandidate, RawItem


def _use_settings_env(monkeypatch, settings) -> None:
    """`cli.main()` builds its own `Settings` via `load_settings()` (reading
    MI_* env vars), so the `settings` fixture's tmp_path locations only take
    effect through env vars — mirrors test_cli_subprocess.py's approach,
    in-process instead of subprocess."""
    monkeypatch.setenv("MI_DB_PATH", settings.db_path)
    monkeypatch.setenv("MI_RAW_DIR", settings.raw_dir)
    monkeypatch.setenv("MI_LOG_DIR", settings.log_dir)


def test_schedule_subcommands_are_registered_via_the_extension_hook():
    parser = cli.build_parser()
    args = parser.parse_args(["schedule", "list", "--days", "30"])
    assert args.command == "schedule"
    assert args.schedule_command == "list"
    assert args.days == 30

    args2 = parser.parse_args(["schedule", "changes"])
    assert args2.schedule_command == "changes"
    assert args2.days == 7  # default


def test_schedule_list_format_on_empty_db(settings, capsys, monkeypatch):
    """An empty DB still lists computed 13F deadlines (spec B4 — they are
    not fact-dependent), but must contain zero DB-sourced calendar rows."""
    _use_settings_env(monkeypatch, settings)
    rc = cli.main(["schedule", "list", "--days", "60"])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    assert lines[0].startswith("upcoming_total=") and "window_days=60" in lines[0]
    assert all("13F 제출 마감" in line for line in lines[1:]), lines


def _seed(conn, raw_dir):
    raw = RawItem(external_id="fomc", source_published_at="2026-08-01", safe_source_url="https://example.test", payload="{}")
    snap_id = db_mod.insert_raw_snapshot(conn, raw_dir, "policy_calendar", raw)
    fc = FactCandidate(
        raw_ref="fomc", subject="fomc", category="calendar", metric="scheduled_date",
        event_at="2026-01-01T00:00:00+00:00", market="US", country="US",
        value_text="2026-09-16,2026-10-28,2026-12-09",
        comparison_basis="연도 일정표(그 해에 공표된 예정일 전부)",
        data_status="source_verified", extra={"dates": ["2026-09-16", "2026-10-28", "2026-12-09"]},
    )
    db_mod.upsert_fact(conn, "policy_calendar:fomc:scheduled_date:20260101", snap_id, "2026-08-01T00:00:00+00:00", fc)
    conn.commit()


def test_schedule_list_shows_seeded_fomc_row(settings, capsys, monkeypatch):
    _use_settings_env(monkeypatch, settings)
    db_mod.init_db(settings.db_path)
    conn = db_mod.connect(settings.db_path)
    _seed(conn, settings.raw_dir)
    conn.close()

    # Freeze "now" so the FOMC 09-16 slot is inside the default 60-day window
    # regardless of when this test actually runs.
    import market_intel.cli_schedule as cli_schedule_mod

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 1, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(cli_schedule_mod, "datetime", _FrozenDatetime)

    rc = cli.main(["schedule", "list", "--days", "60"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "upcoming_total=" in out
    assert "FOMC" in out
    assert "A US FOMC" in out
    assert "status=원자료 확인" in out


def test_schedule_changes_format(settings, capsys, monkeypatch):
    _use_settings_env(monkeypatch, settings)
    db_mod.init_db(settings.db_path)
    rc = cli.main(["schedule", "changes", "--days", "7"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "changes_total=0 since=" in out


def test_calendar_collect_registers_the_deliberate_gaps(settings, capsys, monkeypatch):
    """spec ST1 boundary + §제외한 것: the Korean macro release calendar (no
    free keyless source could be confirmed) and cancellation classification
    are NOT implemented, so both are registered as gaps instead of being
    filled with a guess. Registered on the write path (`collect`), not from
    the read-only `schedule` commands."""
    from market_intel import schedule as schedule_mod

    _use_settings_env(monkeypatch, settings)
    monkeypatch.setattr("market_intel.providers.PROVIDERS", {}, raising=False)

    assert cli.main(["init"]) == 0
    rc = cli.main(["collect", "--workflow", "calendar"])
    capsys.readouterr()
    assert rc == 0

    conn = db_mod.connect(settings.db_path)
    gaps = {r["gap_id"]: r for r in conn.execute("SELECT * FROM data_gaps")}
    conn.close()

    kr = gaps.get(schedule_mod.KR_MACRO_GAP_ID)
    assert kr is not None, "kr_macro_calendar:release_date must be registered"
    assert kr["status"] == "보류"
    assert kr["subject"] == "kr_macro_calendar" and kr["metric"] == "release_date"
    # the reason has to carry the measured evidence, not just "not available"
    assert "kosis.kr" in kr["reason"] and "sdate" in kr["reason"]

    cancel = gaps.get(schedule_mod.CANCELLATION_GAP_ID)
    assert cancel is not None, "calendar:cancellation_detection must be registered"
    assert "분류 부족" in cancel["reason"]
