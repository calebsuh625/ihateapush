"""api/planner: crops.py, water.py, response.py, calibrate.py (Phase 3, piece B1). No network.

The core logic runs on small made-up tables, so these tests always run. The tests at the bottom use
the real tables under data/ and skip when one is missing (a fresh clone has no data/ folder).
Nothing here writes outside pytest's tmp_path.
"""
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from api.planner import calibrate, response, water
from api.planner.crops import CROP_TABLE, CROPS, FAIL_BELOW, MAX_YIELD_REL, PRACTICES, PREV_KINDS, prev_kind

ROOT = Path(__file__).resolve().parents[1]
RAIN = [12, 13, 32, 48, 75, 81, 87, 74, 42, 44, 16, 19]            # mm per month, roughly Finney County
ET0 = [44, 57, 101, 144, 180, 221, 235, 206, 169, 110, 64, 42]
NO_SCALE = water.WaterConstants(et0_scale=1.0)


# --- made-up tables ----------------------------------------------------------------------------------

def monthly_table(years, rain=RAIN, et0=ET0, model="observed", scenario="observed", lead=True):
    """Monthly rows; `rain` / `et0` are 12 numbers or a function (year, month) -> number."""
    def val(x, y, m):
        return float(x(y, m)) if callable(x) else float(x[m - 1])
    rows = []
    first = min(years)
    months = ([(first - 1, m) for m in (10, 11, 12)] if lead else []) + [(y, m) for y in years for m in range(1, 13)]
    for y, m in months:
        rows.append({"model": model, "scenario": scenario, "year": y, "month": m,
                     "rain_mm": val(rain, y, m), "et0_mm": val(et0, y, m)})
    return pd.DataFrame(rows)


def yearly_table(years, hot=45, wheat_hot=21, model=None, scenario=None):
    df = pd.DataFrame({"year": list(years), "hot_days": float(hot), "wheat_hot_days": float(wheat_hot)})
    if model is not None:
        df.insert(0, "scenario", scenario)
        df.insert(0, "model", model)
    return df


def pick(table, crop, practice, year=None):
    sub = table[(table["crop"] == crop) & (table["practice"] == practice)]
    if year is not None:
        sub = sub[sub["year"] == year]
    return sub.reset_index(drop=True)


# --- crops.py ----------------------------------------------------------------------------------------

def test_crop_table_is_complete_and_sane():
    assert tuple(CROP_TABLE) == CROPS == ("corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow")
    for name, crop in CROP_TABLE.items():
        assert len(crop.kc) == len(crop.season)
        assert set(crop.heat_loss) == set(crop.normal_yield) == set(crop.scale) == set(PRACTICES)
        for practice in PRACTICES:
            assert abs(sum(crop.typical_prev[practice].values()) - 1.0) < 1e-9
            assert set(crop.typical_prev[practice]) == set(PREV_KINDS)
        if name == "fallow":
            assert crop.season == () and crop.normal_yield == {"irrigated": 0.0, "dryland": 0.0}
            continue
        assert all(0.1 <= k <= 1.25 for k in crop.kc)
        assert 0.8 <= crop.ky <= 1.3
        assert crop.normal_yield["irrigated"] > crop.normal_yield["dryland"] > 0
        assert crop.heat_loss["dryland"] >= crop.heat_loss["irrigated"] > 0
    assert CROP_TABLE["alfalfa"].unit == "tons/acre" and CROP_TABLE["corn"].unit == "bu/acre"
    assert CROP_TABLE["winter wheat"].season[0] == (-1, 10) and CROP_TABLE["winter wheat"].season[-1] == (0, 6)
    assert CROP_TABLE["winter wheat"].heat_column == "wheat_hot_days" and CROP_TABLE["corn"].heat_column == "hot_days"


def test_prev_kind():
    assert prev_kind("winter wheat") == "wheat"
    assert prev_kind("fallow") == "fallow"
    assert [prev_kind(c) for c in ("corn", "soybeans", "sorghum", "alfalfa")] == ["summer"] * 4
    assert prev_kind(None) == "typical"
    with pytest.raises(KeyError):
        prev_kind("cover crop")


# --- water.py ----------------------------------------------------------------------------------------

def test_effective_rain_formula():
    assert water.effective_rain(0) == 0
    assert water.effective_rain(100) == pytest.approx(84.0)          # 100 x (125 - 20) / 125
    assert water.effective_rain(250) == pytest.approx(150.0)
    assert water.effective_rain(300) == pytest.approx(155.0)         # 125 + 0.1 x 300
    p = np.arange(0, 400, 5.0)
    e = water.effective_rain(p)
    assert (np.diff(e) >= 0).all() and (e <= p).all()


def test_water_need_is_kc_times_et0_over_the_season():
    monthly = monthly_table([2001, 2002], rain=[0] * 12, et0=[100] * 12)
    t = water.water_table(monthly, NO_SCALE)
    for crop in ("corn", "soybeans", "sorghum", "winter wheat", "alfalfa"):
        need = t[(t["crop"] == crop) & (t["prev"] == "summer")]["water_need_mm"]
        assert need.to_numpy() == pytest.approx(100 * sum(CROP_TABLE[crop].kc))
    scaled = water.water_table(monthly)   # default constants: x 0.85
    need = scaled[(scaled["crop"] == "corn") & (scaled["prev"] == "summer")]["water_need_mm"]
    assert need.to_numpy() == pytest.approx(0.85 * 100 * sum(CROP_TABLE["corn"].kc))
    fallow = t[t["crop"] == "fallow"]
    assert (fallow[["water_need_mm", "gap_mm", "irrigation_mm", "stored_mm"]] == 0).all().all()


def test_wheat_season_starts_the_autumn_before():
    # reference ET is 200 mm a month in 2001 and 100 in 2002: the 2002 wheat crop takes Oct-Dec from 2001
    monthly = monthly_table([2001, 2002], rain=[0] * 12, et0=lambda y, m: 200 if y == 2001 else 100)
    t = water.water_table(monthly, NO_SCALE)
    kc = CROP_TABLE["winter wheat"].kc
    need = t[(t["crop"] == "winter wheat") & (t["prev"] == "summer") & (t["year"] == 2002)]["water_need_mm"].iloc[0]
    assert need == pytest.approx(200 * sum(kc[:3]) + 100 * sum(kc[3:]))


def test_stored_soil_water_grows_with_the_wait_before_planting():
    monthly = monthly_table([2001, 2002, 2003], rain=[40] * 12)
    t = water.water_table(monthly).set_index(["crop", "prev", "year"])["stored_mm"]
    # corn is planted in May: Nov-Apr after a summer crop, Jul-Apr after wheat, 12 months more after fallow
    assert t[("corn", "summer", 2003)] == pytest.approx(0.25 * 40 * 6)
    assert t[("corn", "wheat", 2003)] == pytest.approx(0.25 * 40 * 10)
    assert t[("corn", "fallow", 2003)] == pytest.approx(0.25 * 40 * 18)
    # wheat is sown in October: straight after a summer crop there is no wait
    assert t[("winter wheat", "summer", 2003)] == 0
    assert t[("winter wheat", "wheat", 2003)] == pytest.approx(0.25 * 40 * 3)
    assert t[("winter wheat", "fallow", 2003)] == pytest.approx(0.25 * 40 * 11)
    # sorghum is planted in June
    assert t[("sorghum", "wheat", 2003)] == pytest.approx(0.25 * 40 * 11)
    # the first year has no earlier months in the table: filled with the series' monthly means (40 here)
    assert t[("corn", "fallow", 2001)] == pytest.approx(0.25 * 40 * 18)


def test_stored_soil_water_is_capped():
    monthly = monthly_table([2001, 2002, 2003], rain=[200] * 12)
    t = water.water_table(monthly).set_index(["crop", "prev", "year"])["stored_mm"]
    assert t[("corn", "fallow", 2003)] == pytest.approx(water.DEFAULTS.awc_mm)


def test_bucket_gap_irrigation_and_shortfall():
    # no rain at all: nothing stored, the whole need is the gap
    dry = water.water_table(monthly_table([2001, 2002], rain=[0] * 12, et0=[100] * 12), NO_SCALE)
    corn = dry[(dry["crop"] == "corn") & (dry["prev"] == "wheat")]
    need = 100 * sum(CROP_TABLE["corn"].kc)
    assert corn["rainfed_et_mm"].to_numpy() == pytest.approx(0)
    assert corn["gap_mm"].to_numpy() == pytest.approx(need)
    assert corn["irrigation_mm"].to_numpy() == pytest.approx(need / 0.85)
    # by hand: corn after a summer crop, 50 mm of rain every month, reference ET 100
    t = water.water_table(monthly_table([2001, 2002], rain=[50] * 12, et0=[100] * 12), NO_SCALE)
    pe = 50 * (125 - 0.2 * 50) / 125            # 46 mm effective a month
    soil = 0.25 * 50 * 6                        # 75 mm stored Nov-Apr
    got = 0.0
    for kc in CROP_TABLE["corn"].kc:
        use = min(100 * kc, soil + pe)
        soil = soil + pe - use
        got += use
    row = t[(t["crop"] == "corn") & (t["prev"] == "summer") & (t["year"] == 2002)].iloc[0]
    assert row["eff_rain_mm"] == pytest.approx(5 * pe)
    assert row["rainfed_et_mm"] == pytest.approx(got)
    assert row["gap_mm"] == pytest.approx(need - got)
    assert 0 < row["gap_mm"] < need
    # plenty of rain: no gap (and the yield of a wet, cool year is capped at twice normal)
    wet_resp = response.crop_response(yearly_table([2001, 2002], hot=0, wheat_hot=0),
                                      monthly_table([2001, 2002], rain=[240] * 12, et0=[100] * 12), response.default_params())
    corn = pick(wet_resp, "corn", "dryland")
    assert (corn["yield_rel"] == MAX_YIELD_REL).all() and (corn["water_factor"] == 1).all()
    assert CROP_TABLE["corn"].scale["dryland"] > MAX_YIELD_REL
    wet = water.water_table(monthly_table([2001, 2002], rain=[240] * 12, et0=[100] * 12), NO_SCALE)
    assert (wet["gap_mm"] == 0).all()


def test_month_grid_needs_whole_years_and_fills_the_lead_in():
    monthly = monthly_table([2001, 2002])
    grid = water.month_grid(monthly)
    assert list(grid.years) == [2001, 2002]
    assert not np.isnan(grid.rain).any() and not np.isnan(grid.et0).any()
    assert grid.rain.shape == (1, 2 + water.LEAD_YEARS, 12)
    # October 2000 is real data in the table, September 2000 is the fill (the same number here)
    assert grid.take(grid.rain, [-3])[0, 0, 0] == RAIN[9]
    assert grid.complete.all()
    with pytest.raises(ValueError):
        water.month_grid(monthly[monthly["month"] != 7])       # no complete year
    with pytest.raises(ValueError):
        water.month_grid(monthly.drop(columns=["et0_mm"]))


# --- response.py: shapes -----------------------------------------------------------------------------

def test_response_shape_columns_and_order():
    years = [2001, 2002, 2003]
    monthly = pd.concat([monthly_table(years, model="A", scenario="ssp245"),
                         monthly_table(years, model="B", scenario="ssp585")])
    yearly = pd.concat([yearly_table(years, model="B", scenario="ssp585"), yearly_table(years, model="A", scenario="ssp245")])
    out = response.crop_response(yearly, monthly, response.default_params())
    assert list(out.columns) == response.COLUMNS
    assert len(out) == 2 * 3 * len(CROPS) * len(PRACTICES)
    assert not out.duplicated(response.KEYS).any()
    assert not out.drop(columns=["unit"]).isna().any().any()
    first = out.iloc[: len(CROPS) * len(PRACTICES)]
    assert set(first["model"]) == {"A"} and set(first["year"]) == {2001}
    assert list(first["crop"]) == [c for c in CROPS for _ in PRACTICES]
    assert list(first["practice"]) == list(PRACTICES) * len(CROPS)
    assert out["failed"].dtype == bool and out["failed_after_fallow"].dtype == bool
    assert ((out["yield_rel"] >= 0) & (out["fail_prob"] >= 0) & (out["fail_prob"] <= 1)).all()
    crops = out[out["crop"] != "fallow"]
    np.testing.assert_allclose(crops["yield"], crops["normal_yield"] * crops["yield_rel"])
    assert (out["yield_rel"] <= MAX_YIELD_REL).all()
    assert (crops["water_need_mm"] > 0).all()
    assert set(out.loc[out["crop"] == "alfalfa", "unit"]) == {"tons/acre"}
    assert set(out.loc[out["crop"] == "corn", "unit"]) == {"bu/acre"}


def test_response_accepts_the_observed_table_without_model_columns():
    years = [2001, 2002]
    out = response.crop_response(yearly_table(years), monthly_table(years), response.default_params())
    assert set(out["model"]) == {"observed"} and set(out["scenario"]) == {"observed"}
    assert len(out) == 2 * 12
    # only the series asked for are computed, and a subset of years is fine
    monthly = pd.concat([monthly_table(years), monthly_table(years, model="A", scenario="ssp245")])
    one = response.crop_response(yearly_table([2002]), monthly, response.default_params())
    assert set(one["year"]) == {2002} and set(one["model"]) == {"observed"}
    pd.testing.assert_frame_equal(one.reset_index(drop=True), out[out["year"] == 2002].reset_index(drop=True))


def test_series_of_different_lengths_share_one_call():
    # like the real tables: observed rows stop earlier than the model rows
    monthly = pd.concat([monthly_table([2001, 2002]), monthly_table([2001, 2002, 2003, 2004], model="A", scenario="ssp245")])
    yearly = pd.concat([yearly_table([2001, 2002], model="observed", scenario="observed"),
                        yearly_table([2001, 2002, 2003, 2004], model="A", scenario="ssp245")])
    out = response.crop_response(yearly, monthly, response.default_params())
    assert len(out) == 6 * 12 and not out.drop(columns=["unit"]).isna().any().any()
    alone = response.crop_response(yearly_table([2001, 2002]), monthly_table([2001, 2002]), response.default_params())
    pd.testing.assert_frame_equal(out[out["model"] == "observed"].reset_index(drop=True), alone)
    grid = water.month_grid(monthly)
    assert list(grid.years) == [2001, 2002, 2003, 2004]
    assert grid.complete.tolist() == [[True] * 4, [True, True, False, False]]
    t = water.water_table(monthly)
    assert set(t.loc[t["model"] == "observed", "year"]) == {2001, 2002} and set(t.loc[t["model"] == "A", "year"]) == {2001, 2002, 2003, 2004}
    # an observed year that is not in the monthly table is an error, not a guess
    with pytest.raises(ValueError, match="twelve months"):
        response.crop_response(yearly_table([2003], model="observed", scenario="observed"), monthly)


def test_response_errors_are_clear():
    years = [2001, 2002]
    with pytest.raises(ValueError, match="hot_days"):
        response.crop_response(yearly_table(years).drop(columns=["hot_days"]), monthly_table(years))
    with pytest.raises(ValueError, match="no rows"):
        response.crop_response(yearly_table(years, model="X", scenario="ssp245"), monthly_table(years))
    with pytest.raises(ValueError, match="twelve months"):
        response.crop_response(yearly_table([2001, 2005]), monthly_table(years))
    empty = response.crop_response(yearly_table([]), monthly_table(years))
    assert list(empty.columns) == response.COLUMNS and len(empty) == 0


def test_irrigated_and_dryland_water_columns():
    years = [2001, 2002]
    out = response.crop_response(yearly_table(years), monthly_table(years), response.default_params())
    irr = out[(out["practice"] == "irrigated") & (out["crop"] != "fallow")]
    dry = out[(out["practice"] == "dryland") & (out["crop"] != "fallow")]
    assert (irr["shortfall_mm"] == 0).all() and (irr["irrigation_mm"] > 0).all() and (irr["water_factor"] == 1).all()
    np.testing.assert_allclose(irr["irrigation_mm"], irr["irrigation_net_mm"] / 0.85)
    assert (dry["irrigation_mm"] == 0).all() and (dry["shortfall_mm"] > 0).all() and (dry["water_factor"] < 1).all()
    # the same water gap, pumped on one side and missing on the other
    assert irr["irrigation_net_mm_after_wheat"].to_numpy() == pytest.approx(dry["shortfall_mm_after_wheat"].to_numpy())
    # the plain columns are the county's usual mix of previous crops
    for crop in ("winter wheat", "sorghum"):
        d = pick(out, crop, "dryland")
        w = CROP_TABLE[crop].typical_prev["dryland"]
        mix = sum(w[k] * d[f"yield_rel_after_{k}"] for k in PREV_KINDS)
        assert d["yield_rel"].to_numpy() == pytest.approx(mix.to_numpy())


def test_column_helper():
    assert response.column("yield", "fallow") == "yield_after_fallow"
    assert response.column("yield_rel", "winter wheat") == "yield_rel_after_wheat"
    assert response.column("shortfall_mm", "corn") == "shortfall_mm_after_summer"
    assert response.column("yield", None) == "yield"
    assert response.column("water_need_mm", "fallow") == "water_need_mm"
    for name in response.PREV_COLUMNS:
        for crop in CROPS:
            assert response.column(name, crop) in response.COLUMNS


# --- response.py: monotonicity -----------------------------------------------------------------------

YIELD_COLUMNS = ["yield_rel", "yield"] + [f"{c}_after_{k}" for k in PREV_KINDS for c in ("yield_rel", "yield")]


@pytest.mark.parametrize("params_name", ["builtin", "json"])
def test_more_hot_days_never_raise_yield(params_name):
    params = response.default_params() if params_name == "builtin" else response.load_params()
    years = [2001, 2002]
    monthly = monthly_table(years)
    previous = None
    for hot, wheat_hot in [(0, 0), (20, 10), (45, 21), (70, 30), (85, 38)]:          # up to the cap: strictly worse
        out = response.crop_response(yearly_table(years, hot=hot, wheat_hot=wheat_hot), monthly, params)
        if previous is not None:
            for col in YIELD_COLUMNS:
                assert (out[col].to_numpy() <= previous[col].to_numpy() + 1e-12).all(), col
            crops = out["crop"] != "fallow"
            assert (out.loc[crops, "yield_rel"].to_numpy() < previous.loc[crops, "yield_rel"].to_numpy()).any()
            assert (out["fail_prob"].to_numpy() >= previous["fail_prob"].to_numpy() - 1e-12).all()
        previous = out
    # beyond the cap more hot days change nothing (never better, and no worse either)
    for hot, wheat_hot in [(110, 55), (200, 61)]:
        out = response.crop_response(yearly_table(years, hot=hot, wheat_hot=wheat_hot), monthly, params)
        for col in YIELD_COLUMNS + ["fail_prob", "heat_factor"]:
            assert np.array_equal(out[col].to_numpy(), previous[col].to_numpy()), col
    # heat hits irrigated crops too
    cool = response.crop_response(yearly_table(years, hot=10, wheat_hot=5), monthly, params)
    hot = response.crop_response(yearly_table(years, hot=80, wheat_hot=40), monthly, params)
    for crop in ("corn", "winter wheat"):
        assert pick(hot, crop, "irrigated")["yield"].iloc[0] < pick(cool, crop, "irrigated")["yield"].iloc[0]


@pytest.mark.parametrize("params_name", ["builtin", "json"])
def test_less_rain_never_raises_dryland_yield_and_irrigated_yield_ignores_rain(params_name):
    params = response.default_params() if params_name == "builtin" else response.load_params()
    years = [2001, 2002, 2003]
    yearly = yearly_table(years)
    previous = None
    for share in [1.6, 1.0, 0.7, 0.4, 0.1, 0.0]:
        out = response.crop_response(yearly, monthly_table(years, rain=[r * share for r in RAIN]), params)
        if previous is not None:
            dry = (out["practice"] == "dryland").to_numpy()
            irr = ~dry
            for col in YIELD_COLUMNS:
                assert (out[col].to_numpy()[dry] <= previous[col].to_numpy()[dry] + 1e-12).all(), col
                assert out[col].to_numpy()[irr] == pytest.approx(previous[col].to_numpy()[irr], abs=1e-12), col
            assert (out["shortfall_mm"].to_numpy() >= previous["shortfall_mm"].to_numpy() - 1e-9).all()
            assert (out["irrigation_mm"].to_numpy() >= previous["irrigation_mm"].to_numpy() - 1e-9).all()
        previous = out
    # and with no rain at all a dryland crop has failed
    assert pick(previous, "corn", "dryland")["failed"].all()
    assert not pick(previous, "corn", "irrigated")["failed"].any()


def test_a_single_dry_month_never_helps():
    years = [2001, 2002]
    yearly = yearly_table(years)
    base = response.crop_response(yearly, monthly_table(years), response.default_params())
    for month in range(1, 13):
        drier = response.crop_response(
            yearly, monthly_table(years, rain=lambda y, m, month=month: 0 if m == month else RAIN[m - 1]),
            response.default_params())
        dry = (base["practice"] == "dryland").to_numpy()
        for col in YIELD_COLUMNS:
            assert (drier[col].to_numpy()[dry] <= base[col].to_numpy()[dry] + 1e-12).all(), (month, col)


def test_more_reference_et_never_raises_dryland_yield_and_raises_irrigation():
    years = [2001, 2002]
    yearly = yearly_table(years)
    a = response.crop_response(yearly, monthly_table(years), response.default_params())
    b = response.crop_response(yearly, monthly_table(years, et0=[e * 1.1 for e in ET0]), response.default_params())
    dry = (a["practice"] == "dryland").to_numpy()
    assert (b["yield_rel"].to_numpy()[dry] <= a["yield_rel"].to_numpy()[dry] + 1e-12).all()
    irr = ((a["practice"] == "irrigated") & (a["crop"] != "fallow")).to_numpy()
    assert (b["irrigation_mm"].to_numpy()[irr] > a["irrigation_mm"].to_numpy()[irr]).all()


def test_fallow_before_a_dryland_crop_helps():
    years = [2001, 2002, 2003]
    out = response.crop_response(yearly_table(years), monthly_table(years), response.default_params())
    dry = out[(out["practice"] == "dryland") & (out["crop"] != "fallow")]
    assert (dry["stored_mm_after_fallow"] > dry["stored_mm_after_summer"]).all()
    assert (dry["yield_rel_after_fallow"] > dry["yield_rel_after_summer"]).all()
    assert (dry["shortfall_mm_after_fallow"] < dry["shortfall_mm_after_summer"]).all()
    summer = dry[dry["crop"].isin(["corn", "sorghum", "soybeans"])]
    assert (summer["yield_rel_after_wheat"] > summer["yield_rel_after_summer"]).all()
    irr = out[(out["practice"] == "irrigated") & (out["crop"] != "fallow")]
    assert (irr["yield_after_fallow"] == irr["yield_after_summer"]).all()
    assert (irr["irrigation_mm_after_fallow"] < irr["irrigation_mm_after_summer"]).all()


def test_fallow_never_fails():
    years = [2001, 2002]
    worst = response.crop_response(yearly_table(years, hot=183, wheat_hot=61),
                                   monthly_table(years, rain=[0] * 12, et0=[e * 2 for e in ET0]))
    fallow = worst[worst["crop"] == "fallow"]
    assert len(fallow) == 2 * 2
    assert not fallow[["failed"] + [f"failed_after_{k}" for k in PREV_KINDS]].any().any()
    assert (fallow[["yield", "fail_prob", "water_need_mm", "irrigation_mm", "shortfall_mm", "normal_yield"]] == 0).all().all()
    assert (fallow["yield_rel"] == 1).all()
    # in the same year every real dryland crop has failed
    assert worst[(worst["crop"] != "fallow") & (worst["practice"] == "dryland")]["failed"].all()


def test_failed_is_yield_below_half_of_normal_and_fail_prob_follows():
    years = [2001, 2002]
    params = response.default_params()
    params["series"]["corn"]["dryland"]["resid_sd"] = 0.3
    out = response.crop_response(yearly_table(years, hot=60), monthly_table(years), params)
    crops = out[out["crop"] != "fallow"]
    assert (crops["failed"] == (crops["yield_rel"] < FAIL_BELOW)).all()
    for k in PREV_KINDS:
        assert (crops[f"failed_after_{k}"] == (crops[f"yield_rel_after_{k}"] < FAIL_BELOW)).all()
    corn = pick(out, "corn", "dryland")
    assert ((corn["fail_prob"] > 0) & (corn["fail_prob"] < 1)).all()        # graded where the error is known
    assert (corn["fail_prob_after_summer"] >= corn["fail_prob_after_fallow"]).all()
    other = crops[~((crops["crop"] == "corn") & (crops["practice"] == "dryland"))]
    assert (other["fail_prob"] == other["failed"].astype(float)).all()      # 0 or 1 without a resid_sd
    assert response.fail_probability(0.5, 0.2) == pytest.approx(0.5)
    assert response.fail_probability(0.9, 0.2) == pytest.approx(0.02275, abs=1e-4)


def test_heat_and_water_factor_arithmetic():
    assert response.heat_factor([0, 10, 50, 400], 0.004) == pytest.approx([1.0, 0.96, 0.8, 0.0])
    assert response.water_factor([1.0, 0.8, 0.5, 0.0], 1.25) == pytest.approx([1.0, 0.75, 0.375, 0.0])
    # one crop by hand: dryland sorghum after wheat
    years = [2001, 2002]
    params = response.default_params()
    out = response.crop_response(yearly_table(years, hot=50), monthly_table(years), params)
    row = pick(out, "sorghum", "dryland", 2002).iloc[0]
    t = water.water_table(monthly_table(years)).set_index(["crop", "prev", "year"])
    w = t.loc[("sorghum", "wheat", 2002)]
    par = params["series"]["sorghum"]["dryland"]
    wf = 1 - par["ky"] * (1 - w["rainfed_et_mm"] / w["water_need_mm"])
    assert row["water_factor_after_wheat"] == pytest.approx(wf)
    assert row["heat_factor"] == pytest.approx(1 - par["heat_loss"] * 50)
    assert row["yield_rel_after_wheat"] == pytest.approx(par["scale"] * wf * (1 - par["heat_loss"] * 50))
    assert row["shortfall_mm_after_wheat"] == pytest.approx(w["gap_mm"])
    assert row["water_need_mm"] == pytest.approx(w["water_need_mm"])


def test_hot_days_are_capped_at_the_record_plus_a_margin():
    from api.planner.crops import HOT_DAYS_MARGIN, HOT_DAYS_OBSERVED_MAX

    assert HOT_DAYS_OBSERVED_MAX == {"hot_days": 75.0, "wheat_hot_days": 33.0}
    assert HOT_DAYS_MARGIN == {"hot_days": 10.0, "wheat_hot_days": 5.0}
    params = response.default_params()
    assert params["heat_cap"] == {"hot_days": {"observed_max": 75.0, "margin": 10.0, "cap": 85.0},
                                  "wheat_hot_days": {"observed_max": 33.0, "margin": 5.0, "cap": 38.0}}
    assert response.heat_caps(params) == {"hot_days": 85.0, "wheat_hot_days": 38.0}
    assert response.heat_caps({}) == {"hot_days": 85.0, "wheat_hot_days": 38.0}          # nothing given: the built-in cap
    years = [2001, 2002]
    monthly = monthly_table(years)
    at_cap = response.crop_response(yearly_table(years, hot=85, wheat_hot=38), monthly, params)
    beyond = response.crop_response(yearly_table(years, hot=124, wheat_hot=61), monthly, params)
    below = response.crop_response(yearly_table(years, hot=84, wheat_hot=37), monthly, params)
    numbers = [c for c in response.COLUMNS if c not in response.KEYS + ["unit"]]
    assert all(np.array_equal(at_cap[c].to_numpy(), beyond[c].to_numpy()) for c in numbers)
    corn, wheat = pick(beyond, "corn", "dryland").iloc[0], pick(beyond, "winter wheat", "dryland").iloc[0]
    assert corn["heat_days"] == 85.0 and wheat["heat_days"] == 38.0                       # the days that were counted
    assert corn["heat_factor"] == pytest.approx(1 - params["series"]["corn"]["dryland"]["heat_loss"] * 85)
    assert wheat["heat_factor"] == pytest.approx(1 - params["series"]["winter wheat"]["dryland"]["heat_loss"] * 38)
    assert pick(below, "corn", "dryland")["yield"].iloc[0] > corn["yield"]                # inside the range a day still counts
    # the cap comes from the observed table when there is one: its maximum plus the margin
    history = pd.DataFrame({"year": [2000, 2001, 2002], "hot_days": [30.0, 62.0, 41.0], "wheat_hot_days": [9.0, 12.0, 20.0]})
    assert response.observed_hot_day_max(history) == {"hot_days": 62.0, "wheat_hot_days": 20.0}
    lower = response.with_observed_heat_cap(params, history)
    assert response.heat_caps(lower) == {"hot_days": 72.0, "wheat_hot_days": 25.0} and params["heat_cap"]["hot_days"]["cap"] == 85.0
    capped = response.crop_response(yearly_table(years, hot=124, wheat_hot=61), monthly, lower)
    assert pick(capped, "corn", "dryland")["heat_days"].iloc[0] == 72.0
    assert pick(capped, "corn", "dryland")["yield"].iloc[0] > corn["yield"]
    for nothing in (None, history.iloc[:0], pd.DataFrame({"year": [2000]})):              # no table, or not that table
        assert response.with_observed_heat_cap(params, nothing) is params


def test_irrigation_limit_passes_the_unmet_share_through_the_dryland_rule():
    years = [2001, 2002]
    monthly = monthly_table(years, rain=[0.0] * 12)                                       # no rain: everything is pumped
    yearly = yearly_table(years, hot=45)
    params = response.default_params()
    free = response.crop_response(yearly, monthly, params)
    same = response.crop_response(yearly, monthly, params, irrigation_limit_mm=0)
    assert free.equals(same) and free.equals(response.crop_response(yearly, monthly, params, irrigation_limit_mm=None))
    need = pick(free, "corn", "irrigated", 2002)["irrigation_mm"].iloc[0]
    roomy = response.crop_response(yearly, monthly, params, irrigation_limit_mm=need + 50.0)
    assert np.allclose(pick(roomy, "corn", "irrigated")["yield"], pick(free, "corn", "irrigated")["yield"])
    limit = 300.0
    assert need > limit
    short = response.crop_response(yearly, monthly, params, irrigation_limit_mm=limit)
    row, full = pick(short, "corn", "irrigated", 2002).iloc[0], pick(free, "corn", "irrigated", 2002).iloc[0]
    eff = params["constants"]["pivot_efficiency"]
    assert row["irrigation_mm_after_summer"] == pytest.approx(limit)                      # pumps the limit and no more
    assert row["irrigation_net_mm_after_summer"] == pytest.approx(limit * eff)
    unmet = full["irrigation_net_mm_after_summer"] - limit * eff
    assert row["shortfall_mm_after_summer"] == pytest.approx(unmet) and unmet > 0
    ky = params["series"]["corn"]["dryland"]["ky"]                                       # the crop's Ky (textbook here)
    wf = 1 - ky * unmet / row["water_need_mm"]
    assert row["water_factor_after_summer"] == pytest.approx(wf) and full["water_factor_after_summer"] == 1.0
    assert row["yield_rel_after_summer"] == pytest.approx(full["yield_rel_after_summer"] * wf)
    assert row["heat_factor"] == full["heat_factor"]
    # every irrigated crop pumps at most the limit and yields no more than without it; dryland is untouched
    irr, dry = short["practice"] == "irrigated", short["practice"] == "dryland"
    for k in PREV_KINDS:
        assert (short.loc[irr, f"irrigation_mm_after_{k}"] <= limit + 1e-9).all()
        assert (short.loc[irr, f"yield_after_{k}"] <= free.loc[irr, f"yield_after_{k}"] + 1e-9).all()
        assert (short.loc[irr, f"fail_prob_after_{k}"] >= free.loc[irr, f"fail_prob_after_{k}"] - 1e-12).all()
    assert short[dry].equals(free[dry])
    # a tight enough limit makes an irrigated crop fail
    dust = response.crop_response(yearly, monthly, params, irrigation_limit_mm=50.0)
    assert pick(dust, "alfalfa", "irrigated")["failed"].all() and not pick(free, "alfalfa", "irrigated")["failed"].any()
    # the fitted Ky is used where the dryland series has one
    fitted = response.load_params()
    got = response.crop_response(yearly, monthly, fitted, irrigation_limit_mm=limit)
    r2 = pick(got, "corn", "irrigated", 2002).iloc[0]
    assert r2["water_factor_after_summer"] == pytest.approx(
        1 - fitted["series"]["corn"]["dryland"]["ky"] * r2["shortfall_mm_after_summer"] / r2["water_need_mm"])
    for bad in (-1.0, float("nan")):
        with pytest.raises(ValueError):
            response.crop_response(yearly, monthly, params, irrigation_limit_mm=bad)


# --- parameters: JSON and fallback -------------------------------------------------------------------

def test_missing_json_falls_back_to_the_crops_constants(tmp_path):
    params = response.load_params(tmp_path / "nothing.json")
    assert params["source"] == "builtin"
    for crop in CROPS:
        for practice in PRACTICES:
            e = params["series"][crop][practice]
            assert e["heat_loss"] == CROP_TABLE[crop].heat_loss[practice]
            assert e["normal_yield"] == CROP_TABLE[crop].normal_yield[practice]
            assert e["scale"] == CROP_TABLE[crop].scale[practice]
            assert e["ky"] == (CROP_TABLE[crop].ky if practice == "dryland" else 0.0)
    assert params["constants"] == water.DEFAULTS.to_dict() and params["fail_below"] == FAIL_BELOW
    years = [2001, 2002]
    out = response.crop_response(yearly_table(years), monthly_table(years), params)
    assert len(out) == 24 and (out.loc[out["crop"] == "corn", "yield"] > 0).all()


def test_json_values_are_loaded_and_used(tmp_path):
    path = tmp_path / "crop_response.json"
    path.write_text(json.dumps({
        "constants": {"et0_scale": 1.0, "pivot_efficiency": 0.5},
        "fail_below": 0.4,
        "series": {"corn": {"irrigated": {"heat_loss": 0.01, "scale": 1.0, "normal_yield": 200.0, "resid_sd": 0.1, "status": "fitted"}}},
    }))
    params = response.load_params(path)
    assert params["source"] == "json"
    corn = params["series"]["corn"]["irrigated"]
    assert (corn["heat_loss"], corn["scale"], corn["normal_yield"], corn["resid_sd"], corn["status"]) == (0.01, 1.0, 200.0, 0.1, "fitted")
    # what the file does not mention keeps its crops.py value
    assert params["series"]["corn"]["dryland"]["ky"] == CROP_TABLE["corn"].ky
    assert params["series"]["sorghum"]["dryland"]["heat_loss"] == CROP_TABLE["sorghum"].heat_loss["dryland"]
    assert params["constants"]["fallow_efficiency"] == water.DEFAULTS.fallow_efficiency
    years = [2001, 2002]
    out = response.crop_response(yearly_table(years, hot=30), monthly_table(years, rain=[0] * 12, et0=[100] * 12), params)
    row = pick(out, "corn", "irrigated", 2002).iloc[0]
    assert row["yield"] == pytest.approx(200.0 * (1 - 0.01 * 30))
    assert row["water_need_mm"] == pytest.approx(100 * sum(CROP_TABLE["corn"].kc))          # et0_scale 1.0 from the file
    assert row["irrigation_mm"] == pytest.approx(row["water_need_mm"] / 0.5)                 # pivot efficiency from the file
    low = response.crop_response(yearly_table(years, hot=55), monthly_table(years), params)
    assert (low["failed"] == ((low["yield_rel"] < 0.4) & (low["crop"] != "fallow"))).all()   # fail_below from the file
    # the hot-day cap: the file's observed maxima (and margins, if it gives them); crops.py for what it leaves out
    assert response.heat_caps(params) == {"hot_days": 85.0, "wheat_hot_days": 38.0}
    path.write_text(json.dumps({"series": {}, "heat_cap": {"hot_days": {"observed_max": 60, "margin": 4, "cap": 999},
                                                           "wheat_hot_days": {"observed_max": 20}}}))
    assert response.heat_caps(response.load_params(path)) == {"hot_days": 64.0, "wheat_hot_days": 25.0}   # cap = max + margin


@pytest.mark.parametrize("text", ["not json at all", "[1, 2, 3]", '{"series": "nope"}', '{"no_series": {}}',
                                  '{"series": {"corn": {"dryland": {"ky": "abc"}}}}',
                                  '{"series": {"corn": {"dryland": {"scale": -1}}}}',
                                  '{"series": {"corn": {"dryland": {"heat_loss": null}}}}',
                                  '{"series": {}, "heat_cap": {"hot_days": {"observed_max": "many"}}}',
                                  '{"series": {}, "heat_cap": {"hot_days": {"observed_max": -3}}}'])
def test_broken_json_falls_back(tmp_path, text):
    path = tmp_path / "crop_response.json"
    path.write_text(text)
    params = response.load_params(path)
    assert params["source"] == "builtin"
    assert params["series"]["corn"]["dryland"]["ky"] == CROP_TABLE["corn"].ky


def test_default_path_is_next_to_the_module(monkeypatch, tmp_path):
    assert response.PARAMS_PATH.name == "crop_response.json" and response.PARAMS_PATH.parent.name == "planner"
    monkeypatch.setattr(response, "PARAMS_PATH", tmp_path / "missing.json")
    years = [2001, 2002]
    out = response.crop_response(yearly_table(years), monthly_table(years))       # params=None, file missing
    assert len(out) == 24 and not out.drop(columns=["unit"]).isna().any().any()


# --- calibrate.py on made-up history -----------------------------------------------------------------

def synthetic_history(n_years=22, seed=3):
    rng = np.random.default_rng(seed)
    years = list(range(2000, 2000 + n_years))
    wet = {y: float(rng.uniform(0.45, 1.6)) for y in range(1999, 2000 + n_years)}
    monthly = monthly_table(years, rain=lambda y, m: RAIN[m - 1] * wet[y])
    history = pd.DataFrame({"year": years, "hot_days": rng.integers(15, 75, n_years).astype(float),
                            "wheat_hot_days": rng.integers(5, 35, n_years).astype(float)})
    return years, history, monthly


def synthetic_yields(years, history, monthly, crop, practice, ky, heat_loss, base, slope, noise, seed=5):
    inputs = calibrate.observed_inputs(history, monthly, water.DEFAULTS)
    raw = calibrate.raw_response(inputs[crop]["days"], inputs[crop]["ratio"], CROP_TABLE[crop].typical_prev[practice],
                                 np.array([ky]), np.array([heat_loss]), practice == "irrigated")[0, 0]
    rng = np.random.default_rng(seed)
    t = np.array(years) - years[0]
    y = (base + slope * t) * raw / raw.mean() * (1 + noise * rng.standard_normal(len(years)))
    obs = pd.DataFrame({"year": years, "value": y, "level": "county", "source": "by practice"})
    return inputs, obs


def test_calibration_recovers_known_parameters():
    years, history, monthly = synthetic_history()
    inputs, obs = synthetic_yields(years, history, monthly, "corn", "dryland", ky=1.1, heat_loss=0.006, base=50, slope=0.5, noise=0.03)
    e = calibrate.calibrate_series("corn", "dryland", obs, inputs)["entry"]
    assert e["status"] == "fitted" and e["free_parameters"] == ["ky", "heat_loss"]
    assert e["ky"] == pytest.approx(1.1, abs=0.15)
    assert e["heat_loss"] == pytest.approx(0.006, abs=0.0015)
    assert e["loo_skill"] > 0.8 and e["r2"] >= e["loo_skill"] and e["verdict"] == "good"
    assert e["normal_yield"] == pytest.approx(50 + 0.5 * 21, rel=0.08)          # the trend at the last year
    assert 0 < e["resid_sd"] < 0.1
    assert e["n_years"] == 22 and e["levels"] == {"county": 22}

    inputs, obs = synthetic_yields(years, history, monthly, "corn", "irrigated", ky=0.0, heat_loss=0.004, base=190, slope=1.0, noise=0.02)
    e = calibrate.calibrate_series("corn", "irrigated", obs, inputs)["entry"]
    assert e["status"] == "fitted" and e["free_parameters"] == ["heat_loss"] and e["ky"] == 0.0
    assert e["heat_loss"] == pytest.approx(0.004, abs=0.001)


def test_calibration_keeps_textbook_values_when_it_should():
    years, history, monthly = synthetic_history()
    corn = CROP_TABLE["corn"]
    # too few years
    inputs, obs = synthetic_yields(years, history, monthly, "corn", "dryland", 1.1, 0.006, 50, 0.5, 0.03)
    e = calibrate.calibrate_series("corn", "dryland", obs.iloc[:9], inputs)["entry"]
    assert (e["status"], e["verdict"], e["free_parameters"]) == ("textbook", "weak", [])
    assert e["ky"] == corn.ky and e["heat_loss"] == corn.heat_loss["dryland"] and e["loo_skill"] is None
    assert e["status_text"].startswith("Nothing fitted (only 9 usable years)") and "assumption" in e["status_text"]
    # no data at all
    empty = pd.DataFrame({"year": [], "value": [], "level": [], "source": []})
    e = calibrate.calibrate_series("corn", "dryland", empty, inputs)["entry"]
    assert e["status"] == "textbook" and e["normal_yield"] == corn.normal_yield["dryland"] and e["n_years"] == 0
    assert "no yield data" in e["status_text"] and "assumption" in e["status_text"]
    # yields that have nothing to do with the weather: no skill, textbook, weak
    rng = np.random.default_rng(11)
    noise = obs.assign(value=50 * (1 + 0.2 * rng.standard_normal(len(obs))))
    e = calibrate.calibrate_series("corn", "dryland", noise, inputs)["entry"]
    assert e["status"] == "textbook" and e["verdict"] == "weak"
    assert e["ky"] == corn.ky and e["heat_loss"] == corn.heat_loss["dryland"]
    assert "assumption" in e["status_text"]
    # a response with no heat in it: the fitted heat loss sits on its bound, so the textbook one is kept
    inputs, obs = synthetic_yields(years, history, monthly, "corn", "dryland", 1.1, 0.0, 50, 0.5, 0.02)
    e = calibrate.calibrate_series("corn", "dryland", obs, inputs)["entry"]
    assert e["heat_loss"] == corn.heat_loss["dryland"] and "heat_loss" not in e["free_parameters"]
    assert e["status"] in ("partly textbook", "textbook")
    assert any("on a bound" in n for n in e["notes"])
    # and it is called what it is: an assumption the record did not support, not a finding
    assert "assumption the county record did not support" in e["status_text"]
    if e["status"] == "partly textbook":
        assert "Heat loss is an assumption" in e["status_text"] and "Ky is fitted" in e["status_text"]
    # a series fitted in full says so
    inputs, obs = synthetic_yields(years, history, monthly, "corn", "dryland", 1.1, 0.006, 50, 0.5, 0.03)
    e = calibrate.calibrate_series("corn", "dryland", obs, inputs)["entry"]
    assert e["status"] == "fitted" and e["status_text"] == "Fitted to the county yields."


def nass_rows(crop, practice, level, values, stat="yield"):
    return [{"year": y, "crop": crop, "practice": practice, "stat": stat, "value": float(v), "unit": "x",
             "level": level, "source_desc": "made up"} for y, v in values.items()]


def test_build_series_rules():
    rows = []
    # sorghum: split 2000-2004 (2004 only at state level), all-practices 2000-2009 (2009 only at state level)
    rows += nass_rows("sorghum", "dryland", "county", {y: 50 + y - 2000 for y in range(2000, 2004)})
    rows += nass_rows("sorghum", "dryland", "state", {2004: 80})
    rows += nass_rows("sorghum", "irrigated", "county", {y: 100 for y in range(2000, 2004)})
    rows += nass_rows("sorghum", "all", "county", {y: 2 * (50 + y - 2000) for y in range(2000, 2009)})
    rows += nass_rows("sorghum", "all", "state", {2009: 90})
    rows += nass_rows("sorghum", "dryland", "county", {y: 9000 for y in range(2000, 2004)}, stat="harvested_acres")
    rows += nass_rows("sorghum", "irrigated", "county", {y: 1000 for y in range(2000, 2004)}, stat="harvested_acres")
    # corn: irrigated does not dominate enough, so its all-practices years are never used
    rows += nass_rows("corn", "irrigated", "county", {y: 190 for y in range(2000, 2004)})
    rows += nass_rows("corn", "all", "county", {y: 170 for y in range(2000, 2010)})
    # alfalfa has no split
    rows += nass_rows("alfalfa", "all", "district", {y: 5 for y in range(2000, 2006)})
    series = calibrate.build_series(pd.DataFrame(rows))

    s = series[("sorghum", "dryland")]
    assert list(s["year"]) == list(range(2000, 2009))                    # state rows dropped, 2004-2008 from all-practices
    assert list(s.loc[s["year"] <= 2003, "source"]) == ["by practice"] * 4
    assert set(s.loc[s["year"] >= 2004, "source"]) == {"all practices x 0.50"}
    assert s.loc[s["year"] == 2006, "value"].iloc[0] == pytest.approx(56.0)   # 112 x 0.5
    assert s.attrs["nass_share"] == pytest.approx(0.9)
    assert list(series[("sorghum", "irrigated")]["year"]) == list(range(2000, 2004))
    assert list(series[("corn", "irrigated")]["year"]) == list(range(2000, 2004))
    assert len(series[("corn", "dryland")]) == 0
    a = series[("alfalfa", "irrigated")]
    assert list(a["year"]) == list(range(2000, 2006)) and set(a["level"]) == {"district"}
    assert len(series[("alfalfa", "dryland")]) == 0

    # the dominant practice must also dominate in the NASS acres
    rows += nass_rows("soybeans", "irrigated", "county", {y: 55 for y in range(2000, 2004)})
    rows += nass_rows("soybeans", "all", "county", {y: 50 for y in range(2000, 2010)})
    rows += nass_rows("soybeans", "irrigated", "county", {y: 600 for y in range(2000, 2004)}, stat="harvested_acres")
    rows += nass_rows("soybeans", "dryland", "county", {y: 400 for y in range(2000, 2004)}, stat="harvested_acres")
    series = calibrate.build_series(pd.DataFrame(rows))
    assert list(series[("soybeans", "irrigated")]["year"]) == list(range(2000, 2004))


def test_calibrate_end_to_end_on_made_up_tables():
    years, history, monthly = synthetic_history()
    _, wheat = synthetic_yields(years, history, monthly, "winter wheat", "dryland", 1.0, 0.012, 35, 0.2, 0.03)
    _, corn = synthetic_yields(years, history, monthly, "corn", "irrigated", 0.0, 0.004, 190, 1.0, 0.02)
    rows = nass_rows("winter wheat", "dryland", "county", dict(zip(wheat["year"], wheat["value"])))
    rows += nass_rows("corn", "irrigated", "district", dict(zip(corn["year"], corn["value"])))
    nass = pd.DataFrame(rows)
    result = calibrate.calibrate(nass, history, monthly)
    params = result["params"]
    assert set(params["series"]) == set(CROPS)
    assert params["series"]["winter wheat"]["dryland"]["status"] == "fitted"
    assert params["series"]["corn"]["irrigated"]["levels"] == {"district": 22}
    assert params["series"]["sorghum"]["dryland"]["status"] == "textbook"
    assert params["constants"] == water.DEFAULTS.to_dict()
    json.dumps(params)                                                    # everything is plain JSON
    assert len(calibrate.fit_lines(params)) == 10
    # the hot-day cap is the most any observed year had, plus the margin of crops.py
    cap = params["heat_cap"]
    assert cap["hot_days"]["observed_max"] == history["hot_days"].max() and cap["hot_days"]["margin"] == 10.0
    assert cap["hot_days"]["cap"] == history["hot_days"].max() + 10.0
    assert cap["wheat_hot_days"]["cap"] == history["wheat_hot_days"].max() + 5.0
    assert cap["hot_days"]["years"] == f"{history['year'].min()}-{history['year'].max()}"
    assert all(params["series"][c][p]["status_text"] for c in CROPS for p in PRACTICES)
    # the result works as parameters, and a calibrated series averages 1 over its years
    merged = response._merge(response.default_params(), params)
    out = response.crop_response(history, monthly, merged)
    assert pick(out, "winter wheat", "dryland")["yield_rel"].mean() == pytest.approx(1.0, abs=0.01)
    assert pick(out, "corn", "irrigated")["yield_rel"].mean() == pytest.approx(1.0, abs=0.01)
    check = calibrate.failure_check(nass, out, result["rows"], params)
    assert set(check["tally"]["crop"]) == {"winter wheat", "corn"}
    assert calibrate.water_summary(out).loc["corn", "irrigation_mm"] > 0


# --- the real tables (skipped when they are not built) ------------------------------------------------

DATA = ROOT / "data"
REAL = {name: DATA / f"{name}.parquet" for name in ("climate_history", "climate_monthly", "climate_futures", "nass_finney")}


def real(*names):
    missing = [str(REAL[n].relative_to(ROOT)) for n in names if not REAL[n].exists()]
    if missing:
        pytest.skip("not built: " + ", ".join(missing))
    return [pd.read_parquet(REAL[n]) for n in names]


def test_tracked_json_is_present_and_sane():
    if not response.PARAMS_PATH.exists():
        pytest.skip("api/planner/crop_response.json has not been written yet")
    params = response.load_params()
    assert params["source"] == "json"
    raw = json.loads(response.PARAMS_PATH.read_text())
    for crop in CROPS:
        for practice in PRACTICES:
            e = params["series"][crop][practice]
            assert 0 <= e["heat_loss"] <= 0.025 and 0 <= e["ky"] <= 2.0 and e["scale"] > 0
            if crop != "fallow":
                assert e["normal_yield"] > 0
                r = raw["series"][crop][practice]
                assert r["verdict"] in ("good", "fair", "weak") and r["status"] in ("fitted", "partly textbook", "textbook")
                assert r["status_text"].endswith(".")
                assert (r["status"] == "fitted") == (r["status_text"] == "Fitted to the county yields.")
                if r["status"] == "textbook":
                    assert r["verdict"] == "weak"
                    assert r["ky"] == (CROP_TABLE[crop].ky if practice == "dryland" else 0.0)
                    assert r["heat_loss"] == CROP_TABLE[crop].heat_loss[practice]
                else:
                    assert r["loo_skill"] > 0 and r["n_years"] >= calibrate.MIN_YEARS and len(r["free_parameters"]) <= 2


def test_tracked_json_says_what_is_an_assumption_and_carries_the_heat_cap():
    if not response.PARAMS_PATH.exists():
        pytest.skip("api/planner/crop_response.json has not been written yet")
    raw = json.loads(response.PARAMS_PATH.read_text())
    wheat = raw["series"]["winter wheat"]["dryland"]
    if "heat_loss" not in wheat["free_parameters"]:          # the textbook heat loss was kept: say so plainly
        assert "Heat loss is an assumption the county record did not support" in wheat["status_text"]
    cap = raw["heat_cap"]
    assert cap["hot_days"]["cap"] == cap["hot_days"]["observed_max"] + cap["hot_days"]["margin"]
    assert cap["hot_days"]["margin"] == 10.0 and cap["wheat_hot_days"]["margin"] == 5.0
    assert response.heat_caps(response.load_params()) == {c: v["cap"] for c, v in cap.items()}
    if REAL["climate_history"].exists():                     # and it is the record's own maximum
        history = pd.read_parquet(REAL["climate_history"])
        assert cap["hot_days"]["observed_max"] == history["hot_days"].max()
        assert cap["wheat_hot_days"]["observed_max"] == history["wheat_hot_days"].max()


def test_real_futures_table_is_fast_and_complete():
    futures, monthly = real("climate_futures", "climate_monthly")
    params = response.load_params()
    response.crop_response(futures.head(5), monthly, params)        # warm up
    start = time.perf_counter()
    out = response.crop_response(futures, monthly, params)
    took = time.perf_counter() - start
    assert len(out) == len(futures) * 12
    assert not out.drop(columns=["unit"]).isna().any().any()
    assert took < 1.0, f"crop_response took {took:.2f} s"
    assert not out.loc[out["crop"] == "fallow", "failed"].any()
    irr = out[(out["practice"] == "irrigated") & (out["crop"] != "fallow")]
    assert not irr["failed"].any()                                   # heat alone never halves an irrigated crop here
    assert irr["irrigation_mm"].between(0, 1500).all() and 250 < irr["irrigation_mm"].mean() < 650
    assert (out[["yield_rel"] + [f"yield_rel_after_{k}" for k in PREV_KINDS]] <= MAX_YIELD_REL).all().all()
    dry = out[(out["practice"] == "dryland") & (out["crop"].isin(["corn", "sorghum", "winter wheat"]))]
    assert 0.02 < dry["failed"].mean() < 0.6
    # hotter futures cost yield: the end of the high-emissions run against the same models' start
    late = out[(out["scenario"] == "ssp585") & (out["year"] >= 2050) & (out["crop"] != "fallow")]
    early = out[(out["scenario"] == "ssp585") & (out["year"].between(2015, 2034)) & (out["crop"] != "fallow")]
    assert (late.groupby(["crop", "practice"])["yield_rel"].mean() < early.groupby(["crop", "practice"])["yield_rel"].mean()).all()


def test_real_observed_years_average_about_normal():
    history, monthly = real("climate_history", "climate_monthly")
    for params in (response.default_params(), response.load_params()):
        out = response.crop_response(history, monthly, params)
        assert len(out) == 25 * 12
        means = out[out["crop"] != "fallow"].groupby(["crop", "practice"])["yield_rel"].mean()
        # built-in scales are exact over 2000-2024; calibrated ones over their own (shorter) calibration years
        tolerance = 0.01 if params["source"] == "builtin" else 0.15
        assert ((means - 1.0).abs() < tolerance).all(), means.round(3).to_dict()
    need = out[out["practice"] == "irrigated"].groupby("crop")["water_need_mm"].mean()
    assert 600 < need["corn"] < 850 and 450 < need["sorghum"] < 700 and 800 < need["alfalfa"] < 1100


def test_real_calibration_writes_only_where_told(tmp_path):
    real("climate_history", "climate_monthly", "nass_finney")
    tracked = response.PARAMS_PATH.read_text() if response.PARAMS_PATH.exists() else None
    code = calibrate.main(["--json", str(tmp_path / "p.json"), "--report", str(tmp_path / "r.md"),
                           "--log-dir", str(tmp_path / "logs")])
    assert code == 0
    written = json.loads((tmp_path / "p.json").read_text())
    assert set(written["series"]) == set(CROPS)
    assert "Fit per crop and practice" in (tmp_path / "r.md").read_text()
    assert (tmp_path / "logs" / "calibrate.log").exists() and (tmp_path / "logs" / "crop_response_fit.csv").exists()
    assert (response.PARAMS_PATH.read_text() if response.PARAMS_PATH.exists() else None) == tracked
    if tracked is not None:    # the tracked file is what the calibration gives today (apart from the date)
        old = json.loads(tracked)
        for crop in CROPS:
            for practice in PRACTICES:
                for key in ("ky", "heat_loss", "scale", "normal_yield"):
                    assert written["series"][crop][practice][key] == pytest.approx(old["series"][crop][practice][key], rel=1e-6)
