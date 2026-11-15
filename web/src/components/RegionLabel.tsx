// The county the API serves ("Gray County, KS"), from GET /region. Falls back to the built-in label
// while that loads or if the API is older than the endpoint. When the county borrows inputs from
// Finney County (climate futures, rotation effects, yields) it says so instead of passing them off as local.
import { useEffect, useState } from "react";
import { getJson } from "../api";
import { REGION_LABEL } from "../config";

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

const looksRight = (b: unknown): boolean =>
  typeof b === "object" && b !== null && typeof (b as RegionInfo).label === "string" &&
  Array.isArray((b as RegionInfo).borrowed);

export function RegionLabel() {
  const [info, setInfo] = useState<RegionInfo | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    getJson<RegionInfo>("/region", looksRight, ctrl.signal).then(setInfo, () => setInfo(null));
    return () => ctrl.abort();
  }, []);

  if (!info) return <>{REGION_LABEL}</>;
  if (info.borrowed.length === 0) return <span data-testid="region-label">{info.label}</span>;
  const notes = info.borrowed.map((b) => b.note).join("\n");
  return (
    <span data-testid="region-label" title={`Borrowed from Finney County until this county has its own:\n${notes}`}>
      {info.label} <span className="region-borrowed" style={{ opacity: 0.75 }}>· partly Finney data</span>
    </span>
  );
}
