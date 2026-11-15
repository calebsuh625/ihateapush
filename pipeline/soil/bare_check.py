"""Independent recompute of rows of the bare-soil table from raw pixels (a check, not a pipeline step).

    python -m pipeline.soil.bare_check --table data/soil_bare_obs_proto.parquet --n 6

For a few (field, year) pairs per sensor family it downloads every raw pixel value of every scene
over the field (ImageCollection.getRegion: digital numbers and quality bands, nothing computed by
Earth Engine), then redoes everything in numpy with code that shares only the constants of
pipeline/soil/bare_soil.py: which pixels belong to the field, scaling, clear masks, same-day
mosaic, the bare rule, per-pixel medians, counts, bare_days. It prints the largest differences from
the table. Raw downloads are cached under data/raw/gee_soil/check/.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import satellite as sat  # noqa: E402
from pipeline.schema import REAL_PATH, load_fields  # noqa: E402
from pipeline.soil import bare_soil as bs  # noqa: E402

CHECK_CACHE = bs.CACHE_DIR / "check"
MAX_PX = 260  # only fields this small are checked, to keep the downloads small


# --------------------------------------------------------------------------- raw download


def _region_rows(ee, collection, bands, region, sensor, year) -> list:
    """getRegion of the whole year, two months at a time (one call is limited to ~1 M values)."""
    crs, transform = bs.SENSOR_GRID[sensor]
    rows = None
    for month in range(1, 13, 2):
        start = f"{year}-{month:02d}-01"
        end = f"{year + 1}-01-01" if month == 11 else f"{year}-{month + 2:02d}-01"
        part = (
            collection.filterDate(start, end).select(bands)
            .getRegion(geometry=region, crs=crs, crsTransform=transform).getInfo()
        )
        rows = part if rows is None else rows + part[1:]
    return rows


def download_raw(ee, sensor: str, year: int, bounds: list[float]) -> dict:
    """{"scenes": [...], "cs": [...]}: raw getRegion tables (header + rows) of every scene over `bounds`."""
    region = ee.Geometry.Rectangle(bounds, None, False)
    not_overcast = ee.Filter.lt(bs.SCENE_CLOUD_PROPERTY[sensor], bs.SCENE_MAX_CLOUD_PCT)
    out: dict = {}
    if sensor == "s2":
        scenes = ee.ImageCollection(bs.S2_COLLECTION).filterBounds(region).filter(not_overcast)
        out["scenes"] = _region_rows(ee, scenes, [*bs.S2_BANDS.values(), "SCL"], region, sensor, year)
        # Cloud Score+ is downloaded on its own and joined here by scene id (not by Earth Engine)
        cs = ee.ImageCollection(bs.S2_CS_COLLECTION).filterBounds(region)
        out["cs"] = _region_rows(ee, cs, [bs.S2_CS_BAND], region, sensor, year)
        return out
    rows = None
    for collection, first in bs.LANDSAT_COLLECTIONS:
        if year < first:
            continue
        scenes = ee.ImageCollection(collection).filterBounds(region).filter(not_overcast)
        part = _region_rows(ee, scenes, [*bs.LANDSAT_BANDS.values(), "QA_PIXEL", "QA_RADSAT"], region, sensor, year)
        rows = part if rows is None else rows + part[1:]
    out["scenes"] = rows
    return out


def cached_raw(ee_factory, sensor: str, year: int, field_id: str, bounds: list[float]) -> dict:
    key = hashlib.sha256(sat._canonical([bs.request_signature(sensor), year, field_id, bounds])).hexdigest()[:24]
    path = CHECK_CACHE / f"{sensor}_{year}_{field_id}_{key}.json.gz"
    if path.exists():
        with gzip.open(path, "rt") as fh:
            return json.load(fh)
    raw = download_raw(ee_factory(), sensor, year, bounds)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with gzip.open(tmp, "wt") as fh:
        json.dump(raw, fh)
    os.replace(tmp, path)
    return raw


# --------------------------------------------------------------------------- numpy recompute


def _table(rows) -> pd.DataFrame:
    return pd.DataFrame(rows[1:], columns=rows[0])


def recompute(sensor: str, year: int, outline, raw: dict) -> dict:
    """The table's columns for one field-year, from raw pixels. `outline` = shapely geometry (lon/lat)
    exactly as sent to Earth Engine."""
    import pyproj
    import shapely

    crs, transform = bs.SENSOR_GRID[sensor]
    names = bs.SENSOR_BANDS[sensor]
    raw_bands = bs.S2_BANDS if sensor == "s2" else bs.LANDSAT_BANDS
    df = _table(raw["scenes"])
    # getRegion gives pixel centres in lon/lat; put them back on the grid as (column, row)
    to_grid = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = to_grid.transform(df["longitude"].to_numpy(), df["latitude"].to_numpy())
    df["col"] = np.floor((x - transform[2]) / transform[0]).astype(int)
    df["row"] = np.floor((y - transform[5]) / transform[4]).astype(int)
    if sensor == "s2":
        cs = _table(raw["cs"])
        cx, cy = to_grid.transform(cs["longitude"].to_numpy(), cs["latitude"].to_numpy())
        cs["col"] = np.floor((cx - transform[2]) / transform[0]).astype(int)
        cs["row"] = np.floor((cy - transform[5]) / transform[4]).astype(int)
        df = df.merge(cs[["id", "col", "row", bs.S2_CS_BAND]], on=["id", "col", "row"], how="left")

    # which grid cells belong to the field: the centre is inside the outline
    cells = df[["col", "row"]].drop_duplicates().reset_index(drop=True)
    centre_x = transform[2] + (cells["col"] + 0.5) * transform[0]
    centre_y = transform[5] + (cells["row"] + 0.5) * transform[4]
    outline_grid = shapely.transform(outline, lambda xy: np.column_stack(to_grid.transform(xy[:, 0], xy[:, 1])))
    inside = shapely.contains_xy(outline_grid, centre_x.to_numpy(), centre_y.to_numpy())
    cells = cells[inside]
    n_px = len(cells)
    if n_px == 0:
        return {"n_px": 0}
    df = df.merge(cells, on=["col", "row"])

    # clear flags per scene pixel, on raw values
    if sensor == "s2":
        cloud_ok = df[bs.S2_CS_BAND].astype(float) >= bs.S2_CS_MIN  # NaN (no score) -> False
        clear = cloud_ok & ~df["SCL"].isin(bs.S2_SCL_REJECT) & df["SCL"].notna()
        scale, offset = bs.S2_SCALE, bs.S2_OFFSET
    else:
        qa = df["QA_PIXEL"].fillna(1).astype(int).to_numpy()  # missing = fill
        radsat = df["QA_RADSAT"].fillna(0).astype(int).to_numpy()
        clear = pd.Series((qa & bs.LANDSAT_QA_PIXEL_REJECT == 0) & (radsat & bs.LANDSAT_QA_RADSAT_REJECT == 0), index=df.index)
        scale, offset = bs.LANDSAT_SCALE, bs.LANDSAT_OFFSET
    for name in names:
        clear &= df[raw_bands[name]].notna()
    df = df[clear].copy()
    df["day"] = (df["time"] // 86_400_000).astype(int)
    # same-day mosaic: scenes in id order, the last clear one wins
    df = df.sort_values("id").groupby(["col", "row", "day"], as_index=False).last()
    refl = {name: df[raw_bands[name]].to_numpy(dtype=float) * scale + offset for name in names}
    valid = np.ones(len(df), dtype=bool)
    for name in names:
        valid &= (refl[name] > bs.VALID_MIN) & (refl[name] <= bs.VALID_MAX)
    # Earth Engine holds the scaled values as 32-bit floats before it forms the indices
    f32 = {name: refl[name].astype(np.float32) for name in names}
    ndvi = (f32["nir"] - f32["red"]) / (f32["nir"] + f32["red"])
    nbr2 = (f32["swir1"] - f32["swir2"]) / (f32["swir1"] + f32["swir2"])
    if bs.NDSI_MAX is not None:  # snow and bright cloud are not clear views
        valid &= (f32["green"] - f32["swir1"]) / (f32["green"] + f32["swir1"]) < bs.NDSI_MAX
    bare = valid & (ndvi < bs.NDVI_MAX[sensor]) & (nbr2 < bs.NBR2_MAX[sensor])
    month = pd.to_datetime(df["day"] * 86_400, unit="s").dt.month.to_numpy()

    clear_m = np.array([(valid & (month == m)).sum() for m in range(1, 13)], dtype=float)
    bare_m = np.array([(bare & (month == m)).sum() for m in range(1, 13)], dtype=float)
    out = {
        "n_px": n_px,
        "n_clear": clear_m.sum() / n_px,
        "n_bare": bare_m.sum() / n_px,
    }
    pixel = df.loc[bare, ["col", "row"]].copy()
    for name in names:
        pixel[name] = f32[name][bare]
    medians = pixel.groupby(["col", "row"])[names].median()  # even counts: mean of the two middle values
    out["bare_px_frac"] = len(medians) / n_px
    for name in names:
        out[name] = float(medians[name].mean()) if len(medians) else None

    # bare_days, written out step by step (no np.interp)
    days_in = [calendar.monthrange(year, m)[1] for m in range(1, 13)]
    share = [bare_m[i] / clear_m[i] if clear_m[i] / n_px >= bs.MIN_CLEAR_PER_MONTH else None for i in range(12)]
    known = [i for i in range(12) if share[i] is not None]
    out["clear_months"] = len(known)
    if len(known) < bs.MIN_MONTHS:
        out["bare_days"] = None
    else:
        total = 0.0
        for i in range(12):
            if share[i] is not None:
                value = share[i]
            else:
                before = [k for k in known if k < i]
                after = [k for k in known if k > i]
                if not before:
                    value = share[after[0]]
                elif not after:
                    value = share[before[-1]]
                else:
                    lo, hi = before[-1], after[0]
                    value = share[lo] + (share[hi] - share[lo]) * (i - lo) / (hi - lo)
            total += value * days_in[i]
        out["bare_days"] = total
    return out


# --------------------------------------------------------------------------- CLI


def pick(table: pd.DataFrame, sensor: str, n: int, seed: int) -> list[tuple[str, int]]:
    """n (field, year) pairs of one sensor: small fields, most with bare views, spread over the years."""
    part = table[(table["sensor"] == sensor) & (table["n_px"] > 20) & (table["n_px"] <= MAX_PX)]
    rng = np.random.default_rng(seed)
    with_bare = part[part["n_bare"] > 0]
    without = part[part["n_bare"] == 0]
    chosen: list[tuple[str, int]] = []
    years = rng.permutation(sorted(with_bare["year"].unique()))
    for i in range(max(0, n - 1)):
        rows = with_bare[with_bare["year"] == years[i % len(years)]]
        row = rows.iloc[int(rng.integers(len(rows)))]
        chosen.append((row["field_id"], int(row["year"])))
    pool = without if len(without) else with_bare
    row = pool.iloc[int(rng.integers(len(pool)))]
    chosen.append((row["field_id"], int(row["year"])))
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--table", type=Path, default=bs.BARE_OBS_PATH)
    parser.add_argument("--fields", type=Path, default=REAL_PATH)
    parser.add_argument("--n", type=sat.positive_int, default=6, help="field-years per sensor family")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--sensors", type=bs.parse_sensors, default=list(bs.SENSORS))
    args = parser.parse_args(argv)
    try:
        table = pd.read_parquet(args.table)
        fields = load_fields(args.fields)
    except FileNotFoundError as exc:
        print(f"ERROR: file not found: {exc}", file=sys.stderr)
        return 2
    # check the table against the method it says it was computed with
    method = bs.read_table_method(args.table)
    if method is not None and not method.get("snow_screen", True):
        bs.NDSI_MAX = None
        print("note: this table was computed without the snow / bright-cloud screen; checking it as such")
    fields = fields[fields["field_id"].isin(table["field_id"].unique())].reset_index(drop=True)
    shrunk = bs.buffer_inward(fields)
    outlines = dict(zip(fields["field_id"], shrunk))
    log = bs.RunLog(bs.LOG_DIR / f"soil_bare_check_{time.strftime('%Y%m%d_%H%M%S')}.log")

    engine: list = []

    def ee_factory():
        if not engine:
            engine.append(sat.EarthEngine().ee)
        return engine[0]

    from shapely.geometry import shape

    numeric = ["n_px", "n_clear", "n_bare", "bare_px_frac", "bare_days", "clear_months"]
    try:
        for sensor in args.sensors:
            worst: dict[str, float] = {}
            mismatched_nulls = 0
            pairs = pick(table, sensor, args.n, args.seed)
            for field_id, year in pairs:
                geojson = sat.geometry_to_geojson(outlines[field_id])  # rounded exactly as the pipeline sends it
                west, south, east, north = bs.geojson_bounds(geojson)
                pad = 0.0006  # ~55 m: more than one grid cell around the outline
                raw = cached_raw(ee_factory, sensor, year, field_id, [west - pad, south - pad, east + pad, north + pad])
                mine = recompute(sensor, year, shape(geojson), raw)
                row = table[(table["field_id"] == field_id) & (table["year"] == year) & (table["sensor"] == sensor)].iloc[0]
                log(f"{sensor} {field_id} {year}: table {row[numeric].to_dict()} recomputed { {k: mine.get(k) for k in numeric} }")
                for col in numeric + bs.SENSOR_BANDS[sensor]:
                    theirs, ours = row[col], mine.get(col)
                    theirs_null = theirs is None or pd.isna(theirs)
                    ours_null = ours is None or pd.isna(ours)
                    if theirs_null or ours_null:
                        mismatched_nulls += int(theirs_null != ours_null)
                        continue
                    group = col if col in numeric else "reflectance"
                    worst[group] = max(worst.get(group, 0.0), abs(float(theirs) - float(ours)))
                    log(f"    {col}: table {float(theirs):.5f} recomputed {float(ours):.5f}")
            print(
                f"{sensor}: {len(pairs)} field-years recomputed from raw pixels; largest absolute differences: "
                + ", ".join(f"{k} {v:.4g}" for k, v in worst.items())
                + f"; null/non-null mismatches: {mismatched_nulls}"
            )
    except sat.EarthEngineUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"details: {log.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
