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

```
pipeline/   data scripts; run_all.py builds data/fields.parquet from public USDA / USGS data
api/        FastAPI backend (main.py) + rotation planner (planner.py)
web/        Vite + React + TypeScript + react-leaflet frontend
data/       data files (git-ignored); downloads are cached in data/raw/
tests/      pytest tests for the pipeline, API and planner
schema.md   the data contracts for data/fields.parquet and data/soil_health.parquet
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
| `GET /field/{id}/plans` | the planner's top 3 four-year rotations with soil / water / money / overall scores and a one-line reason |
| `GET /field/{id}/satellite` | per-year `ndvi_peak`, `cover_frac` and `n_obs`, plus averages and the trend per decade; `"available": false` with a reason if the satellite file has not been built |
| `GET /field/{id}/soil` | per-year `om_pred`, `confidence`, `n_obs` and `bare_days` for 2016–2024, the soil-survey value, which signal to trust, the typical noise and a plain verdict; `"available": false` with a reason if the soil table has not been built |
| `GET /effects` | Phase 2B: `data/effects.json` (503 until `make causal` has run) |
| `GET /field/{id}/neighbors` | Phase 2B: the field's matched look-alike neighbours, their crops and soil timelines |

On startup the API loads `data/fields.parquet` if it exists, otherwise `data/fields_fake.parquet`,
logs which one, and validates it against `schema.md`; a file that does not match stops the server
with a readable list of problems. Set `FIELDS_PATH=/some/file.parquet` to load a specific file.

The satellite file is optional: `data/field_satellite.parquet` is loaded when the real fields file
is in use (`SATELLITE_PATH=/some/file.parquet` overrides). If it is missing or invalid the server
logs one line and runs without it. The soil table works the same way: `data/soil_health.parquet`
and `data/soil_health_meta.json` (`SOIL_HEALTH_PATH` and `SOIL_HEALTH_META_PATH` override).

The planner lives in `api/planner.py` behind one function, `rank_plans(field) -> list[Plan]`.
To try a smarter model, write a function with that signature and pass it to `create_app(planner=...)`
in `api/main.py`.

## The data contract

`schema.md` defines `data/fields.parquet` and `data/soil_health.parquet`. It only changes on purpose:
the pipeline validates against it before writing and the API validates on startup
(`pipeline/schema.py` and `pipeline/soil/health_schema.py` are the validators).

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
