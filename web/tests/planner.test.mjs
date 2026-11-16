// Unit tests for the planner helpers (src/lib/planner.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DEFAULT_CHOICES,
  HORIZONS,
  climateLine,
  climateRange,
  formatChange,
  formatDollars,
  formatInches,
  formatMm,
  formatShare,
  holdsText,
  holdsTone,
  horizonAt,
  horizonIndex,
  horizonLabel,
  irrigationReason,
  isDefaultWeights,
  placeText,
  plansQuery,
  scenarioLabel,
  spreadDomain,
  spreadGeometry,
  wellQualifier,
  yieldChangeLine,
  yieldChangeRange,
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

const FUTURE = {
  label: "+2.0°C, models disagree on summer rain, crops need 8% more water, 26 more days above 35°C (95°F)",
  changes: { summer_rain_agree: 3, models: 6 },
  range: { summer_rain_pct: [-31.6, 13.3], tmean_c: [1.32, 3.26] },
  window: [2050, 2060],
  models: 6,
  yield_change: {
    corn: { pct: -40, low: -56, high: -25 },
    "winter wheat": { pct: -17, low: -28, high: -11 },
    sorghum: { pct: -25, low: -36, high: -12 },
  },
};
const TODAY = {
  label: "Today's climate (model average, 2015-2034)",
  changes: { summer_rain_agree: 6, models: 6 },
  range: { summer_rain_pct: [0, 0], tmean_c: [0, 0] },
  window: [2015, 2034],
  models: 6,
  yield_change: { corn: { pct: 0, low: 0, high: 0 }, sorghum: { pct: 0, low: 0, high: 0 } },
};

test("the climate line is the API's label as is, with no second rain sentence", () => {
  const line = climateLine(FUTURE, 2060);
  assert.equal(line, `${FUTURE.label}. Compared with today; average of 2050–2060 across 6 climate models.`);
  assert.equal(line.startsWith(FUTURE.label), true);
  // rain is named once, by the label
  assert.equal(line.match(/rain/g).length, 1);
  assert.equal(/do not agree/.test(line), false);
  const agreed = { ...FUTURE, label: "+0.4°C, 12% more summer rain, 3 more days above 35°C (95°F)", window: [2020, 2039] };
  assert.equal(climateLine(agreed, 2030), `${agreed.label}. Compared with today; average of 2020–2039 across 6 climate models.`);
  assert.equal(climateLine(TODAY, 2025), "Today's climate (model average, 2015-2034), from 6 climate models.");
  assert.equal(climateLine({ label: "Warmer" }, 2040), "Warmer. Compared with today; average across climate models.");
});

test("the range across the models is small print, and absent at Today", () => {
  assert.equal(climateRange(FUTURE, 2060), "Range across the models: +1.3 to +3.3°C; summer rain from 32% less to 13% more.");
  assert.equal(climateRange({ ...FUTURE, range: { summer_rain_pct: [-0.3, 31.1] } }, 2040), "Range across the models: summer rain from no change to 31% more.");
  assert.equal(climateRange(TODAY, 2025), null);
  assert.equal(climateRange({ label: "x" }, 2040), null);
});

test("yield changes are listed hardest hit first, and hidden at Today", () => {
  assert.equal(yieldChangeLine(FUTURE.yield_change, false, 2060), "Dryland yields in this future: corn −40%, sorghum −25%, winter wheat −17%");
  assert.equal(
    yieldChangeLine({ corn: { pct: -6, low: -9, high: 0 }, alfalfa: { pct: 2, low: -1, high: 4 }, soybeans: { pct: 0, low: -2, high: 3 } }, true, 2040),
    "Irrigated yields in this future: corn −6%, soybeans no change, alfalfa +2%",
  );
  assert.equal(yieldChangeLine(TODAY.yield_change, false, 2025), null);
  assert.equal(yieldChangeLine(undefined, false, 2060), null);
  assert.equal(yieldChangeLine({}, false, 2060), null);
  assert.equal(yieldChangeRange(FUTURE.yield_change), "Lowest to highest model: corn −56% to −25%; sorghum −36% to −12%; winter wheat −28% to −11%.");
  assert.equal(yieldChangeRange(null), null);
  assert.equal(formatChange(-41.6), "−42%");
  assert.equal(formatChange(0.3), "no change");
});

test("cards 2 and 3 say where they sit in the full list", () => {
  assert.equal(placeText(7, 115, 2), "Number 7 of 115, the next different choice");
  assert.equal(placeText(2, 17, 2), "Number 2 of 17, the next different choice");
  assert.equal(placeText(9, undefined, 3), "Number 9, the next different choice");
  assert.equal(placeText(1, 115, 1), null);
  assert.equal(placeText(undefined, 115, 2), null);
});

test("water is in inches, with mm kept for the title", () => {
  assert.equal(formatInches(18.3, 465), "18.3 in");
  assert.equal(formatInches(undefined, 465), "18.3 in");
  assert.equal(formatInches(24, 609.6), "24 in");
  assert.equal(formatInches(undefined, undefined), "no figure");
  assert.equal(formatMm(464.6), "465 mm");
});

test("irrigated holds-up badges say what they take for granted", () => {
  assert.equal(wellQualifier(24.0), "if the well delivers up to 24 inches a year");
  assert.equal(wellQualifier(19.7), "if the well delivers up to 19.7 inches a year");
  assert.equal(wellQualifier(null), "if the well delivers all the crops need");
  assert.equal(wellQualifier(undefined), "if the well delivers all the crops need");
});

test("the irrigation reason says whose call it is", () => {
  assert.equal(irrigationReason("guess", "fallow in 4 of 17 years"), "our guess: fallow in 4 of 17 years");
  assert.equal(irrigationReason("guess", null), "our guess from the crop history");
  assert.equal(irrigationReason("override", "set by the request"), "you set this");
});

// --- Price and cost what-if (Phase 5, item F) ---
import { breakEvenLine, isDefaultPriceCost, signedPercent } from "../src/lib/planner.ts";

test("plansQuery sends price_pct and cost_pct only when they are not zero", () => {
  const base = plansQuery(DEFAULT_CHOICES);
  assert.equal(plansQuery({ ...DEFAULT_CHOICES, pricePct: 0, costPct: 0 }), base);
  assert.equal(plansQuery({ ...DEFAULT_CHOICES, pricePct: -20 }), `${base}&price_pct=-20`);
  assert.equal(plansQuery({ ...DEFAULT_CHOICES, pricePct: 10, costPct: 15 }), `${base}&price_pct=10&cost_pct=15`);
  assert.equal(plansQuery({ ...DEFAULT_CHOICES, pricePct: 999, costPct: -999 }), `${base}&price_pct=100&cost_pct=-50`);
});

test("isDefaultPriceCost and signedPercent", () => {
  assert.equal(isDefaultPriceCost(DEFAULT_CHOICES), true);
  assert.equal(isDefaultPriceCost({ pricePct: 5 }), false);
  assert.equal(signedPercent(20), "+20%");
  assert.equal(signedPercent(-30), "−30%");
  assert.equal(signedPercent(0), "0%");
});

test("breakEvenLine is the API's sentence, or nothing", () => {
  assert.equal(breakEvenLine(null), null);
  assert.equal(breakEvenLine(undefined), null);
  assert.equal(breakEvenLine({ sentence: "Plan 1 stays above its direct costs." }), "Plan 1 stays above its direct costs.");
});
