// Whole-farm planner: which fields are in the farm, the request the app sends to POST /farm/plan,
// the two limit inputs, the lasso's "which fields are inside" test, the per-year chart's scale and
// the words on the farm panel. Pure helpers with no imports, so the unit tests can load this file
// directly.

// ---------------------------------------------------------------------------------------------
// The farm's fields
// ---------------------------------------------------------------------------------------------

/** Add `id` if it is not in the farm, remove it if it is. The order fields were added in is kept. */
export function toggleField(ids: readonly string[], id: string): string[] {
  return ids.includes(id) ? ids.filter((other) => other !== id) : [...ids, id];
}

/** Add every id that is not in the farm yet (a lasso only adds), keeping the order. */
export function addFields(ids: readonly string[], more: Iterable<string>): string[] {
  const seen = new Set(ids);
  const next = [...ids];
  for (const id of more) {
    if (seen.has(id)) continue;
    seen.add(id);
    next.push(id);
  }
  return next;
}

// ---------------------------------------------------------------------------------------------
// Lasso: fields whose centre falls inside the loop
// ---------------------------------------------------------------------------------------------

export interface Centre {
  id: string;
  lon: number;
  lat: number;
}

/**
 * A field's centre for the lasso: the middle of the box around its outer ring ([lon, lat]
 * positions), which is also where the map puts the locator ring. Null without usable points.
 */
export function ringCentre(ring: readonly (readonly number[])[] | undefined): { lon: number; lat: number } | null {
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (const position of ring ?? []) {
    const lon = position[0];
    const lat = position[1];
    if (lon === undefined || lat === undefined || !Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    if (lon < west) west = lon;
    if (lon > east) east = lon;
    if (lat < south) south = lat;
    if (lat > north) north = lat;
  }
  return west <= east && south <= north ? { lon: (west + east) / 2, lat: (south + north) / 2 } : null;
}

/** Even-odd test of one point against one closed loop of [x, y] points (the loop closes itself). */
export function insideLoop(x: number, y: number, loop: readonly (readonly number[])[]): boolean {
  let inside = false;
  for (let i = 0, j = loop.length - 1; i < loop.length; j = i++) {
    const ax = loop[i]?.[0];
    const ay = loop[i]?.[1];
    const bx = loop[j]?.[0];
    const by = loop[j]?.[1];
    if (ax === undefined || ay === undefined || bx === undefined || by === undefined) continue;
    if (ay > y !== by > y && x < ax + ((y - ay) * (bx - ax)) / (by - ay)) inside = !inside;
  }
  return inside;
}

/** Ids of the fields whose centre is inside `loop` ([lon, lat] points), in the order given. */
export function fieldsInside(centres: readonly Centre[], loop: readonly (readonly number[])[]): string[] {
  if (loop.length < 3) return [];
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (const point of loop) {
    const x = point[0];
    const y = point[1];
    if (x === undefined || y === undefined) continue;
    west = Math.min(west, x);
    east = Math.max(east, x);
    south = Math.min(south, y);
    north = Math.max(north, y);
  }
  const ids: string[] = [];
  for (const centre of centres) {
    if (centre.lon < west || centre.lon > east || centre.lat < south || centre.lat > north) continue;
    if (insideLoop(centre.lon, centre.lat, loop)) ids.push(centre.id);
  }
  return ids;
}

/** The four corners of the box dragged from `a` to `b` (pixel or lon/lat points alike). */
export function boxLoop(a: readonly [number, number], b: readonly [number, number]): [number, number][] {
  return [
    [a[0], a[1]],
    [b[0], a[1]],
    [b[0], b[1]],
    [a[0], b[1]],
  ];
}

/** Area enclosed by a loop of [x, y] points (shoelace), always positive. */
export function loopArea(loop: readonly (readonly number[])[]): number {
  let twice = 0;
  for (let i = 0, j = loop.length - 1; i < loop.length; j = i++) {
    twice += (loop[j]?.[0] ?? 0) * (loop[i]?.[1] ?? 0) - (loop[i]?.[0] ?? 0) * (loop[j]?.[1] ?? 0);
  }
  return Math.abs(twice) / 2;
}

// ---------------------------------------------------------------------------------------------
// The two limit inputs and the request
// ---------------------------------------------------------------------------------------------

export interface Amount {
  /** the number typed; null when the box is empty (no limit) or cannot be read */
  value: number | null;
  /** false when something was typed that is not a usable number */
  ok: boolean;
}

/**
 * What the farmer typed in a limit box: "" → no limit, "1,200" → 1200, "$20,000" → 20000.
 * `allowNegative` is for the minimum return (a farm can accept a small loss in its leanest year);
 * a water budget below zero is not a number we can use.
 */
export function parseAmount(text: string, allowNegative = false): Amount {
  const cleaned = text.replace(/[\s,$]/g, "").replace(/^[−–]/, "-");
  if (cleaned === "") return { value: null, ok: true };
  if (!/^-?(\d+\.?\d*|\.\d+)$/.test(cleaned)) return { value: null, ok: false };
  const value = Number(cleaned);
  if (!Number.isFinite(value) || (value < 0 && !allowNegative)) return { value: null, ok: false };
  return { value, ok: true };
}

export interface FarmChoices {
  weights: Record<"soil" | "water" | "money" | "risk", number>;
  horizon: number;
  scenario: string;
}

export interface FarmRequest {
  field_ids: string[];
  weights: Record<"soil" | "water" | "money" | "risk", number>;
  scenario: string;
  horizon: number;
  water_budget_acre_in: number | null;
  min_income: number | null;
}

/**
 * The body of POST /farm/plan, or null when there is nothing to ask (no fields, or a limit box
 * holds something that is not a number). Keys are always in the same order, so two equal requests
 * give the same JSON text (the app uses it as the request's identity).
 */
export function farmRequest(ids: readonly string[], choices: FarmChoices, waterText: string, minimumText: string): FarmRequest | null {
  if (ids.length === 0) return null;
  const water = parseAmount(waterText);
  const minimum = parseAmount(minimumText, true);
  if (!water.ok || !minimum.ok) return null;
  return {
    field_ids: [...ids],
    weights: {
      soil: choices.weights.soil,
      water: choices.weights.water,
      money: choices.weights.money,
      risk: choices.weights.risk,
    },
    scenario: choices.scenario === "ssp585" ? "ssp585" : "ssp245",
    horizon: choices.horizon,
    water_budget_acre_in: water.value,
    min_income: minimum.value,
  };
}

// ---------------------------------------------------------------------------------------------
// Numbers and words
// ---------------------------------------------------------------------------------------------

/** 1418 → "1,418" */
export const formatWhole = (n: number): string => (Number.isFinite(n) ? Math.round(n).toLocaleString("en-US") : "no figure");

/** 13900 → "$13,900", -1200 → "−$1,200" (a real minus sign) */
export function formatFarmDollars(n: number): string {
  if (!Number.isFinite(n)) return "no figure";
  const rounded = Math.round(n);
  const digits = Math.abs(rounded).toLocaleString("en-US");
  return rounded < 0 ? `−$${digits}` : `$${digits}`;
}

/** The farm's ranking score as the API rounds it: 0.639 → "0.639" */
export const formatFarmScore = (n: number): string => (Number.isFinite(n) ? n.toFixed(3) : "no figure");

/** 26.81 → "26.8" */
export const formatAcres = (n: number): string =>
  Number.isFinite(n) ? n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "no figure";

/** "$15,900 to $25,400" / "1,094 to 1,256" */
export const rangeWords = (low: number, high: number, format: (n: number) => string): string => `${format(low)} to ${format(high)}`;

/** [3, 6, 9] of 12 → "No, in 3 of 12 years"; [] → "Yes"; null → the words for "not set". */
export function metWords(ok: boolean | null, yearsBroken: readonly number[], years: number, notSet: string): string {
  if (ok === null) return notSet;
  if (ok) return "Yes, every year";
  return `No: ${yearsBroken.length} of ${years} years`;
}

export type LimitTone = "off" | "binds" | "free" | "cannot" | "unknown";

interface LimitLike {
  set: boolean;
  met: boolean | null;
  binds: boolean | null;
}

/** The short tag in front of a limit's sentence. */
export function limitTone(limit: LimitLike): LimitTone {
  if (!limit.set) return "off";
  if (limit.met === false) return "cannot";
  if (limit.binds === null) return "unknown";
  return limit.binds ? "binds" : "free";
}

export const LIMIT_TAGS: Record<LimitTone, string> = {
  off: "Not set",
  binds: "Binds",
  free: "Does not bind",
  cannot: "Cannot be met",
  unknown: "Not settled",
};

export type FieldChange = "same" | "shifted" | "changed";

interface FieldLike {
  same_rotation: boolean;
  same_start: boolean;
}

/** How a field's farm plan relates to its own first pick. */
export const fieldChange = (field: FieldLike): FieldChange =>
  !field.same_rotation ? "changed" : field.same_start ? "same" : "shifted";

export const FIELD_TAGS: Record<FieldChange, string> = {
  same: "Own first pick",
  shifted: "Own pick, other start year",
  changed: "Different rotation",
};

// ---------------------------------------------------------------------------------------------
// The per-year chart: one scale for the bars, the "each alone" ticks and the limit line
// ---------------------------------------------------------------------------------------------

export interface BandLike {
  mid: number;
  low: number;
  high: number;
}

/** Step of 1, 2 or 5 times a power of ten that gives about `count` steps over `span`. */
export function niceStep(span: number, count = 3): number {
  if (!(span > 0) || !Number.isFinite(span)) return 1;
  const raw = span / Math.max(1, count);
  const power = 10 ** Math.floor(Math.log10(raw));
  const lead = raw / power;
  return (lead <= 1 ? 1 : lead <= 2 ? 2 : lead <= 5 ? 5 : 10) * power;
}

export interface YearScale {
  /** the lowest and highest value drawn; always includes zero */
  domain: [number, number];
  /** round values for the grid lines, lowest first */
  ticks: number[];
  /** 0 (bottom of the plot) .. 1 (top) */
  at: (value: number) => number;
}

/**
 * The scale for one per-year chart. It covers every model range of the plan, every "each field
 * alone" value and the limit, starts at zero (bars of money and water are read from zero), and
 * ends on a round number.
 */
export function yearScale(bands: readonly BandLike[], others: readonly number[], limit: number | null): YearScale {
  let low = 0;
  let high = 0;
  const take = (value: number | null | undefined) => {
    if (typeof value !== "number" || !Number.isFinite(value)) return;
    low = Math.min(low, value);
    high = Math.max(high, value);
  };
  for (const band of bands) {
    take(band.low);
    take(band.mid);
    take(band.high);
  }
  others.forEach(take);
  take(limit);
  if (high - low <= 0) high = low + 1;
  const step = niceStep(high - low);
  const bottom = Math.floor(low / step) * step;
  const top = Math.ceil(high / step) * step;
  const ticks: number[] = [];
  for (let value = bottom; value <= top + step / 2; value += step) ticks.push(Math.round(value / step) * step);
  const span = top - bottom;
  return {
    domain: [bottom, top],
    ticks,
    at: (value) => (Number.isFinite(value) ? Math.min(1, Math.max(0, (value - bottom) / span)) : 0),
  };
}

/** 20000 → "$20k", 1500 → "$1.5k", 800 → "$800", -5000 → "−$5k": short labels for the money axis. */
export function shortDollars(n: number): string {
  const sign = n < 0 ? "−" : "";
  const size = Math.abs(n);
  if (size >= 1_000_000) return `${sign}$${trim(size / 1_000_000)}M`;
  if (size >= 1000) return `${sign}$${trim(size / 1000)}k`;
  return `${sign}$${trim(size)}`;
}

const trim = (n: number): string => String(Math.round(n * 10) / 10);
