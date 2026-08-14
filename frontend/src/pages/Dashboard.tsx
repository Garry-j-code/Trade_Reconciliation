import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ApiError, getBreaks, getSummary } from "../api/client";
import type { BreakListItem, SummaryResponse } from "../api/types";
import { BreaksTable } from "../components/BreaksTable";
import { SummaryCards } from "../components/SummaryCards";
import { labelize } from "../lib/format";

const CHART = {
  grid: "#d8dee8",
  tick: "#64748b",
  bar: "#0f766e",
  tooltipBg: "#ffffff",
  tooltipBorder: "#d8dee8",
};

export function Dashboard() {
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [recent, setRecent] = useState<BreakListItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getSummary(), getBreaks({ status: "open", page: 1, page_size: 8 })])
      .then(([s, breaks]) => {
        if (!cancelled) {
          setSummary(s);
          setRecent(breaks.items);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.detail : "Failed to load dashboard");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return <div className="banner error">{error}</div>;
  }
  if (!summary) {
    return <p className="loading-state">Loading summary…</p>;
  }

  const chartData = (summary.breaks_by_type ?? []).map((row) => ({
    name: labelize(row.break_type),
    count: row.count,
  }));

  return (
    <div className="page-enter">
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p>Deterministic match stats. Agent suggestions appear on break detail.</p>
        </div>
        <Link to="/breaks" className="btn">
          View all breaks
        </Link>
      </div>
      <SummaryCards summary={summary} />
      <div className="panel">
        <h2>Open breaks by type</h2>
        {chartData.length === 0 ? (
          <p className="placeholder">No open breaks.</p>
        ) : (
          <div style={{ width: "100%", height: 280 }}>
            <ResponsiveContainer>
              <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
                <CartesianGrid stroke={CHART.grid} vertical={false} strokeDasharray="3 6" />
                <XAxis
                  dataKey="name"
                  tick={{ fill: CHART.tick, fontSize: 12 }}
                  axisLine={{ stroke: CHART.grid }}
                  tickLine={false}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fill: CHART.tick, fontSize: 12 }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  contentStyle={{
                    background: CHART.tooltipBg,
                    border: `1px solid ${CHART.tooltipBorder}`,
                    borderRadius: 8,
                    boxShadow: "0 4px 16px rgba(12, 18, 34, 0.08)",
                    color: "#0c1222",
                  }}
                  cursor={{ fill: "rgba(15, 118, 110, 0.06)" }}
                />
                <Bar dataKey="count" fill={CHART.bar} radius={[4, 4, 0, 0]} maxBarSize={48} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
      <div className="panel">
        <h2>Recent open breaks</h2>
        <BreaksTable items={recent} />
      </div>
    </div>
  );
}
