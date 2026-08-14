---
name: corporate-action-analysis
description: Judge whether a cached split or dividend explains a quantity/price break. Do not recompute the matcher's arithmetic.
---

# Corporate-action analysis

Use `get_corporate_actions` for the break's symbol and trade date. The pipeline
already computed quantities and prices; you **compare** those stored numbers to
the cached split factor. You do **not** invent a new match, notional, or
adjusted price.

## Split timing (common)

Broker files are often split-adjusted while the desk blotter still shows
pre-split shares (or the reverse). The generator models this as a genuine
quantity mismatch, not an injected error.

Treat as `corporate_action_timing` when:

- A split `execution_date` falls within ~14 days of `trade_date`, **and**
- Pipeline `detail` quantities (broker vs desk) are consistent with the cached
  `split_to / split_from` factor **or its inverse**, **and**
- Notionals in `detail` (if present) roughly agree.

If those conditions hold, prefer `wait_for_corporate_action` rather than
`amend_quantity`. Ops is waiting for the lagging system to catch the CA, not
fixing a fat-finger.

## When it is NOT a corporate action

- No split/dividend in the window → do not force this root cause.
- Qty ratio is unrelated to the split factor (e.g. 10% injection) →
  `quantity_mismatch` / `desk_booking_error`.
- The matcher is supposed to **not** flag a CA-adjusted pair as a break. If you
  still see one, say so in the explanation and keep confidence lower — the
  pipeline may have lacked the split cache that day.

## Dividends

Cash dividends rarely cause share-quantity breaks. Only mention a dividend when
the ex-date is on/next to the trade date **and** the break is a small price
discrepancy that `detail` already quantified. Do not compute a dividend
adjustment yourself.

## Guardrail

Never output a newly calculated dollar amount. Quote pipeline `detail` figures
and the cached factor only.
