# Crop rotation decision-support tool (NASA Space Apps)

Long-term goal: use satellites to measure soil health, compare neighbouring fields to learn which crop
rotations actually improve soil, and recommend climate-resilient rotations.

**Phase 1 (this repo today):** a working skeleton on real data for Finney County, Kansas (FIPS 20055):
map → click a field → see its real crop history, soil info and top 3 rotation plans.

## Layout

- `pipeline/` — data scripts. `run_all.py` rebuilds `data/fields.parquet` from public USDA/USGS data;
  `make_fake_fields.py` writes the 30-field safety net `data/fields_fake.parquet`;
  `schema.py` is the validator for the data contract.
- `api/` — FastAPI backend (`api/main.py`) and the rotation planner (`api/planner.py`,
  entry point `rank_plans(field) -> list[Plan]`).
- `web/` — Vite + React + TypeScript + react-leaflet frontend.
- `data/` — data files, git-ignored. Downloads are cached in `data/raw/`.
- `tests/` — pytest tests for the API, planner and pipeline helpers.
- `schema.md` — the data contract for `data/fields.parquet`.

## Rules

- **`schema.md` only changes on purpose.** It is the contract between pipeline, API and frontend.
  If it must change, update `pipeline/schema.py`, the API and the frontend in the same commit.
- The six core crop names are fixed: `corn`, `soybeans`, `winter wheat`, `sorghum`, `alfalfa`, `fallow`.
- Cache every download under `data/raw/`; never re-download what is already there.
- Do not push; the owner pushes.

## How to run

```bash
make setup      # .venv + pip install + npm install
make fake       # data/fields_fake.parquet (instant)
make pipeline   # data/fields.parquet from real data (python pipeline/run_all.py)
make dev        # API on :8010 + web on :5173
make test       # pytest
```

Python is always `.venv/bin/python`, run from the repo root (`api` and `pipeline` are packages).
The API loads `data/fields.parquet` if present, otherwise `data/fields_fake.parquet`.
See README.md for the full commands.
