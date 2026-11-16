"""Weather replay (Phase 5, item B): api/planner/replay.py and GET /field/{id}/replay.

The arithmetic runs on made-up observed tables with one drought year in them, so these tests always run, with or
without data/. The last tests use the real tables and skip when data/ is missing. Nothing here needs the network.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from api.planner import money, replay, smart, soil_effect  # noqa: E402
from api.planner.crops import CROPS, PRACTICES  # noqa: E402
from api.planner.legacy import FieldInput  # noqa: E402
from api.planner.response import crop_response  # noqa: E402

import test_api_smart as api_tests  # noqa: E402
from test_api_smart import (  # noqa: E402,F401  (fixtures)
    basic_client, clean_engine, client, fields_file, no_tables, tables,
)
from test_planner_smart import B, C, F, IRRIGATED_HISTORY, S, W, a_field, climate_tables  # noqa: E402

DRY, WET = api_tests.DRY, api_tests.WET
DROUGHT = 2012
YEARS = list(range(2000, 2025))


def observed_tables(drought=DROUGHT, years=YEARS):
    """(yearly, monthly, history): made-up model tables for 2000-2060, with observed rows for `years` whose
    weather differs from year to year, and one drought year (a fifth of the rain, many hot days)."""
    yearly, monthly = climate_tables(years=range(2000, 2061))
    base = monthly[(monthly["model"] == "mid") & (monthly["scenario"] == "ssp245")]
    rows = []
    for year in [1999, *years]:
        part = base[base["year"] == year].copy()
        wobble = 0.85 + 0.03 * ((year * 7) % 11)                   # 0.85 .. 1.15, a different number each year
        part["rain_mm"] = part["rain_mm"] * (0.2 if year == drought else wobble)
        rows.append(part.assign(model="observed", scenario="observed"))
    monthly = pd.concat([monthly, *rows], ignore_index=True)
    monthly = monthly.assign(tmax_c=25.0, tmin_c=10.0, tmean_c=17.5, wind_ms=4.0, hot_days=1.0)
    mid = yearly[(yearly["model"] == "mid") & (yearly["scenario"] == "ssp245") & yearly["year"].isin(years)]
    history = mid.drop(columns=["model", "scenario"]).reset_index(drop=True)
    history = history.assign(gdd=2000.0, wind_ms=4.0, wheat_et0_mm=500.0)
    wobbles = np.array([0.85 + 0.03 * ((y * 7) % 11) for y in history["year"]])
    history["rain_mm"] = np.where(history["year"] == drought, history["rain_mm"] * 0.2, history["rain_mm"] * wobbles)
    history["hot_days"] = np.where(history["year"] == drought, 70.0, 40.0)
    history["wheat_hot_days"] = np.where(history["year"] == drought, 30.0, 20.0)
    return yearly, monthly, history


@pytest.fixture(scope="module")
def made_up():
    yearly, monthly, history = observed_tables()
    engine = smart.Engine(yearly, monthly, economics=money.builtin_economics(),
                          soil=soil_effect.CropAverageProvider(soil_effect.builtin_table()))
    return engine, history


def run(made_up, field=None, **kwargs):
    engine, history = made_up
    kwargs.setdefault("irrigated", False)
    return replay.replay(field or a_field(history={str(2008 + i): crop for i, crop in enumerate(([W, S, F] * 6)[:17])}),
                         engine=engine, history=history, **kwargs)


def field_ending(crops, last_year=2024):
    first = last_year - len(crops) + 1
    return FieldInput("KS-000001", {str(first + i): crop for i, crop in enumerate(crops)}, 1.8, 1.0, 60.0)


# --- the rules we invented: dry years, phases ---------------------------------------------------------------

def test_the_dry_years_are_the_driest_fifth_whatever_their_names():
    years = list(range(2000, 2025))
    rain = [400.0] * 25
    for year, value in {2003: 150, 2008: 200, 2010: 210, 2017: 100, 2021: 180}.items():
        rain[year - 2000] = value
    assert replay.dry_year_set(years, rain) == [2003, 2008, 2010, 2017, 2021]
    # short records still mark one year; ties at the edge go to the earlier year
    assert replay.dry_year_set([2001, 2002, 2003], [5.0, 3.0, 3.0]) == [2002]
    assert replay.dry_year_set(list(range(10)), [1.0] * 10) == [0, 1]


@pytest.mark.parametrize("n, first, anchor", [(3, 2000, 2025), (4, 2000, 2021), (2, 2001, 2002), (1, 2000, 2030)])
def test_the_shown_phase_puts_the_cycles_first_crop_in_the_anchor_year(n, first, anchor):
    p = replay.shown_phase(n, first, anchor)
    assert 0 <= p < n
    assert (anchor - first + p) % n == 0
    assert replay.shown_phase(n, first, None) is None


# --- the numbers are the planner's own ------------------------------------------------------------------------

def test_a_years_return_is_the_crop_models_own_number(made_up):
    """Continuous irrigated corn, year by year, from crop_response on the observed rows directly, with the money
    arithmetic written out: the replay adds no model of its own."""
    engine, history = made_up
    body = run(made_up, field_ending([C, C, C, C]), rotation=[C, C], irrigated=True)
    monthly = engine._tables[1]
    obs = monthly[(monthly["model"] == "observed") & (monthly["scenario"] == "observed")]
    table = crop_response(history.assign(model="observed", scenario="observed"), obs, engine.params,
                          irrigation_limit_mm=engine.irrigation_limit_mm)
    corn = table[(table["crop"] == C) & (table["practice"] == "irrigated")].set_index("year")
    normal = engine.params["series"][C]["irrigated"]["normal_yield"]
    for row in body["plan"]["years"]:
        y = corn.loc[row["year"]]
        expect = (engine.economics.price(C) * y["yield_after_summer"] - engine.economics.cost(C, "irrigated", normal)
                  - engine.economics.pump_per_mm * y["irrigation_mm_after_summer"])
        assert abs(row["return"] - expect) <= 1.0, row["year"]
        assert abs(row["yield_pct"] - 100 * y["yield_rel_after_summer"]) <= 1.0
        assert row["failed"] == bool(y["yield_rel_after_summer"] < engine.params["fail_below"])


def test_the_drought_year_is_marked_by_the_rule_and_pulls_a_dryland_crop_down(made_up):
    body = run(made_up, field_ending([S, S, S, S]), rotation=[S, S])
    rows = {r["year"]: r for r in body["plan"]["years"]}
    assert body["weather"]["dry_years"] == [y for y in YEARS if rows[y]["dry"]]
    assert DROUGHT in body["weather"]["dry_years"] and len(body["weather"]["dry_years"]) == 5
    assert rows[DROUGHT]["return"] == min(r["return"] for r in rows.values())
    assert rows[DROUGHT]["yield_pct"] < 0.75 * np.median([r["yield_pct"] for r in rows.values()])
    assert rows[DROUGHT]["worst_rank"] == 1 and body["plan"]["worst_years"][0] == DROUGHT
    assert sum(1 for r in rows.values() if r["worst_rank"]) == replay.WORST_MARKED


def test_the_drought_is_found_in_whichever_year_it_falls():
    yearly, monthly, history = observed_tables(drought=2005)
    engine = smart.Engine(yearly, monthly, economics=money.builtin_economics(),
                          soil=soil_effect.CropAverageProvider(soil_effect.builtin_table()))
    body = replay.replay(field_ending([S, S, S, S]), rotation=[S, S], irrigated=False, engine=engine, history=history)
    assert 2005 in body["weather"]["dry_years"] and 2012 not in body["weather"]["dry_years"]
    assert body["plan"]["summary"]["worst_year"]["year"] == 2005


def test_the_summary_is_the_rows_own_arithmetic(made_up):
    body = run(made_up, plan=1)
    p = body["plan"]
    returns = np.array([r["return"] for r in p["years"]])
    s = p["summary"]
    assert len(returns) == 25 and s["years"] == 25
    assert s["worst_year"]["return"] == returns.min() and p["years"][int(np.argmin(returns))]["year"] == s["worst_year"]["year"]
    assert s["best_year"]["return"] == returns.max()
    assert s["median_return"] == int(np.floor(np.median(returns) + 0.5))
    assert s["below_cost_years"] == int((returns < 0).sum()) == sum(r["below_cost"] for r in p["years"])
    assert s["below_cost_share"] == round(s["below_cost_years"] / 25, 3)
    planted = [r for r in p["years"] if r["crop"] != F]
    assert s["planted_years"] == len(planted) and s["failed_years"] == sum(bool(r["failed"]) for r in planted)
    dry = np.array([r["dry"] for r in p["years"]])
    assert s["dry"]["mean_return"] == int(np.floor(returns[dry].mean() + 0.5))
    assert s["dry"]["other_mean_return"] == int(np.floor(returns[~dry].mean() + 0.5))
    for r in p["years"]:
        assert r["low"] <= r["return"] <= r["high"] and r["return"] in r["returns_by_phase"]


def test_the_cycle_runs_in_every_phase_and_one_phase_matches_the_field(made_up):
    # a field whose history ends in 2024: the plan's first crop is 2025's, so 2024 holds the cycle's last crop
    body = run(made_up, field_ending([W, S, F, W, S, F]), rotation=[W, S, F])
    p = body["plan"]
    rows = {r["year"]: r for r in p["years"]}
    assert p["phases"] == 3 and p["phase"]["known"] and p["phase"]["first_year"] == 2025
    assert [rows[2024]["crop"], rows[2023]["crop"], rows[2022]["crop"]] == [F, S, W]
    # every year the three phases hold the three crops, one each
    assert all(sorted(r["crops_by_phase"]) == sorted([W, S, F]) for r in rows.values())
    # an older field: history ends in 2020, so 2021 is the first crop and 2020 the last
    old = run(made_up, field_ending([W, S, F, W, S, F], last_year=2020), rotation=[W, S, F])
    old_rows = {r["year"]: r for r in old["plan"]["years"]}
    assert old["plan"]["phase"]["first_year"] == 2021
    assert [old_rows[2020]["crop"], old_rows[2019]["crop"], old_rows[2021 - 3]["crop"]] == [F, S, W]
    assert [old_rows[2021 - 4]["crop"]] == [F]


def test_without_a_crop_history_the_bars_are_the_average_of_the_phases(made_up):
    nobody = FieldInput("KS-000001", {}, 1.8, 1.0, 60.0)
    body = run(made_up, nobody, rotation=[W, S, F])
    p = body["plan"]
    assert not p["phase"]["known"] and p["basis"] == "all_starts" and p["phase"]["shown"] is None
    for r in p["years"]:
        assert r["crop"] is None and r["failed"] is None
        assert r["return"] == int(np.floor(np.mean(r["returns_by_phase"]) + 0.5))
    pooled = np.array([v for r in p["years"] for v in r["returns_by_phase"]])
    assert p["summary"]["crop_years"] == 75 and p["summary"]["worst_year"]["return"] == pooled.min()
    assert p["summary"]["median_return"] == int(np.floor(np.median(pooled) + 0.5))
    assert body["carry_on"] is None and body["comparison"] is None
    assert any("No crop history" in n for n in body["notes"])


def test_the_pooled_summary_is_kept_beside_the_shown_phase(made_up):
    p = run(made_up, field_ending([W, S, F, W, S, F]), rotation=[W, S, F])["plan"]
    pooled = np.array([v for r in p["years"] for v in r["returns_by_phase"]])
    a = p["all_starts"]
    assert a["crop_years"] == 75 and a["worst_year"]["return"] == pooled.min()
    assert a["below_cost_years"] == int((pooled < 0).sum())
    assert p["summary"]["crop_years"] == 25 and p["basis"] == "this_start"


# --- fallow, water, failure ----------------------------------------------------------------------------------

def test_a_fallow_year_has_no_yield_no_failure_and_only_its_cost(made_up):
    body = run(made_up, field_ending([W, S, F, W, S, F]), rotation=[W, S, F])
    fallow = [r for r in body["plan"]["years"] if r["crop"] == F]
    assert fallow and all(r["yield_pct"] is None and r["failed"] is False and r["water_met"] is None for r in fallow)
    assert all(r["return"] < 0 for r in fallow)         # direct costs and no crop
    assert len({r["return"] for r in fallow}) == 1      # the same every fallow year, whatever the weather


def test_irrigated_water_stays_within_the_limit_and_dryland_has_none_pumped(made_up):
    engine, history = made_up
    wet = run(made_up, field_ending([C, C, B, C]), rotation=[C, B], irrigated=True)
    limit = wet["settings"]["irrigation_limit_in"]
    assert all(r["water_in"] <= limit + 0.05 for r in wet["plan"]["years"])
    assert any(r["water_met"] < 1.0 for r in wet["plan"]["years"] if r["year"] == DROUGHT)
    dry = run(made_up, rotation=[W, S, F], irrigated=False)
    assert all(r["water_in"] is None for r in dry["plan"]["years"])
    assert all(0.0 <= r["water_met"] <= 1.0 for r in dry["plan"]["years"] if r["water_met"] is not None)


def test_a_lower_pumping_limit_lowers_a_dry_years_return(made_up):
    engine, history = made_up
    base = replay.replay(field_ending([C, C, B, C]), rotation=[C, B], irrigated=True, engine=engine, history=history)
    tight = replay.replay(field_ending([C, C, B, C]), rotation=[C, B], irrigated=True,
                          engine=engine.with_limit(8 * smart.MM_PER_INCH), history=history)
    a = {r["year"]: r for r in base["plan"]["years"]}
    b = {r["year"]: r for r in tight["plan"]["years"]}
    assert b[DROUGHT]["return"] < a[DROUGHT]["return"] and b[DROUGHT]["water_in"] <= 8.05
    assert tight["settings"]["irrigation_limit_in"] == 8.0


# --- carrying on as now ------------------------------------------------------------------------------------

def test_the_comparison_is_year_by_year_in_the_same_weather(made_up):
    body = run(made_up, field_ending([S, S, S, S]), rotation=[W, S, F])
    carry, cmp_ = body["carry_on"], body["comparison"]
    assert carry["name"] == "Continuous sorghum every year" or "sorghum" in carry["name"].lower()
    plan_r = np.array([r["return"] for r in body["plan"]["years"]])
    carry_r = np.array([r["return"] for r in carry["years"]])
    assert cmp_["difference"] == list(plan_r - carry_r) and cmp_["carry_return"] == list(carry_r)
    assert cmp_["years_ahead"] == int((plan_r > carry_r).sum()) and cmp_["years_behind"] == int((plan_r < carry_r).sum())
    assert cmp_["years_ahead"] + cmp_["years_behind"] + cmp_["years_same"] == cmp_["total"] == 25
    assert cmp_["mean_difference"] == int(np.floor((plan_r - carry_r).mean() + 0.5))
    assert cmp_["worst_year"] == {"plan": int(plan_r.min()), "carry_on": int(carry_r.min())}
    assert len(cmp_["sentences"]) == 2


def test_a_field_already_in_the_rotation_is_compared_with_itself(made_up):
    body = run(made_up, field_ending([W, S, F, W, S, F]), rotation=[W, S, F])
    assert set(body["comparison"]["difference"]) == {0} and body["comparison"]["years_same"] == 25
    assert "same" in body["comparison"]["sentences"][0]


def test_no_carry_on_without_four_years_of_the_six_crops(made_up):
    body = run(made_up, field_ending([W, S]), rotation=[C, B])
    assert body["carry_on"] is None and body["comparison"] is None
    assert any("carry on as now" in n for n in body["notes"])


# --- the words -----------------------------------------------------------------------------------------------

def test_the_sentences_say_return_over_direct_costs_and_never_profit_or_soil_health(made_up):
    body = run(made_up, field_ending([S, S, S, S]), rotation=[W, S, F])
    text = " ".join(body["plan"]["sentences"] + body["comparison"]["sentences"] + body["notes"]
                    + body["not_modelled"] + [body["weather"]["dry_rule"]]).lower()
    assert "direct costs" in text and "not a forecast" in text and "today's" in text
    assert "profit" not in text.replace("not profit", "") and "soil health" not in text \
        and not re.search(r"\bearns?\b", text)
    assert str(DROUGHT) in " ".join(body["plan"]["sentences"])
    assert body["plan"]["sentences"][0].startswith("In the real weather of 2000-2024")


def test_the_crop_model_error_is_carried_for_each_crop_grown(made_up):
    body = run(made_up, rotation=[W, S, F])
    errors = {e["crop"]: e for e in body["plan"]["model_error"]}
    assert set(errors) == {W, S} and all(e["typical_miss_pct"] is None or e["typical_miss_pct"] >= 0 for e in errors.values())


def test_a_plan_or_a_rotation_not_both_and_a_plan_number_in_range(made_up):
    with pytest.raises(replay.ReplayError):
        run(made_up, plan=1, rotation=[W, S])
    with pytest.raises(replay.ReplayError):
        run(made_up, plan=4)
    with pytest.raises(ValueError):
        run(made_up, rotation=[W, "triticale"])


def test_a_plan_is_replayed_as_the_card_shows_it(made_up):
    engine, _ = made_up
    body = run(made_up, plan=2)
    result = smart.rank_plans_smart(a_field(), irrigated=False, engine=engine)
    assert body["subject"]["cycle"] == result.plans[1].cycle and body["subject"]["name"] == result.plans[1].name
    assert body["settings"]["irrigated"] is False and body["plan"]["cycle"] == list(result.plans[1].cycle)


def test_the_tables_are_built_once_per_engine(made_up):
    engine, history = made_up
    one = replay.observed_for(engine, history)
    assert one is not replay.observed_for(engine, history)          # a table handed in is never cached
    assert replay.build_observed(engine, history).dry_years == one.dry_years


def test_missing_observed_rows_are_unavailable_with_one_plain_sentence():
    yearly, monthly = climate_tables(years=range(2000, 2061))          # model rows only
    engine = smart.Engine(yearly, monthly, economics=money.builtin_economics(),
                          soil=soil_effect.CropAverageProvider(soil_effect.builtin_table()))
    history = yearly[(yearly["model"] == "mid") & (yearly["scenario"] == "ssp245")].drop(columns=["model", "scenario"])
    with pytest.raises(replay.ReplayUnavailable) as exc:
        replay.replay(a_field(), engine=engine, history=history)
    assert "observed weather record" in str(exc.value)


# --- the endpoint ------------------------------------------------------------------------------------------------

@pytest.fixture()
def with_observed(tables):
    """The tmp climate folder of `tables`, with a history file and observed rows that differ year to year."""
    yearly, monthly, history = observed_tables()
    yearly = yearly.assign(gdd=2000.0, wind_ms=4.0, wheat_et0_mm=500.0)
    yearly.to_parquet(tables / "climate_futures.parquet", index=False)
    monthly.to_parquet(tables / "climate_monthly.parquet", index=False)
    history.to_parquet(tables / "climate_history.parquet", index=False)
    smart.reset()
    return tables


def replay_of(client, field=DRY, **params):
    return client.get(f"/field/{field}/replay", params=params)


def test_endpoint_answers_with_the_replay_of_our_first_plan_by_default(client, with_observed):
    response = replay_of(client)
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "smart" and body["status"] == "ok" and body["field_id"] == DRY
    assert body["subject"]["source"] == "plan" and body["subject"]["plan"] == 1
    plans = client.get(f"/field/{DRY}/plans").json()["plans"]
    assert body["subject"]["cycle"] == plans[0]["cycle"] and body["subject"]["name"] == plans[0]["name"]
    assert len(body["plan"]["years"]) == 25 and body["weather"]["years"] == 25
    assert body["weather"]["first_year"] == 2000 and body["weather"]["last_year"] == 2024
    assert set(body) >= {"plan", "carry_on", "comparison", "weather", "notes", "not_modelled", "settings", "subject"}


def test_endpoint_takes_plan_numbers_a_typed_rotation_and_the_plans_parameters(client, with_observed):
    second = replay_of(client, plan=2).json()
    plans = client.get(f"/field/{DRY}/plans").json()["plans"]
    assert second["subject"]["plan"] == 2 and second["subject"]["cycle"] == plans[1]["cycle"]
    own = replay_of(client, rotation="Corn, soy", irrigated="true", limit_in=10).json()
    assert own["subject"]["source"] == "own" and own["plan"]["cycle"] == [C, B] and own["settings"]["irrigated"] is True
    assert own["settings"]["irrigation_limit_in"] == 10.0
    assert all(r["water_in"] <= 10.05 for r in own["plan"]["years"])
    weighted = replay_of(client, plan=1, money=5, soil=0, water=0, risk=0).json()
    assert weighted["settings"]["weights"]["money"] == 1.0


@pytest.mark.parametrize("params, said", [
    ({"plan": 1, "rotation": "corn, soy"}, "either a rotation or a plan number"),
    ({"plan": 7}, "plan must be a number from 1 to 3"),
    ({"rotation": "corn, triticale"}, "do not have numbers for"),
    ({"scenario": "rcp9"}, "scenario"),
    ({"horizon": 2099}, "between 2025 and 2060"),
    ({"limit_in": 500}, "pumping limit"),
    ({"money": -1}, "zero or more"),
])
def test_endpoint_answers_400_with_one_plain_sentence(client, with_observed, params, said):
    response = replay_of(client, **params)
    assert response.status_code == 400 and said in response.json()["detail"]


def test_endpoint_unknown_field_is_404(client, with_observed):
    assert client.get("/field/KS-999999/replay").status_code == 404


def test_endpoint_is_503_on_the_basic_planner(basic_client):
    response = basic_client.get(f"/field/{DRY}/replay")
    assert response.status_code == 503 and "climate tables" in response.json()["detail"]


def test_endpoint_is_503_and_plain_when_the_observed_weather_is_missing(client, tables):
    # the `tables` folder has model rows and observed monthly rows but no climate_history.parquet
    assert not (tables / "climate_history.parquet").exists()
    response = replay_of(client)
    assert response.status_code == 503 and "observed weather record" in response.json()["detail"]
    # the plans themselves still answer
    assert client.get(f"/field/{DRY}/plans").status_code == 200


def test_endpoint_is_503_when_the_observed_monthly_rows_are_missing(client, with_observed):
    """The monthly table's own schema wants its observed rows, so the engine is not built at all: the same
    plain 503 as on the basic planner."""
    monthly = pd.read_parquet(with_observed / "climate_monthly.parquet")
    monthly[monthly["model"] != "observed"].to_parquet(with_observed / "climate_monthly.parquet", index=False)
    smart.reset()
    response = replay_of(client)
    assert response.status_code == 503 and "climate tables" in response.json()["detail"]


# --- the real tables ---------------------------------------------------------------------------------------------

REAL = [api_tests.DATA / name for name in ("climate_futures.parquet", "climate_monthly.parquet",
                                            "climate_history.parquet", "fields.parquet")]
needs_data = pytest.mark.skipif(not all(p.is_file() for p in REAL), reason="real climate tables or fields not built")


@needs_data
def test_real_history_marks_the_driest_fifth_by_our_rule_and_the_bars_match_the_plan_logic():
    smart.reset()
    engine = smart.get_engine()
    history = pd.read_parquet(api_tests.DATA / "climate_history.parquet")
    expect = sorted(int(y) for y in history.nsmallest(5, "rain_mm")["year"])
    field = FieldInput("KS-TEST", {str(y): c for y, c in zip(range(2008, 2025), ([C, C, B, C] * 5)[:17])}, 1.8, 1.0, 60.0)
    body = replay.replay(field, plan=1, irrigated=True, engine=engine)
    assert body["weather"]["dry_years"] == expect and body["weather"]["years"] == len(history)
    p = body["plan"]
    assert len(p["years"]) == len(history)
    # the plan card's own long-run figure sits inside the range the weather replay spans
    card = smart.rank_plans_smart(field, irrigated=True, engine=engine).plans[0].numbers["profit_per_acre"]
    pooled = np.array([v for r in p["years"] for v in r["returns_by_phase"]])
    assert pooled.min() - 1 <= card <= pooled.max() + 1
    assert abs(float(pooled.mean()) - card) < 0.35 * max(abs(card), 50.0)       # same machinery, observed weather
