// Unit tests for the map layer helpers (src/lib/layers.ts), on made-up layers.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  NONE, NO_VALUE_COLOUR, PALETTES, boxesOf, classColours, classIndexes, classOf, countyStats, decimalsOf, fieldColours,
  formatShare, formatValue, luminance, pickSteps, quantile, realValue, sameFields, seriesFor, viewStats, visibleIndexes, yearFor,
} from "../src/lib/layers.ts";

const numberLayer = {
  name: "bare_days", title: "Days the soil sat bare", kind: "number", status: "ready", reason: null, unit: "days a year",
  value_scale: 1, years: [2016, 2017], method: "", caveat: "", field_count: 6, breaks_scaled: [1, 20, 50],
  classes: [0, 1, 2, 3].map((index) => ({ index, label: `c${index}` })),
};
const series = { year: 2017, values: [0, 5, null, 20, 49, 300], summary: { fields: 6, fields_with_value: 5, fields_no_value: 1, classes: [] } };
const acres = [10, 20, 30, 40, 50, 60];

test("classOf counts the breaks at or below the value", () => {
  assert.deepEqual([0, 1, 19, 20, 49, 50, 300].map((v) => classOf(v, [1, 20, 50])), [0, 1, 1, 2, 2, 3, 3]);
});

test("classIndexes: a missing value is NONE, never a class", () => {
  assert.deepEqual([...classIndexes(numberLayer, series)], [0, 1, NONE, 2, 2, 3]);
});

test("every sequential scale gets lighter or darker in one direction and never uses the no-value grey", () => {
  for (const [name, scale] of Object.entries(PALETTES)) {
    const lum = scale.map(luminance);
    for (let i = 1; i < lum.length; i++) assert.ok(lum[i - 1] - lum[i] >= 0.05, `${name}: step ${i} is too close in lightness`);
    assert.ok(!scale.includes(NO_VALUE_COLOUR), `${name} uses the no-value colour`);
    assert.ok(luminance(scale[0]) > 0.6 && luminance(scale.at(-1)) < 0.12, `${name}: range of lightness is too narrow`);
  }
  // the grey has no hue
  const [r, g, b] = [1, 3, 5].map((at) => Number.parseInt(NO_VALUE_COLOUR.slice(at, at + 2), 16));
  assert.ok(Math.max(r, g, b) - Math.min(r, g, b) <= 8);
});

test("pickSteps: evenly spaced, ends kept", () => {
  const s = ["a", "b", "c", "d", "e"];
  assert.deepEqual(pickSteps(s, 5), s);
  assert.deepEqual(pickSteps(s, 3), ["a", "c", "e"]);
  assert.deepEqual(pickSteps(s, 2), ["a", "e"]);
});

test("classColours: number layers use their scale, no-value classes are grey, categories use their key", () => {
  assert.deepEqual(classColours({ ...numberLayer, classes: [0, 1, 2, 3, 4].map((index) => ({ index, label: "" })) }), PALETTES.bare_days);
  const rotation = {
    name: "rotation_change", kind: "class", ordered: true,
    classes: [{ index: 0, key: "same" }, { index: 1, key: "x" }, { index: 2, key: "unknown", no_value: true }],
  };
  const c = classColours(rotation);
  assert.equal(c[2], NO_VALUE_COLOUR);
  assert.ok(!PALETTES.rotation_change.includes(NO_VALUE_COLOUR) && c[0] !== c[1]);
  const irrigation = { name: "irrigation", kind: "class", ordered: false, classes: [{ index: 0, key: "dryland" }, { index: 1, key: "irrigated" }, { index: 2, key: "unknown", no_value: true }] };
  const i = classColours(irrigation);
  assert.ok(i[0] !== i[1] && i[2] === NO_VALUE_COLOUR);
});

test("fieldColours: one colour per field, grey for no value", () => {
  assert.deepEqual(fieldColours(Uint8Array.from([0, 1, NONE]), ["x", "y"]), ["x", "y", NO_VALUE_COLOUR]);
});

test("yearFor and seriesFor", () => {
  assert.equal(yearFor(numberLayer, null), 2017);
  assert.equal(yearFor(numberLayer, 2016), 2016);
  assert.equal(yearFor(numberLayer, 2030), 2017);
  assert.equal(yearFor({ years: null }, 2016), null);
  const answer = { ...numberLayer, series: [{ year: 2016, values: [] }, series] };
  assert.equal(seriesFor(answer, 2017), series);
  assert.equal(seriesFor({ years: null, series: [series] }, null), series);
});

test("quantile matches numpy's linear interpolation", () => {
  assert.equal(quantile([1, 2, 3, 4], 0.5), 2.5);
  assert.equal(quantile([1, 2, 3, 4], 0.25), 1.75);
  assert.equal(quantile([], 0.5), null);
});

test("rounding and formatting follow the layer's step", () => {
  assert.equal(decimalsOf(1), 0);
  assert.equal(decimalsOf(0.001), 3);
  assert.equal(realValue(46.5, 1), 47);
  assert.equal(realValue(42, 0.001), 0.042);
  assert.equal(formatValue(0.04, 0.001), "0.040");
  assert.equal(formatValue(1234, 1), "1,234");
  assert.equal(formatShare(0.2), "20%");
  assert.equal(formatShare(0.001), "under 1%");
  assert.equal(formatShare(null), "–");
});

test("visibleIndexes: a field counts when any part of it is in view", () => {
  const features = [
    { geometry: { coordinates: [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]] } },
    { geometry: { coordinates: [[[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]]] } },
    { geometry: null },
  ];
  const boxes = boxesOf(features);
  assert.deepEqual([...visibleIndexes(boxes, 0.5, 0.5, 2, 2)], [0]);
  assert.deepEqual([...visibleIndexes(boxes, -1, -1, 10, 10)], [0, 1]);
  assert.deepEqual([...visibleIndexes(boxes, 2, 2, 3, 3)], []);
});

test("viewStats: counts, acres, median and spread of the fields asked for", () => {
  const cls = classIndexes(numberLayer, series);
  const all = viewStats(numberLayer, series, cls, acres, null);
  assert.equal(all.fields, 6);
  assert.equal(all.withValue, 5);
  assert.equal(all.noValue, 1);
  assert.equal(all.acres, 10 + 20 + 40 + 50 + 60, "acres are of the fields with a value");
  assert.equal(all.median, 20);
  assert.equal(all.p25, 5);
  assert.equal(all.p75, 49);
  assert.deepEqual(all.classes.map((c) => c.fields), [1, 1, 2, 1]);
  assert.equal(all.classes[2].share, 0.4);
  const some = viewStats(numberLayer, series, cls, acres, Uint32Array.from([0, 1, 2]));
  assert.equal(some.fields, 3);
  assert.equal(some.median, 3, "2.5 days rounds up to whole days");
  assert.equal(some.noValue, 1);
  const none = viewStats(numberLayer, series, cls, acres, new Uint32Array(0));
  assert.equal(none.fields, 0);
  assert.equal(none.median, null);
  assert.equal(none.classes[0].share, null);
});

test("viewStats on a class layer: shares are of all fields, the unknown class counts as no value", () => {
  const layer = {
    name: "irrigation", kind: "class", value_scale: 1, ordered: false,
    classes: [{ index: 0, key: "dryland" }, { index: 1, key: "irrigated" }, { index: 2, key: "unknown", no_value: true }],
  };
  const s = { year: null, values: [0, 1, 1, 2], summary: { fields: 4, fields_with_value: 3, fields_no_value: 1, acres: 100, classes: [{ index: 0, fields: 1, acres: 10, share_fields: 0.25, share_acres: 0.1 }] } };
  const cls = classIndexes(layer, s);
  const v = viewStats(layer, s, cls, [10, 20, 30, 40], null);
  assert.equal(v.fields, 4);
  assert.equal(v.noValue, 1);
  assert.equal(v.acres, 100, "class layers count the acres of every field");
  assert.deepEqual(v.classes.map((c) => c.share), [0.25, 0.5, 0.25]);
  assert.equal(v.median, null);
  const county = countyStats(layer, s);
  assert.equal(county.acres, 100);
  assert.equal(county.classes[0].share, 0.25);
});

test("sameFields: count must match; the ETag too when both are known", () => {
  const answer = { field_count: 6, fields_etag: 'W/"a"' };
  assert.equal(sameFields(answer, 6, 'W/"a"'), true);
  assert.equal(sameFields(answer, 6, null), true);
  assert.equal(sameFields(answer, 7, null), false);
  assert.equal(sameFields(answer, 6, 'W/"b"'), false);
});

test("county shares come from the counts, not from the API's rounded shares; class medians are carried", async () => {
  const { detailText } = await import("../src/lib/layers.ts");
  const layer = {
    name: "rotation_change", title: "t", kind: "class", status: "ready", unit: null, value_scale: 1, years: null, caveat: "", ordered: true,
    classes: [{ index: 0, key: "clearly_higher", label: "a" }, { index: 1, key: "unknown", label: "b", no_value: true }],
    detail: { key: "score_gap", label: "First pick's score minus the current pattern's", unit: "score points", value_scale: 0.001 },
  };
  const s = { year: null, values: [], summary: { fields: 7255, fields_with_value: 7004, fields_no_value: 251, acres: 1, classes: [
    { index: 0, fields: 251, acres: 1, share_fields: 0.035, share_acres: 0.02, detail_median: 0.072 },
    { index: 1, fields: 7004, acres: 1, share_fields: 0.965, share_acres: 0.98, detail_median: null },
  ] } };
  const county = countyStats(layer, s);
  assert.equal(formatShare(county.classes[0].share), "3%", "251 of 7,255 is 3%, though 0.035 alone would round to 4%");
  assert.equal(detailText(layer, county.classes[0]), "county median gap 0.072");
  assert.equal(detailText(layer, county.classes[1]), null);
  assert.equal(detailText({ detail: { key: "irrigated_prob", label: "x", unit: "%", value_scale: 1 } }, { index: 0, fields: 1, acres: 1, share: 1, detailMedian: 97 }), "county median chance 97%");
});
