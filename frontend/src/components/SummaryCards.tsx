import type { SummaryResponse } from "../api/types";
import { formatNumber, formatPct, formatUsd } from "../lib/format";

export function SummaryCards({ summary }: { summary: SummaryResponse }) {
  const open = summary.open_break_count;
  return (
    <div className="cards">
      <div className="card tone-neutral">
        <div className="label">Total trades</div>
        <div className="value">{formatNumber(summary.total_trades)}</div>
        <div className="sub">{formatNumber(summary.match_count)} matches</div>
      </div>
      <div className="card tone-ok">
        <div className="label">Clean matched</div>
        <div className="value">{formatPct(summary.pct_clean_matched)}</div>
        <div className="sub">{formatNumber(summary.break_count)} breaks total</div>
      </div>
      <div className="card tone-warn">
        <div className="label">Open breaks</div>
        <div className="value">{formatNumber(open)}</div>
        <div className="sub">{(summary.breaks_by_type ?? []).length} types</div>
      </div>
      <div className="card tone-accent">
        <div className="label">Notional at risk</div>
        <div className="value">{formatUsd(summary.notional_at_risk)}</div>
        <div className="sub">Open breaks only</div>
      </div>
    </div>
  );
}
