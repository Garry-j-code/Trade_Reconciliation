import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

type DateRangeFilterProps = {
  fromDate: string;
  toDate: string;
  onChange: (fromDate: string, toDate: string) => void;
};

const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

function iso(year: number, month: number, day: number): string {
  const m = String(month + 1).padStart(2, "0");
  const d = String(day).padStart(2, "0");
  return `${year}-${m}-${d}`;
}

function parseIso(value: string): Date | null {
  if (!value) return null;
  const [y, m, d] = value.split("-").map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d);
}

function orderedRange(a: string, b: string): { from: string; to: string } {
  if (a <= b) return { from: a, to: b };
  return { from: b, to: a };
}

function formatRangeLabel(fromDate: string, toDate: string): string {
  if (!fromDate && !toDate) return "All dates";
  if (fromDate && toDate && fromDate === toDate) return fromDate;
  if (fromDate && toDate) return `${fromDate} – ${toDate}`;
  return fromDate || toDate;
}

function monthLabel(year: number, month: number): string {
  return new Date(year, month, 1).toLocaleString("en-US", { month: "long", year: "numeric" });
}

export function DateRangeFilter({ fromDate, toDate, onChange }: DateRangeFilterProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [pendingStart, setPendingStart] = useState<string | null>(null);
  const [hoverDay, setHoverDay] = useState<string | null>(null);
  const [popoverPos, setPopoverPos] = useState({ top: 0, left: 0 });
  const initial = parseIso(fromDate) ?? parseIso(toDate) ?? new Date();
  const [viewYear, setViewYear] = useState(initial.getFullYear());
  const [viewMonth, setViewMonth] = useState(initial.getMonth());

  const preview = useMemo(() => {
    if (pendingStart && hoverDay) return orderedRange(pendingStart, hoverDay);
    if (pendingStart) return { from: pendingStart, to: pendingStart };
    if (fromDate && toDate) return orderedRange(fromDate, toDate);
    return null;
  }, [pendingStart, hoverDay, fromDate, toDate]);

  useEffect(() => {
    if (!open) return;
    function onDoc(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setPendingStart(null);
        setHoverDay(null);
      }
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        setPendingStart(null);
        setHoverDay(null);
      }
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;

    function place(): void {
      const trigger = triggerRef.current;
      const pop = popoverRef.current;
      if (!trigger || !pop) return;
      const gap = 6;
      const pad = 8;
      const tr = trigger.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const pw = Math.min(pop.offsetWidth, vw - pad * 2);
      const ph = pop.offsetHeight;
      let left = tr.left;
      if (left + pw > vw - pad) left = vw - pad - pw;
      if (left < pad) left = pad;
      const below = tr.bottom + gap;
      const above = tr.top - gap - ph;
      let top = below;
      if (below + ph > vh - pad && above >= pad) {
        top = above;
      } else if (below + ph > vh - pad) {
        top = Math.max(pad, vh - pad - ph);
      }
      setPopoverPos({ top, left });
    }

    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, viewYear, viewMonth, pendingStart]);

  const firstWeekday = new Date(viewYear, viewMonth, 1).getDay();
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
  const cells: Array<{ iso: string; day: number } | null> = [];
  for (let i = 0; i < firstWeekday; i += 1) cells.push(null);
  for (let day = 1; day <= daysInMonth; day += 1) {
    cells.push({ iso: iso(viewYear, viewMonth, day), day });
  }

  function shiftMonth(delta: number) {
    const next = new Date(viewYear, viewMonth + delta, 1);
    setViewYear(next.getFullYear());
    setViewMonth(next.getMonth());
  }

  function selectDay(value: string) {
    if (!pendingStart) {
      setPendingStart(value);
      setHoverDay(value);
      return;
    }
    const range = orderedRange(pendingStart, value);
    onChange(range.from, range.to);
    setPendingStart(null);
    setHoverDay(null);
    setOpen(false);
  }

  function clearRange() {
    onChange("", "");
    setPendingStart(null);
    setHoverDay(null);
    setOpen(false);
  }

  function openPicker() {
    const cursor = parseIso(fromDate) ?? parseIso(toDate) ?? new Date();
    setViewYear(cursor.getFullYear());
    setViewMonth(cursor.getMonth());
    setPendingStart(null);
    setHoverDay(null);
    setOpen((prev) => !prev);
  }

  const label = formatRangeLabel(fromDate, toDate);
  const hint = pendingStart ? "Click an end date" : "Click a start date";

  return (
    <div className="field date-range-field" ref={rootRef}>
      <label htmlFor="trade-date-range">Trade date</label>
      <button
        id="trade-date-range"
        ref={triggerRef}
        type="button"
        className={`date-range-trigger${open ? " open" : ""}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Trade date range, ${label}`}
        onClick={openPicker}
      >
        <span className="date-range-trigger-label">{label}</span>
        <span className="date-range-chevron" aria-hidden>
          ▾
        </span>
      </button>
      {open && (
        <div
          ref={popoverRef}
          className="date-range-popover"
          role="dialog"
          aria-label="Select trade date range"
          style={{ top: popoverPos.top, left: popoverPos.left }}
        >
          <div className="date-range-toolbar">
            <button type="button" className="btn btn-ghost date-range-nav" onClick={() => shiftMonth(-1)} aria-label="Previous month">
              ‹
            </button>
            <div className="date-range-month">{monthLabel(viewYear, viewMonth)}</div>
            <button type="button" className="btn btn-ghost date-range-nav" onClick={() => shiftMonth(1)} aria-label="Next month">
              ›
            </button>
          </div>
          <p className="date-range-hint">{hint}</p>
          <div className="date-range-weekdays" aria-hidden>
            {WEEKDAYS.map((d) => (
              <span key={d}>{d}</span>
            ))}
          </div>
          <div className="date-range-grid">
            {cells.map((cell, idx) => {
              if (!cell) {
                return <span key={`e-${idx}`} className="date-range-empty" />;
              }
              const inRange =
                preview != null && cell.iso >= preview.from && cell.iso <= preview.to;
              const isStart = preview != null && cell.iso === preview.from;
              const isEnd = preview != null && cell.iso === preview.to;
              const classes = [
                "date-range-day",
                inRange ? "in-range" : "",
                isStart ? "range-start" : "",
                isEnd ? "range-end" : "",
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <button
                  key={cell.iso}
                  type="button"
                  className={classes}
                  onClick={() => selectDay(cell.iso)}
                  onMouseEnter={() => pendingStart && setHoverDay(cell.iso)}
                  aria-pressed={inRange}
                  aria-label={cell.iso}
                >
                  {cell.day}
                </button>
              );
            })}
          </div>
          <div className="date-range-actions">
            <button type="button" className="btn btn-ghost" onClick={clearRange}>
              All dates
            </button>
            <button type="button" className="btn" onClick={() => setOpen(false)}>
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
