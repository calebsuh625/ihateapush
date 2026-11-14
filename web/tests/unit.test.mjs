// Unit tests for the pure modules. Node runs the TypeScript sources directly (type stripping):
//   npm test
import assert from "node:assert/strict";
import { test } from "node:test";
import { CROP_COLORS, cropBackground, cropColor, cropLabel, cropPalette, doubleCropParts, textColorOn } from "../src/crops.ts";
import { clamp01, formatCount, formatOneDecimal, formatPercent, formatScore, hectaresToAcres, plural } from "../src/lib/format.ts";
import { collectionBounds, pointInRings, ringBounds } from "../src/lib/geo.ts";
import { organicMatterClass, slopeClass } from "../src/thresholds.ts";

const HEX = /^#[0-9a-f]{6}$/;

test("the seven named crops have fixed, distinct colours", () => {
  const names = ["corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow", "cover crop"];
  assert.deepEqual(Object.keys(CROP_COLORS).sort(), [...names].sort());
  const colors = names.map(cropColor);
  assert.equal(new Set(colors).size, 7);
  for (const color of colors) assert.match(color, HEX);
  assert.equal(cropColor("  Corn "), CROP_COLORS.corn);
});

test("an unseen crop gets a deterministic colour that is not one of the named ones", () => {
  for (const name of ["cotton", "grass/pasture", "triticale", "cdl class 250", "popcorn or ornamental corn"]) {
    const color = cropColor(name);
    assert.match(color, HEX);
    assert.equal(color, cropColor(name));
    assert.ok(!Object.values(CROP_COLORS).includes(color), `${name} reuses a named colour`);
  }
});

test("crops shown together never share a colour", () => {
  const others = ["cotton", "grass/pasture", "triticale", "sunflower", "oats", "other hay", "rye", "barley"];
  const palette = cropPalette(["corn", "fallow", ...others]);
  assert.equal(palette.get("corn"), CROP_COLORS.corn);
  assert.equal(new Set(others.map((name) => palette.get(name))).size, others.length);
  // independent of the order the crops arrive in
  assert.deepEqual([...cropPalette([...others].reverse())].sort(), [...cropPalette(others)].sort());
  // a single unseen crop keeps the colour its name hashes to
  assert.equal(cropPalette(["cotton"]).get("cotton"), cropColor("cotton"));
});

test("a double crop is drawn as a split of its two crops", () => {
  assert.deepEqual(doubleCropParts("double crop: winter wheat/sorghum"), ["winter wheat", "sorghum"]);
  assert.equal(doubleCropParts("grass/pasture"), null);
  assert.equal(doubleCropParts("winter wheat"), null);
  const fill = cropBackground("double crop: winter wheat/sorghum");
  assert.ok(fill.startsWith("linear-gradient("));
  assert.ok(fill.includes(CROP_COLORS["winter wheat"]) && fill.includes(CROP_COLORS.sorghum));
  assert.equal(cropBackground("corn"), CROP_COLORS.corn);
  const palette = cropPalette(["double crop: barley/sorghum", "barley"]);
  assert.ok(cropBackground("double crop: barley/sorghum", palette).includes(palette.get("barley")));
});

test("text on a crop colour has at least 4.5:1 contrast", () => {
  const luminance = (hex) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  for (const name of [...Object.keys(CROP_COLORS), "cotton", "grass/pasture", "triticale", "sunflower", "oats", "rye", "barley", "millet"]) {
    const background = cropColor(name);
    const text = textColorOn(background);
    const [hi, lo] = [luminance(background), luminance(text)].sort((a, b) => b - a);
    assert.ok((hi + 0.05) / (lo + 0.05) >= 4.5, `${name}: ${text} on ${background}`);
  }
});

test("labels are title-cased", () => {
  assert.equal(cropLabel("winter wheat"), "Winter Wheat");
  assert.equal(cropLabel("grass/pasture"), "Grass/Pasture");
  assert.equal(cropLabel("double crop: winter wheat/sorghum"), "Double Crop: Winter Wheat/Sorghum");
  assert.equal(cropLabel("corn"), "Corn");
});

test("number formatting", () => {
  assert.equal(formatCount(8412), "8,412");
  assert.equal(formatOneDecimal(52.34), "52.3");
  assert.equal(formatOneDecimal(1234.56), "1,234.6");
  assert.equal(formatPercent(0), "0.0%");
  assert.equal(formatPercent(64.7), "64.7%");
  assert.equal(formatScore(0.714), "0.71");
  assert.equal(formatScore(0.575), "0.58");
  assert.equal(formatScore(0.665), "0.67");
  assert.equal(formatScore(1), "1.00");
  assert.equal(formatScore(0), "0.00");
  assert.equal(clamp01(1.4), 1);
  assert.equal(clamp01(-0.2), 0);
  assert.equal(clamp01(Number.NaN), 0);
  assert.equal(formatOneDecimal(hectaresToAcres(52.3)), "129.2");
  assert.equal(plural(1, "field"), "field");
  assert.equal(plural(30, "field"), "fields");
});

test("qualitative classes", () => {
  assert.deepEqual([0.4, 1, 1.9, 2, 3.9, 4, 9].map(organicMatterClass), ["Very low", "Low", "Low", "Moderate", "Moderate", "High", "High"]);
  assert.deepEqual([0, 1.9, 2, 5.9, 6, 11.9, 12, 40].map(slopeClass), [
    "Nearly level", "Nearly level", "Gently sloping", "Gently sloping", "Sloping", "Sloping", "Steep", "Steep",
  ]);
});

test("bounds and point-in-polygon", () => {
  const square = [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]];
  const withHole = [...square, [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]];
  const lShape = [[[0, 0], [4, 0], [4, 1], [1, 1], [1, 4], [0, 4], [0, 0]]];
  assert.deepEqual(ringBounds(square), { west: 0, south: 0, east: 4, north: 4 });
  assert.equal(ringBounds([[]]), null);
  assert.equal(pointInRings(2, 2, square), true);
  assert.equal(pointInRings(5, 2, square), false);
  assert.equal(pointInRings(2, 2, withHole), false);
  assert.equal(pointInRings(0.5, 2, withHole), true);
  assert.equal(pointInRings(2, 2, lShape), false); // the middle of an L's bounding box is outside it
  assert.equal(pointInRings(0.5, 3, lShape), true);

  const feature = (rings) => ({ type: "Feature", properties: { field_id: "KS-000001", area_ha: 1 }, geometry: { type: "Polygon", coordinates: rings } });
  const collection = { type: "FeatureCollection", features: [feature(square), feature([[[10, 10], [12, 10], [12, 13], [10, 10]]])] };
  assert.deepEqual(collectionBounds(collection), [[0, 0], [13, 12]]); // computed: [[south, west], [north, east]]
  assert.deepEqual(collectionBounds({ ...collection, bbox: [-101, 37, -100, 38] }), [[37, -101], [38, -100]]); // bbox wins
  assert.equal(collectionBounds({ type: "FeatureCollection", features: [] }), null);
});
