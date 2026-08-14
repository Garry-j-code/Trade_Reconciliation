import type {
  ApprovalRequest,
  ApprovalResponse,
  BreakDetailResponse,
  BreaksQuery,
  HealthResponse,
  OverrideRequest,
  PaginatedBreaks,
  PaginatedMatches,
  ReconRunRequest,
  ReconRunResponse,
  SuggestionOut,
  SummaryResponse,
} from "./types";
import { bearerToken, clearTokens } from "../auth/session";

const BASE = (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function apiUrl(path: string): string {
  return `${BASE}${path}`;
}

async function readDetail(res: Response): Promise<string> {
  const text = await res.text();
  if (!text) return res.statusText || `HTTP ${res.status}`;
  try {
    const body: unknown = JSON.parse(text);
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
      if (Array.isArray(detail)) {
        return detail
          .map((item) => {
            if (item && typeof item === "object" && "msg" in item) {
              return String((item as { msg: unknown }).msg);
            }
            return JSON.stringify(item);
          })
          .join("; ");
      }
      return JSON.stringify(detail);
    }
    return text;
  } catch {
    return text;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init?.body ? { "Content-Type": "application/json" } : {}),
  };
  const token = path === "/health" ? null : bearerToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  Object.assign(headers, init?.headers ?? {});

  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      ...init,
      headers,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Network error";
    throw new ApiError(
      0,
      `Cannot reach API at ${BASE || "the Vite proxy (http://127.0.0.1:8000)"}. ${message}. Is \`uv run serve-api\` running?`,
    );
  }
  if (res.status === 401 && path !== "/health") {
    clearTokens();
    window.dispatchEvent(new Event("auth:unauthorized"));
  }
  if (!res.ok) {
    throw new ApiError(res.status, await readDetail(res));
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return (await res.json()) as T;
}

function queryString(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export function getSummary(): Promise<SummaryResponse> {
  return request<SummaryResponse>("/api/summary");
}

export function getBreaks(query: BreaksQuery = {}): Promise<PaginatedBreaks> {
  return request<PaginatedBreaks>(
    `/api/breaks${queryString({
      desk: query.desk,
      symbol: query.symbol,
      break_type: query.break_type,
      date: query.trade_date,
      date_from: query.date_from,
      date_to: query.date_to,
      status: query.status,
      page: query.page,
      page_size: query.page_size,
    })}`,
  );
}

export function getBreak(id: string): Promise<BreakDetailResponse> {
  return request<BreakDetailResponse>(`/api/breaks/${id}`);
}

export function getMatches(page = 1, pageSize = 25): Promise<PaginatedMatches> {
  return request<PaginatedMatches>(
    `/api/matches${queryString({ page, page_size: pageSize })}`,
  );
}

export function runRecon(body: ReconRunRequest = { mode: "daily" }): Promise<ReconRunResponse> {
  return request<ReconRunResponse>("/api/recon/run", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function approveBreak(
  id: string,
  body: ApprovalRequest = {},
): Promise<ApprovalResponse> {
  return request<ApprovalResponse>(`/api/breaks/${id}/approve`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function rejectBreak(
  id: string,
  body: OverrideRequest,
): Promise<ApprovalResponse> {
  return request<ApprovalResponse>(`/api/breaks/${id}/reject`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function investigateBreak(id: string): Promise<SuggestionOut | BreakDetailResponse> {
  return request<SuggestionOut | BreakDetailResponse>(`/api/breaks/${id}/investigate`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
