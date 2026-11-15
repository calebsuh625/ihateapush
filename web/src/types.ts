// Shapes returned by the HTTP API. Keep in step with the API contract (see schema.md and api/main.py).

/** [minLon, minLat, maxLon, maxLat] */
export type Bbox = [number, number, number, number];

export interface PolygonGeometry {
  type: "Polygon";
  /** rings → positions → [lon, lat] */
  coordinates: number[][][];
}

export interface FieldFeature {
  type: "Feature";
  id?: string;
  properties: { field_id: string; area_ha: number };
  geometry: PolygonGeometry;
}

/** GET /fields */
export interface FieldCollection {
  type: "FeatureCollection";
  bbox?: Bbox;
  features: FieldFeature[];
}

/** GET /health */
export interface Health {
  status: string;
  source: string;
  is_fake: boolean;
  field_count: number;
  /** true when the optional satellite side file is loaded */
  satellite?: boolean;
  /** true when the optional soil-health side file is loaded */
  soil_health?: boolean;
}

export interface Streak {
  crop: string;
  years: number;
  start_year: number;
  end_year: number;
}

export interface FieldStats {
  years_covered: number;
  /** ordered by frequency desc, then name */
  distinct_crops: string[];
  distinct_crop_count: number;
  longest_streak: Streak | null;
  /** 0–100 */
  pct_years_corn: number;
}

/** GET /field/{field_id} */
export interface FieldDetail {
  field_id: string;
  geometry: PolygonGeometry;
  /** year ("2008") → lowercase crop name; years may be missing */
  crop_history: Record<string, string>;
  organic_matter_pct: number | null;
  slope_pct: number | null;
  area_ha: number;
  stats: FieldStats;
}

export interface PlanScores {
  soil: number;
  water: number;
  money: number;
  overall: number;
  /** smart planner only */
  risk?: number;
  /** smart planner only: the number plans are ranked on */
  robust?: number;
}

/** Lowest, middle and highest value across the climate models. */
export interface Spread {
  low: number;
  mid: number;
  high: number;
}

export interface PlanNumbers {
  bare_days: number;
  erosion_index?: number;
  /** water pumped a year, mm (0 for dryland) */
  irrigation_mm: number;
  /** the same in inches, 1 decimal; missing on older servers */
  irrigation_in?: number;
  /** share 0..1 of the crops' water need that is met; can be below 1 when irrigated, if the limit leaves crops short */
  water_met_share: number;
  /** dollars per acre per year */
  profit_per_acre: number;
  /** share 0..1 of planted crops that fail */
  failed_share: number;
}

export interface Plan {
  name: string;
  /** one crop per year, lowercase */
  rotation: string[];
  /** each 0..1, higher is better */
  scores: PlanScores;
  reason: string;
  // The rest comes from the smart planner only.
  /** the repeating unit: the same crops as `rotation`, or three for a 3-year cycle */
  cycle?: string[];
  /** in how many climate models the plan holds up */
  futures?: { holds: number; total: number };
  spread?: { overall?: Spread; profit_per_acre?: Spread };
  numbers?: PlanNumbers;
  /** place among all the patterns compared (`candidates`), sorted on scores.robust */
  rank?: number | null;
}

/** The field's own last four years, scored like a plan (smart planner). */
export interface CurrentPattern extends Plan {
  rules_broken?: string[];
  /** [first, last] year of the pattern */
  years?: [number, number];
  is_candidate?: boolean;
}

export interface YieldChange {
  pct: number;
  low: number;
  high: number;
}

export type Scenario = "ssp245" | "ssp585";

export interface PlannerSettings {
  /** normalised to add up to 1 */
  weights: { soil: number; water: number; money: number; risk: number };
  scenario: Scenario;
  horizon: number;
  /** [first, last] year averaged */
  window: [number, number];
  irrigated: boolean;
  /** "guess" or "override" */
  irrigation_source: string;
  irrigation_basis?: string | null;
  soil_note?: string;
  holds_up_rule?: string;
  ranking_rule?: string;
  profit_basis?: string;
  /** the most water a well is assumed to deliver per acre a year; null when there is no limit */
  irrigation_limit_mm?: number | null;
  irrigation_limit_in?: number | null;
  /** what the yield changes in `climate.yield_change` do and do not include */
  yield_change_note?: string;
  /** plain sentences on what the scores leave out */
  limits?: string[];
  models?: string[];
}

export interface ClimateSummary {
  label: string;
  changes: { summer_rain_agree?: number; models?: number; [key: string]: number | undefined };
  range?: { summer_rain_pct?: [number, number]; tmean_c?: [number, number]; [key: string]: [number, number] | undefined };
  window?: [number, number];
  models?: number;
  /** yield against today for the field's practice, whole percent, with the lowest and highest model */
  yield_change?: Record<string, YieldChange>;
}

/** The farmer's own rotation, scored like a plan (GET /field/{field_id}/score). */
export interface OwnRotation extends Plan {
  /** the years as typed, 2 to 6 crops */
  cycle: string[];
  /** "Our plans avoid ..." sentences; empty when it breaks none of our rules */
  rules_broken: string[];
  /** true when it is one of the rotations the planner compares */
  is_candidate: boolean;
  /** 1, 2 or 3 when it is that pick of ours; null otherwise */
  same_as_pick: number | null;
  /** false when no year plants a crop: then there is no rank, no comparison and no mark on yours */
  rankable?: boolean;
  /** null when it is not rankable */
  rank: number | null;
  /** what `rank` is out of: the candidates, plus one when yours is not among them; null when not rankable */
  rank_of: number | null;
  /** the cycle the numbers were worked out for (shortest unit, fixed starting year) */
  scored_as: string[];
}

export type ComparisonVerdict = "ours" | "yours" | "too_close" | "same";
/** "not_compared": yours plants no crop, so it is not set against our first pick. */
export type OverallVerdict = ComparisonVerdict | "not_compared";

/** In how many climate models each side is ahead. */
export interface ModelCount {
  yours_ahead: number;
  ours_ahead: number;
  total: number;
}

export interface ComparisonRow {
  key: "soil" | "water" | "money" | "risk" | "holds";
  label: string;
  unit: string;
  yours: number;
  ours: number;
  /** yours minus ours */
  diff: number;
  verdict: ComparisonVerdict;
  /** null for a figure that is one number, not one per climate model */
  models: ModelCount | null;
  sentence: string;
  score_yours?: number;
  score_ours?: number;
  score_diff?: number;
  /** money only: [lowest, highest] climate model */
  yours_range?: [number, number];
  ours_range?: [number, number];
}

export interface PriorityPlace {
  priority: "soil" | "water" | "money" | "risk";
  rank: number;
  of: number;
  first: boolean;
  /** how many of our candidates score exactly the same */
  tied: number;
}

export interface ScoreComparison {
  /** the name of our first pick, which every row is measured against */
  against: string;
  verdict: OverallVerdict;
  /** null when yours is not compared */
  models: ModelCount | null;
  headline: string[];
  rows: ComparisonRow[];
  priorities: PriorityPlace[];
  priority_sentences: string[];
  notes: string[];
}

/** GET /field/{field_id}/score?rotation=... (smart planner only) */
export interface FieldScore {
  field_id: string;
  engine: "smart";
  /** the crops the API read from the text, one per year */
  understood: string[];
  settings: PlannerSettings;
  yours: OwnRotation;
  /** our top picks for the same settings, as in GET /field/{id}/plans */
  plans: Plan[];
  candidates: number;
  /** table row -> columns to mark (0 = yours, 1.. = our picks) */
  marks: Record<string, number[]>;
  marks_note: string;
  comparison: ScoreComparison;
}

/** A figure across the six climate models: the mean, the lowest model and the highest. */
export interface ModelSpread {
  mid: number;
  low: number;
  high: number;
}

/** One year of a path (or of "carry on as now"): the crop after the crop before it. */
export interface TransitionYear {
  /** 1-based year of the path */
  n: number;
  /** calendar year: the last year on record plus n */
  year: number;
  crop: string;
  /** the crop the year before */
  after: string;
  role: "bridge" | "plan" | "carry_on";
  /** which year of the target cycle (or of the current pattern) this is; null for a bridge year */
  cycle_year: number | null;
  /** return over direct costs, $ an acre */
  return: ModelSpread;
  /** null for fallow */
  yield: number | null;
  yield_unit: string | null;
  irrigation_mm: number;
  irrigation_in: number;
  /** share of the crop's water need that is met; null for fallow */
  water_met_share: number | null;
  bare_days: number;
  /** chance the crop fails; null for fallow */
  fail_chance: number | null;
}

export interface TransitionDifferenceYear {
  n: number;
  year: number;
  /** this path minus carrying on as now, that year */
  return: ModelSpread;
  /** the running total of that difference */
  cumulative: ModelSpread;
  bare_days: number;
  cumulative_bare_days: number;
  /** irrigated fields only */
  irrigation_in?: number;
}

export interface TransitionPayback {
  status: "no_difference" | "never_behind" | "paid_back" | "not_within_span";
  /** the year (1-based) from which the running total stays at or above zero; null unless "paid_back" */
  n: number | null;
  year: number | null;
  lowest: { n: number; year: number; value: number };
  models_ahead_at_end: number;
  models: number;
}

export interface LongRunSide {
  return: ModelSpread;
  bare_days: number;
  irrigation_mm: number;
  irrigation_in: number;
  water_met_share: number | null;
  failed_share: number | null;
}

export type TransitionStatus = "path" | "already_there" | "no_path" | "no_history";

/** GET /field/{field_id}/transition?plan=1|2|3 or target=... (smart planner only) */
export interface FieldTransition {
  field_id: string;
  engine: "smart";
  settings: PlannerSettings;
  /** how many years ahead are scored, 4 to 12 */
  years: number;
  target: {
    source: "plan" | "own";
    plan: number | null;
    name: string;
    cycle: string[];
    /** what the rotation itself breaks, year after year (not the path's doing) */
    rules_broken: string[];
  };
  /** the field's last real years, at most three, oldest first */
  history: { year: number; crop: string }[];
  status: TransitionStatus;
  /** one or two plain sentences: how the path gets in, or why there is none */
  summary: string;
  path: {
    bridge_years: number;
    already_there: boolean;
    entry: { n: number; year: number; crop: string; cycle_year: number };
    /** how many valid ways in there were with that many bridge years */
    options: number;
    years: TransitionYear[];
    total_return: ModelSpread;
  } | null;
  /** "Our plans avoid ..." sentences in the way of starting the rotation straight away */
  blocked: string[];
  carry_on: {
    name: string;
    pattern: string[];
    /** false: the last four years are not a repeating pattern and are repeated as they are */
    repeating: boolean;
    basis_years: [number, number];
    years: TransitionYear[];
    total_return: ModelSpread;
  } | null;
  carry_on_note: string | null;
  difference: {
    years: TransitionDifferenceYear[];
    payback: TransitionPayback;
    total: ModelSpread;
    total_sentence: string;
  } | null;
  /** each rotation as a repeating cycle, once the plan is established */
  long_run: {
    plan: LongRunSide;
    carry_on: LongRunSide;
    difference: {
      return: ModelSpread;
      verdict: "plan" | "carry_on" | "too_close" | "same";
      models: { plan_ahead: number; carry_on_ahead: number; total: number };
      bare_days: number;
      irrigation_in?: number;
      failed_share?: number;
    };
  } | null;
  sentences: { short_term: string; long_term: string } | null;
  not_modelled: string[];
  notes: string[];
}

/** GET /field/{field_id}/plans */
export interface FieldPlans {
  field_id: string;
  /** "smart" (future-weather planner) or "basic"; missing on old servers */
  engine?: "smart" | "basic";
  plans: Plan[];
  // smart engine only
  settings?: PlannerSettings;
  climate?: ClimateSummary;
  current?: CurrentPattern | null;
  current_note?: string | null;
  candidates?: number;
}

export interface SatelliteYear {
  year: number;
  /** peak greenness (NDVI), about 0..1; null when no clear view */
  ndvi_peak: number | null;
  /** share 0..1 of clear views in which the field was green; null when no clear view */
  cover_frac: number | null;
  /** mean clear views per pixel that year */
  n_obs: number;
}

export interface SatelliteSummary {
  ndvi_peak_mean: number | null;
  cover_frac_mean: number | null;
  /** least-squares slope of ndvi_peak per decade; null with fewer than 5 years of data */
  ndvi_peak_trend_per_decade: number | null;
}

/** GET /field/{field_id}/satellite */
export interface FieldSatellite {
  field_id: string;
  available: boolean;
  /** only when available is false */
  reason?: "not_built" | "no_data_for_field";
  source: string;
  /** ascending; empty when unavailable */
  years: SatelliteYear[];
  summary: SatelliteSummary;
}

export interface SoilYear {
  year: number;
  /** estimated topsoil organic matter, %; null when no usable bare-soil view that year */
  om_pred: number | null;
  /** 0..1, how much to trust om_pred (0 when it is null) */
  confidence: number;
  /** mean bare-soil views per pixel that year */
  n_obs: number;
  /** estimated days the soil sat bare, 0..366; null when too few clear views */
  bare_days: number | null;
}

export interface SoilSummary {
  om_pred_mean: number | null;
  /** years that have an om_pred value */
  om_pred_years: number | null;
  bare_days_mean: number | null;
  /** least-squares slope of bare_days per decade; null with fewer than 5 values */
  bare_days_trend_per_decade: number | null;
}

/** GET /field/{field_id}/soil */
export interface FieldSoil {
  field_id: string;
  available: boolean;
  /** only when available is false */
  reason?: "not_built" | "no_data_for_field";
  source: string;
  /** which signal to trust; null when the meta file is missing */
  recommended: "om_pred" | "bare_days" | null;
  /** typical year-to-year jump of om_pred, in OM %; null when not measured */
  om_noise_sd: number | null;
  verdict: string | null;
  /** the field's static soil-survey organic matter, % */
  ssurgo_om_pct: number | null;
  /** ascending; empty when unavailable */
  years: SoilYear[];
  summary: SoilSummary;
}
