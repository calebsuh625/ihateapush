"""County boundary (Finney County, KS by default) from the US Census Bureau.

    from pipeline.county import county_boundary
    county = county_boundary()          # GeoDataFrame, EPSG:4326, one row

Sources, tried in order (everything is cached under data/raw/county/):

1. Census cartographic boundary file, 1:500,000 (`cb_2023_us_county_500k.zip`, 11.6 MB).
2. Census TIGERweb REST service (one GeoJSON feature, full TIGER resolution).

The extracted single-county outline is cached as `county_<fips>.geojson`, so later runs
never reopen the national shapefile.

    python pipeline/county.py           # prints the outline's bounds and area
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/county.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import requests  # noqa: E402
from shapely.geometry import shape  # noqa: E402

from pipeline.common import EQUAL_AREA_CRS, FIPS, RAW_DIR, USER_AGENT, download, log  # noqa: E402

COUNTY_DIR = RAW_DIR / "county"
CB_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"
TIGERWEB_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1/query"
)


def _from_cartographic_file(fips: str) -> tuple[gpd.GeoDataFrame, str]:
    zip_path = download(CB_URL, COUNTY_DIR / Path(CB_URL).name)
    gdf = gpd.read_file(f"zip://{zip_path}", where=f"GEOID = '{fips}'")
    if len(gdf) != 1:
        raise RuntimeError(f"{zip_path.name}: expected exactly one county with GEOID {fips}, found {len(gdf)}")
    return gdf[["GEOID", "NAME", "geometry"]], CB_URL


def _from_tigerweb(fips: str) -> tuple[gpd.GeoDataFrame, str]:
    params = {"where": f"GEOID='{fips}'", "outFields": "GEOID,NAME", "outSR": "4326", "f": "geojson"}
    resp = requests.get(TIGERWEB_URL, params=params, timeout=120, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if len(features) != 1:
        raise RuntimeError(f"TIGERweb: expected exactly one county with GEOID {fips}, found {len(features)}")
    props = features[0]["properties"]
    gdf = gpd.GeoDataFrame(
        {"GEOID": [str(props.get("GEOID", fips))], "NAME": [str(props.get("NAME", ""))]},
        geometry=[shape(features[0]["geometry"])],
        crs="EPSG:4326",
    )
    return gdf, resp.url


def county_boundary(fips: str = FIPS) -> gpd.GeoDataFrame:
    """The county outline as a one-row GeoDataFrame in EPSG:4326 (columns GEOID, NAME, geometry).

    `attrs["source"]` holds the URL the outline came from.
    """
    cache = COUNTY_DIR / f"county_{fips}.geojson"
    if cache.exists() and cache.stat().st_size > 0:
        gdf = gpd.read_file(cache)
        source = json.loads(cache.read_text()).get("source", "")
        gdf = gdf[["GEOID", "NAME", "geometry"]]
        gdf["GEOID"] = gdf["GEOID"].astype(str)
        gdf.attrs["source"] = source
        return gdf

    errors = []
    for fetch in (_from_cartographic_file, _from_tigerweb):
        try:
            gdf, source = fetch(fips)
            break
        except Exception as exc:  # try the next source
            errors.append(f"{fetch.__name__}: {exc}")
            log(f"county boundary: {fetch.__name__} failed ({exc})")
    else:
        raise RuntimeError("could not get the county boundary: " + "; ".join(errors))

    gdf = gdf.to_crs("EPSG:4326").reset_index(drop=True)
    gdf["GEOID"] = gdf["GEOID"].astype(str)
    if not gdf.geometry.is_valid.all():
        gdf["geometry"] = gdf.geometry.make_valid()

    COUNTY_DIR.mkdir(parents=True, exist_ok=True)
    doc = json.loads(gdf.to_json())
    doc["source"] = source
    tmp = cache.with_name(cache.name + ".part")
    tmp.write_text(json.dumps(doc))
    tmp.replace(cache)
    gdf.attrs["source"] = source
    return gdf


def main() -> int:
    gdf = county_boundary()
    area_km2 = float(gdf.to_crs(EQUAL_AREA_CRS).area.iloc[0]) / 1e6
    minx, miny, maxx, maxy = gdf.total_bounds
    print(f"{gdf['NAME'].iloc[0]} County (GEOID {gdf['GEOID'].iloc[0]}), {gdf.geometry.iloc[0].geom_type}")
    print(f"bounds (lon/lat): {minx:.4f}, {miny:.4f} -> {maxx:.4f}, {maxy:.4f}")
    print(f"area: {area_km2:,.1f} km2")
    print(f"source: {gdf.attrs.get('source')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
