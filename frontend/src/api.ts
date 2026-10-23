/**
 * Typed client for the sn0wy API.
 *
 * HOW IT WORKS
 * Every backend call in the app goes through this one module. The TypeScript types
 * below mirror the pydantic models in services/common/models.py and
 * services/api/main.py field for field, so if the backend renames a field, this is
 * the one file to update (and `npm run typecheck` then points at every screen that
 * used it). `request()` adds the base URL, sends/parses JSON, and turns any non-2xx
 * response into an `ApiError` that carries the HTTP status, so pages can branch on
 * it (404 = unknown scan, 409 = report not ready yet, 503 = AI not configured).
 *
 * WHY THIS DESIGN
 * - Base URL comes from VITE_API_URL, which Vite inlines at build time. In dev it
 *   defaults to the local uvicorn server; on Vercel it is the Cloud Run URL.
 * - `createApi(base, fetchFn)` makes the client easy to test: tests pass a fake
 *   fetch instead of patching globals. The app uses the default `api` instance.
 * - Dates stay ISO strings here (what JSON gives us); screens convert with
 *   `new Date()` when formatting. Keeping the wire types honest avoids surprises.
 */

export type ScanStatus = "queued" | "running" | "done" | "failed";

/** services/common/models.py: Scan */
export interface Scan {
  id: string;
  url: string;
  status: ScanStatus;
  attempts: number;
  error: string | null;
  duration_ms: number | null;
  created_at: string;
  finished_at: string | null;
}

/** services/common/models.py: StoredFinding. `score` is Lighthouse's 0..1 audit score. */
/** One resource behind a finding (v2 evidence), worst first. */
export interface Offender {
  url: string;
  wasted_kb: number | null;
  wasted_ms: number | null;
  total_kb: number | null;
}

/** What the site is built on, detected from evidence (never assumed). */
export interface Platform {
  id: string;
  name: string;
  evidence: string[];
}

export interface StoredFinding {
  id: number;
  audit_id: string;
  title: string;
  description: string;
  score: number | null;
  savings_ms: number | null;
  savings_bytes: number | null;
  /** Optional so reports from an older API still type-check. */
  offenders?: Offender[];
}

/** Keys of Report.metrics (services/scanner/parse.py: Metrics minus performance_score). */
export type MetricKey = "fcp_ms" | "lcp_ms" | "tbt_ms" | "cls" | "speed_index_ms";

/** services/common/models.py: ConditionsOut. What the score was measured under. */
export interface Conditions {
  lighthouse_version?: string | null;
  form_factor?: string | null;
  throttling_method?: string | null;
  cpu_slowdown?: number | null;
  rtt_ms?: number | null;
  throughput_kbps?: number | null;
  benchmark_index?: number | null;
}

/** services/common/models.py: Report */
export interface Report {
  scan: Scan;
  performance_score: number | null;
  metrics: Partial<Record<MetricKey, number | null>>;
  findings: StoredFinding[];
  platform?: Platform | null;
  /** How this score was measured. Absent on scans recorded before it was captured. */
  conditions?: Conditions;
}

/** What POST /scans gives back: the scan, and whether it already existed.
 *
 * `reused: true` means the API returned a scan it already had rather than
 * starting a new one, because the same URL was scanned very recently or is being
 * scanned right now. Nothing was re-measured, so a screen that says "scanned" is
 * lying. See services/common/dedupe.py for why the window exists. */
export interface CreatedScan {
  scan: Scan;
  reused: boolean;
}

/** services/common/models.py: ScanSummary */
export interface ScanSummary {
  scan: Scan;
  performance_score: number | null;
}

/** services/api/main.py: FixOut */
export interface FixOut {
  audit_id: string;
  title: string;
  diagnosis: string;
  fix: string;
  code: string;
  /** Which file/screen this change goes in, for the detected platform. May be "". */
  where?: string;
  sources: string[];
  latency_ms: number;
  cost_usd: number;
  /** Site URLs in the fix whose folder the audit never saw (possible invented paths). */
  unverified_urls?: string[];
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Status 0 means the request never got an HTTP answer (API down, CORS, offline). */
export const NETWORK_ERROR = 0;

export const DEFAULT_API_URL = "http://localhost:8000";

/** FastAPI errors look like {"detail": "..."} or, for validation (422),
 *  {"detail": [{"msg": "...", "loc": [...]}]}. Pull out a human sentence. */
export function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first?.msg === "string") return first.msg;
    }
  }
  return fallback;
}

type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

export function createApi(baseUrl: string, fetchFn: FetchFn = (i, init) => fetch(i, init)) {
  const base = baseUrl.replace(/\/+$/, "");

  /** Like `request`, but keeps the Response so a caller can read the status.
   *  Only POST /scans needs it: 200 and 202 mean different things there. */
  async function send<T>(path: string, init?: RequestInit): Promise<{ data: T; status: number }> {
    let res: Response;
    try {
      res = await fetchFn(base + path, {
        ...init,
        headers: { accept: "application/json", ...(init?.headers ?? {}) },
      });
    } catch (e) {
      // Our own cancellation (page unmounted): not an error, let callers ignore it.
      // Checked by name because DOMException differs between browser and test realms.
      if ((e as { name?: string } | null)?.name === "AbortError") throw e;
      throw new ApiError(NETWORK_ERROR, `Can't reach the sn0wy API at ${base}.`);
    }
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      body = null; // empty or non-JSON body (e.g. a proxy's HTML error page)
    }
    if (!res.ok) {
      throw new ApiError(res.status, errorMessage(body, `Request failed (HTTP ${res.status}).`));
    }
    return { data: body as T, status: res.status };
  }

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    return (await send<T>(path, init)).data;
  }

  const q = encodeURIComponent;

  return {
    baseUrl: base,
    /** POST /scans. 202 = a new scan is queued, 200 = an existing one was handed
     *  back (see services/common/dedupe.py). `force` skips the dedupe window and
     *  can answer 429 while the per-URL cooldown is running. */
    createScan: async (
      url: string,
      opts: { force?: boolean; signal?: AbortSignal } = {},
    ): Promise<CreatedScan> => {
      const { data, status } = await send<Scan>("/scans", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(opts.force ? { url, force: true } : { url }),
        signal: opts.signal,
      });
      return { scan: data, reused: status === 200 };
    },
    /** GET /scans/{id} */
    getScan: (id: string, signal?: AbortSignal) =>
      request<Scan>(`/scans/${q(id)}`, { signal }),
    /** GET /scans/{id}/report : 409 until the scan is done. */
    getReport: (id: string, signal?: AbortSignal) =>
      request<Report>(`/scans/${q(id)}/report`, { signal }),
    /** GET /scans/{id}/fixes?limit= : AI fixes for the top-N findings, in report order. */
    getFixes: (id: string, limit: number, signal?: AbortSignal) =>
      request<FixOut[]>(`/scans/${q(id)}/fixes?limit=${limit}`, { signal }),
    /** GET /sites/scans?url= : one site's history, newest first. */
    siteHistory: (url: string, limit = 100, signal?: AbortSignal) =>
      request<ScanSummary[]>(`/sites/scans?url=${q(url)}&limit=${limit}`, { signal }),
  };
}

export type Api = ReturnType<typeof createApi>;

export const api: Api = createApi(import.meta.env.VITE_API_URL || DEFAULT_API_URL);
