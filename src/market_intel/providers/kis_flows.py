"""한국투자증권 KIS — 국내주식 투자자별 매매동향(개인·외국인·기관 순매수).

명세 §6.1 "누가 사고 누가 팔았나"에 답하는 유일한 경로다. 왜 여기까지 왔는지는
기록해 둘 값이 있다 — 2026-08-03에 다른 길을 전부 두드려 봤다:

- **KRX 오픈API에 수급 엔드포인트가 없다.** 이름 340가지를 시도해 전부 404였고,
  실제 승인받은 서비스(유가증권·코스닥 일별매매정보) 응답 필드 15개에도 투자자
  구분이 없다(거래대금 `ACC_TRDVAL`은 총액이지 투자자별이 아니다).
- **pykrx 익명 경로는 막혔다.** 투자자별 함수 9개 전부 0건이고, 엔드포인트를
  직접 두드리면 `HTTP 400 · 본문 LOGOUT`이 온다. 같은 시각 OHLCV는 정상이므로
  KRX가 그 화면만 회원 전용으로 바꾼 것이다. pykrx로 가려면 계정 비밀번호가 필요하다.
- **KIS는 키 방식이다.** 앱키+시크릿으로 토큰을 받아 조회한다. 비밀번호가
  오가지 않고, 유출되면 재발급으로 끝난다.

## ⚠️ 이 자격증명에는 주문 API가 붙어 있다

같은 앱키로 실제 매매 주문을 낼 수 있다. **이 프로젝트는 매매를 하지 않는다.**
그래서 `_quotation_url()`이 조회(`/uapi/domestic-stock/v1/quotations/`) 경로만
허용하고 그 밖은 `ValueError`로 거부한다. 문자열 규칙이 아니라 함수 하나를
지나가게 만들어 두는 이유는, 나중에 누가 무심코 다른 경로를 부를 때 조용히
성공하지 않게 하기 위해서다. `tests/providers/test_kis_flows.py`가 이것을 못박는다.

## 단위 함정 (실측으로 잡았다)

응답의 `*_ntby_qty`는 **주식 수**, `*_ntby_tr_pbmn`은 **백만원**이다. 후자를 원으로
읽으면 리포트가 "외국인 순매수 210만원"이라고 쓴다 — 실제로는 2,099,936백만원
= 약 2.1조원이고, 8,359,011주 x 262,500원과 맞아떨어진다(삼성전자 2026-07-31).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

KR_TZ = ZoneInfo("Asia/Seoul")
BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "/oauth2/tokenP"
QUOTATION_PREFIX = "/uapi/domestic-stock/v1/quotations/"
INVESTOR_PATH = QUOTATION_PREFIX + "investor-trade-by-stock-daily"
TR_ID_INVESTOR = "FHPTJ04160001"

# KIS는 **당일** 투자자별 수급을 장 마감 전에는 주지 않는다. 실측 2026-08-03:
# 오늘 날짜로 물으면 `rt_cd=2 TIME LIMIT 00:00 ~ 15:40`. 아침 수집(06:50)이
# 매일 조용히 0건이 됐을 자리다. 그래서 15:40 이전이면 **전날**을 기준일로 잡는다.
# 손해는 없다 — 한 번 호출에 그 날짜부터 30거래일이 오고, 아침 리포트의 차단선은
# 07:15 KST라 어차피 당일 한국 수급은 범위 밖이다.
KIS_INVESTOR_READY_LOCAL = (15, 40)

# 응답 필드 -> (metric, 단위, 배수). 순매수 **수량**과 **금액**을 둘 다 싣는다:
# 수량은 "몇 주를 담았나", 금액은 "얼마를 넣었나"로 서로 다른 질문에 답하고,
# 명세 §6.1이 묻는 것은 후자에 가깝지만 주식 수가 있어야 가격 착시를 걷어낸다.
FIELD_MAP: dict[str, tuple[str, str, int]] = {
    "frgn_ntby_qty": ("net_buy_foreign", "shares", 1),
    "prsn_ntby_qty": ("net_buy_individual", "shares", 1),
    "orgn_ntby_qty": ("net_buy_institution", "shares", 1),
    "frgn_ntby_tr_pbmn": ("net_buy_foreign_value", "KRW", 1_000_000),
    "prsn_ntby_tr_pbmn": ("net_buy_individual_value", "KRW", 1_000_000),
    "orgn_ntby_tr_pbmn": ("net_buy_institution_value", "KRW", 1_000_000),
}


def _quotation_url(path: str) -> str:
    """조회 경로만 통과시킨다. 주문 경로는 여기서 막힌다 — 위 경고 참조."""
    if not path.startswith(QUOTATION_PREFIX):
        raise ValueError(
            f"KIS 조회(quotations) 경로만 허용된다 — 이 프로젝트는 매매를 하지 않는다: {path!r}")
    return BASE_URL + path


def _event_at(yyyymmdd: str) -> str:
    """한국 장 마감 15:30 KST -> UTC (yfinance KR 규약과 같은 시각)."""
    local = datetime.strptime(yyyymmdd, "%Y%m%d").replace(hour=15, minute=30, tzinfo=KR_TZ)
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def _base_date(now_utc: datetime) -> str:
    """KIS가 당일치를 내주기 시작하는 시각(15:40 KST) 전이면 전날을 쓴다.

    이 한 줄이 없으면 아침 수집이 매일 `rt_cd=2 TIME LIMIT 00:00 ~ 15:40`으로
    0건을 받는다(실측 2026-08-03). 주말·공휴일이어도 문제없다 — 그 날짜에
    거래가 없으면 KIS가 그 이전 거래일들로 30행을 채워 준다."""
    local = now_utc.astimezone(KR_TZ)
    hh, mm = KIS_INVESTOR_READY_LOCAL
    if (local.hour, local.minute) < (hh, mm):
        local -= timedelta(days=1)
    return local.strftime("%Y%m%d")


def _token_cache_path(settings) -> Path:
    return Path(settings.raw_dir).parent / "kis_token.json"


def _read_cached_token(settings) -> str | None:
    """만료 10분 전부터는 새로 받는다 — 수집 도중에 죽는 것을 피한다."""
    path = _token_cache_path(settings)
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expires_at = cache.get("expires_at", 0)
    if not isinstance(expires_at, (int, float)) or expires_at - time.time() < 600:
        return None
    token = cache.get("access_token")
    return token if isinstance(token, str) and token else None


def _write_cached_token(settings, token: str, expires_in) -> None:
    path = _token_cache_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ttl = float(expires_in) if expires_in else 86400.0
        payload = json.dumps({"access_token": token, "expires_at": time.time() + ttl})
        # 먼저 0600으로 만들고 쓴다 — 기본 권한으로 만든 뒤 chmod하면 그 사이에
        # 다른 사용자가 읽을 수 있는 창이 생긴다.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError:
        pass  # 캐시는 최적화지 필수가 아니다 — 못 쓰면 매번 발급받는다


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


class KisFlowsProvider:
    name = "kis"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        settings = ctx.settings
        if not (settings.kis_app_key and settings.kis_app_secret):
            # 키가 없으면 네트워크를 두드리지도 않는다(다른 provider와 같은 규율).
            return ProviderResult(status="NO_DATA", reason_code="키없음")

        symbols = [m for m in ctx.universe
                   if m.get("market") == "KR" and m.get("asset_type") == "equity"]
        if not symbols:
            return ProviderResult(status="NO_DATA", reason_code="no_subjects")

        base_date = _base_date(ctx.now)
        client = ctx.http("kis")
        try:
            token = self._token(client, settings)
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(status="ERROR", reason_code="auth_failed",
                                  safe_detail=f"{exc.__class__.__name__}: {exc}"[:300])

        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for meta in symbols:
            symbol = meta["symbol"]
            code = symbol.split(".")[0]
            try:
                rows, safe_url, payload = self._investor_rows(
                    client, settings, token, code, base_date)
            except Exception as exc:  # noqa: BLE001
                # 예외 클래스명만 남기면 운영자가 왜 실패했는지 알 수 없다 —
                # KIS는 `msg1`에 사유를 한국어로 준다. 그걸 살려서 보고한다.
                missing.append(f"{code}:{exc.__class__.__name__}:{exc}"[:160])
                continue
            if not rows:
                missing.append(f"{code}:empty")
                continue

            # 한 번 호출에 최근 30거래일이 온다 — 전부 사실로 남긴다.
            # 오늘치 하나만 쓰고 버리면 백필을 따로 또 해야 한다.
            for row in rows:
                date_str = str(row.get("stck_bsop_date") or "")
                if len(date_str) != 8:
                    continue
                external_id = f"kis:{code}:{date_str}"
                raw_items.append(RawItem(
                    external_id=external_id, source_published_at=_event_at(date_str),
                    safe_source_url=safe_url,
                    payload=json.dumps(row, ensure_ascii=False), fetch_status="ok"))
                for field, (metric, unit, scale) in FIELD_MAP.items():
                    value = _num(row.get(field))
                    if value is None:
                        continue
                    facts.append(FactCandidate(
                        raw_ref=external_id, subject=symbol, category="flow", metric=metric,
                        event_at=_event_at(date_str), market="KR", country="KR",
                        value_num=value * scale, unit=unit,
                        publisher="한국투자증권 KIS", data_status="source_verified",
                        extra={"source_field": field, "close": row.get("stck_clpr")},
                    ))
            _ = payload  # 원자료는 raw_items로 이미 보관된다

        if not facts:
            return ProviderResult(status="NO_DATA", reason_code="empty_response",
                                  raw_items=raw_items,
                                  safe_detail="; ".join(missing[:8])[:400])
        return ProviderResult(
            status="PARTIAL" if missing else "OK", reason_code=None,
            raw_items=raw_items, facts=facts,
            safe_detail="; ".join(missing[:8])[:400])

    def _token(self, client, settings) -> str:
        """접근 토큰. **디스크에 캐시해 만료 전까지 재사용한다.**

        KIS는 토큰 발급 자체에 빈도 제한을 건다 — 실측 2026-08-03: 짧은 시간에
        몇 번 받았더니 발급이 `HTTP 403`으로 막혔다. 토큰은 24시간 유효하므로
        매 수집마다 새로 받을 이유가 없고, 받으면 언젠가 수집이 통째로 멈춘다.

        캐시 파일은 `var/`(gitignore) 아래에 소유자만 읽게(0600) 둔다. 토큰은
        24시간 뒤 스스로 죽으므로 앱 시크릿보다 노출 수명이 짧다."""
        cached = _read_cached_token(settings)
        if cached:
            return cached
        resp = client.post(
            BASE_URL + TOKEN_PATH,
            json={"grant_type": "client_credentials",
                  "appkey": settings.kis_app_key, "appsecret": settings.kis_app_secret},
            headers={"content-type": "application/json"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"token http {resp.status_code}")
        body = json.loads(resp.text) or {}
        token = body.get("access_token")
        if not token:
            raise RuntimeError("token missing in response")
        _write_cached_token(settings, token, body.get("expires_in"))
        return token

    def _investor_rows(self, client, settings, token: str, code: str, base_date: str):
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": base_date,
            "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": "",
        }
        resp = client.get(
            _quotation_url(INVESTOR_PATH), params=params,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": settings.kis_app_key, "appsecret": settings.kis_app_secret,
                "tr_id": TR_ID_INVESTOR, "custtype": "P",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"http {resp.status_code}")
        body = json.loads(resp.text) or {}
        if body.get("rt_cd") != "0":
            raise RuntimeError(f"rt_cd={body.get('rt_cd')} {body.get('msg1', '')}"[:150])
        rows = body.get("output2") or body.get("output1") or []
        # 토큰·앱키는 헤더로만 보냈으므로 URL에는 없다. 그래도 safe_url을 거친다.
        return rows, client.safe_url(str(resp.request.url)), resp.text
