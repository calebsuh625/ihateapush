// GET /field/{id}/neighbors: response shapes and the pure helpers the "Your neighbors" section
// draws with. No imports, so the unit tests can load this file directly.

export interface SoilPoint {
  year: number;
  value: number | null;
}

export interface ComparisonEffect {
  a: string;
  b: string;
  effect: number | null;
  ci_low: number | null;
  ci_high: number | null;
  n_pairs: number;
  p_value: number | null;
  ci_excludes_zero: boolean;
}

export interface Neighbor {
  pair_id: string;
  field_id: string;
  comparison: string;
  /** the neighbour's 2016–2024 rotation type */
  rotation: string;
  rotation_pre: string;
  /** this field's 2016–2024 rotation type */
  this_rotation: string;
  irrigation: string;
  distance_km: number | null;
  match_score: number | null;
  similarity: {
    slope_diff: number | null;
    ssurgo_om_diff: number | null;
    baseline_diff: number | null;
    /** 0 = the same 2008–2015 crop mix, 1 = nothing in common */
    history_distance: number | null;
  };
  /** how much more this field's soil score moved (before → after the switch) than the neighbour's */
  divergence: number | null;
  crop_history: Record<string, string>;
  soil: SoilPoint[];
  comparison_effect: ComparisonEffect | null;
}

export interface Outcome {
  column: string;
  label: string;
  units: string;
  higher_is_better: boolean;
}

export interface FieldNeighbors {
  field_id: string;
  is_fake: boolean;
  outcome: Outcome | null;
  switch_year: number;
  field: {
    rotation_pre: string;
    rotation_post: string;
    irrigation: string;
    irrigation_reason: string;
    soil_group: string;
    eligible: boolean;
  } | null;
  crop_history: Record<string, string>;
  soil: SoilPoint[];
  neighbors: Neighbor[];
  reason: string | null;
  examples: string[];
}

const MINUS = "−";

/** 0.1122 → "+0.11", -0.05 → "−0.05" (a real minus sign). */
export function formatSigned(n: number, decimals = 2): string {
  const fixed = Math.abs(n).toFixed(decimals);
  if (Number(fixed) === 0) return (0).toFixed(decimals);
  return `${n < 0 ? MINUS : "+"}${fixed}`;
}

/** "alfalfa/perennial" → "Alfalfa / perennial", "corn-soy" → "Corn–soy". */
export function rotationLabel(type: string): string {
  const text = type.replace("/", " / ").replace(/-/g, "–");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Share of the 2008–2015 crop mix two fields have in common, as a whole percent. */
export function sharedHistoryPct(historyDistance: number | null): number | null {
  if (historyDistance === null || !Number.isFinite(historyDistance)) return null;
  return Math.round(100 * (1 - Math.min(1, Math.max(0, historyDistance))));
}

export interface ChartGeometry {
  /** SVG path per series ("" when the series has no point) */
  paths: string[];
  x: (year: number) => number;
  y: (value: number) => number;
  min: number;
  max: number;
  firstYear: number;
  lastYear: number;
}

/**
 * Shared scales for several yearly series in a width × height box with `pad` on every side.
 * A missing value breaks the line (a new "M" segment) instead of drawing across the gap.
 */
export function chartGeometry(series: SoilPoint[][], width: number, height: number, pad = 4): ChartGeometry {
  const points = series.flat().filter((p): p is { year: number; value: number } => p.value !== null && Number.isFinite(p.value));
  const years = series.flat().map((p) => p.year);
  const firstYear = years.length ? Math.min(...years) : 0;
  const lastYear = years.length ? Math.max(...years) : 1;
  let min = points.length ? Math.min(...points.map((p) => p.value)) : 0;
  let max = points.length ? Math.max(...points.map((p) => p.value)) : 1;
  if (max - min < 1e-9) {
    min -= 0.5;
    max += 0.5;
  }
  const spanX = Math.max(1, lastYear - firstYear);
  const x = (year: number) => pad + ((year - firstYear) / spanX) * (width - 2 * pad);
  const y = (value: number) => height - pad - ((value - min) / (max - min)) * (height - 2 * pad);
  const paths = series.map((s) => {
    let d = "";
    let pen = false;
    for (const p of [...s].sort((a, b) => a.year - b.year)) {
      if (p.value === null || !Number.isFinite(p.value)) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${x(p.year).toFixed(1)},${y(p.value).toFixed(1)}`;
      pen = true;
    }
    return d;
  });
  return { paths, x, y, min, max, firstYear, lastYear };
}
