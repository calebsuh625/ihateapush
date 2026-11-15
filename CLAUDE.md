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
- `api/` — FastAPI backend (`api/main.py`) and the rotation planner (`api/planner.py`,
  entry point `rank_plans(field) -> list[Plan]`).
- `web/` — Vite + React + TypeScript + react-leaflet frontend.
- `data/` — data files, git-ignored. Downloads are cached in `data/raw/`.
- `tests/` — pytest tests for the API, planner and pipeline helpers.
- `schema.md` — the data contracts for `data/fields.parquet` and `data/soil_health.parquet`.

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
- `make soil-obs` costs about 30 EECU-hours for the county. Run `--estimate` first, use
  `--max-eecu-hours`, and bump `METHOD_VERSION` in `soil/bare_soil.py` when the method changes.
- Do not push; the owner pushes.

## How to run

```bash
make setup      # .venv + pip install + npm install
make fake       # data/fields_fake.parquet (instant)
make pipeline   # data/fields.parquet from real data (python pipeline/run_all.py)
make satellite  # optional: data/field_satellite.parquet (needs Earth Engine login; ~18 min first time)
make soil-obs   # optional: bare-soil views via Earth Engine (about 30 EECU-hours for the county)
make soil-health  # optional: data/soil_health.parquet from those views (local)
make dev        # API on :8010 + web on :5173
make test       # pytest + web unit tests
make e2e        # with `make dev` running: headless browser clicks real fields
```

Python is always `.venv/bin/python`, run from the repo root (`api` and `pipeline` are packages).
The API loads `data/fields.parquet` if present, otherwise `data/fields_fake.parquet`
(`FIELDS_PATH=<file>` overrides). Port 8000 is avoided on purpose; the API uses 8010.
See README.md for the full commands.
