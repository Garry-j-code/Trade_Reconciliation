---
name: root-cause-classification
description: Map evidence onto the pinned root_cause enum, with tie-breaking rules. Never emit free text.
---

# Root-cause classification

`root_cause` **must** be one of the pinned enum values. If nothing fits, use
`insufficient_evidence` — do not invent a new category.

## Mapping (break type is a prior, not a verdict)

| Evidence | `root_cause` |
|---|---|
| One-sided: raw record missing on one leg, history shows no pair | `missing_trade` |
| Both sides present; qty differs; no CA factor match | `quantity_mismatch` |
| Both sides present; price differs beyond pipeline tolerance; no CA | `price_mismatch` |
| Extra same-day booking of the same symbol/side | `duplicate_booking` |
| Settle dates differ; calendar shows holiday/weekend/T+ shift | `settlement_date_mismatch` |
| One desk block vs 2+ broker fills (ids in `detail` / raw records) | `split_fill` |
| Qty/price ratio matches a cached split factor | `corporate_action_timing` |
| Desk-side extra/wrong qty or price; desk is historically noisy | `desk_booking_error` |
| Broker file lag (desk booked, broker not yet / late CA adjust) | `broker_reporting_lag` |
| Trade date on a closed session / early close explains timing | `calendar_timing` |
| Garbage ids, unparseable fields, conflicting raw vs normalized | `data_quality` |
| Tools empty, conflict, or budget exhausted with no thesis | `insufficient_evidence` |

## Tie-breakers (apply in order)

1. **Corporate action over generic qty/price.** If the split factor explains
   the pipeline's stored ratio, classify `corporate_action_timing` even if
   `break_type` is `quantity_break` or `price_break`.
2. **Calendar over generic settlement.** Closed or early-close session →
   `calendar_timing` or `settlement_date_mismatch` (settlement if only T+ dates
   disagree; calendar if the trade date itself is a non-session).
3. **Precedent is a hypothesis.** `search_similar_breaks` / `get_similar_resolved_breaks`
   / `get_relevant_memory` may suggest a cause; keep it only if this break's own
   evidence agrees. Copy `root_cause` and `suggested_action` from the pinned enums.
4. **Do not upgrade confidence to pick a prettier enum.** If two causes remain
   plausible, pick the more specific one and lower confidence, or use
   `insufficient_evidence` and `escalate_to_ops`.

Human overrides in `audit_log` describe what ops did last time — they do not
automatically classify this break.
