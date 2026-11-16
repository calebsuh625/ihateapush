// The one place the app talks to the backend. Every call returns typed JSON or throws ApiError
// (or the browser's AbortError when the caller cancelled the request).
import type { AdvisorReply, AdvisorStatus } from "./lib/advisor";
import type { LayerAnswer, LayerCatalog } from "./lib/layers";
import type { FarmPlan, FieldCollection, FieldDetail, FieldPlans, FieldSatellite, FieldScore, FieldSoil, FieldTransition, Health } from "./types";

const configured: string | undefined = import.meta.env.VITE_API_URL;

/** "/api" in local dev (Vite proxies it to the FastAPI server), or an absolute URL from VITE_API_URL. */
export const API_ROOT = (configured ?? "/api").replace(/\/+$/, "");

/** The county picked in the app (?region=20069), or null for the default county (Finney). */
export const SELECTED_REGION: string | null = (() => {
  try {
    const picked = new URLSearchParams(window.location.search).get("region");
    return picked && /^\d{5}$/.test(picked) ? picked : null;
  } catch {
    return null; // no window (unit tests)
  }
})();

/** Where this page's county is served: the default county at the root, another at /r/<fips>
 * (the multi-county gateway, deploy/vercel/index.py). */
export const API_BASE = SELECTED_REGION ? `${API_ROOT}/r/${SELECTED_REGION}` : API_ROOT;

/** Absolute form of a path under the API base, for messages a person has to act on. */
export function apiUrl(path = ""): string {
  try {
    return new URL(API_BASE + path, window.location.href).href;
  } catch {
    return API_BASE + path;
  }
}

export class ApiError extends Error {
  /** HTTP status, or null when the request never got a response. */
  readonly status: number | null;
  readonly url: string;

  constructor(message: string, url: string, status: number | null) {
    super(message);
    this.name = "ApiError";
    this.url = url;
    this.status = status;
  }
}

export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

/** FastAPI errors look like {"detail": "..."}; use that text when it is there. */
async function errorDetail(res: Response): Promise<string | null> {
  try {
    const body: unknown = await res.json();
    if (isObject(body) && typeof body.detail === "string" && body.detail) return body.detail;
  } catch {
    // not JSON — fall through to the status line
  }
  return null;
}

export async function getJson<T>(path: string, looksRight: (body: unknown) => boolean, signal?: AbortSignal, init?: RequestInit): Promise<T> {
  const url = apiUrl(path);
  let res: Response;
  try {
    res = await fetch(API_BASE + path, { ...init, signal });
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ApiError(`No response from ${url}`, url, null);
  }
  if (!res.ok) {
    const detail = await errorDetail(res);
    throw new ApiError(detail ?? `${url} answered ${res.status} ${res.statusText}`.trim(), url, res.status);
  }
  let body: unknown;
  try {
    body = await res.json();
  } catch (err) {
    if (isAbort(err)) throw err;
    throw new ApiError(`${url} did not return valid JSON`, url, res.status);
  }
  if (!looksRight(body)) throw new ApiError(`${url} returned an unexpected response`, url, res.status);
  return body as T;
}

/** POST `body` as JSON; errors and checks as getJson. */
export const postJson = <T>(path: string, body: unknown, looksRight: (body: unknown) => boolean, signal?: AbortSignal): Promise<T> =>
  getJson<T>(path, looksRight, signal, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });

const fieldPath = (fieldId: string) => `/field/${encodeURIComponent(fieldId)}`;

export const api = {
  health: (signal?: AbortSignal) =>
    getJson<Health>("/health", (b) => isObject(b) && typeof b.field_count === "number", signal),

  fields: (signal?: AbortSignal) =>
    getJson<FieldCollection>(
      "/fields",
      (b) => isObject(b) && b.type === "FeatureCollection" && Array.isArray(b.features),
      signal,
    ),

  /** The list of map layers (GET /layers). */
  layers: (signal?: AbortSignal) =>
    getJson<LayerCatalog>("/layers", (b) => isObject(b) && Array.isArray(b.layers) && typeof b.field_count === "number", signal),

  /** One map layer, every year of it (GET /layers/{name}). Answers 202 with status "building" while it is made. */
  layer: (name: string, signal?: AbortSignal) =>
    getJson<LayerAnswer>(
      `/layers/${encodeURIComponent(name)}`,
      (b) => isObject(b) && b.name === name && typeof b.status === "string" && typeof b.field_count === "number",
      signal,
    ),

  /**
   * The ETag of the fields the map holds, read from the cached answer of GET /fields (so no second
   * download). Null when the browser has no copy or the header is hidden from this page.
   */
  async fieldsEtag(signal?: AbortSignal): Promise<string | null> {
    try {
      const res = await fetch(`${API_BASE}/fields`, { signal, cache: "force-cache" });
      void res.body?.cancel();
      return res.headers.get("etag");
    } catch {
      return null;
    }
  },

  field: (fieldId: string, signal?: AbortSignal) =>
    getJson<FieldDetail>(
      fieldPath(fieldId),
      (b) => isObject(b) && b.field_id === fieldId && isObject(b.crop_history) && isObject(b.stats),
      signal,
    ),

  /** `query` is a ready query string without the "?" (see lib/planner.ts), or empty. */
  plans: (fieldId: string, signal?: AbortSignal, query = "") =>
    getJson<FieldPlans>(
      `${fieldPath(fieldId)}/plans${query ? `?${query}` : ""}`,
      (b) => isObject(b) && b.field_id === fieldId && Array.isArray(b.plans),
      signal,
    ),

  /** The farmer's own rotation next to our picks. `query` comes from scoreQuery (lib/custom.ts). */
  score: (fieldId: string, signal: AbortSignal | undefined, query: string) =>
    getJson<FieldScore>(
      `${fieldPath(fieldId)}/score?${query}`,
      (b) =>
        isObject(b) &&
        b.field_id === fieldId &&
        Array.isArray(b.understood) &&
        isObject(b.yours) &&
        Array.isArray(b.plans) &&
        isObject(b.marks) &&
        isObject(b.comparison),
      signal,
    ),

  /** The year-by-year path to a rotation. `query` comes from transitionQuery (lib/transition.ts). */
  transition: (fieldId: string, signal: AbortSignal | undefined, query: string) =>
    getJson<FieldTransition>(
      `${fieldPath(fieldId)}/transition?${query}`,
      (b) =>
        isObject(b) &&
        b.field_id === fieldId &&
        typeof b.status === "string" &&
        typeof b.summary === "string" &&
        isObject(b.target) &&
        Array.isArray(b.history) &&
        Array.isArray(b.not_modelled),
      signal,
    ),

  /** The whole-farm plan. `body` is the request as JSON text (farmRequest in lib/farm.ts). */
  farmPlan: (body: string, signal?: AbortSignal) =>
    postJson<FarmPlan>(
      "/farm/plan",
      body,
      (b) =>
        isObject(b) &&
        typeof b.status === "string" &&
        Array.isArray(b.summary) &&
        isObject(b.limits) &&
        isObject(b.plan) &&
        isObject(b.alone) &&
        Array.isArray(b.fields) &&
        isObject(b.solver),
      signal,
    ),

  /** Is there an API key for the farm advisor? (GET /advisor/status) */
  advisorStatus: (signal?: AbortSignal) =>
    getJson<AdvisorStatus>("/advisor/status", (b) => isObject(b) && typeof b.available === "boolean", signal),

  /** One question to the farm advisor. `body` is the request as JSON text (see lib/advisor.ts). */
  advisorChat: (body: string, signal?: AbortSignal) =>
    postJson<AdvisorReply>(
      "/advisor/chat",
      body,
      (b) => isObject(b) && typeof b.answer === "string" && Array.isArray(b.tool_calls) && isObject(b.grounding) && Array.isArray(b.grounding.unmatched),
      signal,
    ),

  satellite: (fieldId: string, signal?: AbortSignal) =>
    getJson<FieldSatellite>(
      `${fieldPath(fieldId)}/satellite`,
      (b) =>
        isObject(b) &&
        b.field_id === fieldId &&
        typeof b.available === "boolean" &&
        Array.isArray(b.years) &&
        isObject(b.summary),
      signal,
    ),

  soil: (fieldId: string, signal?: AbortSignal) =>
    getJson<FieldSoil>(
      `${fieldPath(fieldId)}/soil`,
      (b) =>
        isObject(b) &&
        b.field_id === fieldId &&
        typeof b.available === "boolean" &&
        Array.isArray(b.years) &&
        isObject(b.summary),
      signal,
    ),
};
