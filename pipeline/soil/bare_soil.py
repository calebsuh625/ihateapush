"""Bare-soil satellite views per field, year and sensor, via Google Earth Engine -> data/soil_bare_obs.parquet.

    python -m pipeline.soil.bare_soil                       # all fields, 2016-2024, Landsat + Sentinel-2
    python -m pipeline.soil.bare_soil --max-fields 30 --years 2022 --out /tmp/t.parquet
    python -m pipeline.soil.bare_soil --field-ids-file data/soil_proto_fields.txt --out data/soil_bare_obs_proto.parquet
    python -m pipeline.soil.bare_soil --estimate            # requests and rough EECU-hours; asks nothing of Earth Engine

This is the input of the soil model (it models nothing itself). It prints a short summary; the full
log goes to data/logs/soil_bare_<time>.log. Every Earth Engine request is cached under
data/raw/gee_soil/, so an interrupted run resumes and a rerun makes no request at all.

Whole county: about 39 EECU-hours (Landsat about 14, Sentinel-2 about 26; --estimate prints the
current figure), 865 requests in 51 chunks. Run it in stages, which costs nothing extra because
of the cache, and give every stage a budget (--max-eecu-hours stops sending requests when this
run has used that much; exit code 3; what finished stays cached):

    python -m pipeline.soil.bare_soil --sensors landsat --max-eecu-hours 18      # a complete table on its own
    python -m pipeline.soil.bare_soil --sensors s2 --years 2022 --max-eecu-hours 5 --out data/soil_bare_obs_s2_2022.parquet
    python -m pipeline.soil.bare_soil --max-eecu-hours 30                        # the rest; writes both sensors

After each stage read its last log lines ("this run: ... EECU-hours, slowest ... s; retries,
chunk splits" and "cost:") before releasing the next. pipeline.soil.build works on the Landsat-only
table, so Sentinel-2 is a choice, not a requirement (its report says what Sentinel-2 adds).

Output table
------------
Plain Parquet, long format, sorted by (field_id, sensor, year). One row per (field_id, year, sensor)
for every requested field, for sensor "landsat" in every requested year and for sensor "s2" in every
requested year from S2_FIRST_YEAR (2017) on: Sentinel-2 surface reflectance is incomplete for 2016.
The two sensor families are never mixed: compare them to measure noise.

    field_id      string  a field_id of data/fields.parquet
    year          int     calendar year (views from 1 January to 31 December, UTC)
    sensor        string  "landsat" (Landsat 8 + Landsat 9 from Nov 2021, Collection 2 Level 2, Tier 1)
                          or "s2" (Sentinel-2 A/B/C, COPERNICUS/S2_SR_HARMONIZED)
    n_px          int     number of grid cells (landsat: 30 m, s2: 40 m) whose centre lies inside the field
                          after it was shrunk inward by BUFFER_M (30 m). 0 = the field is too thin to
                          keep a single cell: every other column of the row is then null.
    n_clear       float   mean number of clear views per pixel that year (a view = one overpass day)
    n_bare        float   mean number of bare-soil views per pixel that year (0 when there were none)
    bare_px_frac  float   share (0-1) of the field's pixels with at least one bare-soil view
    bare_days     float   estimated days in the year (0-366) the soil sat bare and exposed. For each
                          month: bare share = bare pixel-views / clear pixel-views of the field in that
                          month; bare_days = sum over the 12 months of share x days in the month, so a
                          season with many clear views does not weigh more than one with few. A month
                          with fewer than MIN_CLEAR_PER_MONTH (0.5) clear views per pixel has no share
                          of its own: it is interpolated linearly between the nearest months that have
                          one (before the first / after the last such month: that month's share).
                          Null when fewer than MIN_MONTHS (8) months have a share of their own.
    clear_months  int     number of months (0-12) that had a share of their own (see bare_days)
    blue green red nir swir1 swir2                       (both sensors)
    re1 re2 re3 nir8a                                    (s2 only: B5, B6, B7, B8A; null for landsat)
                  float   surface reflectance 0-1 of the bare soil: per pixel the median over that
                          year's bare-soil views (each band on its own), then the mean over the field's
                          pixels that had at least one bare-soil view. Null when n_bare is 0.
                          Landsat: SR_B2, B3, B4, B5, B6, B7. Sentinel-2: B2, B3, B4, B8, B11, B12.
                          The bands of the two sensors are similar but not identical and no cross-sensor
                          harmonisation is applied: on the same field-years Sentinel-2 composites are
                          0.01-0.04 brighter than Landsat's in every band but nir.

Method
------
* Field outlines are shrunk inward by BUFFER_M in EPSG:5070 before they are sent, so cells that
  straddle a field edge, a road or a neighbour are not used. Only the shrunk outlines and their
  position number in the request are sent to Earth Engine; field ids and crops never leave this machine.
* Clear view. Landsat: QA_PIXEL without fill, dilated cloud, cirrus, cloud, cloud shadow, snow or
  water (LANDSAT_QA_PIXEL_REJECT) and QA_RADSAT without saturation in bands 2-7 or terrain occlusion;
  reflectance = DN x 0.0000275 - 0.2. Sentinel-2: Cloud Score+ (GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED)
  band S2_CS_BAND >= S2_CS_MIN, and the scene classification (SCL) not no-data, defective, water or
  snow; reflectance = DN x 0.0001 (the HARMONIZED collection has already removed the +1000 offset of
  2022 on). Both: every output band must be in (0, 1], and NDSI = (green - swir1) / (green + swir1)
  must be below NDSI_MAX (-0.25): snow that Cloud Score+ rates as clear and bright cloud that a mask
  missed would otherwise pass the bare-soil rule below.
* Scenes of the same UTC day are mosaicked first (in system:index order, the last clear scene on
  top), so a view is one overpass, not one scene or tile.
* Bare-soil view = clear view with NDVI < NDVI_MAX[sensor] and NBR2 < NBR2_MAX[sensor], where
  NDVI = (nir - red) / (nir + red) and NBR2 = (swir1 - swir2) / (swir1 + swir2). Low NBR2 = dry bare
  soil; crop residue (cellulose absorbs in swir2) and wet soil raise it. Landsat: NDVI < 0.25 and
  NBR2 < 0.10. Sentinel-2: NDVI < 0.20 and NBR2 < 0.08, which are the same limits seen through
  Sentinel-2 (it reads NDVI 0.04 and NBR2 0.02 lower on the same ground). See the constants for the
  measurements behind these values. A wet bare field therefore does not count as bare that day.
* Scenes whose metadata cloud cover is SCENE_MAX_CLOUD_PCT (80 %) or more are skipped.
* Grids (SENSOR_GRID), both UTM 14N: Landsat on its own 30 m grid; Sentinel-2 on a 40 m grid, read
  from the 40 m level of its image pyramid (each cell is the mean of 2 x 2 cells of the 20 m bands
  and 4 x 4 of the 10 m bands), which is cheaper than 20 m and uses every Sentinel-2 pixel.
* A pixel belongs to the field whose shrunk outline contains its centre. No area-weighted fallback:
  a field with no such cell gets nulls.

The file's Parquet metadata records the method it was computed with (read_table_method(path)):
the request signature of each sensor, whether the snow screen was on, and the bare_days rule.

Cache: one JSON file per (chunk of neighbouring fields, year, sensor) with the per-field sums that
come back from Earth Engine (pixels, bare pixels, band sums, clear and bare pixel-views per month).
The key covers the field ids, the outlines sent, the year and every constant that changes what Earth
Engine computes (request_signature). The bare_days rule (MIN_CLEAR_PER_MONTH, MIN_MONTHS) is applied
to the cached monthly counts when the table is assembled, so changing it needs no new request.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # allow `python pipeline/soil/bare_soil.py`
    sys.path.insert(0, str(ROOT))

from pipeline import satellite as sat  # noqa: E402  (reused, never edited: its cache is expensive)
from pipeline.common import DATA_DIR, EQUAL_AREA_CRS, RAW_DIR  # noqa: E402
from pipeline.satellite import (  # noqa: E402
    GRID_CRS,
    GRID_TRANSFORM,
    MAX_TILE_SCALE,
    MS_PER_DAY,
    Chunk,
    EarthEngineUnavailable,
    SatelliteError,
    Stats,
    classify_error,
    make_chunk,
    parse_years,
    positive_int,
)
from pipeline.schema import REAL_PATH, SchemaError, load_fields  # noqa: E402
from pipeline.soil.health_schema import YEARS  # noqa: E402

BARE_OBS_PATH = DATA_DIR / "soil_bare_obs.parquet"
CACHE_DIR = RAW_DIR / "gee_soil"
LOG_DIR = DATA_DIR / "logs"

# Bump when the Earth Engine computation changes in a way the constants below do not capture: it is
# part of every cache key, so old cached results are ignored (not deleted).
METHOD_VERSION = "1"

BUFFER_M = 30.0  # inward buffer of every field outline, metres, applied in EPSG:5070

# A clear view is a bare-soil view when NDVI < NDVI_MAX[sensor] and NBR2 < NBR2_MAX[sensor].
#
# Landsat: NDVI 0.25 is the usual bare-soil limit. NBR2 0.10 was chosen from the data: on 30 Finney
# fields in 2022 (6 each of fallow, alfalfa, corn, winter wheat, sorghum; 714 whole-field clear views
# with NDVI < 0.25), fallow fields peak at NBR2 0.06-0.10 and freshly harvested corn / wheat stubble
# at 0.10-0.14 (wheat stubble in July: median 0.139; fallow May-December: 0.07-0.085). 0.10 is where
# the fallow views stop outnumbering the stubble views; it is inside the published 0.05-0.15 range.
# At 0.10 the share of clear views called bare was fallow 69 %, sorghum 29 %, wheat 16 %, corn 7 %,
# alfalfa 3 %; at 0.075 fallow drops to 33 % and corn / wheat to 1 % (most field-years would have no
# bare view at all); at 0.125 corn rises to 26 % and wheat to 33 % (standing residue gets in).
#
# Sentinel-2 reads the same ground differently: on 301 same-day pairs of whole-field views its NDVI
# (B8) was 0.040 lower and its NBR2 0.022 lower than Landsat's (S2 = -0.040 + 0.969 x Landsat for NDVI;
# -0.019 + 0.974 x Landsat for NBR2; swir2 0.03 and swir1 0.02 brighter). Its limits are the Landsat
# limits passed through those fits, so the two families call the same views bare: with (0.20, 0.08)
# the same-day calls agreed on 282 of 301 pairs (53 both bare, 11 Landsat only, 8 Sentinel-2 only)
# and the per-crop shares were fallow 60 %, sorghum 21 %, wheat 15 %, corn 8 %, alfalfa 3 %.
# With Landsat's own limits Sentinel-2 would call 53 % more views bare.
NDVI_MAX = {"landsat": 0.25, "s2": 0.20}
NBR2_MAX = {"landsat": 0.10, "s2": 0.08}

# Snow and bright-cloud screen: a view whose NDSI = (green - swir1) / (green + swir1) is NDSI_MAX or
# more is not a clear view. Neither sensor's own masks are enough: on the prototype fields 8 % of
# the Sentinel-2 bare-soil composites computed without this screen were snow (blue 0.3-0.9, swir1
# 0.15; 24 % in 2019: Cloud Score+ rates snow as clear and the scene classification called it cloud
# or bare soil, not snow) and 1 % of Landsat's were cloud (blue > 0.2; 7 % in 2024). Both pass the
# NDVI and NBR2 limits. In raw pixels of 16 field-years the views that passed those limits had
# NDSI -0.6 to -0.4 on soil (98 % of them) and above -0.1 on snow or cloud, with almost nothing in
# between (-0.3 to -0.2: 0.2 %); green crops (NDVI > 0.4) stay below -0.38 (99th percentile), so the
# screen costs no real view: 0.7 % of Landsat's and 2.3 % of Sentinel-2's clear pixel-views fail it,
# nearly all from November to March. None switches the screen off (and then reproduces, from the
# cache, tables computed before the screen existed).
NDSI_MAX: float | None = -0.25

# Scenes whose own metadata says they are at least this cloudy are not read at all. Measured on the
# same fields (2022): such scenes held 0.5 % of the clear pixel-views of Sentinel-2 and none of
# Landsat's, but are 21-32 % of the Sentinel-2 scenes (4-8 % of Landsat's), i.e. of the cost.
SCENE_MAX_CLOUD_PCT = 80
SCENE_CLOUD_PROPERTY = {"landsat": "CLOUD_COVER", "s2": "CLOUDY_PIXEL_PERCENTAGE"}

VALID_MIN, VALID_MAX = 0.0, 1.0  # every band must be in (VALID_MIN, VALID_MAX]

# Landsat 8/9 OLI, Collection 2 Level 2, Tier 1: (collection, first calendar year used)
LANDSAT_COLLECTIONS = [("LANDSAT/LC08/C02/T1_L2", 2013), ("LANDSAT/LC09/C02/T1_L2", 2021)]
LANDSAT_BANDS = {
    "blue": "SR_B2", "green": "SR_B3", "red": "SR_B4", "nir": "SR_B5", "swir1": "SR_B6", "swir2": "SR_B7",
}
LANDSAT_SCALE, LANDSAT_OFFSET = 2.75e-05, -0.2
# QA_PIXEL bits: 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow, 5 snow, (6 = clear), 7 water
LANDSAT_QA_PIXEL_REJECT = 0b10111111
# QA_RADSAT: bit n-1 = band n saturated (bands 2-7 are used), bit 11 = terrain occlusion
LANDSAT_QA_RADSAT_REJECT = 0b100001111110

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_BANDS = {
    "blue": "B2", "green": "B3", "red": "B4", "re1": "B5", "re2": "B6", "re3": "B7",
    "nir": "B8", "nir8a": "B8A", "swir1": "B11", "swir2": "B12",
}
S2_SCALE, S2_OFFSET = 1e-04, 0.0
S2_CS_COLLECTION = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
S2_CS_BAND = "cs"
S2_CS_MIN = 0.60
S2_SCL_REJECT = (0, 1, 6, 11)  # no data, saturated/defective, water, snow/ice
# Measured over Finney County (Nov 2026): S2_SR_HARMONIZED holds a Level-2A image for every Level-1C
# image from 2017 on, but only for 64 of the 119 images of 2016. 2017 is thin (Sentinel-2A alone
# until mid-year: 3-8 overpass days a month over the county against 10-13 from 2018 on).
S2_FIRST_YEAR = 2017

# Output grids, both UTM 14N (the projection of the Landsat scenes and of both Sentinel-2 tiles here).
# Landsat: its own 30 m grid, read pixel for pixel. Sentinel-2: a 40 m grid, so that Earth Engine
# reads the 40 m level of the image pyramid (means of 2 x 2 cells of the 20 m bands and 4 x 4 of
# the 10 m bands; the scene classification is a sample, never a mean) - a quarter of the pixels of
# the 20 m level, which is what a 30 m grid would read and then use only 44 % of. Measured on one
# 202-field chunk, 2022: 269 EECU-s on the 30 m grid, 192 on this one (most of a request's cost is
# per scene, not per pixel). The 40 m rows start at northing 10 (mod 40):
# the two tiles' own 40 m cells start at 0 and at 20, so a sampled cell reaches at most 30 m from
# the centre it is sampled for, i.e. stays inside the field outline that was shrunk by BUFFER_M.
SENSOR_GRID = {"landsat": (GRID_CRS, GRID_TRANSFORM), "s2": (GRID_CRS, [40, 0, 0, 0, -40, 10])}

SENSORS = ("landsat", "s2")
SENSOR_BANDS = {"landsat": list(LANDSAT_BANDS), "s2": list(S2_BANDS)}
SENSOR_FIRST_YEAR = {"landsat": 2013, "s2": S2_FIRST_YEAR}
BAND_COLUMNS = ["blue", "green", "red", "re1", "re2", "re3", "nir", "nir8a", "swir1", "swir2"]
COLUMNS = [
    "field_id", "year", "sensor", "n_px", "n_clear", "n_bare", "bare_px_frac", "bare_days", "clear_months",
    *BAND_COLUMNS,
]

# bare_days (applied to the cached monthly counts at assembly time; not part of the cache key)
MIN_CLEAR_PER_MONTH = 0.5  # clear views per pixel a month needs to have a bare share of its own
MIN_MONTHS = 8  # months with a share of their own needed for a bare_days value

CHUNK_SIZE = 300  # fields per request at most
# A request computes every cell of its bounding box (measured, 2022: about 0.9 EECU-s per km2 for
# Landsat and 3-4 for Sentinel-2, plus ~15 per request), so fields that are far apart must not share
# one: a chunk is closed when its box is larger than CHUNK_FREE_KM2 and the fields would fill less
# than MIN_FILL of it.
MIN_FILL = 0.10
CHUNK_FREE_KM2 = 15.0
# No request box larger than this. Without a cap seven county chunks had boxes of 252-493 km2, half
# of the area to compute, and nothing that large was ever measured; a time-out there loses the whole
# request's EECU. With the cap the county is 51 chunks, the largest 139 km2, and the two largest
# were measured with the current method (2022, data/logs/soil_bare_probe_*.log): Landsat 149 and
# 221 EECU-s, Sentinel-2 309 and 489, each answered in 16-17 s with no retry.
MAX_BOX_KM2 = 140.0
# Padding of a request box, degrees (~2 m, against coordinate rounding). It decides which grid
# cells the box cuts, so request_signature() carries any value other than this original one.
BOX_PAD_DEG = 0.00002
_ORIGINAL_BOX_PAD_DEG = 0.00002
# Rough EECU-seconds of one request against its box area (km2), for --estimate only. Points are
# (km2, EECU-s), joined by straight lines and continued with the last slope.
# Landsat: 7 per request + 1.28 per km2. That is the mean of the two 139 km2 probes (185) and it
# reproduces the prototype run (243 requests, boxes of 7 km2 on average, 12 EECU-s each).
# Sentinel-2: 192 at 67 km2 (2022) and 399 at 139 km2 (mean of the two probes); the prototype's
# small boxes cost 29 each against 32 from this line.
# The two probes differ by a factor 1.5 at the same box size (231 against 278 fields), so a single
# request is known to about +-25 % and the total to perhaps +-20 %.
COST_POINTS = {
    "landsat": [(0.0, 7.0), (139.0, 185.0)],
    "s2": [(0.0, 15.0), (67.0, 192.0), (139.0, 399.0)],
}
COST_MEASURED_UP_TO_KM2 = 140.0
TILE_PX = 256  # fields are grouped by 7.68 km cells (256 pixels of the 30 m grid) before they are chunked
WORKERS = 4  # concurrent requests (the free tier allows few concurrent aggregations)

# Packing of the monthly view counts (see BareSoilEngine.year_image): at most 255 views a month.
PACK_BARE = 256
PACK_MONTH = 65536


def sensor_years(sensor: str, years) -> list[int]:
    """The requested years this sensor family has rows for."""
    return [int(y) for y in sorted(years) if int(y) >= SENSOR_FIRST_YEAR[sensor]]


def cache_columns(sensor: str) -> list[str]:
    """Names of the per-field sums cached for one (chunk, year, sensor) request, in order."""
    return (
        ["n_px", "bare_px"]
        + [f"sum_{b}" for b in SENSOR_BANDS[sensor]]
        + [f"clear_{m:02d}" for m in range(1, 13)]
        + [f"bare_{m:02d}" for m in range(1, 13)]
    )


def request_signature(sensor: str) -> dict:
    """Everything that defines what Earth Engine computes for `sensor`; hashed into every cache key."""
    common = {
        "version": METHOD_VERSION,
        "sensor": sensor,
        "ndvi_max": NDVI_MAX[sensor],
        "nbr2_max": NBR2_MAX[sensor],
        "scene_max_cloud": [SCENE_CLOUD_PROPERTY[sensor], SCENE_MAX_CLOUD_PCT],
        "valid": [VALID_MIN, VALID_MAX],
        "buffer_m": BUFFER_M,
        "grid": list(SENSOR_GRID[sensor]),
        "columns": cache_columns(sensor),
    }
    if NDSI_MAX is not None:
        common["ndsi_max"] = NDSI_MAX
    if BOX_PAD_DEG != _ORIGINAL_BOX_PAD_DEG:  # only when changed, so results cached before this entry existed stay valid
        common["box_pad_deg"] = BOX_PAD_DEG
    if sensor == "landsat":
        return {
            **common,
            "collections": [list(c) for c in LANDSAT_COLLECTIONS],
            "bands": LANDSAT_BANDS,
            "scale_offset": [LANDSAT_SCALE, LANDSAT_OFFSET],
            "qa_pixel_reject": LANDSAT_QA_PIXEL_REJECT,
            "qa_radsat_reject": LANDSAT_QA_RADSAT_REJECT,
        }
    if sensor == "s2":
        return {
            **common,
            "collection": S2_COLLECTION,
            "bands": S2_BANDS,
            "scale_offset": [S2_SCALE, S2_OFFSET],
            "cloud_score": [S2_CS_COLLECTION, S2_CS_BAND, S2_CS_MIN],
            "scl_reject": list(S2_SCL_REJECT),
        }
    raise ValueError(f"unknown sensor {sensor!r}")


class InputError(SatelliteError):
    """A problem with what the user passed in (exit code 2)."""


class BudgetExceeded(SatelliteError):
    """--max-eecu-hours was reached: no new request is sent; what finished is cached (exit code 3)."""


# --------------------------------------------------------------------------- log file


class RunLog:
    """Full log in a file; nothing is printed (the CLI prints a short summary at the end)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def __call__(self, msg: str) -> None:
        if self.path is None:
            return
        with self._lock, self.path.open("a") as fh:
            fh.write(f"[{time.monotonic() - self._t0:8.1f}s] {msg}\n")


def new_log_path() -> Path:
    return LOG_DIR / f"soil_bare_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"


# --------------------------------------------------------------------------- fields -> chunks


def buffer_inward(fields, metres: float = BUFFER_M):
    """Field outlines shrunk inward by `metres` (in EPSG:5070), as a lon/lat GeoSeries aligned with
    `fields`. A field too thin to survive becomes an empty geometry; one that splits keeps all parts."""
    shrunk = fields.geometry.to_crs(EQUAL_AREA_CRS).buffer(-float(metres))
    return shrunk.to_crs("EPSG:4326")


def geojson_bounds(geometry: dict) -> tuple[float, float, float, float]:
    """(west, south, east, north) of a GeoJSON Polygon or MultiPolygon dict."""
    xs: list[float] = []
    ys: list[float] = []

    def walk(obj):
        if obj and isinstance(obj[0], (int, float)):
            xs.append(obj[0])
            ys.append(obj[1])
        else:
            for item in obj:
                walk(item)

    walk(geometry["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def chunk_bounds(chunk: Chunk) -> list[float]:
    """[west, south, east, north] of a chunk's outlines (lon/lat), padded by ~2 m (coordinate rounding)."""
    boxes = [geojson_bounds(g) for g in chunk.geometries]
    pad = BOX_PAD_DEG
    return [
        min(b[0] for b in boxes) - pad, min(b[1] for b in boxes) - pad,
        max(b[2] for b in boxes) + pad, max(b[3] for b in boxes) + pad,
    ]


def grid_cell_order(geometries) -> tuple[np.ndarray, np.ndarray]:
    """(row positions of the lon/lat geometries in walking order, a cell number per position).

    First by the 7.68 km grid cell (TILE_PX pixels of the 30 m grid) that holds the geometry's
    centroid, the cells along a Hilbert curve; then along a Hilbert curve inside the cell. Fields
    of one cell are therefore never interleaved with another cell's.
    """
    import geopandas as gpd

    size = GRID_TRANSFORM[0] * TILE_PX
    centroids = geometries.to_crs(GRID_CRS).centroid
    col = np.floor((centroids.x.to_numpy() - GRID_TRANSFORM[2]) / size).astype(int)
    row = np.floor((centroids.y.to_numpy() - GRID_TRANSFORM[5]) / size).astype(int)
    if len(set(zip(col.tolist(), row.tolist()))) > 1:
        cells = gpd.GeoSeries(gpd.points_from_xy(col, row))
        cell_rank = cells.hilbert_distance(total_bounds=cells.total_bounds).to_numpy()
    else:
        cell_rank = np.zeros(len(col), dtype=int)
    within = geometries.hilbert_distance(total_bounds=geometries.total_bounds).to_numpy()
    order = np.lexsort((within, row, col, cell_rank))
    cell_id = (col - col.min()) * (int(row.max() - row.min()) + 1) + (row - row.min())
    return order, cell_id[order]


def make_soil_chunks(
    fields, chunk_size: int = CHUNK_SIZE, min_fill: float = MIN_FILL, max_box_km2: float | None = None
) -> tuple[list[Chunk], list[str]]:
    """(chunks of shrunk outlines, ids of the fields too thin to survive the buffer).

    Fields are walked in grid_cell_order. A chunk is closed when it holds `chunk_size` fields; at
    the start of a grid cell whose fields would not all fit any more; when adding the next field
    would leave the chunk's bounding box larger than CHUNK_FREE_KM2 and less than `min_fill`
    covered by fields (so far-apart fields never share a request); or when it would make the box
    larger than `max_box_km2` (default MAX_BOX_KM2), so no request is larger than the largest one
    whose cost was measured. Deterministic for a given set of fields, whatever their row order.
    """
    max_box_km2 = MAX_BOX_KM2 if max_box_km2 is None else max_box_km2
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    fields = fields.sort_values("field_id").reset_index(drop=True)
    shrunk = buffer_inward(fields)
    keep = ~(shrunk.is_empty | shrunk.isna()).to_numpy()
    ids = fields["field_id"].to_numpy()
    lost = sorted(str(f) for f in ids[~keep])
    if not keep.any():
        return [], lost
    kept = shrunk[keep].reset_index(drop=True)
    order, cell = grid_cell_order(kept)
    projected = kept.to_crs(GRID_CRS)
    areas = projected.area.to_numpy()[order]
    boxes = projected.bounds.to_numpy()[order]
    kept_ids = ids[keep][order]
    geoms = [sat.geometry_to_geojson(g) for g in kept.to_numpy()[order]]
    cell_sizes = {c: int(n) for c, n in zip(*np.unique(cell, return_counts=True))}

    chunks: list[Chunk] = []
    start, area, box = 0, 0.0, None
    for i in range(len(kept_ids)):
        if box is not None:
            new_box = (min(box[0], boxes[i][0]), min(box[1], boxes[i][1]), max(box[2], boxes[i][2]), max(box[3], boxes[i][3]))
            box_m2 = (new_box[2] - new_box[0]) * (new_box[3] - new_box[1])
            sparse = box_m2 > CHUNK_FREE_KM2 * 1e6 and (area + areas[i]) / box_m2 < min_fill
            new_cell = cell[i] != cell[i - 1] and (i - start) + cell_sizes[cell[i]] > chunk_size
            too_wide = box_m2 > max_box_km2 * 1e6
            if i - start >= chunk_size or sparse or new_cell or too_wide:
                chunks.append(make_chunk(kept_ids[start:i].tolist(), geoms[start:i]))
                start, area, box = i, 0.0, None
        if box is None:
            new_box = tuple(boxes[i])
        area += areas[i]
        box = new_box
    chunks.append(make_chunk(kept_ids[start:].tolist(), geoms[start:]))
    return chunks, lost


def chunk_box_km2(chunk: Chunk) -> float:
    """Area of a chunk's request box in km2 (lon/lat box on a sphere; good to about 1 %)."""
    west, south, east, north = chunk_bounds(chunk)
    mid = np.radians((south + north) / 2.0)
    return float((east - west) * 111.32 * np.cos(mid) * (north - south) * 110.95)


def request_cost_eecu_s(sensor: str, box_km2: float) -> float:
    """Rough EECU-seconds of one request (COST_POINTS): straight lines between the measured points."""
    points = COST_POINTS[sensor]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    if box_km2 <= xs[-1]:
        return float(np.interp(box_km2, xs, ys))
    slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
    return float(ys[-1] + slope * (box_km2 - xs[-1]))


def estimate_cost(chunks: list[Chunk], years, sensors, cache_dir: Path | None = None, force: bool = False) -> dict:
    """What a run would ask of Earth Engine, without contacting it.

    Per sensor: requests (not cached yet), their estimated EECU-hours, and how many of them have a
    box beyond the largest one measured (COST_MEASURED_UP_TO_KM2), where the estimate is a guess.
    """
    areas = [chunk_box_km2(c) for c in chunks]
    out: dict = {
        "chunks": len(chunks),
        "box_km2_total": float(sum(areas)),
        "box_km2_max": float(max(areas)) if areas else 0.0,
        "sensors": {},
    }
    for sensor in sensors:
        requests, cached, eecu, beyond = 0, 0, 0.0, 0
        for year in sensor_years(sensor, years):
            for chunk, area in zip(chunks, areas):
                if not force and cache_dir is not None and cache_path(cache_dir, chunk, year, sensor).exists():
                    cached += 1
                    continue
                requests += 1
                eecu += request_cost_eecu_s(sensor, area)
                beyond += area > COST_MEASURED_UP_TO_KM2
        out["sensors"][sensor] = {"requests": requests, "cached": cached, "eecu_hours": eecu / 3600.0, "requests_beyond_measured": int(beyond)}
    out["eecu_hours"] = float(sum(v["eecu_hours"] for v in out["sensors"].values()))
    out["requests"] = int(sum(v["requests"] for v in out["sensors"].values()))
    return out


def estimate_lines(est: dict) -> list[str]:
    lines = [
        f"{est['chunks']} chunks; request boxes {est['box_km2_total']:,.0f} km2 in all, largest {est['box_km2_max']:.0f} km2"
        f" (cost measured up to {COST_MEASURED_UP_TO_KM2:.0f} km2)"
    ]
    for sensor, v in est["sensors"].items():
        note = f"; {v['requests_beyond_measured']} with a box beyond the measured range" if v["requests_beyond_measured"] else ""
        lines.append(f"  {sensor}: {v['requests']} requests to send ({v['cached']} cached), about {v['eecu_hours']:.1f} EECU-hours{note}")
    lines.append(
        f"estimate: {est['requests']} requests, about {est['eecu_hours']:.1f} EECU-hours"
        " (rough: from a handful of 2022 measurements; allow +-25 % and read the cost line of the first stage)"
    )
    return lines


def spent_eecu_s(costs: list[dict]) -> float:
    """EECU-seconds of the finished requests; one whose profile did not come back counts as its sensor's mean."""
    total = 0.0
    for sensor in {c["sensor"] for c in costs}:
        known = [c["eecu_s"] for c in costs if c["sensor"] == sensor and c["eecu_s"] is not None]
        unknown = sum(1 for c in costs if c["sensor"] == sensor and c["eecu_s"] is None)
        total += sum(known) + (unknown * sum(known) / len(known) if known else 0.0)
    return float(total)


# --------------------------------------------------------------------------- cache


def cache_key(chunk: Chunk, year: int, sensor: str) -> str:
    """Identifies one (chunk, year, sensor) result: field ids + outlines sent + year + the method."""
    payload = {"method": request_signature(sensor), "year": int(year), "chunk": chunk.digest}
    return hashlib.sha256(sat._canonical(payload)).hexdigest()[:32]


def cache_path(cache_dir: Path, chunk: Chunk, year: int, sensor: str) -> Path:
    return Path(cache_dir) / f"{sensor}_{int(year)}_{cache_key(chunk, year, sensor)}.json"


def read_cache(cache_dir: Path, chunk: Chunk, year: int, sensor: str) -> dict[str, list] | None:
    """The cached {field_id: sums in cache_columns(sensor) order}, or None if absent/unusable."""
    path = cache_path(cache_dir, chunk, year, sensor)
    width = len(cache_columns(sensor))
    try:
        values = json.loads(path.read_text())["values"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(values, dict):
        return None
    for fid in chunk.field_ids:
        row = values.get(fid)
        if not isinstance(row, list) or len(row) != width:
            return None
    return {fid: values[fid] for fid in chunk.field_ids}


def write_cache(cache_dir: Path, chunk: Chunk, year: int, sensor: str, values: dict[str, list]) -> None:
    path = cache_path(cache_dir, chunk, year, sensor)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    tmp.write_text(
        json.dumps(
            {
                "sensor": sensor,
                "year": int(year),
                "method_version": METHOD_VERSION,
                "columns": cache_columns(sensor),
                "values": {fid: values[fid] for fid in chunk.field_ids},
            }
        )
    )
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Earth Engine layer


def parse_profile_eecu(text: str) -> float:
    """Total EECU-seconds in the text of an Earth Engine computation profile."""
    total = 0.0
    for line in text.splitlines():
        parts = line.split()
        if parts:
            try:
                total += float(parts[0])
            except ValueError:
                pass
    return total


class BareSoilEngine:
    """The only code that talks to Earth Engine. `reduce(chunk, year, sensor)` is one request."""

    def __init__(self, profile: bool = True) -> None:
        self.ee = sat.EarthEngine().ee  # initialises Earth Engine, with the clean authentication error
        self.profile = profile
        self._lock = threading.Lock()
        self.costs: list[dict] = []  # one entry per finished request: sensor, year, fields, seconds, eecu_s

    # -- images ------------------------------------------------------------------------------

    def scenes(self, sensor: str, year: int, region):
        """The raw scenes of one sensor family over `region` in the calendar year."""
        ee = self.ee
        start, end = f"{year}-01-01", f"{year + 1}-01-01"
        not_overcast = ee.Filter.lt(SCENE_CLOUD_PROPERTY[sensor], SCENE_MAX_CLOUD_PCT)
        if sensor == "s2":
            return (
                ee.ImageCollection(S2_COLLECTION).filterBounds(region).filterDate(start, end).filter(not_overcast)
                .linkCollection(ee.ImageCollection(S2_CS_COLLECTION), [S2_CS_BAND])
            )
        scenes = None
        for collection, first in LANDSAT_COLLECTIONS:
            if year >= first:
                part = ee.ImageCollection(collection).filterBounds(region).filterDate(start, end).filter(not_overcast)
                scenes = part if scenes is None else scenes.merge(part)
        return scenes

    def clear_scene(self, sensor: str, img):
        """One scene -> its raw band values (named as in SENSOR_BANDS), masked to the sensor's clear flags."""
        names = SENSOR_BANDS[sensor]
        if sensor == "s2":
            scl = img.select("SCL")
            clear = img.select(S2_CS_BAND).gte(S2_CS_MIN)
            for code in S2_SCL_REJECT:
                clear = clear.And(scl.neq(code))
            return img.select([S2_BANDS[n] for n in names], names).updateMask(clear)
        clear = (
            img.select("QA_PIXEL").bitwiseAnd(LANDSAT_QA_PIXEL_REJECT).eq(0)
            .And(img.select("QA_RADSAT").bitwiseAnd(LANDSAT_QA_RADSAT_REJECT).eq(0))
        )
        return img.select([LANDSAT_BANDS[n] for n in names], names).updateMask(clear)

    def views(self, sensor: str, year: int, region):
        """One image per overpass day: reflectance bands + 'ndvi' + 'nbr2', masked to clear views.

        Same-day scenes are mosaicked (system:index order, last clear scene on top) before the
        values are scaled and checked, so every per-view step runs once per day, not per scene.
        """
        ee = self.ee
        scenes = self.scenes(sensor, year, region)
        names = SENSOR_BANDS[sensor]
        scale, offset = (S2_SCALE, S2_OFFSET) if sensor == "s2" else (LANDSAT_SCALE, LANDSAT_OFFSET)
        # A never-clear placeholder keeps the bands present when a year has no scene at all.
        placeholder = (
            ee.Image.constant([0] * (len(names) + 2)).rename([*names, "ndvi", "nbr2"]).toFloat()
            .updateMask(ee.Image.constant(0)).set("system:time_start", ee.Date(f"{year}-07-01").millis())
        )
        if scenes is None:
            return ee.ImageCollection([placeholder])

        def one_day(day):
            day_start = ee.Date(ee.Number(day).multiply(MS_PER_DAY))
            raw = (
                scenes.filterDate(day_start, day_start.advance(1, "day")).sort("system:index")
                .map(lambda img: self.clear_scene(sensor, img)).mosaic()
            )
            sr = raw.multiply(scale).add(offset) if offset else raw.multiply(scale)
            in_range = sr.gt(VALID_MIN).And(sr.lte(VALID_MAX))
            valid = in_range.select([names[0]])
            for name in names[1:]:
                valid = valid.And(in_range.select([name]))
            if NDSI_MAX is not None:  # snow and bright cloud are not clear views
                valid = valid.And(sr.normalizedDifference(["green", "swir1"]).lt(NDSI_MAX))
            return (
                sr.addBands(sr.normalizedDifference(["nir", "red"]).rename("ndvi"))
                .addBands(sr.normalizedDifference(["swir1", "swir2"]).rename("nbr2"))
                .updateMask(valid).toFloat().set("system:time_start", day_start.millis())
            )

        days = (
            ee.List(scenes.aggregate_array("system:time_start"))
            .map(lambda t: ee.Number(t).divide(MS_PER_DAY).floor())
            .distinct()
        )
        return ee.ImageCollection([placeholder]).merge(ee.ImageCollection.fromImages(days.map(one_day)))

    def year_image(self, sensor: str, year: int, region):
        """Per-pixel image: the bands' medians over the bare views (masked where there was none),
        then clear_01..clear_12 and bare_01..bare_12 = number of clear / bare views per month.

        The 24 monthly counts are summed over the views as 4 packed numbers (three months each,
        16 bits a month: clear count in the low byte, bare count in the high byte) and unpacked
        afterwards: summing 24 separate bands over the year's views cost about 4x more.
        """
        ee = self.ee
        names = SENSOR_BANDS[sensor]
        quarters = ee.Image.constant([0, 1, 2, 3])
        packed_names = ["q0", "q1", "q2", "q3"]

        def per_view(view):
            month0 = ee.Number(ee.Date(view.get("system:time_start")).get("month")).subtract(1)
            clear = view.select("ndvi").mask().gt(0)
            bare = view.select("ndvi").lt(NDVI_MAX[sensor]).And(view.select("nbr2").lt(NBR2_MAX[sensor])).unmask(0)
            code = clear.add(bare.multiply(PACK_BARE)).multiply(
                ee.Image.constant(ee.Number(PACK_MONTH).pow(month0.mod(3)))
            )
            packed = quarters.eq(ee.Image.constant(month0.divide(3).floor())).multiply(code).toDouble()
            return view.select(names).updateMask(bare).addBands(packed.rename(packed_names))

        views = self.views(sensor, year, region).map(per_view)
        medians = views.select(names).reduce(ee.Reducer.median()).rename(names)
        packed = views.select(packed_names).reduce(ee.Reducer.sum()).unmask(0).toInt64()
        clear_bands, bare_bands = [], []
        for month0 in range(12):
            slot = packed.select([month0 // 3]).rightShift(16 * (month0 % 3))
            clear_bands.append(slot.bitwiseAnd(PACK_BARE - 1).rename(f"clear_{month0 + 1:02d}"))
            bare_bands.append(slot.rightShift(8).bitwiseAnd(PACK_BARE - 1).rename(f"bare_{month0 + 1:02d}"))
        return medians.addBands(ee.Image.cat(clear_bands + bare_bands).toFloat())

    # -- request -----------------------------------------------------------------------------

    def reduce(self, chunk: Chunk, year: int, sensor: str, tile_scale: int = 1) -> dict[str, list]:
        """One Earth Engine request: {field_id: sums in cache_columns(sensor) order}.

        The outlines are painted onto the grid (a pixel belongs to the outline that contains its
        centre) and all of them are summed in one grouped reduction over the chunk's bounding box.
        A field with no pixel gets zeros.
        """
        ee = self.ee
        names = SENSOR_BANDS[sensor]
        width = len(cache_columns(sensor))
        region = ee.Geometry.Rectangle(chunk_bounds(chunk), None, False)
        image = self.year_image(sensor, year, region)
        features = ee.FeatureCollection(
            # idx (position in the chunk) is the only attribute sent with an outline
            [ee.Feature(ee.Geometry(geometry), {"idx": i}) for i, geometry in enumerate(chunk.geometries)]
        )
        medians = image.select(names)
        stack = (
            ee.Image.constant(1).addBands(medians.select([names[0]]).mask()).addBands(medians.unmask(0))
            .addBands(image.select(cache_columns(sensor)[2 + len(names):])).toFloat()
            .addBands(ee.Image().toInt().paint(features, "idx"))
        )
        request = stack.reduceRegion(
            reducer=ee.Reducer.sum().repeat(width).group(groupField=width, groupName="idx"),
            geometry=region,
            crs=SENSOR_GRID[sensor][0],
            crsTransform=SENSOR_GRID[sensor][1],
            maxPixels=1e9,
            tileScale=tile_scale,
        )
        started = time.monotonic()
        profile_ids: list[str] = []
        try:
            if self.profile:
                with ee.data.profiling(profile_ids.append):
                    grouped = request.getInfo()
            else:
                grouped = request.getInfo()
        finally:
            self._record(sensor, year, len(chunk), time.monotonic() - started, profile_ids)
        out = {fid: [0.0] * width for fid in chunk.field_ids}
        for group in grouped.get("groups", []):
            out[chunk.field_ids[int(group["idx"])]] = [float(v) for v in group["sum"]]
        return out

    def _record(self, sensor: str, year: int, n_fields: int, seconds: float, profile_ids: list[str]) -> None:
        eecu = fetch_profile_eecu(profile_ids) if profile_ids else None
        with self._lock:
            self.costs.append({"sensor": sensor, "year": year, "fields": n_fields, "seconds": seconds, "eecu_s": eecu})


def fetch_profile_eecu(profile_ids: list[str], attempts: int = 4, sleep=time.sleep) -> float | None:
    """EECU-seconds of the profiled calls, or None: the cost figure is a nicety, never a reason to fail.

    A profile is often "not found" for a few seconds after its request returns, hence the retries.
    """
    try:
        from ee import apifunction

        get_profiles = apifunction.ApiFunction.lookup("Profile.getProfiles")
    except Exception:  # noqa: BLE001
        return None
    for attempt in range(attempts):
        try:
            return parse_profile_eecu(get_profiles.call(ids=profile_ids).getInfo())
        except Exception:  # noqa: BLE001
            sleep(1.5 * (attempt + 1))
    return None


# --------------------------------------------------------------------------- retries


def fetch(
    compute,
    chunk: Chunk,
    year: int,
    sensor: str,
    cache_dir: Path,
    *,
    force: bool = False,
    stats: Stats | None = None,
    sleep=time.sleep,
    max_attempts: int = 7,
    tile_scale: int = 1,
    log=lambda msg: None,
) -> dict[str, list]:
    """Result for one (chunk, year, sensor): from the cache, else from `compute(chunk, year, sensor, tile_scale)`.

    Same policy as pipeline/satellite.py: quota / concurrency / network errors are retried with
    exponential backoff; time-outs and memory errors are retried as two half chunks (each cached
    on its own), a single field with a larger tileScale. The combined result is cached under the
    original chunk's key, so a rerun needs no request.
    """
    stats = stats or Stats()
    if not force:
        cached = read_cache(cache_dir, chunk, year, sensor)
        if cached is not None:
            stats.add("cache_hits")
            return cached

    what = f"{sensor} {year} chunk of {len(chunk)} fields"
    attempt = 0
    while True:
        attempt += 1
        stats.add("requests")
        try:
            values = compute(chunk, year, sensor, tile_scale)
            break
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below, anything unknown is re-raised
            kind = classify_error(exc)
            reason = " ".join(str(exc).split())[:160]
            if kind == "too_big":
                if len(chunk) > 1:
                    log(f"{what}: {reason}; retrying as two halves")
                    stats.add("splits")
                    values = {}
                    for half in chunk.split():
                        values.update(
                            fetch(
                                compute, half, year, sensor, cache_dir, force=force, stats=stats, sleep=sleep,
                                max_attempts=max_attempts, tile_scale=tile_scale, log=log,
                            )
                        )
                    break
                if tile_scale < MAX_TILE_SCALE:
                    tile_scale *= 2
                    attempt = 0
                    log(f"{what}: {reason}; retrying with tileScale={tile_scale}")
                    stats.add("retries")
                    continue
                raise SatelliteError(f"Earth Engine could not compute {what}: {reason}") from exc
            if kind == "fatal":
                raise SatelliteError(f"Earth Engine request failed ({what}): {reason}") from exc
            if attempt >= max_attempts:
                raise SatelliteError(
                    f"Earth Engine request still failing after {attempt} attempts ({what}): {reason}\n"
                    "Finished chunks are cached: run the same command again later to resume."
                ) from exc
            base = 15 if kind == "busy" else 4
            delay = min(240.0, base * 2 ** (attempt - 1)) * (0.75 + 0.5 * random.random())
            log(f"{what}: {reason}; attempt {attempt}/{max_attempts}, waiting {delay:.0f}s")
            stats.add("retries")
            sleep(delay)

    width = len(cache_columns(sensor))
    bad = [fid for fid in chunk.field_ids if not isinstance(values.get(fid), (list, tuple)) or len(values[fid]) != width]
    if bad:  # reduce() returns a row for every field it was sent; anything else is a bug upstream
        raise SatelliteError(f"Earth Engine returned no usable row for {len(bad)} field(s), e.g. {bad[0]} ({what})")
    values = {fid: [_number(v) for v in values[fid]] for fid in chunk.field_ids}
    write_cache(cache_dir, chunk, year, sensor, values)
    stats.add("computed")
    return values


def _number(value) -> float:
    return 0.0 if value is None or value != value else float(value)


# --------------------------------------------------------------------------- orchestration


def collect(
    chunks: list[Chunk],
    years: list[int],
    sensors,
    cache_dir: Path,
    *,
    compute=None,
    force: bool = False,
    workers: int = WORKERS,
    stats: Stats | None = None,
    sleep=time.sleep,
    log=lambda msg: None,
    engine_factory=None,
    max_eecu_s: float | None = None,
    spent=None,
) -> dict[tuple[str, int, str], list]:
    """{(field_id, year, sensor): sums} for every chunk, sensor and year that sensor has.

    `compute(chunk, year, sensor, tile_scale)` is one Earth Engine request; by default it is
    `BareSoilEngine().reduce`, and Earth Engine is only initialised when something is not cached.

    `max_eecu_s`: once `spent()` (EECU-seconds used by this run; by default what the engine's
    profiles add up to) reaches it, no further request is sent and BudgetExceeded is raised. The
    requests already running finish and are cached, so the overshoot is at most `workers` requests.
    """
    stats = stats or Stats()
    results: dict[tuple[str, int, str], list] = {}
    pending: list[tuple[Chunk, int, str]] = []
    total = 0
    for sensor in sensors:
        for year in sensor_years(sensor, years):
            for chunk in chunks:
                total += 1
                cached = None if force else read_cache(cache_dir, chunk, year, sensor)
                if cached is None:
                    pending.append((chunk, year, sensor))
                else:
                    stats.add("cache_hits")
                    for fid, row in cached.items():
                        results[(fid, year, sensor)] = row
    log(f"{total} (chunk, year, sensor) results needed: {total - len(pending)} cached, {len(pending)} to request")
    if not pending:
        return results

    if compute is None:
        engine = (engine_factory or BareSoilEngine)()
        compute = engine.reduce
        if spent is None:
            spent = lambda: spent_eecu_s(list(engine.costs))  # noqa: E731
        log("Earth Engine initialised")
    if max_eecu_s is not None:
        if spent is None:
            raise InputError("a budget needs to know what requests cost: do not combine --max-eecu-hours with --no-profile")
        unguarded = compute

        def compute(chunk, year, sensor, tile_scale):
            used = spent()
            if used >= max_eecu_s:
                raise BudgetExceeded(
                    f"stopped at the budget: {used / 3600:.2f} EECU-hours used by this run, limit {max_eecu_s / 3600:.2f}. "
                    "Finished requests are cached: run the same command again (with a new budget) to continue."
                )
            return unguarded(chunk, year, sensor, tile_scale)

    started = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                fetch, compute, chunk, year, sensor, cache_dir, force=force, stats=stats, sleep=sleep, log=log
            ): (chunk, year, sensor)
            for chunk, year, sensor in pending
        }
        try:
            not_done = set(futures)
            while not_done:
                finished, not_done = wait(not_done, return_when=FIRST_COMPLETED)
                for future in finished:
                    chunk, year, sensor = futures[future]
                    for fid, row in future.result().items():  # raises the first failure
                        results[(fid, year, sensor)] = row
                    done += 1
                    elapsed = time.monotonic() - started
                    log(
                        f"{done}/{len(pending)} requests done ({sensor} {year}, {len(chunk)} fields); "
                        f"{elapsed / 60:.1f} min elapsed, ~{elapsed / done * (len(pending) - done) / 60:.1f} min left"
                    )
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return results


# --------------------------------------------------------------------------- assembly


def bare_days(clear_by_month, bare_by_month, n_px: float, year: int) -> tuple[float | None, int]:
    """(bare_days or None, clear_months) from a field's 12 monthly sums of clear and bare pixel-views.

    A month with at least MIN_CLEAR_PER_MONTH clear views per pixel has a bare share of its own
    (bare / clear); the other months are interpolated linearly between their nearest neighbours that
    have one (flat before the first and after the last). bare_days = sum of share x days in month;
    None when fewer than MIN_MONTHS months have a share of their own.
    """
    clear = np.asarray(clear_by_month, dtype=float)
    bare = np.asarray(bare_by_month, dtype=float)
    if not n_px or n_px <= 0:
        return None, 0
    known = clear / float(n_px) >= MIN_CLEAR_PER_MONTH
    n_known = int(known.sum())
    if n_known < MIN_MONTHS:
        return None, n_known
    months = np.arange(12)
    share = np.interp(months, months[known], bare[known] / clear[known])
    days = np.array([calendar.monthrange(int(year), m)[1] for m in range(1, 13)], dtype=float)
    return float(np.clip((share * days).sum(), 0.0, days.sum())), n_known


def row_from_sums(sensor: str, year: int, sums) -> dict:
    """The output columns (without the keys) of one (field, year, sensor) from its cached sums."""
    names = SENSOR_BANDS[sensor]
    out = {c: None for c in COLUMNS[3:]}
    out["n_px"], out["clear_months"] = 0, None
    if sums is None:
        return out
    n_px, bare_px = float(sums[0]), float(sums[1])
    if n_px <= 0:
        return out
    band_sums = sums[2 : 2 + len(names)]
    clear_m = np.asarray(sums[2 + len(names) : 14 + len(names)], dtype=float)
    bare_m = np.asarray(sums[14 + len(names) : 26 + len(names)], dtype=float)
    days, months = bare_days(clear_m, bare_m, n_px, year)
    out.update(
        n_px=int(round(n_px)),
        n_clear=float(clear_m.sum() / n_px),
        n_bare=float(bare_m.sum() / n_px),
        bare_px_frac=float(min(1.0, bare_px / n_px)),
        bare_days=days,
        clear_months=months,
    )
    if bare_px > 0:
        for name, total in zip(names, band_sums):
            out[name] = float(total) / bare_px
    return out


def assemble(field_ids, years, sensors, results: dict[tuple[str, int, str], list]) -> pd.DataFrame:
    """The long table: one row per (field_id, sensor, year the sensor has); a missing result is a null row."""
    field_ids = sorted(str(f) for f in field_ids)
    sensors = sorted(set(sensors))
    rows = []
    for fid in field_ids:
        for sensor in sensors:
            for year in sensor_years(sensor, years):
                rows.append(
                    {"field_id": fid, "year": year, "sensor": sensor,
                     **row_from_sums(sensor, year, results.get((fid, year, sensor)))}
                )
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["field_id"] = df["field_id"].astype(object)
    df["sensor"] = df["sensor"].astype(object)
    df["year"] = df["year"].astype("int64")
    df["n_px"] = df["n_px"].astype("int64")
    df["clear_months"] = df["clear_months"].astype("Int64")
    for col in ["n_clear", "n_bare", "bare_px_frac", "bare_days", *BAND_COLUMNS]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df["n_clear"] = df["n_clear"].round(3)
    df["n_bare"] = df["n_bare"].round(3)
    df["bare_px_frac"] = df["bare_px_frac"].round(4)
    df["bare_days"] = df["bare_days"].round(1)
    df[BAND_COLUMNS] = df[BAND_COLUMNS].round(5)
    return df


def check_table(df: pd.DataFrame) -> list[str]:
    """Every way `df` breaks the contract in the module docstring (empty list = valid)."""
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        return [f"missing column(s): {', '.join(missing)}"]
    problems = []
    if df.duplicated(["field_id", "year", "sensor"]).any():
        problems.append("duplicate (field_id, year, sensor) rows")
    if not set(df["sensor"].unique()) <= set(SENSORS):
        problems.append("unknown sensor value")
    seen = df["n_px"] > 0
    for col, lo, hi in [("n_clear", 0, None), ("n_bare", 0, None), ("bare_px_frac", 0, 1), ("bare_days", 0, 366)]:
        s = df[col]
        if ((s < lo) | ((s > hi) if hi is not None else False)).any():
            problems.append(f"{col}: values outside the allowed range")
    if df.loc[seen, ["n_clear", "n_bare", "bare_px_frac"]].isna().any().any():
        problems.append("n_clear / n_bare / bare_px_frac null on a field that has pixels")
    if df.loc[~seen, ["n_clear", "n_bare", "bare_px_frac", "bare_days", *BAND_COLUMNS]].notna().any().any():
        problems.append("values on a field that has no pixel")
    if (df["n_bare"] > df["n_clear"] + 1e-6).any():
        problems.append("n_bare larger than n_clear")
    for sensor in df["sensor"].unique():
        part = df[(df["sensor"] == sensor) & seen]
        own = SENSOR_BANDS[sensor]
        bands = part[own]
        if ((bands <= 0) | (bands > 1)).any().any():
            problems.append(f"{sensor}: reflectance outside (0, 1]")
        has_bare = part["n_bare"] > 0
        if bands[has_bare].isna().any().any() or bands[~has_bare].notna().any().any():
            problems.append(f"{sensor}: band values must be present exactly when n_bare > 0")
        others = [b for b in BAND_COLUMNS if b not in own]
        if others and part[others].notna().any().any():
            problems.append(f"{sensor}: values in bands the sensor does not have")
    return problems


def summarize(df: pd.DataFrame) -> list[str]:
    lines = [f"rows: {len(df):,}  ({df['field_id'].nunique():,} fields; too thin after the {BUFFER_M:.0f} m buffer: "
             f"{int(df.groupby('field_id')['n_px'].max().eq(0).sum()):,})"]
    lines.append("sensor   years      field-years  no bare view  null bare_days  median n_clear  n_bare  bare_days")
    for sensor, part in df.groupby("sensor"):
        seen = part[part["n_px"] > 0]
        if seen.empty:
            lines.append(f"{sensor:8s} no field with pixels")
            continue
        lines.append(
            f"{sensor:8s} {part['year'].min()}-{part['year'].max()}  {len(seen):11,}  "
            f"{(seen['n_bare'] == 0).mean():11.1%}  {seen['bare_days'].isna().mean():13.1%}  "
            f"{seen['n_clear'].median():14.1f}  {seen['n_bare'].median():6.1f}  {seen['bare_days'].median():9.0f}"
        )
    return lines


METHOD_KEY = b"soil_bare_method"


def table_method(sensors) -> dict:
    """What a table was computed with: the request signature per sensor and the bare_days rule."""
    return {
        "request": {s: request_signature(s) for s in sorted(set(sensors))},
        "snow_screen": NDSI_MAX is not None,
        "bare_days": {"min_clear_per_month": MIN_CLEAR_PER_MONTH, "min_months": MIN_MONTHS},
    }


def write_table(df: pd.DataFrame, out: Path, sensors) -> None:
    """Atomic Parquet write; the method (table_method) is stored in the file's own metadata."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata[METHOD_KEY] = json.dumps(table_method(sensors), sort_keys=True).encode()
    tmp = out.with_name(out.name + ".part")
    pq.write_table(table.replace_schema_metadata(metadata), tmp)
    os.replace(tmp, out)


def read_table_method(path: Path) -> dict | None:
    """The method stored in a table written by this module (None if the file has none)."""
    import pyarrow.parquet as pq

    raw = (pq.read_schema(path).metadata or {}).get(METHOD_KEY)
    return json.loads(raw) if raw else None


def read_field_ids(path: Path) -> list[str]:
    """Field ids from a text file: one per line, blank lines and # comments ignored."""
    ids = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    if not ids:
        raise InputError(f"{path} lists no field id")
    return ids


def build(
    fields_path: Path,
    out_path: Path,
    years: list[int],
    sensors=SENSORS,
    *,
    field_ids: list[str] | None = None,
    max_fields: int | None = None,
    force: bool = False,
    cache_dir: Path | None = None,
    compute=None,
    chunk_size: int = CHUNK_SIZE,
    workers: int = WORKERS,
    profile: bool = True,
    log_path: Path | None = None,
    max_eecu_hours: float | None = None,
    estimate_only: bool = False,
) -> pd.DataFrame | None:
    """Read fields, get every (field, year, sensor) from cache or Earth Engine, check and write.

    `estimate_only`: print what the run would ask of Earth Engine (estimate_cost) and return None
    without contacting it. `max_eecu_hours`: stop sending requests once this run has used that much.
    """
    started = time.monotonic()
    log = RunLog(log_path)
    fields = load_fields(fields_path)
    fields = fields.sort_values("field_id").reset_index(drop=True)
    if field_ids is not None:
        wanted = set(field_ids)
        unknown = sorted(wanted - set(fields["field_id"]))
        if unknown:
            raise InputError(f"{len(unknown)} field id(s) are not in {fields_path}, e.g. {unknown[0]}")
        fields = fields[fields["field_id"].isin(wanted)].reset_index(drop=True)
    if max_fields is not None:  # a compact block of neighbours, so a test run is few small requests
        fields = fields.iloc[np.sort(sat.spatial_order(fields)[:max_fields])].reset_index(drop=True)
    sensors = [s for s in SENSORS if s in set(sensors)]
    chunks, lost = make_soil_chunks(fields, chunk_size)
    log(f"{len(fields):,} fields from {fields_path}; years {years[0]}-{years[-1]}; sensors {sensors}")
    log(f"{len(chunks)} chunks; {len(lost)} field(s) too thin for the {BUFFER_M:.0f} m inward buffer: {lost[:20]}")
    estimate = estimate_lines(estimate_cost(chunks, years, sensors, cache_dir or CACHE_DIR, force=force))
    for line in estimate:
        log(line)
    if estimate_only:
        print("\n".join([f"{len(fields):,} fields, years {years[0]}-{years[-1]}, sensors {', '.join(sensors)}; nothing was requested", *estimate]))
        return None
    if max_eecu_hours is not None and not profile and compute is None:
        raise InputError("a budget needs to know what requests cost: do not combine --max-eecu-hours with --no-profile")

    stats = Stats()
    engines: list[BareSoilEngine] = []

    def engine_factory():
        engines.append(BareSoilEngine(profile=profile))
        return engines[-1]

    try:
        results = collect(
            chunks, years, sensors, cache_dir or CACHE_DIR, compute=compute, force=force, workers=workers,
            stats=stats, log=log, engine_factory=engine_factory,
            max_eecu_s=None if max_eecu_hours is None else max_eecu_hours * 3600.0,
        )
    finally:
        costs = [c for e in engines for c in e.costs]
        for c in costs:
            eecu = "n/a" if c["eecu_s"] is None else f"{c['eecu_s']:.1f}"
            log(f"request {c['sensor']} {c['year']} fields={c['fields']} seconds={c['seconds']:.1f} eecu_s={eecu}")
        if costs:
            slowest = max(c["seconds"] for c in costs)
            log(
                f"this run: {len(costs)} requests, {spent_eecu_s(costs) / 3600:.3f} EECU-hours, slowest {slowest:.0f} s; "
                f"{stats.retries} retries, {stats.splits} chunk splits"
            )
    df = assemble(fields["field_id"], years, sensors, results)
    problems = check_table(df)
    if problems:
        raise SatelliteError("the bare-soil table failed its own checks:\n" + "\n".join(f"  - {p}" for p in problems))
    write_table(df, out_path, sensors)
    log(f"wrote {out_path}")

    lines = summarize(df)
    if NDSI_MAX is None:
        lines.append("NOTE: computed WITHOUT the snow / bright-cloud screen (--no-snow-screen); see NDSI_MAX")
    cost_line = ""
    for sensor in sensors:
        known = [c["eecu_s"] for c in costs if c["sensor"] == sensor and c["eecu_s"] is not None]
        if known:
            cost_line += f"  {sensor}: {sum(known) / 3600:.3f} EECU-h in {len(known)} profiled requests ({np.mean(known):.0f} EECU-s each)"
    lines.append(
        f"Earth Engine requests: {stats.requests} ({stats.retries} retries, {stats.splits} chunk splits); "
        f"cached results used: {stats.cache_hits}; took {time.monotonic() - started:.0f} s"
    )
    if cost_line:
        lines.append("cost:" + cost_line)
    lines.append(f"wrote {out_path}" + (f"; full log: {log_path}" if log_path else ""))
    for line in lines:
        log(line)
    print("\n".join(lines))
    return df


# --------------------------------------------------------------------------- CLI


def not_authenticated_message(exc: BaseException) -> str:
    """The three lines printed when Earth Engine cannot be used (exit code 2)."""
    text = " ".join(str(exc).split())
    reason = text.split("Earth Engine said:", 1)[1].strip() if "Earth Engine said:" in text else text
    return (
        "ERROR: Google Earth Engine is not available, so nothing new was computed (cached results are kept).\n"
        "  Fix: run  .venv/bin/earthengine authenticate  then  .venv/bin/earthengine set_project <project-id>"
        "  (or export EE_PROJECT=<project-id>)\n"
        f"  Earth Engine said: {reason[:300]}"
    )


def positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not value > 0:
        raise argparse.ArgumentTypeError(f"{text!r}: expected a number above 0")
    return value


def parse_sensors(text: str) -> list[str]:
    names = [s.strip().lower() for s in text.split(",") if s.strip()]
    if not names or any(s not in SENSORS for s in names):
        raise argparse.ArgumentTypeError(f"{text!r}: sensors are {', '.join(SENSORS)} (comma-separated)")
    return [s for s in SENSORS if s in names]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fields", type=Path, default=REAL_PATH, help="fields GeoParquet (default: data/fields.parquet)")
    parser.add_argument("--out", type=Path, default=BARE_OBS_PATH, help="output (default: data/soil_bare_obs.parquet)")
    parser.add_argument("--years", type=parse_years, default=list(YEARS), help="e.g. 2016-2024 (default) or 2022")
    parser.add_argument("--sensors", type=parse_sensors, default=list(SENSORS), help="landsat,s2 (default) or one of them")
    parser.add_argument("--field-ids-file", type=Path, default=None, help="text file with one field_id per line: only these fields")
    parser.add_argument("--max-fields", type=positive_int, default=None, help="only N neighbouring fields (for testing)")
    parser.add_argument("--force", action="store_true", help="ignore cached results and ask Earth Engine again")
    parser.add_argument("--workers", type=positive_int, default=WORKERS, help=f"concurrent requests (default {WORKERS})")
    parser.add_argument("--no-profile", action="store_true", help="do not ask Earth Engine what each request cost")
    parser.add_argument(
        "--estimate", action="store_true",
        help="print how many requests and roughly how many EECU-hours the run would take, then stop (no Earth Engine call)",
    )
    parser.add_argument(
        "--max-eecu-hours", type=positive_float, default=None,
        help="stop sending requests once this run has used this many EECU-hours (finished ones are cached; exit code 3)",
    )
    parser.add_argument(
        "--no-snow-screen", action="store_true",
        help="leave out the NDSI snow / bright-cloud screen: only to reproduce a table made before it existed",
    )
    args = parser.parse_args(argv)
    log_path = new_log_path()
    global NDSI_MAX
    screen = NDSI_MAX
    if args.no_snow_screen:
        NDSI_MAX = None
    try:
        field_ids = read_field_ids(args.field_ids_file) if args.field_ids_file else None
        build(
            args.fields, args.out, args.years, args.sensors, field_ids=field_ids, max_fields=args.max_fields,
            force=args.force, workers=args.workers, profile=not args.no_profile, log_path=log_path,
            max_eecu_hours=args.max_eecu_hours, estimate_only=args.estimate,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: file not found: {exc}. Run `make pipeline` first, or check --fields / --field-ids-file.", file=sys.stderr)
        return 2
    except (SchemaError, InputError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except EarthEngineUnavailable as exc:
        print(not_authenticated_message(exc), file=sys.stderr)
        return 2
    except BudgetExceeded as exc:
        print(f"STOPPED: {exc}\n  full log: {log_path}", file=sys.stderr)
        return 3
    except SatelliteError as exc:
        print(f"ERROR: {exc}\n  full log: {log_path}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted: finished chunks are cached, run the same command again to resume.", file=sys.stderr)
        return 130
    finally:
        NDSI_MAX = screen
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
