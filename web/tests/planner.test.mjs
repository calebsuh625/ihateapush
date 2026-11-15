// Unit tests for the planner helpers (src/lib/planner.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DEFAULT_CHOICES,
  HORIZONS,
  climateLine,
  formatDollars,
  formatShare,
  holdsText,
  holdsTone,
  horizonAt,
  horizonIndex,
  horizonLabel,
  irrigationReason,
  isDefaultWeights,
  plansQuery,
  rainDisagreement,
  scenarioLabel,
  spreadDomain,
  spreadGeometry,
} from "../src/lib/planner.ts";

test("the default query spells out the API defaults, in a fixed order", () => {
  assert.equal(plansQuery(DEFAULT_CHOICES), "soil=3&water=2&money=3&risk=2&scenario=ssp245&horizon=2025");
  assert.equal(isDefaultWeights(DEFAULT_CHOICES.weights), true);
});

test("the query carries weights, future and the irrigation override only when set", () => {
  const choices = { weights: { soil: 1, water: 10, money: 1, risk: 1 }, horizon: 2060, scenario: "ssp585" };
  assert.equal(plansQuery(choices), "soil=1&water=10&money=1&risk=1&scenario=ssp585&horizon=2060");
  assert.equal(plansQuery(choices, true), "soil=1&water=10&money=1&risk=1&scenario=ssp585&horizon=2060&irrigated=true");
  assert.equal(plansQuery(choices, false).endsWith("&irrigated=false"), true);
  assert.equal(isDefaultWeights(choices.weights), false);
});

test("the query never sends values the API would refuse", () => {
  const odd = { weights: { soil: -4, water: 99, money: 2.6, risk: Number.NaN }, horizon: 2043, scenario: "nonsense" };
  assert.equal(plansQuery(odd), "soil=0&water=10&money=3&risk=0&scenario=ssp245&horizon=2045");
});

test("the future slider maps positions to Today, 2030 ... 2060 and back", () => {
  assert.deepEqual(HORIZONS.map((_, i) => horizonAt(i)), [2025, 2030, 2035, 2040, 2045, 2050, 2055, 2060]);
  assert.equal(horizonAt(-3), 2025);
  assert.equal(horizonAt(99), 2060);
  assert.equal(horizonAt(Number.NaN), 2025);
  for (const [i, year] of HORIZONS.entries()) assert.equal(horizonIndex(year), i);
  assert.equal(horizonIndex(2041), 3);
  assert.equal(horizonIndex(1990), 0);
  assert.equal(horizonLabel(2025), "Today");
  assert.equal(horizonLabel(2060), "2060");
  assert.equal(scenarioLabel("ssp245"), "Middle road");
  assert.equal(scenarioLabel("ssp585"), "High emissions");
});

test("holds-up tone follows the share of futures", () => {
  assert.equal(holdsTone(6, 6), "good");
  assert.equal(holdsTone(5, 6), "good");
  assert.equal(holdsTone(4, 6), "mixed");
  assert.equal(holdsTone(3, 6), "mixed");
  assert.equal(holdsTone(2, 6), "poor");
  assert.equal(holdsTone(0, 6), "poor");
  assert.equal(holdsTone(0, 0), "poor");
  assert.equal(holdsText(4, 6), "Holds up in 4 of 6 futures");
});

test("the spread scale always includes zero and covers every plan", () => {
  assert.deepEqual(spreadDomain([{ low: 8, mid: 33, high: 68 }, { low: 11, mid: 51, high: 82 }]), [0, 82]);
  assert.deepEqual(spreadDomain([{ low: -62, mid: -21, high: 3 }, null, { low: -45, mid: -34, high: -18 }]), [-62, 3]);
  assert.deepEqual(spreadDomain([{ low: -45, mid: -34, high: -18 }]), [-45, 0]);
  assert.deepEqual(spreadDomain([]), [0, 1]);
  assert.deepEqual(spreadDomain([{ low: 0, mid: 0, high: 0 }]), [0, 1]);
});

test("spread geometry places low, middle, high and zero in percent", () => {
  const g = spreadGeometry({ low: 25, mid: 50, high: 100 }, [0, 100]);
  assert.deepEqual(g, { left: 25, width: 75, mid: 50, zero: 0 });
  const loss = spreadGeometry({ low: -60, mid: -20, high: 20 }, [-60, 40]);
  assert.equal(loss.left, 0);
  assert.equal(loss.width, 80);
  assert.equal(loss.mid, 40);
  assert.equal(loss.zero, 60);
});

test("spread geometry stays visible and inside the strip", () => {
  const point = spreadGeometry({ low: 100, mid: 100, high: 100 }, [0, 100]);
  assert.equal(point.width, 2);
  assert.equal(point.left, 98);
  assert.equal(point.mid >= point.left && point.mid <= point.left + point.width, true);
  const outside = spreadGeometry({ low: -50, mid: 500, high: 900 }, [0, 100]);
  assert.deepEqual([outside.left, outside.width, outside.mid], [0, 100, 100]);
  const flat = spreadGeometry({ low: 1, mid: 2, high: 3 }, [5, 5]);
  assert.equal(flat.zero, null);
  assert.equal(flat.width, 2);
  // a reversed pair is drawn the right way round
  assert.deepEqual(spreadGeometry({ low: 80, mid: 50, high: 20 }, [0, 100]).left, 20);
  // zero outside the scale is not drawn
  assert.equal(spreadGeometry({ low: 10, mid: 20, high: 30 }, [10, 30]).zero, null);
});

test("dollars and shares read plainly", () => {
  assert.equal(formatDollars(48), "$48");
  assert.equal(formatDollars(-24.4), "−$24");
  assert.equal(formatDollars(-0.2), "$0");
  assert.equal(formatDollars(1234.5), "$1,235");
  assert.equal(formatShare(0.363), "36%");
  assert.equal(formatShare(1), "100%");
  assert.equal(formatShare(0.004), "0%");
});

test("rain disagreement is said only when the models really split", () => {
  assert.equal(
    rainDisagreement({ summer_rain_agree: 3, models: 6 }, [-33.2, 10.0]),
    "the models do not agree on summer rain (from 33% less to 10% more)",
  );
  assert.equal(
    rainDisagreement({ summer_rain_agree: 4, models: 6 }, [-0.3, 31.1]),
    "the models do not agree on summer rain (from no change to 31% more)",
  );
  // 5 of 6 agree: the API's label already states the change
  assert.equal(rainDisagreement({ summer_rain_agree: 5, models: 6 }, [-7.5, 14.2]), null);
  // every model on the same side
  assert.equal(rainDisagreement({ summer_rain_agree: 2, models: 6 }, [2, 14]), null);
  // today: no change at all
  assert.equal(rainDisagreement({ summer_rain_agree: 0, models: 6 }, [0, 0]), null);
  assert.equal(rainDisagreement(undefined, [-5, 5]), null);
  assert.equal(rainDisagreement({ summer_rain_agree: 1, models: 6 }, undefined), null);
});

test("the climate line names the change, the years and the models", () => {
  const future = {
    label: "+1.9°C, about the same summer rain, 25 more 35°C+ days",
    changes: { summer_rain_agree: 3, models: 6 },
    range: { summer_rain_pct: [-33.2, 10.0] },
    window: [2050, 2060],
    models: 6,
  };
  assert.equal(
    climateLine(future, 2060),
    "+1.9°C, about the same summer rain, 25 more 35°C+ days than today (average of 2050–2060, 6 climate models). " +
      "The models do not agree on summer rain (from 33% less to 10% more).",
  );
  const agreed = { ...future, label: "+0.2°C, 4% more summer rain, 2 more 35°C+ days", changes: { summer_rain_agree: 5, models: 6 }, window: [2020, 2039] };
  assert.equal(climateLine(agreed, 2030), "+0.2°C, 4% more summer rain, 2 more 35°C+ days than today (average of 2020–2039, 6 climate models).");
  const today = { label: "Today's climate", changes: { summer_rain_agree: 0, models: 6 }, range: { summer_rain_pct: [0, 0] }, window: [2015, 2034], models: 6 };
  assert.equal(climateLine(today, 2025), "Today's climate (average of 2015–2034, 6 climate models).");
});

test("the irrigation reason says whose call it is", () => {
  assert.equal(irrigationReason("guess", "fallow in 4 of 17 years"), "our guess: fallow in 4 of 17 years");
  assert.equal(irrigationReason("guess", null), "our guess from the crop history");
  assert.equal(irrigationReason("override", "set by the request"), "you set this");
});
