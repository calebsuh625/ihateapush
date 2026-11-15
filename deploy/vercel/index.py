"""Vercel entry point: serves the FastAPI app (api/main.py) under /api.

Vercel sends /api/<path> here (see vercel.json); the app's routes have no /api prefix (in local dev
Vite strips it), so this strips it too. Startup work (loading data/fields.parquet etc.) runs on the
first request if the platform did not send the ASGI lifespan startup event.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# The function's file system is read-only except /tmp; the planner caches its bare-day table there.
os.environ.setdefault("PLANNER_SOIL_CACHE_PATH", "/tmp/planner_bare_days.json")

# geopandas is vendored without its file-format extras (pyogrio/GDAL), and pyarrow is left out: together
# they push the function past Vercel's bundle size, which makes it install packages at start-up and run
# out of disk. Parquet is read with fastparquet instead; geometry is GeoParquet WKB.
sys.path.insert(0, str(Path(__file__).resolve().parent / "_vendor"))

import json  # noqa: E402

import geopandas  # noqa: E402
import shapely  # noqa: E402


def _read_geoparquet(path, *args, **kwargs):
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


geopandas.read_parquet = _read_geoparquet

from api.main import app as inner  # noqa: E402

_lock = asyncio.Lock()
_lifespan = None  # kept open for the life of the function instance


async def _ensure_started() -> None:
    global _lifespan
    if hasattr(inner.state, "store"):
        return
    async with _lock:
        if not hasattr(inner.state, "store"):
            _lifespan = inner.router.lifespan_context(inner)
            await _lifespan.__aenter__()


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await inner(scope, receive, send)
        return
    await _ensure_started()
    path = scope.get("path", "")
    if path == "/api" or path.startswith("/api/"):
        scope = dict(scope, path=path[4:] or "/", raw_path=(path[4:] or "/").encode())
    await inner(scope, receive, send)
