// The search box (components/PlaceSearch.tsx): pure helpers, unit-tested in tests/places.test.mjs.
// Counties come from GET /regions/shapes (one GeoJSON feature per county on offer); towns and
// addresses from OpenStreetMap's Nominatim, asked only when the visitor presses Enter.
// No imports, so the unit tests can load this file directly.

/** Even-odd point-in-polygon over all rings (the same rule as geo.pointInRings). */
function inRings(lon: number, lat: number, rings: number[][][]): boolean {
  let inside = false;
  for (const ring of rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [ax, ay] = ring[i] ?? [];
      const [bx, by] = ring[j] ?? [];
      if (ax === undefined || ay === undefined || bx === undefined || by === undefined) continue;
      if (ay > lat !== by > lat && lon < ax + ((lat - ay) * (bx - ax)) / (by - ay)) inside = !inside;
    }
  }
  return inside;
}

export interface CountyShape {
  fips: string;
  label: string; // "Ford County, KS"
  fieldCount: number | null;
  polygons: number[][][][]; // [polygon][ring][point] = [lon, lat]
  bounds: [[number, number], [number, number]]; // [[south, west], [north, east]]
}

/** The counties in a /regions/shapes answer, with their outlines as plain polygon lists. */
export function countiesFromShapes(shapes: unknown): CountyShape[] {
  const features = (shapes as { features?: unknown })?.features;
  if (!Array.isArray(features)) return [];
  const out: CountyShape[] = [];
  for (const f of features) {
    const p = (f as { properties?: Record<string, unknown> })?.properties;
    const g = (f as { geometry?: { type?: string; coordinates?: unknown } })?.geometry;
    if (!p || typeof p.fips !== "string" || typeof p.label !== "string" || !g) continue;
    const polygons = (g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : []) as number[][][][];
    let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity;
    for (const poly of polygons) {
      for (const [lon, lat] of poly[0] ?? []) {
        if (lon === undefined || lat === undefined) continue;
        west = Math.min(west, lon);
        east = Math.max(east, lon);
        south = Math.min(south, lat);
        north = Math.max(north, lat);
      }
    }
    if (!(west <= east && south <= north)) continue;
    out.push({
      fips: p.fips,
      label: p.label,
      fieldCount: typeof p.field_count === "number" ? p.field_count : null,
      polygons,
      bounds: [[south, west], [north, east]],
    });
  }
  return out;
}

/** The county a point falls in, or null when it is in none of them. */
export function countyAt(counties: readonly CountyShape[], lon: number, lat: number): CountyShape | null {
  for (const c of counties) {
    const [[s, w], [n, e]] = c.bounds;
    if (lat < s || lat > n || lon < w || lon > e) continue;
    if (c.polygons.some((rings) => inRings(lon, lat, rings))) return c;
  }
  return null;
}

const simple = (text: string) =>
  text.toLowerCase().replace(/,?\s*(ks|kansas)$/, "").replace(/\bcounty\b/g, "").replace(/[^a-z0-9]+/g, " ").trim();

/** Counties whose name matches what was typed: names that start with it first, then the rest. */
export function matchCounties(counties: readonly CountyShape[], query: string, limit = 5): CountyShape[] {
  const q = simple(query);
  if (!q) return [];
  const starts: CountyShape[] = [];
  const contains: CountyShape[] = [];
  for (const c of counties) {
    const name = simple(c.label);
    if (name.startsWith(q)) starts.push(c);
    else if (name.includes(q)) contains.push(c);
  }
  const byName = (a: CountyShape, b: CountyShape) => a.label.localeCompare(b.label);
  return [...starts.sort(byName), ...contains.sort(byName)].slice(0, limit);
}

/** "37.97, -100.87" (or with a space only) as [lat, lon]; null for anything else. */
export function parseLatLon(text: string): [number, number] | null {
  const m = text.trim().match(/^(-?\d{1,2}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)$/);
  if (!m) return null;
  const lat = Number(m[1]);
  const lon = Number(m[2]);
  return Math.abs(lat) <= 90 && Math.abs(lon) <= 180 ? [lat, lon] : null;
}

/** The ?at=lat,lon of a page opened from a search, or null. */
export function parseAt(search: string): [number, number] | null {
  const at = new URLSearchParams(search).get("at");
  return at ? parseLatLon(at) : null;
}

export interface Place {
  name: string; // "Garden City"
  detail: string; // "Finney County"
  lat: number;
  lon: number;
}

/** Kansas, a little wider, for Nominatim's viewbox (west, north, east, south). */
export const KANSAS_VIEWBOX = "-102.1,40.05,-94.55,36.95";

/** Nominatim's answer as places: the first part of each name, then the county or the next part. */
export function placesFromNominatim(body: unknown): Place[] {
  if (!Array.isArray(body)) return [];
  const out: Place[] = [];
  for (const r of body) {
    const lat = Number((r as { lat?: unknown }).lat);
    const lon = Number((r as { lon?: unknown }).lon);
    const display = (r as { display_name?: unknown }).display_name;
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || typeof display !== "string") continue;
    const parts = display.split(",").map((s) => s.trim()).filter((s) => s && s !== "United States" && s !== "Kansas" && !/^\d{5}(-\d{4})?$/.test(s));
    const county = parts.find((s) => / County$/.test(s));
    const name = parts[0] ?? display;
    out.push({ name, detail: county && county !== name ? county : (parts[1] ?? ""), lat, lon });
  }
  return out;
}
