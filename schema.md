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

## Tracked planner files (`api/planner/`)

Both are in git so a fresh clone works. Never edit `crop_response.json` by hand.

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
