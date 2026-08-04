"""SEC EDGAR — 13F-HR for the 5 tracked superinvestor managers (spec §9.2
핵심 추적군, §9.4 steps 1-2): 제출 감지 **와 보유내역 표**. 대가 카드·종목 카드
(§9.4 steps 3-7) are Stage 2 analysis, still out of scope here.

CIKs for the 5 managers are resolved live via SEC's own company-name search
(browse-edgar, on the same whitelisted www.sec.gov host sec_edgar.py already
uses) instead of being hardcoded — a stale/wrong hand-typed CIK would
silently attribute filings to the wrong entity, and Prime Rule 8's
confidence gate says verify, don't fabricate. A name search can return
either a single matched company (its filings listed directly) or several
companies sharing the search term (each match nested, no filings) — the
latter is resolved by re-querying each candidate CIK directly and keeping
whichever one is still actually filing 13F-HR.

보유내역을 읽으면서 실측(2026-08-04)으로 드러난 것 네 가지 — 전부 아래 코드의
모양을 정한다:

1. **같은 종목이 여러 줄로 쪼개져 온다.** 버크셔는 자회사 14곳을 대신 신고해서
   `ALLY FINL INC` 한 종목이 `otherManager`만 다른 세 줄 이상으로 나뉜다
   (2026Q1: 90줄). 줄 하나를 사실 하나로 만들면 `_fact_id`가 같아져 **마지막
   줄만 남는다**. 그래서 (cusip, 단위, 콜/풋)로 묶어 합산한다.
2. **보유 시점과 알게 된 시점이 다르다.** 이 문서는 3월 31일 기준 보유를 5월
   15일에 신고한 것이다. `event_at`은 `periodOfReport`(사실이 참인 시점)이고,
   알게 된 시점은 엔진이 수집 시각으로 찍는다 — 둘을 합치면 5월 15일 이전
   리포트가 3월 말 보유를 아는 것처럼 보인다.
3. **정보표 파일 이름이 제각각이다** — `53405.xml`·`infotable.xml`·
   `Form13fInfoTable.xml`·`BGLLCQ12026.xml`. 이름으로 고를 수 없으므로
   `primary_doc.xml`이 아닌 XML을 열어 **루트 태그가 informationTable인지**로 고른다.
4. **접수번호 앞자리는 신고자가 아니라 제출 대행사 CIK다.** 버크셔 접수번호는
   `0001193125-…`(Donnelley)로 시작하는데 문서는 신고자 CIK `1067983` 밑에
   있어 앞자리로 경로를 만들면 404다. atom 응답의 `filing-href`가 이미 올바른
   경로를 들고 있으므로 그것을 쓴다 — CIK를 따로 추측하지 않는다.

**문서가 제 답을 들고 있다**: `primary_doc.xml`의 `tableValueTotal`이 표 합계를
적어 둔다. 우리가 합산한 값과 어긋나면 그 운용사의 보유내역은 **내지 않고**
결측으로 신고한다 — 조용히 틀린 보유내역을 발행하는 것보다 낫다.
"""
from __future__ import annotations

import json
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# spec §9.2 — subject slug, display name, company-search term (not a CIK).
TRACKED_MANAGERS = [
    ("berkshire_hathaway", "Berkshire Hathaway", "berkshire hathaway"),
    ("pershing_square", "Pershing Square Capital Management", "pershing square capital"),
    ("baupost_group", "Baupost Group", "baupost group"),
    ("lone_pine_capital", "Lone Pine Capital", "lone pine capital"),
    ("tci_fund_management", "TCI Fund Management", "tci fund management"),
]

MAX_AMBIGUOUS_CANDIDATES = 5
# 정보표 후보 XML을 몇 개까지 열어보나. 실측 파일은 디렉터리마다 1개뿐이고,
# 상한이 없으면 첨부가 많은 제출에서 요청이 통제 없이 늘어난다.
MAX_TABLE_CANDIDATES = 4
# 우리가 더한 값과 문서의 `tableValueTotal`이 이 비율 이상 어긋나면 그
# 운용사의 보유내역은 내지 않는다. 0이 아닌 이유는 반올림·정정 신고에서
# 몇 달러가 흔들리기 때문이고, 1%면 종목 하나를 통째로 놓친 경우는 반드시
# 걸린다(가장 작은 보유도 보통 총액의 0.1%를 넘는다).
VALUE_TOTAL_TOLERANCE = 0.01

# **금액 단위가 신고자마다 다르다.** 2023-01 규칙 개정으로 `value`는 달러
# 단위가 됐지만, 옛 관행대로 **천 달러**로 내는 곳이 아직 있다(실측 2026-08-04:
# Baupost 2026Q1 신고 총액 5,115,380 — 달러로 읽으면 510만 달러인데, 13F는
# 1억 달러 이상만 제출 의무가 있으므로 달러일 수 없다). 섞인 채로 실으면
# 버크셔 $263B 옆에 Baupost $5M이 나란히 서서 화면이 거짓말을 한다.
#
# 총액으로 가리지 않는 이유: 천 단위로 내는 초대형 펀드는 총액도 1억을 넘어
# 그 잣대를 통과해 버린다. 대신 **주당 환산가**(금액÷주식수)의 중앙값을 본다 —
# 단위가 1000배 어긋나면 이 값도 1000배 어긋나므로 규모와 무관하게 잡힌다.
# 실측 중앙값: Baupost 0.126 vs 나머지 73~292달러. 1달러면 양쪽 다 넉넉하다
# (동전주가 섞여도 **중앙값**이 1달러 밑으로 내려가지는 않는다).
MIN_IMPLIED_PRICE = 1.0
THOUSANDS_SCALE = 1000.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _search(client, search_term: str, cik: str | None = None):
    params = {
        "action": "getcompany", "type": "13F-HR", "dateb": "", "owner": "include",
        "count": "10", "output": "atom",
    }
    if cik:
        params["CIK"] = cik
    else:
        params["company"] = search_term
    return client.get(BROWSE_EDGAR_URL, params=params)


def _parse_filing_entries(root: ET.Element) -> list[dict]:
    """Single-company mode: each <entry> IS a filing (its <content> carries
    an <accession-number>). Most recent filing first."""
    out = []
    for entry in root.findall("a:entry", ATOM_NS):
        content = entry.find("a:content", ATOM_NS)
        if content is None:
            continue
        acc = content.findtext("a:accession-number", namespaces=ATOM_NS) or content.findtext("accession-number")
        if not acc:
            continue
        out.append({
            "accession_number": acc,
            "filing_date": content.findtext("a:filing-date", namespaces=ATOM_NS) or content.findtext("filing-date"),
            "filing_type": content.findtext("a:filing-type", namespaces=ATOM_NS) or content.findtext("filing-type"),
            # 문서 디렉터리의 정본 주소. 접수번호 앞자리(제출 대행사 CIK)로
            # 경로를 조립하면 신고자 CIK와 달라 404가 난다(버크셔 실측).
            "filing_href": (content.findtext("a:filing-href", namespaces=ATOM_NS)
                            or content.findtext("filing-href") or ""),
        })
    return out


def _local(tag: str) -> str:
    """`{ns}infoTable` -> `infoTable`. 13F 스키마는 판(X0202 등)마다 네임스페이스가
    다르고 `informationTable`은 기본 네임스페이스에 있다 — 이름공간을 고정해
    찾으면 판이 하나 바뀌는 순간 조용히 0건이 된다."""
    return tag.rsplit("}", 1)[-1]


def _text(node, name: str) -> str:
    for child in node.iter():
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _num(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _iso_period(raw: str) -> str:
    """`03-31-2026` -> `2026-03-31`. 형식이 다르면 빈 문자열(= 시점 모름)."""
    parts = (raw or "").split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""
    mm, dd, yyyy = parts
    if len(yyyy) != 4:
        return ""
    return f"{yyyy}-{mm}-{dd}"


def parse_information_table(xml_text: str) -> list[dict]:
    """`<informationTable>` -> 종목별로 **합산된** 보유 목록.

    합산 키는 (cusip, 단위, 콜/풋)이다: 같은 종목이라도 주식과 원금액(PRN)은
    더할 수 없는 양이고, 콜·풋은 방향이 반대라 합치면 순보유가 거짓이 된다.
    합산하지 않으면 대신 신고하는 운용사(버크셔 14곳)의 줄이 서로를 덮어쓴다.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if _local(root.tag) != "informationTable":
        return []

    merged: dict[tuple, dict] = {}
    for node in root:
        if _local(node.tag) != "infoTable":
            continue
        cusip = _text(node, "cusip").upper()
        if not cusip:
            continue
        unit = _text(node, "sshPrnamtType") or "SH"
        put_call = _text(node, "putCall")
        key = (cusip, unit, put_call)
        entry = merged.setdefault(key, {
            "cusip": cusip, "unit": unit, "put_call": put_call,
            "issuer": _text(node, "nameOfIssuer"), "title_of_class": _text(node, "titleOfClass"),
            "value": 0.0, "amount": 0.0, "rows": 0,
        })
        entry["value"] += _num(_text(node, "value")) or 0.0
        entry["amount"] += _num(_text(node, "sshPrnamt")) or 0.0
        entry["rows"] += 1
    return sorted(merged.values(), key=lambda h: h["value"], reverse=True)


def value_scale(holdings: list[dict]) -> float:
    """이 신고가 금액을 천 달러로 냈는지 판정 -> 곱할 배수(1 또는 1000).

    주식(SH) 보유의 **주당 환산가 중앙값**으로 본다. 판단할 표본이 없으면
    1을 돌려준다 — 모를 때 1000배 부풀리는 쪽으로 기울면 안 된다."""
    prices = [h["value"] / h["amount"] for h in holdings
              if h["unit"] == "SH" and h["amount"] and h["value"]]
    if not prices:
        return 1.0
    return THOUSANDS_SCALE if statistics.median(prices) < MIN_IMPLIED_PRICE else 1.0


def parse_cover_page(xml_text: str) -> dict:
    """`primary_doc.xml` -> {period, table_value_total, entry_total}.

    `tableValueTotal`이 이 파싱의 **오라클**이다 — 문서가 스스로 적어 둔 표
    합계라, 우리가 더한 값과 어긋나면 우리가 틀린 것이다."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    return {
        "period": _iso_period(_text(root, "periodOfReport")),
        "table_value_total": _num(_text(root, "tableValueTotal")),
        "entry_total": _num(_text(root, "tableEntryTotal")),
    }


def _parse_company_matches(root: ET.Element) -> list[str]:
    """Multi-company mode (ambiguous search term): each <entry> wraps a
    nested <company-info> with a CIK but no filing details."""
    ciks = []
    for entry in root.findall("a:entry", ATOM_NS):
        info = entry.find("a:content/a:company-info", ATOM_NS)
        if info is None:
            continue
        cik = info.findtext("a:cik", namespaces=ATOM_NS) or info.findtext("cik")
        if cik:
            ciks.append(cik.strip())
    return ciks


class Sec13fProvider:
    name = "sec_edgar_13f"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        # Same host whitelist / rate limit / User-Agent as sec_edgar (spec A6) —
        # this provider is a distinct workflow entry, not a distinct HTTP identity.
        client = ctx.http("sec_edgar")
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []

        for subject, display_name, search_term in TRACKED_MANAGERS:
            resp, filings = self._resolve(client, display_name, search_term, missing)
            if resp is None:
                continue

            latest = filings[0]
            external_id = f"13f:{subject}:{latest['accession_number']}"
            raw_items.append(
                RawItem(
                    external_id=external_id, source_published_at=latest["filing_date"] or _now_iso(),
                    safe_source_url=client.safe_url(str(resp.request.url)), payload=resp.text,
                )
            )
            facts.append(
                FactCandidate(
                    raw_ref=external_id, subject=subject, category="13f_filing", metric="filing_event",
                    event_at=f"{latest['filing_date']}T00:00:00+00:00", market="US", country="US",
                    value_text=latest["accession_number"], unit="", publisher="SEC EDGAR",
                    data_status="source_verified",
                    extra={"form": latest["filing_type"], "manager": display_name},
                )
            )
            facts.extend(self._holdings(client, subject, display_name, latest,
                                        raw_items, missing))

        if not facts:
            return ProviderResult(status="NO_DATA", reason_code="empty_response", raw_items=raw_items, safe_detail=("; ".join(missing[:8]))[:400])
        status = "PARTIAL" if missing else "OK"
        return ProviderResult(
            status=status, reason_code=None, raw_items=raw_items, facts=facts,
            safe_detail=("; ".join(missing[:8]))[:400],
        )

    def _holdings(self, client, subject: str, display_name: str, filing: dict,
                  raw_items: list[RawItem], missing: list[str]) -> list[FactCandidate]:
        """그 제출의 보유내역 -> 종목별 FactCandidate 두 줄(금액·수량).

        어느 단계에서 막히든 **그 운용사만** 결측으로 신고하고 넘어간다 —
        보유내역을 못 읽었다고 제출 감지까지 잃을 이유가 없다."""
        base = filing.get("filing_href", "").rsplit("/", 1)[0]
        if not base:
            missing.append(f"{display_name}:no_filing_href")
            return []

        index = self._get_json(client, f"{base}/index.json")
        if index is None:
            missing.append(f"{display_name}:index_unreadable")
            return []
        names = [i.get("name", "") for i in index.get("directory", {}).get("item", [])]
        candidates = [n for n in names if n.lower().endswith(".xml") and n != "primary_doc.xml"]

        holdings, table_url = [], ""
        for name in candidates[:MAX_TABLE_CANDIDATES]:
            body = self._get_text(client, f"{base}/{name}")
            if body is None:
                continue
            parsed = parse_information_table(body)
            if parsed:
                holdings, table_url = parsed, f"{base}/{name}"
                raw_items.append(RawItem(
                    external_id=f"13f-table:{subject}:{filing['accession_number']}",
                    source_published_at=filing.get("filing_date") or _now_iso(),
                    safe_source_url=client.safe_url(table_url), payload=body,
                ))
                break
        if not holdings:
            missing.append(f"{display_name}:no_information_table")
            return []

        cover_text = self._get_text(client, f"{base}/primary_doc.xml")
        cover = parse_cover_page(cover_text) if cover_text else {}
        period = cover.get("period")
        if not period:
            # 보유 시점을 모르면 실을 수 없다. 제출일로 대신하면 3월 말 보유가
            # 5월 15일 사실로 둔갑한다.
            missing.append(f"{display_name}:no_period_of_report")
            return []

        # 문서가 적어 둔 표 합계와 대조한다. 어긋나면 우리가 종목을 놓쳤거나
        # 중복해 더한 것이므로, 틀린 보유내역을 내느니 결측으로 신고한다.
        expected = cover.get("table_value_total")
        got = sum(h["value"] for h in holdings)
        if expected and abs(got - expected) > abs(expected) * VALUE_TOTAL_TOLERANCE:
            missing.append(f"{display_name}:value_total_mismatch:{got:.0f}_vs_{expected:.0f}")
            return []

        # 오라클 대조가 끝난 **뒤에** 단위를 맞춘다: `tableValueTotal`도 표와 같은
        # 단위라 먼저 곱하면 둘 다 1000배가 되어 대조가 무의미해진다.
        scale = value_scale(holdings)

        event_at = f"{period}T00:00:00+00:00"
        out: list[FactCandidate] = []
        for h in holdings:
            extra = {
                "manager": display_name, "issuer": h["issuer"], "cusip": h["cusip"],
                "title_of_class": h["title_of_class"], "amount_type": h["unit"],
                "accession": filing["accession_number"], "period_of_report": period,
                "merged_rows": h["rows"],
            }
            if scale != 1.0:
                extra["value_scale"] = scale
                extra["value_scale_reason"] = (
                    "신고서가 금액을 천 달러 단위로 냈다(주당 환산가 중앙값이 "
                    f"{MIN_IMPLIED_PRICE:g}달러 미만). 달러로 환산해 싣는다.")
            if h["put_call"]:
                extra["put_call"] = h["put_call"]
            # subject에 cusip이 들어가는 이유: `_fact_id`가 제공자:종목:항목:날짜라
            # 운용사만 쓰면 한 분기의 모든 보유가 같은 이름을 갖는다.
            holding_subject = f"{subject}/{h['cusip']}"
            out.append(FactCandidate(
                raw_ref=f"13f-table:{subject}:{filing['accession_number']}",
                subject=holding_subject, category="13f_holding", metric="holding_value",
                event_at=event_at, market="US", country="US", value_num=h["value"] * scale,
                unit="USD", publisher="SEC EDGAR", data_status="source_verified",
                comparison_basis="quarterly", extra=extra,
            ))
            out.append(FactCandidate(
                raw_ref=f"13f-table:{subject}:{filing['accession_number']}",
                subject=holding_subject, category="13f_holding", metric="holding_amount",
                event_at=event_at, market="US", country="US", value_num=h["amount"],
                unit=h["unit"], publisher="SEC EDGAR", data_status="source_verified",
                comparison_basis="quarterly", extra=extra,
            ))
        return out

    def _get_text(self, client, url: str) -> str | None:
        try:
            resp = client.get(url)
        except Exception:  # noqa: BLE001
            return None
        return resp.text if resp.status_code == 200 else None

    def _get_json(self, client, url: str):
        body = self._get_text(client, url)
        if body is None:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _resolve(self, client, display_name: str, search_term: str, missing: list[str]):
        """Returns (response_holding_the_chosen_filing, filings) or
        (None, None) with a reason appended to `missing`."""
        try:
            resp = _search(client, search_term)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{display_name}:search_error:{exc.__class__.__name__}")
            return None, None
        if resp.status_code != 200:
            missing.append(f"{display_name}:search_http_{resp.status_code}")
            return None, None
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            missing.append(f"{display_name}:unparseable_response")
            return None, None

        filings = _parse_filing_entries(root)
        if filings:
            return resp, filings

        # Ambiguous name match: no direct filings, but possibly several
        # candidate companies. Re-query each candidate CIK directly and
        # keep the one with the most recent actual 13F-HR filing (the
        # live, currently-filing entity) rather than guessing from the name.
        candidates = _parse_company_matches(root)
        if not candidates:
            missing.append(f"{display_name}:no_match")
            return None, None

        best_resp, best_filings = None, None
        for cik in candidates[:MAX_AMBIGUOUS_CANDIDATES]:
            try:
                cresp = _search(client, search_term, cik=cik)
            except Exception:  # noqa: BLE001
                continue
            if cresp.status_code != 200:
                continue
            try:
                croot = ET.fromstring(cresp.text)
            except ET.ParseError:
                continue
            cfilings = _parse_filing_entries(croot)
            if cfilings and (best_filings is None or cfilings[0]["filing_date"] > best_filings[0]["filing_date"]):
                best_resp, best_filings = cresp, cfilings

        if best_resp is None:
            missing.append(f"{display_name}:no_recent_13f_among_{len(candidates)}_candidates")
            return None, None
        return best_resp, best_filings
