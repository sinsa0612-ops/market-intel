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
        # krx_breadth 백필이 레지스트리에 들어간 뒤로, 이 값을 비우지 않으면
        # `.env`의 실제 키를 물려받아 `--source all` 계열 테스트가 실제 KRX
        # API에 최대 980회를 때리며 몇 분씩 멈춘다(실측 2026-08-04). fred/ecos/
        # dart와 같은 규율 — 키가 필요한 테스트는 자기 fixture에서 명시적으로 채운다.
        krx_api_key="",
        sec_user_agent="test-agent contact@example.com",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)
