#!/usr/bin/env node
// The states the smoke test cannot reach with healthy data: loading, failures and retries,
// an unknown field in the URL, and a deliberately awkward field (gaps, long crop names, nulls).
// Each state is forced by intercepting the app's API requests in the browser — the API itself
// is untouched — then checked and photographed into e2e/screenshots/state-*.png.
//
//   WEB_URL=http://localhost:5173 npm run e2e:states
//
// Run it after (or instead of) `npm run e2e`; it needs the same running web + API.

import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const WEB_URL = (process.env.WEB_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const TIMEOUT = Number.parseInt(process.env.E2E_TIMEOUT_MS ?? "20000", 10) || 20000;
const SHOTS = path.join(path.dirname(fileURLToPath(import.meta.url)), "screenshots");

let checks = 0;
class Failure extends Error {}
function check(condition, message) {
  checks++;
  if (!condition) throw new Failure(message);
}
const equal = (actual, expected, what) =>
  check(actual === expected, `${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
const log = (line) => console.log(line);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A field built to break layouts: gaps, 8 crops, long names, no soil values, no repeated crop. */
function awkwardField(fieldId) {
  const crops = [
    "double crop: winter wheat/sorghum",
    "grass/pasture",
    "other hay/non alfalfa",
    "corn",
    "winter wheat",
    "sunflower",
    "fallow",
    "triticale",
  ];
  const history = {};
  for (let year = 2008; year <= 2024; year++) {
    if (year === 2011 || year === 2012 || year === 2021) continue; // no observation
    history[String(year)] = crops[(year - 2008) % crops.length];
  }
  const counts = {};
  for (const crop of Object.values(history)) counts[crop] = (counts[crop] ?? 0) + 1;
  const distinct = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  return {
    field_id: fieldId,
    geometry: { type: "Polygon", coordinates: [[[0, 0], [0, 1], [1, 1], [0, 0]]] },
    crop_history: history,
    organic_matter_pct: null,
    slope_pct: null,
    area_ha: 1234.56,
    stats: {
      years_covered: Object.keys(history).length,
      distinct_crops: distinct,
      distinct_crop_count: distinct.length,
      longest_streak: { crop: "triticale", years: 1, start_year: 2024, end_year: 2024 },
      pct_years_corn: Math.round((1000 * (counts.corn ?? 0)) / Object.keys(history).length) / 10,
    },
  };
}

function awkwardPlans(fieldId) {
  const reason =
    "A deliberately long explanation to see how the card copes: it keeps residue on the surface through the " +
    "windiest months, spreads irrigation demand across the season and leaves room for a cash crop in year four.";
  return {
    field_id: fieldId,
    plans: [
      { name: "Winter wheat – Cover crop – Sorghum – Fallow", rotation: ["winter wheat", "cover crop", "sorghum", "fallow"], scores: { soil: 1, water: 0.995, money: 0, overall: 0.665 }, reason },
      { name: "Soybeans – Corn – Winter wheat – Cover crop", rotation: ["soybeans", "corn", "winter wheat", "cover crop"], scores: { soil: 0.5, water: 0.05, money: 0.905, overall: 0.485 }, reason: "Short reason." },
      { name: "Alfalfa (3 yrs) – Corn", rotation: ["alfalfa", "alfalfa", "alfalfa", "corn"], scores: { soil: 0.82, water: 0.204, money: 0.591, overall: 0.4 }, reason: "Builds the soil with deep-rooted alfalfa." },
    ],
  };
}

async function main() {
  await mkdir(SHOTS, { recursive: true });
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    page.setDefaultTimeout(TIMEOUT);
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message)); // console errors are expected here (404s, aborts)

    const shot = async (name) => {
      await page.waitForTimeout(350);
      await page.screenshot({ path: path.join(SHOTS, name) });
      return `e2e/screenshots/${name}`;
    };
    const drawn = () => page.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });
    const panelState = (id, state) =>
      page.waitForSelector(`[data-testid="panel"][data-field-id="${id}"][data-state="${state}"]`, { timeout: TIMEOUT });
    const isApi = (suffix) => (url) => new RegExp(`/api${suffix}$`).test(url.pathname) || url.pathname.endsWith(suffix);

    // ---- 1. fields loading, then failing, then recovering through Retry ----------------------
    let failFields = true;
    await page.route(isApi("/fields"), async (route) => {
      await sleep(900);
      if (failFields) await route.abort("connectionrefused");
      else await route.continue();
    });
    await page.goto(`${WEB_URL}/`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector('.masthead-note[role="status"]');
    equal((await page.textContent('.masthead-note[role="status"]')).trim(), "Loading fields…", "loading note");
    log(`  ✓ loading: "Loading fields…" while GET /fields is pending → ${await shot("state-1-loading.png")}`);

    await page.waitForSelector('[data-testid="fields-error"]');
    const errorText = (await page.textContent('[data-testid="fields-error"]')).replace(/\s+/g, " ").trim();
    check(errorText.includes("Could not load the fields"), `fields error has no title: "${errorText}"`);
    check(errorText.includes(`${WEB_URL}/api`), `fields error does not name the API URL: "${errorText}"`);
    equal(await page.locator('[data-testid="hint"]').count(), 0, "hint shown while the fields failed to load");
    log(`  ✓ fields failed: "${errorText.replace(/Retry$/, "").trim()}" → ${await shot("state-2-fields-error.png")}`);

    failFields = false;
    await page.click('[data-testid="fields-error"] button');
    await drawn();
    equal(await page.locator('[data-testid="fields-error"]').count(), 0, "error card after a successful retry");
    await page.unroute(isApi("/fields"));
    log("  ✓ Retry loads the fields and removes the error");

    const apiBase = await page.getAttribute('[data-testid="app"]', "data-api-base");
    const fieldIds = await page.evaluate(
      async (url) => (await (await fetch(url)).json()).features.slice(0, 2).map((feature) => String(feature.id)),
      `${apiBase}/fields`,
    );
    const [idA, idB] = [fieldIds[0], fieldIds[1] ?? fieldIds[0]];

    // ---- 2. panel opens at once with a skeleton; detail and plans fail independently ----------
    let failDetail = true;
    let failPlans = false;
    await page.route(isApi(`/field/${idA}`), async (route) => {
      await sleep(800);
      if (failDetail) await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"Internal Server Error"}' });
      else await route.continue();
    });
    await page.route(isApi(`/field/${idA}/plans`), async (route) => {
      await sleep(800);
      if (failPlans) await route.abort("connectionreset");
      else await route.continue();
    });
    await page.evaluate((id) => (window.location.hash = id), idA);
    await panelState(idA, "loading");
    equal((await page.textContent('[data-testid="panel-title"]')).trim(), idA, "panel title while loading");
    check((await page.locator(".skeleton").count()) >= 5, "no skeleton shown while the field is loading");
    check(/ha/.test(await page.textContent('[data-testid="panel-area"]')), "area (known from the map) not shown while loading");
    log(`  ✓ panel opens immediately with title, area and skeletons → ${await shot("state-3-panel-loading.png")}`);

    await panelState(idA, "error");
    await page.waitForSelector('[data-testid="plan-card"]'); // plans arrived although the detail failed
    equal(await page.locator('[data-testid="error-note"]').count(), 1, "error notes with only the detail failing");
    check((await page.textContent('[data-testid="error-note"]')).includes("Could not load this field"), "detail error title");
    equal(await page.locator('[data-testid="plan-card"]').count(), 3, "plan cards while the detail failed");
    log(`  ✓ detail failed, plans still shown → ${await shot("state-4-detail-error.png")}`);

    failDetail = false;
    await page.click('[data-testid="error-note"] button');
    await panelState(idA, "ready");
    equal(await page.locator('[data-testid="error-note"]').count(), 0, "error notes after retrying the detail");
    check((await page.locator('[data-testid="timeline-cell"]').count()) > 0, "timeline after retrying the detail");
    log("  ✓ Retry on the detail error recovers it (plans were not refetched into an error)");

    failPlans = true;
    await page.keyboard.press("Escape");
    await page.evaluate((id) => (window.location.hash = id), idA);
    await panelState(idA, "error");
    await page.waitForSelector('[data-testid="timeline-cell"]');
    equal(await page.locator('[data-testid="error-note"]').count(), 1, "error notes with only the plans failing");
    check((await page.textContent('[data-testid="error-note"]')).includes("Could not load rotation plans"), "plans error title");
    const plansShot = await shot("state-5-plans-error.png");
    failPlans = false;
    await page.click('[data-testid="error-note"] button');
    await panelState(idA, "ready");
    equal(await page.locator('[data-testid="plan-card"]').count(), 3, "plan cards after retrying the plans");
    log(`  ✓ plans failed, crop history and soil still shown; Retry recovers → ${plansShot}`);
    await page.unroute(isApi(`/field/${idA}`));
    await page.unroute(isApi(`/field/${idA}/plans`));

    // ---- 3. a field id in the URL that does not exist ----------------------------------------
    await page.evaluate(() => (window.location.hash = "KS-999999"));
    await panelState("KS-999999", "error");
    const notFound = (await page.locator('[data-testid="error-note"]').first().textContent()).replace(/\s+/g, " ");
    check(notFound.includes("This field is not in the current data set"), `unknown id message: "${notFound}"`);
    await page.waitForTimeout(400); // let the plans request come back (404) too
    equal(await page.locator('[data-testid="error-note"]').count(), 1, "unknown id: one message, not one per request");
    equal(await page.locator('[data-testid="error-note"] button').count(), 0, "unknown id: nothing to retry");
    equal(await page.locator("#section-plans").count(), 0, "unknown id: no plans section");
    equal(await page.locator(".panel-head .skeleton").count(), 0, "unknown id: no skeleton left in the header");
    log(`  ✓ unknown id in the URL: "${notFound.trim()}" → ${await shot("state-6-unknown-field.png")}`);

    // ---- 4. an awkward field: gaps, long names, 8 crops, nulls, extreme scores ---------------
    const awkward = awkwardField(idB);
    await page.route(isApi(`/field/${idB}`), (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(awkward) }));
    await page.route(isApi(`/field/${idB}/plans`), (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(awkwardPlans(idB)) }));
    await page.evaluate((id) => (window.location.hash = id), idB);
    await panelState(idB, "ready");
    equal(await page.locator('[data-testid="timeline-cell"]').count(), 17, "awkward field: year cells");
    equal(await page.locator('[data-testid="timeline-cell"].is-empty').count(), 3, 'awkward field: "no data" cells');
    equal((await page.textContent('[data-testid="history-range"]')).replace(/\s+/g, " ").trim(), "2008–2024 · 14 of 17 years on record", "awkward field: range");
    equal((await page.textContent('[data-testid="stat-om"]')).trim(), "No data", "awkward field: organic matter");
    equal((await page.textContent('[data-testid="stat-slope"]')).trim(), "No data", "awkward field: slope");
    equal((await page.textContent('[data-testid="stat-streak"]')).trim(), "No crop grown two years running", "awkward field: streak of one");
    const legend = (await page.locator('[data-testid="legend"] li').allTextContents()).map((s) => s.replace(/\s+/g, " ").trim());
    equal(legend.length, 9, "awkward field: legend entries (8 crops + no data)");
    equal(legend[legend.length - 1], "No data3 years", "awkward field: last legend entry");
    equal((await page.locator('[data-testid="plan-overall"]').allTextContents()).join(" "), "0.67 0.49 0.40", "awkward field: overall scores (halves round up)");
    const overflow = await page.evaluate(() => {
      const body = document.querySelector(".panel-body");
      const clipped = [...document.querySelectorAll(".chip, .legend-item, .facts dd, .tile, .plan")].filter((node) => node.scrollWidth > node.clientWidth + 1);
      return { body: body.scrollWidth - body.clientWidth, clipped: clipped.map((node) => node.className) };
    });
    check(overflow.body <= 0 && overflow.clipped.length === 0, `awkward field: content overflows (${JSON.stringify(overflow)})`);
    const awkwardShot = await shot("state-7-awkward-field.png");

    // tooltips at both ends must stay inside the panel; arrow keys walk the years
    const panelBox = await page.locator('[data-testid="panel"]').boundingBox();
    for (const [index, expected] of [[0, "2008: double crop: winter wheat/sorghum"], [16, "2024: double crop: winter wheat/sorghum"], [3, "2011: no data"]]) {
      await page.locator('[data-testid="timeline-cell"]').nth(index).hover();
      equal((await page.textContent('[data-testid="timeline-tip"]')).trim(), expected, `awkward field: tooltip for cell ${index}`);
      const tip = await page.locator('[data-testid="timeline-tip"]').boundingBox();
      check(tip.x >= panelBox.x && tip.x + tip.width <= panelBox.x + panelBox.width, `tooltip for cell ${index} leaves the panel (${JSON.stringify(tip)})`);
    }
    const tipShot = await shot("state-8-tooltip.png");
    await page.mouse.move(5, 5);
    await page.focus('[data-testid="timeline"]');
    equal((await page.textContent('[data-testid="timeline-tip"]')).trim(), "2024: double crop: winter wheat/sorghum", "keyboard: focus shows the latest year");
    await page.keyboard.press("ArrowLeft");
    equal((await page.textContent('[data-testid="timeline-tip"]')).trim(), "2023: triticale", "keyboard: ArrowLeft");
    await page.keyboard.press("Home");
    equal((await page.textContent('[data-testid="timeline-tip"]')).trim(), "2008: double crop: winter wheat/sorghum", "keyboard: Home");
    // legend entry picked (keyboard: focus + Enter) → the other crops fade; picked again → back
    const entry = page.locator('[data-testid="legend"] button').nth(1);
    await entry.focus();
    await page.keyboard.press("Enter");
    equal(await entry.getAttribute("aria-pressed"), "true", "legend entry pressed state");
    const dimmed = await page.locator('[data-testid="timeline-cell"].is-dim').count();
    equal(dimmed, 15, "years faded while Fallow (2 years) is picked in the legend");
    const legendShot = await shot("state-9-legend-picked.png");
    await page.keyboard.press("Enter");
    equal(await page.locator('[data-testid="timeline-cell"].is-dim').count(), 0, "years faded after un-picking");
    log(`  ✓ awkward field: 3 "no data" cells, 9 legend entries, nulls, long names, nothing overflows → ${awkwardShot}`);
    log(`  ✓ tooltips stay inside the panel; arrow keys step through years; picking a legend entry fades the other crops → ${tipShot}, ${legendShot}`);

    // the same awkward field in the plans area and on a narrow desktop window
    await page.locator('[data-testid="plans"]').scrollIntoViewIfNeeded();
    await page.locator('[data-testid="plan-card"]').nth(2).scrollIntoViewIfNeeded();
    const plansAwkward = await shot("state-10-awkward-plans.png");
    await page.setViewportSize({ width: 800, height: 600 });
    await page.waitForTimeout(300);
    const narrow = await page.evaluate(() => {
      const box = (selector) => document.querySelector(selector).getBoundingClientRect();
      const [mast, zoom, panel] = [box(".masthead"), box(".leaflet-control-zoom"), box('[data-testid="panel"]')];
      return { mastRight: mast.right, zoomLeft: zoom.left, panelLeft: panel.left, mapWidth: box(".leaflet-container").width };
    });
    check(narrow.mastRight < narrow.zoomLeft, `800px window: the title card overlaps the zoom buttons (${JSON.stringify(narrow)})`);
    check(narrow.mapWidth >= 300, `800px window: only ${narrow.mapWidth}px of map left`);
    log(`  ✓ extreme scores and a long reason; 800×600 window keeps ${Math.round(narrow.mapWidth)} px of map → ${plansAwkward}, ${await shot("state-11-narrow-desktop.png")}`);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.unroute(isApi(`/field/${idB}`));
    await page.unroute(isApi(`/field/${idB}/plans`));

    // ---- 5. /health down: the badge falls back to the count from /fields, no county claim ------
    await page.route(isApi("/health"), (route) => route.abort("connectionrefused"));
    await page.goto(`${WEB_URL}/`, { waitUntil: "domcontentloaded" });
    await page.reload({ waitUntil: "domcontentloaded" });
    await drawn();
    await page.waitForTimeout(300);
    const badge = (await page.textContent('[data-testid="badge"]')).trim();
    check(/^[\d,]+ fields?$/.test(badge), `badge with /health down should be just the count, got "${badge}"`);
    log(`  ✓ /health unreachable: badge reads "${badge}"`);

    // ---- 6. hover highlight on the map ---------------------------------------------------------
    const spot = await page.evaluate(() => {
      const map = window.__map;
      let layer = null;
      map.eachLayer((candidate) => {
        if (!layer && candidate.feature && map.getBounds().pad(-0.3).contains(candidate.getBounds().getCenter())) layer = candidate;
      });
      if (!layer) return null;
      const bounds = layer.getBounds();
      map.fitBounds(bounds.pad(2.5), { animate: false });
      const p = map.latLngToContainerPoint(bounds.getCenter());
      const frame = map.getContainer().getBoundingClientRect();
      return { x: frame.left + p.x, y: frame.top + p.y };
    });
    if (spot) {
      await page.waitForTimeout(400);
      await page.mouse.move(spot.x - 3, spot.y - 3);
      await page.waitForTimeout(80);
      await page.mouse.move(spot.x, spot.y);
      await page.waitForTimeout(80);
      log(`  ✓ hover highlight → ${await shot("state-12-hover.png")}`);
    }

    check(pageErrors.length === 0, `uncaught page errors: ${pageErrors.join(" | ")}`);
    await context.close();
  } finally {
    await browser.close();
  }
}

try {
  log(`states check: ${WEB_URL}`);
  await main();
  log(`\nPASS: ${checks} checks`);
} catch (error) {
  console.error(`\nFAIL: ${error instanceof Failure ? error.message : (error.stack ?? error)}`);
  process.exitCode = 1;
}
