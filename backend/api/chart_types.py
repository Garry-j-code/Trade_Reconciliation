"""Dashboard chart type rollup. Display-only — not matching, no LLM.

Chart and Breaks type filter use agent ``root_cause`` after investigation, or
``unclassified`` until then. Pipeline ``break_type`` stays a matcher fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.enums import ROOT_CAUSE_VALUES

# Chart "Others": a display type is rolled up when its share of the current
# series is < 5% OR its count is < 3. If that would name zero bars, keep the
# top MAX_NAMED_BARS and dump only the leftover tail into Others.
OTHERS_MIN_SHARE = 0.05
OTHERS_MIN_COUNT = 3
MAX_NAMED_BARS = 8
OTHERS_CHART_KEY = "__others__"
OTHERS_LABEL = "Others"
UNCLASSIFIED_DISPLAY_TYPE = "unclassified"


@dataclass(frozen=True)
class ChartTypeRollup:
    chart: list[dict[str, Any]]
    options: list[str]
    others_members: list[str]


def displayed_break_category(row: Any) -> str:
    """Agent root_cause when a suggestion exists; otherwise unclassified.

    Matcher ``break_type`` is not a chart series. Unknown stored causes map
    to enum ``other`` rather than free text.
    """
    try:
        suggestions = list(getattr(row, "suggestions", None) or [])
    except Exception:  # noqa: BLE001 — detached rows stay unclassified
        suggestions = []
    if not suggestions:
        return UNCLASSIFIED_DISPLAY_TYPE
    latest = max(
        suggestions,
        key=lambda s: (
            getattr(s, "created_at", None) is not None,
            getattr(s, "created_at", None),
            str(getattr(s, "suggestion_id", "")),
        ),
    )
    root = str(getattr(latest, "root_cause", "") or "")
    if root in ROOT_CAUSE_VALUES:
        return root
    return "other"


def _is_rare(count: int, total: int) -> bool:
    if total <= 0:
        return True
    return (count / total) < OTHERS_MIN_SHARE or count < OTHERS_MIN_COUNT


def rollup_chart_types(counts: dict[str, int]) -> ChartTypeRollup:
    """Build chart bars plus the real-type option list (no Others in options)."""
    clean = {
        str(key): int(value)
        for key, value in counts.items()
        if key and str(key) != OTHERS_CHART_KEY and int(value) > 0
    }
    options = sorted(clean)
    total = sum(clean.values())
    if total <= 0:
        return ChartTypeRollup(chart=[], options=[], others_members=[])

    ranked = sorted(clean.items(), key=lambda item: (-item[1], item[0]))
    named = [(key, n) for key, n in ranked if not _is_rare(n, total)]
    rare = [(key, n) for key, n in ranked if _is_rare(n, total)]
    if not named:
        named = ranked[:MAX_NAMED_BARS]
        rare = ranked[MAX_NAMED_BARS:]
    if not rare:
        return ChartTypeRollup(
            chart=[{"break_type": key, "count": n} for key, n in named],
            options=options,
            others_members=[],
        )
    others_n = sum(n for _, n in rare)
    members = [key for key, _ in sorted(rare, key=lambda item: item[0])]
    chart = [{"break_type": key, "count": n} for key, n in named]
    chart.append(
        {
            "break_type": OTHERS_CHART_KEY,
            "count": others_n,
            "members": members,
        }
    )
    return ChartTypeRollup(chart=chart, options=options, others_members=members)


def parse_break_type_filter(raw: str | None) -> list[str]:
    """Split ``break_type`` query: one name, comma list, or ``__others__``."""
    if raw is None:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]
