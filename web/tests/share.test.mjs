// Unit tests for the share link (src/lib/share.ts) and the CSV export (src/lib/exportplan.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_WEIGHTS, HORIZONS, DEFAULT_CHOICES, plansQuery } from "../src/lib/planner.ts";
import { OWN_CROPS } from "../src/lib/custom.ts";
import {
  SHARE_CROPS, SHARE_DEFAULT_WEIGHTS, SHARE_HORIZONS, decodeShare, emptyShare, encodeShare, splitHash,
} from "../src/lib/share.ts";
import { asciiText, csvComment, csvNumber, csvText, farmCsv, plansCsv, settingsLine, csvFileName } from "../src/lib/exportplan.ts";

test("share defaults are the planner's", () => {
  assert.deepEqual(SHARE_DEFAULT_WEIGHTS, DEFAULT_WEIGHTS);
  assert.deepEqual([...SHARE_HORIZONS], [...HORIZONS]);
  assert.deepEqual([...SHARE_CROPS], [...OWN_CROPS]);
});

test("the old hash form still names the field, and has no settings", () => {
  const { state, hasParams } = decodeShare("#KS-014376");
  assert.equal(state.field, "KS-014376");
  assert.equal(hasParams, false);
  assert.equal(encodeShare(state), "KS-014376");
  assert.deepEqual(splitHash("#KS-1?w=1.2.3.4"), { field: "KS-1", query: "w=1.2.3.4" });
  assert.deepEqual(splitHash(""), { field: null, query: null });
  assert.deepEqual(splitHash("#?m=farm"), { field: null, query: "m=farm" });
});

test("nothing open and nothing changed is an empty hash", () => {
  assert.equal(encodeShare(emptyShare()), "");
});

test("a full state round-trips", () => {
  const s = emptyShare();
  s.field = "KS-014376";
  s.choices = { weights: { soil: 1, water: 2, money: 5, risk: 0 }, horizon: 2040, scenario: "ssp585", limitIn: 12.5, pricePct: -20, costPct: 10 };
  s.irrigation = false;
  s.own = ["corn", null, "winter wheat", "fallow"];
  s.layer = "bare_days";
  s.year = 2022;
  const text = encodeShare(s);
  assert.match(text, /^KS-014376\?/);
  assert.ok(!text.includes("%2C"), "commas are left readable");
  const back = decodeShare(`#${text}`);
  assert.equal(back.hasParams, true);
  assert.deepEqual(back.state, s);
});

test("a farm round-trips and limits are kept as typed", () => {
  const s = emptyShare();
  s.farmMode = true;
  s.farmIds = ["KS-000001", "KS-000002"];
  s.farmWater = "900";
  s.farmMinimum = "20000.5";
  const back = decodeShare(`#${encodeShare(s)}`).state;
  assert.equal(back.farmMode, true);
  assert.deepEqual(back.farmIds, s.farmIds);
  assert.equal(back.farmWater, "900");
  assert.equal(back.farmMinimum, "20000.5");
  assert.equal(back.field, null);
});

test("defaults are left out and nonsense is ignored", () => {
  const s = emptyShare();
  s.field = "KS-1";
  assert.equal(encodeShare(s), "KS-1");
  const bad = decodeShare("#KS-1?w=1.2&h=1999&s=nope&lim=-3&pp=abc&cp=500&irr=maybe&own=corn,potato&layer=a%20b&yr=x&fw=1e9x");
  assert.equal(bad.hasParams, true);
  assert.deepEqual(bad.state.choices.weights, SHARE_DEFAULT_WEIGHTS);
  assert.equal(bad.state.choices.horizon, 2025);
  assert.equal(bad.state.choices.scenario, "ssp245");
  assert.equal(bad.state.choices.limitIn, null);
  assert.equal(bad.state.choices.pricePct, 0);
  assert.equal(bad.state.choices.costPct, 100, "out-of-range percentages are clamped");
  assert.equal(bad.state.irrigation, null);
  assert.equal(bad.state.own, null);
  assert.equal(bad.state.layer, null);
  assert.equal(bad.state.farmWater, "");
});

test("the link's settings give the planner the same query", () => {
  const s = emptyShare();
  s.field = "KS-1";
  s.choices.horizon = 2050;
  s.choices.pricePct = 15;
  const back = decodeShare(`#${encodeShare(s)}`).state.choices;
  assert.equal(plansQuery({ ...DEFAULT_CHOICES, ...back }), plansQuery({ ...DEFAULT_CHOICES, horizon: 2050, pricePct: 15 }));
});

test("CSV text is plain, quoted when needed, and safe against formulas", () => {
  assert.equal(asciiText("−5% – corn × 2"), "-5% - corn x 2");
  assert.equal(csvText('a, "b"'), '"a, ""b"""');
  assert.equal(csvText("=SUM(A1)"), "'=SUM(A1)");
  assert.equal(csvNumber(0.7849, 2), "0.78");
  assert.equal(csvNumber(-23.6), "-24");
  assert.equal(csvNumber(null), "");
  assert.equal(csvComment("a, b"), "# a; b");
  assert.equal(csvFileName("plans", "KS-1/2"), "rotation-plans-KS-1_2.csv");
});

const plan = (name, rotation, extra = {}) => ({
  name, rotation, cycle: rotation.slice(0, 3), reason: "Because really.",
  scores: { soil: 0.784, water: 0.639, money: 0.773, risk: 0.979, overall: 0.79, robust: 0.78 },
  futures: { holds: 5, total: 6 }, rank: 4,
  spread: { profit_per_acre: { low: 321, mid: 356, high: 410 } },
  numbers: { bare_days: 43.5, irrigation_mm: 361, irrigation_in: 14.2, water_met_share: 0.933, profit_per_acre: 364, failed_share: 0.021 },
  ...extra,
});
const settings = {
  weights: { soil: 0.3, water: 0.2, money: 0.3, risk: 0.2 }, scenario: "ssp585", horizon: 2040, window: [2030, 2049],
  irrigated: true, irrigation_source: "guess", irrigation_limit_in: 18,
  what_if: { active: true, price_pct: -10, cost_pct: 5, crop_price_pct: {}, note: null },
};

test("the plans CSV: comment rows, one header, a row per plan", () => {
  const text = plansCsv({ fieldId: "KS-1", areaAcres: 120.4, settings, plans: [plan("A – B", ["corn", "soybeans", "corn", "soybeans"]), plan("C", ["fallow", "corn", "corn", "corn"]), plan("D", ["corn", "corn", "corn", "corn"])], current: plan("Now", ["corn", "corn", "corn", "corn"]), candidates: 115 });
  const lines = text.trim().split("\r\n");
  const comments = lines.filter((l) => l.startsWith("#"));
  assert.ok(comments.length >= 3);
  assert.ok(comments.every((l) => !l.includes(",")), "comment rows have no commas");
  assert.ok(comments.some((l) => /soil protection/i.test(l)) && comments.some((l) => /not profit/.test(l)));
  assert.ok(comments.some((l) => /2040/.test(l) && /high emissions/.test(l) && /prices -10%/.test(l)));
  const rows = lines.filter((l) => !l.startsWith("#"));
  assert.equal(rows.length, 1 + 3 + 1);
  assert.ok(rows[0].startsWith("rank,name,"));
  const first = rows[1].split(",");
  assert.equal(first[0], "1");
  assert.equal(first[1], "A - B");
  assert.equal(first[8], "0.78"); // ranking score
  assert.equal(first[13], "5");
  assert.equal(first[15], "321");
  assert.equal(first[19], "14.2");
  assert.equal(first[20], "93");
  assert.equal(rows[4].split(",")[0], "as now");
  assert.equal(rows[1].split(",").length, rows[0].split(",").length);
});

test("a dryland field has no water-pumped figure", () => {
  const dry = { ...settings, irrigated: false };
  const text = plansCsv({ fieldId: "KS-2", areaAcres: null, settings: dry, plans: [plan("X", ["corn", "corn", "corn", "corn"])] });
  const row = text.trim().split("\r\n").filter((l) => !l.startsWith("#"))[1].split(",");
  assert.equal(row[19], "");
  assert.equal(row[20], "93");
  assert.match(settingsLine(dry), /Dryland/);
});

test("the farm CSV has a row per field and the settings in comments", () => {
  const years = Array.from({ length: 12 }, (_, i) => (i % 2 ? "soybeans" : "corn"));
  const field = (id, same) => ({
    field_id: id, area_acres: 80.25, area_ha: 32, irrigated: true, irrigation_source: "guess",
    plan: { ...plan("Corn and soybeans", ["corn", "soybeans"]), start_year: 2026, years }, own_pick: plan("Own", ["corn"]),
    same_rotation: same, same_start: same, differs: !same, gives_up: null, sentence: "Keeps it.",
  });
  const answer = {
    settings: { weights: { soil: 0.3, water: 0.2, money: 0.3, risk: 0.2 }, scenario: "ssp245", horizon: 2025, window: [2015, 2034], models: [], span_years: 12, water_budget_acre_in: 900, min_income: null, max_fields: 100 },
    farm: { fields: 2, area_acres: 160.5, area_ha: 64, irrigated_fields: 2, irrigated_acres: 160, dryland_fields: 0, dryland_acres: 0 },
    status: "limits_met", plan: { average: { water_acre_in: 500, return: 12345.6 }, peak_water: { n: 3, value: 880 }, worst_return: { n: 5, value: -200 } },
    fields: [field("KS-1", true), field("KS-2", false)],
  };
  const lines = farmCsv(answer).trim().split("\r\n");
  const rows = lines.filter((l) => !l.startsWith("#"));
  assert.equal(rows.length, 3);
  assert.ok(lines.filter((l) => l.startsWith("#")).some((l) => /water budget 900 acre-inches/.test(l) && /no minimum return/.test(l)));
  assert.ok(lines.some((l) => /average return \$12346/.test(l) && /leanest year -\$200/.test(l)));
  assert.ok(rows[0].includes("year_12"));
  assert.ok(rows[2].includes("different rotation"));
  assert.ok(rows[1].startsWith("KS-1,80.3,irrigated,own first pick,"));
});
