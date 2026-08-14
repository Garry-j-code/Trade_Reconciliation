---
name: break-triage
description: Decide investigation path — which tool to call first given the break's shape.
---

# Break triage

You investigate **one pipeline-computed break**. You do not re-match trades and you
do not compute new notionals, P&L, or price/quantity arithmetic. Use the
fields the pipeline already stored (`break_type`, `detail`, trade ids).

Hard cap: **5 tool calls**. Spend them. Do not wander.

## First tool by break type

| `break_type` | First tool | Why |
|---|---|---|
| `quantity_break` | `get_corporate_actions` | A real split/dividend may explain the qty ratio. |
| `price_break` | `get_corporate_actions` then `get_market_session_info` | Split-adjusted price vs holiday/early-close print. |
| `missing_broker` / `missing_desk` | `get_raw_records` then `get_trade_history` | Confirm the other side never booked vs a dropped file row. |
| `duplicate` | `get_raw_records` then `get_trade_history` | Same-day extras on this symbol/desk. |
| `settlement_date_mismatch` | `get_market_session_info` | Holiday / weekend / T+ settlement lag. |
| `split_fill` | `get_raw_records` | One desk block vs multiple broker fills. |
| unknown / other | `get_similar_resolved_breaks` | Look for a precedent before guessing. |

## Second-wave tools (if budget remains)

- `get_desk_metadata` — once, if a desk code is known and you need context on whether this desk is usually clean.
- `get_similar_resolved_breaks` — after you have a working hypothesis, check precedent.
- `get_relevant_memory` — **hypothesis only**. A retrieved note is not a verdict; confirm against this break's evidence.

## Stop calling tools when

- You have one coherent root cause supported by at least one tool result, or
- Two tools conflict and a third will not resolve it, or
- The budget is exhausted.

Then emit the JSON output contract. Do not call a tool just to fill the quota.
