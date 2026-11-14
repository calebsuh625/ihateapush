"""Steps 5.4-5.5: rebuild `data/fields.parquet` from real public data - the one command.

    python pipeline/run_all.py                     # also: make pipeline
    python pipeline/run_all.py --max-fields 5000   # spatially even subset (default: every field)
    python pipeline/run_all.py --allow-missing soil,slope

What it does, in order (every step is logged and timed):

 1. fields + crop history   `pipeline.fields.build_fields` (USDA CSB outlines, CDL 2008-2024;
                            falls back to segmenting the CDL itself if CSB cannot be had)
 2. organic matter          `pipeline.soil.organic_matter_pct` (SSURGO via Soil Data Access)
 3. slope                   `pipeline.slope.mean_slope_pct` (USGS 3DEP 1/3 arc-second DEM)
 4. area                    recomputed from the final outlines in EPSG:5070 (equal area)
 5. assemble                exactly the schema.md columns, in order, floats as float64
 6. validate                `pipeline.schema.validate_fields` - BEFORE anything is written
 7. write                   GeoParquet to a temporary file in the output folder, read back and
                            compared, then `os.replace`d over the target (atomic)
 8. re-read                 `pipeline.schema.load_fields` on the final file + comparison
 9. summary + sanity checks `pipeline.schema.summarize`, then PASS / WARN lines with numbers
10. side files              `<out stem>_provenance.json` and `<out stem>_source_ids.csv`
                            (not part of the data contract)

Everything downloaded is cached under data/raw/, so a second run makes no download.

Failure policy
--------------
* Fields / crop history failing is fatal (after `build_fields`' own CSB -> CDL-segmentation
  fallback).
* Soil or slope failing (network down, service error, no value for any field) is fatal too:
  a half-empty file is never written silently. `--allow-missing soil,slope` writes NaN in
  that column instead, with a loud warning (schema.md allows null there).
* A table that does not match schema.md is never written. Whenever a run fails before the
  final rename, the previous `data/fields.parquet` is left exactly as it was.

Exit code: 0 when the file was written (also with WARN sanity checks), 1 otherwise.

Field cap
---------
`--max-fields` defaults to no cap. Measured on 2026-11-14 for the whole county (7,255
fields): 7 s with everything cached; 85 s with empty caches except the national CSB zip
(17 CDL rasters 46 s, county extract from the zip 10 s, SSURGO 21 s, DEM 6 s), plus about
70 s once for the 3.8 GB CSB zip itself. That is far below the point where a cap would be
worth its bias, and 7,255 fields is inside what the API and the frontend are built for
(~10k). With `--max-fields N`, `build_fields` keeps a spatially even subset.

One WARN is expected with the 2008-2024 data
--------------------------------------------
"non-crop share by year: JUMP in 2024". The 2024 CDL itself maps more land as grass/pasture
than the years before it (33.0 % of the county's pixels, 28.1 % in 2023; fallow and alfalfa
shrink accordingly), so 7.0 % of the fields have grass/pasture as their 2024 crop against
0.2 % in 2023 - mostly small polygons (median 8 ha). CSB's own CDL2024 attribute says the
same for 496 of those 506 fields, and outlines segmented from the CDL show the same jump, so
it is the source data, not this pipeline. The histories are written as the CDL has them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
import sys
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/run_all.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402

from pipeline.cdl_codes import is_crop_name  # noqa: E402
from pipeline.common import (  # noqa: E402
    COUNTY_NAME,
    EQUAL_AREA_CRS,
    FIPS,
    MIN_FIELD_HA,
    STATE_ABBR,
    YEARS,
    log,
)
from pipeline.county import county_boundary  # noqa: E402
from pipeline.fields import SOURCES as FIELD_SOURCES  # noqa: E402
from pipeline.fields import build_fields, compact_history  # noqa: E402
from pipeline.schema import (  # noqa: E402
    COLUMNS,
    CORE_CROPS,
    FLOAT_COLUMNS,
    REAL_PATH,
    SchemaError,
    load_fields,
    summarize,
    validate_fields,
)
from pipeline.slope import mean_slope_pct  # noqa: E402
from pipeline.soil import organic_matter_pct  # noqa: E402

ALLOWABLE_MISSING = ("soil", "slope")
N_STEPS = 10

# rounding of the numeric columns (the sources are not more precise than this)
OM_DECIMALS = 3  # SSURGO om_r is given to 0.01-0.25 %
SLOPE_DECIMALS = 2  # realistic precision of a field mean is ~0.05-0.1 percentage points
AREA_DECIMALS = 2  # 0.01 ha = 100 m2, about a tenth of a CDL pixel

# Used only if the county outline cannot be loaded. Finney County, KS (Census Bureau).
NOMINAL_COUNTY_KM2 = {"20055": 3374.0}

# --- sanity check thresholds: what "plausible for southwest Kansas" means -----------------
CORE_SHARE_MIN = 0.75  # the six core crops together, share of field-years
STAPLE_SHARE_MIN = 0.05  # corn, winter wheat and sorghum each
NON_CROP_SHARE_MAX = 0.10  # grass/pasture and other non-crop classes, share of field-years
ALL_YEARS_SHARE_MIN = 0.99  # fields that have every year
MIN_YEARS_SLACK = 2  # no field may lack more than this many years
ROTATING_SHARE_MIN = 0.80  # fields with >= 2 distinct crops
STREAK_MEDIAN_MAX = 5  # median of the longest same-crop streak, years
MONOCULTURE_SHARE_MAX = 0.10  # fields with the same class in every year
YEAR_SINGLE_CLASS_MAX = 0.60  # one class taking this share of the fields in a year = a broken raster
YEAR_MIX_DISTANCE_MAX = 0.25  # total variation distance of a year's class mix from the all-years mix
NON_CROP_JUMP_MAX = 0.04  # year-to-year change of the non-crop share of fields
OM_RANGE = (0.3, 4.0)  # percent, surface horizon, southwest Kansas
OM_IN_RANGE_SHARE_MIN = 0.99
SLOPE_FLAT_PCT = 5.0
SLOPE_FLAT_SHARE_MIN = 0.90
SLOPE_MAX_PCT = 30.0
NULL_SHARE_MAX = 0.01
OVERLAP_SLIVER_M2 = 100.0  # an overlap larger than this (0.01 ha) is not a sliver
OVERLAP_NOISE_M2 = 1.0  # below this it is floating-point noise from reprojection
AREA_ROUNDING_HA = 0.006  # min area may fall short of --min-area-ha by rounding / reprojection
MEDIAN_AREA_RANGE_HA = (8.0, 80.0)  # "tens of hectares"
MAX_FIELD_HA = 1000.0  # a quarter section is 65 ha, a section 259 ha
COUNTY_SHARE_RANGE = (0.30, 0.85)  # total field area / county area
OUTSIDE_AREA_SHARE_MAX = 0.01  # field area lying outside the county outline
AREA_RECOMPUTE_TOLERANCE_HA = 0.01
AGREEMENT_MIN = 0.97  # raster majority vs CSB's own per-year CDL attribute, all years
AGREEMENT_YEAR_MIN = 0.95


class PipelineError(RuntimeError):
    """A step failed in a way that stops the run. `written` says whether the output file exists
    in its new form (only possible for failures after the atomic rename)."""

    def __init__(self, step: str, message: str, written: bool = False):
        self.step = step
        self.written = written
        super().__init__(message)


# --------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------
def _rel(path: Path) -> str:
    path = Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


class StepTimer:
    """Numbers, logs and times the steps of a run."""

    def __init__(self, total: int = N_STEPS):
        self.total = total
        self.n = 0
        self.seconds: dict[str, float] = {}
        self._t0 = time.monotonic()

    @contextmanager
    def step(self, key: str, title: str):
        self.n += 1
        label = f"step {self.n}/{self.total}"
        log(f"{label}: {title}")
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.seconds[key] = round(time.monotonic() - t0, 2)
        log(f"{label} done in {self.seconds[key]:.1f}s")

    def total_seconds(self) -> float:
        return round(time.monotonic() - self._t0, 2)


def jsonable(obj):
    """Make `obj` safe for `json.dumps(..., allow_nan=False)`: numpy scalars and arrays, NaN and
    infinity (-> null), paths, sets, tuples and timestamps."""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, (pd.Series, pd.Index)):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (datetime, pd.Timestamp)):
        return obj.isoformat()
    return str(obj)


def write_text_atomic(path: Path, text: str) -> None:
    """Write a text file through a temporary file in the same folder + `os.replace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_versions() -> dict:
    out: dict = {"python": platform.python_version(), "platform": platform.platform()}
    for name in ("geopandas", "pandas", "shapely", "pyproj", "pyogrio", "pyarrow", "rasterio", "numpy", "scipy",
                 "requests", "affine"):  # fmt: skip
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    try:
        out["GEOS"] = shapely.geos_version_string
    except Exception:  # pragma: no cover - attribute name differs between versions
        pass
    try:
        import pyogrio
        import pyproj
        import rasterio

        out["GDAL (pyogrio)"] = pyogrio.__gdal_version_string__
        out["GDAL (rasterio)"] = rasterio.__gdal_version__
        out["PROJ"] = pyproj.proj_version_str
    except Exception:  # pragma: no cover
        pass
    return out


def side_paths(out: Path) -> tuple[Path, Path]:
    """(provenance json, source-id csv) that belong to the output file `out`."""
    out = Path(out)
    return out.with_name(out.stem + "_provenance.json"), out.with_name(out.stem + "_source_ids.csv")


# --------------------------------------------------------------------------------------------
# steps 2-3: the two nullable columns
# --------------------------------------------------------------------------------------------
def aligned_floats(series, fields: gpd.GeoDataFrame, column: str) -> np.ndarray:
    """The values of a per-field Series as float64, in the row order of `fields`.

    The step functions promise "same index as `fields`"; anything else is an error here rather
    than a silently shifted column.
    """
    if not isinstance(series, pd.Series):
        raise TypeError(f"{column}: expected a pandas Series, got {type(series).__name__}")
    if len(series) != len(fields):
        raise ValueError(f"{column}: {len(series):,} values for {len(fields):,} fields")
    if not series.index.equals(fields.index):
        raise ValueError(f"{column}: the index of the result is not the index of the fields")
    values = pd.to_numeric(series, errors="raise").to_numpy(dtype="float64", na_value=np.nan)
    if np.isinf(values).any():
        raise ValueError(f"{column}: contains infinite values")
    return values


_EXPECTED_FAILURES = ("SoilDataError", "DemUnavailable", "DemFetchError", "ConnectionError", "Timeout", "HTTPError")


def optional_column(step: str, column: str, compute, fields: gpd.GeoDataFrame, allow_missing) -> tuple[np.ndarray, dict, str | None]:
    """Run one of the nullable-column steps. Returns (values, provenance, reason it is missing).

    Any failure - an exception, a malformed result, or no value for a single field - stops the
    run unless `step` is in `allow_missing`, in which case the column is all NaN and the reason
    is returned (and shouted about).
    """
    try:
        series = compute(fields)
        values = aligned_floats(series, fields, column)
        provenance = dict(getattr(series, "attrs", {}).get("provenance") or {})
        if len(values) and np.isnan(values).all():
            raise RuntimeError(f"the {step} step returned no value for any of the {len(values):,} fields")
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if type(exc).__name__ not in _EXPECTED_FAILURES:  # probably a bug, not the network: show where
            log(f"!!! {step}: unexpected error\n{traceback.format_exc().rstrip()}")
        if step not in allow_missing:
            raise PipelineError(
                step,
                f"the {step} step failed, so {column} cannot be filled in.\n  {reason}\n"
                f"  Nothing was written. Fix the cause and rerun (every download so far is cached), or rerun with\n"
                f"  --allow-missing {step} to write the file with {column} = null for every field.",
            ) from exc
        log("!" * 100)
        log(f"!!! WARNING: the {step} step FAILED and --allow-missing {step} was given.")
        log(f"!!! {column} will be NULL FOR EVERY FIELD in the output file.")
        log(f"!!! reason: {reason}")
        log("!" * 100)
        return np.full(len(fields), np.nan), {"error": reason, "allowed_missing": True}, reason
    return values, provenance, None


# --------------------------------------------------------------------------------------------
# steps 4-5: area and assembly
# --------------------------------------------------------------------------------------------
def equal_area_ha(geometry: gpd.GeoSeries) -> np.ndarray:
    """Area in hectares of every geometry, measured in EPSG:5070 (NAD83 / CONUS Albers)."""
    if geometry.crs is None:
        raise ValueError("geometries have no CRS")
    return geometry.to_crs(EQUAL_AREA_CRS).area.to_numpy(dtype="float64") / 10_000.0


def _rounded(values: np.ndarray, decimals: int, keep_positive: bool = False) -> np.ndarray:
    out = np.round(np.asarray(values, dtype="float64"), decimals)
    if keep_positive:  # never round a real, tiny area down to 0 (schema: area_ha > 0)
        tiny = (out <= 0) & (values > 0)
        out[tiny] = values[tiny]
    return out


def assemble(fields: gpd.GeoDataFrame, organic_matter, slope, area_ha) -> tuple[gpd.GeoDataFrame, np.ndarray]:
    """The table of schema.md: exactly its columns, in its order, sorted by field_id.

    Returns (table, order) where `order[i]` is the row of `fields` that became row i of the
    table, so that anything else aligned with `fields` can be put in the same order.
    """
    n = len(fields)
    for name, values in (("organic_matter_pct", organic_matter), ("slope_pct", slope), ("area_ha", area_ha)):
        if len(values) != n:
            raise ValueError(f"{name}: {len(values):,} values for {n:,} fields")
    geometry = fields.geometry
    if geometry.crs is None:
        raise ValueError("the field geometries have no CRS")
    if geometry.crs.to_epsg() != 4326:
        geometry = geometry.to_crs("EPSG:4326")
    table = gpd.GeoDataFrame(
        {
            "field_id": fields["field_id"].astype(str).to_numpy(dtype=object),
            "crop_history": fields["crop_history"].astype(str).to_numpy(dtype=object),
            "organic_matter_pct": _rounded(organic_matter, OM_DECIMALS),
            "slope_pct": _rounded(slope, SLOPE_DECIMALS),
            "area_ha": _rounded(area_ha, AREA_DECIMALS, keep_positive=True),
        },
        geometry=gpd.GeoSeries(geometry.to_numpy(), crs="EPSG:4326"),
    )
    order = np.argsort(table["field_id"].to_numpy(dtype=object), kind="stable")
    table = table.iloc[order].reset_index(drop=True)[COLUMNS]
    for col in FLOAT_COLUMNS:
        table[col] = table[col].astype("float64")
    return table, order


# --------------------------------------------------------------------------------------------
# steps 7-8: atomic write and round trip
# --------------------------------------------------------------------------------------------
def table_differences(expected: gpd.GeoDataFrame, actual: gpd.GeoDataFrame) -> list[str]:
    """Every way `actual` (read back from disk) differs from `expected` (what was written)."""
    problems = []
    if list(actual.columns) != list(expected.columns):
        problems.append(f"columns are {list(actual.columns)}, expected {list(expected.columns)}")
    if len(actual) != len(expected):
        problems.append(f"{len(actual):,} rows, expected {len(expected):,}")
    if problems:
        return problems
    if actual.crs is None or expected.crs is None or actual.crs.to_epsg() != expected.crs.to_epsg():
        problems.append(f"CRS is {actual.crs}, expected {expected.crs}")
    for col in ("field_id", "crop_history"):
        if actual[col].tolist() != expected[col].tolist():
            problems.append(f"{col} differs")
    for col in FLOAT_COLUMNS:
        if not pd.api.types.is_float_dtype(actual[col]):
            problems.append(f"{col} came back as {actual[col].dtype}")
        elif not np.array_equal(actual[col].to_numpy(dtype=float), expected[col].to_numpy(dtype=float), equal_nan=True):
            problems.append(f"{col} differs")
    same = shapely.equals_exact(actual.geometry.to_numpy(), expected.geometry.to_numpy(), 0.0)
    if not bool(np.all(same)):
        problems.append(f"geometry differs in {int((~same).sum()):,} row(s)")
    return problems


def write_fields_atomic(table: gpd.GeoDataFrame, out: Path) -> None:
    """Write `table` as GeoParquet to `out` without ever exposing a partial or wrong file.

    The table goes to a temporary file in the same folder, is read back with `load_fields`
    (schema check) and compared with what was meant to be written, and only then renamed over
    `out`. On any failure the temporary file is removed and `out` is untouched.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp-{os.getpid()}")
    try:
        table.to_parquet(tmp, index=False)
        differences = table_differences(table, load_fields(tmp))
        if differences:
            raise IOError("the file did not read back as written: " + "; ".join(differences))
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------------
# step 9: sanity checks
# --------------------------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str  # "PASS" | "WARN"
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


def _check(name: str, ok: bool, detail: str) -> Check:
    return Check(name, "PASS" if ok else "WARN", detail)


def history_frame(histories) -> pd.DataFrame:
    """crop_history strings -> one row per field, one column per year (str), None = no data."""
    parsed = [json.loads(h) for h in histories]
    years = sorted({y for h in parsed for y in h})
    return pd.DataFrame([[h.get(y) for y in years] for h in parsed], columns=years, dtype=object)


def longest_streaks(hist: pd.DataFrame) -> np.ndarray:
    """Longest run of consecutive years with the same class, per field (a missing year ends a run)."""
    values = hist.to_numpy(dtype=object)
    out = np.zeros(len(values), dtype=int)
    for i, row in enumerate(values):
        best = run = 0
        previous = None
        for crop in row:
            if crop is None or crop != crop:  # no data
                run, previous = 0, None
                continue
            run = run + 1 if crop == previous else 1
            previous = crop
            best = max(best, run)
        out[i] = best
    return out


def year_table(hist: pd.DataFrame) -> pd.DataFrame:
    """Share of fields (with data that year) per class: rows = class names, columns = years."""
    shares = {year: hist[year].dropna().value_counts(normalize=True) for year in hist.columns}
    return pd.DataFrame(shares).fillna(0.0)


def year_groups(table: pd.DataFrame) -> pd.DataFrame:
    """`year_table` condensed to the six core crops, grass/pasture, other crops, other non-crop."""
    rows = {}
    for crop in CORE_CROPS:
        rows[crop] = table.loc[crop] if crop in table.index else pd.Series(0.0, index=table.columns)
    rest = [name for name in table.index if name not in CORE_CROPS]
    grass = [name for name in rest if name == "grass/pasture"]
    other_crop = [name for name in rest if is_crop_name(name)]
    other_non = [name for name in rest if not is_crop_name(name) and name != "grass/pasture"]
    rows["grass/pasture"] = table.loc[grass].sum(axis=0)
    rows["other crops"] = table.loc[other_crop].sum(axis=0)
    rows["other non-crop"] = table.loc[other_non].sum(axis=0)
    return pd.DataFrame(rows).T[list(table.columns)]


def format_year_table(groups: pd.DataFrame) -> str:
    """The per-year class mix as text (percent of the fields that have data in that year)."""
    lines = ["class mix per year (% of fields):", "  " + " " * 16 + "".join(f"{y:>6}" for y in groups.columns)]
    for name, row in groups.iterrows():
        lines.append(f"  {name:<16}" + "".join(f"{100 * v:6.1f}" for v in row.to_numpy()))
    return "\n".join(lines)


def overlap_stats(geoms: np.ndarray) -> dict:
    """Pairwise overlaps between polygons in a metric CRS (spatial index, all pairs)."""
    n = len(geoms)
    out = {"pairs_checked": 0, "pairs_overlapping": 0, "pairs_beyond_sliver": 0, "largest_m2": 0.0, "worst_pair": None}
    if n < 2:
        return out
    a, b = shapely.STRtree(geoms).query(geoms, predicate="intersects")
    keep = a < b
    a, b = a[keep], b[keep]
    out["pairs_checked"] = int(len(a))
    if not len(a):
        return out
    area = shapely.area(shapely.intersection(geoms[a], geoms[b]))
    out["pairs_overlapping"] = int((area > OVERLAP_NOISE_M2).sum())
    out["pairs_beyond_sliver"] = int((area > OVERLAP_SLIVER_M2).sum())
    worst = int(np.argmax(area))
    out["largest_m2"] = float(area[worst])
    if area[worst] > OVERLAP_NOISE_M2:
        out["worst_pair"] = (int(a[worst]), int(b[worst]))
    return out


def county_outline() -> dict | None:
    """The county outline in EPSG:5070 with its area, or None when it cannot be loaded."""
    try:
        outline = county_boundary(FIPS)
        geom = outline.to_crs(EQUAL_AREA_CRS).geometry.iloc[0]
        return {"geometry": geom, "area_km2": float(geom.area) / 1e6, "source": outline.attrs.get("source")}
    except Exception as exc:
        log(f"!!! county outline unavailable ({type(exc).__name__}: {exc}); the checks against it are skipped")
        return None


def inside_share(geoms: np.ndarray, outline) -> tuple[np.ndarray, float]:
    """(share of each polygon's area that lies inside `outline`, how far any polygon reaches
    beyond it, in the units of the common metric CRS)."""
    shapely.prepare(outline)
    share = np.ones(len(geoms))
    reach = 0.0
    crossing = np.flatnonzero(~shapely.contains(outline, geoms))
    if len(crossing):
        share[crossing] = shapely.area(shapely.intersection(geoms[crossing], outline)) / shapely.area(geoms[crossing])
        vertices = shapely.points(shapely.get_coordinates(geoms[crossing]))
        reach = float(shapely.distance(vertices, outline).max())
    return share, reach


def sanity_checks(table: gpd.GeoDataFrame, *, min_area_ha: float = MIN_FIELD_HA, county: dict | None = None,
                  expected_years=None, context: dict | None = None) -> tuple[list[Check], dict]:  # fmt: skip
    """Plausibility checks on a finished fields table. Returns (checks, numbers behind them).

    `county`: result of `county_outline()` (or None). `expected_years`: the years every history
    should have. `context` (optional) carries what only the run knows: `fields_provenance`,
    `slope_provenance`, `missing` ({step: reason}), `build_area_ha` (areas reported by
    `build_fields`, aligned with `table`).
    """
    context = context or {}
    expected_years = [str(y) for y in (YEARS if expected_years is None else expected_years)]
    checks: list[Check] = []
    numbers: dict = {}
    n = len(table)
    ids = table["field_id"].astype(str)
    hist = history_frame(table["crop_history"])
    flat = Counter(v for v in hist.to_numpy(dtype=object).ravel().tolist() if isinstance(v, str))
    field_years = sum(flat.values())

    # --- crop mix over all field-years ---
    share = {name: count / field_years for name, count in flat.items()}
    core = sum(share.get(c, 0.0) for c in CORE_CROPS)
    numbers["core_share"] = core
    checks.append(_check(
        "core crops", core >= CORE_SHARE_MIN,
        f"{_pct(core)} of {field_years:,} field-years are corn/soybeans/winter wheat/sorghum/alfalfa/fallow "
        f"(expect >= {_pct(CORE_SHARE_MIN, 0)})",
    ))  # fmt: skip
    staples = {c: share.get(c, 0.0) for c in ("corn", "winter wheat", "sorghum")}
    checks.append(_check(
        "corn / wheat / sorghum", min(staples.values()) >= STAPLE_SHARE_MIN,
        ", ".join(f"{c} {_pct(v)}" for c, v in staples.items()) + f" of field-years (expect each >= {_pct(STAPLE_SHARE_MIN, 0)})",
    ))  # fmt: skip
    grass = share.get("grass/pasture", 0.0)
    other_non_crop = sum(v for name, v in share.items() if not is_crop_name(name) and name != "grass/pasture")
    numbers["non_crop_share"] = grass + other_non_crop
    checks.append(_check(
        "non-crop classes", grass + other_non_crop <= NON_CROP_SHARE_MAX,
        f"grass/pasture {_pct(grass)} + other non-crop classes {_pct(other_non_crop, 2)} of field-years "
        f"(expect <= {_pct(NON_CROP_SHARE_MAX, 0)} together)",
    ))  # fmt: skip

    # --- years ---
    per_field = hist.notna().sum(axis=1).to_numpy()
    all_years = float((per_field == len(expected_years)).mean()) if n else 0.0
    present = list(hist.columns)
    absent = [y for y in expected_years if y not in present]
    unexpected = [y for y in present if y not in expected_years]
    ok = (not absent and not unexpected and all_years >= ALL_YEARS_SHARE_MIN
          and per_field.min() >= len(expected_years) - MIN_YEARS_SLACK)  # fmt: skip
    detail = (f"{_pct(all_years, 2)} of fields have all {len(expected_years)} years "
              f"{expected_years[0]}-{expected_years[-1]}; fewest years in a field: {int(per_field.min())}")  # fmt: skip
    if absent:
        detail += f"; MISSING FROM EVERY FIELD: {', '.join(absent)}"
    if unexpected:
        detail += f"; unexpected years: {', '.join(unexpected)}"
    checks.append(_check("years per field", ok, detail))

    # --- rotation ---
    distinct = hist.nunique(axis=1).to_numpy()
    rotating = float((distinct >= 2).mean())
    numbers["rotating_share"] = rotating
    checks.append(_check(
        "fields that rotate", rotating >= ROTATING_SHARE_MIN,
        f"{_pct(rotating)} of fields have >= 2 distinct crops (expect >= {_pct(ROTATING_SHARE_MIN, 0)}); "
        f"median {int(np.median(distinct))} distinct crops per field, max {int(distinct.max())}",
    ))  # fmt: skip
    streaks = longest_streaks(hist)
    bins = [("1", 1, 1), ("2", 2, 2), ("3", 3, 3), ("4-5", 4, 5), ("6-9", 6, 9), ("10+", 10, 10**6)]
    dist = {label: float(((streaks >= lo) & (streaks <= hi)).mean()) for label, lo, hi in bins}
    mono = distinct == 1
    mono_crops = Counter(next(v for v in row if isinstance(v, str)) for row in hist[mono].to_numpy(dtype=object))
    numbers["streak_distribution"] = dist
    numbers["monoculture_fields"] = dict(mono_crops)
    mono_txt = f"{int(mono.sum()):,} field(s) ({_pct(mono.mean(), 2)}) have one class in every year"
    if mono_crops:
        mono_txt += ": " + ", ".join(f"{crop} {count}" for crop, count in mono_crops.most_common(4))
    checks.append(_check(
        "longest same-crop streak", np.median(streaks) <= STREAK_MEDIAN_MAX and mono.mean() <= MONOCULTURE_SHARE_MAX,
        f"median {int(np.median(streaks))} yr; " + ", ".join(f"{label} yr: {_pct(v, 0)}" for label, v in dist.items())
        + f"; {mono_txt}",
    ))  # fmt: skip

    # --- one year that looks different from the others ---
    per_year = year_table(hist)
    groups = year_groups(per_year)
    numbers["year_groups"] = {name: {y: round(float(v), 4) for y, v in row.items()} for name, row in groups.iterrows()}
    mean_mix = per_year.mean(axis=1)
    distance = 0.5 * per_year.sub(mean_mix, axis=0).abs().sum(axis=0)
    top_share = per_year.max(axis=0)
    top_class = per_year.idxmax(axis=0)
    worst_year = distance.idxmax()
    dominated = [f"{y} ({top_class[y]} {_pct(top_share[y], 0)})" for y in per_year.columns if top_share[y] > YEAR_SINGLE_CLASS_MAX]
    far = [f"{y} ({distance[y]:.2f})" for y in per_year.columns if distance[y] > YEAR_MIX_DISTANCE_MAX]
    numbers["year_mix_distance"] = {y: round(float(v), 4) for y, v in distance.items()}
    detail = (f"largest single-class share in any year: {top_class[top_share.idxmax()]} {_pct(top_share.max(), 0)} in "
              f"{top_share.idxmax()} (a broken raster would be > {_pct(YEAR_SINGLE_CLASS_MAX, 0)}); most unusual year: "
              f"{worst_year}, distance {distance[worst_year]:.2f} from the all-years mix (limit {YEAR_MIX_DISTANCE_MAX})")  # fmt: skip
    if dominated:
        detail += "; DOMINATED BY ONE CLASS: " + ", ".join(dominated)
    if far:
        detail += "; UNUSUAL MIX: " + ", ".join(far)
    checks.append(_check("per-year class mix", not dominated and not far, detail))

    non_crop = groups.loc["grass/pasture"] + groups.loc["other non-crop"]
    jumps = non_crop.diff().abs().dropna()
    jump_years = [f"{y} ({_pct(non_crop.iloc[i - 1])} -> {_pct(non_crop.iloc[i])})"
                  for i, y in enumerate(non_crop.index) if i > 0 and abs(non_crop.iloc[i] - non_crop.iloc[i - 1]) > NON_CROP_JUMP_MAX]  # fmt: skip
    detail = (f"share of fields mapped as grass/pasture or another non-crop class: {_pct(non_crop.min())} "
              f"({non_crop.idxmin()}) to {_pct(non_crop.max())} ({non_crop.idxmax()})")  # fmt: skip
    if len(jumps):
        detail += f"; largest year-to-year change {_pct(jumps.max())} points (limit {_pct(NON_CROP_JUMP_MAX, 0)})"
    if jump_years:
        detail += ("; JUMP in " + ", ".join(jump_years)
                   + " - look at that year's CDL (a change in the CDL product itself also shows up here)")  # fmt: skip
    checks.append(_check("non-crop share by year", not jump_years, detail))

    # --- soil and slope ---
    missing = context.get("missing") or {}
    om = table["organic_matter_pct"].dropna().to_numpy(dtype=float)
    if len(om):
        in_range = float(((om >= OM_RANGE[0]) & (om <= OM_RANGE[1])).mean())
        checks.append(_check(
            "organic matter", in_range >= OM_IN_RANGE_SHARE_MIN,
            f"min {om.min():.2f} / median {np.median(om):.2f} / max {om.max():.2f} %; {_pct(in_range, 2)} of fields within "
            f"{OM_RANGE[0]}-{OM_RANGE[1]} % (expect >= {_pct(OM_IN_RANGE_SHARE_MIN, 0)}; "
            f"{int((om < OM_RANGE[0]).sum())} below, {int((om > OM_RANGE[1]).sum())} above)",
        ))  # fmt: skip
    else:
        checks.append(_check("organic matter", False, "NO VALUE FOR ANY FIELD"))
    slope = table["slope_pct"].dropna().to_numpy(dtype=float)
    if len(slope):
        flat_share = float((slope < SLOPE_FLAT_PCT).mean())
        checks.append(_check(
            "slope", flat_share >= SLOPE_FLAT_SHARE_MIN and slope.max() <= SLOPE_MAX_PCT,
            f"min {slope.min():.2f} / median {np.median(slope):.2f} / max {slope.max():.2f} %; {_pct(flat_share)} of fields "
            f"below {SLOPE_FLAT_PCT:g} % (expect >= {_pct(SLOPE_FLAT_SHARE_MIN, 0)}); "
            f"{int((slope >= SLOPE_FLAT_PCT).sum()):,} field(s) at or above",
        ))  # fmt: skip
    else:
        checks.append(_check("slope", False, "NO VALUE FOR ANY FIELD"))

    nulls = {col: int(table[col].isna().sum()) for col in COLUMNS}
    numbers["nulls"] = nulls
    nullable_ok = all(nulls[c] <= NULL_SHARE_MAX * n for c in ("organic_matter_pct", "slope_pct"))
    required_ok = all(nulls[c] == 0 for c in COLUMNS if c not in ("organic_matter_pct", "slope_pct"))
    detail = (f"organic_matter_pct {nulls['organic_matter_pct']:,}, slope_pct {nulls['slope_pct']:,} of {n:,} fields "
              f"(expect <= {_pct(NULL_SHARE_MAX, 0)} each); other columns "
              f"{sum(v for c, v in nulls.items() if c not in ('organic_matter_pct', 'slope_pct'))}")  # fmt: skip
    for step, reason in missing.items():
        detail += f"; {step.upper()} STEP FAILED AND WAS ALLOWED TO BE MISSING ({reason})"
    checks.append(_check("nulls", nullable_ok and required_ok and not missing, detail))

    duplicates = int(ids.duplicated().sum())
    checks.append(_check("duplicate ids", duplicates == 0, f"{duplicates} duplicate field_id(s) among {n:,}"))

    # --- geometry ---
    metric = table.geometry.to_crs(EQUAL_AREA_CRS)
    geoms = metric.to_numpy()
    area_m2 = shapely.area(geoms)
    kinds = Counter(table.geometry.geom_type.tolist())
    invalid = int((~table.geometry.is_valid).sum())
    vertices = shapely.get_num_coordinates(geoms)
    epsg = table.crs.to_epsg() if table.crs is not None else None
    checks.append(_check(
        "geometry", set(kinds) == {"Polygon"} and invalid == 0 and epsg == 4326,
        f"{', '.join(f'{count:,} {kind}' for kind, count in kinds.items())}; {invalid} invalid; EPSG:{epsg}; "
        f"vertices per field: median {int(np.median(vertices))}, max {int(vertices.max())}",
    ))  # fmt: skip

    overlaps = overlap_stats(geoms)
    numbers["overlaps"] = {k: v for k, v in overlaps.items() if k != "worst_pair"}
    detail = (f"{overlaps['pairs_checked']:,} touching pair(s) checked with a spatial index: {overlaps['pairs_overlapping']} "
              f"overlap by > {OVERLAP_NOISE_M2:g} m2, {overlaps['pairs_beyond_sliver']} by > {OVERLAP_SLIVER_M2:g} m2 "
              f"(largest {overlaps['largest_m2']:,.1f} m2)")  # fmt: skip
    if overlaps["worst_pair"] and overlaps["pairs_beyond_sliver"]:
        i, j = overlaps["worst_pair"]
        detail += f", e.g. {ids.iloc[i]} / {ids.iloc[j]}"
    checks.append(_check("overlapping fields", overlaps["pairs_beyond_sliver"] == 0, detail))

    area = table["area_ha"].to_numpy(dtype=float)
    median_area = float(np.median(area))
    ok = (area.min() >= min_area_ha - AREA_ROUNDING_HA and MEDIAN_AREA_RANGE_HA[0] <= median_area <= MEDIAN_AREA_RANGE_HA[1]
          and area.max() <= MAX_FIELD_HA)  # fmt: skip
    checks.append(_check(
        "field area", ok,
        f"min {area.min():.2f} ha (--min-area-ha {min_area_ha:g}), median {median_area:.1f} ha (expect "
        f"{MEDIAN_AREA_RANGE_HA[0]:g}-{MEDIAN_AREA_RANGE_HA[1]:g}), mean {area.mean():.1f}, max {area.max():.1f} ha "
        f"(expect <= {MAX_FIELD_HA:,.0f})",
    ))  # fmt: skip
    stored_vs_geometry = float(np.abs(area - area_m2 / 1e4).max())
    built = context.get("build_area_ha")
    detail = f"area_ha vs the outline's area in {EQUAL_AREA_CRS}: max difference {stored_vs_geometry:.4f} ha (rounding to {10 ** -AREA_DECIMALS:g})"
    ok = stored_vs_geometry <= AREA_RECOMPUTE_TOLERANCE_HA
    if built is not None:
        versus_build = float(np.abs(area - np.asarray(built, dtype=float)).max())
        detail += f"; vs the area build_fields reported: max difference {versus_build:.4f} ha"
        ok = ok and versus_build <= AREA_RECOMPUTE_TOLERANCE_HA
    checks.append(_check("area recomputation", ok, detail))

    county_km2 = county["area_km2"] if county else NOMINAL_COUNTY_KM2.get(FIPS)
    total_km2 = float(area.sum()) / 100.0
    if county_km2:
        cover = total_km2 / county_km2
        numbers["share_of_county"] = cover
        largest = float(area.max()) / 100.0 / county_km2
        # with --max-fields only a (spatially even, size-blind) sample is in the file: judge the
        # share the whole set would have
        before_cap = ((context.get("fields_provenance") or {}).get("counts") or {}).get("fields_before_max_fields")
        capped = bool(before_cap) and before_cap > n
        judged = cover * before_cap / n if capped else cover
        cap_note = (f"; --max-fields kept {n:,} of {before_cap:,} fields, so all of them would cover about {_pct(judged)}"
                    if capped else "")  # fmt: skip
        checks.append(_check(
            "total area vs county", COUNTY_SHARE_RANGE[0] <= judged <= COUNTY_SHARE_RANGE[1],
            f"fields cover {total_km2:,.0f} km2 = {_pct(cover)} of {COUNTY_NAME} County's {county_km2:,.0f} km2"
            f"{'' if county else ' (nominal area; outline unavailable)'}{cap_note} (expect {_pct(COUNTY_SHARE_RANGE[0], 0)}-"
            f"{_pct(COUNTY_SHARE_RANGE[1], 0)}); the largest field is {_pct(largest, 2)} of the county",
        ))  # fmt: skip
    else:
        checks.append(_check("total area vs county", False, f"fields cover {total_km2:,.0f} km2; county area unknown, not checked"))
    if county:
        inside, reach_m = inside_share(geoms, county["geometry"])
        outside_area = float(((1 - inside) * area_m2).sum() / area_m2.sum())
        mostly_outside = int((inside < 0.5).sum())
        crossing = int((inside < 1.0).sum())
        numbers["fields_crossing_county_line"] = crossing
        numbers["reach_beyond_county_m"] = reach_m
        checks.append(_check(
            "inside the county", mostly_outside == 0 and outside_area <= OUTSIDE_AREA_SHARE_MAX,
            f"{n - crossing:,} of {n:,} fields entirely inside the county outline, {crossing} cross it (by at most "
            f"{reach_m:,.0f} m), {mostly_outside} lie mostly outside; {_pct(outside_area, 3)} of the field area is outside "
            f"(expect <= {_pct(OUTSIDE_AREA_SHARE_MAX, 0)})",
        ))  # fmt: skip
    else:
        checks.append(_check("inside the county", False, "county outline unavailable, not checked"))

    # --- what only the run knows ---
    fields_prov = context.get("fields_provenance") or {}
    agreement = (fields_prov.get("agreement_with_csb_attributes") or {}).get("final_fields")
    if agreement and agreement.get("overall", {}).get("rate") is not None:
        rates = {y: v["rate"] for y, v in agreement["per_year"].items() if v.get("rate") is not None}
        low = min(rates, key=rates.get)
        overall = agreement["overall"]
        checks.append(_check(
            "CSB vs raster", overall["rate"] >= AGREEMENT_MIN and rates[low] >= AGREEMENT_YEAR_MIN,
            f"the raster-majority crop equals CSB's own CDL attribute in {_pct(overall['rate'], 2)} of "
            f"{overall['compared']:,} field-years ({min(rates)}-{max(rates)}); lowest year {low}: {_pct(rates[low], 2)} "
            f"(expect >= {_pct(AGREEMENT_MIN, 0)} overall, >= {_pct(AGREEMENT_YEAR_MIN, 0)} per year)",
        ))  # fmt: skip
    if "fallbacks" in context:
        fallbacks = context["fallbacks"]
        checks.append(_check(
            "data sources", not fallbacks,
            "every step used its first-choice source" if not fallbacks else "FALLBACKS: " + " | ".join(fallbacks),
        ))  # fmt: skip
    return checks, numbers


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = ["sanity checks (PASS = as expected for southwest Kansas cropland, WARN = look at it):"]
    lines += [f"  {c.status}  {c.name:<{width}}  {c.detail}" for c in checks]
    n_warn = sum(not c.ok for c in checks)
    lines.append(f"sanity: {len(checks) - n_warn} PASS, {n_warn} WARN")
    return "\n".join(lines)


def format_samples(table: gpd.GeoDataFrame, n: int, seed: int = 20055) -> str:
    """`n` random fields, one line each: id, area, organic matter, slope, compact history."""
    n = min(n, len(table))
    if n <= 0:
        return ""
    picks = np.sort(np.random.default_rng(seed).choice(len(table), size=n, replace=False))
    years = sorted({int(y) for h in table["crop_history"].iloc[picks] for y in json.loads(h)})
    span = list(range(years[0], years[-1] + 1))
    lines = [
        f"{n} random fields, history {span[0]}-{span[-1]} (C corn, W winter wheat, S sorghum, F fallow, A alfalfa, "
        "B soybeans, T triticale, g grass/pasture, h other hay, d double crop, x developed, ? other, . no data):",
        f"  {'field_id':<10} {'area_ha':>8} {'om_pct':>7} {'slope_pct':>9}  history",
    ]
    for i in picks:
        row = table.iloc[int(i)]
        om = "null" if pd.isna(row["organic_matter_pct"]) else f"{row['organic_matter_pct']:.2f}"
        slope = "null" if pd.isna(row["slope_pct"]) else f"{row['slope_pct']:.2f}"
        lines.append(f"  {row['field_id']:<10} {row['area_ha']:>8.1f} {om:>7} {slope:>9}  {compact_history(row['crop_history'], span)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# step 10: provenance
# --------------------------------------------------------------------------------------------
def sources_and_fallbacks(fields_prov: dict, soil_prov: dict, slope_prov: dict, missing: dict) -> tuple[list[dict], list[str], dict]:
    """(sources actually used, fallbacks taken, what was downloaded during this run)."""
    sources: list[dict] = []
    fallbacks: list[str] = []
    downloads: dict = {}

    boundary = fields_prov.get("county_boundary") or {}
    if boundary.get("url"):
        sources.append({"what": "county outline (US Census Bureau)", "url": boundary["url"], "area_km2": boundary.get("area_km2")})
        if "tigerweb" in str(boundary["url"]).lower():
            fallbacks.append("county outline: the cartographic boundary file failed, TIGERweb was used")

    used = fields_prov.get("source")
    csb_prov = fields_prov.get("csb") or {}
    if used == "csb":
        sources.append({
            "what": "field outlines (USDA NASS Crop Sequence Boundaries)",
            "release": csb_prov.get("release"), "window": csb_prov.get("window"), "url": csb_prov.get("url"),
            "landing_page": csb_prov.get("landing_page"), "layer": csb_prov.get("layer"),
            "read_via": csb_prov.get("read_via"), "served_from_local_cache": csb_prov.get("from_cache"),
        })  # fmt: skip
        downloads["csb"] = "cached county extract" if csb_prov.get("from_cache") else f"downloaded / read {csb_prov.get('release')}"
        try:
            from pipeline.csb import CSB_WINDOWS

            if csb_prov.get("window") and csb_prov["window"] != CSB_WINDOWS[0]:
                fallbacks.append(f"field outlines: CSB release {csb_prov['window']} was used instead of the newest ({CSB_WINDOWS[0]})")
        except Exception:  # pragma: no cover
            pass
    elif used:
        sources.append({"what": "field outlines", "method": "segmented from the CDL rasters (pipeline/cdl_segment.py)"})
    if fields_prov.get("fallback_reason"):
        fallbacks.append(f"field outlines: CSB failed ({fields_prov['fallback_reason']}); outlines were segmented from the CDL")

    cdl_prov = fields_prov.get("cdl") or {}
    per_year = cdl_prov.get("per_year") or {}
    if per_year:
        sources.append({
            "what": "crop history (USDA NASS Cropland Data Layer via CropScape, one county raster per year)",
            "service": cdl_prov.get("service"),
            "years": {y: {"url": v.get("url"), "obtained_via": v.get("downloaded_via") or v.get("route"),
                          "downloaded_at": v.get("downloaded_at")} for y, v in per_year.items()},
        })  # fmt: skip
        downloaded = [y for y, v in per_year.items() if v.get("route") not in (None, "local-cache")]
        downloads["cdl_years_downloaded"] = downloaded
        via_api = [y for y, v in per_year.items() if (v.get("downloaded_via") or v.get("route")) == "GetCDLFile"]
        if via_api:
            fallbacks.append(f"CDL {', '.join(via_api)}: the CropScape cache URL failed, the GetCDLFile API was used")
    if cdl_prov.get("years_missing"):
        fallbacks.append(f"CDL: no raster for {cdl_prov['years_missing']}; those years are missing from every history")

    if "soil" in missing:
        fallbacks.append(f"organic_matter_pct: the soil step failed and was allowed to be missing ({missing['soil']})")
    elif soil_prov:
        tiles = soil_prov.get("tile_scheme") or {}
        sources.append({
            "what": "soil organic matter (USDA NRCS SSURGO via Soil Data Access)", "url": soil_prov.get("endpoint"),
            "survey_areas": soil_prov.get("survey_areas"), "tiles": tiles.get("n_tiles"),
            "tiles_from_cache": tiles.get("tiles_from_cache"), "tiles_fetched": tiles.get("tiles_fetched"),
        })  # fmt: skip
        downloads["ssurgo_http_requests"] = (soil_prov.get("http") or {}).get("http_requests")

    if "slope" in missing:
        fallbacks.append(f"slope_pct: the slope step failed and was allowed to be missing ({missing['slope']})")
    elif slope_prov:
        sources.append({
            "what": "elevation for slope", "source": slope_prov.get("source"), "label": slope_prov.get("source_label"),
            "urls": slope_prov.get("urls"), "resolution_arcsec": slope_prov.get("resolution_arcsec"),
        })  # fmt: skip
        downloads["dem_units_fetched"] = slope_prov.get("cache_units_fetched_this_run")
        if slope_prov.get("fallback_used"):
            failed = "; ".join(f"{a.get('source')}: {a.get('error')}" for a in slope_prov.get("attempts", []) if not a.get("ok"))
            fallbacks.append(
                f"slope: the first-choice DEM failed ({failed}); {slope_prov.get('source')} was used instead - "
                "slopes from a coarser DEM read lower and are not comparable with a 3dep13 run"
            )
    return sources, fallbacks, downloads


def format_downloads(downloads: dict) -> str:
    parts = []
    if "csb" in downloads:
        parts.append(f"CSB: {downloads['csb']}")
    if "cdl_years_downloaded" in downloads:
        years = downloads["cdl_years_downloaded"]
        parts.append(f"CDL: {len(years)} raster(s) downloaded" + (f" ({', '.join(years)})" if years else ""))
    if "ssurgo_http_requests" in downloads:
        parts.append(f"SSURGO: {downloads['ssurgo_http_requests']} HTTP request(s)")
    if "dem_units_fetched" in downloads:
        parts.append(f"DEM: {downloads['dem_units_fetched']} block(s) fetched")
    return "network this run - " + "; ".join(parts) if parts else "network this run - unknown"


# --------------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------------
def parse_allow_missing(text: str | None) -> frozenset[str]:
    names = {part.strip().lower() for part in (text or "").split(",") if part.strip()}
    unknown = names - set(ALLOWABLE_MISSING)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown step(s) {', '.join(sorted(unknown))}; --allow-missing takes a comma-separated list of: "
            + ", ".join(ALLOWABLE_MISSING)
        )
    return frozenset(names)


def run(*, source: str = "auto", min_area_ha: float = MIN_FIELD_HA, max_fields: int | None = None,
        out: Path = REAL_PATH, allow_missing=frozenset(), samples: int = 12, argv: list[str] | None = None) -> dict:  # fmt: skip
    """Build, check and write the fields file. Returns the provenance dict.

    Raises PipelineError when the file could not be written (or, with `.written`, when it was
    written but a later step failed).
    """
    started = datetime.now(timezone.utc)
    timer = StepTimer()
    out = Path(out)
    cap = "no cap" if max_fields is None else f"max {max_fields:,} fields"
    log(f"building {_rel(out)} for {COUNTY_NAME} County, {STATE_ABBR} (FIPS {FIPS}): fields source {source}, "
        f"min area {min_area_ha:g} ha, {cap}")  # fmt: skip

    # 1 --- fields + crop history (fatal on failure)
    with timer.step("fields", f"field outlines + crop history {YEARS[0]}-{YEARS[-1]}"):
        try:
            fields = build_fields(source=source, min_area_ha=min_area_ha, max_fields=max_fields)
        except Exception as exc:
            log(f"!!! fields: {traceback.format_exc().rstrip()}")
            raise PipelineError(
                "fields",
                f"field outlines / crop histories could not be built.\n  {type(exc).__name__}: {exc}\n"
                "  This step has no substitute (build_fields already tried its own fallback). Nothing was written.",
            ) from exc
        # .attrs does not survive most pandas operations: read it now
        fields_prov = dict(fields.attrs.get("provenance") or {})
        source_ids = list(fields.attrs.get("source_ids") or [None] * len(fields))
        lacking = [c for c in ("field_id", "geometry", "crop_history") if c not in fields.columns]
        if lacking or len(fields) == 0 or len(source_ids) != len(fields):
            raise PipelineError("fields", f"build_fields returned an unusable table (missing columns: {lacking}; rows: {len(fields)})")
        fields = fields.reset_index(drop=True)
        fields.attrs = {}
        build_area = fields["area_ha"].to_numpy(dtype=float) if "area_ha" in fields.columns else None

    # 2-3 --- the nullable columns
    missing: dict[str, str] = {}
    with timer.step("soil", "soil organic matter (SSURGO via Soil Data Access)"):
        om, soil_prov, reason = optional_column("soil", "organic_matter_pct", organic_matter_pct, fields, allow_missing)
        if reason:
            missing["soil"] = reason
    with timer.step("slope", "mean slope (USGS 3DEP elevation)"):
        slope, slope_prov, reason = optional_column("slope", "slope_pct", mean_slope_pct, fields, allow_missing)
        if reason:
            missing["slope"] = reason

    # 4-6 --- area, assembly, validation
    with timer.step("area", f"area_ha from the final outlines in {EQUAL_AREA_CRS}"):
        area = equal_area_ha(fields.geometry)
    with timer.step("assemble", "assemble the schema.md columns"):
        try:
            table, order = assemble(fields, om, slope, area)
        except Exception as exc:
            raise PipelineError("assemble", f"could not assemble the table: {type(exc).__name__}: {exc}") from exc
        source_ids = [source_ids[i] for i in order]  # assemble sorts by field_id
        if build_area is not None:
            build_area = build_area[order]
    with timer.step("validate", "validate against schema.md (before writing)"):
        try:
            validate_fields(table, "pipeline output, not written")
        except SchemaError as exc:
            raise PipelineError("validate", f"{exc}\n  Nothing was written.") from exc
        log(f"  OK: {len(table):,} fields match schema.md")

    # 7-8 --- write atomically, prove the round trip
    with timer.step("write", f"write {_rel(out)} (GeoParquet, atomic)"):
        try:
            write_fields_atomic(table, out)
        except Exception as exc:
            raise PipelineError("write", f"could not write {_rel(out)}: {type(exc).__name__}: {exc}\n  The previous file, if any, is untouched.") from exc
    with timer.step("reread", f"re-read {_rel(out)} with pipeline.schema.load_fields"):
        try:
            written = load_fields(out)
        except Exception as exc:
            raise PipelineError("reread", f"{_rel(out)} was written but cannot be loaded: {exc}", written=True) from exc
        differences = table_differences(table, written)
        if differences:
            raise PipelineError("reread", f"{_rel(out)} was written but differs from the table in memory: " + "; ".join(differences), written=True)
        size = out.stat().st_size
        log(f"  OK: round trip of {len(written):,} fields, {size / 1e6:.2f} MB")

    # 9 --- summary + sanity checks. The file is written and valid by now: nothing below may
    # turn the run into a failure, so a crash in the reporting is reported, not raised.
    with timer.step("summary", "summary and sanity checks"):
        summary = summarize(written)
        print()
        print(f"=== {_rel(out)} ===")
        print(summary)
        print()
        sources, fallbacks, downloads, county, numbers = [], [], {}, None, {}
        try:
            sources, fallbacks, downloads = sources_and_fallbacks(fields_prov, soil_prov, slope_prov, missing)
            county = county_outline()
            context = {"fields_provenance": fields_prov, "slope_provenance": slope_prov, "missing": missing,
                       "build_area_ha": build_area, "fallbacks": fallbacks}  # fmt: skip
            checks, numbers = sanity_checks(written, min_area_ha=min_area_ha, county=county, context=context)
            if samples > 0:
                print(format_samples(written, samples))
                print()
            print(format_year_table(year_groups(year_table(history_frame(written["crop_history"])))))
            print()
        except Exception as exc:
            log(f"!!! the sanity checks crashed; {_rel(out)} itself is complete and valid\n{traceback.format_exc().rstrip()}")
            checks = [Check("sanity checks", "WARN", f"COULD NOT BE COMPLETED ({type(exc).__name__}: {exc})")]
        print(format_checks(checks))
        print()
        sys.stdout.flush()

    # 10 --- side files (not part of the contract)
    provenance_path, ids_path = side_paths(out)
    provenance: dict = {}
    with timer.step("provenance", f"write {_rel(provenance_path)} and {_rel(ids_path)}"):
        try:
            provenance = _write_side_files(
                out=out, provenance_path=provenance_path, ids_path=ids_path, written=written, source_ids=source_ids,
                size=size, started=started, timer=timer, argv=argv, county=county,
                arguments={"fields_source": source, "min_area_ha": min_area_ha, "max_fields": max_fields,
                           "out": str(out), "allow_missing": sorted(allow_missing)},
                fields_prov=fields_prov, soil_prov=soil_prov, slope_prov=slope_prov, missing=missing, sources=sources,
                fallbacks=fallbacks, downloads=downloads, summary=summary, checks=checks, numbers=numbers,
            )  # fmt: skip
        except Exception as exc:  # the data file is fine; say so instead of failing the run
            log(f"!!! could not write the side files ({type(exc).__name__}: {exc}); {_rel(out)} itself is complete")

    n_warn = sum(not c.ok for c in checks)
    log(format_downloads(downloads))
    log("timings: " + ", ".join(f"{k} {v:.1f}s" for k, v in timer.seconds.items()) + f"; total {timer.total_seconds():.1f}s")
    if missing:
        log("!" * 100)
        for step, reason in missing.items():
            log(f"!!! WARNING: {step} is MISSING in {_rel(out)} (null for every field): {reason}")
        log("!" * 100)
    log(f"wrote {_rel(out)}: {len(written):,} fields, {size / 1e6:.2f} MB; sanity checks: {len(checks) - n_warn} PASS, {n_warn} WARN")
    return provenance


def _write_side_files(*, out, provenance_path, ids_path, written, source_ids, size, started, timer, argv, county, arguments,
                      fields_prov, soil_prov, slope_prov, missing, sources, fallbacks, downloads, summary, checks,
                      numbers) -> dict:  # fmt: skip
    """Step 10: `<stem>_source_ids.csv` and `<stem>_provenance.json` next to the output file."""
    finished = datetime.now(timezone.utc)
    seconds = dict(timer.seconds)
    provenance = {
        "what": f"How {out.name} was built. A side file for humans; not part of the data contract (schema.md).",
        "run": {
            "started_utc": started.isoformat(timespec="seconds"),
            "finished_utc": finished.isoformat(timespec="seconds"),
            "command": list(sys.argv if argv is None else argv),
            "arguments": arguments,
        },
        "region": {"state": STATE_ABBR, "county": COUNTY_NAME, "fips": FIPS},
        "output": {
            "path": _rel(out), "bytes": size, "sha256": sha256_of(out), "fields": len(written),
            "columns": list(written.columns), "crs": "EPSG:4326", "format": "GeoParquet (WKB geometry)",
            "source_ids_file": _rel(ids_path),
            "rounding_decimals": {"organic_matter_pct": OM_DECIMALS, "slope_pct": SLOPE_DECIMALS, "area_ha": AREA_DECIMALS},
        },  # fmt: skip
        "sources_used": sources,
        "fallbacks_taken": fallbacks,
        "columns_allowed_missing": missing,
        "downloads_this_run": downloads,
        "fields_source": fields_prov.get("source"),
        "csb_release": (fields_prov.get("csb") or {}).get("release"),
        "filters": fields_prov.get("filters"),
        "cropland_rule": fields_prov.get("cropland_rule"),
        "csb_vs_raster_agreement": fields_prov.get("agreement_with_csb_attributes"),
        "field_cap": {
            "max_fields": arguments.get("max_fields"),
            "fields_before_cap": (fields_prov.get("counts") or {}).get("fields_before_max_fields"),
            "rule": fields_prov.get("max_fields_rule"),
        },
        "area": {
            "crs": EQUAL_AREA_CRS,
            "method": "area of the final EPSG:4326 outline reprojected to the equal-area CRS",
            "county_km2": county["area_km2"] if county else None,
            "share_of_county": numbers.get("share_of_county"),
        },
        "summary": summary.splitlines(),
        "sanity_checks": [asdict(c) for c in checks],
        "sanity_numbers": numbers,
        "timings_s": {**seconds, "total": timer.total_seconds()},
        "steps": {"fields": fields_prov, "soil": soil_prov, "slope": slope_prov},
        "versions": package_versions(),
    }
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["field_id", "source", "source_id"])
    label = fields_prov.get("source") or ""
    if label == "csb" and (fields_prov.get("csb") or {}).get("release"):
        label = f"csb:{fields_prov['csb']['release']}"
    for field_id, source_id in zip(written["field_id"], source_ids, strict=True):
        writer.writerow([field_id, label, "" if source_id is None else source_id])
    write_text_atomic(ids_path, buffer.getvalue())
    provenance["timings_s"]["total"] = timer.total_seconds()
    write_text_atomic(provenance_path, json.dumps(jsonable(provenance), indent=2, allow_nan=False) + "\n")
    return provenance


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild the fields file (data/fields.parquet) from real USDA / USGS data.",
        epilog="Exit code 0 when the file was written (also with WARN sanity checks), 1 otherwise.",
    )
    ap.add_argument("--max-fields", type=int, default=None, metavar="N",
                    help="keep a spatially even subset of N fields (default: no cap, the whole county)")  # fmt: skip
    ap.add_argument("--min-area-ha", type=float, default=MIN_FIELD_HA, help="smallest field kept (default: %(default)s)")
    ap.add_argument("--fields-source", choices=FIELD_SOURCES, default="auto",
                    help="auto = USDA Crop Sequence Boundaries, falling back to segmenting the CDL (default)")  # fmt: skip
    ap.add_argument("--out", type=Path, default=REAL_PATH, help=f"output GeoParquet (default: {_rel(REAL_PATH)})")
    ap.add_argument("--allow-missing", type=parse_allow_missing, default=frozenset(), metavar="soil,slope",
                    help="write the file with a null column instead of failing when that step fails")  # fmt: skip
    ap.add_argument("--samples", type=int, default=12, help="random fields printed with the summary (default: %(default)s)")
    args = ap.parse_args(argv)
    if args.max_fields is not None and args.max_fields < 1:
        ap.error("--max-fields must be >= 1")
    if not args.min_area_ha > 0:
        ap.error("--min-area-ha must be > 0")

    try:
        run(source=args.fields_source, min_area_ha=args.min_area_ha, max_fields=args.max_fields, out=args.out,
            allow_missing=args.allow_missing, samples=args.samples, argv=None if argv is None else ["run_all", *argv])  # fmt: skip
    except PipelineError as exc:
        sys.stdout.flush()
        print(f"\nFAILED at step '{exc.step}': {exc}", file=sys.stderr)
        if exc.written:
            print(f"{_rel(args.out)} WAS replaced but must not be trusted; rerun the pipeline.", file=sys.stderr)
        else:
            state = "is unchanged" if Path(args.out).exists() else "does not exist"
            print(f"{_rel(args.out)} was NOT written ({state}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
