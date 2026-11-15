// End-to-end checks of the Phase 4 features, against the running app.
//
//   npm run e2e:phase4          (with `make dev` running)
//
// Item 1, "Try your own rotation": fills the year slots by picking, moving, dragging and typing,
// and compares the side-by-side table, the marks and every sentence with what the API answers for
// the same rotation and settings (asked directly, never hard-coded). Screenshots go to
// e2e/screenshots/phase4-*.png.
//
// Item 2, "How to get there": opens the year-by-year path on a plan card and under the farmer's own
// rotation and compares the strip of years, the two sentences, the running-total chart, both tables
// and every list with a direct call to GET /field/{id}/transition for the same target and settings.
//
// Item 3, the whole-farm planner: POST /farm/plan is called from inside the page, through the same
// API prefix the app uses, and compared with a direct call and with each field's own
// GET /field/{id}/plans. Then the farm screen: "Plan a farm" on the map (clicks add and remove,
// a box and a freehand loop add every field whose centre is inside, Clear), and the farm panel,
// where every number and sentence is compared with a direct POST /farm/plan for the same request
// (no limits, limits met, limits that cannot be met, another future), plus the waiting state, the
// stale-answer rule, opening a field from a row, the refusal for too many fields and phone width.
//
// Environment: WEB_URL (default http://localhost:5173), E2E_TIMEOUT_MS (default 20000),
// DRY_FIELD (default KS-013847), IRRIGATED_FIELD (default KS-014376), PATH_FIELD (default
// KS-004139, a dryland field with four real years of the six crops), BRIDGE_FIELD (default
// KS-012807, irrigated with a young alfalfa stand), SETTLED_FIELD (default KS-004074, already in
// wheat-sorghum-fallow).
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { columnLabel, moveSlot, rangeText, scoreQuery, verdictLabel } from "../src/lib/custom.ts";
import {
  FIELD_TAGS, LIMIT_TAGS, addFields, farmRequest, fieldChange, formatAcres, formatFarmDollars, formatFarmScore, formatWhole, insideLoop,
  limitTone, metWords, rangeWords,
} from "../src/lib/farm.ts";
import { DEFAULT_CHOICES, formatDollars, formatInches, formatShare, plansQuery, wellQualifier } from "../src/lib/planner.ts";
import { DEFAULT_SPAN, SPANS, shortYear, signedDollars, signedRange, transitionQuery } from "../src/lib/transition.ts";

const WEB_URL = (process.env.WEB_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const TIMEOUT = Number.parseInt(process.env.E2E_TIMEOUT_MS ?? "20000", 10) || 20000;
const DRY = process.env.DRY_FIELD ?? "KS-013847";
const WET = process.env.IRRIGATED_FIELD ?? "KS-014376";
const PATH_FIELD = process.env.PATH_FIELD ?? "KS-004139";
const BRIDGE_FIELD = process.env.BRIDGE_FIELD ?? "KS-012807";
const SETTLED_FIELD = process.env.SETTLED_FIELD ?? "KS-004074";
const SHOTS = path.join(path.dirname(fileURLToPath(import.meta.url)), "screenshots");
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };
const C = "corn", B = "soybeans", W = "winter wheat", S = "sorghum", A = "alfalfa", F = "fallow";

class Failure extends Error {}
let checks = 0;
const check = (ok, message) => {
  checks += 1;
  if (!ok) throw new Failure(message);
};
const equal = (actual, expected, what) =>
  check(actual === expected, `${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
const log = (line) => console.log(line);
const score = (n) => (Math.round((n + Number.EPSILON) * 100) / 100).toFixed(2);
const squash = (s) => (s ?? "").replace(/\s+/g, " ").trim();

/** Console errors and uncaught errors fail the run; every /score request the app makes is kept. */
function watch(page, label) {
  const problems = [];
  const scoreRequests = [];
  const pathRequests = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location()?.url ?? "";
    if (/tile\.openstreetmap|openstreetmap\.org/.test(where)) return; // map tiles are an outside dependency
    problems.push(`${label}: console error: ${message.text()}${where ? ` (${where})` : ""}`);
  });
  page.on("pageerror", (error) => problems.push(`${label}: uncaught page error: ${error.message}`));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/\/field\/[^/]+\/score$/.test(url.pathname)) scoreRequests.push(url.pathname.split("/").at(-2) + "?" + url.search.slice(1));
    if (/\/field\/[^/]+\/transition$/.test(url.pathname)) pathRequests.push(url.pathname.split("/").at(-2) + "?" + url.search.slice(1));
  });
  return { problems, scoreRequests, pathRequests };
}

async function openField(page, fieldId) {
  await page.goto(`${WEB_URL}/#${fieldId}`, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await page.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${fieldId}"][data-state="ready"]`, { timeout: TIMEOUT });
  await page.waitForSelector('[data-testid="plans-region"][data-updating="false"]', { timeout: TIMEOUT });
}

const text = async (page, testId) => squash(await page.locator(`[data-testid="${testId}"]`).first().textContent());
const texts = async (page, selector) => (await page.locator(selector).allTextContents()).map(squash);

/** The crops in the year slots, as the page shows them ("" for an empty year). */
const slotsOnScreen = (page) => page.locator('[data-testid="own-slot"] select').evaluateAll((nodes) => nodes.map((node) => node.value));

async function pick(page, crops) {
  for (const [index, crop] of crops.entries()) await page.locator(`[data-testid="own-crop-${index}"]`).selectOption(crop);
}

/** Wait until the table on screen is the answer for exactly this query. */
async function scored(page, query, what) {
  try {
    await page.waitForSelector(`[data-testid="own-result"][data-state="ready"][data-updating="false"][data-query="${query}"]`, { timeout: TIMEOUT });
  } catch {
    const region = page.locator('[data-testid="own-result"]');
    const got = (await region.count()) ? `${await region.getAttribute("data-state")} / ${await region.getAttribute("data-query")}` : "(no result region)";
    throw new Failure(`${what}: the table never showed the answer for ${query} (showing ${got})`);
  }
}

/** What each table cell should read, from one API answer: {row: [[text, sub], ...]}. */
function expectedTable(body) {
  const columns = [body.yours, ...body.plans.slice(0, 3)];
  const irrigated = body.settings.irrigated;
  const each = (cell) => columns.map((plan) => [].concat(cell(plan)));
  // a rotation that is fallow every year has no crop to water or to lose: those cells say "no crop"
  const bare = (p) => (p.cycle ?? p.rotation).every((crop) => crop === F);
  // ...and it is not rankable: no ranking score, and no water or risk score for a crop that is not there
  const unranked = (p) => p === body.yours && body.yours.rankable === false;
  return {
    robust: each((p) => (unranked(p) ? "not ranked" : score(p.scores.robust))),
    soil: each((p) => score(p.scores.soil)),
    water: each((p) => (unranked(p) ? "no crop" : score(p.scores.water))),
    money: each((p) => score(p.scores.money)),
    risk: each((p) => (unranked(p) ? "no crop" : score(p.scores.risk))),
    holds: each((p) => `${p.futures.holds} of ${p.futures.total}`),
    bare_days: each((p) => String(Math.round(p.numbers.bare_days))),
    water_use: each((p) => (irrigated ? formatInches(p.numbers.irrigation_in, p.numbers.irrigation_mm) : bare(p) ? "no crop" : formatShare(p.numbers.water_met_share))),
    profit: each((p) => [formatDollars(p.numbers.profit_per_acre), rangeText(formatDollars(p.spread.profit_per_acre.low), formatDollars(p.spread.profit_per_acre.high))]),
    failed: each((p) => (bare(p) ? "no crop" : formatShare(p.numbers.failed_share))),
  };
}

/** The whole section on screen against one API answer: key, table, marks, sentences. */
async function checkResult(page, body, what) {
  equal(body.engine, "smart", `${what}: engine of the API answer`);
  const columns = [body.yours, ...body.plans.slice(0, 3)];
  equal(columns.length, 4, `${what}: yours plus three of ours`);
  equal((await slotsOnScreen(page)).join(","), body.understood.join(","), `${what}: the year slots are the rotation the API scored`);
  for (const [column, plan] of columns.entries()) {
    equal(await text(page, `own-key-name-${column}`), plan.name, `${what}: name of ${columnLabel(column)}`);
  }
  equal((await texts(page, '[data-testid="own-table"] thead th')).join("|"), columns.map((_, i) => columnLabel(i)).join("|"), `${what}: column headers`);
  // our columns are the cards above: same names, same order
  const cardNames = await texts(page, '[data-testid="plan-name"]');
  equal(cardNames.join("|"), body.plans.slice(0, 3).map((plan) => plan.name).join("|"), `${what}: our columns are the plan cards on screen`);
  const table = expectedTable(body);
  equal(Object.keys(table).sort().join(","), Object.keys(body.marks).sort().join(","), `${what}: the API marks the rows the table has`);
  for (const [row, cells] of Object.entries(table)) {
    equal(await page.locator(`[data-testid="own-row-${row}"]`).count(), 1, `${what}: table row ${row}`);
    for (const [column, [value, sub]] of cells.entries()) {
      const cell = page.locator(`[data-testid="own-cell-${row}-${column}"]`);
      equal(squash(await cell.locator(".own-cell-value").textContent()), value, `${what}: ${row}, ${columnLabel(column)}`);
      if (sub) equal(squash(await cell.locator(".own-cell-sub").textContent()), sub, `${what}: ${row} low-high, ${columnLabel(column)}`);
      else equal(await cell.locator(".own-cell-sub").count(), 0, `${what}: ${row}, ${columnLabel(column)} has no second line`);
      equal(await cell.getAttribute("data-best"), body.marks[row].includes(column) ? "true" : "false", `${what}: mark on ${row}, ${columnLabel(column)}`);
    }
  }
  const waterLabel = squash(await page.locator('[data-testid="own-row-water_use"] th').textContent());
  equal(waterLabel, body.settings.irrigated ? "Water pumped a year" : "Crop water need met by rain", `${what}: water figure for the practice`);
  equal(await text(page, "own-marks-note"), body.marks_note, `${what}: what a mark means`);
  if (body.settings.irrigated) check((await text(page, "own-holds")).includes(wellQualifier(body.settings.irrigation_limit_in)), `${what}: the well qualifier is missing`);
  else equal(await page.locator('[data-testid="own-holds"]').count(), 0, `${what}: no well qualifier on dryland`);

  // the words are the API's own
  const comparison = body.comparison;
  equal((await texts(page, '[data-testid="own-headline"] p')).join(" | "), comparison.headline.join(" | "), `${what}: headline`);
  if (body.yours.same_as_pick === 1) {
    equal(await page.locator('[data-testid="own-compare"]').count(), 0, `${what}: no row-by-row comparison of our first pick with itself`);
  } else if (comparison.rows.length === 0) {
    equal(body.yours.rankable, false, `${what}: only a rotation that is not rankable has no comparison rows`);
    equal(await page.locator('[data-testid="own-compare"]').count(), 0, `${what}: no row-by-row comparison of a rotation that is not rankable`);
  } else {
    for (const row of comparison.rows) {
      const item = page.locator(`[data-testid="own-compare-${row.key}"]`);
      equal(squash(await item.locator(".own-compare-sentence").textContent()), row.sentence, `${what}: ${row.key} sentence`);
      equal(squash(await item.locator(".own-verdict").textContent()), verdictLabel(row.verdict), `${what}: ${row.key} verdict`);
      equal(await item.getAttribute("data-verdict"), row.verdict, `${what}: ${row.key} verdict attribute`);
      equal(row.sentence.includes("Too close to call"), row.verdict === "too_close", `${what}: ${row.key} says too close to call only when it is`);
    }
  }
  equal((await texts(page, '[data-testid="own-priorities"] p')).join(" | "), comparison.priority_sentences.join(" | "), `${what}: when yours would come first`);
  equal(await page.locator('[data-testid="own-priorities"]').count(), comparison.priority_sentences.length > 0 ? 1 : 0, `${what}: "When would yours come first?" is shown only when there is an answer`);
  const broken = page.locator('[data-testid="own-broken"] li');
  equal((await broken.allTextContents()).map(squash).join(" | "), body.yours.rules_broken.join(" | "), `${what}: what our plans avoid`);
  check(body.yours.rules_broken.every((sentence) => sentence.startsWith("Our plans avoid")), `${what}: a broken rule is not worded gently`);
  equal((await texts(page, '[data-testid="own-notes"] li')).join(" | "), comparison.notes.join(" | "), `${what}: notes`);
  const all = await text(page, "own-rotation");
  check(!/soil health|improves? the soil|\bprofit\b|\bincome\b/i.test(all), `${what}: the section uses a word it must not ("${all.match(/soil health|improves? the soil|\bprofit\b|\bincome\b/i)?.[0]}")`);
}

// ---------------------------------------------------------------------------------------------
// Item 2: "How to get there"
// ---------------------------------------------------------------------------------------------

const title = (crop) => crop.replace(/(^|[\s/(-])([a-z])/g, (_m, before, letter) => before + letter.toUpperCase());
const FORBIDDEN = /soil health|improves? the soil|\bprofit\b|\bincome\b/i;

/** Wait until the opened path inside `scope` is the answer for exactly this query. */
async function pathShown(scope, query, what) {
  try {
    await scope.locator(`[data-testid="how-region"][data-state="ready"][data-updating="false"][data-query="${query}"]`).waitFor({ timeout: TIMEOUT });
  } catch {
    const region = scope.locator('[data-testid="how-region"]');
    const got = (await region.count()) ? `${await region.getAttribute("data-state")} / ${await region.getAttribute("data-query")}` : "(no region)";
    throw new Failure(`${what}: the path never showed the answer for ${query} (showing ${got})`);
  }
}

/** One table cell: its figure and, when given, its second line. */
async function cellIs(scope, testId, value, sub, what) {
  const cell = scope.locator(`[data-testid="${testId}"]`);
  equal(squash(await cell.locator(".how-cell-value").textContent()), value, `${what}: ${testId}`);
  if (sub) equal(squash(await cell.locator(".how-cell-sub").textContent()), sub, `${what}: ${testId}, second line`);
  else equal(await cell.locator(".how-cell-sub").count(), 0, `${what}: ${testId} has no second line`);
}

/** The opened path inside `scope` against one answer of GET /field/{id}/transition. */
async function checkPath(scope, body, what) {
  const region = scope.locator('[data-testid="how-region"]');
  const inside = (testId) => region.locator(`[data-testid="${testId}"]`);
  const list = async (testId) => (await inside(testId).locator("li").allTextContents()).map(squash).join(" | ");
  equal(body.engine, "smart", `${what}: engine of the API answer`);
  equal(await region.getAttribute("data-status"), body.status, `${what}: status`);
  equal(squash(await inside("how-summary").textContent()), body.summary, `${what}: summary sentence`);
  check(body.summary.length > 20, `${what}: the API gives no summary sentence`);
  equal(await list("how-blocked"), body.blocked.join(" | "), `${what}: rules in the way`);
  check(body.blocked.every((sentence) => sentence.startsWith("Our plans avoid")), `${what}: a rule in the way is not in the planner's gentle words`);

  // the strip: the last real years (greyed), then the path, bridge years marked
  const shown = await inside("how-year").evaluateAll((nodes) =>
    nodes.map((node) => ({
      kind: node.dataset.kind,
      year: Number(node.dataset.year),
      crop: node.dataset.crop,
      label: node.querySelector(".chip").textContent.trim(),
      tag: node.querySelector(".how-tag")?.textContent.trim() ?? null,
      note: node.querySelector(".how-year-note")?.textContent.trim() ?? null,
      money: node.querySelector('[data-testid="how-return"]')?.textContent.trim() ?? null,
      range: node.querySelector('[data-testid="how-range"]')?.textContent.replace(/\s+/g, " ").trim() ?? null,
      greyed: getComputedStyle(node.querySelector(".chip")).filter.includes("grayscale"),
      dashed: getComputedStyle(node.querySelector(".chip")).outlineStyle === "dashed",
    })),
  );
  const years = body.path ? body.path.years : [];
  check(body.history.length <= 3, `${what}: more than three real years in the API answer`);
  equal(shown.length, body.history.length + years.length, `${what}: years in the strip`);
  for (const [index, past] of body.history.entries()) {
    const cell = shown[index];
    equal(`${cell.kind} ${cell.year} ${cell.crop}`, `history ${past.year} ${past.crop}`, `${what}: real year ${index + 1} of the strip`);
    equal(cell.label, title(past.crop), `${what}: crop name of ${past.year}`);
    equal(cell.greyed, true, `${what}: the real year ${past.year} is greyed`);
    equal(cell.money, null, `${what}: no return is shown for the real year ${past.year}`);
    equal(cell.note, "grown", `${what}: the real year ${past.year} says it was grown`);
  }
  for (const [index, row] of years.entries()) {
    const cell = shown[body.history.length + index];
    equal(`${cell.kind} ${cell.year} ${cell.crop}`, `${row.role} ${row.year} ${row.crop}`, `${what}: year ${row.n} of the path`);
    equal(cell.money, formatDollars(row.return.mid), `${what}: return of ${row.year}`);
    equal(cell.range, rangeText(formatDollars(row.return.low), formatDollars(row.return.high)), `${what}: low-high return of ${row.year}`);
    equal(cell.tag, row.role === "bridge" ? "bridge" : null, `${what}: bridge mark on ${row.year}`);
    equal(cell.dashed, row.role === "bridge", `${what}: bridge outline on ${row.year}`);
    equal(cell.greyed, false, `${what}: the path year ${row.year} is not greyed`);
    check(row.return.low <= row.return.mid && row.return.mid <= row.return.high, `${what}: ${row.year} return is outside its own range`);
  }
  if (body.path) {
    equal(years.filter((row) => row.role === "bridge").length, body.path.bridge_years, `${what}: bridge years in the API answer`);
    check(body.path.bridge_years <= 2, `${what}: more than two bridge years`);
    equal(years[0].year, body.history.at(-1).year + 1, `${what}: the path starts the year after the last real one`);
    equal(years.length, body.years, `${what}: years scored`);
  }

  // the two sentences
  if (body.sentences) {
    equal(squash(await inside("how-short").textContent()), body.sentences.short_term, `${what}: short-term sentence`);
    equal(squash(await inside("how-long").textContent()), body.sentences.long_term, `${what}: long-term sentence`);
    check(body.sentences.short_term.startsWith("Short term: ") && body.sentences.long_term.startsWith("Long term: "), `${what}: the sentences do not say which is which`);
  } else {
    equal(await inside("how-short").count(), 0, `${what}: no sentences without a comparison`);
  }

  // against carrying on as now: chart, total, table
  const diff = body.difference;
  if (body.path && body.carry_on && diff) {
    const carry = squash(await inside("how-carry").textContent());
    equal(carry, `Carrying on as now: ${body.carry_on.name}.${body.carry_on_note ? ` ${body.carry_on_note}` : ""}`, `${what}: what carrying on as now is`);
    equal(squash(await inside("how-total").textContent()), diff.total_sentence, `${what}: running total sentence`);
    equal(await inside("how-table").locator("tbody tr").count(), body.years, `${what}: rows of the comparison table`);
    for (const [index, row] of diff.years.entries()) {
      const mine = body.path.years[index];
      const theirs = body.carry_on.years[index];
      await cellIs(region, `how-cell-path-${row.n}`, formatDollars(mine.return.mid), title(mine.crop), what);
      await cellIs(region, `how-cell-carry-${row.n}`, formatDollars(theirs.return.mid), title(theirs.crop), what);
      await cellIs(region, `how-cell-diff-${row.n}`, signedDollars(row.return.mid), signedRange(row.return.low, row.return.high), what);
      await cellIs(region, `how-cell-total-${row.n}`, signedDollars(row.cumulative.mid), signedRange(row.cumulative.low, row.cumulative.high), what);
      equal(row.return.mid, mine.return.mid - theirs.return.mid, `${what}: ${row.year}: the difference is the two returns shown, subtracted`);
      const paidBack = diff.payback.status === "paid_back" && diff.payback.n === row.n;
      const head = squash(await inside(`how-row-${row.n}`).locator("th").textContent());
      equal(head, paidBack ? `${row.year}paid back` : String(row.year), `${what}: year cell of row ${row.n}`);
    }
    // the chart: one column a year, the dot above the zero line when ahead and below it when behind
    const columns = await inside("how-chart-col").evaluateAll((nodes) => {
      const zero = document.querySelector(".how-chart-zero");
      return nodes.map((node) => {
        const plot = node.parentElement.querySelector(".how-chart-zero").getBoundingClientRect();
        const dot = node.querySelector(".how-chart-mid").getBoundingClientRect();
        const bar = node.querySelector(".how-chart-range").getBoundingClientRect();
        return { year: Number(node.dataset.year), mid: Number(node.dataset.mid), title: node.title, payback: node.classList.contains("is-payback"),
                 dot: (dot.top + dot.bottom) / 2, zero: (plot.top + plot.bottom) / 2, barTop: bar.top, barBottom: bar.bottom, any: Boolean(zero) };
      });
    });
    equal(columns.length, body.years, `${what}: columns of the running-total chart`);
    for (const [index, row] of diff.years.entries()) {
      const column = columns[index];
      equal(column.year, row.year, `${what}: chart column ${row.n}`);
      equal(column.mid, row.cumulative.mid, `${what}: chart value of ${row.year}`);
      check(column.title.startsWith(`${row.year}: ${signedDollars(row.cumulative.mid)} an acre`), `${what}: the chart's hover text for ${row.year} is "${column.title}"`);
      if (row.cumulative.mid > 0) check(column.dot < column.zero, `${what}: ${row.year} is ahead (${row.cumulative.mid}) but its dot is not above the zero line`);
      if (row.cumulative.mid < 0) check(column.dot > column.zero, `${what}: ${row.year} is behind (${row.cumulative.mid}) but its dot is not below the zero line`);
      check(column.barTop <= column.dot + 1 && column.dot - 1 <= column.barBottom, `${what}: ${row.year}: the dot is outside its low-high bar`);
      equal(column.payback, diff.payback.status === "paid_back" && diff.payback.n === row.n, `${what}: payback shading on ${row.year}`);
    }
    const yearLabels = (await region.locator(".how-chart-years span").allTextContents()).join(" ");
    equal(yearLabels, diff.years.map((row) => shortYear(row.year)).join(" "), `${what}: year labels under the chart`);
    const label = await region.locator(".how-chart-body").getAttribute("aria-label");
    check(diff.years.every((row) => label.includes(`${row.year}: ${signedDollars(row.cumulative.mid)}`)), `${what}: the chart's text alternative does not carry every year`);
    // the payback year is the first from which the running total stays at or above zero
    const totals = diff.years.map((row) => row.cumulative.mid);
    if (diff.payback.status === "paid_back") {
      check(totals.slice(diff.payback.n - 1).every((v) => v >= 0) && totals[diff.payback.n - 2] < 0, `${what}: the payback year ${diff.payback.year} is not where the running total turns for good`);
    } else if (diff.payback.status === "not_within_span") {
      check(totals.at(-1) < 0, `${what}: "not paid back" but the running total ends at ${totals.at(-1)}`);
    } else {
      check(totals.every((v) => v >= 0), `${what}: "${diff.payback.status}" but the running total dips below zero`);
    }
  } else {
    equal(await inside("how-table").count(), 0, `${what}: no comparison table without a comparison`);
    equal(await inside("how-chart").count(), 0, `${what}: no chart without a comparison`);
    if (body.path && body.carry_on_note) equal(squash(await inside("how-carry").textContent()), body.carry_on_note, `${what}: why there is no comparison`);
  }

  if (body.path) {
    // year-by-year water, bare days and crop failure
    const irrigated = body.settings.irrigated;
    equal(squash(await inside("how-detail-table").locator("thead th").nth(2).textContent()), irrigated ? "Water pumped" : "Water need met by rain", `${what}: water figure for the practice`);
    for (const row of years) {
      const grown = row.yield === null ? null : `${row.yield >= 20 ? Math.round(row.yield) : row.yield.toFixed(1)} ${row.yield_unit}`;
      const cells = await inside(`how-detail-${row.n}`).locator("td").evaluateAll((nodes) =>
        nodes.map((node) => [node.querySelector(".how-cell-value").textContent.trim(), node.querySelector(".how-cell-sub")?.textContent.trim() ?? null]));
      equal(JSON.stringify(cells), JSON.stringify([
        [title(row.crop), grown],
        [irrigated ? formatInches(row.irrigation_in, row.irrigation_mm) : row.water_met_share === null ? "no crop" : formatShare(row.water_met_share), null],
        [String(Math.round(row.bare_days)), null],
        [row.fail_chance === null ? "no crop" : formatShare(row.fail_chance), null],
      ]), `${what}: water, bare days and failure of ${row.year}`);
      equal(row.crop === F, row.fail_chance === null && row.water_met_share === null && row.yield === null, `${what}: only a fallow year of ${row.year} has no crop figures`);
    }
    equal(await list("how-not-modelled"), body.not_modelled.join(" | "), `${what}: what is not modelled`);
    check(body.not_modelled.some((line) => /equipment/i.test(line) && /contracts/i.test(line)), `${what}: equipment and contracts are not listed as left out`);
    equal(await list("how-notes"), body.notes.join(" | "), `${what}: notes`);
    check(body.notes[0].includes(`${body.settings.window[0]}–${body.settings.window[1]}`), `${what}: the first note does not name the climate window`);
    equal(await list("how-target-broken"), body.target.rules_broken.join(" | "), `${what}: what the rotation itself breaks`);
    equal(await inside("how-span").inputValue(), String(body.years), `${what}: years-ahead control`);
  } else {
    equal(await inside("how-detail-table").count(), 0, `${what}: no year-by-year table without a path`);
    equal(await inside("how-span").count(), 0, `${what}: no years-ahead control without a path`);
  }
  const all = squash(await region.textContent());
  check(!FORBIDDEN.test(all), `${what}: the path uses a word it must not ("${all.match(FORBIDDEN)?.[0]}")`);
}

async function runTransition(browser, { get, defaults, shot }) {
  const askPath = async (fieldId, query) => {
    const response = await get(`/field/${fieldId}/transition?${query}`);
    if (!response.ok) throw new Failure(`${fieldId}/transition?${query} answered ${response.status}`);
    return response.json();
  };
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
  const monitor = watch(page, "path");
  const card = (n) => page.locator('[data-testid="plan-card"]').nth(n - 1);
  const toView = (target, selector) =>
    target.evaluate((sel) => {
      document.querySelector(sel).scrollIntoView({ block: "start" });
      return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    }, selector);

  // ---- 14. a plan card: nothing asked until opened, then the path matches the API -------------------
  try {
    await openField(page, PATH_FIELD);
  } catch (error) {
    throw new Failure(`could not open ${WEB_URL}/#${PATH_FIELD} (set PATH_FIELD to a dryland field with crop history): ${error.message.split("\n")[0]}`);
  }
  equal(await page.locator('[data-testid="plan-card"] [data-testid="how-toggle"]').count(), 3, '"How to get there" on each plan card');
  equal(await page.locator('[data-testid="how-region"]').count(), 0, "no path is open to start with");
  equal(monitor.pathRequests.length, 0, "no /transition request before a path is opened");
  const toggle = card(2).locator('[data-testid="how-toggle"]');
  equal(squash(await toggle.textContent()), "How to get there", "the button's words");
  equal(await toggle.getAttribute("aria-expanded"), "false", "the button starts collapsed");
  equal(await card(2).locator('[data-testid="plan-edit"]').count(), 1, '"Edit this plan" is still on the card');
  await toggle.click();
  let query = transitionQuery(defaults, { plan: 2 }, DEFAULT_SPAN);
  equal(query, `plan=2&years=8&${defaults}`, "the query is the card number, eight years and the plan cards' settings");
  await pathShown(card(2), query, "second card");
  equal(await toggle.getAttribute("aria-expanded"), "true", "the button says it is open");
  equal(monitor.pathRequests.at(-1), `${PATH_FIELD}?${query}`, "request sent for the opened card");
  const second = await askPath(PATH_FIELD, query);
  const cards = await (await get(`/field/${PATH_FIELD}/plans?${defaults}`)).json();
  equal(second.target.name, cards.plans[1].name, "the path leads to the plan on that card");
  equal(JSON.stringify(second.target.cycle), JSON.stringify(cards.plans[1].cycle), "the target cycle is the card's");
  equal(JSON.stringify(second.settings), JSON.stringify(cards.settings), "the path is scored with the plan cards' settings");
  check(second.path !== null, `${PATH_FIELD} has no path to its second plan (${second.status}): set PATH_FIELD to a field that has one`);
  await checkPath(card(2), second, "second card");
  if (second.long_run) {
    equal(second.long_run.plan.return.mid, cards.plans[1].numbers.profit_per_acre, "long-run return of the plan is the card's return");
    equal(second.long_run.plan.bare_days, cards.plans[1].numbers.bare_days, "long-run bare days of the plan are the card's");
  }
  await toView(page, '[data-testid="how-region"]');
  const firstShot = await shot(page, "phase4-transition.png");
  await card(2).locator('[data-testid="how-details"] summary').click();
  await toView(page, ".how-versus");
  await shot(page, "phase4-transition-chart.png");
  log(`  ✓ "How to get there" on a plan card: ${second.status}, ${second.path.bridge_years} bridge ${second.path.bridge_years === 1 ? "year" : "years"}, payback "${second.difference?.payback.status ?? "no comparison"}"; strip, sentences, chart and tables match the API → ${firstShot}`);

  // ---- 15. years ahead, and the planner controls ------------------------------------------------------
  equal((await card(2).locator('[data-testid="how-span"] option').allTextContents()).join(","), SPANS.join(","), "years-ahead choices");
  await card(2).locator('[data-testid="how-span"]').selectOption("12");
  query = transitionQuery(defaults, { plan: 2 }, 12);
  await pathShown(card(2), query, "twelve years");
  const twelve = await askPath(PATH_FIELD, query);
  equal(twelve.years, 12, "the API scored twelve years");
  await checkPath(card(2), twelve, "second card, twelve years");
  await card(2).locator('[data-testid="how-span"]').selectOption("4");
  query = transitionQuery(defaults, { plan: 2 }, 4);
  await pathShown(card(2), query, "four years");
  await checkPath(card(2), await askPath(PATH_FIELD, query), "second card, four years");
  const before = monitor.pathRequests.length;
  await page.locator('[data-testid="horizon"]').focus();
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowRight");
  await page.locator('[data-testid="scenario-ssp585"]').check();
  const choices = { ...DEFAULT_CHOICES, horizon: 2050, scenario: "ssp585" };
  query = transitionQuery(plansQuery(choices), { plan: 2 }, 4);
  await pathShown(card(2), query, "2050, high emissions");
  await page.waitForSelector('[data-testid="plans-region"][data-updating="false"]');
  const future = await askPath(PATH_FIELD, query);
  equal(future.settings.horizon, 2050, "API horizon of the path");
  equal(JSON.stringify(future.settings.window), JSON.stringify([2040, 2059]), "the path uses the climate window of the chosen future");
  await checkPath(card(2), future, "second card, 2050 high emissions");
  equal(squash(await card(2).locator('[data-testid="plan-name"]').textContent()), future.target.name, "the open path follows the plan now on that card");
  const sent = monitor.pathRequests.length - before;
  check(sent >= 1 && sent < 6, `6 control changes sent ${sent} /transition requests: the debounce is not working`);
  await page.locator('[data-testid="horizon"]').focus();
  await page.keyboard.press("Home");
  await page.locator('[data-testid="scenario-ssp245"]').check();
  query = transitionQuery(defaults, { plan: 2 }, 4);
  await pathShown(card(2), query, "back at the defaults");
  await toggle.click();
  equal(await card(2).locator('[data-testid="how-region"]').count(), 0, "the button closes the path again");
  equal(await toggle.getAttribute("aria-expanded"), "false", "the button says it is closed");
  log(`  ✓ years ahead (4 to 12) and the planner controls: the open path follows them (6 quick changes → ${sent} ${sent === 1 ? "request" : "requests"})`);

  // ---- 16. the farmer's own rotation -----------------------------------------------------------------
  const own = page.locator('[data-testid="own-rotation"]');
  const typed = [C, S, W, F];
  await page.locator('[data-testid="own-text"]').fill(typed.join(", "));
  await page.locator('[data-testid="own-text-submit"]').click();
  await scored(page, scoreQuery(defaults, typed), "own rotation for the path");
  equal(await own.locator('[data-testid="how-toggle"]').count(), 1, '"How to get there" under the build-your-own result');
  await own.locator('[data-testid="how-toggle"]').click();
  query = transitionQuery(defaults, { rotation: typed }, DEFAULT_SPAN);
  equal(query, `target=${encodeURIComponent(typed.join(","))}&years=8&${defaults}`, "the query carries the rotation as the target");
  await pathShown(own, query, "own rotation");
  const mine = await askPath(PATH_FIELD, query);
  equal(mine.target.source, "own", "the API takes the rotation as the farmer's own");
  equal(JSON.stringify(mine.target.cycle), JSON.stringify(typed), "the target is the rotation typed");
  await checkPath(own, mine, "own rotation");
  if (!mine.settings.irrigated) check(mine.target.rules_broken.length > 0, "dryland wheat after sorghum should be listed as what the rotation itself breaks");
  if (mine.sentences) check(mine.sentences.long_term.includes("your rotation"), `the long-term sentence does not speak of "your rotation" ("${mine.sentences.long_term}")`);
  // changing the rotation keeps the path open and asks again
  await page.locator('[data-testid="own-later-0"]').click();
  const moved = moveSlot(typed, 0, 1);
  query = transitionQuery(defaults, { rotation: moved }, DEFAULT_SPAN);
  await pathShown(own, query, "own rotation, a year moved");
  await checkPath(own, await askPath(PATH_FIELD, query), "own rotation, a year moved");
  // a rotation that plants nothing is not offered a path
  await page.locator('[data-testid="own-text"]').fill("fallow, fallow");
  await page.locator('[data-testid="own-text-submit"]').click();
  await scored(page, scoreQuery(defaults, [F, F]), "nothing planted, for the path");
  equal(await own.locator('[data-testid="how-toggle"]').count(), 0, 'no "How to get there" for a rotation that plants nothing');
  log(`  ✓ the farmer's own rotation: ${mine.status}, starts ${mine.path ? `${mine.path.entry.year} with ${mine.path.entry.crop}` : "nowhere"}; nothing planted gets no path`);

  // ---- 17. a failed request says so and can be tried again -------------------------------------------
  const failing = (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Made-up failure for the test." }) });
  const hitsPath = (url) => /\/transition$/.test(url.pathname) && url.search.includes("plan=1");
  await page.route(hitsPath, failing);
  await card(1).locator('[data-testid="how-toggle"]').click();
  await card(1).locator('[data-testid="how-error"]').waitFor();
  check(squash(await card(1).locator('[data-testid="how-error"]').textContent()).includes("Made-up failure for the test."), "the error line does not carry the API's sentence");
  equal(await page.locator('[data-testid="plan-card"]').count(), 3, "the plan cards stay when a path cannot be loaded");
  await page.unroute(hitsPath, failing);
  await card(1).locator('[data-testid="how-error"] button').click();
  query = transitionQuery(defaults, { plan: 1 }, DEFAULT_SPAN);
  await pathShown(card(1), query, "first card after Try again");
  await checkPath(card(1), await askPath(PATH_FIELD, query), "first card");
  log("  ✓ a failed request shows the API's sentence with Try again, and the cards stay");

  // ---- 18. bridge years, already there, and a field we cannot start from ----------------------------
  const openOn = async (fieldId) => {
    await page.evaluate((id) => (window.location.hash = id), fieldId);
    await page.waitForSelector(`[data-testid="panel"][data-field-id="${fieldId}"][data-state="ready"]`);
    await page.waitForSelector('[data-testid="plans-region"][data-updating="false"]');
    equal(await page.locator('[data-testid="how-region"]').count(), 0, `${fieldId}: a path opened for one field is not carried to the next`);
  };
  const seen = [];
  for (const [fieldId, wanted] of [[BRIDGE_FIELD, "a bridge year"], [SETTLED_FIELD, "already there"], [DRY, "no usable last year"]]) {
    if ((await get(`/field/${fieldId}`)).status !== 200) {
      log(`  - ${fieldId} is not in this data set; skipping the "${wanted}" case`);
      continue;
    }
    await openOn(fieldId);
    const answers = [];
    for (const n of [1, 2, 3]) answers.push(await askPath(fieldId, transitionQuery(defaults, { plan: n }, DEFAULT_SPAN)));
    const fits = { "a bridge year": (b) => b.path && b.path.bridge_years > 0, "already there": (b) => b.status === "already_there", "no usable last year": (b) => b.status === "no_history" || b.status === "no_path" }[wanted];
    const n = (answers.findIndex(fits) + 1) || 1;
    const body = answers[n - 1];
    await card(n).locator('[data-testid="how-toggle"]').click();
    await pathShown(card(n), transitionQuery(defaults, { plan: n }, DEFAULT_SPAN), `${fieldId}, card ${n}`);
    await checkPath(card(n), body, `${fieldId}, card ${n}`);
    if (fits(body)) seen.push(wanted);
    if (body.path && body.path.bridge_years > 0) {
      check(body.blocked.length > 0, `${fieldId}: a bridge is needed but no rule is named as in the way`);
      check(/bridge year/.test(body.summary), `${fieldId}: the summary does not mention the bridge ("${body.summary}")`);
      await toView(page, '[data-testid="how-region"]');
      await shot(page, "phase4-transition-bridge.png");
    }
    if (body.status === "already_there") {
      check(body.summary.startsWith("This field is already in this rotation"), `${fieldId}: already there is not said ("${body.summary}")`);
      check(body.difference.years.every((row) => row.cumulative.mid === 0), `${fieldId}: already there, yet the running total is not zero`);
    }
    if (body.path === null) {
      check(/cannot work out a path|found no way/.test(body.summary), `${fieldId}: no path, and the summary does not say so plainly ("${body.summary}")`);
    }
    log(`  ✓ ${fieldId}, card ${n}: ${body.status}${body.path ? `, ${body.path.bridge_years} bridge ${body.path.bridge_years === 1 ? "year" : "years"}` : ""}: "${body.summary.slice(0, 90)}${body.summary.length > 90 ? "…" : ""}"`);
  }
  const realProblems = monitor.problems.filter((p) => !p.includes("500 (Internal Server Error)"));    // the failure this test made
  check(realProblems.length === 0, `console errors in the path checks:\n  ${realProblems.join("\n  ")}`);
  log(`  ✓ special cases seen in this data: ${seen.length ? seen.join(", ") : "none (the fields have changed; the pages still match the API)"}; no console errors`);
  await context.close();

  // ---- 19. phone width -------------------------------------------------------------------------------
  const phoneContext = await browser.newContext({ viewport: PHONE, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const phone = await phoneContext.newPage();
  phone.setDefaultTimeout(TIMEOUT);
  const phoneMonitor = watch(phone, "phone path");
  await openField(phone, PATH_FIELD);
  const phoneCard = phone.locator('[data-testid="plan-card"]').nth(1);
  await phoneCard.locator('[data-testid="how-toggle"]').tap();
  await pathShown(phoneCard, transitionQuery(defaults, { plan: 2 }, DEFAULT_SPAN), "phone");
  await phoneCard.locator('[data-testid="how-span"]').selectOption("12");
  query = transitionQuery(defaults, { plan: 2 }, 12);
  await pathShown(phoneCard, query, "phone, twelve years");
  await phoneCard.locator('[data-testid="how-details"] summary').tap();
  await checkPath(phoneCard, await askPath(PATH_FIELD, query), "phone, twelve years");
  const fit = await phone.evaluate(() => {
    const body = document.querySelector(".panel-body");
    const clipped = [...document.querySelectorAll(".how, .how-actions, .how-region, .how-strip, .how-year, .how-sentences, .how-versus, .how-chart, .how-chart-body, .how-chart-plot, .how-table-wrap, .how-table, .how-more, .how-span")]
      .filter((node) => node.scrollWidth > node.clientWidth + 1)
      .map((node) => node.className);
    const cut = [...document.querySelectorAll(".how-chip")].filter((node) => node.scrollWidth > node.clientWidth + 1).length;
    const small = [...document.querySelectorAll(".how-toggle, .how-span select")].filter((node) => node.getBoundingClientRect().height < 24).length;
    const plot = document.querySelector(".how-chart-plot").getBoundingClientRect();
    const axis = [...document.querySelectorAll(".how-chart-axis span")].map((node) => node.getBoundingClientRect());
    return { page: document.documentElement.scrollWidth, body: body.scrollWidth - body.clientWidth, clipped, cut, small, plotWidth: plot.width,
             axisInside: axis.every((box) => box.left >= body.getBoundingClientRect().left && box.right <= plot.left + 1) };
  });
  check(fit.page <= PHONE.width, `phone: the page scrolls sideways (${fit.page}px wide)`);
  check(fit.body <= 0, `phone: the panel scrolls sideways by ${fit.body}px`);
  equal(fit.clipped.join(","), "", "phone: clipped parts of How to get there");
  equal(fit.cut, 0, "phone: crop names cut off in the strip of years");
  equal(fit.small, 0, "phone: path controls under 24px tall");
  check(fit.plotWidth >= 12 * 14, `phone: the chart is too narrow for twelve years (${Math.round(fit.plotWidth)}px)`);
  check(fit.axisInside, "phone: the chart's value labels run into the plot or off the panel");
  await phone.evaluate(() => document.querySelector('[data-testid="how-region"]').scrollIntoView({ block: "start" }));
  const phoneShot = await shot(phone, "phase4-transition-phone.png");
  await phone.evaluate(() => document.querySelector(".how-versus").scrollIntoView({ block: "start" }));
  const phoneChart = await shot(phone, "phase4-transition-phone-chart.png");
  check(phoneMonitor.problems.length === 0, `console errors on the phone:\n  ${phoneMonitor.problems.join("\n  ")}`);
  await phoneContext.close();
  log(`  ✓ phone width: twelve years fit, strip wraps, chart and tables fit, no sideways scroll → ${phoneShot}, ${phoneChart}`);
}

/** Item 3: the whole-farm planner's endpoint, asked the way a page will ask it. */
async function runFarm(browser, { get, apiPrefix, defaults }) {
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.setDefaultTimeout(Math.max(TIMEOUT, 40000));
  const monitor = watch(page, "farm");
  await openField(page, DRY);
  const apiBase = await page.getAttribute('[data-testid="app"]', "data-api-base");
  const fromPage = (body) =>
    page.evaluate(
      async ([base, payload]) => {
        const response = await fetch(`${base}/farm/plan`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        return { status: response.status, body: await response.json() };
      },
      [apiBase, body],
    );
  const direct = async (body) => {
    const response = await fetch(`${apiPrefix}/farm/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() };
  };
  /** The answer without the clock: a repeat of the same request must give the same plan. */
  const steady = (body) => {
    const copy = structuredClone(body);
    delete copy.solver.time_s;
    delete copy.solver.note;
    for (const stage of copy.solver.stages) delete stage.seconds;
    copy.summary = copy.summary.filter((sentence) => !sentence.includes("seconds"));
    return JSON.stringify(copy);
  };

  const ids = [...new Set([DRY, WET, PATH_FIELD, BRIDGE_FIELD, SETTLED_FIELD])];
  const free = await fromPage({ field_ids: ids });
  equal(free.status, 200, "farm: POST /farm/plan from the page");
  equal(free.body.status, "no_limits", "farm: status without limits");
  equal(free.body.fields.map((f) => f.field_id).join(","), ids.join(","), "farm: fields in the order asked");
  equal(free.body.plan.years.length, 12, "farm: years planned");
  equal(free.body.settings.models.length > 0, true, "farm: climate models listed");
  for (const entry of free.body.fields) {
    const own = await (await get(`/field/${entry.field_id}/plans?${defaults}`)).json();
    equal(entry.own_pick.name, own.plans[0].name, `farm: ${entry.field_id} own first pick`);
    equal(JSON.stringify(entry.own_pick.cycle), JSON.stringify(own.plans[0].cycle), `farm: ${entry.field_id} own cycle`);
    equal(entry.own_pick.scores.robust, own.plans[0].scores.robust, `farm: ${entry.field_id} own ranking score`);
    equal(entry.irrigated, own.settings.irrigated, `farm: ${entry.field_id} irrigated or dryland`);
    equal(entry.plan.name, entry.own_pick.name, `farm: ${entry.field_id} keeps its own pick without limits`);
    equal(entry.plan.years.length, 12, `farm: ${entry.field_id} twelve years`);
  }

  // limits taken from the farm's own numbers: a budget under the heaviest year, a minimum above the leanest
  const alone = free.body.alone;
  const asked = { field_ids: ids };
  if (alone.peak_water.value > 0) asked.water_budget_acre_in = Math.round(0.85 * alone.peak_water.value);
  if (alone.worst_return.value < alone.average.return) {
    asked.min_income = Math.round((alone.worst_return.value + alone.average.return) / 200) * 100;
  }
  const limited = await fromPage(asked);
  equal(limited.status, 200, "farm: POST with limits");
  const body = limited.body;
  check(["limits_met", "limits_not_met"].includes(body.status), `farm: status with limits is ${body.status}`);
  check(["optimal", "time_limit"].includes(body.solver.status), `farm: solver status is ${body.solver.status}`);
  check(body.solver.gap >= 0 && body.solver.time_s < 15, `farm: gap ${body.solver.gap}, ${body.solver.time_s}s`);
  for (const year of body.plan.years) {
    for (const key of ["water_acre_in", "return"]) {
      check(year[key].low <= year[key].mid && year[key].mid <= year[key].high, `farm: year ${year.n} ${key} low <= mid <= high`);
    }
    if (body.status === "limits_met") {
      if (asked.water_budget_acre_in !== undefined) {
        check(year.water_ok === true && year.water_acre_in.mid <= asked.water_budget_acre_in + 1, `farm: year ${year.n} is within the water budget`);
      }
      if (asked.min_income !== undefined) {
        check(year.return_ok === true && year.return.mid >= asked.min_income - 100, `farm: year ${year.n} meets the minimum return`);
      }
    }
  }
  for (const [key, set] of [["water", asked.water_budget_acre_in !== undefined], ["return", asked.min_income !== undefined]]) {
    equal(body.limits[key].set, set, `farm: ${key} limit set`);
    if (set) {
      equal(body.limits[key].alone.met, false, `farm: each field alone breaks the ${key} limit`);
      equal(body.limits[key].binds, true, `farm: the ${key} limit binds`);
      check(body.summary.join(" ").includes("Each field planned alone would"), "farm: the summary says what each field alone would do");
    }
  }
  for (const entry of body.fields) {
    equal(entry.differs, !(entry.same_rotation && entry.same_start), `farm: ${entry.field_id} differs flag`);
    equal(entry.gives_up === null, entry.same_rotation, `farm: ${entry.field_id} gives_up only when the rotation changes`);
    check(entry.sentence.length > 0, `farm: ${entry.field_id} has a sentence`);
  }
  const again = await direct(asked);
  equal(again.status, 200, "farm: direct POST");
  equal(steady(again.body), steady(body), "farm: the page's answer equals a direct call for the same request");

  const tooMany = await fromPage({ field_ids: Array.from({ length: 61 }, (_, i) => `KS-${String(i + 1).padStart(6, "0")}`) });
  equal(tooMany.status, 400, "farm: 61 fields");
  check(/at most 60 fields; this one has 61\.$/.test(tooMany.body.detail), `farm: the 61-field refusal reads "${tooMany.body.detail}"`);
  equal((await fromPage({ field_ids: [DRY, "KS-999999"] })).status, 404, "farm: unknown field");
  equal((await fromPage({ field_ids: [DRY], water_budget_acre_in: -1 })).status, 400, "farm: negative water budget");
  // console errors from the deliberate 400 / 404 answers above are the browser's own log of them
  const problems = monitor.problems.filter((line) => !/farm\/plan|status of 40[04]/.test(line));
  check(problems.length === 0, `console errors on the farm step:\n  ${problems.join("\n  ")}`);
  await context.close();
  const changed = body.fields.filter((f) => !f.same_rotation).length;
  const shifted = body.fields.filter((f) => f.same_rotation && !f.same_start).length;
  log(`  ✓ whole-farm planner: ${ids.length} fields from the page = direct call; own picks = /plans; ${body.status}, ` +
      `${changed} changed rotation, ${shifted} shifted start, solver ${body.solver.status} in ${body.solver.time_s}s; 400 and 404 are plain`);
}

// ------------------------------------------------------------------------------------------
// Item 3, the farm screen: "Plan a farm" on the map and the farm panel
// ------------------------------------------------------------------------------------------

const FARM_FILL = "#f97316";

/** A point inside a field's outline ([lon, lat] rings): the middle of its box, or of a chord through it. */
function pointInside(rings) {
  const inside = (x, y) => {
    let hit = false;
    for (const ring of rings) {
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [ax, ay] = ring[i];
        const [bx, by] = ring[j];
        if (ay > y !== by > y && x < ax + ((y - ay) * (bx - ax)) / (by - ay)) hit = !hit;
      }
    }
    return hit;
  };
  const lons = rings[0].map((p) => p[0]);
  const lats = rings[0].map((p) => p[1]);
  const centre = { lon: (Math.min(...lons) + Math.max(...lons)) / 2, lat: (Math.min(...lats) + Math.max(...lats)) / 2 };
  for (const share of [0.5, 0.35, 0.65, 0.2, 0.8]) {
    const lat = Math.min(...lats) + share * (Math.max(...lats) - Math.min(...lats));
    if (share === 0.5 && inside(centre.lon, lat)) return { ...centre, centre };
    const crossings = [];
    for (const ring of rings) {
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [ax, ay] = ring[i];
        const [bx, by] = ring[j];
        if (ay > lat !== by > lat) crossings.push(ax + ((lat - ay) * (bx - ax)) / (by - ay));
      }
    }
    crossings.sort((a, b) => a - b);
    let best = null;
    for (let i = 0; i + 1 < crossings.length; i += 2) {
      if (best === null || crossings[i + 1] - crossings[i] > best[1] - best[0]) best = [crossings[i], crossings[i + 1]];
    }
    if (best && inside((best[0] + best[1]) / 2, lat)) return { lon: (best[0] + best[1]) / 2, lat, centre };
  }
  return { ...centre, centre };
}

async function runFarmScreen(browser, { get, apiPrefix, shot }) {
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.setDefaultTimeout(Math.max(TIMEOUT, 40000));
  const monitor = watch(page, "farm screen");
  const farmRequests = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname.endsWith("/farm/plan")) farmRequests.push(request.postData());
  });
  await page.goto(`${WEB_URL}/`, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await page.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });

  const collection = await (await get("/fields")).json();
  const spots = new Map();
  for (const feature of collection.features) {
    const id = String(feature.id ?? feature.properties.field_id);
    spots.set(id, pointInside(feature.geometry.coordinates));
  }
  const order = [...spots.keys()];
  const direct = async (body) => {
    const response = await fetch(`${apiPrefix}/farm/plan`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  };
  const one = (selector) => page.locator(selector).first();
  const chips = () => page.locator('[data-testid="farm-chip"]').evaluateAll((nodes) => nodes.map((node) => node.dataset.fieldId));
  const onMap = () =>
    page.evaluate((fill) => {
      const ids = [];
      window.__map.eachLayer((layer) => {
        if (layer.feature && layer.options?.fillColor === fill) ids.push(String(layer.feature.id ?? layer.feature.properties.field_id));
      });
      return ids.sort();
    }, FARM_FILL);
  const sameSet = async (expected, what) => {
    equal((await chips()).join(","), expected.join(","), `${what}: fields listed in the farm panel`);
    equal((await onMap()).join(","), [...expected].sort().join(","), `${what}: fields highlighted on the map`);
    equal(await text(page, "farm-count"), `${expected.length} ${expected.length === 1 ? "field" : "fields"} picked`, `${what}: count by the map tools`);
  };
  /** Centre the map on a field and return a page position inside it that the map (not a card) is under. */
  const aim = async (id, zoom = 15) => {
    const spot = spots.get(id);
    check(spot !== undefined, `${id} is not in /fields`);
    const target = await page.evaluate(
      ({ lat, lon, zoom }) =>
        new Promise((resolve) => {
          const map = window.__map;
          map.setView([lat, lon], zoom, { animate: false });
          requestAnimationFrame(() =>
            requestAnimationFrame(() => {
              const inMap = map.latLngToContainerPoint([lat, lon]);
              const frame = map.getContainer().getBoundingClientRect();
              const x = frame.left + inMap.x;
              const y = frame.top + inMap.y;
              const element = document.elementFromPoint(x, y);
              resolve({ x, y, onMap: element instanceof HTMLCanvasElement, under: (() => { const chain = []; for (let n = element; n && n !== document.body; n = n.parentElement) chain.push(`${n.tagName.toLowerCase()}.${n.className?.baseVal ?? n.className}`); return chain.slice(0, 4).join(" in ") || "nothing"; })() });
            }),
          );
        }),
      { lat: spot.lat, lon: spot.lon, zoom },
    );
    check(target.onMap, `${id}: ${target.under} covers the field at the middle of the map (${Math.round(target.x)}, ${Math.round(target.y)})`);
    return target;
  };
  const clickOnMap = async (id) => {
    const target = await aim(id);
    await page.mouse.click(target.x, target.y);
  };
  /** Lon/lat of page positions, so the test can work out which centres a lasso holds. */
  const toLonLat = (points) =>
    page.evaluate((points) => {
      const frame = window.__map.getContainer().getBoundingClientRect();
      return points.map(([x, y]) => {
        const where = window.__map.containerPointToLatLng([x - frame.left, y - frame.top]);
        return [where.lng, where.lat];
      });
    }, points);
  const centresIn = (loop) => order.filter((id) => insideLoop(spots.get(id).centre.lon, spots.get(id).centre.lat, loop));
  const drag = async (points) => {
    await page.mouse.move(points[0][0], points[0][1]);
    await page.mouse.down();
    for (const [x, y] of points.slice(1)) await page.mouse.move(x, y, { steps: 6 });
    return () => page.mouse.up();
  };
  const settled = async (body, what) => {
    const wanted = JSON.stringify(body);
    try {
      await page.waitForFunction(
        (query) => {
          const node = document.querySelector('[data-testid="farm-result"]');
          return node !== null && node.dataset.updating === "false" && node.dataset.query === query;
        },
        wanted,
        { timeout: 45000 },
      );
    } catch {
      throw new Failure(`${what}: the farm plan on screen never became the one for ${wanted.slice(0, 200)}`);
    }
    equal(await page.locator('[data-testid="farm-waiting"]').count(), 0, `${what}: the waiting note is gone once the plan is in`);
  };
  const cell = async (row, col) => squash(await one(`[data-testid="farm-compare"] tr[data-row="${row}"] td[data-col="${col}"]`).textContent());

  /** Everything the farm panel shows, against the API's answer for the same request. */
  const checkFarm = async (answer, what) => {
    const { plan, alone, limits } = answer;
    const years = plan.years.length;
    equal(await text(page, "farm-title"), `${answer.farm.fields} ${answer.farm.fields === 1 ? "field" : "fields"}`, `${what}: title`);
    equal(await text(page, "farm-acres"), `${formatWhole(answer.farm.area_acres)} acres in total`, `${what}: total acres`);
    const chipTexts = await texts(page, '[data-testid="farm-chip"] .farm-chip-text');
    answer.fields.forEach((field, index) =>
      equal(chipTexts[index], `${field.field_id} · ${formatAcres(field.area_acres)} acres · ${field.irrigated ? "irrigated" : "dryland"}`, `${what}: chip ${index + 1}`),
    );
    equal(await one('[data-testid="farm-result"]').getAttribute("data-status"), answer.status, `${what}: status`);
    const summary = await text(page, "farm-summary");
    const limitSentences = [limits.water.sentence, limits.return.sentence].filter(Boolean);
    for (const sentence of answer.summary.filter((s) => !limitSentences.includes(s))) {
      check(summary.includes(squash(sentence)), `${what}: the summary is missing the API's sentence "${sentence}"`);
    }
    for (const key of ["water", "return"]) {
      const line = page.locator(`[data-testid="farm-limit-${key}"]`);
      equal(await line.count(), limits[key].set ? 1 : 0, `${what}: ${key} limit line shown only when set`);
      if (!limits[key].set) continue;
      const tone = limitTone(limits[key]);
      equal(await line.getAttribute("data-tone"), tone, `${what}: ${key} limit tone`);
      equal(squash(await line.textContent()), `${LIMIT_TAGS[tone]} ${squash(limits[key].sentence)}`, `${what}: ${key} limit sentence`);
      check(squash(limits[key].sentence).includes(squash(limits[key].binds_sentence)) || limits[key].met === false, `${what}: the ${key} sentence says whether it binds`);
    }
    equal(await cell("score", "plan"), formatFarmScore(plan.score), `${what}: farm score`);
    equal(await cell("score", "alone"), formatFarmScore(alone.score), `${what}: score alone`);
    equal(await cell("peak-water", "plan"), `${formatWhole(plan.peak_water.value)} (year ${plan.peak_water.n})`, `${what}: heaviest year`);
    equal(await cell("peak-water", "alone"), `${formatWhole(alone.peak_water.value)} (year ${alone.peak_water.n})`, `${what}: heaviest year alone`);
    equal(await cell("average-water", "plan"), formatWhole(plan.average.water_acre_in), `${what}: average water`);
    equal(await cell("average-water", "alone"), formatWhole(alone.average.water_acre_in), `${what}: average water alone`);
    equal(await cell("worst-return", "plan"), `${formatFarmDollars(plan.worst_return.value)} (year ${plan.worst_return.n})`, `${what}: leanest year`);
    equal(await cell("worst-return", "alone"), `${formatFarmDollars(alone.worst_return.value)} (year ${alone.worst_return.n})`, `${what}: leanest year alone`);
    equal(await cell("average-return", "plan"), formatFarmDollars(plan.average.return), `${what}: average return`);
    equal(await cell("average-return", "alone"), formatFarmDollars(alone.average.return), `${what}: average return alone`);
    equal(await cell("water-ok", "plan"), metWords(plan.water_ok, plan.water_years_over, years, "No budget set"), `${what}: plan within the budget`);
    equal(await cell("water-ok", "alone"), metWords(alone.water_ok, alone.water_years_over, years, "No budget set"), `${what}: alone within the budget`);
    equal(await cell("return-ok", "plan"), metWords(plan.return_ok, plan.return_years_under, years, "No minimum set"), `${what}: plan meets the minimum`);
    equal(await cell("return-ok", "alone"), metWords(alone.return_ok, alone.return_years_under, years, "No minimum set"), `${what}: alone meets the minimum`);

    // the guides by the two boxes: the farm's own unconstrained numbers
    const waterGuide = await text(page, "farm-water-guide");
    if (answer.farm.irrigated_fields > 0) {
      check(waterGuide.includes(`up to ${formatWhole(alone.peak_water.value)} acre-inches`) && waterGuide.includes(`${formatWhole(alone.average.water_acre_in)} in an average year`), `${what}: water guide reads "${waterGuide}"`);
    } else check(/pumps no water/.test(waterGuide), `${what}: water guide for a dryland farm reads "${waterGuide}"`);
    const returnGuide = await text(page, "farm-minimum-guide");
    check(returnGuide.includes(`${formatFarmDollars(alone.worst_return.value)} over direct costs`) && returnGuide.includes(`average year ${formatFarmDollars(alone.average.return)}`), `${what}: return guide reads "${returnGuide}"`);

    // the per-year strip and its table
    for (const [chart, key, limit] of [["farm-chart-return", "return", limits.return], ["farm-chart-water", "water_acre_in", limits.water]]) {
      const shown = await page.locator(`[data-testid="${chart}"]`).count();
      if (key === "water_acre_in" && plan.peak_water.value === 0 && alone.peak_water.value === 0) {
        equal(shown, 0, `${what}: no water chart for a farm that pumps nothing`);
        continue;
      }
      equal(shown, 1, `${what}: ${chart} is shown`);
      equal(await page.locator(`[data-testid="${chart}"] g[data-year]`).count(), years, `${what}: ${chart} has one mark per year`);
      const tips = await page.locator(`[data-testid="${chart}"] g[data-year] title`).allTextContents();
      const format = key === "return" ? formatFarmDollars : formatWhole;
      plan.years.forEach((year, index) => {
        const want = `Year ${year.n}: ${format(year[key].mid)} (models ${rangeWords(year[key].low, year[key].high, format)}); each field alone ${format(alone.years[index][key].mid)}`;
        equal(squash(tips[index]), want, `${what}: ${chart} year ${year.n}`);
      });
      const paint = await page.locator(`[data-testid="${chart}"]`).evaluate((node) => ({
        dot: getComputedStyle(node.querySelector(".farm-dot:not(.farm-dot--short)") ?? node.querySelector(".farm-dot")).stroke,
        range: getComputedStyle(node.querySelector(".farm-range")).fill,
        alone: getComputedStyle(node.querySelector(".farm-alone")).stroke,
      }));
      check(paint.range !== "rgb(0, 0, 0)" && paint.alone !== "none" && paint.dot !== "none", `${what}: ${chart} marks are styled (${JSON.stringify(paint)})`);
      const hollow = await page.locator(`[data-testid="${chart}"] .farm-dot--short`).count();
      equal(hollow, key === "return" ? plan.return_years_under.length : plan.water_years_over.length, `${what}: ${chart} hollow dots = years that miss the limit`);
      const label = page.locator(`[data-testid="${chart}-limit"]`);
      equal(await label.count(), limit.set ? 1 : 0, `${what}: ${chart} limit shown only when set`);
      if (limit.set) check(squash(await label.textContent()).endsWith(format(limit.limit)), `${what}: ${chart} limit label`);
      equal(await page.locator(`[data-testid="${chart}"] .farm-limit-rule`).count(), limit.set ? 1 : 0, `${what}: ${chart} limit line`);
    }
    await page.evaluate(() => { document.querySelector('[data-testid="farm-years-details"]').open = true; });
    const rows = await page.locator('[data-testid="farm-years-table"] tbody tr').evaluateAll((nodes) => nodes.map((row) => [...row.children].map((c) => c.textContent.replace(/\s+/g, " ").trim())));
    equal(rows.length, years, `${what}: rows in the year table`);
    plan.years.forEach((year, index) => {
      equal(rows[index][0], String(year.n), `${what}: year table row ${index + 1}`);
      equal(rows[index][1], `${formatFarmDollars(year.return.mid)} (${rangeWords(year.return.low, year.return.high, formatFarmDollars)})`, `${what}: year ${year.n} return`);
      equal(rows[index][2], `${formatWhole(year.water_acre_in.mid)} (${rangeWords(year.water_acre_in.low, year.water_acre_in.high, formatWhole)})`, `${what}: year ${year.n} water`);
      check(year.return.low <= year.return.mid && year.return.mid <= year.return.high, `${what}: year ${year.n} low <= mid <= high`);
    });

    // the plan for each field
    equal(await text(page, "farm-changed"), `${plan.changed_fields} with a different rotation, ${plan.shifted_fields} with another start year`, `${what}: changed and shifted counts`);
    const fieldRows = await page.locator('[data-testid="farm-row"]').evaluateAll((nodes) =>
      nodes.map((node) => ({
        id: node.dataset.fieldId,
        change: node.dataset.change,
        tag: node.querySelector('[data-testid="farm-row-tag"]').textContent.trim(),
        name: node.querySelector('[data-testid="farm-row-plan"]').textContent.trim(),
        sentence: node.querySelector('[data-testid="farm-row-sentence"]').textContent.replace(/\s+/g, " ").trim(),
        crops: [...node.querySelectorAll(".farm-row-year")].map((c) => c.dataset.crop),
        head: node.querySelector(".farm-row-head > span").textContent.replace(/\s+/g, " ").trim(),
      })),
    );
    equal(fieldRows.length, answer.fields.length, `${what}: one row per field`);
    answer.fields.forEach((field, index) => {
      const row = fieldRows[index];
      const change = fieldChange(field);
      equal(row.id, field.field_id, `${what}: row ${index + 1} field`);
      equal(row.head, `${field.field_id} · ${formatAcres(field.area_acres)} acres · ${field.irrigated ? "irrigated" : "dryland"}`, `${what}: ${field.field_id} heading`);
      equal(row.name, field.plan.name, `${what}: ${field.field_id} plan`);
      equal(row.crops.join(","), field.plan.years.join(","), `${what}: ${field.field_id} twelve years`);
      equal(row.change, change, `${what}: ${field.field_id} mark`);
      equal(row.tag, FIELD_TAGS[change], `${what}: ${field.field_id} tag`);
      equal(row.sentence, squash(field.sentence), `${what}: ${field.field_id} sentence`);
      equal(change !== "same", field.differs, `${what}: ${field.field_id} is marked exactly when it differs from its own first pick`);
    });
    const solverShown = await page.locator('[data-testid="farm-solver"]').count();
    equal(solverShown, answer.summary.includes(answer.solver.note) ? 0 : 1, `${what}: the solver line is said once`);
    const all = squash(await one('[data-testid="farm-panel"]').textContent());
    check(!FORBIDDEN.test(all), `${what}: the farm panel uses a word we do not use: ${all.match(FORBIDDEN)?.[0]}`);
    equal(await page.locator('[data-testid="farm-panel"] [data-testid="error-note"]').count(), 0, `${what}: no error box`);
  };

  // --- farm mode: off by default, the toggle turns it on, clicks add and remove -------------
  equal(await one('[data-testid="farm-toggle"]').getAttribute("aria-pressed"), "false", "farm mode is off to start with");
  equal(await page.locator('[data-testid="farm-panel"]').count(), 0, "no farm panel outside farm mode");
  await clickOnMap(DRY);
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${DRY}"]`);
  equal((await onMap()).length, 0, "outside farm mode a click opens the field and picks nothing for a farm");
  await page.click('[data-testid="farm-toggle"]');
  await page.waitForSelector('[data-testid="farm-panel"]');
  equal(await one('[data-testid="app"]').getAttribute("data-mode"), "farm", "farm mode on");
  equal(await page.locator('[data-testid="panel"]').count(), 0, "the farm panel takes the field panel's place");
  check((await text(page, "farm-empty")).length > 0, "the empty farm says what to do");
  equal(farmRequests.length, 0, "nothing is asked for an empty farm");

  const picked = [...new Set([DRY, WET, PATH_FIELD, BRIDGE_FIELD, SETTLED_FIELD])];
  for (const id of picked) await clickOnMap(id);
  await sameSet(picked, "five clicks");
  equal(await page.locator('[data-testid="panel"]').count(), 0, "in farm mode a click does not open the field");
  await clickOnMap(SETTLED_FIELD);
  await sameSet(picked.filter((id) => id !== SETTLED_FIELD), "a second click on a field");
  await clickOnMap(SETTLED_FIELD);
  let ids = [...picked.filter((id) => id !== SETTLED_FIELD), SETTLED_FIELD];
  await sameSet(ids, "a third click");

  // --- box: every field whose centre is inside is added -------------------------------------
  await aim(DRY, 14);
  await page.check('[data-testid="farm-tool-box"]', { force: true });
  await page.waitForSelector('[data-testid="lasso-surface"][data-tool="box"]');
  const frame = await page.evaluate(() => { const r = window.__map.getContainer().getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; });
  const mid = [frame.x + frame.w / 2, frame.y + frame.h / 2];
  const corners = [[mid[0] - 110, mid[1] - 90], [mid[0] + 130, mid[1] + 100]];
  const boxLonLat = await toLonLat([corners[0], [corners[1][0], corners[0][1]], corners[1], [corners[0][0], corners[1][1]]]);
  const inBox = centresIn(boxLonLat);
  check(inBox.length >= 2, `the box should hold a few fields, it holds ${inBox.length}`);
  const releaseBox = await drag(corners);
  check((await one('[data-testid="lasso-surface"] path').getAttribute("d")).length > 0, "the box is drawn while dragging");
  const lassoShot = await shot(page, "phase4-farm-lasso.png");
  await releaseBox();
  ids = addFields(ids, inBox);
  await sameSet(ids, `box around ${inBox.length} centres`);

  // --- freehand loop ----------------------------------------------------------------------------
  await page.check('[data-testid="farm-tool-click"]', { force: true });
  await aim(WET, 14);
  await page.check('[data-testid="farm-tool-loop"]', { force: true });
  await page.waitForSelector('[data-testid="lasso-surface"][data-tool="loop"]');
  const loop = [[mid[0] - 140, mid[1] - 20], [mid[0] - 30, mid[1] - 130], [mid[0] + 150, mid[1] - 60], [mid[0] + 90, mid[1] + 120], [mid[0] - 80, mid[1] + 90]];
  const inLoop = centresIn(await toLonLat(loop));
  check(inLoop.length >= 1, `the loop should hold at least one field, it holds ${inLoop.length}`);
  await (await drag(loop))();
  ids = addFields(ids, inLoop);
  await sameSet(ids, `loop around ${inLoop.length} centres`);
  // a stray click with a lasso tool on picks nothing, and the map does not move under a drag
  const before = await page.evaluate(() => window.__map.getCenter().lat + "," + window.__map.getCenter().lng);
  await page.mouse.click(mid[0], mid[1]);
  await sameSet(ids, "a click with the lasso tool on");
  equal(await page.evaluate(() => window.__map.getCenter().lat + "," + window.__map.getCenter().lng), before, "the map stays put while a lasso tool is on");
  await page.check('[data-testid="farm-tool-click"]', { force: true });
  equal(await page.locator('[data-testid="lasso-surface"]').count(), 0, "the lasso surface is gone with the Click tool");

  // --- the plan, no limits ------------------------------------------------------------------------
  const free = farmRequest(ids, DEFAULT_CHOICES, "", "");
  await settled(free, "no limits");
  const freeAnswer = (await direct(free)).body;
  equal(freeAnswer.status, "no_limits", "API status without limits");
  await checkFarm(freeAnswer, "no limits");

  // --- limits: a slow answer shows the waiting state; a stale one is dropped ---------------------
  const alone = freeAnswer.alone;
  check(alone.peak_water.value > 0, "the test farm should pump water");
  const budget = String(Math.round(0.85 * alone.peak_water.value));
  const minimum = String(Math.round((alone.worst_return.value + alone.average.return) / 200) * 100);
  const staleBody = farmRequest(ids, DEFAULT_CHOICES, "1", "");
  await page.route("**/farm/plan", async (route) => {
    const sent = route.request().postData();
    await new Promise((resolve) => setTimeout(resolve, sent === JSON.stringify(staleBody) ? 3500 : 1200));
    await route.continue().catch(() => {});
  });
  const sentBefore = farmRequests.length;
  await page.fill('[data-testid="farm-water"]', "1");
  await page.waitForSelector('[data-testid="farm-waiting"]');
  equal(await one('[data-testid="farm-result"]').getAttribute("data-updating"), "true", "the old plan is marked as out of date while waiting");
  check((await one('[data-testid="farm-result"]').getAttribute("class")).includes("is-updating"), "the old plan is dimmed while waiting");
  check(/Working out the farm plan/.test(await text(page, "farm-waiting")), "the waiting note says what is happening");
  await page.waitForTimeout(700); // past the debounce: the request for "1" is on its way
  equal(farmRequests.at(-1), JSON.stringify(staleBody), 'the request for a budget of "1" went out');
  await page.fill('[data-testid="farm-water"]', budget);
  await page.fill('[data-testid="farm-minimum"]', minimum);
  const limited = farmRequest(ids, DEFAULT_CHOICES, budget, minimum);
  await settled(limited, "with limits");
  await page.waitForTimeout(3000); // the slow answer for "1" would have landed by now
  equal(await one('[data-testid="farm-result"]').getAttribute("data-query"), JSON.stringify(limited), "a late answer for older inputs does not replace the plan");
  await page.unroute("**/farm/plan");
  check(farmRequests.length - sentBefore <= 3, `typing two limits sent ${farmRequests.length - sentBefore} requests (debounced)`);
  const limitedAnswer = (await direct(limited)).body;
  check(["limits_met", "limits_not_met"].includes(limitedAnswer.status), `API status with limits is ${limitedAnswer.status}`);
  equal(limitedAnswer.limits.water.alone.met, false, "each field alone breaks the budget the test chose");
  await checkFarm(limitedAnswer, "with limits");
  await one('[data-testid="farm-summary"]').evaluate((node) => node.scrollIntoView({ block: "start" }));
  const farmShot = await shot(page, "phase4-farm.png");
  await one('[data-testid="farm-years"]').evaluate((node) => node.scrollIntoView({ block: "start" }));
  const yearsShot = await shot(page, "phase4-farm-years.png");

  // --- a row opens that field; coming back keeps the farm -------------------------------------
  const moved = limitedAnswer.fields.find((field) => field.differs) ?? limitedAnswer.fields[0];
  await page.click(`[data-testid="farm-row"][data-field-id="${moved.field_id}"]`);
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${moved.field_id}"][data-state="ready"]`);
  await page.waitForSelector('[data-testid="plans-region"][data-updating="false"]');
  equal(await page.locator('[data-testid="farm-panel"]').count(), 0, "the field takes the farm panel's place");
  equal(squash(await one('[data-testid="plans"] .plan-name').textContent()), moved.own_pick.name, `${moved.field_id}: its first card is the "own first pick" the farm plan compares with`);
  equal((await onMap()).length, ids.length - 1, "the farm stays highlighted around the opened field");
  await page.click('[data-testid="panel-back"]');
  await page.waitForSelector('[data-testid="farm-panel"]');
  await settled(limited, "back from a field");
  equal((await chips()).join(","), ids.join(","), "the farm is as it was after opening a field");
  equal(await page.inputValue('[data-testid="farm-water"]'), budget, "the budget is as it was");

  // --- limits that cannot be met: the API's explanation, not an error ---------------------------
  const tooMuch = String(Math.round(alone.average.return * 10));
  await page.fill('[data-testid="farm-minimum"]', tooMuch);
  const impossible = farmRequest(ids, DEFAULT_CHOICES, budget, tooMuch);
  await settled(impossible, "a minimum that cannot be met");
  const impossibleAnswer = (await direct(impossible)).body;
  equal(impossibleAnswer.status, "limits_not_met", "API status for ten times the average return");
  equal(impossibleAnswer.limits.return.met, false, "the API says the minimum cannot be met");
  await checkFarm(impossibleAnswer, "cannot be met");
  equal(await text(page, "farm-status"), "Limits cannot all be met", "the headline says so plainly");
  equal(await one('[data-testid="farm-limit-return"]').getAttribute("data-tone"), "cannot", "the minimum is tagged as one that cannot be met");
  await one('[data-testid="farm-summary"]').evaluate((node) => node.scrollIntoView({ block: "start" }));
  const cannotShot = await shot(page, "phase4-farm-cannot.png");

  // --- something that is not a number: said by the box, nothing asked ---------------------------
  const sentBeforeBad = farmRequests.length;
  await page.fill('[data-testid="farm-minimum"]', "lots");
  await page.waitForSelector('[data-testid="farm-minimum-guide"].is-bad');
  equal(await one('[data-testid="farm-minimum"]').getAttribute("aria-invalid"), "true", "the box is marked");
  await page.waitForTimeout(800);
  equal(farmRequests.length, sentBeforeBad, "nothing is asked while a box holds something that is not a number");
  await page.fill('[data-testid="farm-minimum"]', "");

  // --- the sliders and the future are shared with the field view ---------------------------------
  await page.locator('[data-testid="farm-panel"] [data-testid="weight-water"]').focus();
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowRight");
  await page.locator('[data-testid="farm-panel"] [data-testid="scenario-ssp585"]').check({ force: true });
  await page.locator('[data-testid="farm-panel"] [data-testid="horizon"]').focus();
  await page.keyboard.press("End");
  const thirsty = { ...DEFAULT_CHOICES, weights: { ...DEFAULT_CHOICES.weights, water: DEFAULT_CHOICES.weights.water + 5 }, scenario: "ssp585", horizon: 2060 };
  const later = farmRequest(ids, thirsty, budget, "");
  await settled(later, "other priorities and another future");
  const laterAnswer = (await direct(later)).body;
  equal(laterAnswer.settings.horizon, 2060, "API horizon");
  await checkFarm(laterAnswer, "other priorities, 2060");
  const climate = await (await get(`/field/${ids[0]}/plans?${plansQuery(thirsty)}`)).json();
  check((await text(page, "climate-line")).startsWith(climate.climate.label), "the climate line is the API's label for that future");
  equal(await page.locator('[data-testid="farm-panel"] [data-testid="irrigation-switch"]').count(), 0, "no irrigation switch on the farm panel");
  await page.click(`[data-testid="farm-row"][data-field-id="${ids[0]}"]`);
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${ids[0]}"][data-state="ready"]`);
  await page.waitForSelector(`[data-testid="plans-region"][data-updating="false"][data-query="${plansQuery(thirsty)}"]`);
  equal(squash(await one('[data-testid="plans"] .plan-name').textContent()), laterAnswer.fields[0].own_pick.name, "the field view uses the farm's sliders and future");
  await page.keyboard.press("Escape");
  await page.waitForSelector('[data-testid="farm-panel"]');

  // --- too many fields: the API's sentence -------------------------------------------------------
  await page.evaluate(() => window.__map.setZoom(11, { animate: false }));
  await page.waitForTimeout(400);
  await page.check('[data-testid="farm-tool-box"]', { force: true });
  await page.waitForSelector('[data-testid="lasso-surface"]');
  const wide = [[mid[0] - 150, mid[1] - 150], [mid[0] + 150, mid[1] + 150]];
  const inWide = centresIn(await toLonLat([wide[0], [wide[1][0], wide[0][1]], wide[1], [wide[0][0], wide[1][1]]]));
  await (await drag(wide))();
  const many = addFields(ids, inWide);
  check(many.length > laterAnswer.settings.max_fields, `the wide box should pick more than ${laterAnswer.settings.max_fields} fields, it picked ${many.length}`);
  await page.waitForSelector('[data-testid="farm-refusal"]');
  const refused = await direct(farmRequest(many, thirsty, budget, ""));
  equal(refused.status, 400, "API refuses the big farm");
  equal(await text(page, "farm-refusal"), refused.body.detail, "the refusal is the API's own sentence");
  equal(await page.locator('[data-testid="farm-result"]').count(), 0, "no plan for another farm is left under a refusal");
  equal(await page.locator('[data-testid="farm-panel"] [data-testid="error-note"]').count(), 0, "a refusal is not shown as an error");
  equal((await onMap()).length, many.length, `${many.length} fields highlighted`);
  await page.click('[data-testid="farm-clear"]');
  await sameSet([], "Clear");
  check((await text(page, "farm-empty")).length > 0, "after Clear the panel is empty again");

  // --- leaving farm mode: the map is as before ---------------------------------------------------
  await page.check('[data-testid="farm-tool-click"]', { force: true });
  await clickOnMap(DRY);
  await sameSet([DRY], "one field again");
  await page.click('[data-testid="farm-toggle"]');
  equal(await page.locator('[data-testid="farm-panel"]').count(), 0, "the farm panel closes with farm mode");
  equal((await onMap()).length, 0, "no farm highlight outside farm mode");
  equal(await page.locator('[data-testid="lasso-surface"]').count(), 0, "no lasso outside farm mode");
  await clickOnMap(WET);
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${WET}"][data-state="ready"]`);
  equal(await page.locator('[data-testid="panel-back"]').count(), 0, "outside farm mode a field has no way back to a farm");
  const problems = monitor.problems.filter((line) => !/farm\/plan|status of 400/.test(line));
  check(problems.length === 0, `console errors on the farm screen:\n  ${problems.join("\n  ")}`);
  await context.close();
  log(`  ✓ farm screen: clicks add and remove, box (${inBox.length}) and loop (${inLoop.length}) add by centre; ${ids.length} fields planned = API ` +
      `(no limits, ${limitedAnswer.status}, cannot be met, 2060); waiting state, stale answer dropped, debounced; rows open fields; ` +
      `${many.length} fields get the API's refusal → ${lassoShot}, ${farmShot}, ${yearsShot}, ${cannotShot}`);

  // --- phone: a farm kept in the browser comes back; nothing scrolls sideways ---------------------
  const phoneContext = await browser.newContext({ viewport: PHONE, deviceScaleFactor: 2, hasTouch: true, isMobile: true });
  await phoneContext.addInitScript(
    ([key, farm]) => window.localStorage.getItem(key) === null && window.localStorage.setItem(key, JSON.stringify(farm)),
    ["crop-rotation-farm", { ids, water: budget, minimum }],
  );
  const phone = await phoneContext.newPage();
  phone.setDefaultTimeout(Math.max(TIMEOUT, 40000));
  const phoneMonitor = watch(phone, "farm phone");
  await phone.goto(`${WEB_URL}/`, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await phone.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });
  equal(await phone.locator('[data-testid="farm-panel"]').count(), 0, "phone: the app still starts on the plain map");
  await phone.tap('[data-testid="farm-toggle"]');
  await phone.waitForFunction((query) => document.querySelector('[data-testid="farm-result"]')?.dataset.query === query, JSON.stringify(limited), { timeout: 45000 });
  equal((await phone.locator('[data-testid="farm-chip"]').count()), ids.length, "phone: the farm kept in the browser is back");
  equal(await phone.inputValue('[data-testid="farm-water"]'), budget, "phone: so is the water budget");
  const fit = await phone.evaluate(() => {
    const body = document.querySelector('[data-testid="farm-panel"] .panel-body');
    const frame = body.getBoundingClientRect();
    const clipped = [...body.querySelectorAll("p, li, td, th, button, figure, table, input, h3, h4")]
      .filter((node) => !node.closest(".farm-scroll") && !node.closest(".farm-chips"))
      .filter((node) => { const box = node.getBoundingClientRect(); return box.width > 0 && (box.left < frame.left - 1 || box.right > frame.right + 1); }).length;
    return { page: document.documentElement.scrollWidth, sideways: body.scrollWidth - body.clientWidth, clipped };
  });
  check(fit.page <= PHONE.width, `phone: the page is ${fit.page}px wide`);
  check(fit.sideways <= 1, `phone: the farm panel scrolls sideways by ${fit.sideways}px`);
  equal(fit.clipped, 0, "phone: nothing in the farm panel is cut off at the sides");
  const phoneTop = await shot(phone, "phase4-farm-phone.png");
  await phone.evaluate(() => document.querySelector('[data-testid="farm-compare"]').scrollIntoView({ block: "start" }));
  const phoneTable = await shot(phone, "phase4-farm-phone-plan.png");
  await phone.evaluate(() => document.querySelector('[data-testid="farm-rows"]').scrollIntoView({ block: "start" }));
  const phoneRows = await shot(phone, "phase4-farm-phone-fields.png");
  await phone.tap(`[data-testid="farm-row"][data-field-id="${ids[0]}"]`);
  await phone.waitForSelector(`[data-testid="panel"][data-field-id="${ids[0]}"]`);
  await phone.tap('[data-testid="panel-back"]');
  await phone.waitForSelector('[data-testid="farm-panel"]');
  check(phoneMonitor.problems.length === 0, `console errors on the phone farm screen:\n  ${phoneMonitor.problems.join("\n  ")}`);
  await phoneContext.close();
  log(`  ✓ farm screen at phone width: the saved farm comes back, no sideways scroll, rows open fields → ${phoneTop}, ${phoneTable}, ${phoneRows}`);
}

async function run(browser) {
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
  const monitor = watch(page, "desktop");
  try {
    await openField(page, DRY);
  } catch (error) {
    throw new Failure(`could not open ${WEB_URL}/#${DRY}: is the dev server running and is this field in the data? (${error.message.split("\n")[0]})`);
  }
  const apiBase = await page.getAttribute('[data-testid="app"]', "data-api-base");
  const apiPrefix = new URL(apiBase, `${WEB_URL}/`).href.replace(/\/+$/, "");
  const get = (pathAndQuery) => fetch(`${apiPrefix}${pathAndQuery}`);
  const ask = async (fieldId, query) => {
    const response = await get(`/field/${fieldId}/score?${query}`);
    if (!response.ok) throw new Failure(`${fieldId}/score?${query} answered ${response.status}`);
    return response.json();
  };
  const askPlans = async (fieldId, query) => (await get(`/field/${fieldId}/plans?${query}`)).json();
  const shot = async (target, name) => {
    await target.screenshot({ path: path.join(SHOTS, name) });
    return `e2e/screenshots/${name}`;
  };
  const toSection = (target) =>
    target.evaluate(() => {
      document.querySelector("#section-own").scrollIntoView({ block: "start" });
      return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    });

  const defaults = plansQuery(DEFAULT_CHOICES);
  const plans = await askPlans(DRY, defaults);
  if (plans.engine !== "smart") {
    // No climate tables on this server: the feature is hidden and the endpoint says why.
    equal(await page.locator('[data-testid="own-rotation"]').count(), 0, "basic planner: Try your own rotation is hidden");
    equal(await page.locator('[data-testid="plan-edit"]').count(), 0, "basic planner: no Edit this plan");
    equal(await page.locator('[data-testid="how-toggle"]').count(), 0, "basic planner: no How to get there");
    equal((await get(`/field/${DRY}/transition?plan=1`)).status, 503, "basic planner: /transition answers 503");
    const refused = await get(`/field/${DRY}/score?rotation=corn,wheat`);
    equal(refused.status, 503, "basic planner: /score answers 503");
    check(((await refused.json()).detail ?? "").includes("future-weather"), "basic planner: the 503 gives a plain reason");
    log("phase 4 test: this server answers with the basic planner; checked that the feature is hidden. Nothing else to test.");
    return;
  }
  log(`phase 4 test: ${WEB_URL} (API ${apiPrefix}), dryland ${DRY}, irrigated ${WET}`);

  // ---- 1. the empty builder ----------------------------------------------------------------------
  equal(await page.locator('[data-testid="own-rotation"]').count(), 1, "Try your own rotation section");
  equal(await text(page, "own-rotation").then((t) => t.startsWith("Try your own rotation")), true, "section title");
  equal((await slotsOnScreen(page)).join("|"), "|||", "four empty years to start with");
  equal(await page.locator('[data-testid="own-result"]').getAttribute("data-state"), "empty", "nothing scored before a crop is picked");
  check((await text(page, "own-hint")).includes("Pick a crop for every year"), "the empty builder gives no hint");
  equal(await page.locator('[data-testid="own-table"]').count(), 0, "no table before a rotation is complete");
  equal(monitor.scoreRequests.length, 0, "no /score request before a rotation is complete");
  equal(await page.locator('[data-testid="own-clear"]').isDisabled(), true, "Clear is disabled on the untouched builder");
  for (let year = 1; year <= 4; year++) {
    check(await page.getByLabel(`Crop for year ${year}`, { exact: true }).isVisible(), `year ${year} has no labelled crop picker`);
  }
  equal(await page.locator('[data-testid="plan-edit"]').count(), 3, '"Edit this plan" on each card');

  // ---- 2. pick four crops: scored like the plans, next to them -----------------------------------
  let slots = [C, S, W, F];
  await pick(page, slots.slice(0, 3));
  equal(await page.locator('[data-testid="own-result"]').getAttribute("data-state"), "empty", "still nothing scored with one year empty");
  await pick(page, slots);
  let query = scoreQuery(defaults, slots);
  equal(query, `rotation=${encodeURIComponent(slots.join(","))}&${defaults}`, "the query is the rotation plus the plan cards' settings");
  await scored(page, query, "four picked crops");
  equal(monitor.scoreRequests.at(-1), `${DRY}?${query}`, "request sent for the picked rotation");
  const first = await ask(DRY, query);
  equal(JSON.stringify(first.plans), JSON.stringify(plans.plans), "the API's top picks next to yours are the /plans answer");
  await checkResult(page, first, "dryland, picked");
  check(first.comparison.headline.length >= 2 && first.comparison.rows.length === 5, "the API answer lacks a headline or the five rows");
  check(/number \d+ of \d+|come first|come after all/.test(first.comparison.headline[0]) || first.yours.same_as_pick !== null, `the headline does not say where yours ranks ("${first.comparison.headline[0]}")`);
  const chipColour = await page.locator('[data-testid="own-chip-0"]').evaluate((node) => getComputedStyle(node).backgroundColor);
  const cardColour = await page.locator(`[data-testid="plan-chip"][data-crop="${slots[0]}"], [data-testid="timeline"] [data-crop="${slots[0]}"]`).first().evaluate((node) => getComputedStyle(node).backgroundColor).catch(() => null);
  check(chipColour !== "rgb(255, 255, 255)", "a picked year is not shown in its crop colour");
  if (cardColour) equal(chipColour, cardColour, "a picked year has the crop's colour from the plan cards");
  equal(await text(page, "own-chip-2"), "Winter Wheat", "the chip shows the crop's full name");
  await toSection(page);
  log(`  ✓ four picked crops: table, marks and every sentence match the API (yours is number ${first.yours.rank} of ${first.yours.rank_of}) → ${await shot(page, "phase4-own-rotation.png")}`);

  // ---- 3. reorder with the keyboard: focus stays on the year that moved ---------------------------
  await page.locator('[data-testid="own-later-0"]').focus();
  await page.keyboard.press("Enter");
  slots = moveSlot(slots, 0, 1);
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "years after Move later on year 1");
  equal(await page.evaluate(() => document.activeElement?.getAttribute("data-testid")), "own-later-1", "focus follows the year that moved");
  check((await text(page, "own-announcement")).includes("moved to year 2"), "the move is not announced");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  slots = moveSlot(moveSlot(slots, 1, 2), 2, 3);
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "years after moving corn to the last year");
  equal(await page.evaluate(() => document.activeElement?.getAttribute("data-testid")), "own-earlier-3", "at the last year the focus moves to Move earlier");
  equal(await page.locator('[data-testid="own-later-3"]').isDisabled(), true, "Move later is disabled on the last year");
  equal(await page.locator('[data-testid="own-earlier-0"]').isDisabled(), true, "Move earlier is disabled on the first year");
  query = scoreQuery(defaults, slots);
  await scored(page, query, "after keyboard moves");
  const rotated = await ask(DRY, query);
  await checkResult(page, rotated, "dryland, started a year later");
  // the same cycle started in another year: the same numbers
  equal(JSON.stringify(rotated.yours.scores), JSON.stringify(first.yours.scores), "a cycle started in another year scores the same");
  log("  ✓ Move earlier / later buttons work from the keyboard, keep the focus, and a cycle started a year later scores the same");

  // ---- 4. drag a year onto another ----------------------------------------------------------------
  await page.locator('[data-testid="own-drag-3"]').dragTo(page.locator('[data-testid="own-slot"]').nth(0));
  slots = moveSlot(slots, 3, 0);
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "years after dragging year 4 onto year 1");
  await page.locator('[data-testid="own-drag-0"]').dragTo(page.locator('[data-testid="own-slot"]').nth(2));
  slots = moveSlot(slots, 0, 2);
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "years after dragging year 1 onto year 3");
  query = scoreQuery(defaults, slots);
  await scored(page, query, "after dragging");
  await checkResult(page, await ask(DRY, query), "dryland, after dragging");
  log(`  ✓ dragging a year onto another reorders them: ${slots.join(", ")}`);

  // ---- 5. add and remove years (2 to 6) -----------------------------------------------------------
  await page.locator('[data-testid="own-add"]').click();
  equal((await slotsOnScreen(page)).length, 5, "years after Add a year");
  equal(await page.locator('[data-testid="own-result"]').getAttribute("data-state"), "empty", "an empty added year: nothing scored, no stale table");
  equal(await page.locator('[data-testid="own-table"]').count(), 0, "the old table is gone while a year is empty");
  await page.locator('[data-testid="own-crop-4"]').selectOption(S);
  await page.locator('[data-testid="own-add"]').click();
  await page.locator('[data-testid="own-crop-5"]').selectOption(F);
  slots = [...slots, S, F];
  equal(await page.locator('[data-testid="own-add"]').isDisabled(), true, "Add a year is disabled at six years");
  query = scoreQuery(defaults, slots);
  await scored(page, query, "six years");
  const six = await ask(DRY, query);
  equal(six.yours.cycle.length, 6, "the API scored six years");
  await checkResult(page, six, "dryland, six years");
  check(six.yours.name.includes("6-year cycle"), `a six-year rotation is not named as one ("${six.yours.name}")`);
  for (const index of [5, 4, 3, 2]) await page.locator(`[data-testid="own-remove-${index}"]`).click();
  slots = slots.slice(0, 2);
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "years after removing four");
  equal(await page.locator('[data-testid="own-remove-0"]').isDisabled(), true, "Remove is disabled at two years");
  query = scoreQuery(defaults, slots);
  await scored(page, query, "two years");
  await checkResult(page, await ask(DRY, query), "dryland, two years");
  log("  ✓ years can be added up to six and removed down to two; an empty year hides the old table");

  // ---- 6. typing a rotation ----------------------------------------------------------------------
  const typed = "Wheat, milo, idle";
  await page.locator('[data-testid="own-text"]').fill(typed);
  await page.keyboard.press("Enter");
  const understood = (await ask(DRY, scoreQuery(defaults, typed))).understood;
  equal(understood.join(","), [W, S, F].join(","), "what the API understands from common names");
  slots = understood;
  query = scoreQuery(defaults, slots);
  await scored(page, query, "typed rotation");
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "the slots show what the API understood");
  const typedBody = await ask(DRY, query);
  await checkResult(page, typedBody, "dryland, typed");
  // nothing planted (fallow every year): not rankable. No rank, no "would come first", no mark on yours;
  // one plain sentence with its own figures instead.
  const idle = scoreQuery(defaults, [F, F]);
  await page.locator('[data-testid="own-text"]').fill("fallow, fallow");
  await page.keyboard.press("Enter");
  await scored(page, idle, "nothing planted");
  const idleBody = await ask(DRY, idle);
  equal(idleBody.yours.rankable, false, "nothing planted: the API says it is not rankable");
  equal(idleBody.yours.rank, null, "nothing planted: no rank");
  equal(idleBody.yours.rank_of, null, "nothing planted: no rank out of anything");
  equal(idleBody.comparison.verdict, "not_compared", "nothing planted: not compared with our first pick");
  equal(idleBody.comparison.headline.length, 1, "nothing planted: one plain sentence");
  const idleSentence = idleBody.comparison.headline[0];
  check(idleSentence.includes("plants no crop") && idleSentence.includes("returns nothing"), `nothing planted: the sentence does not say it plants and returns nothing ("${idleSentence}")`);
  check(idleSentence.includes(`bare about ${Math.round(idleBody.yours.numbers.bare_days)} days a year`), `nothing planted: the sentence does not carry its bare days ("${idleSentence}")`);
  check(idleSentence.includes(`$${Math.abs(Math.round(idleBody.yours.numbers.profit_per_acre))} an acre a year to keep fallow`), `nothing planted: the sentence does not carry the fallow charge ("${idleSentence}")`);
  check(Object.values(idleBody.marks).every((columns) => !columns.includes(0)), "nothing planted: the API marks a figure of yours");
  await checkResult(page, idleBody, "dryland, nothing planted");
  equal(await page.locator('[data-testid="own-table"] td[data-testid$="-0"][data-best="true"]').count(), 0, "nothing planted: no better-value mark in the Yours column");
  equal(await text(page, "own-cell-robust-0"), "not ranked", "nothing planted: no ranking score for yours");
  const idleWhy = await text(page, "own-why");
  check(idleWhy.startsWith("Why yours is not ranked"), `nothing planted: the section is not headed "Why yours is not ranked" ("${idleWhy.slice(0, 40)}")`);
  check(!/come first|ahead of all|number \d+ of/i.test(idleWhy), `nothing planted: the section still ranks it ("${idleWhy.match(/come first|ahead of all|number \d+ of/i)?.[0]}")`);
  await page.evaluate(() => document.querySelector('[data-testid="own-table"]').scrollIntoView({ block: "start" }));
  await shot(page, "phase4-own-rotation-nothing.png");
  await page.locator('[data-testid="own-text"]').fill(typed);
  await page.keyboard.press("Enter");
  await scored(page, query, "typed rotation again");
  // a crop we have no numbers for: the API's own sentence, and the rotation stays as it was
  await page.locator('[data-testid="own-text"]').fill("cotton, wheat");
  await page.locator('[data-testid="own-text-submit"]').click();
  await page.waitForSelector('[data-testid="own-text-error"]');
  const refused = await get(`/field/${DRY}/score?${scoreQuery(defaults, "cotton, wheat")}`);
  equal(refused.status, 400, "the API refuses a crop it has no numbers for");
  const detail = (await refused.json()).detail;
  check(/cotton/.test(detail) && [C, B, W, S, A, F].every((crop) => detail.includes(crop)), `the refusal does not list the six crops ("${detail}")`);
  equal(await text(page, "own-text-error"), detail, "the refusal shown is the API's sentence");
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "a refused rotation leaves the slots alone");
  equal(await page.locator('[data-testid="own-result"]').getAttribute("data-query"), query, "and the table too");
  await page.locator('[data-testid="own-text"]').fill("corn sorghum");
  equal(await page.locator('[data-testid="own-text-error"]').count(), 0, "the refusal goes away when the text changes");
  log(`  ✓ typed "${typed}" is read as ${slots.join(", ")}; "cotton, wheat" gets the API's sentence: "${detail}"`);

  // ---- 7. start from what the field grows now ----------------------------------------------------
  const startCurrent = page.locator('[data-testid="own-start-current"]');
  if (plans.current) {
    await startCurrent.click();
    slots = plans.current.cycle;
    equal((await slotsOnScreen(page)).join(","), slots.join(","), "slots after Start from what this field grows now");
    query = scoreQuery(defaults, slots);
    await scored(page, query, "the field's own pattern");
    await checkResult(page, await ask(DRY, query), "dryland, what the field grows now");
    log(`  ✓ "Start from what this field grows now" loads ${slots.join(", ")}`);
  } else {
    equal(await startCurrent.count(), 0, "no Start from what this field grows now when the current pattern is not scored");
    log("  ✓ this field's current pattern is not scored, so the start button is not offered");
  }

  // ---- 8. Edit this plan -------------------------------------------------------------------------
  await page.locator('[data-testid="plan-edit"]').nth(1).click();
  slots = plans.plans[1].cycle;
  equal((await slotsOnScreen(page)).join(","), slots.join(","), "slots after Edit this plan on the second card");
  query = scoreQuery(defaults, slots);
  await scored(page, query, "our second plan");
  const second = await ask(DRY, query);
  equal(second.yours.same_as_pick, 2, "the API recognises our second pick");
  await checkResult(page, second, "dryland, our second plan");
  for (const row of ["robust", "soil", "water", "money", "risk", "holds", "bare_days", "water_use", "profit", "failed"]) {
    equal(await text(page, `own-cell-${row}-0`), await text(page, `own-cell-${row}-2`), `our second plan as yours: ${row} equals the Our 2 column`);
  }
  const inView = await page.evaluate(() => {
    const body = document.querySelector(".panel-body").getBoundingClientRect();
    const top = document.querySelector('[data-testid="own-rotation"]').getBoundingClientRect().top;
    return top >= body.top - 2 && top < body.bottom - 100;
  });
  check(inView, "Edit this plan did not bring the section into view");
  equal(await page.evaluate(() => document.activeElement?.id), "section-own", "Edit this plan moves the focus to the section");
  await page.locator('[data-testid="own-clear"]').click();
  equal((await slotsOnScreen(page)).join("|"), "|||", "Clear empties the years");
  await page.locator('[data-testid="plan-edit"]').nth(0).click();
  slots = plans.plans[0].cycle;
  query = scoreQuery(defaults, slots);
  await scored(page, query, "our first plan");
  const ourFirst = await ask(DRY, query);
  equal(ourFirst.yours.same_as_pick, 1, "the API recognises our first pick");
  equal(ourFirst.comparison.headline.join(" "), "Yours is our first pick for this field with these priorities.", "headline for our own first pick");
  await checkResult(page, ourFirst, "dryland, our first plan");
  log('  ✓ "Edit this plan" loads a card\'s rotation, brings the section into view, and the API says which pick it is');

  // ---- 9. follows the priorities, the future, the scenario and the irrigation switch --------------
  slots = [C, S, W, F];
  await page.locator('[data-testid="own-text"]').fill(slots.join(", "));
  await page.locator('[data-testid="own-text-submit"]').click();
  await scored(page, scoreQuery(defaults, slots), "typed again");
  const before = monitor.scoreRequests.length;
  await page.locator('[data-testid="horizon"]').focus();
  for (let i = 0; i < 7; i++) await page.keyboard.press("ArrowRight");
  await page.locator('[data-testid="scenario-ssp585"]').check();
  await page.locator('[data-testid="weight-money"]').focus();
  for (let i = 0; i < 7; i++) await page.keyboard.press("ArrowRight");
  const choices = { weights: { ...DEFAULT_CHOICES.weights, money: 10 }, horizon: 2060, scenario: "ssp585" };
  query = scoreQuery(plansQuery(choices), slots);
  await scored(page, query, "2060, high emissions, money 10");
  await page.waitForTimeout(400); // nothing late may replace it
  await scored(page, query, "2060, high emissions, money 10, after waiting");
  const future = await ask(DRY, query);
  equal(future.settings.horizon, 2060, "API horizon");
  equal(future.settings.scenario, "ssp585", "API scenario");
  await checkResult(page, future, "dryland, 2060 high emissions, money 10");
  const sent = monitor.scoreRequests.length - before;
  check(sent >= 1 && sent < 15, `15 control changes sent ${sent} /score requests: the debounce is not working`);
  equal(monitor.scoreRequests.at(-1), `${DRY}?${query}`, "last /score request sent");
  check(JSON.stringify(future.yours.scores) !== JSON.stringify(first.yours.scores), "the future and the priorities did not change the scores");
  // irrigation switch: the same rotation under the irrigated rules and figures
  await page.locator('[data-testid="irrigation-switch"]').focus();
  await page.keyboard.press("Space");
  query = scoreQuery(plansQuery(choices, true), slots);
  check(query.endsWith("&irrigated=true"), "override query");
  await scored(page, query, "irrigation override");
  const overridden = await ask(DRY, query);
  equal(overridden.settings.irrigated, true, "API treats the field as irrigated when told to");
  await checkResult(page, overridden, "dryland field overridden to irrigated");
  await page.locator('[data-testid="irrigation-reset"]').click();
  await page.locator('[data-testid="weights-reset"]').click();
  await page.locator('[data-testid="horizon"]').focus();
  await page.keyboard.press("Home");
  await page.locator('[data-testid="scenario-ssp245"]').check();
  query = scoreQuery(defaults, slots);
  await scored(page, query, "back at the defaults");
  log(`  ✓ the table follows the priorities, the future, the scenario and the irrigation switch (15 quick changes → ${sent} ${sent === 1 ? "request" : "requests"})`);

  // ---- 10. a failed update keeps the old table and says so ----------------------------------------
  const failing = (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Made-up failure for the test." }) });
  const hits2030 = (url) => /\/score$/.test(url.pathname) && url.search.includes("horizon=2030");
  await page.route(hits2030, failing);
  await page.locator('[data-testid="horizon"]').focus();
  await page.keyboard.press("ArrowRight");
  await page.waitForSelector('[data-testid="own-update-error"]');
  check((await text(page, "own-update-error")).includes("Made-up failure for the test."), "the error line does not carry the API's sentence");
  equal(await page.locator('[data-testid="own-table"]').count(), 1, "table kept while the update failed");
  equal(await page.locator('[data-testid="own-result"]').getAttribute("data-query"), query, "table still the previous answer");
  await page.unroute(hits2030, failing);
  await page.keyboard.press("ArrowLeft");
  await scored(page, query, "after the failed update");
  equal(await page.locator('[data-testid="own-update-error"]').count(), 0, "error line gone after a good answer");
  log("  ✓ a failed update keeps the previous table with a plain error line");

  // ---- 11. an irrigated field: forgotten rotation, inches of water --------------------------------
  await page.evaluate((id) => (window.location.hash = id), WET);
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${WET}"][data-state="ready"]`);
  await page.waitForSelector('[data-testid="own-rotation"]');
  equal((await slotsOnScreen(page)).join("|"), "|||", "the rotation built for one field is not carried to the next");
  equal(await page.locator('[data-testid="own-text"]').inputValue(), "", "nor is the typed text");
  slots = [C, C, B, W];
  await pick(page, slots);
  query = scoreQuery(defaults, slots);
  await scored(page, query, "irrigated field");
  const wet = await ask(WET, query);
  equal(wet.settings.irrigated, true, `${WET} should be treated as irrigated by the API`);
  await checkResult(page, wet, "irrigated field");
  check(/^\d+(\.\d)? in$/.test(await text(page, "own-cell-water_use-0")), `water pumped is not in inches ("${await text(page, "own-cell-water_use-0")}")`);
  const wetPlans = await askPlans(WET, defaults);
  if (wetPlans.current) {
    await page.locator('[data-testid="own-start-current"]').click();
    const grown = wetPlans.current.cycle;
    equal((await slotsOnScreen(page)).join(","), grown.join(","), "irrigated field: slots after Start from what this field grows now");
    await scored(page, scoreQuery(defaults, grown), "irrigated field, its own pattern");
    await checkResult(page, await ask(WET, scoreQuery(defaults, grown)), "irrigated field, what it grows now");
    await pick(page, slots);
    await scored(page, query, "irrigated field, picked again");
  } else {
    equal(await page.locator('[data-testid="own-start-current"]').count(), 0, "irrigated field: no start button without a scored current pattern");
  }
  await toSection(page);
  const wetShot = await shot(page, "phase4-own-rotation-irrigated.png");
  await page.evaluate(() => document.querySelector('[data-testid="own-why"]').scrollIntoView({ block: "start" }));
  await shot(page, "phase4-own-rotation-why.png");
  const realProblems = monitor.problems.filter((p) => !p.includes("500 (Internal Server Error)") && !p.includes("400 (Bad Request)")); // the failures this test made
  check(realProblems.length === 0, `console errors on desktop:\n  ${realProblems.join("\n  ")}`);
  log(`  ✓ irrigated field: slots start empty, water in inches, well qualifier shown; no console errors → ${wetShot}`);
  await context.close();

  // ---- 12. the server cannot score (503): the section hides itself, the rest stays ----------------
  const plainContext = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const plain = await plainContext.newPage();
  plain.setDefaultTimeout(TIMEOUT);
  await plain.route((url) => /\/score$/.test(url.pathname), (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Scoring your own rotation needs the future-weather tables, which are not set up on this server." }) }));
  await openField(plain, DRY);
  await pick(plain, [C, S, W, F]);
  await plain.waitForSelector('[data-testid="own-rotation"]', { state: "detached" });
  equal(await plain.locator('[data-testid="plan-card"]').count(), 3, "plan cards stay when the score endpoint answers 503");
  await plainContext.close();
  log("  ✓ when /score answers 503 the section hides itself and the plan cards stay");

  // ---- 13. phone width ---------------------------------------------------------------------------
  const phoneContext = await browser.newContext({ viewport: PHONE, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const phone = await phoneContext.newPage();
  phone.setDefaultTimeout(TIMEOUT);
  const phoneMonitor = watch(phone, "phone");
  await openField(phone, WET);
  slots = [A, A, A, A, C, W];
  await phone.locator('[data-testid="own-text"]').fill("alfalfa alfalfa alfalfa alfalfa corn wheat");
  await phone.locator('[data-testid="own-text-submit"]').tap();
  query = scoreQuery(defaults, slots);
  await scored(phone, query, "phone, six years typed");
  await phone.locator('[data-testid="own-earlier-5"]').tap(); // the touch way to reorder
  slots = moveSlot(slots, 5, 4);
  query = scoreQuery(defaults, slots);
  await scored(phone, query, "phone, after Move earlier");
  await checkResult(phone, await ask(WET, query), "phone, six years");
  const fit = await phone.evaluate(() => {
    const body = document.querySelector(".panel-body");
    const clipped = [...document.querySelectorAll(".own, .own-slots, .own-slot, .own-tools, .own-actions, .own-text-row, .own-key, .own-table-wrap, .own-table, .own-why, .own-compare-head")]
      .filter((node) => node.scrollWidth > node.clientWidth + 1)
      .map((node) => node.className);
    const small = [...document.querySelectorAll(".own-tool, .own-select")].filter((node) => node.getBoundingClientRect().height < 28).length;
    const cut = [...document.querySelectorAll(".own-chip-text")].filter((node) => node.scrollWidth > node.clientWidth + 1).length;
    return { page: document.documentElement.scrollWidth, body: body.scrollWidth - body.clientWidth, clipped, small, cut };
  });
  check(fit.page <= PHONE.width, `phone: the page scrolls sideways (${fit.page}px wide)`);
  check(fit.body <= 0, `phone: the panel scrolls sideways by ${fit.body}px`);
  equal(fit.clipped.join(","), "", "phone: clipped parts of Try your own rotation");
  equal(fit.small, 0, "phone: slot controls under 28px tall");
  equal(fit.cut, 0, "phone: crop names cut off in the year slots");
  await toSection(phone);
  const phoneShot = await shot(phone, "phase4-own-rotation-phone.png");
  await phone.evaluate(() => document.querySelector('[data-testid="own-table"]').scrollIntoView({ block: "start" }));
  const phoneTable = await shot(phone, "phase4-own-rotation-phone-table.png");
  check(phoneMonitor.problems.length === 0, `console errors on the phone:\n  ${phoneMonitor.problems.join("\n  ")}`);
  await phoneContext.close();
  log(`  ✓ phone width: slots wrap, the table fits, no sideways scroll → ${phoneShot}, ${phoneTable}`);

  await runTransition(browser, { get, defaults, shot });
  await runFarm(browser, { get, apiPrefix, defaults });
  await runFarmScreen(browser, { get, apiPrefix, shot });
}

let browser;
try {
  browser = await chromium.launch();
} catch (error) {
  console.error(`FAIL: could not start Chromium (${error.message.split("\n")[0]}). Run: npx playwright install chromium`);
  process.exit(1);
}
try {
  await run(browser);
} catch (error) {
  console.error("");
  console.error(`FAIL: ${error instanceof Failure ? error.message : (error.stack ?? error)}`);
  process.exitCode = 1;
} finally {
  await browser.close();
}
if (!process.exitCode) {
  console.log("");
  console.log(`PASS: ${checks} checks`);
}
