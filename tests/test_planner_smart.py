"""api/planner (Phase 3, piece B2): candidates, futures, soil effect, erosion, money, smart ranking.

The core logic runs on small made-up tables, so these tests always run. The tests at the bottom use
the real tables under data/ and skip when one is missing (a fresh clone has no data/ folder).
No test needs the network and none writes outside pytest's tmp_path.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from api.planner import candidates as cand
from api.planner import config, erosion, futures, money, smart, soil_effect
from api.planner.crops import CROPS, PRACTICES
from api.planner.legacy import FieldInput, Plan
from api.planner.traits import FieldTraits, guess_irrigation, load_traits

ROOT = Path(__file__).resolve().parents[1]
RAIN = [12, 13, 32, 48, 75, 81, 87, 74, 42, 44, 16, 19]            # mm per month, roughly Finney County
ET0 = [44, 57, 101, 144, 180, 221, 235, 206, 169, 110, 64, 42]
W, S, F, C, B, A = "winter wheat", "sorghum", "fallow", "corn", "soybeans", "alfalfa"
MODELS = {"cool": 0.5, "mid": 1.0, "hot": 1.5}      # how fast each made-up model warms


# --- made-up tables ----------------------------------------------------------------------------------

def climate_tables(models=MODELS, years=range(2015, 2061), rain_trend=None):
    """(yearly, monthly) for made-up models that warm steadily; ssp585 warms twice as fast as ssp245.

    `rain_trend`: {model: change of rain per year, as a share}; default no change.
    """
    rain_trend = rain_trend or {}
    yearly, monthly = [], []
    for model, speed in models.items():
        for scenario, factor in (("ssp245", 1.0), ("ssp585", 2.0)):
            for year in [min(years) - 1, *years]:
                t = max(0, year - 2025) * speed * factor            # "warming units" since 2025
                rain_scale = 1.0 + rain_trend.get(model, 0.0) * max(0, year - 2025)
                et_scale = 1.0 + 0.002 * t
                months = (10, 11, 12) if year < min(years) else range(1, 13)
                for month in months:
                    monthly.append({"model": model, "scenario": scenario, "year": year, "month": month,
                                    "rain_mm": RAIN[month - 1] * rain_scale, "et0_mm": ET0[month - 1] * et_scale})
                if year < min(years):
                    continue
                gs_rain = sum(RAIN[3:9]) * rain_scale
                yearly.append({
                    "model": model, "scenario": scenario, "year": year,
                    "tmean_c": 22.0 + 0.04 * t, "rain_mm": gs_rain, "summer_rain_mm": sum(RAIN[5:8]) * rain_scale,
                    "hot_days": 45.0 + 0.8 * t, "dry_spell_days": 20.0 + 0.1 * t, "et0_mm": sum(ET0[3:9]) * et_scale,
                    "wheat_rain_mm": 290.0 * rain_scale, "wheat_hot_days": 21.0 + 0.3 * t, "spring_wind_ms": 5.5,
                    "erosion_climate": 100.0 + 1.0 * t, "annual_rain_mm": sum(RAIN) * rain_scale,
                    "annual_et0_mm": sum(ET0) * et_scale, "annual_tmean_c": 13.0 + 0.04 * t,
                })
    return pd.DataFrame(yearly), pd.DataFrame(monthly)


@pytest.fixture(scope="module")
def engine():
    yearly, monthly = climate_tables()
    return smart.Engine(yearly, monthly, economics=money.builtin_economics(),
                        soil=soil_effect.CropAverageProvider(soil_effect.builtin_table()))


def a_field(history=None, field_id="KS-000001", slope=1.0):
    if history is None:
        history = {str(2008 + i): crop for i, crop in enumerate(([W, S, F] * 6)[:17])}
    return FieldInput(field_id, history, 1.8, slope, 60.0)


IRRIGATED_HISTORY = {str(2008 + i): crop for i, crop in enumerate(([C, C, B, C] * 5)[:17])}


# --- candidates.py -----------------------------------------------------------------------------------

def test_cycle_counts_are_what_the_arithmetic_says():
    assert len(cand.all_cycles(4)) == 336                 # necklaces of 4 beads in 6 colours
    assert len(cand.all_cycles(3)) == 70                  # 76 necklaces of 3, minus the 6 one-crop ones
    assert len(cand.candidates(True)) == 105
    assert len(cand.candidates(False)) == 17
    assert len(set(cand.candidates(True))) == 105 and len(set(cand.candidates(False))) == 17


@pytest.mark.parametrize("key, irrigated, broken, fine", [
    ("no_wheat_after_wheat", True, (W, W, C, S), (W, C, W, S)),
    ("no_wheat_after_wheat", False, (W, S, F, W), (W, S, F, S)),             # the seam counts
    ("no_fallow_after_fallow", False, (F, F, W, S), (F, W, F, S)),
    ("no_soybeans_after_soybeans", True, (B, B, C, C), (B, C, B, C)),
    ("alfalfa_stand", True, (A, A, C, C), (A, A, A, C)),
    ("alfalfa_stand", True, (A, C, A, C), (A, A, A, A)),
    ("dryland_no_alfalfa", False, (A, A, A, A), (S, S, S, S)),
    ("dryland_no_soybeans", False, (B, W, S, F), (S, W, S, F)),
    ("dryland_one_corn_year", False, (C, F, C, F), (C, F, S, F)),            # at most one corn year in a cycle
    ("dryland_one_corn_year", False, (C, C, F), (W, C, F)),                  # so wheat-corn-fallow is allowed
    ("dryland_wheat_after_fallow", False, (W, S, W, S), (W, F, S, F)),       # wheat follows fallow
    ("dryland_wheat_after_fallow", False, (C, W, F), (W, C, F)),
    ("dryland_wheat_after_fallow", False, (W, F, S, S), (W, S, S, F)),       # the seam counts: year 1 follows the last sorghum
    ("no_long_grain_run", True, (C, C, C, C), (C, C, B, B)),                 # no monoculture
    ("no_long_grain_run", False, (S, S, S, S), (S, S, F)),
    ("no_long_grain_run", True, (C, C, C, B), (C, C, B)),
    ("no_long_grain_run", True, (C, C, B, C), (C, B, C, B)),                 # three in a row around the cycle
    ("irrigated_one_fallow_year", True, (C, F, W, F), (C, C, W, F)),
    ("irrigated_one_fallow_year", True, (C, W, F), (C, W, B)),
])
def test_each_rule_is_one_predicate(key, irrigated, broken, fine):
    rule = next(r for r in cand.RULES if r.key == key)
    assert rule.applies_to(irrigated)
    assert not rule.ok(broken) and rule.ok(fine)
    assert rule.text in cand.rules_broken(broken, irrigated)
    assert rule.text not in cand.rules_broken(fine, irrigated)


def test_rules_have_a_sentence_each_and_apply_to_the_right_fields():
    keys = [r["key"] for r in cand.describe_rules()]
    assert len(keys) == len(set(keys)) == 10
    assert all(r["text"].endswith(".") and r["applies"] in ("all", "irrigated", "dryland") for r in cand.describe_rules())
    # every rule can also be said gently to a farmer whose field breaks it
    assert all(r["gentle"].startswith("Our plans avoid ") and r["gentle"].endswith(".") for r in cand.describe_rules())
    assert cand.rules_broken((W, S, W, S), False, gentle=True) == [
        "Our plans avoid winter wheat straight after corn, sorghum or soybeans without irrigation (there is no time "
        "to sow it and no stored water; wheat follows fallow)."]
    assert cand.rules_broken((W, S, W, S), False)[0].startswith("Without irrigation, no winter wheat straight after")
    assert cand.rules_broken((W, S, W, S), True) == []                      # an irrigated field may do it
    assert cand.allowed((A, A, A, C), True) and not cand.allowed((A, A, A, C), False)
    assert cand.allowed((W, F, W, F), False) and not cand.allowed((W, F, W, F), True)


def test_no_candidate_breaks_a_rule():
    for irrigated in (True, False):
        for cycle in cand.candidates(irrigated):
            assert cand.rules_broken(cycle, irrigated) == []
            assert cand.canonical(cycle) == cycle
            assert len(cycle) in (3, 4) and len(cand.show(cycle)) == 4
    dry = cand.candidates(False)
    assert not any(A in c or B in c for c in dry)
    assert all(sum(x == C for x in c) <= 1 for c in dry)
    assert all(sum(x == F for x in c) * 4 <= len(c) for c in cand.candidates(True))
    for c in dry:                                   # dryland wheat only after fallow, year 1 after the last year too
        assert all(c[i - 1] == F for i in range(len(c)) if c[i] == W), c
    for irrigated in (True, False):                 # no monoculture and no three grain years in a row
        for c in cand.candidates(irrigated):
            assert cand.longest_run(c, C) <= 2 and cand.longest_run(c, S) <= 2, c
        assert (C, C, C, C) not in cand.candidates(irrigated) and (S, S, S, S) not in cand.candidates(irrigated)
    assert (W, C, F) in dry and (W, S, F) in dry and (W, F, W, F) in dry and (W, S, W, S) not in dry
    assert (A, A, A, A) in cand.candidates(True) and (C, C, B) in cand.candidates(True)


def test_longest_run_counts_around_the_cycle():
    assert cand.longest_run((C, C, B, C), C) == 3 and cand.longest_run((C, C, B, C), C, cyclic=False) == 2
    assert cand.longest_run((C, B, C, B), C) == 1 and cand.longest_run((S, S, F), S) == 2
    assert cand.longest_run((C, C, C, C), C) > 4 and cand.longest_run((C, C, C, C), C, cyclic=False) == 4
    assert cand.longest_run((W, F, W, F), C) == 0


def test_a_cycle_is_one_plan_whichever_year_it_starts():
    shifts = [(C, W, S, F), (W, S, F, C), (S, F, C, W), (F, C, W, S)]
    assert {cand.canonical(s) for s in shifts} == {(C, W, S, F)}          # starts the year after fallow
    assert cand.canonical((C, A, A, A)) == (A, A, A, C)                   # starts with the stand
    assert cand.canonical((S, W, S, W)) == (W, S, W, S)                   # fixed crop order otherwise


def test_three_year_wheat_sorghum_fallow_is_a_dryland_candidate():
    assert not cand.allowed((W, S, F, W), False)          # as a 4-year cycle: wheat after wheat at the seam
    assert (W, S, F) in cand.candidates(False)
    assert cand.show((W, S, F)) == (W, S, F, W)
    assert cand.matching_candidate((W, S, F, W), False) == (W, S, F)
    assert cand.matching_candidate((S, F, W, S), False) == (W, S, F)                 # both readings fit ...
    assert cand.matching_candidate((S, F, W, S), False, before=W) == (W, S, F)
    assert cand.matching_candidate((S, F, W, S), False, before=S) == (W, S, S, F)    # ... the year before decides
    assert cand.matching_candidate((W, S, W, S), False) is None                      # wheat straight after sorghum
    assert cand.matching_candidate((W, S, W, S), True) == (W, S, W, S)
    assert cand.matching_candidate((C, F, W, C), False) == (W, C, F)                 # wheat-corn-fallow, any start year
    assert cand.matching_candidate((C, C, C, C), False) is None
    assert cand.matching_candidate((C, C, C, C), True) is None                       # a monoculture is not a plan
    assert cand.matching_candidate((W, S, F, "cotton"), False) is None


def test_rules_on_years_as_they_happened_ignore_the_seam():
    assert cand.rules_broken((W, S, F, W), False, cyclic=False) == []
    assert cand.rules_broken((W, W, S, F), False, cyclic=False) != []
    assert cand.rules_broken((C, C, A, A), True, cyclic=False) == []       # the stand may go on after year 4
    assert cand.rules_broken((C, A, A, C), True, cyclic=False) != []       # a two-year stand


def test_years_different():
    assert cand.years_different((W, S, W, S), (S, W, S, W)) == 0
    assert cand.years_different((W, S, W, S), (W, S, S, F)) == 1
    assert cand.years_different((W, S, W, S), (W, F, W, F)) == 2
    assert cand.years_different((C, C, C, C), (A, A, A, A)) == 4
    assert cand.years_different((W, S, F), (W, S, W, S)) == pytest.approx(4 / 3)
    with pytest.raises(ValueError):
        cand.rules_broken((W, S), False)
    with pytest.raises(ValueError):
        cand.rules_broken((W, S, F, "cover crop"), False)


# --- futures.py --------------------------------------------------------------------------------------

@pytest.mark.parametrize("horizon, expected", [(2025, (2015, 2034)), (2026, (2016, 2035)), (2040, (2030, 2049)),
                                               (2055, (2045, 2060)), (2060, (2050, 2060))])
def test_window_rule(horizon, expected):
    assert futures.window(horizon) == expected


@pytest.mark.parametrize("bad", [2024, 2061, "2040", 2040.5, None, True])
def test_bad_horizons_and_scenarios_are_errors(bad):
    with pytest.raises(ValueError):
        futures.check_horizon(bad)
    with pytest.raises(ValueError):
        futures.check_scenario("ssp126")


def test_futures_are_one_per_model_over_the_window():
    yearly, _ = climate_tables()
    climate = futures.Climate(yearly)
    got = climate.futures("ssp585", 2040)
    assert [f.model for f in got] == ["cool", "hot", "mid"]              # alphabetical, fixed
    assert all((f.scenario, f.first_year, f.last_year) == ("ssp585", 2030, 2049) for f in got)
    assert climate.averages("ssp245", 2025)["hot_days"].shape == (3,)


def test_changes_are_each_model_against_its_own_today_then_averaged():
    yearly, _ = climate_tables()
    climate = futures.Climate(yearly)
    by_model = climate.changes_by_model("ssp245", 2060)
    # hand arithmetic for model "mid" (speed 1): t = year - 2025 from 2025 on, 0 before; Today pools the
    # model's two runs (ssp585 warms twice as fast), so it is 1.5 times the ssp245 window mean
    today = 1.5 * np.mean([max(0, y - 2025) for y in range(2015, 2035)])
    then = np.mean([y - 2025 for y in range(2050, 2061)])
    mid = climate.models.index("mid")
    assert by_model["tmean_c"][mid] == pytest.approx(0.04 * (then - today))
    assert by_model["hot_days"][mid] == pytest.approx(0.8 * (then - today))
    changes = climate.changes("ssp245", 2060)
    assert changes["tmean_c"] == pytest.approx(by_model["tmean_c"].mean(), abs=0.006)
    assert changes["range"]["tmean_c"][0] < changes["tmean_c"] < changes["range"]["tmean_c"][1]
    assert changes["summer_rain_pct"] == 0.0 and changes["models"] == 3
    assert changes["summer_rain_agree"] == 3                   # no model changes its rain: they agree on that
    today_changes = climate.changes("ssp585", 2025)
    assert all(today_changes[k] == 0 for k in futures.CHANGES)
    # the high scenario warms more than the middle one
    assert climate.changes("ssp585", 2060)["tmean_c"] > changes["tmean_c"]


def test_today_is_one_climate_for_both_scenarios():
    yearly, _ = climate_tables()
    climate = futures.Climate(yearly)
    a, b = climate.averages("ssp245", 2025), climate.averages("ssp585", 2025)
    assert all(np.array_equal(a[c], b[c]) for c in futures.COLUMNS)
    # per model: its two runs pooled over 2015-2034
    mid = climate.models.index("mid")
    rows = yearly[(yearly["model"] == "mid") & yearly["year"].between(2015, 2034)]
    assert len(rows) == 40 and a["hot_days"][mid] == pytest.approx(rows["hot_days"].mean())
    assert climate.values("ssp245", 2025) == climate.values("ssp585", 2025)
    assert climate.describe("ssp245", 2025)["label"] == climate.describe("ssp585", 2025)["label"] \
        == "Today's climate (model average, 2015-2034)"
    # later windows are the scenario's own, and both are measured against the same Today
    assert (climate.averages("ssp585", 2040)["hot_days"] > climate.averages("ssp245", 2040)["hot_days"]).all()
    d245, d585 = climate.changes_by_model("ssp245", 2040), climate.changes_by_model("ssp585", 2040)
    gap = climate.averages("ssp585", 2040)["hot_days"] - climate.averages("ssp245", 2040)["hot_days"]
    assert d585["hot_days"] - d245["hot_days"] == pytest.approx(gap)


def test_label_rules_for_rain_water_and_hot_days():
    base = {"tmean_c": 1.44, "hot_days": 11.2, "models": 6}
    assert futures.label_for({**base, "summer_rain_pct": -12.0, "summer_rain_agree": 6}, 2040) == \
        "+1.4°C, 12% less summer rain, 11 more days above 35°C (95°F)"
    assert futures.label_for({**base, "summer_rain_pct": 10.4, "summer_rain_agree": 5}, 2040) == \
        "+1.4°C, 10% more summer rain, 11 more days above 35°C (95°F)"
    # the models agree but the change is under 10%: no direction is claimed
    assert "about the same summer rain" in futures.label_for({**base, "summer_rain_pct": -8.0, "summer_rain_agree": 6}, 2040)
    assert "about the same summer rain" in futures.label_for({**base, "summer_rain_pct": 9.9, "summer_rain_agree": 5}, 2040)
    # fewer than 5 of 6 agree on the direction: say so, however big the mean
    for pct, agree in ((-8.0, 4), (-25.0, 3), (0.5, 4)):
        label = futures.label_for({**base, "summer_rain_pct": pct, "summer_rain_agree": agree}, 2040)
        assert "models disagree on summer rain" in label and "less" not in label and "the same" not in label
    # crop water demand, from reference ET, at 2% or more
    assert futures.label_for({**base, "summer_rain_pct": -8.7, "summer_rain_agree": 3, "et0_pct": 8.4, "tmean_c": 1.96,
                              "hot_days": 25.6}, 2060) == \
        "+2.0°C, models disagree on summer rain, crops need 8% more water, 26 more days above 35°C (95°F)"
    assert "crops need" not in futures.label_for({**base, "summer_rain_pct": 0.0, "summer_rain_agree": 6, "et0_pct": 1.9}, 2040)
    assert "crops need 3% less water" in futures.label_for(
        {**base, "summer_rain_pct": 0.0, "summer_rain_agree": 6, "et0_pct": -3.2}, 2040)
    # hot days in both units, singular and plural
    quiet = {**base, "summer_rain_pct": 0.0, "summer_rain_agree": 6}
    assert futures.label_for({**quiet, "hot_days": 0.2}, 2040).endswith("about as many days above 35°C (95°F)")
    assert futures.label_for({**quiet, "hot_days": 1.2}, 2040).endswith("1 more day above 35°C (95°F)")
    assert futures.label_for({**quiet, "hot_days": -3.0}, 2040).endswith("3 fewer days above 35°C (95°F)")
    assert futures.label_for({**base, "summer_rain_pct": -6.0, "summer_rain_agree": 6}, 2025) == \
        "Today's climate (model average, 2015-2034)"
    assert all("\n" not in futures.label_for({**base, "summer_rain_pct": p, "summer_rain_agree": 3, "et0_pct": 9.0}, 2050)
               and len(futures.label_for({**base, "summer_rain_pct": p, "summer_rain_agree": 3, "et0_pct": 9.0}, 2050)) < 110
               for p in (-30.0, 30.0))


def test_rain_agreement_counts_the_models_on_the_side_of_the_mean():
    assert futures.rain_agreement(np.array([3.9, 6.8, -30.8, 10.0, -4.1, -33.2])) == 3      # the real ssp585 2060 case
    assert futures.rain_agreement(np.array([-5.0, -6.0, -7.0, -8.0, -9.0, 1.0])) == 5
    assert futures.rain_agreement(np.array([20.0, 20.0, 20.0, -20.0, -20.0, -20.0])) == 3   # no direction: the larger camp
    assert futures.rain_agreement(np.zeros(6)) == 6                                         # nothing changes: they agree


def test_label_on_tables_where_models_disagree_about_rain():
    agree, _ = climate_tables(rain_trend={"cool": -0.004, "mid": -0.005, "hot": -0.006})
    split, _ = climate_tables(rain_trend={"cool": 0.004, "mid": -0.005, "hot": -0.012})
    assert "less summer rain" in futures.Climate(agree).label("ssp245", 2060)
    label = futures.Climate(split).label("ssp245", 2060)
    assert "models disagree on summer rain" in label and label.startswith("+")
    assert futures.Climate(split).changes("ssp245", 2060)["summer_rain_agree"] == 2
    flat = futures.Climate(climate_tables()[0]).label("ssp585", 2060)                        # no rain change at all
    assert "about the same summer rain" in flat and "crops need" in flat and "more days above 35°C (95°F)" in flat


def test_climate_summary_shape():
    yearly, _ = climate_tables()
    summary = futures.Climate(yearly).summary("made up", {"years": [2000, 2024], "values": {}})
    assert summary["available"] is True and summary["models"] == ["cool", "hot", "mid"]
    assert summary["baseline"]["window"] == [2015, 2034]
    assert set(summary["scenarios"]) == {"ssp245", "ssp585"}
    rows = summary["scenarios"]["ssp585"]
    assert [r["horizon"] for r in rows] == list(range(2025, 2061, 5))
    assert rows[0]["label"] == "Today's climate (model average, 2015-2034)" and rows[-1]["window"] == [2050, 2060]
    assert rows[0] == summary["scenarios"]["ssp245"][0]                      # one Today, whichever scenario
    base = summary["baseline"]
    assert base["label"] == rows[0]["label"] and base["pooled_scenarios"] == ["ssp245", "ssp585"]
    assert base["values"] == base["values_by_scenario"]["ssp245"] == base["values_by_scenario"]["ssp585"]
    assert set(rows[-1]) == {"horizon", "window", "label", "changes", "values", "range"}
    assert rows[-1]["range"]["tmean_c"][0] <= rows[-1]["changes"]["tmean_c"] <= rows[-1]["range"]["tmean_c"][1]
    json.dumps(summary)            # plain JSON all the way down


def test_climate_table_must_be_complete():
    yearly, _ = climate_tables()
    with pytest.raises(ValueError):
        futures.Climate(yearly.iloc[1:])
    with pytest.raises(ValueError):
        futures.Climate(yearly.drop(columns=["erosion_climate"]))


# --- soil_effect.py ----------------------------------------------------------------------------------

def soil_fixture(n_fields=40, years=range(2016, 2025), year_shift=None, seed=1):
    """Made-up fields with known bare days per crop and a known shift per year."""
    rng = np.random.default_rng(seed)
    true = {C: 60.0, W: 40.0, S: 50.0, F: 140.0, A: 5.0, B: 45.0}
    year_shift = year_shift or {y: (20.0 if y == 2023 else -2.5) for y in years}
    histories, irrigation, rows = {}, {}, []
    for i in range(n_fields):
        fid = f"KS-{i:06d}"
        cycle = [(W, S, F), (C, C, B, C), (A, A, A, A), (W, F), (S, W)][i % 5]
        offset = int(rng.integers(0, 4))
        histories[fid] = {str(y): cycle[(y + offset) % len(cycle)] for y in range(2014, 2025)}
        irrigation[fid] = "irrigated" if cycle[0] in (C, A) else "dryland"
        for y in years:
            crop = histories[fid][str(y)]
            rows.append({"field_id": fid, "year": y, "bare_days": true[crop] + year_shift[y] + rng.normal(0, 1.0)})
    return pd.DataFrame(rows), histories, irrigation, true


def test_bare_day_table_removes_year_effects_and_finds_the_crop_levels():
    soil, histories, irrigation, true = soil_fixture()
    table = soil_effect.build_table(soil, histories, irrigation, shrink=0.0)
    assert table.source == "tables"
    assert table.by_crop["dryland"][F] == pytest.approx(true[F], abs=1.5)
    assert table.by_crop["dryland"][W] == pytest.approx(true[W], abs=1.5)
    assert table.by_crop["irrigated"][A] == pytest.approx(true[A], abs=1.5)
    assert table.by_crop["irrigated"][C] == pytest.approx(true[C], abs=1.5)
    shifts = table.info["year_shift_days"]
    assert shifts["2023"] == pytest.approx(20.0, abs=1.0) and shifts["2016"] == pytest.approx(-2.5, abs=1.0)
    for practice in PRACTICES:      # fallow the barest, alfalfa the least bare
        assert max(table.by_crop[practice], key=table.by_crop[practice].get) == F
        assert min(table.by_crop[practice], key=table.by_crop[practice].get) == A


def test_small_cells_are_shrunk_towards_the_crop_mean():
    fields = {f"KS-{i:06d}": {"2019": S, "2020": W} for i in range(20)}
    fields["KS-999999"] = {"2019": F, "2020": W}
    soil = pd.DataFrame([{"field_id": fid, "year": 2020, "bare_days": 100.0 if fid == "KS-999999" else 40.0}
                         for fid in fields])
    irrigation = {fid: "dryland" for fid in fields}
    raw = soil_effect.build_table(soil, fields, irrigation, shrink=0.0)
    assert raw.by_prev["dryland"][W][F] == pytest.approx(100.0)
    shrunk = soil_effect.build_table(soil, fields, irrigation, shrink=3.0)
    crop_mean = (20 * 40 + 100) / 21
    # the crop's own mean is first pulled towards the both-practices mean (the same number here)
    assert shrunk.by_crop["dryland"][W] == pytest.approx(crop_mean, abs=0.06)
    assert shrunk.by_prev["dryland"][W][F] == pytest.approx((1 * 100 + 3 * crop_mean) / 4, abs=0.1)
    assert shrunk.by_prev["dryland"][W][S] == pytest.approx((20 * 40 + 3 * crop_mean) / 23, abs=0.1)
    assert shrunk.n_prev["dryland"][W] == {**{c: 0 for c in CROPS}, F: 1, S: 20}
    assert shrunk.by_prev["dryland"][W][C] == shrunk.by_crop["dryland"][W]        # an empty cell is the crop mean


def test_bare_day_table_skips_nulls_and_unknown_crops_and_guesses_irrigation():
    histories = {"KS-000001": {"2019": "cotton", "2020": C, "2021": C}, "KS-000002": {"2020": W, "2021": F}}
    soil = pd.DataFrame([{"field_id": "KS-000001", "year": 2020, "bare_days": 50.0},
                         {"field_id": "KS-000001", "year": 2021, "bare_days": None},
                         {"field_id": "KS-000001", "year": 2019, "bare_days": 11.0},
                         {"field_id": "KS-000002", "year": 2021, "bare_days": 150.0}])
    table = soil_effect.build_table(soil, histories, irrigation=None, shrink=0.0)
    assert table.info["field_years"] == 2
    assert table.n_crop["irrigated"][C] == 1 and table.n_crop["dryland"][F] == 1
    empty = soil_effect.build_table(soil.iloc[:0], histories)
    assert empty.source == "builtin"


def test_builtin_constants_and_provider_lookup():
    table = soil_effect.builtin_table()
    for practice in PRACTICES:
        assert max(table.by_crop[practice], key=table.by_crop[practice].get) == F
        assert min(table.by_crop[practice], key=table.by_crop[practice].get) == A
    provider = soil_effect.CropAverageProvider(table)
    assert provider.name == "crop_average"
    assert provider.bare_days(F, W, False) == soil_effect.BUILTIN_BY_CROP["dryland"][F]
    assert provider.bare_days(C, None, True) == soil_effect.BUILTIN_BY_CROP["irrigated"][C]
    with pytest.raises(KeyError):
        provider.bare_days("cotton", None, True)
    by_prev = {p: {c: {} for c in CROPS} for p in PRACTICES}
    by_prev["dryland"][S][F] = 120.0
    detailed = soil_effect.CropAverageProvider(soil_effect.BareDayTable(table.by_crop, by_prev))
    assert detailed.bare_days(S, F, False) == 120.0
    assert detailed.bare_days(S, W, False) == table.by_crop["dryland"][S]
    # a cycle: each year after the one before, year 1 after the last; or after a known previous crop
    assert soil_effect.plan_bare_days(detailed, (S, F), False) == pytest.approx((120.0 + table.by_crop["dryland"][F]) / 2)
    assert soil_effect.plan_bare_days(detailed, (S, W), False, first_prev=F) == pytest.approx(
        (120.0 + table.by_crop["dryland"][W]) / 2)


def settings_in(tmp_path, **env):
    names = {"CLIMATE_FUTURES_PATH": "climate_futures.parquet", "CLIMATE_MONTHLY_PATH": "climate_monthly.parquet",
             "CLIMATE_HISTORY_PATH": "climate_history.parquet", "FIELD_TRAITS_PATH": "field_traits.parquet",
             "SOIL_HEALTH_PATH": "soil_health.parquet", "PLANNER_FIELDS_PATH": "fields.parquet",
             "EFFECTS_PATH": "effects.json", "NASS_PATH": "nass.parquet",
             "PLANNER_SOIL_CACHE_PATH": "planner_bare_days.json"}
    values = {var: str(tmp_path / name) for var, name in names.items()}
    values.update(env)
    return config.settings(values)


def test_settings_read_the_environment_in_one_place(tmp_path):
    default = config.settings({})
    assert default.soil_effect_source == "crop_average"
    assert default.climate_futures_path == ROOT / "data" / "climate_futures.parquet"
    assert default.economics_path == ROOT / "api" / "planner" / "economics.json"
    custom = settings_in(tmp_path, SOIL_EFFECT_SOURCE="Effects")
    assert custom.soil_effect_source == "effects" and custom.soil_health_path == tmp_path / "soil_health.parquet"
    with pytest.raises(ValueError):
        config.settings({"SOIL_EFFECT_SOURCE": "magic"})
    assert sum(config.DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)


def test_missing_tables_fall_back_to_builtin_constants(tmp_path):
    settings = settings_in(tmp_path)
    table = soil_effect.load_table(settings, write_cache=True)
    assert table.source == "builtin" and not (tmp_path / "planner_bare_days.json").exists()
    provider = soil_effect.load_provider(settings)
    assert provider.name == "crop_average" and provider.describe()["table"] == "builtin"
    assert provider.bare_days(F, None, False) == soil_effect.BUILTIN_BY_CROP["dryland"][F]
    (tmp_path / "soil_health.parquet").write_text("not a parquet file")
    (tmp_path / "fields.parquet").write_text("not a parquet file")
    assert soil_effect.load_table(settings).source == "builtin"          # unreadable tables too


def test_cache_is_written_on_request_and_reused_until_a_source_changes(tmp_path):
    soil, histories, irrigation, _ = soil_fixture(n_fields=10)
    settings = settings_in(tmp_path)
    soil.to_parquet(settings.soil_health_path)
    pd.DataFrame({"field_id": list(histories), "crop_history": [json.dumps(h) for h in histories.values()]}).to_parquet(
        settings.fields_path)
    pd.DataFrame({"field_id": list(irrigation), "irrigation": list(irrigation.values())}).to_parquet(
        settings.field_traits_path)
    first = soil_effect.load_table(settings)
    assert first.source == "tables" and not settings.soil_cache_path.exists()      # no write unless asked
    built = soil_effect.load_table(settings, write_cache=True)
    assert built.source == "tables" and settings.soil_cache_path.exists()
    cached = soil_effect.load_table(settings)
    assert cached.source == "cache" and cached.by_crop == built.by_crop and cached.by_prev == built.by_prev
    soil.iloc[:-5].to_parquet(settings.soil_health_path)                             # the soil table changed
    assert soil_effect.load_table(settings).source == "tables"
    settings.soil_cache_path.write_text("{ broken")
    assert soil_effect.load_table(settings).source == "tables"


def effects_json(**changes):
    row = {"a": "corn-soy", "b": "continuous corn", "comparison": "corn-soy vs continuous corn",
           "group_kind": "irrigation", "group": "irrigated", "effect": 3.6, "n_pairs": 30,
           "from_crops": {"effect": -6.0, "ci_excludes_zero": False},
           "beyond_crops": {"effect": 9.6, "ci_low": 3.3, "ci_high": 17.3, "ci_excludes_zero": True}}
    row.update(changes.pop("row", {}))
    raw = {"is_fake": False, "outcome": {"column": "bare_days"}, "effects": [row],
           "placebos": {"fake_switch_years": [{"comparison": "corn-soy vs continuous corn", "ci_excludes_zero": False}]}}
    raw.update(changes)
    return raw


def test_effects_provider_shifts_by_the_gap_beyond_crops():
    base = soil_effect.CropAverageProvider(soil_effect.builtin_table())
    provider = soil_effect.EffectsProvider(base, effects_json())
    assert provider.name == "effects"
    assert provider.shifts["irrigated"] == {"continuous corn": -4.8, "corn-soy": 4.8}
    assert provider.shifts["dryland"] == {}
    assert provider.bare_days(C, C, True) == base.bare_days(C, C, True)             # per crop: unchanged
    plain = soil_effect.plan_bare_days(base, (C, B, C, B), True)
    assert soil_effect.plan_bare_days(provider, (C, B, C, B), True) == pytest.approx(plain + 4.8)
    assert soil_effect.plan_bare_days(provider, (C, C, C, C), True) == pytest.approx(
        soil_effect.plan_bare_days(base, (C, C, C, C), True) - 4.8)
    assert soil_effect.plan_bare_days(provider, (C, B, C, B), False) == soil_effect.plan_bare_days(base, (C, B, C, B), False)
    assert soil_effect.plan_bare_days(provider, (A, A, A, C), True) == soil_effect.plan_bare_days(base, (A, A, A, C), True)
    note = provider.describe()
    assert note["adjustments"][0]["gap_days"] == 9.6 and "corn-soy vs continuous corn" in note["note"]


@pytest.mark.parametrize("changes, why", [
    ({"is_fake": True}, "made-up soil"),
    ({"outcome": {"column": "om_pred"}}, "another outcome"),
    ({"row": {"beyond_crops": {"effect": 9.6, "ci_excludes_zero": False}}}, "interval includes zero"),
    ({"row": {"n_pairs": 12}}, "too few pairs"),
    ({"row": {"group": "all"}}, "no row for the practice"),
    ({"row": {"group_kind": "soil", "group": "1-2%"}}, "a soil-band row"),
    ({"placebos": {"fake_switch_years": [{"comparison": "corn-soy vs continuous corn", "ci_excludes_zero": True}]}},
     "the placebo fails"),
])
def test_effects_provider_falls_back_to_crop_averages(changes, why):
    base = soil_effect.CropAverageProvider(soil_effect.builtin_table())
    provider = soil_effect.EffectsProvider(base, effects_json(**changes))
    assert provider.shifts == {"irrigated": {}, "dryland": {}}, why
    for cycle in [(C, B, C, B), (C, C, C, C), (W, S, F)]:
        for irrigated in (True, False):
            assert soil_effect.plan_bare_days(provider, cycle, irrigated) == soil_effect.plan_bare_days(base, cycle, irrigated)
    assert provider.describe()["adjustments"] == []


def test_effects_provider_from_files(tmp_path):
    base = soil_effect.CropAverageProvider()
    missing = soil_effect.EffectsProvider.from_file(base, tmp_path / "effects.json")
    assert missing.shifts["irrigated"] == {} and "missing" in missing.describe()["note"]
    (tmp_path / "effects.json").write_text("{ nope")
    assert soil_effect.EffectsProvider.from_file(base, tmp_path / "effects.json").shifts["irrigated"] == {}
    (tmp_path / "effects.json").write_text(json.dumps(effects_json()))
    settings = settings_in(tmp_path, SOIL_EFFECT_SOURCE="effects")
    provider = soil_effect.load_provider(settings)
    assert provider.name == "effects" and provider.shifts["irrigated"]["corn-soy"] == 4.8


def test_two_comparisons_share_a_type():
    shifts = soil_effect.type_shifts([("a", "b", 6.0, 40), ("b", "c", 3.0, 40)])
    assert shifts["a"] - shifts["b"] == pytest.approx(6.0) and shifts["b"] - shifts["c"] == pytest.approx(3.0)
    assert sum(shifts.values()) == pytest.approx(0.0, abs=0.02)
    assert soil_effect.type_shifts([]) == {}


def test_plan_rotation_types():
    assert soil_effect.plan_rotation_type((C, C, C, C)) == "continuous corn"
    assert soil_effect.plan_rotation_type((C, B, C, B)) == "corn-soy"
    assert soil_effect.plan_rotation_type((W, S, F)) == "wheat-sorghum-fallow"
    assert soil_effect.plan_rotation_type((W, F, W, F)) == "wheat-sorghum-fallow"
    assert soil_effect.plan_rotation_type((A, A, A, C)) == "alfalfa/perennial"
    assert soil_effect.plan_rotation_type((C, B, W, S)) == "diverse"


# --- erosion.py --------------------------------------------------------------------------------------

def test_erosion_index_arithmetic_and_fallbacks():
    assert erosion.erosion_index(365, 100, wei=56, slope_pct=0) == pytest.approx(1.0)
    assert erosion.erosion_index(73, 150, wei=112, slope_pct=4) == pytest.approx(0.2 * 2.0 * 1.5 * 1.3)
    assert erosion.soil_factor(None, "3") == pytest.approx(86 / 56)
    assert erosion.soil_factor(None, "4L") == pytest.approx(86 / 56)
    assert erosion.soil_factor(None, None) == 1.0 and erosion.soil_factor(float("nan"), None) == 1.0
    assert erosion.soil_factor(250, "5") == pytest.approx(250 / 56)                 # wei wins over the group
    assert erosion.knoll_factor(None) == 1.0 and erosion.knoll_factor(1.9) == 1.0
    assert erosion.knoll_factor(3.0) == pytest.approx(1.15) and erosion.knoll_factor(30.0) == erosion.KNOLL_CAP
    assert erosion.erosion_index(365, 300, wei=310, slope_pct=10) == erosion.INDEX_CAP
    assert erosion.erosion_index(0, 100) == 0.0
    grid = erosion.erosion_index(np.array([[10.0], [100.0]]), np.array([[80.0, 160.0]]), wei=56)
    assert grid.shape == (2, 2) and (np.diff(grid, axis=0) > 0).all() and (np.diff(grid, axis=1) > 0).all()


# --- money.py ----------------------------------------------------------------------------------------

def test_profit_is_price_times_yield_minus_costs_and_pumping():
    econ = money.builtin_economics()
    assert money.profit(C, "irrigated", 200.0, 400.0, econ) == pytest.approx(4.73 * 200 - 518.41 - 0.1639 * 400)
    assert money.profit(F, "dryland", 0.0, 0.0, econ) == pytest.approx(-14.0)
    assert money.profit(W, "dryland", np.array([30.0, 40.0]), 0.0, econ) == pytest.approx([6.74 * 30 - 233.92, 6.74 * 40 - 233.92])
    # matched to a normal yield below the budget's: cheaper, along the budget's own line, with a floor
    assert econ.cost(W, "dryland", 36.7) == pytest.approx(233.92 + 1.7319 * (36.7 - 54.0))
    assert econ.cost(W, "dryland", 54.0) == pytest.approx(233.92)
    steep = money.Economics(prices=econ.prices, costs=econ.costs, pump_per_mm=econ.pump_per_mm,
                            slopes={W: {"dryland": 10.0}}, budget_yields=econ.budget_yields)
    assert steep.cost(W, "dryland", 20.0) == pytest.approx(0.5 * 233.92)             # never below half the budget
    assert econ.cost(F, "dryland", 0.0) == 14.0
    assert money.profit(W, "dryland", 36.7, 0.0, econ, normal_yield=36.7) > money.profit(W, "dryland", 36.7, 0.0, econ)


def test_tracked_economics_file_loads_and_matches_the_builtin_copy(tmp_path):
    tracked = money.load_economics(ROOT / "api" / "planner" / "economics.json")
    builtin = money.builtin_economics()
    assert tracked.source == "json"
    assert tracked.prices == builtin.prices and tracked.costs == builtin.costs
    assert tracked.slopes == builtin.slopes and tracked.budget_yields == builtin.budget_yields
    assert tracked.pump_per_mm == pytest.approx(builtin.pump_per_mm)
    raw = tracked.raw
    for crop in CROPS:                       # every cost has a source and says whether it was checked
        for practice in PRACTICES:
            entry = raw["costs"][crop][practice]
            assert entry["source"] and entry["status"] in ("verified", "rough, unverified")
    assert raw["prices"]["years"] == [2020, 2024]
    # fallow: one herbicide pass; what the budget check found is on record, the number is still our estimate
    fallow = raw["costs"][F]["dryland"]
    assert fallow["per_acre"] == 14.0 and fallow["status"] == "rough, unverified"
    check = fallow["double_count_check"]
    assert check["status"] == "verified" and check["old_value"] == 55.0 and check["found"] and check["cannot_tell"]
    assert raw["costs"][F]["irrigated"]["per_acre"] == 14.0
    # the irrigation limit: 24 inches, with a source and a status
    limit = raw["irrigation_limit"]
    assert limit["mm"] == pytest.approx(24 * 25.4) and limit["inches"] == 24.0
    assert limit["status"] in ("verified", "rough, unverified") and limit["source"] and limit["url"].startswith("https://")
    assert tracked.irrigation_limit_mm == builtin.irrigation_limit_mm == pytest.approx(609.6)
    assert tracked.price_years == builtin.price_years == (2020, 2024) and tracked.cost_year == builtin.cost_year == 2027
    basis = money.profit_basis(tracked)
    assert "2020-2024" in basis and "2027" in basis and "direct costs" in basis
    assert "Land and machinery ownership are not taken off" in basis and "no crop insurance payments" in basis
    assert "earn" not in basis.lower() and "income" not in basis.lower()
    no_limit = {**raw, "irrigation_limit": {"mm": 0}}
    (tmp_path / "free.json").write_text(json.dumps(no_limit))
    assert money.load_economics(tmp_path / "free.json").irrigation_limit_mm == 0.0
    # missing or broken files fall back
    assert money.load_economics(tmp_path / "nope.json").source == "builtin"
    for text in ("not json", "[]", '{"prices": {}}', json.dumps({**raw, "pumping": {"per_mm": -1}})):
        (tmp_path / "e.json").write_text(text)
        assert money.load_economics(tmp_path / "e.json") == builtin


def nass_prices(values, level="state"):
    return pd.DataFrame([{"year": y, "crop": crop, "practice": "all", "stat": "price", "value": v,
                          "unit": "$/ton" if crop == A else "$/bu", "level": level, "source_desc": "made up"}
                         for crop, series in values.items() for y, v in series.items()])


def test_prices_from_the_nass_table_and_the_regeneration_script(tmp_path):
    series = {crop: {2019: 100.0, 2020: 4.0, 2021: 6.0, 2022: 7.0, 2023: 5.0, 2024: 4.5} for crop in money.PRICE_CROPS}
    nass = pd.concat([nass_prices(series), nass_prices({C: {2022: 99.0}}, level="county"),
                      pd.DataFrame([{"year": 2022, "crop": C, "practice": "irrigated", "stat": "yield", "value": 200.0,
                                     "unit": "bu/acre", "level": "county", "source_desc": ""}])])
    got = money.prices_from_nass(nass)
    assert got[C] == {"price": 5.0, "unit": "$/bu", "n_years": 5, "level": "state"}
    assert got[A]["unit"] == "$/ton"
    assert money.prices_from_nass(nass, (2021, 2022), "mean")[W]["price"] == 6.5
    with pytest.raises(ValueError):
        money.prices_from_nass(nass, (1990, 1995))
    copy = tmp_path / "economics.json"
    copy.write_text((ROOT / "api" / "planner" / "economics.json").read_text())
    nass.to_parquet(tmp_path / "nass.parquet")
    money.update_prices(copy, tmp_path / "nass.parquet", (2020, 2024))
    updated = money.load_economics(copy)
    assert updated.source == "json" and updated.price(C) == 5.0
    assert updated.costs == money.builtin_economics().costs                 # only the prices were rewritten


# --- traits.py ---------------------------------------------------------------------------------------

def test_irrigation_guess_from_crop_history_and_traits_loader(tmp_path):
    assert guess_irrigation(IRRIGATED_HISTORY)[0] is True
    assert guess_irrigation(a_field().crop_history)[0] is False
    assert guess_irrigation({})[0] is False and "assumed dryland" in guess_irrigation({})[1]
    assert guess_irrigation({"2020": C, "2021": F, "2022": C, "2023": F})[0] is False
    assert load_traits(tmp_path / "missing.parquet") == {}
    (tmp_path / "bad.parquet").write_text("x")
    assert load_traits(tmp_path / "bad.parquet") == {}
    pd.DataFrame({"field_id": ["KS-000001", "KS-000002"], "irrigation": ["irrigated", "dryland"],
                  "irrigated_prob": [0.9, 0.1], "irrigation_basis": ["pivot", "fallow"], "weg": ["3", None],
                  "wei": [86.0, float("nan")], "et_gs_mm": [700.0, 400.0]}).to_parquet(tmp_path / "t.parquet")
    traits = load_traits(tmp_path / "t.parquet")
    assert traits["KS-000001"] == FieldTraits("irrigated", 0.9, "pivot", "3", 86.0)
    assert traits["KS-000002"].wei is None and traits["KS-000002"].weg is None


# --- smart.py: scoring and ranking on the made-up engine ---------------------------------------------

def test_result_has_everything_the_plans_endpoint_needs(engine):
    result = smart.rank_plans_smart(a_field(), engine=engine, scenario="ssp585", horizon=2040)
    body = result.to_dict()
    json.dumps(body)
    assert set(body) == {"field_id", "engine", "settings", "climate", "plans", "current", "current_note", "candidates"}
    assert body["engine"] == "smart" and body["field_id"] == "KS-000001" and body["candidates"] == 17
    s = body["settings"]
    assert s["scenario"] == "ssp585" and s["horizon"] == 2040 and s["window"] == [2030, 2049]
    assert s["irrigated"] is False and s["irrigation_source"] == "guess" and s["soil_source"] == "crop_average"
    assert s["weights"] == config.DEFAULT_WEIGHTS and "1 in 4" in s["holds_up_rule"]
    assert s["models"] == ["cool", "hot", "mid"]
    assert set(s) == {"weights", "scenario", "horizon", "window", "irrigated", "irrigation_source", "irrigation_basis",
                      "soil_source", "soil_note", "holds_up_rule", "ranking_rule", "profit_basis", "irrigation_limit_mm",
                      "irrigation_limit_in", "yield_change_note", "limits", "models"}
    assert s["irrigation_limit_mm"] == 610 and s["irrigation_limit_in"] == 24.0
    assert s["profit_basis"] == money.profit_basis(money.builtin_economics())
    assert "new varieties" in s["yield_change_note"] and "adaptation" in s["yield_change_note"]
    assert set(body["climate"]) == {"label", "changes", "range", "window", "models", "yield_change"}
    assert set(body["climate"]["yield_change"]) == {C, W, S}                 # the crops dryland plans grow
    assert len(body["plans"]) == 3
    for plan in body["plans"]:
        assert set(plan) == {"name", "rotation", "cycle", "scores", "reason", "futures", "spread", "numbers", "rank"}
        assert len(plan["rotation"]) == 4 and all(c in CROPS for c in plan["rotation"])
        assert set(plan["scores"]) == {"soil", "water", "money", "risk", "overall", "robust"}
        assert all(0.0 <= v <= 1.0 for v in plan["scores"].values())
        assert plan["futures"]["total"] == 3 and 0 <= plan["futures"]["holds"] <= 3
        assert set(plan["spread"]) == {"overall", "profit_per_acre"}
        assert plan["spread"]["overall"]["low"] <= plan["spread"]["overall"]["mid"] <= plan["spread"]["overall"]["high"]
        assert set(plan["numbers"]) == {"bare_days", "erosion_index", "irrigation_mm", "irrigation_in",
                                        "water_met_share", "profit_per_acre", "failed_share"}
        assert cand.rules_broken(plan["cycle"], False) == []
        w = s["weights"]
        mean = sum(w[k] * plan["scores"][k] for k in config.SCORE_NAMES)
        assert plan["scores"]["overall"] == pytest.approx(mean, abs=0.002)        # the weighted mean of the four
        assert plan["scores"]["robust"] <= plan["scores"]["overall"] + 1e-9         # the worst third pulls it down
    assert [p["rank"] for p in body["plans"]] == sorted(p["rank"] for p in body["plans"]) and body["plans"][0]["rank"] == 1
    robust = [p["scores"]["robust"] for p in body["plans"]]
    assert robust == sorted(robust, reverse=True)
    old = result.as_plans()
    assert all(isinstance(p, Plan) for p in old) and old[0].rotation == body["plans"][0]["rotation"]
    assert old[0].scores.overall == body["plans"][0]["scores"]["overall"]


def test_results_are_deterministic(engine):
    for field in (a_field(), a_field(IRRIGATED_HISTORY, "KS-000002")):
        a = smart.rank_plans_smart(field, engine=engine, scenario="ssp245", horizon=2050, weights={"soil": 2, "risk": 1})
        b = smart.rank_plans_smart(field, engine=engine, scenario="ssp245", horizon=2050, weights={"soil": 2, "risk": 1})
        assert a == b and json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), sort_keys=True)
    yearly, monthly = climate_tables()
    again = smart.Engine(yearly.sample(frac=1.0, random_state=3), monthly.sample(frac=1.0, random_state=4),
                         economics=money.builtin_economics(), soil=soil_effect.CropAverageProvider())
    assert smart.rank_plans_smart(a_field(), engine=again).to_dict() == smart.rank_plans_smart(a_field(), engine=engine).to_dict()


def test_weights_are_normalised_and_checked(engine):
    assert smart.Weights(3, 1, 0, 0).normalised() == smart.Weights(0.75, 0.25, 0.0, 0.0)
    assert smart.Weights.of({"soil": 2}).normalised() == smart.Weights(1.0, 0.0, 0.0, 0.0)
    assert smart.Weights.of(None) == smart.Weights(**config.DEFAULT_WEIGHTS)
    result = smart.rank_plans_smart(a_field(), engine=engine, weights={"soil": 30, "water": 20, "money": 30, "risk": 20})
    assert result.settings["weights"] == config.DEFAULT_WEIGHTS
    assert result == smart.rank_plans_smart(a_field(), engine=engine)
    for bad in ({"soil": 0, "water": 0, "money": 0, "risk": 0}, {"soil": -1, "money": 2}, {"soil": float("nan")},
                {"soil": float("inf")}, {"soil": "1"}, {"profit": 1}):
        with pytest.raises(ValueError):
            smart.rank_plans_smart(a_field(), engine=engine, weights=bad)


@pytest.mark.parametrize("kwargs", [{"scenario": "ssp126"}, {"scenario": None}, {"horizon": 2024}, {"horizon": 2061},
                                    {"horizon": "2040"}, {"top": 0}, {"top": 2.5}])
def test_bad_requests_are_value_errors(engine, kwargs):
    with pytest.raises(ValueError):
        smart.rank_plans_smart(a_field(), engine=engine, **kwargs)


def test_irrigation_comes_from_the_override_then_traits_then_history(engine):
    dry_history = a_field()
    guessed = smart.rank_plans_smart(dry_history, engine=engine)
    assert guessed.settings["irrigated"] is False and guessed.settings["irrigation_basis"].startswith("crop history")
    assert guessed.candidates == 17 and all(p.numbers["irrigation_mm"] == 0 for p in guessed.plans)
    assert all(p.numbers["irrigation_in"] == 0 for p in guessed.plans)
    by_traits = smart.rank_plans_smart(dry_history, engine=engine, traits=FieldTraits("irrigated", 0.9, "pivot circle"))
    assert by_traits.settings["irrigated"] is True and by_traits.settings["irrigation_source"] == "guess"
    assert by_traits.settings["irrigation_basis"] == "pivot circle" and by_traits.candidates == 105
    assert all(p.numbers["irrigation_mm"] > 0 for p in by_traits.plans)
    assert all(p.numbers["irrigation_in"] == pytest.approx(p.numbers["irrigation_mm"] / 25.4, abs=0.06) for p in by_traits.plans)
    forced = smart.rank_plans_smart(dry_history, engine=engine, traits=FieldTraits("irrigated"), irrigated=False)
    assert forced.settings["irrigated"] is False and forced.settings["irrigation_source"] == "override"
    assert forced.plans == guessed.plans
    assert smart.rank_plans_smart(a_field(IRRIGATED_HISTORY), engine=engine).settings["irrigated"] is True
    looked_up = smart.Engine(*climate_tables(), economics=money.builtin_economics(),
                             traits={"KS-000001": FieldTraits("irrigated", 0.8, "from the table", "3", 86.0)})
    assert smart.rank_plans_smart(dry_history, engine=looked_up).settings["irrigation_basis"] == "from the table"


def test_a_hotter_future_lowers_scores_on_fixed_scales(engine):
    w = smart.Weights().normalised()
    for irrigated in (True, False):
        today = engine.evaluate(irrigated, "ssp245", 2025)
        middle = engine.evaluate(irrigated, "ssp245", 2060)
        high = engine.evaluate(irrigated, "ssp585", 2060)
        assert (middle.overall(w).mean(axis=1) < today.overall(w).mean(axis=1)).all()
        assert (high.overall(w).mean(axis=1) < middle.overall(w).mean(axis=1)).all()
        assert (high.profit.mean(axis=1) <= today.profit.mean(axis=1) + 1e-9).all()
        assert (high.failed.mean(axis=1) >= today.failed.mean(axis=1) - 1e-9).all()
        assert (high.erosion.mean(axis=1) > today.erosion.mean(axis=1)).all()
        for arr in (high.soil, high.water, high.money, high.risk, today.soil, today.water, today.money, today.risk):
            assert np.isfinite(arr).all() and arr.min() >= 0.0 and arr.max() <= 1.0
    wet = engine.evaluate(True, "ssp245", 2025)
    dry = engine.evaluate(True, "ssp585", 2060)
    assert (dry.irrigation.mean(axis=1) > wet.irrigation.mean(axis=1)).all()
    assert (dry.water.mean(axis=1) < wet.water.mean(axis=1)).all()
    # within one window the hot model is the worst future, the cool one the best
    order = [engine.climate.models.index(m) for m in ("cool", "mid", "hot")]
    dryland = engine.evaluate(False, "ssp585", 2060).overall(w)[:, order]
    assert (np.diff(dryland, axis=1) < 0).all()


def test_each_priority_alone_picks_the_plan_that_is_best_at_it(engine):
    for irrigated, field in ((True, a_field(IRRIGATED_HISTORY)), (False, a_field())):
        ev = engine.evaluate(irrigated, "ssp245", 2040, slope_pct=1.0)
        top = lambda **w: smart.rank_plans_smart(field, engine=engine, horizon=2040, weights=w).plans[0]   # noqa: E731
        assert top(soil=1).numbers["bare_days"] == pytest.approx(ev.bare.min(), abs=0.06)
        assert top(money=1).numbers["profit_per_acre"] == pytest.approx(ev.profit.mean(axis=1).max(), abs=3.0)
        assert top(risk=1).numbers["failed_share"] == pytest.approx(ev.failed.mean(axis=1).min(), abs=0.02)
        if irrigated:
            assert top(water=1).numbers["irrigation_mm"] == pytest.approx(ev.irrigation.mean(axis=1).min(), abs=3.0)
            assert A in top(soil=1).rotation
        else:
            assert top(water=1).numbers["water_met_share"] == pytest.approx(ev.water_met.mean(axis=1).max(), abs=0.01)


def test_fallow_heavy_plans_are_worst_on_soil_and_alfalfa_best(engine):
    ev = engine.evaluate(True, "ssp245", 2025, wei=56, slope_pct=1.0)
    soil = ev.soil.mean(axis=1)
    best, worst = ev.plans.cycles[int(soil.argmax())], ev.plans.cycles[int(soil.argmin())]
    assert best == (A, A, A, A) and F in worst
    dry = engine.evaluate(False, "ssp245", 2025, wei=56, slope_pct=1.0)
    soil, ero = dry.soil.mean(axis=1), dry.erosion.mean(axis=1)
    fallow_share = np.array([sum(c == F for c in cy) / len(cy) for cy in dry.plans.cycles])
    assert fallow_share[int(soil.argmin())] == 0.5 and fallow_share[int(ero.argmax())] == 0.5
    assert fallow_share[int(soil.argmax())] == 0.0


def test_sandier_and_steeper_fields_score_lower_on_soil(engine):
    loam = engine.evaluate(False, "ssp245", 2025, wei=56, slope_pct=1.0)
    sand = engine.evaluate(False, "ssp245", 2025, wei=250, slope_pct=1.0)
    knoll = engine.evaluate(False, "ssp245", 2025, wei=56, slope_pct=5.0)
    unknown = engine.evaluate(False, "ssp245", 2025)
    by_group = engine.evaluate(False, "ssp245", 2025, weg="5", slope_pct=1.0)
    assert (sand.soil < loam.soil).all() and (knoll.soil < loam.soil).all()
    assert np.allclose(unknown.soil, loam.soil) and np.allclose(by_group.soil, loam.soil)       # unknown soil is neutral
    assert np.allclose(sand.money, loam.money) and np.allclose(sand.risk, loam.risk)
    # the erosion part matters more on sand: the gap between the best and the worst plan widens
    assert np.ptp(sand.soil.mean(axis=1)) > np.ptp(loam.soil.mean(axis=1))


def test_holds_up_is_the_stated_rule(engine):
    for irrigated in (True, False):
        ev = engine.evaluate(irrigated, "ssp585", 2060)
        expected = (ev.failed <= config.HOLDS_MAX_FAILED_SHARE + 1e-12) & (ev.profit >= config.HOLDS_MIN_PROFIT)
        assert (ev.holds == expected).all()
    assert "1 in 4" in smart.HOLDS_UP_RULE and "direct costs" in smart.HOLDS_UP_RULE
    now = engine.evaluate(False, "ssp245", 2025).holds.sum()
    later = engine.evaluate(False, "ssp585", 2060).holds.sum()
    assert later < now


def test_robust_value_is_half_mean_half_worst_third():
    overall = np.array([[0.6, 0.6, 0.6, 0.6, 0.6, 0.6], [0.9, 0.9, 0.9, 0.9, 0.2, 0.1], [0.5, 0.7, 0.3, 0.6, 0.4, 0.8]])
    got = smart.robust_value(overall)
    assert got[0] == pytest.approx(0.6)
    assert got[1] == pytest.approx(0.5 * np.mean(overall[1]) + 0.5 * 0.15)
    assert got[2] == pytest.approx(0.5 * 0.55 + 0.5 * 0.35)
    assert got[0] > got[1]                      # steady beats better-on-average-but-fragile (means 0.60 and 0.65)
    assert smart.robust_value(np.array([[0.2, 0.8, 0.5]]))[0] == pytest.approx(0.5 * 0.5 + 0.5 * 0.2)   # 3 futures: the worst one


def test_top_plans_are_different_choices(engine):
    for field in (a_field(), a_field(IRRIGATED_HISTORY)):
        for weights in (None, {"soil": 1}, {"money": 1}, {"water": 1, "risk": 1}):
            result = smart.rank_plans_smart(field, engine=engine, weights=weights, top=4)
            assert len(result.plans) == 4 and len({tuple(p.cycle) for p in result.plans}) == 4
            for i, p in enumerate(result.plans):
                for q in result.plans[i + 1:]:
                    assert cand.years_different(p.cycle, q.cycle) > config.SAME_CHOICE_YEARS
    shares = np.array([[1, 0, 0], [0.75, 0.25, 0], [0.5, 0.5, 0], [0, 0, 1.0], [0.75, 0, 0.25]])
    assert smart.pick_different([0, 1, 2, 3, 4], shares, 3) == [0, 2, 3]       # 1 is a one-year tweak of 0
    assert smart.pick_different([0, 1, 4], shares, 3) == [0, 1, 4]             # the bar drops when the list runs out
    assert smart.pick_different([0, 1, 2, 3, 4], shares, 1) == [0]
    assert len(smart.rank_plans_smart(a_field(), engine=engine, top=50).plans) == 17


def test_reasons_are_plain_short_and_built_from_the_plans_numbers(engine):
    seen = set()
    for field in (a_field(), a_field(IRRIGATED_HISTORY)):
        for scenario, horizon in (("ssp245", 2025), ("ssp585", 2060)):
            for plan in smart.rank_plans_smart(field, engine=engine, scenario=scenario, horizon=horizon).plans:
                seen.add(plan.reason)
                assert plan.reason[0].isupper()
                text = plan.reason[0].lower() + plan.reason[1:]
                assert len(text) < config.MAX_REASON_CHARS and text.endswith(".")
                assert f"holds up in {plan.futures['holds']} of {plan.futures['total']} climate futures" in text
                assert f"bare about {plan.numbers['bare_days']:.0f} days" in text
                # dollars are return over direct costs, never "earns" or "loses"
                amount = f"${abs(plan.numbers['profit_per_acre']):,.0f} an acre"
                assert (f"returns about {amount} a year over direct costs" in text
                        or f"falls about {amount} short of its direct costs" in text or "about covers its direct costs" in text)
                assert "earn" not in text.lower() and "loses" not in text and " mm " not in text
                if plan.numbers["irrigation_mm"] > 0:      # water in inches; the mm stay in the numbers
                    assert f"pumps about {round(plan.numbers['irrigation_mm'] / 25.4):.0f} inches of water a year" in text
    assert len(seen) >= 6
    worst = {"bare_days": 138.4, "irrigation_mm": 1234.0, "water_met_share": 0.384, "profit_per_acre": -1234.0,
             "failed_share": 0.61}
    scores = {"soil": 0.1, "water": 0.2, "money": 0.0}
    for irrigated in (True, False):
        text = smart.reason_for(worst, scores, 0, 6, irrigated, crop_failures=6)
        assert len(text) < config.MAX_REASON_CHARS and "falls about $1,234 an acre short of its direct costs" in text
        assert "(more than 1 crop in 2 fails)" in text
    assert "umps about 49 inches of water a year" in smart.reason_for(worst, scores, 0, 6, True)
    assert "umps about 24 inches of water a year" in smart.reason_for({**worst, "irrigation_mm": 610.0}, scores, 0, 6, True)
    assert "eeds almost no pumped water" in smart.reason_for({**worst, "irrigation_mm": 5.0}, scores, 0, 6, True)
    good = smart.reason_for({**worst, "profit_per_acre": 345.4, "failed_share": 0.01}, scores, 6, 6, True)
    assert "returns about $345 an acre a year over direct costs" in good and "fails" not in good
    fine = smart.reason_for({**worst, "profit_per_acre": 0.2, "failed_share": 0.01}, scores, 6, 6, False)
    assert "about covers its direct costs" in fine and "fails" not in fine


@pytest.mark.parametrize("failed, words", [
    (0.61, "more than 1 crop in 2 fails"), (0.50, "more than 1 crop in 3 fails"), (0.34, "more than 1 crop in 3 fails"),
    (0.28, "more than 1 crop in 4 fails"), (0.251, "more than 1 crop in 4 fails"),      # never rounded down to "about 1 in 4"
    (0.25, "about 1 crop in 4 fails"), (0.24, "about 1 crop in 4 fails"), (0.19, "about 1 crop in 5 fails"),
    (0.10, "about 1 crop in 10 fails"),
])
def test_failed_crops_are_never_rounded_across_the_holds_up_line(failed, words):
    numbers = {"bare_days": 81.0, "irrigation_mm": 0.0, "water_met_share": 0.58, "profit_per_acre": -3.0, "failed_share": failed}
    text = smart.reason_for(numbers, {"soil": 0.5, "water": 0.6, "money": 0.4}, 2, 6, False, crop_failures=4)
    assert f"({words})" in text and len(text) < config.MAX_REASON_CHARS
    assert "fails" not in smart.reason_for(numbers, {"soil": 0.5, "water": 0.6, "money": 0.4}, 6, 6, False, crop_failures=0)


# --- one Today, the irrigation limit, the yield change, the plain limits --------------------------------

def test_today_is_identical_under_both_scenarios(engine):
    for irrigated, field in ((False, a_field()), (True, a_field(IRRIGATED_HISTORY))):
        a = engine.evaluate(irrigated, "ssp245", 2025)
        b = engine.evaluate(irrigated, "ssp585", 2025)
        for name in ("profit", "failed", "irrigation", "water_met", "soil", "water", "money", "risk", "erosion", "holds"):
            assert np.array_equal(getattr(a, name), getattr(b, name)), name
        assert a.profit.shape[1] == 3                                          # still one future per model
        middle = smart.rank_plans_smart(field, engine=engine, scenario="ssp245", horizon=2025).to_dict()
        high = smart.rank_plans_smart(field, engine=engine, scenario="ssp585", horizon=2025).to_dict()
        assert middle["settings"].pop("scenario") == "ssp245" and high["settings"].pop("scenario") == "ssp585"
        assert middle == high                                                  # everything else, to the last digit
        assert middle["climate"]["label"] == "Today's climate (model average, 2015-2034)"
        assert all(v == {"pct": 0, "low": 0, "high": 0} for v in middle["climate"]["yield_change"].values())
        # a year later the scenarios part again
        assert not np.array_equal(engine.evaluate(irrigated, "ssp245", 2030).profit,
                                  engine.evaluate(irrigated, "ssp585", 2030).profit)
    # the pooled Today is the mean of the two runs' own windows (window means are plain averages)
    w = engine._window("ssp245", 2025)["profit"]
    years = engine.climate.year_slice(2025)
    assert np.allclose(w, engine._response["profit"][:, :, :, years].mean(axis=(1, 3)))


def test_yield_change_is_each_model_against_today_for_the_fields_practice(engine):
    dry = smart.rank_plans_smart(a_field(), engine=engine, scenario="ssp585", horizon=2060).climate["yield_change"]
    wet = smart.rank_plans_smart(a_field(IRRIGATED_HISTORY), engine=engine, scenario="ssp585", horizon=2060).climate["yield_change"]
    assert set(dry) == {C, W, S} and set(wet) == {C, B, W, S, A}              # no dryland alfalfa or soybeans, no fallow
    for change in (dry, wet):
        for crop, v in change.items():
            assert set(v) == {"pct", "low", "high"} and all(isinstance(x, int) for x in v.values())
            assert v["low"] <= v["pct"] <= v["high"] and v["pct"] < 0, crop        # hotter and thirstier: every crop yields less
    assert dry[C]["pct"] < wet[C]["pct"]                                       # dryland corn loses more than irrigated corn
    # hand check for dryland corn: window mean yield (county's usual previous crop), model by model
    p, ci = PRACTICES.index("dryland"), CROPS.index(C)
    typical = engine._response["yield_typical"][p]                             # [scenario, model, year, crop]
    now = typical[:, :, engine.climate.year_slice(2025), ci].mean(axis=(0, 2))
    then = typical[engine.climate.scenarios.index("ssp585"), :, engine.climate.year_slice(2060), ci].mean(axis=1)
    pct = (then / now - 1) * 100
    assert dry[C] == {"pct": round(pct.mean()), "low": round(pct.min()), "high": round(pct.max())}
    mild = smart.rank_plans_smart(a_field(), engine=engine, scenario="ssp245", horizon=2040).climate["yield_change"]
    assert dry[C]["pct"] < mild[C]["pct"] < 0


def test_irrigation_limit_caps_pumping_and_costs_thirsty_plans(engine):
    yearly, monthly = climate_tables()
    common = dict(economics=money.builtin_economics(), soil=soil_effect.CropAverageProvider(soil_effect.builtin_table()))
    free = smart.Engine(yearly, monthly, irrigation_limit_mm=0, **common)
    tight = smart.Engine(yearly, monthly, irrigation_limit_mm=400.0, **common)
    assert engine.irrigation_limit_mm == pytest.approx(609.6) and free.irrigation_limit_mm == 0 and tight.irrigation_limit_mm == 400
    a, b = free.evaluate(True, "ssp585", 2060), tight.evaluate(True, "ssp585", 2060)
    assert (b.irrigation <= 400.0 + 1e-9).all() and a.irrigation.max() > 400.0
    assert (b.irrigation <= a.irrigation + 1e-9).all() and (b.water >= a.water - 1e-12).all()      # the water score uses what is pumped
    assert (a.water_met == 1.0).all() and b.water_met.min() < 1.0 and (b.water_met <= 1.0).all()
    assert (b.failed >= a.failed - 1e-12).all() and b.failed.max() > a.failed.max()                 # short crops can fail
    cycles = a.plans.cycles
    alfalfa, thrifty = cycles.index((A, A, A, A)), cycles.index((B, W, B, W))
    cost = (a.profit - b.profit).mean(axis=1)                    # what the limit costs, $ an acre a year
    assert cost[alfalfa] > cost[thrifty] >= 0 and cost[alfalfa] > 20
    # the cost is yield lost minus pumping saved: profit = price x yield - costs - pumping at what is really pumped
    assert (a.irrigation[alfalfa] - b.irrigation[alfalfa]).mean() > 100
    # as reference ET rises the limit costs the thirsty plan more
    early = (free.evaluate(True, "ssp585", 2030).profit - tight.evaluate(True, "ssp585", 2030).profit).mean(axis=1)
    assert cost[alfalfa] - early[alfalfa] > cost[thrifty] - early[thrifty] and cost[alfalfa] > early[alfalfa]
    # dryland is untouched by the limit
    assert np.array_equal(free.evaluate(False, "ssp585", 2060).profit, tight.evaluate(False, "ssp585", 2060).profit)
    # how often the limit binds, per crop
    binds = tight.limit_binds("ssp585", 2060)
    assert set(binds) == {C, B, W, S, A} and binds[A] == 1.0 and 0.0 <= binds[W] <= binds[A]
    assert all(v == 0.0 for v in free.limit_binds("ssp585", 2060).values())
    # the result says what the limit is, in both units, and the plain limits name it
    field = a_field(IRRIGATED_HISTORY)
    s_tight = smart.rank_plans_smart(field, engine=tight).settings
    s_free = smart.rank_plans_smart(field, engine=free).settings
    assert s_tight["irrigation_limit_mm"] == 400 and s_tight["irrigation_limit_in"] == 15.7
    assert s_free["irrigation_limit_mm"] is None and s_free["irrigation_limit_in"] is None
    assert any("up to 16 inches of water an acre every year" in line for line in s_tight["limits"])
    assert any("always delivers all the water" in line for line in s_free["limits"])
    for bad in (-1.0, float("nan"), "610", True):
        with pytest.raises(ValueError):
            smart.Engine(yearly, monthly, irrigation_limit_mm=bad, **common)


def test_plain_limits_are_five_short_sentences(engine):
    limits = smart.rank_plans_smart(a_field(), engine=engine).settings["limits"]
    assert len(limits) == 5 and all(isinstance(x, str) and x.endswith(".") and len(x) < 160 for x in limits)
    text = " ".join(limits)
    assert "Yields do not change with the crop grown before, apart from the soil water" in text
    assert "up to 24 inches of water an acre every year" in text
    assert "Prices and costs stay at today's levels" in text
    assert "water, money and risk scores move together" in text
    assert '"Today" is a model climate for 2015-2034, a little hotter than the 2000-2024 record.' in text
    assert "soil health" not in text.lower() and "improve" not in text.lower()
    assert limits == smart.rank_plans_smart(a_field(IRRIGATED_HISTORY), engine=engine, horizon=2050).settings["limits"]


def test_irrigation_limit_setting_from_the_environment(tmp_path):
    assert config.settings({}).irrigation_limit_mm is None                      # not set: the economics file's 610 mm
    assert config.settings({"IRRIGATION_LIMIT_MM": "500"}).irrigation_limit_mm == 500.0
    for text in ("0", "none", "None", " NONE ", "off"):
        assert config.settings({"IRRIGATION_LIMIT_MM": text}).irrigation_limit_mm == 0.0
    for text in ("-5", "lots", "nan", "inf"):
        with pytest.raises(ValueError):
            config.settings({"IRRIGATION_LIMIT_MM": text})
    # it is part of what the default engine is built from, so changing it rebuilds the engine
    a = smart._stamp(settings_in(tmp_path))
    b = smart._stamp(settings_in(tmp_path, IRRIGATION_LIMIT_MM="none"))
    assert a != b and a[1] is None and b[1] == 0.0


def test_hot_days_beyond_the_record_are_capped_in_the_engine():
    """Made-up models: 45 hot days today, rising without end. Past the cap (85) more heat changes nothing."""
    from api.planner import response

    hot = {"mid": 1.0}
    yearly, monthly = climate_tables(models=hot)
    beyond = yearly.assign(hot_days=yearly["hot_days"] + 60.0, wheat_hot_days=yearly["wheat_hot_days"] + 40.0)   # all above the cap
    furnace = beyond.assign(hot_days=beyond["hot_days"] + 100.0, wheat_hot_days=beyond["wheat_hot_days"] + 100.0)
    common = dict(economics=money.builtin_economics(), soil=soil_effect.CropAverageProvider(soil_effect.builtin_table()))
    a = smart.Engine(beyond, monthly, **common).evaluate(False, "ssp585", 2060)
    b = smart.Engine(furnace, monthly, **common).evaluate(False, "ssp585", 2060)
    assert np.allclose(a.profit, b.profit) and np.allclose(a.failed, b.failed)
    cooler = smart.Engine(yearly, monthly, **common).evaluate(False, "ssp245", 2025)
    assert (cooler.profit > a.profit).all()                                    # inside the range heat still costs yield
    # a higher cap (as if the record had hotter years) lets the heat through again
    params = response.with_observed_heat_cap(response.load_params(), pd.DataFrame(
        {"year": [2000, 2001], "hot_days": [60.0, 300.0], "wheat_hot_days": [10.0, 200.0]}))
    assert params["heat_cap"]["hot_days"] == {"observed_max": 300.0, "margin": 10.0, "cap": 310.0}
    c = smart.Engine(furnace, monthly, params=params, **common).evaluate(False, "ssp585", 2060)
    assert (c.profit < b.profit - 1.0).all()


def test_current_pattern_is_scored_the_same_way(engine):
    wsf = smart.rank_plans_smart(a_field(), engine=engine)                 # history ends ... F, W, S (2022-2024)
    current = wsf.current.to_dict()
    assert current["rotation"] == [S, F, W, S] and current["years"] == [2021, 2024]
    assert current["cycle"] == [W, S, F] and current["is_candidate"] is True and current["rules_broken"] == []
    assert current["name"] == "Winter wheat – Sorghum – Fallow (3-year cycle)"
    assert 1 <= current["rank"] <= wsf.candidates
    # it is exactly the candidate's numbers
    ev = engine.evaluate(False, "ssp245", 2025, slope_pct=1.0)
    i = ev.plans.cycles.index((W, S, F))
    assert current["numbers"]["profit_per_acre"] == pytest.approx(ev.profit[i].mean(), abs=0.6)
    assert current["rank"] == 1 + int((np.round(ev.robust(smart.Weights().normalised()), 6)
                                       > round(float(ev.robust(smart.Weights().normalised())[i]), 6)).sum())

    # a pattern that breaks a rule is still scored, and told so gently
    corn = {str(y): C for y in range(2008, 2025)}
    stuck = smart.rank_plans_smart(a_field(corn), engine=engine, irrigated=False).current.to_dict()
    assert stuck["rotation"] == [C] * 4 and stuck["is_candidate"] is False and stuck["previous_crop"] == C
    assert len(stuck["rules_broken"]) == 2 and stuck["rank"] > 1
    assert all(text.startswith("Our plans avoid ") for text in stuck["rules_broken"])
    assert 0.0 <= stuck["scores"]["overall"] <= 1.0 and stuck["reason"].endswith(".")
    pivot = smart.rank_plans_smart(a_field(corn), engine=engine, irrigated=True).current.to_dict()
    assert pivot["is_candidate"] is False                                   # continuous corn is not offered as a plan
    assert pivot["rules_broken"] == ["Our plans avoid more than two years in a row of corn, or of sorghum (one grain "
                                     "crop year after year is not a rotation)."]
    assert pivot["numbers"]["profit_per_acre"] > 0 and pivot["rank"] >= 1
    # the county's wheat-sorghum with no fallow: scored, with the planting-window sentence
    ws = {str(y): (W, S)[y % 2] for y in range(2008, 2025)}
    double = smart.rank_plans_smart(a_field(ws), engine=engine, irrigated=False).current.to_dict()
    assert double["is_candidate"] is False and len(double["rules_broken"]) == 1
    assert "winter wheat straight after corn, sorghum or soybeans" in double["rules_broken"][0]
    wcf = {str(y): (W, C, F)[y % 3] for y in range(2008, 2025)}
    assert smart.rank_plans_smart(a_field(wcf), engine=engine, irrigated=False).current.to_dict()["cycle"] == [W, C, F]

    odd = {**corn, "2023": "triticale"}
    result = smart.rank_plans_smart(a_field(odd), engine=engine)
    assert result.current is None and "triticale" in result.current_note and result.to_dict()["current"] is None
    assert smart.rank_plans_smart(a_field({}), engine=engine).current_note.startswith("No crop history")
    gap = {k: v for k, v in corn.items() if k != "2022"}
    assert "not all on record" in smart.rank_plans_smart(a_field(gap), engine=engine).current_note


def test_years_as_they_happened_use_the_real_previous_crop(engine):
    rotation, years, note, before = smart.current_pattern({"2019": F, "2020": W, "2021": W, "2022": S, "2023": C, "2024": "fallow "})
    assert rotation == (W, S, C, F) and years == [2021, 2022, 2023, 2024] and note is None and before == W
    after_wheat = engine.evaluate(False, "ssp245", 2025, rotations=[rotation], first_prev=W)
    after_fallow = engine.evaluate(False, "ssp245", 2025, rotations=[rotation], first_prev=F)
    as_cycle = engine.evaluate(False, "ssp245", 2025, rotations=[rotation])
    assert np.allclose(after_fallow.profit, as_cycle.profit)                # year 4 is fallow: the same thing
    assert (after_fallow.profit > after_wheat.profit).all()                 # wheat after fallow has stored water


def test_soil_source_setting_reaches_the_result():
    yearly, monthly = climate_tables()
    base = soil_effect.CropAverageProvider(soil_effect.builtin_table())
    eng = smart.Engine(yearly, monthly, economics=money.builtin_economics(), soil=soil_effect.EffectsProvider(base, effects_json()))
    result = smart.rank_plans_smart(a_field(IRRIGATED_HISTORY), engine=eng)
    assert result.settings["soil_source"] == "effects" and "corn-soy vs continuous corn" in result.settings["soil_note"]
    plain = smart.Engine(yearly, monthly, economics=money.builtin_economics(), soil=base)
    cycles = cand.candidates(True)
    a, b = eng.evaluate(True).bare, plain.evaluate(True).bare
    moved = {cycles[i]: round(a[i] - b[i], 2) for i in range(len(cycles)) if abs(a[i] - b[i]) > 1e-9}
    assert moved and set(moved.values()) <= {4.8, -4.8} and 4.8 in moved.values()
    assert all(soil_effect.plan_rotation_type(c) in ("corn-soy", "continuous corn") for c in moved)
    # continuous corn is no longer a candidate, but a field that grows it is still scored with its shift
    mono = [(C, C, C, C)]
    assert eng.evaluate(True, rotations=mono).bare[0] - plain.evaluate(True, rotations=mono).bare[0] == pytest.approx(-4.8)


def test_no_climate_tables_means_not_available(tmp_path, monkeypatch):
    for var in ("CLIMATE_FUTURES_PATH", "CLIMATE_MONTHLY_PATH", "CLIMATE_HISTORY_PATH", "FIELD_TRAITS_PATH",
                "SOIL_HEALTH_PATH", "PLANNER_FIELDS_PATH", "EFFECTS_PATH", "PLANNER_SOIL_CACHE_PATH"):
        monkeypatch.setenv(var, str(tmp_path / (var.lower() + ".x")))
    smart.reset()
    try:
        assert smart.available() is False
        assert smart.climate_summary() == {"available": False, "reason": "not_built"}
        with pytest.raises(RuntimeError):
            smart.rank_plans_smart(a_field())
        # files that exist but are not the climate tables: still not available, and no crash
        (tmp_path / "climate_futures_path.x").write_text("junk")
        (tmp_path / "climate_monthly_path.x").write_text("junk")
        assert smart.available() is False
        assert smart.climate_summary()["available"] is False
    finally:
        smart.reset()


def test_old_planner_is_untouched():
    from api.planner import FieldInput as OldInput, rank_plans

    plans = rank_plans(OldInput("KS-000001", {"2020": C}, 2.0, 1.0, 50.0))
    assert len(plans) == 3 and all(isinstance(p, Plan) for p in plans)


# --- real tables (skipped on a fresh clone) ----------------------------------------------------------

DATA = ROOT / "data"
needs_climate = pytest.mark.skipif(
    not all((DATA / n).is_file() for n in ("climate_futures.parquet", "climate_monthly.parquet")),
    reason="climate tables not built")
needs_fields = pytest.mark.skipif(not (DATA / "fields.parquet").is_file(), reason="data/fields.parquet not built")
needs_soil = pytest.mark.skipif(not (DATA / "soil_health.parquet").is_file(), reason="data/soil_health.parquet not built")


@pytest.fixture()
def real_settings(tmp_path, monkeypatch):
    """The real tables, with the planner's own cache pointed at tmp_path so nothing under data/ is written."""
    for var in ("CLIMATE_FUTURES_PATH", "CLIMATE_MONTHLY_PATH", "CLIMATE_HISTORY_PATH", "FIELD_TRAITS_PATH",
                "SOIL_HEALTH_PATH", "PLANNER_FIELDS_PATH", "EFFECTS_PATH", "NASS_PATH", "ECONOMICS_PATH",
                "SOIL_EFFECT_SOURCE", "IRRIGATION_LIMIT_MM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PLANNER_SOIL_CACHE_PATH", str(tmp_path / "planner_bare_days.json"))
    smart.reset()
    yield config.settings()
    smart.reset()


@needs_soil
@needs_fields
def test_real_bare_day_table_has_fallow_barest_and_alfalfa_least(real_settings):
    table = soil_effect.load_table(real_settings, write_cache=True)
    assert table.source == "tables" and real_settings.soil_cache_path.parent != DATA
    for practice in PRACTICES:
        order = sorted(CROPS, key=lambda c: table.by_crop[practice][c])
        assert order[0] == A and order[-1] == F
    assert table.by_crop["dryland"][F] > 100 and table.by_crop["irrigated"][A] < 15
    assert table.info["field_years"] > 30000
    # the built-in constants are these numbers (to within what a rebuild of the tables would move)
    for practice in PRACTICES:
        for crop in CROPS:
            assert table.by_crop[practice][crop] == pytest.approx(soil_effect.BUILTIN_BY_CROP[practice][crop], abs=5.0)


@needs_climate
@needs_fields
def test_real_fields_rank_fast_and_sanely(real_settings):
    import time

    from api.planner import sanity

    assert smart.available() is True
    summary = smart.climate_summary()
    assert summary["available"] is True and len(summary["models"]) == 6
    assert len(summary["scenarios"]["ssp245"]) == 8 and summary["baseline"]["window"] == [2015, 2034]
    assert summary["scenarios"]["ssp585"][-1]["changes"]["tmean_c"] > summary["scenarios"]["ssp245"][-1]["changes"]["tmean_c"] > 0
    fields = sanity.load_fields(real_settings.fields_path)[::400]
    smart.rank_plans_smart(fields[0])                       # the load happens here, once
    start = time.perf_counter()
    for k, field in enumerate(fields):
        result = smart.rank_plans_smart(field, scenario=("ssp245", "ssp585")[k % 2], horizon=2025 + 5 * (k % 8))
        assert len(result.plans) == 3 and result.plans[0].rank == 1
        for plan in result.plans:
            assert cand.rules_broken(plan.cycle, result.settings["irrigated"]) == []
            assert all(0.0 <= v <= 1.0 for v in plan.scores.values()) and len(plan.reason) < config.MAX_REASON_CHARS
    per_call = (time.perf_counter() - start) / len(fields)
    assert per_call < 0.15, f"one call took {per_call * 1e3:.1f} ms"


@needs_climate
@needs_fields
def test_real_today_is_one_climate_and_the_limit_and_cap_are_in_force(real_settings, monkeypatch):
    from api.planner import sanity

    engine = smart.get_engine()
    assert engine.irrigation_limit_mm == pytest.approx(609.6)
    # the hot-day cap is the record's own maximum plus the margin
    cap = engine.params["heat_cap"]
    if real_settings.climate_history_path.is_file():
        history = pd.read_parquet(real_settings.climate_history_path)
        assert cap["hot_days"]["cap"] == history["hot_days"].max() + 10.0
        assert cap["wheat_hot_days"]["cap"] == history["wheat_hot_days"].max() + 5.0
    fields = sanity.load_fields(real_settings.fields_path)[::900]
    for field in fields:
        a = smart.rank_plans_smart(field, scenario="ssp245", horizon=2025).to_dict()
        b = smart.rank_plans_smart(field, scenario="ssp585", horizon=2025).to_dict()
        assert a["settings"].pop("scenario") != b["settings"].pop("scenario") and a == b
        far = smart.rank_plans_smart(field, scenario="ssp585", horizon=2060)
        assert "°C" in far.climate["label"] and "days above 35°C (95°F)" in far.climate["label"]
        change = far.climate["yield_change"]
        assert change and all(v["low"] <= v["pct"] <= v["high"] for v in change.values())
        assert all(p.numbers["irrigation_mm"] <= 609.6 + 1 for p in far.plans)
        for plan in far.plans:
            assert "earn" not in plan.reason.lower() and " mm " not in plan.reason
    # the thirstier the crop, the more often the limit leaves it short; and more often in a hotter future
    today, later = engine.limit_binds("ssp245", 2025), engine.limit_binds("ssp585", 2060)
    assert today["alfalfa"] > today["corn"] > today["sorghum"] and later["alfalfa"] > today["alfalfa"]
    assert later["corn"] > today["corn"]
    # no limit when the environment says so: the engine is rebuilt and alfalfa pumps more than 24 inches
    monkeypatch.setenv("IRRIGATION_LIMIT_MM", "none")
    free = smart.get_engine()
    assert free is not engine and free.irrigation_limit_mm == 0.0
    cycles = free.candidate_set(True).cycles
    i = cycles.index((A, A, A, A))
    assert free.evaluate(True, "ssp585", 2060).irrigation[i].mean() > 609.6 > engine.evaluate(True, "ssp585", 2060).irrigation[i].mean()


@needs_climate
@needs_fields
def test_sanity_script_runs_and_writes_only_where_told(real_settings, tmp_path, capsys):
    from api.planner import sanity

    code = sanity.main(["--report", str(tmp_path / "sanity.md"), "--json", str(tmp_path / "sanity.json"), "--no-cache-write"])
    out = capsys.readouterr().out
    assert code in (0, 1) and len(out.splitlines()) <= 8
    report = json.loads((tmp_path / "sanity.json").read_text())
    assert set(report["checks"]) == {"dryland_heat", "irrigation", "weights", "soil_order", "results", "irrigation_limit"}
    assert len(report["checks"]["irrigation_limit"]["lines"]) >= 6 and report["irrigation_limit_mm"] > 0
    assert all("lead" in line for line in report["checks"]["dryland_heat"]["lines"][:4])     # the raw leads are printed
    assert report["checks"]["results"]["passed"] is True          # rules, 0-1 and determinism must always hold
    assert (tmp_path / "sanity.md").read_text().startswith("# Planner sanity checks")
    assert not (tmp_path / "planner_bare_days.json").exists()


@pytest.mark.skipif(not (DATA / "nass_finney.parquet").is_file(), reason="data/nass_finney.parquet not built")
def test_tracked_prices_are_what_the_nass_table_gives():
    tracked = json.loads((ROOT / "api" / "planner" / "economics.json").read_text())["prices"]
    fresh = money.prices_from_nass(pd.read_parquet(DATA / "nass_finney.parquet"), tuple(tracked["years"]), tracked["statistic"])
    assert {c: v["price"] for c, v in fresh.items()} == {c: v["price"] for c, v in tracked["per_unit"].items()}
