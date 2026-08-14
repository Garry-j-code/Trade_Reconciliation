import { Link } from "react-router-dom";
import type { BreakListItem } from "../api/types";
import { formatDate, formatUsd, labelize, shortId } from "../lib/format";

export function BreaksTable({ items }: { items: BreakListItem[] }) {
  if (items.length === 0) {
    return <div className="empty-table">No breaks match these filters.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Type</th>
            <th>Status</th>
            <th>Desk</th>
            <th>Symbol</th>
            <th>Trade date</th>
            <th>Notional</th>
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <tr key={row.break_id}>
              <td className="mono">
                <Link to={`/breaks/${row.break_id}`}>{shortId(row.break_id)}</Link>
              </td>
              <td>{labelize(row.break_type)}</td>
              <td>
                <span className={`pill ${row.status}`}>{row.status}</span>
              </td>
              <td>{row.desk ?? "—"}</td>
              <td className="mono">{row.symbol ?? "—"}</td>
              <td>{formatDate(row.trade_date)}</td>
              <td>{formatUsd(row.notional_at_risk)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
