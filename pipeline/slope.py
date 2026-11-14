"""Step 5.3 — mean terrain slope per field (`slope_pct` in schema.md: percent rise, 100 x rise/run).

    from pipeline.slope import mean_slope_pct
    slope = mean_slope_pct(fields)        # pandas Series, same index as `fields`, NaN = no elevation data
    slope.attrs["provenance"]             # DEM source, resolution, tiles/urls, method, counts

    python pipeline/slope.py [fields.parquet]              # default: data/fields_fake.parquet
    python pipeline/slope.py --compare 3dep1,srtm          # same fields on other DEMs, side by side

How it works

1. Elevation (`pipeline/dem.py`): USGS 3DEP 1/3 arc-second (~10 m); if that cannot be fetched,
   3DEP 1 arc-second (~30 m); if USGS cannot be reached at all, the SRTM-layout "skadi" tiles
   on AWS. One source is used for all fields of a run and recorded in the provenance. Only the
   parts of the tiles that cover fields (+ 3 pixels) are fetched; they are cached under
   data/raw/dem/ on a fixed grid, so a rerun — also over a different set of fields — makes no
   network request for anything already there.
2. Slope: Horn's 3x3 method on the DEM's own lon/lat grid, with the metric size of a pixel
   computed per row on the GRS80 ellipsoid (a 1/3" pixel at 38N is 8.1 m wide but 10.3 m tall).
   The DEM is never resampled or reprojected. A pixel whose 3x3 neighbourhood contains a missing
   elevation gets no slope — a nodata value can never become a cliff.
3. Zonal mean: the fields are burned into a label raster (a pixel belongs to a field when its
   centre is inside), then sum and count per label come from np.bincount — no loop over
   polygons. Slope and labels are produced in bands of rows, so memory stays near the size of
   the elevation mosaic. Fields too small to catch a pixel centre use every pixel they touch;
   failing that, the pixel under an interior point. Overlapping polygons are split into
   non-overlapping groups first, so each field is averaged over all of its own pixels.

Which resolution, and why (measured on Finney County, November 2026)

Slope depends on the scale it is measured at, so this is a choice, not a detail. On 7,347 real
field outlines of 5 ha or more (USDA Crop Sequence Boundaries):

                          3DEP 1/3" (10 m)   3DEP 1" (30 m)
    median field slope         0.84 %            0.76 %
    mean                       1.24 %            1.03 %
    fields above 3 %            8.6 %             3.8 %
    rank correlation between the two: 0.987

* On the flattest fields (below 0.5 % at 30 m) the 10 m DEM adds only 0.06 percentage points:
  lidar noise, pivot wheel tracks and terraces are visible in the 10 m slope map but do not
  move a field mean much. The worry that 10 m would mostly measure noise here did not hold.
* Where the land really rolls — the sandhills south of the Arkansas River, the breaks in the
  north-east — 10 m reads 25-30 % higher, because 30 m pixels average dunes and draw sides
  away. The soil survey agrees with the finer reading: at 149 random points, SSURGO's
  representative slope correlates 0.79 with the 10 m field means and 0.67 with the 30 m ones.
* At 10 m, road ditches and field-edge berms are lines one or two pixels wide that stay at the
  field edge; at 30 m the 3x3 window (73 x 92 m) spreads them well into the field.
* Small fields: at 30 m a 1 ha field holds about a dozen pixels, at 10 m about a hundred.
* Cost is not an argument either way: for all 15,926 CSB polygons of the county, 10 m means
  105 MB of cache, ~7 s to fetch it and ~1.6 s per run afterwards, with ~0.6 GB peak memory
  (30 m: 13 MB, ~3 s, 0.6 s, ~0.45 GB).

So 10 m is the default. The price is that the fallbacks are coarser: a run that falls back
to a 30 m source reports slopes about 20 % lower on rolling land (see `fallback_used` in the
provenance), and the SRTM-layout tiles store whole metres, which turns flat land into
terraces of zero slope separated by one-metre steps — fine as a county average, noisy for a
single flat field.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/slope.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import shapely  # noqa: E402
from affine import Affine  # noqa: E402
from rasterio.features import rasterize  # noqa: E402

from pipeline import dem  # noqa: E402
from pipeline.common import log  # noqa: E402
from pipeline.dem import DemFetchError, DemUnavailable  # noqa: E402,F401  (re-exported)
from pipeline.schema import FAKE_PATH  # noqa: E402

#: Sources tried in order (keys of `pipeline.dem.SOURCES`): 3DEP 1/3", then 3DEP 1", then SRTM.
DEFAULT_SOURCES: tuple[str, ...] = dem.DEFAULT_SOURCES

#: Pixels of DEM fetched around every field: 1 for the 3x3 slope kernel, 1 for pixels a tiny
#: field merely touches, 1 spare.
BUFFER_PX = 3

# GRS80 (the ellipsoid of NAD83; WGS84 differs by 0.1 mm in the semi-minor axis)
_A = 6378137.0
_F = 1.0 / 298.257222101
_E2 = _F * (2.0 - _F)

METHOD = (
    "Horn 3x3 finite differences on the DEM's native lon/lat grid; per-row metric pixel size on the "
    "GRS80 ellipsoid (dx = N(lat)*cos(lat)*dlon, dy = M(lat)*dlat); slope_pct = 100*sqrt((dz/dx)^2 + "
    "(dz/dy)^2); no resampling or reprojection; pixels with a missing elevation in their 3x3 window "
    "are excluded. Field value = unweighted mean of the slope pixels whose centre is inside the "
    "polygon (label raster + np.bincount, in bands of rows); fields that contain no pixel centre use "
    "all touched pixels, then the pixel under an interior point."
)


# ----------------------------------------------------------------------------------------------
# slope
# ----------------------------------------------------------------------------------------------

def pixel_size_m(lat_deg, res_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Ground size (east-west, north-south) in metres of a `res_deg` x `res_deg` lon/lat pixel
    centred at latitude `lat_deg`, on the GRS80 ellipsoid."""
    phi = np.radians(np.asarray(lat_deg, dtype=np.float64))
    w = 1.0 - _E2 * np.sin(phi) ** 2
    prime_vertical = _A / np.sqrt(w)  # N: radius of curvature east-west
    meridional = _A * (1.0 - _E2) / w ** 1.5  # M: radius of curvature north-south
    step = np.radians(res_deg)
    return step * prime_vertical * np.cos(phi), step * meridional


def horn_slope_pct(z, dx, dy, *, nodata: float | None = None, chunk_rows: int = 256) -> np.ndarray:
    """Slope in percent rise (100 * rise / run) from a north-up elevation grid, Horn (1981) 3x3.

    z       2-D elevations, same unit as dx/dy (metres). NaN = no data; `nodata` additionally
            marks a sentinel value (e.g. -999999) as missing.
    dx, dy  pixel size east-west / north-south in metres: scalars for a projected grid, or one
            value per row for a lon/lat grid (see `pixel_size_m`). NEVER pass degrees.

    For the 3x3 window  a b c / d e f / g h i  around a pixel:
        dz/dx = ((c + 2f + i) - (a + 2d + g)) / (8 dx)
        dz/dy = ((g + 2h + i) - (a + 2b + c)) / (8 dy)
        slope = 100 * sqrt(dz/dx^2 + dz/dy^2)
    Returns float32, NaN on the one-pixel border and wherever any of the nine cells is missing.
    Works through the rows in chunks, so the temporaries stay small on big grids.
    """
    z = np.asarray(z)
    if z.ndim != 2:
        raise ValueError("z must be 2-D")
    if z.dtype != np.float32 or nodata is not None:
        z = z.astype(np.float32)  # a copy
        if nodata is not None:
            z[z == np.float32(nodata)] = np.nan
    h, w = z.shape
    out = np.full((h, w), np.nan, dtype=np.float32)
    if h < 3 or w < 3:
        return out
    dx = np.broadcast_to(np.asarray(dx, dtype=np.float64), (h,))
    dy = np.broadcast_to(np.asarray(dy, dtype=np.float64), (h,))
    if not (np.all(dx > 0) and np.all(dy > 0)):
        raise ValueError("pixel sizes must be positive")
    for a in range(1, h - 1, chunk_rows):
        b = min(a + chunk_rows, h - 1)  # output rows a .. b-1
        zz = z[a - 1:b + 1]
        # differences first (east - west, south - north), then the 1-2-1 smoothing across them:
        # the subtraction happens between neighbouring heights, so float32 loses nothing.
        ew = zz[:, 2:] - zz[:, :-2]
        gx = ew[:-2] + 2.0 * ew[1:-1] + ew[2:]
        del ew
        ns = zz[2:] - zz[:-2]
        gy = ns[:, :-2] + 2.0 * ns[:, 1:-1] + ns[:, 2:]
        del ns
        gx /= (8.0 * dx[a:b])[:, None]
        gy /= (8.0 * dy[a:b])[:, None]
        s = np.hypot(gx, gy)
        s *= 100.0
        s[np.isnan(zz[1:-1, 1:-1])] = np.nan  # Horn does not use the centre cell; we still require it
        out[a:b, 1:-1] = s
    return out


# ----------------------------------------------------------------------------------------------
# zonal mean
# ----------------------------------------------------------------------------------------------

@dataclass
class ZonalResult:
    mean: np.ndarray  # float64, NaN where no valid pixel
    n_pixels: np.ndarray  # valid pixels averaged
    method: np.ndarray  # "pixel_centre" | "all_touched" | "point" | "no_data" | "no_geometry"


def _disjoint_layers(geoms: np.ndarray) -> list[np.ndarray]:
    """Split geometries into groups with pairwise disjoint interiors (greedy graph colouring).

    One label raster can hold only one field per pixel. Real field maps do not overlap (shared
    edges do not count), so this normally returns a single group; overlapping inputs get one
    group per "layer" so that no field loses pixels to another.
    """
    n = len(geoms)
    if n <= 1:
        return [np.arange(n)]
    tree = shapely.STRtree(geoms)
    a, b = tree.query(geoms, predicate="intersects")
    keep = a < b
    a, b = a[keep], b[keep]
    if len(a):
        try:
            interiors_meet = shapely.relate_pattern(geoms[a], geoms[b], "T********")
        except shapely.errors.GEOSException:  # invalid geometry: assume the worst
            interiors_meet = np.ones(len(a), dtype=bool)
        a, b = a[interiors_meet], b[interiors_meet]
    if not len(a):
        return [np.arange(n)]
    order = np.argsort(b, kind="stable")
    a, b = a[order], b[order]
    nodes = np.unique(b)
    starts, ends = np.searchsorted(b, nodes, side="left"), np.searchsorted(b, nodes, side="right")
    colour = np.zeros(n, dtype=np.int64)
    for node, s, e in zip(nodes.tolist(), starts.tolist(), ends.tolist()):  # only nodes with a conflict
        used = set(colour[a[s:e]].tolist())  # neighbours with a smaller index are already coloured
        c = 0
        while c in used:
            c += 1
        colour[node] = c
    return [np.flatnonzero(colour == c) for c in range(int(colour.max()) + 1)]


def _to_pixel(transform: Affine, x, y):
    """(column, row) as floats for coordinates on a north-up grid (pixel (0, 0) spans [0, 1) x [0, 1))."""
    return (np.asarray(x) - transform.c) / transform.a, (np.asarray(y) - transform.f) / transform.e


class _ArrayBands:
    """A 2-D array seen as a raster that is read in bands of rows."""

    def __init__(self, values: np.ndarray):
        self.values = np.asarray(values)
        if self.values.ndim != 2:
            raise ValueError("values must be 2-D")
        self.shape = self.values.shape

    def rows(self, a: int, b: int) -> np.ndarray:
        return self.values[a:b]


class SlopeBands:
    """The slope raster of an elevation grid, computed band by band and never held in full.

    `rows(a, b)` is exactly `horn_slope_pct(z, dx, dy)[a:b]`, but only rows a-1 .. b are touched.
    """

    def __init__(self, z: np.ndarray, dx, dy):
        self.z = z
        self.shape = z.shape
        self.dx = np.broadcast_to(np.asarray(dx, dtype=np.float64), (z.shape[0],))
        self.dy = np.broadcast_to(np.asarray(dy, dtype=np.float64), (z.shape[0],))

    def rows(self, a: int, b: int) -> np.ndarray:
        lo, hi = max(a - 1, 0), min(b + 1, self.shape[0])
        return horn_slope_pct(self.z[lo:hi], self.dx[lo:hi], self.dy[lo:hi])[a - lo:b - lo]


def _burn(raster, transform: Affine, geoms: np.ndarray, ids: np.ndarray, layers: list[np.ndarray], all_touched: bool,
          sums: np.ndarray, counts: np.ndarray, seen: np.ndarray, band_px: int) -> None:
    """Add per-geometry sum / valid-pixel count / pixel count of geoms[ids] into the accumulators.

    The raster is walked in bands of rows. In each band the geometries that reach it are burned
    into a label image (one per layer of mutually non-overlapping geometries — normally one)
    and summed with np.bincount: no loop over polygons, and never more than one band in memory.
    """
    if len(ids) == 0:
        return
    h, w = raster.shape
    b = shapely.bounds(geoms[ids])
    col0, row0 = _to_pixel(transform, b[:, 0], b[:, 3])
    col1, row1 = _to_pixel(transform, b[:, 2], b[:, 1])
    col0 = np.clip(np.floor(col0) - 1, 0, w).astype(np.int64)
    col1 = np.clip(np.ceil(col1) + 1, 0, w).astype(np.int64)
    row0 = np.clip(np.floor(row0) - 1, 0, h).astype(np.int64)
    row1 = np.clip(np.ceil(row1) + 1, 0, h).astype(np.int64)
    on_grid = (row1 > row0) & (col1 > col0)
    if not on_grid.any():
        return
    layer_of = np.zeros(len(ids), dtype=np.int64)
    for k, members in enumerate(layers):
        layer_of[members] = k
    band = max(1, band_px // w)
    for a in range(int(row0[on_grid].min()), int(row1[on_grid].max()), band):
        end = min(a + band, h)
        here = on_grid & (row0 < end) & (row1 > a)  # geometries that reach rows a .. end-1
        if not here.any():
            continue
        c_lo, c_hi = int(col0[here].min()), int(col1[here].max())
        values = raster.rows(a, end)[:, c_lo:c_hi]
        window = Affine(transform.a, 0.0, transform.c + c_lo * transform.a,
                        0.0, transform.e, transform.f + a * transform.e)
        for k in range(len(layers)):
            sel = np.flatnonzero(here & (layer_of == k))
            if not len(sel):
                continue
            try:
                labels = rasterize(
                    ((g, i) for i, g in enumerate(geoms[ids[sel]], start=1)),
                    out_shape=values.shape, transform=window, fill=0, all_touched=all_touched,
                    dtype="uint16" if len(sel) < 65535 else "int32",
                )
            except ValueError:  # rasterio found nothing it can burn (e.g. only degenerate outlines)
                continue
            inside = labels > 0
            if not inside.any():
                continue
            lab, v = labels[inside], values[inside]
            good = np.isfinite(v)
            n = len(sel) + 1
            target = ids[sel]
            seen[target] += np.bincount(lab, minlength=n)[1:]
            counts[target] += np.bincount(lab[good], minlength=n)[1:]
            sums[target] += np.bincount(lab[good], weights=v[good], minlength=n)[1:]


def zonal_mean(values, transform: Affine, geoms, *, band_px: int = 1_000_000) -> ZonalResult:
    """Mean of a raster (NaN = no data, north-up `transform`) inside each geometry.

    `values` is a 2-D array, or anything with `.shape` and `.rows(a, b)` (see `SlopeBands`).
    Geometries must be in the raster's coordinates. Pass 1 counts pixels whose centre is inside;
    geometries that caught no pixel centre get pass 2 (every pixel they touch) and, if they
    still have nothing, pass 3 (the pixel under an interior point). NaN only if all the pixels a
    geometry covers are missing, or it lies outside the raster. `band_px` bounds the memory:
    the raster is processed in bands of about that many pixels.
    """
    if transform.b != 0 or transform.d != 0 or transform.a <= 0 or transform.e >= 0:
        raise ValueError("zonal_mean needs a north-up grid (no rotation, x to the east, y to the north)")
    raster = values if hasattr(values, "rows") else _ArrayBands(values)
    geoms = np.asarray(geoms, dtype=object)
    n = len(geoms)
    sums, counts, seen = np.zeros(n, np.float64), np.zeros(n, np.int64), np.zeros(n, np.int64)
    method = np.full(n, "no_data", dtype=object)
    usable = ~(shapely.is_missing(geoms) | shapely.is_empty(geoms))
    method[~usable] = "no_geometry"
    idx = np.flatnonzero(usable)

    # pass 1: pixel centre inside the polygon
    _burn(raster, transform, geoms, idx, _disjoint_layers(geoms[idx]), False, sums, counts, seen, band_px)
    method[idx[counts[idx] > 0]] = "pixel_centre"

    # pass 2: polygons narrower than a pixel — every pixel they touch. Touched pixels are not
    # exclusive, so group by pixel footprint (bounds snapped outwards to the pixel grid).
    left = idx[seen[idx] == 0]
    if len(left):
        b = shapely.bounds(geoms[left])
        x0, y0 = _to_pixel(transform, b[:, 0], b[:, 3])
        x1, y1 = _to_pixel(transform, b[:, 2], b[:, 1])
        footprints = shapely.box(np.floor(x0) - 0.5, np.floor(y0) - 0.5, np.floor(x1) + 1.5, np.floor(y1) + 1.5)
        _burn(raster, transform, geoms, left, _disjoint_layers(footprints), True, sums, counts, seen, band_px)
        method[left[counts[left] > 0]] = "all_touched"

    # pass 3: still nothing burned (degenerate outline) — the pixel under an interior point
    left = left[seen[left] == 0] if len(left) else left
    if len(left):
        pts = shapely.point_on_surface(geoms[left])
        col, row = _to_pixel(transform, shapely.get_x(pts), shapely.get_y(pts))
        ok = np.isfinite(col) & np.isfinite(row)
        col = np.floor(np.where(ok, col, -1)).astype(np.int64)
        row = np.floor(np.where(ok, row, -1)).astype(np.int64)
        ok &= (row >= 0) & (row < raster.shape[0]) & (col >= 0) & (col < raster.shape[1])
        v = np.full(len(left), np.nan)
        for r in np.unique(row[ok]):  # a handful of degenerate outlines at most
            at = ok & (row == r)
            v[at] = raster.rows(int(r), int(r) + 1)[0, col[at]]
        hit = np.isfinite(v)
        sums[left[hit]], counts[left[hit]] = v[hit], 1
        method[left[hit]] = "point"

    mean = np.full(n, np.nan)
    has = counts > 0
    mean[has] = sums[has] / counts[has]
    return ZonalResult(mean=mean, n_pixels=counts, method=method)


# ----------------------------------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------------------------------

def field_slopes(fields, *, sources: Sequence[str] = DEFAULT_SOURCES, cache_dir: str | Path | None = None,
                 buffer_px: int = BUFFER_PX, allow_unavailable: bool = False, quiet: bool = False) -> pd.DataFrame:
    """Per-field slope with diagnostics: columns `slope_pct`, `n_pixels`, `method`; index as `fields`.

    `fields` is a GeoDataFrame/GeoSeries (EPSG:4326; anything else is reprojected). The frame's
    `.attrs["provenance"]` describes the DEM and the method. See `mean_slope_pct`.
    """
    t_start = time.monotonic()
    sources = [sources] if isinstance(sources, str) else list(sources)
    geo = fields.geometry if isinstance(fields, gpd.GeoDataFrame) else gpd.GeoSeries(fields)
    if geo.crs is not None and geo.crs.to_epsg() != 4326:
        geo = geo.to_crs("EPSG:4326")
    geoms = np.asarray(geo.values, dtype=object)
    n = len(geoms)
    usable = ~(shapely.is_missing(geoms) | shapely.is_empty(geoms))
    out = pd.DataFrame(
        {"slope_pct": np.full(n, np.nan), "n_pixels": np.zeros(n, np.int64),
         "method": np.where(usable, "no_data", "no_geometry").astype(object)},
        index=fields.index,
    )
    prov: dict = {
        "step": "slope_pct", "source": None, "source_label": None, "method": METHOD,
        "sources_requested": sources, "attempts": [], "n_fields": int(n),
        "computed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out.attrs["provenance"] = prov
    if not usable.any():
        prov["note"] = "no usable geometries"
        return out

    bounds = shapely.bounds(geoms[usable])
    if bounds[:, 0].min() < -180 or bounds[:, 2].max() > 180 or bounds[:, 1].min() < -90 or bounds[:, 3].max() > 90:
        raise ValueError("field geometries are not lon/lat degrees — set the GeoDataFrame's CRS")
    cache = Path(cache_dir) if cache_dir is not None else dem.DEM_DIR
    try:
        mosaic, attempts = dem.load_dem(bounds, sources, cache_dir=cache,
                                        buffer_px=buffer_px, quiet=quiet)
    except DemUnavailable as exc:
        prov["attempts"] = exc.attempts
        prov["error"] = str(exc)
        if allow_unavailable:
            log(f"slope: {exc} — slope_pct left empty")
            return out
        raise
    t_dem = time.monotonic()

    src = mosaic.source
    cache_used = (cache / src.key).resolve()
    shape, grid_bounds = mosaic.z.shape, mosaic.bounds
    dx, dy = pixel_size_m(mosaic.row_latitudes(), mosaic.res)
    # slope and zonal sums go band by band: the full slope raster is never in memory
    res = zonal_mean(SlopeBands(mosaic.z, dx, dy), mosaic.transform, geoms)
    mosaic.z = None
    t_zonal = time.monotonic()

    out["slope_pct"] = res.mean
    out["n_pixels"] = res.n_pixels
    out["method"] = res.method
    methods = pd.Series(res.method).value_counts().to_dict()
    mid = len(dx) // 2
    prov.update({
        "source": src.key,
        "source_label": src.label,
        "fallback_used": src.key != sources[0],
        "attempts": attempts,
        "resolution_arcsec": round(3600.0 / src.px_per_deg, 6),
        "pixel_size_m": [round(float(dx[mid]), 2), round(float(dy[mid]), 2)],  # east-west, north-south
        "horizontal_crs": f"{src.horizontal_crs} lon/lat grid, used as EPSG:4326 (NAD83 vs WGS84 differ by ~1 m here)",
        "vertical": src.vertical,
        "source_note": src.note,
        "tiles": mosaic.tiles,
        "urls": [t["url"] for t in mosaic.tiles],
        "cache_dir": str(cache_used.relative_to(ROOT) if cache_used.is_relative_to(ROOT) else cache_used),
        "cache_units_used": int(mosaic.blocks_used),  # 3DEP: fixed-grid blocks; SRTM: whole tiles
        "cache_units_fetched_this_run": int(mosaic.blocks_fetched),
        "cache_bytes_used": int(mosaic.bytes_cached),
        "grid_shape": [int(shape[0]), int(shape[1])],
        "grid_bounds": [round(float(v), 6) for v in grid_bounds],
        "buffer_px": int(buffer_px),
        "fields_by_method": {str(k): int(v) for k, v in methods.items()},
        "n_nan": int(np.isnan(res.mean).sum()),
        "seconds": {"dem": round(t_dem - t_start, 3), "slope_and_zonal": round(t_zonal - t_dem, 3),
                    "total": round(t_zonal - t_start, 3)},
    })
    if not quiet:
        ok = res.mean[np.isfinite(res.mean)]
        stats = f"median {np.median(ok):.2f}%, mean {ok.mean():.2f}%, max {ok.max():.2f}%" if len(ok) else "no values"
        log(f"slope: {n:,} fields on {src.key} ({shape[0]}x{shape[1]} px, "
            f"{prov['pixel_size_m'][0]:.1f} x {prov['pixel_size_m'][1]:.1f} m): {stats}; "
            f"{prov['n_nan']} NaN; {prov['seconds']['total']:.1f}s")
    return out


def mean_slope_pct(fields, *, sources: Sequence[str] = DEFAULT_SOURCES, cache_dir: str | Path | None = None,
                   buffer_px: int = BUFFER_PX, allow_unavailable: bool = False, quiet: bool = False) -> pd.Series:
    """Mean terrain slope of every field, in percent rise (100 * rise / run).

    fields   GeoDataFrame (or GeoSeries) of polygons, EPSG:4326.
    sources  DEM sources tried in order (keys of `pipeline.dem.SOURCES`): the first that can
             supply all the elevation needed is used for *every* field. Default: 3DEP 1/3",
             then 3DEP 1", then the SRTM-layout tiles.
    Returns a float Series named "slope_pct" with the same index as `fields`; NaN only where
    there is no elevation data (or no geometry). `.attrs["provenance"]` is a JSON-serialisable
    dict: source used, resolution, tiles and URLs, method, how many fields used which rule.

    Raises `DemUnavailable` if no source can be fetched and nothing is cached — unless
    `allow_unavailable=True`, in which case every value is NaN and the provenance says why.
    """
    table = field_slopes(fields, sources=sources, cache_dir=cache_dir, buffer_px=buffer_px,
                         allow_unavailable=allow_unavailable, quiet=quiet)
    series = table["slope_pct"].astype("float64").rename("slope_pct")
    series.attrs["provenance"] = table.attrs["provenance"]
    return series


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------

BINS = [0, 0.5, 1, 2, 3, 5, 8, 15, np.inf]


def describe(values) -> str:
    """Distribution summary of slope values (percent)."""
    v = np.asarray(values, dtype=float)
    ok = v[np.isfinite(v)]
    lines = [f"  fields {len(v):,}   with a value {len(ok):,}   NaN {len(v) - len(ok):,}"]
    if not len(ok):
        return "\n".join(lines)
    q = np.percentile(ok, [5, 25, 50, 75, 95, 99])
    lines.append(f"  min {ok.min():.2f}   p05 {q[0]:.2f}   p25 {q[1]:.2f}   median {q[2]:.2f}   mean {ok.mean():.2f}"
                 f"   p75 {q[3]:.2f}   p95 {q[4]:.2f}   p99 {q[5]:.2f}   max {ok.max():.2f}")
    hist, _ = np.histogram(ok, bins=BINS)
    for lo, hi, k in zip(BINS[:-1], BINS[1:], hist):
        label = f"{lo:g}-{hi:g}%" if np.isfinite(hi) else f">{lo:g}%"
        lines.append(f"  {label:>8} {k:>7,}  {100 * k / len(ok):5.1f}%  {'#' * int(round(50 * k / len(ok)))}")
    return "\n".join(lines)


def _print_provenance(prov: dict) -> None:
    if prov.get("source") is None:
        print(f"DEM: none ({prov.get('error') or prov.get('note')})")
        return
    px = prov["pixel_size_m"]
    print(f"DEM: {prov['source_label']} [{prov['source']}]"
          f"{'  (FALLBACK — ' + prov['attempts'][0]['error'] + ')' if prov['fallback_used'] else ''}")
    print(f"     pixel {px[0]} m (E-W) x {px[1]} m (N-S); grid {prov['grid_shape'][0]} x {prov['grid_shape'][1]}; "
          f"{prov['cache_units_used']} cached unit(s), {prov['cache_units_fetched_this_run']} fetched this run, "
          f"{prov['cache_bytes_used'] / 1e6:.1f} MB on disk")
    for t in prov["tiles"]:
        version = f", last modified {t['last_modified']}" if t.get("last_modified") else ""
        print(f"     {t['url']} ({t['blocks']} block(s){version})")
    s = prov["seconds"]
    print(f"     time: elevation {s['dem']}s, slope + zonal mean {s['slope_and_zonal']}s, total {s['total']}s")
    print(f"     fields by rule: {prov['fields_by_method']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mean terrain slope (percent rise) per field.")
    ap.add_argument("fields", nargs="?", default=str(FAKE_PATH), help="GeoParquet with field_id + geometry "
                    "(default: data/fields_fake.parquet)")
    ap.add_argument("--source", default=",".join(DEFAULT_SOURCES),
                    help=f"comma-separated fallback order; choices: {', '.join(dem.SOURCES)} (default: %(default)s)")
    ap.add_argument("--compare", metavar="SOURCES", help="also compute these sources (comma-separated), "
                    "each on its own, and print them side by side")
    ap.add_argument("--all", action="store_true", help="print every field (default: all if <= 60, else a sample)")
    args = ap.parse_args(argv)

    path = Path(args.fields)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    fields = gpd.read_parquet(path)
    if "field_id" in fields.columns:
        ids = fields["field_id"].astype(str)
    else:
        ids = pd.Series(fields.index.astype(str), index=fields.index)

    try:
        table = field_slopes(fields, sources=[s.strip() for s in args.source.split(",") if s.strip()])
    except DemUnavailable as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    table.insert(0, "field_id", ids)
    extra: dict[str, pd.DataFrame] = {}
    for key in [s.strip() for s in (args.compare or "").split(",") if s.strip()]:
        try:
            extra[key] = field_slopes(fields, sources=[key])
        except DemUnavailable as exc:
            print(f"compare: {key} unavailable ({exc})", file=sys.stderr)
    for key, t in extra.items():
        table[f"{key}_pct"] = t["slope_pct"]

    print(f"\nslope_pct for {len(table):,} fields in {path}")
    _print_provenance(table.attrs["provenance"])
    show = table if (args.all or len(table) <= 60) else pd.concat(
        [table.head(20), table.sort_values("slope_pct", ascending=False).head(10)]).drop_duplicates("field_id")
    if len(show) < len(table):
        print(f"\nfirst 20 fields and the 10 steepest (use --all for all {len(table):,}):")
    else:
        print()
    with pd.option_context("display.max_rows", None, "display.width", 200):
        print(show.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print("\ndistribution of slope_pct (percent rise):")
    print(describe(table["slope_pct"]))
    for key, t in extra.items():
        both = table["slope_pct"].notna() & t["slope_pct"].notna()
        d = (t["slope_pct"] - table["slope_pct"])[both]
        r = np.corrcoef(table["slope_pct"][both], t["slope_pct"][both])[0, 1] if both.sum() > 2 else float("nan")
        print(f"\n{key} ({t.attrs['provenance']['source_label']}) vs {table.attrs['provenance']['source']}:")
        print(describe(t["slope_pct"]))
        print(f"  difference ({key} - {table.attrs['provenance']['source']}): mean {d.mean():+.2f}, "
              f"median {d.median():+.2f}, max |d| {d.abs().max():.2f} percentage points; Pearson r {r:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
