"""Transition path (Phase 4, item 2): api/planner/transition.py and GET /field/{id}/transition.

The path search and the arithmetic run on hand-made histories and numbers; the endpoint runs on the
made-up climate tables of test_planner_smart / test_api_smart, so nothing here needs data/. The last
test uses the real tables and skips without them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from api.planner import candidates as cand  # noqa: E402
from api.planner import custom, smart, transition  # noqa: E402
from api.planner.crops import CROPS  # noqa: E402
from api.planner.legacy import FieldInput  # noqa: E402

import test_api_smart as api_tests  # noqa: E402
from test_api_smart import (  # noqa: E402,F401  (fixtures)
    basic_client, clean_engine, client, fields_file, no_tables, tables,
)
from test_planner_smart import (  # noqa: E402,F401
    A, B, C, F, IRRIGATED_HISTORY, S, W, a_field, engine,
)

DRY, WET = api_tests.DRY, api_tests.WET
RULE = {rule.key: rule for rule in cand.RULES}


def field_with(crops, last_year=2024, field_id="KS-000001"):
    """A field whose last years, ending in `last_year`, are `crops` (oldest first)."""
    first = last_year - len(crops) + 1
    return FieldInput(field_id, {str(first + i): crop for i, crop in enumerate(crops)}, 1.8, 1.0, 60.0)


# --- the rules, one step at a time ---------------------------------------------------------------------

@pytest.mark.parametrize("crop, key", [(W, "no_wheat_after_wheat"), (F, "no_fallow_after_fallow"),
                                       (B, "no_soybeans_after_soybeans")])
def test_a_crop_straight_after_itself(crop, key):
    other = C if crop != C else S
    assert key in transition.step_breaks([other, crop], crop, irrigated=True)
    assert key in transition.step_breaks([other, crop], crop, irrigated=False)
    assert key not in transition.step_breaks([crop, other], crop, irrigated=True)
    assert key not in transition.step_breaks([], crop, irrigated=True)             # nothing known before: nothing broken


def test_an_alfalfa_stand_is_not_ended_before_its_third_year():
    assert transition.step_breaks([C, A], C, True) == ["alfalfa_stand"]
    assert transition.step_breaks([C, A, A], C, True) == ["alfalfa_stand"]
    assert transition.step_breaks([C, A, A, A], C, True) == []
    assert transition.step_breaks([C, A, A], A, True) == []                         # carrying the stand on is fine
    # a stand that reaches back to the start of the record may be older than it looks
    assert transition.step_breaks([A, A], C, True) == []
    assert transition.step_breaks([A, A], C, True, open_start=False) == ["alfalfa_stand"]


@pytest.mark.parametrize("crop", cand.GRAIN_CROPS)
def test_no_third_year_in_a_row_of_corn_or_of_sorghum(crop):
    assert transition.step_breaks([F, crop, crop], crop, True) == ["no_long_grain_run"]
    assert transition.step_breaks([crop, F, crop], crop, True) == []
    other = S if crop == C else C
    assert transition.step_breaks([crop, crop], other, True) == []                   # corn, corn, sorghum is allowed


def test_dryland_rules_involve_the_crops_before():
    for summer in (C, S, B):
        assert "dryland_wheat_after_fallow" in transition.step_breaks([F, summer], W, irrigated=False)
        assert "dryland_wheat_after_fallow" not in transition.step_breaks([F, summer], W, irrigated=True)
    assert transition.step_breaks([S, F], W, False) == []
    assert transition.step_breaks([W, F], A, False) == ["dryland_no_alfalfa"]
    assert transition.step_breaks([W, F], B, False) == ["dryland_no_soybeans"]
    # "one corn year in a cycle": the shortest cycle is three years, so no corn within two years of corn
    assert transition.step_breaks([F, C], C, False) == ["dryland_one_corn_year"]
    assert transition.step_breaks([C, F], C, False) == ["dryland_one_corn_year"]
    assert transition.step_breaks([C, W, F], C, False) == []
    assert transition.step_breaks([F, C], C, True) == []


def test_irrigated_fallow_is_one_year_in_four():
    assert transition.step_breaks([F, C, B], F, True) == ["irrigated_one_fallow_year"]
    assert transition.step_breaks([F, C, B, W], F, True) == []
    assert transition.step_breaks([F, C, B], F, False) == []                         # dryland fallows more often
    assert transition.step_breaks([C, F], F, True) == ["no_fallow_after_fallow", "irrigated_one_fallow_year"]


def test_every_planner_rule_is_read_step_by_step_and_said_in_its_own_words():
    assert set(transition.STEP_RULES) == set(RULE)
    said = transition.rule_sentences(["dryland_wheat_after_fallow", "no_wheat_after_wheat"])
    assert said == [RULE["no_wheat_after_wheat"].gentle, RULE["dryland_wheat_after_fallow"].gentle]      # the planner's order
    assert all(sentence.startswith("Our plans avoid") for sentence in transition.rule_sentences(RULE))


# --- the way in: entry year, bridge years, already there, impossible -----------------------------------

def keys(history, bridge, unit, phase, irrigated):
    return [(n, key) for n, _, key in transition.seam_breaks(history, bridge, unit, phase, irrigated)]


def test_entry_year_must_follow_the_last_real_crop():
    unit = (W, S, F)
    # after wheat: not wheat again; sorghum or fallow can follow
    assert keys([S, F, W], [], unit, 0, False) == [(1, "no_wheat_after_wheat")]
    assert keys([C, F, W], [], unit, 1, False) == [] and keys([C, F, W], [], unit, 2, False) == []
    # after sorghum: dryland wheat cannot follow it; after two sorghum years a third cannot either
    assert keys([W, S], [], unit, 0, False) == [(1, "dryland_wheat_after_fallow")]
    assert keys([W, S], [], unit, 1, False) == []
    assert keys([S, S], [], unit, 1, False) == [(1, "no_long_grain_run")]
    # after fallow: not fallow again
    assert keys([S, F], [], unit, 2, False) == [(1, "no_fallow_after_fallow")]
    assert keys([S, F], [], unit, 0, False) == []


def test_what_the_cycle_itself_breaks_is_not_held_against_the_path():
    # corn, sorghum, wheat, fallow on dryland plants wheat after sorghum every round: the farmer's choice
    unit = (C, S, W, F)
    assert cand.rules_broken(unit, False) != []
    assert all(keys([W, F], [], unit, phase, False) == [] for phase in (0, 1))
    assert keys([F, S], [], unit, 2, False) == []                 # wheat after the real sorghum, as the cycle does
    assert keys([F, C], [], unit, 2, False) == []                 # and after real corn: the same rule, the cycle's own
    # but a break that only the seam makes is counted: fallow after the real fallow
    assert keys([W, F], [], unit, 3, False) == [(1, "no_fallow_after_fallow")]


def test_alfalfa_stand_across_the_seam():
    unit = (A, A, A, C)
    assert keys([C, B], [], unit, 0, True) == []
    assert keys([C, B], [], unit, 1, True) == [(3, "alfalfa_stand")]          # a new stand of two years
    assert keys([C, B], [], unit, 2, True) == [(2, "alfalfa_stand")]
    assert keys([C, B], [], unit, 3, True) == []
    # one real alfalfa year counts towards the stand
    assert keys([C, A], [], unit, 1, True) == []
    assert keys([C, A], [], unit, 2, True) == [(2, "alfalfa_stand")]
    assert keys([C, A], [], unit, 3, True) == [(1, "alfalfa_stand")]
    # a bridge year is checked like any other year
    assert keys([C, B], [A], (C, B), 0, True) == [(2, "alfalfa_stand")]
    assert keys([C, A], [A, A], (C, B), 0, True) == []


def test_path_takes_the_first_valid_entry_year_or_the_better_one():
    found = transition.find_path([S, F], (W, S, F), False)
    assert found == {"status": "path", "bridge": [], "phase": 0, "options": 2, "blocked": []}
    # sorghum can follow fallow too; a value that likes sorghum first picks that start
    likes_sorghum = transition.find_path([S, F], (W, S, F), False, value=lambda crops: (1.0 if crops[0] == S else 0.0,))
    assert likes_sorghum["phase"] == 1 and likes_sorghum["options"] == 2 and likes_sorghum["bridge"] == []
    # after wheat only two of the three years can follow
    after_wheat = transition.find_path([C, F, W], (W, S, F), False)
    assert after_wheat["phase"] == 1 and after_wheat["options"] == 2
    # the value sees exactly the years that will be shown
    seen = []
    transition.find_path([S, F], (W, S, F), False, value=lambda crops: (seen.append(list(crops)), 0.0)[1:], years=5)
    assert seen and all(len(crops) == 5 for crops in seen) and [W, S, F, W, S] in seen and [S, F, W, S, F] in seen


def test_bridge_years_only_when_no_year_of_the_cycle_can_follow():
    # a one-year-old alfalfa stand under irrigation: two more alfalfa years, then corn-soybeans
    two = transition.find_path([C, B, A], (C, B), True)
    assert two["status"] == "path" and two["bridge"] == [A, A] and two["blocked"] == ["alfalfa_stand"]
    one = transition.find_path([C, A, A], (C, B), True)
    assert one["bridge"] == [A] and one["options"] == 2            # then corn or soybeans may start
    none = transition.find_path([A, A, A], (C, B), True)
    assert none["bridge"] == [] and none["blocked"] == []
    # fewest bridge years first, whatever a longer way would be worth
    greedy = transition.find_path([C, A, A], (C, B), True, value=lambda crops: (float(crops.count(A)),))
    assert greedy["bridge"] == [A]
    # a bridge that is not alfalfa: dryland wheat every year cannot follow sorghum, so one fallow year first
    wheat = transition.find_path([F, S], (W,), False)
    assert wheat["bridge"] == [F] and wheat["phase"] == 0 and wheat["options"] == 1
    assert wheat["blocked"] == ["dryland_wheat_after_fallow"]
    # never more than two
    assert transition.MAX_BRIDGE_YEARS == 2
    for history, unit, irrigated in ([C, B, A], (C, B), True), ([F, S], (W,), False), ([C, F, W], (W, S, F), False):
        assert len(transition.find_path(history, unit, irrigated)["bridge"]) <= 2


def test_already_in_the_cycle():
    unit = (W, S, F)
    assert transition.already_in([W, S, F, W, S], unit) == 2          # fallow comes next
    assert transition.already_in([C, C, S, F, W], unit) == 1          # the last three years are enough
    assert transition.already_in([C, C, C, F, W], unit) is None
    assert transition.already_in([F, W], unit) is None                # fewer than three years on record
    assert transition.already_in([B, C, B], (C, B)) == 0              # a two-year cycle still needs three years
    assert transition.already_in([C, C, B], (C, B)) is None
    assert transition.already_in([C, C, C], (C,)) == 0
    assert transition.already_in([W, C, C, B, W, C, C, B], (C, C, B, W)) == 3
    there = transition.find_path([S, F, W, S, F], unit, False)
    assert there == {"status": "already_there", "bridge": [], "phase": 0, "options": 1, "blocked": []}


def test_no_path_within_two_bridge_years_says_what_is_in_the_way():
    # a one-year-old alfalfa stand on dryland: it cannot be ended, and it cannot be kept
    stuck = transition.find_path([W, S, A], (W, S, F), False)
    assert stuck["status"] == "no_path" and stuck["phase"] is None and stuck["bridge"] == []
    assert stuck["blocked"] == ["alfalfa_stand", "dryland_no_alfalfa"]
    # the same field treated as irrigated has a way: two more alfalfa years
    assert transition.find_path([W, S, A], (W, S, F), True)["bridge"] == [A, A]


# --- carry on as now ---------------------------------------------------------------------------------

@pytest.mark.parametrize("history, pattern, repeating", [
    ([C, C, C, C], (C,), True),
    ([B, C, B, C, B], (C, B), True),                 # next year is corn
    ([F, W, S, F, W], (S, F, W), True),              # next year is sorghum
    ([S, F, W, S, S, F], (W, S, S, F), True),        # a 4-year pattern the two years before confirm
    ([C, W, S, S, F], (W, S, S, F), False),          # the year before does not fit: repeated as they are
    ([W, S, S, F], (W, S, S, F), False),             # nothing before to confirm it
    ([C, B, W], None, False),                        # fewer than four years
])
def test_carry_on_pattern(history, pattern, repeating):
    assert transition.carry_on_pattern(history) == (pattern, repeating)
    if pattern is not None:        # continued, it runs straight on from the last real years
        run = list(history) + transition.cycle_from(pattern, 0, 8)
        period = len(pattern)
        assert all(run[i] == run[i - period] for i in range(len(history), len(run)))


def test_real_years_stop_at_a_gap_or_a_crop_we_have_no_numbers_for():
    run, shown = transition.real_years({"2019": C, "2020": "cotton", "2021": W, "2022": S, "2024": F, "x": C})
    assert run == [(2024, F)] and shown == [(2022, S), (2024, F)]
    run, shown = transition.real_years({"2021": W, "2022": S, "2023": F, "2024": "Winter Wheat "})
    assert run == [(2021, W), (2022, S), (2023, F), (2024, W)] and [y for y, _ in shown] == [2022, 2023, 2024]
    run, shown = transition.real_years({"2023": F, "2024": "grass/pasture"})
    assert run == [] and shown == [(2023, F), (2024, "grass/pasture")]
    assert transition.real_years({}) == ([], []) and transition.real_years(None) == ([], [])


# --- the arithmetic, on hand-made numbers ----------------------------------------------------------------

@pytest.mark.parametrize("cumulative, status, n, lowest", [
    ([0, 0, 0], "no_difference", None, 1),
    ([5, 0, 3], "never_behind", None, 2),
    ([-10, -5, 0, 4], "paid_back", 3, 1),            # level counts as paid back
    ([-10, 5, -2, 3], "paid_back", 4, 1),            # it dipped again in year 3: paid back only once it stays up
    ([-10, 5, -2], "not_within_span", None, 1),
    ([3, -8, -1], "not_within_span", None, 2),
])
def test_payback_is_the_year_the_running_total_stays_at_or_above_zero(cumulative, status, n, lowest):
    back = transition.payback(cumulative)
    assert (back["status"], back["n"], back["lowest"]["n"]) == (status, n, lowest)
    assert back["lowest"]["value"] == min(cumulative)


class HandEngine:
    """Two climate models and numbers set by hand: {(crop, kind of previous crop): [model 1, model 2]}."""

    PROFIT = {(W, "fallow"): [30.0, 50.0], (W, "summer"): [-20.0, 0.0], (W, "wheat"): [0.0, 10.0],
              (S, "fallow"): [100.0, 140.0], (S, "wheat"): [60.0, 100.0], (S, "summer"): [20.0, 60.0],
              (F, "fallow"): [-14.0, -14.0], (F, "wheat"): [-14.0, -14.0], (F, "summer"): [-14.0, -14.0]}
    BARE = {W: 50.0, S: 60.0, F: 130.0}

    def crop_year(self, irrigated, scenario, horizon, crop, prev):
        kind = {W: "wheat", F: "fallow"}.get(prev, "summer")
        planted = crop != F
        return {"profit": np.array(self.PROFIT[(crop, kind)]), "yield": np.array([30.0, 40.0] if planted else [0.0, 0.0]),
                "irrigation_mm": np.array([254.0, 508.0] if irrigated and planted else [0.0, 0.0]),
                "water_met": np.array([0.5, 0.7]) if planted else None,
                "fail_prob": np.array([0.1, 0.3]) if planted else None, "bare_days": self.BARE[crop]}


def hand_rows(crops, first_prev, irrigated=False, role="plan"):
    return transition.score_years(HandEngine(), crops, first_prev, irrigated, "ssp245", 2025, 2025,
                                  [role] * len(crops), [None] * len(crops))


def test_each_year_is_the_crop_after_the_crop_before_it():
    rows, profit = hand_rows([W, S, F, W], first_prev=S, irrigated=True)
    assert [row["after"] for row in rows] == [S, W, S, F] and [row["year"] for row in rows] == [2025, 2026, 2027, 2028]
    assert [row["n"] for row in rows] == [1, 2, 3, 4]
    # wheat after the real sorghum, sorghum after wheat, fallow, wheat after fallow: mean, lowest and highest model
    assert [row["return"] for row in rows] == [{"mid": -10, "low": -20, "high": 0}, {"mid": 80, "low": 60, "high": 100},
                                               {"mid": -14, "low": -14, "high": -14}, {"mid": 40, "low": 30, "high": 50}]
    assert profit.shape == (4, 2) and profit[3].tolist() == [30.0, 50.0]
    wheat, fallow = rows[0], rows[2]
    assert wheat["yield"] == 35.0 and wheat["yield_unit"] == "bu/acre" and wheat["bare_days"] == 50.0
    assert wheat["irrigation_mm"] == 381 and wheat["irrigation_in"] == 15.0
    assert wheat["water_met_share"] == 0.6 and wheat["fail_chance"] == 0.2
    # a fallow year has no crop to water, to yield or to fail
    assert fallow["yield"] is None and fallow["water_met_share"] is None and fallow["fail_chance"] is None
    assert fallow["irrigation_in"] == 0 and fallow["bare_days"] == 130.0


def test_difference_is_paired_by_climate_model_and_adds_up():
    path_rows, path_profit = hand_rows([F, W, S], first_prev=S)          # -14 | 30, 50 | 60, 100
    carry_rows, carry_profit = hand_rows([S, F, W], first_prev=S, role="carry_on")     # 20, 60 | -14 | 30, 50
    diff = transition.difference(path_rows, path_profit, carry_rows, carry_profit, irrigated=False)
    years = diff["years"]
    # year 1: -14 against 20 and 60 -> -34 and -74 in the two models
    assert years[0]["return"] == {"mid": -54, "low": -74, "high": -34}
    # year 2: 30 and 50 against -14 -> +44 and +64; running total -34+44 = 10 and -74+64 = -10
    assert years[1]["return"] == {"mid": 54, "low": 44, "high": 64}
    assert years[1]["cumulative"] == {"mid": 0, "low": -10, "high": 10}
    # year 3: 60 and 100 against 30 and 50 -> +30 and +50; running total 40 and 40
    assert years[2]["return"] == {"mid": 40, "low": 30, "high": 50}
    assert years[2]["cumulative"] == {"mid": 40, "low": 40, "high": 40} == diff["total"]
    for row, p, c in zip(years, path_rows, carry_rows):
        assert row["return"]["mid"] == p["return"]["mid"] - c["return"]["mid"]           # the three figures add up as shown
    assert [row["bare_days"] for row in years] == [70.0, -80.0, 10.0]
    assert [row["cumulative_bare_days"] for row in years] == [70.0, -10.0, 0.0]
    assert "irrigation_in" not in years[0]
    back = diff["payback"]
    assert back["status"] == "paid_back" and back["n"] == 2 and back["year"] == 2026
    assert back["lowest"] == {"n": 1, "value": -54, "year": 2025}
    assert back["models_ahead_at_end"] == 2 and back["models"] == 2
    assert transition.total_sentence(diff) == (
        "After 3 years this path is about $40 an acre ahead of carrying on as now (the same in every climate model); "
        "it is level or ahead in 2 of the 2 climate models.")
    assert transition.short_term_sentence(diff, path_rows) == (
        "Short term: at its lowest point, after 2025 (year 1), this path is about $54 an acre behind carrying on as "
        "now (−$74 to −$34 across the climate models).")


def test_irrigated_difference_carries_the_water_pumped():
    path_rows, path_profit = hand_rows([F, W], first_prev=S, irrigated=True)
    carry_rows, carry_profit = hand_rows([S, S], first_prev=S, irrigated=True)
    diff = transition.difference(path_rows, path_profit, carry_rows, carry_profit, irrigated=True)
    assert [row["irrigation_in"] for row in diff["years"]] == [-15.0, 0.0]
    assert diff["payback"]["status"] == "not_within_span" and diff["payback"]["year"] is None
    assert diff["payback"]["models_ahead_at_end"] == 0
    # year 1: -14 against 20 and 60; year 2: 30 and 50 against 20 and 60 -> running total -24 and -84
    assert diff["total"] == {"mid": -54, "low": -84, "high": -24}
    assert transition.short_term_sentence(diff, path_rows) == (
        "Short term: this path falls behind carrying on as now and has not caught up within the 2 years; at its "
        "lowest point, after 2025 (year 1), it is about $54 an acre behind (−$74 to −$34 across the climate models).")


def made_up_run(mid, low, high, verdict, ahead=(2, 0), bare=0.0, inches=None):
    d = {"return": {"mid": mid, "low": low, "high": high}, "verdict": verdict, "bare_days": bare,
         "models": {"plan_ahead": ahead[0], "carry_on_ahead": ahead[1], "total": sum(ahead)}}
    if inches is not None:
        d["irrigation_in"] = inches
    return {"difference": d}


def made_up_diff(status, n=None, year=None, span=8):
    return {"years": [{}] * span, "payback": {"status": status, "n": n, "year": year}}


def test_long_term_sentence_names_a_winner_only_when_every_model_agrees():
    said = transition.long_term_sentence(made_up_run(23, 20, 27, "plan", bare=49.6),
                                         made_up_diff("paid_back", 4, 2028), irrigated=False)
    assert said == ("Long term: once established, the plan returns about $23 an acre a year more than carrying on as "
                    "now ($20 to $27 across the climate models), and the switch has paid back by 2028 (year 4); it "
                    "also leaves the soil bare about 50 more days a year.")
    less = transition.long_term_sentence(made_up_run(-27, -34, -15, "carry_on", ahead=(0, 2), bare=-12.0, inches=-3.1),
                                         made_up_diff("not_within_span"), irrigated=True, what="your rotation")
    assert less == ("Long term: once established, your rotation returns about $27 an acre a year less than carrying "
                    "on as now (−$34 to −$15 across the climate models), so on these numbers the switch does not pay "
                    "back in dollars; it also leaves the soil bare about 12 fewer days a year and pumps about 3.1 "
                    "inches less water a year.")
    close = transition.long_term_sentence(made_up_run(-6, -20, 7, "too_close", ahead=(1, 5)),
                                          made_up_diff("not_within_span"), irrigated=False)
    assert close == ("Long term: once established, returns are too close to call: the plan returns more in 1 of the 6 "
                     "climate models, carrying on as now in 5 (the difference runs from −$20 to $7 an acre a year), "
                     "and the switch has not paid back within these 8 years.")
    never = transition.long_term_sentence(made_up_run(26, 22, 31, "plan", bare=0.4), made_up_diff("never_behind"), False)
    assert never.endswith("and this path is never behind, so there is nothing to pay back.")
    for sentence in (said, less, close, never):
        assert "profit" not in sentence and "income" not in sentence and "soil health" not in sentence


def test_bridge_years_are_named_in_the_short_term_sentence():
    rows = [{"role": "bridge", "crop": A, "year": 2025}, {"role": "bridge", "crop": A, "year": 2026},
            {"role": "plan", "crop": C, "year": 2027}]
    assert transition.bridge_words(rows) == "2 bridge years (alfalfa in 2025 and 2026)"
    assert transition.bridge_words(rows[1:]) == "1 bridge year (alfalfa in 2026)"
    assert transition.bridge_words(rows[2:]) == ""
    mixed = [{"role": "bridge", "crop": F, "year": 2025}, {"role": "bridge", "crop": S, "year": 2026}]
    assert transition.bridge_words(mixed) == "2 bridge years (fallow in 2025 and sorghum in 2026)"
    diff = {"years": [{}] * 6, "payback": {"status": "never_behind", "lowest": {"n": 1}}}
    assert transition.short_term_sentence(diff, rows) == (
        "Short term: the switch takes 2 bridge years (alfalfa in 2025 and 2026) before the plan starts, and no cost "
        "shows in these numbers, as this path is level with or ahead of carrying on as now in every one of the 6 years.")


# --- transition_path on the made-up engine -------------------------------------------------------------

def path(engine, field, **kwargs):
    return transition.transition_path(field, engine=engine, **kwargs)


def test_answer_has_the_documented_shape_and_the_plans_settings(engine):
    field = field_with([C, C, S, F, W, S, S])                    # dryland; no repeating pattern in the last four years
    body = path(engine, field, plan=2, years=6, scenario="ssp585", horizon=2040, irrigated=False)
    plans = smart.rank_plans_smart(field, scenario="ssp585", horizon=2040, irrigated=False, engine=engine).to_dict()
    assert set(body) == {"field_id", "engine", "settings", "years", "target", "history", "status", "summary", "path",
                         "blocked", "carry_on", "carry_on_note", "difference", "long_run", "sentences", "not_modelled",
                         "notes"}
    assert body["settings"] == plans["settings"] and body["engine"] == "smart" and body["years"] == 6
    assert body["settings"]["window"] == [2030, 2049] and "2030–2049" in body["notes"][0]
    assert body["target"] == {"source": "plan", "plan": 2, "name": plans["plans"][1]["name"],
                              "cycle": plans["plans"][1]["cycle"], "rules_broken": []}
    assert body["history"] == [{"year": 2022, "crop": W}, {"year": 2023, "crop": S}, {"year": 2024, "crop": S}]
    assert body["status"] == "path" and body["summary"]
    years = body["path"]["years"]
    assert len(years) == 6 and [row["year"] for row in years] == list(range(2025, 2031))
    assert set(years[0]) == {"n", "year", "crop", "after", "role", "cycle_year", "return", "yield", "yield_unit",
                             "irrigation_mm", "irrigation_in", "water_met_share", "bare_days", "fail_chance"}
    assert years[0]["after"] == S and all(row["after"] == before["crop"] for before, row in zip(years, years[1:]))
    json.dumps(body)                                             # plain JSON all the way down
    text = json.dumps({key: value for key, value in body.items() if key != "settings"}, ensure_ascii=False).lower()
    assert not any(word in text for word in ("profit", "income", "soil health", "improves the soil"))


def test_every_year_is_the_engines_own_number_in_the_chosen_climate_window(engine):
    field = field_with([C, C, S, F, W, S, S])
    for horizon, scenario in ((2025, "ssp245"), (2050, "ssp585")):
        body = path(engine, field, target=[W, S, F], years=4, horizon=horizon, scenario=scenario, irrigated=False)
        for row in body["path"]["years"] + body["carry_on"]["years"]:
            y = engine.crop_year(False, scenario, horizon, row["crop"], row["after"])
            assert row["return"] == {"mid": round(float(y["profit"].mean())), "low": round(float(y["profit"].min())),
                                     "high": round(float(y["profit"].max()))}
            assert row["bare_days"] == round(y["bare_days"], 1)
            if row["crop"] == F:
                assert row["fail_chance"] is None and row["water_met_share"] is None and row["return"]["mid"] < 0
            else:
                assert row["fail_chance"] == round(float(y["fail_prob"].mean()), 3)
                assert row["water_met_share"] == round(float(y["water_met"].mean()), 3)
                assert row["return"]["low"] <= row["return"]["mid"] <= row["return"]["high"]
    hot = path(engine, field, target=[W, S, F], years=4, horizon=2050, scenario="ssp585", irrigated=False)
    now = path(engine, field, target=[W, S, F], years=4, irrigated=False)
    assert [r["return"] for r in hot["path"]["years"]] != [r["return"] for r in now["path"]["years"]]


def test_crop_year_averages_back_to_the_plan_cards_numbers(engine):
    cycle = (W, S, F)
    ev = engine.evaluate(False, "ssp585", 2040, rotations=[cycle])
    years = [engine.crop_year(False, "ssp585", 2040, cycle[i], cycle[i - 1]) for i in range(3)]
    assert np.allclose(np.mean([y["profit"] for y in years], axis=0), ev.profit[0])
    assert np.mean([y["bare_days"] for y in years]) == pytest.approx(float(ev.bare[0]))
    planted = [y for y in years if y["fail_prob"] is not None]
    assert np.allclose(np.mean([y["fail_prob"] for y in planted], axis=0), ev.failed[0])
    wet = engine.crop_year(True, "ssp245", 2025, C, B)
    assert wet["irrigation_mm"].mean() > 0 and engine.crop_year(False, "ssp245", 2025, C, B)["irrigation_mm"].max() == 0


def test_difference_running_total_and_long_run_agree_with_the_years(engine):
    field = field_with([C, C, S, F, W, S, S])
    body = path(engine, field, target=[W, F], years=8, irrigated=False)
    assert body["carry_on"]["pattern"] == [F, W, S, S] and body["carry_on"]["repeating"] is False
    assert body["carry_on"]["basis_years"] == [2021, 2024]
    assert body["carry_on_note"] == ('The last four years (2021–2024) are not a repeating pattern, so "carry on as '
                                     'now" repeats those four years as they are.')
    assert [row["crop"] for row in body["carry_on"]["years"]] == [F, W, S, S, F, W, S, S]
    running = 0.0
    for d, p, c in zip(body["difference"]["years"], body["path"]["years"], body["carry_on"]["years"]):
        assert d["return"]["mid"] == p["return"]["mid"] - c["return"]["mid"]
        assert d["bare_days"] == pytest.approx(p["bare_days"] - c["bare_days"], abs=0.051)
        running += d["return"]["mid"]
        assert abs(d["cumulative"]["mid"] - running) <= d["n"]          # only rounding apart
        assert d["cumulative"]["low"] <= d["cumulative"]["mid"] <= d["cumulative"]["high"]
    assert body["difference"]["total"] == body["difference"]["years"][-1]["cumulative"]
    total = body["path"]["total_return"]["mid"] - body["carry_on"]["total_return"]["mid"]
    assert abs(body["difference"]["total"]["mid"] - total) <= 1
    # the long run: each rotation as a cycle, as the plan cards score it
    run = body["long_run"]
    target = engine.evaluate(False, rotations=[custom.normal_form((W, F))], slope_pct=field.slope_pct)
    assert run["plan"]["return"]["mid"] == round(float(target.profit[0].mean()))
    assert run["plan"]["bare_days"] == round(float(target.bare[0]), 1)
    assert run["difference"]["return"]["mid"] == run["plan"]["return"]["mid"] - run["carry_on"]["return"]["mid"]
    assert run["difference"]["bare_days"] == pytest.approx(run["plan"]["bare_days"] - run["carry_on"]["bare_days"], abs=0.051)
    assert run["difference"]["verdict"] in ("plan", "carry_on", "too_close", "same")
    models = run["difference"]["models"]
    assert models["plan_ahead"] + models["carry_on_ahead"] <= models["total"] == len(engine.climate.models)
    assert body["sentences"]["short_term"].startswith("Short term: ")
    assert body["sentences"]["long_term"].startswith("Long term: once established, your rotation ")
    assert body["difference"]["total_sentence"].startswith("After 8 years ")


def test_a_plan_card_and_its_path_show_the_same_long_run_numbers(engine):
    field = a_field(IRRIGATED_HISTORY)
    plans = smart.rank_plans_smart(field, irrigated=True, engine=engine).to_dict()["plans"]
    for n, card in enumerate(plans, start=1):
        run = path(engine, field, plan=n, irrigated=True)["long_run"]["plan"]
        assert run["return"]["mid"] == card["numbers"]["profit_per_acre"]
        assert run["return"]["low"] == card["spread"]["profit_per_acre"]["low"]
        assert run["return"]["high"] == card["spread"]["profit_per_acre"]["high"]
        assert run["bare_days"] == card["numbers"]["bare_days"] and run["irrigation_in"] == card["numbers"]["irrigation_in"]
        assert run["failed_share"] == card["numbers"]["failed_share"]


def test_already_there_says_so_and_shows_the_next_years(engine):
    field = a_field()                                    # wheat, sorghum, fallow since 2008; 2024 is sorghum
    body = path(engine, field, target=["sorghum", "fallow", "winter wheat"])
    assert body["status"] == "already_there" and body["path"]["already_there"] is True
    assert body["summary"] == ("This field is already in this rotation: its last 3 years (2022–2024) follow it. "
                               "These are its next years.")
    assert [row["crop"] for row in body["path"]["years"]] == [F, W, S, F, W, S, F, W]
    assert body["path"]["bridge_years"] == 0 and all(row["role"] == "plan" for row in body["path"]["years"])
    assert body["path"]["entry"] == {"n": 1, "year": 2025, "crop": F, "cycle_year": 2}     # year 2 of sorghum, fallow, wheat
    assert [row["cycle_year"] for row in body["path"]["years"][:4]] == [2, 3, 1, 2]
    # carrying on as now is the same thing: no difference in any year
    assert [row["crop"] for row in body["carry_on"]["years"]] == [row["crop"] for row in body["path"]["years"]]
    assert all(d["return"] == {"mid": 0, "low": 0, "high": 0} == d["cumulative"] for d in body["difference"]["years"])
    assert body["difference"]["payback"]["status"] == "no_difference"
    assert body["sentences"] == {"short_term": transition.ALREADY_SHORT_TERM, "long_term": transition.ALREADY_LONG_TERM}
    assert body["long_run"]["difference"]["verdict"] == "same"
    # even when the year before the last three breaks the pattern, the field carries on with the cycle
    odd = path(engine, field_with([C, C, C, F, W, S]), target=[W, S, F], irrigated=False)
    assert odd["status"] == "already_there" and odd["carry_on"]["pattern"] == [F, W, S] and odd["carry_on_note"] is None


def test_bridge_years_are_marked_and_scored(engine):
    field = field_with([C, C, B, C, B, A])              # irrigated, a one-year-old alfalfa stand
    body = path(engine, field, target=[C, B], irrigated=True)
    assert body["status"] == "path" and body["path"]["bridge_years"] == 2
    years = body["path"]["years"]
    assert [(row["crop"], row["role"], row["cycle_year"]) for row in years[:4]] == [
        (A, "bridge", None), (A, "bridge", None), (years[2]["crop"], "plan", years[2]["cycle_year"]),
        (years[3]["crop"], "plan", years[3]["cycle_year"])]
    assert {years[2]["crop"], years[3]["crop"]} == {C, B}
    assert body["path"]["entry"]["n"] == 3 and body["path"]["entry"]["year"] == 2027
    assert body["blocked"] == [RULE["alfalfa_stand"].gentle]
    assert "2 bridge years (alfalfa in 2025 and 2026)" in body["summary"]
    assert "2 bridge years (alfalfa in 2025 and 2026)" in body["sentences"]["short_term"]
    assert years[0]["return"]["mid"] == round(float(engine.crop_year(True, "ssp245", 2025, A, A)["profit"].mean()))
    # the best start after the bridge is the one that returns the most over the span
    other = [C, B] if years[2]["crop"] == B else [B, C]
    alt_rows, alt = transition.score_years(engine, [A, A] + (other * 3), A, True, "ssp245", 2025, 2025, [""] * 8, [None] * 8)
    assert body["path"]["total_return"]["mid"] >= round(float(alt.sum(axis=0).mean()))


def test_no_path_is_said_plainly_with_the_rules_in_the_way(engine):
    field = field_with([W, S, F, W, S, A])
    body = path(engine, field, target=[W, S, F], irrigated=False)
    assert body["status"] == "no_path" and body["path"] is None and body["difference"] is None
    assert body["sentences"] is None
    assert body["summary"] == ("We found no way from this field's last crops (alfalfa in 2024) into this rotation "
                               "within 2 bridge years that keeps to what our plans avoid.")
    assert body["blocked"] == [RULE["alfalfa_stand"].gentle, RULE["dryland_no_alfalfa"].gentle]
    assert body["carry_on"] is not None and body["long_run"] is not None       # the two can still be compared as cycles


def test_a_last_year_we_have_no_numbers_for_gives_no_path_and_says_why(engine):
    field = FieldInput("KS-000001", {"2021": W, "2022": S, "2023": F, "2024": "grass/pasture"}, 1.8, 1.0, 60.0)
    body = path(engine, field, plan=1)
    assert body["status"] == "no_history" and body["path"] is None and body["carry_on"] is None
    assert body["summary"] == ("The last year on record (2024) is grass/pasture, which the planner has no numbers "
                               "for, so we cannot work out a path from it.")
    assert body["history"][-1] == {"year": 2024, "crop": "grass/pasture"}
    empty = path(engine, FieldInput("KS-000001", {}, 1.8, 1.0, 60.0), plan=1)
    assert empty["status"] == "no_history" and empty["history"] == [] and "No crop history" in empty["summary"]


def test_fewer_than_four_real_years_gives_a_path_without_a_comparison(engine):
    body = path(engine, field_with(["cotton", F, W]), target=[W, S, F], irrigated=False)
    assert body["status"] == "path" and body["path"]["years"][0]["crop"] in (S, F)
    assert body["carry_on"] is None and body["difference"] is None and body["long_run"] is None and body["sentences"] is None
    assert body["carry_on_note"].startswith("Fewer than four years in a row of the six crops are on record")


def test_what_is_not_modelled_is_listed_and_a_new_alfalfa_stand_adds_its_line(engine):
    plain = path(engine, field_with([C, C, B, C]), target=[C, B], irrigated=True)
    assert plain["not_modelled"] == list(transition.NOT_MODELLED)
    assert any("Equipment" in line and "contracts" in line for line in plain["not_modelled"])
    stand = path(engine, field_with([C, C, B, C]), target=[A, A, A, A, C, W], irrigated=True)
    assert stand["not_modelled"][0] == transition.ALFALFA_NOT_MODELLED and "first year of a new alfalfa stand" in stand["not_modelled"][0]
    assert stand["not_modelled"][1:] == list(transition.NOT_MODELLED)


def test_the_farmers_own_rule_breaking_rotation_still_gets_a_path_and_is_told_what_it_breaks(engine):
    body = path(engine, a_field(), target=[C, S, W, F], irrigated=False)
    assert body["status"] == "path" and body["target"]["source"] == "own" and body["target"]["plan"] is None
    assert body["target"]["name"] == custom.cycle_name((C, S, W, F))
    assert body["target"]["rules_broken"] == cand.rules_broken((C, S, W, F), False, gentle=True)
    assert body["blocked"] == []


def test_default_is_plan_one_and_the_span_is_four_to_twelve_years(engine):
    field = a_field()
    assert path(engine, field)["target"]["plan"] == 1 and path(engine, field)["years"] == 8
    assert len(path(engine, field, years=4)["path"]["years"]) == 4
    assert len(path(engine, field, years=12)["path"]["years"]) == 12
    for bad in (3, 13, 0, True, 8.0, "8"):
        with pytest.raises(transition.TransitionError, match="whole number from 4 to 12"):
            path(engine, field, years=bad)
    with pytest.raises(transition.TransitionError, match="not both"):
        path(engine, field, target=[C, B], plan=1)
    with pytest.raises(transition.TransitionError, match="number from 1 to 3"):
        path(engine, field, plan=4)
    with pytest.raises(custom.RotationError):
        path(engine, field, target=["cotton", W])
    with pytest.raises(ValueError):
        path(engine, field, scenario="rcp85")
    assert issubclass(transition.TransitionError, ValueError)


def test_dryland_answers_carry_the_money_caution(engine):
    dry = path(engine, a_field(), plan=2, irrigated=False)
    wet = path(engine, a_field(IRRIGATED_HISTORY), plan=2, irrigated=True)
    assert transition.DRYLAND_MONEY_NOTE in dry["notes"] and transition.DRYLAND_MONEY_NOTE not in wet["notes"]
    assert transition.LONG_RUN_NOTE in dry["notes"] and transition.ROUNDING_NOTE in dry["notes"]
    assert "irrigation_in" in wet["difference"]["years"][0] and "irrigation_in" not in dry["difference"]["years"][0]


# --- the endpoint --------------------------------------------------------------------------------------

def test_endpoint_defaults_to_our_first_plan_with_the_plans_settings(client):
    params = {"horizon": 2050, "scenario": "ssp585", "water": 4}
    response = client.get(f"/field/{DRY}/transition", params=params)
    assert response.status_code == 200
    body = response.json()
    plans = client.get(f"/field/{DRY}/plans", params=params).json()
    assert body["field_id"] == DRY and body["engine"] == "smart" and body["settings"] == plans["settings"]
    assert body["target"]["plan"] == 1 and body["target"]["cycle"] == plans["plans"][0]["cycle"]
    assert body["years"] == 8 and body["status"] in ("path", "already_there")
    second = client.get(f"/field/{DRY}/transition", params={**params, "plan": 2, "years": 5}).json()
    assert second["target"]["name"] == plans["plans"][1]["name"] and len(second["path"]["years"]) == 5


def test_endpoint_takes_a_typed_target_and_the_irrigation_switch(client):
    body = client.get(f"/field/{DRY}/transition", params={"target": "Corn, soy", "irrigated": "true", "years": 4}).json()
    assert body["target"] == {"source": "own", "plan": None, "name": "Corn – Soybeans (2-year cycle)",
                              "cycle": ["corn", "soybeans"], "rules_broken": []}
    assert body["settings"]["irrigated"] is True and body["settings"]["irrigation_source"] == "override"
    assert all(row["irrigation_in"] > 0 for row in body["path"]["years"])


@pytest.mark.parametrize("params, said", [
    ({"years": 3}, "whole number from 4 to 12"),
    ({"years": 13}, "whole number from 4 to 12"),
    ({"plan": 4}, "The plan must be a number from 1 to 3."),
    ({"plan": 0}, "The plan must be a number from 1 to 3."),
    ({"plan": 1, "target": "corn, soy"}, "Give either a target rotation or a plan number, not both."),
    ({"target": "cotton, wheat"}, 'We do not have numbers for "cotton".'),
    ({"target": "wheat"}, "A rotation needs 2 to 6 years"),
    ({"target": ""}, "Give one crop for each year"),
    ({"horizon": 1990}, "The year must be between"),
    ({"scenario": "nonsense"}, "The scenario must be"),
    ({"money": -1}, "The money priority must be"),
])
def test_endpoint_answers_400_with_one_plain_sentence(client, params, said):
    response = client.get(f"/field/{DRY}/transition", params=params)
    assert response.status_code == 400
    assert said in response.json()["detail"] and "Traceback" not in response.text


def test_endpoint_unknown_field_is_404_and_basic_planner_is_503(client, monkeypatch):
    assert client.get("/field/KS-999999/transition").status_code == 404
    monkeypatch.setenv("PLANNER_ENGINE", "basic")
    response = client.get(f"/field/{DRY}/transition")
    assert response.status_code == 503 and response.json()["detail"] == api_tests.main.TRANSITION_NEEDS_CLIMATE


def test_endpoint_is_503_with_a_plain_reason_without_the_climate_tables(basic_client):
    response = basic_client.get(f"/field/{DRY}/transition", params={"plan": 1})
    assert response.status_code == 503 and "future-weather" in response.json()["detail"]
    assert basic_client.get(f"/field/{DRY}/plans").json()["engine"] == "basic"       # the rest still works


def test_existing_endpoints_are_untouched_by_the_new_one(client):
    plans = client.get(f"/field/{DRY}/plans").json()
    assert plans["engine"] == "smart" and len(plans["plans"]) == 3
    assert client.get(f"/field/{DRY}/score", params={"rotation": "corn, wheat"}).status_code == 200


@api_tests.needs_real
def test_real_tables_give_a_real_field_a_path(monkeypatch):
    for var in list(api_tests.ENV_PATHS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PLANNER_ENGINE", raising=False)
    monkeypatch.delenv("FIELDS_PATH", raising=False)
    smart.reset()
    from fastapi.testclient import TestClient

    with TestClient(api_tests.main.create_app()) as c:
        if c.get("/health").json()["is_fake"]:
            pytest.skip("real fields not loaded")
        seen = set()
        for field_id in ("KS-004139", "KS-014376", "KS-012807", "KS-013847"):
            if c.get(f"/field/{field_id}").status_code != 200:
                continue
            plans = c.get(f"/field/{field_id}/plans", params={"horizon": 2050}).json()
            for n, card in enumerate(plans["plans"], start=1):
                body = c.get(f"/field/{field_id}/transition", params={"plan": n, "horizon": 2050}).json()
                seen.add(body["status"])
                assert body["settings"] == plans["settings"] and body["target"]["cycle"] == card["cycle"]
                if body["path"] is None:
                    assert body["status"] in ("no_history", "no_path") and body["summary"]
                    continue
                assert len(body["path"]["years"]) == 8 and body["path"]["bridge_years"] <= 2
                for row in body["path"]["years"]:
                    assert row["crop"] in CROPS and row["return"]["low"] <= row["return"]["mid"] <= row["return"]["high"]
                if body["long_run"] is not None:
                    assert body["long_run"]["plan"]["return"]["mid"] == card["numbers"]["profit_per_acre"]
                    assert body["long_run"]["plan"]["bare_days"] == card["numbers"]["bare_days"]
                    assert body["long_run"]["difference"]["models"]["total"] == 6
        assert seen
