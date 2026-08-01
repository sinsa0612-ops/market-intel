from market_intel.workflows import WORKFLOWS


def test_workflow_composition_is_fixed():
    # sec_edgar_13f added alongside sec_edgar (repair.md finding #4: 13F-HR
    # detection for the tracked managers was in scope but unimplemented).
    assert WORKFLOWS["morning"] == ["yfinance", "sec_edgar", "sec_edgar_13f", "fred"]
    assert WORKFLOWS["close"] == ["yfinance", "pykrx", "ecos", "dart"]
    assert WORKFLOWS["all"] == ["yfinance", "pykrx", "sec_edgar", "sec_edgar_13f", "fred", "ecos", "dart"]
    assert set(WORKFLOWS["all"]) == set(WORKFLOWS["morning"]) | set(WORKFLOWS["close"])
