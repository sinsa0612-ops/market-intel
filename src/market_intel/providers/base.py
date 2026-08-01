"""Provider protocol + small shared helpers (spec A5)."""
from __future__ import annotations

from ..models import CollectContext, Provider, ProviderResult

__all__ = ["Provider", "CollectContext", "ProviderResult", "no_data"]


def no_data(reason_code: str, safe_detail: str = "") -> ProviderResult:
    """Shorthand for the common 'nothing to do' result (e.g. missing key)."""
    return ProviderResult(status="NO_DATA", reason_code=reason_code, raw_items=[], facts=[], safe_detail=safe_detail)
