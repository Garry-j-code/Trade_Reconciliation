"""Cluster similar open breaks; investigate one representative per cluster.

Result is copied to siblings with ``inferred=True`` on ``resolution_suggestions``.
Clustering itself is deterministic (no LLM). Stamping ``breaks.cluster_id`` is
orchestration, not the agent write path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from backend.db.models import Break
from backend.pipeline.rules import as_date


def cluster_key(brk: Break | dict[str, Any]) -> tuple[str, str, date | None]:
    """Group by break type + symbol + trade date."""
    if isinstance(brk, dict):
        btype = str(brk.get("break_type") or "")
        symbol = str(brk.get("symbol") or "").upper()
        td = as_date(brk.get("trade_date"))
    else:
        btype = str(brk.break_type or "")
        symbol = str(brk.symbol or "").upper()
        td = as_date(brk.trade_date)
    return (btype, symbol, td)


def _break_id(brk: Break | dict[str, Any]) -> UUID:
    if isinstance(brk, dict):
        raw = brk.get("break_id")
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    return brk.break_id


def _created_at(brk: Break | dict[str, Any]) -> Any:
    if isinstance(brk, dict):
        return brk.get("created_at")
    return brk.created_at


@dataclass
class BreakCluster:
    cluster_id: UUID
    key: tuple[str, str, date | None]
    members: list[Break | dict[str, Any]] = field(default_factory=list)

    @property
    def representative(self) -> Break | dict[str, Any]:
        """Oldest member (stable) is the one the agent investigates."""
        return min(self.members, key=lambda b: (_created_at(b) is None, _created_at(b), str(_break_id(b))))

    @property
    def representative_id(self) -> UUID:
        return _break_id(self.representative)

    @property
    def sibling_ids(self) -> list[UUID]:
        rid = self.representative_id
        return [i for i in (_break_id(m) for m in self.members) if i != rid]


def cluster_breaks(breaks: Sequence[Break | dict[str, Any]]) -> list[BreakCluster]:
    """Partition breaks into clusters. Pure function — no DB writes."""
    buckets: dict[tuple[str, str, date | None], list[Break | dict[str, Any]]] = {}
    for brk in breaks:
        buckets.setdefault(cluster_key(brk), []).append(brk)
    clusters: list[BreakCluster] = []
    for key, members in buckets.items():
        clusters.append(BreakCluster(cluster_id=uuid4(), key=key, members=list(members)))
    clusters.sort(key=lambda c: (c.key[0], c.key[1], str(c.key[2]), str(c.cluster_id)))
    return clusters


def stamp_cluster_ids(session: Session, clusters: Iterable[BreakCluster]) -> int:
    """Write ``breaks.cluster_id``. Orchestration only — not used by persist."""
    updated = 0
    for cluster in clusters:
        for member in cluster.members:
            bid = _break_id(member)
            row = session.get(Break, bid)
            if row is None:
                continue
            row.cluster_id = cluster.cluster_id
            updated += 1
    return updated


def apply_output_across_cluster(
    representative_output: dict[str, Any],
    cluster: BreakCluster,
) -> list[dict[str, Any]]:
    """Copy the representative JSON onto every member; flag siblings inferred."""
    copies: list[dict[str, Any]] = []
    for member in cluster.members:
        bid = _break_id(member)
        payload = dict(representative_output)
        payload["break_id"] = str(bid)
        payload["inferred"] = bid != cluster.representative_id
        copies.append(payload)
    return copies
