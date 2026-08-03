"""Settings loaded from .env (python-dotenv). Never print/repr real secret
values — Prime Rule 8.

``find_dotenv()`` walks up from *this file's* location, so it locates the
project-root ``.env`` regardless of the caller's current working directory
(e.g. when this package is nested under an ``_org/.../exec-b/`` sandbox for
a paired execution, it still finds the real project's ``.env`` two levels
up — read-only, never written by this module).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(filename=".env", usecwd=False))

# <project root>/src/market_intel/config.py -> parents[2] is the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _path(env_name: str, default_rel: str) -> str:
    """Storage locations default to the *project root*, never the process cwd.

    cron/launchd start the command from an arbitrary directory; a cwd-relative
    default would silently create a second, empty DB there and report
    "collected successfully, facts_total=0" forever. An explicit MI_* override
    is honoured verbatim (that is a deliberate operator choice).
    """
    override = os.environ.get(env_name, "")
    if override:
        return override
    return str(PROJECT_ROOT / default_rel)


def _mask(value: str) -> str:
    return "***" if value else ""


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _path("MI_DB_PATH", "var/market_intel.db"))
    raw_dir: str = field(default_factory=lambda: _path("MI_RAW_DIR", "var/raw"))
    log_dir: str = field(default_factory=lambda: _path("MI_LOG_DIR", "var/logs"))
    fred_api_key: str = field(default_factory=lambda: os.environ.get("MI_FRED_API_KEY", ""))
    ecos_api_key: str = field(default_factory=lambda: os.environ.get("MI_ECOS_API_KEY", ""))
    dart_api_key: str = field(default_factory=lambda: os.environ.get("MI_DART_API_KEY", ""))
    sec_user_agent: str = field(default_factory=lambda: os.environ.get("MI_SEC_USER_AGENT", ""))
    # KRX 오픈API (openapi.krx.co.kr) — 유가증권·코스닥 일별매매정보(전종목 시세).
    # 수급은 여기 없다(2026-08-03 실측: 엔드포인트 자체가 없음) — 수급은 KIS가 준다.
    # 지금은 읽는 provider가 없고, 전종목 시세 수집기가 붙을 때 쓴다.
    krx_api_key: str = field(default_factory=lambda: os.environ.get("MI_KRX_API_KEY", ""))
    # KRX 회원 계정. **이 값을 읽는 코드는 없다** — 유일한 사용처였던 pykrx를
    # 2026-08-04에 걷어냈다(7회 실행 전부 NO_DATA, 남긴 fact 0건). 그래도 아래
    # `secret_values()`에 남겨 두는 이유는 `.env`에 값이 남아 있는 한 로그·오류
    # 메시지에 섞여 나갈 수 있기 때문이다 — 가리는 데는 비용이 없다.
    # (`.env`에서 두 줄을 지우면 이 필드도 같이 지워도 된다.)
    krx_id: str = field(default_factory=lambda: os.environ.get("KRX_ID", ""))
    krx_pw: str = field(default_factory=lambda: os.environ.get("KRX_PW", ""))
    # 한국투자증권 KIS — 투자자별 수급을 받는 유일한 키 기반 경로.
    # **주의: 같은 자격증명에 주문 API가 붙어 있다.** 이 프로젝트는 매매를 하지
    # 않으므로 `providers/kis_flows.py`가 조회(quotations) 경로만 부르도록 막고
    # 테스트로 고정한다(`test_kis_flows.py`).
    kis_app_key: str = field(default_factory=lambda: os.environ.get("MI_KIS_APP_KEY", ""))
    kis_app_secret: str = field(default_factory=lambda: os.environ.get("MI_KIS_APP_SECRET", ""))

    def __repr__(self) -> str:  # never leak secret values via repr/logging
        return (
            f"Settings(db_path={self.db_path!r}, raw_dir={self.raw_dir!r}, "
            f"log_dir={self.log_dir!r}, fred_api_key={_mask(self.fred_api_key)!r}, "
            f"ecos_api_key={_mask(self.ecos_api_key)!r}, dart_api_key={_mask(self.dart_api_key)!r}, "
            f"sec_user_agent={_mask(self.sec_user_agent)!r}, "
            f"krx_api_key={_mask(self.krx_api_key)!r}, krx_id={_mask(self.krx_id)!r}, "
            f"krx_pw={_mask(self.krx_pw)!r}, kis_app_key={_mask(self.kis_app_key)!r}, "
            f"kis_app_secret={_mask(self.kis_app_secret)!r})"
        )

    def secret_values(self) -> list[str]:
        """All non-empty secret-bearing values, for masking/redaction."""
        return [
            v
            for v in (self.fred_api_key, self.ecos_api_key, self.dart_api_key,
                      self.sec_user_agent, self.krx_api_key, self.krx_id, self.krx_pw,
                      self.kis_app_key, self.kis_app_secret)
            if v
        ]


def load_settings() -> Settings:
    return Settings()
