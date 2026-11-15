"""Load the per-field, per-year soil score table (Phase 2A's data/soil_health.parquet).

Contract (from Phase 2A): one row per field per year with
`field_id` (str), `year` (int), `om_pred` (float), `n_obs` (int), `confidence` (float, 0-1).
Phase 2A may add more score columns (e.g. a bare-soil exposure index); any of them can be the
outcome (see pipeline/causal/config.py). A year with no usable observation may be missing or
have a null score; both are treated the same.

Which file: data/soil_health.parquet when it exists, else data/soil_health_fake.parquet (made by
pipeline/causal/make_fake_soil.py), mirroring how the API falls back to the fake fields. A file
whose name contains "fake" is reported as fake everywhere downstream. $SOIL_PATH overrides.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.causal import config

REQUIRED = ["field_id", "year"]


def resolve_soil_path() -> Path | None:
    env = os.environ.get("SOIL_PATH")
    if env:
        return Path(env).expanduser()
    for path in (config.SOIL_REAL_PATH, config.SOIL_FAKE_PATH):
        if path.is_file():
            return path
    return None


def is_fake(path: Path | str | None) -> bool:
    return path is not None and "fake" in Path(path).stem.lower()


def load_soil(path: Path, column: str) -> pd.DataFrame:
    """Long table field_id, year, value, weight (rows with a null value dropped).

    weight = `confidence` when the file has it (clipped to [0.05, 1]), else 1.
    Raises ValueError with a readable message when the file does not fit.
    """
    df = pd.read_parquet(path)
    missing = [c for c in REQUIRED + [column] if c not in df.columns]
    if missing:
        have = ", ".join(map(str, df.columns))
        raise ValueError(f"{path.name} has no column(s) {', '.join(missing)} (it has: {have})")
    if not pd.api.types.is_numeric_dtype(df[column]):
        raise ValueError(f"{path.name}: outcome column {column!r} is {df[column].dtype}, expected numeric")
    out = pd.DataFrame(
        {
            "field_id": df["field_id"].astype(str),
            "year": pd.to_numeric(df["year"], errors="coerce"),
            "value": df[column].astype(float),
            "weight": df["confidence"].astype(float).clip(0.05, 1.0) if "confidence" in df else 1.0,
        }
    )
    out = out[np.isfinite(out["value"]) & out["year"].notna()]
    out["year"] = out["year"].astype(int)
    out["weight"] = out["weight"].fillna(0.05)
    return out.drop_duplicates(["field_id", "year"], keep="last").reset_index(drop=True)


def series_by_field(soil: pd.DataFrame) -> dict[str, dict[int, float]]:
    """field_id -> {year: value}, years ascending."""
    out: dict[str, dict[int, float]] = {}
    for fid, year, value in zip(soil["field_id"], soil["year"], soil["value"]):
        out.setdefault(fid, {})[int(year)] = float(value)
    return {fid: dict(sorted(s.items())) for fid, s in out.items()}
