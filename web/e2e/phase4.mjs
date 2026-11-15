// End-to-end checks of the Phase 4 features, against the running app.
//
//   npm run e2e:phase4          (with `make dev` running)
//
// Item 1, "Try your own rotation": fills the year slots by picking, moving, dragging and typing,
// and compares the side-by-side table, the marks and every sentence with what the API answers for
// the same rotation and settings (asked directly, never hard-coded). Screenshots go to
// e2e/screenshots/phase4-*.png.
//
// Environment: WEB_URL (default http://localhost:5173), E2E_TIMEOUT_MS (default 20000),
// DRY_FIELD (default KS-013847), IRRIGATED_FIELD (default KS-014376).
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { columnLabel, moveSlot, rangeText, scoreQuery, verdictLabel } from "../src/lib/custom.ts";
import { DEFAULT_CHOICES, formatDollars, formatInches, formatShare, plansQuery, wellQualifier } from "../src/lib/planner.ts";

const WEB_URL = (process.env.WEB_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const TIMEOUT = Number.parseInt(process.env.E2E_TIMEOUT_MS ?? "20000", 10) || 20000;
const DRY = process.env.DRY_FIELD ?? "KS-013847";
const WET = process.env.IRRIGATED_FIELD ?? "KS-014376";
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
  });
  return { problems, scoreRequests };
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
  return {
    robust: each((p) => score(p.scores.robust)),
    soil: each((p) => score(p.scores.soil)),
    water: each((p) => score(p.scores.water)),
    money: each((p) => score(p.scores.money)),
    risk: each((p) => score(p.scores.risk)),
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
  const broken = page.locator('[data-testid="own-broken"] li');
  equal((await broken.allTextContents()).map(squash).join(" | "), body.yours.rules_broken.join(" | "), `${what}: what our plans avoid`);
  check(body.yours.rules_broken.every((sentence) => sentence.startsWith("Our plans avoid")), `${what}: a broken rule is not worded gently`);
  equal((await texts(page, '[data-testid="own-notes"] li')).join(" | "), comparison.notes.join(" | "), `${what}: notes`);
  const all = await text(page, "own-rotation");
  check(!/soil health|improves? the soil|\bprofit\b|\bincome\b/i.test(all), `${what}: the section uses a word it must not ("${all.match(/soil health|improves? the soil|\bprofit\b|\bincome\b/i)?.[0]}")`);
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
  log(`  ✓ four picked crops: table, marks and every sentence match the API (yours would be number ${first.yours.rank} of ${first.yours.rank_of}) → ${await shot(page, "phase4-own-rotation.png")}`);

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
  // nothing planted: no "share of crops" figures for yours, and none of them marked
  const idle = scoreQuery(defaults, [F, F]);
  await page.locator('[data-testid="own-text"]').fill("fallow, fallow");
  await page.keyboard.press("Enter");
  await scored(page, idle, "nothing planted");
  const idleBody = await ask(DRY, idle);
  check(!idleBody.marks.failed.includes(0) && !idleBody.marks.water_use.includes(0), "nothing planted: no mark on a figure that is not there");
  await checkResult(page, idleBody, "dryland, nothing planted");
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
