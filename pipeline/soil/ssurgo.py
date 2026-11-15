"""Step 5.2 — soil organic matter per field from SSURGO (USDA Soil Data Access).

    from pipeline.soil import organic_matter_pct
    om = organic_matter_pct(fields)     # Series of float (percent), same index as `fields`
    om.attrs["provenance"]              # endpoint, queries, counts, coverage statistics

    python pipeline/soil/ssurgo.py [fields.parquet] [--spot-check 8]    # default data/fields_fake.parquet

How it works
------------
1. **Tiles.** The lon/lat plane is cut into a fixed 0.1 x 0.1 degree grid aligned to round
   coordinates (tile (ix, iy) covers lon [ix/10, (ix+1)/10], lat [iy/10, (iy+1)/10]).
   Every tile touched by a field's bounding box is fetched from Soil Data Access (SDA)
   with ONE spatial query that returns every SSURGO map unit polygon intersecting the
   tile, whole and unclipped, as WKT. Each tile is cached as
   `data/raw/ssurgo/mupolygon_<lon>_<lat>_0.1deg_<sha1 of the query>.parquet`, so a rerun
   sends no HTTP requests, another set of fields only fetches the tiles it is missing,
   and changing the query invalidates the cache by itself. Polygons that straddle tiles
   come back more than once and are de-duplicated on `mupolygonkey`. There is never one
   request per field.
2. **Attributes.** One query (per 400 map units) returns map unit -> component -> horizon
   rows for every map unit seen in those tiles; cached as `muattr_*.parquet`, and only
   map units that are not cached yet are requested.
3. **Map unit organic matter** (`mapunit_organic_matter`). Per component, take `om_r` of
   the surface horizon; the map unit value is the `comppct_r`-weighted mean over the
   components that have a value, with the weights renormalised. Map units where no
   component has a value (water, pits, riverwash, ...) are null.
4. **Field organic matter** (`field_organic_matter`). Fields and map unit polygons are
   projected to EPSG:5070 (equal area), made valid, intersected through a spatial index,
   and the field value is the intersection-area-weighted mean of the map unit values,
   ignoring null map units and renormalising. NaN only when no part of the field lies on
   a map unit with a value. The covered share of each field is kept (`om_coverage`) and
   its distribution goes into the provenance; `min_coverage=0.5` (say) turns thinly
   covered fields into NaN instead, the default 0 keeps every field with any real overlap.

`python pipeline/soil_bench.py` repeats the scale test (6,000 synthetic fields over the
whole county) and `--spot-check N` compares N fields with a calculation done entirely by
SDA's own SQL Server (`sda_field_organic_matter`).

Choices that are worth knowing about
------------------------------------
* **Surface horizon** = the horizon with the smallest `hzdept_r` (ties: smaller
  `hzdepb_r`). If that horizon is an organic "O" layer (forest litter, duff) and a
  mineral horizon starts within 20 cm of the surface, the mineral horizon is used
  instead: the litter layer is not the soil a crop grows in, and its 30-90 % organic
  matter would swamp the average. Where the organic material is thicker than that
  (a real organic soil), the O horizon is the surface and is used as is. In the Finney
  County tiles there is not a single O horizon and every component's first horizon starts
  at 0 cm, so here the rule is never triggered (the number of skips is in the provenance).
* If the surface horizon has no `om_r` the component counts as "no data"; a deeper
  horizon is never substituted, because subsoil organic matter is systematically lower.
* **All components**, major and minor, are used (default `components="all"`), each
  weighted by `comppct_r`: the percentages are the surveyors' estimate of what is really
  inside the polygon, in this county the minor components do carry horizon data, and it
  is the same idea as the "weighted average" aggregation of NRCS's own soil viewers.
  `components="major"` restricts the average to `majcompflag = 'Yes'`; over 6,000 test
  fields the two differ by -0.11 to +0.13 percentage points (mean -0.01).
* `om_r` is the survey's representative value for a soil series, not a measurement of
  the field: every field on the same map unit gets the same number, and it does not
  change with management. It describes the soil a field sits on, not what a rotation
  did to it.
* SDA is retried with exponential backoff on timeouts, HTTP errors and non-JSON bodies
  (it sometimes answers with an HTML error page). A tile or attribute query that keeps
  failing raises `SoilDataError` (no new tile is started after that, tiles that did
  arrive stay cached); nothing is ever quietly turned into NaN, and a result with no
  value for any field also raises.
* The cache has no expiry. SSURGO is refreshed about once a year; pass `refresh=True`
  (CLI `--refresh`) or delete `data/raw/ssurgo/` to pick up a new version. The survey
  area versions that were used are listed in the provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # allow `python pipeline/soil/ssurgo.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
import shapely  # noqa: E402

from pipeline.common import DATA_DIR, EQUAL_AREA_CRS, RAW_DIR, USER_AGENT, log  # noqa: E402

SDA_URL = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
CACHE_DIR = RAW_DIR / "ssurgo"

TILES_PER_DEGREE = 10  # 0.1 degree tiles (about 8.8 km x 11.1 km at 38 N)
MAX_TILES = 2500  # refuse absurd extents (wrong CRS, a stray field on another continent)
MAX_WORKERS = 3  # concurrent tile requests; SDA is a shared public service
MUKEY_CHUNK = 400  # map units per attribute query
LITTER_MAX_CM = 20.0  # an O layer over mineral soil that starts within this depth is litter
COVERAGE_EPS = 1e-9  # a covered share below this is floating-point noise, not coverage

# One tile: every map unit polygon that intersects the tile rectangle, unclipped.
TILE_SQL = (
    "SELECT P.mupolygonkey, P.mukey, P.areasymbol, P.musym, P.mupolygongeo.STAsText() AS wkt "
    "FROM SDA_Get_Mupolygonkey_from_intersection_with_WktWgs84('{wkt}') AS K "
    "INNER JOIN mupolygon AS P ON P.mupolygonkey = K.mupolygonkey"
)

# Map unit -> component -> horizon. LEFT JOINs so that map units without components and
# components without horizons (water, pits, rock outcrop) still come back as a row.
ATTR_SQL = (
    "SELECT mu.mukey, mu.musym, mu.muname, mu.mukind, l.areasymbol, "
    "c.cokey, c.compname, c.compkind, c.comppct_r, c.majcompflag, "
    "ch.chkey, ch.hzname, ch.desgnmaster, ch.hzdept_r, ch.hzdepb_r, ch.om_r "
    "FROM mapunit AS mu "
    "INNER JOIN legend AS l ON l.lkey = mu.lkey "
    "LEFT JOIN component AS c ON c.mukey = mu.mukey "
    "LEFT JOIN chorizon AS ch ON ch.cokey = c.cokey "
    "WHERE mu.mukey IN ({mukeys}) "
    "ORDER BY mu.mukey, c.cokey, ch.hzdept_r"
)
ATTR_STR_COLUMNS = [
    "mukey", "musym", "muname", "mukind", "areasymbol", "cokey", "compname", "compkind",
    "majcompflag", "chkey", "hzname", "desgnmaster",
]  # fmt: skip
ATTR_NUM_COLUMNS = ["comppct_r", "hzdept_r", "hzdepb_r", "om_r"]
ATTR_COLUMNS = ATTR_STR_COLUMNS + ATTR_NUM_COLUMNS

SURVEY_SQL = (
    "SELECT areasymbol, areaname, saversion, saverest FROM sacatalog "
    "WHERE areasymbol IN ({areasymbols}) ORDER BY areasymbol"
)

# Independent check, computed entirely by SDA's SQL Server for ONE field outline: the
# intersection area of every map unit with the field and the map unit's organic matter
# (same definition as above, written in SQL). Areas are planar square degrees, which
# across one field are proportional to true area to about 1 part in 10,000.
SPOT_SQL = (
    "WITH hit AS ("
    " SELECT P.mukey, P.mupolygongeo.MakeValid().STIntersection("
    "geometry::STGeomFromText('{wkt}', 4326)).STArea() AS a"
    " FROM SDA_Get_Mupolygonkey_from_intersection_with_WktWgs84('{wkt}') AS K"
    " INNER JOIN mupolygon AS P ON P.mupolygonkey = K.mupolygonkey"
    "), mua AS ("
    " SELECT mukey, SUM(a) AS a FROM hit GROUP BY mukey"
    "), top1 AS ("
    " SELECT c.mukey, c.cokey, c.comppct_r,"
    " (SELECT TOP 1 ch.chkey FROM chorizon AS ch WHERE ch.cokey = c.cokey"
    " ORDER BY ch.hzdept_r, ch.hzdepb_r, ch.chkey) AS top_chkey,"
    " (SELECT TOP 1 ch.chkey FROM chorizon AS ch WHERE ch.cokey = c.cokey"
    " AND NOT (ISNULL(ch.desgnmaster, '') LIKE 'O%' OR ISNULL(ch.hzname, '') LIKE 'O%')"
    " AND ch.hzdept_r <= {litter_cm}"
    " ORDER BY ch.hzdept_r, ch.hzdepb_r, ch.chkey) AS mineral_chkey"
    " FROM component AS c WHERE c.mukey IN (SELECT mukey FROM mua)"
    "), surf AS ("
    " SELECT t.mukey, t.comppct_r, ch.om_r FROM top1 AS t"
    " INNER JOIN chorizon AS ch ON ch.chkey = ISNULL(t.mineral_chkey, t.top_chkey)"
    "), muom AS ("
    " SELECT mukey, SUM(CAST(comppct_r AS float) * om_r) / SUM(CAST(comppct_r AS float)) AS om"
    " FROM surf WHERE om_r IS NOT NULL AND comppct_r > 0 GROUP BY mukey"
    ") "
    "SELECT mua.mukey, mu.musym, mu.muname, mua.a AS area_deg2, muom.om AS om_pct "
    "FROM mua INNER JOIN mapunit AS mu ON mu.mukey = mua.mukey "
    "LEFT JOIN muom ON muom.mukey = mua.mukey ORDER BY mua.a DESC"
)


class SoilDataError(RuntimeError):
    """SSURGO data could not be obtained (SDA kept failing) or yielded no usable value."""


# --------------------------------------------------------------------------- SDA client


class _Retryable(Exception):
    pass


class _Fatal(Exception):
    pass


# A 400 with one of these is a bug in the query, not a hiccup: retrying cannot help.
_FATAL_RE = re.compile(
    r"Incorrect syntax|Invalid column name|Invalid object name|not allowed|Must declare|Ambiguous column",
    re.IGNORECASE,
)
_SERVICE_EXCEPTION_RE = re.compile(r"<ServiceException>\s*(.*?)\s*</ServiceException>", re.DOTALL)


def _snippet(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _parse_sda_response(resp) -> list[list]:
    """Rows of an SDA `JSON+COLUMNNAME` answer (first row = column names; [] = no rows)."""
    if resp.status_code != 200:
        found = _SERVICE_EXCEPTION_RE.search(resp.text or "")
        message = html.unescape(found.group(1)) if found else _snippet(resp.text or "")
        if resp.status_code == 400 and _FATAL_RE.search(message):
            raise _Fatal(message)
        raise _Retryable(f"HTTP {resp.status_code}: {_snippet(message)}")
    try:
        payload = resp.json()
    except ValueError:
        raise _Retryable(f"HTTP 200 but the body is not JSON: {_snippet(resp.text or '', 100)!r}") from None
    if payload == {}:  # SDA's way of saying "the query returned no rows"
        return []
    table = payload.get("Table") if isinstance(payload, dict) else None
    if not isinstance(table, list) or not table or not isinstance(table[0], list):
        raise _Retryable(f"unexpected JSON shape: {_snippet(json.dumps(payload), 100)}")
    return table


class SDAClient:
    """Soil Data Access tabular service: one T-SQL query in, one table out, with retries.

    `post` and `sleep` are injectable so the tests never touch the network.
    """

    def __init__(
        self,
        url: str = SDA_URL,
        *,
        timeout: tuple[float, float] = (15, 90),  # connect, read (a tile normally answers in about a second)
        retries: int = 5,
        backoff: float = 2.0,
        post=None,
        sleep=None,
        say=None,
    ):
        self.url = url
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.backoff = backoff
        self._post = post or requests.post
        self._sleep = sleep or time.sleep
        self._say = say or (lambda msg: None)
        self._lock = threading.Lock()
        self.stats = {"queries": 0, "http_requests": 0, "retries": 0}

    def _count(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    def query(self, sql: str, what: str = "SDA query") -> pd.DataFrame:
        """Run `sql`; return the rows as strings / None (an empty frame when there are none).

        Retries with exponential backoff, then raises SoilDataError naming `what`.
        """
        self._count("queries")
        last = "no attempt was made"
        for attempt in range(1, self.retries + 1):
            self._count("http_requests")
            try:
                resp = self._post(
                    self.url,
                    data={"query": sql, "format": "JSON+COLUMNNAME"},
                    timeout=self.timeout,
                    headers={"User-Agent": USER_AGENT},
                )
                table = _parse_sda_response(resp)
            except _Fatal as exc:
                raise SoilDataError(
                    f"{what}: Soil Data Access rejected the query ({exc}). Query: {_snippet(sql, 300)}"
                ) from None
            except (_Retryable, requests.RequestException, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}" if not isinstance(exc, _Retryable) else str(exc)
                if attempt < self.retries:
                    wait = self.backoff * 2 ** (attempt - 1)
                    self._count("retries")
                    self._say(f"  {what}: attempt {attempt}/{self.retries} failed ({_snippet(last)}); retrying in {wait:g}s")
                    self._sleep(wait)
                continue
            if not table:
                return pd.DataFrame()
            return pd.DataFrame(table[1:], columns=table[0])
        raise SoilDataError(
            f"{what}: Soil Data Access ({self.url}) failed {self.retries} time(s) in a row; "
            f"last error: {_snippet(last, 300)}. Nothing was written for this request; "
            "rerun later (everything already fetched is cached)."
        )


# --------------------------------------------------------------------------- tiles


def _fmt(value: float) -> str:
    """-100.9 -> '-100.9', 38.0 -> '38' (stable text for file names and queries)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _sha(text: str, length: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def tile_bounds(ix: int, iy: int, tiles_per_degree: int = TILES_PER_DEGREE) -> tuple[float, float, float, float]:
    """(min lon, min lat, max lon, max lat) of tile (ix, iy)."""
    n = tiles_per_degree
    return ix / n, iy / n, (ix + 1) / n, (iy + 1) / n


def tile_wkt(ix: int, iy: int, tiles_per_degree: int = TILES_PER_DEGREE) -> str:
    x0, y0, x1, y1 = (_fmt(v) for v in tile_bounds(ix, iy, tiles_per_degree))
    return f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


def tile_query(ix: int, iy: int, tiles_per_degree: int = TILES_PER_DEGREE) -> str:
    return TILE_SQL.format(wkt=tile_wkt(ix, iy, tiles_per_degree))


def tile_path(cache_dir: Path, ix: int, iy: int, tiles_per_degree: int = TILES_PER_DEGREE) -> Path:
    x0, y0, _, _ = tile_bounds(ix, iy, tiles_per_degree)
    name = f"mupolygon_{_fmt(x0)}_{_fmt(y0)}_{_fmt(1 / tiles_per_degree)}deg_{_sha(tile_query(ix, iy, tiles_per_degree))}"
    return Path(cache_dir) / f"{name}.parquet"


def tiles_for_bounds(bounds, tiles_per_degree: int = TILES_PER_DEGREE) -> list[tuple[int, int]]:
    """Sorted (ix, iy) of every grid tile touched by any of the (minx, miny, maxx, maxy) boxes.

    The grid is fixed (aligned to multiples of 1/tiles_per_degree degree), so the answer
    for one field never depends on which other fields are in the set.
    """
    b = np.asarray(bounds, dtype=float).reshape(-1, 4)
    b = b[np.isfinite(b).all(axis=1)]
    if len(b) == 0:
        return []
    idx = np.floor(b * tiles_per_degree).astype(np.int64)  # columns: ix0, iy0, ix1, iy1
    tiles: set[tuple[int, int]] = set()
    for ix0, iy0, ix1, iy1 in np.unique(idx, axis=0).tolist():
        if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > MAX_TILES:
            raise ValueError(
                f"one field spans {(ix1 - ix0 + 1) * (iy1 - iy0 + 1)} tiles; are the geometries really lon/lat fields?"
            )
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                tiles.add((ix, iy))
    return sorted(tiles)


def _write_parquet_atomic(frame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


_TILE_COLUMNS = ["mupolygonkey", "mukey", "areasymbol", "musym"]


def _tile_frame(raw: pd.DataFrame, what: str) -> gpd.GeoDataFrame:
    """SDA rows (mupolygonkey, mukey, areasymbol, musym, wkt) -> GeoDataFrame in EPSG:4326."""
    if raw.empty:
        data = {c: pd.Series([], dtype="str") for c in _TILE_COLUMNS}
        return gpd.GeoDataFrame(data, geometry=gpd.GeoSeries([], crs="EPSG:4326"))
    missing = [c for c in [*_TILE_COLUMNS, "wkt"] if c not in raw.columns]
    if missing:
        raise SoilDataError(f"{what}: SDA answer has no column(s) {missing} (got {list(raw.columns)})")
    try:
        geometry = shapely.from_wkt(raw["wkt"].to_numpy(dtype=object))
    except Exception as exc:  # GEOSException / TypeError on a damaged WKT string
        raise SoilDataError(f"{what}: SDA returned a geometry that cannot be parsed ({exc})") from exc
    frame = gpd.GeoDataFrame(
        {c: raw[c].astype(str).to_numpy(dtype=object) for c in _TILE_COLUMNS},
        geometry=gpd.GeoSeries(geometry, crs="EPSG:4326"),
    )
    blank = frame.geometry.isna() | frame.geometry.is_empty
    return frame[~blank].reset_index(drop=True)


def load_mupolygons(
    tiles: list[tuple[int, int]],
    client: SDAClient,
    cache_dir: Path = CACHE_DIR,
    *,
    tiles_per_degree: int = TILES_PER_DEGREE,
    max_workers: int = MAX_WORKERS,
    refresh: bool = False,
    say=lambda msg: None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Map unit polygons (EPSG:4326, unique `mupolygonkey`) for `tiles`, from cache or SDA.

    Raises SoilDataError if any tile cannot be fetched; tiles fetched before the failure
    stay cached, so a rerun only asks for the rest.
    """
    cache_dir = Path(cache_dir)
    frames: dict[tuple[int, int], gpd.GeoDataFrame] = {}
    todo: list[tuple[int, int]] = []
    for tile in tiles:
        path = tile_path(cache_dir, *tile, tiles_per_degree)
        if path.exists() and not refresh:
            try:
                frames[tile] = gpd.read_parquet(path)
                continue
            except Exception as exc:  # truncated / foreign file: fetch it again
                say(f"  cache file {path.name} is unreadable ({_snippet(exc, 80)}); fetching it again")
        todo.append(tile)
    n_cached = len(frames)
    if todo:
        say(f"ssurgo: fetching {len(todo)} tile(s) from Soil Data Access ({n_cached} already cached)")

    gave_up = threading.Event()  # set as soon as one tile has failed for good

    def fetch(tile: tuple[int, int]) -> pd.DataFrame | None:
        if gave_up.is_set():
            return None  # do not start new requests once the run is known to fail
        x0, y0, x1, y1 = tile_bounds(*tile, tiles_per_degree)
        what = f"map unit polygons for tile lon {_fmt(x0)}..{_fmt(x1)}, lat {_fmt(y0)}..{_fmt(y1)}"
        try:
            return client.query(tile_query(*tile, tiles_per_degree), what=what)
        except SoilDataError:
            gave_up.set()
            raise

    if todo:
        failure: SoilDataError | None = None
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as pool:
            futures = {pool.submit(fetch, tile): tile for tile in todo}
            for future in as_completed(futures):
                tile = futures[future]
                try:
                    raw = future.result()
                    if raw is None:  # skipped after an earlier failure
                        continue
                    x0, y0, _, _ = tile_bounds(*tile, tiles_per_degree)
                    frame = _tile_frame(raw, f"tile {_fmt(x0)},{_fmt(y0)}")
                except SoilDataError as exc:
                    gave_up.set()
                    failure = failure or exc  # tiles already in flight are still kept below
                    continue
                _write_parquet_atomic(frame, tile_path(cache_dir, *tile, tiles_per_degree))
                frames[tile] = frame
                done = len(frames) - n_cached
                if done % 10 == 0 or done == len(todo):
                    say(f"  {done}/{len(todo)} tiles fetched")
        if failure is not None:
            raise failure  # the caller decides what to do; fetched tiles stay cached

    empty_tiles = [t for t in tiles if len(frames[t]) == 0]
    parts = [frames[t] for t in tiles if len(frames[t])]
    n_rows = sum(len(p) for p in parts)
    if parts:
        polygons = pd.concat(parts, ignore_index=True)
        polygons = polygons.drop_duplicates("mupolygonkey", ignore_index=True)
        polygons = gpd.GeoDataFrame(polygons, geometry="geometry", crs="EPSG:4326")
    else:
        polygons = _tile_frame(pd.DataFrame(), "no tiles")
    polygons["mukey"] = polygons["mukey"].astype(str)
    info = {
        "n_tiles": len(tiles),
        "tiles_from_cache": n_cached,
        "tiles_fetched": len(todo),
        "empty_tiles": [list(tile_bounds(*t, tiles_per_degree)) for t in empty_tiles],
        "polygon_rows_in_tiles": int(n_rows),
        "mupolygons": int(len(polygons)),
    }
    return polygons, info


# --------------------------------------------------------------------------- attributes

_ATTR_VERSION = _sha(ATTR_SQL, 6)  # cache files of another query version are ignored


def _normalise_attributes(raw: pd.DataFrame, requested: list[str]) -> pd.DataFrame:
    """Typed attribute rows; map units SDA did not return get an all-null placeholder row."""
    frame = pd.DataFrame(index=range(len(raw)))
    for col in ATTR_STR_COLUMNS:
        values = raw[col].to_numpy(dtype=object) if col in raw.columns else [None] * len(raw)
        frame[col] = np.array([None if v is None or v != v else str(v) for v in values], dtype=object)
    for col in ATTR_NUM_COLUMNS:
        frame[col] = pd.to_numeric(raw[col], errors="coerce").astype(float).to_numpy() if col in raw.columns else np.nan
    seen = set(frame["mukey"].dropna())
    absent = [k for k in requested if k not in seen]
    if absent:
        filler = pd.DataFrame({c: [None] * len(absent) for c in ATTR_STR_COLUMNS})
        filler["mukey"] = absent
        for col in ATTR_NUM_COLUMNS:
            filler[col] = np.nan
        frame = pd.concat([frame, filler[frame.columns]], ignore_index=True)
    return frame


def load_mapunit_attributes(
    mukeys,
    client: SDAClient,
    cache_dir: Path = CACHE_DIR,
    *,
    refresh: bool = False,
    say=lambda msg: None,
) -> tuple[pd.DataFrame, dict]:
    """Map unit / component / horizon rows for `mukeys`; only uncached map units are requested.

    `refresh=True` drops every cached attribute file first (not only the ones for `mukeys`).
    """
    cache_dir = Path(cache_dir)
    wanted = sorted({str(k) for k in mukeys}, key=lambda k: (len(k), k))
    bad = [k for k in wanted if not k.isdigit()]
    if bad:
        raise SoilDataError(f"map unit keys must be integers, got {bad[:5]}")
    frames: list[pd.DataFrame] = []
    pattern = f"muattr_{_ATTR_VERSION}_*.parquet"
    for path in sorted(cache_dir.glob(pattern)):
        if refresh:
            path.unlink()
            continue
        try:
            frames.append(pd.read_parquet(path))
        except Exception as exc:
            say(f"  cache file {path.name} is unreadable ({_snippet(exc, 80)}); ignoring it")
    have: set[str] = set()
    for frame in frames:
        have.update(frame["mukey"].astype(str))
    missing = [k for k in wanted if k not in have]
    n_queries = 0
    for start in range(0, len(missing), MUKEY_CHUNK):
        chunk = missing[start : start + MUKEY_CHUNK]
        sql = ATTR_SQL.format(mukeys=", ".join(chunk))
        raw = client.query(sql, what=f"component/horizon attributes for {len(chunk)} map unit(s)")
        frame = _normalise_attributes(raw, chunk)
        _write_parquet_atomic(frame, cache_dir / f"muattr_{_ATTR_VERSION}_{_sha(sql)}.parquet")
        frames.append(frame)
        n_queries += 1
    if n_queries:
        say(f"ssurgo: fetched attributes for {len(missing)} map unit(s) in {n_queries} query(ies)")
    if frames:
        attrs = pd.concat([f[ATTR_COLUMNS] for f in frames], ignore_index=True)
        attrs["mukey"] = attrs["mukey"].astype(str)
        attrs = attrs[attrs["mukey"].isin(set(wanted))]
        attrs = attrs.drop_duplicates(["mukey", "cokey", "chkey"], ignore_index=True)
    else:
        attrs = _normalise_attributes(pd.DataFrame(), [])
    out_of_range = attrs["om_r"].notna() & ((attrs["om_r"] < 0) | (attrs["om_r"] > 100))
    if out_of_range.any():  # cannot be a percentage; treat as "no data" rather than average it in
        attrs.loc[out_of_range, "om_r"] = np.nan
    info = {
        "mukeys": len(wanted),
        "mukeys_from_cache": len(wanted) - len(missing),
        "mukeys_fetched": len(missing),
        "attribute_queries": n_queries,
        "attribute_rows": int(len(attrs)),
        "om_r_out_of_range_set_null": int(out_of_range.sum()),
    }
    return attrs, info


def load_survey_areas(areasymbols, client: SDAClient, cache_dir: Path = CACHE_DIR, *, refresh: bool = False) -> list[dict]:
    """SSURGO version per survey area (provenance only): areasymbol, areaname, saversion, saverest.

    Cached like the attributes: only survey areas that are in no `sacatalog_*.json` yet are requested.
    """
    cache_dir = Path(cache_dir)
    symbols = sorted({str(s) for s in areasymbols if isinstance(s, str) and re.fullmatch(r"[A-Za-z0-9]+", s)})
    known: dict[str, dict] = {}
    for path in sorted(cache_dir.glob("sacatalog_*.json")):
        if refresh:
            path.unlink()
            continue
        try:
            for row in json.loads(path.read_text()):
                known[row["areasymbol"]] = row
        except (ValueError, KeyError, TypeError):
            pass  # unreadable file: the symbols are simply asked for again
    missing = [s for s in symbols if s not in known]
    if missing:
        sql = SURVEY_SQL.format(areasymbols=", ".join(f"'{s}'" for s in missing))
        table = client.query(sql, what="survey area versions")
        rows = [{k: (None if v is None or v != v else str(v)) for k, v in row.items()} for row in table.to_dict("records")]
        rows = [row for row in rows if row.get("areasymbol")]
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"sacatalog_{_sha(sql)}.json"
        tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
        tmp.write_text(json.dumps(rows, indent=1))
        os.replace(tmp, path)
        known.update({row["areasymbol"]: row for row in rows})
    return [known[s] for s in symbols if s in known]


# --------------------------------------------------------------------------- the maths


def _is_organic_layer(desgnmaster, hzname) -> np.ndarray:
    """True for O horizons (master designation 'O', or a name such as Oi / Oe / Oa / 2Oa)."""

    def one(master, name) -> bool:
        if isinstance(master, str) and master.strip().upper().startswith("O"):
            return True
        return isinstance(name, str) and name.strip().lstrip("0123456789^' ").upper().startswith("O")

    return np.array([one(m, n) for m, n in zip(desgnmaster, hzname)], dtype=bool)


def surface_horizons(attrs: pd.DataFrame) -> pd.DataFrame:
    """The horizon that counts as "the surface" for each component (index: cokey).

    Smallest `hzdept_r` wins (ties: smaller `hzdepb_r`, then `chkey`). If that horizon is
    an organic O layer and a mineral horizon starts within LITTER_MAX_CM of the surface,
    the mineral horizon is taken instead (`organic_layer_skipped` is True). Components
    without any horizon row are not in the result. `om_r` may be null: a deeper horizon
    is never substituted.
    """
    cols = ["chkey", "hzname", "desgnmaster", "hzdept_r", "hzdepb_r", "om_r"]
    hz = attrs[attrs["cokey"].notna() & attrs["chkey"].notna()][["cokey", *cols]].copy()
    hz["_organic"] = _is_organic_layer(hz["desgnmaster"].to_numpy(dtype=object), hz["hzname"].to_numpy(dtype=object))
    hz["_top"] = hz["hzdept_r"].astype(float).fillna(np.inf)
    hz["_bottom"] = hz["hzdepb_r"].astype(float).fillna(np.inf)
    hz["_key"] = hz["chkey"].astype(str)
    hz = hz.sort_values(["cokey", "_top", "_bottom", "_key"], kind="stable").reset_index(drop=True)
    hz["_row"] = np.arange(len(hz))
    first = hz.drop_duplicates("cokey", keep="first")
    mineral = hz[~hz["_organic"]].drop_duplicates("cokey", keep="first").set_index("cokey")
    mineral = mineral.reindex(first["cokey"].to_numpy(dtype=object))  # all-NaN row where there is no mineral horizon
    with np.errstate(invalid="ignore"):
        swap = first["_organic"].to_numpy(dtype=bool) & (mineral["_top"].to_numpy(dtype=float) <= LITTER_MAX_CM)
    rows = np.where(swap, mineral["_row"].to_numpy(dtype=float), first["_row"].to_numpy(dtype=float)).astype(np.int64)
    chosen = hz.iloc[rows].set_index("cokey")
    chosen["organic_layer_skipped"] = swap
    chosen["is_organic"] = chosen["_organic"].to_numpy(dtype=bool)
    return chosen[[*cols, "is_organic", "organic_layer_skipped"]]


def mapunit_organic_matter(attrs: pd.DataFrame, components: str = "all") -> pd.DataFrame:
    """Surface organic matter per map unit (index: mukey).

    `om_pct` = sum(comppct_r * om_r) / sum(comppct_r) over the components whose surface
    horizon has an `om_r` (and a positive `comppct_r`); NaN when there is none.
    `components="major"` only uses components flagged `majcompflag = 'Yes'`.

    Other columns: `comppct_with_om` (sum of the percentages that carried a value),
    `comppct_total`, `n_components`, `n_components_with_om`, `organic_layers_skipped`,
    `musym`, `muname`.
    """
    if components not in ("all", "major"):
        raise ValueError(f"components must be 'all' or 'major', got {components!r}")
    attrs = attrs.copy()
    attrs["mukey"] = attrs["mukey"].astype(str)
    units = attrs.drop_duplicates("mukey").set_index("mukey")
    comp = attrs[attrs["cokey"].notna()].drop_duplicates("cokey")[["mukey", "cokey", "comppct_r", "majcompflag"]]
    surf = surface_horizons(attrs)
    comp = comp.merge(surf[["om_r", "organic_layer_skipped"]], left_on="cokey", right_index=True, how="left")
    comp["comppct_r"] = comp["comppct_r"].astype(float)
    if components == "major":
        major = np.array([isinstance(v, str) and v.strip().lower() == "yes" for v in comp["majcompflag"]], dtype=bool)
        comp = comp[major]
    usable = comp["om_r"].notna() & comp["comppct_r"].notna() & (comp["comppct_r"] > 0)
    weight = comp["comppct_r"].where(usable, 0.0)
    skipped = np.array(  # NaN (component without horizons) counts as False
        [isinstance(v, (bool, np.bool_)) and bool(v) for v in comp["organic_layer_skipped"]], dtype=bool
    )
    grouped = pd.DataFrame(
        {
            "mukey": comp["mukey"],
            "wx": weight * comp["om_r"].where(usable, 0.0),
            "w": weight,
            "pct": comp["comppct_r"].fillna(0.0),
            "n": 1,
            "n_om": usable.astype(int),
            "skipped": (skipped & usable.to_numpy(dtype=bool)).astype(int),
        }
    ).groupby("mukey").sum()
    out = pd.DataFrame(index=units.index)
    out["om_pct"] = (grouped["wx"] / grouped["w"].where(grouped["w"] > 0)).reindex(out.index)
    out["comppct_with_om"] = grouped["w"].reindex(out.index).fillna(0.0)
    out["comppct_total"] = grouped["pct"].reindex(out.index).fillna(0.0)
    out["n_components"] = grouped["n"].reindex(out.index).fillna(0).astype(int)
    out["n_components_with_om"] = grouped["n_om"].reindex(out.index).fillna(0).astype(int)
    out["organic_layers_skipped"] = grouped["skipped"].reindex(out.index).fillna(0).astype(int)
    for col in ("musym", "muname"):
        out[col] = units[col] if col in units.columns else None
    return out


def _polygonal(geoms) -> np.ndarray:
    """make_valid, keep only the polygonal part; None where nothing polygonal is left."""
    geoms = np.array(geoms, dtype=object)
    present = ~shapely.is_missing(geoms)
    invalid = present & ~shapely.is_valid(geoms)
    if invalid.any():
        geoms[invalid] = shapely.make_valid(geoms[invalid])
    type_id = shapely.get_type_id(geoms)  # 3 = Polygon, 6 = MultiPolygon, -1 = missing
    for i in np.flatnonzero(present & ~np.isin(type_id, (3, 6))):
        parts = [p for p in shapely.get_parts(geoms[i]) if p.geom_type in ("Polygon", "MultiPolygon")]
        geoms[i] = shapely.union_all(parts) if parts else None
    blank = ~shapely.is_missing(geoms) & shapely.is_empty(geoms)
    geoms[blank] = None
    return geoms


def _geoseries(obj) -> gpd.GeoSeries:
    geom = obj.geometry if isinstance(obj, gpd.GeoDataFrame) else obj
    if not isinstance(geom, gpd.GeoSeries):
        raise TypeError(f"expected a GeoDataFrame or GeoSeries, got {type(obj).__name__}")
    if geom.crs is None:
        raise ValueError("geometries have no CRS; expected EPSG:4326")
    return geom


def field_organic_matter(
    fields, mupolygons: gpd.GeoDataFrame, mapunit_om: pd.Series, min_coverage: float = 0.0
) -> pd.DataFrame:
    """Area-weighted map unit organic matter per field. Pure geometry + arithmetic, no I/O.

    `fields`: GeoDataFrame / GeoSeries (any CRS). `mupolygons`: GeoDataFrame with a `mukey`
    column (any CRS, polygons must not overlap). `mapunit_om`: Series mukey -> percent,
    NaN or absent = the map unit has no value. `min_coverage`: a field whose `om_coverage`
    is below this share gets NaN (default 0: any real overlap with a valued map unit counts).

    Returns a DataFrame with the index of `fields`:
      organic_matter_pct  area-weighted mean over the part of the field with a value; NaN if none
      om_coverage         share of the field's area lying on map units with a value (0..1)
      ssurgo_coverage     share of the field's area lying on any map unit polygon (0..1)
      n_mapunits          distinct map units under the field
      dominant_mukey      the map unit covering most of the field; dominant_share = its share
    """
    field_geom = _geoseries(fields)
    mu_geom = _geoseries(mupolygons)
    f = _polygonal(field_geom.to_crs(EQUAL_AREA_CRS).to_numpy())
    m = _polygonal(mu_geom.to_crs(EQUAL_AREA_CRS).to_numpy())
    mukeys = mupolygons["mukey"].astype(str).to_numpy(dtype=object)
    keep = ~shapely.is_missing(m)
    m, mukeys = m[keep], mukeys[keep]

    n = len(f)
    field_area = shapely.area(f)  # NaN for missing geometries
    if len(m) and n:
        fi, mi = shapely.STRtree(m).query(f, predicate="intersects")
        area = shapely.area(shapely.intersection(f[fi], m[mi]))
    else:
        fi = mi = np.zeros(0, dtype=np.int64)
        area = np.zeros(0)
    positive = area > 0
    pairs = pd.DataFrame({"f": fi[positive], "mukey": mukeys[mi[positive]], "area": area[positive]})
    pairs = pairs.groupby(["f", "mukey"], sort=False, as_index=False)["area"].sum()

    lookup = pd.Series(mapunit_om, dtype=float)
    lookup.index = lookup.index.astype(str)
    om = pairs["mukey"].map(lookup).to_numpy(dtype=float)
    pf = pairs["f"].to_numpy(dtype=np.int64)
    pa = pairs["area"].to_numpy(dtype=float)
    has = ~np.isnan(om)
    any_area = np.bincount(pf, weights=pa, minlength=n)
    om_area = np.bincount(pf[has], weights=pa[has], minlength=n)
    om_sum = np.bincount(pf[has], weights=pa[has] * om[has], minlength=n)

    with np.errstate(divide="ignore", invalid="ignore"):
        om_cov = np.where(field_area > 0, np.minimum(om_area / field_area, 1.0), 0.0)
        any_cov = np.where(field_area > 0, np.minimum(any_area / field_area, 1.0), 0.0)
        value = np.where(om_cov >= max(float(min_coverage), COVERAGE_EPS), om_sum / om_area, np.nan)

    out = pd.DataFrame(
        {
            "organic_matter_pct": value,
            "om_coverage": om_cov,
            "ssurgo_coverage": any_cov,
            "n_mapunits": np.bincount(pf, minlength=n).astype(int),
        }
    )
    # largest share first; an exact tie goes to the smaller mukey so the answer is deterministic
    top = pairs.sort_values(["f", "area", "mukey"], ascending=[True, False, True]).drop_duplicates("f")
    top_f = top["f"].to_numpy(dtype=np.int64)
    dominant = np.full(n, None, dtype=object)
    dominant[top_f] = top["mukey"].to_numpy(dtype=object)
    share = np.zeros(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        share[top_f] = top["area"].to_numpy(dtype=float) / field_area[top_f]
    out["dominant_mukey"] = dominant
    out["dominant_share"] = np.minimum(share, 1.0)
    out.index = field_geom.index
    return out


# --------------------------------------------------------------------------- public API


def _text(value):
    return value if isinstance(value, str) else None


def _dist(values, digits: int = 4) -> dict:
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return {"n": 0}
    q = np.percentile(v, [1, 5, 25, 50, 75, 95, 99])
    keys = ["p01", "p05", "p25", "median", "p75", "p95", "p99"]
    out = {"n": int(len(v)), "min": float(v.min()), "mean": float(v.mean()), "max": float(v.max())}
    out.update({k: float(x) for k, x in zip(keys, q)})
    return {k: (round(x, digits) if isinstance(x, float) else x) for k, x in out.items()}


def _cache_summary(cache_dir: Path) -> dict:
    files = [p for pat in ("mupolygon_*.parquet", "muattr_*.parquet", "sacatalog_*.json") for p in Path(cache_dir).glob(pat)]
    if not files:
        return {"dir": str(cache_dir), "files": 0, "bytes": 0}
    mtimes = [p.stat().st_mtime for p in files]
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {
        "dir": str(cache_dir),
        "files": len(files),
        "bytes": int(sum(p.stat().st_size for p in files)),
        "oldest_file_utc": stamp(min(mtimes)),
        "newest_file_utc": stamp(max(mtimes)),
    }


def field_soil_table(
    fields,
    *,
    cache_dir: str | Path | None = None,
    client: SDAClient | None = None,
    components: str = "all",
    min_coverage: float = 0.0,
    tiles_per_degree: int = TILES_PER_DEGREE,
    max_workers: int = MAX_WORKERS,
    refresh: bool = False,
    verbose: bool = True,
) -> pd.DataFrame:
    """Everything `organic_matter_pct` knows, one row per field (same index as `fields`).

    Columns: see `field_organic_matter`, plus `dominant_muname`. `.attrs["provenance"]`
    is the provenance dict. Raises SoilDataError if SDA keeps failing or if not a single
    field gets a value.

    `components`: "all" (default) or "major". `min_coverage`: fields with a smaller
    `om_coverage` get NaN (default 0 = any overlap with a valued map unit gives a value).
    `refresh`: ignore the cache. `client`: an SDAClient (tests pass a fake one).
    """
    say = log if verbose else (lambda msg: None)
    t_start = time.monotonic()
    cache_dir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    client = client or SDAClient(say=say)
    geom = _geoseries(fields)
    lonlat = geom if geom.crs.to_epsg() == 4326 else geom.to_crs("EPSG:4326")

    bounds = lonlat.bounds.to_numpy(dtype=float)
    bounds = bounds[np.isfinite(bounds).all(axis=1)]
    if len(bounds) and (
        bounds[:, 0].min() < -180 or bounds[:, 2].max() > 180 or bounds[:, 1].min() < -90 or bounds[:, 3].max() > 90
    ):
        raise ValueError("field bounds are not lon/lat degrees; is the CRS set correctly?")
    tiles = tiles_for_bounds(bounds, tiles_per_degree)
    if len(tiles) > MAX_TILES:
        raise ValueError(
            f"the fields touch {len(tiles)} tiles of {1 / tiles_per_degree:g} degrees (limit {MAX_TILES}); "
            "that is not a county-sized extent"
        )
    say(f"ssurgo: {len(geom):,} field(s) touch {len(tiles)} tile(s) of {1 / tiles_per_degree:g} degrees")

    t0 = time.monotonic()
    polygons, tile_info = load_mupolygons(
        tiles, client, cache_dir, tiles_per_degree=tiles_per_degree, max_workers=max_workers, refresh=refresh, say=say
    )
    t_polygons = time.monotonic() - t0

    t0 = time.monotonic()
    attrs, attr_info = load_mapunit_attributes(polygons["mukey"].unique(), client, cache_dir, refresh=refresh, say=say)
    mapunits = mapunit_organic_matter(attrs, components=components)
    t_attrs = time.monotonic() - t0

    t0 = time.monotonic()
    table = field_organic_matter(geom, polygons, mapunits["om_pct"], min_coverage=min_coverage)
    table["dominant_muname"] = table["dominant_mukey"].map(mapunits["muname"]).to_numpy(dtype=object)
    table.index = geom.index
    t_overlay = time.monotonic() - t0

    n_fields = len(table)
    n_value = int(table["organic_matter_pct"].notna().sum())
    if n_fields and n_value == 0:
        raise SoilDataError(
            f"SSURGO gave no organic matter value for any of the {n_fields:,} field(s): {tile_info['n_tiles']} tile(s) "
            f"held {tile_info['mupolygons']:,} map unit polygon(s) and {int(mapunits['om_pct'].notna().sum())} map unit(s) "
            "with a value. Check the field geometries / CRS, or whether SSURGO covers this area."
        )

    try:
        survey_areas = load_survey_areas(polygons["areasymbol"].unique(), client, cache_dir, refresh=refresh)
    except SoilDataError as exc:  # version labels are metadata only; say so instead of failing the run
        survey_areas = [{"error": _snippet(exc, 200)}]

    used = mapunits.reindex(pd.unique(table["dominant_mukey"].dropna()))
    no_value = mapunits[mapunits["om_pct"].isna()]
    cov = table["om_coverage"].to_numpy(dtype=float)
    lon0, lat0, lon1, lat1 = (float(v) for v in lonlat.total_bounds) if n_fields else (np.nan,) * 4
    provenance = {
        "source": "USDA-NRCS SSURGO via Soil Data Access (SDA)",
        "endpoint": client.url,
        "computed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": {
            "attribute": "chorizon.om_r (organic matter, percent by weight of the <2 mm fraction, representative value)",
            "surface_horizon": (
                "smallest hzdept_r per component; an organic O horizon is skipped when a mineral horizon starts "
                f"within {LITTER_MAX_CM:g} cm of the surface; no substitution from deeper horizons when om_r is null"
            ),
            "map_unit": (
                f"comppct_r-weighted mean over {'all' if components == 'all' else 'major (majcompflag = Yes)'} components "
                "with a non-null surface om_r, weights renormalised; null when no component has a value"
            ),
            "field": (
                f"mean of the map unit values weighted by intersection area in {EQUAL_AREA_CRS}, null map units "
                "ignored and weights renormalised; NaN only when no part of the field has a value"
                + (f" or less than {min_coverage:g} of its area has one" if min_coverage > 0 else "")
            ),
            "components": components,
            "min_coverage": float(min_coverage),
        },
        "tile_scheme": {
            "tile_size_deg": 1 / tiles_per_degree,
            "alignment": f"tile (ix, iy) covers lon [ix/{tiles_per_degree}, (ix+1)/{tiles_per_degree}], "
            f"lat [iy/{tiles_per_degree}, (iy+1)/{tiles_per_degree}]; fixed grid, independent of the field set",
            "n_tiles": tile_info["n_tiles"],
            "tiles_from_cache": tile_info["tiles_from_cache"],
            "tiles_fetched": tile_info["tiles_fetched"],
            "empty_tiles": tile_info["empty_tiles"],
            "tile_extent": [
                min(t[0] for t in tiles) / tiles_per_degree,
                min(t[1] for t in tiles) / tiles_per_degree,
                (max(t[0] for t in tiles) + 1) / tiles_per_degree,
                (max(t[1] for t in tiles) + 1) / tiles_per_degree,
            ]
            if tiles
            else None,
            "fields_extent": [round(v, 6) for v in (lon0, lat0, lon1, lat1)] if n_fields else None,
        },
        "queries": {
            "mupolygon_tile": TILE_SQL,
            "mupolygon_tile_example": tile_query(*tiles[0], tiles_per_degree) if tiles else None,
            "mapunit_attributes": ATTR_SQL,
            "survey_areas": SURVEY_SQL,
            "format": "JSON+COLUMNNAME",
        },
        "http": dict(client.stats),
        "counts": {
            "fields": n_fields,
            "fields_with_value": n_value,
            "fields_nan": n_fields - n_value,
            "mupolygon_rows_in_tiles": tile_info["polygon_rows_in_tiles"],
            "mupolygons": tile_info["mupolygons"],
            "mukeys": attr_info["mukeys"],
            "mukeys_with_om": int(mapunits["om_pct"].notna().sum()),
            "mukeys_without_om": [
                {"mukey": str(k), "musym": _text(r["musym"]), "muname": _text(r["muname"])}
                for k, r in no_value.head(60).iterrows()
            ],
            "mukeys_dominant_in_a_field": int(len(used)),
            "components": int(attrs["cokey"].nunique()),
            "horizon_rows": int(attrs["chkey"].notna().sum()),
            "organic_layers_skipped": int(mapunits["organic_layers_skipped"].sum()),
            "attribute_rows": attr_info["attribute_rows"],
            "attribute_queries": attr_info["attribute_queries"],
            "mukeys_fetched": attr_info["mukeys_fetched"],
            "om_r_out_of_range_set_null": attr_info["om_r_out_of_range_set_null"],
        },
        "survey_areas": survey_areas,
        "coverage": {
            "definition": "om_coverage = share of the field area on map units that have an organic matter value",
            "full_ge_0.999": int((cov >= 0.999).sum()),
            "partial": int(((cov >= COVERAGE_EPS) & (cov < 0.999)).sum()),
            "none": int((cov < COVERAGE_EPS).sum()),
            "below_0.5_but_not_none": int(((cov >= COVERAGE_EPS) & (cov < 0.5)).sum()),
            "distribution": _dist(cov),
            "ssurgo_coverage_distribution": _dist(table["ssurgo_coverage"]),
        },
        "organic_matter_pct": _dist(table["organic_matter_pct"]),
        "mapunit_om_pct": _dist(mapunits["om_pct"]),
        "cache": _cache_summary(cache_dir),
        "timings_s": {
            "polygons": round(t_polygons, 2),
            "attributes": round(t_attrs, 2),
            "overlay": round(t_overlay, 2),
            "total": round(time.monotonic() - t_start, 2),
        },
    }
    table.attrs["provenance"] = provenance
    say(
        f"ssurgo: organic matter for {n_value:,}/{n_fields:,} field(s) from {tile_info['mupolygons']:,} map unit polygons "
        f"/ {attr_info['mukeys']} map units; {client.stats['http_requests']} HTTP request(s); "
        f"{provenance['timings_s']['total']}s"
    )
    return table


def organic_matter_pct(fields, **options) -> pd.Series:
    """Surface soil organic matter (percent) per field, from SSURGO.

    `fields`: GeoDataFrame (EPSG:4326) with a geometry column. Returns a float Series
    named `organic_matter_pct` with the same index, rounded to 3 decimals, NaN where
    SSURGO has no value under the field. `.attrs["provenance"]` describes exactly what
    was fetched and how. Options are those of `field_soil_table` (cache_dir, client,
    components, refresh, verbose, ...). Raises SoilDataError instead of returning all-NaN.
    """
    table = field_soil_table(fields, **options)
    om = table["organic_matter_pct"].astype(float).round(3).rename("organic_matter_pct")
    om.attrs["provenance"] = table.attrs["provenance"]
    return om


class _OfflineClient:
    """Stands in for SDAClient when the network must not be used: any query is an error."""

    url = "offline (cache only)"

    def __init__(self):
        self.stats = {"queries": 0, "http_requests": 0, "retries": 0}

    def query(self, sql: str, what: str = "SDA query") -> pd.DataFrame:
        raise SoilDataError(
            f"{what}: not in the SSURGO cache and the network is switched off (offline=True). "
            "Run `python pipeline/run_all.py` (or call with offline=False) once to fill data/raw/ssurgo/."
        )


def mapunit_series(attrs: pd.DataFrame) -> pd.Series:
    """mukey -> name of the map unit's largest component (its soil series), e.g. "Richfield".

    A tie in comppct_r goes to the smaller cokey. Map units without a component are absent.
    """
    comp = attrs[attrs["cokey"].notna() & attrs["compname"].notna()].drop_duplicates("cokey")
    comp = comp.assign(mukey=comp["mukey"].astype(str), pct=comp["comppct_r"].astype(float).fillna(0.0), cokey=comp["cokey"].astype(str))
    comp = comp.sort_values(["mukey", "pct", "cokey"], ascending=[True, False, True])
    first = comp.drop_duplicates("mukey").set_index("mukey")["compname"]
    return first.map(lambda name: str(name).strip() or None)


def field_mapunits(
    fields,
    *,
    cache_dir: str | Path | None = None,
    offline: bool = True,
    client: SDAClient | None = None,
    components: str = "all",
    tiles_per_degree: int = TILES_PER_DEGREE,
) -> pd.DataFrame:
    """Each field's SSURGO map-unit make-up, one row per field (same index as `fields`).

    Used to group fields for cross-validation: organic_matter_pct is one representative value per
    map unit, so two fields on the same map unit carry (almost) the same label.

    Columns:
      dominant_mukey     the map unit covering most of the field's area (str; None if the field
                         lies on no map unit polygon). An exact tie goes to the smaller mukey.
      dominant_share     that map unit's share of the field's area (0-1)
      n_mapunits         number of distinct map units under the field
      dominant_muname    its name, e.g. "Richfield silt loam, 0 to 1 percent slopes"
      dominant_series    the soil series of that map unit: the name of its largest component, e.g.
                         "Richfield" (None if it has no component). Sister map units share it.
      dominant_om_pct    the surface organic matter of that map unit alone (percent; NaN if it has none)
      organic_matter_pct the field's area-weighted value over all its map units, unrounded (what
                         `organic_matter_pct()` returns before rounding)

    `offline=True` (default) reads only the cached tiles and attributes in `cache_dir`
    (data/raw/ssurgo/); if something is missing it raises SoilDataError instead of calling Soil Data
    Access. `offline=False` (or an explicit `client`) fetches and caches what is missing.
    """
    cache_dir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    if client is None:
        client = _OfflineClient() if offline else SDAClient()
    geom = _geoseries(fields)
    lonlat = geom if geom.crs.to_epsg() == 4326 else geom.to_crs("EPSG:4326")
    bounds = lonlat.bounds.to_numpy(dtype=float)
    bounds = bounds[np.isfinite(bounds).all(axis=1)]
    tiles = tiles_for_bounds(bounds, tiles_per_degree)
    if len(tiles) > MAX_TILES:
        raise ValueError(f"the fields touch {len(tiles)} tiles (limit {MAX_TILES}); that is not a county-sized extent")
    polygons, _ = load_mupolygons(tiles, client, cache_dir, tiles_per_degree=tiles_per_degree)
    attrs, _ = load_mapunit_attributes(polygons["mukey"].unique(), client, cache_dir)
    mapunits = mapunit_organic_matter(attrs, components=components)
    table = field_organic_matter(geom, polygons, mapunits["om_pct"])
    out = table[["dominant_mukey", "dominant_share", "n_mapunits"]].copy()
    out["dominant_muname"] = table["dominant_mukey"].map(mapunits["muname"]).to_numpy(dtype=object)
    out["dominant_series"] = table["dominant_mukey"].map(mapunit_series(attrs)).to_numpy(dtype=object)
    out["dominant_om_pct"] = table["dominant_mukey"].map(mapunits["om_pct"]).to_numpy(dtype=float)
    out["organic_matter_pct"] = table["organic_matter_pct"].to_numpy(dtype=float)
    out.index = geom.index
    return out


# --------------------------------------------------------------------------- independent check


def sda_field_organic_matter(geometry, client: SDAClient | None = None) -> dict:
    """Organic matter of ONE lon/lat polygon computed entirely by SDA's SQL Server.

    This shares no code with the tile cache, the horizon selection or the overlay above,
    which is the point: it is the reference for `spot_check`. One HTTP request per call.
    """
    client = client or SDAClient()
    geom = _polygonal([geometry])[0]
    if geom is None:
        raise ValueError("geometry has no polygonal part")
    wkt = shapely.to_wkt(geom, rounding_precision=7)
    rows = client.query(SPOT_SQL.format(wkt=wkt, litter_cm=f"{LITTER_MAX_CM:g}"), what="field spot check")
    if rows.empty:
        return {"om_pct": float("nan"), "om_coverage": 0.0, "n_mapunits": 0, "mapunits": []}
    area = pd.to_numeric(rows["area_deg2"], errors="coerce").to_numpy(dtype=float)
    om = pd.to_numeric(rows["om_pct"], errors="coerce").to_numpy(dtype=float)
    keep = area > 0
    area, om, rows = area[keep], om[keep], rows[keep]
    has = ~np.isnan(om)
    value = float((area[has] * om[has]).sum() / area[has].sum()) if has.any() else float("nan")
    return {
        "om_pct": value,
        "om_coverage": float(area[has].sum() / shapely.area(geom)) if has.any() else 0.0,
        "n_mapunits": int(len(rows)),
        "mapunits": [
            {"mukey": str(k), "muname": n, "share": round(float(a / area.sum()), 4), "om_pct": None if o != o else round(float(o), 4)}
            for k, n, a, o in zip(rows["mukey"], rows["muname"], area, om)
        ],
    }


def spot_check(fields, table: pd.DataFrame, n: int = 8, *, client: SDAClient | None = None, seed: int = 20055) -> pd.DataFrame:
    """Compare `n` fields of `table` (from `field_soil_table`) with `sda_field_organic_matter`.

    Picks the lowest and highest value, the field with the most map units, the lowest
    non-zero coverage, then random fields. Returns one row per checked field with
    `om_local`, `om_sda`, `diff` and the coverage on both sides.
    """
    client = client or SDAClient()
    geom = _geoseries(fields)
    lonlat = geom if geom.crs.to_epsg() == 4326 else geom.to_crs("EPSG:4326")
    valued = np.flatnonzero(table["organic_matter_pct"].notna().to_numpy())
    if len(valued) == 0 or n <= 0:
        return pd.DataFrame()
    om = table["organic_matter_pct"].to_numpy(dtype=float)
    picks = [
        valued[np.argmin(om[valued])],
        valued[np.argmax(om[valued])],
        valued[np.argmax(table["n_mapunits"].to_numpy()[valued])],
        valued[np.argmin(table["om_coverage"].to_numpy(dtype=float)[valued])],
    ]
    rng = np.random.default_rng(seed)
    picks += list(rng.permutation(valued))
    chosen: list[int] = []
    for p in picks:
        if int(p) not in chosen:
            chosen.append(int(p))
        if len(chosen) >= n:
            break
    ids = fields["field_id"].to_numpy(dtype=object) if isinstance(fields, gpd.GeoDataFrame) and "field_id" in fields.columns else None
    rows = []
    for p in chosen:
        ref = sda_field_organic_matter(lonlat.iloc[p], client)
        rows.append(
            {
                "field": ids[p] if ids is not None else table.index[p],
                "om_local": om[p],
                "om_sda": ref["om_pct"],
                "diff": om[p] - ref["om_pct"],
                "coverage_local": float(table["om_coverage"].iloc[p]),
                "coverage_sda": ref["om_coverage"],
                "n_mapunits_local": int(table["n_mapunits"].iloc[p]),
                "n_mapunits_sda": ref["n_mapunits"],
                "dominant_muname": table["dominant_muname"].iloc[p],
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- CLI


def _histogram(values, width: float = 0.25, bar: int = 40) -> list[str]:
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return ["  (no values)"]
    lo = np.floor(v.min() / width) * width
    edges = np.arange(lo, v.max() + width, width)
    if len(edges) < 2:
        edges = np.array([lo, lo + width])
    counts, _ = np.histogram(v, bins=edges)
    peak = counts.max()
    return [
        f"  {a:5.2f}-{b:5.2f} %  {c:>6,}  {'#' * int(round(bar * c / peak))}"
        for a, b, c in zip(edges[:-1], edges[1:], counts)
        if c
    ]


def format_report(fields, table: pd.DataFrame, show_all: bool = False) -> str:
    """Per-field values + summary statistics, as text."""
    prov = table.attrs.get("provenance", {})
    ids = (
        fields["field_id"].astype(str).to_numpy(dtype=object)
        if isinstance(fields, gpd.GeoDataFrame) and "field_id" in fields.columns
        else np.array([str(i) for i in table.index], dtype=object)
    )
    lines = [f"{'field_id':<12} {'om_pct':>7} {'coverage':>9} {'units':>5}  dominant map unit (share)"]
    order = range(len(table)) if show_all or len(table) <= 60 else [*range(20), None, *range(len(table) - 10, len(table))]
    for i in order:
        if i is None:
            lines.append(f"  ... {len(table) - 30:,} more field(s); --all prints every row ...")
            continue
        row = table.iloc[i]
        value = "    NaN" if pd.isna(row["organic_matter_pct"]) else f"{row['organic_matter_pct']:7.3f}"
        name = row["dominant_muname"] if isinstance(row["dominant_muname"], str) else "(no SSURGO polygon)"
        lines.append(
            f"{ids[i]:<12} {value} {row['om_coverage']:9.3f} {int(row['n_mapunits']):>5}  {name} ({row['dominant_share']:.0%})"
        )
    om = prov.get("organic_matter_pct", {})
    cov = prov.get("coverage", {})
    counts = prov.get("counts", {})
    tiles = prov.get("tile_scheme", {})
    lines += ["", "organic_matter_pct (percent)"]
    if om.get("n"):
        lines.append(
            f"  n={om['n']:,}  NaN={counts.get('fields_nan', 0):,}  min={om['min']:.3f}  p05={om['p05']:.3f}  "
            f"p25={om['p25']:.3f}  median={om['median']:.3f}  mean={om['mean']:.3f}  p75={om['p75']:.3f}  "
            f"p95={om['p95']:.3f}  max={om['max']:.3f}"
        )
    lines += _histogram(table["organic_matter_pct"])
    dist = cov.get("distribution", {})
    if dist.get("n"):
        lines += [
            "coverage (share of each field's area on map units that have a value)",
            f"  full (>=0.999): {cov['full_ge_0.999']:,}   partial: {cov['partial']:,}   none: {cov['none']:,}   "
            f"below 0.5: {cov['below_0.5_but_not_none']:,}   min={dist['min']:.4f}  p01={dist['p01']:.4f}  p05={dist['p05']:.4f}  median={dist['median']:.4f}  mean={dist['mean']:.4f}",
        ]
    lines += [
        "source",
        f"  {tiles.get('n_tiles')} tile(s) of {tiles.get('tile_size_deg')} deg ({tiles.get('tiles_from_cache')} cached, "
        f"{tiles.get('tiles_fetched')} fetched); {counts.get('mupolygons', 0):,} map unit polygons; "
        f"{counts.get('mukeys')} map units ({counts.get('mukeys_with_om')} with a value); "
        f"{prov.get('http', {}).get('http_requests')} HTTP request(s); {prov.get('timings_s', {}).get('total')} s",
    ]
    for area in prov.get("survey_areas", []):
        if "areasymbol" in area:
            lines.append(f"  {area['areasymbol']} {area.get('areaname')}: version {area.get('saversion')} ({area.get('saverest')})")
    without = counts.get("mukeys_without_om", [])
    if without:
        names = sorted({str(m["muname"]) for m in without})
        lines.append(f"  map units without a value ({len(without)}): {', '.join(names[:12])}{' ...' if len(names) > 12 else ''}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SSURGO surface organic matter per field (USDA Soil Data Access).")
    parser.add_argument("fields", nargs="?", default=str(DATA_DIR / "fields_fake.parquet"), help="fields GeoParquet")
    parser.add_argument("--components", choices=["all", "major"], default="all")
    parser.add_argument("--min-coverage", type=float, default=0.0, help="NaN for fields with less valued coverage (0..1)")
    parser.add_argument("--cache-dir", default=None, help=f"default {CACHE_DIR.relative_to(ROOT)}")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache and fetch everything again")
    parser.add_argument("--all", action="store_true", help="print every field (default: all if <= 60)")
    parser.add_argument("--spot-check", type=int, default=0, metavar="N", help="check N fields against SDA's own SQL (N requests)")
    parser.add_argument("--provenance", action="store_true", help="also print the provenance dict as JSON")
    args = parser.parse_args(argv)

    path = Path(args.fields)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    fields = gpd.read_parquet(path)
    try:
        table = field_soil_table(
            fields,
            cache_dir=args.cache_dir,
            components=args.components,
            min_coverage=args.min_coverage,
            refresh=args.refresh,
        )
    except SoilDataError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print()
    print(format_report(fields, table, show_all=args.all))
    if args.spot_check:
        check = spot_check(fields, table, args.spot_check)
        print(f"\nspot check against SDA's own SQL for {len(check)} field(s):")
        with pd.option_context("display.width", 200, "display.max_columns", 20, "display.float_format", "{:.4f}".format):
            print(check.to_string(index=False))
        if len(check):
            print(f"max |diff| = {check['diff'].abs().max():.5f} percentage points")
    if args.provenance:
        print()
        print(json.dumps(table.attrs["provenance"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
