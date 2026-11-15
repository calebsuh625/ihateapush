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
| `n_obs`      | float  | no    | Mean number of bare-soil satellite views per pixel behind `om_pred` that year (Landsat plus Sentinel-2); `0` when `om_pred` is null |
| `confidence` | float  | no    | 0–1: how repeatable `om_pred` is in this row (how often and how fully the field was seen bare, and whether the two satellites agreed), scaled by the model's overall skill |
| `bare_days`  | float  | yes   | Estimated days in the year the soil sat bare and exposed (no crop and no residue cover) |

## Rules

- `(field_id, year)` is unique.
- `om_pred` is null when the field had no usable bare-soil view that year; then `confidence` is `0`.
  Otherwise `om_pred` is in `[0, 100]`.
- `n_obs` is `>= 0`; `confidence` is in `[0, 1]`; `bare_days` is in `[0, 366]`.
- `bare_days` is null only when the year had too few clear views to tell.
- `confidence` is repeatability, not accuracy. It cannot exceed the model's overall skill, so it stays
  well below 1 everywhere; do not apply fixed cut-offs such as 0.5. The formula is in
  `pipeline/soil/confidence.py`.
- Consumers must handle nulls and must not treat `om_pred` as a measurement: read `confidence` and the
  meta file below.

## Meta file: `data/soil_health_meta.json`

Written next to the table. A JSON object with at least:

| key           | type           | meaning |
|---------------|----------------|---------|
| `recommended` | string         | `"om_pred"` or `"bare_days"`: which column is the trustworthy signal |
| `om_noise_sd` | number or null | Typical year-to-year jump of `om_pred` (one standard deviation, in OM %) on a field whose crop did not change |
| `verdict`     | string         | One or two plain sentences for the app |

Optional keys the build also writes: `notes` (an object with one plain sentence each for `om_pred`,
`bare_days` and `confidence`) and `trustworthy` (an object: `om_pred` true/false, `bare_days`
true/false/null). `recommended` names `bare_days` even when `bare_days` fails its own checks; in that
case `trustworthy.bare_days` is false and `verdict` says not to rely on either number.
Other keys (model accuracy, noise numbers, method version) are free-form.

The API serves the table at `GET /field/{id}/soil`.

```bash
python -m pipeline.soil.health_schema data/soil_health.parquet   # validates, then prints a summary
```

---

# Phase 2B outputs: rotations compared on matched neighbours

Written by `make causal` (`python -m pipeline.causal.run_causal`). None of these change
`data/fields.parquet`; they are read by `GET /effects` and `GET /field/{id}/neighbors`.

**Input.** The yearly soil table above, `data/soil_health.parquet`, exactly as its contract says
(the run stops if `pipeline/soil/health_schema.py` finds a problem). The outcome column is a setting,
not code: `CAUSAL_OUTCOME=<column>`, default **`bare_days`** (lower is better; averaged with equal
weights). `om_pred` stays selectable (higher is better; weighted by `confidence`), but it cannot show
soil change. Only if the real table is missing does the run fall back to
`data/soil_health_fake.parquet`: the same contract, **made-up values with planted crop effects**, from
`pipeline/causal/make_fake_soil.py`; every output then says `is_fake: true`.

**Years.** All derived from one setting, the switch year (`SWITCH_YEAR` in `pipeline/causal/config.py`,
2020), and the years the two tables cover:

| name | years | used for |
|------|-------|----------|
| history | 2008–2019 | crop mix for matching; the irrigated / dryland guess |
| rotation | 2020–2024 | the rotation type being compared |
| baseline | 2016–2017 | soil value the pair is matched on |
| pre | 2018–2019 | the "before" of the difference |
| post | 2020–2024 | the "after" |

## `data/effects.json`

| key | meaning |
|-----|---------|
| `is_fake`, `warning` | `false` + `null` on the real soil table; `true` + a warning sentence on the fake one |
| `sources` | file names of the fields and soil tables used |
| `outcome` | `column`, `label`, `units`, `higher_is_better`, `weight_column` (or `null`), `note` (one sentence on how to read a gap) |
| `design` | `switch_year`, `history_years`, `rotation_years`, `soil_years`, `baseline_years`, `pre_years`, `post_years` (each `[first, last]`), `placebo_switch_years`, calipers, bootstrap settings |
| `counts` | fields, eligible fields, irrigation groups, rotation types, candidate pairs, matched pairs (total and `pairs_by_comparison`), `fields_in_a_pair` |
| `effects[]` | one row per comparison and group (below) |
| `by_year` | comparison → `[{year, gap, n_pairs}]`: mean A − B gap each soil year, relative to the pair's gap in the pre years |
| `placebos.fake_switch_years[]` | `fake_switch_year`, `before_years`, `after_years` (both before the real switch; `before_years` always includes the baseline years the pairs were matched on, so part of a non-zero value is the gap springing back after matching), `comparison`, `effect`, `ci_low`, `ci_high`, `p_value`, `n_pairs`, `ci_excludes_zero`, `from_crops` |
| `placebos.shuffled_labels[]` | `comparison`, `real_effect`, `null_mean`, `null_sd`, `null_p95_abs`, `p_value`, `n_shuffles` |
| `demo_pairs[]` | the clearest pairs for a demo: `pair_id`, `comparison`, `rotation_a`, `rotation_b`, `field_a`, `field_b`, `distance_km`, `match_score`, `pre_a`, `post_a`, `pre_b`, `post_b`, `did`, `better` (`"a"` / `"b"`: the field with the better post level; in a demo pair the change points the same way) |

`effects[]` row: `a`, `b` (rotation types; `comparison` = `"a vs b"`), `group_kind` (`irrigation` or `soil`),
`group` (`all`, `irrigated`, `dryland`, or a SSURGO organic-matter band), `effect`, `ci_low`, `ci_high`
(95% cluster bootstrap), `se`, `p_value` (sign-flip permutation), `n_pairs`, `n_clusters`,
`ci_excludes_zero`, `favours`, `gap_pre`, `gap_post`, `from_crops`, `beyond_crops`, `fallow_share_a`, `fallow_share_b`, and with
fake soil only `planted_effect` (what the estimate should find). Rows with fewer than 10 pairs are left out.

- `effect` = mean over pairs of `(post_A − pre_A) − (post_B − pre_B)` in outcome units, using only the
  years in which both fields of the pair have a value. Positive means the outcome rose more under
  rotation A. **For `bare_days` a negative effect is the good direction** (A sat bare fewer days).
- `favours` = `"a"` or `"b"`, the rotation whose outcome moved the better way given `higher_is_better`;
  `null` when the interval includes zero.
- `gap_pre`, `gap_post` = mean over the same pairs of the plain level difference A − B in the pre years
  and in the post years. `effect` = `gap_post − gap_pre`: a change in the gap, not a level. The two can
  have different signs.
- `from_crops` = `{effect, ci_low, ci_high, ci_excludes_zero}`: the same difference computed after
  replacing every field-year by the county average, that year, for the crop grown that year. It is the
  part of `effect` that follows from which crops were grown and nothing else.
- `beyond_crops` = same shape, `effect − from_crops` pair by pair: what is left.
- `fallow_share_a/_b` = share of the rotation years (2020–2024) spent in fallow, averaged over the A / B fields.

Rotation types (read from 2020–2024, `pipeline/causal/rotations.py`): `alfalfa/perennial`, `diverse`,
`corn-soy`, `wheat-sorghum-fallow`, `continuous corn`, `other` (`other` is never compared). In a
comparison, A is the type earlier in that list.

## `data/pairs.parquet`

One row per matched pair (plain Parquet, no geometry).

| column | meaning |
|--------|---------|
| `pair_id` | `"P00042"` |
| `comparison`, `rotation_a`, `rotation_b` | as in `effects[]` |
| `field_a`, `field_b` | field ids; `field_a` has rotation A |
| `irrigation` | `irrigated` / `dryland` (both fields) |
| `soil_group` | SSURGO organic-matter band of the pair |
| `distance_km`, `slope_diff`, `ssurgo_om_diff`, `baseline_diff`, `history_distance` | how alike the two fields are (absolute differences; `history_distance` = total-variation distance of the 2008–2019 crop mix, 0 = same) |
| `match_score` | sum of each difference over its caliper; lower = more alike |
| `rotation_pre_a`, `rotation_pre_b` | 2008–2019 rotation type |
| `baseline_a/_b` | each field's mean outcome over 2016–2017 (matched on) |
| `pre_a/_b`, `post_a/_b` | mean outcome over 2018–2019 and over 2020–2024, counting only years both fields have |
| `n_pre`, `n_post` | how many shared years went into those means |
| `did` | `(post_a − pre_a) − (post_b − pre_b)`: a change. Its sign can differ from that of `post_a − post_b` (the level) |
| `did_crops`, `did_beyond` | the pair's share of `from_crops` and of `beyond_crops`; they add up to `did` |

## `data/causal_fields.parquet`

One row per field: `field_id`, `irrigation` (`irrigated` / `dryland` / `uncertain`), `irrigation_reason`,
`circle_fill`, `rotation_pre`, `rotation_post`, `soil_group`, `eligible` (bool), `n_baseline`, `n_pre`,
`n_post` (soil years with a value in each window), `baseline`, `pre`, `post` (the field's own means).
