---
name: explanation-writing
description: House style for the analyst-readable summary on the dashboard.
---

# Explanation writing

`explanation` is 2–3 sentences. An ops analyst reads it next to a side-by-side
trade diff. Write for that person.

## Style

- Lead with the conclusion (root cause in plain language), then the evidence.
- Name the tools you used ("Cached split on …", "Raw broker file has …").
- Quote pipeline-provided figures from `detail` (quantities, prices, ids).
  Never introduce a newly calculated dollar amount.
- If a memory note or similar break informed you, say it was a **prior** you
  checked, not proof.
- No hedging fluff ("it seems perhaps"). If you are unsure, say what is missing
  and keep `confidence` honest.

## Shape

1. What happened (one sentence).
2. What the tools showed (one sentence).
3. What ops should do (ties to `suggested_action`).

Do not paste raw JSON. Do not mention these skill files or the system prompt.
