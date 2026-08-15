/** Mirrors backend/api/schemas.py — keep field names in lockstep. */

export type ReviewRouting = "one_click" | "manual_review";

export interface HealthResponse {
  status: "ok";
  db: "connected" | "unavailable";
}

export interface BreaksByType {
  break_type: string;
  count: number;
}

export interface SummaryResponse {
  total_trades: number;
  pair_count?: number;
  broker_leg_count?: number;
  desk_leg_count?: number;
  match_count: number;
  matched_pair_count?: number;
  match_row_count?: number;
  break_count: number;
  open_break_count: number;
  pct_clean_matched: number;
  breaks_by_type: BreaksByType[];
  notional_at_risk: number;
}

export interface BreakListItem {
  break_id: string;
  break_type: string;
  status: string;
  symbol: string | null;
  trade_date: string | null;
  executed_at?: string | null;
  pair_id: string | null;
  desk: string | null;
  notional_at_risk: number;
  created_at: string | null;
  last_action?: string | null;
  last_actor?: string | null;
  last_decided_at?: string | null;
  last_note?: string | null;
}

export interface PaginatedBreaks {
  items: BreakListItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface NormalizedTradeOut {
  trade_id: string;
  source: string;
  symbol: string;
  trade_date: string;
  executed_at?: string | null;
  settlement_date: string;
  settlement_datetime?: string | null;
  side: string;
  quantity: number;
  price: number;
  currency: string;
  account: string;
  executing_party: string;
  pair_id: string | null;
  raw_payload: Record<string, unknown> | null;
}

export interface SideBySide {
  trade_ids: string[];
  normalized: NormalizedTradeOut[];
  raw: Record<string, unknown>[];
}

export interface EvidenceItem {
  tool?: string;
  result_summary?: string;
  [key: string]: unknown;
}

export interface SuggestionOut {
  break_id: string;
  root_cause: string | null;
  confidence: number | null;
  explanation: string | null;
  suggested_action: string | null;
  evidence: EvidenceItem[];
  suggestion_id: string | null;
  inferred?: boolean;
  tool_calls?: number;
  review_route?: string;
}

export interface AuditDecision {
  audit_id: string;
  actor: string;
  action: string;
  override_note: string | null;
  created_at: string | null;
  suggestion_id: string | null;
  root_cause: string | null;
  suggested_action: string | null;
  explanation: string | null;
}

export interface BreakDetailResponse {
  break_id: string;
  break_type: string;
  status: string;
  symbol: string | null;
  trade_date: string | null;
  executed_at?: string | null;
  pair_id: string | null;
  desk: string | null;
  notional_at_risk: number;
  detail: Record<string, unknown> | null;
  cluster_id: string | null;
  created_at: string | null;
  broker_side: SideBySide;
  desk_side: SideBySide;
  suggestion: SuggestionOut | null;
  review_routing: ReviewRouting;
  decisions?: AuditDecision[];
}

export interface MatchListItem {
  match_id: string;
  broker_trade_id: string;
  desk_trade_id: string;
  pair_id: string | null;
  match_pass: string;
  created_at: string | null;
}

export interface PaginatedMatches {
  items: MatchListItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface ReconRunRequest {
  input_dir?: string | null;
  replace?: boolean;
  mode?: string;
  trade_date?: string | null;
}

export interface ReconRunResponse {
  broker_rows: number;
  desk_rows: number;
  normalized_rows: number;
  match_count: number;
  break_count: number;
  breaks_by_type: Record<string, number>;
  elapsed_seconds: number;
  db_loaded: boolean;
}

export interface ApprovalRequest {
  actor?: string | null;
  note?: string | null;
}

export interface OverrideRequest {
  actor?: string | null;
  note: string;
}

export interface ApprovalResponse {
  break_id: string;
  status: string;
  action: string;
  audit_id: string;
  suggestion_id: string | null;
}

export type BreakSortField =
  | "break_type"
  | "status"
  | "desk"
  | "symbol"
  | "trade_date"
  | "notional";

export type SortOrder = "asc" | "desc";

export interface BreaksQuery {
  desk?: string;
  symbol?: string;
  break_type?: string;
  trade_date?: string;
  from_date?: string;
  to_date?: string;
  date_from?: string;
  date_to?: string;
  status?: string;
  sort?: BreakSortField;
  order?: SortOrder;
  page?: number;
  page_size?: number;
}

export const BREAK_TYPES = [
  "missing_broker",
  "missing_desk",
  "price_break",
  "quantity_break",
  "duplicate",
  "settlement_date_mismatch",
] as const;

export const BREAK_STATUSES = [
  "open",
  "resolved",
  "rejected",
  "overridden",
] as const;

export const STATUS_FILTER_OPTIONS = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "rejected", label: "Rejected" },
  { value: "overridden", label: "Overridden" },
  { value: "all", label: "All" },
] as const;

export const TERMINAL_STATUSES = new Set(["resolved", "overridden"]);
