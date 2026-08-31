import { getToken, setToken } from "./auth";
import type {
  Health,
  Opportunities,
  Overview,
  Performance,
  Position,
  Risk,
  Shadow,
  TradesPage,
} from "./types";

export class AuthError extends Error {
  constructor() {
    super("unauthorized");
    this.name = "AuthError";
  }
}

async function api<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") url.searchParams.set(key, String(value));
    }
  }
  const token = getToken();
  const headers: HeadersInit = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(url.toString(), { headers });
  if (res.status === 401) {
    setToken("");
    throw new AuthError();
  }
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const fetchHealth = () => api<Health>("/api/health");
export const fetchOverview = () => api<Overview>("/api/overview");
export const fetchPositions = () => api<{ positions: Position[] }>("/api/positions");
export const fetchTrades = (params: Record<string, string | number | undefined>) =>
  api<TradesPage>("/api/trades", params);
export const fetchPerformance = () => api<Performance>("/api/performance");
export const fetchRisk = () => api<Risk>("/api/risk");
export const fetchOpportunities = () => api<Opportunities>("/api/opportunities");
export const fetchShadow = () => api<Shadow>("/api/shadow");
