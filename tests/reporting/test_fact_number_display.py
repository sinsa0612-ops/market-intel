"""F-번호는 화면에서만 사라지고, 검증기와 원장에는 그대로 남는다.

왜 이 파일이 있는가 (CEO 지적 2026-08-12): 발행문이 `F73의 KOSPI와 F74의
KOSDAQ 상승은…` 처럼 쓰여 있어 읽기 힘들다는 지적이 있었다. 그런데 F-번호는
표시용 장식이 아니라 **환각 검증기의 접지 장치**다 — 규칙 8은 "F45 뒤의 주체가
정말 F45의 주체인가"를, 규칙 9는 "인용에 붙은 숫자가 그 인용의 숫자인가"를
F-번호로 대조한다. 그 둘이 잡아낸 실제 발행 사고가 있다(F45=KOSPI에 삼성전자
등락률 26.81%를 붙여 내보낸 2026-08-03 주간 브리핑).

그래서 지우는 자리는 **화면뿐**이다. 이 파일이 그 경계를 못박는다:
  1. 화면(두 렌더러)에는 F-번호가 없다.
  2. 저장된 리포트 원문에는 남는다(감사 추적).
  3. 검증기는 여전히 F-번호를 보고 귀속 오류를 잡는다.
"""
from __future__ import annotations

import re

import pytest

from market_intel.interp import validate as validate_mod
from market_intel.reporting.render_md import strip_fact_numbers

_FNUM = re.compile(r"(?<![A-Za-z0-9])F\d+")


# 발행본 40개 필드에서 실제로 관측된 인용 모양들.
@pytest.mark.parametrize("src,want", [
    # 이름 앞에 붙는 형태 — luna가 쓰는 지배적 모양
    ("F73의 KOSPI와 F74의 KOSDAQ 상승은 국내 지수가 반등했음을 보여준다.",
     "KOSPI와 KOSDAQ 상승은 국내 지수가 반등했음을 보여준다."),
    # 여러 인용을 조사로 묶은 형태
    ("F60과 F69에서 외국인이 순매도했다.", "외국인이 순매도했다."),
    # 문장 끝 괄호 묶음
    ("기관과 외국인의 순매도가 진행 중이다(F1, F2, F3).",
     "기관과 외국인의 순매도가 진행 중이다."),
    # 연결어가 붙는 형태 — `에`가 먼저 먹으면 `따르면`이 남는다
    ("F12에 따르면 실업률이 낮아졌다.", "실업률이 낮아졌다."),
    # 범위 표기. 이걸 빼먹으면 `-금리 방향`처럼 찌꺼기가 남는다(시제품에서 실제로 났다)
    ("F20-F21의 금리 방향이 확인된다.", "금리 방향이 확인된다."),
    # `A부터 B까지` — 조사가 번호마다 붙어 묶음 규칙만으로는 안 잡힌다.
    # 빠뜨리면 `부터 까지는`이 남는다(2026-08-12 발행문에서 실제로 났던 결함).
    ("F56부터 F58까지는 개인 순매도가 나타났다.", "개인 순매도가 나타났다."),
    ("F76부터 F78까지 미국 지수가 내렸다.", "미국 지수가 내렸다."),
])
def test_citation_shapes_are_removed_cleanly(src, want):
    assert strip_fact_numbers(src) == want


def test_every_published_interpretation_strips_without_residue():
    """⚠️ 예시 목록이 아니라 **발행본 전수**로 검증한다.

    위의 parametrize는 내가 아는 모양만 담는다. 실제 결함은 둘 다 내가 몰랐던
    모양에서 나왔다 — `F20-F21의`(시제품)와 `F56부터 F58까지는`(2026-08-12
    발행문). 모델이 바뀌면 인용 습관도 바뀌므로, 발행된 글 전부를 훑어
    "지운 뒤 찌꺼기가 없는가"를 자동으로 묻는 이 테스트가 관문이다.

    리포트가 없는 환경(CI 초기 체크아웃)에서는 조용히 건너뛴다.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "reports"
    files = sorted(root.glob("*/*.json")) if root.is_dir() else []
    if not files:
        pytest.skip("발행본이 없다")

    checked = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        interp = data.get("interpretation") or {}
        for field in ("reading", "counter_reading", "next_check"):
            src = (interp.get(field) or "").strip()
            if not _FNUM.search(src):
                continue
            checked += 1
            got = strip_fact_numbers(src)
            where = f"{path.parent.name}/{path.name} [{field}]"
            assert not _FNUM.search(got), f"{where}: F-번호가 남았다 — {got[:120]}"
            # 조사만 남아 붕 뜬 자리. 한국어 조사는 앞말에 붙으므로 **앞뒤가 모두
            # 공백인 조사**는 인용이 사라진 자리다(`F56부터 F58까지는` -> `부터 까지는`).
            #
            # ⚠️ `이`·`그`는 **뺀다** — 지시대명사로 홀로 서는 것이 정상이기 때문이다.
            # 처음엔 넣었다가 2026-08-13 발행문의 `점도 이 해석을 보탠다`를 찌꺼기로
            # 오인해 빨간불이 났다. 제거는 정상이었고 **검출기가 틀렸다.** 도구가 없는
            # 결함을 만들어 내면 있는 결함보다 나쁘다(감사 도구에서 이미 한 번 겪었다).
            # 접속사 `및`도 명사 사이에 홀로 서는 것이 정상이라 넣지 않는다.
            assert not re.search(r"\s(부터|까지|의|은|는|를|을)\s", got), (
                f"{where}: 인용을 지운 자리에 조사가 남았다 — {got[:120]}")
            # 절 첫머리에 조사가 오는 것도 같은 증상이다(여기서도 `이`는 뺀다).
            assert not re.search(r"(^|[.!?]\s+)(부터|까지|와|과|및|의|은|는|를|을)\s", got), (
                f"{where}: 절이 조사로 시작한다 — {got[:120]}")
            # ⚠️ 하이픈 앞에 **공백이나 문장 첫머리**가 오는 것만 본다. 규칙을
            # 지운 것이 아니라 조준을 좁힌 것이다 — `[-~–—]\s*[가-힣]`은 단어
            # 안의 하이픈까지 잡아서 `미국 2년물-정책금리`(이 시스템이 매일 쓰는
            # 지표 이름)와 `코스피-코스닥`을 찌꺼기로 오인했다. 2026-08-20
            # close_delta 발행문이 실제로 그렇게 빨간불을 냈다.
            #
            # **지우면 안 되는 이유는 실측했다.** 지우개의 구분자 목록에서 `-`가
            # 빠지면(주석 위쪽이 "시제품에서 발견"이라 적은 그 결함) `F20-F21의
            # 금리`가 `-금리`로 남는데, **이 줄만 그것을 잡는다** — F-번호·조사·
            # 이중공백 규칙은 전부 통과시킨다.
            assert not re.search(r"(^|\s)[-~–—]\s*[가-힣]", got), (
                f"{where}: 찌꺼기 기호가 남았다 — {got[:120]}")
            assert "  " not in got, f"{where}: 이중 공백"
            # 괄호 안에 **찌꺼기 기호만** 남은 모양. `()`만 보던 옛 단언은
            # `금리(F20-F21)의` -> `금리(-)의`를 통과시켰고, 다른 어떤 규칙도
            # 그것을 잡지 않았다(2026-08-20 실측). 지금까지 뚫려 있던 자리다.
            assert not re.search(r"[(（]\s*[-~–—,·/]*\s*[)）]", got), (
                f"{where}: 인용을 지운 자리에 빈 괄호가 남았다 — {got[:120]}")
    assert checked, "F-번호를 쓴 발행문이 하나도 없다 — 표본이 비었는지 확인할 것"


def test_no_stray_punctuation_is_left_behind():
    """지운 자리에 이중 공백이나 떠 있는 하이픈이 남으면 안 된다."""
    got = strip_fact_numbers("F20-F21의 금리와 F3의 실업률(F28, F29)을 본다.")
    assert not _FNUM.search(got)
    assert "  " not in got
    assert not re.search(r"[-~–—]\s*[가-힣]", got)
    assert "()" not in got


def test_text_without_citations_is_untouched():
    plain = "외국인이 순매도하고 개인이 순매수했다."
    assert strip_fact_numbers(plain) == plain


def test_blank_is_passed_through():
    assert strip_fact_numbers("") == ""


def _report_with(reading: str, counter: str = "", nxt: str = ""):
    from market_intel.reporting import model as model_mod

    return model_mod.Report(
        report_type="morning", report_date="2026-08-12",
        cutoff_utc="2026-08-11T22:15:00+00:00", cutoff_kst="2026-08-12 07:15",
        facts=[model_mod.FactRow(
            label="KOSPI", value="6,358.35", comparison="전일대비 +0.84%",
            source_url="", data_status="source_verified",
            known_at="2026-08-11T22:00:00+00:00", subject="^KS11", metric="price_close")],
        interpretation=model_mod.Interpretation(
            reading=reading, counter_reading=counter, next_check=nxt,
            generated_by="ai:gpt:gpt-5.6-luna · interpretation_v3"),
    )


def test_both_renderers_show_no_fact_numbers():
    """화면 계약: 마크다운·HTML 어느 쪽에도 F-번호가 나오지 않는다.

    두 렌더러는 `render_md._interp()`가 만든 같은 블록을 소비하므로 관문은
    하나지만, **그 사실 자체가 회귀할 수 있어** 양쪽을 다 확인한다.
    """
    from market_intel.reporting import render_html as render_html_mod
    from market_intel.reporting import render_md as render_md_mod

    report = _report_with(
        "F73의 KOSPI가 상승했다(F1, F2).",
        "F60과 F69에서 외국인이 순매도했다.",
        "F12에 따르면 다음 발표를 본다.")

    md = render_md_mod.render_markdown(report)
    html = render_html_mod.render_html(report)

    assert not _FNUM.search(md), "마크다운에 F-번호가 남았다"
    assert not _FNUM.search(html), "HTML에 F-번호가 남았다"
    # 지워도 내용은 남아야 한다 — 문장째로 사라지면 안 된다.
    assert "KOSPI가 상승했다" in md
    assert "외국인이 순매도했다" in md
    assert "외국인이 순매도했다" in html


def test_stored_report_keeps_the_citations():
    """감사 추적: 리포트 JSON은 F-번호를 그대로 갖는다.

    여기서 원문까지 지우면 "이 문장이 무슨 사실을 인용했나"를 되짚을 수 없다.
    """
    original = "F73의 KOSPI가 상승했다(F1, F2)."
    report = _report_with(original)

    assert report.interpretation.reading == original
    assert "F73" in report.to_json()


def test_validator_still_catches_attribution_errors():
    """⚠️ 안전장치 회귀 감시 — 이 파일에서 가장 중요한 테스트.

    화면에서 F-번호를 없앴다고 검증기까지 눈을 감으면, 규칙 9가 잡았던 그 사고
    (F1=KOSPI 인용에 삼성전자 등락률 26.81%를 붙여 발행한 2026-08-03 주간
    브리핑)가 그대로 다시 나간다. **검증은 F-번호가 붙은 원문에 대해 돌고,
    제거는 그 뒤 화면 단계에서만 일어난다**는 순서를 못박는다.

    검증기 입력은 프로덕션과 같은 모양이다(`apply._report_dict` =
    `dataclasses.asdict(report)`).
    """
    report_dict = _accident_report_dict()

    # F1은 KOSPI인데 삼성전자의 26.81%를 붙였다 — 실제로 발행됐던 사고의 모양.
    bad = "F1 에 기록된 전일대비 26.81% 급등이 나타났다."
    assert validate_mod.check(report_dict, bad), "검증기가 귀속 오류를 놓쳤다"

    # 반대로, 제 숫자를 인용한 문장은 통과해야 한다(오탐이면 매일 해석이 반려된다).
    good = "F1 의 KOSPI가 전일대비 17.91% 올랐다."
    assert not validate_mod.check(report_dict, good), "정당한 인용이 막혔다"

    # 그리고 화면 단계에서 F-번호가 사라져도 **문장의 사실 내용은 남는다**.
    assert "26.81%" in strip_fact_numbers(bad)
    assert not _FNUM.search(strip_fact_numbers(bad))


def _accident_report_dict() -> dict:
    """2026-08-03에 실제로 발행된 사고를 그대로 재현하는 리포트.

    `delta_pct`를 채우는 것이 핵심이다 — 규칙 9는 그 행이 **수로 들고 있는**
    값과 대조하지 `comparison` 문자열을 파싱하지 않는다. 비워 두면 대조할
    것이 없어 규칙이 조용히 통과하고, 테스트는 초록인데 아무것도 안 지킨다.
    """
    import dataclasses

    from market_intel.reporting import model as model_mod

    def fact(label, value, comparison, subject, delta_pct):
        return model_mod.FactRow(
            label=label, value=value, comparison=comparison, source_url="",
            data_status="source_verified", known_at="2026-08-03T06:00:00+00:00",
            subject=subject, metric="price_close", delta_pct=delta_pct)

    # `week_start`는 2026-08-20에 `weekly_review`로 합쳐져 더는 발행되지 않지만
    # 여기서는 그대로 둔다 — 이 픽스처는 **실제로 발행된 사고 리포트**의 재현이고,
    # 그 리포트의 종류가 `week_start`였다. 타입을 바꾸면 있지도 않았던 사고를
    # 재현하는 테스트가 된다. 검증기는 report_type을 읽지 않는다.
    return dataclasses.asdict(model_mod.Report(
        report_type="week_start", report_date="2026-08-03",
        facts=[
            fact("KOSPI", "6,305.10", "전일대비 +17.91%", "^KS11", 17.91),
            fact("삼성전자", "88,000 KRW", "전일대비 +26.81%", "005930.KS", 26.81),
        ]))


def test_stripping_before_validation_would_disarm_the_net():
    """왜 순서가 그 순서여야 하는지를 증거로 남긴다.

    F-번호를 먼저 지우고 검증기에 넣으면 규칙 8·9는 접지할 것이 없어져 그
    귀속 오류를 못 잡는다. 이 테스트가 깨진다면 누군가 순서를 뒤집은 것이다.

    (숫자 실재 규칙(규칙 6)은 F-번호와 무관하게 계속 돈다 — 그래서 여기서는
    `citation_num`/`attribution`이 사라지는지만 본다. 26.81은 리포트 안에
    실재하는 숫자라 규칙 6은 애초에 발화하지 않는다.)
    """
    report_dict = _accident_report_dict()
    bad = "F1 에 기록된 전일대비 26.81% 급등이 나타났다."

    kinds_before = {k for k, _ in validate_mod.check(report_dict, bad)}
    assert "citation_num" in kinds_before, "전제 확인: 원문은 걸려야 한다"

    kinds_after = {k for k, _ in validate_mod.check(report_dict, strip_fact_numbers(bad))}
    assert not (kinds_after & {"citation_num", "attribution"}), (
        "지운 뒤에 검증하면 귀속 오류를 못 잡는다 — 그래서 검증이 먼저다")
