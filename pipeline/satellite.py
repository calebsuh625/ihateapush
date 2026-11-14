"""Yearly Landsat numbers per field, via Google Earth Engine -> data/field_satellite.parquet.

    python pipeline/satellite.py                     # all fields, 2008-2024 (resumes from the cache)
    python pipeline/satellite.py --max-fields 50 --years 2015-2016 --out /tmp/test.parquet

The output contract (columns, GREEN_NDVI, SEASON) lives in pipeline/satellite_schema.py.

Method
------
* Data: Landsat Collection 2 Level 2 surface reflectance, Tier 1 (30 m): Landsat 5 TM (to 2012),
  Landsat 7 ETM+ (to 2019), Landsat 8 OLI (2013 on), Landsat 9 OLI-2 (late 2021 on).
* Landsat 7 is used with its scan-line gaps (they are just missing pixels, so a pixel gets fewer
  views) but dropped from 2020: its orbit was no longer maintained after 2017 and, measured over
  Finney County, its June-July overpass was 19 min earlier in 2020 than in 2008 (45 min in 2021,
  2 h in 2023; sun elevation 64 deg -> 61 -> 56 -> 41), outside the mission's +/-15 min window.
  2008-2019 it stayed within 13 min.
* Per image: Collection 2 scale/offset (x 0.0000275 - 0.2); drop fill, dilated cloud, cirrus,
  cloud, cloud shadow and snow (QA_PIXEL bits 0-5), saturated red/NIR and dropped/occluded pixels
  (QA_RADSAT), and reflectances outside (0, 1]. NDVI = (NIR - red) / (NIR + red) with
  SR_B4/SR_B3 for TM/ETM+ and SR_B5/SR_B4 for OLI.
* Harmonisation: OLI red and NIR are converted to ETM+-equivalent reflectance before the NDVI
  (HARMONISE_OLI_SR: Roy et al. 2016), so the 2012 -> 2013 sensor change is not a step.
* Images taken on the same day (adjacent rows of one overpass) are mosaicked first, so a "view" is
  one overpass, not one scene.
* Per pixel and year: ndvi_peak = 90th percentile of NDVI over the SEASON window (Earth Engine's
  percentile is numpy's "averaged_inverted_cdf", no interpolation); cover_frac = share of the
  calendar year's clear views with NDVI >= GREEN_NDVI; n_obs = number of clear views in the
  calendar year. Then the mean over each field's pixels on the Landsat grid (UTM 14N, 30 m):
  a pixel belongs to the field that contains its centre, so the smallest 5 ha field averages ~55
  pixels; a field too thin to contain any pixel centre falls back to an area-weighted mean.

Earth Engine is called interactively (no Export tasks): fields are sent in spatially compact chunks,
one (chunk, year) request each, a few at a time. Every finished (chunk, year) is cached under
data/raw/gee/, so an interrupted run resumes and a rerun makes no Earth Engine request at all.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/satellite.py`
    sys.path.insert(0, str(ROOT))

from pipeline.common import RAW_DIR, YEARS, log  # noqa: E402
from pipeline.satellite_schema import (  # noqa: E402
    COLUMNS,
    GREEN_NDVI,
    SATELLITE_PATH,
    SEASON,
    SOURCE,
    validate_satellite,
)
from pipeline.schema import REAL_PATH, SchemaError, load_fields  # noqa: E402

CACHE_DIR = RAW_DIR / "gee"

# Bump when the computation changes in a way the constants below do not capture: it is part of
# every cache key, so old cached results are ignored (not deleted).
METHOD_VERSION = "1"

PEAK_PERCENTILE = 90
SR_SCALE, SR_OFFSET = 2.75e-05, -0.2  # Collection 2 Level 2 surface reflectance

# (collection, red band, NIR band, QA_RADSAT bits to reject, is OLI, first year, last year)
# QA_RADSAT: bit n-1 = band n saturated; TM/ETM+ bit 9 = dropped pixel; OLI bit 11 = terrain occlusion.
_TM_RADSAT = (1 << 2) | (1 << 3) | (1 << 9)
_OLI_RADSAT = (1 << 3) | (1 << 4) | (1 << 11)
SENSORS = [
    ("LANDSAT/LT05/C02/T1_L2", "SR_B3", "SR_B4", _TM_RADSAT, False, 1984, 2012),
    ("LANDSAT/LE07/C02/T1_L2", "SR_B3", "SR_B4", _TM_RADSAT, False, 1999, 2019),
    ("LANDSAT/LC08/C02/T1_L2", "SR_B4", "SR_B5", _OLI_RADSAT, True, 2013, 9999),
    ("LANDSAT/LC09/C02/T1_L2", "SR_B4", "SR_B5", _OLI_RADSAT, True, 2021, 9999),
]
QA_PIXEL_REJECT = 0b111111  # fill, dilated cloud, cirrus, cloud, cloud shadow, snow

# OLI surface reflectance -> ETM+-equivalent: ETM+ = intercept + slope * OLI, per band.
# Roy et al. 2016 (Remote Sens. Environ. 185:57-70), Table 2, surface reflectance, OLS "ETM+ = f(OLI)".
# It lowers OLI NDVI by ~0.05 at NDVI 0.8 and ~0.01-0.02 below 0.4.
# Why this set: on 300 Finney fields, Landsat 8 alone vs Landsat 7 alone (2014, 2016, 2018), the
# unharmonised OLI ndvi_peak was 0.055 higher on average (corn +0.04, fallow +0.06); with these
# coefficients the mean difference was +0.001 (2014, 2018). The same paper's NDVI-level fits
# remove only 0.01-0.03. It is not perfect at the ends: low-NDVI fields (fallow) stay ~0.03-0.04
# higher from 2013 on, and the greenest fields read ~0.02-0.03 lower once Landsat 7 is gone (2020 on).
# TM is treated as ETM+ (Landsat 5 alone vs 7 alone, 2008-2011: -0.01 on corn, 0.00 on fallow).
HARMONISE_OLI_SR = {"red": (0.0123, 0.9372), "nir": (0.0448, 0.8339)}  # band: (intercept, slope)

# Landsat's own grid here (UTM 14N, 30 m, pixel edges on x15 m), so UTM 14N scenes are read pixel
# for pixel; path 31 scenes delivered in UTM 13N are resampled nearest-neighbour by Earth Engine.
GRID_CRS = "EPSG:32614"
GRID_TRANSFORM = [30, 0, 15, 0, -30, 15]

CHUNK_SIZE = 300  # fields per request: 7-27 s and ~110 EECU-seconds each, far inside the 5 min limit
WORKERS = 4  # concurrent requests (the free tier allows few concurrent aggregations)
COORD_DECIMALS = 7  # ~1 cm; keeps the request small
MAX_TILE_SCALE = 8
REQUEST_DEADLINE_MS = 6 * 60 * 1000  # interactive computations are cut off server-side at 5 min
MS_PER_DAY = 86_400_000

BANDS = ["ndvi_peak", "cover_frac", "n_obs"]


def method_signature() -> dict:
    """Everything that defines the numbers; hashed into every cache key."""
    return {
        "version": METHOD_VERSION,
        "green_ndvi": GREEN_NDVI,
        "season": list(SEASON),
        "percentile": PEAK_PERCENTILE,
        "sensors": [list(s) for s in SENSORS],
        "qa_pixel_reject": QA_PIXEL_REJECT,
        "harmonise_oli_sr": {band: list(v) for band, v in sorted(HARMONISE_OLI_SR.items())},
        "grid": [GRID_CRS, GRID_TRANSFORM],
    }


class SatelliteError(RuntimeError):
    """A problem the user can act on; printed without a traceback."""


class EarthEngineUnavailable(SatelliteError):
    pass


# --------------------------------------------------------------------------- chunks and cache


@dataclass(frozen=True)
class Chunk:
    field_ids: tuple[str, ...]
    geometries: tuple[dict, ...]  # GeoJSON Polygon dicts, exactly as sent to Earth Engine
    digest: str  # hash of ids + geometries

    def __len__(self) -> int:
        return len(self.field_ids)

    def split(self) -> tuple["Chunk", "Chunk"]:
        mid = len(self) // 2
        return (
            make_chunk(self.field_ids[:mid], self.geometries[:mid]),
            make_chunk(self.field_ids[mid:], self.geometries[mid:]),
        )


def _round_coords(obj):
    if isinstance(obj, (list, tuple)):
        return [_round_coords(v) for v in obj]
    return round(float(obj), COORD_DECIMALS)


def geometry_to_geojson(geom) -> dict:
    """Shapely Polygon -> GeoJSON dict with rounded lon/lat."""
    mapping = geom.__geo_interface__
    return {"type": mapping["type"], "coordinates": _round_coords(mapping["coordinates"])}


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def make_chunk(field_ids, geometries) -> Chunk:
    field_ids, geometries = tuple(field_ids), tuple(geometries)
    digest = hashlib.sha256(_canonical([list(field_ids), list(geometries)])).hexdigest()
    return Chunk(field_ids, geometries, digest)


def spatial_order(fields) -> np.ndarray:
    """Row positions of `fields` along a Hilbert curve (ties by field_id): neighbours stay together."""
    return (
        pd.DataFrame(
            {
                "field_id": fields["field_id"].to_numpy(),
                "h": fields.geometry.hilbert_distance(total_bounds=fields.total_bounds).to_numpy(),
                "pos": np.arange(len(fields)),
            }
        )
        .sort_values(["h", "field_id"])["pos"]
        .to_numpy()
    )


def make_chunks(fields, chunk_size: int = CHUNK_SIZE) -> list[Chunk]:
    """Split the fields into spatially compact chunks of at most `chunk_size`.

    Fields are ordered along a Hilbert curve (ties by field_id) so that each request only touches
    a small part of the county. Deterministic for a given set of fields.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    order = spatial_order(fields)
    ids = fields["field_id"].to_numpy()[order]
    geoms = [geometry_to_geojson(g) for g in fields.geometry.to_numpy()[order]]
    return [
        make_chunk(ids[i : i + chunk_size].tolist(), geoms[i : i + chunk_size])
        for i in range(0, len(ids), chunk_size)
    ]


def cache_key(chunk: Chunk, year: int) -> str:
    """Identifies one (chunk, year) result: field ids + geometry + year + the method."""
    payload = {"method": method_signature(), "year": int(year), "chunk": chunk.digest}
    return hashlib.sha256(_canonical(payload)).hexdigest()[:32]


def cache_path(cache_dir: Path, chunk: Chunk, year: int) -> Path:
    return Path(cache_dir) / f"{int(year)}_{cache_key(chunk, year)}.json"


def read_cache(cache_dir: Path, chunk: Chunk, year: int) -> dict[str, list] | None:
    """The cached {field_id: [ndvi_peak, cover_frac, n_obs]} or None if absent/unusable."""
    path = cache_path(cache_dir, chunk, year)
    try:
        values = json.loads(path.read_text())["values"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(values, dict) or any(fid not in values for fid in chunk.field_ids):
        return None
    return {fid: values[fid] for fid in chunk.field_ids}


def write_cache(cache_dir: Path, chunk: Chunk, year: int, values: dict[str, list]) -> None:
    path = cache_path(cache_dir, chunk, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    tmp.write_text(
        json.dumps(
            {
                "year": int(year),
                "method_version": METHOD_VERSION,
                "columns": BANDS,
                "values": {fid: values[fid] for fid in chunk.field_ids},
            }
        )
    )
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Earth Engine layer


def chunk_bounds(chunk: Chunk) -> list[float]:
    """[west, south, east, north] of a chunk's fields (lon/lat), padded by ~100 m."""
    xs = [x for g in chunk.geometries for ring in g["coordinates"] for x, _ in ring]
    ys = [y for g in chunk.geometries for ring in g["coordinates"] for _, y in ring]
    pad = 0.001
    return [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]


def season_window(year: int) -> tuple[str, str]:
    """[start, end) dates of the ndvi_peak window for `year` (end is exclusive)."""
    end = date.fromisoformat(f"{year}-{SEASON[1]}") + timedelta(days=1)
    return f"{year}-{SEASON[0]}", end.isoformat()


class EarthEngine:
    """The only code that talks to Earth Engine. `reduce(chunk, year)` is one request."""

    def __init__(self) -> None:
        try:
            import ee
        except ImportError as exc:
            raise EarthEngineUnavailable(
                "the Earth Engine client is not installed: run  .venv/bin/python -m pip install earthengine-api"
            ) from exc
        try:
            ee.Initialize(project=os.environ.get("EE_PROJECT") or None)
        except Exception as exc:  # no credentials, no project, project not registered, no network
            reason = " ".join(str(exc).split())[:300]
            raise EarthEngineUnavailable(
                "could not initialise Google Earth Engine.\n"
                "  1. run  .venv/bin/earthengine authenticate\n"
                "  2. set a Cloud project registered for Earth Engine:  .venv/bin/earthengine set_project <project-id>"
                "  (or export EE_PROJECT=<project-id>)\n"
                f"  Earth Engine said: {reason}"
            ) from exc
        ee.data.setDeadline(REQUEST_DEADLINE_MS)
        self.ee = ee

    # -- image -------------------------------------------------------------------------------

    def _views(self, year: int, region):
        """One image per satellite overpass in the calendar year: clear-sky NDVI (ETM+-equivalent), band 'ndvi'."""
        ee = self.ee
        start, end = f"{year}-01-01", f"{year + 1}-01-01"

        def sensor_views(collection, red_band, nir_band, radsat_reject, is_oli):
            scenes = ee.ImageCollection(collection).filterBounds(region).filterDate(start, end)

            def ndvi(img):
                sr = img.select([nir_band, red_band]).multiply(SR_SCALE).add(SR_OFFSET)
                sr = sr.updateMask(sr.gt(0).And(sr.lte(1)))  # either band out of range masks the NDVI
                if is_oli:
                    (nir_icpt, nir_slope), (red_icpt, red_slope) = HARMONISE_OLI_SR["nir"], HARMONISE_OLI_SR["red"]
                    sr = sr.multiply(ee.Image.constant([nir_slope, red_slope])).add(
                        ee.Image.constant([nir_icpt, red_icpt])
                    )
                out = sr.normalizedDifference()
                clear = (
                    img.select("QA_PIXEL").bitwiseAnd(QA_PIXEL_REJECT).eq(0)
                    .And(img.select("QA_RADSAT").bitwiseAnd(radsat_reject).eq(0))
                )
                return out.updateMask(clear).rename("ndvi").toFloat()

            def one_day(day):  # scenes of the same day are one overpass: mosaic them into one view
                day_start = ee.Date(ee.Number(day).multiply(MS_PER_DAY))
                return (
                    scenes.filterDate(day_start, day_start.advance(1, "day")).map(ndvi).mosaic()
                    .set("system:time_start", day_start.millis())
                )

            days = (
                ee.List(scenes.aggregate_array("system:time_start"))
                .map(lambda t: ee.Number(t).divide(MS_PER_DAY).floor())
                .distinct()
            )
            return ee.ImageCollection.fromImages(days.map(one_day))

        # A never-clear placeholder keeps the bands present when a year has no image at all.
        views = ee.ImageCollection(
            [
                ee.Image.constant(0).toFloat().rename("ndvi").updateMask(ee.Image.constant(0))
                .set("system:time_start", ee.Date(f"{year}-07-01").millis())
            ]
        )
        for collection, red_band, nir_band, radsat_reject, is_oli, first, last in SENSORS:
            if first <= year <= last:
                views = views.merge(sensor_views(collection, red_band, nir_band, radsat_reject, is_oli))
        return views

    def year_image(self, year: int, region):
        """Per-pixel image with bands ndvi_peak, cover_frac (masked where never clear) and n_obs (never masked)."""
        ee = self.ee
        season_start, season_end = (ee.Date(d).millis() for d in season_window(year))

        def bands(view):
            t = ee.Number(view.get("system:time_start"))
            in_season = ee.Image.constant(t.gte(season_start).And(t.lt(season_end)))
            return view.updateMask(in_season).rename("season").addBands(view.gte(GREEN_NDVI).rename("green")).toFloat()

        # One pass over the year's views: percentile of the in-season NDVI, mean and count of "green".
        reducer = ee.Reducer.percentile([PEAK_PERCENTILE]).combine(
            ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True), sharedInputs=False
        )
        out = self._views(year, region).map(bands).reduce(reducer)
        return (
            out.select([f"p{PEAK_PERCENTILE}", "mean"], ["ndvi_peak", "cover_frac"])
            .addBands(out.select("count").unmask(0).toFloat().rename("n_obs"))
        )

    # -- request -----------------------------------------------------------------------------

    def reduce(self, chunk: Chunk, year: int, tile_scale: int = 1) -> dict[str, list]:
        """One Earth Engine request: {field_id: [ndvi_peak, cover_frac, n_obs]} (None = no data).

        The fields are painted onto the Landsat grid (a pixel belongs to the field that contains its
        centre) and all of them are averaged in one grouped reduction over the chunk's bounding box,
        which costs ~20x less than reduceRegions. A field too thin to contain any pixel centre gets
        a second, area-weighted request, so every field has a value.
        """
        ee = self.ee
        region = ee.Geometry.Rectangle(chunk_bounds(chunk), None, False)
        image = self.year_image(year, region)
        features = ee.FeatureCollection(
            [
                # idx (position in the chunk) is the number painted into the raster; field_id is the only data sent
                ee.Feature(ee.Geometry(geometry), {"field_id": field_id, "idx": i})
                for i, (field_id, geometry) in enumerate(zip(chunk.field_ids, chunk.geometries))
            ]
        )
        peak, cover = image.select("ndvi_peak"), image.select("cover_frac")
        stack = (
            peak.unmask(0).addBands(peak.mask()).addBands(cover.unmask(0)).addBands(cover.mask())
            .addBands(image.select("n_obs")).addBands(ee.Image.constant(1)).toFloat()
            .addBands(ee.Image().toInt().paint(features, "idx"))
        )
        grouped = stack.reduceRegion(
            reducer=ee.Reducer.sum().repeat(6).group(groupField=6, groupName="idx"),
            geometry=region,
            crs=GRID_CRS,
            crsTransform=GRID_TRANSFORM,
            maxPixels=1e9,
            tileScale=tile_scale,
        ).getInfo()
        out = {
            chunk.field_ids[int(group["idx"])]: sums_to_means(group["sum"]) for group in grouped.get("groups", [])
        }
        no_pixel = [i for i, field_id in enumerate(chunk.field_ids) if field_id not in out]
        if no_pixel:
            thin = ee.FeatureCollection(
                [ee.Feature(ee.Geometry(chunk.geometries[i]), {"field_id": chunk.field_ids[i]}) for i in no_pixel]
            )
            info = (
                image.reduceRegions(
                    collection=thin, reducer=ee.Reducer.mean(), crs=GRID_CRS, crsTransform=GRID_TRANSFORM,
                    tileScale=tile_scale,
                )
                .select(["field_id", *BANDS], None, False)
                .getInfo()
            )
            for feature in info["features"]:
                props = feature.get("properties") or {}
                out[props["field_id"]] = [props.get(band) for band in BANDS]
        return out


def sums_to_means(sums) -> list:
    """Per-field sums [peak, peak pixels, cover, cover pixels, n_obs, pixels] -> [ndvi_peak, cover_frac, n_obs]."""
    peak_sum, peak_n, cover_sum, cover_n, obs_sum, pixels = sums
    return [
        peak_sum / peak_n if peak_n else None,
        cover_sum / cover_n if cover_n else None,
        obs_sum / pixels if pixels else None,
    ]


# --------------------------------------------------------------------------- retries

_BUSY = ("429", "too many concurrent aggregations", "too many requests", "quota", "rate limit")
_TOO_BIG = (
    "computation timed out",
    "user memory limit exceeded",
    "memory capacity exceeded",
    "response size exceeds",
    "payload size exceeds",
    "request payload size",
    "too many pixels",
)
_TRANSIENT = (
    "internal error",
    "backend error",
    "bad gateway",
    "unavailable",
    "deadline exceeded",
    "timed out",
    "timeout",
    "connection",
    "remote end closed",
    "broken pipe",
    "reset by peer",
    "temporarily",
    "try again",
)


def classify_error(exc: BaseException) -> str:
    """'too_big' (retry smaller), 'busy' (wait, retry), 'transient' (retry) or 'fatal'."""
    text = str(exc).lower()
    if any(s in text for s in _TOO_BIG):
        return "too_big"
    if any(s in text for s in _BUSY):
        return "busy"
    if isinstance(exc, (ConnectionError, TimeoutError)) or any(s in text for s in _TRANSIENT):
        return "transient"
    return "fatal"


class Stats:
    """Thread-safe counters for the run summary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0  # Earth Engine requests sent (including failed ones)
        self.retries = 0
        self.splits = 0
        self.cache_hits = 0
        self.computed = 0  # (chunk, year) results obtained from Earth Engine

    def add(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)


def fetch_chunk_year(
    compute,
    chunk: Chunk,
    year: int,
    cache_dir: Path,
    *,
    force: bool = False,
    stats: Stats | None = None,
    sleep=time.sleep,
    max_attempts: int = 7,
    tile_scale: int = 1,
) -> dict[str, list]:
    """Result for one (chunk, year): from the cache, else from `compute(chunk, year, tile_scale)`.

    Quota / concurrency / network errors are retried with exponential backoff. Time-outs and
    memory errors are retried smaller: the chunk is halved (each half cached on its own), and a
    single field that is still too big is retried with a larger tileScale. The combined result
    is cached under the original chunk's key, so a rerun needs no request.
    """
    stats = stats or Stats()
    if not force:
        cached = read_cache(cache_dir, chunk, year)
        if cached is not None:
            stats.add("cache_hits")
            return cached

    attempt = 0
    while True:
        attempt += 1
        stats.add("requests")
        try:
            values = compute(chunk, year, tile_scale)
            break
        except Exception as exc:  # noqa: BLE001 - classified below, anything unknown is re-raised
            kind = classify_error(exc)
            reason = " ".join(str(exc).split())[:160]
            if kind == "too_big":
                if len(chunk) > 1:
                    log(f"  {year} chunk of {len(chunk)} fields: {reason}; retrying as two halves")
                    stats.add("splits")
                    values = {}
                    for half in chunk.split():
                        values.update(
                            fetch_chunk_year(
                                compute, half, year, cache_dir, force=force, stats=stats, sleep=sleep,
                                max_attempts=max_attempts, tile_scale=tile_scale,
                            )
                        )
                    break
                if tile_scale < MAX_TILE_SCALE:
                    tile_scale *= 2
                    attempt = 0
                    log(f"  {year} field {chunk.field_ids[0]}: {reason}; retrying with tileScale={tile_scale}")
                    stats.add("retries")
                    continue
                raise SatelliteError(
                    f"Earth Engine could not compute field {chunk.field_ids[0]} for {year}: {reason}"
                ) from exc
            if kind == "fatal":
                raise SatelliteError(f"Earth Engine request failed ({year}, {len(chunk)} fields): {reason}") from exc
            if attempt >= max_attempts:
                raise SatelliteError(
                    f"Earth Engine request still failing after {attempt} attempts ({year}, {len(chunk)} fields): "
                    f"{reason}\nFinished chunks are cached: run the same command again later to resume."
                ) from exc
            base = 15 if kind == "busy" else 4
            delay = min(240.0, base * 2 ** (attempt - 1)) * (0.75 + 0.5 * random.random())
            log(f"  {year} chunk of {len(chunk)} fields: {reason}; attempt {attempt}/{max_attempts}, waiting {delay:.0f}s")
            stats.add("retries")
            sleep(delay)

    missing = [fid for fid in chunk.field_ids if fid not in values]
    if missing:  # Earth Engine returns every feature it was sent; anything else is a bug upstream
        raise SatelliteError(f"Earth Engine returned no row for {len(missing)} field(s), e.g. {missing[0]} ({year})")
    values = {fid: _clean(values[fid]) for fid in chunk.field_ids}
    write_cache(cache_dir, chunk, year, values)
    stats.add("computed")
    return values


def _clean(triple) -> list:
    """[ndvi_peak, cover_frac, n_obs] with None for missing and plain floats otherwise."""
    out = []
    for v in list(triple)[:3]:
        out.append(None if v is None or v != v else float(v))
    return out + [None] * (3 - len(out))


# --------------------------------------------------------------------------- orchestration


def collect(
    chunks: list[Chunk],
    years: list[int],
    cache_dir: Path,
    *,
    compute=None,
    force: bool = False,
    workers: int = WORKERS,
    stats: Stats | None = None,
    sleep=time.sleep,
) -> dict[tuple[str, int], list]:
    """{(field_id, year): [ndvi_peak, cover_frac, n_obs]} for every chunk and year.

    `compute(chunk, year, tile_scale)` is one Earth Engine request; by default it is
    `EarthEngine().reduce`, and Earth Engine is only initialised when something is not cached.
    """
    stats = stats or Stats()
    results: dict[tuple[str, int], list] = {}
    pending: list[tuple[Chunk, int]] = []
    for year in years:
        for chunk in chunks:
            cached = None if force else read_cache(cache_dir, chunk, year)
            if cached is None:
                pending.append((chunk, year))
            else:
                stats.add("cache_hits")
                for fid, triple in cached.items():
                    results[(fid, year)] = triple
    total = len(chunks) * len(years)
    log(f"{total} (chunk, year) results needed: {total - len(pending)} cached, {len(pending)} to request")
    if not pending:
        return results

    if compute is None:
        compute = EarthEngine().reduce
        log("Earth Engine initialised")

    started = time.monotonic()
    done = 0
    step = max(1, len(pending) // 40)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                fetch_chunk_year, compute, chunk, year, cache_dir, force=force, stats=stats, sleep=sleep
            ): (chunk, year)
            for chunk, year in pending
        }
        try:
            not_done = set(futures)
            while not_done:
                finished, not_done = wait(not_done, return_when=FIRST_COMPLETED)
                for future in finished:
                    chunk, year = futures[future]
                    for fid, triple in future.result().items():  # raises the first failure
                        results[(fid, year)] = triple
                    done += 1
                    if done % step == 0 or done == len(pending):
                        elapsed = time.monotonic() - started
                        eta = elapsed / done * (len(pending) - done)
                        log(
                            f"  {done}/{len(pending)} requests done (last: {year}, {len(chunk)} fields); "
                            f"{elapsed / 60:.1f} min elapsed, ~{eta / 60:.1f} min left"
                        )
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return results


def assemble(field_ids, years, results: dict[tuple[str, int], list]) -> pd.DataFrame:
    """The long table: one row per (field_id, year), sorted, missing results as null / n_obs 0."""
    field_ids = sorted(str(f) for f in field_ids)
    years = sorted(int(y) for y in years)
    n = len(field_ids) * len(years)
    peak = np.full(n, np.nan)
    cover = np.full(n, np.nan)
    n_obs = np.zeros(n)
    i = 0
    for fid in field_ids:
        for year in years:
            triple = results.get((fid, year))
            if triple is not None:
                p, c, k = _clean(triple)
                if k is not None and k > 0:
                    n_obs[i] = k
                    if p is not None:
                        peak[i] = p
                    if c is not None:
                        cover[i] = c
            i += 1
    df = pd.DataFrame(
        {
            "field_id": pd.Series(np.repeat(np.array(field_ids, dtype=object), len(years)), dtype=object),
            "year": np.tile(np.array(years, dtype="int64"), len(field_ids)),
            "ndvi_peak": np.round(np.clip(peak, -1.0, 1.0), 3),
            "cover_frac": np.round(np.clip(cover, 0.0, 1.0), 3),
            "n_obs": np.round(n_obs, 3),
        }
    )
    return df[COLUMNS]


def write_atomic(df: pd.DataFrame, out: Path) -> None:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, out)


def summarize(df: pd.DataFrame) -> str:
    lines = [
        f"rows: {len(df):,}  ({df['field_id'].nunique():,} fields x {df['year'].nunique()} years)",
        f"source: {SOURCE}",
        f"null ndvi_peak: {int(df['ndvi_peak'].isna().sum()):,}   null cover_frac: {int(df['cover_frac'].isna().sum()):,}"
        f"   n_obs == 0: {int((df['n_obs'] == 0).sum()):,}",
        "year   ndvi_peak  cover_frac   n_obs   null_peak   (means over fields)",
    ]
    by_year = df.groupby("year")
    table = by_year[["ndvi_peak", "cover_frac", "n_obs"]].mean()
    nulls = by_year["ndvi_peak"].apply(lambda s: int(s.isna().sum()))
    for year, row in table.iterrows():
        lines.append(
            f"{year}   {row['ndvi_peak']:9.3f}  {row['cover_frac']:10.3f}  {row['n_obs']:6.1f}  {nulls[year]:10,}"
        )
    return "\n".join(lines)


def build(
    fields_path: Path,
    out_path: Path,
    years: list[int],
    *,
    max_fields: int | None = None,
    force: bool = False,
    cache_dir: Path | None = None,
    compute=None,
    chunk_size: int = CHUNK_SIZE,
    workers: int = WORKERS,
) -> pd.DataFrame:
    """Read fields, get every (field, year) from cache or Earth Engine, validate and write."""
    started = time.monotonic()
    fields = load_fields(fields_path)
    fields = fields.sort_values("field_id").reset_index(drop=True)
    if max_fields is not None:  # a compact block of neighbours, so a test run is one small request per year
        fields = fields.iloc[np.sort(spatial_order(fields)[:max_fields])].reset_index(drop=True)
    log(f"{len(fields):,} fields from {fields_path}; years {years[0]}-{years[-1]} ({len(years)})")
    chunks = make_chunks(fields, chunk_size)
    stats = Stats()
    results = collect(
        chunks, years, cache_dir or CACHE_DIR, compute=compute, force=force, workers=workers, stats=stats
    )
    df = assemble(fields["field_id"], years, results)
    validate_satellite(df, "pipeline/satellite.py output")
    write_atomic(df, out_path)
    log(f"wrote {out_path}")
    print(summarize(df))
    print(
        f"Earth Engine requests: {stats.requests} ({stats.retries} retries, {stats.splits} chunk splits); "
        f"cached results used: {stats.cache_hits}; took {time.monotonic() - started:.0f} s"
    )
    return df


# --------------------------------------------------------------------------- CLI


def parse_years(text: str) -> list[int]:
    """'2008-2024', '2015' or '2012,2013' -> sorted list of years."""
    years: set[int] = set()
    try:
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                first, last = (int(p) for p in part.split("-", 1))
                if first > last:
                    raise ValueError
                years.update(range(first, last + 1))
            else:
                years.add(int(part))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a year range like 2008-2024") from None
    if not years or min(years) < 1984 or max(years) > date.today().year:
        raise argparse.ArgumentTypeError(f"{text!r}: years must be between 1984 and {date.today().year}")
    return sorted(years)


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fields", type=Path, default=REAL_PATH, help="fields GeoParquet (default: data/fields.parquet)")
    parser.add_argument("--out", type=Path, default=SATELLITE_PATH, help="output (default: data/field_satellite.parquet)")
    parser.add_argument("--years", type=parse_years, default=list(YEARS), help="e.g. 2008-2024 (default) or 2015")
    parser.add_argument("--max-fields", type=positive_int, default=None, help="only N neighbouring fields (for testing)")
    parser.add_argument("--force", action="store_true", help="ignore cached results and ask Earth Engine again")
    args = parser.parse_args(argv)
    try:
        build(args.fields, args.out, args.years, max_fields=args.max_fields, force=args.force)
    except FileNotFoundError as exc:
        print(f"ERROR: fields file not found: {exc}. Run `make pipeline` first, or pass --fields.", file=sys.stderr)
        return 2
    except SchemaError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except EarthEngineUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except SatelliteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted: finished chunks are cached, run the same command again to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
