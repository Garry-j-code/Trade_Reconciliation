---
name: confidence-calibration
description: Numeric confidence rules — single strong signal vs conflicting vs none.
---

# Confidence calibration

`confidence` is a number in **[0.0, 1.0]**. It drives HITL routing (high
confidence + low notional → one-click approve; otherwise forced review). Do not
inflate it to look decisive. You still never auto-approve.

## Bands

| Band | Range | When |
|---|---|---|
| High | 0.85–0.95 | One strong, uncontradicted signal (e.g. split factor matches stored qty ratio; raw row clearly missing; obvious duplicate id). |
| Medium | 0.55–0.80 | Break type and one tool agree, but a second plausible cause remains. |
| Low | 0.20–0.50 | Tools conflict, cache miss, or you are analogizing from memory/precedent without a direct hit. |
| Floor | 0.10 | Budget exhausted or no useful tool result → `insufficient_evidence`. |

Never output 1.0. Leave room for ops override.

## Modifiers

- **+** cached corporate action that matches pipeline ratios: stay in the high band.
- **−** `get_similar_resolved_breaks` shows ops **overrode** this pattern: drop at least 0.15.
- **−** desk metadata says the desk is noisy **and** you have no raw-record confirmation: cap at 0.70.
- Memory retrieved: treat as a prior. Do not go high unless this break's own
  tools confirm the same cause.

If evidence is empty (JSON-only / no tools), cap confidence at 0.45 unless the
break type is unambiguous (e.g. `duplicate` with two identical ids in `detail`).
