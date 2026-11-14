"""USDA NASS Crop Sequence Boundaries (CSB) for one county.

CSB (https://www.nass.usda.gov/Research_and_Science/Crop-Sequence-Boundaries/index.php)
are field polygons that NASS/ERS derive from eight consecutive years of Cropland Data
Layer. They are only distributed as national file geodatabases, one zip (3.5-4.3 GB)
per 8-year window, e.g. `NationalCSB_2018-2025_rev23.zip` containing `CSB1825.gdb` with
the single layer `national1825` (16 million MultiPolygons, EPSG:5070). Attributes:

    CSBID       15-character id, unique nation-wide (state FIPS + window + sequence)
    CSBYEARS    window, e.g. "1825"
    CSBACRES    polygon area in acres
    CDL2018 ... CDL2025   the polygon's CDL class in each year of the window
    STATEFIPS, CNTYFIPS, CNTY, STATEASD, ASD   location attributes (one county per polygon)
    INSIDE_X, INSIDE_Y    a point inside the polygon (EPSG:5070)

`county_csb()` downloads the newest release once (ranged, resumable), reads only the
county's polygons straight out of the zip (GDAL `/vsizip/`, spatial-index bbox filter
plus the STATEFIPS/CNTYFIPS attribute filter, ~10 s; if that fails the geodatabase is
unpacked next to the zip and read from there) and caches them as
`data/raw/csb/csb_<fips>.parquet` (+ `.json` with the provenance), so later runs open
neither the national geodatabase nor the network. Once that parquet exists the zip
(3.8 GB) is no longer needed and may be deleted by hand.

    python pipeline/csb.py              # prints what is cached / gets it
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/csb.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import pyogrio  # noqa: E402

from pipeline.common import EQUAL_AREA_CRS, FIPS, RAW_DIR, log  # noqa: E402
from pipeline.county import county_boundary  # noqa: E402
from pipeline.csb_download import ranged_download  # noqa: E402

CSB_DIR = RAW_DIR / "csb"
CSB_PAGE = "https://www.nass.usda.gov/Research_and_Science/Crop-Sequence-Boundaries/index.php"
CSB_URL = (
    "https://www.nass.usda.gov/sites/default/files/Research_and_Science/"
    "Crop-Sequence-Boundaries/datasets/NationalCSB_{window}_rev23.zip"
)
# 8-year windows to try, newest first ("2018-2025" was released 2026-03-27). A window
# whose zip is already on disk is preferred over downloading a newer one.
CSB_WINDOWS = ["2018-2025", "2017-2024", "2016-2023"]
BBOX_PAD_M = 5_000.0  # padding of the county bounding box used with the spatial index
CDL_COLUMN_RE = re.compile(r"^CDL(\d{4})$")


def zip_path(window: str) -> Path:
    return CSB_DIR / Path(CSB_URL.format(window=window)).name


def cache_paths(fips: str = FIPS) -> tuple[Path, Path]:
    return CSB_DIR / f"csb_{fips}.parquet", CSB_DIR / f"csb_{fips}.json"


def cdl_columns(columns) -> dict[int, str]:
    """year -> column name for the per-year CDL attributes (`CDL2018` ...)."""
    out = {}
    for col in columns:
        match = CDL_COLUMN_RE.match(str(col))
        if match:
            out[int(match.group(1))] = str(col)
    return dict(sorted(out.items()))


def _gdb_in_zip(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        gdbs = sorted({name.split(".gdb/")[0] + ".gdb" for name in zf.namelist() if ".gdb/" in name})
    if len(gdbs) != 1:
        raise RuntimeError(f"{path.name}: expected exactly one .gdb inside, found {gdbs}")
    return gdbs[0]


class CountyNotInCSB(RuntimeError):
    """The geodatabase was read fine but has no polygon for the county."""


def _extract_gdb(zip_file: Path, gdb: str) -> Path:
    """Unpack the geodatabase next to the zip (`<zip name without .zip>/<gdb>`); returns its path."""
    target_dir = zip_file.with_suffix("")
    target = target_dir / gdb
    if target.is_dir():
        return target
    tmp_dir = target_dir.with_name(target_dir.name + ".extracting")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    log(f"CSB: unpacking {gdb} from {zip_file.name} (several GB)")
    with zipfile.ZipFile(zip_file) as zf:
        zf.extractall(tmp_dir, members=[n for n in zf.namelist() if n.startswith(gdb + "/")])
    target_dir.mkdir(exist_ok=True)
    (tmp_dir / gdb).replace(target)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return target


def _read_county_from_zip(zip_file: Path, fips: str) -> tuple[gpd.GeoDataFrame, dict]:
    """County polygons out of a national CSB zip: straight from the zip, else from an unpacked copy."""
    gdb = _gdb_in_zip(zip_file)
    unpacked = zip_file.with_suffix("") / gdb
    if not unpacked.is_dir():
        try:
            log(f"CSB: reading county {fips} from {zip_file.name}/{gdb} (GDAL /vsizip/)")
            gdf, meta = read_county_from_gdb(f"/vsizip/{zip_file}/{gdb}", fips)
            return gdf, {**meta, "geodatabase": gdb, "read_via": "/vsizip/"}
        except CountyNotInCSB:
            raise
        except Exception as exc:
            log(f"CSB: could not read the geodatabase inside the zip ({exc}); unpacking it instead")
            unpacked = _extract_gdb(zip_file, gdb)
    gdf, meta = read_county_from_gdb(str(unpacked), fips)
    return gdf, {**meta, "geodatabase": gdb, "read_via": "unpacked copy"}


def read_county_from_gdb(gdb: str, fips: str = FIPS) -> tuple[gpd.GeoDataFrame, dict]:
    """The county's CSB polygons from a national geodatabase (also used by the tests)."""
    state, county = fips[:2], fips[2:]
    layers = pyogrio.list_layers(gdb)
    polygon_layers = [name for name, kind in layers if kind and "Polygon" in str(kind)]
    if len(polygon_layers) != 1:
        raise RuntimeError(f"{gdb}: expected one polygon layer, found {layers.tolist()}")
    layer = polygon_layers[0]
    info = pyogrio.read_info(gdb, layer=layer)
    fields = [str(f) for f in info["fields"]]
    for needed in ("CSBID", "STATEFIPS", "CNTYFIPS"):
        if needed not in fields:
            raise RuntimeError(f"{gdb}:{layer} has no {needed} attribute (found {fields})")

    outline = county_boundary(fips).to_crs(info["crs"])
    minx, miny, maxx, maxy = outline.total_bounds
    bbox = (minx - BBOX_PAD_M, miny - BBOX_PAD_M, maxx + BBOX_PAD_M, maxy + BBOX_PAD_M)
    where = f"STATEFIPS = '{state}' AND CNTYFIPS = '{county}'"
    gdf = pyogrio.read_dataframe(gdb, layer=layer, bbox=bbox, where=where)
    if len(gdf) == 0:
        raise CountyNotInCSB(f"{gdb}:{layer} has no features with {where}")
    if gdf.crs is None:
        raise RuntimeError(f"{gdb}:{layer} has no CRS")
    if gdf.crs == EQUAL_AREA_CRS:  # CSB is NAD83 / CONUS Albers, stored as WKT without the EPSG code
        gdf = gdf.set_crs(EQUAL_AREA_CRS, allow_override=True)
    else:
        gdf = gdf.to_crs(EQUAL_AREA_CRS)
    gdf["CSBID"] = gdf["CSBID"].astype(str)
    keep = ["CSBID"] + [c for c in ("CSBYEARS", "CSBACRES", "CNTY") if c in gdf.columns]
    keep += list(cdl_columns(gdf.columns).values()) + ["geometry"]
    gdf = gdf[keep].sort_values("CSBID", kind="stable").reset_index(drop=True)
    meta = {
        "layer": layer,
        "national_features": int(info["features"]),
        "attributes": fields,
        "filter": f"bbox of the county outline padded by {BBOX_PAD_M:g} m AND {where}",
        "county_features": int(len(gdf)),
    }
    return gdf, meta


def _load_cache(fips: str) -> gpd.GeoDataFrame | None:
    parquet, sidecar = cache_paths(fips)
    if not (parquet.exists() and sidecar.exists()):
        return None
    try:
        gdf = gpd.read_parquet(parquet)
        meta = json.loads(sidecar.read_text())
    except Exception as exc:
        log(f"CSB: ignoring unreadable cache {parquet.name} ({exc})")
        return None
    if len(gdf) == 0 or "CSBID" not in gdf.columns:
        return None
    gdf.attrs["provenance"] = {**meta, "from_cache": True}
    return gdf


def county_csb(fips: str = FIPS, windows: list[str] | None = None) -> gpd.GeoDataFrame:
    """All CSB polygons whose county attribute is `fips`, in EPSG:5070, sorted by CSBID.

    Columns: CSBID, CSBYEARS, CSBACRES, CNTY, CDL<year>..., geometry (as stored: MultiPolygon,
    not cleaned). `attrs["provenance"]` describes the release and the query. Raises
    RuntimeError if no release can be downloaded / read, and CountyNotInCSB (a
    RuntimeError) if the release has no polygon for the county.
    """
    cached = _load_cache(fips)
    if cached is not None:
        log(f"cached   data/raw/csb/{cache_paths(fips)[0].name} ({len(cached):,} CSB polygons)")
        return cached

    windows = list(CSB_WINDOWS) if windows is None else list(windows)
    windows.sort(key=lambda w: not zip_path(w).exists())  # stable: zips on disk first
    errors: list[str] = []
    for window in windows:
        url = CSB_URL.format(window=window)
        try:
            zip_file = ranged_download(url, zip_path(window))
            gdf, meta = _read_county_from_zip(zip_file, fips)
        except CountyNotInCSB:
            raise  # another release would not help; do not download gigabytes to find out
        except Exception as exc:
            errors.append(f"{window}: {exc}")
            log(f"CSB {window}: failed ({exc})")
            continue
        meta = {
            "release": f"NationalCSB_{window}_rev23",
            "window": window,
            "url": url,
            "landing_page": CSB_PAGE,
            "zip_bytes": zip_file.stat().st_size,
            "crs": EQUAL_AREA_CRS,
            **meta,
        }
        parquet, sidecar = cache_paths(fips)
        tmp = parquet.with_name(parquet.name + ".part")
        gdf.to_parquet(tmp, index=False)
        tmp.replace(parquet)
        sidecar.write_text(json.dumps(meta, indent=2))
        log(f"CSB {window}: {len(gdf):,} polygons for county {fips} -> data/raw/csb/{parquet.name}")
        gdf.attrs["provenance"] = {**meta, "from_cache": False}
        return gdf
    raise RuntimeError("no CSB release could be used: " + " | ".join(errors))


def main() -> int:
    gdf = county_csb()
    prov = gdf.attrs["provenance"]
    print(json.dumps({k: v for k, v in prov.items() if k != "attributes"}, indent=2))
    print("attributes:", ", ".join(prov.get("attributes", [])))
    area_ha = gdf.geometry.area / 1e4
    print(f"polygons: {len(gdf):,}   total {area_ha.sum():,.0f} ha   median {area_ha.median():.1f} ha")
    print("per-year CDL attributes:", ", ".join(cdl_columns(gdf.columns).values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
