# Data contract: `data/fields.parquet`

**This file is the contract between the pipeline, the API and the frontend. It only changes on purpose** —
if you change it, change `pipeline/schema.py` (the validator), the API and the frontend in the same commit.

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
