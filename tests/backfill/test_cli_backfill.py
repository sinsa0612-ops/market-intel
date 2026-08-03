"""ST1 — `market-intel backfill` 골격 (spec 백필 §4 S8/S9).

소스 모듈이 아직 없는 상태에서도 **무인 실행 가능**해야 한다: 프롬프트 없음,
네 소스 모두 `status=NO_DATA reason=미구현`, 종료코드 0.

`from conftest import` 금지 — `settings` 픽스처는 루트 conftest가 준다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from market_intel import cli as cli_mod
from market_intel import cli_backfill
from market_intel.backfill import SOURCE_ORDER, BackfillResult, load_source, run_source


def _run(args_list, settings, capsys):
    parser = cli_mod.build_parser()
    args = parser.parse_args(args_list)
    code = cli_backfill.dispatch(args, settings)
    return code, capsys.readouterr().out


def test_backfill_is_a_registered_subcommand():
    assert "market_intel.cli_backfill" in cli_mod.CLI_EXTENSIONS
    args = cli_mod.build_parser().parse_args(["backfill", "--source", "all", "--dry-run"])
    assert args.command == "backfill" and args.dry_run is True


# ST1이 뼈대만 놓았을 때는 네 소스가 전부 "미구현"이었다. ST2가 셋을 채웠으므로
# 남은 것은 financials 하나다 — 그 사실 자체를 고정한다. 전부 미구현이라고
# 계속 주장하면 ST3가 붙는 순간 또 빨개지고, 그때 무엇이 바뀐 건지 알 수 없다.
IMPLEMENTED = ("prices", "macro_us", "macro_kr")
NOT_YET = ("financials",)


def test_only_financials_is_still_unimplemented(settings, capsys):
    code, out = _run(["backfill", "--source", "all", "--dry-run"], settings, capsys)
    lines = {ln.split()[0].removeprefix("source="): ln
             for ln in out.splitlines() if ln.startswith("source=")}
    assert code == 0
    assert set(lines) == set(SOURCE_ORDER)
    for source in NOT_YET:
        assert "status=NO_DATA reason=미구현" in lines[source], lines[source]
    for source in IMPLEMENTED:
        assert "reason=미구현" not in lines[source], lines[source]
    # --dry-run이므로 무엇을 하든 append는 0이어야 한다.
    for line in lines.values():
        assert "appended=0" in line, line


def test_dry_run_writes_no_facts(settings, capsys):
    from market_intel import db as db_mod

    _run(["backfill", "--source", "all", "--dry-run"], settings, capsys)
    conn = db_mod.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM fact_revisions").fetchone()["c"] == 0
    finally:
        conn.close()


def test_json_output_is_parseable(settings, capsys):
    code, out = _run(["backfill", "--source", "financials", "--json"], settings, capsys)
    payload = json.loads(out)
    assert code == 0
    assert [p["source"] for p in payload] == ["financials"]
    assert payload[0]["status"] == "NO_DATA" and payload[0]["reason_code"] == "미구현"


def test_bad_date_argument_exits_two(settings, capsys):
    code, _ = _run(["backfill", "--source", "prices", "--since", "2024-13-99"], settings, capsys)
    assert code == 2
    code, _ = _run(
        ["backfill", "--source", "prices", "--since", "2026-01-02", "--until", "2026-01-01"],
        settings, capsys,
    )
    assert code == 2


def test_unknown_source_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        cli_mod.build_parser().parse_args(["backfill", "--source", "nope"])


def test_default_since_per_source():
    today = date(2026, 8, 2)
    assert cli_backfill._default_since("prices", today) == date(2024, 8, 2)
    assert cli_backfill._default_since("macro_us", today) == date(2023, 8, 2)
    assert cli_backfill._default_since("macro_kr", today) == date(2023, 8, 2)
    assert cli_backfill._default_since("financials", today) == today - timedelta(days=730)
    # 2월 29일에도 죽지 않는다.
    assert cli_backfill._default_since("prices", date(2028, 2, 29)) == date(2026, 2, 28)


def test_registry_declares_all_four_sources_up_front():
    """ST2·ST3가 이 레지스트리 파일을 건드리지 않아도 되도록 미리 선언한다.

    그 설계가 실제로 통했는지가 여기서 보인다: ST2는 소스 모듈 두 개만 새로
    쓰고 `SOURCES`는 손대지 않았는데 `load_source`가 바로 찾아낸다."""
    from market_intel.backfill import SOURCES

    assert set(SOURCES) == set(SOURCE_ORDER)
    assert all(load_source(s) is not None for s in IMPLEMENTED)
    assert all(load_source(s) is None for s in NOT_YET)


def test_run_source_reports_error_instead_of_raising(settings, monkeypatch):
    """소스가 던진 예외(50-revision 가드 포함)는 status=ERROR로 보고된다."""
    import market_intel.backfill as backfill_mod

    class Boom:
        @staticmethod
        def run(conn, settings, source, **kwargs):
            raise RuntimeError("backfill 폭주 방지: ...")

    monkeypatch.setattr(backfill_mod, "load_source", lambda source: Boom)
    result = run_source(None, settings, "prices", since=date(2024, 8, 2), until=date(2026, 8, 2))
    assert isinstance(result, BackfillResult)
    assert result.status == "ERROR" and "폭주 방지" in result.detail


def test_partial_source_makes_the_command_exit_one(settings, capsys, monkeypatch):
    import market_intel.cli_backfill as cli_backfill_mod

    def fake_run_source(conn, settings, source, **kwargs):
        return BackfillResult(source=source, status="PARTIAL", detail="ECOS 빈티지 미제공")

    monkeypatch.setattr(cli_backfill_mod, "run_source", fake_run_source)
    code, out = _run(["backfill", "--source", "macro_kr"], settings, capsys)
    assert code == 1
    assert "status=PARTIAL" in out and "detail=ECOS 빈티지 미제공" in out
