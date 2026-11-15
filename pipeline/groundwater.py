"""Groundwater levels in and next to Finney County: download, table, trend.

    python -m pipeline.groundwater            (make groundwater)  ->  data/groundwater.parquet

Two public sources, for the county's bounding box widened by `BUFFER_DEG` (0.15 degrees, about 10 miles):

1. The USGS Water Data API (https://api.waterdata.usgs.gov/ogcapi/v0, no key needed), which took over from
   the retired waterservices.usgs.gov/nwis/gwlevels service in 2025:
       field-measurements    parameter 72019 = depth to water level, feet below land surface
       monitoring-locations  site type GW (wells): where the well is, how deep it is, which aquifer
   Its record for this area runs from 1911 to January 2015 and then stops (two readings since): Kansas no
   longer loads its yearly well round into the USGS database.
2. The Kansas Geological Survey's WIZARD water-level database (https://geohydro.kgs.ku.edu/geohydro/wizard),
   which holds the same wells and carries them on to this winter: the yearly January round of the High
   Plains wells by the KGS and the state's Division of Water Resources. It is what brings the table up to
   date. Where both hold a reading of the same well on the same day, the USGS one is kept (they agree).

Every answer is cached (data/raw/nwis/ and data/raw/kgs/) and never asked for again; at most two requests a
second go out, seven in all. Delete the cache folders to refresh.

The table keeps every usable measurement (the contract is pipeline/groundwater_schema.py). The trend rules
are applied afterwards, by the functions below, which the API (api/groundwater.py) also uses:

Winter level (`winter_levels`)
    A well's level for a winter is the median of its measurements taken from December to March, when the
    irrigation pumps are off and the water in the well has had the autumn to settle. December counts
    towards the next year's winter. Measurements whose record says the well was pumping, had just been
    pumped, or that something else disturbed the reading (`DISTURBED`; also the readings the KGS has
    marked as anomalous) are left out.

Trend (`well_trends`)
    For a well with at least `MIN_WINTERS` (10) winter levels since `TREND_FROM` (1996), the first and
    last of them at least `MIN_SPAN_YEARS` (10) years apart, and the last no more than `MAX_STALE_WINTERS`
    (5) winters older than the newest winter in the table: the change in depth to water per year as a
    Theil-Sen slope (the median of the slopes between every pair of winters, so one odd year cannot move
    it) with its 95% interval (`scipy.stats.theilslopes`). Positive = the water is getting deeper = the
    level is falling. 1996 is where the trend starts because it gives every well up to about 30 winters
    of the same yearly round; it is a choice, and every summary reports the winters actually used.
    Wells whose record names a bedrock aquifer under the High Plains aquifer (`is_bedrock`) are left out:
    they are another water body.

Water column and "if the rate continued" (`well_trends`, `column_in`)
    Where the well's depth is on record: water column = well depth - latest winter depth to water. The
    column in a later year is that minus the measured rate times the years in between (never below 0,
    never above the well depth). This is straight-line arithmetic on the measured rate, not a forecast:
    pumping, rules and wet or dry spells change the rate. A well's depth is not the bottom of the
    aquifer, so the column in the well can be less than the water-bearing thickness under it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/groundwater.py`
    sys.path.insert(0, str(ROOT))

from pipeline import groundwater_schema  # noqa: E402
from pipeline.common import FIPS, RAW_DIR, download, log  # noqa: E402

NWIS_DIR = RAW_DIR / "nwis"
KGS_DIR = RAW_DIR / "kgs"
API = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
WIZARD_URL = "https://geohydro.kgs.ku.edu/geohydro/wizard"
# A descriptive User-Agent with no personal details.
USER_AGENT = "crop-rotation-tool/0.4 (Finney County KS school research project; groundwater levels; cached, low volume)"
BUFFER_DEG = 0.15           # about 10 miles: "next to" the county
PAGE_SIZE = 10000
PAUSE_S = 0.5               # between requests: two a second at most
MAX_PAGES = 200             # a safety stop, far above what the region holds
DEPTH_PARAMETER = "72019"   # depth to water level, feet below land surface

# --- trend rules (see the module docstring) --------------------------------------------------------------
WINTER_MONTHS = (12, 1, 2, 3)
TREND_FROM = 1996
MIN_WINTERS = 10
MIN_SPAN_YEARS = 10
MAX_STALE_WINTERS = 5
EXTRAPOLATE_TO = (2040, 2060)
# Words in a record's status that mean the reading is not a resting water level. "Foreign substance" (oil
# on the water) is not among them: the KGS files those readings as static.
DISTURBED = ("pump", "obstruct", "dry", "flow", "injector", "plugged", "destroyed", "affected", "nearby",
             "surface water", "above", "anomalous", "discontinued", "other")
# WIZARD's status letters (its field definitions, cached as data/raw/kgs/wizardfgdc.html), in the words used
# here. Blank = static. "B" only says the day of the month is unknown.
WIZARD_STATUS = {
    "A": "Anomalous (KGS check)", "D": "Dry", "E": "Recently flowing", "F": "Flowing", "G": "Nearby flowing",
    "H": "Nearby recently flowing", "I": "Injector site", "J": "Injector site monitor", "M": "Plugged",
    "N": "Discontinued", "O": "Obstruction", "P": "Pumping", "R": "Recently pumped", "S": "Nearby pumping",
    "T": "Nearby recently pumped", "V": "Foreign substance", "W": "Destroyed", "X": "Surface water effects",
    "Z": "Other",
}
NOT_A_FORECAST = (
    "This is the measured rate carried forward in a straight line, not a forecast: pumping, rules and wet "
    "or dry years change it, and it says nothing about when any one well or field runs short."
)


# --- download ------------------------------------------------------------------------------------------

def region_bbox(buffer_deg: float = BUFFER_DEG) -> tuple:
    """(west, south, east, north) of the county's outline, widened by `buffer_deg`, to 2 decimals."""
    from pipeline.county import county_boundary

    west, south, east, north = county_boundary().total_bounds
    return (round(float(west) - buffer_deg, 2), round(float(south) - buffer_deg, 2),
            round(float(east) + buffer_deg, 2), round(float(north) + buffer_deg, 2))


def fetch_pages(collection: str, params: dict, cache_dir: Path = NWIS_DIR, pause_s: float = PAUSE_S) -> list:
    """Every feature of `collection` for `params`, following the answer's "next" links.

    Each page is one file, `<collection>_<hash of the query>_p0001.json`; a page that is on disk is read
    from disk, so a second run sends nothing and an interrupted run carries on where it stopped.
    """
    query = {"f": "json", "limit": PAGE_SIZE, **params}
    url = f"{API}/{collection}/items?{urlencode(query)}"
    tag = hashlib.sha1(url.encode()).hexdigest()[:10]
    features = []
    for page in range(1, MAX_PAGES + 1):
        dest = Path(cache_dir) / f"{collection}_{tag}_p{page:04d}.json"
        cached = dest.exists() and dest.stat().st_size > 0
        download(url, dest, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, quiet=True)
        if not cached:
            time.sleep(pause_s)
        body = json.loads(dest.read_text())
        if not isinstance(body, dict) or "features" not in body:
            dest.unlink()        # an error page must not sit in the cache looking like data
            raise RuntimeError(f"{collection}: page {page} is not a feature collection")
        features.extend(body["features"])
        following = [link.get("href") for link in body.get("links", []) if link.get("rel") == "next"]
        if not following or not body["features"]:
            log(f"{collection}: {len(features):,} records in {page} page(s) ({dest.parent.name}/{collection}_{tag}_*)")
            return features
        url = following[0]
    raise RuntimeError(f"{collection}: more than {MAX_PAGES} pages; stopped")


def fetch_wizard(bbox: tuple, cache_dir: Path = KGS_DIR, pause_s: float = 1.0) -> tuple[Path, Path]:
    """(sites csv, water levels csv) of the KGS WIZARD database for the box, cached.

    WIZARD has no API: its public query form (`WIZARD_URL`) answers with a list of wells and a download
    page that links two generated csv files. Four requests in all, once: the form, the download page, the
    two files. With both files on disk nothing is sent.
    """
    import re

    import requests

    west, south, east, north = bbox
    tag = hashlib.sha1(f"wizard|{west},{south},{east},{north}".encode()).hexdigest()[:10]
    cache_dir = Path(cache_dir)
    sites, levels = cache_dir / f"wizard_{tag}_sites.csv", cache_dir / f"wizard_{tag}_levels.csv"
    if all(path.exists() and path.stat().st_size > 0 for path in (sites, levels)):
        log(f"cached   {sites.parent.name}/{sites.name}, {levels.name}")
        return sites, levels
    cache_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    form = {"f_t": "0", "f_r": "0", "ew": "0", "f_s": "0", "f_nlat": str(north), "f_wlong": str(west),
            "f_elong": str(east), "f_slat": str(south), "f_c": "0", "f_gmd": "0", "f_st": "20", "f_dstart": "",
            "f_dend": "", "f_usgs": "", "f_kgsid": "", "f_only": "Y"}       # f_only: wells with water levels
    listing = session.post(f"{WIZARD_URL}/wizardviewer.cfm", files={k: (None, v) for k, v in form.items()}, timeout=180)
    listing.raise_for_status()
    found = re.search(r"([\d,]+)\s+records currently selected", listing.text)
    if not found:
        raise RuntimeError("WIZARD: the query form did not answer with a list of wells")
    time.sleep(pause_s)
    page = session.get(f"{WIZARD_URL}/wizarddownload.cfm", timeout=180)
    page.raise_for_status()
    (cache_dir / f"wizard_{tag}_download_page.html").write_text(page.text)
    links = {}
    for kind, dest in (("sites", sites), ("wlevel", levels)):
        match = re.search(rf'href\s*=\s*"?(https?://[^" >]*/{kind}\d+\.csv)', page.text, flags=re.I)
        if not match:
            raise RuntimeError(f"WIZARD: the download page has no {kind} file")
        links[dest] = match.group(1)
    for dest, url in links.items():
        time.sleep(pause_s)
        download(url, dest, headers={"User-Agent": USER_AGENT}, quiet=True)
    log(f"WIZARD: {found.group(1)} wells with water levels ({sites.parent.name}/{sites.name}, {levels.name})")
    return sites, levels


# --- the table -----------------------------------------------------------------------------------------

def _text(value) -> str | None:
    if value is None or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v)
    value = str(value).strip()
    return value or None


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


WELL_COLUMNS = ["well_id", "number", "lon", "lat", "well_depth_ft", "aquifer", "county_fips"]
ROW_COLUMNS = ["well_id", "date", "date_exact", "depth_ft", "status", "source"]


def wells_table(features: list) -> pd.DataFrame:
    """One row per well from USGS monitoring-locations features: `WELL_COLUMNS` (`number` is the site number
    without the agency in front, which is what WIZARD calls USGS_ID)."""
    rows = []
    for feature in features:
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or [math.nan, math.nan]
        depth = _number(props.get("well_constructed_depth"))
        if not depth > 0:
            depth = _number(props.get("hole_constructed_depth"))
        state, county = _text(props.get("state_code")), _text(props.get("county_code"))
        well_id = _text(feature.get("id") or props.get("id"))
        rows.append({
            "well_id": well_id,
            "number": _text(props.get("monitoring_location_number")) or (well_id or "").split("-")[-1] or None,
            "lon": _number(coords[0]), "lat": _number(coords[1]),
            "well_depth_ft": depth if depth > 0 else math.nan,
            "aquifer": _text(props.get("national_aquifer_code")),
            "county_fips": f"{state}{county}" if state and county else None,
        })
    table = pd.DataFrame(rows, columns=WELL_COLUMNS)
    return table.dropna(subset=["well_id"]).drop_duplicates("well_id").reset_index(drop=True)


def measurements_table(features: list) -> tuple[pd.DataFrame, dict]:
    """(rows, counts of what was dropped) from USGS field-measurements features.

    Kept: depth to water in feet with at least the month known and a value from 0 to 2,000 ft. A record
    with the year only cannot be placed in a season; a negative depth is a flowing well.
    """
    rows, dropped = [], {"usgs_not_feet": 0, "usgs_no_month": 0, "usgs_bad_value": 0}
    for feature in features:
        props = feature.get("properties") or {}
        if props.get("parameter_code") != DEPTH_PARAMETER or (props.get("unit_of_measure") or "ft") != "ft":
            dropped["usgs_not_feet"] += 1
            continue
        value = _number(props.get("value"))
        if not 0 <= value <= groundwater_schema.DEPTH_MAX_FT:
            dropped["usgs_bad_value"] += 1
            continue
        year, month, day = props.get("year"), props.get("month"), props.get("day")
        if not year or not month:
            dropped["usgs_no_month"] += 1
            continue
        try:
            date = pd.Timestamp(year=int(year), month=int(month), day=int(day) if day else 15)
        except ValueError:
            dropped["usgs_no_month"] += 1
            continue
        rows.append({"well_id": _text(props.get("monitoring_location_id")), "date": date, "date_exact": bool(day),
                     "depth_ft": value, "status": _text(props.get("qualifier")), "source": "usgs"})
    table = pd.DataFrame(rows, columns=ROW_COLUMNS)
    return table.dropna(subset=["well_id"]), dropped


def wizard_wells(sites: pd.DataFrame, number_to_id: dict | None = None) -> pd.DataFrame:
    """`WELL_COLUMNS` from WIZARD's sites file (all text). A well the USGS also lists keeps its USGS id;
    one it does not is "KGS-<number>". `aquifer` is "KGS:<geological unit or aquifer code>" or null."""
    number_to_id = number_to_id or {}
    sites = sites.rename(columns=lambda c: str(c).strip())
    rows = []
    for record in sites.to_dict("records"):
        number = _text(record.get("USGS_ID"))
        if not number:
            continue
        depth = _number(record.get("DEPTH_OF_WELL"))
        if not depth > 0:
            depth = _number(record.get("DEPTH_OF_HOLE"))
        unit = _text(record.get("GEOLOGICAL_UNIT1")) or _text(record.get("AQUIFER_CODE"))
        state, county = _text(record.get("STATE_CODE")), _text(record.get("COUNTY_CODE"))
        rows.append({
            "well_id": number_to_id.get(number, f"KGS-{number}"), "number": number,
            "lon": _number(record.get("LONGITUDE")), "lat": _number(record.get("LATITUDE")),
            "well_depth_ft": depth if depth > 0 else math.nan,
            "aquifer": f"KGS:{unit}" if unit else None,
            "county_fips": f"{int(state):02d}{int(county):03d}" if state and county and state.isdigit() and county.isdigit() else None,
        })
    return pd.DataFrame(rows, columns=WELL_COLUMNS).drop_duplicates("well_id").reset_index(drop=True)


def wizard_measurements(levels: pd.DataFrame, number_to_id: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """(rows, counts of what was dropped) from WIZARD's water-levels file (all text): `ROW_COLUMNS`.

    Status "B" means only the month is known (WIZARD then writes the first of the month): the row gets the
    15th and `date_exact` false, as the USGS rows do. A blank status is a static level.
    """
    number_to_id = number_to_id or {}
    rows, dropped = [], {"kgs_no_value": 0, "kgs_bad_value": 0, "kgs_bad_date": 0}
    for record in levels.to_dict("records"):
        number = _text(record.get("USGS_ID"))
        value = _number(record.get("DEPTH_TO_WATER"))
        if not number or value != value:
            dropped["kgs_no_value"] += 1      # dry, obstructed, destroyed: no level was taken
            continue
        if not 0 <= value <= groundwater_schema.DEPTH_MAX_FT:
            dropped["kgs_bad_value"] += 1
            continue
        date = pd.to_datetime(str(record.get("MEASUREMENT_DATE_AND_TIME"))[:10], format="%m/%d/%Y", errors="coerce")
        if pd.isna(date):
            dropped["kgs_bad_date"] += 1
            continue
        code = (_text(record.get("STATUS")) or "").upper()
        exact = code != "B"
        if not exact:
            date = date.replace(day=15)
        status = "Static" if code in ("", "B") else WIZARD_STATUS.get(code, f"Code {code}")
        rows.append({"well_id": number_to_id.get(number, f"KGS-{number}"), "date": date, "date_exact": exact,
                     "depth_ft": value, "status": status, "source": "kgs"})
    return pd.DataFrame(rows, columns=ROW_COLUMNS), dropped


def build_table(measurement_features: list, well_features: list, wizard_sites: pd.DataFrame | None = None,
                wizard_levels: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """The table of the contract, and counts for the log. The WIZARD files are optional."""
    rows, dropped = measurements_table(measurement_features)
    wells = wells_table(well_features)
    if wizard_sites is not None and wizard_levels is not None:
        number_to_id = dict(zip(wells["number"], wells["well_id"]))
        more, more_dropped = wizard_measurements(wizard_levels, number_to_id)
        dropped.update(more_dropped)
        kgs_wells = wizard_wells(wizard_sites, number_to_id)
        # a KGS reading is left out when the USGS has that well on that day, or (for a reading known to the
        # month only, on either side) in that month
        have_day = set(zip(rows["well_id"], rows["date"]))
        have_month = set(zip(rows["well_id"], rows["date"].dt.year, rows["date"].dt.month))
        loose_month = set(zip(rows.loc[~rows["date_exact"], "well_id"], rows.loc[~rows["date_exact"], "date"].dt.year,
                              rows.loc[~rows["date_exact"], "date"].dt.month))
        keys_day = list(zip(more["well_id"], more["date"]))
        keys_month = list(zip(more["well_id"], more["date"].dt.year, more["date"].dt.month))
        twice = np.array([(day in have_day) or (month in loose_month) or (not exact and month in have_month)
                          for day, month, exact in zip(keys_day, keys_month, more["date_exact"])], dtype=bool)
        dropped["kgs_already_in_usgs"] = int(twice.sum())
        rows = pd.concat([rows, more[~twice]], ignore_index=True)
        # the well's own facts: the USGS record first, gaps filled from the KGS one. The well's depth is the
        # other way round: the KGS sounds these wells today, and where the two differ (15 of 472 wells here)
        # the USGS figure is the older one, sometimes from before the well was deepened.
        both = wells.merge(kgs_wells, on="well_id", how="outer", suffixes=("", "_kgs"))
        for col in ("number", "lon", "lat", "aquifer", "county_fips"):
            both[col] = both[col].where(both[col].notna(), both[f"{col}_kgs"])
        both["well_depth_ft"] = both["well_depth_ft_kgs"].where(both["well_depth_ft_kgs"].notna(), both["well_depth_ft"])
        wells = both[WELL_COLUMNS]
    # one row per well and day: the mean depth; the status when every reading that day agrees, else all of them
    rows = rows.sort_values(["well_id", "date", "source"], ascending=[True, True, False], kind="stable")
    grouped = rows.groupby(["well_id", "date"], sort=True)
    table = grouped.agg(
        date_exact=("date_exact", "all"), depth_ft=("depth_ft", "mean"),
        status=("status", lambda s: ", ".join(sorted(set(s.dropna()))) or None),
        source=("source", "first"),
    ).reset_index()
    dropped["same_day"] = int(len(rows) - len(table))
    table = table.merge(wells.drop(columns=["number"]), on="well_id", how="left")
    nowhere = table["lon"].isna() | table["lat"].isna()
    dropped["no_location"] = int(nowhere.sum())
    table = table[~nowhere]
    for col in ("depth_ft", "well_depth_ft", "lon", "lat"):
        table[col] = table[col].astype(float)
    table["date_exact"] = table["date_exact"].astype(bool)
    for col in ("status", "aquifer", "county_fips", "source"):
        table[col] = table[col].astype(object).where(table[col].notna(), None)
    table = table[groundwater_schema.COLUMNS].sort_values(["well_id", "date"]).reset_index(drop=True)
    return table, dropped


# --- winter levels and trends (pure; used by the API) -----------------------------------------------------

def is_disturbed(status) -> bool:
    """True when the record's status says the reading is not a resting water level."""
    if status is None or (isinstance(status, float) and status != status):
        return False
    text = str(status).lower()
    return any(word in text for word in DISTURBED)


def is_bedrock(aquifer) -> bool:
    """True when the well's record names a bedrock aquifer under the High Plains aquifer: the USGS
    national code for the Lower Cretaceous aquifers, or a KGS unit of Cretaceous or Jurassic age (codes
    starting K or J, or a 2xx Mesozoic aquifer code such as 210DKOT, the Dakota). Unknown is not bedrock."""
    if aquifer is None or (isinstance(aquifer, float) and aquifer != aquifer):
        return False
    text = str(aquifer).strip().upper()
    if text.startswith("KGS:"):
        return text[4:5] in ("K", "J", "2")
    return text == "N300LCRTCS"


def winter_levels(df: pd.DataFrame) -> pd.DataFrame:
    """One row per well and winter: well_id, winter (the year the winter ends in), depth_ft (median), n."""
    dates = pd.to_datetime(df["date"])
    month = dates.dt.month
    keep = month.isin(WINTER_MONTHS) & ~df["status"].map(is_disturbed)
    rows = df.loc[keep, ["well_id", "depth_ft"]].copy()
    rows["winter"] = (dates[keep].dt.year + (month[keep] == 12)).astype(int)
    out = rows.groupby(["well_id", "winter"], sort=True)["depth_ft"].agg(depth_ft="median", n="size").reset_index()
    return out


def column_in(column_now: float, slope: float, last_winter: int, year: int, well_depth: float) -> float:
    """Water column in `year` if the measured rate simply continued: never below 0 or above the well depth."""
    return float(min(max(column_now - slope * (year - last_winter), 0.0), well_depth))


TREND_COLUMNS = ["well_id", "lon", "lat", "aquifer", "county_fips", "well_depth_ft", "n_winters", "first_winter",
                 "last_winter", "slope_ft_per_year", "slope_low", "slope_high", "intercept_ft", "last_depth_ft",
                 "column_ft"]


def well_trends(df: pd.DataFrame, *, trend_from: int = TREND_FROM, min_winters: int = MIN_WINTERS,
                min_span: int = MIN_SPAN_YEARS, max_stale: int = MAX_STALE_WINTERS,
                years: tuple = EXTRAPOLATE_TO) -> pd.DataFrame:
    """One row per well that meets the trend rule (module docstring): `TREND_COLUMNS` plus
    column_<year>_ft for each of `years`.

    slope_ft_per_year (+ slope_low, slope_high: the 95% interval): change in depth to water per year,
    positive = falling. intercept_ft: the fitted line is intercept_ft + slope x winter. last_depth_ft: the
    latest winter level. column_ft: well depth - last_depth_ft
    (NaN without a usable well depth, and then well_depth_ft is NaN too).
    """
    from scipy.stats import theilslopes

    columns = [*TREND_COLUMNS, *(f"column_{year}_ft" for year in years)]
    levels = winter_levels(df)
    if len(levels) == 0:
        return pd.DataFrame(columns=columns)
    newest = int(levels["winter"].max())
    levels = levels[levels["winter"] >= trend_from]
    meta = df.drop_duplicates("well_id").set_index("well_id")
    rows = []
    for well_id, group in levels.groupby("well_id", sort=True):
        x = group["winter"].to_numpy(dtype=float)
        y = group["depth_ft"].to_numpy(dtype=float)
        if len(x) < min_winters or x[-1] - x[0] < min_span or x[-1] < newest - max_stale:
            continue
        info = meta.loc[well_id]
        if is_bedrock(info["aquifer"]):
            continue
        slope, intercept, low, high = theilslopes(y, x, 0.95, method="joint")   # joint: the intercept is robust too
        well_depth = float(info["well_depth_ft"]) if pd.notna(info["well_depth_ft"]) else math.nan
        last_depth, last_winter = float(y[-1]), int(x[-1])
        # a well cannot hold water below its own bottom: a depth on record that is shallower than the water
        # is a wrong depth (or a deepened well), so no column is given for it
        known = math.isfinite(well_depth) and well_depth > last_depth
        column = well_depth - last_depth if known else math.nan
        row = {"well_id": well_id, "lon": float(info["lon"]), "lat": float(info["lat"]),
               "aquifer": info["aquifer"], "county_fips": info["county_fips"],
               "well_depth_ft": well_depth if known else math.nan,
               "n_winters": int(len(x)), "first_winter": int(x[0]), "last_winter": last_winter,
               "slope_ft_per_year": float(slope), "slope_low": float(low), "slope_high": float(high),
               "intercept_ft": float(intercept), "last_depth_ft": last_depth, "column_ft": column}
        for year in years:
            row[f"column_{year}_ft"] = column_in(column, slope, last_winter, year, well_depth) if known else math.nan
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def _quantiles(values: np.ndarray) -> dict:
    """median, the middle half (p25, p75) and the full range of a set of numbers, unrounded."""
    values = np.asarray(values, dtype=float)
    return {"median": float(np.median(values)), "p25": float(np.percentile(values, 25)),
            "p75": float(np.percentile(values, 75)), "min": float(values.min()), "max": float(values.max())}


def summarize_trends(trends: pd.DataFrame, years: tuple = EXTRAPOLATE_TO) -> dict | None:
    """The picture of a set of wells (the county's, or the ones near a field); None when there are none.

    {"wells", "first_winter", "last_winter", "decline_ft_per_year": {median, p25, p75, min, max},
     "falling": wells whose whole 95% interval is above zero, "rising": whose whole interval is below zero,
     "depth_to_water_ft": {median, ...}, "column": None or {"wells", "now_ft": {...}, "<year>_ft": {...}}}
    """
    if trends is None or len(trends) == 0:
        return None
    slope = trends["slope_ft_per_year"].to_numpy(dtype=float)
    out = {
        "wells": int(len(trends)),
        "first_winter": int(trends["first_winter"].min()),
        "last_winter": int(trends["last_winter"].max()),
        "decline_ft_per_year": _quantiles(slope),
        "falling": int((trends["slope_low"] > 0).sum()),
        "rising": int((trends["slope_high"] < 0).sum()),
        "depth_to_water_ft": _quantiles(trends["last_depth_ft"].to_numpy(dtype=float)),
        "column": None,
    }
    known = trends[trends["column_ft"].notna()]
    if len(known):
        column = {"wells": int(len(known)), "now_ft": _quantiles(known["column_ft"].to_numpy(dtype=float))}
        for year in years:
            column[f"{year}_ft"] = _quantiles(known[f"column_{year}_ft"].to_numpy(dtype=float))
        out["column"] = column
    return out


# --- command line ----------------------------------------------------------------------------------------

def build(out_path: Path = groundwater_schema.GROUNDWATER_PATH, nwis_dir: Path = NWIS_DIR,
          kgs_dir: Path = KGS_DIR) -> pd.DataFrame:
    bbox = region_bbox()
    box = ",".join(str(v) for v in bbox)
    log(f"region: county {FIPS} widened by {BUFFER_DEG} degrees = bbox {box}")
    measurements = fetch_pages("field-measurements", {"parameter_code": DEPTH_PARAMETER, "bbox": box}, nwis_dir)
    wells = fetch_pages("monitoring-locations", {"site_type_code": "GW", "bbox": box}, nwis_dir)
    sites_csv, levels_csv = fetch_wizard(bbox, kgs_dir)
    table, dropped = build_table(measurements, wells, pd.read_csv(sites_csv, dtype=str),
                                 pd.read_csv(levels_csv, dtype=str))
    groundwater_schema.validate_groundwater(table, "built")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(out_path, index=False)
    log(f"wrote {out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path}; left out: "
        + ", ".join(f"{name} {count:,}" for name, count in dropped.items()))
    print(groundwater_schema.summarize(table))
    trends = well_trends(table)
    summary = summarize_trends(trends[trends["county_fips"] == FIPS])
    if summary:
        d = summary["decline_ft_per_year"]
        print(f"trend rule met by {len(trends):,} wells, {summary['wells']:,} of them in the county "
              f"(winters {summary['first_winter']}-{summary['last_winter']}): median change in depth to water "
              f"{d['median']:+.2f} ft a year (middle half {d['p25']:+.2f} to {d['p75']:+.2f}); "
              f"{summary['falling']} falling beyond their own uncertainty, {summary['rising']} rising")
    else:
        print("no well in the county meets the trend rule")
    return table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=groundwater_schema.GROUNDWATER_PATH)
    args = parser.parse_args(argv)
    try:
        build(args.out)
    except Exception as exc:
        print(f"groundwater: failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
