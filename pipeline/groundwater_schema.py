"""Contract for data/groundwater.parquet (built by `python -m pipeline.groundwater`, `make groundwater`).

Measured depth to water in wells in and next to Finney County, from the USGS Water Data API (to 2015)
and the Kansas Geological Survey's WIZARD database (the same wells, carried on to this winter). This
module is the single validator. It imports nothing heavy (no geopandas, no requests), so the API can
use it. The file is optional: without it the app shows no water outlook and everything else works.

Plain Parquet, one row per well and day (a well measured twice on one day gets the mean of the two):

    well_id        string   the USGS monitoring-location id, e.g. "USGS-375018100171901" (never null)
    date           date     the day of the measurement (never null). When the record gives only the
                            month, the 15th of that month, and `date_exact` is false.
    date_exact     bool     true when the record gives the day
    depth_ft       float    depth to water below the land surface, feet (never null, 0 to 2,000)
    status         string   what the record says about the well at the time ("Static", "Pumping",
                            "Recently pumped", ...); null when it says nothing
    well_depth_ft  float    depth of the finished well, feet (the hole depth when only that is on record);
                            null when neither is known
    lon, lat       float    where the well is, degrees (never null)
    aquifer        string   the USGS national aquifer code ("N100HGHPLN" = High Plains aquifer), or for a
                            well only the KGS describes "KGS:<its geological unit or aquifer code>"
                            ("KGS:TO" = Ogallala); null when the record has none
    county_fips    string   five-digit county code of the well ("20055" = Finney); null when missing
    source         string   "usgs" or "kgs": whose database the reading was taken from (never null)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
GROUNDWATER_PATH = ROOT / "data" / "groundwater.parquet"
SOURCE = ("Depth to water measured in wells: USGS Water Data API (api.waterdata.usgs.gov, field measurements, "
          "parameter 72019) to 2015, and the Kansas Geological Survey WIZARD water-level database for the years since")
SOURCES = ("usgs", "kgs")

COLUMNS = ["well_id", "date", "date_exact", "depth_ft", "status", "well_depth_ft", "lon", "lat", "aquifer",
           "county_fips", "source"]
DEPTH_MAX_FT = 2000.0
HIGH_PLAINS = "N100HGHPLN"


class GroundwaterSchemaError(ValueError):
    pass


def check_groundwater(df) -> list[str]:
    """Every way `df` breaks the contract above (empty list = valid)."""
    if not isinstance(df, pd.DataFrame):
        return [f"expected a DataFrame, got {type(df).__name__}"]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        return [f"missing column(s): {', '.join(missing)}"]
    if len(df) == 0:
        return ["table has no rows"]
    problems = []
    ids = df["well_id"]
    if ids.isna().any() or not ids.map(lambda v: isinstance(v, str) and bool(v.strip())).all():
        problems.append("well_id: must be non-empty text")
    dates = pd.to_datetime(df["date"], errors="coerce")
    if dates.isna().any():
        problems.append(f"date: {int(dates.isna().sum())} value(s) are missing or not dates")
    elif df.assign(_d=dates).duplicated(["well_id", "_d"]).any():
        problems.append(f"{int(df.assign(_d=dates).duplicated(['well_id', '_d']).sum())} row(s) repeat a well and day")
    if not pd.api.types.is_bool_dtype(df["date_exact"]):
        problems.append(f"date_exact: dtype is {df['date_exact'].dtype}, expected bool")
    for col, lo, hi, nullable in (("depth_ft", 0.0, DEPTH_MAX_FT, False), ("well_depth_ft", 0.0, 10000.0, True),
                                  ("lon", -180.0, 180.0, False), ("lat", -90.0, 90.0, False)):
        s = df[col]
        if not pd.api.types.is_float_dtype(s):
            problems.append(f"{col}: dtype is {s.dtype}, expected float")
            continue
        values = s.to_numpy(dtype=float)
        if not nullable and not np.isfinite(values).all():
            problems.append(f"{col}: {int((~np.isfinite(values)).sum())} value(s) missing")
        out = np.isfinite(values) & ((values < lo) | (values > hi))
        if nullable:
            out |= np.isinf(values)
        if out.any():
            problems.append(f"{col}: {int(out.sum())} value(s) outside [{lo:g}, {hi:g}]")
    for col in ("status", "aquifer", "county_fips"):
        s = df[col]
        if not s.map(lambda v: v is None or (isinstance(v, float) and v != v) or isinstance(v, str)).all():
            problems.append(f"{col}: must be text or null")
    bad = ~df["source"].isin(SOURCES)
    if bad.any():
        problems.append(f"source: {int(bad.sum())} value(s) not in {SOURCES}")
    return problems


def validate_groundwater(df, source: str = "") -> None:
    problems = check_groundwater(df)
    if problems:
        where = f" ({source})" if source else ""
        raise GroundwaterSchemaError(
            f"groundwater table{where} does not match pipeline/groundwater_schema.py:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def load_groundwater(path: str | Path = GROUNDWATER_PATH) -> pd.DataFrame:
    """Read and validate the table. Raises FileNotFoundError / GroundwaterSchemaError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        raise GroundwaterSchemaError(f"{path}: could not read as Parquet: {exc}") from exc
    validate_groundwater(df, str(path))
    df = df[COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df


def summarize(df: pd.DataFrame) -> str:
    dates = pd.to_datetime(df["date"])
    per_well = df.groupby("well_id").size()
    depth_known = df.drop_duplicates("well_id")["well_depth_ft"].notna()
    return "\n".join([
        f"{len(df):,} measurements in {per_well.size:,} wells, {dates.min():%Y} to {dates.max():%Y} "
        f"({int((df['source'] == 'usgs').sum()):,} from the USGS, {int((df['source'] == 'kgs').sum()):,} from the KGS)",
        f"wells with 10 or more measurements: {int((per_well >= 10).sum()):,}",
        f"wells with a known well depth: {int(depth_known.sum()):,} of {len(depth_known):,}",
        f"median depth to water: {df['depth_ft'].median():.0f} ft",
    ])


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else GROUNDWATER_PATH
    try:
        df = load_groundwater(path)
    except FileNotFoundError:
        print(f"{path} does not exist (build it with: make groundwater)")
        return 1
    except GroundwaterSchemaError as exc:
        print(exc)
        return 1
    print(f"{path}: valid")
    print(summarize(df))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
