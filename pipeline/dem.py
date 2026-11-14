"""Elevation data for the slope step (5.3): sources, on-disk cache and mosaicking.

Three sources, all public and without authentication, tried in this order:

* ``3dep13`` — USGS 3DEP seamless DEM, 1/3 arc-second (~10 m). Cloud-optimised GeoTIFFs on S3.
* ``3dep1``  — USGS 3DEP seamless DEM, 1 arc-second (~30 m). Same bucket, 8x smaller files.
* ``srtm``   — AWS Terrain Tiles "skadi" HGT, 1 arc-second, SRTM layout, integer metres.

`load_dem(bounds, sources=...)` returns the first source that can supply everything the fields
need; a source that cannot be fetched is skipped (see `DemFetchError`). Sources are never mixed.

Cache (``data/raw/dem/``) — nothing is keyed on the set of fields, so a run over a different
extent reuses whatever an earlier run already fetched:

* 3DEP: the tiles are COGs with 512x512 internal tiles, so only the parts that cover fields are
  read (HTTP range requests through GDAL's /vsicurl/). They are stored on a fixed grid of blocks
  in the *source tile's own pixel space* — ``3dep1/n38w101/USGS_1_n38w101_b512_r03_c05.tif`` is
  rows 1536..2047, columns 2560..3071 of that tile — as small float32 GeoTIFFs (metres, NaN = no
  data). ``tile.json`` next to them records the URL and the S3 ETag / Last-Modified the blocks
  came from; if USGS republishes a tile, its blocks are refetched rather than mixed.
* SRTM: whole ``.hgt.gz`` tiles (3-4 MB each) under ``srtm/``.

Every source is described on one global pixel lattice per source (`DemSource`), so mosaicking
is integer index arithmetic and never resamples the elevations.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import rasterio  # noqa: E402
import requests  # noqa: E402
from affine import Affine  # noqa: E402
from rasterio.windows import Window  # noqa: E402

from pipeline.common import RAW_DIR, USER_AGENT, download, log  # noqa: E402

DEM_DIR = RAW_DIR / "dem"

#: Order in which sources are tried: best first, each fallback coarser / further from USGS.
DEFAULT_SOURCES: tuple[str, ...] = ("3dep13", "3dep1", "srtm")

#: Refuse to build a mosaic bigger than this (4 bytes per pixel, and the slope step needs about
#: three such arrays). Finney County at 1/3 arc-second is ~57 M pixels; this only trips if the
#: fields span several degrees.
MAX_MOSAIC_PIXELS = 250_000_000

FETCH_THREADS = 8

#: GDAL settings for reading COG windows over HTTP: no directory listing, no sidecar probing,
#: merged range requests, bounded retries/timeouts so a dead network fails fast.
GDAL_HTTP_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_CONNECTTIMEOUT": "15",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_USERAGENT": USER_AGENT,
    "VSI_CACHE": "TRUE",
}


class DemFetchError(RuntimeError):
    """A source could not provide the elevation needed (network, HTTP error, bad tile)."""


class DemTooLarge(DemFetchError):
    """The fields span too many pixels of this source for one mosaic; a coarser source may do."""


class DemUnavailable(RuntimeError):
    """No source could provide elevation. `.attempts` lists what was tried and why it failed."""

    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        why = "; ".join(f"{a['source']}: {a['error']}" for a in attempts)
        super().__init__(f"no elevation source could be fetched ({why})")


@dataclass(frozen=True)
class DemSource:
    """One elevation product, described on a global pixel lattice.

    Global pixel (row r, col c) covers
        lon in [-180 + (c - shift)/P, -180 + (c + 1 - shift)/P)
        lat in (90 - (r + 1 - shift)/P, 90 - (r - shift)/P]
    with P = `px_per_deg` and shift = 0.5 for point-registered products (pixel *centres* on the
    1/P lattice, e.g. SRTM) or 0 for area-registered ones (pixel *edges* on it, e.g. 3DEP).

    Tiles are 1x1 degree cells. The tile for cell (k_r, k_c) = (r // P, c // P) holds global
    rows k_r*P - collar_lo .. (k_r+1)*P + collar_hi - 1 (same for columns): 3DEP tiles carry a
    6 px collar on every side, SRTM tiles repeat one row/column of their S and E neighbours.
    """

    key: str
    label: str
    px_per_deg: int
    collar_lo: int
    collar_hi: int
    point_registered: bool
    block: int  # cache block size in px of the source tile; 0 = whole tiles are cached
    url_template: str
    horizontal_crs: str
    vertical: str
    note: str = ""

    @property
    def res(self) -> float:
        return 1.0 / self.px_per_deg

    @property
    def shift(self) -> float:
        return 0.5 if self.point_registered else 0.0

    @property
    def tile_size(self) -> int:
        return self.px_per_deg + self.collar_lo + self.collar_hi

    def cell_corner(self, k_r: int, k_c: int) -> tuple[int, int]:
        """(lat of the southern edge, lon of the western edge) of cell (k_r, k_c), in degrees."""
        return 89 - k_r, k_c - 180

    def tile_name(self, k_r: int, k_c: int) -> str:
        lat_s, lon_w = self.cell_corner(k_r, k_c)
        ew = f"{'w' if lon_w < 0 else 'e'}{abs(lon_w):03d}"
        if self.key.startswith("3dep"):  # named by the NW corner, lower case
            lat_n = lat_s + 1
            return f"{'n' if lat_n > 0 else 's'}{abs(lat_n):02d}{ew}"
        return f"{'N' if lat_s >= 0 else 'S'}{abs(lat_s):02d}{ew.upper()}"  # SW corner, upper case

    def url(self, k_r: int, k_c: int) -> str:
        tile = self.tile_name(k_r, k_c)
        return self.url_template.format(tile=tile, band=tile[:3])

    def transform(self, r0: int, c0: int) -> Affine:
        """Affine (pixel → lon/lat) of a window whose top-left pixel is global (r0, c0)."""
        return Affine(self.res, 0.0, -180.0 + (c0 - self.shift) * self.res,
                      0.0, -self.res, 90.0 - (r0 - self.shift) * self.res)

    def windows(self, bounds, buffer_px: int = 0):
        """Per-geometry pixel windows (r_lo, r_hi, c_lo, c_hi; half-open) for an (N, 4) array of
        lon/lat bounds (minx, miny, maxx, maxy), each grown by `buffer_px` pixels."""
        b = np.atleast_2d(np.asarray(bounds, dtype=np.float64))
        p, sh = self.px_per_deg, self.shift
        c_lo = np.floor((b[:, 0] + 180.0) * p + sh).astype(np.int64) - buffer_px
        c_hi = np.floor((b[:, 2] + 180.0) * p + sh).astype(np.int64) + 1 + buffer_px
        r_lo = np.floor((90.0 - b[:, 3]) * p + sh).astype(np.int64) - buffer_px
        r_hi = np.floor((90.0 - b[:, 1]) * p + sh).astype(np.int64) + 1 + buffer_px
        return r_lo, r_hi, c_lo, c_hi


_TNM = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation"

SOURCES: dict[str, DemSource] = {
    "3dep1": DemSource(
        key="3dep1",
        label="USGS 3DEP seamless DEM, 1 arc-second (~30 m)",
        px_per_deg=3600, collar_lo=6, collar_hi=6, point_registered=False, block=512,
        url_template=_TNM + "/1/TIFF/current/{tile}/USGS_1_{tile}.tif",
        horizontal_crs="EPSG:4269",
        vertical="NAVD88, metres",
    ),
    "3dep13": DemSource(
        key="3dep13",
        label="USGS 3DEP seamless DEM, 1/3 arc-second (~10 m)",
        px_per_deg=10800, collar_lo=6, collar_hi=6, point_registered=False, block=1024,
        url_template=_TNM + "/13/TIFF/current/{tile}/USGS_13_{tile}.tif",
        horizontal_crs="EPSG:4269",
        vertical="NAVD88, metres",
    ),
    "srtm": DemSource(
        key="srtm",
        label="AWS Terrain Tiles 'skadi' HGT, 1 arc-second (~30 m, SRTM tile layout)",
        px_per_deg=3600, collar_lo=0, collar_hi=1, point_registered=True, block=0,
        url_template="https://s3.amazonaws.com/elevation-tiles-prod/skadi/{band}/{tile}.hgt.gz",
        horizontal_crs="EPSG:4326",
        vertical="whole metres above sea level (geoid model depends on the underlying data; irrelevant for slope)",
        note="Mapzen/Tilezen composite in SRTM .hgt layout (built around 2016; inside the US mostly from "
             "USGS NED, elsewhere SRTM). Heights are whole metres, so flat land comes out as zero-slope "
             "steps: unbiased over many fields, noisy for a single flat field.",
    ),
}


@dataclass
class DemMosaic:
    """Elevation in metres (float32, NaN = no data) on a north-up lon/lat grid."""

    z: np.ndarray
    transform: Affine
    source: DemSource
    tiles: list[dict] = field(default_factory=list)  # name, url, version info, blocks used/fetched
    blocks_used: int = 0
    blocks_fetched: int = 0
    bytes_cached: int = 0

    @property
    def res(self) -> float:
        return self.source.res

    def row_latitudes(self) -> np.ndarray:
        """Latitude of each row's pixel centres."""
        return self.transform.f + self.transform.e * (np.arange(self.z.shape[0]) + 0.5)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        h, w = self.z.shape
        t = self.transform
        return (t.c, t.f + t.e * h, t.c + t.a * w, t.f)


# ----------------------------------------------------------------------------------------------
# network fetchers — the only functions that touch the network (tests replace them)
# ----------------------------------------------------------------------------------------------

def vertical_scale(units: str | None) -> float:
    """Factor that turns a raster's vertical unit into metres (3DEP tiles carry no unit tag and
    are metres by specification; this guards against a product in feet)."""
    u = (units or "").strip().lower()
    if u in ("", "m", "metre", "metres", "meter", "meters"):
        return 1.0
    if u in ("ft", "foot", "feet", "international foot"):
        return 0.3048
    if u in ("us survey foot", "us survey feet", "ftus", "us-ft", "us ft"):
        return 1200.0 / 3937.0
    raise DemFetchError(f"unknown vertical unit {units!r}")


def clean_elevation(a: np.ndarray, nodata: float | None = None, scale: float = 1.0) -> np.ndarray:
    """float32 metres with NaN wherever the source has no data.

    Besides the declared nodata value this also drops anything that cannot be a terrain height
    on Earth (the usual sentinels: -999999, -32768, -9999, -3.4e38), so a missing value can never
    turn into a cliff in the slope.
    """
    z = np.array(a, dtype=np.float32)  # copy
    bad = ~np.isfinite(z)
    if nodata is not None and np.isfinite(nodata):
        bad |= z == np.float32(nodata)
    with np.errstate(invalid="ignore"):
        bad |= (z < -1000.0) | (z > 9500.0)
    if scale != 1.0:
        z *= np.float32(scale)
    z[bad] = np.nan
    return z


def _fetch_3dep_blocks(source: DemSource, k_r: int, k_c: int, blocks: Sequence[tuple[int, int]]) -> list[np.ndarray]:
    """Read cache blocks of one 3DEP tile straight from the COG on S3 (HTTP range requests)."""
    url = source.url(k_r, k_c)
    size, b = source.tile_size, source.block
    lat_s, lon_w = source.cell_corner(k_r, k_c)
    west = lon_w - source.collar_lo * source.res
    north = lat_s + 1 + source.collar_lo * source.res
    out = []
    with rasterio.Env(**GDAL_HTTP_ENV):
        with rasterio.open("/vsicurl/" + url) as src:
            t = src.transform
            if (
                src.width != size or src.height != size
                or abs(t.a - source.res) > 1e-9 or abs(t.e + source.res) > 1e-9
                or abs(t.c - west) > 0.01 * source.res or abs(t.f - north) > 0.01 * source.res
                or t.b != 0 or t.d != 0
            ):
                raise DemFetchError(
                    f"{url}: unexpected tile layout ({src.width}x{src.height}, transform {tuple(t)[:6]})"
                )
            scale = vertical_scale(src.units[0])
            for bi, bj in blocks:
                win = Window(bj * b, bi * b, min(b, size - bj * b), min(b, size - bi * b))
                out.append(clean_elevation(src.read(1, window=win), src.nodata, scale))
    return out


def _head_tile(url: str) -> dict:
    """ETag / Last-Modified / size of a tile on S3 (one HEAD request)."""
    resp = requests.head(url, timeout=15, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
    resp.raise_for_status()
    h = resp.headers
    size = h.get("Content-Length")
    return {
        "etag": (h.get("ETag") or "").strip('"') or None,
        "last_modified": h.get("Last-Modified"),
        "content_length": int(size) if size and size.isdigit() else None,
    }


def _download_srtm(url: str, dest: Path) -> Path:
    """Download one skadi .hgt.gz tile into the cache."""
    return download(url, dest, timeout=60, retries=3, quiet=True)


# ----------------------------------------------------------------------------------------------
# cache
# ----------------------------------------------------------------------------------------------

def _tile_dir(cache_dir: Path, source: DemSource, tile: str) -> Path:
    return cache_dir / source.key / tile


def _block_path(cache_dir: Path, source: DemSource, k_r: int, k_c: int, bi: int, bj: int) -> Path:
    tile = source.tile_name(k_r, k_c)
    stem = Path(source.url(k_r, k_c)).stem  # e.g. USGS_1_n38w101
    return _tile_dir(cache_dir, source, tile) / f"{stem}_b{source.block}_r{bi:02d}_c{bj:02d}.tif"


def _block_shape(source: DemSource, bi: int, bj: int) -> tuple[int, int]:
    size, b = source.tile_size, source.block
    return min(b, size - bi * b), min(b, size - bj * b)


def _write_block(path: Path, arr: np.ndarray, source: DemSource, k_r: int, k_c: int, bi: int, bj: int) -> None:
    """Atomically write one block as a georeferenced float32 GeoTIFF (metres, NaN = no data)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    r0 = k_r * source.px_per_deg - source.collar_lo + bi * source.block
    c0 = k_c * source.px_per_deg - source.collar_lo + bj * source.block
    try:
        with rasterio.open(
            tmp, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
            dtype="float32", crs=source.horizontal_crs, transform=source.transform(r0, c0),
            nodata=float("nan"), compress="deflate", predictor=3, zlevel=6,
        ) as dst:
            dst.write(arr.astype(np.float32, copy=False), 1)
            dst.update_tags(SOURCE_URL=source.url(k_r, k_c), SOURCE_BLOCK=f"{bi},{bj}",
                            SOURCE_BLOCK_SIZE=str(source.block), UNITS="metres")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_block(path: Path, shape: tuple[int, int]) -> np.ndarray | None:
    """A cached block, or None if it is missing or unreadable (it is then fetched again)."""
    if not path.exists():
        return None
    try:
        with rasterio.open(path) as src:
            a = src.read(1)
    except Exception:  # truncated / corrupt file
        return None
    if a.shape != shape:
        return None
    return a.astype(np.float32, copy=False)


def _load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.part")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _read_hgt(path: Path) -> np.ndarray:
    """A skadi/SRTM1 .hgt.gz tile as float32 metres (3601 x 3601, row 0 = north, NaN = void)."""
    with gzip.open(path, "rb") as fh:
        raw = fh.read()
    n = int(round(math.sqrt(len(raw) / 2)))
    if n * n * 2 != len(raw) or n != SOURCES["srtm"].tile_size:
        raise DemFetchError(f"{path.name}: not a 1 arc-second HGT tile ({len(raw)} bytes)")
    return clean_elevation(np.frombuffer(raw, dtype=">i2").reshape(n, n), nodata=-32768)


# ----------------------------------------------------------------------------------------------
# mosaicking
# ----------------------------------------------------------------------------------------------

def _mark_blocks(nb: int, r0, r1, c0, c1) -> np.ndarray:
    """Boolean (nb, nb) grid: True where any of the inclusive block ranges [r0..r1] x [c0..c1] lands."""
    d = np.zeros((nb + 1, nb + 1), dtype=np.int64)
    np.add.at(d, (r0, c0), 1)
    np.add.at(d, (r1 + 1, c0), -1)
    np.add.at(d, (r0, c1 + 1), -1)
    np.add.at(d, (r1 + 1, c1 + 1), 1)
    return d.cumsum(axis=0).cumsum(axis=1)[:nb, :nb] > 0


def _fetch_missing_blocks(source: DemSource, cache_dir: Path, missing: dict, plan: dict, quiet: bool) -> int:
    """Fetch and cache the blocks in `missing` ({(k_r, k_c): [(bi, bj), ...]}); returns how many.

    `plan` holds every block this run needs per tile: if a tile turns out to have been
    republished since its cached blocks were fetched, all of them are fetched again.
    """
    jobs = []
    for (k_r, k_c), blocks in missing.items():
        tile = source.tile_name(k_r, k_c)
        url = source.url(k_r, k_c)
        man_path = _tile_dir(cache_dir, source, tile) / "tile.json"
        manifest = _load_manifest(man_path)
        try:
            head = _head_tile(url)
        except Exception as exc:  # not fatal on its own: the block read below is the real test
            head = {}
            if not quiet:
                log(f"dem: could not read the version of {tile} ({type(exc).__name__}); continuing")
        if head.get("etag") and manifest.get("etag") and head["etag"] != manifest["etag"]:
            # USGS republished the tile since the cached blocks were fetched: never mix versions.
            stale = sorted(_tile_dir(cache_dir, source, tile).glob("*.tif"))
            log(f"dem: {tile} changed upstream ({manifest.get('last_modified')} -> "
                f"{head.get('last_modified')}); refetching, dropped {len(stale)} cached block(s)")
            for path in stale:
                path.unlink()
            blocks = [(int(bi), int(bj)) for bi, bj in plan[(k_r, k_c)]]
            manifest = {}
        if not manifest:
            manifest = {"url": url, "source": source.key, "block_px": source.block,
                        "first_fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        manifest.update({k: v for k, v in head.items() if v is not None})
        _save_manifest(man_path, manifest)
        # a handful of blocks per task: each task opens its own handle on the remote file
        per_task = max(1, min(6, math.ceil(len(blocks) / FETCH_THREADS)))
        for i in range(0, len(blocks), per_task):
            jobs.append((k_r, k_c, blocks[i:i + per_task]))

    done = 0
    pool = ThreadPoolExecutor(max_workers=FETCH_THREADS)
    try:
        futures = {pool.submit(_fetch_3dep_blocks, source, k_r, k_c, chunk): (k_r, k_c, chunk)
                   for k_r, k_c, chunk in jobs}
        for fut in as_completed(futures):
            k_r, k_c, chunk = futures[fut]
            try:
                arrays = fut.result()
            except Exception as exc:
                raise DemFetchError(f"{source.url(k_r, k_c)}: {type(exc).__name__}: {exc}") from exc
            if len(arrays) != len(chunk):
                raise DemFetchError(f"{source.url(k_r, k_c)}: got {len(arrays)} of {len(chunk)} blocks")
            for (bi, bj), arr in zip(chunk, arrays):
                if arr.shape != _block_shape(source, bi, bj):
                    raise DemFetchError(f"{source.url(k_r, k_c)}: block {bi},{bj} has shape {arr.shape}")
                _write_block(_block_path(cache_dir, source, k_r, k_c, bi, bj), arr, source, k_r, k_c, bi, bj)
                done += 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)  # one failure cancels whatever has not started
    return done


def _srtm_tile(source: DemSource, cache_dir: Path, k_r: int, k_c: int) -> tuple[np.ndarray, bool, Path]:
    """(elevation, was_downloaded, path) of one skadi tile, downloading it if it is not cached."""
    tile = source.tile_name(k_r, k_c)
    path = cache_dir / source.key / f"{tile}.hgt.gz"
    fetched = False
    for attempt in (1, 2):
        if not (path.exists() and path.stat().st_size > 0):
            try:
                _download_srtm(source.url(k_r, k_c), path)
            except Exception as exc:
                raise DemFetchError(f"{source.url(k_r, k_c)}: {type(exc).__name__}: {exc}") from exc
            fetched = True
        try:
            return _read_hgt(path), fetched, path
        except (OSError, EOFError, DemFetchError) as exc:  # corrupt cache entry: fetch it once more
            path.unlink(missing_ok=True)
            if attempt == 2:
                raise DemFetchError(f"{source.url(k_r, k_c)}: unreadable tile ({exc})") from exc
    raise AssertionError("unreachable")


def read_mosaic(source: DemSource, bounds, *, cache_dir: Path = DEM_DIR, buffer_px: int = 3,
                quiet: bool = False) -> DemMosaic:
    """Elevation mosaic covering every lon/lat box in `bounds` ((N, 4): minx, miny, maxx, maxy),
    each grown by `buffer_px` pixels. Only tiles/blocks that a box touches are fetched; the rest
    of the mosaic stays NaN. Raises DemFetchError if anything needed cannot be obtained."""
    cache_dir = Path(cache_dir)
    r_lo, r_hi, c_lo, c_hi = source.windows(bounds, buffer_px)
    if len(r_lo) == 0:
        raise ValueError("no bounds given")
    r0, r1, c0, c1 = int(r_lo.min()), int(r_hi.max()), int(c_lo.min()), int(c_hi.max())
    h, w = r1 - r0, c1 - c0
    if h * w > MAX_MOSAIC_PIXELS:
        raise DemTooLarge(
            f"the fields span {h} x {w} pixels of {source.key} ({h * w / 1e6:.0f} M > "
            f"{MAX_MOSAIC_PIXELS / 1e6:.0f} M): split the fields into regions or use a coarser source"
        )
    p, lo = source.px_per_deg, source.collar_lo

    # --- which tiles (and blocks) do the boxes touch? ---
    plan: dict[tuple[int, int], np.ndarray | None] = {}
    for k_r in range(r0 // p, (r1 - 1) // p + 1):
        for k_c in range(c0 // p, (c1 - 1) // p + 1):
            a0, a1 = np.maximum(r_lo, k_r * p), np.minimum(r_hi, (k_r + 1) * p)
            b0, b1 = np.maximum(c_lo, k_c * p), np.minimum(c_hi, (k_c + 1) * p)
            m = (a1 > a0) & (b1 > b0)
            if not m.any():
                continue
            if not source.block:
                plan[(k_r, k_c)] = None
                continue
            bs = source.block
            nb = math.ceil(source.tile_size / bs)
            need = _mark_blocks(
                nb,
                (a0[m] - k_r * p + lo) // bs, (a1[m] - 1 - k_r * p + lo) // bs,
                (b0[m] - k_c * p + lo) // bs, (b1[m] - 1 - k_c * p + lo) // bs,
            )
            plan[(k_r, k_c)] = np.argwhere(need)

    mosaic = DemMosaic(z=np.full((h, w), np.nan, dtype=np.float32), transform=source.transform(r0, c0), source=source)

    def paste(arr: np.ndarray, gr0: int, gc0: int, k_r: int, k_c: int) -> None:
        """Copy the part of `arr` (top-left at global gr0, gc0) that lies in cell (k_r, k_c) and the window."""
        ra, rb = max(gr0, k_r * p, r0), min(gr0 + arr.shape[0], (k_r + 1) * p, r1)
        ca, cb = max(gc0, k_c * p, c0), min(gc0 + arr.shape[1], (k_c + 1) * p, c1)
        if rb > ra and cb > ca:
            mosaic.z[ra - r0:rb - r0, ca - c0:cb - c0] = arr[ra - gr0:rb - gr0, ca - gc0:cb - gc0]

    if source.block:
        plan_blocks = {cell: [(int(bi), int(bj)) for bi, bj in blocks] for cell, blocks in plan.items()}
        missing = {}
        for (k_r, k_c), blocks in plan_blocks.items():
            miss = [b for b in blocks if not _block_path(cache_dir, source, k_r, k_c, *b).exists()]
            if miss:
                missing[(k_r, k_c)] = miss
        n_total = sum(len(b) for b in plan_blocks.values())
        n_missing = sum(len(b) for b in missing.values())
        if n_missing:
            if not quiet:
                log(f"dem: {source.key}: {n_total} block(s) needed in {len(plan_blocks)} tile(s), fetching "
                    f"{n_missing} ({source.block}x{source.block} px windows over HTTP)")
            mosaic.blocks_fetched = _fetch_missing_blocks(source, cache_dir, missing, plan_blocks, quiet)
        elif not quiet:
            log(f"dem: {source.key}: {n_total} block(s) in {len(plan_blocks)} tile(s), all cached")
        for (k_r, k_c), blocks in plan_blocks.items():
            tile = source.tile_name(k_r, k_c)
            for bi, bj in blocks:
                path = _block_path(cache_dir, source, k_r, k_c, bi, bj)
                arr = _read_block(path, _block_shape(source, bi, bj))
                if arr is None:  # corrupt cache entry: fetch that block once more
                    path.unlink(missing_ok=True)
                    mosaic.blocks_fetched += _fetch_missing_blocks(
                        source, cache_dir, {(k_r, k_c): [(bi, bj)]}, plan_blocks, quiet)
                    arr = _read_block(path, _block_shape(source, bi, bj))
                    if arr is None:
                        raise DemFetchError(f"{path}: block unreadable after fetching")
                mosaic.bytes_cached += path.stat().st_size
                paste(arr, k_r * p - lo + bi * source.block, k_c * p - lo + bj * source.block, k_r, k_c)
            manifest = _load_manifest(_tile_dir(cache_dir, source, tile) / "tile.json")
            mosaic.blocks_used += len(blocks)
            mosaic.tiles.append({
                "tile": tile, "url": source.url(k_r, k_c), "blocks": len(blocks),
                "last_modified": manifest.get("last_modified"), "etag": manifest.get("etag"),
                "content_length": manifest.get("content_length"),
            })
    else:
        n_fetched = 0
        for (k_r, k_c) in plan:
            arr, fetched, path = _srtm_tile(source, cache_dir, k_r, k_c)
            n_fetched += fetched
            mosaic.bytes_cached += path.stat().st_size
            paste(arr, k_r * p - lo, k_c * p - lo, k_r, k_c)
            mosaic.tiles.append({"tile": source.tile_name(k_r, k_c), "url": source.url(k_r, k_c),
                                 "blocks": 1, "content_length": path.stat().st_size})
        mosaic.blocks_used = len(plan)
        mosaic.blocks_fetched = n_fetched
        if not quiet:
            log(f"dem: {source.key}: {len(plan)} tile(s), {n_fetched} downloaded, {len(plan) - n_fetched} cached")
    return mosaic


def load_dem(bounds, sources: Sequence[str] = DEFAULT_SOURCES, *, cache_dir: Path = DEM_DIR,
             buffer_px: int = 3, quiet: bool = False) -> tuple[DemMosaic, list[dict]]:
    """Try each source in order; return (mosaic, attempts).

    `attempts` has one entry per source tried: {"source", "ok", "error"}. A source is used only
    if it can supply *everything* the boxes need — sources are never mixed within one run, so
    all fields are measured on the same DEM. Raises DemUnavailable if every source fails.
    """
    attempts: list[dict] = []
    for key in sources:
        if key not in SOURCES:
            raise ValueError(f"unknown DEM source {key!r}; choose from {', '.join(SOURCES)}")
        try:
            mosaic = read_mosaic(SOURCES[key], bounds, cache_dir=cache_dir, buffer_px=buffer_px, quiet=quiet)
        except DemFetchError as exc:
            attempts.append({"source": key, "ok": False, "error": str(exc)})
            if not quiet:
                log(f"dem: {key} unavailable ({exc}); trying the next source")
            continue
        attempts.append({"source": key, "ok": True, "error": None})
        return mosaic, attempts
    raise DemUnavailable(attempts)
