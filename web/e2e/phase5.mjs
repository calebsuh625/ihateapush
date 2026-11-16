// End-to-end checks of the Phase 5 features, against the running app.
//
//   npm run e2e:phase5          (with `make dev` running)
//
// Item A, rotation effects on yield: the page's plan cards, "What this field grows now" row, the "How this
// is scored" fold and "How to get there" are compared with direct calls to GET /field/{id}/plans,
// GET /rotation-effects and GET /field/{id}/transition for the same settings (never hard-coded), and the
// numbers the effects must change (continuous corn no longer number 2, the path to our third plan on the
// irrigated example now pays back) are checked on the API answers themselves. One screenshot goes to
// e2e/screenshots/phase5-rotation-effects.png.
//
// Item B, weather replay: "How it would have done in real weather" on plan cards and under the build-your-own result
// is compared with direct calls to GET /field/{id}/replay (the bars, the dry-year marks, the worst-year marks, the
// worst / middle / under-direct-costs figures, the comparison with carrying on, the notes); the dry years are
// re-derived from the weather rows by the rule (driest fifth), never by name. One screenshot goes to
// e2e/screenshots/phase5-weather-replay.png.
//
// Environment: WEB_URL (default http://localhost:5173), E2E_TIMEOUT_MS (default 20000),
// IRRIGATED_FIELD (default KS-014376, irrigated and growing continuous corn), DRY_FIELD (default KS-013847).
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { DEFAULT_CHOICES, formatDollars, plansQuery } from "../src/lib/planner.ts";
import { DEFAULT_SPAN, transitionQuery } from "../src/lib/transition.ts";
import { replayQuery } from "../src/lib/replay.ts";

const WEB_URL = (process.env.WEB_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const TIMEOUT = Number.parseInt(process.env.E2E_TIMEOUT_MS ?? "20000", 10) || 20000;
const DRY = process.env.DRY_FIELD ?? "KS-013847";
const WET = process.env.IRRIGATED_FIELD ?? "KS-014376";
const SHOTS = path.join(path.dirname(fileURLToPath(import.meta.url)), "screenshots");
const DESKTOP = { width: 1440, height: 900 };

class Failure extends Error {}
let checks = 0;
const check = (ok, message) => {
  checks += 1;
  if (!ok) throw new Failure(message);
};
const equal = (actual, expected, what) =>
  check(actual === expected, `${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
const log = (line) => console.log(line);
const squash = (s) => (s ?? "").replace(/\s+/g, " ").trim();

function watch(page, label) {
  const problems = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location()?.url ?? "";
    if (/tile\.openstreetmap|openstreetmap\.org/.test(where)) return; // map tiles are an outside dependency
    problems.push(`${label}: console error: ${message.text()}${where ? ` (${where})` : ""}`);
  });
  page.on("pageerror", (error) => problems.push(`${label}: uncaught page error: ${error.message}`));
  return problems;
}

async function openField(page, fieldId) {
  await page.goto(`${WEB_URL}/#${fieldId}`, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  await page.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });
  await page.waitForSelector(`[data-testid="panel"][data-field-id="${fieldId}"][data-state="ready"]`, { timeout: TIMEOUT });
  await page.waitForSelector('[data-testid="plans-region"][data-updating="false"]', { timeout: TIMEOUT });
}

const texts = async (page, selector) => (await page.locator(selector).allTextContents()).map(squash);

/** The plan cards, the field's own row and the scoring fold, against one API answer. */
async function checkPlans(page, body, effectsBody, what) {
  equal(body.engine, "smart", `${what}: engine`);
  const block = body.settings.rotation_effects;
  equal(block.setting, effectsBody.setting, `${what}: the answer echoes the setting`);
  equal(block.applied, effectsBody.applied, `${what}: the answer says whether the effects applied`);
  equal(block.effects.map((e) => e.id).join(","), effectsBody.effects.map((e) => e.id).join(","), `${what}: the effects in the answer are the endpoint's`);

  const cards = page.locator('[data-testid="plan-card"]');
  equal(await cards.count(), body.plans.length, `${what}: number of plan cards`);
  for (const [i, plan] of body.plans.entries()) {
    const card = cards.nth(i);
    equal(squash(await card.locator('[data-testid="plan-name"]').textContent()), plan.name, `${what}: card ${i + 1} name`);
    const spread = squash(await card.locator('[data-testid="plan-spread"] .spread-values').textContent());
    const s = plan.spread.profit_per_acre;
    // the three figures are separate spans, so compare without the spaces between them
    equal(spread.replace(/\s+/g, ""), `Low${formatDollars(s.low)}Middle${formatDollars(s.mid)}High${formatDollars(s.high)}`.replace(/\s+/g, ""), `${what}: card ${i + 1} low-middle-high`);
    const line = card.locator('[data-testid="plan-rotation-effects"]');
    if (plan.rotation_effects?.length) {
      equal(squash(await line.textContent()), plan.rotation_effects.map((e) => e.sentence).join(" "), `${what}: card ${i + 1} says which effects it runs into`);
    } else {
      equal(await line.count(), 0, `${what}: card ${i + 1} has no effects line when it runs into none`);
    }
  }

  // the field's own pattern: its rank is the API's
  const current = body.current;
  if (current) {
    equal(await texts(page, '[data-testid="current-name"]').then((t) => t[0]), current.name, `${what}: own pattern name`);
    const own = page.locator('[data-testid="current-rotation-effects"]');
    if (current.rotation_effects?.length) equal(squash(await own.textContent()), current.rotation_effects.map((e) => e.sentence).join(" "), `${what}: own pattern says which effects it runs into`);
    else equal(await own.count(), 0, `${what}: own pattern has no effects line when it runs into none`);
    equal(squash(await page.locator(".current-rank").textContent()), `Ranks ${current.rank} of ${body.candidates}`, `${what}: own pattern rank`);
  }

  // the fold: the limits sentences (the old "no rotation effect" one replaced) and one line per effect
  const scoring = page.locator('[data-testid="scoring-note"]');
  await scoring.locator("summary").click();
  equal((await texts(scoring, '[data-testid="scoring-limits"] li')).join(" | "), body.settings.limits.map(squash).join(" | "), `${what}: what the scores leave out`);
  const listed = await texts(scoring, '[data-testid="scoring-effects"] li');
  if (block.applied) {
    equal(listed.join(" | "), block.effects.map((e) => `${e.sentence} (${e.status})`).join(" | "), `${what}: the effects listed in the fold`);
  } else {
    equal(listed.length, 0, `${what}: nothing listed when the effects are off`);
  }
  const limitText = body.settings.limits.join(" ");
  check(block.applied ? /after itself/.test(limitText) && !/do not change with the crop grown before/.test(limitText)
    : /do not change with the crop grown before/.test(limitText), `${what}: the limits sentence follows the switch`);
}


// ---- Item B: weather replay ("How it would have done in real weather") ---------------------------------------
/** Open the replay block of the card with `selector` and check what it shows against GET /field/{id}/replay. */
async function checkReplayBlock(page, scope, body, query, what) {
  await scope.locator('[data-testid="replay-toggle"]').click();
  const region = scope.locator(`[data-testid="replay-region"][data-state="ready"][data-updating="false"][data-query="${query}"]`);
  await region.waitFor({ timeout: TIMEOUT });
  const p = body.plan;
  const s = p.summary;
  equal(squash(await region.locator('[data-testid="replay-lead"]').textContent()), squash(p.sentences[0]), `${what}: lead sentence`);
  const worst = region.locator('[data-testid="replay-worst"]');
  equal(Number(await worst.getAttribute("data-year")), s.worst_year.year, `${what}: worst year`);
  equal(Number(await worst.getAttribute("data-value")), s.worst_year.return, `${what}: worst-year return`);
  equal(squash(await worst.locator("dd").textContent()), formatDollars(s.worst_year.return), `${what}: worst-year figure shown`);
  equal(Number(await region.locator('[data-testid="replay-median"]').getAttribute("data-value")), s.median_return, `${what}: median return`);
  equal(squash(await region.locator('[data-testid="replay-median"] dd').textContent()), formatDollars(s.median_return), `${what}: median figure shown`);
  const under = region.locator('[data-testid="replay-under"]');
  equal(Number(await under.getAttribute("data-count")), s.below_cost_years, `${what}: years under direct costs`);
  const total = p.basis === "this_start" ? s.years : s.crop_years;
  equal(squash(await under.locator("dd").textContent()), `${s.below_cost_years} of ${total}`, `${what}: under-the-line count shown`);

  // the chart: one bar per observed year, each with the API's return; the marks are the API's
  const bars = region.locator('[data-testid="replay-bar"]');
  equal(await bars.count(), body.weather.years, `${what}: one bar for every observed year`);
  for (const [i, row] of p.years.entries()) {
    const bar = bars.nth(i);
    equal(Number(await bar.getAttribute("data-year")), row.year, `${what}: bar ${i} year`);
    equal(Number(await bar.getAttribute("data-return")), row.return, `${what}: ${row.year} bar return`);
    equal(await bar.getAttribute("data-dry"), row.dry ? "true" : "false", `${what}: ${row.year} dry mark`);
    equal(await bar.getAttribute("data-under"), row.below_cost ? "true" : "false", `${what}: ${row.year} under direct costs`);
    equal((await bar.getAttribute("data-rank")) ?? null, row.worst_rank === null ? null : String(row.worst_rank), `${what}: ${row.year} worst-year mark`);
  }
  const dryShown = (await region.locator('[data-testid="replay-dry-mark"]').evaluateAll((els) => els.map((e) => Number(e.getAttribute("data-year"))))).join(",");
  equal(dryShown, body.weather.dry_years.join(","), `${what}: the dry years marked are the API's (from our own rain rule)`);
  const ranks = await region.locator('[data-testid="replay-rank"]').count();
  equal(ranks, p.worst_years.length, `${what}: the worst years are marked`);
  const svg = region.locator("svg.replay-svg");
  const box = await svg.boundingBox();
  check(box !== null && box.width > 100 && box.height > 60, `${what}: the chart has a size`);

  // the comparison with carrying on as now
  const versus = region.locator('[data-testid="replay-versus"]');
  if (body.comparison) {
    equal(await versus.count(), 1, `${what}: the comparison is shown`);
    equal((await texts(versus, ".replay-sentences li")).join(" | "), body.comparison.sentences.map(squash).join(" | "), `${what}: comparison sentences`);
    equal(Number(await versus.locator('[data-testid="replay-ahead"]').getAttribute("data-count")), body.comparison.years_ahead, `${what}: years ahead of carrying on`);
    equal(Number(await versus.locator('[data-testid="replay-mean-difference"]').getAttribute("data-value")), body.comparison.mean_difference, `${what}: average difference`);
    equal(await region.locator('[data-testid="replay-carry"], line.replay-carry').count(), body.weather.years, `${what}: a carry-on tick on every bar`);
  } else {
    equal(await versus.count(), 0, `${what}: no comparison when there is no carry-on pattern`);
  }

  // the table and the notes
  await region.locator('[data-testid="replay-table-fold"] summary').click();
  const rows = region.locator('[data-testid="replay-table"] tbody tr');
  equal(await rows.count(), body.weather.years, `${what}: a table row for every year`);
  equal(squash(await rows.first().locator("td").nth(1).textContent()), formatDollars(p.years[0].return), `${what}: first table row return`);
  equal((await texts(region, '[data-testid="replay-notes"] li')).join(" | "), body.notes.map(squash).join(" | "), `${what}: the notes are the API's`);
  const text = squash(await region.textContent());
  check(/not a forecast/i.test(text), `${what}: says history is not a forecast`);
  check(/today's/i.test(text) && /direct costs/i.test(text), `${what}: says prices and costs are today's and the dollars are over direct costs`);
  check(!/soil health/i.test(text) && !/\bearns?\b/i.test(text) && !/profit/i.test(text.replace(/not profit/gi, "")), `${what}: wording rules`);
  return region;
}

async function checkReplay(browser, page, get, defaults, apiPrefix) {
  // the API, before the page: the numbers the page must show
  for (const [field, plan] of [[WET, 1], [WET, 3], [DRY, 1]]) {
    const body = await get(`/field/${field}/replay?${replayQuery(defaults, { plan })}`);
    equal(body.status, "ok", `${field} plan ${plan}: replay status`);
    equal(body.weather.years, body.plan.years.length, `${field} plan ${plan}: a row for every observed year`);
    const lowest = body.weather.rows.slice().sort((a, b) => a.season_rain_mm - b.season_rain_mm || a.year - b.year).slice(0, Math.floor(body.weather.years / 5));
    equal(body.weather.dry_years.join(","), lowest.map((r) => r.year).sort((a, b) => a - b).join(","), `${field} plan ${plan}: dry years are the driest fifth by season rain`);
    const rows = body.plan.years;
    equal(body.plan.summary.worst_year.return, Math.min(...rows.map((r) => r.return)), `${field} plan ${plan}: worst year is the lowest bar`);
    for (const r of rows) check(r.low <= r.return && r.return <= r.high, `${field} plan ${plan}: ${r.year} low <= return <= high`);
  }
  const missing = await fetch(`${apiPrefix}/field/${WET}/replay?plan=9`);
  equal(missing.status, 400, "a plan number out of range is a 400");

  // the irrigated field: card 1 (with a carry-on comparison) and card 3
  await openField(page, WET);
  const wet1 = await get(`/field/${WET}/replay?${replayQuery(defaults, { plan: 1 })}`);
  const q1 = replayQuery(defaults, { plan: 1 });
  const cards = page.locator('[data-testid="plan-card"]');
  await checkReplayBlock(page, cards.nth(0), wet1, q1, `${WET} card 1`);
  const wet3 = await get(`/field/${WET}/replay?${replayQuery(defaults, { plan: 3 })}`);
  await checkReplayBlock(page, cards.nth(2), wet3, replayQuery(defaults, { plan: 3 }), `${WET} card 3`);
  check(wet1.comparison !== null, `${WET} is compared with carrying on as now`);
  log(`  ✓ ${WET}: replay on cards 1 and 3 equals the API (${wet1.weather.years} bars, dry ${wet1.weather.dry_years.join(", ")}, worst ${wet1.plan.summary.worst_year.year} ${formatDollars(wet1.plan.summary.worst_year.return)})`);

  // a replay opened on card 1 stays on the page for a screenshot
  await cards.nth(0).locator('[data-testid="replay-chart"]').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(SHOTS, "phase5-weather-replay.png") });

  // the dryland field
  await openField(page, DRY);
  const dry1 = await get(`/field/${DRY}/replay?${replayQuery(defaults, { plan: 1 })}`);
  await checkReplayBlock(page, page.locator('[data-testid="plan-card"]').nth(0), dry1, replayQuery(defaults, { plan: 1 }), `${DRY} card 1`);
  log(`  ✓ ${DRY} (dryland): replay on card 1 equals the API (${dry1.comparison ? "with" : "without"} a carry-on comparison)`);

  // a farmer's own rotation, typed in "Try your own rotation"
  await openField(page, WET);
  const typed = "corn, soybeans, winter wheat, sorghum";
  await page.locator('[data-testid="own-text"]').fill(typed);
  await page.locator('[data-testid="own-text-submit"]').click();
  const own = page.locator('[data-testid="own-result"]');
  await own.locator('[data-testid="replay-toggle"]').waitFor({ timeout: TIMEOUT });
  const ownQuery = replayQuery(defaults, { rotation: ["corn", "soybeans", "winter wheat", "sorghum"] });
  const ownBody = await get(`/field/${WET}/replay?${ownQuery}`);
  equal(ownBody.subject.source, "own", "an own rotation is replayed as the farmer's own");
  await checkReplayBlock(page, own, ownBody, ownQuery, `${WET} own rotation`);
  log(`  ✓ ${WET}: replay under the build-your-own result equals the API`);
}

async function run(browser) {
  const apiBase = await (async () => {
    const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
    const page = await context.newPage();
    await page.goto(`${WEB_URL}/`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });
    const base = await page.getAttribute('[data-testid="app"]', "data-api-base");
    await context.close();
    return base;
  })();
  const apiPrefix = new URL(apiBase, `${WEB_URL}/`).href.replace(/\/+$/, "");
  const get = async (pathAndQuery) => {
    const response = await fetch(`${apiPrefix}${pathAndQuery}`);
    if (!response.ok) throw new Failure(`${pathAndQuery} answered ${response.status}`);
    return response.json();
  };
  const defaults = plansQuery(DEFAULT_CHOICES);

  // ---- the effects file, as the API serves it --------------------------------------------------------
  const effects = await get("/rotation-effects");
  equal(effects.setting, "on", "ROTATION_EFFECTS is on by default");
  check(effects.applied, "the effects are applied");
  const ids = effects.effects.map((e) => e.id);
  for (const id of ["corn_after_corn", "sorghum_after_sorghum", "soybeans_after_soybeans", "wheat_after_wheat"]) check(ids.includes(id), `effect ${id} is listed`);
  for (const e of effects.effects) {
    check(e.low <= e.multiplier && e.multiplier <= e.high && e.high <= 1, `${e.id}: low <= multiplier <= high <= 1`);
    check(e.sources.length > 0 && e.sources.every((s) => s.name && s.url), `${e.id}: every effect names its source`);
    check(["verified", "rough, unverified"].includes(e.status), `${e.id}: status ${e.status}`);
  }
  check(effects.not_applied.length > 0 && effects.not_applied.every((x) => x.reason), "what was left out comes with a reason");
  log(`  ✓ /rotation-effects: ${effects.effects.length} effects with sources and status, ${effects.not_applied.length} left out with a reason`);

  // ---- the irrigated example: continuous corn ---------------------------------------------------------
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
  const problems = watch(page, "desktop");
  try {
    await openField(page, WET);
  } catch (error) {
    throw new Failure(`could not open ${WEB_URL}/#${WET}: is the dev server running and is this field in the data? (${error.message.split("\n")[0]})`);
  }
  const plans = await get(`/field/${WET}/plans?${defaults}`);
  equal(plans.settings.irrigated, true, `${WET} is irrigated`);
  await checkPlans(page, plans, effects, WET);
  check(plans.current && /corn/i.test(plans.current.name) && plans.current.rotation.every((c) => c === "corn"), `${WET} grows continuous corn`);
  check(plans.current.rank > 2, `continuous irrigated corn ranks ${plans.current.rank} of ${plans.candidates}: no longer number 2`);
  check(plans.current.rotation_effects?.some((e) => e.id === "corn_after_corn"), "the field's own pattern carries the corn-after-corn effect");
  log(`  ✓ ${WET}: cards, own pattern (ranks ${plans.current.rank} of ${plans.candidates}) and the fold equal the API; continuous corn is no longer number 2`);

  // the switch is a server setting: the same call carries what it was
  const spreadOk = plans.plans.every((p) => p.spread.profit_per_acre.low <= p.spread.profit_per_acre.mid && p.spread.profit_per_acre.mid <= p.spread.profit_per_acre.high);
  check(spreadOk, "every card's low <= middle <= high");

  // ---- "How to get there": the path to our third plan now pays back ----------------------------------
  const query = transitionQuery(defaults, { plan: 3 }, DEFAULT_SPAN);
  const path3 = await get(`/field/${WET}/transition?${query}`);
  equal(path3.status, "path", "a path to the third plan exists");
  const card = page.locator('[data-testid="plan-card"]').nth(2);
  await card.locator('[data-testid="how-toggle"]').click();
  await card.locator(`[data-testid="how-region"][data-state="ready"][data-updating="false"][data-query="${query}"]`).waitFor({ timeout: TIMEOUT });
  equal(squash(await card.locator('[data-testid="how-short"]').textContent()), squash(path3.sentences.short_term), "how to get there: short-term sentence");
  equal(squash(await card.locator('[data-testid="how-long"]').textContent()), squash(path3.sentences.long_term), "how to get there: long-term sentence");
  check(path3.difference.total.mid > 0, `the path to plan 3 adds ${path3.difference.total.mid} dollars over eight years: it shows a gain`);
  equal(path3.difference.payback.status, "paid_back", "the path to plan 3 pays back within the span");
  const first = await get(`/field/${WET}/transition?${transitionQuery(defaults, { plan: 1 }, DEFAULT_SPAN)}`);
  check(Math.abs(first.long_run.difference.return.mid) < 60, "plan 1 against carrying on is within a few dollars an acre, no longer far behind");
  log(`  ✓ how to get there on card 3 equals the API; eight-year difference ${path3.difference.total.mid} dollars, paid back ${path3.difference.payback.year}`);

  // ---- an own rotation of continuous corn is scored with the effect -----------------------------------
  const own = await get(`/field/${WET}/score?rotation=corn,corn,corn,corn&${defaults}`);
  const ownEffects = (own.yours.rotation_effects ?? []).map((e) => e.id);
  check(ownEffects.includes("corn_after_corn"), "scoring corn,corn,corn,corn carries the corn-after-corn effect");
  check(own.yours.rank > 2, `corn,corn,corn,corn scores number ${own.yours.rank}`);
  const crossed = own.yours.spread.profit_per_acre;
  check(crossed.low <= crossed.mid && crossed.mid <= crossed.high && crossed.low < crossed.high, "the effect's range widens the low-high of the own rotation");

  // ---- a dryland field: same checks on its cards ---------------------------------------------------------
  await openField(page, DRY);
  const dry = await get(`/field/${DRY}/plans?${defaults}`);
  await checkPlans(page, dry, effects, DRY);
  log(`  ✓ ${DRY} (dryland): cards and the fold equal the API`);

  // ---- item B: weather replay ------------------------------------------------------------------------------
  await checkReplay(browser, page, get, defaults, apiPrefix);

  // ---- the picture ---------------------------------------------------------------------------------------
  await openField(page, WET);
  await page.evaluate(() => document.querySelector('[data-testid="current-pattern"]')?.scrollIntoView({ block: "center" }));
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(SHOTS, "phase5-rotation-effects.png") });
  check(problems.length === 0, `console errors:\n  ${problems.join("\n  ")}`);
  await context.close();
  log("  ✓ screenshot → e2e/screenshots/phase5-rotation-effects.png");
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
