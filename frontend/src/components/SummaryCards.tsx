import type { SummaryResponse } from "../api/types";
import { formatNumber, formatPct, formatUsd } from "../lib/format";

export function SummaryCards({ summary }: { summary: SummaryResponse }) {
  const open = summary.open_break_count;
  const pairs = summary.pair_count ?? summary.total_trades;
  const matchedPairs = summary.matched_pair_count ?? summary.match_count;
  const brokerLegs = summary.broker_leg_count;
  const deskLegs = summary.desk_leg_count;
  const legLine =
    brokerLegs != null && deskLegs != null
      ? `${formatNumber(brokerLegs)} broker / ${formatNumber(deskLegs)} desk legs`
      : `${formatNumber(matchedPairs)} matched pairs`;
  const matchLine = `${formatNumber(matchedPairs)} pairs`;

  return (
    <div className="cards">
      <div className="card tone-neutral">
        <div className="label">Total trades</div>
        <div className="value">{formatNumber(pairs)}</div>
        <div className="sub">{legLine}</div>
      </div>
      <div className="card tone-ok">
        <div className="label">Clean matched</div>
        <div className="value">{formatPct(summary.pct_clean_matched)}</div>
        <div className="sub">
          {matchLine} · {formatNumber(summary.break_count)} breaks
        </div>
      </div>
      <div className="card tone-warn">
        <div className="label">Open breaks</div>
        <div className="value">{formatNumber(open)}</div>
        <div className="sub">
          {(summary.break_type_options ?? summary.breaks_by_type ?? []).length} types
        </div>
      </div>
      <div className="card tone-accent">
        <div className="label">Notional at risk</div>
        <div className="value">{formatUsd(summary.notional_at_risk)}</div>
        <div className="sub">Open breaks only</div>
      </div>
    </div>
  );
}
