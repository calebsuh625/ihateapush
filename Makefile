PY := .venv/bin/python
API_PORT ?= 8010
WEB_PORT ?= 5173

.PHONY: help setup fake pipeline satellite validate api web dev test e2e

help:
	@echo "make setup     create .venv, install Python + web dependencies"
	@echo "make fake      write data/fields_fake.parquet (30 fake fields, instant)"
	@echo "make pipeline  build data/fields.parquet from real USDA/USGS data"
	@echo "make satellite build data/field_satellite.parquet (Landsat via Earth Engine; needs 'earthengine authenticate')"
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
e2e:
	cd web && npx playwright install chromium
	WEB_URL=http://localhost:$(WEB_PORT) npm --prefix web run e2e
