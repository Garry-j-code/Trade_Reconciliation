"""Static desk reference used by ``get_desk_metadata``.

No desk table shipped with steps 1–3; this is the small static catalog
the agent tool reads. Keys match ``GeneratorConfig.desks``.
"""

from __future__ import annotations

from typing import Any

DESK_CATALOG: dict[str, dict[str, Any]] = {
    "EQ-US": {
        "desk_code": "EQ-US",
        "name": "US cash equities",
        "region": "US",
        "typical_break_rate": "low",
        "notes": (
            "High-volume cash book. Breaks are usually missing tickets or "
            "price-tolerance misses, not structural booking errors."
        ),
    },
    "EQ-ARB": {
        "desk_code": "EQ-ARB",
        "name": "Equity arb / relative value",
        "region": "US",
        "typical_break_rate": "medium",
        "notes": (
            "More split fills and multi-leg bookings. Quantity breaks are "
            "common when a block is allocated across fills."
        ),
    },
    "EQ-INDEX": {
        "desk_code": "EQ-INDEX",
        "name": "Index / program trading",
        "region": "US",
        "typical_break_rate": "low_medium",
        "notes": (
            "Program trades can lag the broker file around index rebalances "
            "and corporate-action effective dates."
        ),
    },
}


def get_desk(desk_code: str) -> dict[str, Any] | None:
    """Return catalog metadata for ``desk_code``, or None if unknown."""
    key = str(desk_code).strip().upper()
    return DESK_CATALOG.get(key)


def list_desks() -> list[dict[str, Any]]:
    return [dict(row) for row in DESK_CATALOG.values()]
