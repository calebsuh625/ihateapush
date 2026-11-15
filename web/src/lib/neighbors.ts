// GET /field/{id}/neighbors: response shapes and the pure helpers the "Your neighbors" section
// draws with. No imports, so the unit tests can load this file directly.

export interface SoilPoint {
  year: number;
  value: number | null;
}

/** An estimate with its 95% interval, in outcome units. */
export interface Interval {
  effect: number | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_excludes_zero: boolean;
}

export interface ComparisonEffect extends Interval {
  a: string;
  b: string;
  n_pairs: number;
  p_value: number | null;
  /** the rotation whose outcome moved the better way; null when the interval includes zero */
  favours?: "a" | "b" | null;
  /** the part of `effect` explained by which crops were grown each year */
  from_crops?: Interval | null;
  /** what is left of `effect` after that */
  beyond_crops?: Interval | null;
}

export type Verdict = "better" | "worse";

export interface Neighbor {
  pair_id: string;
  field_id: string;
  comparison: string;
  /** the neighbour's rotation type from the switch year on */
  rotation: string;
  rotation_pre: string;
  /** this field's rotation type from the switch year on */
  this_rotation: string;
  irrigation: string;
  distance_km: number | null;
  match_score: number | null;
  similarity: {
    slope_diff: number | null;
    ssurgo_om_diff: number | null;
    baseline_diff: number | null;
    /** 0 = the same crop mix before the switch year, 1 = nothing in common */
    history_distance: number | null;
  };
  /** each field's mean in the "before" years and in the "after" years (years both fields have) */
  before?: { this: number | null; neighbor: number | null };
  after?: { this: number | null; neighbor: number | null };
  /** a level: this field's "after" mean minus the neighbour's, in outcome units */
  after_gap?: number | null;
  /** a change: how much more this field's value moved (before → after) than the neighbour's */
  divergence: number | null;
  /** which field stood better in the "after" years (from after_gap, never from divergence) */
  verdict?: Verdict | null;
  crop_history: Record<string, string>;
  soil: SoilPoint[];
  comparison_effect: ComparisonEffect | null;
}

export interface Outcome {
  column: string;
  label: string;
  units: string;
  higher_is_better: boolean;
  /** one plain sentence on how to read a gap in this outcome */
  note?: string | null;
}

/** Year ranges of the run, each [first, last]. */
export interface Design {
  switch_year: number;
  history_years: [number, number];
  rotation_years: [number, number];
  baseline_years: [number, number];
  pre_years: [number, number];
  post_years: [number, number];
}

export interface FieldNeighbors {
  field_id: string;
  is_fake: boolean;
  outcome: Outcome | null;
  switch_year: number;
  design?: Design;
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

/**
 * "alfalfa/perennial" → "Alfalfa / perennial", "corn-soy" → "Corn–soy". The "diverse" type is
 * shown as "Mixed (3+ crops)": it only means three or more different crops in the rotation years,
 * and about one such field in six is fallow in two or more of them.
 */
export function rotationLabel(type: string): string {
  if (type === "diverse") return "Mixed (3+ crops)";
  const text = type.replace("/", " / ").replace(/-/g, "–");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A short explanation for a rotation tag that needs one, else null. */
export function rotationHint(type: string): string | null {
  if (type === "diverse") return "Three or more different crops in these years. Fallow years count as one of them.";
  if (type === "alfalfa/perennial") return "Alfalfa, hay or grass/pasture in at least two of these years.";
  return null;
}

/** Share of the pre-switch crop mix two fields have in common, as a whole percent. */
export function sharedHistoryPct(historyDistance: number | null): number | null {
  if (historyDistance === null || !Number.isFinite(historyDistance)) return null;
  return Math.round(100 * (1 - Math.min(1, Math.max(0, historyDistance))));
}

/** [2008, 2019] → "2008–2019"; [2016, 2016] → "2016". */
export function yearSpan(years: readonly number[] | null | undefined): string {
  if (!years || years.length !== 2) return "";
  return years[0] === years[1] ? String(years[0]) : `${years[0]}–${years[1]}`;
}

const isDays = (outcome: Outcome) => /\bdays?\b/i.test(outcome.units);

/** Decimals for one field's value (days: none) and for an average effect (days: one). */
export function valueDecimals(outcome: Outcome): number {
  return isDays(outcome) ? 0 : 2;
}
export function effectDecimals(outcome: Outcome): number {
  return isDays(outcome) ? 1 : 2;
}

/** "days per year" → "days a year"; other units unchanged. */
export function unitsText(outcome: Outcome): string {
  return outcome.units.replace(/ per /g, " a ");
}

/**
 * This field's "after" level minus its look-alike's (the API's `after_gap`, else from `after`).
 * A level, not a change: the two can point opposite ways.
 */
export function afterGap(n: Pick<Neighbor, "after_gap" | "after">): number | null {
  if (typeof n.after_gap === "number" && Number.isFinite(n.after_gap)) return n.after_gap;
  const a = n.after?.this;
  const b = n.after?.neighbor;
  if (typeof a !== "number" || typeof b !== "number" || !Number.isFinite(a) || !Number.isFinite(b)) return null;
  return a - b;
}

const roundsToZero = (n: number, outcome: Outcome) => Number(Math.abs(n).toFixed(valueDecimals(outcome))) === 0;

/**
 * Which field stood better in the "after" years. `gap` is a LEVEL gap (this field minus its
 * look-alike, see afterGap), never the change: for bare days lower is better, so a negative gap is
 * "better". Null when there is no gap to speak of (missing, or zero at the shown precision).
 */
export function verdictOf(gap: number | null, outcome: Outcome): Verdict | null {
  if (gap === null || !Number.isFinite(gap) || roundsToZero(gap, outcome)) return null;
  return gap > 0 === outcome.higher_is_better ? "better" : "worse";
}

/** The word on the verdict tag. Bare days say what happened to the soil's cover, not to the soil. */
export function verdictLabel(verdict: Verdict, outcome: Outcome): string {
  if (outcome.column === "bare_days") return verdict === "better" ? "better protected" : "more exposed";
  return verdict;
}

/**
 * A level gap in words, to sit between "this field" and "its look-alike":
 * "sat bare 25 more days a year than" / "was 0.11 percentage points higher than".
 */
export function levelText(gap: number, outcome: Outcome): string {
  const size = Math.abs(gap).toFixed(valueDecimals(outcome));
  if (isDays(outcome)) {
    const verb = outcome.column === "bare_days" ? "sat bare" : "had";
    if (roundsToZero(gap, outcome)) return `${verb} about as many ${unitsText(outcome)} as`;
    const units = size === "1" ? unitsText(outcome).replace(/^days\b/, "day") : unitsText(outcome);
    return `${verb} ${size} ${gap < 0 ? "fewer" : "more"} ${units} than`;
  }
  if (roundsToZero(gap, outcome)) return "was level with";
  return `was ${size} ${outcome.units} ${gap < 0 ? "lower" : "higher"} than`;
}

/**
 * How one change compares with another, as plain arithmetic: "146 days a year lower" /
 * "0.11 percentage points higher". `divergence` = this field's change minus its look-alike's.
 * It says nothing about which field ended up with the lower value.
 */
export function changeText(divergence: number, outcome: Outcome): string {
  const size = Math.abs(divergence).toFixed(valueDecimals(outcome));
  if (roundsToZero(divergence, outcome)) return "the same";
  return `${size} ${unitsText(outcome)} ${divergence < 0 ? "lower" : "higher"}`;
}

/**
 * What a comparison's average says, in words: on which rotation the outcome moved the better way
 * against its look-alikes, or that the data cannot tell. The average is a change (how the gap
 * moved since the "before" years), so the words describe a move, not a level. Never more than the
 * interval supports.
 */
export function effectReading(e: ComparisonEffect, outcome: Outcome): string {
  if (e.effect === null || !e.ci_excludes_zero) return "not distinguishable from zero";
  const ahead = e.effect > 0 === outcome.higher_is_better ? e.a : e.b;
  const name = rotationLabel(ahead).toLowerCase();
  if (outcome.column === "bare_days") return `bare days moved lower on ${name} fields than on their look-alikes`;
  return `${name} fields moved the better way against their look-alikes`;
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
