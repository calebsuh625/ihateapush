// Unit tests for the twin view's pure helpers (src/lib/twin.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import { agreementShares, differenceAxis, lineChart, niceTicks, readingIn, sheetHeight, signed, yearRange } from "../src/lib/twin.ts";

const YEARS = [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024];
const series = (values) => YEARS.map((year, i) => ({ year, value: values[i] ?? null }));

test("niceTicks: round steps that cover the range", () => {
  const t = niceTicks(0, 285.2, 4);
  assert.deepEqual(t.ticks, [0, 100, 200, 300]);
  assert.equal(t.low, 0);
  assert.equal(t.high, 300);
  const d = niceTicks(-150, 60, 5);
  assert.ok(d.ticks.includes(0) && d.low <= -150 && d.high >= 60);
  assert.ok(niceTicks(0, 0, 4).ticks.length >= 2, "a flat range still gets an axis");
});

test("lineChart: starts at zero, keeps gaps as gaps, marks the cut-off between 2019 and 2020", () => {
  const a = series([174.2, 103.1, null, 285.2, 183.1, 143.2, 253.1, 75.6, 134.4]);
  const b = series([151.8, 145.5, 46.3, 60.6, 167.2, 9.4, 213.3, 242.2, 32.7]);
  const c = lineChart([a, b], YEARS, 2020);
  assert.equal(c.yTicks[0], 0);
  assert.ok(c.yTicks.at(-1) >= 285.2);
  assert.equal(c.y(0), c.plot.bottom);
  assert.equal(c.dots[0].length, 8, "the missing year has no marker");
  assert.equal((c.paths[0].match(/M/g) ?? []).length, 2, "the missing year breaks the line in two");
  assert.equal((c.paths[1].match(/M/g) ?? []).length, 1);
  assert.ok(Math.abs(c.cutX - (c.x(2019) + c.x(2020)) / 2) < 1e-9);
  assert.equal(c.afterX[1], c.plot.right);
  assert.ok(c.x(2016) < c.x(2024) && c.y(285.2) < c.y(75.6));
});

test("lineChart: no cut-off when the switch year is outside the series; all-gap series is empty", () => {
  const c = lineChart([series([]), series([])], YEARS, 2030);
  assert.equal(c.cutX, null);
  assert.equal(c.paths[0], "");
  assert.equal(c.dots[1].length, 0);
});

test("differenceAxis always contains zero and every value, and ignores nulls", () => {
  const ax = differenceAxis([-145.8, 5.7, -1.7, 12, -37.1, 46.7, null, undefined]);
  assert.ok(ax.low <= -145.8 && ax.high >= 46.7);
  assert.ok(ax.ticks.includes(0));
  assert.ok(ax.x(-145.8) < ax.zeroX && ax.zeroX < ax.x(46.7));
  assert.ok(ax.x(ax.low) >= 0 && ax.x(ax.high) <= ax.width);
  const only = differenceAxis([null]);
  assert.ok(only.low < 0 && only.high > 0, "with no values the axis still straddles zero");
  const positive = differenceAxis([20, 30]);
  assert.ok(positive.low <= 0, "zero is on the axis even when every value is positive");
});

test("agreementShares add up to one; no other pairs gives zeros", () => {
  const s = agreementShares({ other_pairs: 12, same_way: 6, opposite_way: 5, no_change: 1 });
  assert.ok(Math.abs(s.same + s.opposite + s.none - 1) < 1e-12);
  assert.equal(s.same, 0.5);
  assert.deepEqual(agreementShares({ other_pairs: 0, same_way: 0, opposite_way: 0, no_change: 0 }), { same: 0, opposite: 0, none: 0 });
});

test("signed uses a real minus, one decimal, and never -0.0", () => {
  assert.equal(signed(-145.84), "−145.8");
  assert.equal(signed(5.66), "+5.7");
  assert.equal(signed(-0.04), "0.0");
  assert.equal(signed(null), "?");
  assert.equal(signed(-50, 0), "−50");
});

test("yearRange covers both histories; readingIn gives null for gaps and unknown years", () => {
  assert.deepEqual(yearRange({ 2010: "corn", 2012: "corn" }, { 2011: "fallow", 2013: "corn" }), [2010, 2011, 2012, 2013]);
  assert.deepEqual(yearRange({}, {}), []);
  const s = series([1, null, 3]);
  assert.equal(readingIn(s, 2016), 1);
  assert.equal(readingIn(s, 2017), null);
  assert.equal(readingIn(s, 1999), null);
});

test("sheetHeight leaves the map more than a third of the screen and stops growing at 600px", () => {
  assert.equal(sheetHeight(900), 558);
  assert.equal(sheetHeight(844), 523);
  assert.equal(sheetHeight(2000), 600);
});
