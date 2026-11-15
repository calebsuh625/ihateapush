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

## Phase 2B: rotation effects from look-alike neighbours

```bash
make causal                                  # ~10 s; log in data/causal.log
CAUSAL_OUTCOME=bare_soil_index make causal   # analyse another soil column, no code change
```

Each field's 2016–2024 crops are classed as a rotation type, irrigated vs dryland is inferred from its
shape and 2008–2015 crops, and fields are paired 1:1 with a neighbour under 5 km away with similar slope,
mapped soil, starting soil score and 2008–2015 crops but a different rotation since 2016. Effects are
difference-in-differences on the satellite soil score with spatial-cluster bootstrap CIs, plus two
placebos (fake switch years inside 2008–2015; rotation labels shuffled and everything re-matched).
Outputs and their columns are in `schema.md`. Until Phase 2A's `data/soil_health.parquet` exists the run
uses made-up soil scores (`data/soil_health_fake.parquet`) and every output and the app say so.
