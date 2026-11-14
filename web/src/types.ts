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
}

export interface Plan {
  name: string;
  /** one crop per year, lowercase */
  rotation: string[];
  /** each 0..1, higher is better */
  scores: PlanScores;
  reason: string;
}

/** GET /field/{field_id}/plans */
export interface FieldPlans {
  field_id: string;
  plans: Plan[];
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
