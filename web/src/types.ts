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
  /** share 0..1 of the crops' water need that is met */
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
  /** in how many climate models the plan holds up */
  futures?: { holds: number; total: number };
  spread?: { overall?: Spread; profit_per_acre?: Spread };
  numbers?: PlanNumbers;
  rank?: number;
}

/** The field's own last four years, scored like a plan (smart planner). */
export interface CurrentPattern extends Plan {
  rules_broken?: string[];
  /** [first, last] year of the pattern */
  years?: [number, number];
  is_candidate?: boolean;
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
  models?: string[];
}

export interface ClimateSummary {
  label: string;
  changes: { summer_rain_agree?: number; models?: number; [key: string]: number | undefined };
  range?: { summer_rain_pct?: [number, number]; [key: string]: [number, number] | undefined };
  window?: [number, number];
  models?: number;
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
