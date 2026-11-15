"""Safety net until Phase 2A lands: write data/soil_health_fake.parquet in 2A's schema.

    .venv/bin/python -m pipeline.causal.make_fake_soil

Columns: field_id, year, om_pred, n_obs, confidence (one row per field per observed year).
Every number is MADE UP. The series has a PLANTED crop effect so the causal code can be checked:
each year's crop nudges organic matter by a fixed amount (PLANTED_EFFECT_PER_YEAR below), on top
of the field's SSURGO value, a county-wide weather shock per year, a smooth regional drift,
and satellite noise. Years with no clear scene (n_obs = 0) are left out.

Also writes data/soil_health_fake_truth.parquet (the noiseless series, `om_true`), so the run
can report how close the estimate came to the planted value. When data/soil_health.parquet
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

# OM % points added per year a crop group is grown (planted truth; NOT a real finding)
PLANTED_EFFECT_PER_YEAR = {
    "perennial": 0.040,
    "corn": 0.008,
    "sorghum": 0.004,
    "winter wheat": 0.002,
    "soybeans": -0.004,
    "fallow": -0.025,
    "other": 0.0,
}
NOISE_SD = 0.10  # one clear scene; averages of n scenes get NOISE_SD / sqrt(n)
YEAR_SHOCK_SD = 0.04
FIELD_LEVEL_SD = 0.15


def build(fields, seed: int = config.SEED) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    years = np.arange(config.FIRST_YEAR, config.LAST_YEAR + 1)
    shock = dict(zip(years, rng.normal(0, YEAR_SHOCK_SD, len(years))))
    cent = shapely.centroid(fields.to_crs(EQUAL_AREA_CRS).geometry.values)
    xs, ys = shapely.get_x(cent), shapely.get_y(cent)
    rows, truth = [], []
    for fid, hist_json, om, x, y in zip(
        fields["field_id"], fields["crop_history"], fields["organic_matter_pct"], xs, ys
    ):
        hist = json.loads(hist_json)
        base = (1.7 if om != om else float(om)) + rng.normal(0, FIELD_LEVEL_SD)
        drift = 0.006 * np.sin(x / 20_000) * np.cos(y / 15_000)  # per year, smooth in space
        level = base
        for i, year in enumerate(years):
            crop = hist.get(str(year))
            if i > 0 and crop is not None:
                level += PLANTED_EFFECT_PER_YEAR[_crop_group(crop)]
            true_value = level + drift * i + shock[year]
            truth.append((fid, int(year), true_value))
            n_obs = int(min(rng.poisson(3.0), 8))
            if n_obs == 0:
                continue
            value = true_value + rng.normal(0, NOISE_SD / np.sqrt(n_obs))
            rows.append((fid, int(year), round(max(value, 0.0), 4), n_obs, round(1 - np.exp(-n_obs / 2), 3)))
    df = pd.DataFrame(rows, columns=["field_id", "year", "om_pred", "n_obs", "confidence"])
    df["year"] = df["year"].astype("int64")
    df["n_obs"] = df["n_obs"].astype("int64")
    return df, pd.DataFrame(truth, columns=["field_id", "year", "om_true"])


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
