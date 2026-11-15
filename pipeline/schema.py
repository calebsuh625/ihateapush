"""The data contract from schema.md, as code.

This is the single implementation of the rules in schema.md. The pipeline calls
`validate_fields` before writing, the API calls `load_fields` on startup.

    python pipeline/schema.py data/fields.parquet    # validate + print a summary
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/schema.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
from pipeline.region import DATA_DIR as REGION_DATA_DIR  # noqa: E402  (data/ for Finney, data/regions/<fips>/ otherwise)

DATA_DIR = REGION_DATA_DIR
REAL_PATH = DATA_DIR / "fields.parquet"
FAKE_PATH = ROOT / "data" / "fields_fake.parquet"  # Finney demo fields, shared

COLUMNS = ["field_id", "geometry", "crop_history", "organic_matter_pct", "slope_pct", "area_ha"]
FLOAT_COLUMNS = ["organic_matter_pct", "slope_pct", "area_ha"]
NULLABLE = {"organic_matter_pct", "slope_pct"}
CORE_CROPS = ["corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow"]

FIELD_ID_RE = re.compile(r"^[A-Z]{2}-\d{6}$")
YEAR_RE = re.compile(r"^\d{4}$")


class SchemaError(ValueError):
    """Raised when a fields table does not match schema.md."""

    def __init__(self, problems: list[str], source: str = ""):
        self.problems = problems
        self.source = source
        where = f" ({source})" if source else ""
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            f"fields table{where} does not match schema.md — {len(problems)} problem(s):\n{lines}"
        )


def parse_crop_history(value) -> dict[str, str]:
    """Parse one crop_history cell. Raises ValueError with a readable message if malformed."""
    if not isinstance(value, str):
        raise ValueError(f"expected a JSON string, got {type(value).__name__}")
    try:
        obj = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg})") from None
    if not isinstance(obj, dict):
        raise ValueError(f"JSON must be an object, got {type(obj).__name__}")
    if not obj:
        raise ValueError("no years")
    for year, crop in obj.items():
        if not YEAR_RE.match(year):
            raise ValueError(f"key {year!r} is not a 4-digit year")
        if not isinstance(crop, str) or not crop:
            raise ValueError(f"{year}: crop must be a non-empty string, got {crop!r}")
        if crop != crop.strip() or crop != crop.lower():
            raise ValueError(f"{year}: crop {crop!r} must be lowercase with no surrounding spaces")
    return obj


def _examples(ids, limit: int = 3) -> str:
    ids = [str(i) for i in ids]
    more = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
    return ", ".join(ids[:limit]) + more


def check_fields(gdf) -> list[str]:
    """Return every way `gdf` breaks the contract (empty list = valid)."""
    problems: list[str] = []
    if not isinstance(gdf, gpd.GeoDataFrame):
        return [f"expected a GeoDataFrame (GeoParquet), got {type(gdf).__name__}"]

    missing = [c for c in COLUMNS if c not in gdf.columns]
    if missing:
        problems.append(f"missing column(s): {', '.join(missing)}")
    extra = [c for c in gdf.columns if c not in COLUMNS]
    if extra:
        problems.append(f"unexpected column(s): {', '.join(map(str, extra))}")
    if len(gdf) == 0:
        problems.append("table has no rows")
    if missing or len(gdf) == 0:
        return problems

    label = gdf["field_id"].astype(str)

    # --- field_id ---
    ids = gdf["field_id"]
    not_str = ~ids.map(lambda v: isinstance(v, str))
    if not_str.any():
        problems.append(f"field_id: {int(not_str.sum())} value(s) are not strings")
    else:
        bad = ~ids.map(lambda v: bool(FIELD_ID_RE.match(v)))
        if bad.any():
            problems.append(
                f'field_id: {int(bad.sum())} value(s) do not look like "KS-000123", e.g. {_examples(ids[bad])}'
            )
        dupes = ids[ids.duplicated()]
        if len(dupes):
            problems.append(f"field_id: {len(dupes)} duplicate(s), e.g. {_examples(dupes.unique())}")

    # --- geometry ---
    if gdf.geometry.name != "geometry":
        problems.append(f"active geometry column is {gdf.geometry.name!r}, expected 'geometry'")
    if gdf.crs is None:
        problems.append("geometry: no CRS set, expected EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        problems.append(f"geometry: CRS is {gdf.crs.to_string()}, expected EPSG:4326")
    geom = gdf.geometry
    empty = geom.isna() | geom.is_empty
    if empty.any():
        problems.append(f"geometry: {int(empty.sum())} null/empty, e.g. {_examples(label[empty])}")
    ok = ~empty
    not_poly = ok & (geom.geom_type != "Polygon")
    if not_poly.any():
        kinds = ", ".join(sorted(geom[not_poly].geom_type.unique()))
        problems.append(
            f"geometry: {int(not_poly.sum())} are not single Polygons ({kinds}), e.g. {_examples(label[not_poly])}"
        )
    invalid = ok & ~geom.is_valid
    if invalid.any():
        problems.append(f"geometry: {int(invalid.sum())} invalid, e.g. {_examples(label[invalid])}")
    if ok.any():
        minx, miny, maxx, maxy = geom[ok].total_bounds
        if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
            problems.append(
                f"geometry: bounds ({minx:.1f}, {miny:.1f}, {maxx:.1f}, {maxy:.1f}) are not lon/lat degrees"
            )

    # --- crop_history ---
    bad_hist: list[tuple[str, str]] = []
    for fid, value in zip(label, gdf["crop_history"]):
        try:
            parse_crop_history(value)
        except ValueError as exc:
            bad_hist.append((fid, str(exc)))
    if bad_hist:
        fid, why = bad_hist[0]
        problems.append(f"crop_history: {len(bad_hist)} bad value(s), e.g. {fid}: {why}")

    # --- numeric columns ---
    for col in FLOAT_COLUMNS:
        s = gdf[col]
        if not pd.api.types.is_float_dtype(s):
            problems.append(f"{col}: dtype is {s.dtype}, expected float")
            continue
        if col not in NULLABLE and s.isna().any():
            problems.append(f"{col}: {int(s.isna().sum())} null(s), e.g. {_examples(label[s.isna()])}")
        if (~s.isna() & ~s.map(lambda v: v == v and abs(v) != float("inf"))).any():
            problems.append(f"{col}: contains infinite values")
    if pd.api.types.is_float_dtype(gdf["organic_matter_pct"]):
        s = gdf["organic_matter_pct"]
        bad = (s < 0) | (s > 100)
        if bad.any():
            problems.append(f"organic_matter_pct: {int(bad.sum())} outside [0, 100], e.g. {_examples(label[bad])}")
    if pd.api.types.is_float_dtype(gdf["slope_pct"]):
        bad = gdf["slope_pct"] < 0
        if bad.any():
            problems.append(f"slope_pct: {int(bad.sum())} negative, e.g. {_examples(label[bad])}")
    if pd.api.types.is_float_dtype(gdf["area_ha"]):
        bad = gdf["area_ha"] <= 0
        if bad.any():
            problems.append(f"area_ha: {int(bad.sum())} not > 0, e.g. {_examples(label[bad])}")

    return problems


def validate_fields(gdf, source: str = "") -> None:
    """Raise SchemaError listing every problem, or return None if `gdf` matches schema.md."""
    problems = check_fields(gdf)
    if problems:
        raise SchemaError(problems, source)


def load_fields(path: str | Path):
    """Read a fields GeoParquet file and validate it. Raises SchemaError / FileNotFoundError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        gdf = gpd.read_parquet(path)
    except Exception as exc:  # plain parquet, corrupt file, missing geo metadata...
        raise SchemaError([f"could not read as GeoParquet: {exc}"], str(path)) from exc
    validate_fields(gdf, str(path))
    return gdf


def summarize(gdf) -> str:
    """Human-readable summary: counts, years, crop frequencies, nulls, value ranges."""
    histories = [json.loads(h) for h in gdf["crop_history"]]
    years = sorted({y for h in histories for y in h})
    crops = Counter(c for h in histories for c in h.values())
    total = sum(crops.values())
    lengths = [len(h) for h in histories]

    out = [f"fields: {len(gdf):,}"]
    if years:
        out.append(
            f"years covered: {years[0]}–{years[-1]} ({len(years)} distinct; "
            f"{min(lengths)}–{max(lengths)} per field)"
        )
    minx, miny, maxx, maxy = gdf.total_bounds
    out.append(f"bounds (lon/lat): {minx:.4f}, {miny:.4f} → {maxx:.4f}, {maxy:.4f}")
    out.append(f"crop frequencies ({total:,} field-years):")
    for crop, n in crops.most_common(15):
        out.append(f"  {crop:<36} {n:>8,}  {100 * n / total:5.1f}%")
    if len(crops) > 15:
        rest = sum(n for _, n in crops.most_common()[15:])
        out.append(f"  {'(' + str(len(crops) - 15) + ' other classes)':<36} {rest:>8,}  {100 * rest / total:5.1f}%")
    out.append("nulls:")
    for col in COLUMNS:
        out.append(f"  {col:<20} {int(gdf[col].isna().sum()):>6,}")
    out.append("value ranges (min / median / mean / max):")
    for col in FLOAT_COLUMNS:
        s = gdf[col].dropna()
        if len(s):
            out.append(f"  {col:<20} {s.min():8.2f} / {s.median():8.2f} / {s.mean():8.2f} / {s.max():8.2f}")
        else:
            out.append(f"  {col:<20} (all null)")
    out.append(f"total area: {gdf['area_ha'].sum():,.0f} ha")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else (REAL_PATH if REAL_PATH.exists() else FAKE_PATH)
    try:
        gdf = load_fields(path)
    except (SchemaError, FileNotFoundError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {path} matches schema.md\n")
    print(summarize(gdf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
