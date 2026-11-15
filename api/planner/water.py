"""Crop water need, effective rain, stored soil water, irrigation need and dryland shortfall.

Phase 3, piece B1. Works on the monthly climate table (`data/climate_monthly.parquet`: `model`,
`scenario`, `year`, `month`, `rain_mm`, `et0_mm`), observed or model rows alike. All depths are mm
over the crop's season; a winter wheat season of harvest year Y runs October (Y-1) to June (Y).

The rules, in order
-------------------
1. Crop water need = sum over the crop's season months of Kc x reference ET x `et0_scale`
   (FAO-56 single crop coefficient; Kc by month in crops.py).
   `et0_scale` = 0.85: our reference ET comes from NASA POWER weather, which describes hot, dry, windy
   open country, not air over a watered crop; FAO-56 Annex 6 says reference ET computed from such
   weather runs high. With textbook Kc and no scaling, full irrigated need came out 5-26 % above the
   water irrigated fields here really use (OpenET satellite ET, 2016-2024). One factor for all crops
   closes the middle of that gap; the gap left per crop is printed in `data/crop_response_report.md`.
2. Effective rain per month = USDA-SCS formula as used in FAO CROPWAT:
   P x (125 - 0.2 P) / 125 for P <= 250 mm, 125 + 0.1 P above (the rest runs off or drains).
3. Stored soil water at planting = `fallow_efficiency` (25 %) of the rain that fell between the
   previous crop's harvest and this crop's planting, never more than the root zone can hold
   (`awc_mm`, 250 mm). The previous crop sets how long that wait is:
       after a summer crop (harvested by October): from November to planting
           (a summer crop: about 6 months; winter wheat: no wait at all, it is sown the same autumn);
       after winter wheat (harvested in June): from July to planting (summer crop: about 10 months);
       after a fallow year: twelve months more than after a summer crop.
   25 % is the middle of what fallow stores on the central High Plains (Nielsen and Vigil 2010:
   20-35 %; K-State Tribune). 250 mm is about 1.5 m of silt loam at 0.17 mm of available water per mm.
4. Monthly bucket through the season: each month the crop can use that month's effective rain plus
   what is in the soil; it uses its need if it can, the rest stays in the soil (up to `awc_mm`).
   The sum of what it got is the rain-fed ET; need minus rain-fed ET is the gap.
5. Dryland: the gap is the `shortfall_mm` (it drives the FAO-33 water factor in response.py).
   Irrigated: the gap is the net irrigation need; pumped water = gap / `pivot_efficiency` (0.85,
   centre pivot with drop nozzles, K-State irrigation guides).

Months missing before the start of a series (the look-back reaches up to 18 months before planting,
and the tables start in October 1999) are filled with that series' own calendar-month mean.
Series of different lengths (observed to 2024, models to 2060) can share one call; only years with
all twelve months of that series get numbers.

Entry points: `water_table(monthly)` (a tidy table) and `balance(monthly)` (arrays, used by
response.py and calibrate.py).
"""
from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from api.planner.crops import CROP_TABLE, CROPS, PREV_KINDS, Crop


@dataclass(frozen=True)
class WaterConstants:
    et0_scale: float = 0.85          # see rule 1
    fallow_efficiency: float = 0.25  # see rule 3
    awc_mm: float = 250.0            # see rule 3
    pivot_efficiency: float = 0.85   # see rule 5

    @classmethod
    def from_dict(cls, d: dict | None) -> "WaterConstants":
        if not d:
            return cls()
        known = {k: float(v) for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULTS = WaterConstants()
CLIMATOLOGY_YEARS = 25      # a missing month is filled with the mean of that month over the first 25 years
OBSERVED = "observed"       # model / scenario name of the observed rows in the monthly table
LEAD_YEARS = 2              # the look-back for stored water reaches into year Y-2


def effective_rain(rain_mm):
    """USDA-SCS (CROPWAT) effective rain for a monthly total, mm. Vectorised."""
    p = np.maximum(np.asarray(rain_mm, dtype=float), 0.0)
    return np.where(p <= 250.0, p * (125.0 - 0.2 * p) / 125.0, 125.0 + 0.1 * p)


@dataclass
class MonthGrid:
    """Monthly rain and reference ET as arrays: [series, year (with LEAD_YEARS extra in front), month]."""
    keys: list            # [(model, scenario), ...]
    years: np.ndarray     # consecutive years, from the first to the last year any series has in full
    rain: np.ndarray      # [S, Y + LEAD_YEARS, 12], no gaps (missing months hold the calendar-month mean)
    et0: np.ndarray       # same shape
    complete: np.ndarray  # [S, Y] bool: the table has all twelve months of this year for this series

    def take(self, values: np.ndarray, offsets) -> np.ndarray:
        """values[:, year + offset // 12, offset % 12] for every output year: shape [S, Y, len(offsets)].

        An offset counts months from January of the output year (0 = January, -3 = October before).
        """
        offsets = np.asarray(offsets, dtype=int)
        flat = values.reshape(values.shape[0], -1)
        base = (np.arange(len(self.years)) + LEAD_YEARS) * 12
        return flat[:, base[:, None] + offsets[None, :]]


def month_grid(monthly: pd.DataFrame) -> MonthGrid:
    """Arrange the monthly table into gap-free arrays. Series = distinct (model, scenario)."""
    need = {"year", "month", "rain_mm", "et0_mm"}
    missing = need - set(monthly.columns)
    if missing:
        raise ValueError(f"monthly table lacks columns {sorted(missing)}")
    df = monthly
    if "model" not in df.columns or "scenario" not in df.columns:
        df = df.assign(model=df["model"] if "model" in df.columns else OBSERVED,
                       scenario=df["scenario"] if "scenario" in df.columns else OBSERVED)
    if len(df) == 0:
        raise ValueError("monthly table is empty")
    keys_frame = df[["model", "scenario"]].drop_duplicates().sort_values(["model", "scenario"])
    keys = [tuple(k) for k in keys_frame.itertuples(index=False, name=None)]
    key_index = {k: i for i, k in enumerate(keys)}
    s = np.fromiter((key_index[k] for k in zip(df["model"].to_numpy(), df["scenario"].to_numpy())),
                    dtype=int, count=len(df))
    year = df["year"].to_numpy(dtype=int)
    month = df["month"].to_numpy(dtype=int)
    if ((month < 1) | (month > 12)).any():
        raise ValueError("month must be 1-12")

    # years with all twelve months, per series
    counts = pd.DataFrame({"s": s, "year": year, "month": month}).drop_duplicates().groupby(["s", "year"]).size()
    full = counts[counts == 12].reset_index()
    if len(full) == 0:
        raise ValueError("no series has all twelve months of any year")
    years = np.arange(int(full["year"].min()), int(full["year"].max()) + 1)
    complete = np.zeros((len(keys), len(years)), dtype=bool)
    complete[full["s"].to_numpy(), full["year"].to_numpy() - years[0]] = True
    y0 = int(years[0]) - LEAD_YEARS
    n_years = len(years) + LEAD_YEARS

    out = {}
    yi = year - y0
    ok = (yi >= 0) & (yi < n_years)
    for col in ("rain_mm", "et0_mm"):
        arr = np.full((len(keys), n_years, 12), np.nan)
        arr[s[ok], yi[ok], month[ok] - 1] = df[col].to_numpy(dtype=float)[ok]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)     # a month with no data at all in the window
            clim = np.nanmean(arr[:, LEAD_YEARS:LEAD_YEARS + CLIMATOLOGY_YEARS, :], axis=1, keepdims=True)
            clim = np.where(np.isnan(clim), np.nanmean(arr, axis=1, keepdims=True), clim)
        arr = np.where(np.isnan(arr), clim, arr)
        out[col] = arr
    return MonthGrid(keys=keys, years=years, rain=out["rain_mm"], et0=out["et0_mm"], complete=complete)


def storage_offsets(crop: Crop, kind: str) -> np.ndarray:
    """Month offsets (from January of the harvest year) in which the soil collects rain before planting."""
    off0, m0 = crop.season[0]
    plant = off0 * 12 + m0 - 1
    if kind == "wheat":       # the latest June before planting
        harvest = 5 + 12 * ((plant - 1 - 5) // 12)
    else:                     # the latest October up to planting
        harvest = 9 + 12 * ((plant - 9) // 12)
        if kind == "fallow":
            harvest -= 12
        elif kind != "summer":
            raise KeyError(f"unknown previous-crop kind {kind!r}; expected one of {PREV_KINDS}")
    return np.arange(harvest + 1, plant)


def season_offsets(crop: Crop) -> np.ndarray:
    return np.array([off * 12 + m - 1 for off, m in crop.season], dtype=int)


@dataclass
class CropBalance:
    """Water numbers of one crop for every series and year. Arrays are [S, Y] unless noted."""
    crop: str
    months: tuple                 # calendar months of the season
    etc: np.ndarray               # [S, Y, M] crop water need by month
    eff_rain: np.ndarray          # [S, Y, M] effective rain by month
    rain: np.ndarray              # [S, Y, M] rain by month
    need: np.ndarray              # season water need
    stored: dict                  # {kind: stored soil water at planting}
    rainfed_et: dict              # {kind: ET the crop gets from rain and soil alone}
    rainfed_et_monthly: dict      # {kind: [S, Y, M]}

    def gap(self, kind: str) -> np.ndarray:
        """Need minus rain-fed ET: the dryland shortfall, or the net irrigation need."""
        return np.maximum(self.need - self.rainfed_et[kind], 0.0)

    def et_ratio(self, kind: str) -> np.ndarray:
        """Rain-fed ET / need (1 when the crop needs nothing)."""
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(self.need > 0, self.rainfed_et[kind] / self.need, 1.0)
        return np.clip(r, 0.0, 1.0)


def crop_balance(grid: MonthGrid, crop_name: str, constants: WaterConstants | None = None) -> CropBalance:
    c = constants or DEFAULTS
    crop = CROP_TABLE[crop_name]
    n_s, n_y = len(grid.keys), len(grid.years)
    if not crop.season:  # fallow
        zero = np.zeros((n_s, n_y))
        empty = np.zeros((n_s, n_y, 0))
        return CropBalance(crop_name, (), empty, empty, empty, zero, {k: zero for k in PREV_KINDS},
                           {k: zero for k in PREV_KINDS}, {k: empty for k in PREV_KINDS})
    offs = season_offsets(crop)
    kc = np.asarray(crop.kc, dtype=float)
    etc = grid.take(grid.et0, offs) * kc[None, None, :] * c.et0_scale
    rain = grid.take(grid.rain, offs)
    pe = effective_rain(rain)
    need = etc.sum(axis=2)
    stored, rainfed, rainfed_m = {}, {}, {}
    for kind in PREV_KINDS:
        so = storage_offsets(crop, kind)
        collected = grid.take(grid.rain, so).sum(axis=2) if len(so) else np.zeros((n_s, n_y))
        sw = np.minimum(c.awc_mm, c.fallow_efficiency * collected)
        stored[kind] = sw
        got = np.empty_like(etc)
        for j in range(etc.shape[2]):
            avail = sw + pe[:, :, j]
            got[:, :, j] = np.minimum(etc[:, :, j], avail)
            sw = np.minimum(c.awc_mm, avail - got[:, :, j])
        rainfed_m[kind] = got
        rainfed[kind] = got.sum(axis=2)
    return CropBalance(crop_name, crop.months, etc, pe, rain, need, stored, rainfed, rainfed_m)


def balance(monthly: pd.DataFrame, constants: WaterConstants | None = None,
            crops=CROPS) -> tuple[MonthGrid, dict]:
    """(grid, {crop: CropBalance}) for the whole monthly table."""
    grid = month_grid(monthly)
    return grid, {name: crop_balance(grid, name, constants) for name in crops}


def window_et(grid: MonthGrid, bal: CropBalance, kind: str, irrigated: bool,
              constants: WaterConstants | None = None, months=(4, 5, 6, 7, 8, 9)) -> np.ndarray:
    """Modelled actual ET inside a calendar window (default April-September, what OpenET reports): [S, Y].

    In-season months: the full need (irrigated) or the rain-fed ET (dryland). Months of the window with
    no crop on the field: the rain that is not stored, (1 - fallow_efficiency) x rain, evaporates.
    Only used to compare with OpenET; nothing in the planner depends on it.
    """
    c = constants or DEFAULTS
    total = np.zeros((len(grid.keys), len(grid.years)))
    crop = CROP_TABLE[bal.crop]
    in_season = {m: j for j, (off, m) in enumerate(crop.season) if off == 0}
    for m in months:
        if m in in_season:
            j = in_season[m]
            total += bal.etc[:, :, j] if irrigated else bal.rainfed_et_monthly[kind][:, :, j]
        else:
            total += (1.0 - c.fallow_efficiency) * grid.take(grid.rain, [m - 1])[:, :, 0]
    return total


def water_table(monthly: pd.DataFrame, constants: WaterConstants | None = None) -> pd.DataFrame:
    """Tidy water numbers: one row per (model, scenario, year, crop, prev).

    `prev` is what was on the field before: `summer` (a crop harvested in autumn), `wheat`, `fallow`.
    Columns (mm): `water_need_mm`, `eff_rain_mm` (season total), `stored_mm` (soil water at planting),
    `rainfed_et_mm`, `gap_mm` (= dryland shortfall = net irrigation need),
    `irrigation_mm` (pumped: gap / pivot efficiency; what an irrigated field of this crop would draw).
    """
    c = constants or DEFAULTS
    grid, bals = balance(monthly, c)
    n_s, n_y = len(grid.keys), len(grid.years)
    models = np.repeat([k[0] for k in grid.keys], n_y)
    scenarios = np.repeat([k[1] for k in grid.keys], n_y)
    years = np.tile(grid.years, n_s)
    frames = []
    for name, b in bals.items():
        for kind in PREV_KINDS:
            gap = b.gap(kind)
            frames.append(pd.DataFrame({
                "model": models, "scenario": scenarios, "year": years, "crop": name, "prev": kind,
                "water_need_mm": b.need.ravel(), "eff_rain_mm": b.eff_rain.sum(axis=2).ravel(),
                "stored_mm": b.stored[kind].ravel(), "rainfed_et_mm": b.rainfed_et[kind].ravel(),
                "gap_mm": gap.ravel(), "irrigation_mm": (gap / c.pivot_efficiency).ravel(),
            }))
    out = pd.concat(frames, ignore_index=True)
    out = out[np.tile(grid.complete.ravel(), len(frames))]
    return out.sort_values(["model", "scenario", "year", "crop", "prev"], kind="stable").reset_index(drop=True)
