"""Reference evapotranspiration: FAO-56 Penman-Monteith, daily (Allen et al. 1998, FAO Irrigation and
Drainage Paper 56, chapter 3 for the inputs and chapter 4 for the equation).

    ET0 = [0.408 D (Rn - G) + g 900/(T + 273) u2 (es - ea)] / [D + g (1 + 0.34 u2)]      mm/day

with T = (Tmax + Tmin)/2, G = 0 for a day, es = mean of the saturation pressures at Tmax and Tmin,
net radiation from measured solar radiation, latitude, elevation and day of year. The worked daily
example of chapter 4 (Example 18: Brussels, 6 July, 3.9 mm/day) is the test for this module.

Everything works on numbers or numpy arrays.
"""
from __future__ import annotations

import numpy as np

SOLAR_CONSTANT = 0.0820  # MJ/m2/min
STEFAN_BOLTZMANN = 4.903e-9  # MJ/K4/m2/day
ALBEDO = 0.23  # the grass reference crop
MIN_CLEARNESS = 0.3  # lower bound of Rs/Rso in the net longwave term (heavy overcast)


def wind_2m(speed_ms, height_m: float = 10.0):
    """Wind speed at 2 m from a speed measured at `height_m` (FAO-56 eq. 47, log profile over short grass)."""
    return np.asarray(speed_ms, dtype=float) * 4.87 / np.log(67.8 * height_m - 5.42)


def saturation_vapour_pressure(temp_c):
    """e0(T) in kPa (FAO-56 eq. 11)."""
    t = np.asarray(temp_c, dtype=float)
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def mean_saturation_vapour_pressure(tmax_c, tmin_c):
    """es in kPa: the mean of e0(Tmax) and e0(Tmin) (FAO-56 eq. 12)."""
    return (saturation_vapour_pressure(tmax_c) + saturation_vapour_pressure(tmin_c)) / 2.0


def vapour_pressure_from_rh_mean(tmax_c, tmin_c, rh_mean_pct):
    """ea in kPa from the daily mean relative humidity (FAO-56 eq. 19), the only humidity both sources give."""
    rh = np.clip(np.asarray(rh_mean_pct, dtype=float), 0.0, 100.0)
    return rh / 100.0 * mean_saturation_vapour_pressure(tmax_c, tmin_c)


def vapour_pressure_from_rh_max_min(tmax_c, tmin_c, rh_max_pct, rh_min_pct):
    """ea in kPa from the daily maximum and minimum relative humidity (FAO-56 eq. 17)."""
    return (
        saturation_vapour_pressure(tmin_c) * np.asarray(rh_max_pct, dtype=float) / 100.0
        + saturation_vapour_pressure(tmax_c) * np.asarray(rh_min_pct, dtype=float) / 100.0
    ) / 2.0


def slope_vapour_pressure_curve(temp_c):
    """Delta in kPa/C (FAO-56 eq. 13)."""
    t = np.asarray(temp_c, dtype=float)
    return 4098.0 * saturation_vapour_pressure(t) / (t + 237.3) ** 2


def atmospheric_pressure(elevation_m: float) -> float:
    """kPa (FAO-56 eq. 7)."""
    return 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26


def psychrometric_constant(elevation_m: float) -> float:
    """gamma in kPa/C (FAO-56 eq. 8)."""
    return 0.000665 * atmospheric_pressure(elevation_m)


def extraterrestrial_radiation(latitude_deg: float, day_of_year):
    """Ra in MJ/m2/day (FAO-56 eq. 21-25)."""
    j = np.asarray(day_of_year, dtype=float)
    phi = np.radians(latitude_deg)
    dr = 1.0 + 0.033 * np.cos(2.0 * np.pi * j / 365.0)
    delta = 0.409 * np.sin(2.0 * np.pi * j / 365.0 - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(delta), -1.0, 1.0))
    return (24.0 * 60.0 / np.pi) * SOLAR_CONSTANT * dr * (
        ws * np.sin(phi) * np.sin(delta) + np.cos(phi) * np.cos(delta) * np.sin(ws)
    )


def clear_sky_radiation(latitude_deg: float, elevation_m: float, day_of_year):
    """Rso in MJ/m2/day (FAO-56 eq. 37)."""
    return (0.75 + 2e-5 * elevation_m) * extraterrestrial_radiation(latitude_deg, day_of_year)


def net_radiation(tmax_c, tmin_c, ea_kpa, rs_mj, latitude_deg: float, elevation_m: float, day_of_year):
    """Rn in MJ/m2/day: net shortwave (eq. 38) minus net longwave (eq. 39)."""
    tmax_k = np.asarray(tmax_c, dtype=float) + 273.16
    tmin_k = np.asarray(tmin_c, dtype=float) + 273.16
    rs = np.asarray(rs_mj, dtype=float)
    rso = clear_sky_radiation(latitude_deg, elevation_m, day_of_year)
    clearness = np.clip(rs / rso, MIN_CLEARNESS, 1.0)
    rns = (1.0 - ALBEDO) * rs
    rnl = (
        STEFAN_BOLTZMANN * (tmax_k**4 + tmin_k**4) / 2.0
        * (0.34 - 0.14 * np.sqrt(np.asarray(ea_kpa, dtype=float)))
        * (1.35 * clearness - 0.35)
    )
    return rns - rnl


def penman_monteith(tmax_c, tmin_c, ea_kpa, u2_ms, rs_mj, latitude_deg: float, elevation_m: float, day_of_year):
    """Daily grass reference ET in mm/day (FAO-56 eq. 6), never below 0.

    tmax_c, tmin_c: daily extremes in C; ea_kpa: actual vapour pressure; u2_ms: wind speed at 2 m;
    rs_mj: solar radiation in MJ/m2/day; day_of_year: 1-366.
    """
    tmax = np.asarray(tmax_c, dtype=float)
    tmin = np.asarray(tmin_c, dtype=float)
    ea = np.asarray(ea_kpa, dtype=float)
    u2 = np.asarray(u2_ms, dtype=float)
    tmean = (tmax + tmin) / 2.0
    es = mean_saturation_vapour_pressure(tmax, tmin)
    delta = slope_vapour_pressure_curve(tmean)
    gamma = psychrometric_constant(elevation_m)
    rn = net_radiation(tmax, tmin, ea, rs_mj, latitude_deg, elevation_m, day_of_year)
    et0 = (0.408 * delta * rn + gamma * 900.0 / (tmean + 273.0) * u2 * np.maximum(es - ea, 0.0)) / (
        delta + gamma * (1.0 + 0.34 * u2)
    )
    return np.maximum(et0, 0.0)


def et0_from_daily(daily, latitude_deg: float, elevation_m: float, wind2_column: str | None = None):
    """ET0 (mm/day, numpy array) for a daily frame with tmax_c, tmin_c, rh_pct, wind_ms (10 m), rs_mj and a
    DatetimeIndex. Wind at 2 m is `wind2_column` when given, else the 10 m wind through the FAO log profile.
    """
    u2 = daily[wind2_column].to_numpy(dtype=float) if wind2_column else wind_2m(daily["wind_ms"].to_numpy(dtype=float), 10.0)
    ea = vapour_pressure_from_rh_mean(daily["tmax_c"].to_numpy(), daily["tmin_c"].to_numpy(), daily["rh_pct"].to_numpy())
    return penman_monteith(
        daily["tmax_c"].to_numpy(), daily["tmin_c"].to_numpy(), ea, u2, daily["rs_mj"].to_numpy(),
        latitude_deg, elevation_m, daily.index.day_of_year.to_numpy(),
    )
