"""Contract for data/field_satellite.parquet — the per-field, per-year satellite numbers.

This is a side file next to data/fields.parquet (which schema.md covers); it is optional and
built by `python pipeline/satellite.py` from Landsat via Google Earth Engine. This module has
no Earth Engine dependency, so the API can import it.

Plain Parquet, long format, one row per (field_id, year):

    field_id    string   matches a field_id in data/fields.parquet
    year        int      calendar year, e.g. 2008
    ndvi_peak   float    peak greenness: 90th percentile of NDVI over clear Landsat views from
                         1 March to 31 October, per pixel, averaged over the field. Null if no clear view.
    cover_frac  float    green cover: share (0-1) of the year's clear Landsat views in which the
                         pixel was green (NDVI >= GREEN_NDVI), averaged over the field. Null if no clear view.
    n_obs       float    mean number of clear Landsat views per pixel in the calendar year (>= 0)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SATELLITE_PATH = ROOT / "data" / "field_satellite.parquet"

COLUMNS = ["field_id", "year", "ndvi_peak", "cover_frac", "n_obs"]
GREEN_NDVI = 0.3  # a pixel counts as "green" in a view when its NDVI is at least this
SEASON = ("03-01", "10-31")  # window for ndvi_peak
SOURCE = "Landsat 5/7/8/9 Collection 2 surface reflectance (30 m) via Google Earth Engine"


class SatelliteSchemaError(ValueError):
    pass


def check_satellite(df) -> list[str]:
    """Every way `df` breaks the contract above (empty list = valid)."""
    if not isinstance(df, pd.DataFrame):
        return [f"expected a DataFrame, got {type(df).__name__}"]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        return [f"missing column(s): {', '.join(missing)}"]
    problems = []
    if len(df) == 0:
        return ["table has no rows"]
    if df["field_id"].isna().any() or not df["field_id"].map(lambda v: isinstance(v, str)).all():
        problems.append("field_id: must be non-null strings")
    if not pd.api.types.is_integer_dtype(df["year"]):
        problems.append(f"year: dtype is {df['year'].dtype}, expected integer")
    elif ((df["year"] < 1980) | (df["year"] > 2100)).any():
        problems.append("year: values outside 1980-2100")
    if df.duplicated(["field_id", "year"]).any():
        problems.append(f"{int(df.duplicated(['field_id', 'year']).sum())} duplicate (field_id, year) row(s)")
    for col, lo, hi in [("ndvi_peak", -1.0, 1.0), ("cover_frac", 0.0, 1.0), ("n_obs", 0.0, None)]:
        s = df[col]
        if not pd.api.types.is_float_dtype(s):
            problems.append(f"{col}: dtype is {s.dtype}, expected float")
            continue
        bad = (s < lo) | ((s > hi) if hi is not None else False)
        if bad.any():
            problems.append(f"{col}: {int(bad.sum())} value(s) outside [{lo}, {hi if hi is not None else 'inf'}]")
    if pd.api.types.is_float_dtype(df["n_obs"]) and df["n_obs"].isna().any():
        problems.append("n_obs: must not be null (use 0 when there was no clear view)")
    return problems


def validate_satellite(df, source: str = "") -> None:
    problems = check_satellite(df)
    if problems:
        where = f" ({source})" if source else ""
        raise SatelliteSchemaError(
            f"satellite table{where} does not match pipeline/satellite_schema.py:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def load_satellite(path: str | Path = SATELLITE_PATH) -> pd.DataFrame:
    """Read and validate the satellite file. Raises FileNotFoundError / SatelliteSchemaError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        raise SatelliteSchemaError(f"{path}: could not read as Parquet: {exc}") from exc
    validate_satellite(df, str(path))
    return df[COLUMNS]
