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
// Environment: WEB_URL (default http://localhost:5173), E2E_TIMEOUT_MS (default 20000),
// IRRIGATED_FIELD (default KS-014376, irrigated and growing continuous corn), DRY_FIELD (default KS-013847).
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { DEFAULT_CHOICES, formatDollars, plansQuery } from "../src/lib/planner.ts";
import { DEFAULT_SPAN, transitionQuery } from "../src/lib/transition.ts";

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
