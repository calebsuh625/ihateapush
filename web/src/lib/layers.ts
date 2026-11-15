// Map layers: the answers of GET /layers and GET /layers/{name}, turned into colours and numbers for the
// map. Everything here is plain functions on plain data (no React, no Leaflet), so it can be unit tested.
//
// A layer's `values[i]` belongs to `features[i]` of GET /fields. The class breaks, units and sentences come
// from the API; the colours are ours (the API has none).

/** "No value" in a class array. Never a class index (a layer has far fewer than 255 classes). */
export const NONE = 255;
/** The first choice in the layer control: the map as it always was. */
export const FIELDS_LAYER = "fields";

export interface LayerClass {
  index: number;
  label: string;
  /** class layers: the stable name of the class */
  key?: string;
  /** class layers: this class means "we cannot tell" and is drawn grey */
  no_value?: boolean;
  min_scaled?: number | null;
  max_scaled?: number | null;
}

export interface ClassCount {
  index: number;
  fields: number;
  acres: number;
  share_fields: number | null;
  share_acres: number | null;
  /** class layers with a detail: the county median of the detail in this class, in the detail's unit */
  detail_median?: number | null;
}

/** The county-wide summary the API gives for one year of a layer. */
export interface LayerSummary {
  fields: number;
  fields_with_value: number;
  fields_no_value: number;
  acres?: number;
  acres_with_value?: number;
  median?: number | null;
  p25?: number | null;
  p75?: number | null;
  classes: ClassCount[];
}

export interface LayerSeries {
  year: number | null;
  values: (number | null)[];
  detail?: (number | null)[];
  summary: LayerSummary;
}

export interface LayerInfo {
  name: string;
  title: string;
  kind: "number" | "class";
  status: "ready" | "building" | "unavailable";
  reason: string | null;
  unit: string | null;
  value_scale: number;
  years: number[] | null;
  method: string;
  caveat: string;
  field_count: number;
  higher_is_worse?: boolean;
  ordered?: boolean;
  breaks_scaled?: number[];
  classes?: LayerClass[];
  progress?: { done: number; total: number };
  retry_after_s?: number;
  /** erosion_risk: the wind factor of each year (100 = the 2000-2024 average) */
  inputs?: { erosion_climate_by_year?: Record<string, number>; erosion_climate_note?: string };
  /** class layers: what the second number per field (`series.detail`) is */
  detail?: { key: string; label: string; unit: string | null; value_scale: number; note?: string };
}

/** GET /layers */
export interface LayerCatalog {
  field_count: number;
  fields_etag: string;
  is_fake: boolean;
  layers: LayerInfo[];
}

/** GET /layers/{name} */
export interface LayerAnswer extends LayerInfo {
  fields_etag: string;
  is_fake: boolean;
  series?: LayerSeries[];
}

// ---------------------------------------------------------------------------------------------
// Colours. Every scale here changes in lightness from step to step (so it still reads without
// colour vision and in greyscale), and none of them uses the grey of "no value", the blue of the
// selected field, the amber of the twin or the orange of the farm outlines for its fill.
// ---------------------------------------------------------------------------------------------

/** A field with no value. Neutral grey: it has no hue, and it is never in a scale. */
export const NO_VALUE_COLOUR = "#a9afab";

const SEQUENTIAL: Record<string, readonly string[]> = {
  /** bare days: pale lilac to deep purple */
  bare_days: ["#efe5f6", "#cfb0e0", "#a76dc4", "#7a3a9a", "#4a1566"],
  /** wind erosion risk: pale mint to deep teal */
  erosion_risk: ["#dff2ee", "#9ed3cb", "#52a89f", "#1f7a74", "#0d4a4a"],
};
/** Any other number layer, and the ordered class layers (rotation change): pale rose to deep plum. */
const FALLBACK_SEQUENTIAL: readonly string[] = ["#fbe3ec", "#f3b1ca", "#dd6c9b", "#b02f6c", "#6e0f45"];
/** Unordered classes, by key, then by position. Ochre and teal differ in hue and in lightness. */
const KEYED: Record<string, string> = { dryland: "#d9a441", irrigated: "#1b7f8a" };
const CATEGORICAL: readonly string[] = ["#d9a441", "#1b7f8a", "#a8527a", "#5e8c3a"];

/** The scales, for the tests. */
export const PALETTES = { ...SEQUENTIAL, rotation_change: FALLBACK_SEQUENTIAL } as const;

/** `n` evenly spaced steps of a scale (n of 1 gives its middle). */
export function pickSteps(scale: readonly string[], n: number): string[] {
  if (n <= 1) return [scale[Math.floor(scale.length / 2)] ?? NO_VALUE_COLOUR];
  if (n >= scale.length) return scale.slice(0, n).concat(Array(Math.max(0, n - scale.length)).fill(scale[scale.length - 1]));
  return Array.from({ length: n }, (_, i) => scale[Math.round((i * (scale.length - 1)) / (n - 1))] ?? NO_VALUE_COLOUR);
}

/** The colour of every class of a layer, by class index. */
export function classColours(layer: LayerInfo): string[] {
  const classes = layer.classes ?? [];
  if (layer.kind === "number") {
    return pickSteps(SEQUENTIAL[layer.name] ?? FALLBACK_SEQUENTIAL, classes.length);
  }
  const real = classes.filter((c) => !c.no_value);
  const ordered = layer.ordered === true;
  const steps = ordered ? pickSteps(SEQUENTIAL[layer.name] ?? FALLBACK_SEQUENTIAL, real.length) : [];
  let seen = 0;
  return classes.map((c) => {
    if (c.no_value) return NO_VALUE_COLOUR;
    const position = seen++;
    if (ordered) return steps[position] ?? NO_VALUE_COLOUR;
    return (c.key !== undefined ? KEYED[c.key] : undefined) ?? CATEGORICAL[position % CATEGORICAL.length] ?? NO_VALUE_COLOUR;
  });
}

/** WCAG relative luminance of "#rrggbb" (0 black … 1 white). */
export function luminance(hex: string): number {
  const rgb = [1, 3, 5].map((at) => {
    const c = Number.parseInt(hex.slice(at, at + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * (rgb[0] ?? 0) + 0.7152 * (rgb[1] ?? 0) + 0.0722 * (rgb[2] ?? 0);
}

// ---------------------------------------------------------------------------------------------
// From the API arrays to one class index per field, and to a colour per field.
// ---------------------------------------------------------------------------------------------

/** Class index = how many of `breaks` are at or below `value`. */
export function classOf(value: number, breaks: readonly number[]): number {
  let n = 0;
  for (const edge of breaks) if (value >= edge) n += 1;
  return n;
}

/** The year the slider shows for a layer: the wanted one if the layer has it, else the nearest, else null. */
export function yearFor(layer: Pick<LayerInfo, "years">, wanted: number | null): number | null {
  const years = layer.years;
  if (!years || years.length === 0) return null;
  if (wanted === null) return years[years.length - 1] ?? null;
  let best = years[0] ?? null;
  for (const year of years) if (best === null || Math.abs(year - wanted) < Math.abs(best - wanted)) best = year;
  return best;
}

/** The series of one year (or the only series of a layer without years). */
export function seriesFor(answer: LayerAnswer, year: number | null): LayerSeries | null {
  const all = answer.series ?? [];
  if (answer.years && year !== null) return all.find((s) => s.year === year) ?? null;
  return all[0] ?? null;
}

/** One class index per field (NONE where the field has no value). */
export function classIndexes(layer: LayerInfo, series: LayerSeries): Uint8Array {
  const out = new Uint8Array(series.values.length).fill(NONE);
  const breaks = layer.breaks_scaled ?? [];
  for (let i = 0; i < series.values.length; i++) {
    const v = series.values[i];
    if (v === null || v === undefined) continue;
    out[i] = layer.kind === "number" ? classOf(v, breaks) : v;
  }
  return out;
}

/** A colour string per field, in the order of the fields. */
export function fieldColours(classes: Uint8Array, colours: readonly string[]): string[] {
  const out = new Array<string>(classes.length);
  for (let i = 0; i < classes.length; i++) out[i] = colours[classes[i] as number] ?? NO_VALUE_COLOUR;
  return out;
}

// ---------------------------------------------------------------------------------------------
// The fields in view, and the numbers for them.
// ---------------------------------------------------------------------------------------------

/** West, south, east, north of every field, four numbers each, in the order of the fields. */
export function boxesOf(
  features: readonly { geometry?: { coordinates?: number[][][] } | null }[],
): Float64Array {
  const out = new Float64Array(features.length * 4).fill(Number.NaN);
  features.forEach((feature, i) => {
    const ring = feature.geometry?.coordinates?.[0];
    if (!ring) return;
    let west = Infinity;
    let south = Infinity;
    let east = -Infinity;
    let north = -Infinity;
    for (const p of ring) {
      const lon = p[0];
      const lat = p[1];
      if (lon === undefined || lat === undefined) continue;
      if (lon < west) west = lon;
      if (lon > east) east = lon;
      if (lat < south) south = lat;
      if (lat > north) north = lat;
    }
    if (west <= east && south <= north) out.set([west, south, east, north], i * 4);
  });
  return out;
}

/** Indexes of the fields whose box touches the view (any part of the field is visible). */
export function visibleIndexes(boxes: Float64Array, west: number, south: number, east: number, north: number): Uint32Array {
  const out = new Uint32Array(boxes.length / 4);
  let n = 0;
  for (let i = 0; i < out.length; i++) {
    const at = i * 4;
    if ((boxes[at] as number) <= east && (boxes[at + 2] as number) >= west && (boxes[at + 1] as number) <= north && (boxes[at + 3] as number) >= south) {
      out[n++] = i;
    }
  }
  return out.slice(0, n);
}

/** Linear-interpolation quantile of a sorted array (numpy's default). */
export function quantile(sorted: ArrayLike<number>, q: number): number | null {
  const n = sorted.length;
  if (n === 0) return null;
  const at = (n - 1) * q;
  const low = Math.floor(at);
  const high = Math.ceil(at);
  const a = sorted[low] as number;
  const b = sorted[high] as number;
  return a + (b - a) * (at - low);
}

/** Decimal places of a layer's smallest step: 1 → 0, 0.001 → 3. */
export function decimalsOf(scale: number): number {
  return scale >= 1 ? 0 : Math.max(0, Math.round(-Math.log10(scale)));
}

/** A value in wire units as a number in the layer's unit, rounded to what the wire carries (halves up). */
export function realValue(scaled: number, scale: number): number {
  const d = decimalsOf(scale);
  const step = 10 ** d;
  return Math.round((scaled * scale + Number.EPSILON) * step) / step;
}

/** "46", "0.042", "1,234" */
export function formatValue(value: number, scale: number): string {
  const d = decimalsOf(scale);
  return value.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export interface ViewClass {
  index: number;
  fields: number;
  acres: number;
  /** fraction 0..1 of the fields with a value (number layers) or of all fields (class layers); null when none */
  share: number | null;
  /** county column of a class layer with a detail: the API's median of the detail in this class */
  detailMedian?: number | null;
}

export interface ViewStats {
  fields: number;
  /** acres of the fields with a value (number layers) or of all the fields (class layers) */
  acres: number;
  withValue: number;
  noValue: number;
  /** number layers only, in the layer's unit; null when no field in view has a value */
  median: number | null;
  p25: number | null;
  p75: number | null;
  classes: ViewClass[];
}

/**
 * The numbers for the fields at `which` (indexes into the fields) of one year of a layer.
 * `acres[i]` is the acreage of field i. With `which` null the whole county is counted.
 */
export function viewStats(
  layer: LayerInfo,
  series: LayerSeries,
  classes: Uint8Array,
  acres: ArrayLike<number>,
  which: ArrayLike<number> | null,
): ViewStats {
  const count = layer.classes?.length ?? 0;
  const noValueIndexes = new Set((layer.classes ?? []).filter((c) => c.no_value).map((c) => c.index));
  const fieldsPer = new Array<number>(count).fill(0);
  const acresPer = new Array<number>(count).fill(0);
  const got: number[] = [];
  let fields = 0;
  let totalAcres = 0;
  let valueAcres = 0;
  let noValue = 0;
  const n = which ? which.length : classes.length;
  for (let k = 0; k < n; k++) {
    const i = which ? (which[k] as number) : k;
    const a = acres[i] ?? 0;
    fields += 1;
    totalAcres += a;
    const c = classes[i] as number;
    if (c === NONE) {
      noValue += 1;
      continue;
    }
    if (noValueIndexes.has(c)) noValue += 1;
    else {
      valueAcres += a;
      if (layer.kind === "number") got.push(series.values[i] as number);
    }
    if (c < count) {
      fieldsPer[c] = (fieldsPer[c] as number) + 1;
      acresPer[c] = (acresPer[c] as number) + a;
    }
  }
  const withValue = fields - noValue;
  // Number layers: shares of the fields with a value. Class layers: of all fields (the unknown class is one).
  const base = layer.kind === "number" ? withValue : fields;
  let median: number | null = null;
  let p25: number | null = null;
  let p75: number | null = null;
  if (layer.kind === "number" && got.length > 0) {
    const sorted = Float64Array.from(got).sort();
    const shown = (q: number) => realValue(quantile(sorted, q) as number, layer.value_scale);
    median = shown(0.5);
    p25 = shown(0.25);
    p75 = shown(0.75);
  }
  return {
    fields,
    acres: Math.round(layer.kind === "number" ? valueAcres : totalAcres),
    withValue,
    noValue,
    median,
    p25,
    p75,
    classes: fieldsPer.map((f, index) => ({
      index,
      fields: f,
      acres: Math.round(acresPer[index] as number),
      share: base > 0 ? f / base : null,
    })),
  };
}

/** The county-wide numbers, from the API's own summary of that year (never recomputed in the browser). */
export function countyStats(layer: LayerInfo, series: LayerSeries): ViewStats {
  const s = series.summary;
  const number = layer.kind === "number";
  // Shares come from the API's counts, not from its shares rounded to three decimals: 251 of 7,255 is 3%,
  // but its rounded share 0.035 would show as 4%.
  const base = number ? s.fields_with_value : s.fields;
  return {
    fields: s.fields,
    acres: Math.round(number ? (s.acres_with_value ?? 0) : (s.acres ?? 0)),
    withValue: s.fields_with_value,
    noValue: s.fields_no_value,
    median: number && s.median !== null && s.median !== undefined ? s.median : null,
    p25: number && s.p25 !== null && s.p25 !== undefined ? s.p25 : null,
    p75: number && s.p75 !== null && s.p75 !== undefined ? s.p75 : null,
    classes: s.classes.map((c) => ({
      index: c.index,
      fields: c.fields,
      acres: c.acres,
      share: base > 0 ? c.fields / base : null,
      detailMedian: c.detail_median ?? null,
    })),
  };
}

/** 0.2196 → "22%"; "under 1%" for a share that is not zero but rounds to it. */
export function formatShare(share: number | null): string {
  if (share === null) return "–";
  if (share > 0 && share < 0.005) return "under 1%";
  return `${Math.round(share * 100)}%`;
}

/** 498797 → "498,797"; acres are whole numbers. */
export function formatAcreCount(acres: number): string {
  return Math.round(acres).toLocaleString("en-US");
}

/** The count of a class and its share: "1,103 · 15%". */
export function classCell(row: ViewClass | undefined): string {
  if (!row) return "–";
  return `${row.fields.toLocaleString("en-US")} · ${formatShare(row.share)}`;
}

/** Do the answer's field count and the fields on the map agree? */
export function sameFields(answer: Pick<LayerAnswer, "field_count" | "fields_etag">, count: number, etag: string | null): boolean {
  if (answer.field_count !== count) return false;
  return etag === null || !answer.fields_etag || answer.fields_etag === etag;
}

/** The legend's line under a class: the county median of the layer's second number in that class, or null. */
export function detailText(layer: Pick<LayerInfo, "detail">, row: ViewClass | undefined): string | null {
  const detail = layer.detail;
  const value = row?.detailMedian;
  if (!detail || value === null || value === undefined) return null;
  const shown = formatValue(value, detail.value_scale);
  if (detail.key === "score_gap") return `county median gap ${shown}`;
  if (detail.key === "irrigated_prob") return `county median chance ${shown}%`;
  if (detail.unit === "%") return `county median ${shown}%`;
  return `county median ${shown}`;
}
