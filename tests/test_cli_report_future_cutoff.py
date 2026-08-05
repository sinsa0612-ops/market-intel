"""차단선이 아직 오지 않은 리포트는 만들지 않는다.

왜 이 파일이 있는가 (CEO 지적 2026-08-05): 차단선이 16:15인 **장마감 리포트가
12:34에 만들어져 세 번 발행됐다.** 장이 닫히기도 전이었다.

리포트 첫 줄은 "이 시각까지 알려진 사실만 싣습니다"라고 약속한다. 그 시각이
미래면 지킬 수 없는 약속이다. 데이터가 틀린 것보다 나쁘다 — 숫자는 맞는데
문서가 자기 자신에 대해 거짓을 말한다.

지난 날짜를 뒤늦게 만드는 캐치업은 막지 않는다(차단선이 과거다).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], db_path: Path, raw_dir: Path):
    """실제 CLI를 서브프로세스로 부른다 — 가드는 CLI 계층에 있고, 사람이
    실수하는 자리도 거기다."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "MI_DB_PATH": str(db_path), "MI_RAW_DIR": str(raw_dir),
        "MI_LOG_DIR": str(raw_dir.parent / "logs"),
        "HOME": str(raw_dir.parent),
    }
    return subprocess.run(
        [sys.executable, "-m", "market_intel.cli", *args],
        capture_output=True, text=True, cwd=PROJECT_ROOT, env=env,
    )


def _future_date() -> str:
    """차단선이 확실히 미래인 날짜(내일). 오늘로 잡으면 이 테스트가 16:15
    이후에는 통과해 버려, 하루 중 언제 돌리느냐에 따라 결과가 달라진다."""
    return (datetime.now(timezone.utc).astimezone(KST).date() + timedelta(days=1)).isoformat()


def _past_date() -> str:
    """**실제 발행본과 겹치지 않는** 아주 오래된 날짜.

    `market-intel report`는 `MI_DB_PATH`와 무관하게 저장소의 `reports/`에 쓴다
    (핸드오프의 알려진 함정). 최근 날짜를 쓰면 이 테스트가 진짜 발행본을
    덮어쓰고, 정리한다며 지운다 — 실제로 2026-08-05에 `morning/2026-08-02.json`을
    한 번 날렸다. 발행본이 존재할 수 없는 날짜를 쓴다."""
    return "1999-01-04"


def test_report_refuses_a_cutoff_that_has_not_arrived(tmp_path):
    db, raw = tmp_path / "t.db", tmp_path / "raw"
    proc = _run(["init"], db, raw)
    assert proc.returncode == 0, proc.stderr

    proc = _run(["report", "--type", "close_delta", "--date", _future_date()], db, raw)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "차단선이 아직 오지 않았다" in proc.stderr, proc.stderr

    # 그리고 **파일을 남기지 않는다** — 거부해 놓고 써 두면 사이트가 주워 간다.
    written = PROJECT_ROOT / "reports" / "close_delta" / f"{_future_date()}.json"
    assert not written.exists(), f"거부했는데 파일이 생겼다: {written}"


def test_report_still_builds_for_a_past_cutoff(tmp_path):
    """캐치업(지난 날짜를 뒤늦게 생성)은 막히면 안 된다."""
    db, raw = tmp_path / "t.db", tmp_path / "raw"
    assert _run(["init"], db, raw).returncode == 0

    proc = _run(["report", "--type", "close_delta", "--date", _past_date()], db, raw)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "report_type=close_delta" in proc.stdout

    out = PROJECT_ROOT / "reports" / "close_delta" / f"{_past_date()}.json"
    try:
        assert out.exists()
        assert json.loads(out.read_text(encoding="utf-8"))["report_type"] == "close_delta"
    finally:
        out.unlink(missing_ok=True)  # 저장소의 발행본을 어지럽히지 않는다


def test_explicit_future_cutoff_is_refused_too(tmp_path):
    """`--cutoff`로 직접 미래를 넣어도 막힌다 — 계산된 차단선만 보면
    이 우회로가 열려 있다."""
    db, raw = tmp_path / "t.db", tmp_path / "raw"
    assert _run(["init"], db, raw).returncode == 0

    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    proc = _run(["report", "--type", "morning", "--date", _past_date(), "--cutoff", future], db, raw)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "차단선이 아직 오지 않았다" in proc.stderr
