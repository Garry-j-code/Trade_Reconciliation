import { Link } from "react-router-dom";
import type { BreakListItem, BreakSortField, SortOrder } from "../api/types";
import { formatTradeTimestamp, formatUsd, labelize, shortId } from "../lib/format";

const SORTABLE: { key: BreakSortField; label: string }[] = [
  { key: "break_type", label: "Type" },
  { key: "status", label: "Status" },
  { key: "desk", label: "Desk" },
  { key: "symbol", label: "Symbol" },
  { key: "trade_date", label: "Trade date" },
  { key: "notional", label: "Notional" },
];

export function BreaksTable({
  items,
  sort,
  order,
  onSort,
}: {
  items: BreakListItem[];
  sort?: BreakSortField;
  order?: SortOrder;
  onSort?: (field: BreakSortField) => void;
}) {
  if (items.length === 0) {
    return <div className="empty-table">No breaks match these filters.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            {SORTABLE.map((col) => {
              const active = sort === col.key;
              const ariaSort = !active ? "none" : order === "asc" ? "ascending" : "descending";
              if (!onSort) {
                return <th key={col.key}>{col.label}</th>;
              }
              return (
                <th key={col.key} aria-sort={ariaSort} className={active ? "sorted" : undefined}>
                  <button
                    type="button"
                    className={`th-sort${active ? " is-active" : ""}`}
                    onClick={() => onSort(col.key)}
                  >
                    {col.label}
                    <span className="sort-ind" aria-hidden="true">
                      {active ? (order === "asc" ? "▲" : "▼") : "↕"}
                    </span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <tr key={row.break_id}>
              <td className="mono">
                <Link to={`/breaks/${row.break_id}`}>{shortId(row.break_id)}</Link>
              </td>
              <td>{labelize(row.display_type || row.break_type)}</td>
              <td>
                <span className={`pill ${row.status}`}>{row.status}</span>
                {row.last_actor ? (
                  <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                    {row.last_action ?? "decided"} · {row.last_actor}
                  </div>
                ) : null}
              </td>
              <td>{row.desk ?? "—"}</td>
              <td className="mono">{row.symbol ?? "—"}</td>
              <td>{formatTradeTimestamp(row.executed_at, row.trade_date)}</td>
              <td>{formatUsd(row.notional_at_risk)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
