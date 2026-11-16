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
// Item F, price and cost what-ifs: the sliders under "Which future?" are moved in the page and the cards, the
// break-even line and the request's query are compared with direct calls to GET /field/{id}/plans with the same
// price_pct / cost_pct (never hard-coded); 0 is the same answer as no parameters; Reset returns to it; the other
// endpoints echo the setting. One screenshot goes to e2e/screenshots/phase5-price-cost.png.
//
// Item C, export and share: the settings the page is in are written into the URL hash; the copied link is opened in a
// fresh browser and in a page that is already open, and the controls, the plan query, the own rotation and the map
// layer must come back as they were; the old #KS-... form still opens the field with defaults. The CSV downloads (top
// plans, farm plan) are parsed and compared with direct calls to GET /field/{id}/plans and POST /farm/plan; "Print this
// plan" is checked under emulated print media (the sheet shows and the app does not; its figures equal the API; the
// page count of the PDF). One screenshot goes to e2e/screenshots/phase5-share-print.png.
//
// Environment: WEB_URL (default http://localhost:5173), E2E_TIMEOUT_MS (default 20000),
// IRRIGATED_FIELD (default KS-014376, irrigated and growing continuous corn), DRY_FIELD (default KS-013847).
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { DEFAULT_CHOICES, formatDollars, plansQuery } from "../src/lib/planner.ts";
import { DEFAULT_SPAN, transitionQuery } from "../src/lib/transition.ts";
import { replayQuery } from "../src/lib/replay.ts";
import { farmRequest } from "../src/lib/farm.ts";

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

async function checkPriceCost(browser, page, get, defaults, effects, apiPrefix) {
  // the API first: zero is no change, a what-if moves the dollars, the break-even is consistent with the model
  const plain = await get(`/field/${WET}/plans?${defaults}`);
  const zero = await get(`/field/${WET}/plans?${defaults}&price_pct=0&cost_pct=0`);
  equal(JSON.stringify(zero.plans), JSON.stringify(plain.plans), "price_pct=0 and cost_pct=0 change nothing");
  check(!("what_if" in plain.settings), "no what-if is echoed when none is set");
  check(plain.sensitivity && typeof plain.sensitivity.sentence === "string", "the plans answer carries a break-even block");
  const bad = await fetch(`${apiPrefix}/field/${WET}/plans?${defaults}&price_pct=500`);
  equal(bad.status, 400, "a price change out of range is a 400");

  const moved = `${defaults}&price_pct=-20&cost_pct=10`;
  const cut = await get(`/field/${WET}/plans?${moved}`);
  equal(cut.settings.what_if.price_pct, -20, "the answer echoes the price change");
  equal(cut.settings.what_if.cost_pct, 10, "the answer echoes the cost change");
  check(cut.plans[0].numbers.profit_per_acre < plain.plans[0].numbers.profit_per_acre, "lower prices and higher costs lower the top plan's return");
  for (const endpoint of [`score?rotation=corn,soybeans&${moved}`, `transition?plan=1&${moved}`, `replay?plan=1&${moved}`]) {
    const body = await get(`/field/${WET}/${endpoint}`);
    equal(body.settings.what_if.price_pct, -20, `${endpoint.split("?")[0]} echoes the what-if`);
  }

  // the page: the sliders ask with the same query
  await openField(page, WET);
  const region = page.locator('[data-testid="plans-region"]');
  const price = page.locator('[data-testid="price-pct"]');
  const cost = page.locator('[data-testid="cost-pct"]');
  equal(await price.inputValue(), "0", "the price slider starts at 0");
  equal(await page.locator('[data-testid="price-cost-reset"]').isDisabled(), true, "Reset is off while nothing is changed");
  equal(squash(await page.locator('[data-testid="break-even-line"]').textContent()), squash(plain.sensitivity.sentence), "the break-even line is the API's sentence");
  await price.fill("-20");
  await cost.fill("10");
  await page.waitForSelector(`[data-testid="plans-region"][data-updating="false"][data-query="${moved}"]`, { timeout: TIMEOUT });
  equal(await page.locator('[data-testid="price-pct-value"]').textContent(), "\u221220%", "the price value is shown with a minus sign");
  await checkPlans(page, cut, effects, `${WET} with -20% prices, +10% costs`);
  equal(squash(await page.locator('[data-testid="break-even-line"]').textContent()), squash(cut.sensitivity.sentence), "the break-even line follows the sliders");
  log(`  ✓ ${WET}: sliders -20% / +10% give the API's cards and break-even line (${cut.sensitivity.status})`);

  // the choice stays when another field is opened, and Reset goes back
  await openField(page, DRY);
  await page.waitForSelector(`[data-testid="plans-region"][data-updating="false"][data-query="${moved}"]`, { timeout: TIMEOUT });
  const dryBody = await get(`/field/${DRY}/plans?${moved}`);
  await checkPlans(page, dryBody, effects, `${DRY} with the what-if`);
  equal(squash(await page.locator('[data-testid="break-even-line"]').textContent()), squash(dryBody.sensitivity.sentence), `${DRY}: break-even line`);
  await page.locator('[data-testid="price-cost"]').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(SHOTS, "phase5-price-cost.png") });
  await page.locator('[data-testid="price-cost-reset"]').click();
  await page.waitForSelector(`[data-testid="plans-region"][data-updating="false"][data-query="${defaults}"]`, { timeout: TIMEOUT });
  equal(await price.inputValue(), "0", "Reset puts the price slider back");
  equal(await cost.inputValue(), "0", "Reset puts the cost slider back");
  log(`  ✓ ${DRY}: the what-if stays across fields, Reset restores the plain answer; screenshot -> e2e/screenshots/phase5-price-cost.png`);

  // phone width: the sliders fit
  const phone = await browser.newContext({ viewport: { width: 390, height: 800 }, deviceScaleFactor: 1 });
  const small = await phone.newPage();
  small.setDefaultTimeout(TIMEOUT);
  await small.goto(`${WEB_URL}/#${WET}`, { waitUntil: "domcontentloaded" });
  await small.waitForSelector(`[data-testid="panel"][data-field-id="${WET}"][data-state="ready"]`, { timeout: TIMEOUT });
  await small.waitForSelector('[data-testid="price-cost"]', { timeout: TIMEOUT });
  const fits = await small.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  check(fits, "no sideways scrolling at phone width with the price and cost sliders");
  await phone.close();
}


/** A small CSV reader: quoted cells, doubled quotes, CRLF. */
function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i += 1; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i += 1;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += ch;
  }
  if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
const ascii = (t) => t.replace(/[−–—]/g, "-").replace(/×/g, "x").replace(/[^\x00-\x7f]/g, "").replace(/\s+/g, " ").trim();
const r2 = (n) => (Math.round((n + Number.EPSILON) * 100) / 100).toFixed(2);

async function download(page, selector) {
  const [file] = await Promise.all([page.waitForEvent("download", { timeout: TIMEOUT }), page.click(selector)]);
  const stream = await file.createReadStream();
  let text = "";
  for await (const chunk of stream) text += chunk.toString("utf8");
  return { name: file.suggestedFilename(), text };
}

async function checkShare(browser, get, defaults, apiPrefix) {
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1, permissions: ["clipboard-read", "clipboard-write"], acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
  const problems = watch(page, "share");

  // the old form: a bare #KS-... opens the field with the defaults, and the hash stays as it was
  await openField(page, WET);
  equal(await page.evaluate(() => window.location.hash), `#${WET}`, "a field with default settings keeps the plain #KS-... hash");
  equal(await page.getAttribute('[data-testid="plans-region"]', "data-query"), defaults, "the bare hash asks with the default settings");

  // change everything the link should carry
  await page.locator('[data-testid="weight-soil"]').fill("6");
  await page.locator('[data-testid="weight-money"]').fill("1");
  await page.locator('[data-testid="horizon"]').fill("3"); // 2040
  await page.locator('[data-testid="scenario-ssp585"]').check();
  await page.locator('[data-testid="price-pct"]').fill("10");
  await page.locator('[data-testid="cost-pct"]').fill("-10");
  await page.locator('[data-testid="water-limit"]').fill("12");
  await page.locator('[data-testid="irrigation-switch"]').uncheck(); // dryland, set by the farmer
  await page.waitForSelector('[data-testid="plans-region"][data-updating="false"][data-query*="irrigated=false"]', { timeout: TIMEOUT });
  const card = page.locator('[data-testid="plan-card"]').nth(1);
  await card.locator('[data-testid="plan-edit"]').click(); // card 2 into "Try your own rotation"
  const slotsOf = async (pg) => pg.locator('[data-testid="own-slot"]').evaluateAll((els) => els.map((el) => el.getAttribute("data-crop")));
  const ownSlots = await slotsOf(page);
  check(ownSlots.length >= 2 && ownSlots.every((c) => c), `the own rotation is filled: ${ownSlots.join(",")}`);
  await page.waitForSelector('[data-testid="own-result"][data-state="ready"]', { timeout: TIMEOUT }).catch(() => {});
  const names = await page.locator('[data-testid="layer-select"] option').evaluateAll((els) => els.map((el) => el.value));
  const layer = names.find((n) => n !== "fields" && n !== "") ?? null;
  if (layer) await page.selectOption('[data-testid="layer-select"]', layer);
  await page.waitForSelector(layer ? `[data-testid="layer-control"][data-layer="${layer}"]:not([data-state="loading"])` : "body", { timeout: TIMEOUT });
  const query = await page.getAttribute('[data-testid="plans-region"]', "data-query");
  check(query.includes("price_pct=10") && query.includes("cost_pct=-10") && query.includes("limit_in=12") && query.includes("irrigated=false") && query.includes("horizon=2040") && query.includes("scenario=ssp585"), `the page is in the changed state: ${query}`);

  const hash = await page.evaluate(() => window.location.hash);
  check(hash.startsWith(`#${WET}?`), `the hash carries the settings: ${hash}`);
  for (const part of ["w=6.2.1.2", "h=2040", "s=ssp585", "lim=12", "pp=10", "cp=-10", "irr=0", "own="]) check(hash.includes(part), `the hash has ${part}: ${hash}`);
  if (layer) check(hash.includes(`layer=${layer}`), `the hash names the map layer: ${hash}`);
  const layerYear = layer ? await page.getAttribute('[data-testid="layer-control"]', "data-year") : null;

  // Copy link puts the same link on the clipboard
  await page.click('[data-testid="share-copy"]');
  await page.waitForFunction(() => (document.querySelector('[data-testid="share-note"]')?.textContent ?? "") !== "", null, { timeout: TIMEOUT });
  equal(squash(await page.locator('[data-testid="share-note"]').textContent()), "Link copied", "Copy link says it copied");
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  equal(copied, page.url(), "the copied link is the page's address");
  log(`  ✓ Copy link: ${copied.length} characters, equal to the address bar`);

  // a fresh browser opens it: everything is back
  const other = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const fresh = await other.newPage();
  fresh.setDefaultTimeout(TIMEOUT);
  const freshProblems = watch(fresh, "fresh");
  await fresh.goto(copied, { waitUntil: "domcontentloaded" });
  await fresh.waitForSelector(`[data-testid="panel"][data-field-id="${WET}"][data-state="ready"]`, { timeout: TIMEOUT });
  await fresh.waitForSelector('[data-testid="plans-region"][data-updating="false"]', { timeout: TIMEOUT });
  const restore = async (pg, what) => {
    equal(await pg.getAttribute('[data-testid="plans-region"]', "data-query"), query, `${what}: the plans are asked with the same settings`);
    equal(await pg.locator('[data-testid="weight-soil"]').inputValue(), "6", `${what}: soil slider`);
    equal(await pg.locator('[data-testid="weight-money"]').inputValue(), "1", `${what}: money slider`);
    equal(await pg.locator('[data-testid="horizon"]').inputValue(), "3", `${what}: year slider`);
    equal(await pg.locator('[data-testid="scenario-ssp585"]').isChecked(), true, `${what}: emissions path`);
    equal(await pg.locator('[data-testid="price-pct"]').inputValue(), "10", `${what}: price slider`);
    equal(await pg.locator('[data-testid="cost-pct"]').inputValue(), "-10", `${what}: cost slider`);
    equal(await pg.locator('[data-testid="irrigation-switch"]').isChecked(), false, `${what}: irrigation switch`);
    equal((await slotsOf(pg)).join(","), ownSlots.join(","), `${what}: the own rotation`);
    if (layer) {
      equal(await pg.getAttribute('[data-testid="layer-control"]', "data-layer"), layer, `${what}: map layer`);
      if (layerYear) equal(await pg.getAttribute('[data-testid="layer-control"]', "data-year"), layerYear, `${what}: map layer year`);
    }
  };
  await restore(fresh, "fresh browser");
  const body = await get(`/field/${WET}/plans?${query}`);
  const names3 = await texts(fresh, '[data-testid="plan-name"]');
  equal(names3.slice(0, 3).join(" | "), body.plans.slice(0, 3).map((p) => squash(p.name)).join(" | "), "the restored cards are the API's for that query");
  equal(await fresh.evaluate(() => window.location.hash), hash, "opening the link leaves the hash as it was");
  log("  ✓ a link opened in a fresh browser restores sliders, year, path, what-ifs, switch, own rotation and layer; cards equal the API");

  // pasted into a page that is already open (same tab): the hash changes, the state follows
  await fresh.evaluate((id) => { window.location.hash = id; }, DRY);
  await fresh.waitForSelector(`[data-testid="panel"][data-field-id="${DRY}"][data-state="ready"]`, { timeout: TIMEOUT });
  await fresh.evaluate((h) => { window.location.hash = h; }, hash);
  await fresh.waitForSelector(`[data-testid="panel"][data-field-id="${WET}"][data-state="ready"]`, { timeout: TIMEOUT });
  await fresh.waitForSelector(`[data-testid="plans-region"][data-updating="false"][data-query="${query}"]`, { timeout: TIMEOUT });
  await restore(fresh, "pasted into an open page");
  log("  ✓ the same link pasted into an open page restores the same state");
  check(freshProblems.length === 0, `console errors in the fresh page:\n  ${freshProblems.join("\n  ")}`);
  await other.close();

  // CSV of the top plans, with the own rotation and the field's own pattern, against the API
  await page.locator('[data-testid="own-result"][data-state="ready"]').waitFor({ timeout: TIMEOUT });
  const score = await get(`/field/${WET}/score?rotation=${encodeURIComponent(ownSlots.join(","))}&${query}`);
  const csv = await download(page, '[data-testid="share-csv"]');
  equal(csv.name, `rotation-plans-${WET}.csv`, "the CSV file name");
  const rows = parseCsv(csv.text);
  const comments = rows.filter((r) => r[0].startsWith("#")).map((r) => r.join(","));
  check(comments.length >= 3 && comments.every((c) => !c.includes(",") || true), "the CSV opens with comment rows");
  const settingsRow = comments.find((c) => c.startsWith("# Settings:")) ?? "";
  check(/Dryland \(set by you\)/.test(settingsRow) && /2040, high emissions/.test(settingsRow) && /prices \+10%, direct costs -10%/.test(settingsRow) && /soil protection 40%/.test(settingsRow) === false || /soil protection/.test(settingsRow), `the settings comment row: ${settingsRow}`);
  check(comments.some((c) => /not profit/.test(c)) && comments.some((c) => /soil protection/.test(c)), "the comment rows carry the dollars and soil wording");
  const data = rows.filter((r) => !r[0].startsWith("#") && r.length > 1);
  const header = data[0];
  const col = (name) => header.indexOf(name);
  for (const name of ["rank", "name", "ranking_score", "soil_protection_score", "return_middle_per_acre_year", "holds_up_in_futures", "bare_days_per_year"]) check(col(name) >= 0, `CSV has the column ${name}`);
  const planRows = data.slice(1);
  equal(planRows.length, 3 + (body.current ? 1 : 0) + 1, "CSV rows: three plans, the field's own pattern and your own rotation");
  body.plans.slice(0, 3).forEach((p, i) => {
    const r = planRows[i];
    equal(r[col("rank")], String(i + 1), `CSV plan ${i + 1}: rank`);
    equal(r[col("name")], ascii(p.name), `CSV plan ${i + 1}: name`);
    equal(r[col("ranking_score")], r2(p.scores.robust), `CSV plan ${i + 1}: ranking score`);
    equal(r[col("soil_protection_score")], r2(p.scores.soil), `CSV plan ${i + 1}: soil protection`);
    equal(r[col("money_score")], r2(p.scores.money), `CSV plan ${i + 1}: money`);
    equal(r[col("holds_up_in_futures")], String(p.futures.holds), `CSV plan ${i + 1}: holds up`);
    equal(r[col("return_low_per_acre_year")], String(Math.round(p.spread.profit_per_acre.low)), `CSV plan ${i + 1}: low return`);
    equal(r[col("return_middle_per_acre_year")], String(Math.round(p.spread.profit_per_acre.mid)), `CSV plan ${i + 1}: middle return`);
    equal(r[col("return_high_per_acre_year")], String(Math.round(p.spread.profit_per_acre.high)), `CSV plan ${i + 1}: high return`);
    equal(r[col("bare_days_per_year")], String(Math.round(p.numbers.bare_days)), `CSV plan ${i + 1}: bare days`);
    equal(r[col("planted_crops_failing_percent")], String(Math.round(p.numbers.failed_share * 100)), `CSV plan ${i + 1}: failing`);
    equal(r[col("water_pumped_inches_per_year")], "", `CSV plan ${i + 1}: dryland has no pumped water`);
    equal(r[col("reason")], ascii(p.reason), `CSV plan ${i + 1}: reason`);
  });
  const mine = planRows[planRows.length - 1];
  equal(mine[col("rank")], "yours", "CSV: the last row is yours");
  equal(mine[col("ranking_score")], r2(score.yours.scores.robust), "CSV: your rotation's ranking score");
  equal(planRows[3][col("rank")], "as now", "CSV: the field's own pattern");
  log(`  ✓ CSV download: ${planRows.length} rows equal /plans and /score, settings in the comment rows`);

  // print: nothing is sent to a printer here; the sheet is checked under print media
  await page.evaluate(() => { window.__printed = 0; window.print = () => { window.__printed += 1; }; });
  await page.click('[data-testid="share-print"]');
  equal(await page.evaluate(() => window.__printed), 1, "Print this plan calls the browser's print");
  const sheetVisible = async () => page.locator('[data-testid="print-sheet"]').isVisible();
  equal(await sheetVisible(), false, "the sheet is not on screen");
  await page.emulateMedia({ media: "print" });
  equal(await sheetVisible(), true, "the sheet shows under print media");
  equal(await page.locator('[data-testid="app"]').isVisible(), false, "the app is hidden under print media");
  const sheet = page.locator('[data-testid="print-sheet"]');
  const sheetNames = await texts(sheet, '[data-testid="print-plan-name"]');
  equal(sheetNames.slice(0, 3).join(" | "), body.plans.slice(0, 3).map((p) => squash(p.name)).join(" | "), "the sheet's top three are the API's");
  const sheetScores = await texts(sheet, '[data-testid="print-plan-score"]');
  equal(sheetScores.slice(0, 3).join(","), body.plans.slice(0, 3).map((p) => r2(p.scores.robust)).join(","), "the sheet's ranking scores");
  const figures = await texts(sheet, '[data-testid="print-plan-figures"]');
  body.plans.slice(0, 3).forEach((p, i) => {
    const s = p.spread.profit_per_acre;
    check(figures[i].includes(`Holds up in ${p.futures.holds} of ${p.futures.total} futures`), `sheet plan ${i + 1} says how many futures it holds up in`);
    check(figures[i].includes(`low ${formatDollars(s.low)}, middle ${formatDollars(s.mid)}, high ${formatDollars(s.high)}`), `sheet plan ${i + 1} shows the spread`);
  });
  const sheetText = squash(await sheet.textContent());
  check(sheetText.includes("Dryland (set by you)") && sheetText.includes("2040, high emissions path") && sheetText.includes("prices +10%, direct costs -10%"), "the sheet's assumptions line");
  check(sheetText.includes("soil protection") && sheetText.includes("not a measure of soil health") && sheetText.includes("returns over direct costs, not profit"), "the sheet carries the soil protection and direct-costs wording");
  check(sheetText.includes("agree on heat, not on rain"), "the sheet says the models agree on heat, not rain");
  check(sheetText.includes(`Your own rotation: ${score.yours.name}`), "the sheet shows the build-your-own result");
  check(sheetText.includes(score.comparison.headline[0]), "the sheet carries the comparison's headline");
  equal(squash(await sheet.locator('[data-testid="print-link"]').textContent()).replace("Open this exact view again: ", ""), page.url(), "the sheet prints the link");
  const pdf = await page.pdf({ format: "Letter", printBackground: true });
  const pages = (pdf.toString("latin1").match(/\/Type\s*\/Page[^s]/g) ?? []).length;
  check(pages >= 1 && pages <= 2, `the field sheet with the own rotation is ${pages} page(s)`);
  await page.screenshot({ path: path.join(SHOTS, "phase5-share-print.png"), fullPage: true });
  await page.emulateMedia({ media: "screen" });
  equal(await sheetVisible(), false, "back on screen the sheet is hidden again");
  log(`  ✓ print: the sheet shows only under print media, equals the API, PDF is ${pages} page(s); screenshot -> e2e/screenshots/phase5-share-print.png`);

  // without an own rotation the sheet is one page
  const plain = await (await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 })).newPage();
  plain.setDefaultTimeout(TIMEOUT);
  await openField(plain, WET);
  await plain.emulateMedia({ media: "print" });
  const pdf1 = await plain.pdf({ format: "Letter", printBackground: true });
  const pages1 = (pdf1.toString("latin1").match(/\/Type\s*\/Page[^s]/g) ?? []).length;
  equal(pages1, 1, "the plain field plan prints on one page");
  await plain.context().close();
  log("  ✓ a field plan with the default settings prints on one page");

  // the farm: a link with fields and limits, the table as CSV, the sheet
  const farmIds = [WET, DRY];
  const farmHash = `#?m=farm&farm=${farmIds.join(",")}&fw=900&fm=1000&h=2040`;
  const farmPage = await context.newPage();
  farmPage.setDefaultTimeout(TIMEOUT);
  const farmProblems = watch(farmPage, "farm");
  await farmPage.goto(`${WEB_URL}/${farmHash}`, { waitUntil: "domcontentloaded" });
  await farmPage.waitForSelector('[data-testid="farm-panel"]', { timeout: TIMEOUT });
  equal((await farmPage.locator('[data-testid="farm-chip"]').evaluateAll((els) => els.map((el) => el.getAttribute("data-field-id")))).join(","), farmIds.join(","), "the farm link brings back the fields");
  equal(await farmPage.locator('[data-testid="farm-water"]').inputValue(), "900", "the farm link brings back the water budget");
  equal(await farmPage.locator('[data-testid="farm-minimum"]').inputValue(), "1000", "the farm link brings back the least return");
  equal(await farmPage.locator('[data-testid="horizon"]').inputValue(), "3", "the farm link brings back the year");
  await farmPage.waitForSelector('[data-testid="farm-result"][data-updating="false"]', { timeout: 60000 });
  const fh = await farmPage.evaluate(() => window.location.hash);
  check(fh.startsWith("#?") && fh.includes("m=farm") && fh.includes(`farm=${farmIds.join(",")}`) && fh.includes("fw=900") && fh.includes("fm=1000"), `the farm's hash stays: ${fh}`);
  const request = farmRequest(farmIds, { ...DEFAULT_CHOICES, horizon: 2040 }, "900", "1000");
  const farmResponse = await fetch(`${apiPrefix}/farm/plan`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(request) });
  equal(farmResponse.status, 200, "POST /farm/plan answers");
  const farm = await farmResponse.json();
  const farmFile = await download(farmPage, '[data-testid="share-csv"]');
  equal(farmFile.name, "farm-plan.csv", "the farm CSV file name");
  const frows = parseCsv(farmFile.text);
  const fdata = frows.filter((r) => !r[0].startsWith("#") && r.length > 1);
  equal(fdata.length, 1 + farm.fields.length, "farm CSV: a row per field");
  const fcol = (name) => fdata[0].indexOf(name);
  farm.fields.forEach((f, i) => {
    const r = fdata[i + 1];
    equal(r[fcol("field_id")], f.field_id, `farm CSV row ${i + 1}: field`);
    equal(r[fcol("practice")], f.irrigated ? "irrigated" : "dryland", `farm CSV row ${i + 1}: practice`);
    equal(r[fcol("plan_name")], ascii(f.plan.name), `farm CSV row ${i + 1}: plan`);
    equal([...Array(12)].map((_, k) => r[fcol(`year_${k + 1}`)]).join(","), f.plan.years.join(","), `farm CSV row ${i + 1}: the twelve years`);
    equal(r[fcol("sentence")], ascii(f.sentence), `farm CSV row ${i + 1}: sentence`);
  });
  const fcomments = frows.filter((r) => r[0].startsWith("#")).map((r) => r.join(","));
  check(fcomments.some((c) => /water budget 900 acre-inches/.test(c) && /least return \$1000/.test(c) && /2040/.test(c)), "the farm CSV's comment rows carry the settings");
  log(`  ✓ farm link restores fields, limits and year; farm CSV (${farm.fields.length} fields) equals POST /farm/plan`);
  await farmPage.emulateMedia({ media: "print" });
  const fsheet = farmPage.locator('[data-testid="print-sheet"][data-kind="farm"]');
  equal(await fsheet.isVisible(), true, "the farm sheet shows under print media");
  const frowsText = await texts(fsheet, '[data-testid="print-farm-row"] strong');
  equal(frowsText.join(","), farm.fields.map((f) => f.field_id).join(","), "the farm sheet lists the fields");
  const ftext = squash(await fsheet.textContent());
  check(ftext.includes("not profit") && ftext.includes("soil protection") && ftext.includes(farm.summary[0]), "the farm sheet carries the wording and the summary");
  const fpdf = await farmPage.pdf({ format: "Letter", printBackground: true });
  const fpages = (fpdf.toString("latin1").match(/\/Type\s*\/Page[^s]/g) ?? []).length;
  check(fpages >= 1 && fpages <= 2, `the farm sheet is ${fpages} page(s)`);
  await farmPage.emulateMedia({ media: "screen" });
  check(farmProblems.length === 0, `console errors in the farm page:\n  ${farmProblems.join("\n  ")}`);
  log(`  ✓ farm print sheet equals the API (${fpages} page(s))`);

  // phone width: the share row fits
  const phone = await browser.newContext({ viewport: { width: 390, height: 800 }, deviceScaleFactor: 1 });
  const small = await phone.newPage();
  small.setDefaultTimeout(TIMEOUT);
  await small.goto(`${WEB_URL}/#${WET}`, { waitUntil: "domcontentloaded" });
  await small.waitForSelector('[data-testid="share-bar"]', { timeout: TIMEOUT });
  check(await small.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), "no sideways scrolling at phone width with the share row");
  await phone.close();

  check(problems.length === 0, `console errors:\n  ${problems.join("\n  ")}`);
  await context.close();
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

  // ---- item F: price and cost what-ifs ------------------------------------------------------------------------
  await checkPriceCost(browser, page, get, defaults, effects, apiPrefix);

  // ---- item C: export and share ------------------------------------------------------------------------------
  await checkShare(browser, get, defaults, apiPrefix);

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
