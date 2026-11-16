// Unit tests for the weather-replay helpers (src/lib/replay.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_CHOICES, plansQuery } from "../src/lib/planner.ts";
import { CHART, barGeometry, labelledYears, niceStep, replayQuery, returnDomain, shortYear, signedDollars, ticks, yOf, yearTitle } from "../src/lib/replay.ts";

const settings = plansQuery(DEFAULT_CHOICES);
const year = (y, value, extra = {}) => ({
  year: y,
  dry: false,
  worst_rank: null,
  crop: "corn",
  return: value,
  low: value,
  high: value,
  yield_pct: 100,
  yield: 1,
  fail_chance: 0,
  failed: false,
  failed_phases: 0,
  water_in: null,
  water_met: 1,
  below_cost: value < 0,
  crops_by_phase: [],
  returns_by_phase: [],
  ...extra,
});

test("the query names the plan or the rotation, then the plan cards' settings", () => {
  assert.equal(replayQuery(settings, { plan: 2 }), `plan=2${settings ? `&${settings}` : ""}`);
  assert.equal(replayQuery("", { plan: 1 }), "plan=1");
  assert.equal(replayQuery("", { rotation: ["corn", "winter wheat"] }), "rotation=corn%2Cwinter%20wheat");
  assert.equal(replayQuery(settings, { plan: 4 }), null);
  assert.equal(replayQuery(settings, { plan: 0 }), null);
  assert.equal(replayQuery(settings, { rotation: [] }), null);
  assert.equal(replayQuery(settings, { rotation: ["corn", " "] }), null);
});

test("nice steps keep a chart to a handful of round gridlines", () => {
  assert.equal(niceStep(500), 100);
  assert.equal(niceStep(300), 100);
  assert.equal(niceStep(100), 20);
  assert.equal(niceStep(1100), 250);
  assert.equal(niceStep(0), 1);
  assert.equal(niceStep(Number.NaN), 1);
});

test("the dollar scale always holds zero, every bar, its low-high line and the carry-on ticks", () => {
  const rows = [year(2000, 120, { low: 80, high: 150 }), year(2001, -60, { low: -90, high: -20 })];
  const [d0, d1] = returnDomain(rows, [210]);
  assert.ok(d0 <= -90 && d1 >= 210 && d0 <= 0 && d1 >= 0);
  assert.deepEqual(returnDomain([year(2000, 0)]), [-10, 10]);
  const [a, b] = returnDomain([year(2000, 50), year(2001, 120)]);
  assert.equal(a, 0);
  assert.ok(b >= 120);
});

test("gridlines include zero and stay inside the domain", () => {
  const domain = returnDomain([year(2000, -80), year(2001, 430)]);
  const t = ticks(domain);
  assert.ok(t.includes(0));
  assert.ok(t.every((v) => v >= domain[0] - 1e-9 && v <= domain[1] + 1e-9));
  assert.ok(t.length >= 3 && t.length <= 8);
});

test("a higher value is higher on the chart and values outside the scale are held at its edge", () => {
  const domain = [-100, 400];
  assert.ok(yOf(300, domain) < yOf(0, domain));
  assert.ok(yOf(0, domain) < yOf(-100, domain));
  assert.equal(yOf(9999, domain), CHART.top);
  assert.equal(yOf(-9999, domain), CHART.height - CHART.bottom);
});

test("a bar runs from the zero line to its value, down for a loss, with a minimum height", () => {
  const domain = [-100, 400];
  const up = barGeometry(0, 25, 200, domain);
  const down = barGeometry(1, 25, -50, domain);
  const flat = barGeometry(2, 25, 0, domain);
  const zero = yOf(0, domain);
  assert.ok(Math.abs(up.y + up.height - zero) < 1e-9, "a gain stands on the zero line");
  assert.ok(Math.abs(down.y - zero) < 1e-9, "a loss hangs from it");
  assert.ok(flat.height >= 1.5, "a return of zero is still a mark");
  assert.ok(up.barX >= up.x && up.barX + up.barWidth <= up.x + up.band, "the bar sits inside its column");
  assert.ok(Math.abs(down.x - up.x - up.band) < 1e-9, "columns are side by side");
  const last = barGeometry(24, 25, 10, domain);
  assert.ok(last.x + last.band <= CHART.width - CHART.right + 1e-9, "the last column ends inside the frame");
});

test("every fourth year is labelled, so the first and last of a 25-year record are", () => {
  const at = labelledYears(25);
  assert.deepEqual(at.slice(0, 3), [0, 4, 8]);
  assert.equal(at[at.length - 1], 24);
  assert.equal(shortYear(2004), "’04");
  assert.equal(shortYear(2012), "’12");
});

test("signed dollars use a real minus sign and no negative zero", () => {
  assert.equal(signedDollars(45.4), "+$45");
  assert.equal(signedDollars(-45.6), "−$46");
  assert.equal(signedDollars(0.3), "$0");
  assert.equal(signedDollars(Number.NaN), "no figure");
});

test("a bar's title says the year, whether it was dry, the crop, the return and a failed crop", () => {
  const dollars = (v) => `$${v}`;
  const t = yearTitle(year(2012, -86, { dry: true, failed: true, yield_pct: 41, water_in: 24, water_met: 0.7, low: -100, high: 20 }), dollars, (c) => c.toUpperCase());
  assert.match(t, /^2012 \(dry growing season\), CORN, return \$-86 an acre/);
  assert.match(t, /\$-100 to \$20 over the starting years/);
  assert.match(t, /yield 41% of normal/);
  assert.match(t, /crop failed/);
  assert.match(t, /24\.0 inches pumped/);
  assert.match(t, /70% of water need met/);
  assert.doesNotMatch(yearTitle(year(2001, 5), dollars, (c) => c), /dry|failed/);
});
