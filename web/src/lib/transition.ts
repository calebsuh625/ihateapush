// "How to get there": the request for GET /field/{id}/transition and the geometry of the small
// running-total chart. Pure helpers with no imports, so the unit tests can load this file directly.

/** How many years ahead the path can be shown for (the API takes 4 to 12). */
export const SPANS: readonly number[] = [4, 6, 8, 10, 12];
export const DEFAULT_SPAN = 8;

/** What the path leads to: one of our picks (1, 2 or 3) or the farmer's own rotation. */
export type TransitionTarget = { plan: number } | { rotation: readonly string[] };

/**
 * The query string for GET /field/{id}/transition, without the "?": the target, the number of
 * years, then the same settings the plan cards were asked with (`plansQuery`, lib/planner.ts).
 * Null when there is nothing to ask for (an empty rotation, a plan number that is not 1, 2 or 3).
 */
export function transitionQuery(plansQuery: string, target: TransitionTarget, years: number = DEFAULT_SPAN): string | null {
  const span = SPANS.includes(years) ? years : DEFAULT_SPAN;
  let first: string;
  if ("plan" in target) {
    if (!Number.isInteger(target.plan) || target.plan < 1 || target.plan > 3) return null;
    first = `plan=${target.plan}`;
  } else {
    const crops = target.rotation.filter((crop) => typeof crop === "string" && crop.trim() !== "");
    if (crops.length === 0 || crops.length !== target.rotation.length) return null;
    first = `target=${encodeURIComponent(crops.join(","))}`;
  }
  return `${first}&years=${span}${plansQuery ? `&${plansQuery}` : ""}`;
}

/** A difference in dollars: 45 → "+$45", -45.2 → "−$45" (a real minus sign), 0.3 → "$0". */
export function signedDollars(value: number): string {
  if (!Number.isFinite(value)) return "no figure";
  const rounded = Math.round(value);
  const digits = Math.abs(rounded).toLocaleString("en-US");
  if (rounded === 0) return "$0";
  return rounded < 0 ? `−$${digits}` : `+$${digits}`;
}

/** "−$136 to −$61" for the lowest and highest climate model of a difference; null when they are equal. */
export function signedRange(low: number, high: number): string | null {
  const [a, b] = [signedDollars(low), signedDollars(high)];
  return a === b ? null : `${a} to ${b}`;
}

export interface SpreadLike {
  low: number;
  mid: number;
  high: number;
}

/** The value scale of the running-total chart: always includes zero, never zero-height. */
export function totalDomain(spreads: readonly SpreadLike[]): [number, number] {
  let low = 0;
  let high = 0;
  for (const spread of spreads) {
    for (const value of [spread.low, spread.mid, spread.high]) {
      if (!Number.isFinite(value)) continue;
      low = Math.min(low, value);
      high = Math.max(high, value);
    }
  }
  if (high - low <= 0) return [-1, 1];
  return [low, high];
}

export interface ColumnGeometry {
  /** bottom edge and height of the low-high bar, in percent of the plot height */
  bottom: number;
  height: number;
  /** height of the middle mark above the plot's bottom, percent */
  mid: number;
}

/** Where one year's running total sits in a plot that covers `domain` bottom to top (0..100). */
export function columnGeometry(spread: SpreadLike, domain: readonly [number, number], minHeight = 3): ColumnGeometry {
  const [d0, d1] = domain;
  const span = d1 - d0;
  const at = (value: number) => (span > 0 && Number.isFinite(value) ? Math.min(100, Math.max(0, ((value - d0) / span) * 100)) : 0);
  const a = at(Math.min(spread.low, spread.high));
  const b = at(Math.max(spread.low, spread.high));
  let bottom = a;
  let height = b - a;
  if (height < minHeight) {
    // too short to see: grow around its centre, staying inside the plot
    bottom = Math.min(100 - minHeight, Math.max(0, (a + b) / 2 - minHeight / 2));
    height = minHeight;
  }
  return { bottom, height, mid: Math.min(bottom + height, Math.max(bottom, at(spread.mid))) };
}

/** Height of the zero line above the plot's bottom, percent. */
export function zeroAt(domain: readonly [number, number]): number {
  const [d0, d1] = domain;
  return d1 - d0 > 0 ? Math.min(100, Math.max(0, ((0 - d0) / (d1 - d0)) * 100)) : 50;
}

/** 2027 → "’27": year labels under a chart with up to twelve narrow columns. */
export const shortYear = (year: number): string => `’${String(year % 100).padStart(2, "0")}`;
