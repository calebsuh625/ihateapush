// Pure helpers for the planner controls and plan cards: the query the app sends, the "which future"
// slider, the profit spread strip, the holds-up badge and the climate line.
// No imports, so the unit tests can load this file directly.

export type WeightKey = "soil" | "water" | "money" | "risk";
export type ScenarioId = "ssp245" | "ssp585";

export interface PlannerChoices {
  /** 0..10 each, as set on the sliders */
  weights: Record<WeightKey, number>;
  /** 2025 (today) .. 2060 */
  horizon: number;
  scenario: ScenarioId;
}

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
 * `irrigated` is only sent when the user overrode our guess.
 */
export function plansQuery(choices: PlannerChoices, irrigated: boolean | null = null): string {
  const parts = WEIGHT_KEYS.map((key) => `${key}=${clampInt(choices.weights[key], WEIGHT_MIN, WEIGHT_MAX)}`);
  parts.push(`scenario=${choices.scenario === "ssp585" ? "ssp585" : "ssp245"}`);
  parts.push(`horizon=${horizonAt(horizonIndex(choices.horizon))}`);
  if (irrigated !== null) parts.push(`irrigated=${irrigated ? "true" : "false"}`);
  return parts.join("&");
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

/** The API calls a rain change agreed when at least this share of models point the same way. */
const RAIN_AGREE_SHARE = 0.8;

/**
 * A few words for when the climate models do not agree on summer rain, with their range, e.g.
 * "the models do not agree on summer rain (from 33% less to 10% more)". Null when they agree,
 * when the range does not cross zero, or when the numbers are missing (as at Today).
 */
export function rainDisagreement(
  changes: { summer_rain_agree?: number; models?: number } | null | undefined,
  range: readonly [number, number] | null | undefined,
): string | null {
  if (!changes || !range) return null;
  const [low, high] = range;
  if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
  // some drier, some wetter, by a visible amount on at least one side
  if (!(low < 0 && high > 0) || (Math.round(low) === 0 && Math.round(high) === 0)) return null;
  const models = changes.models ?? 0;
  const agree = changes.summer_rain_agree ?? 0;
  if (models > 0 && agree / models >= RAIN_AGREE_SHARE) return null;
  return `the models do not agree on summer rain (from ${rainEnd(low)} to ${rainEnd(high)})`;
}

/** "2050–2060" */
export const windowText = (window: readonly [number, number]): string =>
  window[0] === window[1] ? String(window[0]) : `${window[0]}–${window[1]}`;

/**
 * The line under "Which future?": what changes, the years averaged, how many models, and the rain
 * caveat. At Today it reads as today's climate.
 */
export function climateLine(
  climate: {
    label: string;
    changes?: { summer_rain_agree?: number; models?: number };
    range?: { summer_rain_pct?: readonly [number, number] };
    window?: readonly [number, number];
    models?: number;
  },
  horizon: number,
): string {
  const models = climate.models ?? climate.changes?.models ?? 0;
  const modelText = models > 0 ? `${models} climate ${models === 1 ? "model" : "models"}` : "climate models";
  const years = climate.window ? `average of ${windowText(climate.window)}` : "";
  const basis = [years, modelText].filter(Boolean).join(", ");
  if (horizon <= TODAY_HORIZON) return `Today's climate (${basis}).`;
  const disagreement = rainDisagreement(climate.changes, climate.range?.summer_rain_pct);
  const head = `${climate.label} than today (${basis}).`;
  return disagreement ? `${head} ${disagreement.charAt(0).toUpperCase()}${disagreement.slice(1)}.` : head;
}

/** Why the field is treated as irrigated or dryland, in a few words. */
export function irrigationReason(source: string, basis: string | null | undefined): string {
  if (source !== "guess") return "you set this";
  return basis ? `our guess: ${basis}` : "our guess from the crop history";
}
