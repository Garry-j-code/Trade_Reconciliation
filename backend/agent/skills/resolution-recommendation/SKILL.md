---
name: resolution-recommendation
description: Map a classified root cause to the pinned suggested_action playbook.
---

# Resolution recommendation

`suggested_action` **must** be one of the pinned enum values. This is a
**proposal**. You never write to `trades`, `matches`, or `breaks`. A human
approves before anything is resolved.

## Playbook

| `root_cause` | Default `suggested_action` | When to override |
|---|---|---|
| `missing_trade` | `book_missing_trade` | If the surviving side looks like a busted ticket → `escalate_to_ops` |
| `quantity_mismatch` | `amend_quantity` | If desk is source of truth (block vs fills) → `accept_desk`; if broker file is cleaner → `accept_broker` |
| `price_mismatch` | `amend_price` | Same accept-broker / accept-desk split when one side is clearly the market print |
| `duplicate_booking` | `cancel_duplicate` | — |
| `settlement_date_mismatch` | `amend_settlement_date` | If a holiday explains T+ → still amend to the convention date |
| `split_fill` | `accept_broker` | Desk booked the block; broker fills are the economic truth. Use `accept_desk` only if fills don't sum in `detail` |
| `corporate_action_timing` | `wait_for_corporate_action` | — |
| `desk_booking_error` | `accept_broker` | — |
| `broker_reporting_lag` | `accept_desk` | If both sides look incomplete → `escalate_to_ops` |
| `calendar_timing` | `no_action` | If settlement is also wrong → `amend_settlement_date` |
| `data_quality` | `escalate_to_ops` | — |
| `insufficient_evidence` | `escalate_to_ops` | — |

Do not suggest `no_action` for economic mismatches (qty/price/missing). That
action is for informational calendar noise only.

The approval flow (not you) writes `audit_log` and updates break status.
