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
        out.append({**r.as_dict(), "field_count": n,
                    "has_traits": (data_dir_for(fips) / "field_traits.parquet").is_file()})
    (HERE / "regions.json").write_text(json.dumps({"default": DEFAULT_FIPS, "regions": out}, indent=1) + "\n")
    print(f"{len(out)} counties: " + ", ".join(f"{r['county']} ({r['field_count']:,})" for r in out))
    write_shapes(out)


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


if __name__ == "__main__":
    main()
