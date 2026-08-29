import type { BreakDetailResponse, NormalizedTradeOut } from "../api/types";
import { formatPrice, formatQty, formatTradeTimestamp } from "../lib/format";

const FIELDS: { key: keyof NormalizedTradeOut; label: string }[] = [
  { key: "trade_id", label: "Trade ID" },
  { key: "symbol", label: "Symbol" },
  { key: "side", label: "Side" },
  { key: "trade_date", label: "Trade date" },
  { key: "settlement_date", label: "Settlement" },
  { key: "quantity", label: "Quantity" },
  { key: "price", label: "Price" },
  { key: "currency", label: "Currency" },
  { key: "account", label: "Account / desk" },
  { key: "executing_party", label: "Venue / trader" },
  { key: "pair_id", label: "Pair ID" },
];

function display(trade: NormalizedTradeOut | undefined, key: keyof NormalizedTradeOut): string {
  if (!trade) return "—";
  const value = trade[key];
  if (value == null || value === "") return "—";
  if (key === "quantity" && typeof value === "number") return formatQty(value);
  if (key === "price" && typeof value === "number") return formatPrice(value);
  if (key === "trade_date") {
    return formatTradeTimestamp(trade.executed_at, String(value));
  }
  if (key === "settlement_date") {
    return formatTradeTimestamp(trade.settlement_datetime, String(value));
  }
  return String(value);
}

function valuesMismatch(
  broker: NormalizedTradeOut | undefined,
  desk: NormalizedTradeOut | undefined,
  key: keyof NormalizedTradeOut,
): boolean {
  if (!broker || !desk) return false;
  if (key === "trade_id" || key === "account" || key === "executing_party" || key === "source") {
    return false;
  }
  if (key === "quantity" || key === "price") {
    const a = Number(broker[key]);
    const b = Number(desk[key]);
    if (Number.isNaN(a) || Number.isNaN(b)) return String(broker[key]) !== String(desk[key]);
    return Math.abs(a - b) > 1e-6;
  }
  return String(broker[key] ?? "") !== String(desk[key] ?? "");
}

export function TradeDiff({ detail }: { detail: BreakDetailResponse }) {
  const brokerTrades = detail.broker_side.normalized;
  const deskTrades = detail.desk_side.normalized;
  const rows = Math.max(brokerTrades.length, deskTrades.length, 1);

  return (
    <div className="panel">
      <h2>Broker vs desk</h2>
      {Array.from({ length: rows }, (_, i) => {
        const broker = brokerTrades[i];
        const desk = deskTrades[i];
        return (
          <div key={i} className="diff-leg">
            {rows > 1 && <div className="diff-leg-label">Leg {i + 1}</div>}
            <div className="diff-scroll">
            <div className="diff-grid">
              <div className="head">Field</div>
              <div className="head">Broker</div>
              <div className="head">Desk</div>
              {FIELDS.map((field) => {
                const mismatch = valuesMismatch(broker, desk, field.key);
                const cls = `cell${mismatch ? " mismatch" : ""}`;
                return (
                  <div key={field.key} style={{ display: "contents" }}>
                    <div className="cell field-label">{field.label}</div>
                    <div className={`${cls}${broker ? "" : " empty"}`}>
                      {display(broker, field.key)}
                    </div>
                    <div className={`${cls}${desk ? "" : " empty"}`}>
                      {display(desk, field.key)}
                    </div>
                  </div>
                );
              })}
            </div>
            </div>
          </div>
        );
      })}
      {brokerTrades.length === 0 && deskTrades.length === 0 && (
        <p className="placeholder">No normalized trades linked to this break.</p>
      )}
    </div>
  );
}
