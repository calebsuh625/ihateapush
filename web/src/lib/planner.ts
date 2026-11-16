// Pure helpers for the planner controls and plan cards: the query the app sends, the "which future"
// slider, the profit spread strip, the holds-up badge, the climate line and the words on the cards.
// No imports, so the unit tests can load this file directly.

export type WeightKey = "soil" | "water" | "money" | "risk";
export type ScenarioId = "ssp245" | "ssp585";

export interface PlannerChoices {
  /** 0..10 each, as set on the sliders */
  weights: Record<WeightKey, number>;
  /** 2025 (today) .. 2060 */
  horizon: number;
  scenario: ScenarioId;
  /**
   * "What if I can only pump N inches a year?": a lower pumping limit to score irrigated plans under.
   * Missing or null = the planner's own limit (nothing is sent).
   */
  limitIn?: number | null;
  /**
   * "What if prices or costs change?": percent change on every crop price and on every direct cost,
   * -50..+100. Missing or 0 = today's numbers (nothing is sent). We never predict prices; the farmer picks.
   */
  pricePct?: number;
  costPct?: number;
}

export const PRICE_COST_MIN = -50;
export const PRICE_COST_MAX = 100;

const pctValue = (n: number | undefined): number =>
  typeof n === "number" && Number.isFinite(n) ? Math.min(PRICE_COST_MAX, Math.max(PRICE_COST_MIN, Math.round(n))) : 0;

export const isDefaultPriceCost = (choices: Pick<PlannerChoices, "pricePct" | "costPct">): boolean =>
  pctValue(choices.pricePct) === 0 && pctValue(choices.costPct) === 0;

/** 20 → "+20%", -30 → "−30%" (a real minus sign), 0 → "0%" */
export const signedPercent = (n: number): string => {
  const v = Math.round(n);
  return v === 0 ? "0%" : v < 0 ? `\u2212${Math.abs(v)}%` : `+${v}%`;
};

export const WEIGHT_KEYS: readonly WeightKey[] = ["soil", "water", "money", "risk"];
export const WEIGHT_LABELS: Record<WeightKey, string> = {
  soil: "Soil protection",
  water: "Water",
  money: "Money",
  risk: "Risk",
};
export const WEIGHT_MIN = 0;
export const WEIGHT_MAX = 10;
/** The API's own defaults (0.30 / 0.20 / 0.30 / 0.20) on the 0-10 scale. */
export const DEFAULT_WEIGHTS: Record<WeightKey, number> = { soil: 3, water: 2, money: 3, risk: 2 };

export const TODAY_HORIZON = 2025;
/** Slider stops: Today, 2030, 2035 ... 2060. */
export const HORIZONS: readonly number[] = [2025, 2030, 2035, 2040, 2045, 2050, 2055, 2060];
export const SCENARIOS: readonly { id: ScenarioId; label: string }[] = [
  { id: "ssp245", label: "Middle road" },
  { id: "ssp585", label: "High emissions" },
];

export const DEFAULT_CHOICES: PlannerChoices = {
  weights: DEFAULT_WEIGHTS,
  horizon: TODAY_HORIZON,
  scenario: "ssp245",
};

/** How long to wait after the last slider movement before asking the API. */
export const DEBOUNCE_MS = 150;

const clampInt = (n: number, low: number, high: number) =>
  Number.isFinite(n) ? Math.min(high, Math.max(low, Math.round(n))) : low;

/** Slider position (0..7) → year. Out-of-range positions snap to the nearest end. */
export const horizonAt = (index: number): number => HORIZONS[clampInt(index, 0, HORIZONS.length - 1)] ?? TODAY_HORIZON;

/** Year → slider position: the closest stop (2025 → 0, 2060 → 7). */
export function horizonIndex(horizon: number): number {
  if (!Number.isFinite(horizon)) return 0;
  let best = 0;
  let bestGap = Number.POSITIVE_INFINITY;
  HORIZONS.forEach((year, i) => {
    const gap = Math.abs(year - horizon);
    if (gap < bestGap) {
      best = i;
      bestGap = gap;
    }
  });
  return best;
}

/** "Today" for 2025, otherwise the year. */
export const horizonLabel = (horizon: number): string => (horizon <= TODAY_HORIZON ? "Today" : String(horizon));

export const scenarioLabel = (scenario: string): string =>
  SCENARIOS.find((s) => s.id === scenario)?.label ?? scenario;

export const isDefaultWeights = (weights: Record<WeightKey, number>): boolean =>
  WEIGHT_KEYS.every((key) => weights[key] === DEFAULT_WEIGHTS[key]);

/**
 * The query string for GET /field/{id}/plans, without the "?". Always in the same order, so two
 * equal sets of choices give the same string (the app uses it as the request's identity).
 * `irrigated` is only sent when the user overrode our guess, and `limit_in` only while the pumping
 * what-if is on.
 */
export function plansQuery(choices: PlannerChoices, irrigated: boolean | null = null): string {
  const parts = WEIGHT_KEYS.map((key) => `${key}=${clampInt(choices.weights[key], WEIGHT_MIN, WEIGHT_MAX)}`);
  parts.push(`scenario=${choices.scenario === "ssp585" ? "ssp585" : "ssp245"}`);
  parts.push(`horizon=${horizonAt(horizonIndex(choices.horizon))}`);
  if (irrigated !== null) parts.push(`irrigated=${irrigated ? "true" : "false"}`);
  if (typeof choices.limitIn === "number" && Number.isFinite(choices.limitIn)) parts.push(`limit_in=${choices.limitIn}`);
  const price = pctValue(choices.pricePct);
  const cost = pctValue(choices.costPct);
  if (price !== 0) parts.push(`price_pct=${price}`);
  if (cost !== 0) parts.push(`cost_pct=${cost}`);
  return parts.join("&");
}

/** The break-even block of the plans answer (`sensitivity`). */
export interface Sensitivity {
  plan: number;
  plan_name: string;
  /** "today": percentages are against today's prices; "your_prices": against the prices as set */
  basis: "today" | "your_prices";
  status: "ok" | "stays_above_zero" | "below_zero_now" | "no_revenue";
  return_now: number;
  break_even_pct: number | null;
  low_pct: number | null;
  high_pct: number | null;
  range_shown: [number, number];
  sentence: string;
  note: string;
}

/** The one line under the price and cost sliders: the break-even of the top plan, in the API's own words. */
export function breakEvenLine(sensitivity: Sensitivity | null | undefined): string | null {
  return sensitivity && typeof sensitivity.sentence === "string" && sensitivity.sentence ? sensitivity.sentence : null;
}

// ---------------------------------------------------------------------------------------------
// Holds-up badge
// ---------------------------------------------------------------------------------------------

export type HoldsTone = "good" | "mixed" | "poor";

/** Tone by the share of futures the plan holds up in: 5 of 6 or better is good, under half is poor. */
export function holdsTone(holds: number, total: number): HoldsTone {
  if (!(total > 0) || !Number.isFinite(holds)) return "poor";
  const share = holds / total;
  if (share >= 0.8) return "good";
  if (share >= 0.5) return "mixed";
  return "poor";
}

export const holdsText = (holds: number, total: number): string => `Holds up in ${holds} of ${total} futures`;

// ---------------------------------------------------------------------------------------------
// Spread strip (low - middle - high across the futures)
// ---------------------------------------------------------------------------------------------

export interface SpreadLike {
  low: number;
  mid: number;
  high: number;
}

export interface SpreadGeometry {
  /** left edge and width of the low-high bar, in percent of the strip */
  left: number;
  width: number;
  /** position of the middle marker, percent */
  mid: number;
  /** position of the zero line, percent; null when zero is outside the scale */
  zero: number | null;
}

/**
 * One scale for a set of spreads, so the strips on the cards can be compared by eye. Always
 * includes zero (break-even), and never has zero width.
 */
export function spreadDomain(spreads: readonly (SpreadLike | null | undefined)[]): [number, number] {
  let low = 0;
  let high = 0;
  for (const spread of spreads) {
    if (!spread) continue;
    for (const value of [spread.low, spread.mid, spread.high]) {
      if (!Number.isFinite(value)) continue;
      low = Math.min(low, value);
      high = Math.max(high, value);
    }
  }
  if (high - low <= 0) return [0, 1];
  return [low, high];
}

/** Where to draw one spread on a strip that covers `domain`. All numbers are 0..100. */
export function spreadGeometry(spread: SpreadLike, domain: readonly [number, number], minWidth = 2): SpreadGeometry {
  const [d0, d1] = domain;
  const span = d1 - d0;
  const at = (value: number) => (span > 0 && Number.isFinite(value) ? Math.min(100, Math.max(0, ((value - d0) / span) * 100)) : 0);
  const a = at(Math.min(spread.low, spread.high));
  const b = at(Math.max(spread.low, spread.high));
  let left = a;
  let width = b - a;
  if (width < minWidth) {
    // too narrow to see: widen around its centre, staying inside the strip
    left = Math.min(100 - minWidth, Math.max(0, (a + b) / 2 - minWidth / 2));
    width = minWidth;
  }
  const mid = Math.min(left + width, Math.max(left, at(spread.mid)));
  const zero = span > 0 && d0 <= 0 && d1 >= 0 ? at(0) : null;
  return { left, width, mid, zero };
}

// ---------------------------------------------------------------------------------------------
// Words and numbers
// ---------------------------------------------------------------------------------------------

/** 48 → "$48", -24.4 → "−$24" (a real minus sign), -0.2 → "$0". */
export function formatDollars(value: number): string {
  if (!Number.isFinite(value)) return "no figure";
  const rounded = Math.round(value);
  const digits = Math.abs(rounded).toLocaleString("en-US");
  return rounded < 0 ? `−$${digits}` : `$${digits}`;
}

/** 0.363 → "36%" */
export const formatShare = (share: number): string =>
  Number.isFinite(share) ? `${Math.round(Math.min(1, Math.max(0, share)) * 100)}%` : "no figure";

const rainEnd = (pct: number): string => {
  const rounded = Math.round(pct);
  if (rounded === 0) return "no change";
  return `${Math.abs(rounded)}% ${rounded < 0 ? "less" : "more"}`;
};

/** -42 → "−42%", 3 → "+3%", 0 → "no change" (whole percent, a real minus sign). */
export function formatChange(pct: number): string {
  if (!Number.isFinite(pct)) return "no figure";
  const rounded = Math.round(pct);
  if (rounded === 0) return "no change";
  return `${rounded < 0 ? "−" : "+"}${Math.abs(rounded)}%`;
}

/** "2050–2060" */
export const windowText = (window: readonly [number, number]): string =>
  window[0] === window[1] ? String(window[0]) : `${window[0]}–${window[1]}`;

interface ClimateLike {
  label: string;
  changes?: { models?: number };
  range?: { summer_rain_pct?: readonly [number, number]; tmean_c?: readonly [number, number] };
  window?: readonly [number, number];
  models?: number;
  yield_change?: Record<string, { pct: number; low: number; high: number }> | null;
}

/**
 * The line under "Which future?": the API's own label, word for word, then what it is measured
 * against, the years averaged and how many models. Nothing about rain is added here: the label
 * already says whether the models agree.
 */
export function climateLine(climate: ClimateLike, horizon: number): string {
  const models = climate.models ?? climate.changes?.models ?? 0;
  const modelText = models > 0 ? `${models} climate ${models === 1 ? "model" : "models"}` : "climate models";
  if (horizon <= TODAY_HORIZON) return `${climate.label}, from ${modelText}.`;
  const years = climate.window ? `average of ${windowText(climate.window)} across ${modelText}` : `average across ${modelText}`;
  return `${climate.label}. Compared with today; ${years}.`;
}

/**
 * The lowest and highest model, for the small print under the climate line, e.g.
 * "Range across the models: +1.3 to +3.3°C; summer rain from 32% less to 13% more."
 * Null at Today (every change is zero) and when the numbers are missing.
 */
export function climateRange(climate: ClimateLike, horizon: number): string | null {
  if (horizon <= TODAY_HORIZON) return null;
  const parts: string[] = [];
  const warm = climate.range?.tmean_c;
  const degrees = (n: number) => `${n < 0 ? "−" : "+"}${Math.abs(n).toFixed(1)}`;
  if (warm && Number.isFinite(warm[0]) && Number.isFinite(warm[1])) parts.push(`${degrees(warm[0])} to ${degrees(warm[1])}°C`);
  const rain = climate.range?.summer_rain_pct;
  if (rain && Number.isFinite(rain[0]) && Number.isFinite(rain[1])) {
    const [low, high] = [rainEnd(rain[0]), rainEnd(rain[1])];
    parts.push(low === high ? `summer rain ${low}` : `summer rain from ${low} to ${high}`);
  }
  return parts.length > 0 ? `Range across the models: ${parts.join("; ")}.` : null;
}

/**
 * "Dryland yields in this future: corn −42%, sorghum −26%, winter wheat −17%", hardest hit first.
 * Null at Today (nothing has changed yet) and when the answer has no yield changes.
 */
export function yieldChangeLine(
  yieldChange: Record<string, { pct: number; low: number; high: number }> | null | undefined,
  irrigated: boolean,
  horizon: number,
): string | null {
  if (!yieldChange || horizon <= TODAY_HORIZON) return null;
  const crops = Object.entries(yieldChange)
    .filter(([, change]) => change && Number.isFinite(change.pct))
    .sort((a, b) => a[1].pct - b[1].pct || a[0].localeCompare(b[0]));
  if (crops.length === 0) return null;
  const list = crops.map(([crop, change]) => `${crop} ${formatChange(change.pct)}`).join(", ");
  return `${irrigated ? "Irrigated" : "Dryland"} yields in this future: ${list}`;
}

/** The low-high range behind each yield change, for a title: "corn −56% to −25%; sorghum ...". */
export function yieldChangeRange(
  yieldChange: Record<string, { pct: number; low: number; high: number }> | null | undefined,
): string | null {
  if (!yieldChange) return null;
  const crops = Object.entries(yieldChange)
    .filter(([, change]) => change && Number.isFinite(change.low) && Number.isFinite(change.high))
    .sort((a, b) => a[1].pct - b[1].pct || a[0].localeCompare(b[0]));
  if (crops.length === 0) return null;
  return `Lowest to highest model: ${crops.map(([crop, c]) => `${crop} ${formatChange(c.low)} to ${formatChange(c.high)}`).join("; ")}.`;
}

// ---------------------------------------------------------------------------------------------
// Cards: place in the full list, the ranking score, water in inches
// ---------------------------------------------------------------------------------------------

/** What the big number on a smart card is, in a few words and in full. */
export const RANKING_SCORE_LABEL = "Ranking score";
export const RANKING_SCORE_HINT = "leans on the worse futures";
export const RANKING_SCORE_TITLE =
  "The score the plans are sorted on: half the average across the climate models, half the average in the worst third of them. The four bars below are plain averages.";

/**
 * Where a card sits among every pattern compared. Cards 2 and 3 are not places 2 and 3: each is
 * the best plan that differs enough from the cards above it.
 * placeText(7, 115, 2) → "Number 7 of 115, the next different choice". Null for the first card
 * and when the answer has no rank.
 */
export function placeText(rank: number | null | undefined, candidates: number | null | undefined, card: number): string | null {
  if (card <= 1 || typeof rank !== "number" || !Number.isFinite(rank)) return null;
  const of = typeof candidates === "number" && candidates > 0 ? ` of ${candidates}` : "";
  return `Number ${rank}${of}, the next different choice`;
}

const MM_PER_INCH = 25.4;

/** 24 → "24", 18.26 → "18.3" */
const inchNumber = (inches: number): string => {
  const rounded = Math.round(inches * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
};

/** Water depth in inches, from the API's inches when it sends them, else from mm. 465 mm → "18.3 in". */
export function formatInches(inches: number | null | undefined, mm?: number | null): string {
  const value = typeof inches === "number" && Number.isFinite(inches) ? inches : typeof mm === "number" && Number.isFinite(mm) ? mm / MM_PER_INCH : null;
  return value === null ? "no figure" : `${inchNumber(value)} in`;
}

/** "465 mm", for the title next to a figure in inches. */
export const formatMm = (mm: number): string => (Number.isFinite(mm) ? `${Math.round(mm)} mm` : "no figure");

/**
 * What the holds-up count takes for granted on an irrigated field: the well keeps delivering.
 * 24 → "if the well delivers up to 24 inches a year"; no limit → "if the well delivers all the crops need".
 */
export function wellQualifier(limitIn: number | null | undefined): string {
  return typeof limitIn === "number" && Number.isFinite(limitIn) && limitIn > 0
    ? `if the well delivers up to ${inchNumber(limitIn)} inches a year`
    : "if the well delivers all the crops need";
}

/** Title for the water bar on a dryland card: the three scores that follow it move together. */
export const DRYLAND_WATER_TITLE = "how much of the crops' need rain covers";

/** Why the field is treated as irrigated or dryland, in a few words. */
export function irrigationReason(source: string, basis: string | null | undefined): string {
  if (source !== "guess") return "you set this";
  return basis ? `our guess: ${basis}` : "our guess from the crop history";
}
