// Unit tests for the search box helpers (src/lib/places.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import { countiesFromShapes, countyAt, matchCounties, parseAt, parseLatLon, placesFromNominatim } from "../src/lib/places.ts";

const square = (w, s, e, n) => [[[w, s], [e, s], [e, n], [w, n], [w, s]]];
const shapes = {
  type: "FeatureCollection",
  features: [
    { properties: { fips: "20055", label: "Finney County, KS", field_count: 7255 }, geometry: { type: "Polygon", coordinates: square(-101, 37.6, -100.2, 38.3) } },
    { properties: { fips: "20057", label: "Ford County, KS", field_count: 3000 }, geometry: { type: "MultiPolygon", coordinates: [square(-100.2, 37.4, -99.5, 38.0)] } },
    { properties: { fips: "20069", label: "Gray County, KS" }, geometry: { type: "Polygon", coordinates: square(-100.65, 37.4, -100.2, 37.6) } },
    { properties: { label: "no fips" }, geometry: { type: "Polygon", coordinates: square(0, 0, 1, 1) } },
  ],
};

test("counties come out of the shapes with bounds and counts; broken ones are skipped", () => {
  const c = countiesFromShapes(shapes);
  assert.equal(c.length, 3);
  assert.deepEqual(c[0].bounds, [[37.6, -101], [38.3, -100.2]]);
  assert.equal(c[2].fieldCount, null);
  assert.deepEqual(countiesFromShapes(null), []);
});

test("a point finds its county, including in a multipolygon, and none outside", () => {
  const c = countiesFromShapes(shapes);
  assert.equal(countyAt(c, -100.87, 37.97)?.fips, "20055");
  assert.equal(countyAt(c, -99.9, 37.75)?.fips, "20057");
  assert.equal(countyAt(c, -95, 39), null);
});

test("county names match by start first, ignoring 'county' and ', KS'", () => {
  const c = countiesFromShapes(shapes);
  assert.deepEqual(matchCounties(c, "f").map((x) => x.fips), ["20055", "20057"]);
  assert.deepEqual(matchCounties(c, "Ford County").map((x) => x.fips), ["20057"]);
  assert.deepEqual(matchCounties(c, "gray county, ks").map((x) => x.fips), ["20069"]);
  assert.deepEqual(matchCounties(c, "ray").map((x) => x.fips), ["20069"]);
  assert.deepEqual(matchCounties(c, "  "), []);
});

test("coordinates are read as lat, lon", () => {
  assert.deepEqual(parseLatLon("37.97, -100.87"), [37.97, -100.87]);
  assert.deepEqual(parseLatLon("37.97 -100.87"), [37.97, -100.87]);
  assert.equal(parseLatLon("Garden City"), null);
  assert.equal(parseLatLon("97, -100"), null);
  assert.deepEqual(parseAt("?region=20057&at=37.75000,-99.90000"), [37.75, -99.9]);
  assert.equal(parseAt("?region=20057"), null);
});

test("Nominatim answers become a name and a county", () => {
  const places = placesFromNominatim([
    { lat: "37.971", lon: "-100.872", display_name: "Garden City, Finney County, Kansas, 67846, United States" },
    { lat: "37.7", lon: "-99.9", display_name: "123 Main Street, Dodge City, Ford County, Kansas, United States" },
    { lat: "x", lon: "1", display_name: "bad" },
  ]);
  assert.deepEqual(places, [
    { name: "Garden City", detail: "Finney County", lat: 37.971, lon: -100.872 },
    { name: "123 Main Street", detail: "Ford County", lat: 37.7, lon: -99.9 },
  ]);
  assert.deepEqual(placesFromNominatim({}), []);
});
