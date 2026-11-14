# Data contracts

**This file is the contract between the pipeline, the API and the frontend. It only changes on purpose** —
if you change it, change the validator, the API and the frontend in the same commit.

Two tables: [`data/fields.parquet`](#datafieldsparquet) (one row per field) and
[`data/soil_health.parquet`](#datasoil_healthparquet) (one row per field per year).

# `data/fields.parquet`

Validator: `pipeline/schema.py`.

GeoParquet, CRS **EPSG:4326**, **one row per field**. `data/fields_fake.parquet` follows the same contract.

| column               | type            | null? | meaning |
|----------------------|-----------------|-------|---------|
| `field_id`           | string          | no    | Unique id, two-letter state + 6 digits: `"KS-000123"` |
| `geometry`           | Polygon         | no    | Field outline. Single `Polygon` only (no MultiPolygon), valid, lon/lat degrees |
| `crop_history`       | string (JSON)   | no    | JSON object, year → lowercase crop name, e.g. `{"2008":"corn","2009":"soybeans"}` |
| `organic_matter_pct` | float           | yes   | Soil organic matter %, surface horizon, area-weighted over the field (SSURGO `om_r`) |
| `slope_pct`          | float           | yes   | Mean terrain slope over the field, percent rise (100 × rise/run) |
| `area_ha`            | float           | no    | Field area in hectares, computed in an equal-area projection |

## Rules

- `field_id` matches `^[A-Z]{2}-\d{6}$` and is unique.
- `crop_history` is a JSON **string** in the file (the API turns it into a real object).
  - Keys are 4-digit years as strings (`"2008"`). Phase 1 covers 2008–2024. A year with no usable
    observation is simply omitted (never `null`, never `""`).
  - Values are lowercase crop / land-cover names with no leading or trailing spaces.
- `organic_matter_pct` is in `[0, 100]`; `slope_pct` is `>= 0`; `area_ha` is `> 0`.
- `organic_matter_pct` and `slope_pct` may be null **only** when the source has no data for that field
  (e.g. a SSURGO map unit with no horizon data). Consumers must handle null. Everything else is non-null.

## Crop names

Six "core" names are fixed because the planner and the frontend key off them:

`corn` · `soybeans` · `winter wheat` · `sorghum` · `alfalfa` · `fallow`

Any other value is the lowercase USDA Cropland Data Layer class name, lightly normalised
(e.g. `cotton`, `sunflower`, `oats`, `triticale`, `other hay`, `grass/pasture`,
`double crop: winter wheat/sorghum`). The code → name table lives in `pipeline/cdl_codes.py`.
Consumers must tolerate names they have not seen before.

## Validation

`pipeline/schema.py` is the single implementation of these rules:

```bash
python pipeline/schema.py data/fields.parquet   # validates, then prints a summary
```

The pipeline validates before it writes; the API validates on startup and refuses to start on a bad file.

# `data/soil_health.parquet`

Yearly soil condition per field from satellites (Phase 2A). `organic_matter_pct` above is one static
SSURGO value per field and cannot show change; this table is the per-year view. It is optional: the
app works without it. Validator: `pipeline/soil/health_schema.py`. Built by `python -m pipeline.soil.build`.

Plain Parquet (no geometry), **one row per field per year**. Every `field_id` in `data/fields.parquet`
has a row for every year 2016–2024.

| column       | type   | null? | meaning |
|--------------|--------|-------|---------|
| `field_id`   | string | no    | A `field_id` from `data/fields.parquet` |
| `year`       | int    | no    | Calendar year |
| `om_pred`    | float  | yes   | Estimated topsoil organic matter %, predicted from that year's bare-soil reflectance (Sentinel-2 and Landsat 8/9) by a model trained on SSURGO |
| `n_obs`      | float  | no    | Mean number of bare-soil satellite views per pixel that year; `0` when there were none |
| `confidence` | float  | no    | 0–1: how much to trust `om_pred` in this row |
| `bare_days`  | float  | yes   | Estimated days in the year the soil sat bare and exposed (no crop and no residue cover) |

## Rules

- `(field_id, year)` is unique.
- `om_pred` is null when the field had no usable bare-soil view that year; then `confidence` is `0`.
  Otherwise `om_pred` is in `[0, 100]`.
- `n_obs` is `>= 0`; `confidence` is in `[0, 1]`; `bare_days` is in `[0, 366]`.
- `bare_days` is null only when the year had too few clear views to tell.
- Consumers must handle nulls and must not treat `om_pred` as a measurement: read `confidence` and the
  meta file below.

## Meta file: `data/soil_health_meta.json`

Written next to the table. A JSON object with at least:

| key           | type           | meaning |
|---------------|----------------|---------|
| `recommended` | string         | `"om_pred"` or `"bare_days"`: which column is the trustworthy signal |
| `om_noise_sd` | number or null | Typical year-to-year jump of `om_pred` (one standard deviation, in OM %) on a field whose crop did not change |
| `verdict`     | string         | One or two plain sentences for the app |

Other keys (model accuracy, noise numbers, method version) are free-form.

```bash
python -m pipeline.soil.health_schema data/soil_health.parquet   # validates, then prints a summary
```
