"""Cluster similar breaks; inferred copies for siblings."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from backend.agent.clustering import (
    apply_output_across_cluster,
    cluster_breaks,
    cluster_key,
)
from backend.agent.enums import RootCause, SuggestedAction
from backend.agent.persist import persist_cluster_copies, persist_suggestion
from backend.agent.schema import AgentOutput, EvidenceItem
from backend.db.models import ResolutionSuggestion


def _row(
    *,
    break_type: str = "quantity_break",
    symbol: str = "AAPL",
    trade_date: date = date(2024, 6, 10),
    created_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "break_id": uuid4(),
        "break_type": break_type,
        "symbol": symbol,
        "trade_date": trade_date,
        "created_at": created_at or datetime(2024, 6, 10, tzinfo=timezone.utc),
    }


def test_cluster_key_groups_type_symbol_date() -> None:
    a = _row()
    b = _row()
    c = _row(symbol="MSFT")
    assert cluster_key(a) == cluster_key(b)
    assert cluster_key(a) != cluster_key(c)


def test_cluster_breaks_picks_oldest_representative() -> None:
    older = _row(created_at=datetime(2024, 6, 1, tzinfo=timezone.utc))
    newer = _row(created_at=datetime(2024, 6, 2, tzinfo=timezone.utc))
    other = _row(break_type="price_break")
    clusters = cluster_breaks([newer, older, other])
    assert len(clusters) == 2
    qty = next(c for c in clusters if c.key[0] == "quantity_break")
    assert qty.representative_id == older["break_id"]
    assert qty.sibling_ids == [newer["break_id"]]


def test_apply_output_sets_inferred_on_siblings() -> None:
    members = [_row(), _row(), _row()]
    clusters = cluster_breaks(members)
    cluster = clusters[0]
    output = {
        "break_id": str(cluster.representative_id),
        "root_cause": "quantity_mismatch",
        "confidence": 0.6,
        "explanation": "Same pattern.",
        "suggested_action": "amend_quantity",
        "evidence": [],
    }
    copies = apply_output_across_cluster(output, cluster)
    inferred_flags = {c["break_id"]: c["inferred"] for c in copies}
    assert inferred_flags[str(cluster.representative_id)] is False
    assert all(
        inferred_flags[str(sid)] is True for sid in cluster.sibling_ids
    )


def test_persist_cluster_copies_are_inferred() -> None:
    output = AgentOutput(
        break_id=uuid4(),
        root_cause=RootCause.PRICE_MISMATCH,
        confidence=0.5,
        explanation="Representative investigation.",
        suggested_action=SuggestedAction.AMEND_PRICE,
        evidence=[EvidenceItem(tool="get_desk_metadata", result_summary="EQ-US")],
    )
    added: list[ResolutionSuggestion] = []

    class _Session:
        def add(self, obj: ResolutionSuggestion) -> None:
            added.append(obj)

        def flush(self) -> None:
            return None

    session = _Session()
    persist_suggestion(session, output, inferred=False)  # type: ignore[arg-type]
    sibs = [uuid4(), uuid4()]
    persist_cluster_copies(session, output, sibs)  # type: ignore[arg-type]
    assert added[0].inferred is False
    assert all(row.inferred for row in added[1:])
    assert {row.break_id for row in added[1:]} == set(sibs)
    assert all(isinstance(row, ResolutionSuggestion) for row in added)
