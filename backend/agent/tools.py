"""Read-only, parameterized agent tools. No free-form SQL.

Every query uses bound parameters (SQLAlchemy expressions or Python filters).
The agent never receives a SQL string argument.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from backend.agent.cache import load_calendar, load_dividends, load_splits
from backend.agent.desks import get_desk, list_desks
from backend.agent.providers import (
    LLMProvider,
    StubProvider,
    embedder_from_env,
    pad_embedding,
    stub_embedding,
    try_embed,
)
from backend.db.models import (
    AgentMemory,
    AuditLog,
    Break,
    NormalizedTrade,
    RawBrokerTrade,
    RawDeskTrade,
    ResolutionSuggestion,
)
from backend.pipeline.rules import as_date

logger = logging.getLogger(__name__)

MAX_TOOL_ROWS = 50
MAX_ID_LIST = 50
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SQL_META = re.compile(r"(;|--|/\*|\*/|\b(DROP|DELETE|INSERT|UPDATE|ALTER|UNION)\b)", re.I)

TOOL_NAMES: tuple[str, ...] = (
    "get_corporate_actions",
    "get_market_session_info",
    "get_trade_history",
    "get_similar_resolved_breaks",
    "search_similar_breaks",
    "get_desk_metadata",
    "get_raw_records",
    "get_relevant_memory",
)


@dataclass
class InMemoryStore:
    """Test double for DB-backed tools. Never used as a SQL surface."""

    normalized_trades: list[dict[str, Any]] = field(default_factory=list)
    breaks: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    audit_log: list[dict[str, Any]] = field(default_factory=list)
    raw_broker: list[dict[str, Any]] = field(default_factory=list)
    raw_desk: list[dict[str, Any]] = field(default_factory=list)
    memory: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolContext:
    """Dependencies for tools. Session is optional — tests use ``store``."""

    cache_dir: Any
    session: Session | None = None
    store: InMemoryStore | None = None
    s3_bucket: str | None = None
    s3_prefix: str = "market-data"
    aws_region: str = "us-east-1"
    s3_client: Any | None = None
    embed_fn: Callable[[str], list[float]] | None = None
    max_rows: int = MAX_TOOL_ROWS


def attach_embedder(ctx: ToolContext, provider: LLMProvider | None = None) -> ToolContext:
    """Titan on live investigate; stub vectors when the LLM provider is stub."""
    if ctx.embed_fn is not None:
        return ctx
    if isinstance(provider, StubProvider):
        ctx.embed_fn = stub_embedding
        return ctx
    ctx.embed_fn = embedder_from_env().embed
    return ctx


def _clamp_limit(limit: Any, default: int = 20) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_TOOL_ROWS))


def _require_token(value: str, *, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} is required")
    if not _SAFE_TOKEN.match(text):
        raise ValueError(f"Invalid {field}: must be a short identifier, not SQL")
    return text


def _optional_token(value: Any, *, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _require_token(str(value), field=field)


def _parse_date(value: Any, *, field: str) -> date | None:
    if value is None or value == "":
        return None
    parsed = as_date(value)
    if parsed is None:
        raise ValueError(f"Invalid {field}: expected a date")
    return parsed


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return {k: _jsonable(v) for k, v in row.items()}
    data: dict[str, Any] = {}
    for col in row.__table__.columns:  # type: ignore[attr-defined]
        data[col.name] = _jsonable(getattr(row, col.name))
    return data


def _contains_sql_meta(value: str) -> bool:
    return bool(_SQL_META.search(value))


def compile_bound_sql(stmt: Select[Any]) -> tuple[str, dict[str, Any]]:
    """Compile a SQLAlchemy statement without literal binds (tests)."""
    compiled = stmt.compile()
    return str(compiled), dict(compiled.params)


def get_corporate_actions(
    ctx: ToolContext,
    *,
    symbol: str,
    as_of_date: Any,
    window_days: int = 14,
) -> dict[str, Any]:
    """Cached splits + dividends near ``as_of_date`` for ``symbol``."""
    ticker = _require_token(symbol, field="symbol").upper()
    on = _parse_date(as_of_date, field="as_of_date")
    if on is None:
        raise ValueError("as_of_date is required")
    try:
        window = int(window_days)
    except (TypeError, ValueError):
        window = 14
    window = max(0, min(window, 90))
    start = on - timedelta(days=window)
    end = on + timedelta(days=window)

    kwargs = dict(
        s3_bucket=ctx.s3_bucket,
        s3_prefix=ctx.s3_prefix,
        aws_region=ctx.aws_region,
        s3_client=ctx.s3_client,
    )
    splits = load_splits(ctx.cache_dir, **kwargs)
    dividends = load_dividends(ctx.cache_dir, **kwargs)

    split_rows: list[dict[str, Any]] = []
    if not splits.empty and "ticker" in splits.columns:
        for raw in splits.to_dict(orient="records"):
            if str(raw.get("ticker", "")).upper() != ticker:
                continue
            exec_d = as_date(raw.get("execution_date"))
            if exec_d is None or exec_d < start or exec_d > end:
                continue
            split_rows.append(
                {
                    "ticker": ticker,
                    "execution_date": exec_d.isoformat(),
                    "split_from": raw.get("split_from"),
                    "split_to": raw.get("split_to"),
                    "adjustment_type": raw.get("adjustment_type"),
                }
            )

    div_rows: list[dict[str, Any]] = []
    if not dividends.empty and "ticker" in dividends.columns:
        for raw in dividends.to_dict(orient="records"):
            if str(raw.get("ticker", "")).upper() != ticker:
                continue
            ex_d = as_date(raw.get("ex_dividend_date"))
            if ex_d is None or ex_d < start or ex_d > end:
                continue
            pay = as_date(raw.get("pay_date"))
            div_rows.append(
                {
                    "ticker": ticker,
                    "ex_dividend_date": ex_d.isoformat(),
                    "cash_amount": raw.get("cash_amount"),
                    "pay_date": pay.isoformat() if pay else None,
                }
            )

    return {
        "symbol": ticker,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "splits": split_rows[:MAX_TOOL_ROWS],
        "dividends": div_rows[:MAX_TOOL_ROWS],
    }


def get_market_session_info(ctx: ToolContext, *, trade_date: Any) -> dict[str, Any]:
    """Cached trading calendar for ``trade_date`` (holiday / early close)."""
    on = _parse_date(trade_date, field="trade_date")
    if on is None:
        raise ValueError("trade_date is required")
    calendar = load_calendar(
        ctx.cache_dir,
        s3_bucket=ctx.s3_bucket,
        s3_prefix=ctx.s3_prefix,
        aws_region=ctx.aws_region,
        s3_client=ctx.s3_client,
    )
    weekday = on.strftime("%A")
    is_weekend = on.weekday() >= 5
    match: dict[str, Any] | None = None
    if not calendar.empty and "date" in calendar.columns:
        for raw in calendar.to_dict(orient="records"):
            d = as_date(raw.get("date"))
            if d == on:
                match = {
                    "date": d.isoformat(),
                    "exchange": raw.get("exchange"),
                    "name": raw.get("name"),
                    "status": raw.get("status"),
                    "open": raw.get("open"),
                    "close": raw.get("close"),
                }
                break
    if match:
        status = str(match.get("status") or "unknown").lower()
    elif is_weekend:
        status = "closed"
    else:
        status = "open_assumed"
    return {
        "trade_date": on.isoformat(),
        "weekday": weekday,
        "is_weekend": is_weekend,
        "session": match,
        "status": status,
    }


def _history_from_store(
    store: InMemoryStore,
    *,
    symbol: str,
    desk: str | None,
    start: date | None,
    end: date | None,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in store.normalized_trades:
        if str(raw.get("symbol", "")).upper() != symbol:
            continue
        if desk and str(raw.get("account", "")).upper() != desk:
            continue
        td = as_date(raw.get("trade_date"))
        if start and td is not None and td < start:
            continue
        if end and td is not None and td > end:
            continue
        rows.append(_row_to_dict(raw))
        if len(rows) >= limit:
            break
    return rows


def get_trade_history(
    ctx: ToolContext,
    *,
    symbol: str,
    desk: str | None = None,
    start_date: Any = None,
    end_date: Any = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Parameterized history from ``normalized_trades``."""
    ticker = _require_token(symbol, field="symbol").upper()
    desk_code = _optional_token(desk, field="desk")
    if desk_code:
        desk_code = desk_code.upper()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")
    cap = _clamp_limit(limit)

    stmt = trade_history_stmt(
        ticker=ticker, desk_code=desk_code, start=start, end=end, cap=cap
    )
    if ctx.session is None:
        store = ctx.store or InMemoryStore()
        rows = _history_from_store(
            store, symbol=ticker, desk=desk_code, start=start, end=end, limit=cap
        )
        return {"symbol": ticker, "count": len(rows), "trades": rows}

    rows = [_row_to_dict(r) for r in ctx.session.scalars(stmt).all()]
    return {"symbol": ticker, "count": len(rows), "trades": rows}


def trade_history_stmt(
    *,
    ticker: str,
    desk_code: str | None,
    start: date | None,
    end: date | None,
    cap: int,
) -> Select[tuple[NormalizedTrade]]:
    """Bound-parameter SELECT used by ``get_trade_history`` (and tests)."""
    stmt = select(NormalizedTrade).where(NormalizedTrade.symbol == ticker)
    if desk_code:
        stmt = stmt.where(NormalizedTrade.account == desk_code)
    if start:
        stmt = stmt.where(NormalizedTrade.trade_date >= start)
    if end:
        stmt = stmt.where(NormalizedTrade.trade_date <= end)
    return stmt.order_by(NormalizedTrade.trade_date.desc()).limit(cap)


def get_similar_resolved_breaks(
    ctx: ToolContext,
    *,
    break_type: str,
    symbol: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Resolved breaks + suggestions + latest audit row (parameterized)."""
    btype = _require_token(break_type, field="break_type")
    ticker = _optional_token(symbol, field="symbol")
    if ticker:
        ticker = ticker.upper()
    cap = _clamp_limit(limit, default=10)

    if ctx.session is None:
        store = ctx.store or InMemoryStore()
        out: list[dict[str, Any]] = []
        for brk in store.breaks:
            if str(brk.get("break_type")) != btype:
                continue
            if str(brk.get("status", "open")) not in {"resolved", "overridden"}:
                continue
            if ticker and str(brk.get("symbol", "")).upper() != ticker:
                continue
            bid = str(brk.get("break_id"))
            sugg = next(
                (s for s in store.suggestions if str(s.get("break_id")) == bid),
                None,
            )
            audit = next(
                (a for a in store.audit_log if str(a.get("break_id")) == bid),
                None,
            )
            out.append(
                {
                    "break": _row_to_dict(brk),
                    "suggestion": _row_to_dict(sugg) if sugg else None,
                    "audit": _row_to_dict(audit) if audit else None,
                }
            )
            if len(out) >= cap:
                break
        return {"count": len(out), "breaks": out}

    stmt = (
        select(Break)
        .where(Break.break_type == btype)
        .where(Break.status.in_(("resolved", "overridden")))
    )
    if ticker:
        stmt = stmt.where(Break.symbol == ticker)
    stmt = stmt.order_by(Break.created_at.desc()).limit(cap)
    out_db: list[dict[str, Any]] = []
    for brk in ctx.session.scalars(stmt).all():
        sugg = ctx.session.scalars(
            select(ResolutionSuggestion)
            .where(ResolutionSuggestion.break_id == brk.break_id)
            .order_by(ResolutionSuggestion.created_at.desc())
            .limit(1)
        ).first()
        audit = ctx.session.scalars(
            select(AuditLog)
            .where(AuditLog.break_id == brk.break_id)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        ).first()
        out_db.append(
            {
                "break": _row_to_dict(brk),
                "suggestion": _row_to_dict(sugg) if sugg else None,
                "audit": _row_to_dict(audit) if audit else None,
            }
        )
    return {"count": len(out_db), "breaks": out_db}


def get_desk_metadata(ctx: ToolContext, *, desk_code: str | None = None) -> dict[str, Any]:
    """Static desk catalog (no live API). ``ctx`` reserved for a future table."""
    _ = ctx
    if desk_code is None or desk_code == "":
        return {"desks": list_desks()}
    code = _require_token(desk_code, field="desk_code").upper()
    row = get_desk(code)
    if row is None:
        return {"desk_code": code, "found": False, "desk": None}
    return {"desk_code": code, "found": True, "desk": dict(row)}


def _filter_ids(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v.strip() for v in values.split(",") if v.strip()]
    if not isinstance(values, (list, tuple)):
        raise ValueError("trade ids must be a list of identifiers")
    out: list[str] = []
    for raw in values:
        token = _require_token(str(raw), field="trade_id")
        out.append(token)
        if len(out) >= MAX_ID_LIST:
            break
    return out


def get_raw_records(
    ctx: ToolContext,
    *,
    broker_trade_ids: list[str] | str | None = None,
    desk_trade_ids: list[str] | str | None = None,
) -> dict[str, Any]:
    """Untouched raw broker / desk rows by id (parameterized IN)."""
    broker_ids = _filter_ids(broker_trade_ids)
    desk_ids = _filter_ids(desk_trade_ids)
    if not broker_ids and not desk_ids:
        raise ValueError("Provide broker_trade_ids and/or desk_trade_ids")

    if ctx.session is None:
        store = ctx.store or InMemoryStore()
        broker = [
            _row_to_dict(r)
            for r in store.raw_broker
            if str(r.get("broker_trade_id")) in set(broker_ids)
        ]
        desk = [
            _row_to_dict(r)
            for r in store.raw_desk
            if str(r.get("blotter_id")) in set(desk_ids)
        ]
        return {"broker": broker, "desk": desk}

    broker_rows: list[dict[str, Any]] = []
    desk_rows: list[dict[str, Any]] = []
    if broker_ids:
        stmt = select(RawBrokerTrade).where(
            RawBrokerTrade.broker_trade_id.in_(broker_ids)
        )
        broker_rows = [_row_to_dict(r) for r in ctx.session.scalars(stmt).all()]
    if desk_ids:
        stmt = select(RawDeskTrade).where(RawDeskTrade.blotter_id.in_(desk_ids))
        desk_rows = [_row_to_dict(r) for r in ctx.session.scalars(stmt).all()]
    return {"broker": broker_rows, "desk": desk_rows}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _memory_query_vector(ctx: ToolContext, text: str) -> list[float] | None:
    embed = ctx.embed_fn or stub_embedding
    return try_embed(embed, text) or pad_embedding(stub_embedding(text))


def _facts_match(
    facts: dict[str, Any] | None,
    *,
    break_type: str | None,
    symbol: str | None,
    desk: str | None,
) -> bool:
    if not facts:
        return True
    stored_type = str(facts.get("break_type") or "")
    if break_type and stored_type and stored_type != break_type:
        return False
    if symbol:
        stored = str(facts.get("symbol") or "").upper()
        if stored and stored != symbol.upper():
            return False
    if desk:
        desks = facts.get("desks") or []
        names = {str(d).upper() for d in desks}
        if names and desk.upper() not in names:
            return False
    return True


def _note_from_memory_raw(raw: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else {}
    note: dict[str, Any] = {
        "memory_id": _jsonable(raw.get("memory_id")),
        "scope": raw.get("scope"),
        "memory_type": raw.get("memory_type"),
        "content": raw.get("content"),
        "break_type": facts.get("break_type"),
        "symbol": facts.get("symbol"),
        "desks": facts.get("desks"),
        "root_cause": facts.get("root_cause"),
        "suggested_action": facts.get("suggested_action"),
        "outcome": facts.get("outcome"),
        "actor_note": facts.get("actor_note"),
        "notional_band": facts.get("notional_band"),
        "pair_id": facts.get("pair_id"),
        "guardrail": "Hypothesis only — confirm against this break's evidence.",
    }
    if score is not None:
        note["score"] = round(score, 4)
    return note


def _note_from_orm(row: AgentMemory, score: float | None = None) -> dict[str, Any]:
    facts = row.facts if isinstance(row.facts, dict) else {}
    return _note_from_memory_raw(
        {
            "memory_id": row.memory_id,
            "scope": row.scope,
            "memory_type": row.memory_type,
            "content": row.content,
            "facts": facts,
        },
        score=score,
    )


def get_relevant_memory(
    ctx: ToolContext,
    *,
    query_text: str,
    scope: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """pgvector (or in-memory cosine) similarity over ``agent_memory``."""
    text = str(query_text or "").strip()
    if not text:
        raise ValueError("query_text is required")
    if len(text) > 2000:
        text = text[:2000]
    _ = _contains_sql_meta(text)
    scope_key = None
    if scope:
        scope_key = _optional_token(scope, field="scope")
    cap = _clamp_limit(limit, default=5)
    query_vec = _memory_query_vector(ctx, text)

    if ctx.session is None:
        store = ctx.store or InMemoryStore()
        scored: list[tuple[float, dict[str, Any]]] = []
        for raw in store.memory:
            if scope_key and str(raw.get("scope")) != scope_key:
                continue
            vec = raw.get("embedding")
            if not isinstance(vec, list):
                vec = (ctx.embed_fn or stub_embedding)(str(raw.get("content") or ""))
            scored.append((_cosine(query_vec, [float(x) for x in vec]), raw))
        scored.sort(key=lambda t: t[0], reverse=True)
        notes = [_note_from_memory_raw(raw, score) for score, raw in scored[:cap]]
        return {
            "count": len(notes),
            "notes": notes,
            "guardrail": "Memory is a prior, not a verdict — confirm against this break.",
        }

    stmt = select(AgentMemory)
    if scope_key:
        stmt = stmt.where(AgentMemory.scope == scope_key)
    try:
        stmt = stmt.order_by(AgentMemory.embedding.cosine_distance(query_vec)).limit(cap)
        rows = list(ctx.session.scalars(stmt).all())
    except Exception:  # noqa: BLE001 — fall back to recency if pgvector missing
        logger.warning("pgvector order_by failed; falling back to recency")
        stmt = select(AgentMemory)
        if scope_key:
            stmt = stmt.where(AgentMemory.scope == scope_key)
        stmt = stmt.order_by(AgentMemory.created_at.desc()).limit(cap)
        rows = list(ctx.session.scalars(stmt).all())

    notes = [_note_from_orm(row) for row in rows]
    return {
        "count": len(notes),
        "notes": notes,
        "guardrail": "Memory is a prior, not a verdict — confirm against this break.",
    }


def search_similar_breaks(
    ctx: ToolContext,
    *,
    break_type: str,
    symbol: str | None = None,
    desk: str | None = None,
    query_text: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Top-k similar human-resolved cases from ``agent_memory`` (parameterized)."""
    btype = _require_token(break_type, field="break_type")
    ticker = _optional_token(symbol, field="symbol")
    if ticker:
        ticker = ticker.upper()
    desk_code = _optional_token(desk, field="desk")
    if desk_code:
        desk_code = desk_code.upper()
    cap = _clamp_limit(limit, default=5)
    text = str(query_text or "").strip()
    if len(text) > 2000:
        text = text[:2000]
    if not text:
        text = " ".join(p for p in (btype, ticker or "", desk_code or "") if p)
    query_vec = _memory_query_vector(ctx, text)

    if ctx.session is None:
        store = ctx.store or InMemoryStore()
        scored: list[tuple[float, dict[str, Any]]] = []
        for raw in store.memory:
            facts = raw.get("facts") if isinstance(raw.get("facts"), dict) else None
            if not _facts_match(facts, break_type=btype, symbol=ticker, desk=desk_code):
                continue
            vec = raw.get("embedding")
            if not isinstance(vec, list):
                content = str(raw.get("content") or "")
                vec = (ctx.embed_fn or stub_embedding)(content)
            scored.append((_cosine(query_vec, [float(x) for x in vec]), raw))
        scored.sort(key=lambda t: t[0], reverse=True)
        cases = [_note_from_memory_raw(raw, score) for score, raw in scored[:cap]]
        return {
            "count": len(cases),
            "cases": cases,
            "guardrail": (
                "Similar resolved cases are a prior. Bias root_cause and "
                "suggested_action toward the pinned enums only if this break's evidence agrees."
            ),
        }

    stmt = select(AgentMemory).where(AgentMemory.audit_id.isnot(None))
    try:
        stmt = stmt.order_by(AgentMemory.embedding.cosine_distance(query_vec)).limit(max(cap * 4, 20))
        rows = list(ctx.session.scalars(stmt).all())
    except Exception:  # noqa: BLE001
        logger.warning("pgvector search_similar_breaks failed; falling back to recency")
        stmt = (
            select(AgentMemory)
            .where(AgentMemory.audit_id.isnot(None))
            .order_by(AgentMemory.created_at.desc())
            .limit(max(cap * 4, 20))
        )
        rows = list(ctx.session.scalars(stmt).all())

    filtered: list[dict[str, Any]] = []
    for row in rows:
        facts = row.facts if isinstance(row.facts, dict) else {}
        if not _facts_match(facts, break_type=btype, symbol=ticker, desk=desk_code):
            continue
        filtered.append(_note_from_orm(row))
        if len(filtered) >= cap:
            break
    return {
        "count": len(filtered),
        "cases": filtered,
        "guardrail": (
            "Similar resolved cases are a prior. Bias root_cause and "
            "suggested_action toward the pinned enums only if this break's evidence agrees."
        ),
    }


TOOL_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_corporate_actions": get_corporate_actions,
    "get_market_session_info": get_market_session_info,
    "get_trade_history": get_trade_history,
    "get_similar_resolved_breaks": get_similar_resolved_breaks,
    "search_similar_breaks": search_similar_breaks,
    "get_desk_metadata": get_desk_metadata,
    "get_raw_records": get_raw_records,
    "get_relevant_memory": get_relevant_memory,
}


def dispatch_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Allowlisted dispatch. Unknown names and extra SQL args are rejected."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool {name!r}", "allowed": list(TOOL_NAMES)}
    if not isinstance(arguments, dict):
        return {"error": "Tool arguments must be an object"}
    if "sql" in {k.lower() for k in arguments}:
        return {"error": "Free-form SQL is not allowed"}
    try:
        return handler(ctx, **arguments)
    except TypeError as exc:
        return {"error": f"Invalid arguments for {name}: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}


def summarize_tool_result(name: str, result: dict[str, Any]) -> str:
    """Short analyst-facing line for ``evidence[].result_summary``."""
    if result.get("error"):
        return f"{name} error: {result['error']}"
    if name == "get_corporate_actions":
        n_s = len(result.get("splits") or [])
        n_d = len(result.get("dividends") or [])
        return f"{result.get('symbol')}: {n_s} split(s), {n_d} dividend(s) in window"
    if name == "get_market_session_info":
        return f"{result.get('trade_date')} status={result.get('status')}"
    if name == "get_trade_history":
        return f"{result.get('symbol')}: {result.get('count', 0)} normalized trade(s)"
    if name == "get_similar_resolved_breaks":
        return f"{result.get('count', 0)} similar resolved break(s)"
    if name == "search_similar_breaks":
        return f"{result.get('count', 0)} similar human-resolved case(s) (prior only)"
    if name == "get_desk_metadata":
        if "desks" in result:
            return f"{len(result['desks'])} desk(s) in catalog"
        if result.get("found"):
            desk = result.get("desk") or {}
            return f"{desk.get('desk_code')}: {desk.get('typical_break_rate')} break rate"
        return f"{result.get('desk_code')}: not in catalog"
    if name == "get_raw_records":
        nb = len(result.get("broker") or [])
        nd = len(result.get("desk") or [])
        return f"raw records: {nb} broker, {nd} desk"
    if name == "get_relevant_memory":
        return f"{result.get('count', 0)} memory note(s) (prior only)"
    return f"{name} returned {len(result)} key(s)"


def bedrock_tool_specs() -> list[dict[str, Any]]:
    """Converse ``toolConfig.tools`` entries (parameterized JSON schemas)."""
    return [
        {
            "toolSpec": {
                "name": "get_corporate_actions",
                "description": (
                    "Cached splits and dividends for a symbol near a date. "
                    "Read-only Parquet cache (optional S3 fallback)."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "required": ["symbol", "as_of_date"],
                        "additionalProperties": False,
                        "properties": {
                            "symbol": {"type": "string"},
                            "as_of_date": {"type": "string", "description": "YYYY-MM-DD"},
                            "window_days": {"type": "integer", "minimum": 0, "maximum": 90},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_market_session_info",
                "description": "Cached market calendar (holiday / early close) for a trade date.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "required": ["trade_date"],
                        "additionalProperties": False,
                        "properties": {
                            "trade_date": {"type": "string", "description": "YYYY-MM-DD"},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_trade_history",
                "description": "Parameterized normalized_trades history for a symbol.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "required": ["symbol"],
                        "additionalProperties": False,
                        "properties": {
                            "symbol": {"type": "string"},
                            "desk": {"type": "string"},
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_similar_resolved_breaks",
                "description": "Prior resolved/overridden breaks of the same type.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "required": ["break_type"],
                        "additionalProperties": False,
                        "properties": {
                            "break_type": {"type": "string"},
                            "symbol": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "search_similar_breaks",
                "description": (
                    "Top-k similar human Approve/Reject cases from agent_memory "
                    "(pgvector). Hypothesis only — bias pinned root_cause / "
                    "suggested_action enums if evidence agrees."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "required": ["break_type"],
                        "additionalProperties": False,
                        "properties": {
                            "break_type": {"type": "string"},
                            "symbol": {"type": "string"},
                            "desk": {"type": "string"},
                            "query_text": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_desk_metadata",
                "description": "Static desk reference (typical break rate, notes).",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"desk_code": {"type": "string"}},
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_raw_records",
                "description": "Untouched raw broker/desk rows by trade id list.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "broker_trade_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "desk_trade_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "get_relevant_memory",
                "description": "Semantic notes from agent_memory. Hypothesis only.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "required": ["query_text"],
                        "additionalProperties": False,
                        "properties": {
                            "query_text": {"type": "string"},
                            "scope": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                    }
                },
            }
        },
    ]
