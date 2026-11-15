"""From raw daily model output to a corrected daily series: units, calendars, delta-method bias correction.

Daily frames everywhere in this package have a DatetimeIndex (every real calendar day) and the columns
DAILY_COLUMNS: tmax_c, tmin_c (C), rain_mm (mm/day), wind_ms (m/s at 10 m), rh_pct (%), rs_mj (MJ/m2/day).

Bias correction ("delta method"), by calendar month, baseline 2000-2024 against the observed series:

    temperatures (Tmax and Tmin separately) and humidity   add  (observed monthly mean - model monthly mean)
    rain, wind, solar radiation                            multiply by (observed mean / model mean), clipped

so the corrected model baseline has the observed monthly climate by construction and every later
change is the model's own. Humidity is shifted, not scaled, because relative humidity is bounded and
a shift keeps the model's dry-day / humid-day spread; it is then kept inside 1-100 %.

A shift of the mean does not fix how spread out the days are. `baseline_checks` compares, per model,
the corrected baseline with the observed series on days above 35 C, longest dry spell and rain days.
Where days above 35 C are off by more than SPREAD_TOLERANCE, `scale_tmax_spread` stretches or shrinks
the daily Tmax departures from that month's own mean (one factor per calendar month = observed /
model spread inside a month). Monthly means, and so the model's warming, are left exactly as they were.
No quantile mapping.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.climate.indicators import GS_MONTHS, HOT_C, WET_MM, longest_dry_spell

DAILY_COLUMNS = ["tmax_c", "tmin_c", "rain_mm", "wind_ms", "rh_pct", "rs_mj"]
BASELINE = (2000, 2024)
ADDITIVE = ("tmax_c", "tmin_c", "rh_pct")
RATIO_LIMITS = {"rain_mm": (0.25, 4.0), "wind_ms": (0.5, 2.0), "rs_mj": (0.5, 2.0)}  # sane range of a monthly ratio
RH_RANGE = (1.0, 100.0)
SPREAD_TOLERANCE = 0.25  # days above 35 C may be off by this share before the Tmax spread is scaled
SPREAD_LIMITS = (0.5, 2.0)

# NASA/GDDP-CMIP6 band -> our column
GDDP_BANDS = ("hurs", "huss", "pr", "rlds", "rsds", "sfcWind", "tas", "tasmax", "tasmin")


# --------------------------------------------------------------------------- units


def kelvin_to_c(kelvin):
    return np.asarray(kelvin, dtype=float) - 273.15


def pr_to_mm_day(kg_m2_s):
    """Precipitation flux kg/m2/s -> mm/day (1 kg/m2 of water is 1 mm; 86,400 seconds in a day)."""
    return np.asarray(kg_m2_s, dtype=float) * 86400.0


def wm2_to_mj_day(w_m2):
    """Mean radiation flux W/m2 -> MJ/m2/day (x 86,400 s / 1e6)."""
    return np.asarray(w_m2, dtype=float) * 0.0864


# --------------------------------------------------------------------------- calendars


def to_real_calendar(dates, values) -> tuple[pd.DatetimeIndex, np.ndarray, dict]:
    """Put daily rows onto the real calendar: (every day from the first to the last date, values, info).

    `dates` are 'YYYY-MM-DD' strings, `values` one row per date (None / NaN = no value).

    - A real day that has no row, or a row without values (29 February of a 365-day model calendar),
      takes the values of the day before (`info["filled"]` counts them).
    - A year that contains dates the real calendar does not have (30 February: a 360-day model
      calendar) is spread over the real year in proportion, real day k of N taking model day
      floor((k + 0.5) x n / N); 5 or 6 model days are then used twice (`info["respread_years"]`).
      Only whole years can be spread this way.

    Earth Engine's NASA/GDDP-CMIP6 already arrives on the real calendar (it repeats a day where the
    model has none, see `repeated_days`), so for that source nothing is filled; this function is what
    guarantees a complete calendar whatever the source does.
    """
    values = np.array([[np.nan if v is None else v for v in row] for row in values], dtype=float)
    if values.ndim != 2 or len(values) != len(dates) or len(dates) == 0:
        raise ValueError("to_real_calendar needs one row of values per date")
    parsed = pd.to_datetime(pd.Series(list(dates)), format="%Y-%m-%d", errors="coerce")
    years = np.array([int(str(d)[:4]) for d in dates])
    info = {"filled": 0, "respread_years": 0}
    pieces = []
    for year in sorted(set(years)):
        rows = np.flatnonzero(years == year)
        usable = rows[~np.isnan(values[rows]).any(axis=1)]
        if parsed.iloc[rows].isna().any():
            if str(dates[rows[0]])[5:] != "01-01":
                raise ValueError(f"{year}: dates outside the real calendar can only be handled for a whole year")
            real = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
            source = np.floor((np.arange(len(real)) + 0.5) * len(usable) / len(real)).astype(int)
            pieces.append(pd.DataFrame(values[usable][source], index=real))
            info["respread_years"] += 1
        else:
            pieces.append(pd.DataFrame(values[usable], index=pd.DatetimeIndex(parsed.iloc[usable])))
    frame = pd.concat(pieces).sort_index()
    if frame.index.has_duplicates:
        raise ValueError("a date appears twice")
    real_dates = parsed.dropna()  # includes real days whose row has no values
    first = min([frame.index[0], *([real_dates.min()] if len(real_dates) else [])])
    last = max([frame.index[-1], *([real_dates.max()] if len(real_dates) else [])])
    full = pd.date_range(first, last, freq="D")
    info["filled"] = int(len(full) - len(frame))
    frame = frame.reindex(full).ffill().bfill()
    return full, frame.to_numpy(), info


def repeated_days(values) -> int:
    """Days whose values are all identical to the day before: how a source pads a shorter model calendar."""
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return 0
    return int(np.all(values[1:] == values[:-1], axis=1).sum())


def gddp_daily(rows) -> tuple[pd.DataFrame, dict]:
    """Rows [[date, hurs, huss, pr, rlds, rsds, sfcWind, tas, tasmax, tasmin], ...] -> (daily frame, calendar info).

    Units: K -> C, kg/m2/s -> mm/day, W/m2 -> MJ/m2/day; sfcWind is already m/s at 10 m, hurs is %.
    info = {"days", "filled", "respread_years", "repeated", "repeated_by_year": {year: n}}.
    """
    index, values, info = to_real_calendar([r[0] for r in rows], [r[1:] for r in rows])
    raw = pd.DataFrame(values, index=index, columns=list(GDDP_BANDS))
    daily = pd.DataFrame(
        {
            "tmax_c": kelvin_to_c(raw["tasmax"]),
            "tmin_c": kelvin_to_c(raw["tasmin"]),
            "rain_mm": np.maximum(pr_to_mm_day(raw["pr"]), 0.0),
            "wind_ms": raw["sfcWind"].to_numpy(dtype=float),
            "rh_pct": np.clip(raw["hurs"].to_numpy(dtype=float), 0.0, 100.0),
            "rs_mj": wm2_to_mj_day(raw["rsds"]),
        },
        index=index,
    )
    daily.index.name = "date"
    same = np.concatenate([[False], np.all(values[1:] == values[:-1], axis=1)])
    by_year = pd.Series(same, index=index).groupby(index.year).sum()
    info = {**info, "days": int(len(daily)), "repeated": int(same.sum()), "repeated_by_year": {int(y): int(n) for y, n in by_year.items()}}
    return daily, info


# --------------------------------------------------------------------------- delta method


def _baseline(frame: pd.DataFrame, baseline=BASELINE) -> pd.DataFrame:
    return frame[(frame.index.year >= baseline[0]) & (frame.index.year <= baseline[1])]


def monthly_means(daily: pd.DataFrame, baseline=BASELINE) -> pd.DataFrame:
    """Mean of the daily values in each calendar month (index 1-12) over the baseline years."""
    part = _baseline(daily, baseline)[DAILY_COLUMNS]
    means = part.groupby(part.index.month).mean()
    if len(means) != 12:
        raise ValueError(f"the baseline {baseline[0]}-{baseline[1]} does not cover all 12 calendar months")
    return means


def correction_factors(model: pd.DataFrame, observed: pd.DataFrame, baseline=BASELINE) -> tuple[pd.DataFrame, dict]:
    """(factors, clipped). factors: index 1-12, one column per variable: a shift for ADDITIVE columns, a
    ratio for the others. clipped = {column: [months whose ratio hit RATIO_LIMITS]}."""
    m, o = monthly_means(model, baseline), monthly_means(observed, baseline)
    factors = pd.DataFrame(index=m.index, columns=DAILY_COLUMNS, dtype=float)
    clipped: dict = {}
    for column in DAILY_COLUMNS:
        if column in ADDITIVE:
            factors[column] = o[column] - m[column]
            continue
        lo, hi = RATIO_LIMITS[column]
        ratio = (o[column] / m[column].where(m[column] > 0)).fillna(1.0)
        hit = [int(month) for month in ratio.index[(ratio < lo) | (ratio > hi)]]
        if hit:
            clipped[column] = hit
        factors[column] = ratio.clip(lo, hi)
    return factors, clipped


def apply_correction(model: pd.DataFrame, factors: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """(corrected daily frame, notes). Each day gets its calendar month's shift or ratio.

    Afterwards humidity is kept inside RH_RANGE, and on a day where the two separate shifts leave
    Tmin above Tmax both are set to their mean. notes counts both kinds of day.
    """
    months = model.index.month
    out = model[DAILY_COLUMNS].copy()
    for column in DAILY_COLUMNS:
        by_day = factors[column].reindex(months).to_numpy()
        out[column] = out[column].to_numpy() + by_day if column in ADDITIVE else out[column].to_numpy() * by_day
    rh = out["rh_pct"].to_numpy()
    notes = {"rh_clipped_days": int(((rh < RH_RANGE[0]) | (rh > RH_RANGE[1])).sum())}
    out["rh_pct"] = np.clip(rh, *RH_RANGE)
    notes["tmin_above_tmax_days"] = _fix_crossed(out)
    return out, notes


def _fix_crossed(daily: pd.DataFrame) -> int:
    crossed = (daily["tmin_c"] > daily["tmax_c"]).to_numpy()
    if crossed.any():
        middle = (daily.loc[crossed, "tmax_c"] + daily.loc[crossed, "tmin_c"]) / 2.0
        daily.loc[crossed, "tmax_c"] = middle
        daily.loc[crossed, "tmin_c"] = middle
    return int(crossed.sum())


# --------------------------------------------------------------------------- spread of daily Tmax


def _month_keys(index: pd.DatetimeIndex) -> np.ndarray:
    return index.year * 12 + (index.month - 1)


def within_month_sd(series: pd.Series, baseline=BASELINE) -> pd.Series:
    """Spread of the daily values about their own month's mean, pooled by calendar month (index 1-12)."""
    part = series[(series.index.year >= baseline[0]) & (series.index.year <= baseline[1])]
    departures = part - part.groupby(_month_keys(part.index)).transform("mean")
    return np.sqrt((departures**2).groupby(part.index.month).mean())


def spread_factors(model_tmax: pd.Series, observed_tmax: pd.Series, baseline=BASELINE) -> pd.Series:
    """One factor per calendar month: observed / model spread of daily Tmax inside a month, kept in SPREAD_LIMITS."""
    m, o = within_month_sd(model_tmax, baseline), within_month_sd(observed_tmax, baseline)
    return (o / m.where(m > 0)).fillna(1.0).clip(*SPREAD_LIMITS)


def scale_tmax_spread(daily: pd.DataFrame, factors: pd.Series) -> tuple[pd.DataFrame, int]:
    """Daily Tmax departures from each month's own mean times that calendar month's factor.

    Every month keeps its mean Tmax exactly, in the baseline and in the future. Returns the new frame
    and the number of days where Tmin then had to be pulled down to Tmax.
    """
    out = daily.copy()
    tmax = out["tmax_c"]
    month_mean = tmax.groupby(_month_keys(tmax.index)).transform("mean")
    out["tmax_c"] = month_mean + (tmax - month_mean) * factors.reindex(tmax.index.month).to_numpy()
    crossed = (out["tmin_c"] > out["tmax_c"]).to_numpy()
    out.loc[crossed, "tmin_c"] = out.loc[crossed, "tmax_c"]
    return out, int(crossed.sum())


# --------------------------------------------------------------------------- checks a mean shift does not guarantee


def baseline_checks(daily: pd.DataFrame, baseline=BASELINE) -> dict:
    """Per-year means over the baseline: growing-season days above 35 C, longest growing-season dry
    spell (rain < 1 mm), rain days (>= 1 mm) in the growing season and in the whole year."""
    part = _baseline(daily, baseline)
    gs = part[part.index.month.isin(GS_MONTHS)]
    years = sorted(set(part.index.year))
    return {
        "hot_days": float((gs["tmax_c"] > HOT_C).sum() / len(years)),
        "dry_spell_days": float(np.mean([longest_dry_spell(gs.loc[gs.index.year == y, "rain_mm"]) for y in years])),
        "gs_rain_days": float((gs["rain_mm"] >= WET_MM).sum() / len(years)),
        "rain_days": float((part["rain_mm"] >= WET_MM).sum() / len(years)),
    }


def needs_spread_scaling(model_hot_days: float, observed_hot_days: float, tolerance: float = SPREAD_TOLERANCE) -> bool:
    if observed_hot_days <= 0:
        return model_hot_days > 0
    return abs(model_hot_days / observed_hot_days - 1.0) > tolerance
