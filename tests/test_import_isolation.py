"""시험 디렉터리 사이의 **이름 충돌**을 구조적으로 막는다 (검수서 F12의 근본 원인).

## 무엇이 문제였나 (2026-08-21 실측)

`tests/` 아래에 `__init__.py`가 없으면 pytest는 각 시험 파일의 **디렉터리**를
`sys.path`에 얹는다. 그러면 `tests/interp/conftest.py`와
`tests/reporting/conftest.py`가 둘 다 `conftest`라는 이름 하나를 놓고 다투고,
**먼저 읽힌 쪽이 이긴다.** 진 쪽 디렉터리의 `from conftest import ...`는 조용히
형제 폴더의 함수를 가져온다.

```
pytest tests/reporting/ tests/interp/   -> 25 failed
pytest tests/interp/ tests/reporting/   -> 통과
pytest                                  -> 통과 (알파벳 순서가 우연히 맞았다)
```

**위험한 쪽은 빨간불이 아니라 그 반대다.** 순서가 달라지면 엉뚱한 픽스처로
**헛통과**한다 — 시험이 겨냥한 가드에 닿지 못한 채 초록불이 뜨는 것과 같은
종류의 사고이고, 전체 실행만 보고 있으면 영원히 안 보인다.

집(이 저장소)의 기존 처방은 *"`from conftest import` 금지 — 헬퍼는 이 파일 안에"*
였다(`tests/backfill`·`tests/providers`가 그렇게 쓰여 있다). 그 처방은 증상을
막지만 헬퍼를 파일마다 복제해야 하고, 지키는지 아무도 검사하지 않아 27개 파일이
관례를 어긴 채로 남아 있었다. 그래서 원인을 직접 잡는다 — **패키지로 만든다.**

이제 하면 안 되는 것은 이 모양이고,

from conftest import macro_fc

이렇게 써야 한다:

from tests.interp.conftest import macro_fc

**위 두 줄은 일부러 열 0에서 시작한다.** `test_no_bare_sibling_imports`가
독스트링을 걷어내지 않으면 이 설명 자체가 위반으로 잡힌다 — 그래서
`_without_strings`가 놀지 않고 실제로 일한다는 것이 이 파일로 증명된다.
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# 시험 모듈이 아니라 데이터가 사는 곳. 패키지일 이유가 없다.
NOT_A_TEST_PACKAGE = {"fixtures", "__pycache__"}


def _test_dirs() -> list[Path]:
    return [TESTS] + sorted(
        d for d in TESTS.iterdir()
        if d.is_dir() and d.name not in NOT_A_TEST_PACKAGE
        and any(d.glob("test_*.py"))
    )


def test_every_test_directory_is_a_package():
    """`__init__.py` 하나가 빠지는 순간 그 폴더가 다시 `sys.path`에 얹히고,
    동명 모듈 충돌이 조용히 돌아온다."""
    missing = [str(d.relative_to(TESTS.parent)) for d in _test_dirs()
               if not (d / "__init__.py").exists()]
    assert not missing, f"__init__.py 없음: {missing} — 이름 충돌이 돌아온다"


def test_no_bare_sibling_imports():
    """`from conftest import X` / `from test_foo import Y`는 **어느 폴더의**
    conftest·test_foo인지 말하지 않는다. 그 모호함이 곧 위 사고다.
    `from tests.interp.conftest import X`처럼 폴더까지 적으면 모호할 수 없다."""
    bare = re.compile(r"(?m)^\s*(?:from|import)\s+(conftest|test_[a-z0-9_]+)\b")
    offenders = []
    for p in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        code = _without_strings(p.read_text(encoding="utf-8"))
        for m in bare.finditer(code):
            offenders.append(f"{p.relative_to(TESTS.parent)}: {m.group(0).strip()}")
    assert not offenders, "폴더를 밝히지 않은 import: " + " · ".join(offenders)


def _without_strings(code: str) -> str:
    """독스트링·문자열 안의 예시 문구를 위반으로 세지 않는다. 이 파일 자체가
    `from conftest import`를 **설명하기 위해** 여러 번 적고 있다 — 하지 말라는
    약속이 그 자체로 위반으로 읽히면 안 된다
    (`test_transitions_engine.py`가 같은 이유로 쓰는 수법)."""
    code = re.sub(r'"""(?:.|\n)*?"""', '""', code)
    code = re.sub(r"'''(?:.|\n)*?'''", "''", code)
    return re.sub(r"(?m)#.*$", "", code)
