"""Water reality check (Phase 4, item 6): pipeline/groundwater.py, api/groundwater.py, GET /groundwater,
`water_outlook` on the plans of an irrigated field, and the `limit_in` what-if.

Everything runs on made-up wells and the made-up climate tables of test_api_smart, so it never needs data/
or the network. The last tests use the real table and skip without it.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from api import groundwater as api_gw  # noqa: E402
from api.planner import smart  # noqa: E402
from pipeline import groundwater as gw  # noqa: E402
from pipeline import groundwater_schema as schema  # noqa: E402

import test_api_smart as api_tests  # noqa: E402
from test_api_smart import (  # noqa: E402,F401  (fixtures)
    basic_client, clean_engine, client, fields_file, no_tables, tables,
)
from test_planner_smart import IRRIGATED_HISTORY, a_field, engine  # noqa: E402,F401

DRY, WET = api_tests.DRY, api_tests.WET
REAL = ROOT / "data" / "groundwater.parquet"


# --- made-up wells -----------------------------------------------------------------------------------

def well_rows(well_id, lon, lat, *, start_depth=100.0, rate=2.0, winters=range(1996, 2027), well_depth=300.0,
              aquifer="N100HGHPLN", county="20055", status="Static", source="kgs", month=1):
    """One reading a winter on the 10th of `month`: depth = start_depth + rate x (winter - first winter)."""
    winters = list(winters)
    rows = []
    for winter in winters:
        year = winter - 1 if month == 12 else winter
        rows.append({"well_id": well_id, "date": pd.Timestamp(year=year, month=month, day=10), "date_exact": True,
                     "depth_ft": float(start_depth + rate * (winter - winters[0])), "status": status,
                     "well_depth_ft": float(well_depth) if well_depth is not None else math.nan,
                     "lon": float(lon), "lat": float(lat), "aquifer": aquifer, "county_fips": county, "source": source})
    return rows


def table(*wells) -> pd.DataFrame:
    df = pd.DataFrame([row for rows in wells for row in rows], columns=schema.COLUMNS)
    df["date_exact"] = df["date_exact"].astype(bool)
    return df


def five_wells(lon=-100.9, lat=37.9):
    """Five wells 1 to 5 miles north of (lon, lat), dropping 1, 2, 3, 4 and 5 feet a year, and one 30 miles off."""
    per_mile = 1 / 69.05
    wells = [well_rows(f"USGS-{i}", lon, lat + i * per_mile, rate=float(i), start_depth=100.0, well_depth=300.0)
             for i in range(1, 6)]
    wells.append(well_rows("USGS-far", lon, lat + 30 * per_mile, rate=9.0))
    return table(*wells)


# --- the contract ------------------------------------------------------------------------------------

def test_a_made_up_table_is_valid_and_round_trips(tmp_path):
    df = five_wells()
    assert schema.check_groundwater(df) == []
    path = tmp_path / "groundwater.parquet"
    df.to_parquet(path, index=False)
    back = schema.load_groundwater(path)
    assert list(back.columns) == schema.COLUMNS and len(back) == len(df)
    assert "6 wells" in schema.summarize(back) and schema.main([str(path)]) == 0
    assert schema.main([str(tmp_path / "missing.parquet")]) == 1


@pytest.mark.parametrize("change, word", [
    (lambda d: d.drop(columns=["depth_ft"]), "missing column"),
    (lambda d: d.iloc[0:0], "no rows"),
    (lambda d: pd.concat([d, d.iloc[[0]]]), "repeat a well and day"),
    (lambda d: d.assign(depth_ft=d["depth_ft"].where(d.index != 3, -4.0)), "depth_ft"),
    (lambda d: d.assign(depth_ft=d["depth_ft"].where(d.index != 3, np.nan)), "depth_ft"),
    (lambda d: d.assign(source="somewhere"), "source"),
    (lambda d: d.assign(lat=95.0), "lat"),
    (lambda d: d.assign(well_id=None), "well_id"),
    (lambda d: d.assign(date_exact=1), "date_exact"),
])
def test_the_validator_names_what_is_wrong(change, word):
    problems = schema.check_groundwater(change(five_wells()))
    assert problems and any(word in p for p in problems), problems
    with pytest.raises(schema.GroundwaterSchemaError):
        schema.validate_groundwater(change(five_wells()))


# --- reading the two sources -------------------------------------------------------------------------

def usgs_measurement(well, value, year, month, day, qualifier=("Static",), parameter="72019", unit="ft"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-100.5, 38.0]},
            "properties": {"monitoring_location_id": well, "parameter_code": parameter, "value": str(value),
                           "unit_of_measure": unit, "year": year, "month": month, "day": day,
                           "qualifier": list(qualifier) if qualifier else None}}


def usgs_well(well, depth=250.0, hole=None, aquifer="N100HGHPLN", lon=-100.5, lat=38.0):
    return {"type": "Feature", "id": well, "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"id": well, "monitoring_location_number": well.split("-")[-1], "state_code": "20",
                           "county_code": "055", "well_constructed_depth": depth, "hole_constructed_depth": hole,
                           "national_aquifer_code": aquifer}}


def test_usgs_measurements_keep_feet_with_a_known_month():
    rows, dropped = gw.measurements_table([
        usgs_measurement("USGS-1", 120.5, 2001, 1, 9),
        usgs_measurement("USGS-1", 121.0, 2002, 1, None),               # day unknown: the 15th, flagged
        usgs_measurement("USGS-1", 122.0, 2003, None, None),            # year only: cannot be placed in a season
        usgs_measurement("USGS-1", -3.0, 2004, 1, 5),                   # above the land surface: a flowing well
        usgs_measurement("USGS-1", 3000.0, 2005, 1, 5, parameter="62611"),   # an elevation, not a depth
        usgs_measurement("USGS-1", 123.0, 2006, 1, 5, qualifier=("Above", "Pumping")),
    ])
    assert dropped == {"usgs_not_feet": 1, "usgs_no_month": 1, "usgs_bad_value": 1}
    assert list(rows["depth_ft"]) == [120.5, 121.0, 123.0]
    assert list(rows["date"]) == [pd.Timestamp("2001-01-09"), pd.Timestamp("2002-01-15"), pd.Timestamp("2006-01-05")]
    assert list(rows["date_exact"]) == [True, False, True]
    assert list(rows["status"]) == ["Static", "Static", "Above, Pumping"] and set(rows["source"]) == {"usgs"}


def test_wizard_rows_follow_its_status_letters():
    levels = pd.DataFrame({
        "USGS_ID": ["111", "111", "111", "111", "222", "111"],
        "MEASUREMENT_DATE_AND_TIME": ["01/08/2020", "01/01/1990", "07/20/2021", "01/05/2022", "01/09/2023", "bad"],
        "DEPTH_TO_WATER": ["150.25", "120", "170", None, "80.5", "9"],
        "STATUS": [None, "B", "P", "D", "A", None],
    })
    rows, dropped = gw.wizard_measurements(levels, {"111": "USGS-111"})
    assert dropped == {"kgs_no_value": 1, "kgs_bad_value": 0, "kgs_bad_date": 1}
    assert list(rows["well_id"]) == ["USGS-111", "USGS-111", "USGS-111", "KGS-222"]
    assert list(rows["date"]) == [pd.Timestamp("2020-01-08"), pd.Timestamp("1990-01-15"), pd.Timestamp("2021-07-20"),
                                  pd.Timestamp("2023-01-09")]
    assert list(rows["date_exact"]) == [True, False, True, True]
    assert list(rows["status"]) == ["Static", "Static", "Pumping", "Anomalous (KGS check)"]
    assert gw.is_disturbed("Pumping") and gw.is_disturbed("Anomalous (KGS check)") and gw.is_disturbed("Above, Pumping")
    assert not gw.is_disturbed("Static") and not gw.is_disturbed(None) and not gw.is_disturbed("ForeignSubstance")


def test_the_two_sources_are_joined_without_counting_a_reading_twice():
    measurements = [usgs_measurement("USGS-111", 100.0, 2014, 1, 8), usgs_measurement("USGS-111", 102.0, 2015, 1, None),
                    usgs_measurement("USGS-333", 50.0, 2015, 1, 7)]
    wells = [usgs_well("USGS-111", depth=95.0, hole=None), usgs_well("USGS-333", depth=None, hole=90.0, aquifer=None)]
    sites = pd.DataFrame({"USGS_ID": ["111", "222"], "STATE_CODE": ["20", "20"], "COUNTY_CODE": ["55", "93"],
                          "LATITUDE": ["38.0", "38.1"], "LONGITUDE": ["-100.5", "-100.6"],
                          "DEPTH_OF_WELL": ["260", None], "DEPTH_OF_HOLE": [None, "410"],
                          "GEOLOGICAL_UNIT1": ["TO", None], "AQUIFER_CODE": [None, "210DKOT"]})
    levels = pd.DataFrame({
        "USGS_ID": ["111", "111", "111", "222", "222"],
        "MEASUREMENT_DATE_AND_TIME": ["01/08/2014", "01/06/2015", "01/07/2016", "01/09/2016", "01/09/2016"],
        "DEPTH_TO_WATER": ["100.0", "102.1", "104.0", "70", "72"], "STATUS": [None, None, None, None, None]})
    out, dropped = gw.build_table(measurements, wells, sites, levels)
    assert schema.check_groundwater(out) == []
    # 2014: the same day in both; 2015: the USGS knows the month only, so the KGS reading of that month is the same one
    assert dropped["kgs_already_in_usgs"] == 2 and dropped["same_day"] == 1
    w111 = out[out["well_id"] == "USGS-111"]
    assert list(w111["depth_ft"]) == [100.0, 102.0, 104.0] and list(w111["source"]) == ["usgs", "usgs", "kgs"]
    assert set(w111["well_depth_ft"]) == {260.0} and set(w111["aquifer"]) == {"N100HGHPLN"}     # the KGS depth wins
    only_kgs = out[out["well_id"] == "KGS-222"]
    assert list(only_kgs["depth_ft"]) == [71.0]                                    # two readings on one day: their mean
    assert only_kgs["well_depth_ft"].iloc[0] == 410.0 and only_kgs["aquifer"].iloc[0] == "KGS:210DKOT"
    assert only_kgs["county_fips"].iloc[0] == "20093" and only_kgs["lon"].iloc[0] == -100.6
    hole = out[out["well_id"] == "USGS-333"]
    assert hole["well_depth_ft"].iloc[0] == 90.0 and hole["aquifer"].iloc[0] is None
    # without the KGS files the USGS rows stand alone
    alone, _ = gw.build_table(measurements, wells)
    assert len(alone) == 3 and set(alone["source"]) == {"usgs"}


def test_cached_answers_are_never_asked_for_again(tmp_path, monkeypatch):
    import requests

    def no_network(*args, **kwargs):
        raise AssertionError("the network was used although every answer is cached")

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "Session", no_network)
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    params = {"parameter_code": "72019", "bbox": "-101,37,-100,38"}
    first = f"{gw.API}/field-measurements/items?f=json&limit={gw.PAGE_SIZE}&parameter_code=72019&bbox=-101%2C37%2C-100%2C38"
    import hashlib

    tag = hashlib.sha1(first.encode()).hexdigest()[:10]
    page1 = {"features": [usgs_measurement("USGS-1", 10, 2001, 1, 2)], "links": [{"rel": "next", "href": "https://x/next"}]}
    page2 = {"features": [usgs_measurement("USGS-1", 11, 2002, 1, 2)], "links": []}
    (tmp_path / f"field-measurements_{tag}_p0001.json").write_text(json.dumps(page1))
    (tmp_path / f"field-measurements_{tag}_p0002.json").write_text(json.dumps(page2))
    features = gw.fetch_pages("field-measurements", params, tmp_path)
    assert [f["properties"]["value"] for f in features] == ["10", "11"]

    bbox = (-101.0, 37.0, -100.0, 38.0)
    tag = hashlib.sha1(f"wizard|{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}".encode()).hexdigest()[:10]
    for name in ("sites", "levels"):
        (tmp_path / f"wizard_{tag}_{name}.csv").write_text("USGS_ID\n1\n")
    sites, levels = gw.fetch_wizard(bbox, tmp_path)
    assert sites.name.endswith("_sites.csv") and levels.name.endswith("_levels.csv")


def test_a_page_that_is_not_data_does_not_stay_in_the_cache(tmp_path, monkeypatch):
    def fake_download(url, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text(json.dumps({"code": "TooManyRequests"}))
        return Path(dest)

    monkeypatch.setattr(gw, "download", fake_download)
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="not a feature collection"):
        gw.fetch_pages("field-measurements", {"bbox": "0,0,1,1"}, tmp_path)
    assert list(tmp_path.iterdir()) == []


# --- winter levels and trends ------------------------------------------------------------------------

def test_winter_level_is_the_median_of_resting_readings_december_to_march():
    df = table(
        well_rows("A", -100, 38, winters=[2001], start_depth=100.0),                       # January 2001
        well_rows("A", -100, 38, winters=[2001], start_depth=104.0, month=12),             # December 2000: winter 2001
        well_rows("A", -100, 38, winters=[2001], start_depth=110.0, month=3),
        well_rows("A", -100, 38, winters=[2001], start_depth=300.0, month=2, status="Pumping"),   # left out
        well_rows("A", -100, 38, winters=[2001], start_depth=250.0, month=7),              # summer: left out
        well_rows("A", -100, 38, winters=[2002], start_depth=107.0),
    )
    levels = gw.winter_levels(df)
    assert list(levels["winter"]) == [2001, 2002]
    assert list(levels["depth_ft"]) == [104.0, 107.0] and list(levels["n"]) == [3, 1]


def test_trend_is_the_measured_rate_with_an_interval_and_shrugs_off_one_odd_winter():
    rows = well_rows("A", -100.5, 38.0, start_depth=100.0, rate=2.0, well_depth=300.0)
    rows[10]["depth_ft"] += 60.0                                   # one winter far off the line
    trends = gw.well_trends(table(rows))
    assert len(trends) == 1
    t = trends.iloc[0]
    assert t["slope_ft_per_year"] == pytest.approx(2.0, abs=0.01)
    assert t["slope_low"] <= 2.0 <= t["slope_high"] and t["slope_high"] - t["slope_low"] < 0.5
    assert (t["n_winters"], t["first_winter"], t["last_winter"]) == (31, 1996, 2026)
    assert t["last_depth_ft"] == 160.0 and t["column_ft"] == 140.0
    assert t["intercept_ft"] + 2.0 * 2026 == pytest.approx(160.0, abs=0.5)
    # if the rate simply continued: 14 years x 2 ft = 28 ft less in 2040, 68 ft less in 2060
    assert t["column_2040_ft"] == pytest.approx(112.0, abs=0.2) and t["column_2060_ft"] == pytest.approx(72.0, abs=0.4)


def test_the_column_never_goes_below_the_bottom_or_above_the_top_of_the_well():
    assert gw.column_in(20.0, 5.0, 2026, 2040, 200.0) == 0.0             # the line passes the bottom: 0, not negative
    assert gw.column_in(20.0, -5.0, 2026, 2060, 100.0) == 100.0          # a rising well cannot hold more than its depth
    assert gw.column_in(50.0, 1.0, 2026, 2036, 200.0) == 40.0


def test_the_trend_rule_leaves_out_short_old_and_bedrock_records():
    fine = well_rows("fine", -100.5, 38.0)
    df = table(
        fine,
        well_rows("nine", -100.5, 38.0, winters=range(2018, 2027)),                 # 9 winters
        well_rows("gappy", -100.5, 38.0, winters=[*range(2012, 2021), 2026]),       # 10 winters over 14 years: in
        well_rows("stale", -100.5, 38.0, winters=range(1996, 2016)),                # stopped 11 winters ago
        well_rows("summer", -100.5, 38.0, month=7),                                 # never measured in winter
        well_rows("pumped", -100.5, 38.0, status="Recently pumped"),
        well_rows("dakota", -100.5, 38.0, aquifer="KGS:KD"),
        well_rows("cretaceous", -100.5, 38.0, aquifer="N300LCRTCS"),
        well_rows("unknown", -100.5, 38.0, aquifer=None),
        well_rows("shallow", -100.5, 38.0, well_depth=120.0),                       # "depth" above today's water
        well_rows("no_depth", -100.5, 38.0, well_depth=None),
    )
    trends = gw.well_trends(df).set_index("well_id")
    assert sorted(trends.index) == ["fine", "gappy", "no_depth", "shallow", "unknown"]
    assert math.isnan(trends.loc["shallow", "column_ft"]) and math.isnan(trends.loc["shallow", "well_depth_ft"])
    assert math.isnan(trends.loc["no_depth", "column_2040_ft"])
    assert gw.is_bedrock("KGS:210DKOT") and not gw.is_bedrock("KGS:TO") and not gw.is_bedrock(None)
    assert len(gw.well_trends(table(well_rows("summer", -100.5, 38.0, month=7)))) == 0


def test_summary_counts_the_wells_that_fall_beyond_their_own_uncertainty():
    df = table(
        well_rows("falls", -100.5, 38.0, rate=3.0),
        well_rows("rises", -100.5, 38.0, rate=-1.0, start_depth=200.0, well_depth=None),
        well_rows("flat", -100.5, 38.0, rate=0.0, start_depth=150.0),
    )
    summary = gw.summarize_trends(gw.well_trends(df))
    assert (summary["wells"], summary["falling"], summary["rising"]) == (3, 1, 1)
    assert summary["decline_ft_per_year"]["median"] == pytest.approx(0.0, abs=1e-9)
    assert (summary["first_winter"], summary["last_winter"]) == (1996, 2026)
    assert summary["column"]["wells"] == 2 and summary["column"]["now_ft"]["max"] == pytest.approx(150.0)
    assert gw.summarize_trends(gw.well_trends(df).iloc[0:0]) is None


# --- the service -------------------------------------------------------------------------------------

@pytest.fixture()
def service(tmp_path):
    path = tmp_path / "groundwater.parquet"
    five_wells().to_parquet(path, index=False)
    return api_gw.GroundwaterService(path)


def test_miles_between_is_about_right():
    north = api_gw.miles_between(-100.9, 37.9, np.array([-100.9]), np.array([38.9]))[0]
    assert north == pytest.approx(69.1, abs=0.2)
    assert api_gw.miles_between(-100.9, 37.9, np.array([-100.9]), np.array([37.9]))[0] == 0.0


def test_outlook_uses_the_five_nearest_wells_and_says_how_far(service):
    out = service.outlook(-100.9, 37.9, 2025)
    assert out["available"] is True and out["wells_used"] == 5
    assert out["distance_mi"] == {"nearest": 1.0, "farthest": 5.0}
    assert out["winters"] == [1996, 2026]
    assert out["decline_ft_per_year"] == {"median": 3.0, "low": 1.0, "high": 5.0}
    assert (out["falling"], out["rising"]) == (5, 0)
    assert [w["well_id"] for w in out["wells"]] == [f"USGS-{i}" for i in range(1, 6)]        # nearest first
    first = out["wells"][0]
    assert first["distance_mi"] == 1.0 and first["decline_ft_per_year"] == 1.0 and first["winters"] == 31
    assert first["points"][0] == [1996, 100.0] and first["points"][-1] == [2026, 130.0] and len(first["points"]) == 31
    assert first["fit"] == [[1996, 100.0], [2026, 130.0]]
    assert first["interval"][0] <= 1.0 <= first["interval"][1]
    # columns: 300 ft wells, water at 130..250 ft -> 170..50 ft; in 2040 14 years x rate less; in 2060 34 years x rate
    assert out["column"]["wells"] == 5 and out["column"]["now_ft"] == {"median": 110, "low": 50, "high": 170}
    by_year = {entry["year"]: entry for entry in out["column"]["years"]}
    assert sorted(by_year) == [2040, 2060]
    assert by_year[2040]["left_ft"] == {"median": 68, "low": 0, "high": 156} and by_year[2040]["at_bottom"] == 1
    assert by_year[2060]["left_ft"] == {"median": 8, "low": 0, "high": 136} and by_year[2060]["at_bottom"] == 2
    assert out["sentence"] == ("The 5 nearest wells with a long record (1 to 5 miles from this field) have been dropping "
                               "about 3 feet a year in the middle well, winters 1996 to 2026; the slowest about 1 and "
                               "the fastest about 5.")
    assert "110 feet of water today" in out["extrapolation"] and "about 68 feet in 2040 (0 to 156)" in out["extrapolation"]
    assert "simply continued" in out["extrapolation"] and "not a forecast" in out["caveat"]
    assert out["threshold"] is None and "no cut-off" in out["threshold_note"]


def test_the_chosen_future_year_is_added_to_the_years(service):
    years = [entry["year"] for entry in service.outlook(-100.9, 37.9, 2050)["column"]["years"]]
    assert years == [2040, 2050, 2060]
    assert [e["year"] for e in service.outlook(-100.9, 37.9, 2040)["column"]["years"]] == [2040, 2060]


def test_no_outlook_when_too_few_wells_are_near(service):
    far = service.outlook(-99.0, 37.9, 2025)                   # about 100 miles east
    assert far["available"] is False and far["reason"] == "no_wells_nearby" and "10 miles" in far["note"]
    # two wells within ten miles are not enough: 8.5 miles south of the nearest well
    near_two = service.outlook(-100.9, 37.9 - 8.5 / 69.05, 2025)
    assert near_two["available"] is False


def test_service_without_a_file_or_with_a_broken_one(tmp_path, caplog):
    assert api_gw.GroundwaterService(None).outlook(-100.9, 37.9)["reason"] == "not_built"
    assert api_gw.GroundwaterService(tmp_path / "none.parquet").county_body() == {
        "available": False, "reason": "not_built", "source": schema.SOURCE}
    bad = tmp_path / "bad.parquet"
    five_wells().drop(columns=["depth_ft"]).to_parquet(bad, index=False)
    broken = api_gw.GroundwaterService(bad)
    assert broken.county_body()["available"] is False and broken.state() is None
    assert "groundwater table ignored" in caplog.text


def test_the_table_is_read_again_when_the_file_changes(tmp_path):
    path = tmp_path / "groundwater.parquet"
    five_wells().to_parquet(path, index=False)
    service = api_gw.GroundwaterService(path)
    assert service.county_body()["summary"]["wells"] == 6
    table(well_rows("only", -100.9, 37.9, rate=1.5)).to_parquet(path, index=False)
    import os

    os.utime(path, ns=(1, 1))
    assert service.county_body()["summary"]["wells"] == 1


def test_county_body_sentences_come_from_its_own_numbers(service):
    body = service.county_body()
    s = body["summary"]
    assert body["available"] is True and s["wells"] == 6 and s["winters"] == [1996, 2026]
    assert s["decline_ft_per_year"]["median"] == 3.5 and s["falling"] == 6 and s["rising"] == 0
    assert f"{s['wells']} wells" in body["sentence"] and "dropping about 3.5 feet a year" in body["sentence"]
    assert f"{s['falling']} of {s['wells']} are falling" in body["sentence"]
    assert [entry["year"] for entry in s["column"]["years"]] == [2040, 2060]
    assert f"about {s['column']['years'][0]['left_ft']['median']} feet in 2040" in body["extrapolation"]
    assert body["threshold"] is None and len(body["wells"]) == 6 and all(w["in_county"] for w in body["wells"])
    assert body["table"]["wells"] == 6 and body["table"]["from_kgs"] == 6 * 31 and body["rule"]["min_winters"] == 10
    json.dumps(body, allow_nan=False)


@pytest.mark.parametrize("rate, words", [
    (2.64, "dropping about 2.6 feet a year"), (1.0, "dropping about 1 foot a year"),
    (-0.31, "rising about 0.3 feet a year"), (0.04, "holding about steady"), (-0.02, "holding about steady"),
])
def test_rate_words(rate, words):
    assert api_gw.rate_words(rate) == words


def test_plan_line_ties_the_pumping_to_the_measured_rate():
    outlook = {"available": True, "decline_ft_per_year": {"median": 2.6, "low": 1.0, "high": 4.0}}
    assert api_gw.plan_line(20.9, outlook) == ("This plan pumps about 21 inches a year; wells near this field have "
                                               "been dropping about 2.6 feet a year.")
    assert api_gw.plan_line(0.2, outlook).startswith("This plan needs almost no pumped water; wells near")
    # halves go up (the card beside it shows 10.5), and one inch is singular
    assert api_gw.plan_line(10.5, outlook).startswith("This plan pumps about 11 inches a year;")
    assert api_gw.plan_line(1.2, outlook).startswith("This plan pumps about 1 inch a year;")
    steady = {"available": True, "decline_ft_per_year": {"median": 0.0, "low": -0.1, "high": 0.1}}
    assert api_gw.plan_line(12.0, steady).endswith("wells near this field have held about steady.")
    assert api_gw.plan_line(12.0, {"available": False}) is None and api_gw.plan_line(None, outlook) is None


# --- the engines by pumping limit ----------------------------------------------------------------------

def test_an_engine_for_another_limit_is_built_once_and_kept(engine):
    assert engine.with_limit(engine.irrigation_limit_mm) is engine
    twelve = engine.with_limit(12 * 25.4)
    assert twelve is not engine and twelve.irrigation_limit_mm == pytest.approx(304.8)
    assert engine.with_limit(12 * 25.4) is twelve
    assert engine.irrigation_limit_mm == pytest.approx(609.6)                       # the default engine is untouched
    for inches in range(2, 2 + smart.LIMIT_ENGINES_KEPT):                           # eight more push the first one out
        engine.with_limit(inches * 25.4 + 0.3)
    assert len(engine._limited) == smart.LIMIT_ENGINES_KEPT
    assert engine.with_limit(12 * 25.4) is not twelve
    for bad in (0, -5, float("nan"), True, "12"):
        with pytest.raises(ValueError):
            engine.with_limit(bad)
    assert engine.what_if_range_in() == (1.0, 24.0)


def test_a_lower_limit_changes_irrigated_plans_and_leaves_dryland_ones(engine):
    wet = a_field(IRRIGATED_HISTORY)
    usual = smart.rank_plans_smart(wet, irrigated=True, engine=engine).to_dict()
    tight = smart.rank_plans_smart(wet, irrigated=True, engine=engine.with_limit(8 * 25.4)).to_dict()
    assert usual["settings"]["irrigation_limit_in"] == 24.0 and tight["settings"]["irrigation_limit_in"] == 8.0
    assert all(plan["numbers"]["irrigation_in"] <= 8.0 + 1e-6 for plan in tight["plans"])
    assert max(p["numbers"]["irrigation_in"] for p in usual["plans"]) > 8.0
    assert tight["plans"] != usual["plans"]
    assert "up to 8 inches" in tight["settings"]["limits"][1]
    dry = a_field()
    assert (smart.rank_plans_smart(dry, irrigated=False, engine=engine).to_dict()["plans"]
            == smart.rank_plans_smart(dry, irrigated=False, engine=engine.with_limit(8 * 25.4)).to_dict()["plans"])


# --- the API -----------------------------------------------------------------------------------------

def centre_of(client, field_id):
    ring = client.get(f"/field/{field_id}").json()["geometry"]["coordinates"][0]
    xs, ys = [p[0] for p in ring[:-1]], [p[1] for p in ring[:-1]]
    return sum(xs) / len(xs), sum(ys) / len(ys)


@pytest.fixture()
def water_client(tables, tmp_path, monkeypatch):
    """The made-up planner tables plus made-up wells around the irrigated test field."""
    from fastapi.testclient import TestClient

    import api.main as main

    path = tmp_path / "groundwater.parquet"
    monkeypatch.setenv("GROUNDWATER_PATH", str(path))
    with TestClient(main.create_app()) as c:
        lon, lat = centre_of(c, WET)
        five_wells(lon, lat).to_parquet(path, index=False)
        yield c


def test_groundwater_endpoint(water_client):
    body = water_client.get("/groundwater").json()
    assert body["available"] is True and body["summary"]["wells"] == 6 and body["sentence"]


def test_groundwater_endpoint_without_the_table(client):
    # no table next to these fields: the endpoint says so, and everything else works
    assert client.get("/groundwater").json() == {"available": False, "reason": "not_built", "source": schema.SOURCE}
    assert client.get("/health").status_code == 200


def test_irrigated_plans_carry_the_water_outlook_and_dryland_ones_do_not(water_client):
    wet = water_client.get(f"/field/{WET}/plans").json()
    assert wet["settings"]["irrigated"] is True
    out = wet["water_outlook"]
    assert out["available"] is True and out["wells_used"] == 5 and out["decline_ft_per_year"]["median"] == 3.0
    assert out["distance_mi"]["farthest"] == pytest.approx(5.0, abs=0.2)
    assert out["what_if"] == {"limit_in": 24.0, "default_in": 24.0, "active": False, "min_in": 1.0, "max_in": 24.0,
                              "note": out["what_if"]["note"]}
    assert len(out["plan_lines"]) == len(wet["plans"]) == 3
    for plan, line in zip([*wet["plans"], wet["current"]], [*out["plan_lines"], out["current_line"]]):
        inches = plan["numbers"]["irrigation_in"]
        assert line == api_gw.plan_line(inches, out)
        assert f"about {round(inches)} inches a year" in line and "dropping about 3 feet a year" in line
        assert "water_line" not in plan                       # the plans themselves are as the planner made them
    dry = water_client.get(f"/field/{DRY}/plans").json()
    assert dry["settings"]["irrigated"] is False and "water_outlook" not in dry
    # the farmer says the dry field is irrigated: it gets an outlook (its own nearest wells, or the reason it has none)
    forced = water_client.get(f"/field/{DRY}/plans", params={"irrigated": "true"}).json()
    assert "water_outlook" in forced and "what_if" in forced["water_outlook"]


def test_without_the_table_irrigated_plans_still_answer_and_offer_the_what_if(client):
    body = client.get(f"/field/{WET}/plans").json()
    assert body["engine"] == "smart" and len(body["plans"]) == 3
    assert body["water_outlook"]["available"] is False and body["water_outlook"]["reason"] == "not_built"
    assert body["water_outlook"]["what_if"]["default_in"] == 24.0
    assert body["water_outlook"]["plan_lines"] == [None, None, None] and body["water_outlook"]["current_line"] is None


def test_limit_in_rescores_the_plans_and_is_echoed(water_client):
    usual = water_client.get(f"/field/{WET}/plans").json()
    tight = water_client.get(f"/field/{WET}/plans", params={"limit_in": 8}).json()
    assert tight["settings"]["irrigation_limit_in"] == 8.0 and tight["settings"]["irrigation_limit_mm"] == 203
    assert tight["water_outlook"]["what_if"]["active"] is True and tight["water_outlook"]["what_if"]["limit_in"] == 8.0
    assert tight["water_outlook"]["what_if"]["default_in"] == 24.0
    assert all(plan["numbers"]["irrigation_in"] <= 8.0 + 1e-6 for plan in tight["plans"])
    assert [p["numbers"] for p in tight["plans"]] != [p["numbers"] for p in usual["plans"]]
    # the wells do not change with the what-if, and asking for the usual limit is the usual answer
    assert tight["water_outlook"]["decline_ft_per_year"] == usual["water_outlook"]["decline_ft_per_year"]
    assert water_client.get(f"/field/{WET}/plans", params={"limit_in": 24}).json() == usual
    assert water_client.get(f"/field/{WET}/plans").json() == usual                  # and it did not stick


@pytest.mark.parametrize("value", ["0", "0.5", "25", "-3", "nan", "inf"])
def test_a_limit_outside_the_range_is_400_with_one_plain_sentence(water_client, value):
    for url, more in ((f"/field/{WET}/plans", {}), (f"/field/{WET}/score", {"rotation": "corn,soybeans"}),
                      (f"/field/{WET}/transition", {"plan": 1})):
        response = water_client.get(url, params={"limit_in": value, **more})
        assert response.status_code == 400, url
        assert response.json() == {"detail": "The pumping limit must be between 1 and 24 inches an acre a year."}


def test_score_transition_and_farm_take_the_limit(water_client):
    score = water_client.get(f"/field/{WET}/score", params={"rotation": "corn,corn,soybeans", "limit_in": 8}).json()
    assert score["settings"]["irrigation_limit_in"] == 8.0 and score["yours"]["numbers"]["irrigation_in"] <= 8.0 + 1e-6
    usual = water_client.get(f"/field/{WET}/score", params={"rotation": "corn,corn,soybeans"}).json()
    assert usual["settings"]["irrigation_limit_in"] == 24.0 and usual["yours"]["numbers"]["irrigation_in"] > 8.0
    path = water_client.get(f"/field/{WET}/transition", params={"plan": 1, "limit_in": 8}).json()
    assert path["settings"]["irrigation_limit_in"] == 8.0
    plans = water_client.get(f"/field/{WET}/plans", params={"limit_in": 8}).json()
    assert path["target"]["cycle"] == plans["plans"][0]["cycle"]                    # the same first pick, under the same limit
    farm = water_client.post("/farm/plan", json={"field_ids": [WET, DRY], "limit_in": 8}).json()
    assert farm["settings"]["irrigation_limit_in"] == 8.0
    assert any("more than 8 inches" in note for note in farm["notes"])
    again = water_client.post("/farm/plan", json={"field_ids": [WET, DRY]}).json()
    assert again["settings"]["irrigation_limit_in"] == 24.0                         # the two answers are kept apart
    for bad in ("8", True, 40):
        response = water_client.post("/farm/plan", json={"field_ids": [WET], "limit_in": bad})
        assert response.status_code == 400 and "pumping limit" in response.json()["detail"]


def test_a_failing_outlook_never_costs_the_plans(water_client, monkeypatch, caplog):
    def boom(self, *args, **kwargs):
        raise RuntimeError("wells on fire")

    monkeypatch.setattr(api_gw.GroundwaterService, "outlook", boom)
    body = water_client.get(f"/field/{WET}/plans").json()
    assert body["engine"] == "smart" and len(body["plans"]) == 3 and "water_outlook" not in body
    assert "water outlook failed" in caplog.text


# --- the real table (skips without data/) --------------------------------------------------------------

@pytest.mark.skipif(not REAL.exists(), reason="data/groundwater.parquet has not been built (make groundwater)")
def test_the_real_table_is_valid_and_gives_a_county_picture():
    df = schema.load_groundwater(REAL)
    assert schema.check_groundwater(df) == []
    body = api_gw.GroundwaterService(REAL).county_body()
    s = body["summary"]
    assert body["available"] is True and s["wells"] >= 20
    assert s["winters"][0] >= gw.TREND_FROM and s["winters"][1] >= 2024             # carried up to date by the KGS rows
    d = s["decline_ft_per_year"]
    assert d["min"] <= d["p25"] <= d["median"] <= d["p75"] <= d["max"]
    assert 0 <= s["falling"] <= s["wells"] and 0 <= s["rising"] <= s["wells"] - s["falling"]
    assert s["column"]["wells"] <= s["wells"]
    years = s["column"]["years"]
    assert [e["year"] for e in years] == [2040, 2060]
    assert years[1]["left_ft"]["median"] <= years[0]["left_ft"]["median"] <= s["column"]["now_ft"]["median"] or d["median"] < 0
    json.dumps(body, allow_nan=False)
