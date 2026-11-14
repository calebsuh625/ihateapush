"""Fallback field outlines: segment contiguous cropland straight from the CDL rasters.

Used when the Crop Sequence Boundaries cannot be had (`build_fields(source="cdl-segment")`
or `source="auto"` after a CSB failure). It follows the idea behind CSB itself - a field
is a contiguous patch of pixels that were managed the same way for several years - but
much more crudely:

1. Take the most recent `window` years (default 8, as CSB does) and put them on one
   grid (the most common grid of those years; others are resampled nearest-neighbour).
2. Speckle filter each year: connected patches smaller than `year_sieve_px` pixels are
   absorbed by their largest neighbour (GDAL sieve, 4-connectivity).
3. A pixel is *cropland* when its class is a crop class (`cdl_codes.is_crop`) in at
   least half of the window years. Everything else is background.
4. Each cropland pixel gets a *signature*: its sequence of class codes over the window.
5. Sieve the signature image: patches smaller than `sieve_ha` are absorbed by their
   largest neighbour. That removes the mixed pixels along field edges and small
   inclusions, and drops isolated crop specks.
6. Connected components of equal signature (4-connectivity) are polygonised
   (`rasterio.features.shapes`); components smaller than `min_area_ha` are dropped.
7. The stair-stepped outlines are simplified a little with `shapely.coverage_simplify`,
   which keeps shared edges shared (no overlaps or gaps between neighbours). The
   default tolerance of 25 m removes single-pixel steps and moves no edge by more than
   one pixel. (The pixel outlines are first given a vertex at every pixel corner so
   that neighbours share identical vertices, which the coverage functions require.) If
   the polygons still do not form a valid coverage each polygon is simplified on its
   own instead. Results are single, valid Polygons.

Known limits (why CSB is preferred): no road/rail network is used, so neighbouring fields
with the same 8-year sequence merge across a section road that is narrower than a
pixel; outlines follow 30 m pixels; year-to-year classification noise splits some fields.

    python pipeline/cdl_segment.py      # segment the county and print counts
"""
from __future__ import annotations

import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/cdl_segment.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import shapely  # noqa: E402
from rasterio import features  # noqa: E402
from rasterio.warp import Resampling, reproject  # noqa: E402
from shapely.geometry import shape  # noqa: E402

from pipeline.cdl import grid_key  # noqa: E402
from pipeline.cdl_codes import IS_CROP_LUT  # noqa: E402
from pipeline.common import MIN_FIELD_HA, log  # noqa: E402

WINDOW_YEARS = 8  # at most 8: the signature packs one byte per year into a uint64
YEAR_SIEVE_PX = 6  # per-year speckle: patches under 6 pixels (0.54 ha at 30 m)
SIMPLIFY_M = 25.0  # coverage_simplify tolerance; 25 m removes one-pixel (30 m) stair steps only


def largest_polygon(geom):
    """The largest Polygon inside any geometry (None if it has no polygonal part)."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom
    parts = [p for p in shapely.get_parts(geom) if p.geom_type in ("Polygon", "MultiPolygon") and not p.is_empty]
    polys = []
    for p in parts:
        polys.extend(shapely.get_parts(p) if p.geom_type == "MultiPolygon" else [p])
    polys = [p for p in polys if p.geom_type == "Polygon" and not p.is_empty]
    return max(polys, key=lambda p: p.area) if polys else None


def clean_polygons(geoms) -> list:
    """make_valid + keep the largest Polygon part of each geometry (None where nothing is left)."""
    arr = np.asarray(geoms, dtype=object)
    out = []
    for geom in arr:
        if geom is not None and not geom.is_empty and not geom.is_valid:
            geom = shapely.make_valid(geom)
        geom = largest_polygon(geom)
        if geom is not None and not geom.is_valid:  # belt and braces
            geom = largest_polygon(geom.buffer(0))
        out.append(geom)
    return out


def load_stack(rasters: dict[int, Path], years: list[int]):
    """The given years as one uint8 array (n_years, rows, cols) on a common grid.

    Returns (stack, crs, transform, n_resampled). The reference grid is the most common
    grid among the years (ties: the most recent year's).
    """
    meta = {}
    for year in years:
        with rasterio.open(rasters[year]) as src:
            meta[year] = (grid_key(src.crs, src.transform, src.width, src.height), src.crs, src.transform, src.width, src.height)
    counts = Counter(m[0] for m in meta.values())
    best = max(counts.values())
    ref_year = max(y for y in years if counts[meta[y][0]] == best)
    ref_key, crs, transform, width, height = meta[ref_year]

    stack = np.zeros((len(years), height, width), dtype=np.uint8)
    n_resampled = 0
    for k, year in enumerate(years):
        with rasterio.open(rasters[year]) as src:
            if meta[year][0] == ref_key:
                stack[k] = src.read(1)
            else:
                n_resampled += 1
                reproject(
                    source=src.read(1),
                    destination=stack[k],
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=crs,
                    resampling=Resampling.nearest,
                    src_nodata=None,
                    dst_nodata=0,
                )
    return stack, crs, transform, n_resampled


def signature_ids(stack: np.ndarray, cropland: np.ndarray) -> np.ndarray:
    """int32 image: 0 where not cropland, otherwise a number identifying the pixel's class sequence."""
    n_years = stack.shape[0]
    if n_years > 8:
        raise ValueError("signature_ids supports at most 8 years")
    sig = np.zeros(stack.shape[1:], dtype=np.uint64)
    for k in range(n_years):
        sig |= stack[k].astype(np.uint64) << np.uint64(8 * k)
    sig[~cropland] = 0
    _, inverse = np.unique(sig.ravel(), return_inverse=True)
    ids = inverse.reshape(sig.shape).astype(np.int32)
    # np.unique sorts, so signature 0 (if present) is index 0; make sure 0 means "not cropland"
    if cropland.all():
        ids += 1
    else:
        zero_index = int(ids[~cropland].flat[0])
        if zero_index != 0:  # cannot happen (0 sorts first) - defensive
            ids = np.where(ids == zero_index, 0, np.where(ids < zero_index, ids + 1, ids)).astype(np.int32)
    ids[~cropland] = 0
    return ids


def segment_fields(
    rasters: dict[int, Path],
    min_area_ha: float = MIN_FIELD_HA,
    *,
    window: int = WINDOW_YEARS,
    sieve_ha: float | None = None,
    year_sieve_px: int = YEAR_SIEVE_PX,
    simplify_m: float = SIMPLIFY_M,
    quiet: bool = False,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Field polygons (GeoDataFrame with only a geometry column, in the rasters' CRS) + a stats dict.

    `sieve_ha` defaults to half of `min_area_ha`.
    """
    if not rasters:
        raise RuntimeError("cdl-segment: no CDL rasters available")
    window = max(1, min(int(window), 8))
    years = sorted(rasters)[-window:]
    stack, crs, transform, n_resampled = load_stack(rasters, years)
    pixel_ha = abs(transform.a * transform.e) / 1e4
    sieve_ha = min_area_ha / 2 if sieve_ha is None else sieve_ha
    sieve_px = max(2, int(math.ceil(sieve_ha / pixel_ha)))
    min_px_area = min_area_ha * 1e4

    if year_sieve_px > 1:
        for k in range(len(years)):
            stack[k] = features.sieve(stack[k], size=year_sieve_px, connectivity=4)
    crop_years = IS_CROP_LUT[stack].sum(axis=0)
    cropland = crop_years >= math.ceil(len(years) / 2)
    ids = signature_ids(stack, cropland)
    n_signatures = int(ids.max())
    ids = features.sieve(ids, size=sieve_px, connectivity=4)

    polys = []
    n_components = 0
    for geom, _value in features.shapes(ids, mask=ids > 0, connectivity=4, transform=transform):
        n_components += 1
        poly = shape(geom)
        if poly.area >= min_px_area:
            polys.append(poly)
    n_before_simplify = len(polys)
    if not polys:
        raise RuntimeError("cdl-segment: no cropland component reaches the minimum field size")

    geoms = np.array(clean_polygons(polys), dtype=object)
    geoms = geoms[[g is not None for g in geoms]]
    simplifier = "none"
    if simplify_m > 0:
        try:
            # a vertex at every pixel corner, so neighbouring outlines are noded identically
            noded = shapely.segmentize(geoms, max(abs(transform.a), abs(transform.e)))
            if not bool(shapely.coverage_is_valid(noded)):
                raise ValueError("polygons do not form a valid coverage")
            geoms = np.asarray(shapely.coverage_simplify(noded, simplify_m), dtype=object)
            simplifier = "shapely.coverage_simplify"
        except Exception as exc:
            if not quiet:
                log(f"cdl-segment: coverage simplification not possible ({exc}); simplifying polygons one by one")
            geoms = np.asarray(shapely.simplify(geoms, simplify_m, preserve_topology=True), dtype=object)
            simplifier = "shapely.simplify (per polygon)"
    geoms = np.array(clean_polygons(geoms), dtype=object)
    geoms = geoms[[g is not None for g in geoms]]
    areas = shapely.area(geoms)
    geoms = geoms[areas >= min_px_area]

    gdf = gpd.GeoDataFrame(geometry=gpd.GeoSeries(list(geoms), crs=crs))
    stats = {
        "method": "connected components of equal multi-year CDL class sequence",
        "window_years": years,
        "years_resampled_to_reference_grid": n_resampled,
        "pixel_size_m": [abs(transform.a), abs(transform.e)],
        "cropland_rule": f"crop class in >= {math.ceil(len(years) / 2)} of {len(years)} window years",
        "cropland_pixels": int(cropland.sum()),
        "cropland_ha": round(float(cropland.sum()) * pixel_ha, 1),
        "year_sieve_px": year_sieve_px,
        "signature_sieve_px": sieve_px,
        "distinct_signatures": n_signatures,
        "components_after_sieve": n_components,
        "components_ge_min_area": n_before_simplify,
        "simplify_m": simplify_m,
        "simplifier": simplifier,
        "polygons": int(len(gdf)),
    }
    if not quiet:
        log(
            f"cdl-segment: {stats['cropland_ha']:,.0f} ha cropland pixels, {n_components:,} components, "
            f"{len(gdf):,} polygons >= {min_area_ha:g} ha ({simplifier})"
        )
    return gdf, stats


def main() -> int:
    from pipeline.cdl import available_rasters, fetch_all

    gdf, stats = segment_fields(available_rasters(fetch_all(quiet=True)))
    for key, value in stats.items():
        print(f"{key}: {value}")
    area = gdf.geometry.area / 1e4
    print(f"total {area.sum():,.0f} ha; median {area.median():.1f} ha; max {area.max():.1f} ha")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
