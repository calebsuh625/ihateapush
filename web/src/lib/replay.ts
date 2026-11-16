// "How it would have done in real weather" (Phase 5, item B): the request for
// GET /field/{id}/replay, the answer's types and the geometry of the year-by-year bar chart.
// Pure helpers with no imports, so the unit tests can load this file directly.

/** What is replayed: one of our picks (its card number) or the farmer's own rotation. */
export type ReplayTarget = { plan: number } | { rotation: readonly string[] };

/**
 * The query string for GET /field/{id}/replay, without the "?": the plan or the rotation, then the same
 * settings the plan cards were asked with (`plansQuery`, lib/planner.ts). Null when there is nothing
 * to ask for (an empty rotation, a plan number that is not 1, 2 or 3).
 */
export function replayQuery(plansQuery: string, target: ReplayTarget): string | null {
  let first: string;
  if ("plan" in target) {
    if (!Number.isInteger(target.plan) || target.plan < 1 || target.plan > 3) return null;
    first = `plan=${target.plan}`;
  } else {
    const crops = target.rotation.filter((crop) => typeof crop === "string" && crop.trim() !== "");
    if (crops.length === 0 || crops.length !== target.rotation.length) return null;
    first = `rotation=${encodeURIComponent(crops.join(","))}`;
  }
  return plansQuery ? `${first}&${plansQuery}` : first;
}

export interface ReplayYear {
  year: number;
  dry: boolean;
  /** 1, 2 or 3 for the worst years of the bars shown; null otherwise */
  worst_rank: number | null;
  /** the crop of the shown starting year; null when the bars are the average of every start */
  crop: string | null;
  return: number;
  /** the lowest and highest return over every starting year of the cycle */
  low: number;
  high: number;
  yield_pct: number | null;
  yield: number | null;
  fail_chance: number | null;
  failed: boolean | null;
  failed_phases: number;
  water_in: number | null;
  water_met: number | null;
  below_cost: boolean;
  crops_by_phase: string[];
  returns_by_phase: number[];
}

export interface ReplaySummary {
  years: number;
  crop_years: number;
  worst_year: { year: number; return: number; crop?: string };
  best_year: { year: number; return: number; crop?: string };
  median_return: number;
  mean_return: number;
  below_cost_years: number;
  below_cost_share: number;
  planted_years: number;
  failed_years: number;
  failed_share: number;
  dry: {
    years: number;
    mean_return: number | null;
    other_mean_return: number | null;
    below_cost_years: number;
    failed_years: number;
    mean_yield_pct?: number | null;
    other_mean_yield_pct?: number | null;
  };
}

export interface ReplaySeries {
  name: string;
  cycle: string[];
  phases: number;
  phase: { known: boolean; count: number; starts_with: string; shown: number | null; first_year: number | null };
  basis: "this_start" | "all_starts";
  years: ReplayYear[];
  summary: ReplaySummary;
  all_starts: ReplaySummary;
  effects_range: { worst_return: [number, number]; median_return: [number, number]; below_cost_share: [number, number] } | null;
  worst_years: number[];
  worst_in_dry_years: number;
  model_error: { crop: string; typical_miss_pct: number | null; status: string | null }[];
  sentences: string[];
  repeating?: boolean;
}

export interface ReplayComparison {
  against: string;
  years: number[];
  difference: number[];
  carry_return: number[];
  mean_difference: number;
  median_difference: number;
  years_ahead: number;
  years_behind: number;
  years_same: number;
  total: number;
  worst_year: { plan: number; carry_on: number };
  median_return: { plan: number; carry_on: number };
  below_cost_share: { plan: number; carry_on: number };
  below_cost_years: { plan: number; carry_on: number };
  failed_years: { plan: number; carry_on: number };
  sentences: string[];
}

/** GET /field/{field_id}/replay?plan=1|2|3 or rotation=... (smart planner and the observed weather only) */
export interface FieldReplay {
  field_id: string;
  engine: "smart";
  status: "ok";
  settings: { irrigated: boolean; irrigation_limit_in: number | null; profit_basis: string };
  subject: { source: "plan" | "own"; plan: number | null; name: string; cycle: string[] };
  weather: {
    source: string;
    first_year: number;
    last_year: number;
    years: number;
    normal_season_rain_mm: number;
    season: string;
    dry_rule: string;
    dry_years: number[];
    rows: { year: number; season_rain_mm: number; rain_vs_normal_pct: number; hot_days: number; dry: boolean }[];
  };
  plan: ReplaySeries;
  carry_on: ReplaySeries | null;
  comparison: ReplayComparison | null;
  notes: string[];
  not_modelled: string[];
}

export interface ChartBox {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

/** The chart's frame: a 25-year record in 360 wide units, so a phone scales it down only a little. */
export const CHART: ChartBox = { width: 360, height: 240, left: 38, right: 4, top: 14, bottom: 26 };

/** A round step (50, 100, 200, 250, 500, ...) that puts at most `maxTicks` gridlines in a range. */
export function niceStep(range: number, maxTicks = 5): number {
  if (!Number.isFinite(range) || range <= 0) return 1;
  const raw = range / maxTicks;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (m * magnitude >= raw - 1e-9) return m * magnitude;
  }
  return 10 * magnitude;
}

/** The dollar scale of the chart: always holds zero, every bar, its low-high line and the carry-on ticks. */
export function returnDomain(years: readonly ReplayYear[], carry: readonly number[] = []): [number, number] {
  let low = 0;
  let high = 0;
  for (const y of years) {
    for (const v of [y.return, y.low, y.high]) {
      if (!Number.isFinite(v)) continue;
      low = Math.min(low, v);
      high = Math.max(high, v);
    }
  }
  for (const v of carry) {
    if (!Number.isFinite(v)) continue;
    low = Math.min(low, v);
    high = Math.max(high, v);
  }
  if (high - low <= 0) return [-10, 10];
  const step = niceStep(high - low);
  return [Math.floor(low / step) * step, Math.ceil(high / step) * step];
}

/** Gridline values of a domain, bottom to top (zero is always one of them). */
export function ticks(domain: readonly [number, number]): number[] {
  const [d0, d1] = domain;
  const step = niceStep(d1 - d0);
  const out: number[] = [];
  for (let v = Math.ceil(d0 / step) * step; v <= d1 + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

/** The y of a value in the chart frame (SVG y grows downwards). */
export function yOf(value: number, domain: readonly [number, number], box: ChartBox = CHART): number {
  const [d0, d1] = domain;
  const plot = box.height - box.top - box.bottom;
  const share = d1 - d0 > 0 ? (Math.min(d1, Math.max(d0, value)) - d0) / (d1 - d0) : 0;
  return box.top + plot * (1 - share);
}

export interface BarGeometry {
  /** left edge and width of the whole column of one year (the hit area and the dry band) */
  x: number;
  band: number;
  /** left edge and width of the bar itself */
  barX: number;
  barWidth: number;
  /** top and height of the bar: from the zero line to the return */
  y: number;
  height: number;
  /** the middle of the column */
  centre: number;
}

/** Where year `index` of `count` sits in the frame, with its bar from the zero line to its return. */
export function barGeometry(index: number, count: number, value: number, domain: readonly [number, number], box: ChartBox = CHART): BarGeometry {
  const plotWidth = box.width - box.left - box.right;
  const band = plotWidth / Math.max(1, count);
  const barWidth = Math.max(2, band * 0.68);
  const x = box.left + band * index;
  const zero = yOf(0, domain, box);
  const at = yOf(value, domain, box);
  const minHeight = 1.5; // a return of a few dollars is still a visible bar
  let y = Math.min(zero, at);
  let height = Math.abs(zero - at);
  if (height < minHeight) {
    y = value >= 0 ? zero - minHeight : zero;
    height = minHeight;
  }
  return { x, band, barX: x + (band - barWidth) / 2, barWidth, y, height, centre: x + band / 2 };
}

/** 2012 → "’12": year labels under a chart of 25 narrow columns. */
export const shortYear = (year: number): string => `’${String(year % 100).padStart(2, "0")}`;

/** Which columns get a year label: every `every`th from the first, so the last of a 25-year record is labelled. */
export function labelledYears(count: number, every = 4): number[] {
  const out: number[] = [];
  for (let i = 0; i < count; i += every) out.push(i);
  return out;
}

/** A difference in dollars: 45 → "+$45", -45.2 → "−$45" (a real minus sign), 0.3 → "$0". */
export function signedDollars(value: number): string {
  if (!Number.isFinite(value)) return "no figure";
  const rounded = Math.round(value);
  const digits = Math.abs(rounded).toLocaleString("en-US");
  if (rounded === 0) return "$0";
  return rounded < 0 ? `−$${digits}` : `+$${digits}`;
}

/** The words of a bar's hover title and of its row in the table, from the year's own numbers. */
export function yearTitle(y: ReplayYear, dollars: (v: number) => string, label: (crop: string) => string): string {
  const parts = [`${y.year}${y.dry ? " (dry growing season)" : ""}`];
  parts.push(y.crop ? label(y.crop) : "average of every start");
  parts.push(`return ${dollars(y.return)} an acre`);
  if (y.low !== y.high) parts.push(`${dollars(y.low)} to ${dollars(y.high)} over the starting years`);
  if (y.yield_pct !== null) parts.push(`yield ${Math.round(y.yield_pct)}% of normal`);
  if (y.failed === true) parts.push("crop failed");
  if (y.water_in !== null) parts.push(`${y.water_in.toFixed(1)} inches pumped`);
  if (y.water_met !== null && y.water_met < 0.995) parts.push(`${Math.round(y.water_met * 100)}% of water need met`);
  return parts.join(", ");
}
