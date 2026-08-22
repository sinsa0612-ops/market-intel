"""업종 ETF의 **상위 보유종목 비중** — 사각지대 신고가 단정 대신 계산을 하게 만든다.

## 왜 이것이 필요한가 (2026-08-21 실측)

이 시스템이 지금까지 낸 사각지대 신고는 **딱 한 건**이고, 그 한 건이 틀렸다.

```
발행된 문장 (2026-08-20 close_delta):
  "헬스케어 +3.51% — 우리가 보는 Eli Lilly은(는) 평소 범위였다.
   움직인 것은 우리가 관측하지 않는 종목이다."

실제 (2026-08-19 미국 종가):
  XLV +3.51% (자기 이력 상위 0.21% -> 유별남)
  LLY +4.46% (자기 이력 상위 3.19% -> 문턱 2% 미달 = "평소 범위")
  그런데 LLY는 XLV의 15.47%다. 기여도 0.1547 x 4.46 = +0.69%p
  = 업종 움직임 3.51%p의 약 20%를 **우리가 보는 그 종목**이 만들었다.
```

검출기는 **"자기 이력 기준으로 드문가"**(상대적 질문)로 재고서 **"누가 업종을
움직였나"**(절대적 질문)를 단정했다. 다른 질문이다. 개별주는 ETF보다 원래 크게
움직이므로, 같은 문턱을 대면 **개별주는 거의 항상 "평소 범위"로 나온다** — 즉 이
오탐은 우연이 아니라 구조적이다.

비중이 있으면 단정할 필요가 없다: 관측 기업이 업종 움직임의 **얼마를 설명하는지**
계산해서 그 숫자를 쓰면 된다.

## 무엇을 수집하나

우리가 이미 추적하는 업종 지수(`asset_type == "sector_index"`)의 상위 보유종목과
비중. 출처는 **이미 쓰고 있는 yfinance**이고(`funds_data.top_holdings`), 새 벤더가
아니라 같은 벤더의 다른 창구다. 값을 사람이 손으로 적지 않는 것이 핵심이다 —
"어느 종목이 그 업종에 속하나"를 기억으로 적으면 그 자체가 환각 표면이 된다.

**실측(2026-08-21)**: 미국 업종 ETF 11개는 전부 상위 10종목을 준다.
**한국 업종 ETF 14개는 하나도 주지 않는다** — 그래서 한국 업종은 이 방법으로
기여도를 낼 수 없다. 감추지 않고 「비중 미상」으로 남는다.

## 차단선 (중요)

비중은 시간에 따라 변하는데 yfinance는 **"언제 기준"인지 말해 주지 않는다.**
그래서 `event_at`을 수집한 날짜로 둔다 — *"우리가 그날 관측한 비중"*이라는
정직한 주장이고, 과거 어느 시점의 비중이었다고 말하지 않는다.

딸려오는 성질이 오히려 옳다: 지난 리포트를 오늘 다시 만들면 그 차단선에는
비중 관측이 없으므로 기여도가 계산되지 않고, 검출기는 계산 없이 말할 수 있는
것만 말한다. **오늘 안 비중으로 지난주를 설명하지 않는다.**
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import yfinance as yf

from ..models import CollectContext, FactCandidate, ProviderResult, RawItem

# 한 업종에서 몇 종목까지 담나. yfinance가 주는 것이 상위 10이라 그대로 쓴다 —
# 자르면 설명되는 몫이 실제보다 작아지고, 그만큼 "우리가 못 본다"가 부풀려진다.
MAX_HOLDINGS = 10


class YFinanceHoldingsProvider:
    name = "yfinance_holdings"

    def collect(self, ctx: CollectContext) -> ProviderResult:
        etfs = [m for m in ctx.universe if m.get("asset_type") == "sector_index"]
        raw_items: list[RawItem] = []
        facts: list[FactCandidate] = []
        missing: list[str] = []
        today = datetime.now(timezone.utc).date().isoformat()

        for meta in etfs:
            symbol = meta["symbol"]
            try:
                holdings = yf.Ticker(symbol).funds_data.top_holdings
            except Exception as exc:  # noqa: BLE001 - 한 업종의 실패가 나머지를 막지 않는다
                missing.append(f"{symbol}:holdings_error:{exc.__class__.__name__}")
                continue
            if holdings is None or len(holdings) == 0:
                # 한국 업종 ETF가 전부 여기로 온다(실측). 결측이지 오류가 아니다.
                missing.append(f"{symbol}:no_holdings")
                continue

            rows = self._rows(holdings)
            if not rows:
                missing.append(f"{symbol}:unreadable_holdings")
                continue

            external_id = f"{symbol}:holdings:{today}"
            raw_items.append(RawItem(
                external_id=external_id, source_published_at=f"{today}T00:00:00+00:00",
                safe_source_url=f"yfinance://funds_data/top_holdings/{symbol}",
                payload="\n".join(f"{h}\t{w}" for h, w in rows),
            ))
            for holding, weight_pct in rows:
                facts.append(FactCandidate(
                    raw_ref=external_id,
                    # `13f` 보유내역과 같은 수법 — 복합 이름이라야 한 업종의 열
                    # 종목이 서로 다른 `fact_id`를 갖는다(`engine._fact_id`는
                    # provider:종목:항목:날짜다).
                    subject=f"{symbol}/{holding}", category="etf_holding",
                    metric="holding_weight", event_at=f"{today}T00:00:00+00:00",
                    market=meta["market"], country=meta["country"],
                    value_num=weight_pct, unit="percent", publisher="Yahoo Finance",
                    data_status="source_verified",
                    extra={"etf": symbol, "holding": holding},
                ))

        if not facts:
            return ProviderResult(status="NO_DATA", reason_code="empty_response",
                                  raw_items=raw_items, safe_detail=("; ".join(missing[:8]))[:400])
        return ProviderResult(
            status="PARTIAL" if missing else "OK", reason_code=None,
            raw_items=raw_items, facts=facts, safe_detail=("; ".join(missing[:8]))[:400],
        )

    @staticmethod
    def _rows(holdings) -> list[tuple[str, float]]:
        """`top_holdings`는 심볼이 색인이고 `Holding Percent`가 **비율(0~1)**이다.
        백분율로 바꿔 저장한다 — 원장의 `unit="percent"`가 그 뜻이고, 화면·계산이
        둘 다 백분율로 읽는다."""
        out: list[tuple[str, float]] = []
        for holding, row in list(holdings.iterrows())[:MAX_HOLDINGS]:
            try:
                pct = float(row["Holding Percent"]) * 100.0
            except (KeyError, TypeError, ValueError):
                continue
            # ⚠️ `float(NaN)`은 예외를 내지 않고 `NaN <= 0`도 False다 — 빠진 값이
            # 조용히 원장에 들어간다(시험이 실제로 잡았다). pandas는 빈 칸을
            # `None`이 아니라 `NaN`으로 주므로 이 검사가 유일한 관문이다.
            if not math.isfinite(pct) or pct <= 0:
                continue
            out.append((str(holding), pct))
        return out
