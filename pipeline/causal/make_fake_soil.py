"""Safety net for when there is no real soil table: write data/soil_health_fake.parquet.

    .venv/bin/python -m pipeline.causal.make_fake_soil

The file follows the REAL contract (schema.md, pipeline/soil/health_schema.py): one row per field
per year 2016-2024 with field_id, year, om_pred, n_obs, confidence, bare_days. It is checked with
the same validator before it is written, so the fallback runs through exactly the code the real
table does.

Every number is MADE UP, with PLANTED crop effects so the causal code can be checked:

* bare_days: each year's crop sets the level (BARE_DAYS_BY_CROP below, roughly the real medians),
  times a county-wide dry-year factor, plus a per-field offset and measurement noise.
* om_pred: each year's crop nudges organic matter by a fixed amount (OM_EFFECT_PER_YEAR), on top
  of the field's SSURGO value, a county-wide shock per year, a smooth regional drift and noise.
  As in the real table it is null (with n_obs 0 and confidence 0) when the field was never seen
  bare, which is most perennial years.

Also writes data/soil_health_fake_truth.parquet (the noiseless `om_pred` and `bare_days`), so the
run can report how close the estimate came to the planted value. When data/soil_health.parquet
(the real one) exists the causal run uses it instead and ignores both fake files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402

from pipeline.causal import config  # noqa: E402
from pipeline.causal.rotations import _crop_group  # noqa: E402
from pipeline.common import EQUAL_AREA_CRS  # noqa: E402
from pipeline.schema import FAKE_PATH, REAL_PATH, load_fields  # noqa: E402
from pipeline.soil.health_schema import COLUMNS, YEARS, check_soil_health  # noqa: E402

# Planted truth; NOT real findings.
BARE_DAYS_BY_CROP = {  # days bare in a year of this crop group, before the dry-year factor
    "fallow": 140.0,
    "corn": 50.0,
    "sorghum": 45.0,
    "soybeans": 40.0,
    "other": 35.0,
    "winter wheat": 25.0,
    "perennial": 6.0,
}
BARE_FIELD_SD = 4.0  # per-field offset, days
BARE_NOISE = (3.0, 0.2)  # noise SD = 3 days + 20% of the level
BARE_MIN = 2.0  # the noiseless series never goes below this, so the noise is rarely clipped at 0
BARE_NULL_SHARE = 0.005  # too few clear views to tell

OM_EFFECT_PER_YEAR = {  # OM % points added per year a crop group is grown
    "perennial": 0.040,
    "corn": 0.008,
    "sorghum": 0.004,
    "winter wheat": 0.002,
    "soybeans": -0.004,
    "fallow": -0.025,
    "other": 0.0,
}
OM_NOISE_SD = 0.10  # one bare view; the mean of n views gets OM_NOISE_SD / sqrt(n)
OM_YEAR_SHOCK_SD = 0.04
OM_FIELD_LEVEL_SD = 0.15
OM_NULL_SHARE = {"perennial": 0.6}  # share of field-years never seen bare; any other crop:
OM_NULL_SHARE_OTHER = 0.06
MAX_CONFIDENCE = 0.38  # the real table never exceeds its model's skill


def build(fields, seed: int = config.SEED) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(table in the real contract, noiseless truth with field_id, year, om_pred, bare_days)."""
    rng = np.random.default_rng(seed)
    crop_years = np.arange(config.FIRST_YEAR, max(YEARS) + 1)
    om_shock = dict(zip(crop_years, rng.normal(0, OM_YEAR_SHOCK_SD, len(crop_years))))
    dry = dict(zip(crop_years, rng.uniform(0.8, 1.5, len(crop_years))))  # dry years lift bare days
    cent = shapely.centroid(fields.to_crs(EQUAL_AREA_CRS).geometry.values)
    xs, ys = shapely.get_x(cent), shapely.get_y(cent)
    rows, truth = [], []
    for fid, hist_json, om, x, y in zip(fields["field_id"], fields["crop_history"], fields["organic_matter_pct"], xs, ys):
        hist = json.loads(hist_json)
        level = (1.7 if om != om else float(om)) + rng.normal(0, OM_FIELD_LEVEL_SD)
        drift = 0.006 * np.sin(x / 20_000) * np.cos(y / 15_000)  # per year, smooth in space
        bare_offset = rng.normal(0, BARE_FIELD_SD)
        for i, year in enumerate(crop_years):
            crop = hist.get(str(year))
            group = _crop_group(crop) if crop is not None else "other"
            if i > 0 and crop is not None:
                level += OM_EFFECT_PER_YEAR[group]
            if year not in YEARS:
                continue
            om_true = level + drift * i + om_shock[year]
            bare_true = max(BARE_MIN, BARE_DAYS_BY_CROP[group] * dry[year] + bare_offset)
            truth.append((fid, int(year), om_true, bare_true))

            bare = float(np.clip(bare_true + rng.normal(0, BARE_NOISE[0] + BARE_NOISE[1] * bare_true), 0.0, 366.0))
            if rng.random() < BARE_NULL_SHARE:
                bare = np.nan
            n_obs = float(1 + min(rng.poisson(2.5), 8))
            om_pred = max(om_true + rng.normal(0, OM_NOISE_SD / np.sqrt(n_obs)), 0.0)
            confidence = MAX_CONFIDENCE * (1 - np.exp(-n_obs / 2))
            if rng.random() < OM_NULL_SHARE.get(group, OM_NULL_SHARE_OTHER):
                om_pred, n_obs, confidence = np.nan, 0.0, 0.0
            rows.append((str(fid), int(year), om_pred, n_obs, confidence, bare))
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["year"] = df["year"].astype("int64")
    for col in ("om_pred", "n_obs", "confidence", "bare_days"):
        df[col] = df[col].astype("float64").round(4)
    problems = check_soil_health(df)
    if problems:  # the fake must never drift away from the real contract
        raise ValueError("fake soil table breaks the soil_health contract: " + "; ".join(problems))
    return df, pd.DataFrame(truth, columns=["field_id", "year", "om_pred", "bare_days"])


def main() -> None:
    fields_path = REAL_PATH if REAL_PATH.is_file() else FAKE_PATH
    fields = load_fields(fields_path)
    df, truth = build(fields)
    config.SOIL_FAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(config.SOIL_FAKE_PATH, index=False)
    truth.to_parquet(config.SOIL_FAKE_TRUTH_PATH, index=False)
    print(
        f"wrote {config.SOIL_FAKE_PATH.name}: {len(df):,} rows, {df.field_id.nunique():,} fields "
        f"from {fields_path.name} (FAKE soil data with planted effects)"
    )


if __name__ == "__main__":
    main()
