"""Tests for api/planner.py: the crop table, the scoring nudges, the sentences, determinism."""
from __future__ import annotations

import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from api import planner  # noqa: E402
from api.planner import (  # noqa: E402
    BASE_WEIGHTS,
    CROPS,
    ROTATIONS,
    TYPICAL_OM_PCT,
    FieldInput,
    Plan,
    Scores,
    analyse_field,
    rank_plans,
    rotation_name,
    score_rotation,
    weights_for,
)

ROTATION_CROPS = {"corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow", "cover crop"}

CONTINUOUS_CORN = ("corn", "corn", "corn", "corn")
CORN_SOY = ("corn", "soybeans", "corn", "soybeans")
FOUR_CROPS = ("corn", "soybeans", "winter wheat", "sorghum")
WHEAT_SORGHUM_FALLOW = ("winter wheat", "sorghum", "fallow", "winter wheat")
WHEAT_SORGHUM_COVER = ("winter wheat", "sorghum", "cover crop", "winter wheat")
ALFALFA_CORN = ("alfalfa", "alfalfa", "alfalfa", "corn")


def history(crops, start: int = 2008) -> dict[str, str]:
    return {str(start + i): crop for i, crop in enumerate(crops)}


def make_field(crops=(), om=None, slope=None, start: int = 2008) -> FieldInput:
    return FieldInput(
        field_id="KS-000001", crop_history=history(crops, start), organic_matter_pct=om, slope_pct=slope, area_ha=50.0
    )


WORN_OUT = make_field(["corn"] * 17, om=1.1, slope=5.0)  # low OM, steep, continuous corn
HEALTHY = make_field((list(FOUR_CROPS) * 5)[:17], om=3.2, slope=0.4)  # high OM, flat, diverse
CORN_SOY_FIELD = make_field((["corn", "soybeans"] * 9)[:17], om=2.0, slope=1.0)
DRYLAND = make_field((["winter wheat", "sorghum", "fallow"] * 6)[:17], om=1.9, slope=0.5)
NO_INFO = make_field()


def check_plans(plans: list[Plan]) -> None:
    """Everything the HTTP contract promises about a ranked list."""
    assert isinstance(plans, list) and len(plans) == 3
    assert len({tuple(p.rotation) for p in plans}) == 3, "rotations must be distinct"
    overall = [p.scores.overall for p in plans]
    assert overall == sorted(overall, reverse=True)
    for plan in plans:
        assert isinstance(plan, Plan) and isinstance(plan.scores, Scores)
        assert isinstance(plan.name, str) and plan.name
        assert isinstance(plan.rotation, list) and len(plan.rotation) == 4
        assert set(plan.rotation) <= ROTATION_CROPS
        for value in (plan.scores.soil, plan.scores.water, plan.scores.money, plan.scores.overall):
            assert isinstance(value, float) and 0.0 <= value <= 1.0
            assert round(value, 3) == value, "scores are rounded to 3 decimals"
        check_reason(plan.reason)


def check_reason(reason: str) -> None:
    assert isinstance(reason, str) and reason
    assert len(reason) <= 160, reason
    assert reason[0].isupper() and reason.endswith(".") and reason.count(".") - reason.count("%") <= 1, reason
    assert "\n" not in reason and "  " not in reason
    # the only decimals allowed are the field's own percentages ("1.2%", "4.5% slope"), never a score
    assert not re.search(r"\d\.\d+(?![\d%])", reason), f"score-like number in: {reason}"
    assert not re.search(r"\b(score|overall|weight|index)\b", reason.lower()), reason


# --------------------------------------------------------------------------------------
# crop table and candidates
# --------------------------------------------------------------------------------------


def test_crop_table_has_the_seven_rows_in_range():
    assert set(CROPS) == ROTATION_CROPS
    for name, crop in CROPS.items():
        for attr in ("water_need", "soil_cover", "heat_tolerance", "typical_profit"):
            assert 0.0 <= getattr(crop, attr) <= 1.0, (name, attr)
        assert -1.0 <= crop.nitrogen_effect <= 1.0, name


def test_crop_table_is_agronomically_sensible_for_sw_kansas():
    corn, soy, wheat, sorghum, alfalfa = (CROPS[c] for c in ("corn", "soybeans", "winter wheat", "sorghum", "alfalfa"))
    # corn: thirsty and the most profitable; sorghum and wheat: thrifty and heat tolerant
    assert corn.water_need > soy.water_need > wheat.water_need >= sorghum.water_need
    assert corn.typical_profit == max(c.typical_profit for c in CROPS.values())
    assert sorghum.heat_tolerance == max(c.heat_tolerance for c in CROPS.values())
    assert wheat.heat_tolerance > corn.heat_tolerance and sorghum.heat_tolerance > corn.heat_tolerance
    # legumes add nitrogen, grains drain it
    assert soy.nitrogen_effect > 0 and alfalfa.nitrogen_effect > soy.nitrogen_effect
    assert corn.nitrogen_effect < wheat.nitrogen_effect < 0 and sorghum.nitrogen_effect < 0
    # alfalfa: the thirstiest, with the best cover
    assert alfalfa.water_need == max(c.water_need for c in CROPS.values())
    assert alfalfa.soil_cover == max(c.soil_cover for c in CROPS.values())


def test_fallow_and_cover_crop_are_separate_rows_with_opposite_soil_cover():
    fallow, cover = CROPS["fallow"], CROPS["cover crop"]
    assert fallow.soil_cover <= 0.2 and cover.soil_cover >= 0.8
    assert fallow.soil_cover == min(c.soil_cover for c in CROPS.values())
    assert fallow.water_need == min(c.water_need for c in CROPS.values())
    assert fallow.typical_profit == 0.0 and cover.typical_profit <= 0.1
    assert cover.nitrogen_effect > fallow.nitrogen_effect


def test_candidate_rotations_cover_a_sensible_spread():
    assert 8 <= len(ROTATIONS) <= 12
    assert len(set(ROTATIONS)) == len(ROTATIONS)
    for rotation in ROTATIONS:
        assert len(rotation) == 4 and set(rotation) <= ROTATION_CROPS
    assert CONTINUOUS_CORN in ROTATIONS and CORN_SOY in ROTATIONS and FOUR_CROPS in ROTATIONS
    for crop in ROTATION_CROPS:
        assert any(crop in rotation for rotation in ROTATIONS), f"no rotation uses {crop}"
    assert any("fallow" in r and "corn" not in r for r in ROTATIONS), "a dryland wheat-sorghum-fallow style plan"
    assert sum(1 for r in ROTATIONS if r[0] != "corn") >= 4, "plenty of plans that do not start with corn"


def test_rotation_names_are_short_labels():
    assert rotation_name(FOUR_CROPS) == "Corn – Soybeans – Winter wheat – Sorghum"
    assert rotation_name(CONTINUOUS_CORN) == "Continuous corn"
    assert rotation_name(CORN_SOY) == "Corn – Soybeans (alternating)"
    assert rotation_name(ALFALFA_CORN) == "Alfalfa (3 yrs) – Corn"
    assert len({rotation_name(r) for r in ROTATIONS}) == len(ROTATIONS)
    assert all(len(rotation_name(r)) <= 60 for r in ROTATIONS)


# --------------------------------------------------------------------------------------
# interface, determinism
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", [WORN_OUT, HEALTHY, CORN_SOY_FIELD, DRYLAND, NO_INFO], ids=lambda f: "field")
def test_rank_plans_returns_three_valid_plans(field):
    check_plans(rank_plans(field))


def test_rank_plans_is_deterministic():
    for field in (WORN_OUT, HEALTHY, CORN_SOY_FIELD, DRYLAND, NO_INFO):
        assert rank_plans(field) == rank_plans(field)
    # the order the years arrive in does not matter
    items = list(WORN_OUT.crop_history.items())
    random.Random(1).shuffle(items)
    shuffled = FieldInput("KS-000001", dict(items), WORN_OUT.organic_matter_pct, WORN_OUT.slope_pct, WORN_OUT.area_ha)
    assert rank_plans(shuffled) == rank_plans(WORN_OUT)
    # field_id and area are not inputs to the model
    other = FieldInput("KS-000999", WORN_OUT.crop_history, WORN_OUT.organic_matter_pct, WORN_OUT.slope_pct, 3.0)
    assert rank_plans(other) == rank_plans(WORN_OUT)


def test_rank_plans_does_not_modify_its_input():
    crops = {"2020": "corn", "2021": "corn", "2022": "grass/pasture"}
    field = FieldInput("KS-000001", crops, 1.5, 2.0, 10.0)
    rank_plans(field)
    assert crops == {"2020": "corn", "2021": "corn", "2022": "grass/pasture"}


# --------------------------------------------------------------------------------------
# nudges
# --------------------------------------------------------------------------------------


def test_missing_organic_matter_and_slope_are_neutral():
    ctx = analyse_field(NO_INFO)
    assert (ctx.soil_need, ctx.erosion_risk, ctx.dryness, ctx.stuck_pressure) == (0.0, 0.0, 0.0, 0.0)
    assert weights_for(ctx) == pytest.approx(BASE_WEIGHTS)
    # None behaves exactly like a typical soil on flat ground ...
    crops = (["corn", "soybeans"] * 9)[:17]
    assert rank_plans(make_field(crops)) == rank_plans(make_field(crops, om=TYPICAL_OM_PCT, slope=0.0))
    # ... and NaN (what a parquet null turns into) is treated as missing, not as a number
    assert rank_plans(make_field(crops, om=float("nan"), slope=float("nan"))) == rank_plans(make_field(crops))


def test_low_organic_matter_weights_soil_more():
    crops = (["corn", "soybeans"] * 9)[:17]
    weights = [weights_for(analyse_field(make_field(crops, om=om)))["soil"] for om in (0.8, 1.2, 1.6, 2.0, 2.6, 3.4)]
    assert weights == sorted(weights, reverse=True)
    assert weights[0] > BASE_WEIGHTS["soil"] > weights[-1]
    assert weights[0] - weights[-1] > 0.15
    for om in (0.8, 2.0, 3.4):
        assert sum(weights_for(analyse_field(make_field(crops, om=om))).values()) == pytest.approx(1.0)

    # so a soil-building plan gains on a money plan as organic matter drops
    def gap(om):
        ctx = analyse_field(make_field(crops, om=om))
        return score_rotation(ALFALFA_CORN, ctx).overall - score_rotation(CORN_SOY, ctx).overall

    assert gap(1.0) > gap(2.0) > gap(3.2)


def test_slope_rewards_ground_cover():
    flat = analyse_field(make_field(slope=0.5))
    steep = analyse_field(make_field(slope=7.0))
    assert flat.erosion_risk == 0.0 and steep.erosion_risk == 1.0
    assert weights_for(steep)["soil"] > weights_for(flat)["soil"]
    # well-covered plans gain soil points on a slope, bare-ground plans lose them
    for covered in (ALFALFA_CORN, WHEAT_SORGHUM_COVER):
        assert score_rotation(covered, steep).soil > score_rotation(covered, flat).soil + 0.05
    for bare in (WHEAT_SORGHUM_FALLOW, CORN_SOY):
        assert score_rotation(bare, steep).soil < score_rotation(bare, flat).soil
    # water and money are untouched by slope
    assert score_rotation(CORN_SOY, steep).water == score_rotation(CORN_SOY, flat).water
    assert score_rotation(CORN_SOY, steep).money == score_rotation(CORN_SOY, flat).money
    # and it shows in the overall score: on a dryland field the cover-crop plan pulls well ahead of
    # the same plan with a bare fallow year once the field is steep
    dry = (["winter wheat", "sorghum", "fallow"] * 6)[:17]

    def lead(slope):
        ctx = analyse_field(make_field(dry, om=2.0, slope=slope))
        return score_rotation(WHEAT_SORGHUM_COVER, ctx).overall - score_rotation(WHEAT_SORGHUM_FALLOW, ctx).overall

    assert lead(7.0) > lead(0.5) + 0.05
    assert rank_plans(make_field(dry, om=2.0, slope=7.0))[0].rotation == list(WHEAT_SORGHUM_COVER)
    # an irrigated corn/soy field: flat -> the four-cash-crop plan leads; steep -> a covered plan does
    wet = (["corn", "soybeans"] * 9)[:17]
    assert rank_plans(make_field(wet, om=2.0, slope=0.0))[0].rotation == list(FOUR_CROPS)
    steep_top = rank_plans(make_field(wet, om=2.0, slope=7.0))[0].rotation
    assert steep_top != list(FOUR_CROPS) and {"alfalfa", "cover crop"} & set(steep_top)


def test_long_corn_streak_rewards_plans_that_break_it():
    stuck = make_field(["corn"] * 12, start=2013)
    ctx = analyse_field(stuck)
    assert (ctx.run_crop, ctx.run_years, ctx.stuck_crop) == ("corn", 12, "corn")
    assert ctx.stuck_pressure == 1.0

    plans = rank_plans(stuck)
    check_plans(plans)
    assert all(p.rotation[0] != "corn" for p in plans), "nothing in the top 3 starts with the streak crop"
    assert all(p.rotation.count("corn") <= 1 for p in plans), "nothing in the top 3 is dominated by it"
    ranked = sorted(ROTATIONS, key=lambda r: -score_rotation(r, ctx).overall)
    assert {CONTINUOUS_CORN, CORN_SOY} <= set(ranked[-3:]), "more of the same ends up at the bottom"

    # the same soil with nothing known about its history happily starts with corn
    fresh = analyse_field(NO_INFO)
    assert rank_plans(NO_INFO)[0].rotation == list(FOUR_CROPS)
    fresh_ranked = sorted(ROTATIONS, key=lambda r: -score_rotation(r, fresh).overall)
    for rotation in (CORN_SOY, FOUR_CROPS, ("corn", "corn", "soybeans", "winter wheat")):
        assert ranked.index(rotation) > fresh_ranked.index(rotation), f"{rotation} should drop for a corn rut"
    # the penalty lands on corn-first / corn-heavy plans only
    for rotation in (CONTINUOUS_CORN, CORN_SOY, FOUR_CROPS):
        assert score_rotation(rotation, ctx).soil < score_rotation(rotation, fresh).soil
    for rotation in (ALFALFA_CORN, WHEAT_SORGHUM_COVER, ("soybeans", "corn", "winter wheat", "cover crop")):
        assert score_rotation(rotation, ctx).soil == score_rotation(rotation, fresh).soil


def test_corn_streak_counts_more_than_other_streaks_and_longer_counts_more():
    pressure = lambda crops: analyse_field(make_field(crops)).stuck_pressure  # noqa: E731
    assert pressure(["corn"] * 8) > pressure(["sorghum"] * 8) > pressure(["alfalfa"] * 8) > 0
    assert pressure(["corn"] * 2) < pressure(["corn"] * 4) < pressure(["corn"] * 6)
    assert pressure(["soybeans", "corn"]) == 0.0, "one year of a crop is not a rut"
    assert pressure((["corn", "soybeans"] * 5)) == 0.0, "an even corn/soy alternation is not a rut"


def test_streak_is_over_consecutive_calendar_years():
    gap = history(["corn"] * 5, start=2008) | history(["corn"] * 3, start=2014)  # 2013 is missing
    ctx = analyse_field(FieldInput("KS-000001", gap, None, None, 10.0))
    assert (ctx.run_crop, ctx.run_years) == ("corn", 3)
    # a field that is corn most recent years is still "stuck" even when the current run is short
    mostly = ["sorghum"] + ["corn"] * 9 + ["soybeans"] + ["corn"] * 3 + ["soybeans"] + ["corn"] * 2
    ctx = analyse_field(make_field(mostly))
    assert ctx.run_years == 2 and ctx.stuck_crop == "corn" and ctx.stuck_pressure > 0.5
    assert (ctx.top_crop, ctx.top_count, ctx.window_years) == ("corn", 8, 10)


def test_dryland_history_makes_water_count_for_more_and_thirsty_crops_a_gamble():
    dry, wet = analyse_field(DRYLAND), analyse_field(CORN_SOY_FIELD)
    assert dry.dryness == 1.0 and wet.dryness == 0.0
    assert analyse_field(HEALTHY).dryness == 0.0, "a diverse irrigated rotation is not dryland"
    assert 0.3 < analyse_field(make_field((["winter wheat", "corn", "sorghum", "fallow"] * 5)[:17])).dryness < 1.0
    assert weights_for(dry)["water"] > weights_for(wet)["water"] + 0.15

    # thirsty plans lose water points on a dryland field; thrifty plans do not
    for thirsty in (CONTINUOUS_CORN, CORN_SOY, ALFALFA_CORN, FOUR_CROPS):
        assert score_rotation(thirsty, dry).water < score_rotation(thirsty, wet).water - 0.1
    for thrifty in (WHEAT_SORGHUM_FALLOW, WHEAT_SORGHUM_COVER):
        assert score_rotation(thrifty, dry).water == score_rotation(thrifty, wet).water

    plans = rank_plans(DRYLAND)
    check_plans(plans)
    assert plans[0].rotation == list(WHEAT_SORGHUM_COVER), "wheat-sorghum with the fallow year covered"
    assert list(WHEAT_SORGHUM_FALLOW) in [p.rotation for p in plans], "what the field already does is an option"
    assert all("alfalfa" not in p.rotation and "corn" not in p.rotation for p in plans)
    assert plans[0].scores.water >= 0.6
    # the same soil under irrigated corn/soy gets a different list
    assert [p.rotation for p in rank_plans(CORN_SOY_FIELD)] != [p.rotation for p in plans]


def test_unfamiliar_cash_crops_earn_less_but_unknown_history_is_neutral():
    never_grew_alfalfa = analyse_field(CORN_SOY_FIELD)
    grew_alfalfa = analyse_field(make_field(["alfalfa"] * 4 + ["corn", "soybeans"] * 6))
    assert score_rotation(ALFALFA_CORN, never_grew_alfalfa).money < score_rotation(ALFALFA_CORN, grew_alfalfa).money
    assert score_rotation(CORN_SOY, never_grew_alfalfa).money == pytest.approx(0.775)  # both crops already grown
    assert score_rotation(ALFALFA_CORN, analyse_field(NO_INFO)).money == pytest.approx((0.65 * 3 + 0.9) / 4)


def test_double_crops_count_as_having_grown_both_crops():
    plain = make_field(["corn", "soybeans"] * 4)
    doubled = make_field(["corn", "soybeans"] * 3 + ["corn", "double crop: winter wheat/sorghum"])
    assert analyse_field(doubled).grown == {"corn", "soybeans", "winter wheat", "sorghum"}
    assert score_rotation(FOUR_CROPS, analyse_field(plain)).money < score_rotation(FOUR_CROPS, analyse_field(doubled)).money
    assert score_rotation(FOUR_CROPS, analyse_field(doubled)).money == pytest.approx(0.625)  # nothing is new


def test_ordering_changes_between_a_worn_out_and_a_healthy_field():
    worn, healthy = rank_plans(WORN_OUT), rank_plans(HEALTHY)
    check_plans(worn)
    check_plans(healthy)
    assert [p.rotation for p in worn] != [p.rotation for p in healthy]
    assert worn[0].rotation != healthy[0].rotation

    # worn out (low OM, steep, 17 years of corn): soil builders that break the corn run
    assert all(p.rotation[0] != "corn" for p in worn)
    assert all({"alfalfa", "cover crop"} & set(p.rotation) for p in worn)
    assert all(p.scores.soil >= 0.7 for p in worn)
    # healthy (high OM, flat, diverse): the four-cash-crop plan leads, and corn-led income plans appear
    assert healthy[0].rotation == list(FOUR_CROPS)
    assert sum(1 for p in healthy if p.rotation[0] == "corn") >= 2
    assert max(p.scores.money for p in healthy) > max(p.scores.money for p in worn)
    assert not any(p.rotation[0] == "corn" for p in worn)

    # the full ranking of all candidates differs too, not just the top 3
    def full_order(field):
        ctx = analyse_field(field)
        return sorted(ROTATIONS, key=lambda r: -score_rotation(r, ctx).overall)

    assert full_order(WORN_OUT) != full_order(HEALTHY)
    assert full_order(WORN_OUT)[-1] in (CONTINUOUS_CORN, CORN_SOY)


def test_scores_visibly_differ_when_the_field_data_differs():
    crops = (["corn", "soybeans"] * 9)[:17]

    def scores(rotation, **kwargs) -> Scores:
        return score_rotation(rotation, analyse_field(make_field(kwargs.pop("crops", crops), **kwargs)))

    # organic matter moves the overall score (through the weights)
    assert abs(scores(ALFALFA_CORN, om=1.0).overall - scores(ALFALFA_CORN, om=3.2).overall) >= 0.02
    # slope moves the soil score
    assert abs(scores(ALFALFA_CORN, slope=6.0).soil - scores(ALFALFA_CORN, slope=0.0).soil) >= 0.1
    # a corn rut moves the soil score of corn-led plans
    assert scores(CORN_SOY, crops=["corn"] * 17).soil <= scores(CORN_SOY).soil - 0.2
    # and the rounded numbers the API shows differ for every single candidate
    for rotation in ROTATIONS:
        low, high = scores(rotation, om=1.0, slope=5.0), scores(rotation, om=3.2, slope=0.0)
        assert round(low.overall, 2) != round(high.overall, 2), rotation
    # the same plan on two fields: different numbers in the ranked output too
    by_rotation = lambda field: {tuple(p.rotation): p.scores for p in rank_plans(field)}  # noqa: E731
    a, b = by_rotation(make_field(crops, om=1.2, slope=0.5)), by_rotation(make_field(crops, om=1.2, slope=5.5))
    shared = set(a) & set(b)
    assert shared and all(a[r] != b[r] for r in shared)


# --------------------------------------------------------------------------------------
# sentences
# --------------------------------------------------------------------------------------


def test_reason_names_the_streak_and_the_organic_matter_that_drove_it():
    crops = ["soybeans"] * 8 + ["corn"] * 9  # corn 2016-2024
    plans = rank_plans(make_field(crops, om=1.2, slope=0.5))
    check_plans(plans)
    assert all("9-year corn run" in p.reason for p in plans)
    assert plans[0].reason.startswith("Breaks your 9-year corn run and ")
    assert any("low-organic-matter soil (1.2%)" in p.reason for p in plans)


def test_reason_names_the_slope_when_it_matters():
    plans = rank_plans(make_field((["corn", "soybeans"] * 9)[:17], om=2.0, slope=5.5))
    assert all("5.5% slope" in p.reason for p in plans)
    flat = rank_plans(make_field((["corn", "soybeans"] * 9)[:17], om=2.0, slope=0.5))
    assert all("slope" not in p.reason for p in flat)


def test_reason_reflects_the_field_not_just_the_plan():
    """The same rotation gets a different sentence on a different field."""
    def reasons(field):
        return {tuple(p.rotation): p.reason for p in rank_plans(field)}

    a = reasons(make_field(["corn"] * 17, om=1.1, slope=5.0))
    b = reasons(make_field(["corn"] * 6, om=3.0, slope=0.0, start=2019))
    shared = set(a) & set(b)
    assert shared
    assert all(a[r] != b[r] for r in shared)
    assert all("17-year corn run" in a[r] and "6-year corn run" in b[r] for r in shared)
    # healthy soil is called out as such, with its number
    assert any("3.2% organic matter" in p.reason for p in rank_plans(HEALTHY))
    # a field with nothing remarkable still gets tied to what it grows
    assert any("you already grow" in p.reason for p in rank_plans(CORN_SOY_FIELD))
    # no history at all: generic, but still a proper sentence
    for plan in rank_plans(NO_INFO):
        check_reason(plan.reason)


def test_the_three_reasons_for_one_field_are_not_identical():
    for field in (WORN_OUT, HEALTHY, CORN_SOY_FIELD, DRYLAND, NO_INFO, make_field(["corn"] * 17, om=3.3, slope=5.6)):
        reasons = [p.reason for p in rank_plans(field)]
        assert len(set(reasons)) == 3, reasons


def test_alfalfa_stand_is_described_as_a_stand():
    plans = rank_plans(make_field(["corn", "winter wheat"] * 5 + ["alfalfa"] * 6, om=2.0, slope=0.5))
    assert all(p.rotation[0] != "alfalfa" for p in plans)
    assert all("6-year alfalfa stand" in p.reason for p in plans)


# --------------------------------------------------------------------------------------
# robustness
# --------------------------------------------------------------------------------------

UNKNOWN_CROPS = ["cotton", "grass/pasture", "double crop: winter wheat/sorghum", "triticale", "other hay", "x"]


def test_unknown_crop_names_do_not_crash():
    only_unknown = make_field(["grass/pasture"] * 17, om=1.0, slope=7.0)
    plans = rank_plans(only_unknown)
    check_plans(plans)
    assert all("grass/pasture" not in p.reason for p in plans), "we only talk about crops the table knows"

    mixed = make_field(["cotton", "corn", "double crop: winter wheat/sorghum", "triticale", "corn", "other hay"])
    check_plans(rank_plans(mixed))
    assert analyse_field(mixed).dryness == 0.0, "too few known crops to call it dryland"

    # a long cotton run is noticed but cannot penalise anything (no candidate grows cotton)
    cotton = analyse_field(make_field(["cotton"] * 10))
    assert cotton.stuck_crop == "cotton"
    assert all(planner.repeats_stuck_crop(r, cotton) == 0.0 for r in ROTATIONS)


@pytest.mark.parametrize(
    "crop_history",
    [
        {},
        {"2024": "corn"},
        {"2024": "quinoa"},
        {"1999": "corn", "2024": "corn"},
        {"not-a-year": "corn", "2020": "", "2021": None, 2022: "Corn ", "2023": 7},
        {"2010": "fallow", "2011": "fallow", "2012": "fallow", "2013": "fallow", "2014": "fallow"},
    ],
)
def test_odd_histories_do_not_crash(crop_history):
    for om, slope in [(None, None), (0.0, 0.0), (100.0, 250.0), (float("nan"), float("inf")), ("1.5", "x")]:
        check_plans(rank_plans(FieldInput("KS-000001", crop_history, om, slope, 1.0)))


def test_single_year_history_is_not_a_streak():
    ctx = analyse_field(make_field(["corn"], start=2024))
    assert (ctx.run_crop, ctx.run_years, ctx.stuck_crop, ctx.stuck_pressure) == ("corn", 1, None, 0.0)
    assert rank_plans(make_field(["corn"], start=2024))[0].rotation == list(FOUR_CROPS)


def test_many_random_fields_always_give_valid_plans_and_sentences():
    rng = random.Random(20055)
    names = sorted(ROTATION_CROPS - {"cover crop"}) + UNKNOWN_CROPS
    seen_tops = set()
    for _ in range(400):
        style = rng.random()
        years = [y for y in range(2008, 2025) if rng.random() > 0.15]
        if style < 0.3:  # monoculture-ish
            main = rng.choice(names)
            crops = {str(y): (main if rng.random() < 0.85 else rng.choice(names)) for y in years}
        elif style < 0.6:  # a repeating cycle
            cycle = [rng.choice(names) for _ in range(rng.randint(2, 4))]
            crops = {str(y): cycle[i % len(cycle)] for i, y in enumerate(years)}
        else:
            crops = {str(y): rng.choice(names) for y in years}
        om = None if rng.random() < 0.2 else round(rng.uniform(0.2, 5.0), 2)
        slope = None if rng.random() < 0.2 else round(rng.uniform(0.0, 12.0), 2)
        field = FieldInput("KS-000001", crops, om, slope, 40.0)
        plans = rank_plans(field)
        check_plans(plans)
        assert plans == rank_plans(field)
        assert len({p.reason for p in plans}) == 3
        seen_tops.add(tuple(plans[0].rotation))
    assert len(seen_tops) >= 4, "different fields should lead to different winners"
    assert CONTINUOUS_CORN not in seen_tops
