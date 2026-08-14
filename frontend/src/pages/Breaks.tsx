import { useEffect, useMemo, useState } from "react";
import { ApiError, getBreaks } from "../api/client";
import { BREAK_STATUSES, BREAK_TYPES, type BreakListItem, type BreakSortField, type SortOrder } from "../api/types";
import { BreaksTable } from "../components/BreaksTable";
import { labelize } from "../lib/format";

const PAGE_SIZE = 25;

export function Breaks() {
  const [desk, setDesk] = useState("");
  const [symbol, setSymbol] = useState("");
  const [breakType, setBreakType] = useState("");
  const [tradeDate, setTradeDate] = useState("");
  const [status, setStatus] = useState("open");
  const [sort, setSort] = useState<BreakSortField>("trade_date");
  const [order, setOrder] = useState<SortOrder>("desc");
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<BreakListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const query = useMemo(
    () => ({
      desk: desk.trim() || undefined,
      symbol: symbol.trim() || undefined,
      break_type: breakType || undefined,
      trade_date: tradeDate || undefined,
      status: status || undefined,
      sort,
      order,
      page,
      page_size: PAGE_SIZE,
    }),
    [desk, symbol, breakType, tradeDate, status, sort, order, page],
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
  }, [query]);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="page-enter">
      <div className="page-header">
        <div>
          <h1>Breaks</h1>
          <p>Filter by desk, symbol, type, and trade date. Click a column to sort. Open the row for the side-by-side diff.</p>
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
              onChange={(e) => {
                setDesk(e.target.value);
                setPage(1);
              }}
              placeholder="e.g. EQ-US"
            />
          </div>
          <div className="field">
            <label htmlFor="symbol">Symbol</label>
            <input
              id="symbol"
              value={symbol}
              onChange={(e) => {
                setSymbol(e.target.value);
                setPage(1);
              }}
              placeholder="AAPL"
            />
          </div>
          <div className="field">
            <label htmlFor="break_type">Break type</label>
            <select
              id="break_type"
              value={breakType}
              onChange={(e) => {
                setBreakType(e.target.value);
                setPage(1);
              }}
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
            <label htmlFor="trade_date">Trade date</label>
            <input
              id="trade_date"
              type="date"
              value={tradeDate}
              onChange={(e) => {
                setTradeDate(e.target.value);
                setPage(1);
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="status">Status</label>
            <select
              id="status"
              value={status}
              onChange={(e) => {
                setStatus(e.target.value);
                setPage(1);
              }}
            >
              <option value="">All</option>
              {BREAK_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        </div>
        {loading ? (
          <p className="loading-state">Loading…</p>
        ) : (
          <BreaksTable
            items={items}
            sort={sort}
            order={order}
            onSort={(field) => {
              if (field === sort) {
                setOrder((prev) => (prev === "asc" ? "desc" : "asc"));
              } else {
                setSort(field);
                setOrder(field === "trade_date" || field === "notional" ? "desc" : "asc");
              }
              setPage(1);
            }}
          />
        )}
        <div className="pager">
          <button className="btn" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Previous
          </button>
          <span>
            Page {page} of {pages} · {total} breaks
          </span>
          <button
            className="btn"
            disabled={page >= pages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
