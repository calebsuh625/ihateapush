PY := .venv/bin/python
API_PORT ?= 8010
WEB_PORT ?= 5173

.PHONY: help setup fake pipeline satellite soil-obs soil-health causal validate climate nass field-traits calibrate planner-sanity groundwater api web dev test e2e

help:
	@echo "make setup     create .venv, install Python + web dependencies"
	@echo "make fake      write data/fields_fake.parquet (30 fake fields, instant)"
	@echo "make pipeline  build data/fields.parquet from real USDA/USGS data"
	@echo "make satellite build data/field_satellite.parquet (Landsat via Earth Engine; needs 'earthengine authenticate')"
	@echo "make soil-obs  bare-soil satellite views for every field (Earth Engine, about 30 EECU-hours; see README)"
	@echo "make soil-health  build data/soil_health.parquet from those views (local, no network)"
	@echo "make causal    rotations compared on matched neighbours (bare days) -> data/effects.json, data/pairs.parquet"
	@echo "make climate   Phase 3 future-weather tables (Earth Engine, about 2.4 EECU-hours the first time; cached after)"
	@echo "make nass      Phase 3 USDA crop statistics -> data/nass_finney.parquet (first run streams a 1.1 GB file)"
	@echo "make field-traits  Phase 3 per-field irrigation guess, wind erodibility, OpenET water use (about 0.07 EECU-hours)"
	@echo "make calibrate fit the crop response to NASS yields -> api/planner/crop_response.json (local, seconds)"
	@echo "make planner-sanity  run the planner's checks on real fields -> data/planner_sanity.md"
	@echo "make groundwater  Phase 4 measured groundwater levels (USGS + Kansas Geological Survey; small, cached) -> data/groundwater.parquet"
	@echo "make validate  check the active data file against schema.md and summarise it"
	@echo "make dev       start API (:$(API_PORT)) and web (:$(WEB_PORT)) together"
	@echo "make api       start only the API"
	@echo "make web       start only the web dev server"
	@echo "make test      run the Python tests and the web unit tests"
	@echo "make e2e       click real fields in a headless browser (needs 'make dev' running)"

setup:
	@if command -v uv >/dev/null 2>&1; then \
		uv venv --allow-existing --python 3.12 .venv && uv pip install --python $(PY) -r requirements.txt; \
	else \
		python3 -m venv .venv && $(PY) -m pip install --upgrade pip && $(PY) -m pip install -r requirements.txt; \
	fi
	npm --prefix web install

fake:
	$(PY) pipeline/make_fake_fields.py

pipeline:
	$(PY) pipeline/run_all.py

# Optional: per-field yearly greenness from Landsat. Needs a free Google Earth Engine login (see README).
satellite:
	$(PY) pipeline/satellite.py

# Optional (Phase 2A): yearly soil table. soil-obs asks Earth Engine and is expensive on the free tier;
# run `$(PY) -m pipeline.soil.bare_soil --estimate` first. soil-health is local.
soil-obs:
	$(PY) -m pipeline.soil.bare_soil

soil-health:
	$(PY) -m pipeline.soil.build

# Phase 2B. Progress goes to data/causal.log. CAUSAL_OUTCOME=<soil column> picks the outcome
# (default bare_days).
causal:
	$(PY) -m pipeline.causal.run_causal > data/causal.log 2>&1 \
		&& grep '^\[' data/causal.log | cut -c1-200 | tail -4 \
		|| (tail -5 data/causal.log; exit 1)

# Phase 3 (smart planner). Order: nass, climate, field-traits, calibrate. Without these files the app
# runs the old planner. Run `$(PY) -m pipeline.climate.build --estimate` and
# `$(PY) -m pipeline.field_traits --estimate` first; the Earth Engine downloads are cached under data/raw/.
climate:
	@mkdir -p data/logs
	$(PY) -m pipeline.climate.build > data/logs/climate_build.log 2>&1 \
		&& tail -3 data/logs/climate_build.log \
		|| (tail -5 data/logs/climate_build.log; exit 1)

nass:
	$(PY) -m pipeline.nass

field-traits:
	$(PY) -m pipeline.field_traits

# Local, needs data/nass_finney.parquet and the climate tables. Rewrites the tracked crop_response.json.
calibrate:
	$(PY) -m api.planner.calibrate

planner-sanity:
	$(PY) -m api.planner.sanity

# Phase 4 water reality check: depth to water in wells in and next to the county, from the USGS Water Data
# API and the Kansas Geological Survey's WIZARD database. Seven small requests the first time, cached under
# data/raw/nwis/ and data/raw/kgs/; later runs use the cache. Optional: without it the app shows no water outlook.
groundwater:
	$(PY) -m pipeline.groundwater

validate:
	$(PY) pipeline/schema.py

# The API falls back to the fake fields, so make sure at least those exist.
data/fields_fake.parquet:
	$(PY) pipeline/make_fake_fields.py

api: data/fields_fake.parquet
	$(PY) -m uvicorn api.main:app --reload --reload-dir api --reload-dir pipeline --port $(API_PORT)

web:
	API_PORT=$(API_PORT) npm --prefix web run dev -- --port $(WEB_PORT) --strictPort

# Both servers in one terminal; Ctrl-C stops both.
dev: data/fields_fake.parquet
	@trap 'kill 0' INT TERM EXIT; \
	$(PY) -m uvicorn api.main:app --reload --reload-dir api --reload-dir pipeline --port $(API_PORT) & \
	API_PORT=$(API_PORT) npm --prefix web run dev -- --port $(WEB_PORT) --strictPort & \
	wait

test:
	$(PY) -m pytest -q
	npm --prefix web test

# Needs `make dev` running in another terminal. Downloads headless Chromium on first use.
# Runs every browser suite, one after the other; logs go to data/logs/e2e_*.log, one summary line each.
e2e:
	@cd web && npx playwright install chromium > /dev/null 2>&1 || true
	@mkdir -p data/logs
	@fail=0; for s in e2e e2e:states e2e:planner e2e:phase4; do \
		log=data/logs/e2e_$$(echo $$s | tr ':' '_').log; \
		if WEB_URL=http://localhost:$(WEB_PORT) npm --prefix web run $$s --if-present > $$log 2>&1; then \
			echo "$$s: $$(grep -E 'PASS|FAIL' $$log | tail -1)"; \
		else \
			echo "$$s: FAILED (see $$log)"; grep -E 'FAIL|Error' $$log | head -5; fail=1; \
		fi; \
	done; exit $$fail
