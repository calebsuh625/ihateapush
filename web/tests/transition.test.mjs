// Unit tests for the "How to get there" helpers (src/lib/transition.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_CHOICES, plansQuery } from "../src/lib/planner.ts";
import {
  DEFAULT_SPAN,
  SPANS,
  columnGeometry,
  shortYear,
  signedDollars,
  signedRange,
  totalDomain,
  transitionQuery,
  zeroAt,
} from "../src/lib/transition.ts";

const settings = plansQuery(DEFAULT_CHOICES);

test("the spans offered are inside what the API takes, with eight years the default", () => {
  assert.deepEqual([...SPANS], [4, 6, 8, 10, 12]);
  assert.equal(DEFAULT_SPAN, 8);
  assert.ok(SPANS.includes(DEFAULT_SPAN));
});

test("the query names the card or the rotation, the years, then the plan cards' settings", () => {
  assert.equal(transitionQuery(settings, { plan: 2 }), `plan=2&years=8&${settings}`);
  assert.equal(transitionQuery(settings, { plan: 1 }, 12), `plan=1&years=12&${settings}`);
  assert.equal(transitionQuery("", { plan: 3 }, 4), "plan=3&years=4");
  assert.equal(
    transitionQuery(settings, { rotation: ["corn", "winter wheat", "fallow"] }, 6),
    `target=corn%2Cwinter%20wheat%2Cfallow&years=6&${settings}`,
  );
  // the same choices always give the same string: it is the request's identity
  assert.equal(transitionQuery(settings, { plan: 2 }, 8), transitionQuery(settings, { plan: 2 }));
});

test("nothing is asked for a plan that is not a card, an empty rotation or a half-filled one", () => {
  assert.equal(transitionQuery(settings, { plan: 0 }), null);
  assert.equal(transitionQuery(settings, { plan: 4 }), null);
  assert.equal(transitionQuery(settings, { plan: 1.5 }), null);
  assert.equal(transitionQuery(settings, { rotation: [] }), null);
  assert.equal(transitionQuery(settings, { rotation: ["corn", ""] }), null);
});

test("a span the control does not offer falls back to eight years", () => {
  assert.equal(transitionQuery(settings, { plan: 1 }, 7), `plan=1&years=8&${settings}`);
  assert.equal(transitionQuery(settings, { plan: 1 }, Number.NaN), `plan=1&years=8&${settings}`);
});

test("differences carry their sign, with a real minus", () => {
  assert.equal(signedDollars(45), "+$45");
  assert.equal(signedDollars(-45.2), "−$45");
  assert.equal(signedDollars(0.3), "$0");
  assert.equal(signedDollars(-0.4), "$0");
  assert.equal(signedDollars(1053), "+$1,053");
  assert.equal(signedDollars(Number.NaN), "no figure");
  assert.equal(signedRange(-136, -61), "−$136 to −$61");
  assert.equal(signedRange(-25, 13), "−$25 to +$13");
  assert.equal(signedRange(0, 0), null);
  assert.equal(signedRange(40.2, 39.8), null); // the same figure at the precision shown
});

test("the chart's scale always holds zero and never has zero height", () => {
  assert.deepEqual(totalDomain([{ low: -136, mid: -102, high: -61 }, { low: 157, mid: 181, high: 206 }]), [-136, 206]);
  assert.deepEqual(totalDomain([{ low: 44, mid: 51, high: 62 }]), [0, 62]);
  assert.deepEqual(totalDomain([{ low: -30, mid: -20, high: -10 }]), [-30, 0]);
  assert.deepEqual(totalDomain([{ low: 0, mid: 0, high: 0 }]), [-1, 1]);
  assert.deepEqual(totalDomain([]), [-1, 1]);
});

test("a year's bar runs from the lowest to the highest model, with the dot inside it", () => {
  const domain = [-100, 100];
  assert.deepEqual(columnGeometry({ low: -50, mid: 0, high: 50 }, domain), { bottom: 25, height: 50, mid: 50 });
  assert.equal(zeroAt(domain), 50);
  assert.equal(zeroAt([0, 62]), 0);
  assert.equal(zeroAt([-30, 0]), 100);
  // ahead: the dot is above the zero line; behind: below it
  assert.ok(columnGeometry({ low: 20, mid: 40, high: 59 }, domain).mid > zeroAt(domain));
  assert.ok(columnGeometry({ low: -90, mid: -60, high: -20 }, domain).mid < zeroAt(domain));
  // every model the same: a bar still tall enough to see, kept inside the plot, around the value
  const flat = columnGeometry({ low: 100, mid: 100, high: 100 }, domain);
  assert.equal(flat.height, 3);
  assert.equal(flat.bottom, 97);
  assert.equal(flat.mid, 100);
  const floor = columnGeometry({ low: -100, mid: -100, high: -100 }, domain);
  assert.equal(floor.bottom, 0);
  assert.equal(floor.mid, 0);
  // values outside the scale are held at its ends
  assert.deepEqual(columnGeometry({ low: -500, mid: 0, high: 500 }, domain), { bottom: 0, height: 100, mid: 50 });
});

test("year labels under the chart are two digits", () => {
  assert.equal(shortYear(2027), "’27");
  assert.equal(shortYear(2030), "’30");
  assert.equal(shortYear(2105), "’05");
});
