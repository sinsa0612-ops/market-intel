import pytest

from market_intel.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        db_path=str(tmp_path / "market_intel.db"),
        raw_dir=str(tmp_path / "raw"),
        log_dir=str(tmp_path / "logs"),
        fred_api_key="",
        ecos_api_key="",
        dart_api_key="",
        sec_user_agent="test-agent contact@example.com",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)
