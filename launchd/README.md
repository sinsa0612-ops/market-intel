# launchd 자동 실행 (설치 절차)

이 폴더의 `*.plist.template` 9개가 market-intel의 무인 실행 시간표다.
`<REPO_ROOT>` 자리를 실제 경로로 바꿔서 `~/Library/LaunchAgents/`에 넣으면 등록된다.

**이 저장소의 어떤 자동화도 launchd에 스스로 등록하지 않는다.** 등록은 사람이 한다.

## 시간표 (이 맥의 로컬 시간 = KST)

| 시각 | 요일 | job | 하는 일 |
|---|---|---|---|
| 06:50 | 월~금 | `collect-am` | `morning` + `calendar` + `events` 수집 |
| 07:40 | 월 | `weekstart` | `week_start` 리포트 (차단선 07:15) |
| 07:40 | 화~금 | `morning` | `morning` 리포트 (차단선 07:15) |
| 15:50 | 월~금 | `collect-pm` | `close` 수집 |
| 16:15 | 월~금 | `close` | `close_delta` 리포트 (차단선 16:15) |
| 08:00 | 토 · 매월 1일 | `collect-full` | `all` 수집 |
| 08:30 | 토 | `weekly` | `weekly_review` 리포트 |
| 08:30 | 매월 1일 | `monthly` | `monthly` 리포트 |
| 13:00 · 22:00 | 매일 | `eventwatch` | `events` 수집 (공시·실적 감시) |

**수집이 왜 리포트보다 먼저인가.** 리포트의 정보차단선은 명세가 못박은 고정값이다
(morning 07:15, close_delta 16:15). 수집이 차단선 뒤에 돌면 그날 모은 사실이 전부
차단선 밖이라 리포트가 매일 빈 채로 나온다. 그래서 수집 job과 리포트 job을 분리하고
수집을 차단선보다 앞에 뒀다. 실측 소요시간은 `collect-am` 38초, `collect-pm` 21초,
`collect-full` 50초 — 각각 24분 이상 여유가 있다.

## 설치

```bash
for f in ~/dev/market-intel/launchd/*.plist.template; do
  b=$(basename "$f" .template)
  sed "s#<REPO_ROOT>#$HOME/dev/market-intel#g" "$f" > ~/Library/LaunchAgents/"$b"
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"$b"
done
```

확인:

```bash
launchctl list | grep market-intel
```

9줄이 나오면 성공이다.

## 한 번 직접 돌려보기

```bash
launchctl kickstart -k gui/$(id -u)/com.kangtaeklee.market-intel.morning
tail -20 ~/dev/market-intel/var/logs/job-morning-$(date +%Y%m%d).log
```

로그에 `job=morning lock=acquired` … `exit=0`이 보이면 성공이다.

## 제거

```bash
for f in ~/Library/LaunchAgents/com.kangtaeklee.market-intel.*.plist; do
  launchctl bootout gui/$(id -u) "$f"
  rm "$f"
done
```

## 알아둘 것

- **중복 기동 방지는 파이썬 `fcntl.flock`** (`var/locks/<job>.lock`). macOS에는 `flock(1)`도
  `timeout(1)`도 없어서 셸로는 못 한다. 같은 job이 겹치면 두 번째는 `lock=already_running`을
  찍고 종료코드 0으로 조용히 빠진다.
- **맥이 자거나 꺼져 있어서 놓친 실행**은 두 겹으로 복구된다. (1) launchd는
  `StartCalendarInterval` job을 깨어난 직후 한 번 돌려준다. (2) 그와 별개로 모든 리포트 job은
  시작할 때 자기 슬롯의 최근 7일을 점검해서 빠진 날짜를 **그날의 차단선으로** 소급 생성하고
  `지연 생성` 배지를 단다. 차단선을 현재 시각으로 바꾸지 않으므로 후견지명이 섞이지 않는다.
  7일보다 오래 꺼져 있었다면 그 구간은 영구 결측이고, 아카이브에 빈 날짜로 드러난다.
- **AI 해석 단계**(2단계-B)가 리포트 job의 `report`와 `site` 사이에 들어간다. 리포트 한 건당
  약 25~40초가 더 걸린다(로컬 ollama, qwen3.5:9b 기준). 캐치업이 밀려도 한 번에 최대
  `MI_INTERP_MAX_PER_RUN`건(기본 3)만 해석하므로 다음 job과 겹치지 않는다 — job 간격은
  최소 50분이다.
- **ollama는 `homebrew.mxcl.ollama`로 상시 기동**되어 있다. 안 떠 있으면 해석만 실패하고
  리포트·사이트는 그대로 발행된다(`steps: … interpret=fail site=ok …`, 종료코드 0).
  그때 리포트 본문의 해석 칸은 `AI 해석 미생성`으로, 사이트 `docs/status.html`의
  `마지막 AI 해석`은 `llm_unavailable`로 보인다.
- **파이프라인이 살아 있는지 보는 곳은 사이트다.** `docs/status.html`에 job별 마지막 실행
  시각·결과·밀린 실행 수가 나온다. 페이지 맨 위의 `이 페이지 생성 시각`이 며칠 전이면
  자동 실행 자체가 멈춘 것이다. 터미널에서는 `uv run market-intel ops status`가 같은 내용을
  찍는다.
- 로그는 `var/logs/job-<job>-YYYYMMDD.log` (파이프라인 출력)와
  `var/logs/launchd-<job>.log` (launchd 자체 출력) 두 갈래다.
- 시각은 전부 이 맥의 로컬 시간 기준이다. 시간대를 바꾸면 시간표도 따라 움직인다.
