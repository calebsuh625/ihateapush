"""Shared set-up for the Vercel entry (index.py) and each county's API process (region_app.py).

- puts the repo root and the vendored geopandas on sys.path;
- points every cache at /tmp (the function's only writable folder);
- reads GeoParquet with fastparquet: geopandas is vendored without pyogrio/GDAL and pyarrow is left
  out, because together they push the function past Vercel's bundle size, which makes it install
  packages at start-up and run out of disk.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TMP = Path(os.environ.get("DEPLOY_TMP", "/tmp"))

_done = False


def setup() -> None:
    global _done
    if _done:
        return
    for p in (str(HERE / "_vendor"), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("PLANNER_SOIL_CACHE_PATH", str(TMP / "planner_bare_days.json"))
    os.environ.setdefault("LAYERS_CACHE_DIR", str(TMP / "layers_cache"))

    import geopandas
    import shapely

    def read_geoparquet(path, *args, **kwargs):
        import fastparquet
        from pyproj import CRS

        pf = fastparquet.ParquetFile(str(path))
        df = pf.to_pandas()
        meta = json.loads((pf.key_value_metadata or {}).get("geo") or "{}")
        col = meta.get("primary_column", "geometry")
        info = meta.get("columns", {}).get(col, {})
        if "crs" not in info or info["crs"] in ("OGC:CRS84", {"id": {"authority": "OGC", "code": "CRS84"}}):
            crs = "EPSG:4326"  # GeoParquet default, as geopandas.read_parquet does
        elif info["crs"] is None:
            crs = None
        else:
            crs = CRS.from_user_input(info["crs"])
        df[col] = geopandas.GeoSeries(shapely.from_wkb(df[col].values), index=df.index, crs=crs)
        return geopandas.GeoDataFrame(df, geometry=col)

    try:
        import pyarrow  # noqa: F401  (local runs with the full requirements keep geopandas' own reader)
    except ImportError:
        geopandas.read_parquet = read_geoparquet
    _done = True


def has_ortools() -> bool:
    try:
        import ortools  # noqa: F401
        return True
    except ImportError:
        return False
