"""Report data model (spec B5) — the 2B extension point.

`Interpretation` is always constructed empty by this stage (2A). Nothing in
this module, `build.py`, or either renderer ever fills `reading` /
`counter_reading` / `thesis_impact` / `next_check` with generated text — 2B
does that later, out of process, by handing the renderers the very same
`Report` object with only `interpretation` different. The 3 contracts this
protects (spec B5):

1. Renderers take a `Report` and nothing else.
2. `interpretation.is_empty()` → renderers print the literal string
   "AI 해석 미생성" in each of the 4 body sections; filled → an
   "AI 자동판정 · {generated_by}" badge instead.
3. Facts reach this model only through `db.facts_as_of` (build.py's job,
   not this file's — this file has no DB import at all).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

# 2a.2: FactRow에 delta_pct/series, Report에 breadth/sector_summary가 추가됐다
# (가독성 요구 — 등락 색·화살표, 업종·시장 폭, 추이 그래프). 필드 추가뿐이라
# 옛 JSON도 그대로 읽히지만(`from_json` 참조), 스키마가 바뀐 사실 자체를
# 버전에 남긴다.
# 2a.3: Report에 sector_index(업종 지수 표)가 추가됐다. 역시 필드 추가뿐이다.
# 2a.4: FactRow에 doc_url(공시 원문 주소)이 추가됐다. 역시 필드 추가뿐이다.
SCHEMA_VERSION = "2a.4"


@dataclass
class FactRow:
    label: str
    value: str
    comparison: str
    source_url: str
    data_status: str
    known_at: str
    subject: str
    metric: str
    raw_value: Any = None
    # 등락 방향(색·화살표)과 추이 그래프의 입력. 둘 다 **리포트가 계산해서
    # 들고 다닌다** — 렌더러가 `comparison` 문자열에서 부호를 되파싱하거나,
    # 사이트가 DB를 직접 뒤져 시계열을 만들면 그 순간 리포트의 정보차단선
    # 밖으로 나간다(사이트는 렌더러이지 데이터 소스가 아니다).
    delta_pct: float | None = None
    # `delta_pct`를 화면에 찍을 때 붙일 단위. 기본은 `%`(상대 변화율)지만,
    # **값 자체가 이미 퍼센트인 지표**(금리·실업률·금리차)는 `%p`다.
    #
    # 왜 필요한가: 한국 기준금리가 2.50 -> 2.75가 됐을 때 상대 변화율은
    # +10.00%다. 산술은 맞지만 금리를 그렇게 말하는 곳은 없고("0.25%p 인상"),
    # 읽는 사람은 금리가 10%대가 된 줄로 읽는다(CEO 지적 2026-08-05).
    # 실업률 4.3 -> 4.2를 "-2.33%"로 쓰는 쪽이 더 위험하다 — 그럴듯해서
    # 그냥 믿고 넘어간다. 금리차(10Y-2Y)는 0을 넘나들어 상대 변화율 자체가
    # 성립하지 않는다.
    #
    # 단위를 렌더러가 `unit` 문자열에서 추측하지 않고 여기 실어 보내는 이유는
    # `delta_pct` 주석과 같다 — 판단은 build.py가 한 번만 한다.
    delta_unit: str = "%"
    # 오래된 값 -> 최신 값. `db.facts_as_of(cutoff)`가 이미 걸러준 것만
    # 담기므로 PIT 보장은 상속된다. 관측이 1개뿐이면 빈 리스트(= 그래프 없음).
    series: list[float] = field(default_factory=list)
    # 사람이 실제로 읽을 수 있는 문서의 주소(공시 원문). `source_url`은 그
    # 사실을 **어디서 가져왔는지**(API 엔드포인트)라서 감사 추적으로는 맞지만
    # 클릭하면 JSON이 뜬다 — CEO 지적 2026-08-03. 둘을 바꾸지 않고 나란히
    # 들고 다니다가, 렌더러가 있는 쪽(문서)을 링크한다.
    doc_url: str = ""
    # 이 사실이 어느 갈래인가 — `flow`(수급) · `macro`(거시) · `filing`(공시) ·
    # `financials` · `consensus`. 렌더러가 갈래마다 **다른 모양**으로 그리기
    # 위한 것이다: 수급은 막대, 거시는 카드, 공시는 요약 한 줄.
    #
    # metric 문자열로 분류하지 않는 이유: 그러면 "net_buy로 시작하면 수급"
    # 같은 규칙이 렌더러 두 곳에 복사되고, provider가 metric 이름을 바꾸는
    # 순간 화면이 조용히 옛 모양으로 돌아간다. 갈래를 아는 것은 사실을
    # 만드는 build.py이므로 거기서 한 번만 정한다. 빈 값은 "갈래 없음" =
    # 기존 표로 그린다(이 필드가 없던 옛 리포트 JSON이 그렇게 읽힌다).
    group: str = ""


@dataclass
class CalendarRow:
    when: str
    name: str
    country: str
    subject: str
    importance: str  # 'A'|'B'|'C' (spec 5.2)
    change: str  # ''|'신규'|'앞당김'|'연기'|'next_cycle'|'consensus_revised'
    source_url: str
    data_status: str


@dataclass
class MissingItem:
    area: str
    reason: str
    since: str
    gap_id: str


@dataclass
class SectorSummary:
    """spec §12의 한 축에 대한 하루치 요약 (§6.1 "시장 폭과 순환은 어떤가").

    대표값이 평균이 아니라 **중앙값**인 이유: 축 하나에 종목이 1~4개뿐이라
    4종목 축에서 한 종목이 +30% 튀면 평균은 +8%가 되어 "반도체 전면 급등"으로
    읽힌다. 실제로 뛴 건 하나다. 중앙값은 그 한 종목에 흔들리지 않는다.
    그래도 n=1~2인 축은 애초에 '업종'이 아니라 개별 종목이므로
    `small_sample`로 표시해 독자가 스스로 할인해서 읽게 한다."""
    sector: str
    up: int
    down: int
    flat: int
    total: int
    median_pct: float | None
    small_sample: bool


@dataclass
class SectorIndexRow:
    """업종 지수 ETF 한 종목의 하루치 — 업종 지수 표의 한 행.

    `SectorSummary`(Core 16 기업을 묶은 축)와 **다른 것을 잰다**: 저쪽은
    "내가 관측하는 16개 기업이 업종별로 어땠나", 이쪽은 "시장 전체가 업종별로
    어떻게 움직였나"다. 그래서 필드도 다르다 — 여기에는 묶을 종목이 없고
    (ETF 한 종목이 그 업종 전체를 담는다) 대신 지수 값·등락·추이가 있다.

    `FactRow`를 쓰지 않는 이유는 `market`이다. 표를 미국/한국으로 갈라
    보이려면 렌더러가 시장을 알아야 하는데, 렌더러는 `Report` 말고는 아무것도
    읽지 않는다(spec B5 계약 1) — universe를 import하는 순간 그 계약이 깨진다.
    """
    subject: str
    label: str
    market: str  # 'US' | 'KR'
    value: str
    comparison: str
    delta_pct: float | None
    series: list[float]
    source_url: str
    data_status: str


@dataclass
class Interpretation:
    reading: str = ""
    counter_reading: str = ""
    thesis_impact: str = ""
    next_check: str = ""
    generated_by: str = ""
    generated_at: str = ""

    def is_empty(self) -> bool:
        return not (self.reading or self.counter_reading or self.thesis_impact or self.next_check)


@dataclass
class Report:
    schema_version: str = SCHEMA_VERSION
    report_type: str = ""
    report_date: str = ""
    cutoff_kst: str = ""
    cutoff_utc: str = ""
    generated_at: str = ""
    title: str = ""
    headline: str = ""
    breadth: str = ""  # §6.1 "시장 전체인가, 대형주 쏠림인가" 한 줄 답
    data_status: str = ""
    facts: list[FactRow] = field(default_factory=list)
    market_reaction: list[FactRow] = field(default_factory=list)
    events: list[CalendarRow] = field(default_factory=list)
    schedule_changes: list[CalendarRow] = field(default_factory=list)
    missing: list[MissingItem] = field(default_factory=list)
    sector_summary: list[SectorSummary] = field(default_factory=list)
    # 업종 지수(시장 전체). `sector_summary`(Core 16 기업 묶음)와 나란히 놓이는
    # 별개의 표이며, 둘은 서로 다른 질문에 답한다 — `SectorIndexRow` 참조.
    sector_index: list[SectorIndexRow] = field(default_factory=list)
    interpretation: Interpretation = field(default_factory=Interpretation)
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Report":
        """`site build` deletes and rebuilds `docs/` from **every** JSON in
        `reports/`, including the ones written before a field existed. A
        missing key therefore has to fall back to the dataclass default
        rather than raise, or one old artefact takes the whole site down."""
        d = json.loads(raw)
        d["facts"] = [FactRow(**f) for f in d["facts"]]
        d["market_reaction"] = [FactRow(**f) for f in d["market_reaction"]]
        d["events"] = [CalendarRow(**e) for e in d["events"]]
        d["schedule_changes"] = [CalendarRow(**e) for e in d["schedule_changes"]]
        d["missing"] = [MissingItem(**m) for m in d["missing"]]
        d["sector_summary"] = [SectorSummary(**s) for s in d.get("sector_summary", [])]
        d["sector_index"] = [SectorIndexRow(**s) for s in d.get("sector_index", [])]
        d["interpretation"] = Interpretation(**d["interpretation"])
        return cls(**d)


# spec B7 — fixed data_status display mapping. Reused (not re-defined) from
# schedule.py (ST1), which already established this exact table for the
# CLI's `status=` output; duplicating the 4-entry dict here would just be a
# second copy to drift out of sync.
from ..schedule import DATA_STATUS_KO  # noqa: E402

# spec B6 — data_status rank for "리포트 전체 등급 = 구성 fact 중 최악".
DATA_STATUS_RANK = {"source_verified": 0, "reconstructed": 1, "partial": 2, "unverified": 3}
DATA_STATUS_RANK_REVERSE = {v: k for k, v in DATA_STATUS_RANK.items()}


def worst_data_status(rows: list[FactRow]) -> str:
    """spec B6: report-wide grade = worst grade among its constituent facts.
    An empty report (no facts collected at all) is graded 'unverified' —
    nothing here has been confirmed, so it must not claim the best grade by
    default (spec §3.3: 미확인 = "추정으로 채우지 않고 확인 과제로 보류")."""
    if not rows:
        return "unverified"
    worst = max(DATA_STATUS_RANK.get(r.data_status, DATA_STATUS_RANK["unverified"]) for r in rows)
    return DATA_STATUS_RANK_REVERSE[worst]
