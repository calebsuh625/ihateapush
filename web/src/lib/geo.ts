import type { Bbox, FieldCollection } from "../types";

/** Leaflet-style corners: [[south, west], [north, east]]. */
export type LatLngCorners = [[number, number], [number, number]];

/** Bounding box of a polygon's outer ring ([lon, lat] positions). Null if it has no usable points. */
export function ringBounds(rings: number[][][]): { west: number; south: number; east: number; north: number } | null {
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (const position of rings[0] ?? []) {
    const lon = position[0];
    const lat = position[1];
    if (lon === undefined || lat === undefined) continue;
    if (lon < west) west = lon;
    if (lon > east) east = lon;
    if (lat < south) south = lat;
    if (lat > north) north = lat;
  }
  return west <= east && south <= north ? { west, south, east, north } : null;
}

/** Even-odd point-in-polygon over all rings, so a point in a hole counts as outside. */
export function pointInRings(lon: number, lat: number, rings: number[][][]): boolean {
  let inside = false;
  for (const ring of rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const a = ring[i];
      const b = ring[j];
      const ax = a?.[0];
      const ay = a?.[1];
      const bx = b?.[0];
      const by = b?.[1];
      if (ax === undefined || ay === undefined || bx === undefined || by === undefined) continue;
      if (ay > lat !== by > lat && lon < ax + ((lat - ay) * (bx - ax)) / (by - ay)) inside = !inside;
    }
  }
  return inside;
}

const isBbox = (v: unknown): v is Bbox =>
  Array.isArray(v) && v.length === 4 && v.every((n) => typeof n === "number" && Number.isFinite(n));

/** Bounds of every field: the collection's `bbox` when present, otherwise computed. Null if empty. */
export function collectionBounds(fields: FieldCollection): LatLngCorners | null {
  let box: Bbox | null = isBbox(fields.bbox) ? fields.bbox : null;
  if (!box) {
    let west = Infinity;
    let south = Infinity;
    let east = -Infinity;
    let north = -Infinity;
    for (const feature of fields.features) {
      const one = ringBounds(feature.geometry?.coordinates ?? []); // the outer ring: holes lie inside it
      if (!one) continue;
      west = Math.min(west, one.west);
      south = Math.min(south, one.south);
      east = Math.max(east, one.east);
      north = Math.max(north, one.north);
    }
    if (west <= east && south <= north) box = [west, south, east, north];
  }
  if (!box) return null;
  const [west, south, east, north] = box;
  return [
    [south, west],
    [north, east],
  ];
}
