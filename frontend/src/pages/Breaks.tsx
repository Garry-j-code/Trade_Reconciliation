import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, getBreaks } from "../api/client";
import { BREAK_TYPES, STATUS_FILTER_OPTIONS, type BreakListItem, type BreakSortField, type SortOrder } from "../api/types";
import { BreaksTable } from "../components/BreaksTable";
import { DateRangeFilter } from "../components/DateRangeFilter";
import { labelize } from "../lib/format";

const PAGE_SIZE = 25;

function isSortField(value: string | null): value is BreakSortField {
  return (
    value === "break_type" ||
    value === "status" ||
    value === "desk" ||
    value === "symbol" ||
    value === "trade_date" ||
    value === "notional"
  );
}

export function Breaks() {
  const [params, setParams] = useSearchParams();
  const desk = params.get("desk") ?? "";
  const symbol = params.get("symbol") ?? "";
  const breakType = params.get("break_type") ?? "";
  const fromDate = params.get("from_date") ?? "";
  const toDate = params.get("to_date") ?? "";
  const statusRaw = params.get("status");
  const status = statusRaw && statusRaw.length > 0 ? statusRaw : "open";
  const sortParam = params.get("sort");
  const sort: BreakSortField = isSortField(sortParam) ? sortParam : "trade_date";
  const order: SortOrder = params.get("order") === "asc" ? "asc" : "desc";
  const page = Math.max(1, Number(params.get("page") || "1") || 1);

  const [items, setItems] = useState<BreakListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reconTick, setReconTick] = useState(0);

  useEffect(() => {
    function onReconComplete(): void {
      setReconTick((n) => n + 1);
    }
    window.addEventListener("recon:complete", onReconComplete);
    return () => window.removeEventListener("recon:complete", onReconComplete);
  }, []);

  function patchParams(updates: Record<string, string | null>, resetPage = true) {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(updates)) {
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
    }
    if (resetPage) {
      if (updates.page === undefined) next.delete("page");
    }
    setParams(next, { replace: true });
  }

  const query = useMemo(
    () => ({
      desk: desk.trim() || undefined,
      symbol: symbol.trim() || undefined,
      break_type: breakType || undefined,
      from_date: fromDate || undefined,
      to_date: toDate || undefined,
      status,
      sort,
      order,
      page,
      page_size: PAGE_SIZE,
    }),
    [desk, symbol, breakType, fromDate, toDate, status, sort, order, page],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getBreaks(query)
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setTotal(res.total);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.detail : "Failed to load breaks");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [query, reconTick]);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="page-enter">
      <div className="page-header">
        <div>
          <h1>Breaks</h1>
          <p>
            Filter by desk, symbol, type, status (open / resolved / rejected / overridden), and
            trade-date range. Filters are kept in the URL. Click a column to sort.
          </p>
        </div>
      </div>
      {error && <div className="banner error">{error}</div>}
      <div className="panel">
        <div className="filters">
          <div className="field">
            <label htmlFor="desk">Desk</label>
            <input
              id="desk"
              value={desk}
              onChange={(e) => patchParams({ desk: e.target.value })}
              placeholder="e.g. EQ-US"
            />
          </div>
          <div className="field">
            <label htmlFor="symbol">Symbol</label>
            <input
              id="symbol"
              value={symbol}
              onChange={(e) => patchParams({ symbol: e.target.value.toUpperCase() })}
              placeholder="AAPL"
            />
          </div>
          <div className="field">
            <label htmlFor="break_type">Break type</label>
            <select
              id="break_type"
              value={breakType}
              onChange={(e) => patchParams({ break_type: e.target.value })}
            >
              <option value="">All</option>
              {BREAK_TYPES.map((t) => (
                <option key={t} value={t}>
                  {labelize(t)}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="status">Status</label>
            <select
              id="status"
              value={STATUS_FILTER_OPTIONS.some((opt) => opt.value === status) ? status : "open"}
              onChange={(e) => patchParams({ status: e.target.value })}
            >
              {STATUS_FILTER_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <DateRangeFilter
            fromDate={fromDate}
            toDate={toDate}
            onChange={(from, to) => {
              patchParams({ from_date: from, to_date: to });
            }}
          />
        </div>
        {loading ? (
          <p className="loading-state">Loading…</p>
        ) : (
          <BreaksTable
            items={items}
            sort={sort}
            order={order}
            onSort={(field) => {
              const nextOrder =
                field === sort ? (order === "asc" ? "desc" : "asc") : field === "trade_date" || field === "notional" ? "desc" : "asc";
              patchParams({ sort: field, order: nextOrder });
            }}
          />
        )}
        <div className="pager">
          <button
            className="btn"
            disabled={page <= 1}
            onClick={() => patchParams({ page: String(page - 1) }, false)}
          >
            Previous
          </button>
          <span>
            Page {page} of {pages} · {total} breaks
          </span>
          <button
            className="btn"
            disabled={page >= pages}
            onClick={() => patchParams({ page: String(page + 1) }, false)}
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
