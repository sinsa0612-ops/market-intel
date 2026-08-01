"""Provider-facing data contracts (spec A5). Providers never touch the DB —
they return these dataclasses and the engine persists them."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Protocol

Status = Literal["OK", "PARTIAL", "NO_DATA", "ERROR"]

REASON_CODES = {
    "키없음",
    "미구현",
    "network_error",
    "rate_limited",
    "host_rejected",
    "empty_response",
    "market_closed",
}


@dataclass
class RawItem:
    external_id: str
    source_published_at: str
    safe_source_url: str
    payload: bytes | str
    fetch_status: str = "ok"


@dataclass
class FactCandidate:
    raw_ref: str  # RawItem.external_id this fact was derived from
    subject: str
    category: str
    metric: str
    event_at: str
    market: str
    country: str
    value_num: float | None = None
    value_text: str | None = None
    unit: str = ""
    comparison_basis: str = ""
    publisher: str = ""
    data_status: str = "source_verified"
    session_label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    safe_source_url: str = ""  # filled in by the engine from raw_ref's snapshot


@dataclass
class ProviderResult:
    status: Status
    reason_code: str | None
    raw_items: list[RawItem] = field(default_factory=list)
    facts: list[FactCandidate] = field(default_factory=list)
    safe_detail: str = ""


@dataclass
class CollectContext:
    cutoff: datetime
    now: datetime
    settings: Any
    http: Callable[[str], Any]  # provider name -> SafeHttp instance
    universe: Any
    logger: Any


class Provider(Protocol):
    name: str

    def collect(self, ctx: CollectContext) -> ProviderResult: ...
