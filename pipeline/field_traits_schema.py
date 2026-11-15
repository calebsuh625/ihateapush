"""Contract for data/field_traits.parquet (built by `python -m pipeline.field_traits`).

Per-field traits the planner needs and data/fields.parquet does not hold. This module is the single
validator. It imports nothing heavy (no Earth Engine, no geopandas), so the API can use it. The file
is optional: without it the planner guesses irrigation from crop history and treats erodibility as
unknown.

Plain Parquet, one row per field in data/fields.parquet:

    field_id          string   a field_id from data/fields.parquet (unique, never null)
    irrigation        string   "irrigated" or "dryland" (never null): our best guess
    irrigated_prob    float    0-1, how likely the field is irrigated (never null);
                               irrigation is "irrigated" exactly when irrigated_prob >= 0.5
    irrigation_basis  string   short plain text: what the guess rests on (never empty)
    weg               string   SSURGO wind erodibility group of the field's main soil: one of
                               1, 2, 3, 4, 4L, 5, 6, 7, 8 (1 blows most easily). Null when SSURGO has none.
    wei               float    SSURGO wind erodibility index, tons/acre/year (0-310). Null when SSURGO has none.
    et_gs_mm          float    mean April-September actual evapotranspiration 2016-2024, mm (OpenET
                               ensemble). Null when OpenET was not fetched for the field.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from pipeline.region import DATA_DIR as REGION_DATA_DIR  # noqa: E402  (data/ for Finney, data/regions/<fips>/ otherwise)

ROOT = Path(__file__).resolve().parents[1]
FIELD_TRAITS_PATH = REGION_DATA_DIR / "field_traits.parquet"

COLUMNS = ["field_id", "irrigation", "irrigated_prob", "irrigation_basis", "weg", "wei", "et_gs_mm"]
IRRIGATION = ("irrigated", "dryland")
WEG_VALUES = ("1", "2", "3", "4", "4L", "5", "6", "7", "8")
WEI_MAX = 310.0
ET_MAX_MM = 2000.0
PROB_CUT = 0.5


class FieldTraitsSchemaError(ValueError):
    pass


def check_field_traits(df, field_ids=None) -> list[str]:
    """Every way `df` breaks the contract above (empty list = valid).

    `field_ids` (optional): the ids of data/fields.parquet; then the table must hold exactly those.
    """
    if not isinstance(df, pd.DataFrame):
        return [f"expected a DataFrame, got {type(df).__name__}"]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        return [f"missing column(s): {', '.join(missing)}"]
    if len(df) == 0:
        return ["table has no rows"]
    problems = []
    ids = df["field_id"]
    if ids.isna().any() or not ids.map(lambda v: isinstance(v, str)).all():
        problems.append("field_id: must be non-null strings")
    elif ids.duplicated().any():
        problems.append(f"field_id: {int(ids.duplicated().sum())} duplicate(s)")
    elif field_ids is not None:
        want, have = set(field_ids), set(ids)
        if want - have:
            problems.append(f"field_id: {len(want - have)} field(s) of the fields table have no row")
        if have - want:
            problems.append(f"field_id: {len(have - want)} row(s) are not in the fields table")
    bad = ~df["irrigation"].isin(IRRIGATION)
    if bad.any():
        problems.append(f"irrigation: {int(bad.sum())} value(s) not in {IRRIGATION}")
    prob = df["irrigated_prob"]
    if not pd.api.types.is_float_dtype(prob):
        problems.append(f"irrigated_prob: dtype is {prob.dtype}, expected float")
    elif prob.isna().any() or ((prob < 0) | (prob > 1)).any():
        problems.append("irrigated_prob: must be non-null and in [0, 1]")
    elif not bad.any():
        clash = (prob >= PROB_CUT) != (df["irrigation"] == "irrigated")
        if clash.any():
            problems.append(f"irrigation: {int(clash.sum())} row(s) disagree with irrigated_prob (irrigated means >= {PROB_CUT})")
    basis = df["irrigation_basis"]
    if basis.isna().any() or not basis.map(lambda v: isinstance(v, str) and bool(v.strip())).all():
        problems.append("irrigation_basis: must be non-empty text")
    weg = df["weg"]
    bad_weg = weg.notna() & ~weg.isin(WEG_VALUES)
    if bad_weg.any():
        problems.append(f"weg: {int(bad_weg.sum())} value(s) not in {WEG_VALUES} (text, or null)")
    for col, hi in (("wei", WEI_MAX), ("et_gs_mm", ET_MAX_MM)):
        s = df[col]
        if not pd.api.types.is_float_dtype(s):
            problems.append(f"{col}: dtype is {s.dtype}, expected float")
        elif ((s < 0) | (s > hi)).any():
            problems.append(f"{col}: {int(((s < 0) | (s > hi)).sum())} value(s) outside [0, {hi:g}]")
    return problems


def validate_field_traits(df, source: str = "", field_ids=None) -> None:
    problems = check_field_traits(df, field_ids)
    if problems:
        where = f" ({source})" if source else ""
        raise FieldTraitsSchemaError(
            f"field traits table{where} does not match pipeline/field_traits_schema.py:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def load_field_traits(path: str | Path = FIELD_TRAITS_PATH) -> pd.DataFrame:
    """Read and validate the table. Raises FileNotFoundError / FieldTraitsSchemaError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        raise FieldTraitsSchemaError(f"{path}: could not read as Parquet: {exc}") from exc
    validate_field_traits(df, str(path))
    return df[COLUMNS]


def summarize(df: pd.DataFrame) -> str:
    irrigated = df["irrigation"] == "irrigated"
    sure = ((df["irrigated_prob"] >= 0.8) | (df["irrigated_prob"] <= 0.2)).mean()
    weg = df["weg"].value_counts()
    lines = [
        f"{len(df):,} fields: {int(irrigated.sum()):,} irrigated, {int((~irrigated).sum()):,} dryland "
        f"({sure:.0%} with probability above 0.8 or below 0.2)",
        f"weg: {df['weg'].notna().mean():.1%} have a value"
        + ("; most common " + ", ".join(f"{k} ({int(v):,})" for k, v in weg.head(4).items()) if len(weg) else ""),
        f"wei: {df['wei'].notna().mean():.1%} have a value"
        + (f"; median {df['wei'].median():.0f} tons/acre/year" if df["wei"].notna().any() else ""),
        f"et_gs_mm: {df['et_gs_mm'].notna().mean():.1%} have a value"
        + (f"; median irrigated {df.loc[irrigated, 'et_gs_mm'].median():.0f} mm, dryland {df.loc[~irrigated, 'et_gs_mm'].median():.0f} mm"
           if df["et_gs_mm"].notna().any() else ""),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else FIELD_TRAITS_PATH
    try:
        df = load_field_traits(path)
    except FileNotFoundError:
        print(f"{path} does not exist (build it with: python -m pipeline.field_traits)")
        return 1
    except FieldTraitsSchemaError as exc:
        print(exc)
        return 1
    print(f"{path}: valid")
    print(summarize(df))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
