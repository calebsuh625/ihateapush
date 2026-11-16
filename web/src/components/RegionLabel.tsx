// The county the API serves ("Gray County, KS"), from GET /region. Falls back to the built-in label
// while that loads or if the API is older than the endpoint. When the county borrows inputs from
// Finney County (climate futures, rotation effects, yields) it says so instead of passing them off as local.
// RegionPicker: when the site offers more than one county (GET /regions at the API root), it switches
// county: the page reloads with ?region=<fips> and the map fits the new county's fields.
import { useEffect, useState } from "react";
import { API_ROOT, SELECTED_REGION, getJson } from "../api";
import { REGION_LABEL } from "../config";
import { showLeaving } from "./BootLoader";

interface Borrowed {
  kind: string;
  note: string;
}

export interface RegionInfo {
  fips: string;
  label: string;
  is_default: boolean;
  borrowed: Borrowed[];
}

interface Offered {
  fips: string;
  label: string;
  is_default?: boolean;
  field_count?: number;
}

const looksRight = (b: unknown): boolean =>
  typeof b === "object" && b !== null && typeof (b as RegionInfo).label === "string" &&
  Array.isArray((b as RegionInfo).borrowed);

/** The counties on offer, from the API root (not the picked county's own API). */
async function offeredRegions(signal: AbortSignal): Promise<Offered[]> {
  const res = await fetch(`${API_ROOT}/regions`, { signal });
  if (!res.ok) return [];
  const body: unknown = await res.json();
  const list = (body as { regions?: unknown }).regions;
  if (!Array.isArray(list)) return [];
  return list.filter((r): r is Offered =>
    typeof r === "object" && r !== null && typeof (r as Offered).fips === "string" && typeof (r as Offered).label === "string");
}

/** Open another county: the page reloads with ?region=<fips>, and ?at=lat,lon when a search picked a
 * place in it (the map then starts there instead of framing the whole county). */
export function goTo(fips: string, defaultFips: string, opts: { label?: string; at?: [number, number] } = {}) {
  const url = new URL(window.location.href);
  if (fips === defaultFips) url.searchParams.delete("region");
  else url.searchParams.set("region", fips);
  if (opts.at) url.searchParams.set("at", `${opts.at[0].toFixed(5)},${opts.at[1].toFixed(5)}`);
  else url.searchParams.delete("at");
  url.hash = ""; // a field id from one county means nothing in another
  showLeaving(`Opening ${opts.label?.replace(/, KS$/, "") ?? "the county"}`);
  window.location.assign(url.toString());
}

/** Switch county; shown in the masthead when the site offers more than one. */
export function RegionPicker({ current = SELECTED_REGION }: { current?: string | null }) {
  const [offered, setOffered] = useState<Offered[]>([]);

  useEffect(() => {
    const ctrl = new AbortController();
    offeredRegions(ctrl.signal).then(setOffered, () => setOffered([]));
    return () => ctrl.abort();
  }, []);

  if (offered.length < 2) return null;
  const defaultFips = (offered.find((r) => r.is_default) ?? offered[0])?.fips ?? "";
  const value = current ?? defaultFips;
  return (
    <select
      className="region-picker"
      data-testid="region-picker"
      aria-label="Switch county"
      title="Switch county"
      value={value}
      onChange={(e) => goTo(e.target.value, defaultFips, { label: offered.find((r) => r.fips === e.target.value)?.label })}
    >
      {offered.map((r) => (
        <option key={r.fips} value={r.fips}>
          {r.label}
          {typeof r.field_count === "number" ? ` (${r.field_count.toLocaleString("en-US")} fields)` : ""}
        </option>
      ))}
    </select>
  );
}

export function RegionLabel() {
  const [info, setInfo] = useState<RegionInfo | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    const load = (left: number): void => {
      getJson<RegionInfo>("/region", looksRight, ctrl.signal).then(setInfo, () => {
        // a county's API can take a few seconds to start: try again rather than show the wrong name
        if (left > 0 && !ctrl.signal.aborted) window.setTimeout(() => load(left - 1), 3000);
      });
    };
    load(3);
    return () => ctrl.abort();
  }, []);

  if (!info) return <>{SELECTED_REGION ? "loading county…" : REGION_LABEL}</>;
  if (info.borrowed.length === 0) return <span data-testid="region-label">{info.label}</span>;
  const notes = info.borrowed.map((b) => b.note).join("\n");
  return (
    <span data-testid="region-label" title={`Borrowed from Finney County until this county has its own:\n${notes}`}>
      {info.label} <span className="region-borrowed" style={{ opacity: 0.75 }}>· partly Finney data</span>
    </span>
  );
}
