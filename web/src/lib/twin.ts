// GET /field/{id}/twin: response shapes and the pure helpers the twin view draws with.
// No imports, so the unit tests can load this file directly. Nothing here works out a finding:
// every number and every sentence about the data comes from the API's answer; this file only
// turns numbers into pixel positions and a few labels.

export interface BarePoint {
  year: number;
  value: number | null;
}

export interface TwinSide {
  field_id: string;
  rotation_pre: string | null;
  rotation_post: string | null;
  irrigation: string | null;
  soil_group: string | null;
  /** year (text) -> crop, 2008..2024 */
  crop_history: Record<string, string>;
  /** every soil year, in order; `value: null` is a year with no reading (a gap) */
  bare_days: BarePoint[];
}

export interface MatchItem {
  key: string;
  label: string;
  value: number | string | null;
  limit: number | null;
  unit: string;
  text: string;
}

export interface Interval {
  effect: number | null;
  ci_low: number | null;
  ci_high: number | null;
  ci_excludes_zero: boolean;
}

export interface Change {
  before: number | null;
  after: number | null;
  change: number | null;
}

export interface Spread {
  n: number;
  median: number | null;
  p25: number | null;
  p75: number | null;
  min: number | null;
  max: number | null;
}

export interface Agreement {
  other_pairs: number;
  same_way: number;
  opposite_way: number;
  no_change: number;
  this_pair_direction: "fewer" | "more" | null;
  spread_of_other_pairs: Spread | null;
  sentence: string;
}

export interface Placebo {
  fake_switch: {
    rows: { fake_switch_year: number; effect: number | null; ci_low: number | null; ci_high: number | null; ci_excludes_zero: boolean }[];
    n_failed: number;
    n: number;
  };
  shuffled_labels: { p_value: number | null; null_p95_abs: number | null; n_shuffles: number | null } | null;
  county: { comparisons_tested: number; comparisons_failed: number };
  passed: boolean | null;
  sentence: string;
}

export interface TwinAnswer {
  field_id: string;
  is_fake: boolean;
  outcome: { column: string; label: string; units: string; higher_is_better: boolean; note?: string | null } | null;
  switch_year: number;
  design: {
    switch_year: number;
    history_years: [number, number];
    rotation_years: [number, number];
    baseline_years: [number, number];
    pre_years: [number, number];
    post_years: [number, number];
  };
  soil_years: number[];
  this: TwinSide;
  has_twin: boolean;
  reason: string | null;
  examples: string[];
  twin: TwinSide | null;
  match: {
    pair_id: string;
    pairs_of_this_field: number;
    matched_on: MatchItem[];
    closeness: { score: number | null; worst_possible: number; closer_than_pct: number | null; n_pairs_in_run: number; median_score: number | null };
    distance_km: number | null;
    sentence: string;
  } | null;
  measured: {
    pre_years: [number, number];
    post_years: [number, number];
    this: Change;
    twin: Change;
    level_gap_after: number | null;
    difference_in_change: number | null;
    from_crops: number | null;
    beyond_crops: number | null;
    years_used: { before: number; after: number };
    sentence: string;
  } | null;
  comparison: {
    name: string;
    a: string;
    b: string;
    this_is: "a" | "b";
    this_rotation: string;
    twin_rotation: string;
    n_pairs: number;
    estimate: Interval | null;
    from_crops: Interval | null;
    as_stored: Record<string, unknown> | null;
    agreement: Agreement;
    placebo: Placebo;
    sentence: string;
  } | null;
}

// ---------------------------------------------------------------------------------------------
// Scales
// ---------------------------------------------------------------------------------------------

const finite = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** Round tick positions covering [low, high]: steps of 1, 2 or 5 times a power of ten. */
export function niceTicks(low: number, high: number, target = 5): { ticks: number[]; low: number; high: number } {
  const span = high - low > 1e-9 ? high - low : Math.max(1, Math.abs(high) || 1);
  const rough = span / Math.max(1, target);
  const power = 10 ** Math.floor(Math.log10(rough));
  const fraction = rough / power;
  const step = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power;
  const first = Math.floor(low / step + 1e-9) * step;
  let last = Math.ceil(high / step - 1e-9) * step;
  if (last <= first) last = first + step; // a flat range still gets an axis with room above
  const ticks: number[] = [];
  for (let t = first; t <= last + step / 2; t += step) ticks.push(Math.abs(t) < step / 1e6 ? 0 : Number(t.toFixed(10)));
  return { ticks, low: first, high: last };
}

export interface LineChart {
  width: number;
  height: number;
  x: (year: number) => number;
  y: (value: number) => number;
  yTicks: number[];
  years: number[];
  /** one SVG path per series: a missing value breaks the line instead of drawing across it */
  paths: string[];
  /** [x, y, year] for every real value, per series (for markers) */
  dots: { x: number; y: number; year: number; value: number }[][];
  /** x of the cut-off: halfway between the last "before" year and the switch year */
  cutX: number | null;
  /** left edge of the "after" shading and where it ends */
  afterX: [number, number] | null;
  plot: { left: number; right: number; top: number; bottom: number };
}

/**
 * Two (or more) yearly series on one set of axes. The vertical scale starts at zero (days the soil
 * sits bare are a count; a line chart that starts elsewhere exaggerates differences) and ends at a
 * round number above the largest value. Gaps stay gaps.
 */
export function lineChart(series: BarePoint[][], years: number[], switchYear: number, width = 420, height = 220): LineChart {
  const plot = { left: 36, right: width - 22, top: 12, bottom: height - 30 };
  const values = series.flat().map((p) => p.value).filter(finite);
  const { ticks, high } = niceTicks(0, values.length ? Math.max(...values) : 1, 4);
  const first = years.length ? Math.min(...years) : 0;
  const last = years.length ? Math.max(...years) : 1;
  const spanX = Math.max(1, last - first);
  const x = (year: number) => plot.left + ((year - first) / spanX) * (plot.right - plot.left);
  const y = (value: number) => plot.bottom - (value / (high || 1)) * (plot.bottom - plot.top);
  const paths: string[] = [];
  const dots: LineChart["dots"] = [];
  for (const s of series) {
    let d = "";
    let pen = false;
    const own: LineChart["dots"][number] = [];
    for (const p of [...s].sort((a, b) => a.year - b.year)) {
      if (!finite(p.value)) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${x(p.year).toFixed(1)},${y(p.value).toFixed(1)}`;
      own.push({ x: x(p.year), y: y(p.value), year: p.year, value: p.value });
      pen = true;
    }
    paths.push(d);
    dots.push(own);
  }
  const hasCut = years.includes(switchYear) && years.includes(switchYear - 1);
  const cutX = hasCut ? (x(switchYear) + x(switchYear - 1)) / 2 : null;
  return {
    width,
    height,
    x,
    y,
    yTicks: ticks,
    years: [...years],
    paths,
    dots,
    cutX,
    afterX: cutX === null ? null : [cutX, plot.right],
    plot,
  };
}

export interface DifferenceAxis {
  width: number;
  x: (value: number) => number;
  ticks: number[];
  low: number;
  high: number;
  zeroX: number;
  left: number;
  right: number;
}

/**
 * One horizontal axis for numbers that can be negative or positive, always containing zero and
 * every value given (nulls are ignored). The same axis carries the pair's difference, the spread
 * of the other pairs and the comparison's interval, so they can be read against each other.
 */
export function differenceAxis(values: (number | null | undefined)[], width = 420, left = 14, right = 14): DifferenceAxis {
  const real = values.filter(finite);
  const lo = Math.min(0, ...real);
  const hi = Math.max(0, ...real);
  const pad = Math.max((hi - lo) * 0.06, 1e-6);
  const { ticks, low, high } = niceTicks(lo - pad, hi + pad, 5);
  const x = (value: number) => left + ((value - low) / (high - low || 1)) * (width - left - right);
  return { width, x, ticks, low, high, zeroX: x(0), left, right: width - right };
}

/** Segments of the "how many pairs agree" bar, as shares of the other pairs (they add up to 1). */
export function agreementShares(a: Pick<Agreement, "other_pairs" | "same_way" | "opposite_way" | "no_change">): { same: number; opposite: number; none: number } {
  const n = a.other_pairs;
  if (n <= 0) return { same: 0, opposite: 0, none: 0 };
  return { same: a.same_way / n, opposite: a.opposite_way / n, none: a.no_change / n };
}

/** Every year from the first to the last of the two crop histories, ascending. */
export function yearRange(...histories: Record<string, string>[]): number[] {
  const years = histories.flatMap((h) => Object.keys(h).map(Number)).filter(Number.isInteger);
  if (years.length === 0) return [];
  const out: number[] = [];
  for (let y = Math.min(...years); y <= Math.max(...years); y++) out.push(y);
  return out;
}

/** The bare-days reading of a year in a series: a number, or null for a gap or a year outside the series. */
export function readingIn(series: BarePoint[], year: number): number | null {
  const v = series.find((p) => p.year === year)?.value;
  return finite(v) ? v : null;
}

const MINUS = "−";

/** 12.04 → "+12.0", −3 → "−3.0" (a real minus sign, one decimal, never "-0.0"). */
export function signed(value: number | null | undefined, decimals = 1): string {
  if (!finite(value)) return "?";
  const text = Math.abs(value).toFixed(decimals);
  if (Number(text) === 0) return (0).toFixed(decimals);
  return `${value < 0 ? MINUS : "+"}${text}`;
}

/** The height of the bottom sheet that holds the view, in pixels: the map keeps the rest. */
export function sheetHeight(viewportHeight: number): number {
  return Math.round(Math.min(viewportHeight * 0.62, 600));
}
