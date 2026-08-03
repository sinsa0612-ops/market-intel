"""거시 백필 — 미국 3년(FRED) · 한국 제한적(ECOS). (spec 백필 §5 ST2 항목 2·3)

한 모듈이 두 소스를 맡는다(`SOURCES` 레지스트리가 둘 다 여기를 가리킨다).

**미국과 한국의 신뢰 등급이 다르다.** 그 차이가 이 파일의 핵심이다:

- FRED(ALFRED)는 **빈티지를 준다.** 응답 행마다 `realtime_start`가 붙어 있어
  "이 값이 언제부터 유효했는지"를 소스가 직접 말해 준다. 그래서 known_at을
  추정하지 않고 받아 적으면 되고, 등급은 `reconstructed`(복원 완료)다.
- ECOS는 **빈티지 기능이 아예 없다**(spec §2.4 응답 필드 전수 확인). 그래서
  known_at을 API가 아니라 **계열의 성질**로 정할 수밖에 없다. 개정되지 않는
  두 계열(정책금리·고시환율)만 백필하고, 등급을 `partial`(부분 확인)로 낮춘다.
  개정되는 수출 통계는 손대지 않고 `data_gaps`에 남긴다 — 조용히 빼면 "원래
  안 보는 항목"으로 읽힌다.

FRED 요청에서 `realtime_start=1776-07-04`(전체 빈티지)를 **쓰면 안 된다**.
일간 4계열에서 HTTP 400으로 죽는다 — 한 요청이 돌려줄 수 있는 vintage가 2000개로
제한돼 있고 DGS10 같은 일간 계열은 3년치가 그것을 넘는다(spec §2.3 반증 5).
`realtime_start`를 백필 시작일로 묶으면 그 창 안의 판만 오므로 통과한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db as db_mod
from ..engine import _fact_id
from ..http_client import SafeHttp
from ..models import FactCandidate, RawItem
from ..providers.ecos import BASE_URL as ECOS_BASE_URL
from ..providers.ecos import STAT_CODES, _time_to_event_at
from ..providers.fred import OBSERVATIONS_URL, SERIES, SERIES_URL

KR_TZ = ZoneInfo("Asia/Seoul")

# S3: ECOS는 빈티지를 안 주므로 known_at을 계열 성질로 정한다. **보수적 방향
# 으로만** 정한다 — 실제보다 늦게 알려진 것으로 잡으면 사실이 늦게 보일 뿐이고,
# 후견지명은 절대 생기지 않는다. 반대 방향은 정보차단선이 새는 것이라 회복 불가다.
ECOS_BACKFILL_KEYS = ("base_rate", "usd_krw_fx")
ECOS_SKIPPED_KEYS = ("exports_total", "semiconductor_exports")
ECOS_GAP_REASON = (
    "ECOS는 빈티지(과거 시점의 판)를 제공하지 않는다 — 개정되는 계열은 "
    "'그때 알 수 있었던 값'을 복원할 방법이 없어 백필하지 않는다. "
    "통계 코드도 미확정(INFO-200)이다."
)
# 한국 장 마감. 매매기준율은 당일 아침 고시되고 개정되지 않으므로 15:30은 보수적.
KR_FX_KNOWN_LOCAL = time(15, 30)


# FRED가 명시적으로 허용하는 "실시간 최대" 날짜. 문서가 아니라 서버가 알려준
# 문자열이다 — 400 본문: "Variable realtime_end can not be after today's date
# (2026-08-02) unless it's equal to the real-time max date 9999-12-31."
FRED_REALTIME_MAX = "9999-12-31"


def _fred_realtime_end(until: date) -> str:
    """`until`이 FRED의 오늘보다 뒤면 400이 난다.

    실측 2026-08-03: 우리 시계로 오늘(`--until` 기본값)을 그대로 보냈더니 FRED가
    "오늘은 2026-08-02"라며 **11개 계열 전부 400**을 돌려줬다. 서버가 미국
    중부시간을 쓰므로 KST 기준 오늘은 FRED에게 늘 미래다. 시차를 우리가 흉내내
    맞추려 들면(어제로 빼기 등) 서머타임마다 다시 깨지므로, 서버가 직접 알려준
    해법을 쓴다 — 미래 날짜 대신 실시간 최대값을 보낸다. 과거 시점으로 끊어
    보고 싶을 때(`--until 2025-01-01`)는 그 날짜가 그대로 간다."""
    return until.isoformat() if until < date.today() else FRED_REALTIME_MAX


def _default_http(settings):
    return lambda name: SafeHttp(name, settings)


def run(conn, settings, source: str, *, since: date, until: date,
        subjects=None, dry_run: bool = False, http=None):
    """`http`는 테스트가 `MockTransport`를 밀어 넣는 자리다. 레지스트리 계약
    (`SOURCES`)은 이 인자를 주지 않으므로 기본값이 실제 클라이언트다."""
    http = http or _default_http(settings)
    if source == "macro_us":
        return _run_us(conn, settings, since, until, subjects, dry_run, http)
    return _run_kr(conn, settings, since, until, subjects, dry_run, http)


# --- 미국 (FRED / ALFRED) ---------------------------------------------------

def _run_us(conn, settings, since: date, until: date, subjects, dry_run: bool, http):
    from . import BackfillResult

    api_key = settings.fred_api_key
    if not api_key:
        # 키가 없으면 네트워크를 두드리지도 않는다(라이브 provider와 같은 규율).
        return BackfillResult(source="macro_us", status="NO_DATA", reason_code="키없음")

    client = http("fred")
    series_ids = [s for s in SERIES if subjects is None or s in subjects]
    fetched = appended = skipped = 0
    missing: list[str] = []
    unit_cache: dict[str, str] = {}

    realtime_end = _fred_realtime_end(until)
    for series_id in series_ids:
        params = {
            "series_id": series_id, "api_key": api_key, "file_type": "json",
            "observation_start": since.isoformat(),
            # 전체 빈티지를 요구하지 않는다 — 2000 vintage 상한에 걸려 400이 난다.
            "realtime_start": since.isoformat(),
            "realtime_end": realtime_end,
            "sort_order": "asc",
        }
        try:
            resp = client.get(OBSERVATIONS_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{series_id}:error:{exc.__class__.__name__}")
            continue
        if resp.status_code != 200:
            missing.append(f"{series_id}:http_{resp.status_code}")
            continue
        payload = resp.text
        try:
            observations = json.loads(payload).get("observations", [])
        except json.JSONDecodeError:
            missing.append(f"{series_id}:bad_json")
            continue
        if not observations:
            missing.append(f"{series_id}:no_observations")
            continue

        unit = _unit_of(client, series_id, api_key, unit_cache, missing)
        safe_url = client.safe_url(str(resp.request.url))
        snapshot_id = None
        if not dry_run:
            snapshot_id = db_mod.insert_raw_snapshot(
                conn, settings.raw_dir, "fred",
                RawItem(external_id=f"backfill:{series_id}:{since}:{until}",
                        source_published_at=until.isoformat(),
                        safe_source_url=safe_url, payload=payload, fetch_status="ok"),
            )

        for obs in observations:
            obs_date = obs.get("date") or ""
            value_str = obs.get("value")
            # FRED는 결측을 `.`로 준다. 0으로 채우면 거짓말이고 float()는 죽는다.
            if not obs_date or value_str in (".", None, ""):
                continue
            try:
                value_num = float(value_str)
            except (TypeError, ValueError):
                missing.append(f"{series_id}:unparseable_value:{obs_date}")
                continue
            realtime_start = obs.get("realtime_start") or obs_date

            fetched += 1
            if dry_run:
                continue

            fc = FactCandidate(
                raw_ref=f"{series_id}:{obs_date}", subject=series_id, category="macro",
                metric="value", event_at=f"{obs_date}T00:00:00+00:00",
                market="US", country="US", value_num=value_num, unit=unit,
                publisher="FRED", data_status="reconstructed",
                extra={"realtime_start": realtime_start,
                       "realtime_end": obs.get("realtime_end")},
            )
            fc.safe_source_url = safe_url
            if append_or_skip(conn, fc, snapshot_id, f"{realtime_start}T00:00:00+00:00",
                              "backfill:macro_us"):
                appended += 1
            else:
                skipped += 1

    if not dry_run:
        conn.commit()
    if fetched == 0:
        return BackfillResult(source="macro_us", status="NO_DATA", reason_code="empty_response",
                              detail="; ".join(missing[:8])[:300])
    return BackfillResult(
        source="macro_us", status="PARTIAL" if missing else "OK",
        fetched=fetched, appended=appended, skipped_existing=skipped,
        detail="; ".join(missing[:8])[:300],
    )


def _unit_of(client, series_id: str, api_key: str, cache: dict, missing: list) -> str:
    """FRED `units_short`. 관측 응답의 `units`는 변환 방식(`lin`)이지 단위가
    아니다 — 그걸 실으면 리포트가 "미국 CPI 332.57 lin"이라고 쓴다."""
    if series_id in cache:
        return cache[series_id]
    try:
        resp = client.get(SERIES_URL, params={"series_id": series_id, "api_key": api_key,
                                              "file_type": "json"})
        unit = "" if resp.status_code != 200 else (
            (json.loads(resp.text).get("seriess") or [{}])[0].get("units_short") or "")
    except Exception as exc:  # noqa: BLE001
        missing.append(f"{series_id}:unit_lookup_error:{exc.__class__.__name__}")
        unit = ""
    cache[series_id] = unit
    return unit


# --- 한국 (ECOS) ------------------------------------------------------------

def _kr_known_at(key: str, time_str: str) -> str:
    """S3의 보수적 규칙. 둘 다 개정되지 않는 계열이라 "늦어도 이때는 확정"을
    잡으면 된다 — 기준금리는 그 달 말일, 매매기준율은 그날 장 마감."""
    if key == "base_rate":  # YYYYMM
        first = datetime.strptime(time_str, "%Y%m").replace(day=1, tzinfo=KR_TZ)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last_moment = next_month - timedelta(seconds=1)
        return last_moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    day = datetime.strptime(time_str, "%Y%m%d").date()  # YYYYMMDD
    local = datetime.combine(day, KR_FX_KNOWN_LOCAL, tzinfo=KR_TZ)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def _run_kr(conn, settings, since: date, until: date, subjects, dry_run: bool, http):
    from . import BackfillResult

    api_key = settings.ecos_api_key
    if not api_key:
        return BackfillResult(source="macro_kr", status="NO_DATA", reason_code="키없음")

    client = http("ecos")
    fetched = appended = skipped = 0
    missing: list[str] = []

    for key in ECOS_BACKFILL_KEYS:
        spec = STAT_CODES[key]
        subject = f"{spec['stat_code']}.{spec['item_code1']}"
        if subjects is not None and subject not in subjects and key not in subjects:
            continue
        freq = spec["freq"]
        start = since.strftime("%Y%m" if freq == "M" else "%Y%m%d")
        end = until.strftime("%Y%m" if freq == "M" else "%Y%m%d")
        # 라이브와 같은 URL 모양. 키가 경로에 들어가므로 저장·로깅은 반드시
        # `safe_url()`을 거친다(`http_client.safe_url`이 경로 세그먼트도 가린다).
        url = (f"{ECOS_BASE_URL}/{api_key}/json/kr/1/10000/"
               f"{spec['stat_code']}/{freq}/{start}/{end}/{spec['item_code1']}")
        try:
            resp = client.get(url)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{key}:error:{exc.__class__.__name__}")
            continue
        if resp.status_code != 200:
            missing.append(f"{key}:http_{resp.status_code}")
            continue
        payload = resp.text
        try:
            rows = (json.loads(payload).get("StatisticSearch") or {}).get("row") or []
        except json.JSONDecodeError:
            missing.append(f"{key}:bad_json")
            continue
        if not rows:
            missing.append(f"{key}:no_rows")
            continue

        safe = client.safe_url(url)
        snapshot_id = None
        if not dry_run:
            snapshot_id = db_mod.insert_raw_snapshot(
                conn, settings.raw_dir, "ecos",
                RawItem(external_id=f"backfill:{key}:{start}:{end}",
                        source_published_at=end, safe_source_url=safe,
                        payload=payload, fetch_status="ok"),
            )

        for row in rows:
            time_str = row.get("TIME") or ""
            try:
                value_num = float(row.get("DATA_VALUE"))
            except (TypeError, ValueError):
                continue
            try:
                # 주기와 어긋난 TIME 한 줄이 배치 전체를 죽이면 안 된다 — 월간
                # 계열에 일간 형식이 섞여 오면 그 줄만 버리고 이유를 남긴다.
                event_at = _time_to_event_at(time_str, freq)
                known_at = _kr_known_at(key, time_str)
            except ValueError:
                missing.append(f"{key}:bad_time:{time_str}")
                continue
            fetched += 1
            if dry_run:
                continue
            fc = FactCandidate(
                raw_ref=f"{key}:{time_str}", subject=subject, category="macro", metric="value",
                event_at=event_at, market="KR", country="KR",
                value_num=value_num, unit=row.get("UNIT_NAME") or spec["unit_hint"],
                publisher="ECOS(BOK)",
                # `reconstructed`는 "정보 차단선을 지켜 재구성 완료"라는 뜻이다.
                # known_at을 소스가 아니라 우리가 추정했으므로 그렇게 말할 수 없다.
                data_status="partial",
                extra={"logical_key": key, "stat_name": row.get("STAT_NAME"),
                       "known_at_rule": "보수적 추정 — ECOS 빈티지 미제공(spec S3)"},
            )
            fc.safe_source_url = safe
            if append_or_skip(conn, fc, snapshot_id, known_at, "backfill:macro_kr"):
                appended += 1
            else:
                skipped += 1

    if not dry_run:
        for key in ECOS_SKIPPED_KEYS:
            _register_gap(conn, f"backfill:ecos:{key}", key)
        conn.commit()

    detail = "; ".join([*missing[:6], ECOS_GAP_REASON])[:300]
    if fetched == 0:
        return BackfillResult(source="macro_kr", status="NO_DATA",
                              reason_code="empty_response", detail=detail)
    # 언제나 PARTIAL이다 — 두 계열만 채웠고 등급도 낮으므로 OK라고 말하면
    # "한국 거시는 다 됐다"로 읽힌다.
    return BackfillResult(source="macro_kr", status="PARTIAL", fetched=fetched,
                          appended=appended, skipped_existing=skipped, detail=detail)


def _register_gap(conn, gap_id: str, key: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO data_gaps(gap_id, subject, metric, detected_at, reason, status) "
        "VALUES (?,?,?,?,?,?)",
        (gap_id, key, "value", db_mod.iso_utc(), ECOS_GAP_REASON, "제안"),
    )


# --- 공통 -------------------------------------------------------------------

def append_or_skip(conn, fc, snapshot_id, known_at: str, correction_reason: str) -> bool:
    from .ledger import append_vintage

    provider = "fred" if fc.country == "US" else "ecos"
    return append_vintage(conn, _fact_id(provider, fc), snapshot_id, known_at, fc,
                          correction_reason=correction_reason)
