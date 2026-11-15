"""Model features from the bare-soil composites of pipeline/soil/bare_soil.py.

One row of data/soil_bare_obs.parquet is one (field, year, sensor family) composite: the reflectance
of the field's soil on the days it was seen bare. This module decides which composites are usable
and turns their six bands into the features of the organic-matter model (pipeline/soil/model.py).

Features (FEATURES), the same list for both sensor families:

    blue green red nir swir1 swir2     the composite reflectance, 0-1
    nbr2        (swir1 - swir2) / (swir1 + swir2)      residue and moisture raise it
    ndvi        (nir - red) / (nir + red)              greenness left in the "bare" composite
    bsi         ((swir1 + red) - (nir + blue)) / ((swir1 + red) + (nir + blue))    bare soil index
    brightness  sqrt(mean of the six squared bands)    organic matter and moisture darken soil
    vis         (blue + green + red) / 3               visible albedo
    swir_ratio  swir2 / swir1                          clay / carbonate / residue absorption near 2.2 um
    swir_nir    (swir1 - nir) / (swir1 + nir)          slope of the spectrum from nir to swir
    redness     (red - green) / (red + green)          colour index: iron oxides against organic matter
    saturation  (red - blue) / (red + blue)            spectral slope across the visible
    ri          red^2 / (blue x green^3)               redness index (hematite); used by the trees only

Nothing here is a coordinate and nothing comes from SSURGO: the model may only look at the soil.
Sentinel-2's red-edge bands are left out on purpose, so the two families see the same kind of
input and their predictions can be compared as two measurements of one thing.

A composite is usable (usable()) when the field kept at least one pixel, it had at least one
bare-soil view, all six bands are in (0, 1] and the composite passes the snow / bright-cloud screen
NDSI = (green - swir1) / (green + swir1) < NDSI_MAX (-0.25, the same limit bare_soil.py applies to
single views). On a table built with the per-view screen this last test removes next to nothing; on
a table built without it (the first prototype) it drops the composites that are mostly snow.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")
INDICES = ("nbr2", "ndvi", "bsi", "brightness", "vis", "swir_ratio", "swir_nir", "redness", "saturation", "ri")
FEATURES = BANDS + INDICES
SENSORS = ("landsat", "s2")
SENSOR_LABEL = {"landsat": "Landsat 8/9", "s2": "Sentinel-2"}
NDSI_MAX = -0.25  # same value as pipeline.soil.bare_soil.NDSI_MAX (a test keeps them equal)

OBS_COLUMNS = ("field_id", "year", "sensor", "n_px", "n_clear", "n_bare", "bare_px_frac", "bare_days", *BANDS)


class ObsError(ValueError):
    """The bare-soil table does not look like the output of pipeline/soil/bare_soil.py."""


def check_obs(obs: pd.DataFrame) -> None:
    missing = [c for c in OBS_COLUMNS if c not in obs.columns]
    if missing:
        raise ObsError(f"bare-soil table has no column(s) {missing}; expected the output of pipeline/soil/bare_soil.py")
    unknown = sorted(set(obs["sensor"].dropna().unique()) - set(SENSORS))
    if unknown:
        raise ObsError(f"bare-soil table has unknown sensor(s) {unknown}; expected {list(SENSORS)}")
    if obs.duplicated(["field_id", "year", "sensor"]).any():
        raise ObsError("bare-soil table has duplicate (field_id, year, sensor) rows")


def _ratio(a, b) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    out[~np.isfinite(out)] = np.nan
    return out


def composite_ndsi(obs: pd.DataFrame) -> np.ndarray:
    g, s1 = obs["green"].to_numpy(dtype=float), obs["swir1"].to_numpy(dtype=float)
    return _ratio(g - s1, g + s1)


def usable(obs: pd.DataFrame) -> np.ndarray:
    """True for composites the model may use (see the module docstring)."""
    bands = obs[list(BANDS)].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        in_range = (np.isfinite(bands) & (bands > 0) & (bands <= 1)).all(axis=1)
        seen = (obs["n_px"].to_numpy(dtype=float) > 0) & (obs["n_bare"].to_numpy(dtype=float) > 0)
        no_snow = composite_ndsi(obs) < NDSI_MAX
    return in_range & seen & no_snow


def add_features(obs: pd.DataFrame) -> pd.DataFrame:
    """The FEATURES columns for every row of `obs` (same index); NaN where a band is missing."""
    b = {name: obs[name].to_numpy(dtype=float) for name in BANDS}
    out = pd.DataFrame({name: b[name] for name in BANDS}, index=obs.index)
    out["nbr2"] = _ratio(b["swir1"] - b["swir2"], b["swir1"] + b["swir2"])
    out["ndvi"] = _ratio(b["nir"] - b["red"], b["nir"] + b["red"])
    out["bsi"] = _ratio((b["swir1"] + b["red"]) - (b["nir"] + b["blue"]), (b["swir1"] + b["red"]) + (b["nir"] + b["blue"]))
    out["brightness"] = np.sqrt(np.mean([b[name] ** 2 for name in BANDS], axis=0))
    out["vis"] = (b["blue"] + b["green"] + b["red"]) / 3.0
    out["swir_ratio"] = _ratio(b["swir2"], b["swir1"])
    out["swir_nir"] = _ratio(b["swir1"] - b["nir"], b["swir1"] + b["nir"])
    out["redness"] = _ratio(b["red"] - b["green"], b["red"] + b["green"])
    out["saturation"] = _ratio(b["red"] - b["blue"], b["red"] + b["blue"])
    out["ri"] = _ratio(b["red"] ** 2, b["blue"] * b["green"] ** 3)
    return out[list(FEATURES)]


def prepare(obs: pd.DataFrame) -> pd.DataFrame:
    """`obs` plus a boolean `usable` column and the FEATURES (NaN on unusable rows)."""
    check_obs(obs)
    out = obs.reset_index(drop=True).copy()
    out["year"] = out["year"].astype("int64")
    ok = usable(out)
    feats = add_features(out)
    feats.loc[~ok, :] = np.nan
    # the raw bands are replaced by the screened ones so that an unusable composite cannot leak in
    out = out.drop(columns=list(BANDS)).join(feats)
    out["usable"] = ok
    return out
