"""최종 검수(final-review.md) F2·F5 수리에 대한 회귀 테스트.

검수관이 실 LLM 생성물에서 재현한 결함 두 종류를 고정한다.

- **F2 (HIGH)** — 검증기가 "숫자가 실재하는가"만 보고 "그 숫자가 무엇인가"는
  아무도 보지 않아, SEC 접수번호(`filing_event`)를 "JPMorgan 영업현금흐름
  급감"으로 서술한 문단이 `status=ok`로 발행됐다. 다이제스트가 이미 F-번호를
  붙이고 있으므로, 인용된 F-번호와 그 항목의 실제 `metric`/`label`을 대조하면
  이 등급의 조작은 접지로 막힌다.
- **F5 (MED)** — 미탐 12건 중 접지 가능한 것: 정수 인용의 ±0.5 반올림 흡수
  (`실업률 15%` ← 15.3), 한글 단위 `천` 누락(`4천억원`), 완곡한 매매 권유
  (`지금 사도 괜찮은 국면`).

`from conftest import`를 쓰지 않는다 — `tests/` 아래에 `__init__.py`가 없어
전체 실행 시 형제 폴더의 동명 파일로 연결된다(검수서 F12). 이 파일이 쓰는
빌더는 전부 아래에 있다.
"""
from __future__ import annotations

import dataclasses

from market_intel.interp import digest as digest_mod
from market_intel.interp import validate as validate_mod
from market_intel.reporting.model import FactRow, Interpretation, Report

# 분/초까지 접지 집합에 들어간다(`_occurrences`가 cutoff 문자열을 훑는다).
# `22:15`이면 `15`가 리포트가 말한 숫자가 되어 A4 케이스를 못 재게 되므로
# 충돌하지 않는 시각을 쓴다 — 이 오염 자체는 이번 과제 범위 밖이다(잔여 위험).
CUTOFF = "2026-08-01T22:05:00+00:00"


def _row(label: str, value: str, comparison: str = "직전 관측 없음", *,
         subject: str = "TEST", metric: str = "value",
         raw_value: float | None = None) -> FactRow:
    return FactRow(
        label=label, value=value, comparison=comparison,
        source_url="https://example.test/x", data_status="source_verified",
        known_at=CUTOFF, subject=subject, metric=metric, raw_value=raw_value,
    )


def _report_obj() -> Report:
    """실제 `reports/morning/2026-08-01.json` · `reports/quarterly/2026Q3.json`의
    행 모양을 그대로 축소한 것 — 라벨/metric 조합이 이 규칙의 입력이므로
    손으로 예쁘게 다듬은 라벨을 쓰면 규칙을 실제와 다른 것에 대고 재는 셈이 된다."""
    return Report(
        report_type="quarterly", report_date="2026-08-01",
        cutoff_kst=CUTOFF, cutoff_utc=CUTOFF, generated_at=CUTOFF,
        title="테스트 리포트",
        headline="KOSPI 6,595.45(+17.91%) · S&P500 7,489.72(+0.70%)",
        data_status="source_verified",
        facts=[
            _row("한국 기준금리", "2.50 연%", subject="722Y001.0101000", raw_value=2.50),          # F1
            _row("미국 실업률", "4.20 lin", subject="UNRATE", raw_value=4.20),                     # F2
            _row("미국 비농업고용", "158,984 lin", subject="PAYEMS", raw_value=158984),            # F3
            _row("JPMorgan Chase(JPM) · earnings_release_8k", "0001628280-26-048078",
                 comparison="SEC EDGAR", subject="JPM", metric="earnings_release_8k"),             # F4
            _row("tci_fund_management · filing_event", "0001647251-26-000004",
                 comparison="SEC EDGAR", subject="tci_fund_management", metric="filing_event"),    # F5
            _row("Meta Platforms(META) CAPEX", "18,997,000,000 USD", comparison="분기",
                 subject="META", metric="capex", raw_value=18997000000),                           # F6
            _row("Alphabet(GOOGL) 매출", "119,796,000,000 USD", comparison="분기",
                 subject="GOOGL", metric="revenue", raw_value=119796000000),                       # F7
            _row("JPMorgan Chase(JPM) 영업현금흐름", "51,000,000,000 USD", comparison="분기",
                 subject="JPM", metric="operating_cash_flow", raw_value=51000000000),              # F8
            _row("Eli Lilly(LLY) CAPEX", "1,353,600,000 USD", comparison="분기",
                 subject="LLY", metric="capex", raw_value=1353600000),                             # F9
        ],
        market_reaction=[
            _row("KOSPI", "6,595.45 point", "전일대비 +17.91%",
                 subject="^KS11", metric="price_close", raw_value=6595.45),                        # F10
            _row("KOSDAQ", "719.76 point", "전일대비 +11.63%",
                 subject="^KQ11", metric="price_close", raw_value=719.76),                         # F11
        ],
        interpretation=Interpretation(), meta={},
    )


def _report() -> dict:
    return dataclasses.asdict(_report_obj())


# --- F2: F-번호 귀속 대조 --------------------------------------------------

def test_fact_index_follows_the_digest_numbering():
    """규칙 8은 `digest.build`가 매긴 F-번호를 리포트 dict에서 되짚어 쓴다.
    두 순서가 어긋나면 규칙은 엉뚱한 항목과 대조하게 되므로 여기서 못박는다."""
    report_obj = _report_obj()
    _text, findex = digest_mod.build(report_obj)
    index = validate_mod.fact_index(dataclasses.asdict(report_obj))
    assert len(index) == len(findex)
    for i, row in enumerate(index, start=1):
        assert row["label"] == findex[f"F{i}"].label, f"F{i} 불일치"


def test_filing_event_called_a_cash_flow_is_blocked():
    """검수서 F2의 핵심: SEC 접수번호를 '영업현금흐름'이라 부른다."""
    v = validate_mod.check(_report(), "F5 tci_fund_management의 영업현금흐름은 큰 폭으로 감소했다.")
    assert any(kind == "attribution" for kind, _tok in v), v


def test_subject_swapped_on_a_cited_fact_is_blocked():
    """F5는 tci_fund_management인데 JPMorgan이라고 부른다."""
    v = validate_mod.check(_report(), "F5 JPMorgan Chase의 공시가 접수됐다.")
    assert any(kind == "attribution" for kind, _tok in v), v


def test_review_sentence_is_blocked():
    """검수서가 실 LLM 생성물에서 뽑은 문장(F-번호만 이 픽스처에 맞춤)."""
    text = ("F5 JPMorgan Chase의 영업현금흐름은 분기별로 큰 폭으로 감소했으나 "
            "F9 Eli Lilly나 F6 Meta Platforms 같은 다른 기업들의 현금 흐름이 양호하다")
    v = validate_mod.check(_report(), text)
    tokens = [tok for kind, tok in v if kind == "attribution"]
    assert len(tokens) >= 3, f"세 인용 모두 잡아야 한다: {v}"


def test_macro_label_kind_mismatch_is_blocked():
    """F1은 한국 기준금리다 — 실업률이 아니다."""
    v = validate_mod.check(_report(), "F1 미국 실업률은 2.50%다.")
    assert any(kind == "attribution" for kind, _tok in v), v


def test_price_row_called_by_another_index_name_is_blocked():
    v = validate_mod.check(_report(), "F10 KOSDAQ은 6,595.45를 기록했다.")
    assert any(kind == "attribution" for kind, _tok in v), v


def test_consistent_citation_passes():
    assert validate_mod.check(_report(), "F8 JPMorgan Chase의 영업현금흐름은 51,000,000,000 USD다.") == []
    assert validate_mod.check(_report(), "F10 KOSPI 종가는 6,595.45다.") == []
    assert validate_mod.check(_report(), "F1 한국 기준금리는 2.50%다.") == []
    assert validate_mod.check(_report(), "F2 미국 실업률은 4.20%다.") == []


def test_8k_row_is_both_a_filing_and_an_earnings_release():
    """`earnings_release_8k`를 '공시'라고 부르는 것은 정당하다 — 8-K는 공시다.
    한쪽만 인정하면 매일 정당한 문장이 버려진다."""
    assert validate_mod.check(_report(), "F4 JPMorgan Chase 실적발표 공시가 있었다.") == []
    assert validate_mod.check(_report(), "F4 JPMorgan Chase 공시가 확인된다.") == []


def test_window_with_a_matching_kind_word_is_not_flagged():
    """한 창 안에 맞는 종류어와 다른 종류어가 함께 있으면 판단을 보류한다 —
    어느 쪽에 걸린 말인지 문법적으로 가릴 수 없고, 여기서 막으면 정당한
    비교 문장이 매일 버려진다."""
    assert validate_mod.check(_report(), "F5 tci_fund_management 공시에서 매출 얘기가 나왔다.") == []


def test_clause_boundary_stops_the_attribution_window():
    """쉼표 뒤는 다른 절이다 — 거기 나온 지표를 앞의 F-번호에 귀속시키면 오탐."""
    assert validate_mod.check(_report(), "F10 KOSPI가 크게 올랐는데, 실업률은 4.20%다.") == []


def test_unknown_fnumber_is_left_alone():
    """다이제스트에 없는 F-번호는 `digest.resolve_evidence`가 evidence_unresolved로
    처리한다(SA-6: 실패가 아니다). 규칙 8이 여기서 추측하지 않는다."""
    assert validate_mod.check(_report(), "F99 영업현금흐름이 늘었다.") == []


def test_row_without_a_groundable_kind_is_not_judged():
    """F3(미국 비농업고용)의 라벨에는 이 규칙이 아는 종류어가 없다 — 접지할 수
    없는 것을 추측으로 막지 않는다(잔여 위험으로 남긴다)."""
    assert validate_mod.check(_report(), "F3 미국 비농업고용은 158,984다.") == []


# --- F5: 정수 반올림 · 한글 단위 `천` · 완곡 매매 권유 ----------------------

def test_integer_citation_must_be_exact():
    """`17.91`을 `18`로 인용하는 것은 반올림이 아니라 없는 숫자다 (검수서 A5)."""
    v = validate_mod.check(_report(), "지수는 18% 상승했다.")
    assert any(kind == "num" for kind, _tok in v), v


def test_integer_citation_absorbing_a_decimal_is_blocked():
    """검수서 A4 — `15.3`을 `15`로 흡수하던 ±0.5 허용."""
    report = _report()
    report["facts"][1]["value"] = "15.30 lin"
    report["facts"][1]["raw_value"] = 15.30
    v = validate_mod.check(report, "미국 실업률은 15%까지 치솟았다.")
    assert any(kind == "num" for kind, _tok in v), v


def test_exact_integer_citation_still_passes():
    assert validate_mod.check(_report(), "미국 비농업고용은 158,984다.") == []


def test_one_decimal_rounding_still_passes():
    """소수 1자리 인용까지 막으면 `약 4.2%` 같은 정당한 표현이 매일 버려진다."""
    assert validate_mod.check(_report(), "미국 실업률은 약 4.2% 수준이다.") == []
    assert validate_mod.check(_report(), "KOSPI는 17.9% 올랐다.") == []


def test_korean_magnitude_cheon_is_blocked():
    """검수서 C10 — 규칙 3의 `[조억만]`에 `천`이 없어 `4천억`이 통과했다."""
    v = validate_mod.check(_report(), "외국인 순매수는 4천억원 규모다.")
    assert any(kind == "ko_magnitude" for kind, _tok in v), v


def test_soft_buy_recommendation_is_blocked():
    """검수서 G4."""
    v = validate_mod.check(_report(), "지금 사도 괜찮은 국면이다.")
    assert any(kind.startswith("banned:") for kind, _tok in v), v


def test_entry_timing_recommendation_is_blocked():
    """검수서 G5."""
    v = validate_mod.check(_report(), "신규 진입에 유리한 구간이다.")
    assert any(kind.startswith("banned:") for kind, _tok in v), v


def test_entry_word_in_a_non_recommendation_sentence_passes():
    """`진입`이 들어갔다는 이유만으로 막으면 안 된다 — 규칙 4의 첫 교훈."""
    assert validate_mod.check(_report(), "신규 진입 기업이 늘어난 업종이다.") == []


# --- 규칙 9: 인용에 붙은 숫자가 그 인용의 숫자인가 (발행 사고 2026-08-03) ----
#
# 2026-08-03 주간 시작 브리핑이 `KOSPI 가 F45 에 기록된 전일대비 26.81% 급등`
# 을 `status=ok`로 발행했다. F45는 KOSPI(+17.91%)이고 26.81%는 **삼성전자**의
# 등락률이다. 규칙 6은 26.81이 리포트 어딘가에 있다는 이유로(있다, 다른 행에)
# 통과시켰고 규칙 8은 이름·종류어만 보고 숫자는 보지 않았다.

def test_number_from_another_row_pinned_to_a_citation_is_blocked():
    """발행된 그 문장의 구조 그대로 — F10은 KOSPI(+17.91%), 11.63%는 KOSDAQ."""
    v = validate_mod.check(_report(), "KOSPI가 F10에 기록된 전일대비 11.63% 급등을 보였다.")
    assert ("citation_num", "F10 11.63") in v, v


def test_the_rows_own_number_next_to_its_citation_passes():
    """같은 문장 구조라도 제 숫자를 말하면 통과해야 한다 — 안 그러면 이 규칙은
    인용을 단 문장을 전부 죽인다."""
    assert validate_mod.check(_report(), "KOSPI가 F10에 기록된 전일대비 17.91% 급등을 보였다.") == []
    assert validate_mod.check(_report(), "KOSDAQ은 F11에서 11.63% 올랐다.") == []


def test_number_the_report_never_states_stays_rule_6s_job():
    """접지 자체가 안 되는 숫자는 규칙 6이 잡는다 — 규칙 9가 겹쳐 세지 않는다."""
    v = validate_mod.check(_report(), "KOSPI가 F10에서 88.12% 급등했다.")
    assert any(kind == "num" for kind, _t in v), v
    assert not any(kind == "citation_num" for kind, _t in v), v


def test_number_in_a_later_clause_is_not_pinned_to_the_citation():
    """절 경계(쉼표) 뒤는 다른 얘기다. 여기서 막으면 정당한 비교 문장이 죽는다."""
    assert validate_mod.check(
        _report(), "F10은 강세였고, KOSDAQ은 11.63% 올랐다."
    ) == []


def test_citation_of_a_numberless_row_grounds_nothing():
    """공시 행(F4)은 자기 숫자가 없다 — 창 안 숫자를 전부 거절하면 안 된다."""
    assert not any(
        kind == "citation_num"
        for kind, _t in validate_mod.check(_report(), "F4 공시 이후 17.91% 올랐다.")
    )


def test_a_citation_list_grounds_against_the_whole_list():
    """실 ollama 60필드 측정에서 규칙 9의 **유일한 오탐**이 이 모양이었다:

        `F96 과 F103 에 따르면 KOSPI 가 17.9% 급등하고 원화가 약세인 …`

    F103은 원/달러다. 17.9%는 앞의 F96(KOSPI) 것이고, 두 인용을 앞에 나란히
    세운 뒤 순서대로 서술하는 정당한 문장이다. 나열은 한 무리로 접지한다."""
    assert validate_mod.check(
        _report(), "F10 과 F11 에 따르면 KOSDAQ이 11.63% 올랐다."
    ) == []
    # 나열이 아니면(사이에 다른 말이 끼면) 그대로 각자 접지한다
    v = validate_mod.check(_report(), "F10은 강세였다 F11 기준 17.91% 급등이다.")
    assert ("citation_num", "F11 17.91") in v, v


def test_calling_the_fx_row_an_exchange_rate_is_not_an_attribution_error():
    """규칙 8 오탐(2026-08-03 실측): `F54의 달러/원 환율이 전일대비 +0.00%`는
    맞는 문장인데 거절됐다. 환율을 yfinance로 받아 metric이 `price_close`라
    종류가 `price`뿐이었기 때문이다 — metric은 **어떻게 받아왔는지**를 말할 뿐
    그 값이 무엇인지는 말하지 않는다."""
    report = _report_obj()
    report.market_reaction.append(
        _row("달러/원", "1,436.6원", "전일대비 +0.00%", subject="KRW=X",
             metric="price_close", raw_value=1436.6)
    )
    d = dataclasses.asdict(report)
    assert validate_mod.check(d, "F12의 달러/원 환율이 전일대비 0.00%로 움직이지 않았다.") == []
    # 주가 행을 환율이라 부르는 진짜 오류는 그대로 막힌다
    assert any(
        kind == "attribution"
        for kind, _t in validate_mod.check(d, "F10 KOSPI 환율이 올랐다.")
    )


# --- 규칙 3의 전제가 바뀐 뒤 (2026-08-03) ---------------------------------
# 리포트가 금액을 `2.2조 원`으로 쓰기 시작하면서 "리포트 계층은 조/억을 절대
# 안 쓴다"는 전제가 깨졌다. 무조건 금지를 **리포트가 실제로 쓴 표현만 허용**
# 으로 바꿨으므로, 이빨이 그대로인지와 인용이 통과하는지를 같이 못박는다.

def _report_with_flow() -> dict:
    report = _report_obj()
    report.facts = list(report.facts) + [
        _row("삼성전자(005930.KS) 개인 순매수(금액)", "2.1조 원",
             subject="005930.KS", metric="net_buy_individual_value",
             raw_value=2_100_000_000_000.0),
        _row("삼성전자(005930.KS) 기관 순매수(금액)", "-1.2조 원",
             subject="005930.KS", metric="net_buy_institution_value",
             raw_value=-1_220_000_000_000.0),
    ]
    return dataclasses.asdict(report)


def test_magnitude_quoted_from_the_report_now_passes():
    """근거를 대라고 요구해 놓고 근거대로 쓰면 막던 자리 — 해석 13회 중 3회가
    이 규칙으로 partial이었다."""
    v = validate_mod.check(_report_with_flow(), "개인이 2.1조 원을 담았다.")
    assert not any(kind == "ko_magnitude" for kind, _tok in v), v


def test_magnitude_the_report_never_wrote_is_still_blocked():
    """이빨은 그대로다 — 리포트에 없는 자릿수 표현은 지어낸 것이다."""
    for text in ("외국인 순매수는 4천억원 규모다.",
                 "개인이 5.7조 원을 담았다.",
                 "기관은 3억 주를 팔았다."):
        v = validate_mod.check(_report_with_flow(), text)
        assert any(kind == "ko_magnitude" for kind, _tok in v), (text, v)


def test_rounding_a_reported_magnitude_is_still_blocked():
    """리포트가 `2.1조`라고 썼는데 `2조`로 줄이면 그것도 재표현이다 — 정수
    인용에 반올림을 허용하지 않는 규칙 6과 같은 자세."""
    v = validate_mod.check(_report_with_flow(), "개인이 2조 원을 담았다.")
    assert any(kind == "ko_magnitude" for kind, _tok in v), v


def test_magnitude_match_ignores_only_whitespace():
    """공백 하나 때문에 인용이 반려되면 안 된다. 숫자가 다르면 여전히 걸린다."""
    assert not any(k == "ko_magnitude"
                   for k, _t in validate_mod.check(_report_with_flow(), "개인이 2.1 조 원 담았다."))


# --- 규칙 4: 권유와 서술을 가른다 (2026-08-03) ------------------------------
# `매수하다`는 권유의 말이자 서술의 말이다. 어간까지 막으면 "그날 누가 샀나"를
# 적을 수 없는데, 수급이 리포트 앞줄로 온 뒤로는 그것이 매일 쓸 문장이다.

def test_describing_who_bought_is_not_a_recommendation():
    """리포트의 수급 섹션이 답하는 바로 그 문장들 — 사실이지 권유가 아니다."""
    for text in ("개인이 대규모로 매수하고 기관은 매도했다.",
                 "외국인이 순매수하는 흐름이 이어졌다.",
                 "개인은 2.1조 원을 매수했다.",
                 "기관이 매도한 규모가 컸다.",
                 "신규 진입 기업이 늘었다."):
        v = validate_mod.check(_report_with_flow(), text)
        assert not any(kind.startswith("banned") for kind, _tok in v), (text, v)


def test_recommending_a_trade_is_still_blocked():
    """이빨은 그대로다 — 사라고 하거나 살 때를 짚으면 걸린다."""
    for text in ("지금 매수하라.",
                 "매수하세요.",
                 "이 구간에서 매수해야 한다.",
                 "매수 추천 의견이다.",
                 "지금이 매수 타이밍이다.",
                 "매수할 때다.",
                 "매수하는 게 좋다.",
                 "반도체는 사야 한다.",
                 "비중을 늘려야 한다.",
                 "지금 사도 괜찮은 국면이다.",
                 "신규 진입에 유리한 구간이다."):
        v = validate_mod.check(_report_with_flow(), text)
        assert any(kind.startswith("banned") for kind, _tok in v), (text, v)
