// Unit tests for the "Your neighbors" helpers (src/lib/neighbors.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import { chartGeometry, formatSigned, rotationLabel, sharedHistoryPct } from "../src/lib/neighbors.ts";

test("signed numbers use a real minus and never show -0.00", () => {
  assert.equal(formatSigned(0.1122), "+0.11");
  assert.equal(formatSigned(-0.05), "−0.05");
  assert.equal(formatSigned(-0.001), "0.00");
});

test("rotation labels read as words", () => {
  assert.equal(rotationLabel("alfalfa/perennial"), "Alfalfa / perennial");
  assert.equal(rotationLabel("wheat-sorghum-fallow"), "Wheat–sorghum–fallow");
});

test("shared history is a whole percent", () => {
  assert.equal(sharedHistoryPct(0), 100);
  assert.equal(sharedHistoryPct(0.125), 88);
  assert.equal(sharedHistoryPct(null), null);
});

test("chart scales are shared and a missing year breaks the line", () => {
  const a = [{ year: 2008, value: 1 }, { year: 2009, value: null }, { year: 2010, value: 2 }];
  const b = [{ year: 2008, value: 3 }, { year: 2010, value: 3 }];
  const g = chartGeometry([a, b], 100, 50, 0);
  assert.equal(g.min, 1);
  assert.equal(g.max, 3);
  assert.equal(g.x(2008), 0);
  assert.equal(g.x(2010), 100);
  assert.equal(g.y(3), 0);
  assert.equal((g.paths[0].match(/M/g) ?? []).length, 2);
  assert.equal(g.paths[1], "M0.0,0.0L100.0,0.0");
});

test("a flat or empty series still gets a usable range", () => {
  const g = chartGeometry([[{ year: 2016, value: 2 }]], 10, 10);
  assert.ok(g.max > g.min);
  assert.equal(chartGeometry([[]], 10, 10).paths[0], "");
});
