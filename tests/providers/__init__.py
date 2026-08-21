"""시험 디렉터리를 패키지로 만든다 — 이름 충돌 방지 (검수서 F12의 근본 원인).

`__init__.py`가 없으면 pytest가 각 시험 파일의 **디렉터리**를 `sys.path`에
얹는다. 그러면 `tests/interp/conftest.py`와 `tests/reporting/conftest.py`가
둘 다 `conftest`라는 이름 하나를 놓고 다투고, **먼저 읽힌 쪽이 이긴다** —
진 쪽 디렉터리의 `from conftest import ...`는 조용히 형제 폴더의 함수를
가져온다.

실측(2026-08-21): `pytest tests/reporting/ tests/interp/`는 25건이 빨간불,
순서를 뒤집으면 통과, 전체 실행은 알파벳 순서가 우연히 맞아 통과했다.
**위험한 쪽은 빨간불이 아니라 그 반대다** — 순서가 달라지면 엉뚱한 픽스처로
헛통과한다.

패키지가 되면 각 conftest가 `tests.interp.conftest`처럼 제 이름을 갖는다.
같은 이유로 동명 시험 파일(`tests/a/test_x.py`와 `tests/b/test_x.py`)도
안전해진다. `tests/test_no_conftest_name_collision.py`가 이 파일들의 존재를
지킨다.
"""
