"""DART (Korea FSS Open DART) — Korean Core 4 (Samsung Electronics, SK
Hynix, KB Financial, Hyundai Motor) regular-disclosure detection + a
best-effort financial-statement snapshot. corp_code is resolved at
runtime from corpCode.xml (never hardcoded) — with no key, that call
never happens and we return NO_DATA/키없음 immediately."""
from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
FINANCIALS_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"

KR_TZ = ZoneInfo("Asia/Seoul")

# stock_code -> (subject symbol used elsewhere in this DB, display name)
KR_CORE = {
    "005930": ("005930.KS", "삼성전자"),
    "000660": ("000660.KS", "SK하이닉스"),
    "105560": ("105560.KS", "KB금융"),
    "005380": ("005380.KS", "현대차"),
    # 소재·철강 축 (CEO 확정 2026-08-02). 기업이 늘면 분기 실적 읽기 부담도
    # 늘지만 명세 §12가 "분기: 전체의 실적·가이던스 갱신"을 요구하므로
    # 관측군에 넣고 재무를 안 읽을 수는 없다 — 의도된 비용이다.
    "005490": ("005490.KS", "POSCO홀딩스"),
}

# DART account_nm labels for the IS lines we try to extract (best-effort;
# Korean labels vary slightly by filer, so a first real run should verify).
REVENUE_LABELS = {"매출액", "수익(매출액)"}
OPERATING_INCOME_LABELS = {"영업이익", "영업이익(손실)"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DartProvider:
    name = "dart"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        api_key = ctx.settings.dart_api_key
        if not api_key:
            return ProviderResult(status="NO_DATA", reason_code="키없음", raw_items=[], facts=[])

        client = ctx.http("dart")
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        corp_codes, corp_raw, corp_err = self._resolve_corp_codes(client, api_key)
        if corp_raw is not None:
            raw_items.append(corp_raw)
        if corp_err:
            return ProviderResult(status="ERROR", reason_code="empty_response", raw_items=raw_items, safe_detail=corp_err[:300])

        for stock_code, (subject, name) in KR_CORE.items():
            corp_code = corp_codes.get(stock_code)
            if not corp_code:
                missing.append(f"{stock_code}:corp_code_not_found")
                continue

            filing_facts, filing_raw, filing_missing = self._recent_filing(client, api_key, corp_code, subject)
            facts.extend(filing_facts)
            raw_items.extend(filing_raw)
            missing.extend(filing_missing)

            fin_facts, fin_raw, fin_missing = self._financials(client, api_key, corp_code, subject)
            facts.extend(fin_facts)
            raw_items.extend(fin_raw)
            missing.extend(fin_missing)

        if not facts:
            return ProviderResult(
                status="NO_DATA", reason_code="empty_response", raw_items=raw_items,
                safe_detail="; ".join(missing[:8])[:400],
            )
        status = "PARTIAL" if missing else "OK"
        return ProviderResult(
            status=status, reason_code=None, raw_items=raw_items, facts=facts,
            safe_detail="; ".join(missing[:8])[:400],
        )

    def _resolve_corp_codes(self, client, api_key: str):
        try:
            resp = client.get(CORP_CODE_URL, params={"crtfc_key": api_key})
        except Exception as exc:  # noqa: BLE001
            return {}, None, f"corp_code_error:{exc.__class__.__name__}"
        if resp.status_code != 200:
            return {}, None, f"corp_code_http_{resp.status_code}"

        zip_bytes = resp.content
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                xml_bytes = zf.read(zf.namelist()[0])
        except zipfile.BadZipFile:
            return {}, None, "corp_code_bad_zip (invalid key or non-zip error body)"

        root = ET.fromstring(xml_bytes)
        mapping = {}
        for item in root.iter("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            if stock_code in KR_CORE:
                mapping[stock_code] = corp_code

        raw = RawItem(
            external_id="corp_code_zip", source_published_at=_now_iso(),
            safe_source_url=client.safe_url(str(resp.request.url)), payload=zip_bytes,
        )
        return mapping, raw, None

    def _recent_filing(self, client, api_key: str, corp_code: str, subject: str):
        now_kst = datetime.now(KR_TZ)
        params = {
            "crtfc_key": api_key, "corp_code": corp_code,
            "bgn_de": (now_kst - timedelta(days=120)).strftime("%Y%m%d"),
            "end_de": now_kst.strftime("%Y%m%d"),
            "pblntf_ty": "A", "page_no": 1, "page_count": 10,
        }
        try:
            resp = client.get(LIST_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            return [], [], [f"{subject}:list_error:{exc.__class__.__name__}"]
        if resp.status_code != 200:
            return [], [], [f"{subject}:list_http_{resp.status_code}"]

        payload = resp.text
        data = json.loads(payload)
        if data.get("status") != "000":
            return [], [], [f"{subject}:list_status_{data.get('status')}"]

        items = data.get("list", [])
        if not items:
            return [], [], [f"{subject}:no_recent_filing"]
        latest = items[0]
        # DART returns rcept_dt as YYYYMMDD; event_at must be real ISO-8601 or
        # the engine derives a mangled fact_id (`...:filing_event:20260515T0`).
        rcept_dt = str(latest.get("rcept_dt") or "")
        event_date = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}" if len(rcept_dt) == 8 else rcept_dt
        external_id = f"{subject}:filing:{latest.get('rcept_no')}"
        raw = RawItem(
            external_id=external_id, source_published_at=event_date,
            safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
        )
        fact = FactCandidate(
            raw_ref=external_id, subject=subject, category="filing", metric="filing_event",
            event_at=f"{event_date}T00:00:00+00:00", market="KR", country="KR",
            value_text=latest.get("rcept_no"), unit="", publisher="DART", data_status="source_verified",
            extra={"report_name": latest.get("report_nm")},
        )
        return [fact], [raw], []

    def _financials(self, client, api_key: str, corp_code: str, subject: str):
        now_kst = datetime.now(KR_TZ)
        # reprt_code 11011 (사업보고서) for fiscal year Y is filed by ~March
        # of year Y+1, so the most recently *available* annual report as of
        # "now" is for (now.year - 1) once we're past that filing window,
        # else (now.year - 2). [Confirmed via live DART call 2026-08-01:
        # requesting bsns_year=now.year returned status 013 "no data" for
        # all 4 Core companies, since FY2026 hasn't closed yet.]
        year = now_kst.year - 1 if now_kst.month > 3 else now_kst.year - 2
        params = {
            "crtfc_key": api_key, "corp_code": corp_code, "bsns_year": str(year),
            "reprt_code": "11011", "fs_div": "CFS",
        }
        try:
            resp = client.get(FINANCIALS_URL, params=params)
        except Exception as exc:  # noqa: BLE001
            return [], [], [f"{subject}:financials_error:{exc.__class__.__name__}"]
        if resp.status_code != 200:
            return [], [], [f"{subject}:financials_http_{resp.status_code}"]

        payload = resp.text
        data = json.loads(payload)
        if data.get("status") != "000":
            return [], [], [f"{subject}:financials_status_{data.get('status')}"]

        external_id = f"{subject}:financials:{year}"
        raw = RawItem(
            external_id=external_id, source_published_at=f"{year}-12-31",
            safe_source_url=client.safe_url(str(resp.request.url)), payload=payload,
        )

        out_facts: list[FactCandidate] = []
        missing: list[str] = []
        for metric, labels in (("revenue", REVENUE_LABELS), ("operating_income", OPERATING_INCOME_LABELS)):
            # Some filers report a combined comprehensive-income statement
            # (sj_div='CIS') instead of a plain income statement ('IS') —
            # confirmed via live DART call 2026-08-01 (SK Hynix reports
            # under CIS only). Accept either.
            row = next(
                (r for r in data.get("list", []) if r.get("sj_div") in ("IS", "CIS") and r.get("account_nm") in labels),
                None,
            )
            if row is None:
                missing.append(f"{subject}:{metric}:account_not_found")
                continue
            try:
                value_num = float(row.get("thstrm_amount", "").replace(",", ""))
            except (TypeError, ValueError):
                missing.append(f"{subject}:{metric}:unparseable_value")
                continue
            out_facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=subject, category="financials", metric=metric,
                    event_at=f"{year}-12-31T00:00:00+00:00", market="KR", country="KR",
                    value_num=value_num, unit="KRW",
                    # reprt_code 11011 is always the annual report (사업보고서) —
                    # label it so this is never silently compared against a
                    # quarterly figure from another source (repair.md finding #1).
                    comparison_basis="annual",
                    publisher="DART", data_status="source_verified",
                    extra={"account_nm": row.get("account_nm"), "bsns_year": year, "reprt_code": "11011"},
                )
            )
        return out_facts, [raw], missing
