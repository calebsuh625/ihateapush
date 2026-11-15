# Crop rotation decision-support tool

NASA Space Apps project. Long-term goal: use satellites to measure soil health, compare neighbouring
fields to learn which crop rotations actually improve soil, and recommend climate-resilient rotations.

**Phase 1 (this repo):** a working skeleton on real data for **Finney County, Kansas** (FIPS 20055).
Open the map, click a field, and see its real 2008–2024 crop history, soil organic matter, slope, and
the top three rotation plans from a simple rule-based planner.

**Satellite greenness (optional add-on):** for every field and year, how green the field got and how
much of the year it had green cover, measured from Landsat through Google Earth Engine. It shows up
as a "Satellite" section under the crop history.

**Soil over time (Phase 2A, optional add-on):** a per-field, per-year soil table from bare-soil
satellite views, 2016–2024: an organic-matter estimate and the number of days the soil sat bare.
The honest result is that the organic-matter estimate cannot show change; bare days can be
trusted. See [Soil over time](#soil-over-time-phase-2a).

**Smart planner and future weather (Phase 3, optional add-on):** the plans are scored against six climate
models for a year you choose, and the app shows how often each plan holds up. See
[Phase 3](#phase-3-smart-planner-and-future-weather).

```
pipeline/   data scripts; run_all.py builds data/fields.parquet from public USDA / USGS data
api/        FastAPI backend (main.py) + rotation planners (the package planner/: smart.py is the Phase 3 planner, legacy.py the Phase 1 one)
web/        Vite + React + TypeScript + react-leaflet frontend
data/       data files (git-ignored); downloads are cached in data/raw/
tests/      pytest tests for the pipeline, API and planner
schema.md   the data contracts: fields, soil table, Phase 2B outputs, Phase 3 tables and API answers
```

## Requirements

- Python 3.11 or newer (developed on 3.12)
- Node.js 20 or newer with npm (developed on Node 24)
- `make` (optional — every target is one or two plain commands, listed below)
- For the real data: an internet connection and about 5 GB of free disk. The first build downloads
  about 4 GB (almost all of it one national USDA file) and took about 3 minutes from a fresh clone
  on a fast connection; later builds take about 7 seconds because everything is cached in `data/raw/`.

## Setup and run

```bash
make setup      # creates .venv, installs Python packages, runs npm install in web/
make pipeline   # builds data/fields.parquet from real data  (same as: .venv/bin/python pipeline/run_all.py)
make satellite  # optional: builds data/field_satellite.parquet (needs an Earth Engine login, see below)
make soil-obs   # optional: bare-soil satellite views (Earth Engine, about 30 compute-hours, see below)
make soil-health  # optional: builds data/soil_health.parquet from those views (local, about 90 s)
make dev        # starts the API on :8010 and the web app on :5173 — Ctrl-C stops both
```

Then open <http://localhost:5173> and click a field.

`make pipeline` is optional: without `data/fields.parquet` the API serves 30 made-up fields from
`data/fields_fake.parquet` (`make dev` creates that file if it is missing) and the map shows a
"Demo data" badge.

The same thing without `make`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix web install

.venv/bin/python pipeline/make_fake_fields.py      # 30 fake fields (instant)
.venv/bin/python pipeline/run_all.py               # the real data

# two terminals:
.venv/bin/python -m uvicorn api.main:app --reload --reload-dir api --reload-dir pipeline --port 8010
API_PORT=8010 npm --prefix web run dev -- --port 5173 --strictPort
```

Ports: the API defaults to **8010** and the web app to **5173**. To change them:
`make dev API_PORT=8020 WEB_PORT=5180`.

## Checks

```bash
make test       # Python tests (pytest) + web unit tests
make validate   # checks the active data file against schema.md and prints a summary
make e2e        # with `make dev` running: opens a headless browser, clicks real fields on the map
                # and compares the panel with the API (downloads Chromium the first time)
```

## The data pipeline

`python pipeline/run_all.py` rebuilds `data/fields.parquet` end to end and prints a summary plus
PASS/WARN sanity checks. All downloads are cached under `data/raw/`; delete a sub-folder to refetch it.

| step | source | module |
|------|--------|--------|
| Field outlines | USDA NASS Crop Sequence Boundaries (national file, county subset) | `pipeline/csb.py` |
| Crop per field per year, 2008–2024 | USDA Cropland Data Layer county rasters (CropScape); majority class inside each field | `pipeline/cdl.py`, `pipeline/cdl_codes.py` |
| Organic matter | SSURGO via USDA Soil Data Access; surface-horizon `om_r`, area-weighted per field | `pipeline/soil/ssurgo.py` |
| Slope | USGS 3DEP 1/3 arc-second elevation (falls back to 3DEP 1 arc-second, then SRTM); mean slope per field | `pipeline/slope.py`, `pipeline/dem.py` |
| Area | computed in an equal-area projection (EPSG:5070) | `pipeline/run_all.py` |

Fields are kept if they are at least 5 ha and are cropland (see `pipeline/fields.py` for the exact rule).
If the Crop Sequence Boundaries cannot be downloaded or read, field outlines are built by segmenting
contiguous cropland from the Cropland Data Layer instead (`pipeline/cdl_segment.py`).

Useful flags:

```bash
.venv/bin/python pipeline/run_all.py --max-fields 5000            # spatially even subset
.venv/bin/python pipeline/run_all.py --fields-source cdl-segment  # force the fallback outlines
.venv/bin/python pipeline/run_all.py --allow-missing soil,slope   # write nulls if a service is down
```

What the current build contains: 7,255 fields (202,000 ha, about 60% of the county), all with a
full 2008–2024 history. Field-years are 26% winter wheat, 23% corn, 18% fallow, 15% sorghum,
11% alfalfa, 4% grass/pasture, 1% soybeans. Organic matter runs 0.5–3.6% (median 1.7%) and slope
0.1–6.5% (median 0.8%).

One sanity check is expected to print WARN: in 2024 about 7% of fields are classed grass/pasture,
up from 0.3% in 2023. That jump is in the 2024 Cropland Data Layer itself, not a pipeline fault.

An interrupted download resumes on the next run. Stopping `make dev` with Ctrl-C prints
`make: *** [dev] Error 130`; that is normal.

Besides `data/fields.parquet` the run writes two side files that are not part of the contract:
`data/fields_provenance.json` (sources, counts, timings) and `data/fields_source_ids.csv`
(`field_id` → Crop Sequence Boundaries id).

## Satellite greenness (optional)

`make satellite` (same as `.venv/bin/python pipeline/satellite.py`) builds
`data/field_satellite.parquet`: one row per field per year, 2008–2024.

| column | meaning |
|--------|---------|
| `ndvi_peak` | peak greenness: the 90th percentile of NDVI over clear Landsat views from 1 March to 31 October, averaged over the field |
| `cover_frac` | green cover: the share (0–1) of the year's clear views in which the ground was green (NDVI at least 0.3) |
| `n_obs` | how many clear views the year had, on average per pixel |

It is a side file with its own contract in `pipeline/satellite_schema.py`; `schema.md` and
`data/fields.parquet` are unchanged. Without it the app still works and the panel says the satellite
data has not been built.

One-time setup (free for noncommercial use, no payment details):

```bash
# 1. Register a Google Cloud project for Earth Engine: https://code.earthengine.google.com/register
.venv/bin/earthengine authenticate --scopes=https://www.googleapis.com/auth/earthengine
.venv/bin/earthengine set_project <project-id>      # or: export EE_PROJECT=<project-id>
```

The first full run takes about 18 minutes and uses about 13 of the free tier's 150 compute-hours
(EECU-hours) per month. Every result is cached in `data/raw/gee/`, so a rerun takes about a second
and makes no Earth Engine requests. Only the field outlines are sent to Earth Engine.

```bash
.venv/bin/python pipeline/satellite.py --max-fields 50 --years 2015-2016 --out /tmp/test.parquet   # quick test
.venv/bin/python pipeline/satellite.py --force                                                     # ignore the cache
```

How it is computed: Landsat 5, 7, 8 and 9 surface reflectance at 30 m, with clouds, shadows and snow
masked out. Scenes from the same day count as one view. Landsat 8 and 9 are adjusted to match
Landsat 7 (Roy et al. 2016) so the years are comparable.

What the current build shows, averaged by that year's crop:

| crop | peak greenness | green cover |
|------|----------------|-------------|
| alfalfa | 0.79 | 0.76 |
| soybeans | 0.81 | 0.39 |
| corn | 0.72 | 0.40 |
| sorghum | 0.66 | 0.38 |
| winter wheat | 0.59 | 0.39 |
| grass/pasture | 0.50 | 0.39 |
| fallow | 0.33 | 0.18 |

Known limits:

- **2012 is thin.** Only Landsat 7 was flying, so fields have about 15 clear views instead of 35–49,
  and that year's numbers are noisier. 2020 and 2021 have about 23–26 (Landsat 8 only; Landsat 7 is
  left out from 2020 because its orbit drifted to an earlier time of day).
- **Sensor changes leave a small step.** Bare fields read about 0.03–0.04 higher from 2013 on, and
  the greenest fields about 0.02–0.03 lower from 2020 on. That is smaller than normal year-to-year
  change, but be careful reading a long trend on one field across 2012/2013 or 2019/2020.
  Comparing fields within the same year is not affected.

## Soil over time (Phase 2A)

`organic_matter_pct` is one static soil-survey value per field, so it cannot show change. This step
builds a yearly table, `data/soil_health.parquet` (contract in `schema.md`), in two commands:

```bash
.venv/bin/python -m pipeline.soil.bare_soil --estimate   # how many requests and compute-hours, no Earth Engine call
make soil-obs      # Earth Engine: bare-soil views per field and year -> data/soil_bare_obs.parquet
make soil-health   # local: model, noise analysis -> data/soil_health.parquet, _meta.json, _report.md
```

`make soil-obs` needs the same Earth Engine login as `make satellite`. The county run was 861
requests, about 45 minutes and **29.6 EECU-hours** of the free tier's 150 a month (Landsat 11.7,
Sentinel-2 17.9). Results are cached per request in `data/raw/gee_soil/`; `--max-eecu-hours N`
stops a run at a spending cap (exit code 3) and a rerun continues from the cache.

How it works:

1. **Bare-soil views** (`pipeline/soil/bare_soil.py`). Sentinel-2 (2017–2024) and Landsat 8/9
   (2016–2024) surface reflectance, with cloud, shadow and snow masked. A view counts as bare soil when
   NDVI is under 0.25 and NBR2 is under 0.10 (dry, no residue); for Sentinel-2 the limits are 0.20 and
   0.08, the same thresholds seen through its bands. Fields are buffered 30 m inward. Per
   field and year: the median reflectance of the bare views, how many there were, and bare days.
2. **Model** (`model.py`, `features.py`). Gradient boosting predicts SSURGO organic matter from the
   bare-soil bands and soil indices. Every value in the table comes from a model that never saw that
   field's soil map unit.
3. **Noise and verdict** (`noise.py`, `confidence.py`, `exposure.py`, `build.py`). Writes the table,
   `data/soil_health_meta.json` and the full numbers in `data/soil_health_report.md`.

### What the county run found

Organic-matter estimate (`om_pred`), 7,255 fields, 53 soil map units:

| measure | value |
|---------|-------|
| Accuracy with random cross-validation (misleading: neighbours leak the answer) | R² 0.38 |
| Accuracy grouped by soil map unit | R² 0.25 (90% interval −0.12 to 0.33); error 0.39 points against 0.47 for guessing the mean |
| Accuracy on a soil series the model has not seen | R² 0.06 |
| Year-to-year jump on fields whose crop did not change | 0.14 points |
| Change a good rotation might cause | 0.1–0.3 points per **decade** |
| Smallest change detectable on one field over the 9 years | 1.4 points per decade |
| Smallest change detectable averaging 100 / 1,000 fields | 0.36 / 0.30 points per decade |
| Shift caused only by last year's crop (surface residue) | up to 0.15, which reads as 0.6 points of real change |

"Accuracy" here is against SSURGO, which is a soil map, not a set of measurements.

**Verdict: real organic-matter change is buried in noise**, on a single field and on groups of
fields. The estimate mostly tells sandy soil from loam; within one kind of soil it does not follow
organic matter, and it moves with whatever residue lies on the surface. Do not use `om_pred` to
compare rotations.

**Use `bare_days` instead.** It is the estimated number of days a year the soil sat bare and
exposed, and it passes its checks:

- Median by that year's crop: fallow 143 days, corn 46, sorghum 42, winter wheat 18, alfalfa 0.
- The two satellites agree on the same field-year (correlation 0.965, about 8 days apart).
- It moves opposite to green cover from the satellite step (correlation −0.60).
- One year of data separates a fallow year from a cropped year, but not finer differences: on fields
  whose crop did not change it still moves 17 days from year to year (27 for fallow, 32 for corn).

Limits: `bare_days` measures how exposed the soil is, not what the soil has become. Dry years lift
it for everyone (county median 76 days in 2023 against 27–46 in other years), so compare fields
within the same year. `om_pred` is empty in the 10% of field-years with no bare-soil view, mostly
alfalfa and other fields that are never bare. `confidence` is repeatability, not accuracy, and never exceeds 0.38.

## The API

Base URL `http://127.0.0.1:8010` (interactive docs at `/docs`).

| endpoint | returns |
|----------|---------|
| `GET /health` | which data file is loaded, how many fields, and whether the satellite and soil tables are loaded |
| `GET /fields` | GeoJSON FeatureCollection of all fields (`field_id`, `area_ha`, simplified outline) |
| `GET /field/{id}` | one field: outline, `crop_history` as an object, organic matter, slope, area, and stats (distinct crops, longest same-crop streak, % of years in corn) |
| `GET /field/{id}/plans` | the planner's top 3 rotations with soil / water / money / overall scores and a one-line reason; with the Phase 3 tables also risk, "holds up in X of 6 futures", and the future weather assumed (see [Phase 3](#phase-3-smart-planner-and-future-weather)) |
| `GET /field/{id}/score?rotation=...` | Phase 4: the farmer's own rotation scored like the plans, next to our top 3, with a comparison (see [Phase 4](#phase-4); 503 without the Phase 3 tables) |
| `GET /field/{id}/transition?plan=1` | Phase 4: the year-by-year path from what the field grows now to one of our plans (`plan=1|2|3`) or to the farmer's own rotation (`target=...`), against carrying on as now (see [Phase 4](#phase-4); 503 without the Phase 3 tables) |
| `GET /climate` | Phase 3: the future weather the planner assumes, per scenario and year |
| `GET /field/{id}/satellite` | per-year `ndvi_peak`, `cover_frac` and `n_obs`, plus averages and the trend per decade; `"available": false` with a reason if the satellite file has not been built |
| `GET /field/{id}/soil` | per-year `om_pred`, `confidence`, `n_obs` and `bare_days` for 2016–2024, the soil-survey value, which signal to trust, the typical noise and a plain verdict; `"available": false` with a reason if the soil table has not been built |
| `GET /effects` | Phase 2B: `data/effects.json` (503 until `make causal` has run) |
| `GET /field/{id}/neighbors` | Phase 2B: the field's matched look-alike neighbours, their crops and soil timelines |
| `GET /field/{id}/twin` | Phase 4: the field and its best-matched neighbour side by side: histories, bare days, the pair's difference, its comparison's interval, how many pairs agree, the placebo (503 until `make causal` has run) |
| `GET /layers`, `GET /layers/{name}` | Phase 4: county-wide per-field values the map can colour by (`bare_days`, `erosion_risk`, `rotation_change`, `irrigation`), as arrays in the field order of `GET /fields`, with class breaks and county summaries (see [Phase 4](#phase-4)) |

On startup the API loads `data/fields.parquet` if it exists, otherwise `data/fields_fake.parquet`,
logs which one, and validates it against `schema.md`; a file that does not match stops the server
with a readable list of problems. Set `FIELDS_PATH=/some/file.parquet` to load a specific file.

The satellite file is optional: `data/field_satellite.parquet` is loaded when the real fields file
is in use (`SATELLITE_PATH=/some/file.parquet` overrides). If it is missing or invalid the server
logs one line and runs without it. The soil table works the same way: `data/soil_health.parquet`
and `data/soil_health_meta.json` (`SOIL_HEALTH_PATH` and `SOIL_HEALTH_META_PATH` override).

The planners live in the package `api/planner/`. The Phase 1 rule-based one is `api/planner/legacy.py`
behind one function, `rank_plans(field) -> list[Plan]`; it answers when the Phase 3 tables are missing.
To try another model, write a function with that signature and pass it to `create_app(planner=...)`
in `api/main.py` (the answer then says `"engine": "basic"`).

## The data contract

`schema.md` defines `data/fields.parquet`, `data/soil_health.parquet`, the Phase 2B outputs and the Phase 3
tables and API answers. It only changes on purpose: the pipeline validates against it before writing and
the API validates on startup (`pipeline/schema.py`, `pipeline/soil/health_schema.py`,
`pipeline/climate/schema.py`, `pipeline/field_traits_schema.py` and `pipeline/nass.py` are the validators).

## Phase 2B: rotations compared on look-alike neighbours

Real data: `data/fields.parquet` and the Phase 2A soil table `data/soil_health.parquet`
(`is_fake: false` in every output). Run of 2026-11-15, 7,255 fields.

```bash
make causal                         # ~3 s; log in data/causal.log
CAUSAL_OUTCOME=om_pred make causal  # another soil column, no code change (not recommended, see below)
.venv/bin/python -m pipeline.causal.run_causal --counts-only   # stop after matching, write nothing
```

**What it measures.** The outcome is `bare_days`: the days a year the soil sits bare and exposed.
Fewer is better. It is the one column Phase 2A found trustworthy. It is **not soil health**: it says
how long the soil was uncovered, and that is mostly decided by which crop is in the ground (a fallow
year is bare by definition, alfalfa is never bare). So read every number below as "how much cover this
rotation keeps", not as "what this rotation did to the soil".

**Two kinds of number.** A *level* is how many days a field sat bare in some years. A *change* is how
much that moved between 2018–2019 and 2020–2024. The effects below are changes in the gap between two
fields, not levels, and the two can point opposite ways: in 312 of the 1,098 pairs (28%) the field
whose bare days fell more is still the one that sat bare more days in 2020–2024.

### Design

1. **Rotation type** from each field's 2020–2024 crops: alfalfa/perennial, diverse, corn-soy,
   wheat-sorghum-fallow, continuous corn, or other (never compared).
2. **Look-alike pairs.** Two fields are paired 1:1 when they are in the same irrigation group
   (irrigated / dryland, guessed from the field's shape and its 2008–2019 crops), under 5 km apart,
   within 0.5 points of slope and 0.3 points of mapped organic matter, within half a county standard
   deviation of each other in 2016–2017 bare days (25 days), and at most 25% apart in their 2008–2019
   crop mix, but grew different rotation types in 2020–2024. These limits are the ones the method was built
   with; none was loosened.
3. **Difference in differences.** For each pair: (A's 2020–2024 mean − A's 2018–2019 mean) −
   (the same for B), using only years both fields have, so every comparison is made within the same
   year (2023 was dry and lifted bare days for everyone). All field-years count equally. The effect of
   "A vs B" is the mean over its pairs; the 95% interval resamples pairs in 10 km blocks, because
   neighbours share weather.
4. **How much is just the crops.** The same difference is computed again after replacing every
   field-year by the county average, that year, for the crop grown that year ("from the crops alone").
   What remains is "left over".

**Why the switch year is 2020.** The soil table only covers 2016–2024, so the cut has to sit inside
those years. 2020 is the earliest year that leaves two soil years to match on (2016–2017) and two
*other* years as the "before" (2018–2019), which keeps the luck in the matched-on years out of the
comparison; it leaves five "after" years, enough to hold one full three-year wheat-sorghum-fallow
cycle. 2019 would leave a single year to match on (2016, the thinnest year: 4% of fields have no
reading). 2021 leaves four crop years, too few to tell rotations apart. This was settled from year
counts before any effect was computed. Pair counts do not separate 2019 from 2020: under the same
crop rule 2019 gives 1,066 matched pairs against 1,098 (`data/logs/causal_fix_switch2019_k1.log`;
the 355 in `causal_design_switch2019.log` comes from the stricter 2-year rule that a six-year window
gets, not from the year). 2020 is a cut-off, not a year in which everyone changed: many fields
simply carried on, and some had already changed crops before it.

**One change to the rotation rules for a 5-year window.** They were written for nine years and asked
for "wheat in at least 2 years" and "3 crops with at least 2 years each". In five years the second is
impossible (no field was "diverse") and the first threw out one of the three phases of a textbook
wheat-sorghum-fallow rotation (509 fields). Both now ask for 1 year when the window has five years and
still 2 when it has six or more. Nothing else changed.

### Counts

| | |
|---|---|
| Fields | 7,255 |
| Eligible (irrigation known, soil readings on both sides of 2020) | 6,250 (1,004 left out as "irrigation uncertain", 1 for missing readings) |
| Rotation type 2020–2024 | wheat-sorghum-fallow 2,985 · diverse 1,398 · other 1,210 · alfalfa/perennial 1,037 · continuous corn 571 · corn-soy 54 |
| Look-alike candidates passing every limit | 37,575 |
| Matched pairs | 1,098, from 1,782 different fields, in 10 comparisons |
| Comparisons reported (10 pairs or more) | 8; alfalfa/perennial vs corn-soy (6 pairs) and corn-soy vs wheat-sorghum-fallow (5) are too small |

### Results

Days a year the soil sits bare. "Gap" is the plain level difference A − B, first in 2018–2019, then in
2020–2024 (negative = A sat bare fewer days). **The effect is the change in that gap**, the second
minus the first: negative means the gap moved A's way (A's bare days fell more, or rose less, than
B's). It is not how many fewer days A sat bare.

| Comparison (A vs B) | Pairs | Gap 2018–19 → 2020–24 | Effect = change in the gap [95% interval] | Different from zero? | From the crops alone | Left over |
|---|---|---|---|---|---|---|
| alfalfa/perennial vs continuous corn | 85 | −6.8 → −25.8 | −19.0 [−31.3, −6.8] | yes | −19.5 [−27.2, −10.6] | +0.6 [−12.1, +14.8] |
| alfalfa/perennial vs diverse | 168 | −11.4 → −26.6 | −15.3 [−24.9, −7.7] | yes | −20.8 [−27.4, −14.1] | +5.5 [−3.6, +13.5] |
| alfalfa/perennial vs wheat-sorghum-fallow | 54 | −14.8 → −38.9 | −24.1 [−41.6, −4.9] | yes | −33.7 [−43.6, −22.6] | +9.6 [−7.3, +24.9] |
| corn-soy vs continuous corn | 30 | +0.1 → +3.7 | +3.6 [−4.7, +16.4] | no | −6.0 [−11.9, +0.4] | +9.6 [+3.3, +17.3] |
| diverse vs continuous corn | 182 | +9.7 → +11.8 | +2.1 [−9.2, +15.7] | no | +3.0 [−0.9, +7.4] | −0.9 [−12.0, +10.8] |
| diverse vs corn-soy | 25 | +11.9 → +7.8 | −4.1 [−20.0, +13.3] | no | +2.5 [−5.0, +8.9] | −6.6 [−18.4, +7.5] |
| diverse vs wheat-sorghum-fallow | 480 | −4.3 → +1.4 | +5.7 [−1.7, +12.0] | no | −0.5 [−3.0, +1.9] | +6.2 [+0.5, +11.4] |
| wheat-sorghum-fallow vs continuous corn | 63 | +13.0 → +24.6 | +11.7 [−4.9, +29.7] | no | +12.6 [+4.9, +19.1] | −0.9 [−15.1, +13.8] |

The gaps are `gap_pre` and `gap_post` in `data/effects.json` (no interval is computed for them). Rows
by irrigation group and by soil band are there too.

### Placebos

**Fake switch years: failed for five of the eight comparisons.** Pretending the switch happened in
2017, 2018 or 2019 and using only 2016–2019 readings, the "effect" should be zero. 7 of 24 intervals
exclude zero (about 1 would by chance):

| Comparison | fake 2017 | fake 2018 (2016–17 vs 2018–19) | fake 2019 |
|---|---|---|---|
| alfalfa/perennial vs continuous corn | −5.2 [−15.3, +7.4] | −3.7 [−13.9, +7.2] | −13.6 [−27.4, +1.3] |
| alfalfa/perennial vs diverse | −10.7 [−18.4, −3.7] **fails** | −10.8 [−18.3, −4.4] **fails** | −12.1 [−23.9, −0.6] **fails** |
| alfalfa/perennial vs wheat-sorghum-fallow | −4.6 [−12.0, +3.7] | −13.5 [−22.7, −5.8] **fails** | −9.9 [−30.6, +5.8] |
| corn-soy vs continuous corn | +2.6 [−10.4, +17.6] | +0.2 [−7.2, +6.9] | +0.3 [−8.9, +5.8] |
| diverse vs continuous corn | +8.9 [−3.6, +22.2] | +9.8 [+2.8, +15.7] **fails** | +6.2 [−2.7, +14.1] |
| diverse vs corn-soy | +8.2 [−10.1, +25.8] | +11.8 [+2.4, +22.1] **fails** | +9.3 [−0.4, +21.7] |
| diverse vs wheat-sorghum-fallow | −6.9 [−15.4, −0.2] **fails** | −4.1 [−10.4, +1.4] | −4.6 [−12.8, +3.4] |
| wheat-sorghum-fallow vs continuous corn | +13.0 [−12.3, +45.8] | +12.4 [−3.5, +30.2] | +10.4 [−9.9, +33.9] |

So the gap between the two sides of a pair was already moving before 2020. There are two reasons,
and this test cannot separate them:

- **Crops that already differed (the alfalfa rows).** Fields that are alfalfa in 2020–2024 were often
  already going into alfalfa in 2018–2019: in the matched pairs the A fields grew a perennial in
  42–51% of those two years, the B fields in 8–31%. The crops alone predict −11 to −20 days for the
  fake 2018 switch, as much as was measured. The matching allows the 2008–2019 crop mix to differ by
  a quarter.
- **The gap bouncing back after matching (all rows).** Every fake "before" window contains 2016–2017,
  the years the pairs were matched on (the fake 2018 test is exactly 2018–2019 against 2016–2017).
  Pairs were picked for being close in those years, so the gap was small there by selection and
  springs back afterwards. The crops do not explain the two failing "diverse" rows: diverse vs
  continuous corn, fake 2018, is +9.8 where the crops predict 0.0, and its mean gap goes from −0.1
  days in 2016–2017 to +9.7 in 2018–2019; diverse vs corn-soy is +11.8 where the crops predict +4.6.
  The main estimate keeps the matched-on years out of its "before"; this placebo cannot, because
  there are no earlier soil years.

The one check that avoids the matched-on years is the move of the gap from 2018 to 2019 (`by_year` in
`effects.json`, no interval): about −17, −7 and −2 days for the three alfalfa comparisons, and
between −3 and +3 for the other five. That fits both readings above, but it is two single years.

The same placebo also fails on the made-up soil table (13 of 27 intervals, real fields), where bare
days are nothing but the crop of the year plus noise. That shows the failure does not need anything
odd in the satellite readings; it does not show that crops are the only cause. Either way, the
comparison is not a clean before/after of a change made in 2020.

**Shuffled labels: behaves.** Shuffling the rotation labels within each irrigation group and
re-matching 200 times gives effects centred on zero (means between −0.3 and +0.2 days). The three
alfalfa effects lie far outside what shuffling produces (p = 0.005, the smallest value 200 shuffles
can give). Two others stand out against shuffling although their intervals above include zero:
wheat-sorghum-fallow vs continuous corn (p = 0.035) and diverse vs wheat-sorghum-fallow (p = 0.025).
Shuffling ignores that neighbouring pairs move together, so the intervals are the stricter test.

### What this says, in plain words

- **Between 2018–2019 and 2020–2024 the gap in bare days widened by 15 to 24 days a year in favour
  of alfalfa/perennial fields, against look-alike neighbours in corn, mixed rotations or
  wheat-sorghum-fallow.** In 2020–2024 they sat bare 26 to 39 fewer days a year than those
  neighbours; they were already 7 to 15 days ahead in 2018–2019, when many of them were already in
  alfalfa. These three are the only effects that can be told from zero. **They are mechanical:** the
  county-average bare days of the crops grown predict all of the widening (−20 to −34 days), and
  nothing is left over. This is "alfalfa covers the ground", not "alfalfa changed the soil". The
  fake-switch placebo also fails for two of the three.
- **Wheat-sorghum-fallow against continuous corn: +12 days, not distinguishable from zero**
  (−5 to +30), although a third of its years are fallow and the crops alone predict +13. With 63
  pairs the data cannot confirm even this expected, mechanical gap.
- **Every other comparison is not distinguishable from zero.**
- **"Left over" is not a finding.** Two left-over numbers exclude zero (corn-soy vs continuous corn,
  diverse vs wheat-sorghum-fallow, about +10 and +6 days). Among eight comparisons, with placebos
  that fail and only 11 blocks behind the first of the two, that is not enough to claim that
  anything beyond the choice of crop matters.
- **Nothing here shows that a rotation improved the soil.** No trustworthy yearly soil measurement
  exists in this project (Phase 2A), so that question stays open.

### Limits

- `bare_days` is cover, not soil condition, and it is driven by the crop of the year.
- Four soil years before the switch: two to match on, two as the "before". Pre-existing trends
  cannot be ruled out, and the placebo cannot tell them from the gap bouncing back after matching.
- "Look-alike" means alike in mapped soil, slope, 2008–2019 crop mix and 2016–2017 bare days (median
  difference 6 days). It does not mean alike in 2018–2019: by then half of the pairs differ by more
  than 25 bare days and 28% by more than 50. The difference-in-differences subtracts that gap; it
  does not make the fields alike.
- The pairs "grew different crops in 2020–2024". Nothing says they changed in 2020.
- A five-year "after" window is not a whole number of three-year cycles, so which years happen to be
  fallow matters; the year-by-year gaps in `effects.json` (`by_year`) swing between −23 and +28
  days for diverse vs wheat-sorghum-fallow.
- "Diverse" in a five-year window means three or more different crops; it is a mixed bag and
  includes fields with fallow years (in the matched pairs a fifth of its years, against a third
  for wheat-sorghum-fallow). 224 of the 1,398 "diverse" fields (123 of the 727 in matched pairs, 17%)
  are fallow in two or more of the five years. The app therefore shows this type as
  "Mixed (3+ crops)". The rule was left as it is: changing it after seeing the results would be
  choosing the answer.
- "Alfalfa/perennial" needs two perennial years in five, and grass/pasture counts: 35 of the 235
  alfalfa/perennial fields on the A side of a pair have fewer than two alfalfa years.
- Irrigation is guessed, not known; 1,004 fields with mixed signals are left out.
- The small comparisons rest on few blocks (corn-soy ones: 25–30 pairs in 10–11 blocks), so their
  intervals are rough. Results by soil band are thinner still.
- `CAUSAL_OUTCOME=om_pred` runs, but Phase 2A showed that column cannot show soil change; do not
  read its output as findings.

### In the app

`GET /effects` serves `data/effects.json`; `GET /field/{id}/neighbors` serves a field's matched
look-alikes. The "Your neighbors" section of the field panel shows each look-alike's crops and bare
days next to this field's, and then says two things in two separate sentences, because they can
point opposite ways. First the level: which field sat bare more days in 2020–2024; the "better
protected" / "more exposed" tag belongs to this sentence only (`after_gap` and `verdict` in the
response). Then the change: how much each field's bare days moved since 2018–2019 and how the two
changes differ (`divergence`), with no tag. Example: KS-007027 went from 224 to 158 bare days and its
look-alike from 53 to 133. Its change is 146 days lower, yet it still sat bare 25 more days a year,
so it is tagged "more exposed". The section then gives the comparison's average change with its
interval, and says so when that average cannot be told from zero.
A field without a match gets the reason and links to the three demo pairs (`demo_pairs` in
`effects.json`). A demo pair is the best-matched pair that shows its comparison's direction in both
the change and the 2020–2024 level and, where one exists, whose two fields are typical of their type
(a "diverse" field with fallow in under a third of the years, a perennial in over half of the years
for "alfalfa/perennial"). These conditions only choose what to show. Demo pairs are large, clear
cases, not average ones: the first one moved by −176 days where its comparison averages −15.

### Without the real soil table

If `data/soil_health.parquet` is missing, `make causal` writes `data/soil_health_fake.parquet`
(`pipeline/causal/make_fake_soil.py`): the same columns and years as the real table, checked by the
same validator, with **made-up values and planted crop effects**. Every output then carries
`is_fake: true` and the app shows a "Fake soil data · not findings" badge. The unit tests use this
path on a small synthetic grid and check that the planted effect is recovered; they write only to
temp folders, never to `data/`.

## Phase 3: smart planner and future weather

**What it does.** For one field it compares crop patterns (cycles of three or four years) and shows the
best three. Each pattern is tried in the weather of six climate models, for a year you pick (today to 2060)
and one of two emissions paths: "middle road" (`ssp245`) or "high emissions" (`ssp585`). You choose how
much soil protection, water, money and risk matter to you. Each card says how many of the six futures the
pattern holds up in. The field panel has sliders for this; the plans re-rank as you move them. Soil
protection means fewer bare days a year and lower wind-erosion risk; it is not a measure of soil health.
Money is return over direct costs, not take-home pay.

**Data sources.**

| what | source |
|---|---|
| observed weather 2000-2024 | NASA POWER (one download, no key) |
| future weather 2025-2060 | `NASA/GDDP-CMIP6` on Earth Engine: ACCESS-CM2, GFDL-ESM4, MPI-ESM1-2-HR, MRI-ESM2-0, NorESM2-MM, UKESM1-0-LL |
| yields, acres, prices | USDA NASS, Finney County (district or state where the county has no figure) |
| irrigated or dryland, wind erodibility | our guess from outline, crops and water use; SSURGO `weg` / `wei` |
| water use | OpenET (April-September, 2016-2024) |
| costs, irrigation limit | K-State 2027 crop budgets; Kansas rule K.A.R. 5-3-19(a), 24 inches a year |
| bare days per crop | the Phase 2A satellite soil table |

### Build

Order: `make nass`, `make climate`, `make field-traits`, `make calibrate`. Without these files the app
runs the old planner.

```bash
make nass            # data/nass_finney.parquet (first run streams a 1.1 GB file, about 2 min)
make climate         # climate tables (Earth Engine), log in data/logs/climate_build.log
make field-traits    # data/field_traits.parquet (Earth Engine OpenET)
make calibrate       # fit the crop response -> api/planner/crop_response.json (local, seconds)
make planner-sanity  # six checks on real fields -> data/planner_sanity.md
.venv/bin/python -m pipeline.climate.build --estimate      # cost only, nothing sent
.venv/bin/python -m pipeline.field_traits --estimate       # OpenET pilot and cost, writes nothing
.venv/bin/python -m pipeline.field_traits --no-openet      # without Earth Engine
```

- **Cost.** The climate tables send 648 Earth Engine requests, about **2.4 EECU-hours** once and about
  15 minutes; every answer is cached in `data/raw/climate/`, so a second run is local. The build stops at
  `--max-eecu-hours` (default 5). OpenET for all 7,255 fields and 9 years is about **0.07 EECU-hours**
  (cached in `data/raw/openet/`; `--max-eecu-hours` default 3). Needs the Earth Engine login described
  under "Satellite greenness". `make nass`, `make calibrate` and `make planner-sanity` use no Earth Engine.
- **Outputs.** `data/climate_futures.parquet`, `climate_history.parquet`, `climate_monthly.parquet`,
  `climate_report.md`, `nass_finney.parquet`, `field_traits.parquet`, `crop_response_report.md`. The
  tracked files `api/planner/crop_response.json` and `economics.json` come with the repo. Columns are in
  `schema.md`. No new Python package is needed.
- **Prices.** `python -m api.planner.money` shows prices, costs and the irrigation limit;
  `--update-prices` rewrites the prices in `economics.json` from NASS.

### How crops respond

Yield = normal yield x scale x heat factor x water factor (`api/planner/response.py`). Heat: each day above
35 C (above 32 C in May-June for winter wheat) takes off a fitted share of the yield; the day count is
capped at the most any year of 2000-2024 had plus a margin (85 summer days, 38 wheat days). Water: the FAO
rule, with rain and the soil water stored before planting against what the crop needs. Irrigated crops get
what they need up to the irrigation limit (24 inches); beyond it the missing water cuts the yield. A crop
fails when its modelled yield is under half of normal. `make calibrate` fits this to NASS yields and writes
`crop_response.json`.

Calibration quality per crop (leave-one-year-out skill; 0 is no better than the trend, 1 is perfect):

| crop | practice | quality | note |
|---|---|---|---|
| corn | dryland | good (0.60) | fitted |
| winter wheat | dryland | good (0.70) | the heat loss is a textbook value, not a finding |
| sorghum | dryland | fair (0.40) | water term is textbook |
| corn | irrigated | fair (0.46) | fitted |
| soybeans | irrigated | fair (0.31) | fitted |
| winter wheat | irrigated | fair (0.24) | heat loss 1.0 % a day |
| sorghum irrigated, soybeans dryland | | weak | 9 years each, textbook numbers |
| alfalfa, both practices | | weak | no skill (irrigated) or no data (dryland), textbook numbers |

### How plans are scored

- **A future** is one climate model under the chosen scenario, averaged over about twenty years around the
  chosen year (2060 has only 2050-2060). **Today** (2025) is 2015-2034 with each model's two runs pooled,
  so it is the same under either scenario. It is a model climate with about 53 hot days a year against 45
  in the 2000-2024 record.
- **Four scores**, each 0 to 1, higher is better, on fixed scales so a hotter future lowers the score:
  soil protection (0.6 x bare days a year + 0.4 x a wind-erosion index; 0 bare days = 1, 150 = 0), water
  (irrigated: mm pumped, 0 = 1, 1,000 = 0; dryland: the share of the crops' water need that rain and
  stored water cover), money (return over direct costs per acre; irrigated -$100 = 0 and $500 = 1,
  dryland -$125 = 0 and $125 = 1) and risk (the share of planted crops that do not fail). `overall` is the
  mean with your weights (default soil 0.3, water 0.2, money 0.3, risk 0.2).
- **Holds up.** A plan holds up in a future when no more than 1 in 4 of its planted crops fails and it
  still covers its direct costs on average.
- **Ranking.** Half the average `overall` across the six models plus half the average of the two worst
  models, so a plan that is steady beats one that is good on average and bad in the hot, dry models.
  The top three differ from each other by more than one year in four; the third card can be well down the
  full list (`rank` says where).
- **Candidates** (`api/planner/candidates.py`): 105 cycles for irrigated fields, 17 for dryland. Rules: no
  winter wheat after winter wheat, no fallow after fallow, no soybeans after soybeans, alfalfa only as a
  stand of three or more years, at most two years in a row of corn or of sorghum. Dryland also: no
  alfalfa, no soybeans, at most one corn year in a cycle, winter wheat only after fallow. Irrigated: at
  most one fallow year in four. Cover crops are not candidates (no data). A field whose own pattern breaks
  a rule is still scored and told so gently.
- **The field decides** irrigated or dryland, soil erodibility, slope and its current pattern. Everything
  else is county-wide, so two dryland fields on the same soil get the same three plans.

**Soil-effect provider.** `SOIL_EFFECT_SOURCE=effects` (default) starts from the county's average bare
days per crop and previous crop and adds what Phase 2B measured between matched neighbours beyond what
the crops explain (`data/effects.json`, built by `make causal`). `SOIL_EFFECT_SOURCE=crop_average` uses
the county averages alone. When `effects.json` is missing, or holds fake data, the two give the same
answer.

Today one Phase 2B comparison is usable (irrigated corn-soy against continuous corn, +9.6 bare days, 30
pairs). It adds about 5 bare days to 14 corn-soy-type plans out of the 105 irrigated ones and moves no
dryland plan. On 300 dryland and 300 irrigated sampled fields, today and in 2060, the first plan is the
same with either source; on about half of the irrigated fields today the third card changes
(corn-soybeans gives way to continuous alfalfa). A comparison is used only when it is real data on bare
days, has at least 20 pairs for the field's practice, its interval excludes zero and its own fake-switch
placebo does not.

```bash
SOIL_EFFECT_SOURCE=crop_average make dev # county crop averages only, without the neighbour comparisons
IRRIGATION_LIMIT_MM=450 make dev         # a tighter irrigation limit (0 or none = no limit)
PLANNER_ENGINE=basic make dev            # force the old planner
```

Other settings (paths of the tables) are read in `api/planner/config.py`.

### API

`GET /field/{id}/plans?soil=&water=&money=&risk=&scenario=ssp245|ssp585&horizon=2025..2060&irrigated=true|false`
(all optional; priorities are any numbers of zero or more) and `GET /climate`. Without the climate tables
`/plans` answers with the old planner (`"engine": "basic"`, query ignored) and `/climate` says
`{"available": false}`. A bad value gives a 400 with one plain sentence. Full shape in `schema.md`.
Trimmed example, `/field/KS-013847/plans?money=5&scenario=ssp585&horizon=2060`:

```json
{"engine": "smart",
 "climate": {"label": "+2.0°C, models disagree on summer rain, crops need 8% more water, 26 more days above 35°C (95°F)",
             "yield_change": {"corn": {"pct": -40, "low": -56, "high": -25}}},
 "plans": [{"name": "Sorghum – Fallow (alternating)", "rank": 1,
            "scores": {"soil": 0.08, "water": 0.67, "money": 0.57, "risk": 0.77, "overall": 0.56},
            "futures": {"holds": 4, "total": 6},
            "numbers": {"bare_days": 130.6, "profit_per_acre": 18.0, "failed_share": 0.23}}],
 "candidates": 17}
```

### In the app

When the API answers with the smart planner, the field panel's "Rotation plans" section shows four sliders
(soil protection, water, money, risk), a "Which future?" slider from Today to 2060 with a middle-road /
high-emissions choice, and a line saying what changes in that future and how far the six models agree. An
irrigated / dryland switch applies to that field only. Each card shows the next four years, the score bars,
"Holds up in X of 6 futures", a strip of profit across the futures, four figures and one sentence. Under
the cards, "What this field grows now" scores the field's last four years the same way, and "How this is
scored" holds the rules above. Without the climate tables the controls are hidden and the cards look as in
Phase 1. Tests: `npm --prefix web run e2e:planner` (with `make dev` running).

### What is shaky

Read the numbers as a comparison between plans, not a forecast.

- **The dryland profit sign.** Dryland returns sit within about $40 an acre of zero, so "falls short of its
  direct costs" and "holds up in N of 6" flip with the assumptions (the price years, the cost of a fallow
  year, the hot-day cap). With 2015-2024 prices instead of 2020-2024 every dryland plan was short of its
  costs even today (sceptic review, before the fixes). The yield changes in `climate.yield_change` are the
  firmer part.
- **Heat and drought look alike in the record.** Hot years were dry years in 2000-2024 (correlation -0.8 to
  -0.9), so it cannot say how much of a bad year was heat and how much was water. The split leans on
  textbook values, and hot days rise in every model while rain does not.
- **Dryland wheat's heat loss is an assumption.** Fitted freely it came out near zero (0.04 % a day); the
  planner keeps the textbook 1 % a day as the cautious choice. The dryland answer in 2060 depends on it.
  Rerun with the fitted value on a typical dryland field (default priorities): wheat-fallow takes first
  place from wheat-sorghum-fallow in 2060 under both scenarios, and under `ssp585` it returns $16 an acre
  and holds up in 6 of 6 futures, against -$3 and 2 of 6 for wheat-sorghum-fallow with the textbook value.
- **Hot days beyond the record are capped.** Several model years have more than 85 hot days (one model
  averages 101 in 2050-2060 under `ssp585`). The cap removes a guess; it adds no knowledge of such years.
- **Models disagree on rain.** The range crosses zero in every window; they agree on warming and on more
  very hot days. Six models, one run each: the range is a spread, not a probability.
- **The weather is a reanalysis, not a station.** NASA POWER runs about 1.6 C warmer than the Garden City
  station and about 10 % wetter. Models are corrected to it, so levels carry the offset; changes do not.
  Rain days, dry spells and heat waves are the models' own; they have 4-12 % fewer days above 35 C than
  observed. One number for the whole county.
- **"Today" is a model climate** for 2015-2034, a little hotter than 2000-2024. It is the same under both
  scenarios.
- **The irrigation limit binds often.** The model's pumping need is above what K-State budgets (corn about
  500 mm against 381), so with 24 inches alfalfa goes short in 63 % of years today and corn in 27 %. A
  weakening well or a right for less than 24 inches is not modelled; set `IRRIGATION_LIMIT_MM`.
- **Irrigated plans always hold up.** All 105 hold up in 6 of 6 futures in every setting, so the badge
  says little for irrigated fields.
- **Money is not income.** Land, machinery and pivot ownership are not taken off (about $100 an acre
  dryland, $430-490 irrigated), no insurance payments or government payments are counted, prices and
  costs stay at today's level, and the cost of a fallow year ($14) is our rough, unverified estimate.
- **No rotation effect on yield** beyond stored soil water: no nitrogen credit after soybeans or alfalfa,
  no disease carry-over. Three agronomy rules stand in for the worst cases.
- **Close calls at the top.** On dryland the water, money and risk scores move almost together. Under
  `ssp585` in 2060 the first irrigated plan differs from today's for 227 of 300 sampled fields, by 0.006
  on the ranking score. Bare days do not change with the climate in the model; only the wind does.
- **Calibration is weak for some crops** (see the table): sorghum irrigated, soybeans dryland and alfalfa
  run on textbook numbers; the model's yields swing less than real ones and miss some bad years (for
  example the 2023 wheat abandonment).

## Phase 4

### Build-your-own rotation (item 1)

In the field panel, under the plan cards, "Try your own rotation" lets the farmer enter any rotation of
2 to 6 years: pick a crop per year, reorder the years (drag the handle, or the earlier / later buttons
for keyboard and touch), add or remove a year, type it ("corn, milo, wheat, fallow"), start from what the
field grows now, or press "Edit this plan" on one of our cards. It is scored by the same engine, with the
same field numbers, climate futures and priorities as the cards above it, and follows the sliders, the
future, the scenario and the irrigation switch (same debounce and stale-answer rule as the cards:
`web/src/hooks/useLatest.ts`).

What it shows: a table (Yours | Our 1 | 2 | 3) with the ranking score, the four scores, the futures it
holds up in, bare days, water, return per acre with its lowest and highest climate model, and failed
crops; then, in the API's own sentences, where yours would rank, how it compares with our first pick row
by row, under which single priority it would come first, and which of our agronomy rules it breaks
(worded "Our plans avoid ...").

```
GET /field/{id}/score?rotation=corn,sorghum,winter wheat,fallow   (+ the query parameters of /plans)
```

Code: `api/planner/custom.py` (parsing, normal form, comparison, words), `api/main.py` (endpoint),
`web/src/components/OwnRotation.tsx`, `web/src/lib/custom.ts`. Answer shape: schema.md.

How it is scored and compared:

- The rotation is a **cycle** (after the last year comes the first). It is reduced to its shortest
  repeating unit and a fixed starting year, so the same cycle typed from another year scores the same.
  When that is one of the planner's candidates it gets the candidate's own numbers and place; otherwise it
  is scored on its own and told where it would stand ("number 15 of 18").
- A row **names a winner only when the same side is ahead in every climate model**. When the models
  disagree it says "too close to call" and gives the count; when the two figures are equal at the
  precision shown it says they are the same. The marks in the table follow the same rule (a figure is
  marked unless another column is ahead of it in every model).
- "When would yours come first?" tries each priority on its own (that one is all that matters) and
  reports the place truthfully, also when the answer is "under none".

Limits:

- Only the six crops. Anything else (cotton, a cover crop) is refused with one sentence listing them.
- Everything that is county-level in the planner is county-level here; the limits listed under "How this
  is scored" apply unchanged (no yield effect of the previous crop beyond soil water, today's prices).
- A rotation our rules would never recommend can still score well, because the rules are agronomy the
  crop model cannot see (see Phase 3). Continuous corn under irrigation would be number 2 of 106 on
  KS-014376 today; the section says which rule it breaks rather than hiding the score.
- A rotation that plants no crop in any year (fallow every year) is **not rankable**. With nothing planted
  the engine's water and risk scores are 1.00, which once put it "first, ahead of all 17" on dryland. It
  now gets no rank, no comparison, no "would come first" sentence and no mark in its column
  (`yours.rankable: false`, `rank: null`); instead one sentence with its own figures says that it plants
  nothing, returns nothing (what the fallow costs) and leaves the soil bare about N days a year. The table
  shows "not ranked" and "no crop" where a score would mislead.
- On dryland the sign of the dollar figures depends on the price years and the fallow cost; the answer
  carries that sentence as a note.
- Cycles of 5 or 6 years are scored but can never be "one of ours": the planner compares 3- and 4-year
  cycles only. The Phase 2B rotation-type shift of a 5-year cycle is read from 12 years of it, which is
  not a whole number of rounds.
- Without the climate tables (basic planner) the endpoint answers 503 and the app hides the section.
- The rotation built for one field is forgotten when another field is opened.

Checks: `tests/test_custom_rotation.py`, `web/tests/custom.test.mjs`, `npm --prefix web run e2e:phase4`
(part of `make e2e`; compares the table, the marks and every sentence with a direct API call).

### Transition path (item 2)

A farm cannot switch overnight: next year's crop follows this year's. "How to get there", on each plan card
and under the build-your-own result, opens the year-by-year way from what the field grows now to that
rotation:

- a strip of years: the field's last three real years (greyed), then the path, bridge years marked, each
  year with its crop and its return an acre over direct costs (lowest to highest climate model under it);
- two sentences built from the numbers: what the switch costs short term and what it gains long term;
- against "carry on as now" (the field's current pattern continued): a small chart of the running total of
  the difference (bar = lowest to highest climate model, dot = their average, the payback year shaded) and
  a table with both returns, the difference and the running total for every year;
- behind two folds: water, bare days and the chance the crop fails for every year; and what is left out.

It follows the planner controls like the cards do (priorities, future, scenario, irrigation switch) and
can show 4 to 12 years (8 to start with). Nothing is asked of the server until a path is opened.

```
GET /field/{id}/transition?plan=1|2|3            (+ years=8 and the query parameters of /plans)
GET /field/{id}/transition?target=corn,soybeans  (the farmer's own rotation, 2 to 6 crops as typed)
```

Code: `api/planner/transition.py` (path search, year-by-year scoring, difference, words),
`Engine.crop_year` in `api/planner/smart.py` (one crop after one previous crop, per climate model),
`api/main.py` (endpoint), `web/src/components/TransitionPath.tsx`, `web/src/lib/transition.ts`. Answer
shape: schema.md.

How the path is found:

- It starts from the field's real last years (`crop_history`) and picks the year of the target cycle to
  start in. Every step from the last real crop onward is checked against the planner's rules in the form
  that involves the crops before it: no wheat, fallow or soybeans straight after itself; no third year in
  a row of corn or of sorghum; an alfalfa stand is not ended before its third year (the real stand's years
  count); on dryland no wheat straight after corn, sorghum or soybeans, no alfalfa, no soybeans and no
  corn within two years of corn; under irrigation no fallow within three years of fallow.
- When no year of the cycle can follow the field's last crops, up to two **bridge years** come first (in
  the county's fields today that is almost always a young alfalfa stand that has to reach its third year).
- Fewest bridge years first; among those, the start that returns the most over the years shown; then
  fewer bare days.
- A field whose last three years (or a whole cycle, if longer) already follow the rotation is **already
  there**: the answer says so and shows its next years. When nothing valid exists within two bridge years
  the answer says so and names the rules in the way.
- What the target rotation itself breaks every round (a farmer's own rotation may) is not held against
  the path; it is listed separately.

How it is scored:

- Each year is the engine's own figure for that crop after the crop before it, in the climate window of
  the chosen future (the same settings as the cards): return with the lowest and highest of the six
  climate models, water pumped or the share of water need rain covers, bare days, chance the crop fails.
- "Carry on as now": the last four real years repeat with period 1, 2 or 3 (or 4 when the years before
  confirm it); otherwise those four years are repeated as they are, and the answer says so.
- The difference is paired by climate model. The switch "has paid back" in the first year from which the
  running total stays at or above zero to the end of the span; otherwise the answer says it has not.
- The long-run difference compares the two as repeating cycles (the plan cards' own numbers) and names a
  winner on returns only when all six climate models agree; otherwise "too close to call" with the count.

Limits:

- Not modelled, and listed in every answer: equipment, storage, contracts, lease and insurance terms, the
  learning curve of a new crop; carry-over other than soil water (residue, weeds, disease, herbicide
  plant-back); runs of dry or wet years (every year gets the window's average weather, so this is not a
  forecast for 2027); price and cost changes; the weaker first year and the seeding cost of a new alfalfa
  stand.
- Two of the planner's rules are per cycle ("one corn year in a cycle" on dryland, "one fallow year in
  four" under irrigation). Year by year they are read as "no corn within two years of corn" and "no fallow
  within three years of fallow": our reading, stated here and in the module.
- "The start that returns the most over the years shown" depends on the span: with a fallow year in the
  cycle, a start that fits one more crop year into the span wins.
- About one field in eight ends its record with a crop the planner has no numbers for (grass/pasture,
  triticale, a double crop). There the answer is one sentence saying so, and no path. With fewer than
  four real years of the six crops there is a path but nothing to compare it with.
- Year-by-year bare days are the county averages for that crop after the crop before it; the long-run
  figure also carries the Phase 2B rotation-type shift where one stands out, as the cards do.
- On dryland the sign of the dollar figures depends on the price years and the fallow cost; the answer
  carries that sentence as a note.
- Without the climate tables (basic planner) the endpoint answers 503 and the app does not offer the
  button. An opened path belongs to one field and is closed when another field is opened.

Checks: `tests/test_transition.py` (each rule, entry year, bridge years, already there, impossible,
hand-checked arithmetic, the endpoint), `web/tests/transition.test.mjs`, `npm --prefix web run e2e:phase4`
(part of `make e2e`; compares the strip, the sentences, the chart and both tables with a direct API call).

### Whole-farm planner (item 3, backend)

Several fields planned together as one farm. Each field planned alone gets its own first pick, and on a
farm that puts every dryland field in fallow in the same year and every irrigated field at its thirstiest
together. The whole-farm planner picks a rotation AND a start year for every field at once so that the
farm as a whole stays inside two optional limits in every one of the next 12 years:

- a **water budget**: the most the whole farm may pump in any year, acre-inches;
- a **minimum yearly return** over direct costs, dollars (the request calls it `min_income`; it is a
  return over direct costs, not income or profit).

Among the plans that meet the limits it takes the one with the highest area-weighted ranking score (the
same score the single-field planner ranks by, for the same priorities). Staggering start years costs no
score, so it is used first; a field only leaves its first pick when the limits need it.

```
POST /farm/plan
{"field_ids": ["KS-013847", ...],            1 to 60 fields
 "weights": {"soil": 2, "water": 1, "money": 2, "risk": 1},   optional, as /plans
 "scenario": "ssp245", "horizon": 2050,      optional, as /plans
 "water_budget_acre_in": 1200,               optional
 "min_income": 20000,                        optional
 "irrigated": {"KS-013881": false}}          optional: overrides our irrigated/dryland guess per field
```

The answer (shape in schema.md) has: the plan for each field with whether it differs from that field's own
first pick and what the field gives up; farm totals for each of the 12 years (water and return: mean of
the six climate models, with the lowest and highest model); which limits bind; the same totals for "each
field planned alone" and whether that breaks the limits; the solver's status, optimality gap and time; and
sentences built from those numbers. 400 with one plain sentence for a bad request (more than 60 fields,
a field listed twice, a negative budget, ...), 404 for an unknown field, 503 on the basic planner.

Code: `api/planner/farm.py` (the model, the stages, the words), `api/main.py` (endpoint; the solve runs in
a worker thread so the map stays responsive). Needs `ortools` (in requirements.txt).

How it is solved:

- A mixed-integer program, one yes/no per field and plan (candidate cycle x start year), solved with
  Google OR-Tools: SCIP first, then CP-SAT from SCIP's best plan. Nothing is decided by a greedy pass, and
  "optimal" is only said when a solver proved it. On a 12-field farm the usual answer takes well under a
  second; the limit is about 10 seconds for a request, and an answer that hit it says so and gives the gap
  ("no plan that meets the limits can score more than 0.0037 above it").
- Fields of the same practice, soil erodibility and slope share one evaluation. A plan is dropped for a
  field when another is at least as good in score and in every year of every limit (exact). When each
  field's own first pick already meets the limits, that is the answer and nothing is solved.
- The limits use the mean of the six climate models. A year of a plan is that crop after the crop the
  cycle grew before it, in the climate window's average weather (the transition path's year numbers).
- When the limits cannot all be met the endpoint does not fail. It returns the closest plan and says which
  limit cannot be met and the best the farm can do: the least it can pump in its heaviest year, or the best
  it can return in its leanest year. The water budget is kept first and the return second; that order is
  our assumption (a well or a water right cannot be stretched) and the answer says so.
- A limit "binds" when it cannot be met, when each field planned alone would break it, or when dropping
  it would let the farm score higher than the solver's bound allows with it.
- With the rotations decided, the start years are chosen once more so that as many acres as possible keep
  the start year their plan card shows.

Tried on 8 dryland and 4 irrigated neighbours around KS-013847 (243 acres, today's climate, default
priorities): each field alone pumps up to 1,418 acre-inches in a year and returns between $13,900 and
$39,700; with a 1,200 acre-inch budget and a $20,000 minimum, two irrigated fields move from alfalfa-soybeans
to soybeans-wheat and four dryland fields start their wheat-sorghum-fallow in another year; the farm then
pumps at most 1,190 acre-inches and returns at least $20,000 every year, for a farm score of 0.639 instead
of 0.641 (proven optimal, 0.35 s).

Limits:

- "Each field planned alone" starts every field in year 1 of its rotation as the plan card shows it. The
  start year is not matched to what the field grew last; the per-field "How to get there" does that.
- Every year has to meet the limits on its own: no water or money is carried from one year to the next.
- Machinery, labour, storage, contracts, lease and insurance terms are not modelled; neither are runs of
  dry or wet years (every year gets the window's average weather, so year 3 is not a forecast for 2028).
- The limits are met on the average of the climate models; the lowest model of a year can fall under the
  minimum. The answer carries that range for every year.
- On a small farm with tight limits the solver can run to its time share without proving the optimum
  (the 12 neighbours with 1,300 acre-inches and $24,000: best plan found, gap 0.004). The answer says so.
- Farm dollars are rounded to $100 and water to whole acre-inches; "met" is decided on the unrounded totals.
- More than 60 fields is refused (400). The same request asked again gets the remembered answer.

Checks: `tests/test_farm.py` (the optimum against brute force on 60 little farms, each limit binding,
staggering against same-start, limits that cannot be met, determinism, the time-limit note, caps and bad
input, the endpoint, one real-data test), and a step in `npm --prefix web run e2e:phase4` that asks the
endpoint from inside the page and compares it with a direct call and with each field's `/plans`.

### Whole-farm planner (item 3, web): "Plan a farm"

The farm screen on top of `POST /farm/plan`. Nothing is worked out in the browser: every number and
sentence on it comes from that answer.

- **On the map.** "Plan a farm" in the title card turns farm mode on. In it a click adds a field to the
  farm or takes it out (farm fields are orange), and two more tools pick many at once: **Lasso** (draw a
  freehand loop) and **Box** (drag a rectangle). Both add every field whose centre is inside; the centre
  is the middle of the box around the field's outline. "Clear" empties the farm. Outside farm mode the
  map behaves as before. The fields are still one layer on one canvas renderer outside React
  (`web/src/components/FieldMap.tsx`); a change to the farm restyles only the fields that joined or left
  it, and the lasso line is drawn on its own element, so drawing never repaints the fields.
- **The farm panel** (in place of the field panel): the fields with acres and irrigated or dryland, total
  acres, the same priority sliders and future / scenario controls as the field view (one shared state),
  a water budget box (acre-inches a year) and a "least return in any year" box (dollars a year over
  direct costs; this is the request's `min_income`). Under each box is what the farm does with no limit
  (each field planned alone), from the answer. The plan is asked again 0.5 s after the last change; a
  newer request cancels the older one and a late answer for older inputs is dropped; while waiting, the
  last plan stays on screen dimmed under a "Working out the farm plan…" note.
- **The result**: the API's summary sentences; one line per limit (binds / does not bind / cannot be
  met, in the API's words); a table of the farm plan against "each field planned alone" (ranking score,
  water in the heaviest and in an average year, return in the leanest and in an average year, and
  whether each meets the limits); two small per-year charts (return, water: dot = average of the climate
  models, bar = lowest to highest model, tick = each field alone, dashed line = the limit, hollow dot =
  a year that misses it) with the same numbers as a table; and the plan for each field, marked when its
  rotation or start year differs from the field's own first pick, with the API's sentence on what that
  costs or saves. Clicking a field's row opens that field; "Back to the farm plan" returns.
- Limits that cannot be met are shown as the answer they are (amber, "Limits cannot all be met", the
  API's explanation and the closest plan), and a refused request (more than 60 fields) shows the API's
  one sentence. Neither is an error box.

Limits: the irrigated / dryland guess cannot be overridden per field from the farm panel (the API takes
`irrigated`, the screen does not send it); the farm is kept in this browser only (`localStorage`), not in
the link; the lasso was checked with a mouse, not on a touch screen; the climate line under "Which
future?" is read from the first field's `/plans` answer (it is the county's, the same for every field).

Code: `web/src/components/FarmPanel.tsx` + `Farm.css`, `web/src/lib/farm.ts` (pure helpers, unit tests in
`web/tests/farm.test.mjs`), `web/src/App.tsx` (farm mode), `web/src/components/FieldMap.tsx` (highlight,
lasso), `api.farmPlan` in `web/src/api.ts`. Browser checks: `npm --prefix web run e2e:phase4`
(screenshots `web/e2e/screenshots/phase4-farm*.png`).

### Twin view (item 4): "Compare with its twin"

A field next to its best-matched look-alike neighbour from Phase 2B, on one screen. It shows what was
measured, not a cause: a **difference between look-alike neighbours in the days the soil sits bare**. The
fake-switch placebo fails for Phase 2B and the crops grown explain the alfalfa differences, so no sentence
says a rotation caused or improved anything.

- **Endpoint** `GET /field/{id}/twin` (`api/twin.py`, built from `data/pairs.parquet`, `data/effects.json`,
  `data/causal_fields.parquet` and the soil table; 503 until `make causal` has run, 404 for an unknown field,
  200 with `has_twin: false` and a reason for a field in no pair). The twin is the field's pair with the
  smallest match score. The answer has both crop histories (2008-2024), both bare-days series (2016-2024,
  a year with no reading is `null`, a gap), what they were matched on and how close (each difference
  against its limit, the match score and the share of the matched pairs it beats), the pair's before / after
  means and the difference in change, the comparison it belongs to with its estimate and 95% interval
  from `effects.json`, how many of the other pairs in that comparison point the same way (counted from
  `pairs.parquet`), and the comparison's placebo result. Every sentence is written by the API. Shape in `schema.md`.
- **In the app.** The "Your neighbors" section has a "Compare with its twin" button. It opens a wide sheet
  over the bottom of the screen (the map stays above it, fitted to both fields): two columns with a row per
  year (crop and days bare, 2008-2024, rows aligned), one chart with both bare-days lines 2016-2024 and
  the 2020 cut-off marked (gaps stay gaps, the scale starts at zero), the pair's difference in change as a
  bar next to the middle half of the other pairs and the comparison's average with its interval, "N of M
  other pairs point the same way", and the placebo sentence. On the map this field stays blue and the twin
  is amber and dashed; clicking it opens it. Escape or Close closes the sheet. At phone width everything
  stacks. A single pair is noisy and the view says so next to its numbers.
- **Limits.** The twin is the field's closest match, not a typical neighbour, and the numbers of one pair swing
  widely (the "middle half" band shows by how much). "Points
  the same way" counts the sign of each pair's change only. The baseline limit ("within 25 days") is
  rebuilt from `causal_fields.parquet` and only quoted when it reproduces the stored match score; the
  year ranges come from `effects.json`. A field with no pair gets no button; its reason is the one the
  neighbours section gives.
- Code: `api/twin.py`, `web/src/components/TwinView.tsx` + `Twin.css`, `web/src/lib/twin.ts` (pure helpers,
  unit tests in `web/tests/twin.test.mjs`), a twin outline in `web/src/components/FieldMap.tsx`, the button in
  `Neighbors.tsx`. Tests: `tests/test_twin.py` (made-up tables; one real-data test), browser checks in
  `web/e2e/phase4.mjs` (screenshots `web/e2e/screenshots/phase4-twin*.png`).

### Map layers (item 5, backend)

County-wide values per field that the map can colour by. Four layers, all from our own tables and the planner:

| layer | what | from |
|---|---|---|
| `bare_days` | days the soil sat bare, per year 2016-2024, whole days; a field-year with no reading stays empty | `data/soil_health.parquet` |
| `erosion_risk` | the planner's wind erosion index (`api/planner/erosion.py`) per year: that year's measured bare days x the field's wind erodibility x a slope factor x that year's observed wind-erosion climate | soil table, `data/field_traits.parquet`, `data/climate_history.parquet`, field slope |
| `rotation_change` | one class per field for the default priorities and today's climate: same plan / among our top picks / our first pick does not score higher / a little higher but within the spread between climate models / clearly higher / cannot tell; with the score gap | the planner, exactly as `GET /field/{id}/plans` with no parameters |
| `irrigation` | irrigated or dryland (our best guess), with the probability in whole percent | `data/field_traits.parquet` |

- **Endpoints** (`api/layers.py`). `GET /layers` lists the layers: status, years, unit, class breaks, a method
  note and a caveat each (2.5 kB). `GET /layers/{name}` (optionally `?year=2020`) sends the values as
  arrays in the order of the `features` of `GET /fields` (`values[i]` belongs to `features[i]`;
  `field_count` and `fields_etag` let a client check), plus per year a county summary: median, quartiles,
  fields and acres per class with their shares, fields with no value. Whole numbers on the wire
  (`value_scale` says what one unit is), gzipped once, with an ETag (304 when unchanged). Sizes for the
  county: 75 kB for nine years of bare days, 89 kB for nine years of erosion risk, 11 kB for the rotation layer. Shapes in `schema.md`.
- **Class breaks come from the data**: the 20th, 40th, 60th and 80th percentile of every field-year pooled,
  each moved to the nearest round number, so one scale serves every year (today: 1 / 20 / 50 / 100 bare
  days; 0.002 / 0.05 / 0.2 / 0.3 for the erosion index). The web app must take them from the API.
- **Rotation change** runs the planner for all 7,255 fields (about 3 s) once, in a background thread, when
  the layers are first asked for; until then `GET /layers/rotation_change` answers 202 with
  `"status": "building"` and a progress count. The result is cached in `data/layers_cache/rotation_change.json`
  under a key made of the planner's table versions, so a restart reads it back; when a planner table or
  setting changes it is rebuilt. "Within the spread" means the gap between our first pick's ranking score
  and the current pattern's is no bigger than how far either one's score moves across the six climate
  models (highest minus lowest); "clearly higher" means it is bigger.
- **Missing files.** A layer whose table is missing answers `"status": "unavailable"` with a reason; the
  others still work (no traits table: no irrigation and no erosion layer; no climate tables: no rotation
  layer; the made-up demo fields only get the rotation layer).
- **Limits.** The erosion layer uses single years of the wind climate, which are spiky (2022 sits at the
  cap of 3 times the average) and which the planner itself never uses singly: read a year as that
  year's weather on that year's cover, and it is a relative index, not tons of soil. Bare days are a
  satellite estimate; 2016 has 295 fields with no reading. The rotation layer is for the default
  priorities only, and its gap is a model score, not a measured gain; 20% of fields have no four-year
  pattern of the six crops and cannot be compared; 2% follow a pattern outside our candidate list that
  scores as high as our first pick. The irrigation layer is a guess, not a record of wells. The map screen is
  described in the next section; this part is the API (browser suite `e2e:phase4` checks it against `/fields`, `/field/{id}/soil`
  and `/field/{id}/plans`; unit tests in `tests/test_layers.py`).

### Map layers and time-lapse (item 5, web)

A "Colour fields by" card at the bottom left of the map: Fields (as before), Bare days, Wind erosion risk,
Rotation change recommended, Irrigated or dryland. The one canvas layer of fields is restyled in place (only the
fields whose colour changed are touched, nothing is rebuilt), so switching layer or year is instant after the
first load of a layer (every year comes in one answer, `GET /layers/{name}`).

- **Year slider 2016-2024** with play / pause (one year a second; stops at the last year, when the slider is
  dragged or another layer is chosen; play from the last year starts again at the first). For the rotation and
  irrigation layers, which have one value per field, the slider and play are disabled with a one-line reason.
- **Legend** from the API: class labels, unit, the layer's caveat, and for the wind layer that year's wind factor.
  Class breaks are never written in the web app. Colours are ours: purple (bare days) and teal (wind) get lighter or
  darker in one direction, rose (rotation change) the same, ochre / teal for dryland / irrigated, and
  "no value" is a neutral grey that no scale uses. The lightness steps are tested (`web/tests/layers.test.mjs`).
- **Numbers for the view**: the same table gives, for the fields with any part on the map and for the whole
  county, fields, acres, median and middle half (number layers) and the count and share of every class. "In view"
  is computed in the browser from the layer arrays and the field outlines, 150 ms after the map stops moving;
  the county column is the API's own summary. Shares are of the fields with a value (bare days, wind) or of all
  fields (rotation, irrigation), worked out from the counts (the API's shares are rounded to three decimals, and
  rounding those again can be 1% off); acres are acres of the fields counted. Under each class of the rotation
  and irrigation layers: the API's county median of the score gap or of the chance the field is irrigated.
- **Kept**: layer and year stay when a field is opened or closed and in farm mode; the selected field, picked farm
  fields and the twin keep their outlines (blue, orange, dashed amber) over the layer's fill.
- **Checks**: a layer is used only if its `field_count` (and, when the browser can read it, `fields_etag`) agrees
  with the fields on the map; otherwise the card says so instead of colouring the wrong fields.
- Limits: the phone starts with the legend folded; a field partly in view counts as in view; the layer is
  not in the page address (a reload goes back to Fields). See `data/logs/phase4/item_5.md`.
