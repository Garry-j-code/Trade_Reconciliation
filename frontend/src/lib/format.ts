export function formatUsd(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${value.toFixed(2)}%`;
}

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value);
}

export function formatQty(value: number | null | undefined): string {
  return formatNumber(value, 2);
}

export function formatPrice(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(value);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return value.slice(0, 10);
}

export const TRADE_TZ = "America/New_York";

export function formatTradeTimestamp(
  executedAt: string | null | undefined,
  tradeDate?: string | null,
): string {
  if (executedAt) {
    const d = new Date(executedAt);
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleString("en-US", {
        timeZone: TRADE_TZ,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
        timeZoneName: "short",
      });
    }
  }
  if (!tradeDate) return "—";
  return `${tradeDate.slice(0, 10)} (time not recorded)`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  return id.slice(0, 8);
}

export function labelize(value: string | null | undefined): string {
  if (!value) return "—";
  if (value === "unclassified") return "Unclassified";
  if (value === "other") return "Other";
  if (value === "__others__") return "Others";
  return value.replace(/_/g, " ");
}

export function confidencePct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(0)}%`;
}

/** Internal agent tool names → analyst-facing evidence headings. */
export const TOOL_EVIDENCE_LABELS: Record<string, string> = {
  get_corporate_actions: "Checked corporate actions for this symbol",
  get_market_session_info: "Looked up the market session for this trade date",
  get_trade_history: "Reviewed prior trades for this symbol",
  get_similar_resolved_breaks:
    "Compared with previously resolved breaks of the same type",
  search_similar_breaks: "Searched similar human-resolved cases",
  get_desk_metadata: "Looked up desk reference data",
  get_raw_records: "Compared broker and desk source records",
  get_relevant_memory: "Recalled prior investigation notes",
  get_trade_pair: "Compared broker and desk prices for this pair",
};

const TOOL_IDENT = /^[A-Za-z][A-Za-z0-9_]*$/;

export function evidenceHeading(tool: string | null | undefined): string {
  const raw = (tool ?? "").trim();
  if (!raw) return "Reviewed additional records";
  if (TOOL_EVIDENCE_LABELS[raw]) return TOOL_EVIDENCE_LABELS[raw];
  if (raw.includes(" ")) return raw;
  if (TOOL_IDENT.test(raw)) return "Reviewed additional records";
  return raw;
}

export function evidenceDetail(
  resultSummary: string | null | undefined,
  tool?: string | null,
): string {
  const text = (resultSummary ?? "").trim();
  const name = (tool ?? "").trim();
  if (name && text.startsWith(name)) {
    const rest = text.slice(name.length).replace(/^[\s:.-]+/, "");
    if (rest.toLowerCase().startsWith("error")) {
      const msg = rest.includes(":") ? rest.split(":").slice(1).join(":").trim() : rest;
      return msg ? `Could not complete this check: ${msg}` : text;
    }
    return rest || text;
  }
  return text;
}
