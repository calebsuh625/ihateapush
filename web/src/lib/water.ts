// Pure helpers for the "Water outlook" block (Phase 4, item 6): the pumping what-if, the words around
// the API's numbers, and the geometry of the small depth-to-water chart. No imports, so the unit
// tests can load this file directly. Every number shown comes from GET /field/{id}/plans
// (`water_outlook`); nothing is worked out here except where to draw it.

export interface WhatIfLike {
  /** the limit the plans on screen were scored under, inches an acre a year; null = no limit */
  limit_in: number | null;
  /** the planner's own limit; null when it has none */
  default_in: number | null;
  active: boolean;
  min_in: number;
  max_in: number;
}

export interface BandLike {
  median: number;
  low: number;
  high: number;
}

export interface WellLike {
  distance_mi: number;
  /** [winter, depth to water in feet], oldest first */
  points: readonly (readonly [number, number])[];
  /** the straight-line trend: its two ends, [winter, depth] */
  fit: readonly [readonly [number, number], readonly [number, number]];
}

// ---------------------------------------------------------------------------------------------
// "What if I can only pump N inches a year?"
// ---------------------------------------------------------------------------------------------

/** The slider's stops: whole inches from the API's lowest to its highest limit. */
export function limitRange(whatIf: WhatIfLike): { min: number; max: number } {
  const min = Math.ceil(whatIf.min_in);
  const max = Math.max(min, Math.floor(whatIf.max_in));
  return { min, max };
}

/** Where the slider stands: the farmer's own number when there is one, else the planner's limit (or the top). */
export function limitValue(choice: number | null | undefined, whatIf: WhatIfLike): number {
  const { min, max } = limitRange(whatIf);
  const value = typeof choice === "number" && Number.isFinite(choice) ? choice : (whatIf.default_in ?? max);
  return Math.min(max, Math.max(min, Math.round(value)));
}

/**
 * What a slider position means for the request: null at the planner's own limit (nothing is sent, the
 * usual plans come back), else the whole number of inches, kept inside the range the API takes.
 */
export function limitChoice(value: number, whatIf: WhatIfLike): number | null {
  if (!Number.isFinite(value)) return null;
  const { min, max } = limitRange(whatIf);
  const inches = Math.min(max, Math.max(min, Math.round(value)));
  return whatIf.default_in !== null && inches === Math.round(whatIf.default_in) ? null : inches;
}

/** 24 → "24 inches", 1 → "1 inch", 12.5 → "12.5 inches" */
export function inchesWords(inches: number): string {
  const rounded = Math.round(inches * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)} ${rounded === 1 ? "inch" : "inches"}`;
}

/** The line above the plan cards while a what-if is on. Null when the plans are the usual ones. */
export function whatIfLine(whatIf: WhatIfLike | null | undefined): string | null {
  if (!whatIf || !whatIf.active || whatIf.limit_in === null) return null;
  return `What-if: these plans are scored as if no more than ${inchesWords(whatIf.limit_in)} could be pumped on an acre in a year.`;
}

/** "Back to 24 inches" (or "Back to no limit"). */
export const resetWords = (whatIf: WhatIfLike): string =>
  whatIf.default_in === null ? "Back to no limit" : `Back to ${inchesWords(whatIf.default_in)}`;

// ---------------------------------------------------------------------------------------------
// Words around the numbers
// ---------------------------------------------------------------------------------------------

const whole = (n: number): string => (Number.isFinite(n) ? Math.round(n).toLocaleString("en-US") : "no figure");

/** 2.6 → "2.6", 3 → "3", -0.3 → "−0.3" (a real minus sign) */
export function oneDecimal(n: number): string {
  if (!Number.isFinite(n)) return "no figure";
  const rounded = Math.round(n * 10) / 10;
  const text = Number.isInteger(rounded) ? String(Math.abs(rounded)) : Math.abs(rounded).toFixed(1);
  return rounded < 0 ? `−${text}` : text;
}

/** {median: 138, low: 70, high: 146} → "138 ft (70 to 146)"; one well, or all the same → "138 ft" */
export function feetBand(band: BandLike): string {
  const mid = `${whole(band.median)} ft`;
  return Math.round(band.low) === Math.round(band.high) ? mid : `${mid} (${whole(band.low)} to ${whole(band.high)})`;
}

/** A well's own rate with its 95% interval: 5.4, [5.2, 5.7] → "5.4 (5.2 to 5.7)" */
export function rateWithInterval(rate: number, interval: readonly [number, number]): string {
  const [low, high] = interval;
  const mid = oneDecimal(rate);
  return oneDecimal(low) === oneDecimal(high) ? mid : `${mid} (${oneDecimal(low)} to ${oneDecimal(high)})`;
}

/** 1.9 → "1.9 mi" */
export const milesText = (miles: number): string => `${oneDecimal(miles)} mi`;

// ---------------------------------------------------------------------------------------------
// The chart: depth to water by winter, one line a well, deeper is lower
// ---------------------------------------------------------------------------------------------

export interface ChartBox {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

export interface ChartScales {
  /** first and last winter on the x axis */
  winters: [number, number];
  /** shallowest and deepest depth on the y axis, feet (round numbers) */
  depths: [number, number];
  depthTicks: number[];
  winterTicks: number[];
  x: (winter: number) => number;
  y: (depth: number) => number;
}

const STEPS = [5, 10, 20, 25, 50, 100, 200, 250, 500];

/** Round bounds and at most `maxTicks` ticks for a set of depths: 136..338 → [100, 350], step 50. */
export function depthDomain(low: number, high: number, maxTicks = 6): { domain: [number, number]; step: number } {
  if (!Number.isFinite(low) || !Number.isFinite(high)) return { domain: [0, 100], step: 50 };
  const [a, b] = low <= high ? [low, high] : [high, low];
  for (const step of STEPS) {
    const d0 = Math.floor(a / step) * step;
    const d1 = Math.max(Math.ceil(b / step) * step, d0 + step);
    if ((d1 - d0) / step + 1 <= maxTicks) return { domain: [d0, d1], step };
  }
  const step = STEPS[STEPS.length - 1] ?? 500;
  return { domain: [Math.floor(a / step) * step, Math.ceil(b / step) * step + step], step };
}

/** Scales for the wells' points and trend lines inside `box`. Null when there is nothing to draw. */
export function chartScales(wells: readonly WellLike[], box: ChartBox): ChartScales | null {
  const winters: number[] = [];
  const depths: number[] = [];
  for (const well of wells) {
    for (const [winter, depth] of [...well.points, ...well.fit]) {
      if (Number.isFinite(winter) && Number.isFinite(depth)) {
        winters.push(winter);
        depths.push(depth);
      }
    }
  }
  if (winters.length === 0) return null;
  const w0 = Math.min(...winters);
  const w1 = Math.max(Math.max(...winters), w0 + 1);
  const { domain, step } = depthDomain(Math.min(...depths), Math.max(...depths));
  const plotW = box.width - box.left - box.right;
  const plotH = box.height - box.top - box.bottom;
  const depthTicks: number[] = [];
  for (let d = domain[0]; d <= domain[1] + 1e-9; d += step) depthTicks.push(d);
  const winterTicks: number[] = [];
  for (let w = Math.ceil(w0 / 10) * 10; w <= w1; w += 10) winterTicks.push(w);
  return {
    winters: [w0, w1],
    depths: domain,
    depthTicks,
    winterTicks,
    x: (winter) => box.left + ((winter - w0) / (w1 - w0)) * plotW,
    y: (depth) => box.top + ((depth - domain[0]) / (domain[1] - domain[0])) * plotH,
  };
}

/** "M x y L x y ..." through a well's points, to one decimal. */
export function linePath(points: readonly (readonly [number, number])[], scales: ChartScales): string {
  return points
    .map(([winter, depth], i) => `${i === 0 ? "M" : "L"}${scales.x(winter).toFixed(1)} ${scales.y(depth).toFixed(1)}`)
    .join(" ");
}

/**
 * Label positions that do not sit on top of each other: each wanted position is kept when it can be,
 * otherwise the labels are pushed apart to `gap`, staying between `low` and `high`. Order is kept.
 */
export function spreadLabels(wanted: readonly number[], gap: number, low: number, high: number): number[] {
  const order = wanted.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y || a.i - b.i);
  const placed = order.map((entry) => Math.min(high, Math.max(low, entry.y)));
  for (let k = 1; k < placed.length; k += 1) {
    placed[k] = Math.max(placed[k] ?? 0, (placed[k - 1] ?? 0) + gap);
  }
  // pushed past the bottom: walk back up
  for (let k = placed.length - 1; k >= 0; k -= 1) {
    const limit = k === placed.length - 1 ? high : (placed[k + 1] ?? high) - gap;
    placed[k] = Math.min(placed[k] ?? 0, limit);
  }
  const out = new Array<number>(wanted.length).fill(0);
  order.forEach((entry, k) => {
    out[entry.i] = placed[k] ?? entry.y;
  });
  return out;
}

/** The winter nearest to an x position of the plot (for the hover line), kept inside the axis. */
export function winterAt(x: number, box: ChartBox, winters: readonly [number, number]): number {
  const plotW = box.width - box.left - box.right;
  const share = plotW > 0 ? (x - box.left) / plotW : 0;
  const winter = Math.round(winters[0] + share * (winters[1] - winters[0]));
  return Math.min(winters[1], Math.max(winters[0], winter));
}

/** A well's depth in one winter; null when it was not measured that winter. */
export function depthIn(well: WellLike, winter: number): number | null {
  const hit = well.points.find(([w]) => w === winter);
  return hit ? hit[1] : null;
}
