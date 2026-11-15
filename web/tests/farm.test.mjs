// Unit tests for the whole-farm helpers (src/lib/farm.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  FIELD_TAGS,
  LIMIT_TAGS,
  addFields,
  boxLoop,
  farmRequest,
  fieldChange,
  fieldsInside,
  formatAcres,
  formatFarmDollars,
  formatFarmScore,
  formatWhole,
  insideLoop,
  limitTone,
  loopArea,
  metWords,
  niceStep,
  parseAmount,
  rangeWords,
  ringCentre,
  shortDollars,
  toggleField,
  yearScale,
} from "../src/lib/farm.ts";

const CHOICES = { weights: { soil: 3, water: 2, money: 3, risk: 2 }, horizon: 2025, scenario: "ssp245" };

test("a click adds a field, a second click takes it out, and the order of picking is kept", () => {
  assert.deepEqual(toggleField([], "A"), ["A"]);
  assert.deepEqual(toggleField(["A", "B"], "C"), ["A", "B", "C"]);
  assert.deepEqual(toggleField(["A", "B", "C"], "B"), ["A", "C"]);
  const before = ["A"];
  toggleField(before, "B");
  assert.deepEqual(before, ["A"], "the list passed in is not changed");
});

test("a lasso only adds: fields already in the farm stay once, in place", () => {
  assert.deepEqual(addFields(["A", "B"], ["B", "C", "C", "A", "D"]), ["A", "B", "C", "D"]);
  assert.deepEqual(addFields([], []), []);
});

test("a field's centre is the middle of the box around its outline", () => {
  assert.deepEqual(ringCentre([[-101, 38], [-100, 38], [-100, 39], [-101, 39], [-101, 38]]), { lon: -100.5, lat: 38.5 });
  // an L-shape: the centre of the box, even though it lies outside the L itself
  assert.deepEqual(ringCentre([[0, 0], [4, 0], [4, 1], [1, 1], [1, 4], [0, 4]]), { lon: 2, lat: 2 });
  assert.equal(ringCentre([]), null);
  assert.equal(ringCentre(undefined), null);
});

test("fields are picked by their centre: inside the loop yes, outside or in a notch no", () => {
  const centres = [
    { id: "in", lon: 1, lat: 1 },
    { id: "out", lon: 5, lat: 5 },
    { id: "notch", lon: 3, lat: 3 },
    { id: "also-in", lon: 0.5, lat: 3.5 },
  ];
  // an L-shaped loop: the notch at (3, 3) is outside it
  const loop = [[0, 0], [4, 0], [4, 2], [2, 2], [2, 4], [0, 4]];
  assert.deepEqual(fieldsInside(centres, loop), ["in", "also-in"]);
  assert.deepEqual(fieldsInside(centres, [[0, 0], [1, 1]]), [], "two points are not a loop");
  assert.equal(insideLoop(1, 1, loop), true);
  assert.equal(insideLoop(3, 3, loop), false);
});

test("a dragged box is a loop of its four corners, whichever way it was dragged", () => {
  const loop = boxLoop([10, 20], [4, 2]);
  assert.deepEqual(loop, [[10, 20], [4, 20], [4, 2], [10, 2]]);
  assert.equal(loopArea(loop), 6 * 18);
  assert.deepEqual(fieldsInside([{ id: "a", lon: 5, lat: 10 }, { id: "b", lon: 11, lat: 10 }], loop), ["a"]);
  assert.equal(loopArea([[0, 0], [1, 0]]), 0);
});

test("the limit boxes: empty means no limit, commas and a dollar sign are fine, words are not", () => {
  assert.deepEqual(parseAmount(""), { value: null, ok: true });
  assert.deepEqual(parseAmount("   "), { value: null, ok: true });
  assert.deepEqual(parseAmount("1,200"), { value: 1200, ok: true });
  assert.deepEqual(parseAmount("$20,000"), { value: 20000, ok: true });
  assert.deepEqual(parseAmount("12.5"), { value: 12.5, ok: true });
  assert.deepEqual(parseAmount("0"), { value: 0, ok: true });
  assert.deepEqual(parseAmount("lots"), { value: null, ok: false });
  assert.deepEqual(parseAmount("12 acres"), { value: null, ok: false });
  assert.deepEqual(parseAmount("1e5"), { value: null, ok: false });
  assert.deepEqual(parseAmount("-5"), { value: null, ok: false }, "a water budget cannot be negative");
  assert.deepEqual(parseAmount("-5000", true), { value: -5000, ok: true }, "a minimum return can be");
  assert.deepEqual(parseAmount("−5000", true), { value: -5000, ok: true }, "typed with a real minus sign");
});

test("the request has the API's keys, in a fixed order, and null for a limit that is not set", () => {
  const request = farmRequest(["A", "B"], CHOICES, "", "");
  assert.equal(
    JSON.stringify(request),
    '{"field_ids":["A","B"],"weights":{"soil":3,"water":2,"money":3,"risk":2},"scenario":"ssp245","horizon":2025,"water_budget_acre_in":null,"min_income":null}',
  );
  const limited = farmRequest(["A"], { ...CHOICES, scenario: "ssp585", horizon: 2060 }, "1,200", "$20,000");
  assert.equal(limited.water_budget_acre_in, 1200);
  assert.equal(limited.min_income, 20000);
  assert.equal(limited.scenario, "ssp585");
  assert.equal(limited.horizon, 2060);
});

test("nothing is asked for an empty farm or while a box holds something that is not a number", () => {
  assert.equal(farmRequest([], CHOICES, "", ""), null);
  assert.equal(farmRequest(["A"], CHOICES, "lots", ""), null);
  assert.equal(farmRequest(["A"], CHOICES, "", "x"), null);
  assert.equal(farmRequest(["A"], CHOICES, "-1", ""), null);
});

test("numbers are written as the API rounds them", () => {
  assert.equal(formatWhole(1418), "1,418");
  assert.equal(formatWhole(243.1), "243");
  assert.equal(formatFarmDollars(13900), "$13,900");
  assert.equal(formatFarmDollars(-1200), "−$1,200");
  assert.equal(formatFarmDollars(Number.NaN), "no figure");
  assert.equal(formatFarmScore(0.639), "0.639");
  assert.equal(formatFarmScore(0.6), "0.600");
  assert.equal(formatAcres(26.81), "26.8");
  assert.equal(formatAcres(1136), "1,136.0");
  assert.equal(rangeWords(15900, 25400, formatFarmDollars), "$15,900 to $25,400");
  assert.equal(shortDollars(20000), "$20k");
  assert.equal(shortDollars(1500), "$1.5k");
  assert.equal(shortDollars(-5000), "−$5k");
  assert.equal(shortDollars(0), "$0");
  assert.equal(shortDollars(2500000), "$2.5M");
});

test("whether a limit is met is said in words, with the count of years that miss it", () => {
  assert.equal(metWords(true, [], 12, "No budget set"), "Yes, every year");
  assert.equal(metWords(false, [3, 6, 9], 12, "No budget set"), "No: 3 of 12 years");
  assert.equal(metWords(null, [], 12, "No budget set"), "No budget set");
});

test("a limit is tagged as not set, binding, not binding, impossible or not settled", () => {
  assert.equal(limitTone({ set: false, met: null, binds: false }), "off");
  assert.equal(limitTone({ set: true, met: true, binds: true }), "binds");
  assert.equal(limitTone({ set: true, met: true, binds: false }), "free");
  assert.equal(limitTone({ set: true, met: false, binds: true }), "cannot");
  assert.equal(limitTone({ set: true, met: true, binds: null }), "unknown");
  assert.equal(LIMIT_TAGS.cannot, "Cannot be met");
});

test("a field is marked when its rotation or its start year differs from its own first pick", () => {
  assert.equal(fieldChange({ same_rotation: true, same_start: true }), "same");
  assert.equal(fieldChange({ same_rotation: true, same_start: false }), "shifted");
  assert.equal(fieldChange({ same_rotation: false, same_start: false }), "changed");
  assert.equal(fieldChange({ same_rotation: false, same_start: true }), "changed");
  assert.equal(FIELD_TAGS.changed, "Different rotation");
});

test("the chart's scale starts at zero, ends on a round number and holds every mark and the limit", () => {
  const bands = [
    { mid: 20300, low: 15900, high: 25400 },
    { mid: 27000, low: 21000, high: 33100 },
  ];
  const scale = yearScale(bands, [13900, 39700], 20000);
  assert.deepEqual(scale.domain, [0, 40000]);
  assert.deepEqual(scale.ticks, [0, 20000, 40000]);
  assert.equal(scale.at(0), 0);
  assert.equal(scale.at(40000), 1);
  assert.equal(scale.at(20000), 0.5);
  assert.equal(scale.at(99999), 1, "values off the scale are clamped");
  // a limit above everything else stretches the scale so that its line can be drawn
  assert.equal(yearScale(bands, [], 90000).domain[1] >= 90000, true);
});

test("the scale goes below zero when a year loses money, and never has zero height", () => {
  const scale = yearScale([{ mid: -1200, low: -4000, high: 900 }], [], null);
  assert.equal(scale.domain[0] <= -4000, true);
  assert.equal(scale.domain[1] >= 900, true);
  assert.equal(scale.ticks.includes(0), true);
  assert.equal(scale.at(0) > 0 && scale.at(0) < 1, true);
  const flat = yearScale([{ mid: 0, low: 0, high: 0 }], [0], null);
  assert.equal(flat.domain[1] > flat.domain[0], true);
  assert.equal(flat.at(0), 0);
});

test("grid steps are 1, 2 or 5 times a power of ten", () => {
  assert.equal(niceStep(39700), 20000);
  assert.equal(niceStep(1418), 500);
  assert.equal(niceStep(300), 100);
  assert.equal(niceStep(0), 1);
});
