"""API-layer constants (not the agent's §6.3 enum contract)."""

from __future__ import annotations

BREAK_STATUS_OPEN = "open"
BREAK_STATUS_RESOLVED = "resolved"
BREAK_STATUS_OVERRIDDEN = "overridden"
BREAK_STATUS_REJECTED = "rejected"

TERMINAL_BREAK_STATUSES: frozenset[str] = frozenset(
    {BREAK_STATUS_RESOLVED, BREAK_STATUS_OVERRIDDEN}
)

AUDIT_APPROVED = "approved"
AUDIT_REJECTED = "rejected"
AUDIT_OVERRIDDEN = "overridden"

DEFAULT_ACTOR = "local-analyst"
ACTOR_HEADER = "X-Actor"
SCHEDULER_SECRET_HEADER = "X-Recon-Scheduler-Secret"

REVIEW_ONE_CLICK = "one_click"
REVIEW_MANUAL = "manual_review"
HIGH_CONFIDENCE_THRESHOLD = 0.85
LOW_NOTIONAL_THRESHOLD = 50_000.0

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
