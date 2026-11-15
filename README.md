# Crop rotation decision-support tool

NASA Space Apps project. Long-term goal: use satellites to measure soil health, compare neighbouring
fields to learn which crop rotations actually improve soil, and recommend climate-resilient rotations.

**Phase 1 (this repo):** a working skeleton on real data for **Finney County, Kansas** (FIPS 20055).
Open the map, click a field, and see its real 2008–2024 crop history, soil organic matter, slope, and
the top three rotation plans from a simple rule-based planner.

```
pipeline/   data scripts; run_all.py builds data/fields.parquet from public USDA / USGS data
api/        FastAPI backend (main.py) + rotation planner (planner.py)
web/        Vite + React + TypeScript + react-leaflet frontend
data/       data files (git-ignored); downloads are cached in data/raw/
tests/      pytest tests for the pipeline, API and planner
schema.md   the data contract for data/fields.parquet
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
| Organic matter | SSURGO via USDA Soil Data Access; surface-horizon `om_r`, area-weighted per field | `pipeline/soil.py` |
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

## The API

Base URL `http://127.0.0.1:8010` (interactive docs at `/docs`).

| endpoint | returns |
|----------|---------|
| `GET /health` | which data file is loaded and how many fields |
| `GET /fields` | GeoJSON FeatureCollection of all fields (`field_id`, `area_ha`, simplified outline) |
| `GET /field/{id}` | one field: outline, `crop_history` as an object, organic matter, slope, area, and stats (distinct crops, longest same-crop streak, % of years in corn) |
| `GET /field/{id}/plans` | the planner's top 3 four-year rotations with soil / water / money / overall scores and a one-line reason |
| `GET /effects` | Phase 2B: `data/effects.json` (503 until `make causal` has run) |
| `GET /field/{id}/neighbors` | Phase 2B: the field's matched look-alike neighbours, their crops and soil timelines |

On startup the API loads `data/fields.parquet` if it exists, otherwise `data/fields_fake.parquet`,
logs which one, and validates it against `schema.md`; a file that does not match stops the server
with a readable list of problems. Set `FIELDS_PATH=/some/file.parquet` to load a specific file.

The planner lives in `api/planner.py` behind one function, `rank_plans(field) -> list[Plan]`.
To try a smarter model, write a function with that signature and pass it to `create_app(planner=...)`
in `api/main.py`.

## The data contract

`schema.md` defines `data/fields.parquet`. It only changes on purpose: the pipeline validates
against it before writing and the API validates on startup (`pipeline/schema.py` is the validator).

## Phase 2B: rotation effects from look-alike neighbours

> **⚠️ FAKE SOIL DATA.** Every Phase 2B number on this branch comes from **made-up soil scores**
> (`data/soil_health_fake.parquet`) with crop effects planted on purpose. They show the method recovers
> known effects; they are **not findings**. Re-run `make causal` once Phase 2A's real
> `data/soil_health.parquet` is in place; no code changes are needed.
>
> | Comparison (fake data) | Pairs | Estimate [95% CI] | Planted |
> |---|---|---|---|
> | alfalfa/perennial vs continuous corn | 112 | +0.095 [+0.067, +0.124] | +0.089 |
> | alfalfa/perennial vs wheat-sorghum-fallow | 81 | +0.151 [+0.122, +0.179] | +0.154 |
> | diverse vs wheat-sorghum-fallow | 450 | +0.021 [+0.016, +0.028] | +0.018 |
> | corn-soy vs continuous corn | 53 | -0.018 [-0.033, +0.004] (not distinguishable from 0) | -0.015 |
>
> Placebos (also on fake data): shuffled labels centre on 0; fake switch years 2011-2013 flag 3 of 27
> alfalfa estimates (+0.01 to +0.02), likely because matching allows up to 2 alfalfa years before 2016.

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
