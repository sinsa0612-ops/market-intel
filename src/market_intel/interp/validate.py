r"""Hallucination / banned-phrase / format validator (spec SA-5).

These six rules are not a from-scratch design: spec §사전 확인 사실 C ran a
prototype of exactly this ruleset against 17 hand-built adversarial sentences
and 32 *real* qwen3.5:9b/14b/27b-generated fields, and reports 17/17 and
31/32 respectively — the one rejection in the second set was a genuine LLM
error (a unit swap: "1.76%포인트" for what the report actually said was
"+1.76%"), not a false positive. Getting there took two corrections, both
recorded here so a future edit doesn't reintroduce them:

1. A first pass banned the raw substrings "매수"/"목표가"/etc. and blocked
   `외국인 매수세` and `금리 상단 목표가 3.75%` — both legitimate. Switched to
   the context-sensitive regexes below (rule 4), which only fire on
   trade-recommendation *phrasing*, not any sentence that happens to contain
   those characters.
2. A first pass only compared numeric tokens against the report's allowed
   numbers, and missed `12조 4천억원` — because 12 and 4 individually *do*
   appear somewhere in the report, just not as that combined magnitude.
   Added rule 3 (Korean magnitude words are banned outright in interpretation
   prose) on the grounds that the 4 generated reports never use `조`/`억`/`만`
   themselves (grep-verified, 0 hits) — so any appearance in interpretation
   text is by definition an ungrounded re-expression, not a citation.

`check()` intentionally does not short-circuit on the first violation: it
collects every rule that fired, because `apply.fill()` records the full
violation list in the repair prompt and in `meta["interpretation"]["violations"]`.

--- 2026-08-02 hardening (judge.md §6 「양쪽 다 틀린 것」) ---

The ST2 judge ran 50 adversarial cases plus 16 real ollama generations
against both variants and reproduced six holes that the shipped ruleset —
this one — left open. Each fix below is keyed to that report, and every one
of them is pinned by `tests/interp/test_validate_adversarial.py`:

- rule 0 (`cjk`): the local model is Chinese-trained and leaked `持仓`/`背后`
  into Korean prose in 2 of 8 real runs; both passed as `status=ok` and
  reached the published markdown. There was no charset rule at all.
- rule 4 (`bp`/`%p`): `\b` does not exist between a digit and `bp`, and
  Python's `\w` includes Hangul so it does not exist between `%p` and `다`
  either — the rule the spec §C added to catch the one real 27b error was
  dead in exactly the two spellings a model actually writes (`47bp`,
  `1.8%p다`). Replaced with explicit latin-boundary lookarounds.
- rule 6 token regex: the trailing `(?![A-Za-z0-9])` guard (there to skip
  `KOSPI200`-style identifiers) meant any number with a latin unit glued to
  it was never checked at all — `1442KRW`, `9.99pct`, `1.76pp`, `999M` all
  walked through. The guard is now one-sided (leading only, which is what
  identifier-shaped tokens actually need) and a glued latin suffix is itself
  a violation unless it is one the report's own vocabulary uses.
- rule 7 (`unit`, new): `원/달러 환율은 1,442.1달러다` and `미국 실업률은
  4.20원이다` both passed — the digits are real, only the unit was swapped,
  and nothing compared a token's unit against the unit the report attaches
  to that same number.
- rule 6 sign handling: comparing signed values literally (correct, and the
  reason the winning variant caught 3 sign flips the loser missed) also
  rejected `0.21% 하락` for a report that says `-0.21%` — a legitimate
  Korean rendering. Absolute-value restatements are now accepted *only*
  when the surrounding wording states the matching direction, which as a
  side effect catches the inverse hallucination (`0.21% 상승`).

--- 2026-08-02 최종 검수 수리 (final-review.md F2 · F5) ---

적대 검수가 실 LLM 생성물에서 재현한 것: 이 검증기는 **숫자의 실재**만 보고
**그 숫자가 무엇인지**는 아무도 보지 않는다. 발행된 문장 —

    "F84 JPMorgan Chase의 영업현금흐름은 분기별로 큰 폭으로 감소했으나 …"

F84는 SEC 접수번호(`filing_event`)였다. 현금흐름 수치는 어디에도 없고, 그
문단에는 숫자가 아예 없어 규칙 6이 볼 것도 없었다(`status=ok`로 발행).

- 규칙 8 (`attribution`, 신규): 다이제스트가 이미 붙여 준 F-번호를 그 항목의
  실제 `label`/`metric`과 대조한다. 접지 가능한 등급만 막고(주체 이름 바꿔치기,
  항목 종류 바꿔치기) 나머지는 남긴다 — 아래 규칙 8 주석의 잔여 위험 목록.
- 규칙 3: `[조억만]`에 `천`이 없어 `4천억원`이 통과했다(미탐 C10).
- 규칙 4: `지금 사도 괜찮은 국면` / `신규 진입에 유리한 구간` 같은 완곡한 매매
  권유가 패턴 밖이었다(미탐 G4/G5).
- 규칙 6: 정수 인용의 ±0.5 반올림 허용이 `실업률 15%`(실제 15.3)와
  `S&P500 18% 상승`(실제 17.9)을 흡수했다(미탐 A4/A5). 정수만 정확 일치로
  좁혔고, 소수 인용의 반올림(`약 4.7%` <- 4.68)은 그대로 둔다.

오탐 비용 실측(수리 전/후 동일 생성물): 자작 코퍼스 57건 오탐 0 -> 0,
실 ollama 20회(quarterly 10 + morning 10, 60필드) 거부 0 -> 2 — 2건 모두
규칙 8이 `filing_event`를 실적/현금흐름이라 부른 문장을 잡은 것이다.
"""
from __future__ import annotations

import re

from .. import universe as _universe

# Rule 0 — CJK ideographs / kana. The interpretation is Korean prose for a
# Korean reader; `qwen3.5:9b` is a Chinese-trained model and leaks source-
# language tokens mid-sentence (judge.md §6-2: `持仓`, `背后` in 2 of 8 real
# runs, both published). Banned outright rather than filtered, because the
# ban costs nothing here: a full scan of everything this project can put in
# front of the model or the reader — `src/**`, `reports/**` (6 report types),
# `docs/**`, `theses/**`, and all 5.8MB / 304 raw snapshots of
# `var/market_intel.db` including the DART filing payloads — contains **zero**
# characters in these ranges (measured 2026-08-02; the corpus is Hangul +
# latin + `·`/`±` punctuation only). Same argument as rule 3: a character
# class the report layer never produces cannot be a citation.
_CJK_RE = re.compile(
    r"[々〆぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿]"
)

# Rule 1 — format violations. Interpretation text is plain Korean prose; any
# of these mean either injected markup (rendered live by the un-escaped
# Obsidian markdown renderer — SA-13 trust boundary 2) or a raw URL that adds
# nothing over the report's already-normalized fact labels.
_FORMAT_MARKERS = ("<", ">", "](", "http://", "https://", "`")

# Rule 2 — length cap. Real outputs top out around 230 chars; 600 leaves
# generous room without allowing a runaway generation to dominate the report.
_MAX_LEN = 600

# Rule 3 — Korean order-of-magnitude words. 잡으려는 것은 **리포트가 말하지
# 않은 형태로 숫자를 다시 쓰는 것**이다 — 자릿수 하나하나는 진짜여도 형태가
# 지어낸 것이면 그것도 없는 사실이다. `4천억원`이 그렇게 발행됐다
# (final-review.md 미탐 C10).
#
# 원래는 "리포트 계층은 조/억/만/천을 절대 안 쓴다(grep으로 확인)"는 사실에
# 기대어 **무조건 금지**로 구현돼 있었다. 2026-08-03에 그 전제가 깨졌다:
# 수급·재무 금액이 자릿수 12~15개라 읽히지 않는다는 CEO 지적으로 리포트가
# `2.2조 원`을 직접 쓰기 시작했다. 전제가 사라졌는데 금지를 그대로 두면
# **리포트의 표현을 그대로 인용한 해석문이 반려된다** — 근거를 대라고 요구해
# 놓고 근거대로 쓰면 막는 셈이다(실측: 해석 13회 중 3회가 이 규칙으로 partial).
#
# 그래서 프록시를 직접 검사로 바꾼다: 리포트가 **실제로 쓴** 자릿수 표현만
# 허용한다. 규칙의 이빨은 그대로다 — 리포트에 없는 `4천억`은 여전히 걸리고,
# `2.2조`를 `2조`로 반올림한 것도 (표현이 다르므로) 걸린다.
_KO_MAGNITUDE_RE = re.compile(r"\d[\d,.]*\s*[조억만천]")


def _magnitude_token(text: str) -> str:
    """`2.2 조` -> `2.2조`. 리포트와 해석문의 띄어쓰기가 달라도 같은 표현은
    같게 본다 — 공백 하나 때문에 인용이 반려되면 안 된다."""
    return re.sub(r"\s+", "", text)


def allowed_magnitudes(report: dict) -> set[str]:
    """리포트가 실제로 쓴 자릿수 표현들. `_occurrences`가 숫자를 긁는 것과
    같은 자리를 같은 순서로 훑는다 — 한쪽만 보는 필드가 생기면 그 필드를
    인용한 해석문이 이유 없이 반려된다."""
    found: set[str] = set()

    def scan(text: str) -> None:
        for m in _KO_MAGNITUDE_RE.finditer(str(text or "")):
            found.add(_magnitude_token(m.group(0)))

    scan(report.get("headline"))
    for key in ("facts", "market_reaction"):
        for row in report.get(key) or []:
            scan(row.get("value"))
            scan(row.get("comparison"))
            scan(row.get("label"))
    for key in ("events", "schedule_changes"):
        for row in report.get(key) or []:
            scan(row.get("name"))
    return found

# Rule 4 — banned trade-recommendation / price-target / return-forecast
# phrasing (BRIEF rule 5, spec §1/§6.1). Context-sensitive regexes, not bare
# substrings — see correction #1 above for why.
_BANNED_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"목표\s*주가"), "목표주가"),
    (re.compile(r"적정\s*주가"), "적정주가"),
    (re.compile(r"투자\s*의견"), "투자의견"),
    (re.compile(r"비중\s*(확대|축소)"), "비중조절"),
    # `매수하다`는 **권유의 말이자 서술의 말**이다. 원래 이 규칙은 어간까지
    # (`매수` + `하`) 막고 있었는데, 그러면 "개인이 매수하고 기관은 매도했다"와
    # "순매수하는 흐름"처럼 **그날 일어난 일을 적은 문장**이 권유로 걸린다.
    # 2026-08-03에 수급이 리포트 앞줄로 오면서 이 오탐이 매일 나게 됐다(실측:
    # 그날 해석문이 `매수하`로 반려). 막아야 하는 것은 "사라/살 때다"이지
    # "샀다"가 아니므로, 어미로 갈라 **권유·시점 지목만** 남긴다.
    (re.compile(r"(매수|매도)\s*(하라|하세요|하십시오|해라|해야|하는\s*게\s*(좋|낫)|"
                r"하기\s*(좋|적절|유리)|권|추천|의견|타이밍|시점|구간|기회|시그널|신호)"), "매매권유"),
    (re.compile(r"(매수|매도)\s*할\s*(때|시점|자리|구간|타이밍|기회|만하)"), "매매권유"),
    (re.compile(r"사야|팔아야|담아야|비중을\s*(늘|줄)"), "매매권유"),
    (re.compile(r"손절|익절"), "매매권유"),
    # 완곡한 매매 권유 (final-review.md 미탐 G4/G5). `매수/매도`라는 말을
    # 쓰지 않아도 "지금 사도 괜찮은 국면" / "신규 진입에 유리한 구간"은 명세가
    # 금지하는 그 판단 그대로다. 규칙 4의 첫 교훈대로 문구 전체로 걸어
    # `신규 진입 기업이 늘었다` 같은 서술은 통과시킨다.
    (re.compile(r"(사도|사기에|사기엔|담아도|들어가도)\s*(괜찮|좋|무방|나쁘지)"), "매매권유"),
    (re.compile(r"진입\s*(시점|타이밍|기회|적기|구간)|진입에\s*(유리|좋)"), "매매권유"),
    (re.compile(r"목표\s*수익률|기대\s*수익률|예상\s*주가|주가\s*전망치"), "수익예측"),
    # Unit substitution — the one real error the empirical test caught (a 27b
    # output that restated "전일대비 +1.76%" as "1.76%포인트 상승"). Numeric
    # comparison alone cannot catch this because the digit itself is correct.
    # The `\b` spelling this rule shipped with was dead in both of the forms
    # a model actually writes (judge.md §6-3): `\b` needs a word/non-word
    # transition, and `47bp` has a digit on the left while `1.8%p다` has
    # Hangul on the right — Python's `\w` counts both as word characters.
    # Latin-only lookarounds are the boundary this rule actually meant.
    (
        re.compile(r"%\s*포인트|퍼센트\s*포인트|%\s*[pP](?![A-Za-z])|(?<![A-Za-z])bps?(?![A-Za-z])"),
        "단위변조",
    ),
)

# Rule 5 — date tokens must be one of the report's own dates.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Rule 6 — numeric tokens. The *leading* guard is what identifier-shaped
# tokens need (`KOSPI200`, `S&P500`, `Core16`, and the digest's own `F34`
# fact references all put the letters BEFORE the digits). The trailing guard
# this originally carried skipped the token entirely whenever a latin unit
# was glued to its right, which is where `1442KRW`/`9.99pct`/`999M` walked
# through unchecked (judge.md §6-4) — so the suffix is captured and judged
# by rule 6b instead of silencing the whole token.
_NUM_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?")
_LATIN_SUFFIX_RE = re.compile(r"^[A-Za-z]+")
_UNIT_RE = re.compile(r"^\s*(%|％|퍼센트|원|달러|엔|위안|조|억|만|천|배|포인트|%[pP]|bp|pt)")
_SMALL_INT_EXEMPT_MAX = 12

# Rule 6b — latin suffixes the report's own vocabulary uses, measured over
# every string the digest can show the model (headline/label/value/comparison
# /event name across all 6 report types, 2026-08-02): `미10Y`, `2Y`, `13F`,
# `earnings_release_8k`, `180d`/`181d`/`272d`. Every latin unit the report
# writes it writes with a separating space (`1,442.07 USD`, `6,595.45 point`,
# `158,984 lin`) — never glued — so a glued suffix outside this set is, like
# rule 3's `조/억/만`, a re-expression of a number in a form nothing in the
# report states. Deliberately case-sensitive: `999K` (thousands) must stay a
# violation even though `8k` is legal.
_ALLOWED_LATIN_SUFFIXES = frozenset({"Y", "F", "d", "k"})

# Rule 7 — unit contradiction. Only the *currency* classes are enforced,
# because that is the direction of the attack the judge reproduced (`1,442.1
# 달러` for 원, `4.20원` for a rate) and because the report's own macro units
# are FRED's opaque `lin`/`point` strings — enforcing percent against those
# would reject the entirely correct `실업률 4.20%`.
# Known cost, measured over 71 real generated fields (1 hit): the USD/KRW
# market_reaction row states the won rate as `1,442.07 USD` (yfinance quote
# currency), so a correct `1,442.07 원` is rejected too. The report attaches
# both currencies to the same rate, so no rule can block the judge's
# `1,442.1달러` and pass that one — see
# tests/interp/test_validate_adversarial.py::
# test_known_false_positive_usdkrw_row_unit_mislabel, which is a strict
# xfail pointing at the real fix (label that row in 원, in `reporting/**`).
_UNIT_CLASSES = {
    "%": "percent", "％": "percent", "퍼센트": "percent",
    "원": "krw", "KRW": "krw", "달러": "usd", "USD": "usd",
    "엔": "jpy", "JPY": "jpy", "위안": "cny", "CNY": "cny", "유로": "eur", "EUR": "eur",
}
_CURRENCY_CLASSES = frozenset({"krw", "usd", "jpy", "cny", "eur"})
_KNOWN_UNIT_RE = re.compile(r"^[ ]?(퍼센트|%|％|원|달러|엔|위안|유로|KRW|USD|JPY|CNY|EUR)")
# Anything else glued to the number (`4.20 lin`, `6,595.45 point`, `7종목`,
# `2026년`) still says "this magnitude is not money" — enough to contradict a
# currency, not enough to name a unit. A number followed by punctuation or a
# bracket (`KOSPI 6,595.45(+17.9%)`) carries no unit at all and stays
# permissive, so adding a unit to a bare number is never invented evidence.
_OTHER_UNIT_RE = re.compile(r"^[ ]?[A-Za-z가-힣]")

# Rule 6c — direction words, for absolute-value restatements of a negative
# report value (`-0.21%` written as `0.21% 하락`). The nearest one wins.
_DOWN_RE = re.compile(r"하락|하회|내렸|내려|내리|떨어|감소|급락|하향|약세|낙폭|밀렸|빠졌|마이너스|줄었|축소|둔화")
_UP_RE = re.compile(r"상승|상회|올랐|올라|오르|증가|급등|확대|강세|반등|뛰었|늘었|플러스")
_DIRECTION_WINDOW = 30

# --- Rule 8 — F-번호 귀속 대조 (final-review.md F2) -------------------------
#
# 검증기가 "이 숫자가 리포트에 있는가"만 보고 "그 숫자가 무엇인가"는 아무도 보지
# 않아서, 실 LLM 생성물이 SEC 접수번호(`filing_event`)를 "JPMorgan 영업현금흐름
# 급감"이라 서술한 문단이 `status=ok`로 발행됐다. 그 문단에는 숫자가 아예 없어
# 규칙 6이 볼 것도 없었다.
#
# 접지할 수 있는 것만 막는다: 다이제스트가 이미 F-번호를 붙여 모델에게 주므로
# (`digest.build` — facts[] 다음 market_reaction[], 연속 번호), 해석문이 인용한
# F-번호 **바로 뒤**에 오는 주체 이름과, 같은 절 안에서 말하는 항목 종류를 그
# F-번호의 실제 `label`/`metric`과 대조할 수 있다. 어휘는 전부 리포트 자신에게서
# 나온다 — 이름은 라벨에서 뽑고, 종류어는 라벨/metric이 실제로 쓰는 것만 안다.
#
# 의미 검증 일반이 아니다(그건 불가능하다). 못 막는 것은 그대로 남는다:
#   * F-번호를 인용하지 않은 귀속 오류 (`S&P500은 6,595.45를 기록했다` —
#     `test_attribution_error_still_missed`가 strict xfail로 지키고 있는 그 구멍),
#   * 라벨에도 metric에도 종류어가 없는 행(FRED `value` 계열 상당수),
#   * 한 창 안에 맞는 종류어와 틀린 종류어가 함께 있는 문장(아래 보류 규칙).
# 이 잔여 위험은 `AI 자동판정` 배지가 감당한다.
_ATTRIB_WINDOW = 40
# 종류어를 그 인용에 귀속시키는 창은 **주체 이름 바로 뒤부터** 20자다(이름이
# 안 붙었으면 F-번호 바로 뒤부터). 인용에서 바로 세어 넓게 잡으면
# `F98 등 지수 변동과 함께 2026-08-04 에 발표될 실업률 데이터`처럼 앞의 인용과
# 무관한 뒷말까지 끌어와 오탐이 된다 — 실 ollama 10회 중 1건에서 실제로 그랬다.
_ATTRIB_KIND_WINDOW = 20
# 절 경계. 쉼표 뒤는 다른 절이므로 거기 나온 지표를 앞의 F-번호에 귀속시키면
# 오탐이 된다(`F10 KOSPI가 올랐는데, 실업률은 4.20%다`). 날짜도 같은 이유로
# 경계다 — `…에 발표될 X`는 인용한 사실이 아니라 다가오는 일정 얘기다. `.`는
# 소수점에서도 걸리지만 창을 짧게 만들 뿐이라 오탐 방향으로만 작동한다.
_ATTRIB_STOP_RE = re.compile(r"[,.\n·;]|\d{4}-\d{2}-\d{2}")
# 규칙 9는 같은 경계를 쓰되 **소수점에서는 끊지 않는다**. 위의 `.`는 규칙 8을
# 짧게 만들 뿐이지만(오탐 방향), 규칙 9에서 `26.81%`를 `26`으로 잘라 놓으면
# 엉뚱한 토큰을 신고하고 맞는 문장(`29.95%` -> `29`)까지 거절한다 — 실측함.
_ATTRIB_NUM_STOP_RE = re.compile(r"[,\n·;]|(?<!\d)\.|\.(?!\d)|\d{4}-\d{2}-\d{2}")
# 인용 나열(`F96 과 F103 에 따르면 KOSPI 가 17.9% 급등하고 원화가 약세인`).
# 뒤따르는 숫자는 **나열 전체**에 걸린 것이지 마지막 인용의 것이 아니다 —
# 실 ollama 60필드 측정에서 규칙 9의 유일한 오탐이 이 모양이었다. 앞 인용과
# 이 인용 사이에 접속 조사/기호밖에 없으면 한 무리로 보고 접지 집합을 합친다.
_ATTRIB_JOINER_RE = re.compile(r"^\s*(?:[과와랑]|,|·|및|그리고)?\s*$")
# 다이제스트와 동일한 F-번호 문법. 한국어 조사가 바로 붙으므로 오른쪽 `\b`는
# 쓸 수 없다(digest._FNUM_RE와 같은 이유).
_ATTRIB_FNUM_RE = re.compile(r"(?<![A-Za-z0-9])F(\d+)(?!\d)")

# 종류어 -> 그 말이 가리킬 수 있는 항목 종류. 리포트 라벨과 metric이 실제로
# 쓰는 것만 있다. 포괄어(`현금흐름`)는 여러 종류를 가리킬 수 있으므로 집합이다.
# `금리`/`주가`처럼 정상 서술에 흔히 섞이는 포괄어는 일부러 넣지 않았다 —
# 넣는 순간 판단 근거가 아니라 오탐 발생기가 된다.
_KIND_TERMS: tuple[tuple[str, frozenset], ...] = (
    ("영업활동현금흐름", frozenset({"ocf"})),
    ("영업활동 현금흐름", frozenset({"ocf"})),
    ("영업현금흐름", frozenset({"ocf"})),
    ("영업 현금흐름", frozenset({"ocf"})),
    ("잉여현금흐름", frozenset({"fcf"})),
    ("잉여 현금흐름", frozenset({"fcf"})),
    ("현금흐름", frozenset({"ocf", "fcf"})),
    ("현금 흐름", frozenset({"ocf", "fcf"})),
    ("매출액", frozenset({"revenue"})),
    ("매출", frozenset({"revenue"})),
    ("영업이익", frozenset({"operating_income"})),
    ("CAPEX", frozenset({"capex"})),
    ("설비투자", frozenset({"capex"})),
    ("자본지출", frozenset({"capex"})),
    ("종가", frozenset({"price"})),
    ("실적발표", frozenset({"earnings"})),
    ("실적 발표", frozenset({"earnings"})),
    ("공시", frozenset({"filing"})),
    ("기준금리", frozenset({"policy_rate"})),
    ("실업률", frozenset({"unemployment"})),
    ("환율", frozenset({"fx"})),
)

# metric -> 종류. `earnings_release_8k`가 `filing`도 갖는 것은 사실이라서다 —
# 8-K는 공시다. 한쪽만 인정하면 `F4 JPMorgan 공시`라는 맞는 문장이 매일 버려진다.
# `value`(FRED/ECOS 거시)는 metric만으로는 무엇인지 알 수 없으므로 여기 없고,
# 라벨에서만 종류를 얻는다.
_METRIC_KINDS = {
    "operating_cash_flow": frozenset({"ocf"}),
    "free_cash_flow": frozenset({"fcf"}),
    "capex": frozenset({"capex"}),
    "revenue": frozenset({"revenue"}),
    "operating_income": frozenset({"operating_income"}),
    "price_close": frozenset({"price"}),
    "filing_event": frozenset({"filing"}),
    "earnings_release_8k": frozenset({"earnings", "filing"}),
}

# 환율은 yfinance로 받으므로 metric이 `price_close`다 — 즉 위 표대로면 종류가
# `price`뿐이라, **맞는 문장**인 `F54의 달러/원 환율이 전일대비 +0.00%`가
# `환율`(fx) ∩ `price` = ∅ 으로 거절됐다(2026-08-03 실측, 규칙 8 오탐).
# metric은 어떻게 받아왔는지를 말할 뿐 그 값이 무엇인지는 말하지 않는다.
# `universe.py`의 fx 카테고리 심볼을 여기 이름으로 못박는다(import하지 않는
# 것은 이 모듈이 리포트 dict 하나만 보고 판정한다는 계약 때문).
_FX_SUBJECTS = frozenset({"KRW=X", "DX-Y.NYB"})

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")
_NAME_LATIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &._\-]*$")
_NAME_HANGUL_RE = re.compile(r"^[가-힣A-Za-z0-9]{2,}$")
_LABEL_HEAD_RE = re.compile(r"^(.*?)\(([^)]*)\)")
# F-번호와 이름 사이에 허용하는 것: 공백뿐이거나, 한국어 조사 한두 자 + 공백
# (`F84 JPMorgan`, `F84의 JPMorgan`). 이름은 인용 **바로 뒤**에 있을 때만
# 그 인용의 주체로 읽는다 — 같은 문장 어딘가에 다른 종목이 나왔다는 이유로
# 막으면 정당한 비교 문장이 전부 걸린다.
_ATTRIB_LEAD_RE = re.compile(r"^(?:[가-힣]{1,2}\s+|\s+)?")


def fact_index(report: dict) -> list[dict]:
    """F1..Fn -> 행. `digest.build`의 번호 매김(facts[] 다음 market_reaction[],
    연속 번호)을 리포트 dict에서 되짚는다. 두 순서가 어긋나면 규칙 8은 엉뚱한
    항목과 대조하게 되므로 `test_fact_index_follows_the_digest_numbering`이
    둘을 직접 맞대어 못박는다."""
    return list(report.get("facts") or []) + list(report.get("market_reaction") or [])


def _row_kinds(row: dict) -> frozenset:
    kinds = set(_METRIC_KINDS.get(str(row.get("metric") or ""), ()))
    label = str(row.get("label") or "")
    for term, classes in _KIND_TERMS:
        if term in label:
            kinds |= classes
    if str(row.get("subject") or "") in _FX_SUBJECTS:
        kinds.add("fx")
    return frozenset(kinds)


def _row_names(row: dict) -> frozenset:
    """라벨이 그 행을 부르는 이름들(표시명 + 티커). 종류어가 섞인 이름
    (`한국 기준금리`)이나 공백이 낀 한글 서술(`연방기금금리 상단`)은 이름이
    아니라 설명이므로 뽑지 않는다."""
    head = str(row.get("label") or "").split(" · ")[0].strip()
    names: set[str] = set()
    m = _LABEL_HEAD_RE.match(head)
    if m:
        inner = m.group(2).strip()
        if _TICKER_RE.match(inner):
            names.add(inner)
        head = m.group(1).strip()
    if len(head) >= 2 and not any(term in head for term, _c in _KIND_TERMS):
        if _NAME_LATIN_RE.match(head) or _NAME_HANGUL_RE.match(head):
            names.add(head)
    return frozenset(names)


def _leading_name(window: str, names: list[str]) -> tuple[str, int] | None:
    """창 맨 앞의 주체 이름과 그 이름이 끝나는 위치(창 기준)."""
    lead = _ATTRIB_LEAD_RE.match(window).end()
    rest = window[lead:]
    for name in names:  # 긴 이름 우선 — `S&P 500`이 `S&P`보다 먼저 맞아야 한다
        if not rest.startswith(name):
            continue
        after = rest[len(name) : len(name) + 1]
        if name[-1].isascii() and after.isascii() and after.isalnum():
            continue  # `KOSPI`가 `KOSPI200`의 앞부분으로 맞은 것
        return name, lead + len(name)
    return None


def _row_numbers(row: dict) -> set[float]:
    """그 행 **자신이** 말하는 숫자 전부 — 규칙 9의 접지 집합.

    `label`/`value`/`comparison`에 적힌 숫자 + `raw_value` + `delta_pct` +
    `series`(추이 그래프의 입력이고 전부 그 행의 관측값이다). 리포트 전체의
    숫자 집합(`allowed_numbers`)과 달리 **행 단위**라서, 같은 리포트 안의
    다른 종목 숫자를 이 행에 붙인 문장이 여기서 걸린다.

    수량이 아닌 행 — 공시(`raw_value`가 접수번호 문자열) — 은 빈 집합이다.
    접수번호의 자릿수를 "그 행의 숫자"로 세면 `0001628280-26-048078`이 1628280을
    말한 것이 되고, 반대로 그 행을 인용한 문장의 숫자를 전부 거절하게 된다."""
    raw = row.get("raw_value")
    numeric_raw = isinstance(raw, (int, float)) and not isinstance(raw, bool)
    if not numeric_raw and not isinstance(row.get("delta_pct"), (int, float)):
        return set()
    nums: set[float] = set()
    for key in ("label", "value", "comparison"):
        for _m, _tok, value, _dec, _hu, _uc, _latin in _numeric_tokens(str(row.get(key) or "")):
            nums.add(value)
    for key in ("raw_value", "delta_pct"):
        v = row.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.add(float(v))
    for v in row.get("series") or []:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.add(float(v))
    return nums


def _grounded_in(value: float, decimals: int, pool: set[float]) -> bool:
    """규칙 6과 같은 반올림 규약: 정수 인용은 정확 일치, 소수 인용은 그 자릿수."""
    if decimals == 0:
        return value in pool
    return any(round(a, decimals) == round(value, decimals) for a in pool)


def _attribution(report: dict, text: str) -> list[tuple[str, str]]:
    index = fact_index(report)
    if not index:
        return []
    kinds = [_row_kinds(row) for row in index]
    names = [_row_names(row) for row in index]
    row_nums = [_row_numbers(row) for row in index]
    all_names = sorted({n for group in names for n in group}, key=len, reverse=True)
    report_nums = allowed_numbers(report)

    violations: list[tuple[str, str]] = []
    text_no_dates = _DATE_RE.sub(lambda m: " " * len(m.group(0)), text)
    cites = list(_ATTRIB_FNUM_RE.finditer(text))
    for i, m in enumerate(cites):
        n = int(m.group(1))
        if not 1 <= n <= len(index):
            continue  # 없는 F-번호는 digest.resolve_evidence의 몫(SA-6)
        limit = cites[i + 1].start() if i + 1 < len(cites) else len(text)
        window = text[m.end() : min(limit, m.end() + _ATTRIB_WINDOW)]
        stop = _ATTRIB_STOP_RE.search(window)
        if stop:
            window = window[: stop.start()]

        hit = _leading_name(window, all_names)
        name, name_end = hit if hit else ("", 0)
        if name and names[n - 1] and name not in names[n - 1]:
            violations.append(("attribution", f"F{n} {name}"))

        if kinds[n - 1]:
            kind_window = window[name_end : name_end + _ATTRIB_KIND_WINDOW]
            found = [(t, c) for t, c in _KIND_TERMS if t in kind_window]
            # 맞는 종류어가 하나라도 같이 있으면 보류한다: 어느 쪽에 걸린 말인지
            # 문법적으로 가릴 수 없고, 여기서 막으면 `F5 …공시에서 매출 얘기가
            # 나왔다` 같은 정당한 문장이 매일 버려진다.
            if found and not any(c & kinds[n - 1] for _t, c in found):
                violations.append(("attribution", f"F{n} {found[0][0]}"))

        num_window = text_no_dates[m.end() : min(limit, m.end() + _ATTRIB_WINDOW)]
        num_stop = _ATTRIB_NUM_STOP_RE.search(num_window)
        if num_stop:
            num_window = num_window[: num_stop.start()]
        own = set(row_nums[n - 1])
        for j in range(i - 1, -1, -1):  # 앞으로 이어진 인용 나열을 모두 흡수
            prev = cites[j]
            if not _ATTRIB_JOINER_RE.match(text[prev.end() : cites[j + 1].start()]):
                break
            p = int(prev.group(1))
            if 1 <= p <= len(index):
                own |= row_nums[p - 1]
        violations.extend(_citation_numbers(num_window, n, own, report_nums))
    return violations


# 이름 안에 숫자가 든 지수들 — `S&P 500`의 500은 **양이 아니라 이름의 일부**다.
# 실측(2026-08-04 morning): `F64에 따라 KOSPI는 하락한 반면 S&P 500은 올랐다`가
# "F64(KOSPI)에 500을 갖다 붙였다"로 반려됐다. 500은 히어로 카드 라벨
# `S&P 500`에 있으므로 규칙 9의 조건 (2)까지 만족해 버린다.
#
# **목록은 universe에서 뽑는다.** 손으로 적으면 관측군에 지수를 추가할 때마다
# 조용히 오탐이 하나씩 늘어난다 — 이 저장소가 `_MARKET_REACTION_SYMBOLS`에서
# 이미 한 번 겪은 실패 방식이다. 긴 이름부터 지워야 `러셀2000`이 `러셀`+`2000`
# 으로 쪼개지지 않는다.
_NUMERIC_NAMES = sorted(
    {n for m in _universe.UNIVERSE for n in (m["name"], m["name_ko"]) if any(c.isdigit() for c in n)},
    key=len, reverse=True,
)
# 붙여 쓴 표기(`S&P500`)는 애초에 숫자로 안 쪼개지지만, 사람이 쓰는 띄어쓰기
# 변형(`S&P  500`)까지 같이 잡으려면 공백을 유연하게 둔다.
_NUMERIC_NAME_RE = re.compile(
    "|".join(re.escape(name).replace(r"\ ", r"\s*") for name in _NUMERIC_NAMES)
) if _NUMERIC_NAMES else None


def _mask_index_names(text: str) -> str:
    """이름 속 숫자를 같은 길이의 공백으로 지운다. 길이를 유지하는 이유는
    호출부가 창을 문자 위치로 자르기 때문이다."""
    if _NUMERIC_NAME_RE is None:
        return text
    return _NUMERIC_NAME_RE.sub(lambda m: " " * len(m.group(0)), text)


def _citation_numbers(
    window: str, n: int, own: set[float], report_nums: set[float]
) -> list[tuple[str, str]]:
    """규칙 9 (`citation_num`) — 인용에 붙은 숫자가 **그 인용의 숫자**인가.

    2026-08-03 발행 사고: 주간 시작 브리핑이

        `KOSPI 가 F45 에 기록된 전일대비 26.81% 급등과 함께 …`

    를 `status=ok`로 내보냈다. F45는 KOSPI(`^KS11`, 전일대비 +17.91%)이고
    26.81%는 **삼성전자**의 등락률이다. 규칙 6은 26.81이 리포트 어딘가에
    있다는 이유로 통과시켰고(있긴 하다, 다른 행에), 규칙 8은 이름·종류어만
    보고 숫자는 보지 않았다. 즉 접지 검사에 "그 숫자가 누구 것인가"라는
    축이 통째로 비어 있었다.

    막는 조건을 좁게 잡는다 — 창 안의 숫자가
      (1) 그 행 자신의 숫자가 아니고,
      (2) 리포트의 **다른** 곳에는 있는(= 규칙 6이 통과시킬) 숫자이며,
      (3) 그 행이 자기 숫자를 하나라도 갖고 있을 때
    만 위반이다. (2)를 요구하는 이유: 접지 자체가 안 되는 숫자는 이미 규칙 6이
    잡으므로 여기서 또 잡으면 위반만 두 번 세는 것이고, 리포트에 없는 어림수
    (`목표 2%`)까지 이 규칙이 떠안으면 오탐 발생기가 된다. (3)은 공시 행처럼
    숫자가 없는 행에서 창 안 숫자를 전부 거절하는 것을 막는다.

    못 막는 것은 그대로다: F-번호를 인용하지 않은 귀속 오류(strict xfail이
    지키는 그 구멍)와, 창(40자·절 경계) 밖으로 나간 숫자."""
    if not own:
        return []
    window = _mask_index_names(window)
    out: list[tuple[str, str]] = []
    for _m, tok, value, decimals, has_unit, _uc, _latin in _numeric_tokens(window):
        if decimals == 0 and abs(value) <= _SMALL_INT_EXEMPT_MAX and not has_unit:
            continue  # 규칙 6과 같은 면제: 개수 세는 말("2개 분기")
        if _grounded_in(value, decimals, own):
            continue
        # 부호를 뒤집은 재표현(`-0.21%`를 `0.21% 하락`)도 그 행의 숫자다.
        if _grounded_in(-value, decimals, own):
            continue
        if _grounded_in(value, decimals, report_nums):
            out.append(("citation_num", f"F{n} {tok}"))
    return out


def _unit_class(after: str) -> str | None:
    """Classify what follows a numeric token: a named unit class, the
    catch-all `"other"`, or `None` when nothing is attached."""
    m = _KNOWN_UNIT_RE.match(after)
    if m:
        return _UNIT_CLASSES[m.group(1)]
    return "other" if _OTHER_UNIT_RE.match(after) else None


def _numeric_tokens(text: str):
    """Yield (match, raw_token, value, decimal_places, followed_by_unit,
    unit_class, latin_suffix) for every numeric token in `text`."""
    for m in _NUM_RE.finditer(text):
        tok = m.group(0)
        cleaned = tok.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        decimals = len(cleaned.split(".")[1]) if "." in cleaned else 0
        trailing = text[m.end() : m.end() + 8]
        has_unit = bool(_UNIT_RE.match(trailing))
        latin = _LATIN_SUFFIX_RE.match(trailing)
        yield (
            m, tok, value, decimals, has_unit, _unit_class(trailing),
            latin.group(0) if latin else "",
        )


def _direction_near(text: str, start: int, end: int) -> str | None:
    """`"down"` / `"up"` / `None` — whichever direction word sits closest to
    the token within `_DIRECTION_WINDOW` characters on either side."""
    window_start = max(0, start - _DIRECTION_WINDOW)
    window = text[window_start : end + _DIRECTION_WINDOW]
    best: tuple[int, str] | None = None
    for regex, label in ((_DOWN_RE, "down"), (_UP_RE, "up")):
        for m in regex.finditer(window):
            pos = window_start + m.start()
            distance = start - pos if pos < start else pos - end
            if best is None or distance < best[0]:
                best = (distance, label)
    return best[1] if best else None


def _occurrences(report: dict) -> list[tuple[float, str | None]]:
    """Every number the report states, paired with the unit class written
    next to it — the grounding set of SA-5 rule 6, plus the unit dimension
    rule 7 needs. A row's `raw_value` inherits the unit of its own `value`
    string (`"1,442.07 USD"` -> usd), since that float IS that string."""
    found: list[tuple[float, str | None]] = []

    def scan(text: str) -> str | None:
        first: str | None = None
        for i, (_m, _tok, value, _dec, _has_unit, unit_cls, _latin) in enumerate(_numeric_tokens(text)):
            found.append((value, unit_cls))
            if i == 0:
                first = unit_cls
        return first

    scan(str(report.get("headline") or ""))
    for key in ("facts", "market_reaction"):
        for row in report.get(key) or []:
            row_unit = scan(str(row.get("value") or ""))
            scan(str(row.get("comparison") or ""))
            scan(str(row.get("label") or ""))
            raw = row.get("raw_value")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                found.append((float(raw), row_unit))
    for key in ("events", "schedule_changes"):
        for row in report.get(key) or []:
            scan(str(row.get("when") or ""))
            scan(str(row.get("name") or ""))
    scan(str(report.get("report_date") or ""))
    scan(str(report.get("cutoff_kst") or ""))
    return found


def allowed_numbers(report: dict) -> set[float]:
    """SA-5 rule 6's grounding set: headline + facts[]/market_reaction[]'s
    label/value/comparison/raw_value + events[]/schedule_changes[]'s
    when/name + report_date + cutoff_kst."""
    return {value for value, _unit in _occurrences(report)}


def allowed_dates(report: dict) -> set[str]:
    """SA-5 rule 5's grounding set."""
    dates = {report.get("report_date") or "", (report.get("cutoff_kst") or "")[:10]}
    for key in ("events", "schedule_changes"):
        for row in report.get(key) or []:
            when = row.get("when") or ""
            dates.add(when[:10])
    dates.discard("")
    return dates


def check(report: dict, text: str) -> list[tuple[str, str]]:
    """Run all 6 SA-5 rules against `text`. Returns every violation found
    (kind, offending_token) — empty list means the field passes."""
    violations: list[tuple[str, str]] = []

    seen_cjk: set[str] = set()
    for m in _CJK_RE.finditer(text):
        if m.group(0) not in seen_cjk:
            seen_cjk.add(m.group(0))
            violations.append(("cjk", m.group(0)))

    for marker in _FORMAT_MARKERS:
        if marker in text:
            violations.append(("format", marker))

    if len(text) > _MAX_LEN:
        violations.append(("length", str(len(text))))

    magnitudes_ok = allowed_magnitudes(report)
    for m in _KO_MAGNITUDE_RE.finditer(text):
        if _magnitude_token(m.group(0)) not in magnitudes_ok:
            violations.append(("ko_magnitude", m.group(0)))

    for pattern, name in _BANNED_PATTERNS:
        m = pattern.search(text)
        if m:
            violations.append((f"banned:{name}", m.group(0)))

    dates_ok = allowed_dates(report)
    for m in _DATE_RE.finditer(text):
        if m.group(0) not in dates_ok:
            violations.append(("date", m.group(0)))

    occurrences = _occurrences(report)
    nums_ok = {value for value, _unit in occurrences}
    # Blank the dates out *in place* (same length) so rule 6c's direction
    # window still lines up with the caller's original string.
    text_no_dates = _DATE_RE.sub(lambda m: " " * len(m.group(0)), text)
    for m, tok, value, decimals, has_unit, unit_cls, latin in _numeric_tokens(text_no_dates):
        if latin and latin not in _ALLOWED_LATIN_SUFFIXES:
            violations.append(("latin_unit", tok + latin))

        if decimals == 0 and abs(value) <= _SMALL_INT_EXEMPT_MAX and not has_unit:
            continue  # counts ("7종목", "2개 분기") need no grounding.

        # 정수 인용은 반올림을 허용하지 않는다. `round(15.3, 0) == 15`라는
        # ±0.5 허용 때문에 `실업률 15%`(실제 15.3)와 `S&P500 18% 상승`(실제
        # 17.9)이 통과했다(final-review.md 미탐 A4/A5). 정수로 깎으면 남는
        # 정보가 너무 적어 "리포트가 말한 그 숫자"라고 볼 수 없다. 소수 인용의
        # 반올림(`약 4.7%` <- 4.68)은 그대로 둔다 — 좁히면 정당한 표현이 죽는다.
        def _grounded(allowed: float) -> bool:
            if decimals == 0:
                return allowed == value
            return round(allowed, decimals) == round(value, decimals)

        if not any(_grounded(allowed) for allowed in nums_ok):
            signed = tok[0] in "+-"
            mirrored = any(round(allowed, decimals) == round(-value, decimals) for allowed in nums_ok)
            # `0.21% 하락` for a report that says `-0.21%` is correct Korean,
            # not a hallucination — but only when the sentence actually says
            # "down". An explicit sign is never reinterpreted: `+0.21%`
            # against `-0.21%` is the irreversible failure this whole
            # validator exists for.
            if not signed and mirrored and _direction_near(text_no_dates, m.start(), m.end()) == "down":
                continue
            violations.append(("sign" if (not signed and mirrored) else "num", tok))
            continue

        if unit_cls in _CURRENCY_CLASSES:
            # Only an exact-value citation pins down which occurrence is
            # being quoted, and therefore which unit belongs to it; a rounded
            # one (`1,442원` for 1,442.07) could be any of several, so it is
            # left alone. `None` = the report states that number bare, which
            # contradicts nothing.
            exact = {unit for value_, unit in occurrences if value_ == value}
            if exact and None not in exact and unit_cls not in exact:
                written = _KNOWN_UNIT_RE.match(text_no_dates[m.end() : m.end() + 8])
                violations.append(("unit", tok + (written.group(1) if written else "")))

    violations.extend(_attribution(report, text))

    return violations


def _main(argv: list[str]) -> int:
    """`python -m market_intel.interp.validate <report.json>` — re-checks
    the 4 `Interpretation` fields a report file already carries against its
    own fact set, independent of whatever produced them. Exists for spec
    ST2's success-criteria step (2): fields the validator already accepted
    once (during `apply.fill`) must still validate clean on a second, cold
    pass — a field that only ever gets checked by the same code path that
    approved it in the first place is not really being checked."""
    import json
    import sys

    if len(argv) != 1:
        print("usage: python -m market_intel.interp.validate <report.json>", file=sys.stderr)
        return 2
    report = json.loads(open(argv[0], encoding="utf-8").read())
    interp = report.get("interpretation", {})
    ok = True
    for field in ("reading", "counter_reading", "thesis_impact", "next_check"):
        text = interp.get(field, "") or ""
        if not text:
            print(f"{field}: (empty, skipped)")
            continue
        violations = check(report, text)
        status = "PASS" if not violations else "FAIL"
        if violations:
            ok = False
        print(f"{field}: {status} {violations}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
