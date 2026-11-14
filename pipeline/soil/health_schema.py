"""Contract for data/soil_health.parquet and data/soil_health_meta.json (see schema.md).

The yearly soil table: one row per (field_id, year), built by `python -m pipeline.soil.build` from
bare-soil satellite views. This module is the single validator. It imports nothing heavy (no Earth
Engine, no scikit-learn), so the API can use it.

Plain Parquet, long format, every field in data/fields.parquet x every year in YEARS:

    field_id    string   matches a field_id in data/fields.parquet
    year        int      calendar year
    om_pred     float    estimated topsoil organic matter %, predicted from that year's bare-soil
                         reflectance by a model trained on SSURGO. Null when the field had no usable
                         bare-soil view that year.
    n_obs       float    mean number of bare-soil satellite views per pixel that year (>= 0, never null)
    confidence  float    0-1, how much to trust om_pred in this row (0 when om_pred is null, never null)
    bare_days   float    estimated days in the year the soil sat bare and exposed (no crop, no residue
                         cover), 0-366. Null when there were too few clear views to tell.

The meta file is a JSON object written next to the table:

    recommended   "om_pred" or "bare_days": which column is the trustworthy signal
    om_noise_sd   typical year-to-year jump of om_pred (one standard deviation, in OM %) on a field
                  whose crop did not change; a number >= 0, or null if it was not measured
    verdict       one or two plain sentences for the app
    (anything else, e.g. "model" and "noise" blocks with the full numbers, is free-form)
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SOIL_HEALTH_PATH = ROOT / "data" / "soil_health.parquet"
SOIL_HEALTH_META_PATH = ROOT / "data" / "soil_health_meta.json"

COLUMNS = ["field_id", "year", "om_pred", "n_obs", "confidence", "bare_days"]
YEARS = tuple(range(2016, 2025))
RECOMMENDED = ("om_pred", "bare_days")
SOURCE = "Sentinel-2 and Landsat 8/9 bare-soil views via Google Earth Engine; model trained on SSURGO"


class SoilHealthSchemaError(ValueError):
    pass


def check_soil_health(df) -> list[str]:
    """Every way `df` breaks the contract above (empty list = valid)."""
    if not isinstance(df, pd.DataFrame):
        return [f"expected a DataFrame, got {type(df).__name__}"]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        return [f"missing column(s): {', '.join(missing)}"]
    if len(df) == 0:
        return ["table has no rows"]
    problems = []
    if df["field_id"].isna().any() or not df["field_id"].map(lambda v: isinstance(v, str)).all():
        problems.append("field_id: must be non-null strings")
    if not pd.api.types.is_integer_dtype(df["year"]):
        problems.append(f"year: dtype is {df['year'].dtype}, expected integer")
    elif ((df["year"] < 1980) | (df["year"] > 2100)).any():
        problems.append("year: values outside 1980-2100")
    if df.duplicated(["field_id", "year"]).any():
        problems.append(f"{int(df.duplicated(['field_id', 'year']).sum())} duplicate (field_id, year) row(s)")
    floats_ok = True
    for col, lo, hi in [("om_pred", 0.0, 100.0), ("n_obs", 0.0, None), ("confidence", 0.0, 1.0), ("bare_days", 0.0, 366.0)]:
        s = df[col]
        if not pd.api.types.is_float_dtype(s):
            problems.append(f"{col}: dtype is {s.dtype}, expected float")
            floats_ok = False
            continue
        bad = (s < lo) | ((s > hi) if hi is not None else False)
        if bad.any():
            problems.append(f"{col}: {int(bad.sum())} value(s) outside [{lo}, {hi if hi is not None else 'inf'}]")
    if floats_ok:
        for col in ("n_obs", "confidence"):
            if df[col].isna().any():
                problems.append(f"{col}: must not be null (use 0)")
        orphan = df["om_pred"].isna() & (df["confidence"] > 0)
        if orphan.any():
            problems.append(f"confidence: {int(orphan.sum())} row(s) have confidence > 0 but no om_pred")
    return problems


def validate_soil_health(df, source: str = "") -> None:
    problems = check_soil_health(df)
    if problems:
        where = f" ({source})" if source else ""
        raise SoilHealthSchemaError(
            f"soil health table{where} does not match pipeline/soil/health_schema.py:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def load_soil_health(path: str | Path = SOIL_HEALTH_PATH) -> pd.DataFrame:
    """Read and validate the table. Raises FileNotFoundError / SoilHealthSchemaError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        raise SoilHealthSchemaError(f"{path}: could not read as Parquet: {exc}") from exc
    validate_soil_health(df, str(path))
    return df[COLUMNS]


def check_meta(meta) -> list[str]:
    if not isinstance(meta, dict):
        return [f"expected a JSON object, got {type(meta).__name__}"]
    problems = []
    if meta.get("recommended") not in RECOMMENDED:
        problems.append(f"recommended: must be one of {RECOMMENDED}, got {meta.get('recommended')!r}")
    noise = meta.get("om_noise_sd")
    if noise is not None and (isinstance(noise, bool) or not isinstance(noise, (int, float)) or not noise >= 0):
        problems.append(f"om_noise_sd: must be a number >= 0 or null, got {noise!r}")
    if not isinstance(meta.get("verdict"), str) or not meta["verdict"].strip():
        problems.append("verdict: must be a non-empty string")
    return problems


def load_meta(path: str | Path = SOIL_HEALTH_META_PATH) -> dict:
    """Read and validate the meta file. Raises FileNotFoundError / SoilHealthSchemaError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        meta = json.loads(path.read_text())
    except Exception as exc:
        raise SoilHealthSchemaError(f"{path}: could not read as JSON: {exc}") from exc
    problems = check_meta(meta)
    if problems:
        raise SoilHealthSchemaError(f"{path} does not match pipeline/soil/health_schema.py:\n" + "\n".join(f"  - {p}" for p in problems))
    return meta


def summarize(df: pd.DataFrame) -> str:
    years = sorted(df["year"].unique())
    have = df["om_pred"].notna()
    lines = [
        f"{len(df):,} rows: {df['field_id'].nunique():,} fields x {len(years)} years ({years[0]}-{years[-1]})",
        f"om_pred: {have.mean():.1%} of rows have a value; median {df['om_pred'].median():.2f}, "
        f"5th-95th percentile {df['om_pred'].quantile(0.05):.2f}-{df['om_pred'].quantile(0.95):.2f}",
        f"n_obs: median {df['n_obs'].median():.1f}; confidence: median {df.loc[have, 'confidence'].median():.2f} where om_pred exists",
        f"bare_days: {df['bare_days'].notna().mean():.1%} of rows have a value; median {df['bare_days'].median():.0f}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else SOIL_HEALTH_PATH
    try:
        df = load_soil_health(path)
    except FileNotFoundError:
        print(f"{path} does not exist (build it with: python -m pipeline.soil.build)")
        return 1
    except SoilHealthSchemaError as exc:
        print(exc)
        return 1
    print(f"{path}: valid")
    print(summarize(df))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
