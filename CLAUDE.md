# Crop rotation decision-support tool (NASA Space Apps)

Long-term goal: use satellites to measure soil health, compare neighbouring fields to learn which crop
rotations actually improve soil, and recommend climate-resilient rotations.

**Phase 1 (this repo today):** a working skeleton on real data for Finney County, Kansas (FIPS 20055):
map → click a field → see its real crop history, soil info and top 3 rotation plans.

## Layout

- `pipeline/` — data scripts. `run_all.py` rebuilds `data/fields.parquet` from public USDA/USGS data
  (`fields.py` + `csb.py` + `cdl.py`: outlines and crop history; `soil/ssurgo.py`: SSURGO organic matter;
  `slope.py` + `dem.py`: slope). `make_fake_fields.py` writes the 30-field safety net
  `data/fields_fake.parquet`; `schema.py` is the validator for the data contract.
  `satellite.py` builds the optional side file `data/field_satellite.parquet` (per-field, per-year
  Landsat greenness via Google Earth Engine); its contract is `satellite_schema.py`.
  `soil/` also holds Phase 2A, the yearly soil table `data/soil_health.parquet`: `bare_soil.py`
  (Earth Engine bare-soil views), `build.py` (model, noise analysis, verdict; uses `features.py`,
  `model.py`, `confidence.py`, `exposure.py`, `noise.py`, `report.py`), `health_schema.py` (validator).
- `pipeline/causal/` — Phase 2B: how many days a year fields sit bare under each rotation type,
  compared between matched neighbouring fields (`run_causal.py`, `make causal`) → `data/effects.json`,
  `data/pairs.parquet`, `data/causal_fields.parquet` (see schema.md). Runs on the real
  `data/soil_health.parquet`; outcome `bare_days` (lower is better), cut-off year 2020; settings in
  `config.py`. Served by `api/causal.py`; shown in the "Your neighbors" panel section.
- `pipeline/climate/` — Phase 3 future weather: six NEX-GDDP-CMIP6 models × two scenarios, corrected
  to NASA POWER observations (`build.py`, `make climate`) → `data/climate_futures.parquet`,
  `climate_history.parquet`, `climate_monthly.parquet`. `pipeline/nass.py` → `data/nass_finney.parquet`
  (yields, acres, prices); `pipeline/field_traits.py` → `data/field_traits.parquet` (irrigated or
  dryland, wind erodibility, OpenET water use).
- `api/` — FastAPI backend (`api/main.py`) and the rotation planner package `api/planner/`.
  `legacy.py` is the Phase 1 planner (`rank_plans(field) -> list[Plan]`, still the fallback).
  `smart.py` is the Phase 3 planner (`rank_plans_smart`): candidate rotations (`candidates.py`) scored
  in six climate futures on soil, water, money and risk (`response.py`, `water.py`, `soil_effect.py`,
  `erosion.py`, `money.py`, `futures.py`); settings in `config.py`; fitted crop response in the tracked
  `crop_response.json` (`calibrate.py`), prices and costs in the tracked `economics.json`.
- `web/` — Vite + React + TypeScript + react-leaflet frontend.
- `data/` — data files, git-ignored. Downloads are cached in `data/raw/`.
- `tests/` — pytest tests for the API, planner and pipeline helpers.
- `schema.md` — the data contracts for `data/fields.parquet`, `data/soil_health.parquet`, the Phase 3
  tables and the planner's API answers.

## Rules

- **`schema.md` only changes on purpose.** It is the contract between pipeline, API and frontend.
  If it must change, update `pipeline/schema.py`, the API and the frontend in the same commit.
- The six core crop names are fixed: `corn`, `soybeans`, `winter wheat`, `sorghum`, `alfalfa`, `fallow`.
- Cache every download under `data/raw/`; never re-download what is already there.
- Satellite numbers stay in the side file, not in `fields.parquet`. The free Earth Engine tier is
  150 EECU-hours a month and one full `make satellite` run costs about 13, so test with
  `--max-fields 50 --years 2015-2016` and bump `METHOD_VERSION` in `satellite.py` when the method changes.
- In `data/soil_health.parquet`, `om_pred` cannot show soil change (year-to-year noise 0.14 points
  against 0.1–0.3 points per decade of real change). Compare rotations with `bare_days`, within the
  same year. Details in README "Soil over time".
- Phase 2B measures bare days, not soil health. The only differences that stand out are the alfalfa
  ones, and the crops grown explain them; its fake-switch placebo fails. Do not write that a rotation
  "improved the soil" from it. Details in README "Phase 2B".
- Phase 3 planner: the soil score is soil protection (fewer bare days, lower wind-erosion risk), never
  "soil health". Dollar figures are returns over direct costs, not profit or income; on dryland their
  sign flips with the price years and the fallow cost, so quote the yield changes instead. The climate
  models agree on heat but not on rain; do not write "less rain" unless 5 of 6 agree.
- The soil-effect source is one setting, `SOIL_EFFECT_SOURCE=effects|crop_average` (default
  `effects`: county crop averages plus the Phase 2B numbers from `data/effects.json`; without that
  file it scores like `crop_average`).
- Without the climate tables the API answers with the Phase 1 planner (`"engine": "basic"`); keep that
  fallback and `tests/test_planner.py` working.
- `make climate` costs about 2.4 EECU-hours once (cached in `data/raw/climate/`); `make field-traits`
  about 0.07. Run `python -m pipeline.climate.build --estimate` first.
- `make soil-obs` costs about 30 EECU-hours for the county. Run `--estimate` first, use
  `--max-eecu-hours`, and bump `METHOD_VERSION` in `soil/bare_soil.py` when the method changes.
- **Regions:** `REGION=<county FIPS>` (default 20055 Finney) picks the county (`pipeline/region.py`).
  Finney's files stay in `data/`; other counties use `data/regions/<fips>/`. Build paths from
  `pipeline.region.DATA_DIR` (or `common.DATA_DIR`), never `ROOT / "data"`. Never run `make causal` for a
  county without its own soil table. See README "Other counties".
- Do not push; the owner pushes.

## How to run

```bash
make setup      # .venv + pip install + npm install
make fake       # data/fields_fake.parquet (instant)
make pipeline   # data/fields.parquet from real data (python pipeline/run_all.py)
make satellite  # optional: data/field_satellite.parquet (needs Earth Engine login; ~18 min first time)
make soil-obs   # optional: bare-soil views via Earth Engine (about 30 EECU-hours for the county)
make soil-health  # optional: data/soil_health.parquet from those views (local)
make causal     # data/effects.json + data/pairs.parquet (log: data/causal.log)
make nass       # Phase 3: data/nass_finney.parquet (first run streams a 1.1 GB USDA file)
make climate    # Phase 3: future-weather tables (Earth Engine, about 2.4 EECU-hours once)
make field-traits  # Phase 3: data/field_traits.parquet (Earth Engine, about 0.07 EECU-hours)
make calibrate  # Phase 3: refit api/planner/crop_response.json (local, seconds)
make planner-sanity  # Phase 3: planner checks on real fields -> data/planner_sanity.md
make dev        # API on :8010 + web on :5173
make test       # pytest + web unit tests
make e2e        # with `make dev` running: headless browser clicks real fields
```

Python is always `.venv/bin/python`, run from the repo root (`api` and `pipeline` are packages).
The API loads `data/fields.parquet` if present, otherwise `data/fields_fake.parquet`
(`FIELDS_PATH=<file>` overrides). Port 8000 is avoided on purpose; the API uses 8010.
See README.md for the full commands.
