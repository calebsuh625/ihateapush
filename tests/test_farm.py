"""Whole-farm planner (Phase 4, item 3): api/planner/farm.py and POST /farm/plan.

The optimizer runs on hand-made and random little farms whose every combination can be listed, so its
answer is checked against brute force. The planner's side runs on the made-up climate tables of
test_planner_smart / test_api_smart, so nothing here needs data/. The last test uses the real tables
and fields and skips without them.
"""
from __future__ import annotations

import copy
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from api.planner import candidates as cand  # noqa: E402
from api.planner import farm, smart  # noqa: E402
from api.planner.crops import CROPS  # noqa: E402
from api.planner.legacy import FieldInput  # noqa: E402

import test_api_smart as api_tests  # noqa: E402
from test_api_smart import (  # noqa: E402,F401  (fixtures)
    basic_client, clean_engine, client, fields_file, no_tables, tables,
)
from test_planner_smart import (  # noqa: E402,F401
    IRRIGATED_HISTORY, S, W, F, engine,
)

DRY, WET = api_tests.DRY, api_tests.WET


# --- little farms whose every combination can be listed ------------------------------------------------

def options(acres, rotations, years=6):
    """One field. `rotations`: [(score, [water per year of the cycle], [return per year of the cycle])];
    every start year of every rotation is a plan; the natural start is the cycle as written."""
    key, cycle, natural, water, ret = [], [], [], [], []
    for r, (score, w, m) in enumerate(rotations):
        length = len(w)
        assert years % length == 0 and len(m) == length
        for start in range(length):
            key.append(score)
            cycle.append(r)
            natural.append(start == 0)
            water.append([w[(start + t) % length] for t in range(years)])
            ret.append([m[(start + t) % length] for t in range(years)])
    key = np.array(key, dtype=float)
    return farm.Options(float(acres), key, np.array(cycle), np.array(natural), np.array(water, dtype=float),
                        np.array(ret, dtype=float), int(np.argmax(key)))


def random_farm(seed, fields=3, rotations=3):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(fields):
        rots = []
        for _ in range(rotations):
            length = int(rng.choice([2, 3]))
            rots.append((round(float(rng.uniform(0.4, 0.8)), 6), rng.uniform(0, 20, length).round(1).tolist(),
                         rng.uniform(-60, 300, length).round(0).tolist()))
        out.append(options(round(float(rng.uniform(20, 200)), 1), rots))
    return out


def every_choice(opts):
    """(choices [n, fields], score [n], heaviest water year [n], leanest return year [n]) for every combination."""
    idx = np.array(list(itertools.product(*[range(len(o.key)) for o in opts])))
    area = sum(o.acres for o in opts)
    score = sum(o.acres * o.key[idx[:, f]] for f, o in enumerate(opts)) / area
    water = sum(o.acres * o.water[idx[:, f]] for f, o in enumerate(opts))
    ret = sum(o.acres * o.ret[idx[:, f]] for f, o in enumerate(opts))
    return idx, score, water.max(axis=1), ret.min(axis=1)


def brute_force(opts, cap=None, floor=None):
    """What the staged model should return, by listing everything: (limits met?, best score, the limits the
    closest plan is held to)."""
    _, score, peak, worst = every_choice(opts)
    w_scale, r_scale = farm._scales(opts)
    tol_w, tol_r = farm.TOLERANCE * w_scale, farm.TOLERANCE * r_scale
    ok = np.ones(len(score), dtype=bool)
    if cap is not None:
        ok &= peak <= cap + tol_w
    if floor is not None:
        ok &= worst >= floor - tol_r
    if ok.any():
        return True, float(score[ok].max()), cap, floor
    if cap is not None and not (peak <= cap + tol_w).any():
        cap = float(peak.min())                                       # the least the farm can pump in its heaviest year
    within = np.ones(len(score), dtype=bool) if cap is None else peak <= cap + tol_w
    if floor is not None and not (worst[within] >= floor - tol_r).any():
        floor = float(worst[within].max())                            # the best leanest year within that water limit
    ok = within & (True if floor is None else worst >= floor - tol_r)
    return False, float(score[ok].max()), cap, floor


def limits_for(opts, seed):
    """A water budget and a minimum return around what the farm can do, some loose, some impossible."""
    rng = np.random.default_rng(1000 + seed)
    alone = [o.own for o in opts]
    water, ret = farm.farm_totals(opts, alone)
    cap = None if rng.random() < 0.25 else float(rng.uniform(0.2, 1.1) * water.max())
    floor = None if rng.random() < 0.25 else float(ret.mean() * rng.uniform(0.2, 1.4))
    return cap, floor


@pytest.mark.parametrize("seed", range(40))
def test_the_optimum_is_what_brute_force_finds(seed):
    opts = random_farm(seed, fields=2 + seed % 3)
    cap, floor = limits_for(opts, seed)
    met, best, held_cap, held_floor = brute_force(opts, cap, floor)
    solution = farm.solve_farm(opts, water_cap=cap, income_floor=floor)
    assert solution.limits_met is met
    assert solution.proven and solution.gap == pytest.approx(0.0, abs=1e-9) and not solution.stopped_early
    assert solution.score == pytest.approx(best, abs=1e-9)
    assert solution.score == pytest.approx(farm.farm_score(opts, solution.choice), abs=1e-12)
    # the plan it returns really is held to the limits (the asked ones, or the closest it can get)
    assert all(farm.meets(opts, solution.choice, held_cap, held_floor))
    assert all(farm.meets(opts, solution.choice, cap, floor)) is met
    if not met:
        if held_cap != cap:
            assert solution.water["achievable"]["value"] == pytest.approx(held_cap, rel=1e-9)
            assert solution.water["achievable"]["proven"] and solution.water["why"] == "cannot_be_met"
        if held_floor != floor:
            assert solution.income["achievable"]["value"] == pytest.approx(held_floor, rel=1e-9, abs=1e-6)
            assert solution.income["achievable"]["proven"] and solution.income["why"] == "cannot_be_met"
        assert (held_cap != cap) or (held_floor != floor)


@pytest.mark.parametrize("seed", range(40, 60))
def test_start_years_move_no_more_acres_than_the_limits_need(seed):
    opts = random_farm(seed, fields=3)
    cap, floor = limits_for(opts, seed)
    met, best, held_cap, held_floor = brute_force(opts, cap, floor)
    solution = farm.solve_farm(opts, water_cap=cap, income_floor=floor)
    idx, score, peak, worst = every_choice(opts)
    w_scale, r_scale = farm._scales(opts)
    ok = np.ones(len(score), dtype=bool)
    if held_cap is not None:
        ok &= peak <= held_cap + farm.TOLERANCE * w_scale
    if held_floor is not None:
        ok &= worst >= held_floor - farm.TOLERANCE * r_scale
    same = np.all([opts[f].cycle[idx[:, f]] == opts[f].cycle[solution.choice[f]] for f in range(len(opts))], axis=0)
    kept = sum(o.acres * o.natural[idx[:, f]] for f, o in enumerate(opts))
    mine = sum(o.acres * bool(o.natural[j]) for o, j in zip(opts, solution.choice))
    assert mine == pytest.approx(float(kept[ok & same].max()), abs=1e-9)


def test_pruning_never_drops_a_plan_that_is_needed():
    for seed in range(20):
        o = random_farm(seed, fields=1, rotations=5)[0]
        for flags in ((True, True, True), (True, True, False), (True, False, True), (False, True, True),
                      (False, True, False), (False, False, True)):
            kept = set(farm.undominated(o, *flags).tolist())
            for j in range(len(o.key)):
                if j in kept:
                    continue
                # some kept plan is at least as good in every measure in play
                assert any((not flags[0] or o.key[k] >= o.key[j])
                           and (not flags[1] or (o.water[k] <= o.water[j]).all())
                           and (not flags[2] or (o.ret[k] >= o.ret[j]).all()) for k in kept)
    one = options(10, [(0.7, [5, 5], [10, 10]), (0.7, [5, 5], [10, 10]), (0.6, [9, 9], [1, 1])])
    assert farm.undominated(one, True, True, True).tolist() == [0]            # equal plans: the first is kept


# --- each limit, by hand --------------------------------------------------------------------------------

STEADY = (0.50, [0, 0, 0], [20, 20, 20])                     # scores less, returns the same every year
LUMPY = (0.80, [0, 0, 0], [60, 60, -30])                     # the first pick: two good years, one bad


def test_staggering_beats_same_start_under_a_minimum_return():
    """Three equal fields in a 3-year rotation with one losing year. Started together the farm loses money
    every third year; one field in each start year gives the same return every year."""
    opts = [options(100, [LUMPY, STEADY]) for _ in range(3)]
    alone = [o.own for o in opts]
    assert farm.farm_totals(opts, alone)[1].min() == pytest.approx(-9000)
    solution = farm.solve_farm(opts, income_floor=5000.0)
    assert solution.limits_met and solution.proven
    assert solution.score == pytest.approx(0.80)                                  # nobody leaves the first pick
    assert sorted(int(np.flatnonzero(o.cycle == 0).tolist().index(j)) for o, j in zip(opts, solution.choice)) == [0, 1, 2]
    assert farm.farm_totals(opts, solution.choice)[1].tolist() == pytest.approx([9000.0] * 6)
    assert solution.income["binds"] is True and solution.income["why"] == "alone_breaks_it"
    # the same farm when every field must keep its natural start year: it has to give up the first pick
    together = [farm.Options(o.acres, o.key[o.natural], o.cycle[o.natural], o.natural[o.natural],
                             o.water[o.natural], o.ret[o.natural], 0) for o in opts]
    same_start = farm.solve_farm(together, income_floor=5000.0)
    assert same_start.limits_met and same_start.score < solution.score - 0.05
    assert same_start.score == pytest.approx(0.5)             # every field in the steady, lower-scoring rotation


def test_only_as_many_fields_shift_as_the_minimum_needs():
    opts = [options(100, [LUMPY, STEADY]) for _ in range(3)]
    solution = farm.solve_farm(opts, income_floor=-1000.0)       # one field out of step is enough: 60+60-30 = 90 an acre
    shifted = [j for o, j in zip(opts, solution.choice) if not o.natural[j]]
    assert solution.limits_met and solution.score == pytest.approx(0.80) and len(shifted) == 1


THIRSTY = (0.80, [20, 20], [300, 300])
LIGHT = (0.60, [8, 8], [150, 150])


def test_the_water_budget_binds_and_only_the_cheapest_switch_is_made():
    opts = [options(100, [THIRSTY, LIGHT]), options(50, [THIRSTY, LIGHT]), options(200, [(0.7, [0, 0], [30, 30])])]
    assert farm.farm_totals(opts, [o.own for o in opts])[0].max() == pytest.approx(3000)
    solution = farm.solve_farm(opts, water_cap=2500.0)
    # switching the 50-acre field saves 600 (enough) and costs less score than switching the 100-acre one
    assert solution.limits_met and solution.proven
    assert [int(o.cycle[j]) for o, j in zip(opts, solution.choice)] == [0, 1, 0]
    assert solution.score == pytest.approx((100 * 0.8 + 50 * 0.6 + 200 * 0.7) / 350)
    assert solution.water["binds"] is True and solution.water["why"] == "alone_breaks_it"
    assert solution.income["why"] == "not_set" and solution.income["binds"] is False
    loose = farm.solve_farm(opts, water_cap=3000.0)                        # exactly what each-alone pumps: met, no change
    assert loose.choice == loose.alone and loose.water["binds"] is False and loose.water["why"] == "no_cost"
    assert loose.stages[0]["stage"] == "each_alone"


def test_a_limit_that_each_alone_meets_can_still_cost_score():
    """One field. Its first pick breaks the minimum return; the next best needs more water than the budget.
    Each-alone meets the budget, yet the budget is what holds the score down."""
    first = (1.0, [1, 1], [10, 0])
    thirsty = (0.9, [3, 3], [6, 6])
    last = (0.5, [1, 1], [5, 5])
    opts = [options(1, [first, thirsty, last], years=2)]
    tight = farm.solve_farm(opts, water_cap=2.0, income_floor=5.0)
    assert tight.limits_met and tight.score == pytest.approx(0.5)
    assert tight.income["why"] == "alone_breaks_it"
    assert tight.water["binds"] is True and tight.water["why"] == "costs_score"
    assert tight.water["score_without"] == pytest.approx(0.9)
    assert [s["stage"] for s in tight.stages][-1] == "without_water_budget"
    roomy = farm.solve_farm(opts, water_cap=5.0, income_floor=5.0)
    assert roomy.score == pytest.approx(0.9)
    assert roomy.water["binds"] is False and roomy.water["why"] == "no_cost"
    assert roomy.water["score_without"] == pytest.approx(0.9)


def test_limits_that_cannot_be_met_give_the_closest_plan():
    opts = [options(100, [THIRSTY, LIGHT]), options(100, [THIRSTY, LIGHT])]
    # the least this farm can pump is 8 inches on 200 acres
    dry_well = farm.solve_farm(opts, water_cap=1000.0)
    assert not dry_well.limits_met and dry_well.proven
    assert dry_well.water["met"] is False and dry_well.water["why"] == "cannot_be_met"
    assert dry_well.water["achievable"] == {"value": pytest.approx(1600.0), "proven": True, "bound": pytest.approx(1600.0)}
    assert [int(o.cycle[j]) for o, j in zip(opts, dry_well.choice)] == [1, 1]
    # a minimum no plan reaches: the best is both fields in the thirsty rotation
    greedy = farm.solve_farm(opts, income_floor=100000.0)
    assert not greedy.limits_met and greedy.income["achievable"]["value"] == pytest.approx(60000.0)
    assert greedy.choice == greedy.alone and greedy.income["alone_ok"] is None        # no water budget to blame
    # both: the budget is kept first, then the return gets as close as it can; without the budget it could be met
    both = farm.solve_farm(opts, water_cap=2800.0, income_floor=50000.0)
    assert not both.limits_met and both.water["met"] is True and both.income["met"] is False
    assert farm.farm_totals(opts, both.choice)[0].max() <= 2800.0
    assert both.income["achievable"]["value"] == pytest.approx(45000.0)              # one field each way
    assert both.income["alone_ok"] is True
    assert both.water["binds"] is True
    # both impossible, and the minimum impossible even with all the water
    neither = farm.solve_farm(opts, water_cap=100.0, income_floor=1e6)
    assert neither.water["met"] is False and neither.income["met"] is False
    assert neither.water["achievable"]["value"] == pytest.approx(1600.0)
    assert neither.income["achievable"]["value"] == pytest.approx(30000.0) and neither.income["alone_ok"] is False


def test_the_same_farm_twice_gives_the_same_plan():
    for seed in (3, 7, 11):
        opts = random_farm(seed, fields=4, rotations=4)
        cap, floor = limits_for(opts, seed)
        first = farm.solve_farm(opts, water_cap=cap, income_floor=floor)
        second = farm.solve_farm(copy.deepcopy(opts), water_cap=cap, income_floor=floor)
        assert first.choice == second.choice and first.score == second.score
        assert first.water == second.water and first.income == second.income


def test_a_stage_that_runs_out_of_time_is_reported_not_hidden(monkeypatch):
    opts = [options(100, [THIRSTY, LIGHT]), options(50, [THIRSTY, LIGHT])]
    real = farm.optimise

    def slow(*args, **kwargs):
        solved = real(*args, **kwargs)
        if solved.found and solved.binaries:                   # as if the time ran out before the proof
            solved.status, solved.bound = "feasible", solved.objective + 0.01
        return solved

    monkeypatch.setattr(farm, "optimise", slow)
    solution = farm.solve_farm(opts, water_cap=2500.0)
    assert solution.limits_met and not solution.proven and solution.stopped_early
    assert solution.gap == pytest.approx(0.01) and solution.bound == pytest.approx(solution.score + 0.01)


def test_a_known_plan_is_kept_when_the_solver_finds_nothing_better():
    opts = [options(100, [THIRSTY, LIGHT]), options(50, [THIRSTY, LIGHT])]
    columns = [np.arange(len(o.key)) for o in opts]
    area = 150.0
    coef = [o.acres / area * o.key for o in opts]
    solved = farm.optimise(opts, columns, "linear", coef=coef, water_cap=2500.0, seconds=2.0, hint=[2, 2])
    assert solved.status == "optimal" and solved.objective >= (100 * 0.6 + 50 * 0.6) / 150
    assert farm.optimise(opts, columns, "linear", coef=coef, water_cap=10.0, seconds=2.0).status == "infeasible"
    lone = farm.optimise(opts, [np.array([0]), np.array([2])], "peak_water", seconds=1.0)       # nothing to choose
    assert lone.status == "optimal" and lone.objective == pytest.approx(2400.0) and lone.binaries == 0


# --- on the planner's numbers (made-up climate tables) -------------------------------------------------

DRY_HISTORY = {str(2008 + i): crop for i, crop in enumerate(([W, S, F] * 6)[:17])}


def a_farm(dry=3, wet=2):
    fields = [FieldInput(f"KS-00010{i}", DRY_HISTORY, 1.8, 1.0, 40.0 + 15.0 * i) for i in range(dry)]
    fields += [FieldInput(f"KS-00020{i}", IRRIGATED_HISTORY, 1.8, 1.0, 30.0 + 10.0 * i) for i in range(wet)]
    return fields


def stable(body):
    """The answer without the clock."""
    body = copy.deepcopy(body)
    for key in ("time_s", "note"):
        body["solver"].pop(key)
    for stage in body["solver"]["stages"]:
        stage.pop("seconds")
    body["summary"] = [s for s in body["summary"] if "seconds" not in s]
    return body


def test_the_plan_table_is_the_engine_year_by_year(engine):
    for irrigated in (True, False):
        table = farm.plan_table(engine, irrigated, "ssp245", 2050)
        ev = engine.evaluate(irrigated, "ssp245", 2050)
        assert table.cycles == cand.candidates(irrigated)
        # the 12-year mean of a plan is the plan's own figure on its card, whatever the start year
        assert table.ret.mean(axis=1) == pytest.approx(ev.profit[table.cycle], abs=1e-6)
        assert table.water.mean(axis=1) * smart.MM_PER_INCH == pytest.approx(ev.irrigation[table.cycle], abs=1e-6)
        assert len(set(table.crops)) == len(table.crops) and all(len(c) == 12 for c in table.crops)
        for (c, start), j in table.index.items():
            cycle = table.cycles[c]
            assert table.crops[j] == tuple(cycle[(start + t) % len(cycle)] for t in range(12))
            assert int(table.cycle[j]) == c and int(table.start[j]) <= start
        assert table.natural.sum() == len(table.cycles) and (table.start[table.natural] == 0).all()
        # the per-acre pumping limit is inside every plan-year
        assert table.water.max() <= engine.irrigation_limit_mm / smart.MM_PER_INCH + 1e-9
        if not irrigated:
            assert table.water.max() == 0.0


def test_without_limits_every_field_keeps_its_own_first_pick(engine):
    fields = a_farm()
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(), horizon=2050)
    json.dumps(body)                                                  # plain JSON all the way down
    assert body["status"] == "no_limits" and body["limits_met"] is True
    assert body["solver"]["status"] == "optimal" and body["solver"]["gap"] == 0.0
    assert [s["stage"] for s in body["solver"]["stages"]] == ["each_alone"]
    assert body["plan"] == {**body["alone"], "changed_fields": 0, "shifted_fields": 0} and body["score_cost"] == 0.0
    assert body["farm"]["fields"] == 5 and body["farm"]["irrigated_fields"] == 2 and body["farm"]["dryland_fields"] == 3
    assert body["farm"]["area_acres"] == pytest.approx(sum(f.area_ha for f in fields) * farm.ACRES_PER_HA, abs=0.06)
    assert body["solver"]["groups"] == 2                              # one evaluation per practice, soil and slope
    for entry, field in zip(body["fields"], fields):
        own = smart.rank_plans_smart(field, engine=engine, traits=smart.FieldTraits(), horizon=2050).plans[0].to_dict()
        assert entry["field_id"] == field.field_id
        assert {k: entry["plan"][k] for k in own} == own and entry["plan"] == entry["own_pick"]
        assert entry["plan"]["start_year"] == 1 and entry["plan"]["years"][:4] == own["rotation"]
        assert entry["same_rotation"] and entry["same_start"] and not entry["differs"] and entry["gives_up"] is None
        assert entry["area_acres"] == pytest.approx(field.area_ha * farm.ACRES_PER_HA, abs=0.05)
    assert all(limit["set"] is False and limit["binds"] is False for limit in body["limits"].values())
    assert set(body["settings"]) >= {"weights", "scenario", "horizon", "window", "models", "span_years",
                                     "water_budget_acre_in", "min_income", "profit_basis"}


def test_farm_totals_are_the_fields_added_up(engine):
    fields = a_farm()
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits())
    assert [y["n"] for y in body["plan"]["years"]] == list(range(1, 13))
    for t, year in enumerate(body["plan"]["years"]):
        water = np.zeros(len(engine.climate.models))
        ret = np.zeros(len(engine.climate.models))
        acres = {}
        for entry, field in zip(body["fields"], fields):
            crop, prev = entry["plan"]["years"][t], entry["plan"]["years"][t - 1]
            numbers = engine.crop_year(entry["irrigated"], "ssp245", 2025, crop, prev)
            a = field.area_ha * farm.ACRES_PER_HA
            water += a * numbers["irrigation_mm"] / smart.MM_PER_INCH
            ret += a * numbers["profit"]
            acres[crop] = acres.get(crop, 0.0) + a
        assert year["water_acre_in"] == {"mid": round(water.mean()), "low": round(water.min()), "high": round(water.max())}
        assert year["return"]["mid"] == pytest.approx(ret.mean(), abs=50) and year["return"]["mid"] % 100 == 0
        assert year["return"]["low"] == pytest.approx(ret.min(), abs=50)
        assert year["return"]["high"] == pytest.approx(ret.max(), abs=50)
        assert year["return"]["low"] <= year["return"]["mid"] <= year["return"]["high"]
        assert year["acres"] == {crop: round(acres[crop]) for crop in CROPS if crop in acres}
        assert year["water_ok"] is None and year["return_ok"] is None
    # twelve years hold whole rounds of every cycle: the farm's average year is the cards' per-acre figures
    per_acre = sum(e["plan"]["numbers"]["profit_per_acre"] * e["area_acres"] for e in body["fields"])
    assert body["plan"]["average"]["return"] == pytest.approx(per_acre, rel=0.01, abs=200)
    area = sum(e["area_acres"] for e in body["fields"])
    assert body["plan"]["score"] == pytest.approx(
        sum(e["plan"]["scores"]["robust"] * e["area_acres"] for e in body["fields"]) / area, abs=0.0015)


def test_a_minimum_return_is_met_by_staggering_the_start_years(engine):
    fields = a_farm(dry=4, wet=0)
    free = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits())
    worst, mean = free["alone"]["worst_return"]["value"], free["alone"]["average"]["return"]
    assert worst < mean                                                # all fallow in the same year
    floor = round((worst + mean) / 2, -2)
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(), min_income=floor)
    assert body["status"] == "limits_met" and body["solver"]["status"] == "optimal"
    assert body["plan"]["changed_fields"] == 0 and body["plan"]["shifted_fields"] >= 1
    assert body["score_cost"] == 0.0 and body["plan"]["score"] == body["alone"]["score"]
    assert all(year["return_ok"] for year in body["plan"]["years"]) and body["plan"]["return_ok"] is True
    assert body["alone"]["return_ok"] is False and body["alone"]["return_years_under"]
    assert body["alone"]["worst_return"]["margin"] == worst - floor
    limit = body["limits"]["return"]
    assert limit["set"] and limit["met"] and limit["binds"] is True and limit["why"] == "alone_breaks_it"
    assert limit["limit"] == floor and limit["tightest"]["margin"] >= 0 and limit["achievable"] is None
    assert limit["alone"] == {"met": False, "tightest": body["alone"]["worst_return"],
                              "years_broken": body["alone"]["return_years_under"]}
    assert "is met in every year" in limit["sentence"] and "over direct costs" in limit["sentence"]
    shifted = [e for e in body["fields"] if not e["same_start"]]
    assert all(e["same_rotation"] and e["differs"] and e["gives_up"] is None for e in shifted)
    assert all(f"started in year {e['plan']['start_year']}" in e["sentence"] for e in shifted)
    assert all(e["plan"]["years"][0] == e["plan"]["cycle"][e["plan"]["start_year"] - 1] for e in body["fields"])
    assert "profit" not in " ".join(body["summary"]).lower() and "income" not in " ".join(body["summary"]).lower()


def test_a_water_budget_changes_irrigated_fields_and_leaves_dryland_alone(engine):
    fields = a_farm(dry=2, wet=3)
    free = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits())
    budget = round(0.8 * free["alone"]["peak_water"]["value"])
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(), water_budget_acre_in=budget)
    assert body["status"] == "limits_met"
    assert body["plan"]["peak_water"]["value"] <= budget and body["plan"]["peak_water"]["margin"] >= 0
    assert all(year["water_ok"] for year in body["plan"]["years"])
    assert body["alone"]["water_ok"] is False and body["limits"]["water"]["why"] == "alone_breaks_it"
    assert all(e["same_start"] for e in body["fields"] if not e["irrigated"])
    changed = [e for e in body["fields"] if not e["same_rotation"]]
    assert changed and all(e["irrigated"] for e in changed) and body["plan"]["changed_fields"] == len(changed)
    for e in changed:
        gives = e["gives_up"]
        assert gives["score"] == round(e["plan"]["scores"]["robust"] - e["own_pick"]["scores"]["robust"], 3) <= 0
        assert gives["profit_per_acre"] == round(e["plan"]["numbers"]["profit_per_acre"]
                                                 - e["own_pick"]["numbers"]["profit_per_acre"])
        assert gives["irrigation_in"] == round(e["plan"]["numbers"]["irrigation_in"]
                                               - e["own_pick"]["numbers"]["irrigation_in"], 1)
        assert e["sentence"].startswith(f"Not its own first pick ({e['own_pick']['name']})")
    assert body["score_cost"] == round(body["alone"]["score"] - body["plan"]["score"], 3) >= 0
    # an override turns an irrigated field into a dryland one, which pumps nothing
    flipped = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(),
                             irrigated={f.field_id: False for f in fields})
    assert flipped["farm"]["irrigated_fields"] == 0 and flipped["plan"]["peak_water"]["value"] == 0
    assert all(e["irrigation_source"] == "override" for e in flipped["fields"])


def test_limits_the_farm_cannot_meet_are_said_plainly_and_the_best_value_can_be_asked_for(engine):
    fields = a_farm(dry=2, wet=2)
    free = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits())
    # no water at all, on a farm with irrigated fields
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(), water_budget_acre_in=0)
    water = body["limits"]["water"]
    assert body["status"] == "limits_not_met" and body["limits_met"] is False
    assert water["met"] is False and water["binds"] is True and water["why"] == "cannot_be_met"
    best = water["achievable"]
    assert best["proven"] and 0 < best["value"] < free["alone"]["peak_water"]["value"]
    assert body["plan"]["peak_water"]["value"] <= best["value"] and body["plan"]["water_ok"] is False
    assert f"cannot be met: the least this farm can pump in its heaviest year is about {best['value']:,}" in water["sentence"]
    assert water["sentence"] in body["summary"] and any("closest the farm can get" in s for s in body["summary"])
    again = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(), water_budget_acre_in=best["value"])
    assert again["status"] == "limits_met" and again["plan"]["peak_water"]["value"] <= best["value"]
    assert farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(),
                          water_budget_acre_in=best["value"] - 2)["status"] == "limits_not_met"
    # a minimum return far above anything the farm returns
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(),
                          min_income=10 * abs(free["alone"]["average"]["return"]) + 1e6)
    ret = body["limits"]["return"]
    assert body["status"] == "limits_not_met" and ret["met"] is False and ret["why"] == "cannot_be_met"
    assert ret["achievable"]["value"] % 100 == 0 and "alone_ok" in ret and ret["alone_ok"] is None
    assert "cannot be met" in ret["sentence"] and "leanest year" in ret["sentence"]
    assert body["plan"]["worst_return"]["value"] >= ret["achievable"]["value"]
    again = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(), min_income=ret["achievable"]["value"])
    assert again["status"] == "limits_met"


def test_the_same_request_gives_the_same_answer(engine):
    fields = a_farm(dry=3, wet=3)
    free = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits())
    asked = dict(engine=engine, traits=smart.FieldTraits(),
                 water_budget_acre_in=round(0.85 * free["alone"]["peak_water"]["value"]),
                 min_income=round(0.6 * free["alone"]["average"]["return"], -2))
    first, second = farm.plan_farm(fields, **asked), farm.plan_farm(list(fields), **asked)
    assert first["solver"]["status"] == "optimal"
    assert stable(first) == stable(second)
    kept = farm.plan_farm_cached(fields, **asked)
    assert stable(kept) == stable(first) and farm.plan_farm_cached(list(fields), **asked) is kept
    assert farm.plan_farm_cached(fields, **{**asked, "min_income": None}) is not kept


def test_time_limit_answers_carry_the_gap_and_say_so(engine, monkeypatch):
    fields = a_farm(dry=2, wet=3)
    free = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits())
    real = farm.optimise

    def slow(*args, **kwargs):
        solved = real(*args, **kwargs)
        if solved.found and solved.binaries and args[2] == "linear":
            solved.status, solved.bound = "feasible", solved.objective + 0.004
        return solved

    monkeypatch.setattr(farm, "optimise", slow)
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(),
                          water_budget_acre_in=round(0.8 * free["alone"]["peak_water"]["value"]))
    solver = body["solver"]
    assert solver["status"] == "time_limit" and solver["optimal"] is False and solver["stopped_early"] is True
    assert 0 < solver["gap"] <= 0.004 + 1e-9 and solver["bound"] == pytest.approx(solver["score"] + solver["gap"], abs=2e-6)
    assert "stopped at its time limit" in solver["note"] and "not a proven best" in solver["note"]
    assert solver["note"] in body["summary"]


def test_farms_we_cannot_plan_are_refused_in_one_sentence(engine):
    one = a_farm(dry=1, wet=0)
    kwargs = dict(engine=engine, traits=smart.FieldTraits())
    with pytest.raises(farm.FarmError, match="at least one field"):
        farm.plan_farm([], **kwargs)
    many = [FieldInput(f"KS-{i:06d}", DRY_HISTORY, 1.8, 1.0, 20.0) for i in range(farm.MAX_FIELDS + 1)]
    with pytest.raises(farm.FarmError, match="at most 60 fields; this one has 61"):
        farm.plan_farm(many, **kwargs)
    with pytest.raises(farm.FarmError, match="listed more than once"):
        farm.plan_farm(one + one, **kwargs)
    for bad in (-1, float("nan"), float("inf"), "100", True):
        with pytest.raises(farm.FarmError, match="water budget"):
            farm.plan_farm(one, water_budget_acre_in=bad, **kwargs)
    for bad in (float("nan"), "lots", False):
        with pytest.raises(farm.FarmError, match="minimum yearly return"):
            farm.plan_farm(one, min_income=bad, **kwargs)
    with pytest.raises(farm.FarmError, match="not one of the farm's fields"):
        farm.plan_farm(one, irrigated={"KS-999999": True}, **kwargs)
    with pytest.raises(farm.FarmError, match="true or false"):
        farm.plan_farm(one, irrigated={one[0].field_id: "yes"}, **kwargs)
    with pytest.raises(farm.FarmError, match="no area on record"):
        farm.plan_farm([FieldInput("KS-000300", DRY_HISTORY, 1.8, 1.0, 0.0)], **kwargs)
    with pytest.raises(ValueError):
        farm.plan_farm(one, scenario="ssp126", **kwargs)
    with pytest.raises(ValueError):
        farm.plan_farm(one, weights={"soil": 0, "water": 0, "money": 0, "risk": 0}, **kwargs)
    assert farm.plan_farm(one, min_income=-1e9, **kwargs)["status"] == "limits_met"      # a negative minimum is allowed


def test_sixty_fields_are_planned_within_the_time_limit(engine):
    fields = [FieldInput(f"KS-{i:06d}", DRY_HISTORY if i % 3 else IRRIGATED_HISTORY, 1.8, 1.0 + (i % 4), 20.0 + 3.0 * i)
              for i in range(farm.MAX_FIELDS)]
    free = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits())
    assert free["solver"]["groups"] == 8 and free["farm"]["fields"] == 60       # 2 practices x 4 slopes
    body = farm.plan_farm(fields, engine=engine, traits=smart.FieldTraits(),
                          water_budget_acre_in=round(0.9 * free["alone"]["peak_water"]["value"]),
                          min_income=round(0.7 * free["alone"]["average"]["return"], -2))
    solver = body["solver"]
    assert solver["time_s"] <= farm.TIME_LIMIT_S + 3.0
    assert solver["status"] in ("optimal", "time_limit") and solver["gap"] >= 0
    if body["limits_met"]:
        assert body["plan"]["water_ok"] and body["plan"]["return_ok"]
    if solver["status"] == "time_limit":
        assert any("stopped at its time limit" in s for s in body["summary"])


# --- POST /farm/plan -----------------------------------------------------------------------------------

def post(client, **body):
    return client.post("/farm/plan", json=body)


def test_endpoint_plans_a_farm_of_fake_fields(client):
    ids = ["KS-000001", "KS-000004", "KS-000005"]
    response = post(client, field_ids=ids, weights={"soil": 1, "money": 3}, scenario="ssp585", horizon=2050)
    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["engine", "settings", "farm", "status", "limits_met", "summary", "limits", "plan", "alone",
                          "score_cost", "fields", "solver", "notes", "not_modelled"]
    assert body["engine"] == "smart" and [e["field_id"] for e in body["fields"]] == ids
    assert body["settings"]["scenario"] == "ssp585" and body["settings"]["horizon"] == 2050
    defaults = api_tests.config.DEFAULT_WEIGHTS                         # water and risk were not given
    assert body["settings"]["weights"]["money"] == pytest.approx(3 / (1 + 3 + defaults["water"] + defaults["risk"]),
                                                                 abs=1e-3)
    for entry in body["fields"]:                                       # each field's own pick is /plans' first plan
        plans = client.get(f"/field/{entry['field_id']}/plans",
                           params={"soil": 1, "money": 3, "scenario": "ssp585", "horizon": 2050}).json()
        assert {k: entry["own_pick"][k] for k in plans["plans"][0]} == plans["plans"][0]
        assert entry["irrigated"] == plans["settings"]["irrigated"]
    peak = body["alone"]["peak_water"]["value"]
    limited = post(client, field_ids=ids, water_budget_acre_in=round(0.8 * peak), min_income=0,
                   irrigated={"KS-000005": False}).json()
    assert limited["status"] in ("limits_met", "limits_not_met") and limited["limits"]["water"]["set"] is True
    assert limited["settings"]["water_budget_acre_in"] == round(0.8 * peak) and limited["settings"]["min_income"] == 0
    assert set(limited["limits"]["water"]) >= {"set", "limit", "unit", "met", "binds", "why", "binds_sentence",
                                               "tightest", "alone", "achievable", "score_without", "sentence"}


@pytest.mark.parametrize("body, said", [
    ([1, 2], "JSON object"),
    ({}, "field_ids must be a list"),
    ({"field_ids": []}, "field_ids must be a list"),
    ({"field_ids": "KS-000001"}, "field_ids must be a list"),
    ({"field_ids": ["KS-000001", 7]}, "field_ids must be a list"),
    ({"field_ids": [f"KS-{i:06d}" for i in range(61)]}, "at most 60 fields; this one has 61"),
    ({"field_ids": ["KS-000001", "KS-000001"]}, "listed more than once"),
    ({"field_ids": ["KS-000001"], "water_budget": 5}, "Unknown setting \"water_budget\""),
    ({"field_ids": ["KS-000001"], "weights": {"soil": -1}}, "soil priority"),
    ({"field_ids": ["KS-000001"], "weights": {"soil": "high"}}, "soil priority"),
    ({"field_ids": ["KS-000001"], "weights": {"yield": 1}}, "weights must be an object"),
    ({"field_ids": ["KS-000001"], "weights": {"soil": 0, "water": 0, "money": 0, "risk": 0}}, "above zero"),
    ({"field_ids": ["KS-000001"], "scenario": "ssp126"}, "scenario must be"),
    ({"field_ids": ["KS-000001"], "horizon": 2100}, "year must be between"),
    ({"field_ids": ["KS-000001"], "horizon": "2050"}, "year must be between"),
    ({"field_ids": ["KS-000001"], "water_budget_acre_in": -5}, "water budget must be"),
    ({"field_ids": ["KS-000001"], "water_budget_acre_in": "all"}, "water budget must be"),
    ({"field_ids": ["KS-000001"], "min_income": "enough"}, "minimum yearly return must be"),
    ({"field_ids": ["KS-000001"], "irrigated": {"KS-000001": "yes"}}, "true or false"),
    ({"field_ids": ["KS-000001"], "irrigated": {"KS-000002": True}}, "not one of the farm's fields"),
])
def test_endpoint_refuses_bad_requests_in_one_sentence(client, body, said):
    response = client.post("/farm/plan", json=body)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert said in detail and detail.endswith(".") and "\n" not in detail


def test_endpoint_needs_the_smart_planner(basic_client):
    response = post(basic_client, field_ids=["KS-000001"])
    assert response.status_code == 503 and "future-weather tables" in response.json()["detail"]
    assert post(basic_client, field_ids=["KS-999999"]).status_code == 404


def test_endpoint_unknown_field_and_broken_body(client):
    assert post(client, field_ids=["KS-000001", "KS-999999"]).status_code == 404
    assert client.post("/farm/plan", content=b"{not json", headers={"content-type": "application/json"}).status_code == 400
    # the other endpoints still answer next to it
    assert client.get(f"/field/{DRY}/plans").status_code == 200
    assert client.get(f"/field/{DRY}/score", params={"rotation": "wheat, milo, fallow"}).status_code == 200


# --- real tables and fields (skipped without data/) ----------------------------------------------------

REAL_FARM = ["KS-013847", "KS-013970", "KS-013989", "KS-013990", "KS-014041", "KS-014073", "KS-014092", "KS-014140",
             "KS-013881", "KS-013846", "KS-013944", "KS-013897"]              # 8 dryland and 4 irrigated neighbours


def test_real_tables_plan_a_mixed_farm_of_real_neighbours(monkeypatch):
    if not (api_tests.main.REAL_PATH.exists() or api_tests.main.FAKE_PATH.exists()):  # a fresh clone has no data/
        pytest.skip("no fields file (a fresh clone has no data/)")
    for var in list(api_tests.ENV_PATHS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PLANNER_ENGINE", raising=False)
    monkeypatch.delenv("FIELDS_PATH", raising=False)
    smart.reset()
    from fastapi.testclient import TestClient

    with TestClient(api_tests.main.create_app()) as c:
        health = c.get("/health").json()
        if health["is_fake"] or not health["climate"]:
            pytest.skip("real fields or climate tables not loaded")
        if any(c.get(f"/field/{i}").status_code != 200 for i in REAL_FARM):
            pytest.skip("the real fields of this farm are not in the store")
        free = c.post("/farm/plan", json={"field_ids": REAL_FARM}).json()
        assert free["status"] == "no_limits" and free["farm"]["fields"] == 12
        assert len(free["settings"]["models"]) == 6
        for entry in free["fields"]:
            plans = c.get(f"/field/{entry['field_id']}/plans").json()
            assert {k: entry["own_pick"][k] for k in plans["plans"][0]} == plans["plans"][0]
        peak, mean = free["alone"]["peak_water"]["value"], free["alone"]["average"]["return"]
        worst = free["alone"]["worst_return"]["value"]
        body = c.post("/farm/plan", json={"field_ids": REAL_FARM, "water_budget_acre_in": round(0.85 * peak),
                                          "min_income": round((worst + mean) / 2, -2)}).json()
        assert body["status"] == "limits_met"
        assert body["plan"]["water_ok"] and body["plan"]["return_ok"]
        assert body["alone"]["water_ok"] is False and body["alone"]["return_ok"] is False
        assert body["limits"]["water"]["binds"] is True and body["limits"]["return"]["binds"] is True
        assert all(y["water_acre_in"]["low"] <= y["water_acre_in"]["mid"] <= y["water_acre_in"]["high"]
                   and y["return"]["low"] <= y["return"]["mid"] <= y["return"]["high"] for y in body["plan"]["years"])
        assert body["solver"]["status"] in ("optimal", "time_limit") and body["solver"]["time_s"] < 13
        limit = body["settings"]["irrigation_limit_in"]
        assert all(e["plan"]["numbers"]["irrigation_in"] <= limit for e in body["fields"] if e["irrigated"])
        nothing = c.post("/farm/plan", json={"field_ids": REAL_FARM, "water_budget_acre_in": 0}).json()
        assert nothing["status"] == "limits_not_met" and nothing["limits"]["water"]["achievable"]["value"] > 0

