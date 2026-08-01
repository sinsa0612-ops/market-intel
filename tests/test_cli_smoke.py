"""ST1 acceptance test 6: CLI smoke — init then db stats in the fixed
output format (spec A8), parseable as evidence."""
from market_intel import cli
from market_intel.db import init_db


def test_init_and_db_stats_format(settings, capsys):
    init_db(settings.db_path)
    rc = cli.cmd_db_stats(settings)
    out = capsys.readouterr().out
    assert rc == 0
    assert "facts_total=0" in out
    assert "facts_by_provider:" in out
    assert "facts_by_status:" in out
    assert "core16_price_coverage=0/16" in out
    assert "last_run: none" in out


def test_cmd_init_is_idempotent(settings, capsys):
    assert cli.cmd_init(settings) == 0
    assert cli.cmd_init(settings) == 0  # already-initialized DB is harmless
    out = capsys.readouterr().out
    assert "initialized db at" in out


def test_build_parser_rejects_unknown_workflow():
    parser = cli.build_parser()
    import pytest

    with pytest.raises(SystemExit):
        parser.parse_args(["collect", "--workflow", "nonsense"])
