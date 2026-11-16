# Data contracts

**This file is the contract between the pipeline, the API and the frontend. It only changes on purpose** —
if you change it, change the validator, the API and the frontend in the same commit.

Two core tables: [`data/fields.parquet`](#datafieldsparquet) (one row per field) and
[`data/soil_health.parquet`](#datasoil_healthparquet) (one row per field per year). Phase 2B outputs and
the Phase 3 planner inputs (three climate tables, `nass_finney.parquet`, `field_traits.parquet`, two
tracked JSON files) and answers (`/field/{id}/plans`, `/climate`) are described further down.

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

---

# Phase 3: planner inputs and answers

All optional: without them the app runs the old rule-based planner (`api/planner/legacy.py`). None of
them changes `data/fields.parquet`.

## Climate tables

Validator: `pipeline/climate/schema.py` (`check_futures`, `check_history`, `check_monthly`; pandas only).
Written by `python -m pipeline.climate.build`. Plain Parquet, no geometry: one number for the whole county.
Source: `NASA/GDDP-CMIP6` daily model output (Earth Engine), six models, bias-corrected month by month
against NASA POWER observed weather for 2000-2024. Method and limits: `data/climate_report.md`.

Yearly columns, the same in `climate_futures.parquet` and `climate_history.parquet`. All `float`, never
null. Growing season (GS) = 1 Apr - 30 Sep. Wheat season of year Y = 1 Oct (Y-1) - 30 Jun (Y).

| column            | meaning |
|-------------------|---------|
| `tmean_c`         | GS mean of (Tmax + Tmin) / 2, degrees C |
| `rain_mm`         | GS rain total, mm |
| `summer_rain_mm`  | June-August rain total, mm |
| `gdd`             | GS growing degree days, base 10 C (Tmax capped at 30, Tmin floored at 10) |
| `hot_days`        | GS days with Tmax above 35 C |
| `dry_spell_days`  | Longest run of GS days with rain below 1 mm |
| `et0_mm`          | GS reference evapotranspiration total, mm (FAO-56 Penman-Monteith) |
| `wind_ms`         | GS mean wind speed at 10 m, m/s |
| `wheat_rain_mm`   | Wheat-season rain total, mm |
| `wheat_hot_days`  | Days with Tmax above 32 C in May-June |
| `wheat_et0_mm`    | Wheat-season reference ET total, mm |
| `spring_wind_ms`  | February-May mean wind speed at 10 m, m/s |
| `erosion_climate` | Wind-erosion climate factor, scaled so the observed 2000-2024 mean is 100; one year is capped at 300 |
| `annual_rain_mm`, `annual_et0_mm`, `annual_tmean_c` | Calendar-year rain total, reference ET total, mean temperature |

### `data/climate_futures.parquet`

`model` (string), `scenario` (`ssp245` or `ssp585`), `year` (int) + the 16 yearly columns. One row for
every model x scenario x year 2000-2060 (6 x 2 x 61 = 732 rows). Years to 2024 are the model's corrected
baseline; futures proper are 2025-2060. Models: ACCESS-CM2, GFDL-ESM4, MPI-ESM1-2-HR, MRI-ESM2-0,
NorESM2-MM, UKESM1-0-LL.

### `data/climate_history.parquet`

`year` (int, 2000-2024, each once) + the 16 yearly columns, from observed weather (NASA POWER). 25 rows.

### `data/climate_monthly.parquet`

`model`, `scenario`, `year` (int), `month` (int, 1-12) + seven `float` columns, never null: `tmax_c`,
`tmin_c`, `tmean_c` (monthly means, C), `rain_mm`, `et0_mm` (monthly totals, mm), `wind_ms` (mean wind at
10 m), `hot_days` (days above 35 C). Observed rows have `model` = `scenario` = `"observed"` (2000-2024);
model rows cover 2000-2060 for both scenarios. October-December 1999 is present for every series (the
first wheat season starts then); no other month before 2000 is allowed. 9,123 rows.

```bash
python -m pipeline.climate.schema            # validates the three tables, prints a summary
```

## `data/nass_finney.parquet`

USDA NASS crop statistics (yields, acres, prices). Validator and builder: `pipeline/nass.py`
(`python -m pipeline.nass`, `--check`). Plain Parquet, tidy long format, one row per
(year, crop, practice, stat).

| column        | type   | null? | meaning |
|---------------|--------|-------|---------|
| `year`        | int    | no    | Crop year (2000-2024 for yields and acres; the last 10 marketing years for prices) |
| `crop`        | string | no    | `corn`, `sorghum`, `winter wheat`, `soybeans`, `alfalfa` (fallow has no statistics) |
| `practice`    | string | no    | `irrigated`, `dryland` or `all` |
| `stat`        | string | no    | `yield`, `planted_acres`, `harvested_acres` or `price` |
| `value`       | float  | no    | The number, `>= 0` |
| `unit`        | string | no    | `bu/acre`, `tons/acre` (alfalfa), `acres`, `$/bu`, `$/ton` (alfalfa) |
| `level`       | string | no    | Where the value came from: `county` (Finney), `district` (Kansas Southwest) or `state` |
| `source_desc` | string | no    | NASS short description of the series |

- A year NASS did not publish for the county is filled from the district, then the state; `level` says
  which. What NASS publishes nowhere is absent: the irrigated / dryland split ends in 2018 (corn), 2019
  (winter wheat), 2016 (soybeans) and 2009 (sorghum); alfalfa has `all` only and no `planted_acres`.
- `planted_acres` and `harvested_acres` of one (crop, practice, year) come from the same level. Only
  `level == "county"` rows are Finney County amounts.
- Prices are Kansas marketing-year prices (`level` always `state`, `practice` always `all`); sorghum is
  converted from $/cwt at 56 lb per bushel. Live file: 772 rows.

## `data/field_traits.parquet`

Per-field traits for the planner. Validator: `pipeline/field_traits_schema.py`. Built by
`python -m pipeline.field_traits`. Plain Parquet, one row per field in `data/fields.parquet` (7,255 rows).

| column             | type   | null? | meaning |
|--------------------|--------|-------|---------|
| `field_id`         | string | no    | A `field_id` from `data/fields.parquet`, unique |
| `irrigation`       | string | no    | `irrigated` or `dryland`: our best guess; `irrigated` exactly when `irrigated_prob >= 0.5` |
| `irrigated_prob`   | float  | no    | 0-1, how likely the field is irrigated |
| `irrigation_basis` | string | no    | Short plain text on what the guess rests on |
| `weg`              | string | yes   | SSURGO wind erodibility group of the main soil: `1`, `2`, `3`, `4`, `4L`, `5`, `6`, `7`, `8` (1 blows most easily) |
| `wei`              | float  | yes   | SSURGO wind erodibility index, tons/acre/year (0-310) |
| `et_gs_mm`         | float  | yes   | Mean April-September evapotranspiration 2016-2024, mm (OpenET); null when OpenET was not fetched |

The irrigation value is a guess from outline shape, crops grown and (when OpenET ran) water use. It is
not a record of water rights or wells.

## `data/groundwater.parquet` (Phase 4: water reality check)

Measured depth to water in wells in and next to the county (its bounding box widened by 0.15 degrees, about
10 miles). Validator: `pipeline/groundwater_schema.py`. Built by `python -m pipeline.groundwater`
(`make groundwater`) from the USGS Water Data API (to 2015, where its record for this area stops) and the
Kansas Geological Survey's WIZARD database (the same wells, to this winter). Optional: without it the app
shows no water outlook. Plain Parquet, one row per well and day (33,958 rows, 1,941 wells, 1911-2026).

| column          | type   | null? | meaning |
|-----------------|--------|-------|---------|
| `well_id`       | string | no    | The USGS monitoring-location id (`USGS-375018100171901`); `KGS-<number>` for a well only the KGS lists |
| `date`          | date   | no    | Day of the measurement; the 15th when only the month is on record |
| `date_exact`    | bool   | no    | True when the record gives the day |
| `depth_ft`      | float  | no    | Depth to water below the land surface, feet (0 to 2,000) |
| `status`        | string | yes   | What the record says about the well at the time (`Static`, `Pumping`, `Recently pumped`, ...) |
| `well_depth_ft` | float  | yes   | Depth of the finished well, feet (the KGS figure when both sources have one; the hole depth when only that is known) |
| `lon`, `lat`    | float  | no    | Where the well is, degrees |
| `aquifer`       | string | yes   | USGS national aquifer code (`N100HGHPLN` = High Plains), or `KGS:<geological unit or aquifer code>` |
| `county_fips`   | string | yes   | Five-digit county code (`20055` = Finney) |
| `source`        | string | no    | `usgs` or `kgs`: whose database the reading came from |

A well and day appears once (two readings on one day are averaged; a reading both sources hold is kept from
the USGS). The trend rules are not in the file: `pipeline/groundwater.py` applies them (winter level = median
of the December-March readings not marked as disturbed; a well counts with 10 or more winters since 1996
spanning 10 or more years, the last within 5 winters of the newest; Theil-Sen slope with a 95% interval;
wells whose record names a bedrock aquifer are left out). Downloads are cached in `data/raw/nwis/` and
`data/raw/kgs/`.

## Tracked planner files (`api/planner/`)

All three are in git so a fresh clone works. Never edit `crop_response.json` by hand.

- `crop_response.json`: written by `python -m api.planner.calibrate`. Keys: `written`, `written_by`,
  `model`, `sources`, `constants` (`et0_scale`, `fallow_efficiency`, `awc_mm`, `pivot_efficiency`),
  `fail_below` (0.5: a crop "fails" under half its normal yield), `max_yield_rel` (2.0), `heat_cap`
  (`hot_days`, `wheat_hot_days`: the most hot days fed to the heat factor), `typical_prev`,
  `calibration_rules`, and `series`: `crop -> practice -> {unit, n_years, years, ky, heat_loss, scale,
  normal_yield, trend_pct_per_year, resid_sd, r2, loo_skill, loo_rmse, status, verdict, status_text, ...}`.
  `verdict` is `good` / `fair` / `weak` (from the leave-one-year-out skill); `status` is `fitted`,
  `partly textbook` or `textbook`; `status_text` says in plain words which number is a textbook value.
- `economics.json`: prices, costs and the irrigation limit behind `api/planner/money.py`. Keys: `note`,
  `basis`, `read_on`, `prices` (median of 2020-2024 Kansas prices, `per_unit` per crop), `budget_prices`,
  `pumping` (cost per mm and per inch), `costs` (per crop and practice: `per_acre`, `status`, `source`,
  `url`, ...; `fallow` included), `index_pages`, `cost_rule`, `cost_year`, `irrigation_limit` (`mm`,
  `inches`, `status`, `source`, `url`). A cost with `status` = `verified` was read from the K-State
  budget on the date given; `rough, unverified` is our estimate. Dollars are return over direct costs.

- `rotation_effects.json` (Phase 5): the yield cost of a crop after itself, read by `api/planner/rotation.py`.
  Keys: `version`, `written`, `what_it_is`, `how_to_read`, `status_words`, `effects` and `not_applied`. Each
  `effects[]` row: `id`, `crop`, `after` (two of the six crop names, fallow excluded), `practice` (`all`,
  `irrigated` or `dryland`), `multiplier` (relative yield, the same crop after another crop = 1.0), `low` (the
  stronger end of the range), `high` (the weaker end), with `low <= multiplier <= high`, `status` (`verified` or
  `rough, unverified`), `basis` (the figures and the arithmetic) and `sources[]` (`name`, `url`, `figure`). A row
  with no source, a bad range or an unknown status makes the file unusable (the planner then applies nothing and
  says so). `not_applied[]` rows (`id`, `crop`, `after`, `reason`, `status`, `sources`) are effects left out, with
  the reason. Never invent a number here: an effect that cannot be sourced is simply not listed.

## `GET /field/{id}/plans`

Query (all optional): `soil`, `water`, `money`, `risk` (zero or more, any scale, scaled to add up to 1;
defaults 0.30 / 0.20 / 0.30 / 0.20), `scenario` (`ssp245` default or `ssp585`), `horizon` (2025-2060,
default 2025 = today), `irrigated` (`true` / `false` overrides our guess). Soil means soil protection:
fewer bare days a year and lower wind-erosion risk.

Smart answer (`"engine": "smart"`), trimmed:

| key | contents |
|-----|----------|
| `field_id`, `engine` | |
| `settings` | `weights` (soil, water, money, risk), `scenario`, `horizon`, `window` (first and last year averaged), `irrigated`, `irrigation_source` (`guess` or override), `irrigation_basis`, `soil_source`, `soil_note`, `holds_up_rule`, `ranking_rule`, `profit_basis`, `irrigation_limit_mm`, `irrigation_limit_in` (null when no limit), `yield_change_note`, `limits` (list of plain sentences), `models` |
| `climate` | `label` (one plain line), `changes` (tmean_c, summer_rain_pct, hot_days, et0_pct, ..., `summer_rain_agree`, `models`), `range` (lowest and highest model per change), `window`, `models`, `yield_change` (`crop -> {pct, low, high}`) |
| `plans[]` (top 3) | `name`, `rotation` (next four years), `cycle` (repeating unit, 3 or 4 crops), `scores` (`soil`, `water`, `money`, `risk`, `overall`, `robust`; each 0-1), `reason` (one sentence), `futures` (`holds`, `total`), `spread` (`overall` and `profit_per_acre`, each `low` / `mid` / `high` across the models), `numbers` (`bare_days`, `erosion_index`, `irrigation_mm`, `irrigation_in`, `water_met_share`, `profit_per_acre`, `failed_share`), `rank` (place in the full ranking) |
| `current` | the field's last four years scored like a plan (the plan keys plus `years`, `previous_crop`, `is_candidate`, `rules_broken` as gentle sentences), or `null` |
| `current_note` | why `current` is null, otherwise null |
| `candidates` | how many cycles were compared (105 irrigated, 17 dryland) |
| `settings.rotation_effects` | (Phase 5) `setting` (`on` / `off`: ROTATION_EFFECTS), `applied` (true when at least one effect is in use), `file`, `effects[]` (`id`, `crop`, `after`, `practice`, `multiplier`, `low`, `high`, `status`, `sentence`), `problem` only when the file could not be read |
| `plans[].rotation_effects`, `current.rotation_effects` | (Phase 5) only when applied and the plan's years run into one: `[{id, crop, after, multiplier, low, high, status, sentence}]`. Absent otherwise, so a plan keeps its old keys when the effects are off. The same on `yours` of `/score` |

```json
{"field_id": "KS-013847", "engine": "smart",
 "settings": {"scenario": "ssp585", "horizon": 2060, "irrigated": false, "irrigation_source": "guess", "...": 0},
 "climate": {"label": "+2.0°C, models disagree on summer rain, crops need 8% more water, 26 more days above 35°C (95°F)",
             "yield_change": {"corn": {"pct": -40, "low": -56, "high": -25}}},
 "plans": [{"name": "Sorghum – Fallow (alternating)", "rank": 1,
            "scores": {"soil": 0.08, "water": 0.67, "money": 0.57, "risk": 0.77, "overall": 0.56, "robust": 0.53},
            "futures": {"holds": 4, "total": 6},
            "numbers": {"bare_days": 130.6, "profit_per_acre": 18.0, "failed_share": 0.23}}],
 "current": null, "current_note": "The last four years (2021-2024) include grass/pasture, which ...", "candidates": 17}
```

(Values from the live server on 2026-11-15; the trimmed example was taken with `money=5`, `scenario=ssp585`,
`horizon=2060`.)

Basic answer (`"engine": "basic"`): `{"field_id", "engine": "basic", "plans": [{name, rotation, scores, reason}]}`.
Returned when the climate tables are missing, `PLANNER_ENGINE=basic`, a custom planner was passed to
`create_app`, or the smart planner failed for that field. Query parameters are accepted and ignored. Every
plan in either answer keeps `name`, `rotation`, `scores.soil/water/money/overall` and `reason`.

Errors: `400 {"detail": "<one plain sentence>"}` for a negative or non-finite priority, all four zero,
unknown scenario or a horizon outside 2025-2060 (smart planner only); `404` no such field; `422` a value
that is not a number.

### Rotation effects (Phase 5)

`settings.limits[0]` is the sentence about the crop grown before: with the effects on it says a crop after itself
yields less by the amounts the trials measured (and that no nitrogen credit is counted); with `ROTATION_EFFECTS=off`
it is the old "Yields do not change with the crop grown before, apart from the soil water it leaves behind
(rotation effects are switched off)". `spread.profit_per_acre` and `spread.overall` (`low`, `mid`, `high`) span the
six climate models and the stronger and weaker end of each effect the plan runs into; `mid`, `scores` and `numbers`
use the middle values. Off, the whole answer is what it was before Phase 5.

### Price and cost what-ifs (Phase 5, item F)

`/plans`, `/score`, `/transition`, `/replay` and the farm planner take a percent change on prices and costs. We do not
predict prices: the farmer supplies the change. Smart planner only (the basic planner ignores it).

| Parameter (query; `POST /farm/plan` body keys `price_pct`, `cost_pct`, `crop_price_pct` {crop: pct}) | Meaning |
|---|---|
| `price_pct` | -50 to +100, percent change on every crop price (default 0) |
| `cost_pct` | -50 to +100, percent change on every direct cost: the per-acre budget lines, fallow passes and the pumping cost per inch (default 0) |
| `price_corn_pct`, `price_soybeans_pct`, `price_winter_wheat_pct`, `price_sorghum_pct`, `price_alfalfa_pct` | the same for one crop; it replaces `price_pct` for that crop |

Anything outside -50..+100 or not a number is a `400` with one sentence. Zero (or nothing) changes nothing: the answer is
identical to a call without the parameters. `settings.what_if` echoes what was applied (present only while a what-if is on, so a plain answer is unchanged):
`{"active": bool, "price_pct": 0.0, "cost_pct": 0.0, "crop_price_pct": {"corn": 20.0}, "note": str|null}`; when it is
active, `settings.limits` says prices and costs are today's moved by the what-if. Only prices and costs move; yields,
weather, water and soil scores do not.

`/plans` also carries `sensitivity` (null only if it could not be worked out): the break-even for the top plan, from
the planner's own model. Return over direct costs is linear in a uniform price change, so per climate model the
break-even is costs / revenue (revenue from a second run with prices doubled); no search, no new assumption. Costs are
held where they are set.

| Key | Meaning |
|---|---|
| `plan`, `plan_name` | which plan (1 = the top one) |
| `basis` | `today`: percentages are against today's prices (also when `price_pct` moved all crops together); `your_prices`: against the prices as set (when per-crop prices were given) |
| `status` | `ok` (break-even inside the control's range), `stays_above_zero` (every climate model stays above zero for any drop down to -50%), `below_zero_now` (already under its direct costs as set; the figures say how far prices would have to move), `no_revenue` |
| `return_now` | mean return over direct costs, $ an acre a year, as set |
| `break_even_pct`, `low_pct`, `high_pct` | the price change (percent, whole numbers) at which the return reaches zero: ensemble mean, and the lowest and highest across the climate models; null for `no_revenue` |
| `range_shown` | `[-50, 100]` |
| `sentence`, `note` | plain English for the farmer; "A what-if, not a forecast" |

The what-if engines are cached per engine (last 8 adjustments), like the irrigation-limit engines.

## `GET /rotation-effects` (Phase 5)

`{"setting": "on" | "off", "applied": bool, "file": "rotation_effects.json", "effects": [...], "not_applied": [...],
"baseline": "<how the county's mix levels the multipliers>", "levelled": [...]}`. Each `effects[]` row is the file's
row plus `sentence`; `levelled[]` rows (`id`, `practice`, `crop`, `share_after_itself` (the county's share of that crop
that followed itself, field-years of the soil table), `after_itself`, `after_another_crop`) give the multipliers the
engine really uses once each crop's set is divided by its average over the county's mix (omitted when there is no mix;
see README "Phase 5"). `400` with one sentence for an unreadable `ROTATION_EFFECTS`; without the climate tables
`{"applied": false, "setting": null, "effects": [], "reason": "not_built"}`.

### `limit_in` and `water_outlook` (Phase 4: water reality check)

`limit_in` (optional number, smart planner only): a what-if, the most inches that can be pumped on an acre
in a year, from 1 up to the planner's own limit (24). The plans are scored again under it and
`settings.irrigation_limit_in/mm` and `settings.limits` echo it; nothing else about the request changes and
the default answer is untouched. Outside the range: `400 "The pumping limit must be between 1 and 24 inches
an acre a year."`. `GET /field/{id}/score`, `GET /field/{id}/transition` and `POST /farm/plan` (body key
`limit_in`) take the same parameter. Dryland numbers do not change with it.

When `settings.irrigated` is true the smart answer has one more key, `water_outlook` (dryland answers and
the basic answer do not have it; the plans themselves are unchanged):

```
"water_outlook": {
  "available": true,
  "source": "Depth to water measured in wells: USGS Water Data API ... and the Kansas Geological Survey WIZARD ...",
  "rule": "A well counts when it has at least 10 winter measurements ...",
  "search": {"nearest": 5, "max_miles": 10.0, "min_wells": 3},
  "wells_used": 5,
  "distance_mi": {"nearest": 1.9, "farthest": 5.7},
  "winters": [1996, 2026],
  "decline_ft_per_year": {"median": 3.6, "low": 2.3, "high": 5.4},
  "falling": 5, "rising": 0,
  "depth_to_water_ft": {"median": 303, "low": 264, "high": 338},
  "column": {"wells": 5, "now_ft": {"median": 187, "low": 136, "high": 214},
             "years": [{"year": 2040, "at_bottom": 0, "left_ft": {"median": 138, "low": 70, "high": 146}},
                       {"year": 2060, "at_bottom": 1, "left_ft": {"median": 67, "low": 0, "high": 100}}]},
  "sentence": "The 5 nearest wells with a long record (1.9 to 5.7 miles from this field) have been dropping about 3.6 feet a year in the middle well, winters 1996 to 2026; the slowest about 2.3 and the fastest about 5.4.",
  "extrapolation": "The 5 wells with a depth on record hold about 187 feet of water today in the middle well (136 to 214 across the wells). If each well's measured rate simply continued, that would be about 138 feet in 2040 (70 to 146) and about 67 feet in 2060 (0 to 100).",
  "caveat": "This is the measured rate carried forward in a straight line, not a forecast: ...",
  "column_note": "Water column is the well's depth minus the depth to water ...",
  "threshold": null,
  "threshold_note": "No minimum usable water column is drawn. ...",
  "wells": [{"well_id": "USGS-374747100552101", "lon": -100.92294, "lat": 37.79641, "distance_mi": 1.9,
             "winters": 29, "first_winter": 1996, "last_winter": 2024,
             "decline_ft_per_year": 5.4, "interval": [5.2, 5.7],
             "depth_to_water_ft": 284, "well_depth_ft": 498, "column_ft": 214,
             "points": [[1996, 142.3], ...], "fit": [[1996, 139.4], [2024, 291.5]]}, ...],
  "what_if": {"limit_in": 24.0, "default_in": 24.0, "active": false, "min_in": 1.0, "max_in": 24.0, "note": "..."},
  "plan_lines": ["This plan pumps about 21 inches a year; wells near this field have been dropping about 3.6 feet a year.", ...],
  "current_line": "This plan pumps about 19 inches a year; wells near this field have been ..."
}
```

- Wells: the 5 nearest that meet the trend rule, no farther than 10 miles from the middle of the field; nearest
  first. Fewer than 3 within 10 miles, or no groundwater table: `{"available": false, "reason":
  "no_wells_nearby" | "not_built", "what_if", "plan_lines": [null, ...], "current_line": null}` (plus `note`
  for `no_wells_nearby`).
- `decline_ft_per_year`: change in winter depth to water per year, above zero = falling; `median`, `low` and
  `high` are the middle, slowest and fastest of the wells used (0.1 ft). A well's own `interval` is its 95%
  interval. `falling` / `rising`: wells whose whole interval is above / below zero.
- `column`: null when no well used has a depth on record. `left_ft` for a year is each well's column minus its
  own rate times the years since its last winter, never below 0 or above the well's depth, then the middle,
  lowest and highest; `at_bottom` counts the wells whose straight line has reached the bottom of the well.
  `years` holds 2040 and 2060, and the chosen `horizon` when it is a later year than the newest winter. It is
  an extrapolation, not a forecast (`caveat`), and there is no threshold (`threshold` is always null).
- `points`: the well's winter levels since 1996, `[winter, feet]`; `fit`: the two ends of its trend line.
- `what_if`: the limit the plans were scored under (`limit_in`), the planner's own (`default_in`), whether a
  what-if is on (`active`) and the range `limit_in` takes. `plan_lines` follow the order of `plans`.

## `GET /groundwater` (Phase 4: water reality check)

The county picture from `data/groundwater.parquet`. `{"available": false, "reason": "not_built", "source"}`
without the file. Otherwise:

```
{"available": true, "source", "county": "Finney County, Kansas",
 "table": {"measurements": 33958, "wells": 1941, "first_year": 1911, "last_year": 2026, "from_usgs": 27109, "from_kgs": 6849},
 "rule": {"text", "winter_months": [12, 1, 2, 3], "trend_from": 1996, "min_winters": 10, "min_span_years": 10, "max_stale_winters": 5},
 "summary": {"wells": 64, "winters": [1996, 2026],
             "decline_ft_per_year": {"median": 2.6, "p25": 0.8, "p75": 3.6, "min": -0.1, "max": 5.4},
             "falling": 63, "rising": 1,
             "depth_to_water_ft": {"median": 165, "p25", "p75", "min", "max"},
             "column": {"wells": 59, "now_ft": {"median": 110, ...},
                        "years": [{"year": 2040, "left_ft": {"median": 77, ...}, "at_bottom": 5}, {"year": 2060, ...}]}},
 "nearby_summary": {...the same for all 154 wells of the table that meet the rule: the county's and those next to it...},
 "sentence", "extrapolation", "caveat", "threshold": null, "threshold_note", "limits": [...],
 "wells": [{"well_id", "lon", "lat", "winters", "first_winter", "last_winter", "decline_ft_per_year", "interval",
            "depth_to_water_ft", "well_depth_ft", "column_ft", "in_county"}, ...]}
```

`summary` is the wells whose `county_fips` is the county's; its bands are the median, the middle half
(`p25`, `p75`) and the full range across wells. Rates to 0.1 ft a year, depths and columns to whole feet.
`summary` is null (and `available` false, `reason: "no_wells"`) when no county well meets the rule.

## `GET /field/{id}/score` (Phase 4: build-your-own rotation)

The farmer's own rotation scored like the plans, next to our top picks, with a comparison against our
first pick. Smart planner only. Query: `rotation` (required) plus every parameter of `/plans` (`soil`,
`water`, `money`, `risk`, `scenario`, `horizon`, `irrigated`), with the same defaults and the same checks.

`rotation`: 2 to 6 crops, one a year, read as a repeating cycle. Separated by commas, semicolons, slashes,
arrows, " - ", "then", "and", new lines or spaces; case does not matter. Names accepted (aliases in
`api/planner/custom.py`): corn (maize); soybeans (soybean, soy, soya, beans); winter wheat (wheat, hrw,
ww); sorghum (milo, grain sorghum); alfalfa (lucerne); fallow (idle, summer fallow, chem fallow, rest).

```json
{
  "field_id": "KS-013847", "engine": "smart",
  "understood": ["corn", "sorghum", "winter wheat", "fallow"],
  "settings": { "...": "exactly the settings block of /plans" },
  "yours": {
    "name": "Corn – Sorghum – Winter wheat – Fallow",
    "cycle": ["corn", "sorghum", "winter wheat", "fallow"],
    "rotation": ["corn", "sorghum", "winter wheat", "fallow"],
    "scores": {"soil": 0.255, "water": 0.545, "money": 0.569, "risk": 0.777, "overall": 0.512, "robust": 0.488},
    "reason": "Returns about $17 an acre a year over direct costs, ...",
    "futures": {"holds": 4, "total": 6},
    "spread": {"overall": {"low": 0.448, "mid": 0.498, "high": 0.582}, "profit_per_acre": {"low": -6.0, "mid": 16.0, "high": 41.0}},
    "numbers": {"bare_days": 97.3, "erosion_index": 1.788, "irrigation_mm": 0.0, "irrigation_in": 0.0,
                "water_met_share": 0.545, "profit_per_acre": 17.0, "failed_share": 0.223},
    "rank": 15, "rank_of": 18, "rankable": true,
    "is_candidate": false, "same_as_pick": null,
    "rules_broken": ["Our plans avoid winter wheat straight after corn, sorghum or soybeans without irrigation (...)."],
    "scored_as": ["corn", "sorghum", "winter wheat", "fallow"]
  },
  "plans": ["... the three plans of /plans for the same settings, unchanged ..."],
  "candidates": 17,
  "marks": {"robust": [1], "soil": [1], "water": [3], "money": [3], "risk": [2, 3], "holds": [1, 2, 3],
            "bare_days": [1], "water_use": [3], "profit": [3], "failed": [2, 3]},
  "marks_note": "A mark means no other column is ahead of that figure in every climate model. ...",
  "comparison": {
    "against": "Winter wheat – Sorghum – Fallow (3-year cycle)",
    "verdict": "ours",
    "models": {"yours_ahead": 0, "ours_ahead": 6, "total": 6},
    "headline": ["Yours is not one of the 17 rotations we compare (it breaks what our plans avoid, listed below). Scored the same way, it would be number 15 of 18 with these priorities.",
                 "Our first pick scores higher overall in all 6 climate models.",
                 "Ours is ahead on soil protection, water, money, risk and holding up across the futures; yours is not ahead on anything."],
    "rows": [
      {"key": "money", "label": "Money", "unit": "$ an acre a year over direct costs",
       "yours": 17.0, "ours": 36.0, "diff": -19.0, "verdict": "ours",
       "models": {"yours_ahead": 0, "ours_ahead": 6, "total": 6},
       "sentence": "Ours returns about $36 an acre a year over direct costs ($17 to $54 across the climate models). Yours returns about $17 an acre a year over direct costs (−$6 to $41 across the climate models).",
       "score_yours": 0.569, "score_ours": 0.642, "score_diff": -0.073,
       "yours_range": [-6, 41], "ours_range": [17, 54]}
    ],
    "priorities": [{"priority": "soil", "rank": 8, "of": 18, "first": false, "tied": 0}],
    "priority_sentences": ["Yours does not come first under any single priority. It gets closest when soil protection is all that matters: number 8 of 18."],
    "notes": ["Your rotation is scored as a repeating cycle: after its last year comes its first again.",
              "On dryland the sign of these returns depends on the price years and the cost of fallow, so read the money row as a rough guide."]
  }
}
```

(Values from the live server on 2026-11-15, default settings; `rows` and `priorities` trimmed to one entry.)

- `understood`: the crops read from `rotation`, in the six fixed names. `yours.cycle` is the same list.
- `yours`: a plan object as in `/plans` plus: `rules_broken` (the "Our plans avoid ..." sentences; empty
  when none), `is_candidate` (it is one of the rotations the planner compares; then the numbers are that
  candidate's own), `same_as_pick` (1, 2 or 3 when it is that plan of `plans`, else null), `rank` and
  `rank_of` (its place on the ranking score; `rank_of` is `candidates`, or `candidates + 1` when yours is
  not among them, so `rank` can be `candidates + 1`: after all of them), `scored_as` (the cycle the numbers
  were worked out for: shortest repeating unit, a 1- or 2-year unit written over four years, fixed
  starting year). `yours.rotation` is the cycle's next four years (a longer cycle's first four).
- `marks`: for each row of the app's table, the columns to mark; column 0 is yours, 1-3 are `plans` in
  order. Rows: `robust`, `soil`, `water`, `money`, `risk` (the scores), `holds`, `bare_days`, `water_use`
  (inches pumped when irrigated, share of need met by rain on dryland), `profit`, `failed`. A column is
  marked unless another column is ahead of it in every climate model with a different figure at the
  precision shown; an empty list means nothing separates the columns. `robust` marks the highest value.
  When yours is fallow every year (not rankable, below) column 0 is never marked in any row and our
  picks are marked among themselves.
- **A rotation that plants no crop in any year is not rankable.** Then `yours.rankable` is `false`,
  `yours.rank` and `yours.rank_of` are `null`, `comparison.verdict` is `"not_compared"`,
  `comparison.models` is `null`, `comparison.rows`, `priorities` and `priority_sentences` are empty, and
  `comparison.headline` is one sentence built from its own figures: "This rotation plants no crop in any
  year, so it returns nothing (it costs about $14 an acre a year to keep fallow) and leaves the soil bare
  about 104 days a year; it is not ranked or compared with our plans as a cropping plan." `yours.scores`
  and `yours.numbers` still carry what the engine computes (water and risk scores of 1.0 only because
  there is no crop); the app shows "not ranked" and "no crop" in their place. In every other answer
  `rankable` is `true` and the fields are as described here.
- `comparison` is yours against `plans[0]`. `rows` always has `soil`, `water`, `money`, `risk`, `holds`
  in that order. `yours`, `ours` and `diff` (yours minus ours, between the rounded values) are in `unit`:
  bare days a year; inches pumped a year or the share of crop water need met by rain; dollars an acre a
  year over direct costs; share of planted crops that fail; futures held up in. `score_*` are the 0-1
  scores (not on `holds`). `verdict`: `"ours"` or `"yours"` when that side is ahead in every climate model
  where the two differ, `"too_close"` when the models disagree (the sentence then ends with the count),
  `"same"` when the figures are equal at the precision shown. `models` counts the climate models each
  side is ahead in (null on `holds`, which is one count). Every figure in a `sentence` is one of the
  row's own values.
- `comparison.verdict` and `comparison.models` are the same test on the overall score (the weighted
  mean of the four) model by model.
- `comparison.priorities`: one entry per priority in the order soil, water, money, risk: the rotation's
  place among the candidates when that priority alone has weight. `first` is `rank == 1`; `tied` counts
  the candidates with exactly the same value.

Errors: `400 {"detail": "<one plain sentence>"}` for a crop that is not one of the six (the sentence lists
them), fewer than 2 or more than 6 years, no rotation, and the bad settings `/plans` refuses; `404` no such
field; `503 {"detail": "<one plain sentence>"}` when only the basic planner is available (no climate
tables, `PLANNER_ENGINE=basic`, a custom planner) or the engine failed. The app hides the feature on 503.

## `GET /field/{id}/transition` (Phase 4: transition path)

The year-by-year path from the field's real last years to a rotation, scored against carrying on as now.
Smart planner only (`api/planner/transition.py`). Query: `plan` = 1, 2 or 3 (that pick of `/plans` for the
same settings; the default is 1) **or** `target` = a rotation of 2 to 6 crops, read like `rotation` of
`/score`; `years` = how many years ahead, 4 to 12 (default 8); plus every parameter of `/plans`, with the
same defaults and checks.

```json
{
  "field_id": "KS-004139", "engine": "smart",
  "settings": { "...": "exactly the settings block of /plans" },
  "years": 8,
  "target": {"source": "plan", "plan": 2, "name": "Sorghum – Fallow (alternating)",
             "cycle": ["sorghum", "fallow", "sorghum", "fallow"], "rules_broken": []},
  "history": [{"year": 2022, "crop": "sorghum"}, {"year": 2023, "crop": "fallow"}, {"year": 2024, "crop": "winter wheat"}],
  "status": "path",
  "summary": "The rotation can start in 2025 with fallow (year 2 of the cycle). Of the 2 years of the cycle that can follow winter wheat (2024), this start returns the most over the 8 years.",
  "path": {
    "bridge_years": 0, "already_there": false,
    "entry": {"n": 1, "year": 2025, "crop": "fallow", "cycle_year": 2},
    "options": 2,
    "years": [
      {"n": 1, "year": 2025, "crop": "fallow", "after": "winter wheat", "role": "plan", "cycle_year": 2,
       "return": {"mid": -14, "low": -14, "high": -14}, "yield": null, "yield_unit": null,
       "irrigation_mm": 0.0, "irrigation_in": 0.0, "water_met_share": null, "bare_days": 130.0, "fail_chance": null}
    ],
    "total_return": {"mid": 469, "low": 294, "high": 611}
  },
  "blocked": [],
  "carry_on": {
    "name": "Sorghum – Fallow – Winter wheat (3-year cycle)", "pattern": ["sorghum", "fallow", "winter wheat"],
    "repeating": true, "basis_years": [2021, 2024],
    "years": [
      {"n": 1, "year": 2025, "crop": "sorghum", "after": "winter wheat", "role": "carry_on", "cycle_year": 1,
       "return": {"mid": 88, "low": 47, "high": 122}, "yield": 59.4, "yield_unit": "bu/acre",
       "irrigation_mm": 0.0, "irrigation_in": 0.0, "water_met_share": 0.621, "bare_days": 48.9, "fail_chance": 0.186}
    ],
    "total_return": {"mid": 287, "low": 133, "high": 434}
  },
  "carry_on_note": null,
  "difference": {
    "years": [
      {"n": 1, "year": 2025, "return": {"mid": -102, "low": -136, "high": -61},
       "cumulative": {"mid": -102, "low": -136, "high": -61}, "bare_days": 81.1, "cumulative_bare_days": 81.1}
    ],
    "payback": {"status": "paid_back", "n": 4, "year": 2028, "lowest": {"n": 1, "value": -102, "year": 2025},
                "models_ahead_at_end": 6, "models": 6},
    "total": {"mid": 181, "low": 157, "high": 206},
    "total_sentence": "After 8 years this path is about $181 an acre ahead of carrying on as now ($157 to $206 across the climate models); it is level or ahead in 6 of the 6 climate models."
  },
  "long_run": {
    "plan": {"return": {"mid": 59, "low": 37, "high": 76}, "bare_days": 130.6, "irrigation_mm": 0.0,
             "irrigation_in": 0.0, "water_met_share": 0.735, "failed_share": 0.117},
    "carry_on": {"return": {"mid": 36, "low": 17, "high": 54}, "bare_days": 81.0, "irrigation_mm": 0.0,
                 "irrigation_in": 0.0, "water_met_share": 0.656, "failed_share": 0.139},
    "difference": {"return": {"mid": 23, "low": 20, "high": 27}, "verdict": "plan",
                   "models": {"plan_ahead": 6, "carry_on_ahead": 0, "total": 6},
                   "bare_days": 49.6, "failed_share": -0.022}
  },
  "sentences": {
    "short_term": "Short term: at its lowest point, after 2025 (year 1), this path is about $102 an acre behind carrying on as now (−$136 to −$61 across the climate models).",
    "long_term": "Long term: once established, the plan returns about $23 an acre a year more than carrying on as now ($20 to $27 across the climate models), and the switch has paid back by 2028 (year 4); it also leaves the soil bare about 50 more days a year."
  },
  "not_modelled": ["Equipment, storage, contracts, lease and crop insurance terms, and the learning curve of a crop that is new to the farm.", "..."],
  "notes": ["Every year is scored with the average weather of 2015–2034 in each of the 6 climate models (the future chosen for the plans), not with a forecast for that calendar year.", "..."]
}
```

(Values from the live server on 2026-11-15, default settings; the `years` lists trimmed to one entry.)

- `{mid, low, high}` everywhere: the mean over the climate models, the lowest model and the highest.
  Dollars are whole dollars an acre, return over direct costs.
- `target`: `source` is `"plan"` (then `plan` is the card number) or `"own"`. `rules_broken`: the "Our
  plans avoid ..." sentences the rotation itself breaks every round; they are not held against the path.
- `history`: the field's last recorded years as they are, at most three, oldest first.
- `status`: `"path"`; `"already_there"` (the last three real years, or a whole cycle if longer, follow
  the rotation; `path` is then its next years and `carry_on` is the same cycle, so every difference is
  zero); `"no_path"` (nothing valid within two bridge years: `path`, `difference`, `sentences` are null and
  `blocked` names the rules); `"no_history"` (the last recorded year is not one of the six crops, or there
  is no history: everything below `summary` is null or empty). `summary` is always one or two plain
  sentences saying which.
- `path.years[]`: `n` (1-based), `year` (the last recorded year plus `n`), `crop`, `after` (the crop the
  year before; for year 1 the last real crop), `role` (`"bridge"` or `"plan"`), `cycle_year` (1-based year
  of `target.cycle`, null for a bridge year), `return`, `yield` in `yield_unit` an acre (null for fallow),
  `irrigation_mm` / `irrigation_in` pumped, `water_met_share` (share of the crop's water need that is met;
  null for fallow), `bare_days` (that crop after that previous crop, no rotation-type shift), `fail_chance`
  (chance the yield is under half of normal; null for fallow). `path.entry` is the first year of the
  rotation after `bridge_years` (0 to 2) bridge years; `options` counts the valid ways in with that many
  bridge years. `total_return`: the sum over the span.
- `blocked`: with a bridge, the rules in the way of starting the rotation straight away; with no path,
  also what waiting on the present crop would break. Empty otherwise.
- `carry_on`: the current pattern continued. `pattern` is what repeats (next year is `pattern[0]`);
  `repeating` false means the last four years (`basis_years`) are not a repeating pattern and are repeated
  as they are, and `carry_on_note` says so. `years[]` as in `path` with `role: "carry_on"`. Null, with a
  `carry_on_note`, when fewer than four real years of the six crops are on record; then `difference`,
  `long_run` and `sentences` are null too.
- `difference.years[]`: path minus carry on. `return.mid` is the difference of the two rounded returns
  shown; `low` / `high` are the lowest and highest climate model of the difference itself (both face the
  same weather in a model). `cumulative` is the running total. `bare_days` and `cumulative_bare_days` the
  same for bare days; `irrigation_in` only on irrigated fields.
- `difference.payback.status`: `"paid_back"` (the running total is below zero at some point and at or
  above zero from year `n` / `year` to the end), `"never_behind"`, `"not_within_span"` (below zero at the
  end), `"no_difference"`. `lowest` is the lowest running total and its year; `models_ahead_at_end` counts
  the climate models whose running total is at or above zero at the end.
- `long_run`: each rotation as a repeating cycle, as a plan card scores it (for a plan target
  `long_run.plan.return.mid` is the card's `numbers.profit_per_acre`). `difference.verdict`: `"plan"` or
  `"carry_on"` when that side returns more in every climate model, `"too_close"` when they disagree,
  `"same"` when the two rounded figures are equal. `irrigation_in` only on irrigated fields; `failed_share`
  only when both plant a crop.
- `sentences`, `summary`, `total_sentence`, `not_modelled` and `notes` are shown by the app word for word.

Errors: `400 {"detail": "<one plain sentence>"}` for `years` outside 4 to 12, a `plan` that is not 1 to 3,
both `plan` and `target`, a target `/score` would refuse, and the bad settings `/plans` refuses; `404` no
such field; `503 {"detail": "<one plain sentence>"}` when only the basic planner is available or the
engine failed.

## `GET /field/{id}/replay` (Phase 5: weather replay)

A plan or rotation run year by year through the observed weather of 2000-2024 with the planner's own crop
response, water balance, irrigation limit, rotation effects, prices and costs (README "Phase 5", item B).
Query: `plan` (1, 2 or 3; default 1) or `rotation` (2 to 6 crops as typed), not both, plus the `/plans`
parameters (`soil water money risk scenario horizon irrigated limit_in`). Weather does not depend on the
scenario or horizon; they only choose which plan is "plan N". Dollars are returns over direct costs per acre,
whole dollars, at today's prices and costs.

| Key | Meaning |
|---|---|
| `field_id`, `engine` (`"smart"`), `status` (`"ok"`) | |
| `settings` | `irrigated`, `irrigation_source`, `irrigation_basis`, `weights`, `scenario`, `horizon`, `profit_basis`, `irrigation_limit_mm`, `irrigation_limit_in`, `rotation_effects`, `what_if` (as in `/plans`) |
| `subject` | `source` (`plan` or `own`), `plan` (1-3 or null), `name`, `cycle` |
| `weather` | `source`, `first_year`, `last_year`, `years`, `normal_season_rain_mm` (mean April-September rain), `season`, `dry_rule` (sentence), `dry_years` (the driest fifth of the years by that rain, from our own rule), `rows[]` (`year`, `season_rain_mm`, `rain_vs_normal_pct`, `hot_days`, `dry`) |
| `plan` | the replayed rotation, a series (below) |
| `carry_on` | the field's current pattern (`transition.carry_on_pattern`) as a series, plus `repeating`; null with fewer than four years in a row of the six crops |
| `comparison` | `plan` against `carry_on`, year by year; null without `carry_on`. `against`, `years[]`, `difference[]` (plan minus carry on), `carry_return[]`, `mean_difference`, `median_difference`, `years_ahead`, `years_behind`, `years_same`, `total`, `worst_year`, `median_return`, `below_cost_share`, `below_cost_years`, `failed_years` (each `{plan, carry_on}`), `sentences[]` |
| `notes[]`, `not_modelled[]` | plain sentences: history is not a forecast, how the phase was chosen, the dry rule, prices and costs are today's, the irrigation limit, what the replay leaves out |

A series (`plan`, `carry_on`): `name`, `cycle` (the repeating unit), `phases` (its length: one starting year per
phase), `phase` (`known`: true when the field has a crop history, then the bars are the cycle lined up so its first
year is `first_year`, the year after the field's last year on record; `starts_with`, `shown`, `count`), `basis`
(`this_start`, or `all_starts` when the phase is not known: the bars are the average of every phase), `years[]`,
`summary`, `all_starts` (the same pooled over every phase and year), `effects_range` (worst / median return and
share under the line under the strong and weak end of the rotation effects; null when they do not move),
`worst_years` (the three worst years of the bars), `worst_in_dry_years`, `model_error[]` (`crop`,
`typical_miss_pct`: the crop model's own leave-one-year-out error as a share of a normal yield, `status`), `sentences[]`.

`years[]` rows: `year`, `dry`, `worst_rank` (1-3 or null), `crop` (the crop of the shown phase; null when
averaged), `return`, `low` and `high` (over every phase), `yield_pct` (percent of a normal yield; null for fallow),
`yield` (per acre), `fail_chance` (the crop model's chance that the crop failed), `failed` (yield under half of
normal in the model; null when averaged), `failed_phases` (how many phases fail that year), `water_in` (inches
pumped; irrigated only), `water_met` (share of the crop's water need met), `below_cost` (return under $0),
`crops_by_phase`, `returns_by_phase`.

`summary`: `years`, `crop_years`, `worst_year` and `best_year` (`year`, `return`, `crop`), `median_return`,
`mean_return`, `below_cost_years`, `below_cost_share`, `planted_years`, `failed_years`, `failed_share`, `dry`
(`years`, `mean_return`, `other_mean_return`, `below_cost_years`, `failed_years`, `mean_yield_pct`,
`other_mean_yield_pct`).

Errors: `400` (one sentence) for a `plan` outside 1 to 3, both `plan` and `rotation`, a rotation we cannot read,
or bad `/plans` settings; `404` no such field; `503` (one sentence) when only the basic planner is available or the
observed weather record (`climate_history.parquet` with its observed monthly rows) is missing.

## `POST /farm/plan` (Phase 4: whole-farm planner)

Several fields planned together as one farm (`api/planner/farm.py`). Needs the smart planner (503 with a
sentence otherwise). JSON body:

| key | type | meaning |
|---|---|---|
| `field_ids` | list of 1 to 60 field ids, each once | the farm (required) |
| `weights` | `{soil, water, money, risk}`, any of them | the priorities, as the query parameters of `/plans`; a missing one is the default |
| `scenario`, `horizon` | as `/plans` | default `ssp245`, 2025 |
| `water_budget_acre_in` | number >= 0 or null | the most the whole farm may pump in any one year, acre-inches |
| `min_income` | number or null | the least the whole farm must return over direct costs in every year, dollars (a return over direct costs, not income or profit; may be negative) |
| `irrigated` | `{field_id: true/false}` | overrides our irrigated / dryland guess for those fields |

Errors: 400 `{"detail": one plain sentence}` (not a JSON object, an unknown key, no or bad `field_ids`, more
than 60 fields, a field listed twice, bad weights / scenario / horizon / budget / minimum, an `irrigated`
entry that is not true/false or names a field outside the farm); 404 unknown field; 503 basic planner or the
planner failed. The solve can take up to about 10 seconds (`solver.time_limit_s`); a 12-field farm usually
answers in well under one. The same request asked again returns the remembered answer.

Answer (real example: 8 dryland and 4 irrigated neighbours around KS-013847, budget 1,200, minimum 20,000;
lists shortened):

```json
{
  "engine": "smart",
  "settings": {"weights": {"soil": 0.3, "water": 0.2, "money": 0.3, "risk": 0.2}, "scenario": "ssp245",
               "horizon": 2025, "window": [2015, 2034], "models": ["ACCESS-CM2", "..."],
               "ranking_rule": "...", "profit_basis": "...", "irrigation_limit_mm": 610.0,
               "irrigation_limit_in": 24.0, "soil_source": "effects", "span_years": 12,
               "water_budget_acre_in": 1200.0, "min_income": 20000.0, "max_fields": 60},
  "farm": {"fields": 12, "area_acres": 243.1, "area_ha": 98.36, "irrigated_fields": 4, "irrigated_acres": 63.9,
           "dryland_fields": 8, "dryland_acres": 179.1},
  "status": "limits_met",
  "limits_met": true,
  "summary": ["12 fields, 243 acres (4 irrigated fields on 64 acres, 8 dryland fields on 179 acres), planned together over 12 years.",
              "Both limits are met in every one of the 12 years.",
              "2 of the 12 fields take a different rotation from their own first pick and 4 more keep their first pick but start it in a different year.",
              "The farm's ranking score is 0.639, against 0.641 with each field planned alone.",
              "Each field planned alone would pump up to 1,418 acre-inches in a year (218 over the budget; over in 9 of 12 years) and return as little as $13,900 in a year ($6,100 under the minimum; under in 3 of 12 years)."],
  "limits": {
    "water": {"set": true, "limit": 1200.0, "unit": "acre-inches a year", "met": true,
              "binds": true, "why": "alone_breaks_it",
              "binds_sentence": "It binds: each field planned alone would break it, so rotations or start years had to change.",
              "tightest": {"n": 1, "value": 1190, "margin": 10},
              "alone": {"met": false, "tightest": {"n": 1, "value": 1418, "margin": -218},
                        "years_broken": [1, 2, 3, 5, 6, 7, 9, 10, 11]},
              "achievable": null, "score_without": null,
              "sentence": "The water budget of 1,200 acre-inches a year is met in every year: the heaviest pumping year (year 1) is 1,190 acre-inches, 10 acre-inches under the budget. It binds: ..."},
    "return": {"set": true, "limit": 20000.0, "unit": "dollars a year, return over direct costs", "met": true,
               "binds": true, "why": "alone_breaks_it", "binds_sentence": "...",
               "tightest": {"n": 6, "value": 20000, "margin": 0},
               "alone": {"met": false, "tightest": {"n": 3, "value": 13900, "margin": -6100}, "years_broken": [3, 6, 9]},
               "achievable": null, "score_without": null, "alone_ok": null, "sentence": "..."}
  },
  "plan": {
    "score": 0.639,
    "years": [{"n": 1, "water_acre_in": {"mid": 1190, "low": 1094, "high": 1256},
               "return": {"mid": 20300, "low": 15900, "high": 25400}, "water_ok": true, "return_ok": true,
               "acres": {"soybeans": 15, "winter wheat": 116, "sorghum": 21, "alfalfa": 35, "fallow": 57}}, "... 12 in all"],
    "average": {"water_acre_in": 1145, "return": 23200},
    "peak_water": {"n": 1, "value": 1190, "margin": 10},
    "worst_return": {"n": 6, "value": 20000, "margin": 0},
    "water_ok": true, "return_ok": true, "water_years_over": [], "return_years_under": [],
    "changed_fields": 2, "shifted_fields": 4
  },
  "alone": {"score": 0.641, "years": ["... as plan.years"], "average": {"water_acre_in": 1335, "return": 24600},
            "peak_water": {"n": 1, "value": 1418, "margin": -218}, "worst_return": {"n": 3, "value": 13900, "margin": -6100},
            "water_ok": false, "return_ok": false, "water_years_over": [1, 2, 3, 5, 6, 7, 9, 10, 11],
            "return_years_under": [3, 6, 9]},
  "score_cost": 0.002,
  "fields": [{
    "field_id": "KS-013881", "area_acres": 14.4, "area_ha": 5.84, "irrigated": true, "irrigation_source": "guess",
    "plan": {"name": "Soybeans – Winter wheat (alternating)", "rotation": ["..."], "cycle": ["soybeans", "winter wheat", "soybeans", "winter wheat"],
             "scores": {"...": "as /plans"}, "reason": "...", "futures": {"holds": 6, "total": 6}, "spread": {"...": "..."},
             "numbers": {"...": "as /plans"}, "rank": 7, "start_year": 2,
             "years": ["winter wheat", "soybeans", "winter wheat", "soybeans", "... 12 in all"]},
    "own_pick": {"name": "Alfalfa (3 yrs) – Soybeans", "...": "the same keys as plan", "rank": 1, "start_year": 1},
    "same_rotation": false, "same_start": false, "differs": true,
    "gives_up": {"score": -0.022, "profit_per_acre": -48, "bare_days": 17.6, "irrigation_in": -6.5},
    "sentence": "Not its own first pick (Alfalfa (3 yrs) – Soybeans): this rotation scores 0.022 lower for this field; it returns about $48 an acre a year less over direct costs, pumps about 6.5 inches a year less, leaves the soil bare about 18 days a year more."
  }, "... one per field, in the order asked"],
  "solver": {"name": "Google OR-Tools 9.15.6755 (mixed-integer program: SCIP, then CP-SAT)", "status": "optimal",
             "optimal": true, "stopped_early": false, "gap": 0.0, "gap_pct": 0.0, "bound": 0.638743, "score": 0.638743,
             "time_s": 0.35, "time_limit_s": 10.0,
             "stages": [{"stage": "score", "status": "optimal", "seconds": 0.29, "choices": 1036},
                        {"stage": "start_years", "status": "optimal", "seconds": 0.03, "choices": 36}],
             "groups": 12,
             "note": "Solved to proven optimality in 0.35 seconds: no other choice of rotations and start years meets the limits with a higher farm score."},
  "notes": ["..."],
  "not_modelled": ["..."]
}
```

- `status`: `"no_limits"` (neither limit given: every field keeps its own first pick), `"limits_met"`, or
  `"limits_not_met"` (the plan is the closest the farm can get; see `achievable`). `limits_met` is false only
  for the last.
- `summary`: plain sentences in reading order (the farm, the limits, what changed, the score, what each
  field alone would do, and the solver's note when it stopped at its time limit). Built from the numbers
  in the answer.
- **Years** are `n` = 1 to 12 from now, not calendar years: every year gets the average weather of the
  climate window. `{mid, low, high}` = mean, lowest and highest of the climate models for the WHOLE FARM in
  that year (as in `/transition`). Water is whole acre-inches; dollars are returns over direct costs rounded
  to $100. `water_ok` / `return_ok`: that year's `mid` against the limit, decided on the unrounded total
  (null when that limit is not set). `acres`: farm acres in each crop that year.
- `plan` / `alone`: the chosen plan, and every field in its own first pick started in year 1 of its cycle as
  its plan card shows it. `score` is the area-weighted ranking score (`scores.robust` of each field's
  rotation). `peak_water` = the heaviest pumping year, `worst_return` = the leanest year; `margin` is the room
  to the limit (negative = over the budget / under the minimum by that much; absent when the limit is not
  set). `*_years_over` / `*_years_under`: the years that break the limit. `plan.changed_fields`: fields in a
  different rotation from their own pick; `plan.shifted_fields`: fields in their own pick but another start
  year. `score_cost` = `alone.score - plan.score`.
- `limits.water` / `limits.return` (the latter is the request's `min_income`): `set`; `limit`; `met` (null when
  not set); `tightest` = the plan's `peak_water` / `worst_return`; `alone` = the same for each-field-alone with
  the years it breaks; `sentence` (null when not set).
  `binds` true / false / null with `why`:
  `"not_set"`, `"cannot_be_met"`, `"alone_breaks_it"` (each field alone would break it, so rotations or start
  years had to change), `"costs_score"` (each field alone meets it, but dropping it would let the farm score
  higher: `score_without`), `"limits_the_return"` (the budget is kept and is what stops the minimum return
  being met), `"no_cost"` (the plan would be no better without it), `"unknown"` (`binds` null: not settled in
  the time allowed). `binds_sentence` says the same in words.
  `achievable` (only when the limit cannot be met): `{"value", "proven", "bound"}` = the least the farm can
  pump in its heaviest year (whole acre-inches, rounded up) or the best it can return in its leanest year
  (rounded down to $100), so asking for exactly `value` can be met; `proven` false means the best found in
  the time allowed, with `bound` the solver's limit on the true value (or null).
  `limits.return.alone_ok` (only when the minimum cannot be met and a water budget is set): true = it could
  be met with no limit on water, false = not even then, null = not settled or not applicable.
  When both limits are set and cannot both be met, the water budget is kept first (our assumption, said in
  `notes`).
- `fields[]`, in the order asked: `plan` and `own_pick` are plan objects exactly as in `/plans` (`name`,
  `rotation`, `cycle`, `scores`, `reason`, `futures`, `spread`, `numbers`, `rank`) plus `start_year` (1-based
  year of `cycle` the field starts in; 1 = as the card shows) and `years` (the 12 crops). `own_pick` equals
  `plans[0]` of `GET /field/{id}/plans` for the same settings. `same_rotation`, `same_start`, `differs`
  (either changed). `gives_up` (null unless the rotation changed): chosen plan MINUS own pick, per acre per
  year: `score` (ranking score, zero or negative), `profit_per_acre` ($, return over direct costs),
  `bare_days`, `irrigation_in` (null on dryland). `sentence` says it in words. `irrigation_source` is
  `"override"` when the request set it.
- `solver`: `status` `"optimal"` (proven: every stage the plan rests on was solved to optimality) or
  `"time_limit"` (best plan found; `gap` = how much higher any plan that meets the limits, or gets as close
  to them, could still score, in score points; `gap_pct` in percent of `bound`). `stages[]`: `stage` is one
  of `each_alone` (nothing to solve), `score`, `peak_water`, `worst_return`, `worst_return_alone`,
  `score_closest`, `start_years`, `without_water_budget`, `without_min_return`; `status` `optimal` /
  `feasible` (time share used up, best found) / `infeasible` / `unknown`; `choices` = yes/no variables.
  `groups` = evaluations shared by fields of the same practice, soil erodibility and slope. `note`: one or
  two sentences on what the solver did.
- `notes` and `not_modelled`: sentences to show under the plan (what "each field alone" means, what a year
  is, the model range, what dollars are, the per-acre pumping limit, the order of the limits when they
  cannot both be met, what is left out).

## `GET /field/{id}/twin` (Phase 4: twin view)

A field and its best-matched look-alike neighbour from Phase 2B. Built by `api/twin.py` from
`data/pairs.parquet`, `data/effects.json`, `data/causal_fields.parquet` and the soil table named in
`effects.json` (so it has the same 503 as `/field/{id}/neighbors` until `make causal` has run). 404 for an
unknown field. A field in no matched pair gets 200 with `has_twin: false`.

The twin is the field's pair with the smallest `match_score` (ties: smaller `distance_km`, then `pair_id`).
Every difference is **this field minus its twin**. A pair is stored as A minus B; when this field is the B
side the sign flips and an interval swaps its ends (`comparison.as_stored` keeps the stored row untouched).
All sentences are written by the API, in one place, and never say a rotation caused or improved anything:
what the numbers are is a difference between look-alike neighbours in the days the soil sits bare.

```jsonc
{
  "field_id": "KS-007027", "is_fake": false,
  "outcome": { "column": "bare_days", "label": "...", "units": "days per year", "higher_is_better": false, "note": "..." },
  "switch_year": 2020, "design": { /* year ranges, as /neighbors */ }, "soil_years": [2016, ..., 2024],
  "this": {                       // the same shape for "twin"
    "field_id": "KS-007027", "rotation_pre": "diverse", "rotation_post": "diverse",
    "irrigation": "dryland", "soil_group": "...",
    "crop_history": { "2008": "fallow", ..., "2024": "corn" },        // 2008-2024 as in /field/{id}
    "bare_days": [ { "year": 2016, "value": 174.2 }, { "year": 2018, "value": null } ]   // every soil year; null = no reading (a gap)
  },
  "has_twin": true, "reason": null, "examples": [],      // no twin: has_twin false, reason + examples (demo pairs), and twin/match/measured/comparison are null
  "twin": { /* as "this" */ },
  "match": {
    "pair_id": "P00123", "pairs_of_this_field": 2,
    "matched_on": [ { "key": "irrigation|distance|slope|mapped_soil|baseline|crop_mix", "label": "...",
                      "value": 0.8, "limit": 5.0, "unit": "km", "text": "0.8 km apart (limit 5.0 km)" }, ... ],
    "closeness": { "score": 1.5, "worst_possible": 5, "closer_than_pct": 62, "n_pairs_in_run": 1098, "median_score": 1.9 },
    "distance_km": 0.8, "sentence": "..."
  },
  "measured": {                   // one pair; noisy on its own
    "pre_years": [2018, 2019], "post_years": [2020, 2024],
    "this": { "before": 224.1, "after": 157.9, "change": -66.2 }, "twin": { "before": 53.5, "after": 133.0, "change": 79.5 },
    "level_gap_after": 24.9,      // this after minus twin after: a level
    "difference_in_change": -145.8,   // this change minus twin change (the pair's `did`, oriented)
    "from_crops": -15.7, "beyond_crops": -130.1,   // the pair's `did_crops` / `did_beyond`, oriented
    "years_used": { "before": 2, "after": 5 }, "sentence": "..."
  },
  "comparison": {
    "name": "diverse vs wheat-sorghum-fallow", "a": "...", "b": "...", "this_is": "a",
    "this_rotation": "diverse", "twin_rotation": "wheat-sorghum-fallow",
    "n_pairs": 480,               // pairs of this comparison in pairs.parquet, this one included
    "estimate": { "effect": 5.7, "ci_low": -1.7, "ci_high": 12.0, "ci_excludes_zero": false },   // effects.json "all" row, oriented this minus twin; null under 10 pairs
    "from_crops": { /* same shape: what the crops alone predict */ },
    "as_stored": { "effect": 5.7, "ci_low": ..., "ci_high": ..., "n_pairs": 480, "p_value": ..., "ci_excludes_zero": false, "favours": null },
    "agreement": {
      "other_pairs": 479, "same_way": 228, "opposite_way": 251, "no_change": 0,   // same + opposite + no_change = other_pairs
      "this_pair_direction": "fewer",     // this field's change vs its twin's: fewer / more bare days; null if exactly zero
      "spread_of_other_pairs": { "n": 479, "median": ..., "p25": ..., "p75": ..., "min": ..., "max": ... },   // their changes, oriented
      "sentence": "228 of 479 other pairs point the same way: ..."
    },
    "placebo": {
      "fake_switch": { "rows": [ { "fake_switch_year": 2017, "effect": ..., "ci_low": ..., "ci_high": ..., "ci_excludes_zero": true } ], "n_failed": 1, "n": 3 },
      "shuffled_labels": { "p_value": 0.005, "null_p95_abs": 4.6, "n_shuffles": 200 },    // or null
      "county": { "comparisons_tested": 8, "comparisons_failed": 5 },
      "passed": false, "sentence": "..."
    },
    "sentence": "..."
  }
}
```

"Points the same way" = the other pair's stored `did` has the same sign as this pair's (same comparison, so the
same rotation sits on the same side); a `did` of exactly zero is counted as `no_change`. Numbers are rounded
to one decimal (bare days are measured to a tenth).

## `GET /layers` and `GET /layers/{name}` (Phase 4: map layers)

County-wide per-field values the map colours by (`api/layers.py`). Layer names, in this order:
`bare_days`, `erosion_risk`, `rotation_change`, `irrigation`.

**How to join.** Every `values` (and `detail`) array has one entry per field in the order of the
`features` array of `GET /fields`: `values[i]` belongs to `features[i]`. `field_count` and `fields_etag`
(the ETag header of `GET /fields`) are in every answer.

**Number layers** (`kind: "number"`: `bare_days`, `erosion_risk`) send whole numbers or `null`:
real value = `values[i] * value_scale` (`1` for bare days, `0.001` for the erosion index; bare days are
rounded half up). Class index of a value = how many entries of `breaks_scaled` are `<=` it.
**Class layers** (`kind: "class"`: `rotation_change`, `irrigation`) send the index into `classes`, and a
second array `detail` (the score gap in thousandths; the irrigation probability in whole percent).

`GET /layers` (always 200; about 2 kB):

```jsonc
{
  "field_count": 7255,
  "fields_etag": "W/\"f72bb73aa9a48d763981\"",
  "is_fake": false,
  "join": "values[i] (and detail[i]) belongs to features[i] of GET /fields: ...",
  "layers": [
    {
      "name": "bare_days", "url": "/layers/bare_days",
      "title": "Days the soil sat bare",
      "kind": "number",                     // "number" | "class"
      "status": "ready",                    // "ready" | "building" | "unavailable"
      "reason": null,                       // one sentence when not ready
      "unit": "days a year",                // null for class layers
      "value_scale": 1,
      "years": [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],   // null for layers without years
      "method": "...", "caveat": "...", "source": "data/soil_health.parquet",
      "field_count": 7255,
      "higher_is_worse": true,              // number layers only
      "rule": "value = values[i] * value_scale; ...",
      "breaks": [1, 20, 50, 100],           // number layers: in the real unit
      "breaks_scaled": [1, 20, 50, 100],    // the same in the unit of `values`
      "breaks_note": "Breaks are the 20th, 40th, 60th and 80th percentile ...",
      "classes": [                          // number layers: one more than the breaks
        {"index": 0, "label": "under 1", "min": null, "max": 1, "min_scaled": null, "max_scaled": 1},
        {"index": 1, "label": "1 to under 20", "min": 1, "max": 20, "min_scaled": 1, "max_scaled": 20}
        // ... {"index": 4, "label": "100 or more", "min": 100, "max": null, ...}
      ]
    },
    // erosion_risk: as above with "value_scale": 0.001, "unit": "relative index",
    //   "breaks": [0.002, 0.05, 0.2, 0.3], "breaks_scaled": [2, 50, 200, 300], and
    //   "inputs": {"erosion_climate_by_year": {"2016": 71, ..., "2022": 300, ...},   // 100 = 2000-2024 average
    //              "erosion_climate_note": "...", "fields_soil_assumed": 0,
    //              "fields_soil_assumed_note": "...", "index_cap": 3.0}
    {
      "name": "rotation_change", "kind": "class", "status": "ready", "years": null, "unit": null, "value_scale": 1,
      "ordered": true,                      // class layers: true when the classes run in a meaningful order
      "classes": [
        {"index": 0, "key": "same", "label": "Same plan: the field already follows our first pick", "no_value": false},
        {"index": 1, "key": "top_pick", "label": "Among our top picks: the field follows our second or third pick", "no_value": false},
        {"index": 2, "key": "not_higher", "label": "Our first pick does not score higher than what the field does now", "no_value": false},
        {"index": 3, "key": "within_spread", "label": "Our first pick scores a little higher, within the spread between climate models", "no_value": false},
        {"index": 4, "key": "clearly_higher", "label": "Our first pick scores clearly higher", "no_value": false},
        {"index": 5, "key": "unknown", "label": "Cannot tell: no four-year pattern on record", "no_value": true}
      ],
      "detail": {"key": "score_gap", "label": "First pick's ranking score minus the current pattern's",
                 "unit": "score points (0 to 1 scale)", "value_scale": 0.001, "note": "..."},
      "settings": {"weights": {"soil": 0.3, "water": 0.2, "money": 0.3, "risk": 0.2}, "scenario": "ssp245", "horizon": 2025}
      // while it is being worked out: "status": "building", "reason": "...",
      //   "progress": {"done": 1200, "total": 7255}, "retry_after_s": 2, and no classes / detail / settings
    },
    {
      "name": "irrigation", "kind": "class", "status": "ready", "years": null, "ordered": false,
      "classes": [{"index": 0, "key": "dryland", "label": "Dryland (our best guess)", "no_value": false},
                  {"index": 1, "key": "irrigated", "label": "Irrigated (our best guess)", "no_value": false},
                  {"index": 2, "key": "unknown", "label": "Not in the field traits table", "no_value": true}],
      "detail": {"key": "irrigated_prob", "label": "How likely the field is irrigated", "unit": "%", "value_scale": 1, "note": "..."}
    }
  ]
}
```

An unavailable layer has only the common keys (`name`, `title`, `kind`, `status`, `reason`, `unit`,
`value_scale`, `years: null`, `method`, `caveat`, `source`, `field_count`, `rule`, `higher_is_worse` or
`ordered`, `url`).

`GET /layers/{name}`: the layer's entry from the listing (without `url`), plus `fields_etag`, `is_fake`,
`join` and `series`. Query `year=2020` keeps that one year in `series` (400 for a year the layer does not
have, or for a layer without years). 404 for an unknown name. 200 with `"status": "unavailable"` and no
`series` when its table is missing. **202** with `"status": "building"`, `progress` and a `Retry-After`
header while `rotation_change` is being worked out (ask again). Headers as `GET /fields`: `ETag`
(send `If-None-Match` for a 304), `Cache-Control: no-cache`, gzip.

```jsonc
// number layer: one entry per year, ascending
"series": [
  {
    "year": 2016,
    "values": [54, 15, 144, 139, ...],     // field_count whole numbers or null, in the order of GET /fields
    "summary": {
      "fields": 7255, "fields_with_value": 6960, "fields_no_value": 295,
      "acres_with_value": 467566,
      "median": 27, "p25": 0, "p75": 92,     // real unit, of the fields with a value (null when there are none)
      "classes": [                           // one per class; shares are of the fields (acres) WITH a value
        {"index": 0, "fields": 1745, "acres": 103758, "share_fields": 0.251, "share_acres": 0.222}
      ]
    }
  }
]
// class layer: one entry, "year": null
"series": [
  {
    "year": null,
    "values": [0, 0, 3, 5, ...],             // class index per field
    "detail": [0, 0, 25, null, ...],         // see `detail` above; null = none
    "summary": {
      "fields": 7255, "fields_with_value": 5835, "fields_no_value": 1420,   // no value = the `no_value` class
      "acres": 498797,
      "classes": [                           // shares are of ALL fields (acres)
        {"index": 0, "fields": 1419, "acres": 150853, "share_fields": 0.196, "share_acres": 0.302, "detail_median": 0.0}
      ],
      "detail": {"fields": 5835, "median": 0.022, "p25": 0.0, "p75": 0.057}   // real unit of `detail`
    }
  }
]
```

Rotation-change classes (`classify_change` in `api/layers.py`), from the field's `GET /field/{id}/plans`
answer with no parameters: `unknown` when `current` is null; `same` when `current.is_candidate` and its
`cycle` is `plans[0].cycle` (gap 0); `top_pick` when it is `plans[1]` or `plans[2]`; otherwise with
gap = `plans[0].scores.robust - current.scores.robust`: `not_higher` when gap <= 0, `within_spread` when
gap <= the larger of the two `spread.overall.high - spread.overall.low`, else `clearly_higher`.

## Cache: `data/layers_cache/rotation_change.json` (optional, git-ignored)

Written by the API after it has run the planner for every field (`rotation_change_fake.json` for the
made-up fields; `$LAYERS_CACHE_DIR` moves the folder). Safe to delete: it is rebuilt on the next request.
`{"version", "key", "field_ids_sha1", "field_count", "built", "class_keys", "classes": [class index per
field], "gaps": [thousandths or null per field]}`, in the field order of `GET /fields`. `key` is a hash
of the planner's table versions (path, size and time stamp of every file the planner reads, its
settings), the fields file's ETag and the default priorities; a file with another key is ignored.

## `GET /advisor/status` and `POST /advisor/chat` (Phase 4: farm advisor)

Served by `api/advisor.py`. The advisor is Google Gemini (`GEMINI_MODEL`, default `gemini-flash-latest`; when that answers 5xx, 404, 429 or is slow the
request is run again on `gemini-3.5-flash`, then `gemini-3-flash-preview`; `ADVISOR_MODEL` is the older name for the same setting)
with nine tools (`api/advisor_tools.py`) that run this API's own endpoint functions in-process. Nothing is
stored on the server; the page sends the conversation with every question. The key is `GEMINI_API_KEY`, read
from the environment or the git-ignored `.env` at the repo root on every call. It is never returned.

`GET /advisor/status` -> `{available: bool, provider, model, reason, tools: [name, ...]}`. `reason` is null with a
key and "add GEMINI_API_KEY to .env" without one (`POST /advisor/chat` then answers 503 with that same sentence as
`detail`). Without a key `available` is false and the answer also adds `env_file` (".env"), `env_line` ("GEMINI_API_KEY=your-key-here"), `message` ("Add an
API key to use the advisor") and `how`.

`POST /advisor/chat`, request:

```
{"messages": [{"role": "user" | "assistant", "text": "..."}],      // ("content" also accepted) the last one is the question; at most 24 turns, 2000 characters each
 "context":  {"field_id": "KS-014376",                              // all keys optional
              "farm_field_ids": ["KS-...", ...],
              "weights": {"soil": 3, "water": 2, "money": 3, "risk": 2},
              "scenario": "ssp245" | "ssp585", "horizon": 2040,
              "irrigated": true | false,                            // the irrigation switch, for field_id
              "limit_in": 12}}                                      // the pumping what-if
```

Answer (200): `{answer: str, tool_calls: [{name, args, summary, ok}], grounding: {checked, matched, unmatched:
[{text, value, start, end}], passed}, model, corrected}`. `corrected` is true when the number check failed and the model was asked once to
rewrite the answer. `answer` may use `**bold**`, `*` / `-` list lines and blank
lines, nothing else. `grounding` is the number check of `api/advisor_grounding.py`: every number written in the
answer is looked up among the numbers of this request's tool results, the arguments, the farmer's own words
and the on-screen settings; `unmatched` lists those it cannot find, with `start` / `end` as JavaScript string
positions (`answer.slice(start, end) === text`).

Errors are `{detail: "<one plain sentence>", code}`: 400 `bad_request`, 503 `no_key`, 429 `busy`, 504
`timeout`, 502 `key` / `model` / `upstream` / `network` / `blocked` / `empty` / `refused`, 499 `cancelled`
(the browser went away), 500 `server`.

## `GET /climate`

What the smart planner assumes about the future. `{"available": true, "source", "models" (list of 6),
"window_rule", "baseline", "horizons", "scenarios", "observed"}`. `horizons` is 2025, 2030 ... 2060.
`baseline` is the pooled Today (`horizon`, `window`, `label`, `pooled_scenarios`, `values`, and
`values_by_scenario` kept for older clients). `scenarios` maps `ssp245` and `ssp585` to one row per
horizon: `horizon`, `window`, `label`, `changes`, `values`, `range` (lowest and highest model). `observed`
is `{years: [2000, 2024], values}`, the observed averages. When the tables have not been built: `{"available": false, "reason":
"not_built"}`. `GET /health` also reports `"climate": true|false`.

## Cache: `data/planner_bare_days.json` (optional, git-ignored)

Written only by `python -m api.planner.soil_effect` and the sanity script. Keys: `version`, `signature`
(name, size and modification time of the source tables; the cache is ignored when they differ), `built`,
`by_crop` (`practice -> crop -> bare days a year`), `by_prev` (`practice -> crop -> previous crop -> bare
days`), `n_crop`, `n_prev`, `info`.

## Errors, every endpoint (Phase 4, item 8)

Every error answer is JSON with `detail` as one plain sentence (a string, never a list or an object), so a page
can show it as it is. `400` bad input (a number out of range, an unknown crop, an empty body); `404` no such
field or layer; `422` a query value that cannot be read at all (`horizon=abc`): `{"detail": "The horizon must
be a whole number."}` (the name is the parameter's; "a number" and "true or false" for the other types); `503`
a table the answer needs has not been built (the sentence names the `make` command or the missing step);
`429`/`502`/`504` the advisor's outside service. A server fault is `500`; none is known, and
`tests/test_reliability.py` asks every endpoint with the data files missing and with bad input to keep it so.
`GET /fields` and `GET /layers...` answer `ETag` and honour `If-None-Match` (304); both are gzipped once.
The API does first-use work in the background right after it starts (`API_WARMUP=0` turns that off); it
changes no answer.
