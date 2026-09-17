export interface ConditionResult {
  name: string;
  status: "pass" | "fail" | "unknown";
  computed_value: string | null;
  threshold: string | null;
  detail: string;
}

export interface GateResultJSON {
  gate: string;
  passed: boolean;
  conditions: ConditionResult[];
}

export interface DistanceToFlipItem {
  gate: string;
  condition: string;
  status: string;
  current_value: string | null;
  required: string | null;
  detail: string;
}

export interface DataIntegrity {
  sources_queried: string[];
  sources_used: string[];
  sources_rejected: { source_id: string; reason: string }[];
  independent_upstream_count_by_metric: Record<string, number>;
  venue_dispersion_observed: Record<string, string | null>;
}

export interface StateClassification {
  regime?: string;
  regime_evidence?: Record<string, unknown>;
  trapped_cohort?: string;
  trapped_cohort_evidence?: Record<string, unknown>;
  constraint_ratios?: Record<string, string | null>;
}

export type Direction = "long" | "short" | "unclear";

export interface StructuralRead {
  setup: string;
  direction: Direction;
  read: string;
}

export interface AnalysisReport {
  run_id: string;
  instrument: string;
  as_of: string;
  config_hash: string;
  config_validated: boolean;
  verdict: "GATE_FAIL" | "NO_SETUP" | "ELIGIBLE_SETUP" | "CLASSIFIER_CONFLICT";
  verdict_bias: Direction | null;
  gate_status: GateResultJSON[];
  data_integrity: Partial<DataIntegrity>;
  state_classification: StateClassification;
  setup_evaluation: GateResultJSON[];
  distance_to_flip: DistanceToFlipItem[];
  structural_reads: StructuralRead[];
}

const BASE = "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, (body as { detail?: string }).detail || res.statusText);
  }
  return res.json() as Promise<T>;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export const api = {
  login: (password: string) => request<{ ok: boolean }>("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  authStatus: () => request<{ authenticated: boolean }>("/api/auth/status"),
  instruments: () => request<{ instruments: string[] }>("/api/instruments"),
  addInstrument: (symbol: string) => request<{ ok: boolean; instrument: string }>(`/api/instruments/${symbol}`, { method: "POST" }),
  report: (symbol: string) => request<AnalysisReport>(`/api/report/${symbol}`),
  reportHistory: (symbol: string) => request<{ records: Record<string, unknown>[] }>(`/api/report/${symbol}/history`),
};
