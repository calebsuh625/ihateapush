#!/usr/bin/env node
// End-to-end smoke test: drives the real app in headless Chromium against a running web + API.
//
//   npm run e2e                                   # http://localhost:5173
//   WEB_URL=http://localhost:5181 N_FIELDS=6 npm run e2e
//
// It loads the page, really clicks N fields on the map (at a pixel computed from the field's
// geometry), and checks what the panel shows against what the API returns. It also checks the
// URL hash, Esc, link restore, the mobile bottom sheet, and prints load / click / pan timings.
// Screenshots go to e2e/screenshots/. Exits non-zero with a message on the first failure.
//
// Environment: WEB_URL, N_FIELDS (default 4), E2E_TIMEOUT_MS (default 20000),
//              STRICT_TILES=1 (treat failed OpenStreetMap tile downloads as failures too).

import { mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const WEB_URL = (process.env.WEB_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const N_FIELDS = Math.max(1, Number.parseInt(process.env.N_FIELDS ?? "4", 10) || 4);
const TIMEOUT = Number.parseInt(process.env.E2E_TIMEOUT_MS ?? "20000", 10) || 20000;
const STRICT_TILES = process.env.STRICT_TILES === "1";
const SHOTS = path.join(path.dirname(fileURLToPath(import.meta.url)), "screenshots");

// Outline colours of a selected / hovered polygon — must match src/components/FieldMap.tsx.
const SELECTED_OUTLINE = "#1d4ed8";
const HOVER_OUTLINE = "#0c3523";
const TILE_HOST = "tile.openstreetmap.org";
const EN_DASH = "–";

// ------------------------------------------------------------------------------------------
// Reporting
// ------------------------------------------------------------------------------------------

let checks = 0;
const warnings = [];

class Failure extends Error {}

function check(condition, message) {
  checks++;
  if (!condition) throw new Failure(message);
}

function equal(actual, expected, what) {
  check(actual === expected, `${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

const log = (line) => console.log(line);
const ms = (n) => `${Math.round(n)} ms`;

// ------------------------------------------------------------------------------------------
// Geometry (lon/lat rings, as served by GET /fields)
// ------------------------------------------------------------------------------------------

/** Even-odd test against all rings, so holes count as outside. */
function insidePolygon(lon, lat, rings) {
  let inside = false;
  for (const ring of rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if (yi > lat !== yj > lat && lon < xi + ((lat - yi) * (xj - xi)) / (yj - yi)) inside = !inside;
    }
  }
  return inside;
}

/** Distance in metres from a point to the nearest polygon edge (flat-earth, fine at field scale). */
function clearanceMetres(lon, lat, rings) {
  const kx = 111_320 * Math.cos((lat * Math.PI) / 180);
  const ky = 110_540;
  let best = Infinity;
  for (const ring of rings) {
    for (let i = 0; i + 1 < ring.length; i++) {
      const ax = (ring[i][0] - lon) * kx;
      const ay = (ring[i][1] - lat) * ky;
      const bx = (ring[i + 1][0] - lon) * kx;
      const by = (ring[i + 1][1] - lat) * ky;
      const dx = bx - ax;
      const dy = by - ay;
      const len2 = dx * dx + dy * dy;
      const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2));
      best = Math.min(best, Math.hypot(ax + t * dx, ay + t * dy));
    }
  }
  return best;
}

/**
 * Points guaranteed to be inside the polygon, best (furthest from any edge) first.
 * A centroid can fall outside a concave field or inside a hole; instead, cut the polygon with
 * horizontal lines and take the midpoint of each inside stretch — inside by construction.
 */
function interiorPoints(rings) {
  const lats = rings[0].map((p) => p[1]);
  const south = Math.min(...lats);
  const north = Math.max(...lats);
  const points = [];
  for (let step = 1; step < 16; step++) {
    const lat = south + ((north - south) * step) / 16;
    const crossings = [];
    for (const ring of rings) {
      for (let i = 0; i + 1 < ring.length; i++) {
        const [x1, y1] = ring[i];
        const [x2, y2] = ring[i + 1];
        if (y1 > lat !== y2 > lat) crossings.push(x1 + ((lat - y1) * (x2 - x1)) / (y2 - y1));
      }
    }
    crossings.sort((a, b) => a - b);
    for (let i = 0; i + 1 < crossings.length; i += 2) {
      const lon = (crossings[i] + crossings[i + 1]) / 2;
      if (crossings[i + 1] > crossings[i] && insidePolygon(lon, lat, rings)) {
        points.push({ lon, lat, clearance: clearanceMetres(lon, lat, rings) });
      }
    }
  }
  return points.sort((a, b) => b.clearance - a.clearance);
}

function bboxOf(rings) {
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (const [lon, lat] of rings[0]) {
    west = Math.min(west, lon);
    east = Math.max(east, lon);
    south = Math.min(south, lat);
    north = Math.max(north, lat);
  }
  return { west, south, east, north };
}

/** The best interior point of `feature` that no other field covers, or null. */
function clickablePoint(feature, features, boxes) {
  const candidates = interiorPoints(feature.geometry.coordinates);
  for (const point of candidates) {
    const covered = features.some((other, i) => {
      if (other === feature) return false;
      const box = boxes[i];
      if (point.lon < box.west || point.lon > box.east || point.lat < box.south || point.lat > box.north) return false;
      return insidePolygon(point.lon, point.lat, other.geometry.coordinates);
    });
    if (!covered) return point;
  }
  return null;
}

/** Smallest zoom at which `metres` spans at least `pixels` on screen. */
function zoomForClearance(metres, lat, pixels) {
  const metresPerPixelAtZoom0 = 156_543.03 * Math.cos((lat * Math.PI) / 180);
  return Math.ceil(Math.log2((metresPerPixelAtZoom0 * pixels) / Math.max(metres, 0.5)));
}

// ------------------------------------------------------------------------------------------
// What the panel should say (mirrors src/lib/format.ts and FieldPanel.tsx)
// ------------------------------------------------------------------------------------------

const titleCase = (crop) => crop.replace(/(^|[\s/(-])([a-z])/g, (_m, before, letter) => before + letter.toUpperCase());
const oneDecimal = (n) => new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(n);
const percent = (n) => `${oneDecimal(n)}%`;
const score = (n) => (Math.round((n + Number.EPSILON) * 100) / 100).toFixed(2);

function expectedStreak(streak) {
  if (!streak) return "No data";
  if (streak.years <= 1) return "No crop grown two years running";
  return `${titleCase(streak.crop)} × ${streak.years}, ${streak.start_year}${EN_DASH}${streak.end_year}`;
}

// ------------------------------------------------------------------------------------------
// Page helpers
// ------------------------------------------------------------------------------------------

/**
 * Collect everything that should fail the run: console errors, uncaught page errors, and API
 * requests that fail or answer 4xx/5xx. Failed OpenStreetMap tile downloads are an outside
 * dependency, so they are reported as warnings unless STRICT_TILES=1.
 */
function watch(page, label) {
  const problems = [];
  const requests = []; // { url, problem } — judged in assertClean, once the API base is known
  const isTile = (url) => url.includes(TILE_HOST);
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location()?.url ?? "";
    const text = `${label}: console error: ${message.text()}${where ? ` (${where})` : ""}`;
    if (isTile(where) && !STRICT_TILES) warnings.push(text);
    else problems.push(text);
  });
  page.on("pageerror", (error) => problems.push(`${label}: uncaught page error: ${error.message}`));
  page.on("requestfailed", (request) => {
    const url = request.url();
    const reason = request.failure()?.errorText ?? "failed";
    // Cancelled by the page itself, on purpose: the app aborts superseded API requests
    // (AbortController) and Leaflet drops tiles for a view the map has already left.
    if (reason === "net::ERR_ABORTED") return;
    if (isTile(url)) (STRICT_TILES ? problems : warnings).push(`${label}: map tile failed: ${url} (${reason})`);
    else requests.push({ url, problem: `${label}: API request failed: ${url} (${reason})` });
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() < 400) return;
    if (isTile(url)) (STRICT_TILES ? problems : warnings).push(`${label}: map tile answered ${response.status()}: ${url}`);
    else requests.push({ url, problem: `${label}: API answered ${response.status()} for ${url}` });
  });
  return {
    /** `apiPrefix`: absolute URL every API request starts with. */
    assertClean(apiPrefix) {
      const failedCalls = requests.filter((request) => request.url.startsWith(apiPrefix)).map((request) => request.problem);
      const all = [...problems, ...failedCalls];
      check(all.length === 0, all.join("\n"));
    },
  };
}

const twoFrames = (page) =>
  page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

/** Load the app and wait until the field polygons are on the canvas. Returns timings in ms. */
async function openApp(page, url) {
  const started = performance.now();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: TIMEOUT });
  // done when the polygons are drawn — or when the app itself reports that it could not load them
  const outcome = await page
    .waitForSelector('[data-testid="app"][data-fields-drawn], [data-testid="fields-error"]', { timeout: TIMEOUT })
    .catch(() => null);
  if (!outcome || (await page.locator('[data-testid="fields-error"]').count()) > 0) {
    const state = await page.getAttribute('[data-testid="app"]', "data-fields", { timeout: 1000 }).catch(() => null);
    const said = await page.innerText('[data-testid="fields-error"] .error-note > div', { timeout: 1000 }).catch(() => null);
    throw new Failure(
      (said ? `the app could not load the fields at ${url}` : `fields did not render within ${TIMEOUT} ms at ${url}`) +
        ` (state: ${state ?? "app did not mount"})` +
        (said ? `\n  the page says: ${said.replace(/\s+/g, " ").trim()}` : ""),
    );
  }
  await twoFrames(page);
  const wall = performance.now() - started;
  const inPage = await page.evaluate(() => {
    const mark = performance.getEntriesByName("fields-rendered").pop();
    // the last one: under React StrictMode (dev) the first request is cancelled and repeated
    const fetched = performance.getEntriesByType("resource").filter((entry) => /\/fields(\?|$)/.test(entry.name)).pop();
    return {
      sinceNavigation: mark ? mark.startTime : null,
      fetchMs: fetched ? fetched.duration : null,
      fetchKb: fetched && fetched.encodedBodySize ? fetched.encodedBodySize / 1024 : null,
    };
  });
  return { wall, ...inPage };
}

/** Give the base map a moment to finish loading so screenshots are not half-tiled. */
async function settleTiles(page, limit = 6000) {
  await page
    .waitForFunction(
      () => {
        const tiles = [...document.querySelectorAll("img.leaflet-tile")];
        return tiles.length > 0 && tiles.every((tile) => tile.complete);
      },
      undefined,
      { timeout: limit },
    )
    .catch(() => warnings.push("map tiles were still loading when a screenshot was taken"));
  await page.waitForTimeout(250); // Leaflet fades tiles in over 200 ms
}

const shot = async (page, name) => {
  const file = path.join(SHOTS, name);
  await page.screenshot({ path: file });
  return path.relative(process.cwd(), file);
};

/**
 * Find the page coordinates of `point` (a spot inside a field) and make sure the map — not the
 * title card or a control — is what is under them.
 *   zoom = a number: centre the map on the point at that zoom, then shift it by `offset` pixels.
 *   zoom = null:     leave the view alone; only if something covers the point, centre on it.
 */
async function aimAt(page, point, zoom, offset) {
  const target = await page.evaluate(
    ({ lat, lon, zoom, offset }) =>
      new Promise((resolve) => {
        const map = window.__map;
        const settle = (then) => requestAnimationFrame(() => requestAnimationFrame(then));
        const locate = () => {
          const inMap = map.latLngToContainerPoint([lat, lon]);
          const frame = map.getContainer().getBoundingClientRect();
          const x = frame.left + inMap.x;
          const y = frame.top + inMap.y;
          const element = document.elementFromPoint(x, y);
          return {
            x,
            y,
            zoom: map.getZoom(),
            onMap: element instanceof HTMLCanvasElement && !!element.closest(".leaflet-container"),
            under: element ? `${element.tagName.toLowerCase()}.${element.className}` : "nothing",
          };
        };
        if (zoom !== null) {
          map.setView([lat, lon], zoom, { animate: false });
          map.panBy(offset, { animate: false });
        }
        settle(() => {
          const where = locate();
          if (where.onMap || zoom !== null) return resolve(where);
          map.panTo([lat, lon], { animate: false });
          settle(() => resolve(locate()));
        });
      }),
    { lat: point.lat, lon: point.lon, zoom, offset },
  );
  check(target.onMap, `the pixel for the field (${Math.round(target.x)}, ${Math.round(target.y)}) is covered by <${target.under}>`);
  return target;
}

/** Start an in-page stopwatch: from the next click event to the panel being fully populated. */
async function armStopwatch(page, fieldId) {
  await page.evaluate((id) => {
    const watch = { clicked: null, populated: null };
    window.__e2eWatch = watch;
    window.addEventListener("click", () => (watch.clicked = performance.now()), { capture: true, once: true });
    const selector = `[data-testid="panel"][data-field-id="${id}"][data-state="ready"]`;
    const observer = new MutationObserver(() => {
      if (!document.querySelector(selector)) return;
      watch.populated = performance.now();
      observer.disconnect();
    });
    observer.observe(document.body, { subtree: true, childList: true, attributes: true });
  }, fieldId);
}

async function readStopwatch(page) {
  return page.evaluate(() => {
    const { clicked, populated } = window.__e2eWatch ?? {};
    return clicked != null && populated != null ? populated - clicked : null;
  });
}

async function waitForPanel(page, fieldId) {
  const ready = `[data-testid="panel"][data-field-id="${fieldId}"][data-state="ready"]`;
  try {
    await page.waitForSelector(ready, { timeout: TIMEOUT });
  } catch {
    const panel = page.locator('[data-testid="panel"]');
    if ((await panel.count()) === 0) throw new Failure(`${fieldId}: clicking the field did not open the panel`);
    const shown = await panel.getAttribute("data-field-id");
    const state = await panel.getAttribute("data-state");
    if (shown !== fieldId) throw new Failure(`${fieldId}: the click selected ${shown} instead`);
    const errors = await page.locator('[data-testid="error-note"]').allTextContents();
    throw new Failure(`${fieldId}: panel stuck in state "${state}"${errors.length ? `: ${errors.join(" | ")}` : ""}`);
  }
}

const text = async (page, testId) => ((await page.locator(`[data-testid="${testId}"]`).first().textContent()) ?? "").trim();

/** Compare everything in the open panel with the API's answers for that field. */
async function checkPanel(page, fieldId, detail, plans, satellite, soil) {
  const panel = page.locator('[data-testid="panel"]');
  equal(await text(page, "panel-title"), fieldId, `${fieldId}: panel title`);
  const areaText = await text(page, "panel-area");
  check(areaText.startsWith(`${oneDecimal(detail.area_ha)} ha`), `${fieldId}: area reads "${areaText}", expected ${oneDecimal(detail.area_ha)} ha`);
  check(areaText.includes(`${oneDecimal(detail.area_ha * 2.47105)} acres`), `${fieldId}: acres missing or wrong in "${areaText}"`);

  // timeline: one cell per year across the range, "no data" cells for the gaps, right crops
  const years = Object.keys(detail.crop_history).map(Number).sort((a, b) => a - b);
  const span = years[years.length - 1] - years[0] + 1;
  const cells = await panel.locator('[data-testid="timeline-cell"]').evaluateAll((nodes) =>
    nodes.map((node) => ({ year: node.dataset.year, crop: node.dataset.crop, width: node.getBoundingClientRect().width })),
  );
  equal(cells.length, span, `${fieldId}: timeline cells (years ${years[0]}${EN_DASH}${years[years.length - 1]})`);
  cells.forEach((cell, i) => {
    const year = String(years[0] + i);
    equal(cell.year, year, `${fieldId}: timeline cell ${i} year`);
    equal(cell.crop, detail.crop_history[year] ?? "", `${fieldId}: crop shown for ${year}`);
    check(cell.width >= 6, `${fieldId}: timeline cell for ${year} is only ${cell.width.toFixed(1)}px wide`);
  });
  equal(cells.filter((cell) => cell.crop === "").length, span - years.length, `${fieldId}: "no data" cells`);

  // tooltip on hover: "2014: winter wheat"
  const probe = Math.floor(cells.length / 2);
  await panel.locator('[data-testid="timeline-cell"]').nth(probe).hover();
  equal(await text(page, "timeline-tip"), `${cells[probe].year}: ${cells[probe].crop || "no data"}`, `${fieldId}: timeline tooltip`);
  await page.mouse.move(2, 2);

  // legend: only the crops present, with their counts
  const counts = {};
  for (const crop of Object.values(detail.crop_history)) counts[crop] = (counts[crop] ?? 0) + 1;
  const legend = (await panel.locator('[data-testid="legend"] li').allTextContents()).map((s) => s.replace(/\s+/g, " ").trim());
  const expectedLegend = Object.entries(counts)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([crop, n]) => `${titleCase(crop)}${n} ${n === 1 ? "year" : "years"}`);
  if (span > years.length) expectedLegend.push(`No data${span - years.length} ${span - years.length === 1 ? "year" : "years"}`);
  equal(legend.join(" | "), expectedLegend.join(" | "), `${fieldId}: legend`);

  // stats and soil
  equal(await text(page, "stat-distinct"), String(detail.stats.distinct_crop_count), `${fieldId}: distinct crops`);
  equal(await text(page, "stat-streak"), expectedStreak(detail.stats.longest_streak), `${fieldId}: longest streak`);
  const corn = await text(page, "stat-corn");
  check(corn.startsWith(percent(detail.stats.pct_years_corn)), `${fieldId}: years in corn reads "${corn}", expected ${percent(detail.stats.pct_years_corn)}`);
  for (const [testId, value, name] of [
    ["stat-om", detail.organic_matter_pct, "organic matter"],
    ["stat-slope", detail.slope_pct, "slope"],
  ]) {
    const shown = await text(page, testId);
    if (value === null) equal(shown, "No data", `${fieldId}: ${name}`);
    else {
      check(shown.startsWith(percent(value)), `${fieldId}: ${name} reads "${shown}", expected ${percent(value)}`);
      check(shown.length > percent(value).length, `${fieldId}: ${name} has no qualitative tag ("${shown}")`);
    }
  }

  // plans: exactly 3 cards, 4 crops each, a reason, values as served, best first
  equal(plans.plans.length, 3, `${fieldId}: plans returned by the API`);
  const cards = panel.locator('[data-testid="plan-card"]');
  equal(await cards.count(), 3, `${fieldId}: plan cards`);
  const overalls = [];
  for (let i = 0; i < 3; i++) {
    const card = cards.nth(i);
    const plan = plans.plans[i];
    const chips = (await card.locator('[data-testid="plan-chip"]').allTextContents()).map((s) => s.trim());
    equal(chips.length, 4, `${fieldId}: plan ${i + 1} crops`);
    equal(chips.join(" > "), plan.rotation.map(titleCase).join(" > "), `${fieldId}: plan ${i + 1} rotation`);
    const reason = ((await card.locator('[data-testid="plan-reason"]').textContent()) ?? "").trim();
    check(reason.length > 0, `${fieldId}: plan ${i + 1} has an empty reason`);
    equal(reason, plan.reason.trim(), `${fieldId}: plan ${i + 1} reason`);
    equal(((await card.locator('[data-testid="plan-name"]').textContent()) ?? "").trim(), plan.name, `${fieldId}: plan ${i + 1} name`);
    const overall = ((await card.locator('[data-testid="plan-overall"]').textContent()) ?? "").trim();
    equal(overall, score(plan.scores.overall), `${fieldId}: plan ${i + 1} overall score`);
    const shownScores = (await card.locator(".score-value").allTextContents()).map((s) => s.trim());
    equal(shownScores.join(" "), ["soil", "water", "money"].map((k) => score(plan.scores[k])).join(" "), `${fieldId}: plan ${i + 1} soil / water / money values`);
    overalls.push(Number(overall));
    const meters = await card.locator('[role="meter"]').evaluateAll((nodes) =>
      nodes.map((node) => `${node.getAttribute("aria-label")}=${Number(node.getAttribute("aria-valuenow")).toFixed(2)}`),
    );
    equal(
      meters.join(" "),
      ["soil", "water", "money"].map((k) => `${titleCase(k)} score=${Math.min(1, Math.max(0, plan.scores[k])).toFixed(2)}`).join(" "),
      `${fieldId}: plan ${i + 1} score bars`,
    );
  }
  check(overalls.every((v, i) => i === 0 || overalls[i - 1] >= v), `${fieldId}: plans are not ranked best first (${overalls.join(", ")})`);
  check((await cards.first().textContent()).includes("Recommended"), `${fieldId}: the top plan is not marked as the recommendation`);

  // satellite section: matches the API, and its year columns sit exactly under the timeline cells
  await checkSatellite(page, fieldId, detail, satellite, cells.length);
  await checkSoil(page, fieldId, detail, soil, cells.length);

  // nothing in the panel may spill sideways, and the page itself must never scroll
  const overflow = await panel.evaluate((node) => {
    const body = node.querySelector(".panel-body");
    const root = document.documentElement;
    return {
      panel: node.scrollWidth - node.clientWidth,
      body: body ? body.scrollWidth - body.clientWidth : 0,
      pageX: root.scrollWidth - window.innerWidth,
      pageY: root.scrollHeight - window.innerHeight,
    };
  });
  check(overflow.panel <= 0 && overflow.body <= 0, `${fieldId}: panel content overflows horizontally (${JSON.stringify(overflow)})`);
  check(overflow.pageX <= 0 && overflow.pageY <= 0, `${fieldId}: the page itself scrolls (${JSON.stringify(overflow)})`);
}

/** The satellite section against GET /field/{id}/satellite (its state is independent of the panel's). */
async function checkSatellite(page, fieldId, detail, satellite, spanCells) {
  const panel = page.locator('[data-testid="panel"]');
  const section = panel.locator('[data-testid="satellite"]');
  await page.waitForSelector('[data-testid="satellite"]:not([data-state="loading"])', { timeout: TIMEOUT });
  equal(await section.getAttribute("data-state"), "ready", `${fieldId}: satellite section state`);
  equal(await section.getAttribute("data-available"), String(satellite.available), `${fieldId}: satellite available`);
  // directly under the crop history section
  const order = await panel.evaluate((node) => [...node.querySelectorAll("section.section h3")].map((h) => h.textContent));
  equal(order.slice(0, 2).join(" > "), "Crop history > Satellite", `${fieldId}: satellite section sits under the crop history`);
  if (!satellite.available) {
    const message = { not_built: "Satellite data has not been built yet (make satellite).", no_data_for_field: "No satellite data for this field." }[satellite.reason];
    equal(await text(page, "sat-unavailable"), message, `${fieldId}: unavailable message`);
    equal(await section.locator('[data-testid="sat-peak-col"]').count(), 0, `${fieldId}: bars shown although satellite data is unavailable`);
    return;
  }
  const first = Math.min(...Object.keys(detail.crop_history).map(Number));
  const inRange = satellite.years.filter((row) => row.year >= first && row.year < first + spanCells);
  for (const [rowId, key] of [["sat-peak-col", "ndvi_peak"], ["sat-cover-col", "cover_frac"]]) {
    const cols = await section.locator(`[data-testid="${rowId}"]`).evaluateAll((nodes) =>
      nodes.map((n) => ({ year: Number(n.dataset.year), state: n.dataset.state, x: n.getBoundingClientRect().x, w: n.getBoundingClientRect().width, h: n.firstElementChild ? n.firstElementChild.getBoundingClientRect().height : 0, box: n.getBoundingClientRect().height })),
    );
    equal(cols.length, spanCells, `${fieldId}: ${key} columns (one per timeline year)`);
    equal(cols.filter((c) => c.state === "value").length, inRange.filter((r) => r[key] !== null).length, `${fieldId}: ${key} bars vs API`);
    equal(cols.filter((c) => c.state === "null").length, inRange.filter((r) => r[key] === null).length, `${fieldId}: ${key} "no data" marks vs API`);
    equal(cols.filter((c) => c.state === "gap").length, spanCells - inRange.length, `${fieldId}: ${key} gaps vs API`);
    const timeline = await panel.locator('[data-testid="timeline-cell"]').evaluateAll((nodes) => nodes.map((n) => ({ x: n.getBoundingClientRect().x, w: n.getBoundingClientRect().width })));
    cols.forEach((c, i) => check(Math.abs(c.x - timeline[i].x) <= 1 && Math.abs(c.w - timeline[i].w) <= 1, `${fieldId}: ${key} column ${c.year} is not under its timeline cell (${c.x.toFixed(1)}/${c.w.toFixed(1)} vs ${timeline[i].x.toFixed(1)}/${timeline[i].w.toFixed(1)})`));
    for (const c of cols) {
      const row = inRange.find((r) => r.year === c.year);
      if (c.state === "value") check(Math.abs(c.h - row[key] * c.box) <= 1.5, `${fieldId}: ${key} bar height for ${c.year} is ${c.h.toFixed(1)}px of ${c.box}px, API says ${row[key]}`);
    }
  }
  const summary = await text(page, "sat-summary");
  const s = satellite.summary;
  if (s.ndvi_peak_mean !== null) check(summary.includes(s.ndvi_peak_mean.toFixed(2)), `${fieldId}: satellite summary "${summary}" lacks mean ${s.ndvi_peak_mean}`);
  if (s.ndvi_peak_trend_per_decade !== null) {
    const word = Math.abs(s.ndvi_peak_trend_per_decade) < 0.02 ? "steady" : s.ndvi_peak_trend_per_decade > 0 ? "rising" : "falling";
    check(summary.includes(word), `${fieldId}: satellite summary "${summary}" should say ${word}`);
  } else check(!/trend/.test(summary), `${fieldId}: satellite summary shows a trend without 5 years of data`);
  check((await section.textContent()).includes("From Landsat satellite images, 30 m."), `${fieldId}: satellite note missing`);
  // tooltip on a year that has data
  const row = inRange.find((r) => r.ndvi_peak !== null && r.cover_frac !== null);
  if (row) {
    await section.locator(`[data-testid="sat-peak-col"][data-year="${row.year}"]`).hover();
    const tip = await text(page, "sat-tip");
    const crop = detail.crop_history[String(row.year)] ?? "no crop on record";
    equal(tip, `${row.year} · ${crop} · peak greenness ${row.ndvi_peak.toFixed(2)} · green ${Math.round(row.cover_frac * 100)}% of clear views · ${Math.round(row.n_obs)} ${Math.round(row.n_obs) === 1 ? "view" : "views"}`, `${fieldId}: satellite tooltip`);
    await page.mouse.move(2, 2);
  }
}

const oneDec = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const OM_SCALE_MAX = 5;
const confWord = (c) => (c < 0.34 ? "low" : c < 0.67 ? "medium" : "high");

/** The "Soil over time" section against GET /field/{id}/soil. */
async function checkSoil(page, fieldId, detail, soil, spanCells) {
  const panel = page.locator('[data-testid="panel"]');
  const section = panel.locator('[data-testid="soil-timeline"]');
  await page.waitForSelector('[data-testid="soil-timeline"]:not([data-state="loading"])', { timeout: TIMEOUT });
  equal(await section.getAttribute("data-state"), "ready", `${fieldId}: soil section state`);
  equal(await section.getAttribute("data-available"), String(soil.available), `${fieldId}: soil available`);
  const order = await panel.evaluate((node) => [...node.querySelectorAll("section.section h3")].map((h) => h.textContent));
  equal(order.slice(0, 3).join(" > "), "Crop history > Satellite > Soil over time", `${fieldId}: soil section sits directly under the satellite section`);
  if (!soil.available) {
    const message = { not_built: "Soil timeline has not been built yet (make soil-health).", no_data_for_field: "No soil timeline for this field." }[soil.reason];
    equal(await text(page, "soil-unavailable"), message, `${fieldId}: soil unavailable message`);
    equal(await section.locator('[data-testid="soil-om-col"]').count(), 0, `${fieldId}: soil marks shown although unavailable`);
    return;
  }
  const first = Math.min(...Object.keys(detail.crop_history).map(Number));
  const inRange = soil.years.filter((row) => row.year >= first && row.year < first + spanCells);
  const timeline = await panel.locator('[data-testid="timeline-cell"]').evaluateAll((nodes) => nodes.map((n) => ({ x: n.getBoundingClientRect().x, w: n.getBoundingClientRect().width })));
  const specs = [["soil-om-col", "om_pred"], ["soil-bare-col", "bare_days"]];
  for (const [rowId, key] of specs) {
    const cols = await section.locator(`[data-testid="${rowId}"]`).evaluateAll((nodes) =>
      nodes.map((n) => {
        const r = n.getBoundingClientRect();
        const kid = n.firstElementChild ? n.firstElementChild.getBoundingClientRect() : null;
        return { year: Number(n.dataset.year), state: n.dataset.state, x: r.x, w: r.width, top: r.top, bottom: r.bottom, kid: kid && { cy: kid.top + kid.height / 2, h: kid.height } };
      }),
    );
    equal(cols.length, spanCells, `${fieldId}: ${key} columns (one per timeline year)`);
    equal(cols.filter((c) => c.state === "value").length, inRange.filter((r) => r[key] !== null).length, `${fieldId}: ${key} marks vs API`);
    equal(cols.filter((c) => c.state === "null").length, inRange.filter((r) => r[key] === null).length, `${fieldId}: ${key} empty years vs API`);
    equal(cols.filter((c) => c.state === "gap").length, spanCells - inRange.length, `${fieldId}: ${key} gaps vs API`);
    cols.forEach((c, i) => check(Math.abs(c.x - timeline[i].x) <= 1 && Math.abs(c.w - timeline[i].w) <= 1, `${fieldId}: ${key} column ${c.year} is not under its timeline cell`));
    for (const c of cols.filter((c) => c.state === "value")) {
      const row = inRange.find((r) => r.year === c.year);
      const box = c.bottom - c.top;
      if (key === "bare_days") check(Math.abs(c.kid.h - (row.bare_days / 366) * box) <= 1.5, `${fieldId}: bare bar height for ${c.year} is ${c.kid.h.toFixed(1)}px of ${box}px, API says ${row.bare_days}`);
      else check(Math.abs((c.bottom - c.kid.cy) - (Math.min(OM_SCALE_MAX, row.om_pred) / OM_SCALE_MAX) * box) <= 1.5, `${fieldId}: organic-matter mark for ${c.year} is at the wrong height (API says ${row.om_pred})`);
    }
  }
  // reference line, band, tags, order
  const ref = section.locator('[data-testid="soil-ref"]');
  equal(await ref.count(), soil.ssurgo_om_pct === null ? 0 : 1, `${fieldId}: soil-survey line present iff the field has a static value`);
  const legend = (await text(page, "soil-legend"));
  if (soil.ssurgo_om_pct !== null) check(legend.includes("soil survey") && legend.includes(oneDec.format(soil.ssurgo_om_pct)), `${fieldId}: legend "${legend}" lacks the soil survey value`);
  const showBand = soil.om_noise_sd !== null && soil.om_noise_sd > 0 && soil.summary.om_pred_mean !== null;
  equal(await section.locator('[data-testid="soil-band"]').count(), showBand ? 1 : 0, `${fieldId}: noise band present`);
  if (showBand) check(legend.includes("year-to-year noise"), `${fieldId}: legend "${legend}" does not label the band`);
  const blocks = await section.locator("[data-signal][data-testid$='-block']").evaluateAll((nodes) => nodes.map((n) => n.dataset.signal));
  const firstSignal = soil.recommended === "bare_days" ? "bare_days" : "om_pred";
  equal(blocks[0], firstSignal, `${fieldId}: which soil signal comes first`);
  const tags = await section.locator('[data-testid="soil-tag"]').evaluateAll((nodes) => nodes.map((n) => `${n.dataset.signal}=${n.textContent.trim()}`));
  if (soil.recommended === null) equal(tags.length, 0, `${fieldId}: tags without a recommendation`);
  else {
    const other = soil.recommended === "om_pred" ? "bare_days" : "om_pred";
    equal(tags.join("|"), [`${soil.recommended}=more reliable`, `${other}=noisy - use with caution`].join("|"), `${fieldId}: reliability tags`);
  }
  // summary and verdict
  const summary = await text(page, "soil-summary");
  const sm = soil.summary;
  if (sm.om_pred_mean !== null) check(summary.includes(`${oneDec.format(sm.om_pred_mean)}%`), `${fieldId}: soil summary "${summary}" lacks the organic-matter mean`);
  if (sm.bare_days_mean !== null) check(summary.includes(String(Math.round(sm.bare_days_mean))), `${fieldId}: soil summary "${summary}" lacks mean bare days`);
  if (sm.bare_days_trend_per_decade !== null) {
    const word = Math.abs(sm.bare_days_trend_per_decade) < 5 ? "steady" : sm.bare_days_trend_per_decade > 0 ? "rising" : "falling";
    check(summary.includes(word), `${fieldId}: soil summary "${summary}" should say ${word}`);
  } else check(!/trend/.test(summary), `${fieldId}: soil summary shows a trend without 5 values`);
  if (soil.verdict) equal(await text(page, "soil-verdict"), soil.verdict.trim(), `${fieldId}: verdict text`);
  else equal(await section.locator('[data-testid="soil-verdict"]').count(), 0, `${fieldId}: verdict shown without one`);
  // tooltips
  const omRow = inRange.find((r) => r.om_pred !== null);
  if (omRow) {
    await section.locator(`[data-testid="soil-om-col"][data-year="${omRow.year}"]`).hover();
    equal(await text(page, "soil-tip"), `${omRow.year} · ${oneDec.format(omRow.om_pred)}% · ${confWord(omRow.confidence)} confidence · ${Math.round(omRow.n_obs)} ${Math.round(omRow.n_obs) === 1 ? "bare view" : "bare views"}`, `${fieldId}: organic-matter tooltip`);
  }
  const bareRow = inRange.find((r) => r.bare_days !== null);
  if (bareRow) {
    await section.locator(`[data-testid="soil-bare-col"][data-year="${bareRow.year}"]`).hover();
    const crop = detail.crop_history[String(bareRow.year)] ?? "no crop on record";
    equal(await text(page, "soil-tip"), `${bareRow.year} · ${crop} · ${Math.round(bareRow.bare_days)} ${Math.round(bareRow.bare_days) === 1 ? "day" : "days"} bare`, `${fieldId}: bare-days tooltip`);
  }
  await page.mouse.move(2, 2);
}

/** Ids of the polygons currently drawn with the given outline colour. */
const outlined = (page, color) =>
  page.evaluate((color) => {
    const ids = [];
    window.__map.eachLayer((layer) => {
      if (layer.feature && layer.options && layer.options.color === color) ids.push(String(layer.feature.id));
    });
    return ids;
  }, color);
const selectedOnMap = (page) => outlined(page, SELECTED_OUTLINE);

/** Move the real mouse onto a point and report what the map shows there. */
async function hoverAt(page, target) {
  await page.mouse.move(target.x - 2, target.y - 2);
  await page.waitForTimeout(60); // Leaflet looks at the pointer at most every 32 ms
  await page.mouse.move(target.x, target.y);
  await page.waitForTimeout(60);
  return {
    cursor: await page.evaluate(() => getComputedStyle(document.querySelector(".leaflet-container canvas")).cursor),
    hovered: await outlined(page, HOVER_OUTLINE),
  };
}

// ------------------------------------------------------------------------------------------
// Responsiveness probe
// ------------------------------------------------------------------------------------------

const stats = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] ?? 0;
  return { median: at(0.5), p95: at(0.95), max: sorted[sorted.length - 1] ?? 0, n: sorted.length };
};
const describe = (s) => `median ${s.median.toFixed(1)} / p95 ${s.p95.toFixed(1)} / max ${s.max.toFixed(1)} ms`;

async function recordFrames(page, action) {
  await page.evaluate(() => {
    const frames = [];
    let last = performance.now();
    window.__e2eFrames = { frames, running: true };
    const tick = (now) => {
      frames.push(now - last);
      last = now;
      if (window.__e2eFrames.running) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await action();
  return page.evaluate(() => {
    window.__e2eFrames.running = false;
    return window.__e2eFrames.frames.slice(1);
  });
}

async function measureResponsiveness(page, viewport, corners) {
  // start from the widest view, where the most polygons are on screen
  await page.evaluate(
    (corners) =>
      new Promise((resolve) => {
        window.__map.fitBounds(corners, { animate: false });
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      }),
    corners,
  );
  const cx = Math.round(viewport.width / 2);
  const cy = Math.round(viewport.height / 2);

  // 1. drag the map with the real mouse (Leaflet moves the canvas, then redraws on release)
  const drag = await recordFrames(page, async () => {
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx - 320, cy - 140, { steps: 40 });
    await page.mouse.move(cx + 120, cy + 100, { steps: 40 });
    await page.mouse.up();
    await page.waitForTimeout(400); // inertia + the redraw after release
  });

  // 2. sweep the pointer across the fields (hit-testing every polygon + hover restyle)
  const hover = await recordFrames(page, async () => {
    await page.mouse.move(cx - 300, cy - 200);
    await page.mouse.move(cx + 300, cy + 200, { steps: 60 });
    await page.mouse.move(cx - 300, cy + 200, { steps: 60 });
  });
  await page.mouse.move(2, 2);

  // 3. full redraws: every polygon is re-projected and repainted after a pan or zoom step
  const redraw = await page.evaluate(
    () =>
      new Promise((resolve) => {
        const map = window.__map;
        const steps = [
          ["pan", () => map.panBy([260, 0], { animate: false })],
          ["pan", () => map.panBy([0, 200], { animate: false })],
          ["pan", () => map.panBy([-260, -200], { animate: false })],
          ["zoom", () => map.setZoom(map.getZoom() + 1, { animate: false })],
          ["zoom", () => map.setZoom(map.getZoom() + 1, { animate: false })],
          ["zoom", () => map.setZoom(map.getZoom() - 1, { animate: false })],
          ["zoom", () => map.setZoom(map.getZoom() - 1, { animate: false })],
        ];
        // `script`: time the step blocks the main thread (re-project + issue the canvas draw calls).
        // `frame`: the longest gap between frames over the next three, i.e. including the paint.
        const out = { pan: { script: [], frame: [] }, zoom: { script: [], frame: [] } };
        const next = (i) => {
          if (i === steps.length) return resolve(out);
          const [kind, run] = steps[i];
          const started = performance.now();
          run();
          out[kind].script.push(performance.now() - started);
          let last = started;
          let worst = 0;
          let frames = 0;
          const tick = (now) => {
            const stamp = Math.max(now, last); // rAF timestamps are frame-start times and can trail `started`
            worst = Math.max(worst, stamp - last);
            last = stamp;
            if (++frames < 3) return requestAnimationFrame(tick);
            out[kind].frame.push(worst);
            next(i + 1);
          };
          requestAnimationFrame(tick);
        };
        next(0);
      }),
  );

  return {
    drag: stats(drag),
    hover: stats(hover),
    pan: { script: stats(redraw.pan.script), frame: stats(redraw.pan.frame) },
    zoom: { script: stats(redraw.zoom.script), frame: stats(redraw.zoom.frame) },
  };
}

// ------------------------------------------------------------------------------------------
// The run
// ------------------------------------------------------------------------------------------

const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

/** Hover, then really click the field at `target`; returns how long the panel took to fill. */
async function clickField(page, id, target) {
  const hover = await hoverAt(page, target);
  equal(hover.cursor, "pointer", `${id}: cursor over the field`);
  const expectedHover = (await selectedOnMap(page)).includes(id) ? "" : id; // a selected field keeps its selected look
  equal(hover.hovered.join(","), expectedHover, `${id}: polygon highlighted on hover`);
  await armStopwatch(page, id);
  await page.mouse.click(target.x, target.y);
  await waitForPanel(page, id);
  return readStopwatch(page);
}

async function run(browser) {
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: 1 });
  const page = await context.newPage();
  page.setDefaultTimeout(TIMEOUT);
  const monitor = watch(page, "desktop");

  // --- load ---------------------------------------------------------------------------------
  let load;
  try {
    load = await openApp(page, `${WEB_URL}/`);
  } catch (error) {
    if (error instanceof Failure) throw error;
    throw new Failure(`could not open ${WEB_URL}/ — is the web server running? (${error.message.split("\n")[0]})`);
  }
  const apiBase = await page.getAttribute('[data-testid="app"]', "data-api-base");
  check(typeof apiBase === "string" && apiBase.length > 0, "the app does not expose its API base (data-api-base)");
  check(await page.evaluate(() => typeof window.__map?.latLngToContainerPoint === "function"), "window.__map is not a Leaflet map");
  const apiPrefix = new URL(apiBase, `${WEB_URL}/`).href;

  // every API call the test makes goes through the page, with the page's own API base
  const fetchJson = (from, pathname) =>
    from.evaluate(async (url) => {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`${url} answered ${response.status}`);
      return response.json();
    }, apiBase + pathname);
  const fieldData = (from, id) => Promise.all([
    fetchJson(from, `/field/${id}`),
    fetchJson(from, `/field/${id}/plans`),
    fetchJson(from, `/field/${id}/satellite`),
    fetchJson(from, `/field/${id}/soil`),
  ]);

  const [health, collection] = await Promise.all([fetchJson(page, "/health"), fetchJson(page, "/fields")]);
  const features = collection.features;
  check(Array.isArray(features) && features.length > 0, "GET /fields returned no features");
  const drawn = Number(await page.getAttribute('[data-testid="app"]', "data-fields-drawn"));
  equal(drawn, features.length, "polygons drawn on the map vs features served");
  equal(health.field_count, features.length, "/health field_count vs features served");

  log(`smoke test: ${WEB_URL} (API base ${apiBase}) — ${features.length} fields from ${health.source}${health.is_fake ? " (demo data)" : ""}`);
  const sinceNav = load.sinceNavigation ?? load.wall;
  log(
    `  ✓ fields rendered ${ms(sinceNav)} after navigation` +
      (load.fetchMs !== null ? ` (GET /fields took ${ms(load.fetchMs)}${load.fetchKb ? `, ${Math.round(load.fetchKb)} kB` : ""})` : ""),
  );

  const badge = await text(page, "badge");
  const count = new Intl.NumberFormat("en-US").format(health.field_count);
  const noun = health.field_count === 1 ? "field" : "fields";
  equal(badge, health.is_fake ? `Demo data — ${count} fake ${noun}` : `${count} ${noun} · Finney County, KS`, "data badge");
  equal(await text(page, "hint"), "Click a field to see its crop history, soil and rotation plans", "empty-selection hint");
  equal(await page.locator('[data-testid="panel"]').count(), 0, "panels open before any click");

  // the whole data set must be in view after the initial fit
  const boxes = features.map((feature) => bboxOf(feature.geometry.coordinates));
  const all = collection.bbox ?? [
    Math.min(...boxes.map((b) => b.west)),
    Math.min(...boxes.map((b) => b.south)),
    Math.max(...boxes.map((b) => b.east)),
    Math.max(...boxes.map((b) => b.north)),
  ];
  const corners = [[all[1], all[0]], [all[3], all[2]]];
  const overview = await page.evaluate((corners) => ({ fits: window.__map.getBounds().contains(corners), zoom: window.__map.getZoom() }), corners);
  check(overview.fits, "the initial view does not contain the fields' bounding box");

  await settleTiles(page);
  log(`  ✓ badge "${badge}", hint shown, all fields in view at zoom ${overview.zoom} → ${await shot(page, "01-map.png")}`);

  // --- click fields ---------------------------------------------------------------------------
  const wanted = Math.min(N_FIELDS, features.length);
  const picks = [];
  for (let i = 0; i < wanted; i++) {
    // spread across the list; if a field has no point of its own (fully covered), take the next
    let index = Math.floor(((i + 0.5) * features.length) / wanted);
    for (let tries = 0; tries < features.length; tries++, index = (index + 1) % features.length) {
      const feature = features[index];
      if (picks.some((pick) => pick.feature === feature)) continue;
      const point = clickablePoint(feature, features, boxes);
      if (point) {
        picks.push({ feature, point, id: String(feature.id) });
        break;
      }
      warnings.push(`${feature.id} is entirely covered by other fields; skipped`);
    }
  }
  equal(picks.length, wanted, "fields with a clickable interior point");

  // The first field is clicked from the overview, as a visitor would (small target, neighbours
  // a few pixels away); the others at closer zooms and away from the middle of the map.
  const zoomPlan = [null, 14, 13, 15, 12, 16];
  const offsets = [[0, 0], [130, -70], [-170, 90], [-90, -130], [160, 110], [60, -150]];
  const clickTimes = [];

  for (let i = 0; i < picks.length; i++) {
    const { point, id } = picks[i];
    const [detail, plans, satellite, soil] = await fieldData(page, id);

    const planned = zoomPlan[i % zoomPlan.length];
    const zoom = planned === null ? null : Math.min(18, Math.max(planned, zoomForClearance(point.clearance, point.lat, 6)));
    const target = await aimAt(page, point, zoom, offsets[i % offsets.length]);
    const acrossPx = (2 * point.clearance) / ((156_543.03 * Math.cos((point.lat * Math.PI) / 180)) / 2 ** target.zoom);
    const elapsed = await clickField(page, id, target);
    if (elapsed !== null) clickTimes.push(elapsed);

    await checkPanel(page, id, detail, plans, satellite, soil);
    equal(await page.evaluate(() => window.location.hash), `#${id}`, `${id}: URL hash after the click`);
    equal((await selectedOnMap(page)).join(","), id, `${id}: polygon highlighted as selected`);
    equal(await page.locator('[data-testid="hint"]').count(), 0, `${id}: hint still shown with a field selected`);
    const visible = await page.evaluate(({ lat, lon }) => window.__map.getBounds().contains([lat, lon]), point);
    check(visible, `${id}: the clicked field is no longer in view after the panel opened`);

    await settleTiles(page, 4000);
    const years = Object.keys(detail.crop_history).length;
    log(
      `  ✓ ${id}: clicked at (${Math.round(target.x)}, ${Math.round(target.y)}), zoom ${target.zoom}, field ~${Math.round(acrossPx)} px across → ` +
        `panel populated in ${elapsed === null ? "?" : ms(elapsed)}; ${years} years, ${detail.stats.distinct_crop_count} crops, ` +
        `OM ${detail.organic_matter_pct ?? "null"}, slope ${detail.slope_pct ?? "null"}, 3 plans → ` +
        (await shot(page, `${String(i + 2).padStart(2, "0")}-field-${id}.png`)),
    );
  }

  // --- Esc ------------------------------------------------------------------------------------
  await page.keyboard.press("Escape");
  await page.waitForSelector('[data-testid="panel"]', { state: "detached", timeout: TIMEOUT });
  equal(await page.evaluate(() => window.location.hash), "", "URL hash after Esc");
  equal((await selectedOnMap(page)).length, 0, "polygons still highlighted after Esc");
  equal(await page.locator('[data-testid="hint"]').count(), 1, "hint after Esc");
  log("  ✓ Esc closes the panel, clears the hash and the highlight");

  // --- deep link ------------------------------------------------------------------------------
  const linked = picks[0];
  await page.goto(`${WEB_URL}/#${linked.id}`, { waitUntil: "domcontentloaded" });
  await page.reload({ waitUntil: "domcontentloaded" }); // a real cold load with the hash present
  await page.waitForSelector('[data-testid="app"][data-fields-drawn]', { timeout: TIMEOUT });
  await waitForPanel(page, linked.id);
  await twoFrames(page);
  equal((await selectedOnMap(page)).join(","), linked.id, "linked field highlighted after reload");
  const restored = await page.evaluate(
    ({ lat, lon }) => ({ inView: window.__map.getBounds().contains([lat, lon]), zoom: window.__map.getZoom() }),
    linked.point,
  );
  check(restored.inView, `#${linked.id}: the linked field is not in view after reload`);
  check(restored.zoom > overview.zoom || restored.zoom >= 15, `#${linked.id}: the map did not zoom in to the linked field (zoom ${restored.zoom}, overview ${overview.zoom})`);
  await checkPanel(page, linked.id, ...(await fieldData(page, linked.id)));
  log(`  ✓ #${linked.id} restored on a cold load: panel populated, field highlighted, zoom ${restored.zoom}`);

  // --- stale answers --------------------------------------------------------------------------
  // Click A then B at once while A's answers are held back: the panel must show B, and must
  // still show B after A's late answers would have arrived.
  if (picks.length >= 2) {
    const a = picks[picks.length - 1];
    const b = picks[picks.length - 2];
    const slow = (url) => url.href.startsWith(apiPrefix) && url.pathname.includes(`/field/${a.id}`);
    await page.route(slow, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700));
      await route.continue().catch(() => {}); // already cancelled by the app: fine
    });
    const spots = await page.evaluate(
      ({ a, b }) =>
        new Promise((resolve) => {
          const map = window.__map;
          map.fitBounds([[a.lat, a.lon], [b.lat, b.lon]], { animate: false, padding: [180, 180], maxZoom: 15 });
          requestAnimationFrame(() =>
            requestAnimationFrame(() => {
              const frame = map.getContainer().getBoundingClientRect();
              const at = (p) => {
                const q = map.latLngToContainerPoint([p.lat, p.lon]);
                const x = frame.left + q.x;
                const y = frame.top + q.y;
                return { x, y, onMap: document.elementFromPoint(x, y) instanceof HTMLCanvasElement };
              };
              resolve({ a: at(a), b: at(b) });
            }),
          );
        }),
      { a: a.point, b: b.point },
    );
    if (spots.a.onMap && spots.b.onMap) {
      const expected = await fieldData(page, b.id);
      await page.mouse.click(spots.a.x, spots.a.y);
      await page.mouse.click(spots.b.x, spots.b.y);
      await waitForPanel(page, b.id);
      await checkPanel(page, b.id, ...expected);
      await page.waitForTimeout(1000);
      await checkPanel(page, b.id, ...expected);
      equal((await selectedOnMap(page)).join(","), b.id, "polygon highlighted after two quick clicks");
      log(`  ✓ ${a.id} then ${b.id} clicked back to back with ${a.id}'s answers delayed: panel shows ${b.id} and stays on it`);
    } else {
      warnings.push("stale-answer check skipped: could not get both fields on screen at once");
    }
    await page.unroute(slow);
  }

  // --- responsiveness -------------------------------------------------------------------------
  await page.keyboard.press("Escape");
  await page.waitForSelector('[data-testid="panel"]', { state: "detached", timeout: TIMEOUT });
  const perf = await measureResponsiveness(page, DESKTOP, corners);
  monitor.assertClean(apiPrefix);
  await context.close();

  // --- mobile ---------------------------------------------------------------------------------
  const mobileContext = await browser.newContext({ viewport: PHONE, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const mobile = await mobileContext.newPage();
  mobile.setDefaultTimeout(TIMEOUT);
  const mobileMonitor = watch(mobile, "mobile");
  await openApp(mobile, `${WEB_URL}/`);
  const pick = picks[Math.min(1, picks.length - 1)];
  const mobileZoom = Math.max(13, zoomForClearance(pick.point.clearance, pick.point.lat, 10));
  const mobileTarget = await aimAt(mobile, pick.point, mobileZoom, [0, 120]);
  await mobile.touchscreen.tap(mobileTarget.x, mobileTarget.y);
  await waitForPanel(mobile, pick.id);
  await checkPanel(mobile, pick.id, ...(await fieldData(mobile, pick.id)));
  const layout = await mobile.evaluate(({ lat, lon }) => {
    const panel = document.querySelector('[data-testid="panel"]').getBoundingClientRect();
    const map = document.querySelector(".leaflet-container").getBoundingClientRect();
    return {
      panel: { top: panel.top, bottom: panel.bottom, width: panel.width },
      map: { bottom: map.bottom, height: map.height },
      page: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      fieldInView: window.__map.getBounds().contains([lat, lon]),
    };
  }, pick.point);
  equal(layout.panel.width, PHONE.width, "mobile: sheet width");
  equal(Math.round(layout.panel.bottom), PHONE.height, "mobile: sheet is anchored to the bottom edge");
  check(layout.panel.top >= 200 && Math.abs(layout.panel.top - layout.map.bottom) <= 1, `mobile: the sheet should sit directly under the map (${JSON.stringify(layout)})`);
  check(layout.map.height >= 240, `mobile: only ${layout.map.height}px of map left above the sheet`);
  check(layout.page.width <= PHONE.width && layout.page.height <= PHONE.height, `mobile: the page itself scrolls (${JSON.stringify(layout.page)})`);
  check(layout.fieldInView, "mobile: the tapped field is hidden once the sheet opens");
  equal((await selectedOnMap(mobile)).join(","), pick.id, "mobile: polygon highlighted as selected");
  await settleTiles(mobile, 4000);
  const mobileShot = await shot(mobile, `mobile-${pick.id}.png`);
  await mobile.locator('[data-testid="plans"]').scrollIntoViewIfNeeded();
  await mobile.waitForTimeout(150);
  const mobilePlansShot = await shot(mobile, `mobile-${pick.id}-plans.png`);
  mobileMonitor.assertClean(apiPrefix);
  await mobileContext.close();
  log(
    `  ✓ mobile ${PHONE.width}×${PHONE.height}: tap selects ${pick.id}; bottom sheet ${Math.round(PHONE.height - layout.panel.top)} px ` +
      `under a ${Math.round(layout.map.height)} px map → ${mobileShot}, ${mobilePlansShot}`,
  );

  // --- timings --------------------------------------------------------------------------------
  log("");
  log("timings (headless Chromium, 1440×900)");
  log(`  navigation → fields rendered   ${ms(sinceNav)} (${features.length} polygons)`);
  if (clickTimes.length) {
    const s = stats(clickTimes);
    log(`  click → panel fully populated  ${describe(s)} (${s.n} clicks)`);
  }
  log(`  frame time while dragging      ${describe(perf.drag)} (${perf.drag.n} frames)`);
  log(`  frame time while hovering      ${describe(perf.hover)} (${perf.hover.n} frames)`);
  log(`  redraw after a pan step        script ${describe(perf.pan.script)}; longest frame ${perf.pan.frame.max.toFixed(1)} ms`);
  log(`  redraw after a zoom step       script ${describe(perf.zoom.script)}; longest frame ${perf.zoom.frame.max.toFixed(1)} ms`);
  if (perf.drag.p95 > 50 || perf.hover.p95 > 50) warnings.push("frame times above 50 ms while dragging or hovering — the map may feel sluggish");
  if (Math.max(perf.pan.frame.max, perf.zoom.frame.max) > 120) warnings.push("a full redraw took more than 120 ms — panning and zooming may stutter");
}

async function main() {
  await rm(SHOTS, { recursive: true, force: true });
  await mkdir(SHOTS, { recursive: true });
  let browser;
  try {
    browser = await chromium.launch();
  } catch (error) {
    console.error(`FAIL: could not start Chromium (${error.message.split("\n")[0]}). Run: npx playwright install chromium`);
    return 1;
  }
  try {
    await run(browser);
  } catch (error) {
    console.error("");
    console.error(`FAIL: ${error instanceof Failure ? error.message : (error.stack ?? error)}`);
    return 1;
  } finally {
    await browser.close();
    if (warnings.length) {
      console.log("");
      console.log(`warnings (${warnings.length})`);
      for (const warning of [...new Set(warnings)].slice(0, 12)) console.log(`  ! ${warning}`);
      if (new Set(warnings).size > 12) console.log(`  … and ${new Set(warnings).size - 12} more`);
    }
  }
  console.log("");
  console.log(`PASS: ${checks} checks`);
  return 0;
}

process.exitCode = await main();
