// End-to-end check of the planner controls and plan cards (Phase 3), against the running app.
//
//   npm run e2e:planner          (with `make dev` running)
//
// Opens a dryland and an irrigated field by link, moves the sliders with the keyboard, and
// compares what the cards show with what the API answers for the same settings (asked directly,
// never hard-coded). Screenshots go to e2e/screenshots/phase3-*.png.
//
// Environment: WEB_URL (default http://localhost:5173), E2E_TIMEOUT_MS (default 20000),
// DRY_FIELD (default KS-013847), IRRIGATED_FIELD (default KS-014376).
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import {
  DEFAULT_CHOICES,
  DRYLAND_WATER_TITLE,
  climateLine,
  climateRange,
  formatInches,
  holdsText,
  placeText,
  plansQuery,
  wellQualifier,
  yieldChangeLine,
} from "../src/lib/planner.ts";

const WEB_URL = (process.env.WEB_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const TIMEOUT = Number.parseInt(process.env.E2E_TIMEOUT_MS ?? "20000", 10) || 20000;
const DRY = process.env.DRY_FIELD ?? "KS-013847";
const WET = process.env.IRRIGATED_FIELD ?? "KS-014376";
const SHOTS = path.join(path.dirname(fileURLToPath(import.meta.url)), "screenshots");
const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

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

/** Console errors and uncaught errors fail the run; every /plans request the app makes is kept. */
function watch(page, label) {
  const problems = [];
  const planRequests = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location()?.url ?? "";
    if (/tile\.openstreetmap|openstreetmap\.org/.test(where)) return; // map tiles are an outside dependency
    problems.push(`${label}: console error: ${message.text()}${where ? ` (${where})` : ""}`);
  });
  page.on("pageerror", (error) => problems.push(`${label}: uncaught page error: ${error.message}`));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/\/field\/[^/]+\/plans$/.test(url.pathname)) planRequests.push(url.pathname.split("/").at(-2) + "?" + url.search.slice(1));
  });
  return { problems, planRequests };
}

async function openField(page, fieldId) {
  await page.goto(`${WEB_URL}/#${fieldId}`, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await page.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${fieldId}"][data-state="ready"]`, { timeout: TIMEOUT });
}

/** Wait until the cards on screen are the answer for exactly this query. */
async function shown(page, query, what) {
  try {
    await page.waitForSelector(`[data-testid="plans-region"][data-updating="false"][data-query="${query}"]`, { timeout: TIMEOUT });
  } catch {
    const region = page.locator('[data-testid="plans-region"]');
    const got = (await region.count()) ? await region.getAttribute("data-query") : "(no plans on screen)";
    throw new Failure(`${what}: the cards never showed the answer for ${query} (showing ${got})`);
  }
}

const text = async (page, testId) => squash(await page.locator(`[data-testid="${testId}"]`).first().textContent());

/** Move a range input to `value` with the keyboard (Home, then arrow presses). */
async function setRange(page, testId, value) {
  const input = page.locator(`[data-testid="${testId}"]`);
  await input.focus();
  await page.keyboard.press("Home");
  for (let i = Number(await input.getAttribute("min")); i < value; i++) await page.keyboard.press("ArrowRight");
  equal(await input.inputValue(), String(value), `${testId} after keyboard input`);
}

/** What the choices read as on screen, as the choices object the query builder takes. */
async function readChoices(page) {
  const weights = {};
  for (const key of ["soil", "water", "money", "risk"]) weights[key] = Number(await page.locator(`[data-testid="weight-${key}"]`).inputValue());
  const position = Number(await page.locator('[data-testid="horizon"]').inputValue());
  const scenario = (await page.locator('[data-testid="scenario-ssp585"]').isChecked()) ? "ssp585" : "ssp245";
  return { weights, horizon: 2025 + 5 * position, scenario };
}

/** The three cards on screen against one API answer. */
async function checkCards(page, body, what) {
  equal(body.engine, "smart", `${what}: engine of the API answer`);
  const cards = page.locator('[data-testid="plan-card"]');
  equal(await cards.count(), 3, `${what}: plan cards`);
  for (let i = 0; i < 3; i++) {
    const card = cards.nth(i);
    const plan = body.plans[i];
    equal(squash(await card.locator('[data-testid="plan-name"]').textContent()), plan.name, `${what}: plan ${i + 1} name`);
    check(await card.locator('[data-testid="plan-name"]').isVisible(), `${what}: plan ${i + 1} name is not visible`);
    // the big number is what the plans are sorted on, and says so
    equal(squash(await card.locator('[data-testid="plan-overall"]').textContent()), score(plan.scores.robust), `${what}: plan ${i + 1} ranking score`);
    const bigLabel = squash(await card.locator('[data-testid="plan-overall-label"]').textContent());
    check(bigLabel.startsWith("Ranking score") && bigLabel.includes("worse futures"), `${what}: plan ${i + 1} big number is labelled "${bigLabel}"`);
    // cards 2 and 3 say where they sit in the full list
    const place = card.locator('[data-testid="plan-place"]');
    if (i === 0) {
      equal(plan.rank, 1, `${what}: rank of the first plan in the API answer`);
      equal(await place.count(), 0, `${what}: the first card needs no place line`);
    } else {
      equal(squash(await place.textContent()), placeText(plan.rank, body.candidates, i + 1), `${what}: plan ${i + 1} place in the full list`);
      check(squash(await place.textContent()).includes(`Number ${plan.rank} of ${body.candidates}`), `${what}: plan ${i + 1} place line lacks its rank`);
    }
    equal(squash(await card.locator('[data-testid="plan-reason"]').textContent()), plan.reason.trim(), `${what}: plan ${i + 1} reason`);
    const holds = squash(await card.locator('[data-testid="plan-holds"]').textContent());
    check(holds.startsWith("Holds up in"), `${what}: plan ${i + 1} has no "Holds up in" badge ("${holds}")`);
    equal(holds, holdsText(plan.futures.holds, plan.futures.total), `${what}: plan ${i + 1} holds-up badge`);
    equal(await card.locator('[role="meter"]').count(), 4, `${what}: plan ${i + 1} score bars`);
    const spread = card.locator('[data-testid="plan-spread"]');
    equal(await spread.count(), 1, `${what}: plan ${i + 1} profit spread strip`);
    const p = plan.spread.profit_per_acre;
    const dollars = (n) => (Math.round(n) < 0 ? `−$${Math.abs(Math.round(n)).toLocaleString("en-US")}` : `$${Math.abs(Math.round(n)).toLocaleString("en-US")}`); // abs: just under zero rounds to -0, shown as $0
    equal(squash(await spread.locator(".spread-values").textContent()), `Low ${dollars(p.low)}Middle ${dollars(p.mid)}High ${dollars(p.high)}`, `${what}: plan ${i + 1} low / middle / high`);
    const numbers = squash(await card.locator('[data-testid="plan-numbers"]').textContent());
    check(numbers.includes("bare days a year") && numbers.includes("return per acre") && numbers.includes("crops fail"), `${what}: plan ${i + 1} numbers line reads "${numbers}"`);
    check(numbers.includes(body.settings.irrigated ? "water pumped" : "water need met by rain"), `${what}: plan ${i + 1} shows the wrong water figure ("${numbers}")`);
    const note = card.locator('[data-testid="plan-holds-note"]');
    const waterBar = card.locator(".score").nth(1);
    if (body.settings.irrigated) {
      // water in inches (mm in the title), and the badge says it assumes the well delivers
      const water = card.locator('[data-testid="plan-water"]');
      const inches = formatInches(plan.numbers.irrigation_in, plan.numbers.irrigation_mm);
      check(/^\d+(\.\d)? in$/.test(inches), `${what}: plan ${i + 1} inches read "${inches}"`);
      equal(squash(await water.locator("dd").textContent()), inches, `${what}: plan ${i + 1} water pumped, in inches`);
      equal(await water.getAttribute("title"), `${Math.round(plan.numbers.irrigation_mm)} mm a year`, `${what}: plan ${i + 1} water title in mm`);
      check(!/\bmm\b/.test(numbers), `${what}: plan ${i + 1} still shows mm ("${numbers}")`);
      equal(squash(await note.textContent()), wellQualifier(body.settings.irrigation_limit_in), `${what}: plan ${i + 1} holds-up qualifier`);
      if (body.settings.irrigation_limit_in) check(squash(await note.textContent()).includes(`${Math.round(body.settings.irrigation_limit_in * 10) / 10} inches`), `${what}: plan ${i + 1} qualifier lacks the limit`);
      equal(await waterBar.getAttribute("title"), null, `${what}: plan ${i + 1} irrigated water bar title`);
    } else {
      equal(await note.count(), 0, `${what}: plan ${i + 1} dryland badge has no well qualifier`);
      equal(await waterBar.getAttribute("title"), DRYLAND_WATER_TITLE, `${what}: plan ${i + 1} dryland water bar title`);
    }
  }
  // the climate line is the API's label as is; rain is not mentioned a second time
  const line = await text(page, "climate-line");
  equal(line, climateLine(body.climate, body.settings.horizon), `${what}: climate line`);
  check(line.startsWith(body.climate.label), `${what}: climate line "${line}" does not start with the API label`);
  equal((line.match(/rain/g) ?? []).length, (body.climate.label.match(/rain/g) ?? []).length, `${what}: mentions of rain in the climate line`);
  const range = climateRange(body.climate, body.settings.horizon);
  if (range) equal(await text(page, "climate-range"), range, `${what}: range across the models`);
  else equal(await page.locator('[data-testid="climate-range"]').count(), 0, `${what}: no model range at Today`);
  // yield changes for the field's practice, hidden at Today
  const yields = yieldChangeLine(body.climate.yield_change, body.settings.irrigated, body.settings.horizon);
  if (body.settings.horizon <= 2025) equal(yields, null, `${what}: yield line at Today`);
  if (yields) {
    equal(await text(page, "yield-line"), yields, `${what}: yield-change line`);
    check(yields.startsWith(body.settings.irrigated ? "Irrigated yields" : "Dryland yields"), `${what}: yield line "${yields}" names the wrong practice`);
    for (const crop of Object.keys(body.climate.yield_change)) check(yields.includes(crop), `${what}: yield line lacks ${crop}`);
  } else equal(await page.locator('[data-testid="yield-line"]').count(), 0, `${what}: yield-change line should be hidden`);
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
  const ask = async (fieldId, query) => {
    const response = await fetch(`${apiPrefix}/field/${fieldId}/plans?${query}`);
    if (!response.ok) throw new Failure(`${fieldId}?${query} answered ${response.status}`);
    return response.json();
  };
  const shot = async (target, name) => {
    await target.screenshot({ path: path.join(SHOTS, name) });
    return `e2e/screenshots/${name}`;
  };
  const toControls = (target) =>
    target.evaluate(() => {
      document.querySelector("#section-plans").scrollIntoView({ block: "start" });
      return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    });

  const defaults = plansQuery(DEFAULT_CHOICES);
  const first = await ask(DRY, defaults);
  if (first.engine !== "smart") {
    // No climate tables on this server: the controls must be hidden and the cards plain.
    equal(await page.locator('[data-testid="planner-controls"]').count(), 0, "basic planner: controls hidden");
    equal(await page.locator('[data-testid="plan-card"]').count(), 3, "basic planner: plan cards");
    check((await text(page, "plans-basic-note")).includes("not set up on this server"), "basic planner: the quiet note is missing");
    log("planner test: this server answers with the basic planner; checked that the controls are hidden. Nothing else to test.");
    return;
  }
  log(`planner test: ${WEB_URL} (API ${apiPrefix}), dryland ${DRY}, irrigated ${WET}`);

  // ---- 1. dryland field, today -----------------------------------------------------------------
  await shown(page, defaults, "dryland, defaults");
  equal(await page.locator('[data-testid="planner-controls"]').count(), 1, "planner controls");
  equal(JSON.stringify(await readChoices(page)), JSON.stringify(DEFAULT_CHOICES), "default slider positions");
  equal(await page.locator('[data-testid="planner-controls"] input[type="range"]').count(), 5, "range inputs");
  for (const label of ["Soil protection", "Water", "Money", "Risk"]) {
    check(await page.getByLabel(label, { exact: true }).first().isVisible(), `slider "${label}" has no visible label`);
  }
  equal(first.settings.irrigated, false, `${DRY} should be treated as dryland by the API`);
  await checkCards(page, first, "dryland, today");
  const todayLine = await text(page, "climate-line");
  check(todayLine.startsWith(first.climate.label) && todayLine.includes("6 climate models"), `today's climate line reads "${todayLine}"`);
  equal(await page.locator('[data-testid="yield-line"]').count(), 0, "no yield-change line at Today");
  const dryLine = await text(page, "irrigation-line");
  check(dryLine.startsWith("Treated as dryland") && dryLine.includes(first.settings.irrigation_basis), `irrigation line reads "${dryLine}"`);
  equal(await page.locator('[data-testid="irrigation-switch"]').isChecked(), false, "irrigation switch on a dryland field");
  equal(await page.locator('[data-testid="plans-region"]').getAttribute("aria-live"), "polite", "the card list is a polite live region");
  if (first.current) equal(await text(page, "current-name"), first.current.name, "current pattern name");
  else check((await text(page, "current-note")).includes(first.current_note), "current_note is not shown");
  // "How this is scored" is collapsed, and holds the rules when opened
  const scoring = page.locator('[data-testid="scoring-note"]');
  equal(await scoring.evaluate((node) => node.open), false, "scoring note starts collapsed");
  await scoring.locator("summary").click();
  const scoringText = squash(await scoring.textContent());
  for (const key of ["holds_up_rule", "ranking_rule", "profit_basis", "soil_note"]) {
    check(scoringText.includes(first.settings[key]), `scoring note is missing settings.${key}`);
  }
  check(first.settings.models.every((model) => scoringText.includes(model)), "scoring note is missing a model name");
  const limits = (await scoring.locator('[data-testid="scoring-limits"] li').allTextContents()).map(squash);
  check(first.settings.limits.length >= 1, "the API answer has no settings.limits");
  equal(limits.join(" | "), first.settings.limits.join(" | "), "limits listed under How this is scored");
  check(!/improves? the soil|soil health/i.test(scoringText), "scoring note talks about soil health");
  await scoring.locator("summary").click();
  await toControls(page);
  log(`  ✓ dryland field today: controls, 3 cards with "Holds up in", spread strips, scoring note → ${await shot(page, "phase3-today.png")}`);

  // ---- 2. 2060, high emissions -----------------------------------------------------------------
  await page.locator('[data-testid="horizon"]').focus();
  await page.keyboard.press("End");
  await page.locator('[data-testid="scenario-ssp585"]').check();
  const future = { ...DEFAULT_CHOICES, horizon: 2060, scenario: "ssp585" };
  equal(JSON.stringify(await readChoices(page)), JSON.stringify(future), "choices after moving to 2060 / High emissions");
  equal(await text(page, "horizon-value"), "2060", "year shown next to the slider");
  const futureQuery = plansQuery(future);
  await shown(page, futureQuery, "2060, high emissions");
  const futureBody = await ask(DRY, futureQuery);
  equal(futureBody.settings.horizon, 2060, "API horizon");
  await checkCards(page, futureBody, "dryland, 2060 high emissions");
  const futureLine = await text(page, "climate-line");
  check(futureLine !== todayLine, "the climate line did not change for 2060");
  check(futureLine.startsWith(futureBody.climate.label), `climate line "${futureLine}" does not start with the API label`);
  check(futureLine.includes(`${futureBody.climate.window[0]}–${futureBody.climate.window[1]}`) && futureLine.includes("6 climate models"), `climate line "${futureLine}" lacks the years or the models`);
  check(!/do not agree on summer rain/.test(futureLine), `climate line "${futureLine}" adds its own rain sentence to the API label`);
  const [rainLow, rainHigh] = futureBody.climate.range.summer_rain_pct;
  if (rainLow < -0.5 && rainHigh > 0.5) {
    // the spread across the models is still on screen, as small print
    check(/summer rain from \d+% less to \d+% more/.test(await text(page, "climate-range")), "the model range for summer rain is not shown");
  }
  const yieldLine = await text(page, "yield-line");
  check(/^Dryland yields in this future: .+%/.test(yieldLine), `yield-change line reads "${yieldLine}"`);
  await toControls(page);
  log(`  ✓ 2060 + High emissions: "${futureLine}" → ${await shot(page, "phase3-2060.png")}`);

  // ---- 3. Water 10, the others 1 ---------------------------------------------------------------
  for (const key of ["soil", "money", "risk"]) await setRange(page, `weight-${key}`, 1);
  await page.locator('[data-testid="weight-water"]').focus();
  await page.keyboard.press("End");
  const thirsty = { ...future, weights: { soil: 1, water: 10, money: 1, risk: 1 } };
  equal(JSON.stringify(await readChoices(page)), JSON.stringify(thirsty), "choices with Water at 10");
  await shown(page, plansQuery(thirsty), "water 10");
  const thirstyBody = await ask(DRY, plansQuery(thirsty));
  await checkCards(page, thirstyBody, "dryland, water 10");
  log(`  ✓ Water 10, others 1: top plan "${thirstyBody.plans[0].name}" as the API ranks it`);

  // ---- 4. a fast run of changes ends on the answer for the final values -------------------------
  const before = monitor.planRequests.length;
  await page.locator('[data-testid="horizon"]').focus();
  for (let i = 0; i < 7; i++) await page.keyboard.press("ArrowLeft");
  for (let i = 0; i < 3; i++) await page.keyboard.press("ArrowRight");
  await page.locator('[data-testid="weight-money"]').focus();
  for (let i = 0; i < 6; i++) await page.keyboard.press("ArrowRight");
  await page.locator('[data-testid="weight-water"]').focus();
  for (let i = 0; i < 5; i++) await page.keyboard.press("ArrowLeft");
  const settled = { weights: { soil: 1, water: 5, money: 7, risk: 1 }, horizon: 2040, scenario: "ssp585" };
  equal(JSON.stringify(await readChoices(page)), JSON.stringify(settled), "choices after the fast run");
  await shown(page, plansQuery(settled), "fast run");
  await page.waitForTimeout(400); // nothing late may replace it
  await shown(page, plansQuery(settled), "fast run, after waiting");
  await checkCards(page, await ask(DRY, plansQuery(settled)), "dryland, after the fast run");
  const sent = monitor.planRequests.length - before;
  check(sent >= 1 && sent < 21, `21 key presses sent ${sent} requests: the debounce is not working`);
  equal(monitor.planRequests.at(-1), `${DRY}?${plansQuery(settled)}`, "last request sent");
  log(`  ✓ 21 quick slider steps → ${sent} ${sent === 1 ? "request" : "requests"}, cards show the answer for the final values`);

  // ---- 5. irrigation override ------------------------------------------------------------------
  const namesBefore = (await page.locator('[data-testid="plan-name"]').allTextContents()).join(" | ");
  await page.locator('[data-testid="irrigation-switch"]').focus();
  await page.keyboard.press("Space");
  const overridden = plansQuery(settled, true);
  check(overridden.endsWith("&irrigated=true"), "override query");
  await shown(page, overridden, "irrigation override");
  equal(monitor.planRequests.at(-1), `${DRY}?${overridden}`, "request sent for the override");
  const overriddenBody = await ask(DRY, overridden);
  equal(overriddenBody.settings.irrigated, true, "API treats the field as irrigated when told to");
  await checkCards(page, overriddenBody, "dryland field, overridden to irrigated");
  const namesAfter = (await page.locator('[data-testid="plan-name"]').allTextContents()).join(" | ");
  check(namesAfter !== namesBefore, `the override did not change the cards (${namesAfter})`);
  const overrideLine = await text(page, "irrigation-line");
  check(overrideLine.startsWith("Treated as irrigated") && overrideLine.includes("you set this"), `irrigation line after the override reads "${overrideLine}"`);
  equal(await page.locator('[data-testid="irrigation-reset"]').count(), 1, '"Use our guess" after an override');
  log(`  ✓ irrigation override sends irrigated=true and changes the cards: ${namesAfter}`);

  // ---- 6. another field: override forgotten, sliders kept ----------------------------------------
  await page.evaluate((id) => (window.location.hash = id), WET);
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${WET}"][data-state="ready"]`);
  await shown(page, plansQuery(settled), "irrigated field, same settings");
  equal(JSON.stringify(await readChoices(page)), JSON.stringify(settled), "sliders, future and scenario carried over to the next field");
  equal(monitor.planRequests.at(-1), `${WET}?${plansQuery(settled)}`, "first request for the next field (no irrigated=)");
  equal(await page.locator('[data-testid="irrigation-reset"]').count(), 0, "override forgotten on the next field");
  const wetBody = await ask(WET, plansQuery(settled));
  equal(wetBody.settings.irrigated, true, `${WET} should be treated as irrigated by the API`);
  equal(wetBody.settings.irrigation_source, "guess", "irrigation source on the next field");
  await checkCards(page, wetBody, "irrigated field, carried-over settings");

  // back to the defaults with Reset, Home and Middle road
  await page.locator('[data-testid="weights-reset"]').click();
  await page.locator('[data-testid="horizon"]').focus();
  await page.keyboard.press("Home");
  await page.locator('[data-testid="scenario-ssp245"]').check();
  equal(JSON.stringify(await readChoices(page)), JSON.stringify(DEFAULT_CHOICES), "choices after Reset, Today, Middle road");
  check(await page.locator('[data-testid="weights-reset"]').isDisabled(), "Reset should be disabled at the defaults");
  await shown(page, defaults, "irrigated field, defaults");
  const wetDefault = await ask(WET, defaults);
  await checkCards(page, wetDefault, "irrigated field, today");
  const wetLine = await text(page, "irrigation-line");
  check(wetLine.startsWith("Treated as irrigated") && wetLine.includes(wetDefault.settings.irrigation_basis), `irrigation line reads "${wetLine}"`);
  equal(await page.locator('[data-testid="irrigation-switch"]').isChecked(), true, "irrigation switch on an irrigated field");
  if (wetDefault.current) {
    equal(await text(page, "current-name"), wetDefault.current.name, "irrigated field: current pattern name");
    const currentText = await text(page, "current-pattern");
    check(currentText.includes(holdsText(wetDefault.current.futures.holds, wetDefault.current.futures.total)), `current pattern row lacks its holds-up count ("${currentText}")`);
    check(currentText.includes(`Ranking score${score(wetDefault.current.scores.robust)}`), `current pattern row lacks its ranking score ("${currentText}")`);
    check(currentText.includes(wellQualifier(wetDefault.settings.irrigation_limit_in)), `current pattern row lacks the well qualifier ("${currentText}")`);
  }
  await toControls(page);
  const irrigatedShot = await shot(page, "phase3-irrigated.png");
  await page.locator('[data-testid="plan-card"]').nth(1).scrollIntoViewIfNeeded();
  await page.evaluate(() => document.querySelector('[data-testid="plan-card"]').scrollIntoView({ block: "start" }));
  const cardsShot = await shot(page, "phase3-irrigated-cards.png");
  log(`  ✓ irrigated field: override forgotten, sliders kept, Reset works → ${irrigatedShot}, ${cardsShot}`);

  // override the other way: dryland rules now apply to what the field grows
  await page.locator('[data-testid="irrigation-switch"]').click();
  await shown(page, plansQuery(DEFAULT_CHOICES, false), "irrigated field overridden to dryland");
  equal(monitor.planRequests.at(-1), `${WET}?${plansQuery(DEFAULT_CHOICES, false)}`, "request sent for the dryland override");
  const wetDry = await ask(WET, plansQuery(DEFAULT_CHOICES, false));
  await checkCards(page, wetDry, "irrigated field, overridden to dryland");
  check((await page.locator('[data-testid="plan-name"]').allTextContents()).join("|") !== wetDefault.plans.map((p) => p.name).join("|"), "the dryland override did not change the cards");
  const broken = wetDry.current?.rules_broken ?? [];
  if (broken.length > 0) {
    const shownBroken = (await page.locator('[data-testid="current-broken"] li').allTextContents()).map(squash);
    equal(shownBroken.join(" | "), broken.join(" | "), "rules the current pattern breaks");
  }
  await page.locator('[data-testid="irrigation-reset"]').click();
  await shown(page, defaults, "back to our guess");
  log(`  ✓ dryland override on the irrigated field sends irrigated=false${broken.length ? ` and lists ${broken.length} broken ${broken.length === 1 ? "rule" : "rules"}` : ""}; "Use our guess" undoes it`);

  // ---- 7. a failed update keeps the old cards and says so --------------------------------------
  const failing = (route) => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Made-up failure for the test." }) });
  await page.route((url) => /\/plans$/.test(url.pathname) && url.search.includes("horizon=2030"), failing);
  await page.locator('[data-testid="horizon"]').focus();
  await page.keyboard.press("ArrowRight");
  await page.waitForSelector('[data-testid="plans-update-error"]');
  check((await text(page, "plans-update-error")).includes("Made-up failure for the test."), "the error line does not carry the API's sentence");
  equal(await page.locator('[data-testid="plan-card"]').count(), 3, "cards kept while the update failed");
  equal(await page.locator('[data-testid="plans-region"]').getAttribute("data-query"), defaults, "cards still the previous answer");
  await page.unroute((url) => /\/plans$/.test(url.pathname) && url.search.includes("horizon=2030"), failing);
  await page.keyboard.press("ArrowLeft");
  await shown(page, defaults, "after the failed update");
  equal(await page.locator('[data-testid="plans-update-error"]').count(), 0, "error line gone after a good answer");
  const realProblems = monitor.problems.filter((p) => !p.includes("500 (Internal Server Error)")); // the failure this test made up
  check(realProblems.length === 0, `console errors on desktop:\n  ${realProblems.join("\n  ")}`);
  log("  ✓ a failed update keeps the previous cards with a plain error line; no console errors");
  await context.close();

  // ---- 8. phone width --------------------------------------------------------------------------
  const phoneContext = await browser.newContext({ viewport: PHONE, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const phone = await phoneContext.newPage();
  phone.setDefaultTimeout(TIMEOUT);
  const phoneMonitor = watch(phone, "phone");
  await openField(phone, DRY);
  await shown(phone, defaults, "phone, defaults");
  await phone.locator('[data-testid="horizon"]').focus();
  await phone.keyboard.press("End");
  const phoneQuery = plansQuery({ ...DEFAULT_CHOICES, horizon: 2060 });
  await shown(phone, phoneQuery, "phone, 2060");
  await checkCards(phone, await ask(DRY, phoneQuery), "phone, 2060");
  const fit = await phone.evaluate(() => {
    const body = document.querySelector(".panel-body");
    const clipped = [...document.querySelectorAll(".planner, .planner-row, .planner-ticks, .segmented, .plan, .plan-numbers, .spread, .current, .holds, .plan-head, .plan-place, .planner-yields, .planner-climate-range")]
      .filter((node) => node.scrollWidth > node.clientWidth + 1)
      .map((node) => node.className);
    const ticks = [...document.querySelectorAll(".planner-ticks span")].map((node) => node.getBoundingClientRect());
    const overlapping = ticks.filter((rect, i) => i > 0 && rect.left < ticks[i - 1].right - 0.5).length;
    return { page: document.documentElement.scrollWidth, body: body.scrollWidth - body.clientWidth, clipped, overlapping };
  });
  check(fit.page <= PHONE.width, `phone: the page scrolls sideways (${fit.page}px wide)`);
  check(fit.body <= 0, `phone: the panel scrolls sideways by ${fit.body}px`);
  equal(fit.clipped.join(","), "", "phone: clipped planner parts");
  equal(fit.overlapping, 0, "phone: year labels under the future slider that overlap");
  await toControls(phone);
  const phoneShot = await shot(phone, "phase3-phone.png");
  await phone.evaluate(() => document.querySelector('[data-testid="plan-card"]').scrollIntoView({ block: "start" }));
  const phoneCards = await shot(phone, "phase3-phone-cards.png");
  check(phoneMonitor.problems.length === 0, `console errors on the phone:\n  ${phoneMonitor.problems.join("\n  ")}`);
  await phoneContext.close();
  log(`  ✓ phone width: controls and cards fit, no sideways scroll → ${phoneShot}, ${phoneCards}`);
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
