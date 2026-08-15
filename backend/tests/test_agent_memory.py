"""Memory writer + stub embeddings. No live Bedrock."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.agent.memory_writer import (
    _parse_memory_notes,
    compact_old_notes,
    persist_memory_notes,
    run_memory_writer,
    semantic_notes_from_llm,
    upsert_decision_memory,
)
from backend.agent.providers import EMBEDDING_DIM, StubProvider, stub_embedding
from backend.db.models import AgentMemory, AuditLog, Break, ResolutionSuggestion, AuditLog, Break, ResolutionSuggestion


def test_stub_embedding_is_1536d_and_deterministic() -> None:
    a = stub_embedding("hello")
    b = stub_embedding("hello")
    c = stub_embedding("other")
    assert len(a) == EMBEDDING_DIM
    assert a == b
    assert a != c


def test_parse_memory_notes_filters_bad_types() -> None:
    text = json.dumps(
        {
            "notes": [
                {
                    "scope": "desk:EQ-US",
                    "memory_type": "pattern",
                    "content": "EQ-US price breaks are usually venue prints.",
                    "source_break_ids": [str(uuid4())],
                },
                {"scope": "global", "memory_type": "nope", "content": ""},
            ]
        }
    )
    notes = _parse_memory_notes(text)
    assert len(notes) == 1
    assert notes[0]["memory_type"] == "pattern"


def test_semantic_notes_from_stub_provider() -> None:
    provider = StubProvider(
        default_text=json.dumps(
            {
                "notes": [
                    {
                        "scope": "symbol:AAPL",
                        "memory_type": "override_reason",
                        "content": "Ops overrode CA timing last week.",
                        "source_break_ids": [],
                    }
                ]
            }
        )
    )
    notes = semantic_notes_from_llm(
        [{"break_id": str(uuid4()), "symbol": "AAPL"}], provider
    )
    assert notes[0]["scope"] == "symbol:AAPL"


def test_persist_memory_notes_embeds() -> None:
    added: list[AgentMemory] = []

    class _Session:
        def add(self, obj: AgentMemory) -> None:
            added.append(obj)

        def flush(self) -> None:
            return None

    persist_memory_notes(
        _Session(),  # type: ignore[arg-type]
        [
            {
                "scope": "global",
                "memory_type": "pattern",
                "content": "Override rate on price_mismatch is 20%.",
                "source_break_ids": [],
            }
        ],
        embed_fn=stub_embedding,
    )
    assert len(added) == 1
    assert added[0].embedding is not None
    assert len(added[0].embedding) == EMBEDDING_DIM


def test_compact_old_notes_groups_by_scope_month() -> None:
    old = AgentMemory(
        memory_id=uuid4(),
        scope="global",
        memory_type="incident",
        content="old note",
        created_at=datetime.now(timezone.utc) - timedelta(days=120),
    )
    deleted: list[AgentMemory] = []
    added: list[AgentMemory] = []

    class _Scalars:
        def all(self) -> list[AgentMemory]:
            return [old]

    class _Session:
        def scalars(self, _stmt: object) -> _Scalars:
            return _Scalars()

        def add(self, obj: AgentMemory) -> None:
            added.append(obj)

        def delete(self, obj: AgentMemory) -> None:
            deleted.append(obj)

        def flush(self) -> None:
            return None

    created = compact_old_notes(_Session(), embed_fn=stub_embedding)  # type: ignore[arg-type]
    assert created == 1
    assert deleted == [old]
    assert "Monthly compact" in added[0].content


def test_upsert_decision_memory_keeps_row_when_embed_fails() -> None:
    added: list[AgentMemory] = []

    class _Scalars:
        def first(self) -> None:
            return None

    class _Session:
        def scalars(self, _stmt: object) -> _Scalars:
            return _Scalars()

        def add(self, obj: AgentMemory) -> None:
            added.append(obj)

        def flush(self) -> None:
            return None

    def boom(_text: str) -> list[float]:
        raise RuntimeError("titan down")

    brk = Break(
        break_id=uuid4(),
        break_type="price_break",
        status="resolved",
        symbol="AAPL",
        pair_id="PAIR-1",
        detail={"desk": "EQ-US", "notional_at_risk": 1200.0},
    )
    audit = AuditLog(
        audit_id=uuid4(),
        break_id=brk.break_id,
        actor="analyst",
        action="approved",
        override_note="print is good",
    )
    sugg = ResolutionSuggestion(
        suggestion_id=uuid4(),
        break_id=brk.break_id,
        root_cause="price_mismatch",
        confidence=0.8,
        explanation="x",
        suggested_action="amend_price",
        evidence=[],
    )
    row = upsert_decision_memory(
        _Session(),  # type: ignore[arg-type]
        brk=brk,
        audit=audit,
        suggestion=sugg,
        embed_fn=boom,
    )
    assert row is not None
    assert added[0].embedding is None
    assert added[0].facts is not None
    assert added[0].facts["outcome"] == "approved"
    assert added[0].facts["break_type"] == "price_break"
    assert added[0].facts["symbol"] == "AAPL"
    assert added[0].facts["notional_band"] == "<10k"
    assert added[0].audit_id == audit.audit_id
    assert "print is good" in added[0].content


def test_run_memory_writer_skips_when_caught_up() -> None:
    class _Session:
        def execute(self, _stmt: object) -> list[tuple[object, object, object]]:
            return []

        def scalars(self, _stmt: object) -> object:
            class _S:
                def all(self) -> list[object]:
                    return []

                def first(self) -> None:
                    return None

            return _S()

        def add(self, _obj: object) -> None:
            raise AssertionError("caught-up writer must not insert")

        def flush(self) -> None:
            return None

    stats = run_memory_writer(
        _Session(),  # type: ignore[arg-type]
        StubProvider(default_text='{"notes": []}'),
        embed_fn=stub_embedding,
        write_semantic=False,
        skip_if_caught_up=True,
    )
    assert stats["skipped"] == 1
    assert stats["semantic"] == 0
    assert stats["backfill_caught_up"] == 1
