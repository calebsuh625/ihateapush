"""Yearly and monthly weather numbers from a daily series (see schema.md, "Phase 3 climate tables").

Input everywhere: a daily frame with a DatetimeIndex and tmax_c, tmin_c, rain_mm, wind_ms (10 m) and
et0_mm. The frame must start on 1 October of the year before the first year asked for, because the
wheat season of year Y runs from 1 Oct (Y-1) to 30 Jun (Y).

Seasons: growing season (GS) 1 Apr - 30 Sep; summer Jun - Aug; grain fill May - June; blowing season
Feb - May.

Wind-erosion climatic factor (the "C" of the Wind Erosion Equation, Chepil, Siddoway and Armbrust
1962; Woodruff and Siddoway 1965):

    C = 386 x u^3 / PE^2          u = mean annual wind speed in m/s (about 10 m above ground)
    PE = sum over the 12 months of 115 x (P / (T - 10))^(10/9)

PE is Thornthwaite's precipitation-effectiveness index with P = monthly precipitation in inches (not
below 0.5) and T = monthly mean temperature in F (not below 28.4). The design note wrote "386 ... u in
mph"; 386 belongs to m/s (the mph form is 34.483 x u^3 / PE^2, the same number), and since the factor
is rescaled so that the observed 2000-2024 mean is 100 the constant cancels anyway. C was made for
long-term means; here it is computed for single years from that year's wind, rain and temperature, so
one dry windy year can be several times the mean. Years are capped at EROSION_CAP (three times the
observed mean) and the scale is set so that the observed mean is exactly 100 after capping.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.climate.schema import MONTHLY_VALUE_COLUMNS as MONTHLY_COLUMNS
from pipeline.climate.schema import YEARLY_COLUMNS

GS_MONTHS = (4, 5, 6, 7, 8, 9)
SUMMER_MONTHS = (6, 7, 8)
GRAIN_FILL_MONTHS = (5, 6)
BLOWING_MONTHS = (2, 3, 4, 5)
WHEAT_START_MONTH = 10  # 1 Oct of the year before
WHEAT_END_MONTH = 6  # 30 Jun

GDD_BASE_C = 10.0
GDD_CAP_C = 30.0
HOT_C = 35.0
WHEAT_HOT_C = 32.0
WET_MM = 1.0
EROSION_CAP = 300.0

def gdd_daily(tmax_c, tmin_c, base: float = GDD_BASE_C, cap: float = GDD_CAP_C):
    """Growing degree days of each day: mean of Tmax and Tmin after both are held inside base..cap, minus base."""
    tmax = np.clip(np.asarray(tmax_c, dtype=float), base, cap)
    tmin = np.clip(np.asarray(tmin_c, dtype=float), base, cap)
    return (tmax + tmin) / 2.0 - base


def hot_days(tmax_c, threshold: float = HOT_C) -> int:
    """Days with Tmax strictly above the threshold."""
    return int((np.asarray(tmax_c, dtype=float) > threshold).sum())


def longest_dry_spell(rain_mm, wet_mm: float = WET_MM) -> int:
    """Longest run of days with rain below `wet_mm`."""
    best = run = 0
    for dry in np.asarray(rain_mm, dtype=float) < wet_mm:
        run = run + 1 if dry else 0
        best = max(best, run)
    return best


def pe_index(monthly_rain_mm, monthly_tmean_c) -> float:
    """Thornthwaite precipitation-effectiveness index from 12 monthly rain totals (mm) and mean temperatures (C)."""
    rain = np.asarray(monthly_rain_mm, dtype=float)
    temp = np.asarray(monthly_tmean_c, dtype=float)
    if rain.shape != (12,) or temp.shape != (12,):
        raise ValueError("pe_index needs 12 monthly values of rain and temperature")
    inches = np.maximum(rain / 25.4, 0.5)
    fahrenheit = np.maximum(temp * 9.0 / 5.0 + 32.0, 28.4)
    return float((115.0 * (inches / (fahrenheit - 10.0)) ** (10.0 / 9.0)).sum())


def erosion_factor(wind_ms: float, pe: float) -> float:
    """Unscaled wind-erosion climatic factor C = 386 x u^3 / PE^2 (u in m/s)."""
    return float(386.0 * wind_ms**3 / pe**2)


def erosion_scale(observed_raw, cap: float = EROSION_CAP, target: float = 100.0) -> float:
    """The factor k for which mean(min(k x raw, cap)) over the observed years is `target`."""
    raw = np.asarray(observed_raw, dtype=float)
    if len(raw) == 0 or not np.all(raw > 0):
        raise ValueError("erosion_scale needs positive observed values")
    k = target / raw.mean()
    for _ in range(100):  # capping lowers the mean, so k creeps up until it settles
        mean = np.minimum(k * raw, cap).mean()
        if abs(mean - target) < 1e-9:
            break
        k *= target / mean
    return float(k)


def scale_erosion(raw, k: float, cap: float = EROSION_CAP):
    return np.minimum(np.asarray(raw, dtype=float) * k, cap)


def _need(daily: pd.DataFrame, first_year: int) -> None:
    missing = [c for c in ("tmax_c", "tmin_c", "rain_mm", "wind_ms", "et0_mm") if c not in daily.columns]
    if missing:
        raise ValueError(f"daily frame lacks {', '.join(missing)}")
    if daily[["tmax_c", "tmin_c", "rain_mm", "wind_ms", "et0_mm"]].isna().any().any():
        raise ValueError("daily frame has missing values")
    if daily.index[0] > pd.Timestamp(f"{first_year - 1}-10-01"):
        raise ValueError(f"daily frame must start by 1 Oct {first_year - 1} (the first wheat season)")


def yearly(daily: pd.DataFrame, first_year: int, last_year: int) -> pd.DataFrame:
    """One row per year with YEARLY_COLUMNS; `erosion_climate` here is the unscaled C (see scale_erosion)."""
    _need(daily, first_year)
    if daily.index[-1] < pd.Timestamp(f"{last_year}-12-31"):
        raise ValueError(f"daily frame ends before 31 Dec {last_year}")
    tmean = (daily["tmax_c"] + daily["tmin_c"]) / 2.0
    month = daily.index.month
    rows = []
    for year in range(first_year, last_year + 1):
        in_year = daily.index.year == year
        gs = in_year & month.isin(GS_MONTHS)
        summer = in_year & month.isin(SUMMER_MONTHS)
        wheat = (daily.index >= pd.Timestamp(f"{year - 1}-{WHEAT_START_MONTH:02d}-01")) & (
            daily.index < pd.Timestamp(f"{year}-{WHEAT_END_MONTH + 1:02d}-01")
        )
        grain_fill = in_year & month.isin(GRAIN_FILL_MONTHS)
        blowing = in_year & month.isin(BLOWING_MONTHS)
        year_days = daily[in_year]
        monthly_rain = year_days["rain_mm"].groupby(year_days.index.month).sum().reindex(range(1, 13))
        monthly_temp = tmean[in_year].groupby(year_days.index.month).mean().reindex(range(1, 13))
        annual_wind = float(year_days["wind_ms"].mean())
        rows.append(
            {
                "year": year,
                "tmean_c": float(tmean[gs].mean()),
                "rain_mm": float(daily.loc[gs, "rain_mm"].sum()),
                "summer_rain_mm": float(daily.loc[summer, "rain_mm"].sum()),
                "gdd": float(gdd_daily(daily.loc[gs, "tmax_c"], daily.loc[gs, "tmin_c"]).sum()),
                "hot_days": float(hot_days(daily.loc[gs, "tmax_c"], HOT_C)),
                "dry_spell_days": float(longest_dry_spell(daily.loc[gs, "rain_mm"])),
                "et0_mm": float(daily.loc[gs, "et0_mm"].sum()),
                "wind_ms": float(daily.loc[gs, "wind_ms"].mean()),
                "wheat_rain_mm": float(daily.loc[wheat, "rain_mm"].sum()),
                "wheat_hot_days": float(hot_days(daily.loc[grain_fill, "tmax_c"], WHEAT_HOT_C)),
                "wheat_et0_mm": float(daily.loc[wheat, "et0_mm"].sum()),
                "spring_wind_ms": float(daily.loc[blowing, "wind_ms"].mean()),
                "erosion_climate": erosion_factor(annual_wind, pe_index(monthly_rain.to_numpy(), monthly_temp.to_numpy())),
                "annual_rain_mm": float(year_days["rain_mm"].sum()),
                "annual_et0_mm": float(year_days["et0_mm"].sum()),
                "annual_tmean_c": float(tmean[in_year].mean()),
            }
        )
    return pd.DataFrame(rows, columns=["year", *YEARLY_COLUMNS])


def monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """One row per calendar month the frame covers: year, month + MONTHLY_COLUMNS (means, totals, hot-day count)."""
    keys = [daily.index.year, daily.index.month]
    grouped = daily.groupby(keys)
    out = pd.DataFrame(
        {
            "tmax_c": grouped["tmax_c"].mean(),
            "tmin_c": grouped["tmin_c"].mean(),
            "tmean_c": ((daily["tmax_c"] + daily["tmin_c"]) / 2.0).groupby(keys).mean(),
            "rain_mm": grouped["rain_mm"].sum(),
            "et0_mm": grouped["et0_mm"].sum(),
            "wind_ms": grouped["wind_ms"].mean(),
            "hot_days": (daily["tmax_c"] > HOT_C).groupby(keys).sum().astype(float),
        }
    )
    out.index.names = ["year", "month"]
    out = out.reset_index()
    out["year"] = out["year"].astype("int64")
    out["month"] = out["month"].astype("int64")
    return out[["year", "month", *MONTHLY_COLUMNS]]
