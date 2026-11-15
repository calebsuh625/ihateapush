// Performance measurements (Phase 4, item 8). Not part of `make e2e`.
//
//   npm run perf -- [--out ../data/logs/phase4/perf_before.txt] [--calls 30] [--skip-browser] [--skip-server]
//
// Needs the dev servers running (API :8010, web :5173). Reports
//   * server timings: median and 95th percentile (ms) over many calls for every endpoint,
//   * payload sizes (what travels over the wire, and decompressed),
//   * browser: time to first fields on screen, and frame times (median, 95th percentile, worst, share of
//     frames over 33 ms) while zooming and panning the county with each layer on and during the time-lapse.
// Frame times come from requestAnimationFrame gaps in headless Chromium (software drawing), so read them as
// a comparison between runs on the same machine, not as what a farmer's laptop would show.
import { writeFileSync } from "node:fs";
import { chromium } from "playwright";

const arg = (name, fallback) => {
  const at = process.argv.indexOf(`--${name}`);
  return at >= 0 ? process.argv[at + 1] : fallback;
};
const flag = (name) => process.argv.includes(`--${name}`);
const WEB_URL = (process.env.WEB_URL ?? "http://localhost:5173").replace(/\/+$/, "");
const API = (process.env.API_URL ?? "http://127.0.0.1:8010").replace(/\/+$/, "");
const OUT = arg("out", null);
const CALLS = Number.parseInt(arg("calls", "30"), 10) || 30;
const DRY = process.env.DRY_FIELD ?? "KS-013847";
const WET = process.env.IRRIGATED_FIELD ?? "KS-014376";

const lines = [];
const say = (line = "") => {
  console.log(line);
  lines.push(line);
};
const pct = (sorted, p) => sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1))];
const stats = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  return { n: sorted.length, median: pct(sorted, 50), p95: pct(sorted, 95), max: sorted[sorted.length - 1] };
};
const f1 = (n) => (n === undefined || Number.isNaN(n) ? "-" : n >= 100 ? String(Math.round(n)) : n.toFixed(1));
const pad = (s, n) => String(s).padEnd(n);
const kb = (n) => `${(n / 1000).toFixed(1)} kB`;

// ---------------------------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------------------------

async function timed(url, init) {
  const start = performance.now();
  const response = await fetch(url, init);
  const body = await response.arrayBuffer();
  return { ms: performance.now() - start, status: response.status, bytes: body.byteLength };
}

async function serverTimings() {
  const fieldIds = [DRY, WET, "KS-000005", "KS-007027", "KS-014011"];
  // `vary` changes a setting per call so a cache of identical questions cannot hide the real cost.
  const weight = (i) => `soil=${1 + (i % 5)}&water=${1 + ((i * 3) % 5)}&money=${1 + ((i * 7) % 5)}&risk=1`;
  const rotations = ["corn, soybeans", "corn, corn, wheat", "sorghum, wheat, fallow", "alfalfa, alfalfa, alfalfa, corn"];
  const cases = [
    ["GET /health", () => "/health"],
    ["GET /climate", () => "/climate"],
    ["GET /fields (no gzip)", () => ["/fields", { headers: { "Accept-Encoding": "identity" } }]],
    ["GET /fields (gzip)", () => ["/fields", { headers: { "Accept-Encoding": "gzip" } }]],
    ["GET /fields (If-None-Match)", () => ["/fields", { headers: { "If-None-Match": globalThis.__etag ?? "" } }]],
    ["GET /field/{id}", (i) => `/field/${fieldIds[i % 5]}`],
    ["GET /field/{id}/plans (same settings)", (i) => `/field/${fieldIds[i % 5]}/plans`],
    ["GET /field/{id}/plans (varied weights)", (i) => `/field/${fieldIds[i % 5]}/plans?${weight(i)}`],
    ["GET /field/{id}/score (varied)", (i) => `/field/${fieldIds[i % 5]}/score?rotation=${encodeURIComponent(rotations[i % 4])}&${weight(i)}`],
    ["GET /field/{id}/transition (varied)", (i) => `/field/${fieldIds[i % 5]}/transition?plan=${1 + (i % 3)}&${weight(i)}`],
    ["GET /field/{id}/satellite", (i) => `/field/${fieldIds[i % 5]}/satellite`],
    ["GET /field/{id}/soil", (i) => `/field/${fieldIds[i % 5]}/soil`],
    ["GET /field/{id}/neighbors", (i) => `/field/${fieldIds[i % 5]}/neighbors`],
    ["GET /field/{id}/twin", (i) => `/field/${fieldIds[i % 5]}/twin`],
    ["GET /effects", () => "/effects"],
    ["GET /groundwater", () => "/groundwater"],
    ["GET /layers", () => "/layers"],
    ["GET /layers/{name} (each of 4)", (i) => `/layers/${["bare_days", "erosion_risk", "rotation_change", "irrigation"][i % 4]}${i % 8 < 4 ? "" : "?year=2020"}`],
    ["GET /advisor/status", () => "/advisor/status"],
    [
      "POST /farm/plan (5 fields, varied)",
      (i) => [
        "/farm/plan",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ field_ids: fieldIds, weights: { soil: 1 + (i % 5), water: 1, money: 1 + ((i * 3) % 5), risk: 1 } }) },
      ],
    ],
  ];
  const first = await fetch(`${API}/fields`, { headers: { "Accept-Encoding": "gzip" } });
  globalThis.__etag = first.headers.get("etag") ?? "";
  await first.arrayBuffer();
  say(`## Server timings: ${CALLS} calls each, sequential, in milliseconds`);
  say(`${pad("endpoint", 42)} ${pad("median", 8)} ${pad("p95", 8)} ${pad("max", 8)} ${pad("status", 8)} bytes (wire, as fetched)`);
  for (const [label, make] of cases) {
    const n = label.startsWith("POST /farm") ? Math.min(CALLS, 15) : label.includes("/fields (") ? Math.min(CALLS, 20) : CALLS;
    const times = [];
    const codes = new Set();
    let bytes = 0;
    for (let i = 0; i < n; i++) {
      const made = make(i);
      const [path, init] = Array.isArray(made) ? made : [made, undefined];
      try {
        const result = await timed(`${API}${path}`, init);
        times.push(result.ms);
        codes.add(result.status);
        bytes = result.bytes;
      } catch (error) {
        codes.add("ERR");
      }
    }
    if (times.length === 0) {
      say(`${pad(label, 42)} no answer`);
      continue;
    }
    const s = stats(times);
    say(`${pad(label, 42)} ${pad(f1(s.median), 8)} ${pad(f1(s.p95), 8)} ${pad(f1(s.max), 8)} ${pad([...codes].join("/"), 8)} ${kb(bytes)}`);
  }
  say();
}

// ---------------------------------------------------------------------------------------------
// Browser
// ---------------------------------------------------------------------------------------------

/** Start recording requestAnimationFrame gaps in the page; `stop` returns them (ms). */
const RECORDER = `
  window.__frames = { gaps: [], on: false, last: 0 };
  const tick = (t) => {
    if (window.__frames.on) { if (window.__frames.last) window.__frames.gaps.push(t - window.__frames.last); window.__frames.last = t; }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
`;
const startFrames = (page) => page.evaluate(() => { window.__frames.gaps = []; window.__frames.last = 0; window.__frames.on = true; });
const stopFrames = (page) => page.evaluate(() => { window.__frames.on = false; return window.__frames.gaps; });
const frameLine = (label, gaps) => {
  if (gaps.length === 0) return `${pad(label, 40)} no frames`;
  const s = stats(gaps);
  const slow = gaps.filter((g) => g > 33.4).length;
  return `${pad(label, 40)} ${pad(s.n, 7)} ${pad(f1(s.median), 8)} ${pad(f1(s.p95), 8)} ${pad(f1(s.max), 8)} ${Math.round((100 * slow) / gaps.length)}%`;
};

async function recordMotion(page, label, move) {
  await startFrames(page);
  await move();
  const gaps = await stopFrames(page);
  say(frameLine(label, gaps));
}

async function motionScenarios(page, label) {
  // Zoom: county view in to farm level and back out, animated, one step at a time. The steps are driven
  // from inside the page: driving each one with a separate page.evaluate adds a stall of about 0.6 s per
  // step that belongs to the test harness, not to the map (found while measuring, see perf_before.txt).
  await recordMotion(page, `${label}: zoom in and out`, async () => {
    await page.evaluate(async () => {
      for (const z of [11, 12, 13, 14, 15, 14, 13, 12, 11, 10]) {
        window.__map.setZoom(z, { animate: true });
        await new Promise((resolve) => setTimeout(resolve, 450));
      }
    });
  });
  // Pan: 90 frames of 25 px drags, at county view and at farm level.
  for (const zoom of [10, 13]) {
    await page.evaluate((z) => window.__map.setView([38.05, -100.75], z, { animate: false }), zoom);
    await page.waitForTimeout(300);
    await recordMotion(page, `${label}: pan at zoom ${zoom}`, async () => {
      await page.evaluate(
        () =>
          new Promise((resolve) => {
            let n = 0;
            const step = () => {
              window.__map.panBy([n < 45 ? 25 : -25, n % 2 ? 6 : -6], { animate: false });
              if (++n < 90) requestAnimationFrame(step);
              else resolve();
            };
            requestAnimationFrame(step);
          }),
      );
    });
  }
}

async function browserTimings() {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  await page.addInitScript(RECORDER);
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error" && !/openstreetmap/.test(m.location()?.url ?? "")) errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(e.message));
  say("## Time to first fields on screen (page opened cold, then reloaded with the browser cache)");
  let resources = [];
  for (const label of ["cold", "reload"]) {
    const start = Date.now();
    if (label === "cold") await page.goto(WEB_URL, { waitUntil: "commit" });
    else await page.reload({ waitUntil: "commit" });
    await page.waitForSelector("[data-fields-drawn]", { timeout: 60000 });
    const ms = Date.now() - start;
    const drawn = await page.getAttribute("[data-fields-drawn]", "data-fields-drawn");
    say(`${pad(label, 8)} ${ms} ms until ${drawn} fields are drawn`);
    if (label === "cold") await page.waitForTimeout(1500);
    if (label === "cold") resources = await page.evaluate(() =>
      performance
        .getEntriesByType("resource")
        .filter((r) => /\/api\//.test(r.name))
        .map((r) => ({ path: new URL(r.name).pathname + new URL(r.name).search, wire: r.transferSize, body: r.decodedBodySize, ms: r.duration })),
    );
  }
  say();
  await page.waitForTimeout(500);

  say("## Frame times while moving the map (headless Chromium, 1440x900)");
  say(`${pad("scenario", 40)} ${pad("frames", 7)} ${pad("median", 8)} ${pad("p95", 8)} ${pad("worst", 8)} over 33 ms`);
  await motionScenarios(page, "plain fields");

  const names = await page.$$eval('[data-testid="layer-select"] option', (nodes) => nodes.map((n) => n.value).filter((v) => v !== "fields"));
  for (const name of names) {
    await page.selectOption('[data-testid="layer-select"]', name);
    await page.waitForFunction(() => document.querySelector('[data-testid="layer-control"]')?.getAttribute("data-state") === "ready", null, { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(300);
    await page.evaluate(() => window.__map.setView([38.05, -100.75], 10, { animate: false }));
    await motionScenarios(page, `layer ${name}`);
  }

  // Time-lapse: play through the years of the first layer that has several.
  say();
  say("## Time-lapse (play button, 6 seconds, frame times; restyle count is the number of years shown)");
  for (const name of names.slice(0, 3)) {
    await page.selectOption('[data-testid="layer-select"]', name);
    await page.waitForTimeout(800);
    const hasYears = await page.isEnabled('[data-testid="layer-play"]').catch(() => false);
    if (!hasYears) {
      say(`${pad(`layer ${name}`, 40)} no years to play`);
      continue;
    }
    await page.evaluate(() => window.__map.setView([38.05, -100.75], 10, { animate: false }));
    await startFrames(page);
    await page.click('[data-testid="layer-play"]');
    await page.waitForTimeout(6000);
    const gaps = await stopFrames(page);
    await page.click('[data-testid="layer-play"]').catch(() => {});
    say(frameLine(`time-lapse ${name}`, gaps));
  }
  say();

  // Opening a field: clicks on the page change React state; measure input-to-panel time.
  say("## Opening a field from the URL, time until the plan cards show");
  for (const id of [DRY, WET]) {
    const fresh = await context.newPage();
    const start = Date.now();
    await fresh.goto(`${WEB_URL}/#${id}`, { waitUntil: "commit" });
    await fresh.waitForSelector('[data-testid="plan-card"]', { timeout: 60000 }).catch(() => {});
    say(`${pad(id, 12)} ${Date.now() - start} ms to the first plan card`);
    await fresh.close();
  }
  say();

  say("## Payloads the page fetched on this load (first, cold load; resource timing: bytes on the wire, and after decompressing)");
  say(`${pad("request", 34)} ${pad("wire", 12)} ${pad("decoded", 12)} ms`);
  for (const r of resources.sort((x, y) => y.body - x.body).slice(0, 12)) {
    say(`${pad(r.path, 34)} ${pad(r.wire === 0 ? "cached/304" : kb(r.wire), 12)} ${pad(kb(r.body), 12)} ${f1(r.ms)}`);
  }
  say();
  if (errors.length > 0) say(`console errors during the run: ${errors.length} (first: ${errors[0].slice(0, 120)})`);
  await browser.close();
}

say(`Perf run ${new Date().toISOString()}  api=${API} web=${WEB_URL}`);
say();
try {
  if (!flag("skip-server")) await serverTimings();
  if (!flag("skip-browser")) await browserTimings();
} finally {
  if (OUT) writeFileSync(OUT, lines.join("\n") + "\n");
}
