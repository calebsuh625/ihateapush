"""Load the per-field, per-year soil table (Phase 2A's data/soil_health.parquet).

Contract (schema.md, pipeline/soil/health_schema.py): one row per field per year, 2016-2024, with
`field_id`, `year`, `om_pred`, `n_obs`, `confidence`, `bare_days`. Any numeric column can be the
outcome (see pipeline/causal/config.py); the default is `bare_days`. A year with a null value is
skipped, exactly like a missing row.

Which file: data/soil_health.parquet when it exists, else data/soil_health_fake.parquet (made by
pipeline/causal/make_fake_soil.py to the same contract), mirroring how the API falls back to the
fake fields. A file whose name contains "fake" is reported as fake everywhere downstream.
$SOIL_PATH overrides.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.causal import config
from pipeline.soil.health_schema import check_soil_health

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


def contract_problems(path: Path) -> list[str]:
    """Every way the file breaks the Phase 2A contract (empty list = fine)."""
    return check_soil_health(pd.read_parquet(path))


def load_soil(path: Path, column: str, weight_column: str | None = None) -> pd.DataFrame:
    """Long table field_id, year, value, weight (rows with a null value dropped).

    weight = 1, or `weight_column` clipped to [0.05, 1] when one is named and the file has it
    (config.Outcome.weight_column: `confidence` for om_pred, nothing for bare_days).
    Raises ValueError with a readable message when the file does not fit.
    """
    df = pd.read_parquet(path)
    missing = [c for c in REQUIRED + [column] if c not in df.columns]
    if missing:
        have = ", ".join(map(str, df.columns))
        raise ValueError(f"{path.name} has no column(s) {', '.join(missing)} (it has: {have})")
    if not pd.api.types.is_numeric_dtype(df[column]):
        raise ValueError(f"{path.name}: outcome column {column!r} is {df[column].dtype}, expected numeric")
    weighted = weight_column is not None and weight_column in df
    out = pd.DataFrame(
        {
            "field_id": df["field_id"].astype(str),
            "year": pd.to_numeric(df["year"], errors="coerce"),
            "value": df[column].astype(float),
            "weight": df[weight_column].astype(float).clip(0.05, 1.0).fillna(0.05) if weighted else 1.0,
        }
    )
    out = out[np.isfinite(out["value"]) & out["year"].notna()]
    out["year"] = out["year"].astype(int)
    return out.drop_duplicates(["field_id", "year"], keep="last").reset_index(drop=True)


def series_by_field(soil: pd.DataFrame) -> dict[str, dict[int, float]]:
    """field_id -> {year: value}, years ascending."""
    out: dict[str, dict[int, float]] = {}
    for fid, year, value in zip(soil["field_id"], soil["year"], soil["value"]):
        out.setdefault(fid, {})[int(year)] = float(value)
    return {fid: dict(sorted(s.items())) for fid, s in out.items()}
