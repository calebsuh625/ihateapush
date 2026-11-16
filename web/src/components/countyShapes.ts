// GET /regions/shapes (every county's outline), fetched once and shared by the map outlines and the
// search box. Null when the server has no such endpoint (plain local dev) or it failed.
import { API_ROOT } from "../api";

let shapes: Promise<GeoJSON.FeatureCollection | null> | null = null;

export function countyShapes(): Promise<GeoJSON.FeatureCollection | null> {
  shapes ??= fetch(`${API_ROOT}/regions/shapes`)
    .then((res) => (res.ok ? (res.json() as Promise<GeoJSON.FeatureCollection>) : null))
    .catch(() => {
      shapes = null; // try again next time
      return null;
    });
  return shapes;
}
