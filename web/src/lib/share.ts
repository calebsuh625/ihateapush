// Share link: the app's state written into the URL hash, and read back from it.
//
//   #KS-014376                                     the old form: just the open field (still works)
//   #KS-014376?w=1.2.5.2&h=2040&s=ssp585&irr=0     the field, plus every setting that is not the default
//   #?m=farm&farm=KS-1,KS-2&fw=900&fm=20000        a farm plan (no field open)
//
// Only what differs from the defaults is written, so a plain open field stays `#KS-014376`. Unknown or
// unreadable parts are ignored, never an error. No imports (the unit tests load this file directly); the
// defaults below are copies of lib/planner.ts and lib/custom.ts, and a unit test keeps them equal.

export type ShareWeights = { soil: number; water: number; money: number; risk: number };

export interface ShareChoices {
  weights: ShareWeights;
  horizon: number;
  scenario: "ssp245" | "ssp585";
  limitIn: number | null;
  pricePct: number;
  costPct: number;
}

/** Everything a link carries. `irrigation` and `own` belong to the open field. */
export interface ShareState {
  field: string | null;
  choices: ShareChoices;
  /** the farmer's irrigated / dryland switch for the open field; null = our guess */
  irrigation: boolean | null;
  /** the farmer's own rotation for the open field, one crop or null (empty year) each */
  own: (string | null)[] | null;
  farmMode: boolean;
  /** the farm's fields; null = not in the link (keep the ones this browser has) */
  farmIds: string[] | null;
  farmWater: string;
  farmMinimum: string;
  /** the map layer and its year; null = the plain map */
  layer: string | null;
  year: number | null;
}

export const SHARE_DEFAULT_WEIGHTS: ShareWeights = { soil: 3, water: 2, money: 3, risk: 2 };
export const SHARE_HORIZONS: readonly number[] = [2025, 2030, 2035, 2040, 2045, 2050, 2055, 2060];
export const SHARE_CROPS: readonly string[] = ["corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow"];
const WEIGHT_ORDER = ["soil", "water", "money", "risk"] as const;
const PCT_MIN = -50;
const PCT_MAX = 100;
const MAX_FARM_FIELDS = 400;
const MAX_ID = 40;

export const defaultShareChoices = (): ShareChoices => ({
  weights: { ...SHARE_DEFAULT_WEIGHTS },
  horizon: SHARE_HORIZONS[0] ?? 2025,
  scenario: "ssp245",
  limitIn: null,
  pricePct: 0,
  costPct: 0,
});

export const emptyShare = (): ShareState => ({
  field: null,
  choices: defaultShareChoices(),
  irrigation: null,
  own: null,
  farmMode: false,
  farmIds: null,
  farmWater: "",
  farmMinimum: "",
  layer: null,
  year: null,
});

/** The hash split into the field part and the settings part. `raw` may start with "#". */
export function splitHash(raw: string): { field: string | null; query: string | null } {
  const text = raw.replace(/^#/, "");
  const cut = text.indexOf("?");
  const head = cut < 0 ? text : text.slice(0, cut);
  const query = cut < 0 ? null : text.slice(cut + 1);
  let field: string | null;
  try {
    field = decodeURIComponent(head).trim() || null;
  } catch {
    field = head.trim() || null; // malformed escape sequence: use it as typed
  }
  return { field, query: query === "" ? null : query };
}

const clampInt = (n: number, low: number, high: number) => Math.min(high, Math.max(low, Math.round(n)));
const enc = (value: string) => encodeURIComponent(value).replace(/%2C/gi, ",").replace(/%20/g, "+");
const sameWeights = (a: ShareWeights, b: ShareWeights) => WEIGHT_ORDER.every((key) => a[key] === b[key]);

/** The hash for a state, without the "#". Empty when nothing is open and everything is default. */
export function encodeShare(state: ShareState): string {
  const parts: string[] = [];
  const choices = state.choices;
  const weights = WEIGHT_ORDER.map((key) => clampInt(choices.weights[key], 0, 10));
  const asWeights: ShareWeights = { soil: weights[0] ?? 0, water: weights[1] ?? 0, money: weights[2] ?? 0, risk: weights[3] ?? 0 };
  if (!sameWeights(asWeights, SHARE_DEFAULT_WEIGHTS)) parts.push(`w=${weights.join(".")}`);
  const horizon = SHARE_HORIZONS.includes(choices.horizon) ? choices.horizon : SHARE_HORIZONS[0];
  if (horizon !== SHARE_HORIZONS[0]) parts.push(`h=${horizon}`);
  if (choices.scenario === "ssp585") parts.push("s=ssp585");
  if (typeof choices.limitIn === "number" && Number.isFinite(choices.limitIn) && choices.limitIn > 0) parts.push(`lim=${choices.limitIn}`);
  if (Number.isFinite(choices.pricePct) && Math.round(choices.pricePct) !== 0) parts.push(`pp=${clampInt(choices.pricePct, PCT_MIN, PCT_MAX)}`);
  if (Number.isFinite(choices.costPct) && Math.round(choices.costPct) !== 0) parts.push(`cp=${clampInt(choices.costPct, PCT_MIN, PCT_MAX)}`);
  if (state.field !== null) {
    if (state.irrigation !== null) parts.push(`irr=${state.irrigation ? 1 : 0}`);
    if (state.own && state.own.some((slot) => slot !== null)) parts.push(`own=${state.own.map((slot) => (slot === null ? "_" : enc(slot))).join(",")}`);
  }
  if (state.farmMode) {
    parts.push("m=farm");
    if (state.farmIds && state.farmIds.length > 0) parts.push(`farm=${state.farmIds.slice(0, MAX_FARM_FIELDS).map(enc).join(",")}`);
    if (state.farmWater.trim() !== "") parts.push(`fw=${enc(state.farmWater.trim())}`);
    if (state.farmMinimum.trim() !== "") parts.push(`fm=${enc(state.farmMinimum.trim())}`);
  }
  if (state.layer !== null) {
    parts.push(`layer=${enc(state.layer)}`);
    if (state.year !== null && Number.isFinite(state.year)) parts.push(`yr=${Math.round(state.year)}`);
  }
  const head = state.field === null ? "" : encodeURIComponent(state.field);
  return parts.length === 0 ? head : `${head}?${parts.join("&")}`;
}

const looksLikeAmount = (text: string) => /^[0-9][0-9,]*(\.[0-9]+)?$|^\.[0-9]+$/.test(text) && text.length <= 20;

/**
 * Read a hash ("#..." or not). `hasParams` is true when the settings part exists: a link made by
 * "Copy link". A bare `#KS-014376` has none and only opens the field, as it always did.
 */
export function decodeShare(raw: string): { state: ShareState; hasParams: boolean } {
  const { field, query } = splitHash(raw);
  const state = emptyShare();
  state.field = field;
  if (query === null) return { state, hasParams: false };
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(query);
  } catch {
    return { state, hasParams: false };
  }
  const weights = params.get("w")?.split(".").map((part) => Number(part));
  if (weights && weights.length === 4 && weights.every((n) => Number.isFinite(n))) {
    const w = weights.map((n) => clampInt(n, 0, 10));
    state.choices.weights = { soil: w[0] ?? 3, water: w[1] ?? 2, money: w[2] ?? 3, risk: w[3] ?? 2 };
  }
  const horizon = Number(params.get("h"));
  if (SHARE_HORIZONS.includes(horizon)) state.choices.horizon = horizon;
  if (params.get("s") === "ssp585") state.choices.scenario = "ssp585";
  const limit = params.get("lim");
  if (limit !== null && limit.trim() !== "" && Number.isFinite(Number(limit)) && Number(limit) > 0 && Number(limit) <= 1000) state.choices.limitIn = Number(limit);
  for (const [key, name] of [["pp", "pricePct"], ["cp", "costPct"]] as const) {
    const text = params.get(key);
    if (text !== null && text.trim() !== "" && Number.isFinite(Number(text))) state.choices[name] = clampInt(Number(text), PCT_MIN, PCT_MAX);
  }
  if (field !== null) {
    const irrigation = params.get("irr");
    if (irrigation === "1") state.irrigation = true;
    else if (irrigation === "0") state.irrigation = false;
    const own = params.get("own");
    if (own !== null) {
      const slots = own.split(",").map((part) => (part === "_" ? null : part.trim().toLowerCase()));
      const valid = slots.length >= 2 && slots.length <= 6 && slots.every((slot) => slot === null || SHARE_CROPS.includes(slot));
      if (valid && slots.some((slot) => slot !== null)) state.own = slots;
    }
  }
  if (params.get("m") === "farm") {
    state.farmMode = true;
    const farm = params.get("farm");
    if (farm !== null) {
      const ids = [...new Set(farm.split(",").map((id) => id.trim()).filter((id) => id !== "" && id.length <= MAX_ID))].slice(0, MAX_FARM_FIELDS);
      if (ids.length > 0) state.farmIds = ids;
    }
    const water = params.get("fw")?.trim() ?? "";
    const minimum = params.get("fm")?.trim() ?? "";
    if (looksLikeAmount(water)) state.farmWater = water;
    if (looksLikeAmount(minimum)) state.farmMinimum = minimum;
  }
  const layer = params.get("layer")?.trim() ?? "";
  if (/^[A-Za-z0-9_.-]{1,60}$/.test(layer)) {
    state.layer = layer;
    const year = Number(params.get("yr"));
    if (Number.isInteger(year) && year >= 1900 && year <= 2100) state.year = year;
  }
  return { state, hasParams: true };
}
