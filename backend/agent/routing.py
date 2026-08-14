"""HITL routing helper — confidence + notional decide review strictness.

This never auto-approves. High confidence + low notional only means the
dashboard may offer one-click approve; a human still confirms.
"""

from __future__ import annotations

from typing import Literal

HIGH_CONFIDENCE = 0.85
LOW_NOTIONAL_USD = 50_000.0

ReviewRoute = Literal["one_click", "manual_review"]


def review_routing(
    confidence: float,
    notional: float | None,
) -> ReviewRoute:
    """Return dashboard routing. Never skip the human gate."""
    conf = float(confidence)
    if conf >= HIGH_CONFIDENCE and (
        notional is None or float(notional) <= LOW_NOTIONAL_USD
    ):
        return "one_click"
    return "manual_review"


def notional_from_detail(detail: dict[str, object] | None) -> float | None:
    """Best-effort notional from pipeline ``breaks.detail`` JSON."""
    if not detail:
        return None
    for key in ("notional", "notional_usd", "abs_notional", "notional_at_risk"):
        value = detail.get(key)
        if isinstance(value, (int, float)):
            return abs(float(value))
    broker_n = detail.get("broker_notional")
    desk_n = detail.get("desk_notional")
    nums = [
        abs(float(v))
        for v in (broker_n, desk_n)
        if isinstance(v, (int, float))
    ]
    if nums:
        return max(nums)
    return None
