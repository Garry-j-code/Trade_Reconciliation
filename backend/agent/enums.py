"""Pinned ``root_cause`` / ``suggested_action`` enums (project_plan.md §6.3 / §10).

These are the only allowed values the agent may emit. Dashboard aggregations
and the output contract depend on this closed set — never accept free text.
"""

from __future__ import annotations

from enum import Enum


class RootCause(str, Enum):
    """Why the pipeline flagged this break — judgment, not a new calculation."""

    MISSING_TRADE = "missing_trade"
    QUANTITY_MISMATCH = "quantity_mismatch"
    PRICE_MISMATCH = "price_mismatch"
    DUPLICATE_BOOKING = "duplicate_booking"
    SETTLEMENT_DATE_MISMATCH = "settlement_date_mismatch"
    SPLIT_FILL = "split_fill"
    CORPORATE_ACTION_TIMING = "corporate_action_timing"
    DESK_BOOKING_ERROR = "desk_booking_error"
    BROKER_REPORTING_LAG = "broker_reporting_lag"
    CALENDAR_TIMING = "calendar_timing"
    DATA_QUALITY = "data_quality"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class SuggestedAction(str, Enum):
    """Playbook action for ops — not a mutation of trade tables."""

    ACCEPT_BROKER = "accept_broker"
    ACCEPT_DESK = "accept_desk"
    AMEND_QUANTITY = "amend_quantity"
    AMEND_PRICE = "amend_price"
    AMEND_SETTLEMENT_DATE = "amend_settlement_date"
    CANCEL_DUPLICATE = "cancel_duplicate"
    BOOK_MISSING_TRADE = "book_missing_trade"
    WAIT_FOR_CORPORATE_ACTION = "wait_for_corporate_action"
    ESCALATE_TO_OPS = "escalate_to_ops"
    NO_ACTION = "no_action"


ROOT_CAUSE_VALUES: tuple[str, ...] = tuple(e.value for e in RootCause)
SUGGESTED_ACTION_VALUES: tuple[str, ...] = tuple(e.value for e in SuggestedAction)


def parse_root_cause(value: str) -> RootCause:
    """Parse a root_cause string; raise ValueError on unknown values."""
    try:
        return RootCause(value)
    except ValueError as exc:
        raise ValueError(
            f"Unknown root_cause {value!r}; allowed: {ROOT_CAUSE_VALUES}"
        ) from exc


def parse_suggested_action(value: str) -> SuggestedAction:
    """Parse a suggested_action string; raise ValueError on unknown values."""
    try:
        return SuggestedAction(value)
    except ValueError as exc:
        raise ValueError(
            f"Unknown suggested_action {value!r}; allowed: {SUGGESTED_ACTION_VALUES}"
        ) from exc
