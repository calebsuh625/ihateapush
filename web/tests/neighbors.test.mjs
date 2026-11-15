// Unit tests for the "Your neighbors" helpers (src/lib/neighbors.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  afterGap,
  changeText,
  chartGeometry,
  effectDecimals,
  effectReading,
  formatSigned,
  levelText,
  rotationHint,
  rotationLabel,
  sharedHistoryPct,
  unitsText,
  valueDecimals,
  verdictLabel,
  verdictOf,
  yearSpan,
} from "../src/lib/neighbors.ts";

const BARE = { column: "bare_days", label: "Days the soil sits bare each year", units: "days per year", higher_is_better: false };
const OM = { column: "om_pred", label: "Soil organic matter (satellite estimate)", units: "percentage points", higher_is_better: true };

test("signed numbers use a real minus and never show -0.00", () => {
  assert.equal(formatSigned(0.1122), "+0.11");
  assert.equal(formatSigned(-0.05), "−0.05");
  assert.equal(formatSigned(-0.001), "0.00");
});

test("rotation labels read as words", () => {
  assert.equal(rotationLabel("alfalfa/perennial"), "Alfalfa / perennial");
  assert.equal(rotationLabel("wheat-sorghum-fallow"), "Wheat–sorghum–fallow");
});

test('"diverse" is shown as a mixed bag, with a hint that fallow counts', () => {
  assert.equal(rotationLabel("diverse"), "Mixed (3+ crops)");
  assert.match(rotationHint("diverse"), /fallow/i);
  assert.equal(rotationHint("continuous corn"), null);
});

test("shared history is a whole percent", () => {
  assert.equal(sharedHistoryPct(0), 100);
  assert.equal(sharedHistoryPct(0.125), 88);
  assert.equal(sharedHistoryPct(null), null);
});

test("year ranges come from the run, with an en dash", () => {
  assert.equal(yearSpan([2008, 2019]), "2008–2019");
  assert.equal(yearSpan([2016, 2016]), "2016");
  assert.equal(yearSpan(undefined), "");
});

test("bare days: fewer is better, whole days, plain units", () => {
  assert.equal(verdictOf(-19.66, BARE), "better");
  assert.equal(verdictOf(12.3, BARE), "worse");
  assert.equal(verdictOf(0, BARE), null);
  assert.equal(verdictOf(0.3, BARE), null); // shown as 0 days: nothing to call
  assert.equal(verdictOf(null, BARE), null);
  assert.equal(verdictLabel("better", BARE), "better protected");
  assert.equal(verdictLabel("worse", BARE), "more exposed");
  assert.equal(levelText(-19.66, BARE), "sat bare 20 fewer days a year than");
  assert.equal(levelText(12.3, BARE), "sat bare 12 more days a year than");
  assert.equal(levelText(1.2, BARE), "sat bare 1 more day a year than");
  assert.equal(levelText(0.3, BARE), "sat bare about as many days a year as");
  assert.equal(changeText(-19.66, BARE), "20 days a year lower");
  assert.equal(changeText(12.3, BARE), "12 days a year higher");
  assert.equal(changeText(0.2, BARE), "the same");
  assert.equal(unitsText(BARE), "days a year");
  assert.equal(valueDecimals(BARE), 0);
  assert.equal(effectDecimals(BARE), 1);
});

test("organic matter: higher is better, two decimals", () => {
  assert.equal(verdictOf(0.11, OM), "better");
  assert.equal(verdictOf(-0.11, OM), "worse");
  assert.equal(verdictLabel("worse", OM), "worse");
  assert.equal(levelText(-0.114, OM), "was 0.11 percentage points lower than");
  assert.equal(levelText(0.001, OM), "was level with");
  assert.equal(changeText(-0.114, OM), "0.11 percentage points lower");
  assert.equal(valueDecimals(OM), 2);
  assert.equal(effectDecimals(OM), 2);
});

test("the tag follows the level after the switch, not the change", () => {
  // KS-007027 against KS-007004 in the county run: bare days fell 66 on this field and rose 80 on
  // its look-alike (change: 146 lower), yet this field still sat bare MORE days afterwards
  const n = { before: { this: 224.2, neighbor: 53.4 }, after: { this: 157.9, neighbor: 133.0 }, divergence: -145.8 };
  const gap = afterGap(n);
  assert.ok(Math.abs(gap - 24.9) < 1e-9);
  assert.equal(verdictOf(gap, BARE), "worse");
  assert.equal(verdictLabel(verdictOf(gap, BARE), BARE), "more exposed");
  assert.equal(levelText(gap, BARE), "sat bare 25 more days a year than");
  assert.equal(changeText(n.divergence, BARE), "146 days a year lower");
  assert.doesNotMatch(changeText(n.divergence, BARE), /fewer|protected|exposed/);
  // the API's own after_gap wins when present; no "after" levels, no verdict
  assert.equal(afterGap({ ...n, after_gap: -3 }), -3);
  assert.equal(afterGap({ divergence: -145.8 }), null);
  assert.equal(verdictOf(afterGap({ after: { this: 10, neighbor: null } }), BARE), null);
});

test("a comparison is read only as far as its interval allows", () => {
  const e = { a: "alfalfa/perennial", b: "continuous corn", effect: -19, ci_low: -31.3, ci_high: -6.8, n_pairs: 85, p_value: 0.008, ci_excludes_zero: true };
  // negative effect on bare days: they moved lower under rotation A (a change, not a level)
  assert.equal(effectReading(e, BARE), "bare days moved lower on alfalfa / perennial fields than on their look-alikes");
  assert.equal(effectReading({ ...e, effect: 19, ci_low: 6.8, ci_high: 31.3 }, BARE), "bare days moved lower on continuous corn fields than on their look-alikes");
  assert.doesNotMatch(effectReading(e, BARE), /sat bare/);
  // the same negative number on a higher-is-better outcome favours B
  assert.equal(effectReading(e, OM), "continuous corn fields moved the better way against their look-alikes");
  assert.equal(effectReading({ ...e, ci_low: -31, ci_high: 4, ci_excludes_zero: false }, BARE), "not distinguishable from zero");
  assert.equal(effectReading({ ...e, effect: null }, BARE), "not distinguishable from zero");
});

test("chart scales are shared and a missing year breaks the line", () => {
  const a = [{ year: 2016, value: 1 }, { year: 2017, value: null }, { year: 2018, value: 2 }];
  const b = [{ year: 2016, value: 3 }, { year: 2018, value: 3 }];
  const g = chartGeometry([a, b], 100, 50, 0);
  assert.equal(g.min, 1);
  assert.equal(g.max, 3);
  assert.equal(g.x(2016), 0);
  assert.equal(g.x(2018), 100);
  assert.equal(g.y(3), 0);
  assert.equal((g.paths[0].match(/M/g) ?? []).length, 2);
  assert.equal(g.paths[1], "M0.0,0.0L100.0,0.0");
});

test("a flat or empty series still gets a usable range", () => {
  const g = chartGeometry([[{ year: 2020, value: 2 }]], 10, 10);
  assert.ok(g.max > g.min);
  assert.equal(chartGeometry([[]], 10, 10).paths[0], "");
});
