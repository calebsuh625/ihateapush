// Unit tests for the water outlook helpers (src/lib/water.ts) and the pumping what-if in the queries.
import assert from "node:assert/strict";
import { test } from "node:test";
import { farmRequest } from "../src/lib/farm.ts";
import { DEFAULT_CHOICES, plansQuery } from "../src/lib/planner.ts";
import { scoreQuery } from "../src/lib/custom.ts";
import { transitionQuery } from "../src/lib/transition.ts";
import {
  chartScales,
  depthDomain,
  depthIn,
  feetBand,
  inchesWords,
  limitChoice,
  limitRange,
  limitValue,
  linePath,
  milesText,
  oneDecimal,
  rateWithInterval,
  resetWords,
  spreadLabels,
  whatIfLine,
  winterAt,
} from "../src/lib/water.ts";

const WHAT_IF = { limit_in: 24, default_in: 24, active: false, min_in: 1, max_in: 24 };
const BOX = { width: 400, height: 200, left: 40, right: 60, top: 10, bottom: 20 };
const WELLS = [
  { distance_mi: 1.9, points: [[1996, 140], [2010, 218], [2024, 284]], fit: [[1996, 141.5], [2024, 293.6]] },
  { distance_mi: 3.5, points: [[2012, 290], [2026, 322]], fit: [[2012, 291], [2026, 323]] },
];

test("the what-if is only sent while it is on, and rides along to the score and the path", () => {
  assert.equal(plansQuery(DEFAULT_CHOICES), "soil=3&water=2&money=3&risk=2&scenario=ssp245&horizon=2025");
  assert.equal(plansQuery({ ...DEFAULT_CHOICES, limitIn: null }), plansQuery(DEFAULT_CHOICES));
  const query = plansQuery({ ...DEFAULT_CHOICES, limitIn: 12 }, true);
  assert.equal(query, "soil=3&water=2&money=3&risk=2&scenario=ssp245&horizon=2025&irrigated=true&limit_in=12");
  assert.equal(plansQuery({ ...DEFAULT_CHOICES, limitIn: Number.NaN }), plansQuery(DEFAULT_CHOICES));
  assert.ok(scoreQuery(query, "corn, soybeans").endsWith("&limit_in=12"));
  assert.ok(transitionQuery(query, { plan: 1 }).endsWith("&limit_in=12"));
});

test("the farm request carries the what-if only while it is on", () => {
  const usual = farmRequest(["A"], DEFAULT_CHOICES, "", "");
  assert.equal("limit_in" in usual, false);
  assert.equal(farmRequest(["A"], { ...DEFAULT_CHOICES, limitIn: null }, "", "").limit_in, undefined);
  const tight = farmRequest(["A"], { ...DEFAULT_CHOICES, limitIn: 10 }, "", "");
  assert.equal(tight.limit_in, 10);
  assert.ok(JSON.stringify(tight).endsWith(',"limit_in":10}'));
});

test("the slider stands on the planner's limit until the farmer moves it", () => {
  assert.deepEqual(limitRange(WHAT_IF), { min: 1, max: 24 });
  assert.equal(limitValue(null, WHAT_IF), 24);
  assert.equal(limitValue(undefined, WHAT_IF), 24);
  assert.equal(limitValue(12, WHAT_IF), 12);
  assert.equal(limitValue(99, WHAT_IF), 24);
  assert.equal(limitValue(0, WHAT_IF), 1);
  assert.equal(limitValue(null, { ...WHAT_IF, default_in: null, max_in: 60 }), 60);
});

test("a slider position at the planner's own limit asks for nothing", () => {
  assert.equal(limitChoice(24, WHAT_IF), null);
  assert.equal(limitChoice(12, WHAT_IF), 12);
  assert.equal(limitChoice(12.4, WHAT_IF), 12);
  assert.equal(limitChoice(0, WHAT_IF), 1);
  assert.equal(limitChoice(40, WHAT_IF), null); // clamped to 24, which is the default
  assert.equal(limitChoice(Number.NaN, WHAT_IF), null);
  assert.equal(limitChoice(60, { ...WHAT_IF, default_in: null, max_in: 60 }), 60);
});

test("the what-if line says what the cards were scored under", () => {
  assert.equal(whatIfLine(WHAT_IF), null);
  assert.equal(whatIfLine(null), null);
  assert.equal(
    whatIfLine({ ...WHAT_IF, limit_in: 12, active: true }),
    "What-if: these plans are scored as if no more than 12 inches could be pumped on an acre in a year.",
  );
  assert.equal(inchesWords(1), "1 inch");
  assert.equal(inchesWords(12.5), "12.5 inches");
  assert.equal(resetWords(WHAT_IF), "Back to 24 inches");
  assert.equal(resetWords({ ...WHAT_IF, default_in: null }), "Back to no limit");
});

test("numbers are written as the API rounds them", () => {
  assert.equal(oneDecimal(2.64), "2.6");
  assert.equal(oneDecimal(3), "3");
  assert.equal(oneDecimal(-0.31), "−0.3");
  assert.equal(oneDecimal(Number.NaN), "no figure");
  assert.equal(feetBand({ median: 138, low: 70, high: 146 }), "138 ft (70 to 146)");
  assert.equal(feetBand({ median: 1250, low: 1250, high: 1250 }), "1,250 ft");
  assert.equal(rateWithInterval(5.4, [5.2, 5.7]), "5.4 (5.2 to 5.7)");
  assert.equal(rateWithInterval(2, [2, 2]), "2");
  assert.equal(rateWithInterval(-0.1, [-0.4, 0.2]), "−0.1 (−0.4 to 0.2)");
  assert.equal(milesText(1.9), "1.9 mi");
});

test("the depth axis has round ends and few ticks", () => {
  assert.deepEqual(depthDomain(136, 338), { domain: [100, 350], step: 50 });
  assert.deepEqual(depthDomain(41, 58), { domain: [40, 60], step: 5 });
  assert.deepEqual(depthDomain(120, 120), { domain: [120, 125], step: 5 });
  assert.deepEqual(depthDomain(338, 136), { domain: [100, 350], step: 50 });
  const wide = depthDomain(5, 1900);
  assert.ok(wide.domain[0] <= 5 && wide.domain[1] >= 1900);
});

test("the chart puts later winters to the right and deeper water lower", () => {
  const scales = chartScales(WELLS, BOX);
  assert.deepEqual(scales.winters, [1996, 2026]);
  assert.deepEqual(scales.depths, [100, 350]);
  assert.deepEqual(scales.depthTicks, [100, 150, 200, 250, 300, 350]);
  assert.deepEqual(scales.winterTicks, [2000, 2010, 2020]);
  assert.equal(scales.x(1996), 40);
  assert.equal(scales.x(2026), 340);
  assert.equal(scales.y(100), 10);
  assert.equal(scales.y(350), 180);
  assert.ok(scales.y(284) > scales.y(140));
  assert.equal(linePath(WELLS[1].points, scales), "M200.0 139.2 L340.0 161.0");
  assert.equal(chartScales([], BOX), null);
});

test("labels that would sit on each other are pushed apart, inside the plot, in their order", () => {
  assert.deepEqual(spreadLabels([20, 60, 100], 12, 0, 200), [20, 60, 100]);
  assert.deepEqual(spreadLabels([100, 102, 104], 12, 0, 200), [100, 112, 124]);
  assert.deepEqual(spreadLabels([104, 100, 102], 12, 0, 200), [124, 100, 112]);
  assert.deepEqual(spreadLabels([190, 195, 199], 12, 0, 200), [176, 188, 200]);
  assert.deepEqual(spreadLabels([], 12, 0, 200), []);
});

test("the hover line snaps to the nearest winter and reads that winter's depth", () => {
  assert.equal(winterAt(40, BOX, [1996, 2026]), 1996);
  assert.equal(winterAt(190, BOX, [1996, 2026]), 2011);
  assert.equal(winterAt(0, BOX, [1996, 2026]), 1996);
  assert.equal(winterAt(900, BOX, [1996, 2026]), 2026);
  assert.equal(depthIn(WELLS[0], 2010), 218);
  assert.equal(depthIn(WELLS[0], 2011), null);
});
