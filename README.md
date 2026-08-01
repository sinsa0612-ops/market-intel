# market-intel

시장 데이터(미국·한국 가격 28종목군·수급·공시/실적·거시지표)를 무인(non-interactive)으로
수집해, revision을 덮어쓰지 않는 point-in-time SQLite DB에 멱등 저장하는 수집 엔진.

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
