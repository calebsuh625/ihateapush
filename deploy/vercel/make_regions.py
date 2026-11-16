"""Write deploy/vercel/regions.json: the counties the hosted site offers (Finney first).

Run before `vercel deploy`: .venv/bin/python deploy/vercel/make_regions.py [--require-traits]
A county is listed when data/regions/<fips>/fields.parquet exists; with --require-traits (used while
counties are still being built) only once its field_traits.parquet exists too.
"""
from __future__ import annotations

import json
import time
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pandas as pd  # noqa: E402

from pipeline.region import BASE_DATA_DIR, DEFAULT_FIPS, data_dir_for, get_region  # noqa: E402


CLIMATE_BORROW_KM = 120  # about four cells of the 0.25-degree climate-model grid: still southwest Kansas


def _km_from_finney(fips: str) -> int | None:
    """Distance between county centres in km (from the cached Census outlines), or None if unknown."""
    import math

    import geopandas as gpd

    from pipeline.region import RAW_DIR

    def centre(f: str):
        path = RAW_DIR / "county" / f"county_{f}.geojson"
        if not path.is_file():
            return None
        c = gpd.read_file(path).to_crs(4326).geometry.union_all().centroid
        return c.y, c.x

    a, b = centre(DEFAULT_FIPS), centre(fips)
    if a is None or b is None:
        return None
    dy = (b[0] - a[0]) * 111.2
    dx = (b[1] - a[1]) * 111.2 * math.cos(math.radians((a[0] + b[0]) / 2))
    return round(math.hypot(dx, dy))


def main() -> None:
    require_traits = "--require-traits" in sys.argv[1:]
    out = []
    dirs = sorted(p.name for p in (BASE_DATA_DIR / "regions").glob("[0-9]" * 5) if p.is_dir())
    for fips in [DEFAULT_FIPS] + dirs:
        fields = data_dir_for(fips) / "fields.parquet"
        if not fields.is_file():
            continue
        traits = data_dir_for(fips) / "field_traits.parquet"
        if require_traits and fips != DEFAULT_FIPS and (not traits.is_file() or time.time() - traits.stat().st_mtime < 15):
            continue
        n = len(pd.read_parquet(fields, columns=["field_id"]))
        r = get_region(fips)
        km = _km_from_finney(fips)
        out.append({**r.as_dict(), "field_count": n,
                    "has_traits": (data_dir_for(fips) / "field_traits.parquet").is_file(),
                    "km_from_finney": km,
                    # Finney's climate futures and county yields are lent only to counties near it
                    "borrow_climate": fips == DEFAULT_FIPS or (km is not None and km <= CLIMATE_BORROW_KM)})
    (HERE / "regions.json").write_text(json.dumps({"default": DEFAULT_FIPS, "regions": out}, indent=1) + "\n")
    print(f"{len(out)} counties: " + ", ".join(f"{r['county']} ({r['field_count']:,})" for r in out))
    write_shapes(out)
    write_preview(out)


def write_shapes(regions: list[dict]) -> None:
    """deploy/vercel/regions.geojson: a simplified outline of each listed county, for the map's county layer."""
    import geopandas as gpd

    from pipeline.region import RAW_DIR

    rows = []
    for r in regions:
        cached = RAW_DIR / "county" / f"county_{r['fips']}.geojson"
        if not cached.is_file():
            continue
        g = gpd.read_file(cached).to_crs(4326).geometry.union_all()
        rows.append({"fips": r["fips"], "label": r["label"], "field_count": r["field_count"],
                     "geometry": g.simplify(0.003, preserve_topology=True)})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)
    text = gdf.to_json(drop_id=True)
    shapes = json.loads(text)
    for f in shapes["features"]:  # 4 decimals is about 10 m: plenty for a county line
        f["geometry"] = json.loads(json.dumps(f["geometry"]), parse_float=lambda x: round(float(x), 4))
    (HERE / "regions.geojson").write_text(json.dumps(shapes, separators=(",", ":")))
    print(f"outlines for {len(rows)} counties")


PREVIEW_EVERY = 20  # one sketch shape per this many fields


def write_preview(regions: list[dict]) -> None:
    """deploy/vercel/regions_preview.json: a light sketch of each county's fields for the map before it is
    opened. About one field in PREVIEW_EVERY, drawn at its real centre and size, as a circle when the field
    is round (centre pivots) and a square otherwise. Rows are [lon, lat, side in metres, 1 if round]."""
    import math

    import geopandas as gpd

    counties = {}
    for r in regions:
        gdf = gpd.read_parquet(data_dir_for(r["fips"]) / "fields.parquet")[["geometry"]]
        n = min(len(gdf), max(20, len(gdf) // PREVIEW_EVERY))
        g = gdf.sample(n=n, random_state=int(r["fips"])).geometry.to_crs(5070)  # equal-area metres
        # field outlines follow 30 m pixels, so judge roundness on the convex hull: a pivot's hull is
        # nearly a circle (roundness > 0.88) and the field fills most of it
        hull = g.convex_hull
        area, h_area, h_perim = g.area.to_numpy(), hull.area.to_numpy(), hull.length.to_numpy()
        centres = gpd.GeoSeries(g.centroid, crs=5070).to_crs(4326)
        rows = []
        for c, a, ha, hp in zip(centres, area, h_area, h_perim):
            round_field = hp > 0 and ha > 0 and 4 * math.pi * ha / (hp * hp) > 0.88 and a / ha > 0.8
            rows.append([round(c.x, 4), round(c.y, 4), round(math.sqrt(a)), 1 if round_field else 0])
        counties[r["fips"]] = rows
    (HERE / "regions_preview.json").write_text(
        json.dumps({"every": PREVIEW_EVERY, "counties": counties}, separators=(",", ":")))
    total = sum(len(v) for v in counties.values())
    print(f"preview: {total:,} sketch shapes for {len(counties)} counties")


if __name__ == "__main__":
    main()
