"""Phase 3 climate tables: units, calendars, ET0, indicators, bias correction, fetch plumbing, validator.

Nothing here touches the network or Earth Engine, and nothing writes under data/ (tmp_path only).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from pipeline.climate import bias, build, et0, fetch_gddp, fetch_power, indicators, schema

# --------------------------------------------------------------------------- helpers


def synthetic_daily(start="1999-10-01", end="2024-12-31", *, seed=0, warm=0.0, wet=1.0, windy=1.0, trend=0.0) -> pd.DataFrame:
    """A plausible Kansas-like daily series; `warm`, `wet`, `windy` bias it, `trend` warms it (C per year)."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, end, freq="D")
    season = -np.cos(2 * np.pi * (index.day_of_year.to_numpy() - 15) / 365.25)  # -1 mid January, +1 mid July
    years = (index.year - 2000).to_numpy()
    noise = rng.normal(0, 4.0, len(index))
    tmax = 20.0 + 14.0 * season + noise + warm + trend * years
    tmin = tmax - 13.0 + rng.normal(0, 1.5, len(index))
    rain = np.where(rng.random(len(index)) < 0.2 + 0.05 * season, rng.gamma(0.8, 8.0, len(index)), 0.0) * wet
    return pd.DataFrame(
        {
            "tmax_c": tmax, "tmin_c": tmin, "rain_mm": rain,
            "wind_ms": (5.0 + rng.normal(0, 1.2, len(index))).clip(0.5) * windy,
            "rh_pct": (55.0 - 10.0 * season + rng.normal(0, 8.0, len(index))).clip(10, 95),
            "rs_mj": (17.0 + 9.0 * season + rng.normal(0, 2.0, len(index))).clip(1.0),
        },
        index=index,
    )


SITE = {"latitude": 38.0, "longitude": -100.67, "elevation_m": 860.0, "missing": {}}


@pytest.fixture(scope="module")
def built():
    """The three tables built from synthetic series: 2 models x 2 scenarios, no downloads."""
    observed = synthetic_daily(seed=1)
    observed["wind2_ms"] = observed["wind_ms"] * 0.7
    series = {}
    for i, model in enumerate(["MODEL-A", "MODEL-B"]):
        for j, scenario in enumerate(schema.SCENARIOS):
            series[(model, scenario)] = synthetic_daily(
                end="2060-12-31", seed=10 + 3 * i + j, warm=2.0 - 3.0 * i, wet=1.3, windy=0.7, trend=0.03 + 0.03 * j
            )
    return build.build_tables(observed, SITE, series), observed, series


# --------------------------------------------------------------------------- units


def test_unit_conversions():
    assert bias.kelvin_to_c(273.15) == pytest.approx(0.0)
    assert bias.kelvin_to_c([300.0, 250.0]) == pytest.approx([26.85, -23.15])
    assert bias.pr_to_mm_day(1.0 / 86400.0) == pytest.approx(1.0)  # 1 kg/m2 a day is 1 mm
    assert bias.pr_to_mm_day(5.787e-5) == pytest.approx(5.0, abs=1e-3)
    assert bias.wm2_to_mj_day(100.0) == pytest.approx(8.64)  # 100 W/m2 for a day
    assert bias.wm2_to_mj_day(11.574) == pytest.approx(1.0, abs=1e-4)


def test_wind_from_10m_to_2m_fao_log_profile():
    # FAO-56 example 14 / 18: 10 km/h at 10 m is 2.078 m/s at 2 m; the factor at 10 m is 0.748
    assert et0.wind_2m(10.0 / 3.6, 10.0) == pytest.approx(2.078, abs=0.002)
    assert et0.wind_2m(1.0, 10.0) == pytest.approx(0.748, abs=0.001)
    assert et0.wind_2m(3.0, 2.0) == pytest.approx(3.0, abs=0.01)  # already at 2 m


def test_gddp_rows_are_converted_to_our_units():
    #          date         hurs  huss   pr        rlds   rsds   wind tas    tasmax tasmin
    rows = [["2020-07-01", 55.0, 0.01, 2.0e-5, 400.0, 300.0, 4.0, 300.0, 308.15, 293.15],
            ["2020-07-02", 120.0, 0.01, 0.0, 400.0, 250.0, 3.0, 299.0, 303.15, 291.15]]
    daily, info = bias.gddp_daily(rows)
    assert list(daily.columns) == bias.DAILY_COLUMNS
    first = daily.iloc[0]
    assert first["tmax_c"] == pytest.approx(35.0) and first["tmin_c"] == pytest.approx(20.0)
    assert first["rain_mm"] == pytest.approx(1.728)
    assert first["rs_mj"] == pytest.approx(25.92)
    assert first["wind_ms"] == 4.0 and first["rh_pct"] == 55.0
    assert daily.iloc[1]["rh_pct"] == 100.0  # humidity is kept inside 0-100
    assert info["days"] == 2 and info["filled"] == 0 and info["repeated"] == 0


# --------------------------------------------------------------------------- ET0


def test_et0_fao56_worked_daily_example():
    """FAO-56 chapter 4, example 18: Brussels (50 deg 48' N, 100 m), 6 July -> 3.9 mm/day."""
    ea = et0.vapour_pressure_from_rh_max_min(21.5, 12.3, 84.0, 63.0)
    assert float(ea) == pytest.approx(1.409, abs=0.002)
    assert float(et0.mean_saturation_vapour_pressure(21.5, 12.3)) == pytest.approx(1.997, abs=0.002)
    assert float(et0.slope_vapour_pressure_curve(16.9)) == pytest.approx(0.122, abs=0.001)
    assert et0.psychrometric_constant(100.0) == pytest.approx(0.0666, abs=0.0002)
    assert float(et0.extraterrestrial_radiation(50.8, 187)) == pytest.approx(41.09, abs=0.05)
    assert float(et0.clear_sky_radiation(50.8, 100.0, 187)) == pytest.approx(30.90, abs=0.05)
    assert float(et0.net_radiation(21.5, 12.3, ea, 22.07, 50.8, 100.0, 187)) == pytest.approx(13.28, abs=0.05)
    value = float(et0.penman_monteith(21.5, 12.3, ea, 2.078, 22.07, 50.8, 100.0, 187))
    assert value == pytest.approx(3.88, abs=0.03)
    assert round(value, 1) == 3.9


def test_et0_behaves_and_works_on_a_daily_frame():
    base = dict(tmax_c=32.0, tmin_c=17.0, ea_kpa=1.5, u2_ms=3.0, rs_mj=26.0, latitude_deg=38.0, elevation_m=860.0, day_of_year=190)
    middle = float(et0.penman_monteith(**base))
    assert 6.0 < middle < 10.0  # a hot, windy July day on the High Plains
    assert float(et0.penman_monteith(**{**base, "u2_ms": 5.0})) > middle  # more wind, more ET
    assert float(et0.penman_monteith(**{**base, "ea_kpa": 2.5})) < middle  # more humid, less ET
    assert float(et0.penman_monteith(**{**base, "tmax_c": 36.0})) > middle
    assert float(et0.penman_monteith(-5.0, -15.0, 0.25, 0.5, 0.5, 38.0, 860.0, 10)) >= 0.0  # never negative
    assert float(et0.vapour_pressure_from_rh_mean(25.0, 15.0, 50.0)) == pytest.approx(0.5 * float(et0.mean_saturation_vapour_pressure(25.0, 15.0)))
    daily = synthetic_daily("2020-01-01", "2020-12-31")
    values = et0.et0_from_daily(daily, 38.0, 860.0)
    assert values.shape == (366,) and np.isfinite(values).all() and (values >= 0).all()
    assert 900 < values.sum() < 2200
    assert values[180:210].mean() > 2 * values[:30].mean()  # July well above January
    daily["wind2_ms"] = daily["wind_ms"] * 0.5
    assert et0.et0_from_daily(daily, 38.0, 860.0, "wind2_ms").sum() < values.sum()


# --------------------------------------------------------------------------- indicators


def test_gdd_caps_tmax_at_30_and_floors_tmin_at_10():
    assert indicators.gdd_daily(25.0, 15.0) == pytest.approx(10.0)
    assert indicators.gdd_daily(38.0, 20.0) == pytest.approx(15.0)  # Tmax counted as 30
    assert indicators.gdd_daily(24.0, 2.0) == pytest.approx(7.0)  # Tmin counted as 10
    assert indicators.gdd_daily(8.0, -3.0) == pytest.approx(0.0)  # a cold day adds nothing, never negative
    assert indicators.gdd_daily([40.0, 25.0], [32.0, 15.0]) == pytest.approx([20.0, 10.0])


def test_hot_days_counts_strictly_above_the_threshold():
    tmax = [34.9, 35.0, 35.1, 40.0, 20.0]
    assert indicators.hot_days(tmax) == 2
    assert indicators.hot_days(tmax, 32.0) == 4
    assert indicators.hot_days([]) == 0


def test_longest_dry_spell():
    assert indicators.longest_dry_spell([0, 0, 5, 0, 0, 0, 0.9, 1.0, 0]) == 4  # 0.9 mm is still dry, 1.0 mm is not
    assert indicators.longest_dry_spell([3, 2, 1]) == 0
    assert indicators.longest_dry_spell([0, 0, 0]) == 3
    assert indicators.longest_dry_spell([]) == 0


def test_pe_index_and_erosion_factor():
    # every month 2 inches of rain at 60 F: PE = 12 x 115 x (2 / 50)^(10/9)
    pe = indicators.pe_index([50.8] * 12, [(60.0 - 32.0) / 1.8] * 12)
    assert pe == pytest.approx(12 * 115 * (2.0 / 50.0) ** (10.0 / 9.0), rel=1e-6)
    # floors: a month never counts less than 0.5 inch of rain or colder than 28.4 F
    assert indicators.pe_index([0.0] * 12, [20.0] * 12) == pytest.approx(indicators.pe_index([12.7] * 12, [20.0] * 12))
    assert indicators.pe_index([30.0] * 12, [-20.0] * 12) == pytest.approx(indicators.pe_index([30.0] * 12, [-2.0] * 12))
    with pytest.raises(ValueError):
        indicators.pe_index([1.0] * 11, [1.0] * 12)
    # C = 386 u^3 / PE^2 with u in m/s; the same number as 34.483 u^3 / PE^2 with u in mph
    assert indicators.erosion_factor(5.0, 30.0) == pytest.approx(386.0 * 125.0 / 900.0)
    assert indicators.erosion_factor(5.0, 30.0) == pytest.approx(34.483 * (5.0 * 2.23694) ** 3 / 900.0, rel=2e-3)
    assert indicators.erosion_factor(6.0, 30.0) > indicators.erosion_factor(5.0, 30.0)  # windier is worse
    assert indicators.erosion_factor(5.0, 20.0) > indicators.erosion_factor(5.0, 30.0)  # drier is worse


def test_erosion_scale_makes_the_observed_mean_100_even_with_a_capped_year():
    raw = np.array([20.0, 30.0, 40.0, 50.0, 400.0])
    k = indicators.erosion_scale(raw)
    scaled = indicators.scale_erosion(raw, k)
    assert scaled.mean() == pytest.approx(100.0, abs=1e-6)
    assert scaled.max() == indicators.EROSION_CAP
    plain = np.array([40.0, 50.0, 60.0])
    assert indicators.erosion_scale(plain) == pytest.approx(2.0)


def test_yearly_indicators_on_a_series_with_known_answers():
    index = pd.date_range("1999-10-01", "2001-12-31", freq="D")
    daily = pd.DataFrame({"tmax_c": 25.0, "tmin_c": 15.0, "rain_mm": 0.0, "wind_ms": 4.0, "et0_mm": 5.0}, index=index)
    daily.loc["2000-07-10":"2000-07-19", "tmax_c"] = 36.0  # 10 hot days in the growing season
    daily.loc["2000-05-01":"2000-05-05", "tmax_c"] = 33.0  # 5 days above 32 in grain fill
    daily.loc["2000-10-15", "tmax_c"] = 40.0  # outside the growing season: not a hot day
    daily.loc["2000-06-15", "rain_mm"] = 30.0
    daily.loc["2000-12-01", "rain_mm"] = 12.0  # wheat season of 2001
    daily.loc["1999-11-01", "rain_mm"] = 7.0  # wheat season of 2000 starts in 1999
    daily.loc["2000-02-01":"2000-05-31", "wind_ms"] = 6.0
    out = indicators.yearly(daily, 2000, 2001).set_index("year")
    y = out.loc[2000]
    assert list(out.columns) == schema.YEARLY_COLUMNS
    assert y["hot_days"] == 10 and y["wheat_hot_days"] == 5 + 0
    assert y["rain_mm"] == 30.0 and y["summer_rain_mm"] == 30.0 and y["annual_rain_mm"] == 42.0
    assert y["wheat_rain_mm"] == 37.0  # 1 Nov 1999 + 15 Jun 2000
    assert out.loc[2001, "wheat_rain_mm"] == 12.0  # 1 Dec 2000 belongs to the 2001 wheat season
    assert y["et0_mm"] == 183 * 5.0 and y["annual_et0_mm"] == 366 * 5.0
    assert y["wheat_et0_mm"] == (92 + 182) * 5.0  # Oct-Dec 1999 + Jan-Jun 2000 (leap year)
    assert out.loc[2001, "wheat_et0_mm"] == (92 + 181) * 5.0
    assert y["dry_spell_days"] == 107  # 16 June to 30 September
    assert y["spring_wind_ms"] == pytest.approx(6.0) and 4.0 < y["wind_ms"] < 6.0
    assert out.loc[2001, "tmean_c"] == pytest.approx(20.0) and out.loc[2001, "gdd"] == pytest.approx(183 * 10.0)
    assert y["erosion_climate"] > 0


def test_yearly_needs_the_autumn_before_the_first_wheat_season():
    index = pd.date_range("2000-01-01", "2000-12-31", freq="D")
    daily = pd.DataFrame({"tmax_c": 25.0, "tmin_c": 15.0, "rain_mm": 1.0, "wind_ms": 4.0, "et0_mm": 5.0}, index=index)
    with pytest.raises(ValueError, match="1 Oct 1999"):
        indicators.yearly(daily, 2000, 2000)


def test_monthly_table_totals_and_means():
    index = pd.date_range("2001-01-01", "2001-03-31", freq="D")
    daily = pd.DataFrame({"tmax_c": 20.0, "tmin_c": 10.0, "rain_mm": 2.0, "wind_ms": 4.0, "et0_mm": 3.0}, index=index)
    daily.loc["2001-02-01":"2001-02-03", "tmax_c"] = 36.0
    out = indicators.monthly(daily).set_index(["year", "month"])
    assert list(out.columns) == schema.MONTHLY_VALUE_COLUMNS
    assert out.loc[(2001, 1), "rain_mm"] == 62.0 and out.loc[(2001, 2), "rain_mm"] == 56.0
    assert out.loc[(2001, 3), "et0_mm"] == 93.0
    assert out.loc[(2001, 2), "hot_days"] == 3.0 and out.loc[(2001, 1), "hot_days"] == 0.0
    assert out.loc[(2001, 1), "tmean_c"] == 15.0 and out.loc[(2001, 1), "wind_ms"] == 4.0


# --------------------------------------------------------------------------- calendars


def _rows(dates, value=1.0):
    return [[value + i] for i in range(len(dates))]


def test_calendar_a_full_real_year_is_left_alone():
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2016-01-01", "2016-12-31")]
    index, values, info = bias.to_real_calendar(dates, _rows(dates))
    assert len(index) == 366 and info == {"filled": 0, "respread_years": 0}
    assert values[:, 0].tolist() == [1.0 + i for i in range(366)]


def test_calendar_missing_leap_day_is_filled_from_the_day_before():
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2016-01-01", "2016-12-31") if not (d.month == 2 and d.day == 29)]
    index, values, info = bias.to_real_calendar(dates, _rows(dates))
    assert len(index) == 366 and info["filled"] == 1
    feb28, feb29, mar1 = (values[index.get_loc(pd.Timestamp(d)), 0] for d in ("2016-02-28", "2016-02-29", "2016-03-01"))
    assert feb29 == feb28 and mar1 == feb28 + 1


def test_calendar_a_day_without_values_is_filled_too():
    dates = ["2015-12-30", "2015-12-31", "2016-01-01"]
    index, values, info = bias.to_real_calendar(dates, [[5.0, 1.0], [None, None], [7.0, 3.0]])
    assert info["filled"] == 1 and values.tolist() == [[5.0, 1.0], [5.0, 1.0], [7.0, 3.0]]


def test_calendar_360_day_year_is_spread_over_the_real_year():
    dates = [f"2015-{month:02d}-{day:02d}" for month in range(1, 13) for day in range(1, 31)]  # includes 30 February
    index, values, info = bias.to_real_calendar(dates, _rows(dates, 0.0))
    assert len(index) == 365 and info["respread_years"] == 1
    assert index[0] == pd.Timestamp("2015-01-01") and index[-1] == pd.Timestamp("2015-12-31")
    used = values[:, 0]
    assert used[0] == 0 and used[-1] == 359 and (np.diff(used) >= 0).all()
    assert len(set(used)) == 360 and bias.repeated_days(values) == 5  # every model day used, 5 used twice
    # the season stays in place: model day 180 (1 July in a 360-day year) lands within two days of 1 July
    assert abs(int(np.flatnonzero(used == 180)[0]) - (pd.Timestamp("2015-07-01").day_of_year - 1)) <= 2


def test_calendar_earth_engine_padding_is_counted_not_changed():
    """What Earth Engine returns for a 365-day model in a leap year: 30 December again on the 31st."""
    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2016-01-01", "2016-12-31")]
    rows = [[d, *([float(i)] * 9)] for i, d in enumerate(dates)]
    rows[-1] = [dates[-1], *rows[-2][1:]]
    daily, info = bias.gddp_daily(rows)
    assert len(daily) == 366 and info["filled"] == 0 and info["repeated"] == 1 and info["repeated_by_year"] == {2016: 1}


def test_calendar_rejects_duplicate_dates_and_partial_model_years():
    with pytest.raises(ValueError):
        bias.to_real_calendar(["2016-01-01", "2016-01-01"], [[1.0], [2.0]])
    with pytest.raises(ValueError, match="whole year"):
        bias.to_real_calendar(["2016-02-29", "2016-02-30"], [[1.0], [2.0]])


# --------------------------------------------------------------------------- bias correction


def test_bias_correction_gives_the_observed_monthly_means():
    observed = synthetic_daily(seed=1)
    model = synthetic_daily(end="2060-12-31", seed=2, warm=3.0, wet=1.6, windy=0.6, trend=0.04)
    model["rs_mj"] *= 1.2
    model["rh_pct"] = (model["rh_pct"] + 6.0).clip(0, 100)
    factors, clipped = bias.correction_factors(model, observed)
    corrected, notes = bias.apply_correction(model, factors)
    assert clipped == {} and notes["rh_clipped_days"] == 0
    want, got, before = bias.monthly_means(observed), bias.monthly_means(corrected), bias.monthly_means(model)
    for column in ("rain_mm", "wind_ms", "rs_mj", "rh_pct"):
        assert np.allclose(got[column], want[column], rtol=1e-9), column
        assert not np.allclose(before[column], want[column], rtol=0.02), column  # the raw model really was biased
    for column in ("tmax_c", "tmin_c"):  # exact but for the rare day where Tmin and Tmax had to be repaired
        assert np.allclose(got[column], want[column], atol=0.01), column
        assert not np.allclose(before[column], want[column], atol=0.5), column
    assert (corrected["tmin_c"] <= corrected["tmax_c"]).all() and (corrected["rain_mm"] >= 0).all()
    assert corrected["rh_pct"].between(*bias.RH_RANGE).all()
    assert not corrected.isna().any().any() and len(corrected) == len(model)


def test_bias_correction_keeps_the_models_own_change():
    observed = synthetic_daily(seed=1)
    model = synthetic_daily(end="2060-12-31", seed=2, warm=3.0, wet=1.6, trend=0.05)
    corrected, _ = bias.apply_correction(model, bias.correction_factors(model, observed)[0])

    def change(frame, column, how):
        late, early = frame.loc["2050":"2059", column], frame.loc["2000":"2024", column]
        return late.mean() - early.mean() if how == "abs" else late.mean() / early.mean()

    assert change(corrected, "tmax_c", "abs") == pytest.approx(change(model, "tmax_c", "abs"), abs=0.05)  # same warming
    assert change(corrected, "rain_mm", "ratio") == pytest.approx(change(model, "rain_mm", "ratio"), rel=0.05)  # same relative rain change


def test_bias_ratio_is_clipped_and_reported():
    observed = synthetic_daily(seed=1)
    model = synthetic_daily(seed=2)
    model.loc[model.index.month == 7, "rain_mm"] *= 0.01  # a model with almost no July rain
    model.loc[model.index.month == 3, "rain_mm"] = 0.0  # and none at all in March
    factors, clipped = bias.correction_factors(model, observed)
    assert clipped == {"rain_mm": [7]}
    assert factors.loc[7, "rain_mm"] == bias.RATIO_LIMITS["rain_mm"][1]
    assert factors.loc[3, "rain_mm"] == 1.0  # nothing to scale


def test_tmax_spread_scaling_keeps_every_monthly_mean():
    observed = synthetic_daily(seed=1)
    model = synthetic_daily(end="2030-12-31", seed=3, trend=0.05)
    flat = model.copy()
    month_mean = flat["tmax_c"].groupby([flat.index.year, flat.index.month]).transform("mean")
    flat["tmax_c"] = month_mean + (flat["tmax_c"] - month_mean) * 0.6  # a model with too little day-to-day spread
    flat["tmin_c"] = flat["tmax_c"] - 13.0
    factors = bias.spread_factors(flat["tmax_c"], observed["tmax_c"])
    assert np.allclose(factors, 1 / 0.6, atol=0.15)
    scaled, crossed = bias.scale_tmax_spread(flat, factors)
    keys = [scaled.index.year, scaled.index.month]
    assert np.allclose(scaled["tmax_c"].groupby(keys).mean(), flat["tmax_c"].groupby(keys).mean(), atol=1e-9)
    assert bias.within_month_sd(scaled["tmax_c"]).mean() == pytest.approx(bias.within_month_sd(observed["tmax_c"]).mean(), rel=0.05)
    assert bias.baseline_checks(scaled)["hot_days"] > bias.baseline_checks(flat)["hot_days"]
    assert (scaled["tmin_c"] <= scaled["tmax_c"]).all() and crossed >= 0


def test_baseline_checks_and_the_25_percent_rule():
    index = pd.date_range("2000-01-01", "2001-12-31", freq="D")
    daily = pd.DataFrame({"tmax_c": 20.0, "tmin_c": 10.0, "rain_mm": 0.0, "wind_ms": 4.0, "rh_pct": 50.0, "rs_mj": 15.0}, index=index)
    daily.loc["2000-07-01":"2000-07-10", "tmax_c"] = 36.0
    daily.loc["2001-07-01":"2001-07-20", "tmax_c"] = 36.0
    daily.loc[["2000-06-01", "2001-06-01", "2001-01-05"], "rain_mm"] = 5.0
    checks = bias.baseline_checks(daily, (2000, 2001))
    assert checks["hot_days"] == 15.0 and checks["gs_rain_days"] == 1.0 and checks["rain_days"] == 1.5
    assert checks["dry_spell_days"] == 121.0  # 2 June to 30 September, both years
    assert not bias.needs_spread_scaling(40.0, 45.0) and not bias.needs_spread_scaling(55.0, 45.0)
    assert bias.needs_spread_scaling(30.0, 45.0) and bias.needs_spread_scaling(60.0, 45.0)


# --------------------------------------------------------------------------- NASA POWER


def power_doc(days=40, missing=()):
    dates = [d.strftime("%Y%m%d") for d in pd.date_range("1999-10-01", periods=days)]
    units = {name: {"units": unit} for name, unit in fetch_power.COLUMNS.values()}
    values = {name: {d: 10.0 + i for i, d in enumerate(dates)} for name, _ in fetch_power.COLUMNS.values()}
    for name, date in missing:
        values[name][date] = -999.0
    return {"geometry": {"coordinates": [-100.665, 38.005, 858.35]}, "parameters": units, "properties": {"parameter": values}}


def test_power_parse_units_missing_values_and_site():
    frame, site = fetch_power.parse(power_doc(missing=[("T2M_MAX", "19991005"), ("PRECTOTCORR", "19991006")]))
    assert list(frame.columns) == list(fetch_power.COLUMNS) and len(frame) == 40
    assert site["elevation_m"] == 858.35 and site["latitude"] == 38.005
    assert site["missing"]["tmax_c"] == 1 and site["missing"]["rain_mm"] == 1 and site["missing"]["wind_ms"] == 0
    assert np.isnan(frame.loc["1999-10-05", "tmax_c"])
    filled = fetch_power.fill_gaps(frame)
    assert filled.loc["1999-10-05", "tmax_c"] == pytest.approx(14.0)  # between 13 and 15
    assert filled.loc["1999-10-06", "rain_mm"] == 0.0 and not filled.isna().any().any()
    wrong = power_doc()
    wrong["parameters"]["ALLSKY_SFC_SW_DWN"]["units"] = "kW-hr/m^2/day"
    with pytest.raises(fetch_power.PowerError, match="ALLSKY_SFC_SW_DWN"):
        fetch_power.parse(wrong)
    long_gap = power_doc(missing=[("WS10M", f"199910{d:02d}") for d in range(5, 20)])
    with pytest.raises(fetch_power.PowerError, match="missing days in a row"):
        fetch_power.fill_gaps(fetch_power.parse(long_gap)[0])


def test_power_is_downloaded_once_then_read_from_the_cache(tmp_path):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return power_doc()

    def get(url, params, timeout):
        calls.append(params)
        return Response()

    first = fetch_power.fetch_raw(38.005, -100.665, tmp_path, get=get)
    second = fetch_power.fetch_raw(38.005, -100.665, tmp_path, get=get)
    assert first == second and len(calls) == 1
    assert calls[0]["community"] == "AG" and calls[0]["parameters"].split(",") == list(fetch_power.PARAMETERS)
    assert calls[0]["start"] == "19991001" and calls[0]["end"] == "20241231"
    assert fetch_power.site_for((-101.10, 37.74, -100.23, 38.27)) == (38.005, -100.665)


# --------------------------------------------------------------------------- Earth Engine plumbing (no Earth Engine)

BOUNDS = (-101.1, 37.74, -100.23, 38.27)


def fake_rows(request, null_day=None):
    dates = pd.date_range(request.start, pd.Timestamp(request.end) - pd.Timedelta(days=1))
    rows = [[d.strftime("%Y-%m-%d"), 50.0, 0.01, 1e-5, 350.0, 200.0, 4.0, 290.0, 297.0, 283.0] for d in dates]
    if null_day is not None:
        rows[null_day] = [rows[null_day][0], *[None] * 9]
    return rows


def test_requests_cover_every_model_scenario_and_year_once():
    requests = fetch_gddp.requests_for(["A", "B"])
    assert len(requests) == 2 * (16 + 2 * 46) and len(set(requests)) == len(requests)
    a = [r for r in requests if r.model == "A"]
    assert [r.year for r in a if r.scenario == "historical"] == list(range(1999, 2015))
    for scenario in ("ssp245", "ssp585"):
        assert [r.year for r in a if r.scenario == scenario] == list(range(2015, 2061))
    lead_in = next(r for r in a if r.year == 1999)
    assert (lead_in.start, lead_in.end) == ("1999-10-01", "2000-01-01")  # only what the first wheat season needs
    assert len(fetch_gddp.requests_for(fetch_gddp.PREFERRED_MODELS)) == 648


def test_choose_models_substitutes_in_order_and_says_why():
    full = {s: {"n": 366, "bands": list(fetch_gddp.BANDS)} for s in ("historical", "ssp245", "ssp585")}
    no_huss = {s: {"n": 366, "bands": [b for b in fetch_gddp.BANDS if b != "huss"]} for s in full}
    availability = {m: full for m in fetch_gddp.PREFERRED_MODELS + fetch_gddp.SUBSTITUTES}
    assert fetch_gddp.choose_models(availability) == (list(fetch_gddp.PREFERRED_MODELS), [])
    availability = {**availability, "IPSL-CM6A-LR": no_huss, "MIROC6": {**full, "ssp585": {"n": 0, "bands": []}}, "ACCESS-CM2": no_huss}
    models, swaps = fetch_gddp.choose_models(availability)
    assert models == ["GFDL-ESM4", "NorESM2-MM", "MPI-ESM1-2-HR", "MRI-ESM2-0", "UKESM1-0-LL", "CNRM-ESM2-1"]
    assert [(s["dropped"], s["replacement"]) for s in swaps] == [("IPSL-CM6A-LR", "NorESM2-MM"), ("MIROC6", "CNRM-ESM2-1")]
    assert "huss" in swaps[0]["reason"] and "ssp585" in swaps[1]["reason"]
    with pytest.raises(fetch_gddp.GddpError, match="no substitute"):
        fetch_gddp.choose_models({m: no_huss for m in availability})


def test_fetch_caches_each_request_and_never_asks_twice(tmp_path):
    requests = fetch_gddp.requests_for(["A"])[:5]
    calls = []

    def compute(request, bounds):
        calls.append(request)
        return fake_rows(request), 10.0, 1.0

    first = fetch_gddp.collect(requests, BOUNDS, tmp_path, compute=compute, workers=2)
    assert len(calls) == 5 and set(first) == set(requests)
    assert len(first[requests[0]]) == 92 and len(first[requests[1]]) == 366  # Oct-Dec 1999, then leap year 2000
    again = fetch_gddp.collect(requests, BOUNDS, tmp_path, compute=compute, workers=2)
    assert len(calls) == 5 and again == first
    spent, files, unknown = fetch_gddp.budget_spent_eecu_s(tmp_path)
    assert (spent, files, unknown) == (50.0, 5, 0)
    other_box = fetch_gddp.estimate(requests, (-101.0, 37.0, -100.0, 38.0), tmp_path)
    assert other_box["pending"] == 5  # another area is another request
    est = fetch_gddp.estimate(fetch_gddp.requests_for(["A"]), BOUNDS, tmp_path)
    assert est["cached"] == 5 and est["pending"] == 103 and est["pending_eecu_h"] == pytest.approx(103 * 10.0 / 3600)
    assert "103 to send" in fetch_gddp.estimate_lines(est)[0]


def test_fetch_retries_busy_errors_and_stops_on_fatal_ones(tmp_path):
    request = fetch_gddp.year_request("A", "ssp245", 2030)
    attempts, waits = [], []

    def flaky(req, bounds):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("Too many concurrent aggregations.")
        return fake_rows(req), None, 1.0

    rows = fetch_gddp.fetch_one(flaky, request, BOUNDS, tmp_path, sleep=waits.append)
    assert len(rows) == 365 and len(attempts) == 3 and len(waits) == 2

    def broken(req, bounds):
        raise RuntimeError("Collection.first: band 'nope' not found")

    with pytest.raises(fetch_gddp.GddpError, match="request failed"):
        fetch_gddp.fetch_one(broken, fetch_gddp.year_request("A", "ssp245", 2031), BOUNDS, tmp_path, sleep=waits.append)


def test_fetch_refuses_unusable_answers_and_does_not_cache_them(tmp_path):
    request = fetch_gddp.year_request("A", "ssp245", 2030)
    with pytest.raises(fetch_gddp.GddpError, match="no days"):
        fetch_gddp.fetch_one(lambda r, b: ([], 1.0, 1.0), request, BOUNDS, tmp_path)
    half = fake_rows(request)
    for row in half[:200]:
        row[1:] = [None] * 9
    with pytest.raises(fetch_gddp.GddpError, match="only 165 of 365"):
        fetch_gddp.fetch_one(lambda r, b: (half, 1.0, 1.0), request, BOUNDS, tmp_path)
    partial = fake_rows(request)
    partial[10][3] = None
    with pytest.raises(fetch_gddp.GddpError, match="some bands but not all"):
        fetch_gddp.fetch_one(lambda r, b: (partial, 1.0, 1.0), request, BOUNDS, tmp_path)
    assert list(tmp_path.iterdir()) == []
    ok = fetch_gddp.fetch_one(lambda r, b: (fake_rows(request, null_day=59), 1.0, 1.0), request, BOUNDS, tmp_path)
    assert ok[59][1] is None and len(list(tmp_path.iterdir())) == 1  # one empty day is a calendar matter, kept as null


def test_budget_stops_cleanly_and_keeps_what_is_done(tmp_path):
    requests = fetch_gddp.requests_for(["A"])[:10]

    def compute(request, bounds):
        return fake_rows(request), 1000.0, 1.0

    with pytest.raises(fetch_gddp.BudgetExceeded, match="stopped at the budget"):
        fetch_gddp.collect(requests, BOUNDS, tmp_path, compute=compute, workers=1, max_eecu_s=3500.0)
    assert len(list(tmp_path.glob("*.json"))) == 4  # 4 x 1000 passes 3500, the 5th is never sent
    with pytest.raises(fetch_gddp.BudgetExceeded):  # what earlier runs spent counts too
        fetch_gddp.collect(requests, BOUNDS, tmp_path, compute=compute, workers=1, max_eecu_s=3500.0,
                           already_spent_eecu_s=fetch_gddp.budget_spent_eecu_s(tmp_path)[0])
    assert len(list(tmp_path.glob("*.json"))) == 4
    pilot = fetch_gddp.pilot_requests(fetch_gddp.requests_for(["A", "B"]), BOUNDS, tmp_path)
    assert len(pilot) == 3 and {r.model for r in pilot} == {"A", "B"} and all(r.year != 1999 for r in pilot)


def test_estimate_command_asks_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(fetch_gddp, "CACHE_DIR", tmp_path / "gddp")
    monkeypatch.setattr(fetch_power, "CACHE_DIR", tmp_path / "power")

    def no_network(*args, **kwargs):
        raise AssertionError("--estimate must not talk to anything")

    monkeypatch.setattr(fetch_gddp, "GddpEngine", no_network)
    monkeypatch.setattr(fetch_power, "fetch_raw", no_network)
    assert build.main(["--estimate"]) == 0
    out = capsys.readouterr().out
    assert "648 needed, 0 cached, 648 to send" in out and "EECU-hours" in out and "NASA POWER: not cached" in out
    assert len(out.splitlines()) <= 6


# --------------------------------------------------------------------------- the build, end to end on synthetic series


def test_build_tables_are_complete_valid_and_have_no_nulls(built):
    result, _, series = built
    futures, history, monthly = result["futures"], result["history"], result["monthly"]
    assert schema.check_futures(futures) == [] and schema.check_history(history) == [] and schema.check_monthly(monthly) == []
    assert len(futures) == 2 * 2 * 61 and len(history) == 25
    assert not futures.isna().any().any() and not history.isna().any().any() and not monthly.isna().any().any()
    assert set(futures["year"]) == set(range(2000, 2061))
    assert len(monthly) == (25 * 12 + 3) + 4 * (61 * 12 + 3)  # with October-December 1999
    assert set(monthly.loc[monthly["model"] == "observed", "scenario"]) == {"observed"}
    assert history["erosion_climate"].mean() == pytest.approx(100.0, abs=1e-6)
    assert futures["erosion_climate"].max() <= indicators.EROSION_CAP


def test_build_corrected_baseline_matches_observed_and_keeps_the_trend(built):
    result, observed, series = built
    futures, history, checks = result["futures"], result["history"], result["checks"]
    base = futures[futures["year"] <= 2024].groupby(["model", "scenario"])[["annual_rain_mm", "annual_tmean_c", "wind_ms", "annual_et0_mm"]].mean()
    assert np.allclose(base["annual_rain_mm"], history["annual_rain_mm"].mean(), rtol=1e-6)  # rain totals: by construction
    assert np.allclose(base["annual_tmean_c"], history["annual_tmean_c"].mean(), atol=0.01)
    assert np.allclose(base["wind_ms"], history["wind_ms"].mean(), rtol=1e-6)
    assert np.allclose(base["annual_et0_mm"], history["annual_et0_mm"].mean(), rtol=0.03)  # ET0 is not linear: close, not equal
    for details in checks["series"].values():
        assert max(details["monthly_mean_gap"].values()) < 0.02
    late = futures[futures["year"] >= 2050].groupby("scenario")["tmean_c"].mean()
    early = futures[futures["year"] <= 2024].groupby("scenario")["tmean_c"].mean()
    assert (late - early)["ssp585"] > (late - early)["ssp245"] > 0.5  # the series were made with 0.06 and 0.03 C a year
    assert checks["observed_annual_et0_ws2m_mm"] < checks["observed_means"]["annual_et0_mm"]


def test_decade_changes_and_report(built, tmp_path):
    result, _, _ = built
    changes = build.decade_changes(result["futures"])
    assert len(changes) == 2 * 3 * len(build.CHANGE_STATS)
    assert (changes["low"] <= changes["mean"]).all() and (changes["mean"] <= changes["high"]).all()
    warming = changes[changes["stat"] == "tmean_c"].set_index(["scenario", "window"])["mean"]
    assert warming[("ssp585", "2050s")] > warming[("ssp585", "2030s")] > 0
    assert warming[("ssp585", "2050s")] == pytest.approx(0.06 * 30, abs=0.4)  # 2020s to 2050s at 0.06 C a year
    lines = build.change_lines(changes)
    assert len(lines) == 6 and lines[0].startswith("ssp245 2030s vs 2020-2029: tmean_c +")
    context = {"built": "now", "site": SITE, "bounds": BOUNDS, "swaps": [{"dropped": "X", "replacement": "Y", "reason": "historical lacks huss"}],
               "cost": {}, "calendar": {"MODEL-A": "no repeated days"}, "changes": changes}
    build.write_outputs(result, tmp_path, context)
    text = (tmp_path / "climate_report.md").read_text()
    for heading in ("## Sources", "## Models", "## Method", "## Checks", "## Changes", "## Limits"):
        assert heading in text
    assert "X was replaced by Y" in text and "| ssp585 | 2050s |" in text
    assert schema.main([str(tmp_path)]) == 0
    assert schema.available(tmp_path) and not schema.available(tmp_path / "nowhere")
    assert len(schema.load_futures(tmp_path / "climate_futures.parquet")) == len(result["futures"])


def test_assemble_series_joins_historical_and_scenario_runs():
    requests = fetch_gddp.requests_for(["A"])
    results = {r: fake_rows(r) for r in requests}
    series, infos = build.assemble_series(["A"], results)
    assert set(series) == {("A", "ssp245"), ("A", "ssp585")}
    daily = series[("A", "ssp245")]
    assert daily.index[0] == pd.Timestamp("1999-10-01") and daily.index[-1] == pd.Timestamp("2060-12-31")
    assert len(daily) == 22373 and not daily.isna().any().any()
    assert "repeated" in build.calendar_summary(infos)["A"]
    del results[next(r for r in requests if r.scenario == "ssp585" and r.year == 2040)]
    with pytest.raises(build.BuildError, match="no data for A ssp585 2040"):
        build.assemble_series(["A"], results)


class FakeEngine:
    """Stands in for Earth Engine: synthetic days, a fixed cost per request, and a record of what was asked."""

    asked: list = []
    cost_eecu_s = 12.0

    def availability(self, models):
        full = {s: {"n": 366, "bands": list(fetch_gddp.BANDS)} for s in ("historical", "ssp245", "ssp585")}
        return {m: ({**full, "ssp585": {"n": 366, "bands": ["pr", "tas"]}} if m == "MIROC6" else full) for m in models}

    def daily(self, request, bounds):
        FakeEngine.asked.append(request)
        dates = pd.date_range(request.start, pd.Timestamp(request.end) - pd.Timedelta(days=1))
        season = -np.cos(2 * np.pi * (dates.day_of_year.to_numpy() - 15) / 365.25)
        rng = np.random.default_rng(abs(hash((request.model, request.scenario, request.year))) % 2**32)
        tmax = 293.0 + 14.0 * season + rng.normal(0, 4.0, len(dates)) + 0.04 * (request.year - 2000)
        rain = np.where(rng.random(len(dates)) < 0.2, rng.gamma(0.8, 8.0, len(dates)), 0.0) / 86400.0
        rows = [
            [d.strftime("%Y-%m-%d"), 60.0, 0.008, float(rain[i]), 330.0, float(200.0 + 100.0 * season[i]), 3.5, float(tmax[i] - 6.5),
             float(tmax[i]), float(tmax[i] - 13.0)]
            for i, d in enumerate(dates)
        ]
        return rows, FakeEngine.cost_eecu_s, 0.01


@pytest.fixture
def offline_world(monkeypatch, tmp_path):
    """Point every cache, log and output at tmp_path and replace both data sources with fakes."""
    observed = synthetic_daily(seed=1)
    observed["wind2_ms"] = observed["wind_ms"] * 0.7
    names = {column: name for column, (name, _) in fetch_power.COLUMNS.items()}
    doc = {
        "geometry": {"coordinates": [-100.665, 38.005, 858.35]},
        "parameters": {name: {"units": unit} for name, unit in fetch_power.COLUMNS.values()},
        "properties": {"parameter": {names[c]: {d.strftime("%Y%m%d"): float(v) for d, v in observed[c].items()} for c in names}},
    }
    FakeEngine.asked = []
    monkeypatch.setattr(fetch_gddp, "CACHE_DIR", tmp_path / "raw" / "gddp")
    monkeypatch.setattr(fetch_power, "CACHE_DIR", tmp_path / "raw" / "power")
    monkeypatch.setattr(fetch_gddp, "GddpEngine", FakeEngine)
    monkeypatch.setattr(fetch_gddp, "county_bounds", lambda *a, **k: BOUNDS)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return doc

    import requests

    monkeypatch.setattr(requests, "get", lambda url, params, timeout: Response())  # no real HTTP
    monkeypatch.setattr(build, "LOG_DIR", tmp_path / "logs")
    return tmp_path


def test_build_command_end_to_end_with_fake_sources(offline_world, capsys):
    out = offline_world / "out"
    assert build.main(["--out-dir", str(out), "--workers", "3"]) == 0
    printed = capsys.readouterr().out
    assert "model MIROC6 replaced by ACCESS-CM2" in printed  # the first substitute, and the reason is said
    assert "pilot: 3 request(s) cost 12.0 EECU-seconds each; 645 left" in printed
    assert len(printed.splitlines()) <= 12  # a few summary lines, not a table
    assert len(FakeEngine.asked) == 648 and len(set(FakeEngine.asked)) == 648
    assert [r.model for r in FakeEngine.asked[:3]] == ["GFDL-ESM4", "IPSL-CM6A-LR", "MPI-ESM1-2-HR"]  # the pilot samples models
    assert schema.main([str(out)]) == 0
    futures = schema.load_futures(out / "climate_futures.parquet")
    assert sorted(futures["model"].unique()) == ["ACCESS-CM2", "GFDL-ESM4", "IPSL-CM6A-LR", "MPI-ESM1-2-HR", "MRI-ESM2-0", "UKESM1-0-LL"]
    assert len(futures) == 6 * 2 * 61 and not futures.isna().any().any()
    report = (out / "climate_report.md").read_text()
    assert "MIROC6 was replaced by ACCESS-CM2" in report and "648 cached answers" in report
    assert len(list((offline_world / "logs").glob("climate_build_*.log"))) == 1

    capsys.readouterr()
    assert build.main(["--out-dir", str(out)]) == 0  # second run: everything from the cache
    assert len(FakeEngine.asked) == 648 and "0 to send" in capsys.readouterr().out
    assert build.main(["--out-dir", str(out), "--offline"]) == 0
    assert build.main(["--estimate"]) == 0
    assert "648 needed, 648 cached, 0 to send" in capsys.readouterr().out


def test_build_command_stops_before_the_full_job_when_over_budget(offline_world, capsys):
    FakeEngine.cost_eecu_s = 40.0  # 648 x 40 s = 7.2 EECU-hours: over the limit of 5
    try:
        assert build.main(["--out-dir", str(offline_world / "out")]) == 3
    finally:
        FakeEngine.cost_eecu_s = 12.0
    captured = capsys.readouterr()
    assert "STOPPED" in captured.err and "over the limit of 5.00" in captured.err
    assert len(FakeEngine.asked) == 3  # only the pilot was sent
    assert not (offline_world / "out").exists()
    assert build.main(["--out-dir", str(offline_world / "out"), "--offline"]) == 1  # and offline says what is missing
    assert "645 GDDP request(s) are not cached" in capsys.readouterr().err


# --------------------------------------------------------------------------- validator


def good_tables(built):
    result, _, _ = built
    return result["futures"].copy(), result["history"].copy(), result["monthly"].copy()


def test_validator_names_each_problem_in_a_bad_futures_table(built):
    futures, _, _ = good_tables(built)
    assert schema.check_futures(futures) == []
    bad = futures.copy()
    bad.loc[0, "tmean_c"] = np.nan
    bad.loc[1, "hot_days"] = 400.0
    bad.loc[2, "scenario"] = "ssp126"
    bad = bad[~((bad["model"] == "MODEL-B") & (bad["year"] == 2033))]
    bad = pd.concat([bad, bad.iloc[[5]]], ignore_index=True)
    problems = "\n".join(schema.check_futures(bad))
    assert "tmean_c: 1 null value(s)" in problems
    assert "hot_days: 1 value(s) outside [0, 183]" in problems
    assert "scenario: unknown value(s) ssp126" in problems
    assert "1 duplicate (model, scenario, year) row(s)" in problems
    missing_year = futures[~((futures["model"] == "MODEL-B") & (futures["year"] == 2033))]
    assert any("2 (model, scenario, year) row(s) missing" in p and "MODEL-B ssp245 2033" in p for p in schema.check_futures(missing_year))
    one_scenario = futures[~((futures["model"] == "MODEL-A") & (futures["scenario"] == "ssp585"))]
    assert any("61 (model, scenario, year) row(s) missing" in p for p in schema.check_futures(one_scenario))
    assert schema.check_futures(futures.drop(columns=["gdd", "et0_mm"])) == ["missing column(s): gdd, et0_mm"]
    assert schema.check_futures(futures.iloc[:0]) == ["table has no rows"]
    assert schema.check_futures("nope") == ["expected a DataFrame, got str"]
    assert any("year: dtype" in p for p in schema.check_futures(futures.assign(year=futures["year"].astype(float))))
    assert any("erosion_climate: dtype" in p for p in schema.check_futures(futures.assign(erosion_climate=1)))
    assert any('"observed" belongs in the history table' in p for p in schema.check_futures(futures.assign(model="observed")))
    with pytest.raises(schema.ClimateSchemaError, match="tmean_c: 1 null"):
        schema.validate("futures", bad, "bad.parquet")


def test_validator_history_and_monthly(built):
    _, history, monthly = good_tables(built)
    assert schema.check_history(history) == [] and schema.check_monthly(monthly) == []
    problems = schema.check_history(history[history["year"] != 2012].assign(rain_mm=-1.0))
    assert any("1 year(s) missing from 2000-2024 (first missing: 2012)" in p for p in problems)
    assert any("rain_mm: 24 value(s) outside" in p for p in problems)
    assert any("year: values outside 2000-2024" in p for p in schema.check_history(history.assign(year=history["year"] + 1)))

    bad = monthly.copy()
    bad.loc[bad.index[0], "et0_mm"] = np.nan
    bad.loc[bad.index[1], "month"] = 13
    assert any("et0_mm: 1 null" in p for p in schema.check_monthly(bad))
    assert any("month: values outside 1-12" in p for p in schema.check_monthly(bad))
    dropped = monthly[~((monthly["model"] == "MODEL-A") & (monthly["scenario"] == "ssp245") & (monthly["year"] == 2040) & (monthly["month"] == 7))]
    assert any("first missing: MODEL-A ssp245 2040-07" in p for p in schema.check_monthly(dropped))
    no_observed = monthly[monthly["model"] != "observed"]
    assert any("300 (model, scenario, year, month) row(s) missing" in p for p in schema.check_monthly(no_observed))
    mixed = monthly.copy()
    mixed.loc[mixed.index[0], "scenario"] = "ssp245"
    assert any('only one of model and scenario is "observed"' in p for p in schema.check_monthly(mixed))
    early = monthly.copy()
    early.loc[(early["year"] == 1999) & (early["month"] == 10) & (early["model"] == "observed"), "month"] = 5
    assert any("before 2000 other than October-December 1999" in p for p in schema.check_monthly(early))


def test_validator_command_reports_missing_and_broken_files(built, tmp_path, capsys):
    assert schema.main([str(tmp_path)]) == 1
    assert "does not exist" in capsys.readouterr().out
    futures, history, monthly = good_tables(built)
    futures.assign(wind_ms=-5.0).to_parquet(tmp_path / "climate_futures.parquet", index=False)
    history.to_parquet(tmp_path / "climate_history.parquet", index=False)
    monthly.to_parquet(tmp_path / "climate_monthly.parquet", index=False)
    assert schema.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "wind_ms: 244 value(s) outside" in out and "climate_history.parquet: valid" in out
    with pytest.raises(FileNotFoundError):
        schema.load_history(tmp_path / "missing.parquet")
    (tmp_path / "junk.parquet").write_text("not parquet")
    with pytest.raises(schema.ClimateSchemaError, match="could not read"):
        schema.load_monthly(tmp_path / "junk.parquet")


def test_schema_module_stays_light():
    """The API imports pipeline.climate.schema: it may import pandas and the standard library, nothing else."""
    import ast
    import sys

    tree = ast.parse(open(schema.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pipeline.region":  # stdlib-only: picks the county's data folder
                continue
            imported.add((node.module or "").split(".")[0])
    assert imported - set(sys.stdlib_module_names) == {"pandas"}
