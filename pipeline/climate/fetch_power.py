"""Observed daily weather for the county from the NASA POWER point API (no key, no Earth Engine).

One point, the centre of the county box, 1 Oct 1999 to 31 Dec 2024. One point and not an average of
several because POWER's weather comes from MERRA-2 cells of 0.5 x 0.625 degrees: the cell around
the county centre covers most of the county and is about as large as the county box the climate
models are averaged over, and averaging neighbouring cells would smooth the daily rain and heat that
the hot-day, rain-day and dry-spell counts depend on. Solar radiation is on a 1 degree grid.

The raw JSON is cached under `data/raw/climate/power/` and never asked twice.

    python -m pipeline.climate.fetch_power     # fetch (or reuse) and print a short summary
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "raw" / "climate" / "power"

URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
PARAMETERS = ("T2M_MAX", "T2M_MIN", "PRECTOTCORR", "WS10M", "WS2M", "RH2M", "ALLSKY_SFC_SW_DWN")
COMMUNITY = "AG"  # the agro-climatology set: solar radiation in MJ/m2/day
START = "19991001"
END = "20241231"
MISSING = -999.0
MAX_GAP_DAYS = 7  # longer runs of missing days stop the build

# our column <- POWER parameter and the unit POWER must report for it
COLUMNS = {
    "tmax_c": ("T2M_MAX", "C"),
    "tmin_c": ("T2M_MIN", "C"),
    "rain_mm": ("PRECTOTCORR", "mm/day"),
    "wind_ms": ("WS10M", "m/s"),  # 10 m
    "wind2_ms": ("WS2M", "m/s"),  # 2 m
    "rh_pct": ("RH2M", "%"),
    "rs_mj": ("ALLSKY_SFC_SW_DWN", "MJ/m^2/day"),
}
DEFAULT_ELEVATION_M = 866.0  # Garden City; used only if POWER does not say


class PowerError(RuntimeError):
    pass


def site_for(bounds) -> tuple[float, float]:
    """(latitude, longitude) of the centre of a (west, south, east, north) box, 3 decimals."""
    west, south, east, north = bounds
    return round((south + north) / 2.0, 3), round((west + east) / 2.0, 3)


def cache_path(cache_dir: Path, lat: float, lon: float, start: str = START, end: str = END) -> Path:
    return Path(cache_dir) / f"power_daily_{lat:.3f}_{lon:.3f}_{start}_{end}.json"


def fetch_raw(lat: float, lon: float, cache_dir: Path | None = None, start: str = START, end: str = END, get=None, log=lambda m: None) -> dict:
    """The POWER answer as a dict: from the cache, else one HTTP GET (three tries) that is then cached."""
    cache_dir = CACHE_DIR if cache_dir is None else cache_dir
    path = cache_path(cache_dir, lat, lon, start, end)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            log(f"{path} is not valid JSON; asking POWER again")
    if get is None:
        import requests

        get = requests.get
    params = {
        "parameters": ",".join(PARAMETERS), "community": COMMUNITY, "longitude": lon, "latitude": lat,
        "start": start, "end": end, "format": "JSON",
    }
    last = None
    for attempt in range(3):
        try:
            response = get(URL, params=params, timeout=180)
            response.raise_for_status()
            doc = response.json()
            break
        except Exception as exc:  # noqa: BLE001 - network trouble: try again, then give up clearly
            last = exc
            import time

            time.sleep(5 * (attempt + 1))
    else:
        raise PowerError(f"NASA POWER did not answer ({' '.join(str(last).split())[:200]}); run the command again later")
    parse(doc)  # refuse to cache an answer that cannot be used
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)
    log(f"NASA POWER answer cached in {path}")
    return doc


def parse(doc: dict) -> tuple[pd.DataFrame, dict]:
    """(daily frame, site). The frame has a DatetimeIndex and COLUMNS; -999 becomes NaN (see fill_gaps).

    site = {"latitude", "longitude", "elevation_m", "missing": {column: days}}. Units are checked
    against what POWER says it sent, so a silent unit change cannot slip through.
    """
    try:
        values = doc["properties"]["parameter"]
        units = doc.get("parameters", {})
        coords = doc["geometry"]["coordinates"]
    except (KeyError, TypeError) as exc:
        raise PowerError(f"unexpected NASA POWER answer: missing {exc}") from exc
    data, missing = {}, {}
    for column, (name, unit) in COLUMNS.items():
        if name not in values:
            raise PowerError(f"NASA POWER answer has no {name}")
        got = (units.get(name) or {}).get("units")
        if got is not None and got != unit:
            raise PowerError(f"NASA POWER sent {name} in {got!r}, expected {unit!r}")
        series = pd.Series(values[name], dtype=float)
        series.index = pd.to_datetime(series.index, format="%Y%m%d")
        series = series.where(series != MISSING)
        missing[column] = int(series.isna().sum())
        data[column] = series
    frame = pd.DataFrame(data).sort_index()
    frame = frame.reindex(pd.date_range(frame.index[0], frame.index[-1], freq="D"))
    frame.index.name = "date"
    site = {
        "latitude": float(coords[1]), "longitude": float(coords[0]),
        "elevation_m": float(coords[2]) if len(coords) > 2 and coords[2] not in (None, MISSING) else DEFAULT_ELEVATION_M,
        "missing": {c: int(frame[c].isna().sum()) for c in frame.columns},
    }
    return frame, site


def fill_gaps(frame: pd.DataFrame, max_gap: int = MAX_GAP_DAYS) -> pd.DataFrame:
    """Fill missing days: straight line between neighbours, rain as 0. A gap longer than `max_gap` days is an error."""
    out = frame.copy()
    for column in out.columns:
        gaps = out[column].isna()
        if not gaps.any():
            continue
        run = gaps.groupby((~gaps).cumsum()).sum().max()
        if run > max_gap:
            raise PowerError(f"NASA POWER {column}: {int(run)} missing days in a row (more than {max_gap})")
        out[column] = out[column].fillna(0.0) if column == "rain_mm" else out[column].interpolate(limit_direction="both")
    return out


def load_observed(bounds, cache_dir: Path | None = None, get=None, log=lambda m: None) -> tuple[pd.DataFrame, dict]:
    """(gap-free daily frame, site) for the centre of `bounds`."""
    lat, lon = site_for(bounds)
    frame, site = parse(fetch_raw(lat, lon, cache_dir, get=get, log=log))
    if frame.index[0] > pd.Timestamp("1999-10-01") or frame.index[-1] < pd.Timestamp("2024-12-31"):
        raise PowerError(f"NASA POWER answer covers {frame.index[0].date()} to {frame.index[-1].date()}, need 1999-10-01 to 2024-12-31")
    return fill_gaps(frame), site


def main() -> int:
    from pipeline.climate.fetch_gddp import county_bounds

    frame, site = load_observed(county_bounds(), log=print)
    years = frame.loc["2000":"2024"]
    print(f"NASA POWER at {site['latitude']:.3f}, {site['longitude']:.3f} ({site['elevation_m']:.0f} m): {len(frame):,} days, "
          f"{sum(site['missing'].values())} missing values filled")
    print(f"2000-2024 means: Tmax {years['tmax_c'].mean():.1f} C, Tmin {years['tmin_c'].mean():.1f} C, "
          f"rain {years['rain_mm'].sum() / 25:.0f} mm/year, wind {years['wind_ms'].mean():.2f} m/s at 10 m, "
          f"{years['wind2_ms'].mean():.2f} at 2 m, solar {np.mean(years['rs_mj']):.1f} MJ/m2/day")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
