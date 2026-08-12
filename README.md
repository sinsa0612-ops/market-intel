# market-intel

시장 데이터(미국·한국 가격 28종목군·수급·공시/실적·거시지표)를 무인(non-interactive)으로
수집해, revision을 덮어쓰지 않는 point-in-time SQLite DB에 멱등 저장하는 수집 엔진.

## 이 시스템이 하는 일과 하지 않는 일 (범위 — CEO 확정 2026-08-12)

> **"지금 무엇이 앞서고 있고, 그 판단의 근거와 한계는 무엇인가"를 정직하게 보여주는
> 데까지가 이 시스템의 범위다.**

이 한 줄이 앞으로의 설계 결정을 가른다. 새 기능을 넣을지 말지 헷갈리면 여기로 돌아온다.

**한다**
- 지금 무엇이 시장을 앞서고 있는지 **재서 보여준다**(상대강도·시장 폭·수급).
- 그 판단이 **무엇을 근거로 했고 그 근거가 언제 것인지** 밝힌다(가설 판정의 `근거 <날짜>`).
- 그 판단이 **무엇을 못 보는지** 밝힌다(결측·표본 한계·"시장이 아니라 표본"·근사치 표시).
- 사전등록된 가설을 반증 조건으로 채점하고, **조건이 실제로 발화 가능한지 감사한다**
  (`thesis audit`).

**하지 않는다**
- **예측**하지 않는다. "다음에 뜰 것"을 지목하지 않는다 — 18종목 × 16업종 × 여러 기간을
  훑으면 가장 좋아 보이는 하나는 우연히도 나온다(다중검정). 사외 고문 2인 독립 권고,
  2026-08-12.
- **매매 권유·목표가·비중 조언을 하지 않는다.** AI 해석 검증기 규칙 4가 이것을 문장
  수준에서 막는다.
- **승자를 강조하지 않는다.** 순위를 보여주더라도 하이라이트하지 않는다 — 강조하는 순간
  문구가 무엇이든 "이거 사라"로 기능한다.

**정직하게 남기는 한계**

> ⚠️ **이 시스템은 모멘텀 추종이다.** 상대강도(무엇이 시장을 앞서는가)와 그 지속을
> 재는 것은 모멘텀의 정의 그 자체다. 이것은 결함이 아니라 CEO 목표("흐름에 몸을
> 맡긴다")와 정합하는 선택이지만, **숨기면 안 되는 성질**이다 — 모멘텀의 알려진
> 최대 실패는 **주도가 끝나는 전환점의 급락**이고, 이 설계는 그것을 완화하지
> **못한다**. 더 나쁜 것은, 그 사건이 우리가 **한 번도 관측하지 못한** 바로 그
> 사건이라는 점이다: 보유 데이터에는 주도가 **시작된** 전환(2022)만 있고 **끝난**
> 전환은 0회다. 즉 **시스템의 최대 미검증 리스크 = 모멘텀의 알려진 최대 리스크**다.
> (사외 고문 2인 독립 확인, 2026-08-12)

- 흐름을 따르는 측정은 **방향이 꺾이는 순간 가장 크게 틀린다.** 설계로 없앨 수 있는 것이
  아니라 방식에 내재한 성질이므로, 숨기지 말고 화면에 밝힌다.
- 순위를 읽는 사람의 추격 충동은 설계로 제거되지 않는다. 줄이는 것이지 없애는 게 아니다.
- **2021~2024 구간은 소진됐다.** 2026-08-12에 그 구간으로 문턱을 시험했고(문턱은 그
  전에 고정했으므로 시험 자체는 정당했다), 같은 날 조건이 32 -> 39개로 늘었다. 앞으로
  그 구간을 보고 조건이나 문턱을 손대면 곡선맞춤이다. **이후 유효한 검증은 사전등록
  워크포워드뿐이다** — 즉 규칙을 먼저 적고 앞으로 오는 데이터로만 채점한다.
- 그때 한 시험을 **"홀드아웃"이라 부르지 않는다 — "역사적 백캐스트"다.** 2024 구간이
  개발 기간과 겹치고, "AI 이전/주도기"라는 구분 자체가 사후 지식이며, 발화 16일은
  독립표본 16개가 아니라 **연속구간 4개**였다(실측). 결과를 체제 식별 성공의 증거로
  쓰지 말 것.

## 5분 시작법

**전제:** [uv](https://docs.astral.sh/uv/)가 설치되어 있어야 합니다 (`brew install uv` 등).
파이썬은 uv가 자동으로 3.12를 받아 씁니다 — 직접 설치할 필요 없습니다.

```bash
# 1) 프로젝트 루트로 이동
cd market-intel

# 2) DB 초기화 (스키마 + append-only 트리거 생성, 여러 번 실행해도 안전)
uv run market-intel init

# 3) (선택) 키가 필요한 3종 provider — FRED/ECOS/DART — 를 쓰려면 .env에 키를 채웁니다.
#    .env.example을 복사해 .env로 저장한 뒤 값을 채우세요. 키가 비어 있으면 해당
#    provider는 네트워크 호출 없이 NO_DATA(키없음)로 표시되고, 나머지는 정상 수집됩니다.
cp .env.example .env
#   MI_FRED_API_KEY=...   https://fred.stlouisfed.org/docs/api/api_key.html
#   MI_ECOS_API_KEY=...   https://ecos.bok.or.kr/api/
#   MI_DART_API_KEY=...   https://opendart.fss.or.kr/
#   MI_SEC_USER_AGENT=... SEC EDGAR가 요구하는 식별용 User-Agent ("이름 이메일" 형식)

# 4) 수집 실행 — 워크플로는 morning(개장 전: 가격+공시+거시) / close(마감 후:
#    가격+수급+거시) / all(전부) 중 선택
uv run market-intel collect --workflow morning

# 5) 결과 확인 (facts_total, provider별 건수, Core 16 커버리지 등 고정 포맷 출력)
uv run market-intel db stats
```

## 명령어

- `market-intel init` — DB(`var/market_intel.db`) 생성. 이미 있으면 무해.
- `market-intel collect --workflow morning|close|all [--cutoff ISO8601]` — 워크플로 실행.
  `--cutoff`는 실행 기록용 시각(미지정 시 현재 시각)이며 수집 자체를 필터링하지 않습니다.
- `market-intel db stats` — 누적 수집 현황 요약.
- `market-intel ops status [--json]` — **지금 파이프라인이 살아 있는지** 한 화면으로.
  job별 마지막 실행 시각·결과·밀린 실행 수, 마지막 수집의 provider별 상태, 마지막 AI 해석,
  미해결 결측. 같은 내용이 사이트 `docs/status.html`에도 발행됩니다.
- `market-intel thesis load|list|review` — 가설 원장(`theses/theses.json`) 적재/조회/판정.
- `market-intel interpret --file <report.json>` — 리포트 하나에 AI 해석 4칸을 채웁니다.
  기본은 **Codex CLI(`gpt-5.6-luna`, 추론 수준 `max`)**, 실패하면 **Claude Code
  CLI(haiku)** -> 로컬 ollama 순으로 한 단씩 자동 폴백합니다
  (`--model claude:sonnet` / `--model qwen3.5:9b`로 직접 고를 수 있고, 모델 이름이 곧
  백엔드입니다). `--no-llm`이면 모델을 부르지 않고, 셋 다 죽어 있어도 해석만 비운 채
  종료코드 0입니다. 실제로 무엇이 썼는지는 `ops status`의 `model=`에 남습니다 —
  `gpt:gpt-5.6-luna`를 기대했는데 다른 이름이 보이면 폴백이 일어난 것입니다.
  추론 수준은 `MI_GPT_REASONING_EFFORT`로 바꿀 수 있습니다(서버가 인정하는 값:
  `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`).

리포트·사이트·자동 실행 명령(`report` / `site build` / `obsidian sync` / `job run` /
`publish`)과 시간표는 `launchd/README.md`에 있습니다.

## 워크플로 구성

| 워크플로 | provider |
|---|---|
| morning | yfinance, sec_edgar, sec_edgar_13f, fred |
| close | yfinance, pykrx, ecos, dart |
| all | 위 7종 전부 |

## 데이터 모델 (요약)

- `raw_snapshots` — provider가 실제로 받아온 원본 응답(대용량은 `var/raw/`에 gzip 오프로드).
- `fact_revisions` — 정규화된 사실. **append-only**(DB 트리거가 UPDATE/DELETE를 막습니다) —
  값이 바뀌면 새 revision을 추가하고 이전 값은 그대로 남습니다.
- `facts_as_of(conn, cutoff, **filters)` — 특정 시점(`known_at <= cutoff`)에 알려져 있던
  값만 조회하는 point-in-time 함수.
- 모든 fact는 데이터 상태를 답니다: `source_verified`(원자료확인) / `reconstructed`(복원완료,
  예: FCF=영업현금흐름−CAPEX 같은 파생값) / `partial`(부분확인, 예: 기업이 몇 년째 쓰지 않는
  XBRL 태그에서 나온 오래된 수치) / `unverified`(미확인). 못 가져온 항목은 조용히 넘어가지 않고
  `provider_runs.safe_detail`에 사유가 남습니다.

## 무인 실행 (cron)

```cron
# 평일 아침 8시(KST)에 morning 워크플로 실행 예시
0 8 * * 1-5 /opt/homebrew/bin/uv run --project /path/to/market-intel market-intel collect --workflow morning >> /path/to/market-intel/var/logs/cron.log 2>&1
```

DB·raw·로그 위치는 **프로젝트 루트 기준 절대경로**로 잡히므로, cron이 어느 디렉터리에서
실행하든 같은 DB에 씁니다(엉뚱한 위치에 빈 DB가 생겨 "수집 성공, 0건"을 반복하는 사고 방지).
바꾸고 싶으면 `MI_DB_PATH` / `MI_RAW_DIR` / `MI_LOG_DIR`로 명시 지정하세요.
사람 입력(프롬프트·승인)을 요구하는 코드는 없으며, `tests/test_cli_subprocess.py`가
stdin을 닫은 채 실행해 이를 검증합니다.

## 보안

- API 키는 `.env`에서만 읽습니다(커밋 금지, `.gitignore`에 이미 포함). 저장되는 URL과
  로그는 키를 항상 `***`로 마스킹합니다(쿼리 파라미터·URL 경로 세그먼트 모두 — ECOS는
  키가 경로에 들어가는 방식이라 특히 주의가 필요합니다).
- 키가 비어 있는 provider는 **네트워크 호출을 아예 하지 않고** `NO_DATA/키없음`을
  반환합니다 — 추정치나 가짜 값을 채우지 않습니다.
- 감사 명령: `uv run python secret_leak_check.py` — `.env`의 모든 값이 소스·DB·raw(gzip
  압축분은 압축을 풀어 원문까지 확인)·로그 어디에도 남아 있지 않은지 검사합니다(값은 절대
  출력하지 않고 건수만 보고, 종료코드로 판정).

## 개발/테스트

```bash
uv sync --extra dev
uv run pytest -q              # 전체(네트워크 스모크 포함)
uv run pytest -q -m "not network"   # 오프라인만
```

## 더 알아보기

미검증 [ASSUMPTION] 목록, 알려진 데이터 갭(PMI 등), 다음 작업은 `HANDOFF.md` 참고.
