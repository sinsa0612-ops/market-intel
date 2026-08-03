"""백필 소스 레지스트리 (spec 백필 §4 S9).

네 소스 이름을 **미리 전부 선언**하고 `importlib`로 지연 로드한다. 모듈이 아직
없으면 `status="NO_DATA", reason_code="미구현"`으로 보고한다(`engine.py:76`의
기존 관례와 동일). 덕분에 뒤따르는 서브태스크(ST2·ST3)는 자기 소스 모듈만
추가하면 되고 이 파일을 건드리지 않는다.

소스 모듈이 지켜야 할 계약 — 모듈 하나가 소스 여러 개를 맡을 수 있으므로
소스 이름을 인자로 받는다::

    def run(conn, settings, source: str, *, since, until, subjects, dry_run) -> BackfillResult

`--dry-run`이면 DB에 한 줄도 쓰지 않고 후보 수만 센다(spec S8).
"""
from __future__ import annotations

import importlib
import time
from dataclasses import asdict, dataclass

# `--source all`의 실행 순서 그대로 (spec S8).
SOURCE_ORDER = ("prices", "macro_us", "macro_kr", "financials")

SOURCES: dict[str, str] = {
    "prices": "market_intel.backfill.prices",
    "macro_us": "market_intel.backfill.macro",
    "macro_kr": "market_intel.backfill.macro",
    "financials": "market_intel.backfill.financials",
}


@dataclass
class BackfillResult:
    """소스 한 개의 실행 결과. `line()`이 S8의 파싱 가능한 한 줄이다."""

    source: str
    status: str = "OK"  # OK | PARTIAL | NO_DATA | ERROR
    reason_code: str | None = None
    fetched: int = 0
    appended: int = 0
    skipped_existing: int = 0
    elapsed_s: float = 0.0
    detail: str = ""

    def line(self) -> str:
        parts = [f"source={self.source}", f"status={self.status}"]
        if self.reason_code:
            parts.append(f"reason={self.reason_code}")
        parts += [
            f"fetched={self.fetched}",
            f"appended={self.appended}",
            f"skipped_existing={self.skipped_existing}",
            f"elapsed={self.elapsed_s:.1f}s",
        ]
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)


def load_source(source: str):
    """-> 소스 모듈, 아직 구현되지 않았으면 None.

    지연 import인 이유: import 시 stdout에 배너를 찍는 서드파티가 있으면 파싱
    가능한 CLI 출력이 오염된다(`cli.py`의 선례)."""
    if source not in SOURCES:
        raise ValueError(f"unknown backfill source: {source!r} (choices: {list(SOURCES)})")
    module_name = SOURCES[source]
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        # "이 소스는 아직 없다"만 넘어간다. 이미 있는 모듈 *안의* import 오류를
        # 삼키면 오타 하나가 소스를 조용히 사라지게 만든다(`cli.py`와 같은 규율).
        if exc.name != module_name:
            raise
        return None
    return module if callable(getattr(module, "run", None)) else None


def run_source(conn, settings, source: str, *, since, until, subjects=None,
               dry_run: bool = False) -> BackfillResult:
    started = time.monotonic()
    module = load_source(source)
    if module is None:
        return BackfillResult(source=source, status="NO_DATA", reason_code="미구현",
                              elapsed_s=time.monotonic() - started)
    try:
        result = module.run(conn, settings, source, since=since, until=until,
                            subjects=subjects, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - 한 소스의 실패가 나머지를 죽이지 않는다
        # `reason_code`를 추측해서 채우지 않는다(Prime Rule 7). 실제 예외 클래스가
        # detail로 간다. 50-revision 가드(S6)의 RuntimeError도 이 경로로 나온다.
        return BackfillResult(source=source, status="ERROR",
                              detail=f"{exc.__class__.__name__}: {exc}"[:300],
                              elapsed_s=time.monotonic() - started)
    result.source = source
    result.elapsed_s = time.monotonic() - started
    return result
