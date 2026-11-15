// The one place the app talks to the backend. Every call returns typed JSON or throws ApiError
// (or the browser's AbortError when the caller cancelled the request).
import type { FieldCollection, FieldDetail, FieldPlans, FieldSatellite, FieldSoil, Health } from "./types";

const configured: string | undefined = import.meta.env.VITE_API_URL;

/** "/api" in local dev (Vite proxies it to the FastAPI server), or an absolute URL from VITE_API_URL. */
export const API_BASE = (configured ?? "/api").replace(/\/+$/, "");

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

export async function getJson<T>(path: string, looksRight: (body: unknown) => boolean, signal?: AbortSignal): Promise<T> {
  const url = apiUrl(path);
  let res: Response;
  try {
    res = await fetch(API_BASE + path, { signal });
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

  field: (fieldId: string, signal?: AbortSignal) =>
    getJson<FieldDetail>(
      fieldPath(fieldId),
      (b) => isObject(b) && b.field_id === fieldId && isObject(b.crop_history) && isObject(b.stats),
      signal,
    ),

  plans: (fieldId: string, signal?: AbortSignal) =>
    getJson<FieldPlans>(
      `${fieldPath(fieldId)}/plans`,
      (b) => isObject(b) && b.field_id === fieldId && Array.isArray(b.plans),
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
