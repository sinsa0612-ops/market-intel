# 가설 원장 (`theses.json`) 작성법

이 파일은 **CEO가 소유**한다. LLM은 가설을 만들지도, 판정하지도 않는다
(명세 §13.2·2단계-B BRIEF 규칙 4). 사람이 가설과 반증 조건을 한 번 쓰면,
기계가 매일 그 조건을 결정론적 규칙으로 채점한다.

> **이 파일은 공개된다.** `docs/theses.html`을 통해 사이트에 그대로 노출된다
> (`publish.sh`가 `docs/`를 커밋하고, 사이트는 DB의 `theses` 테이블을 읽는다).
> 공개되면 곤란한 내용(비공개 정보, 특정인 비방 등)을 가설 문장에 쓰지 말 것.

## 상한

- 테마는 정확히 5개로 고정: `ai_semi`(AI·반도체) · `power_energy`(전력·에너지) ·
  `fin_credit`(금융·신용) · `consumer_cycle`(소비·경기) · `policy_geo`(정책·지정학).
- 테마당 가설 **최대 3개**(`slot` 1~3). DB 스키마가 `CHECK`+`UNIQUE`로 물리적으로
  강제한다 — 16번째 가설은 애초에 저장될 수 없다.
- 가설이 없는 테마는 `"theses": []`로 둔다.

## 가설 1개의 필수 필드

```jsonc
{
  "id": "ai_semi_1",              // thesis_id. 파일 안에서 고유.
  "slot": 1,                      // 1~3, 같은 테마 안에서 중복 불가.
  "statement": "...",             // 목표주가·매매판단 문구 금지.
  "leading_indicators": ["MSFT free_cash_flow"],  // 최소 1개.
  "next_check_date": "2026-11-01",                // ISO 날짜, 필수.
  "conditions": {
    "falsify":    [ /* 전부 TRUE -> 무효. 최소 1개 필수 — 없으면 적재 자체를 거부한다 */ ],
    "weaken":     [ /* 하나라도 TRUE -> 약화 */ ],
    "strengthen": [ /* 하나라도 TRUE -> 강화 */ ]
  }
}
```

`falsify` 조건이 0개인 가설은 **가설이 아니다**(명세 §2.2) — `thesis load`가
파일 전체를 거부하고 exit 2로 끝난다. 부분 적재는 없다.

## 조건 원자(atom) — 닫힌 4종

| kind | 뜻 | 필드 |
|---|---|---|
| `threshold` | 최신 관측이 조건을 만족하는가 | `subject`,`metric`,`op`,`value` |
| `change_pct` | N구간 전 대비 변화율 | `subject`,`metric`,`op`,`value`,`lookback` |
| `consecutive` | 최근 N구간 연속 같은 방향 | `subject`,`metric`,`direction`(`up`\|`down`),`periods` |
| `stale` | 마지막 관측이 N일보다 오래됐는가 | `subject`,`metric`,`days` |

`op`은 `> < >= <= == !=` 중 하나. `category`(`price`\|`macro`\|`financials`)는
선택이지만, subject+metric이 여러 카테고리에 걸칠 수 있는 경우 명시를 권장한다.

**관측이 부족하면 `UNKNOWN`이다** — 지금 수집되는 데이터는 재무가
(기업,지표)당 1~2개, 거시가 1개뿐이라 `consecutive`/`change_pct`처럼 여러 구간을
요구하는 조건은 당분간 대부분 `UNKNOWN`으로 나온다. 이건 버그가 아니라 정직한
상태다(§3.3) — 근거 없이 `유지`로 적지 않는다. 평가 가능한 원자가 하나도 없으면
가설 전체 판정이 `판정 불가`가 된다.

## 판정 5종과 순서

1. 모든 원자가 `UNKNOWN` → **판정 불가**
2. `falsify` 전부 `TRUE` → **무효**
3. `weaken` 중 하나라도 `TRUE` → **약화**
4. `strengthen` 중 하나라도 `TRUE` → **강화**
5. 그 외 → **유지**

## CLI

```bash
uv run market-intel thesis load [--file theses/theses.json] [--check]
uv run market-intel thesis list
uv run market-intel thesis review --file reports/<type>/<date>.json [--dry-run]
```

`--check`는 검증만 하고 DB에 쓰지 않는다. `thesis review`는 그 리포트의
`cutoff_utc` 이전에 알려진 사실만 근거로 삼는다(정보 차단선) — 같은 사실도
리포트에 따라 `UNKNOWN`이었다가 나중에 판정 가능해질 수 있다.
