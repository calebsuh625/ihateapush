"""Build-your-own rotation (Phase 4, item 1): api/planner/custom.py and GET /field/{id}/score.

Runs on the made-up climate tables of test_planner_smart / test_api_smart, so it never needs data/.
The last test uses the real tables and skips without them.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from api.planner import candidates as cand  # noqa: E402
from api.planner import config, custom, smart, soil_effect  # noqa: E402
from api.planner.crops import CROPS  # noqa: E402

import test_api_smart as api_tests  # noqa: E402
from test_api_smart import (  # noqa: E402,F401  (fixtures)
    basic_client, clean_engine, client, fields_file, no_tables, tables,
)
from test_planner_smart import (  # noqa: E402,F401
    A, B, C, F, IRRIGATED_HISTORY, S, W, a_field, engine,
)

DRY, WET = api_tests.DRY, api_tests.WET


# --- reading what was typed --------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("corn, sorghum, winter wheat, fallow", [C, S, W, F]),
    ("Corn, Milo, wheat, idle", [C, S, W, F]),
    ("  CORN ;soy beans / HRW\nalfalfa ", [C, B, W, A]),
    ("corn sorghum winter wheat fallow", [C, S, W, F]),
    ("corn-sorghum-wheat-fallow", [C, S, W, F]),
    ("wheat then milo then summer fallow", [W, S, F]),
    ("Corn – Soybeans", [C, B]),
    ("beans and maize", [B, C]),
    ("1. corn 2. grain sorghum 3. chem fallow", [C, S, F]),
    ("soybean,soya,soy,bean,beans,lucerne", [B, B, B, B, B, A]),
])
def test_common_names_and_separators_are_understood(text, expected):
    assert custom.parse_rotation(text) == expected


def test_a_list_of_crops_is_taken_as_it_is():
    assert custom.parse_rotation(["Corn", " milo "]) == [C, S]
    assert custom.check_cycle([C, S]) == (C, S)


@pytest.mark.parametrize("text, word", [("cotton, wheat", "cotton"), ("corn, cover crop, wheat", "cover"),
                                        ("wheat, sunflowers", "sunflowers"), ("corn x2", "x")])
def test_a_crop_we_do_not_model_is_one_plain_sentence_listing_the_six(text, word):
    with pytest.raises(custom.RotationError) as error:
        custom.parse_rotation(text)
    message = str(error.value)
    assert f'"{word}"' in message and all(crop in message for crop in CROPS)
    assert message.count(".") == 2 and "\n" not in message and len(message) < 160
    assert isinstance(error.value, ValueError)


@pytest.mark.parametrize("text, said", [("wheat", "this one has 1 year"), ("corn " * 7, "this one has 7 years"),
                                        ("", "one crop for each year"), (None, "one crop for each year"),
                                        (" , ; ", "one crop for each year")])
def test_too_short_too_long_or_empty_is_a_plain_sentence(text, said):
    with pytest.raises(custom.RotationError, match=said):
        custom.parse_rotation(text)


def test_every_alias_maps_to_one_of_the_six_and_none_is_shared():
    seen = {}
    for crop, names in custom.ALIASES.items():
        assert crop in CROPS and crop in names
        for name in names:
            assert name == name.lower().strip() and seen.setdefault(name, crop) == crop
    assert set(custom.ALIASES) == set(CROPS)


# --- the normal form ---------------------------------------------------------------------------------

def test_normal_form_is_the_shortest_unit_started_where_the_planner_starts():
    assert custom.repeating_unit((C, B, C, B)) == (C, B)
    assert custom.repeating_unit((W, S, F, W, S, F)) == (W, S, F)
    assert custom.repeating_unit((C, C)) == (C,)
    assert custom.repeating_unit((C, S, W, F)) == (C, S, W, F)
    assert custom.normal_form((W, S, F, W, S, F)) == cand.canonical((W, S, F))
    assert custom.normal_form((S, F, W)) == cand.canonical((W, S, F))
    assert custom.normal_form((B, C)) == custom.normal_form((C, B, C, B)) == cand.canonical((C, B, C, B))
    assert custom.normal_form((C, C)) == (C, C, C, C) == custom.normal_form((C, C, C, C, C, C))
    assert custom.normal_form((F, W)) in cand.candidates(False)


def test_normal_form_does_not_depend_on_the_starting_year():
    for cycle in [(C, S, W, F, B), (A, A, A, A, C, W), (C, C, S, W, F, S), (C, S, W)]:
        forms = {custom.normal_form(cycle[i:] + cycle[:i]) for i in range(len(cycle))}
        assert len(forms) == 1 and sorted(forms.pop()) == sorted(cycle)


def test_cycle_names():
    assert custom.cycle_name((C, S, W, F)) == "Corn – Sorghum – Winter wheat – Fallow"
    assert custom.cycle_name((F, W)) == "Fallow – Winter wheat (2-year cycle)"
    assert custom.cycle_name((A, A, A, A, C, W)).endswith("(6-year cycle)")


# --- who is ahead ------------------------------------------------------------------------------------

def test_versus_names_a_winner_only_when_every_model_agrees():
    ours = np.array([0.5, 0.6, 0.7])
    assert custom.versus(ours + 0.1, ours)["verdict"] == "yours"
    assert custom.versus(ours - 0.1, ours)["verdict"] == "ours"
    assert custom.versus(ours - 0.1, ours, higher_is_better=False)["verdict"] == "yours"
    mixed = custom.versus(ours + np.array([0.1, -0.1, -0.2]), ours)
    assert mixed == {"verdict": "too_close", "yours_ahead": 1, "ours_ahead": 2, "total": 3}
    assert custom.versus(ours, ours)["verdict"] == "same"
    assert custom.versus(ours + 0.001, ours, same=True)["verdict"] == "same"       # equal at the precision shown
    assert custom.versus(ours + np.array([0.1, 0.0, 0.0]), ours)["verdict"] == "yours"
    assert custom.versus(4, 6) == {"verdict": "ours", "yours_ahead": 0, "ours_ahead": 1, "total": 1}


def side(**values):
    """A made-up rotation: three climate models, every figure set by hand."""
    base = dict(soil=[0.5] * 3, water=[0.6] * 3, money=[0.5] * 3, risk=[0.9] * 3, overall=[0.6] * 3, robust=0.6,
                bare=80.0, irrigation=[400.0] * 3, water_met=[0.7] * 3, profit=[50.0, 60.0, 70.0],
                failed=[0.1] * 3, holds=3, total=3)
    base.update(values)
    return SimpleNamespace(**{k: (np.array(v, dtype=float) if isinstance(v, list) else v) for k, v in base.items()})


def test_money_row_says_too_close_to_call_when_the_models_disagree():
    ours, yours = side(), side(profit=[40.0, 75.0, 80.0])
    row = {r["key"]: r for r in custom.compare_rows(yours, ours, irrigated=False)}["money"]
    assert row["verdict"] == "too_close" and row["models"] == {"yours_ahead": 2, "ours_ahead": 1, "total": 3}
    assert row["ours"] == 60 and row["yours"] == 65 and row["diff"] == 5
    assert row["ours_range"] == [50, 70] and row["yours_range"] == [40, 80]
    assert row["sentence"] == (
        "Ours returns about $60 an acre a year over direct costs ($50 to $70 across the climate models). "
        "Yours returns about $65 an acre a year over direct costs ($40 to $80 across the climate models). "
        "Too close to call: ours returns more in 1 of the 3 climate models, yours in 2.")
    assert "profit" not in row["sentence"] and "income" not in row["sentence"]


def test_rows_quote_only_their_own_numbers_and_say_same_when_the_figures_shown_are_equal():
    ours = side()
    yours = side(bare=80.4, soil=[0.499] * 3, water_met=[0.52, 0.5, 0.55], water=[0.52, 0.5, 0.55],
                 profit=[-30.0, -20.0, -10.0], money=[0.3] * 3, failed=[0.3] * 3, risk=[0.7] * 3, holds=1)
    rows = {r["key"]: r for r in custom.compare_rows(yours, ours, irrigated=False)}
    assert rows["soil"]["verdict"] == "same" and rows["soil"]["sentence"] == "Both leave the soil bare about 80 days a year."
    assert rows["water"]["verdict"] == "ours"
    assert rows["water"]["sentence"] == "Rain covers about 70% of the water ours needs and about 52% of what yours needs."
    assert rows["money"]["verdict"] == "ours" and "Yours falls about $20 an acre a year short of its direct costs" in rows["money"]["sentence"]
    assert "(−$30 to −$10 across the climate models)" in rows["money"]["sentence"]
    assert rows["risk"]["sentence"] == ("About 10% of the crops ours plants fail (a yield under half of normal), "
                                        "against about 30% of yours.")
    assert rows["holds"]["verdict"] == "ours" and rows["holds"]["models"] is None
    assert rows["holds"]["sentence"] == "Ours holds up in 3 of 3 climate futures, yours in 1."
    assert [r["key"] for r in custom.compare_rows(yours, ours, irrigated=False)] == ["soil", "water", "money", "risk", "holds"]
    for row in rows.values():
        assert row["diff"] == pytest.approx(row["yours"] - row["ours"], abs=1e-9)        # the three numbers add up as shown
        if "score_diff" in row:
            assert row["score_diff"] == pytest.approx(row["score_yours"] - row["score_ours"], abs=1e-9)


def test_irrigated_water_row_is_in_inches_pumped():
    ours, yours = side(irrigation=[508.0] * 3), side(irrigation=[381.0, 400.0, 420.0], water=[0.62, 0.6, 0.58])
    row = {r["key"]: r for r in custom.compare_rows(yours, ours, irrigated=True)}["water"]
    assert row["verdict"] == "yours" and row["unit"] == "inches pumped a year"
    assert row["ours"] == 20.0 and row["yours"] == pytest.approx(15.8, abs=0.05)
    assert row["sentence"] == "Ours pumps about 20 inches of water a year, yours about 15.8."


def test_a_rotation_that_plants_nothing_is_not_praised_for_water_or_risk():
    ours = side()
    nothing = side(water_met=[1.0] * 3, water=[1.0] * 3, failed=[0.0] * 3, risk=[1.0] * 3, profit=[-14.0] * 3, holds=0)
    rows = {r["key"]: r for r in custom.compare_rows(nothing, ours, irrigated=False, plants_nothing=True)}
    assert rows["water"]["sentence"].startswith("Yours plants no crop, so it needs no water")
    assert rows["risk"]["sentence"].startswith("Yours plants no crop, so none can fail")
    assert "(the same in every climate model)" in rows["money"]["sentence"]


def test_table_marks_follow_the_same_rule():
    ours1, ours2 = side(), side(profit=[55.0, 55.0, 75.0], bare=95.0, soil=[0.4] * 3, holds=2)
    yours = side(profit=[20.0, 30.0, 40.0], bare=60.0, soil=[0.62] * 3, robust=0.7)
    marks = custom.table_marks([yours, ours1, ours2], irrigated=False)
    assert marks["bare_days"] == [0] and marks["soil"] == [0]       # yours is ahead of both
    assert marks["profit"] == [1, 2]            # the two of ours cannot be told apart; yours is behind both
    assert marks["holds"] == [0, 1]
    assert marks["water"] == [] and marks["failed"] == []          # nothing separates the columns: no mark
    assert marks["robust"] == [0]               # the highest ranking score, whatever the models say
    assert set(marks) == {"soil", "water", "money", "risk", "holds", "bare_days", "water_use", "profit", "failed", "robust"}


# --- score_rotation on the made-up engine --------------------------------------------------------------

def score(engine, rotation, field=None, **kwargs):
    return custom.score_rotation(field or a_field(), custom.parse_rotation(rotation), engine=engine, **kwargs)


def test_answer_has_the_documented_shape_and_the_plans_endpoints_settings(engine):
    body = score(engine, "corn, sorghum, wheat, fallow", scenario="ssp585", horizon=2040)
    json.dumps(body)
    assert set(body) == {"field_id", "engine", "understood", "settings", "yours", "plans", "candidates", "marks",
                         "marks_note", "comparison"}
    plans = smart.rank_plans_smart(a_field(), engine=engine, scenario="ssp585", horizon=2040).to_dict()
    assert body["settings"] == plans["settings"] and body["plans"] == plans["plans"]
    assert body["candidates"] == plans["candidates"] and body["engine"] == "smart"
    assert body["understood"] == [C, S, W, F]
    yours = body["yours"]
    assert set(yours) == set(plans["plans"][0]) | {"rules_broken", "is_candidate", "same_as_pick", "rank_of", "scored_as"}
    assert yours["cycle"] == [C, S, W, F] and len(yours["rotation"]) == 4
    assert set(yours["scores"]) == {"soil", "water", "money", "risk", "overall", "robust"}
    assert yours["futures"]["total"] == 3 and yours["reason"].endswith("climate futures.") or "climate futures" in yours["reason"]
    comparison = body["comparison"]
    assert set(comparison) == {"against", "verdict", "models", "headline", "rows", "priorities", "priority_sentences", "notes"}
    assert comparison["against"] == plans["plans"][0]["name"]
    assert [r["key"] for r in comparison["rows"]] == ["soil", "water", "money", "risk", "holds"]
    assert [p["priority"] for p in comparison["priorities"]] == list(config.SCORE_NAMES)
    assert all(isinstance(s, str) and s.endswith(".") for s in comparison["headline"] + comparison["priority_sentences"])
    text = json.dumps(body["comparison"]) + json.dumps(yours["rules_broken"])
    assert not re.search(r"soil health|improves? the soil|\bprofit\b|\bincome\b|less rain", text, re.I)


def test_dryland_wheat_after_a_summer_crop_is_scored_and_told_gently_what_our_plans_avoid(engine):
    body = score(engine, "corn, sorghum, wheat, fallow")
    yours = body["yours"]
    assert yours["is_candidate"] is False and yours["same_as_pick"] is None
    assert len(yours["rules_broken"]) == 1 and yours["rules_broken"][0].startswith("Our plans avoid winter wheat straight after")
    assert yours["rank_of"] == body["candidates"] + 1 and 1 <= yours["rank"] <= yours["rank_of"]
    assert body["comparison"]["headline"][0].startswith(f"Yours is not one of the {body['candidates']} rotations we compare")
    assert "it breaks what our plans avoid" in body["comparison"]["headline"][0]
    assert custom.DRYLAND_MONEY_NOTE in body["comparison"]["notes"] and custom.CYCLE_NOTE in body["comparison"]["notes"]


def test_rank_counts_the_candidates_that_score_higher(engine):
    field = a_field()
    body = score(engine, "corn, sorghum, wheat, fallow", field=field)
    ev = engine.evaluate(False, slope_pct=field.slope_pct)
    weights = smart.Weights().normalised()
    mine = engine.evaluate(False, rotations=[(C, S, W, F)], slope_pct=field.slope_pct)
    higher = int((np.round(ev.robust(weights), 6) > round(float(mine.robust(weights)[0]), 6)).sum())
    assert body["yours"]["rank"] == higher + 1
    assert body["yours"]["scores"]["robust"] == round(float(mine.robust(weights)[0]), 3)
    assert body["yours"]["numbers"]["bare_days"] == round(float(mine.bare[0]), 1)


def test_our_own_first_pick_typed_in_another_year_is_our_first_pick(engine):
    plans = smart.rank_plans_smart(a_field(), engine=engine).to_dict()["plans"]
    cycle = plans[0]["cycle"]
    shifted = cycle[1:] + cycle[:1]
    typed = shifted + shifted if len(shifted) == 3 else shifted   # started a year later (a 3-year one written twice)
    body = score(engine, typed)
    yours = body["yours"]
    assert yours["is_candidate"] is True and yours["same_as_pick"] == 1 and yours["rank"] == 1
    assert yours["rank_of"] == body["candidates"] and yours["rules_broken"] == []
    for key in ("scores", "numbers", "futures", "spread", "reason"):
        assert yours[key] == plans[0][key], key
    assert yours["cycle"] == typed and yours["scored_as"] == cycle
    assert body["comparison"]["verdict"] == "same"
    assert body["comparison"]["headline"] == ["Yours is our first pick for this field with these priorities."]
    assert all(row["verdict"] == "same" and row["diff"] == 0 for row in body["comparison"]["rows"])


def test_our_second_pick_says_so_and_keeps_its_place(engine):
    plans = smart.rank_plans_smart(a_field(), engine=engine).to_dict()["plans"]
    body = score(engine, plans[1]["cycle"])
    assert body["yours"]["same_as_pick"] == 2 and body["yours"]["rank"] == plans[1]["rank"]
    assert body["comparison"]["headline"][0] == (
        f"Yours is our pick number 2: it ranks number {plans[1]['rank']} of the {body['candidates']} rotations we compare.")
    assert body["yours"]["scores"] == plans[1]["scores"]


def test_the_score_does_not_change_with_the_starting_year(engine):
    field = a_field(IRRIGATED_HISTORY)
    cycle = [C, C, S, W, F, B]
    answers = [score(engine, cycle[i:] + cycle[:i], field=field, irrigated=True) for i in range(len(cycle))]
    for other in answers[1:]:
        for key in ("scores", "numbers", "futures", "spread", "rank", "rules_broken"):
            assert other["yours"][key] == answers[0]["yours"][key], key
    assert answers[0]["yours"]["is_candidate"] is False
    assert "this one runs 6 years" in answers[0]["comparison"]["headline"][0] or answers[0]["yours"]["rules_broken"]


def test_a_six_year_cycle_is_the_average_of_its_years(engine):
    field = a_field(IRRIGATED_HISTORY)
    cycle = (A, A, A, A, C, W)
    body = score(engine, cycle, field=field, irrigated=True)
    form = custom.normal_form(cycle)
    assert body["yours"]["numbers"]["bare_days"] == round(soil_effect.plan_bare_days(engine.soil, form, True), 1)
    # its return is the mean of the six years, each crop after the one before it
    one = engine.evaluate(True, rotations=[form], max_years=6, slope_pct=field.slope_pct)
    assert one.plans.crop.shape == (1, 6) and one.plans.weight.sum() == pytest.approx(1.0)
    four = engine.evaluate(True, rotations=[(A, A, A, A)], slope_pct=field.slope_pct)
    two = engine.evaluate(True, rotations=[(C, W)], slope_pct=field.slope_pct)
    assert four.plans.crop.shape == (1, 4)
    low = min(four.profit.mean(), two.profit.mean())
    high = max(four.profit.mean(), two.profit.mean())
    assert low - 60 <= one.profit.mean() <= high + 60      # between its parts, give or take the two seams
    assert body["yours"]["rules_broken"] == [] and body["yours"]["is_candidate"] is False
    assert "we compare 3- and 4-year cycles and this one runs 6 years" in body["comparison"]["headline"][0]


def test_cycles_longer_than_four_years_are_only_for_the_custom_rotation(engine):
    with pytest.raises(ValueError, match="at most 4 years"):
        engine.evaluate(False, rotations=[(W, S, F, W, S)])
    with pytest.raises(ValueError, match="at most 6 years"):
        engine.evaluate(False, rotations=[(W, S, F, W, S, F, W)], max_years=6)
    with pytest.raises(ValueError):
        cand.rules_broken((W, S, F, W, S), False)
    rule = {r.key: r for r in cand.RULES}["dryland_wheat_after_fallow"]
    assert cand.rules_broken((W, S, F, W, S), False, any_length=True) == [rule.text]        # sorghum, then wheat at the seam
    assert cand.rules_broken((W, S, F, W, S), False, gentle=True, any_length=True) == [rule.gentle]


def test_two_year_and_continuous_rotations(engine):
    body = score(engine, "fallow, wheat")
    assert body["yours"]["scored_as"] == list(cand.canonical((W, F, W, F))) and body["yours"]["is_candidate"] is True
    corn = score(engine, "corn corn", field=a_field(IRRIGATED_HISTORY), irrigated=True)
    assert corn["yours"]["scored_as"] == [C] * 4 and corn["yours"]["is_candidate"] is False
    assert any("two years in a row of corn" in s for s in corn["yours"]["rules_broken"])
    nothing = score(engine, "fallow fallow")
    assert custom.NOTHING_PLANTED_NOTE in nothing["comparison"]["headline"]
    assert nothing["yours"]["numbers"]["failed_share"] == 0 and nothing["yours"]["futures"]["holds"] == 0
    # no crop, so no "share of crops that fail" or "share of water need met" figure of yours to mark
    assert 0 not in nothing["marks"]["failed"] and 0 not in nothing["marks"]["water_use"]


def test_priorities_say_truthfully_when_yours_would_come_first(engine):
    field = a_field()
    for name in config.SCORE_NAMES:
        only = {key: (1.0 if key == name else 0.0) for key in config.SCORE_NAMES}
        best = smart.rank_plans_smart(field, engine=engine, weights=only).to_dict()["plans"][0]
        body = score(engine, best["cycle"], field=field)            # asked with the default priorities
        place = {p["priority"]: p for p in body["comparison"]["priorities"]}[name]
        assert place["first"] is True and place["rank"] == 1 and place["of"] == body["candidates"]
        words = custom.PRIORITY_WORDS[name]
        assert any(s.startswith(f"Yours comes first if {words} is all that matters") for s in body["comparison"]["priority_sentences"])
    # a rotation that is first under nothing says where it gets closest, with its place
    body = score(engine, "corn, sorghum, wheat, fallow", field=field)
    places = body["comparison"]["priorities"]
    if not any(p["first"] for p in places):
        best = min(places, key=lambda p: p["rank"])
        assert body["comparison"]["priority_sentences"] == [
            f"Yours does not come first under any single priority. It gets closest when "
            f"{custom.PRIORITY_WORDS[best['priority']]} is all that matters: number {best['rank']} of {best['of']}."]
    for place in places:
        only = smart.Weights(**{k: (1.0 if k == place["priority"] else 0.0) for k in config.SCORE_NAMES})
        ev = engine.evaluate(False, slope_pct=field.slope_pct)
        mine = engine.evaluate(False, rotations=[(C, S, W, F)], slope_pct=field.slope_pct)
        assert place["rank"] == 1 + int((np.round(ev.robust(only), 6) > round(float(mine.robust(only)[0]), 6)).sum())


def test_sentences_carry_the_numbers_of_their_rows(engine):
    body = score(engine, "sorghum, sorghum, fallow, wheat", horizon=2050, scenario="ssp585")
    rows = {r["key"]: r for r in body["comparison"]["rows"]}
    yours, ours = body["yours"], body["plans"][0]
    assert rows["soil"]["yours"] == yours["numbers"]["bare_days"] and rows["soil"]["ours"] == ours["numbers"]["bare_days"]
    assert rows["soil"]["score_yours"] == yours["scores"]["soil"] and rows["soil"]["score_ours"] == ours["scores"]["soil"]
    assert rows["water"]["yours"] == yours["numbers"]["water_met_share"]
    assert rows["money"]["yours"] == yours["numbers"]["profit_per_acre"] and rows["money"]["ours"] == ours["numbers"]["profit_per_acre"]
    assert rows["money"]["yours_range"] == [yours["spread"]["profit_per_acre"]["low"], yours["spread"]["profit_per_acre"]["high"]]
    assert rows["risk"]["yours"] == yours["numbers"]["failed_share"]
    assert rows["holds"]["yours"] == yours["futures"]["holds"] and rows["holds"]["ours"] == ours["futures"]["holds"]
    if rows["soil"]["verdict"] != "same":
        assert f"about {custom._days(ours['numbers']['bare_days'])} days a year, yours about {custom._days(yours['numbers']['bare_days'])}." in rows["soil"]["sentence"]
    for row in rows.values():
        assert row["verdict"] in ("ours", "yours", "too_close", "same")
        assert ("Too close to call" in row["sentence"]) == (row["verdict"] == "too_close")


def test_bad_settings_are_value_errors(engine):
    with pytest.raises(ValueError):
        score(engine, "corn, wheat", horizon=1990)
    with pytest.raises(ValueError):
        score(engine, "corn, wheat", weights={"soil": -1})
    with pytest.raises(custom.RotationError):
        custom.score_rotation(a_field(), ["corn", "cotton"], engine=engine)


# --- the endpoint ------------------------------------------------------------------------------------

def test_endpoint_scores_what_was_typed_with_the_plans_settings(client):
    params = {"soil": 1, "water": 4, "money": 2, "risk": 1, "scenario": "ssp585", "horizon": 2050}
    response = client.get(f"/field/{DRY}/score", params={"rotation": "Corn, milo, wheat, idle", **params})
    assert response.status_code == 200
    body = response.json()
    assert body["understood"] == [C, S, W, F] and body["field_id"] == DRY and body["engine"] == "smart"
    plans = client.get(f"/field/{DRY}/plans", params=params).json()
    assert body["settings"] == plans["settings"] and body["plans"] == plans["plans"]
    assert body["settings"]["horizon"] == 2050 and body["settings"]["weights"]["water"] == 0.5
    wet = client.get(f"/field/{DRY}/score", params={"rotation": "corn, soy", "irrigated": "true"}).json()
    assert wet["settings"]["irrigated"] is True and wet["settings"]["irrigation_source"] == "override"
    assert wet["yours"]["numbers"]["irrigation_mm"] > 0
    assert {r["key"]: r for r in wet["comparison"]["rows"]}["water"]["unit"] == "inches pumped a year"


@pytest.mark.parametrize("params, said", [
    ({"rotation": "cotton, wheat"}, 'We do not have numbers for "cotton".'),
    ({"rotation": "cover crop, corn"}, "corn, soybeans, winter wheat, sorghum, alfalfa and fallow"),
    ({"rotation": "wheat"}, "A rotation needs 2 to 6 years"),
    ({}, "Give one crop for each year"),
    ({"rotation": "corn, wheat", "horizon": 1990}, "The year must be between"),
    ({"rotation": "corn, wheat", "scenario": "nonsense"}, "The scenario must be"),
    ({"rotation": "corn, wheat", "soil": -2}, "The soil priority must be"),
])
def test_endpoint_answers_400_with_one_plain_sentence(client, params, said):
    response = client.get(f"/field/{DRY}/score", params=params)
    assert response.status_code == 400
    assert said in response.json()["detail"] and "Traceback" not in response.text


def test_endpoint_unknown_field_is_404(client):
    assert client.get("/field/KS-999999/score", params={"rotation": "corn, wheat"}).status_code == 404


def test_endpoint_is_503_with_a_plain_reason_on_the_basic_planner(basic_client, monkeypatch):
    response = basic_client.get(f"/field/{DRY}/score", params={"rotation": "corn, wheat"})
    assert response.status_code == 503 and response.json()["detail"] == api_tests.main.SCORE_NEEDS_CLIMATE
    assert basic_client.get(f"/field/{DRY}/plans").json()["engine"] == "basic"       # the rest still works


def test_endpoint_is_503_when_the_basic_engine_is_asked_for(client, monkeypatch):
    monkeypatch.setenv("PLANNER_ENGINE", "basic")
    assert client.get(f"/field/{DRY}/score", params={"rotation": "corn, wheat"}).status_code == 503


@api_tests.needs_real
def test_real_tables_score_a_real_fields_rotation(monkeypatch):
    for var in list(api_tests.ENV_PATHS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PLANNER_ENGINE", raising=False)
    monkeypatch.delenv("FIELDS_PATH", raising=False)
    smart.reset()
    from fastapi.testclient import TestClient

    with TestClient(api_tests.main.create_app()) as c:
        if c.get("/health").json()["is_fake"]:
            pytest.skip("real fields not loaded")
        body = c.get("/field/KS-013847/score", params={"rotation": "wheat, milo, fallow", "horizon": 2060}).json()
        plans = c.get("/field/KS-013847/plans", params={"horizon": 2060}).json()
        assert body["plans"] == plans["plans"] and body["yours"]["futures"]["total"] == 6
        assert body["yours"]["is_candidate"] is True
        match = [p for p in plans["plans"] if p["cycle"] == body["yours"]["scored_as"]]
        if match:
            assert body["yours"]["scores"] == match[0]["scores"]
