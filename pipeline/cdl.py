"""USDA Cropland Data Layer (CDL) county rasters and per-field majority crop.

Two jobs:

1. **Download** one CDL GeoTIFF per year, clipped to the county by the CropScape web
   service, into `data/raw/cdl/CDL_<year>_<fips>.tif` (`fetch_cdl`, `fetch_all`).
2. **Zonal majority**: for a set of field polygons, the most frequent CDL class inside
   each polygon for every year (`zonal_majority`) — vectorised, no per-polygon loops.

How the majority is computed
----------------------------
Field polygons are reprojected to the raster's CRS and burned into a *label image* on
that raster's exact grid (pixel value = field number, 0 = no field; a pixel belongs to a
field when its centre is inside the polygon). Years that share a grid share one label
image; a year on a different grid or resolution simply gets its own. The class counts
of every field are then one `np.bincount` over `field * 256 + class`.

* Pixels whose CDL value is 0 (background), the raster's nodata value or 81
  ("Clouds/No Data") do not vote. A field with no voting pixel in a year gets code 0 for
  that year, and the caller omits the year from the history.
* A polygon so small or narrow that it contains no pixel centre at all is burned again,
  on its own, with `all_touched=True` (every pixel it touches votes). Only those
  polygons get that treatment, so large fields are not polluted by their neighbours'
  edge pixels.
* Ties are broken towards the lowest CDL code (deterministic).

    python pipeline/cdl.py              # download/verify all years and print the grids
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/cdl.py`
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
import requests  # noqa: E402
from affine import Affine  # noqa: E402
from rasterio import features  # noqa: E402

from pipeline.common import FIPS, RAW_DIR, USER_AGENT, YEARS, download, log  # noqa: E402

CDL_DIR = RAW_DIR / "cdl"

# CropScape (George Mason University, for USDA NASS). `GetCDLFile` clips the national CDL
# to a county and answers with a small XML document whose <returnURL> is a GeoTIFF.
CDL_SERVICE_URL = "https://nassgeodata.gmu.edu/axis2/services/CDLService/GetCDLFile"
# The returnURL is deterministic and the file normally already sits in the service's own
# cache, so it is tried first: it answers in well under a second, while GetCDLFile takes
# ~2 minutes per call (measured 2026-11-14). If the cached file is missing (HTTP 404)
# GetCDLFile is called, which makes the server (re)create it.
CDL_CACHE_URL = "https://nassgeodata.gmu.edu/webservice/nass_data_cache/byfips/CDL_{year}_{fips}.tif"

BACKGROUND = 0  # CDL "Background"
CLOUDS_NO_DATA = 81  # CDL "Clouds/No Data" (legacy class)
N_CODES = 256


# --------------------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------------------
def cdl_path(year: int, fips: str = FIPS) -> Path:
    return CDL_DIR / f"CDL_{year}_{fips}.tif"


def _origin_path(tif: Path) -> Path:
    """Sidecar remembering where a cached raster was downloaded from (url, route, time)."""
    return tif.with_name(tif.name + ".json")


def describe_raster(path: str | Path) -> dict:
    """Grid facts of a CDL GeoTIFF. Raises if the file is not a usable single-band raster."""
    with rasterio.open(path) as src:
        if src.count != 1:
            raise ValueError(f"{path}: expected 1 band, found {src.count}")
        if src.crs is None:
            raise ValueError(f"{path}: no CRS")
        if src.dtypes[0] != "uint8":
            raise ValueError(f"{path}: expected uint8 class codes, found {src.dtypes[0]}")
        if src.width < 2 or src.height < 2:
            raise ValueError(f"{path}: implausibly small raster {src.width}x{src.height}")
        t = src.transform
        return {
            "crs": src.crs.to_string(),
            "epsg": src.crs.to_epsg(),
            "width": src.width,
            "height": src.height,
            "pixel_size_m": [abs(t.a), abs(t.e)],
            "origin": [t.c, t.f],
            "nodata": src.nodata,
            "bytes": Path(path).stat().st_size,
        }


def _service_return_url(year: int, fips: str, timeout: float) -> str:
    resp = requests.get(
        CDL_SERVICE_URL, params={"year": year, "fips": fips}, timeout=timeout, headers={"User-Agent": USER_AGENT}
    )
    resp.raise_for_status()
    match = re.search(r"<returnURL>\s*([^<\s]+)\s*</returnURL>", resp.text)
    if not match:
        raise RuntimeError(f"GetCDLFile {year}/{fips}: no <returnURL> in response: {resp.text[:200]!r}")
    return match.group(1)


def fetch_cdl(year: int, fips: str = FIPS, *, service_timeout: float = 420, quiet: bool = False) -> dict:
    """Make sure the county CDL raster for `year` is cached. Never raises.

    Returns a dict: year, path (None if the year could not be obtained), url, route
    ("local-cache" | "cropscape-cache-url" | "GetCDLFile"), downloaded_via /
    downloaded_at (how and when the file originally arrived), error (None if fine) and
    the grid facts from `describe_raster`.
    """
    dest = cdl_path(year, fips)
    info: dict = {"year": year, "path": None, "url": None, "route": None, "downloaded_via": None, "downloaded_at": None, "error": None}
    errors: list[str] = []

    def remember(route: str, url: str) -> None:
        info["route"] = info["downloaded_via"] = route
        info["url"] = url
        info["downloaded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _origin_path(dest).write_text(
            json.dumps({"url": url, "route": route, "downloaded_at": info["downloaded_at"]}, indent=2)
        )

    def usable() -> bool:
        try:
            info.update(describe_raster(dest))
            info["path"] = str(dest)
            return True
        except Exception as exc:  # corrupt / truncated / not a GeoTIFF (e.g. an HTML error page)
            errors.append(f"unusable file ({exc})")
            dest.unlink(missing_ok=True)
            return False

    if dest.exists() and dest.stat().st_size > 0:
        info["route"] = "local-cache"
        try:
            origin = json.loads(_origin_path(dest).read_text())
        except (OSError, ValueError):
            origin = {}
        info["url"] = origin.get("url")
        info["downloaded_via"] = origin.get("route")
        info["downloaded_at"] = origin.get("downloaded_at")
        if usable():
            return info
        if not quiet:
            log(f"CDL {year}: cached file was unusable and has been deleted; downloading again")

    # 1) the service's own cache URL (fast)
    url = CDL_CACHE_URL.format(year=year, fips=fips)
    try:
        download(url, dest, timeout=120, retries=2, quiet=quiet)
        if usable():
            remember("cropscape-cache-url", url)
            return info
    except Exception as exc:
        errors.append(f"cache url: {exc}")

    # 2) the documented API (slow: the server clips the national raster on demand)
    for attempt in range(1, 4):
        try:
            if not quiet:
                log(f"CDL {year}: asking CropScape GetCDLFile (attempt {attempt}/3, can take minutes)")
            url = _service_return_url(year, fips, service_timeout)
            download(url, dest, timeout=300, retries=3, quiet=quiet)
            if usable():
                remember("GetCDLFile", url)
                return info
        except Exception as exc:
            errors.append(f"GetCDLFile attempt {attempt}: {exc}")
        time.sleep(5 * attempt)

    info["route"] = info["url"] = None
    info["error"] = "; ".join(errors) or "unknown error"
    log(f"!!! CDL {year}: COULD NOT BE DOWNLOADED - this year will be missing from every crop history ({info['error']})")
    return info


def fetch_all(years: list[int] | None = None, fips: str = FIPS, *, quiet: bool = False) -> dict[int, dict]:
    """`fetch_cdl` for every year, one request at a time (be polite to the server)."""
    years = list(YEARS) if years is None else list(years)
    out = {year: fetch_cdl(year, fips, quiet=quiet) for year in years}
    missing = [y for y, i in out.items() if i["path"] is None]
    if not quiet:
        cached = sum(i["route"] == "local-cache" for i in out.values())
        log(f"CDL: {cached} year(s) from data/raw/cdl, {len(years) - cached - len(missing)} downloaded, {len(missing)} missing")
    if missing:
        log(f"!!! CDL: {len(missing)} of {len(years)} year(s) unavailable: {missing}")
    return out


def available_rasters(infos: dict[int, dict]) -> dict[int, Path]:
    """year -> path for the years that were obtained, in year order."""
    return {y: Path(i["path"]) for y, i in sorted(infos.items()) if i.get("path")}


# --------------------------------------------------------------------------------------
# zonal majority
# --------------------------------------------------------------------------------------
@dataclass
class Labels:
    """Field membership of the pixels of one raster grid."""

    image: np.ndarray  # int32 (rows, cols): field number + 1, 0 = no field (pixel-centre rule)
    extra_field: np.ndarray  # int64: field numbers of the all_touched fallback pixels ...
    extra_pixel: np.ndarray  # int64: ... and their flat pixel indices
    n_fallback: int  # fields that needed the all_touched fallback
    n_outside: int  # fields with no pixel at all on this grid (outside the raster)


@dataclass
class ZonalResult:
    years: list[int]
    codes: np.ndarray  # uint8 (n_fields, n_years): majority CDL code, 0 = no voting pixel
    share: np.ndarray  # float32 (n_fields, n_years): winning class's share of the voting pixels
    pixels: np.ndarray  # int32 (n_fields, n_years): number of voting pixels
    per_year: dict[int, dict] = field(default_factory=dict)
    grids: list[dict] = field(default_factory=list)


def grid_key(crs, transform: Affine, width: int, height: int) -> tuple:
    """Hashable identity of a raster grid; rasters with the same key share a label image."""
    return (crs.to_wkt() if crs is not None else None, tuple(round(v, 6) for v in tuple(transform)[:6]), width, height)


def _touched_pixels(geom, transform: Affine, shape: tuple[int, int]) -> np.ndarray:
    """Flat indices of every pixel of the grid that `geom` touches (all_touched=True)."""
    height, width = shape
    minx, miny, maxx, maxy = geom.bounds
    inv = ~transform  # world -> (col, row); written out so it works for any affine version
    corners = ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy))
    cols = [inv.a * x + inv.b * y + inv.c for x, y in corners]
    rows = [inv.d * x + inv.e * y + inv.f for x, y in corners]
    c0, c1 = max(int(math.floor(min(cols))), 0), min(int(math.ceil(max(cols))), width)
    r0, r1 = max(int(math.floor(min(rows))), 0), min(int(math.ceil(max(rows))), height)
    if c1 <= c0 or r1 <= r0:
        return np.empty(0, dtype=np.int64)
    window_transform = Affine(
        transform.a,
        transform.b,
        transform.c + c0 * transform.a + r0 * transform.b,
        transform.d,
        transform.e,
        transform.f + c0 * transform.d + r0 * transform.e,
    )
    burned = features.rasterize(
        [(geom, 1)], out_shape=(r1 - r0, c1 - c0), transform=window_transform, fill=0, all_touched=True, dtype="uint8"
    )
    rr, cc = np.nonzero(burned)
    return (rr.astype(np.int64) + r0) * width + (cc.astype(np.int64) + c0)


def rasterize_labels(geoms, transform: Affine, shape: tuple[int, int]) -> Labels:
    """Burn polygons (already in the grid's CRS) into a label image.

    Field `i` gets pixel value `i + 1`; a pixel is the field's when its centre is inside.
    Where polygons overlap the later one wins (CSB polygons do not overlap). Polygons
    that catch no pixel centre fall back, one by one, to all_touched pixels.
    """
    geoms = list(geoms)
    n = len(geoms)
    pairs = [(g, i + 1) for i, g in enumerate(geoms) if g is not None and not g.is_empty]
    if pairs:
        image = features.rasterize(pairs, out_shape=shape, transform=transform, fill=0, all_touched=False, dtype="int32")
    else:
        image = np.zeros(shape, dtype="int32")
    hit = np.bincount(image.ravel(), minlength=n + 1)[1:]

    extra_field: list[np.ndarray] = []
    extra_pixel: list[np.ndarray] = []
    n_fallback = n_outside = 0
    for i in np.flatnonzero(hit == 0):
        geom = geoms[i]
        if geom is None or geom.is_empty:
            n_outside += 1
            continue
        pix = _touched_pixels(geom, transform, shape)
        if len(pix) == 0:
            n_outside += 1
            continue
        n_fallback += 1
        extra_pixel.append(pix)
        extra_field.append(np.full(len(pix), i, dtype=np.int64))
    return Labels(
        image=image,
        extra_field=np.concatenate(extra_field) if extra_field else np.empty(0, dtype=np.int64),
        extra_pixel=np.concatenate(extra_pixel) if extra_pixel else np.empty(0, dtype=np.int64),
        n_fallback=n_fallback,
        n_outside=n_outside,
    )


def voting_lut(nodata=None) -> np.ndarray:
    """Boolean lookup table over the 256 codes: may a pixel with this value vote?"""
    lut = np.ones(N_CODES, dtype=bool)
    lut[BACKGROUND] = False
    lut[CLOUDS_NO_DATA] = False
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)) and 0 <= int(nodata) < N_CODES:
        lut[int(nodata)] = False
    return lut


def class_counts(labels: Labels, classes: np.ndarray, n_fields: int, nodata=None) -> np.ndarray:
    """(n_fields, 256) table: how many voting pixels of each class every field has."""
    if classes.shape != labels.image.shape:
        raise ValueError(f"class raster {classes.shape} and label image {labels.image.shape} differ")
    lut = voting_lut(nodata)
    flat_labels = labels.image.ravel()
    flat_classes = classes.ravel()
    inside = np.flatnonzero(flat_labels)
    cls = flat_classes[inside]
    keep = lut[cls]
    keys = (flat_labels[inside][keep].astype(np.int64) - 1) * N_CODES + cls[keep]
    if len(labels.extra_pixel):
        cls_x = flat_classes[labels.extra_pixel]
        keep_x = lut[cls_x]
        keys = np.concatenate([keys, labels.extra_field[keep_x] * N_CODES + cls_x[keep_x]])
    return np.bincount(keys, minlength=n_fields * N_CODES).reshape(n_fields, N_CODES)


def majority(counts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """From a (n_fields, 256) count table: (winning code, its share, voting pixels).

    Fields without voting pixels get code 0 and share 0. Ties go to the lowest code
    (`argmax` returns the first maximum).
    """
    total = counts.sum(axis=1)
    winner = counts.argmax(axis=1)
    top = counts[np.arange(len(counts)), winner]
    codes = np.where(total > 0, winner, 0).astype(np.uint8)
    share = np.divide(top, total, out=np.zeros(len(counts), dtype=np.float64), where=total > 0).astype(np.float32)
    return codes, share, total.astype(np.int32)


def zonal_majority(geoms, rasters: dict[int, Path], *, quiet: bool = False) -> ZonalResult:
    """Majority CDL class of every polygon for every year.

    `geoms` is a GeoSeries (any CRS, must be set); `rasters` maps year -> GeoTIFF path.
    Row order of the result follows `geoms`; column order follows the sorted years.
    """
    if geoms.crs is None:
        raise ValueError("zonal_majority: the field geometries have no CRS")
    geoms = geoms.reset_index(drop=True)
    n = len(geoms)
    years = sorted(rasters)
    codes = np.zeros((n, len(years)), dtype=np.uint8)
    share = np.zeros((n, len(years)), dtype=np.float32)
    pixels = np.zeros((n, len(years)), dtype=np.int32)
    per_year: dict[int, dict] = {}
    label_cache: dict[tuple, Labels] = {}
    grid_info: dict[tuple, dict] = {}
    projected: dict[str, object] = {}  # CRS WKT -> geometries in that CRS

    for j, year in enumerate(years):
        with rasterio.open(rasters[year]) as src:
            classes = src.read(1)
            crs, transform, nodata = src.crs, src.transform, src.nodata
        key = grid_key(crs, transform, classes.shape[1], classes.shape[0])
        if key not in label_cache:
            wkt = crs.to_wkt()
            if wkt not in projected:
                projected[wkt] = geoms if geoms.crs == crs else geoms.to_crs(crs)
            label_cache[key] = rasterize_labels(projected[wkt].values, transform, classes.shape)
            lab = label_cache[key]
            grid_info[key] = {
                "crs": crs.to_string(),
                "pixel_size_m": [abs(transform.a), abs(transform.e)],
                "origin": [transform.c, transform.f],
                "width": classes.shape[1],
                "height": classes.shape[0],
                "years": [],
                "fields_all_touched_fallback": lab.n_fallback,
                "fields_outside_raster": lab.n_outside,
            }
            if not quiet:
                log(
                    f"label image for grid {classes.shape[1]}x{classes.shape[0]} @ {abs(transform.a):g} m "
                    f"({lab.n_fallback} tiny field(s) via all_touched, {lab.n_outside} outside the raster)"
                )
        grid_info[key]["years"].append(year)
        counts = class_counts(label_cache[key], classes, n, nodata)
        codes[:, j], share[:, j], pixels[:, j] = majority(counts)
        has = pixels[:, j] > 0
        per_year[year] = {
            "fields_with_data": int(has.sum()),
            "fields_without_data": int((~has).sum()),
            "mean_majority_share": round(float(share[has, j].mean()), 4) if has.any() else None,
            "pixel_size_m": abs(transform.a),
        }
    return ZonalResult(years=years, codes=codes, share=share, pixels=pixels, per_year=per_year, grids=list(grid_info.values()))


def main() -> int:
    infos = fetch_all()
    print()
    print(f"{'year':<6}{'obtained via':<22}{'size':>10}  {'grid':<12}{'pixel':>7}  {'origin':<26}{'crs':<12}nodata")
    for year, i in infos.items():
        if i["path"] is None:
            print(f"{year:<6}MISSING: {i['error']}")
            continue
        print(
            f"{year:<6}{i['downloaded_via'] or i['route']:<22}{i['bytes'] / 1e6:>8.2f}MB  {str(i['width']) + 'x' + str(i['height']):<12}"
            f"{i['pixel_size_m'][0]:>6g}m  {str(tuple(i['origin'])):<26}{i['crs']:<12}{i['nodata']}"
        )
    return 0 if all(i["path"] for i in infos.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
