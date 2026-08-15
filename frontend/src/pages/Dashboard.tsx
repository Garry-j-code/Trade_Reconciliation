import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
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
import { DateRangeFilter } from "../components/DateRangeFilter";
import { SummaryCards } from "../components/SummaryCards";
import { labelize } from "../lib/format";
import { useTheme } from "../ThemeContext";
import { readCssVar } from "../theme";

export function Dashboard() {
  const navigate = useNavigate();
  const { resolved } = useTheme();
  const chart = useMemo(() => {
    void resolved;
    return {
      grid: readCssVar("--chart-grid"),
      tick: readCssVar("--chart-tick"),
      bar: readCssVar("--chart-bar"),
      tooltipBg: readCssVar("--chart-tooltip-bg"),
      tooltipBorder: readCssVar("--chart-tooltip-border"),
      ink: readCssVar("--ink"),
      shadow: readCssVar("--shadow-md"),
      cursor: readCssVar("--chart-cursor"),
    };
  }, [resolved]);
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [recent, setRecent] = useState<BreakListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const range = {
      from_date: fromDate || undefined,
      to_date: toDate || undefined,
    };
    Promise.all([
      getSummary(range),
      getBreaks({
        status: "open",
        page: 1,
        page_size: 8,
        sort: "trade_date",
        order: "desc",
        ...range,
      }),
    ])
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
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fromDate, toDate]);

  if (error && !summary) {
    return <div className="banner error">{error}</div>;
  }
  if (!summary) {
    return <p className="loading-state">Loading summary…</p>;
  }

  const chartData = (summary.breaks_by_type ?? []).map((row) => ({
    name: labelize(row.break_type),
    break_type: row.break_type,
    count: row.count,
  }));

  return (
    <div className="page-enter">
      <div className="page-header">
        <div>
          <h1>Dashboard</h1>
          <p>Deterministic match stats for the selected trade-date range. Empty dates show the full book.</p>
        </div>
        <Link to="/breaks" className="btn">
          View all breaks
        </Link>
      </div>
      <div className="filters dashboard-filters">
        <DateRangeFilter
          fromDate={fromDate}
          toDate={toDate}
          onChange={(from, to) => {
            setFromDate(from);
            setToDate(to);
          }}
        />
      </div>
      {error && <div className="banner error">{error}</div>}
      <SummaryCards summary={summary} />
      <div className="panel">
        <h2>Open breaks by type</h2>
        <p className="muted">Click a bar to open that break type on the Breaks page.</p>
        {loading ? <p className="loading-state">Updating…</p> : null}
        {chartData.length === 0 ? (
          <p className="placeholder">No open breaks.</p>
        ) : (
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
                <CartesianGrid stroke={chart.grid} vertical={false} strokeDasharray="3 6" />
                <XAxis
                  dataKey="name"
                  tick={{ fill: chart.tick, fontSize: 11 }}
                  axisLine={{ stroke: chart.grid }}
                  tickLine={false}
                  interval="preserveStartEnd"
                  minTickGap={8}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fill: chart.tick, fontSize: 12 }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  contentStyle={{
                    background: chart.tooltipBg,
                    border: `1px solid ${chart.tooltipBorder}`,
                    borderRadius: 8,
                    boxShadow: chart.shadow,
                    color: chart.ink,
                  }}
                  cursor={{ fill: chart.cursor }}
                />
                <Bar
                  dataKey="count"
                  fill={chart.bar}
                  radius={[4, 4, 0, 0]}
                  maxBarSize={48}
                  cursor="pointer"
                  onClick={(data: { payload?: { break_type?: string }; break_type?: string }) => {
                    const breakType = data?.payload?.break_type ?? data?.break_type;
                    if (!breakType) return;
                    const params = new URLSearchParams();
                    params.set("break_type", breakType);
                    params.set("status", "open");
                    if (fromDate) params.set("from_date", fromDate);
                    if (toDate) params.set("to_date", toDate);
                    navigate(`/breaks?${params.toString()}`);
                  }}
                />
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
