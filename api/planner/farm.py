"""Whole-farm planner (Phase 4, item 3): several fields planned together, as one farm, under farm limits.

    from api.planner.farm import plan_farm
    body = plan_farm(fields, weights=..., scenario="ssp245", horizon=2050,
                     water_budget_acre_in=4500, min_income=120000)          # POST /farm/plan

The choice
----------
Every field gets one PLAN = one of the candidate cycles for its practice (candidates.py: 105 irrigated,
17 dryland) started in one of the cycle's years. The start year matters: a 3-year wheat-sorghum-fallow
on three fields can have all three fallow in the same year, or one each year. The optimizer chooses
rotation and start year for every field at once to

    maximise   sum over fields of  acres x ranking score of the field's rotation  /  farm acres

(the ranking score is the "robust" score the single-field planner ranks by, for the farmer's weights),
subject to, in EVERY one of the next 12 years (12 holds whole rounds of 3- and 4-year cycles):

    total water pumped on the farm        <=  the water budget, acre-inches a year   (optional)
    total return over direct costs        >=  the minimum yearly return, dollars     (optional)

Both totals use the mean of the six climate models (the limits are checked on that mean); the answer
also carries the lowest and highest model for the farm total in every year. A year of a plan is that
crop after the crop the cycle grew the year before, in the climate window's average weather
(`Engine.crop_year`), so the 12 years show how the rotations' good and lean years line up, not a
forecast for calendar years. The per-acre pumping limit of the single-field planner (24 inches) is
already inside every plan's water numbers, so it still holds on each field.

"Each field planned alone" is the comparison: every field takes its own first pick (plan 1 of
GET /field/{id}/plans for the same settings) in its NATURAL start year, which is year 1 of the cycle
as its plan card shows it. That is our reading of "natural"; the start year is not matched to what
the field grew last (the per-field "How to get there" does that).

The solver
----------
A mixed-integer program (one 0/1 variable per field and plan, up to 24 farm rows) solved with Google
OR-Tools: SCIP first, then CP-SAT on the same model, started from SCIP's best plan (`SOLVER_BACKENDS`).
SCIP finds a plan that meets the limits quickly, also on a big farm; CP-SAT is better at reaching and
proving the optimum on a small, tight one. Both run deterministically. No greedy pass decides anything:
a plan is only ever reported as "optimal" when a solver proved it. Three things keep it fast:

* fields of the same practice, soil erodibility and slope share one evaluation (the planner is
  county-level beyond those);
* a plan is dropped for a field when another plan of that field is at least as good in the score and
  in every year of every limit that is set (it could never be needed), which is exact;
* when every field's own first pick already meets the limits, that is the optimum and nothing is solved.

Stages, each a MILP with its own share of the 10-second limit (`STAGE_SECONDS`):

1. "score": the highest farm score that meets the limits. Done when it finds a plan.
2. When no plan meets the limits (proven, or none found in time), the closest the farm can get, in a
   fixed order that is OUR assumption and is said in the answer: the water budget first (a well or a
   water right cannot be stretched), then the minimum return.
   "peak_water": the least the farm can pump in its heaviest year. If that is over the budget, the
   budget cannot be met and the least peak takes its place.
   "worst_return": the best return the farm can have in its leanest year within that water limit. If
   that is under the minimum, the minimum cannot be met and the best value takes its place.
   ("worst_return_alone": the same without the water budget, to say whether the minimum fails on its
   own or only together with the budget.)
   "score_closest": the highest farm score among the plans that get that close.
3. "start_years": with the rotations fixed, the start years are chosen again so that as many acres as
   possible keep their natural start year. The score does not depend on the start year, so this
   changes nothing but removes shifts the limits do not need.
4. "without_water_budget" / "without_min_return": only when it is needed to tell whether a limit that
   each-field-alone already meets still costs score.

A stage that hits its time share returns the best plan found and the solver's bound; the answer says
so and gives the optimality gap in score points.

A limit BINDS when (a) it cannot be met, (b) each field planned alone would break it, so rotations or
start years had to change, or (c) dropping it alone would let the farm score higher than the solver's
bound allows with it. `binds` is null when (c) could not be settled in the time allowed.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field as dataclass_field
from typing import Mapping, Sequence

import numpy as np

from api.planner import candidates as cand
from api.planner import config, futures, smart
from api.planner.crops import CROPS
from api.planner.legacy import FieldInput
from api.planner.smart import MM_PER_INCH, Engine, Weights
from api.planner.traits import FieldTraits

MAX_FIELDS = 60
SPAN_YEARS = 12                 # holds whole rounds of every candidate cycle (3 and 4 years)
TIME_LIMIT_S = 10.0             # for one request, all stages together
ACRES_PER_HA = 2.47105
TOLERANCE = 2e-6                # a limit counts as met within this share of the farm's largest possible total
DOLLAR_STEP = 100               # farm dollar totals are rounded to this (per-acre figures are whole dollars)
# Two OR-Tools solvers, one after the other on the same model, each starting from the best plan so far:
# (name, its parameters, share of the stage's remaining seconds; the last one gets all that is left).
# SCIP finds a plan that meets the limits quickly, also on a big farm; CP-SAT is better at reaching and
# proving the optimum on a tight small one. Both are deterministic as set here (CP-SAT interleaved).
SOLVER_BACKENDS = (("scip", "", 0.4), ("sat", "num_workers:8,interleave_search:true", 1.0))
MIN_BACKEND_SECONDS = 0.1
# the most seconds each stage may take; a stage never gets more than what is left of TIME_LIMIT_S
STAGE_SECONDS = {"score": 5.0, "peak_water": 1.5, "worst_return": 2.5, "worst_return_alone": 1.0,
                 "score_closest": 2.0, "start_years": 1.0, "without": 1.5}
MIN_STAGE_SECONDS = 0.25

assert all(SPAN_YEARS % length == 0 for length in cand.CYCLE_LENGTHS), "the span must hold whole cycles"

NATURAL_START_NOTE = (
    "\"Each field planned alone\" starts every field in year 1 of its rotation as its plan card shows it. Start "
    "years are not matched to what a field grew last; \"How to get there\" on a field shows the way into its rotation."
)
YEARS_NOTE = (
    "Every year gets the average weather of the climate window, so years 1 to 12 are not forecasts for calendar "
    "years: they show how the rotations' good and lean years line up across the farm."
)
MODELS_NOTE = (
    "The limits are checked on the average of the climate models. The range next to each farm total is the lowest "
    "and the highest model for the whole farm in that year."
)
SHARED_NOTE = (
    "Fields with the same practice (irrigated or dryland), soil erodibility and slope get the same numbers per acre; "
    "beyond those the planner works at county level."
)
ORDER_NOTE = (
    "When the limits cannot all be met, the plan keeps the water budget first and then gets as close to the minimum "
    "return as it can. That order is our assumption: a well or a water right cannot be stretched."
)
START_YEARS_NOTE = (
    "The solver ran out of time while tidying the start years, so a field or two may start in a different year "
    "without needing to. The limits and the farm score are not affected."
)
DRYLAND_MONEY_NOTE = (
    "On dryland the sign of the returns depends on the price years and the cost of fallow, so read the dryland "
    "dollars as a rough guide."
)
NOT_MODELLED = [
    "Moving machinery and labour between fields, and whether two fields can share a crop's harvest window.",
    "Storage, contracts, lease and crop insurance terms.",
    "Carrying water or money over from one year to the next: every year has to meet the limits on its own.",
    "What each field grew last year (the start year of a rotation is chosen freely).",
    "Runs of dry or wet years, and changes in prices and costs.",
]


class FarmError(ValueError):
    """The farm as asked for cannot be planned. The message is one plain sentence."""


# --- the optimizer: plain arrays in, a choice out ------------------------------------------------------

@dataclass(frozen=True)
class Options:
    """One field's possible plans. Arrays are per plan; `water` and `ret` are per acre and per year.

    Fields that share the same arrays (the same objects) share the dominance check."""

    acres: float
    key: np.ndarray        # [n] ranking score of each plan (the same for every start year of one rotation)
    cycle: np.ndarray      # [n] which rotation each plan is; plans of one rotation differ only in start year
    natural: np.ndarray    # [n] bool: the rotation in its natural start year
    water: np.ndarray      # [n, years] acre-inches an acre, mean of the climate models
    ret: np.ndarray        # [n, years] dollars an acre, mean of the climate models
    own: int               # the field's own first pick in its natural start year


@dataclass
class Solved:
    """One stage's answer. status: optimal / feasible (time limit, best found) / infeasible / unknown."""

    status: str
    choice: list | None
    objective: float | None
    bound: float | None
    seconds: float
    binaries: int

    @property
    def found(self) -> bool:
        return self.choice is not None


@dataclass
class Solution:
    choice: list                 # plan index per field
    alone: list                  # every field's own pick
    limits_met: bool
    proven: bool                 # every stage that decided the plan was solved to optimality
    stopped_early: bool          # a stage hit its time share
    score: float
    alone_score: float
    bound: float                 # no plan that gets as close to the limits scores above this
    gap: float                   # bound - score, >= 0
    water: dict                  # {"set", "met", "achievable", "binds", "why", "score_without"}
    income: dict                 # the same, plus "alone_ok" when it cannot be met within a water budget
    stages: list
    seconds: float


def _scales(options: Sequence[Options]) -> tuple:
    """(water, return): the largest total the farm could reach in a year; tolerances are a share of these."""
    water = sum(o.acres * float(np.abs(o.water).max()) for o in options)
    ret = sum(o.acres * float(np.abs(o.ret).max()) for o in options)
    return max(water, 1.0), max(ret, 1.0)


def farm_totals(options: Sequence[Options], choice: Sequence[int]) -> tuple:
    """(water[years], return[years]) for the whole farm: acre-inches and dollars, model mean."""
    water = sum(o.acres * o.water[j] for o, j in zip(options, choice))
    ret = sum(o.acres * o.ret[j] for o, j in zip(options, choice))
    return np.asarray(water, dtype=float), np.asarray(ret, dtype=float)


def farm_score(options: Sequence[Options], choice: Sequence[int]) -> float:
    area = sum(o.acres for o in options)
    return float(sum(o.acres * float(o.key[j]) for o, j in zip(options, choice)) / area)


def meets(options: Sequence[Options], choice: Sequence[int], water_cap: float | None,
          income_floor: float | None) -> tuple:
    """(water met?, return met?) in every year, within the tolerance. A limit that is not set is met."""
    w_scale, r_scale = _scales(options)
    water, ret = farm_totals(options, choice)
    return (water_cap is None or bool(water.max() <= water_cap + TOLERANCE * w_scale),
            income_floor is None or bool(ret.min() >= income_floor - TOLERANCE * r_scale))


def _year_dominance(o: Options, use_water: bool, use_return: bool) -> tuple:
    """([k, j] k is at least as good as j, [k, j] k is better than j) on the yearly measures in play."""
    n = len(o.key)
    as_good = np.ones((n, n), dtype=bool)
    better = np.zeros((n, n), dtype=bool)
    if use_water:
        as_good &= (o.water[:, None, :] <= o.water[None, :, :]).all(axis=2)
        better |= (o.water[:, None, :] < o.water[None, :, :]).any(axis=2)
    if use_return:
        as_good &= (o.ret[:, None, :] >= o.ret[None, :, :]).all(axis=2)
        better |= (o.ret[:, None, :] > o.ret[None, :, :]).any(axis=2)
    return as_good, better


def undominated(o: Options, use_key: bool, use_water: bool, use_return: bool, yearly: tuple | None = None) -> np.ndarray:
    """Indexes of the plans worth offering: a plan is dropped when another is at least as good in every
    measure in play (score, water in every year, return in every year) and better in one, or equal in
    all and earlier in the list. Exact: a dropped plan is never needed. `yearly`: `_year_dominance` of
    the same water and return tables, when the caller already has it."""
    n = len(o.key)
    as_good, better = yearly if yearly is not None else _year_dominance(o, use_water, use_return)
    if use_key:
        as_good = as_good & (o.key[:, None] >= o.key[None, :])
        better = better | (o.key[:, None] > o.key[None, :])
    earlier = np.arange(n)[:, None] < np.arange(n)[None, :]
    dominated = (as_good & (better | earlier)).any(axis=0)
    return np.flatnonzero(~dominated)


class _Columns:
    """The plans offered to each field in a stage. The yearly part of the dominance check is done once
    per shared table (a practice), the whole check once per shared score table (a group of fields)."""

    def __init__(self, options: Sequence[Options]):
        self.options = options
        self._cache: dict = {}
        self._yearly: dict = {}

    def keep(self, use_key: bool, use_water: bool, use_return: bool, extra: Sequence[int] | None = None) -> list:
        out = []
        for i, o in enumerate(self.options):
            tables = (id(o.water) if use_water else None, id(o.ret) if use_return else None, len(o.key))
            key = (id(o.key) if use_key else None, *tables)
            if key not in self._cache:
                if tables not in self._yearly:
                    self._yearly[tables] = _year_dominance(o, use_water, use_return)
                self._cache[key] = undominated(o, use_key, use_water, use_return, self._yearly[tables])
            cols = self._cache[key]
            if extra is not None and extra[i] not in cols:          # a known plan to start the solver from
                cols = np.sort(np.append(cols, extra[i]))
            out.append(cols)
        return out


def optimise(options: Sequence[Options], columns: Sequence, kind: str, *, coef: Sequence | None = None,
             water_cap: float | None = None, income_floor: float | None = None, seconds: float = 5.0,
             hint: Sequence[int] | None = None) -> Solved:
    """One mixed-integer program over "one plan per field" (OR-Tools: SCIP, then CP-SAT).

    kind  "linear"        maximise sum(coef[field][plan])
          "peak_water"    minimise the farm's heaviest pumping year
          "worst_return"  maximise the farm's leanest year's return
    `water_cap` / `income_floor` hold in every year. `columns[field]`: the plan indexes offered.
    `hint`: a plan per field to start from (kept as the answer when nothing better is found in time).
    """
    from ortools.linear_solver.python import model_builder as mb

    started = time.perf_counter()
    years = options[0].water.shape[1]
    w_scale, r_scale = _scales(options)
    fixed_water, fixed_ret, fixed_obj = np.zeros(years), np.zeros(years), 0.0
    model = mb.Model()
    variables, water_rows, ret_rows, objective, owner = [], [], [], [], []
    choice = [None] * len(options)
    for i, (o, cols) in enumerate(zip(options, columns)):
        cols = np.asarray(cols, dtype=int)
        if len(cols) == 1:                              # nothing to choose: a constant in every row
            j = int(cols[0])
            choice[i] = j
            fixed_water += o.acres * o.water[j]
            fixed_ret += o.acres * o.ret[j]
            fixed_obj += float(coef[i][j]) if coef is not None else 0.0
            continue
        xs = [model.new_bool_var(f"f{i}p{int(j)}") for j in cols]
        model.add(mb.LinearExpr.sum(xs) == 1)
        if hint is not None and hint[i] in cols:
            for x, j in zip(xs, cols):
                model.add_hint(x, 1.0 if j == hint[i] else 0.0)
        variables += xs
        owner += [(i, int(j)) for j in cols]
        water_rows.append(o.acres * o.water[cols] / w_scale)
        ret_rows.append(o.acres * o.ret[cols] / r_scale)
        if coef is not None:
            objective.append(np.asarray(coef[i], dtype=float)[cols])

    def measure(picked: list) -> float | None:
        """The objective of a choice, or None when it does not meet the limits (never trust a solver's word)."""
        water, ret = farm_totals(options, picked)
        if water_cap is not None and water.max() > water_cap + TOLERANCE * w_scale:
            return None
        if income_floor is not None and ret.min() < income_floor - TOLERANCE * r_scale:
            return None
        if kind == "linear":
            return float(sum(float(coef[i][j]) for i, j in enumerate(picked)))
        return float(water.max()) if kind == "peak_water" else float(ret.min())

    def done(status: str, picked: list | None, value: float | None, bound: float | None) -> Solved:
        if status == "optimal":
            bound = value
        return Solved(status, picked, value, bound, time.perf_counter() - started, len(variables))

    if not variables:
        value = measure(choice)
        return done("optimal", choice, value, None) if value is not None else done("infeasible", None, None, None)

    water_matrix, ret_matrix = np.concatenate(water_rows), np.concatenate(ret_rows)
    extreme = None
    if kind == "peak_water":
        extreme = model.new_num_var(0.0, math.inf, "peak")
    elif kind == "worst_return":
        extreme = model.new_num_var(-math.inf, math.inf, "worst")
    for t in range(years):
        if water_cap is not None or kind == "peak_water":
            water_t = mb.LinearExpr.weighted_sum(variables, water_matrix[:, t])
            if water_cap is not None:
                model.add(water_t <= (water_cap - fixed_water[t]) / w_scale)
            if kind == "peak_water":
                model.add(water_t - extreme <= -fixed_water[t] / w_scale)
        if income_floor is not None or kind == "worst_return":
            ret_t = mb.LinearExpr.weighted_sum(variables, ret_matrix[:, t])
            if income_floor is not None:
                model.add(ret_t >= (income_floor - fixed_ret[t]) / r_scale)
            if kind == "worst_return":
                model.add(ret_t - extreme >= -fixed_ret[t] / r_scale)
    if kind == "linear":
        model.maximize(mb.LinearExpr.weighted_sum(variables, np.concatenate(objective)))
    elif kind == "peak_water":
        model.minimize(extreme)
    else:
        model.maximize(extreme)

    smaller_is_better = kind == "peak_water"
    best_value, best_choice, bound, proven = None, None, None, False
    if hint is not None and all(hint[i] in np.asarray(cols) for i, cols in enumerate(columns)):
        value = measure(list(hint))                       # a known plan is the answer until a solver beats it
        if value is not None:
            best_value, best_choice = value, [int(j) for j in hint]
    backends = [(name, parameters, share) for name, parameters, share in SOLVER_BACKENDS
                if mb.Solver(name).solver_is_supported()]
    if not backends:
        raise RuntimeError("OR-Tools has none of its mixed-integer solvers on this machine")
    for n, (name, parameters, share) in enumerate(backends):
        left = float(seconds) - (time.perf_counter() - started)
        if n > 0 and left < MIN_BACKEND_SECONDS:
            break
        solver = mb.Solver(name)
        solver.set_time_limit_in_seconds(max(left if n == len(backends) - 1 else left * share, 0.05))
        if parameters:
            solver.set_solver_specific_parameters(parameters)
        status = solver.solve(model)
        if status == mb.SolveStatus.INFEASIBLE:
            if best_choice is None:
                return done("infeasible", None, None, None)
            continue                                      # it disagrees with a plan we checked ourselves: ignore it
        if status not in (mb.SolveStatus.OPTIMAL, mb.SolveStatus.FEASIBLE):
            continue
        values = np.array([solver.value(x) for x in variables])
        picked = list(choice)
        top: dict = {}
        for (i, j), value in zip(owner, values):
            if i not in top or value > top[i][0]:
                top[i] = (value, j)
        for i, (_, j) in top.items():
            picked[i] = j
        value = measure(picked)
        if value is not None and (best_value is None or (value < best_value if smaller_is_better else value > best_value)):
            best_value, best_choice = value, picked
        limit = float(solver.best_objective_bound)
        if math.isfinite(limit) and abs(limit) < 1e15:
            limit = limit + fixed_obj if kind == "linear" else limit * (w_scale if kind == "peak_water" else r_scale)
            bound = limit if bound is None else (max(bound, limit) if smaller_is_better else min(bound, limit))
        if status == mb.SolveStatus.OPTIMAL and value is not None:
            proven = True
            break
        if best_choice is not None:                       # the next solver starts from the best plan so far
            model.clear_hints()
            for x, (i, j) in zip(variables, owner):
                model.add_hint(x, 1.0 if best_choice[i] == j else 0.0)
    if best_choice is None:
        return done("unknown", None, None, None)
    return done("optimal" if proven else "feasible", best_choice, best_value, bound)


def solve_farm(options: Sequence[Options], *, water_cap: float | None = None, income_floor: float | None = None,
               time_limit_s: float = TIME_LIMIT_S, stage_seconds: Mapping | None = None) -> Solution:
    """Rotation and start year for every field at once (the module docstring has the model and the stages)."""
    started = time.perf_counter()
    caps = {**STAGE_SECONDS, **(stage_seconds or {})}
    area = sum(o.acres for o in options)
    coef = [o.acres / area * np.asarray(o.key, dtype=float) for o in options]
    alone = [int(o.own) for o in options]
    alone_score = farm_score(options, alone)
    top_score = float(sum(c.max() for c in coef))                 # no plan of any kind scores above this
    w_scale, r_scale = _scales(options)
    tol_w, tol_r = TOLERANCE * w_scale, TOLERANCE * r_scale
    columns = _Columns(options)
    stages: list = []

    def seconds_for(stage: str) -> float:
        left = time_limit_s - (time.perf_counter() - started)
        return max(MIN_STAGE_SECONDS, min(caps[stage if stage in caps else "without"], left))

    def run(stage: str, kind: str, cols, **kwargs) -> Solved:
        solved = optimise(options, cols, kind, seconds=seconds_for(stage), **kwargs)
        stages.append({"stage": stage, "status": solved.status, "seconds": round(solved.seconds, 3),
                       "choices": solved.binaries})
        return solved

    def score_stage(stage: str, cap, floor, hint=None) -> Solved:
        cols = columns.keep(True, cap is not None, floor is not None, extra=hint)
        return run(stage, "linear", cols, coef=coef, water_cap=cap, income_floor=floor, hint=hint)

    water = {"set": water_cap is not None, "met": True, "achievable": None, "binds": False, "why": "not_set",
             "score_without": None}
    income = {"set": income_floor is not None, "met": True, "achievable": None, "binds": False, "why": "not_set",
              "score_without": None, "alone_ok": None}
    alone_ok = meets(options, alone, water_cap, income_floor)
    cap, floor = water_cap, income_floor                          # the limits the plan is held to
    decided: list = []                                            # the stages the plan rests on
    return_alone = None                                           # best leanest-year return without the water budget

    if all(alone_ok) and abs(alone_score - top_score) <= 1e-12:
        # every field's own first pick already meets the limits: nothing scores higher, nothing to solve
        final = Solved("optimal", list(alone), alone_score, alone_score, 0.0, 0)
        stages.append({"stage": "each_alone", "status": "optimal", "seconds": 0.0, "choices": 0})
    else:
        final = score_stage("score", cap, floor)
        decided.append(final)
        if not final.found:
            decided.clear()                                       # the closest plan rests on the stages below
            start = None
            if water_cap is not None:
                least = run("peak_water", "peak_water", columns.keep(False, True, False))
                decided.append(least)
                start = least.choice if least.found else list(alone)
                peak = float(farm_totals(options, start)[0].max())
                if peak > water_cap + tol_w:
                    water.update(met=False, achievable={"value": peak, "proven": least.status == "optimal",
                                                        "bound": least.bound})
                    cap = peak
            if income_floor is not None:
                best = run("worst_return", "worst_return", columns.keep(False, cap is not None, True, extra=start),
                           water_cap=cap, hint=start)
                decided.append(best)
                if best.found:
                    start = best.choice
                elif start is None:
                    start = list(alone)
                worst = float(farm_totals(options, start)[1].min())
                if worst < income_floor - tol_r:
                    income.update(met=False, achievable={"value": worst, "proven": best.status == "optimal",
                                                         "bound": best.bound})
                    floor = worst
                    if water_cap is not None:                     # does the minimum fail on its own, or with the budget?
                        free = run("worst_return_alone", "worst_return", columns.keep(False, False, True))
                        if free.found:
                            return_alone = float(free.objective)
                        if return_alone is not None and return_alone >= income_floor - tol_r:
                            income["alone_ok"] = True
                        elif free.status == "optimal" or (free.bound is not None and free.bound < income_floor - tol_r):
                            income["alone_ok"] = False
            closest = score_stage("score_closest", cap, floor, hint=start)
            decided.append(closest)
            final = closest if closest.found else Solved("feasible", list(start), farm_score(options, start), None,
                                                         0.0, 0)

    choice = list(final.choice)
    score = farm_score(options, choice)
    bound = min(top_score, final.bound) if final.bound is not None else top_score
    gap = max(0.0, bound - score)
    proven = all(stage.status == "optimal" for stage in decided) and final.status == "optimal"
    limits_met = water["met"] and income["met"]

    # start years again: keep as many acres as possible in their natural start year (the score cannot change)
    if choice != alone:
        same_rotation = [np.flatnonzero(o.cycle == o.cycle[j]) for o, j in zip(options, choice)]
        stay = [o.acres / area * np.asarray(o.natural, dtype=float) for o in options]
        tidy = run("start_years", "linear", same_rotation, coef=stay, water_cap=cap, income_floor=floor, hint=choice)
        if tidy.found:
            choice = list(tidy.choice)

    # which limits bind
    met_now = meets(options, choice, water_cap, income_floor)
    for name, limit, entry, other_cap, other_floor in (("water", water_cap, water, None, floor),
                                                       ("income", income_floor, income, cap, None)):
        if limit is None:
            continue
        index = 0 if name == "water" else 1
        entry["met"] = entry["met"] and met_now[index]
        if not entry["met"]:
            entry.update(binds=True, why="cannot_be_met")
        elif not alone_ok[index]:
            entry.update(binds=True, why="alone_breaks_it")
        elif name == "water" and not income["met"]:
            # the budget is kept and the minimum return fails: the budget binds when the farm could do better without it
            if return_alone is None:
                entry.update(binds=None, why="unknown")
            elif return_alone > income["achievable"]["value"] + tol_r:
                entry.update(binds=True, why="limits_the_return")
            else:
                entry.update(binds=False, why="no_cost")
        elif score >= top_score - 1e-12:
            entry.update(binds=False, why="no_cost")
        else:
            # each-alone meets this limit but the plan scores below the top: would the score rise without it?
            without = score_stage("without_water_budget" if name == "water" else "without_min_return",
                                  other_cap, other_floor, hint=choice)
            if without.found and without.objective > score + gap + 1e-9:
                entry.update(binds=True, why="costs_score", score_without=float(without.objective))
            elif without.status == "optimal" and gap <= 1e-9:
                entry.update(binds=False, why="no_cost", score_without=float(without.objective))
            else:
                entry.update(binds=None, why="unknown")

    stopped_early = not proven
    return Solution(choice=choice, alone=alone, limits_met=limits_met, proven=proven, stopped_early=stopped_early,
                    score=score, alone_score=alone_score, bound=bound, gap=gap, water=water, income=income,
                    stages=stages, seconds=time.perf_counter() - started)


# --- the planner's numbers as plans over 12 years ------------------------------------------------------

@dataclass(frozen=True)
class PlanTable:
    """Every (candidate cycle, start year) of one practice in one climate window, year by year."""

    irrigated: bool
    cycles: tuple              # the candidate cycles
    cycle: np.ndarray          # [n] index into `cycles`
    start: np.ndarray          # [n] 0-based year of the cycle the plan starts in (0 = as the card shows it)
    crops: tuple               # [n] the 12 crops
    water: np.ndarray          # [n, 12, model] inches an acre
    ret: np.ndarray            # [n, 12, model] dollars an acre
    water_mean: np.ndarray     # [n, 12]
    ret_mean: np.ndarray       # [n, 12]
    natural: np.ndarray        # [n] bool: start == 0
    index: dict                # (cycle index, start) -> plan index


def plan_table(engine: Engine, irrigated: bool, scenario: str, horizon: int) -> PlanTable:
    """All plans of a practice. A start year that gives the same 12 years as an earlier one (corn-soybeans
    alternating started in year 3) is the same plan and is listed once, under the earlier start."""
    year = {(crop, prev): engine.crop_year(irrigated, scenario, horizon, crop, prev)
            for crop in CROPS for prev in CROPS}
    cycles = cand.candidates(irrigated)
    which, start, crops, index, seen = [], [], [], {}, {}
    for c, cycle in enumerate(cycles):
        length = len(cycle)
        for s in range(length):
            sequence = tuple(cycle[(s + t) % length] for t in range(SPAN_YEARS))
            if sequence not in seen:
                seen[sequence] = len(crops)
                which.append(c)
                start.append(s)
                crops.append(sequence)
            index[(c, s)] = seen[sequence]
    water = np.array([[year[(seq[t], seq[t - 1])]["irrigation_mm"] / MM_PER_INCH for t in range(SPAN_YEARS)]
                      for seq in crops], dtype=float)
    ret = np.array([[year[(seq[t], seq[t - 1])]["profit"] for t in range(SPAN_YEARS)] for seq in crops], dtype=float)
    start = np.array(start)
    return PlanTable(bool(irrigated), cycles, np.array(which), start, tuple(crops), water, ret,
                     water.mean(axis=2), ret.mean(axis=2), start == 0, index)


@dataclass
class _Group:
    """Fields that share an evaluation: the same practice, soil erodibility and slope."""

    table: PlanTable
    evaluation: smart.Evaluation
    keys: np.ndarray           # [cycle] ranking score, rounded as the single-field planner ranks
    plan_keys: np.ndarray      # [plan]
    order: list                # cycle indexes, best first
    fields: int = 0


@dataclass
class _Field:
    field: FieldInput
    acres: float
    irrigated: bool
    source: str
    group: _Group
    own: int                   # plan index of its own first pick in its natural start year
    options: Options = dataclass_field(repr=False, default=None)


# --- words and rounding --------------------------------------------------------------------------------

def _dollars(value: float) -> int:
    return int(round(value / DOLLAR_STEP) * DOLLAR_STEP)


def _money(value: float) -> str:
    amount = _dollars(value)
    return f"-${abs(amount):,}" if amount < 0 else f"${amount:,}"


def _inches(value: float) -> str:
    return f"{round(value):,} acre-inches"


def _band(values: np.ndarray, money: bool) -> dict:
    """{mid, low, high} of a farm total across the climate models: mid is their mean."""
    rounded = _dollars if money else (lambda v: int(round(v)))
    return {"mid": rounded(float(values.mean())), "low": rounded(float(values.min())),
            "high": rounded(float(values.max()))}


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _signed(diff: float, unit: str, more: str, less: str, digits: int = 0) -> str | None:
    amount = round(abs(diff), digits)
    if amount == 0:
        return None
    shown = f"{amount:,.{digits}f}"
    return f"{unit.format(shown)} {more if diff > 0 else less}"


def _field_sentence(entry: dict, irrigated: bool) -> str:
    plan, own = entry["plan"], entry["own_pick"]
    first = plan["years"][0]
    if entry["same_rotation"] and entry["same_start"]:
        return "Its own first pick, in the order its plan card shows."
    if entry["same_rotation"]:
        return (f"Its own first pick, started in year {plan['start_year']} of the rotation ({first} first) so that "
                "the farm's years even out.")
    change = entry["gives_up"]
    lost = change["score"]
    lead = (f"Not its own first pick ({own['name']}): "
            + (f"this rotation scores {abs(lost):.3f} lower for this field" if lost < 0
               else "this rotation scores the same for this field at the precision shown"))
    parts = [
        _signed(change["profit_per_acre"], "returns about ${} an acre a year", "more over direct costs",
                "less over direct costs"),
        _signed(change["irrigation_in"], "pumps about {} inches a year", "more", "less", 1) if irrigated else None,
        _signed(change["bare_days"], "leaves the soil bare about {} days a year", "more", "less"),
    ]
    parts = [part for part in parts if part]
    return lead + ("; it " + ", ".join(parts) + "." if parts else ".")


def _limit_words(name: str, limit: float) -> str:
    if name == "water":
        return f"the water budget of {_inches(limit)} a year"
    return f"the minimum return of {_money_exact(limit)} a year over direct costs"


def _money_exact(value: float) -> str:
    amount = round(value)
    return f"-${abs(amount):,}" if amount < 0 else f"${amount:,}"


def _worst(values: np.ndarray, limit: float | None, water: bool) -> dict:
    """The year closest to (or furthest past) a limit: the heaviest pumping year, or the leanest year."""
    n = int(values.argmax() if water else values.argmin())
    value = float(values[n])
    out = {"n": n + 1, "value": int(round(value)) if water else _dollars(value)}
    if limit is not None:
        margin = (limit - value) if water else (value - limit)        # room left; negative = short by that much
        out["margin"] = int(round(margin)) if water else _dollars(margin)
    return out


# --- the entry point -----------------------------------------------------------------------------------

def _number(value, name: str, sentence: str, minimum: float | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FarmError(sentence)
    if minimum is not None and value < minimum:
        raise FarmError(sentence)
    return float(value)


def plan_farm(fields: Sequence[FieldInput], *, weights: Weights | Mapping | None = None,
              scenario: str = config.DEFAULT_SCENARIO, horizon: int = config.TODAY,
              water_budget_acre_in: float | None = None, min_income: float | None = None,
              irrigated: Mapping | None = None, traits: FieldTraits | None = None, engine: Engine | None = None,
              time_limit_s: float = TIME_LIMIT_S) -> dict:
    """The body of POST /farm/plan.

    fields                the farm's fields (1 to 60, each id once); `area_ha` is the field's size
    weights, scenario, horizon   as `rank_plans_smart`, the same for every field
    water_budget_acre_in  the most the whole farm may pump in any year, acre-inches (None = no budget)
    min_income            the least the whole farm must return over direct costs in every year, dollars
                          (None = no minimum). The name is the request's; the figure is a return over
                          direct costs, not income or profit.
    irrigated             {field_id: True / False} overrides our guess for those fields
    traits                one `FieldTraits` for every field (the fake fields pass an empty one); None looks
                          each field up in the engine's table
    FarmError (a ValueError, one plain sentence) for a farm that cannot be planned; ValueError for bad
    settings, as `rank_plans_smart`.
    """
    fields = list(fields)
    if not fields:
        raise FarmError("A farm needs at least one field.")
    if len(fields) > MAX_FIELDS:
        raise FarmError(f"A farm here is at most {MAX_FIELDS} fields; this one has {len(fields)}.")
    ids = [f.field_id for f in fields]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise FarmError(f"Field {twice[0]} is listed more than once.")
    overrides = dict(irrigated or {})
    stray = sorted(str(i) for i in overrides if i not in ids)
    if stray:
        raise FarmError(f"The irrigation setting names field {stray[0]}, which is not one of the farm's fields.")
    if any(not isinstance(value, bool) for value in overrides.values()):
        raise FarmError("The irrigation setting of a field must be true or false.")
    budget = _number(water_budget_acre_in, "water_budget_acre_in",
                     "The water budget must be a number of acre-inches a year, zero or more.", 0.0)
    minimum = _number(min_income, "min_income", "The minimum yearly return must be a number of dollars.")
    engine = engine if engine is not None else smart.get_engine()
    weights = Weights.of(weights).normalised()
    futures.check_scenario(scenario)
    horizon = futures.check_horizon(horizon)

    # every field: its practice and its own first pick, from the single-field planner itself
    tables: dict = {}
    groups: dict = {}
    farm: list = []
    settings = None
    for f in fields:
        if f.area_ha is None or not math.isfinite(f.area_ha) or f.area_ha <= 0:
            raise FarmError(f"Field {f.field_id} has no area on record, so it cannot be part of a farm plan.")
        result = smart.rank_plans_smart(f, weights=weights, scenario=scenario, horizon=horizon,
                                        irrigated=overrides.get(f.field_id), top=1, traits=traits, engine=engine)
        settings = settings or result.settings
        is_irrigated = bool(result.settings["irrigated"])
        own_traits = traits if traits is not None else engine.traits_for(f.field_id)
        wei = own_traits.wei if own_traits is not None else None
        weg = own_traits.weg if own_traits is not None else None
        if is_irrigated not in tables:
            tables[is_irrigated] = plan_table(engine, is_irrigated, scenario, horizon)
        table = tables[is_irrigated]
        shared = (is_irrigated, wei, weg, f.slope_pct)
        if shared not in groups:
            ev = engine.evaluate(is_irrigated, scenario, horizon, wei=wei, weg=weg, slope_pct=f.slope_pct)
            keys = np.round(ev.robust(weights), 6)
            groups[shared] = _Group(table, ev, keys, keys[table.cycle],
                                    sorted(range(len(keys)), key=lambda i: (-keys[i], i)))
        group = groups[shared]
        group.fields += 1
        first = group.order[0]
        if tuple(result.plans[0].cycle) != tuple(table.cycles[first]):      # the two must be one ranking
            raise RuntimeError(f"farm planner and field planner disagree on the first pick of {f.field_id}")
        own = table.index[(first, 0)]
        acres = f.area_ha * ACRES_PER_HA
        farm.append(_Field(f, acres, is_irrigated, result.settings["irrigation_source"], group, own,
                           Options(acres, group.plan_keys, table.cycle, table.natural, table.water_mean,
                                   table.ret_mean, own)))

    solution = solve_farm([f.options for f in farm], water_cap=budget, income_floor=minimum,
                          time_limit_s=time_limit_s)
    return _report(farm, solution, weights, settings, budget, minimum, engine, len(groups), time_limit_s)


_CACHE: "OrderedDict" = OrderedDict()
_CACHE_LOCK = threading.Lock()
CACHE_SIZE = 32


def plan_farm_cached(fields: Sequence[FieldInput], *, engine: Engine | None = None, **settings) -> dict:
    """`plan_farm`, remembering the last answers: the same farm asked again with the same settings gets
    the same answer at once (also when the first solve stopped at its time limit, where a second solve
    could land on another plan). The memory is dropped when the planner's tables are rebuilt."""
    engine = engine if engine is not None else smart.get_engine()

    def frozen(value):
        if isinstance(value, Mapping):
            return tuple(sorted((str(k), frozen(v)) for k, v in value.items()))
        if isinstance(value, Weights):
            return tuple(value.as_dict(9).items())
        if isinstance(value, FieldTraits):
            return ("traits", value.irrigation, value.wei, value.weg)
        return value

    key = (tuple((f.field_id, f.area_ha, f.slope_pct, frozen(f.crop_history or {})) for f in fields),
           frozen(settings))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] is engine:
            _CACHE.move_to_end(key)
            return hit[1]
    result = plan_farm(fields, engine=engine, **settings)
    with _CACHE_LOCK:
        _CACHE[key] = (engine, result)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    return result


def _plan_object(f: _Field, plan: int, weights: Weights) -> dict:
    """A plan as GET /field/{id}/plans gives it, plus the start year and the 12 years."""
    table, group = f.group.table, f.group
    c = int(table.cycle[plan])
    rank = group.order.index(c) + 1
    out = smart._plan_result(group.evaluation, c, weights, rank, f.irrigated).to_dict()
    out["start_year"] = int(table.start[plan]) + 1
    out["years"] = list(table.crops[plan])
    return out


def _totals(farm: Sequence[_Field], choice: Sequence[int], budget, minimum, tol_w: float, tol_r: float) -> dict:
    """Farm totals of a choice, year by year: model mean, lowest and highest model, limits met or not."""
    water = sum(f.acres * f.group.table.water[j] for f, j in zip(farm, choice))        # [year, model]
    ret = sum(f.acres * f.group.table.ret[j] for f, j in zip(farm, choice))
    water_mean, ret_mean = water.mean(axis=1), ret.mean(axis=1)
    years = []
    for t in range(SPAN_YEARS):
        acres: dict = {}
        for f, j in zip(farm, choice):
            crop = f.group.table.crops[j][t]
            acres[crop] = acres.get(crop, 0.0) + f.acres
        years.append({
            "n": t + 1,
            "water_acre_in": _band(water[t], money=False),
            "return": _band(ret[t], money=True),
            "water_ok": None if budget is None else bool(water_mean[t] <= budget + tol_w),
            "return_ok": None if minimum is None else bool(ret_mean[t] >= minimum - tol_r),
            "acres": {crop: int(round(acres[crop])) for crop in CROPS if crop in acres},
        })
    area = sum(f.acres for f in farm)
    score = sum(f.acres * float(f.group.plan_keys[j]) for f, j in zip(farm, choice)) / area
    return {
        "score": round(float(score), 3),
        "years": years,
        "average": {"water_acre_in": int(round(float(water_mean.mean()))), "return": _dollars(float(ret_mean.mean()))},
        "peak_water": _worst(water_mean, budget, water=True),
        "worst_return": _worst(ret_mean, minimum, water=False),
        "water_ok": None if budget is None else bool(water_mean.max() <= budget + tol_w),
        "return_ok": None if minimum is None else bool(ret_mean.min() >= minimum - tol_r),
        "water_years_over": [] if budget is None else [t + 1 for t in range(SPAN_YEARS) if water_mean[t] > budget + tol_w],
        "return_years_under": ([] if minimum is None
                               else [t + 1 for t in range(SPAN_YEARS) if ret_mean[t] < minimum - tol_r]),
    }


def _achievable(entry: dict, water: bool) -> dict | None:
    """The best the farm can do on a limit it cannot meet, rounded to the safe side: asking for exactly
    this value can be met."""
    found = entry["achievable"]
    if found is None:
        return None
    value, bound = found["value"], found["bound"]
    if water:
        out = {"value": int(math.ceil(value - 1e-9)), "proven": bool(found["proven"]),
               "bound": None if bound is None else int(math.floor(max(bound, 0.0) + 1e-9))}
    else:
        out = {"value": int(math.floor(value / DOLLAR_STEP + 1e-9) * DOLLAR_STEP), "proven": bool(found["proven"]),
               "bound": None if bound is None else int(math.ceil(bound / DOLLAR_STEP - 1e-9) * DOLLAR_STEP)}
    if out["proven"]:
        out["bound"] = out["value"]
    return out


BINDS_WORDS = {
    "not_set": "Not set.",
    "cannot_be_met": "It cannot be met.",
    "alone_breaks_it": "It binds: each field planned alone would break it, so rotations or start years had to change.",
    "costs_score": "It binds: without it the farm could score higher.",
    "limits_the_return": "It binds: without it the farm could return more in its leanest year.",
    "no_cost": "It does not bind: the plan would be no better without it.",
    "unknown": "The solver could not tell in the time allowed whether the plan would be better without it.",
}


def _limit_entry(name: str, limit: float | None, solved: dict, plan: dict, alone: dict,
                 water_met: bool = True) -> dict:
    water = name == "water"
    worst_key, ok_key, years_key = (("peak_water", "water_ok", "water_years_over") if water
                                    else ("worst_return", "return_ok", "return_years_under"))
    entry = {
        "set": limit is not None,
        "limit": limit,
        "unit": "acre-inches a year" if water else "dollars a year, return over direct costs",
        "met": None if limit is None else bool(solved["met"]),
        "binds": solved["binds"],
        "why": solved["why"],
        "binds_sentence": BINDS_WORDS[solved["why"]],
        "tightest": plan[worst_key],
        "alone": {"met": alone[ok_key], "tightest": alone[worst_key], "years_broken": alone[years_key]},
        "achievable": _achievable(solved, water),
        "score_without": None if solved["score_without"] is None else round(solved["score_without"], 3),
        "sentence": None,
    }
    if not water:
        entry["alone_ok"] = solved.get("alone_ok")
    if limit is None:
        return entry
    words = _limit_words(name, limit)
    words = words[0].upper() + words[1:]
    best = entry["achievable"]
    if best is None:
        tight = entry["tightest"]
        if tight["margin"] == 0:                        # at the precision shown
            room = "right at the budget" if water else "right at the minimum"
        else:
            room = (f"{tight['margin']:,} acre-inches under the budget" if water
                    else f"{_money(tight['margin'])} above the minimum")
        year = (f"the heaviest pumping year (year {tight['n']}) is {tight['value']:,} acre-inches, {room}" if water
                else f"the leanest year (year {tight['n']}) returns {_money(tight['value'])}, {room}")
        entry["sentence"] = f"{words} is met in every year: {year}. {entry['binds_sentence']}"
        return entry
    if water:
        text = (f"{words} cannot be met: the least this farm can pump in its heaviest year is about "
                f"{best['value']:,} acre-inches")
        if not best["proven"]:
            text += (" (the least found in the time allowed"
                     + (f"; it cannot be under {best['bound']:,}" if best["bound"] is not None else "") + ")")
    else:
        within = ""
        if not water_met:
            within = " while the farm pumps as little as it can"
        elif solved.get("alone_ok") is True:
            within = " together with the water budget"
        text = (f"{words} cannot be met{within}: the best this farm can return in its leanest year is about "
                f"{_money(best['value'])}")
        if not best["proven"]:
            text += (" (the best found in the time allowed"
                     + (f"; it cannot be over {_money(best['bound'])}" if best["bound"] is not None else "") + ")")
        if solved.get("alone_ok") is True:
            text += ". With no limit on water the minimum could be met"
        elif solved.get("alone_ok") is False:
            text += ". It could not be met with no limit on water either"
    entry["sentence"] = text + "."
    return entry


def _report(farm: Sequence[_Field], solution: Solution, weights: Weights, settings: dict, budget, minimum,
            engine: Engine, n_groups: int, time_limit_s: float) -> dict:
    import ortools

    options = [f.options for f in farm]
    w_scale, r_scale = _scales(options)
    tol_w, tol_r = TOLERANCE * w_scale, TOLERANCE * r_scale
    plan = _totals(farm, solution.choice, budget, minimum, tol_w, tol_r)
    alone = _totals(farm, solution.alone, budget, minimum, tol_w, tol_r)

    entries = []
    for f, j, own in zip(farm, solution.choice, solution.alone):
        table = f.group.table
        chosen, first = _plan_object(f, j, weights), _plan_object(f, own, weights)
        same_rotation = int(table.cycle[j]) == int(table.cycle[own])
        entry = {
            "field_id": f.field.field_id,
            "area_acres": round(f.acres, 1),
            "area_ha": round(float(f.field.area_ha), 2),
            "irrigated": f.irrigated,
            "irrigation_source": f.source,
            "plan": chosen,
            "own_pick": first,
            "same_rotation": same_rotation,
            "same_start": same_rotation and j == own,
            "differs": j != own,
            "gives_up": None,
        }
        if not same_rotation:
            a, b = chosen["numbers"], first["numbers"]
            entry["gives_up"] = {
                "score": round(chosen["scores"]["robust"] - first["scores"]["robust"], 3),
                "profit_per_acre": round(a["profit_per_acre"] - b["profit_per_acre"]),
                "bare_days": round(a["bare_days"] - b["bare_days"], 1),
                "irrigation_in": round(a["irrigation_in"] - b["irrigation_in"], 1) if f.irrigated else None,
            }
        entry["sentence"] = _field_sentence(entry, f.irrigated)
        entries.append(entry)

    wet = [f for f in farm if f.irrigated]
    dry = [f for f in farm if not f.irrigated]
    area = sum(f.acres for f in farm)
    summary_farm = {
        "fields": len(farm), "area_acres": round(area, 1),
        "area_ha": round(sum(float(f.field.area_ha) for f in farm), 2),
        "irrigated_fields": len(wet), "irrigated_acres": round(sum(f.acres for f in wet), 1),
        "dryland_fields": len(dry), "dryland_acres": round(sum(f.acres for f in dry), 1),
    }
    limits = {
        "water": _limit_entry("water", budget, solution.water, plan, alone),
        "return": _limit_entry("income", minimum, solution.income, plan, alone, water_met=solution.water["met"]),
    }
    any_limit = budget is not None or minimum is not None
    status = "no_limits" if not any_limit else "limits_met" if solution.limits_met else "limits_not_met"
    changed = sum(1 for e in entries if not e["same_rotation"])
    shifted = sum(1 for e in entries if e["same_rotation"] and not e["same_start"])
    plan.update(changed_fields=changed, shifted_fields=shifted)
    cost = round(alone["score"] - plan["score"], 3)

    # --- the solver, said plainly
    gap = round(solution.gap, 6)
    seconds = round(solution.seconds, 2)
    solved_by = any(stage["choices"] > 0 for stage in solution.stages)
    if not solved_by and solution.stages and solution.stages[0]["stage"] == "each_alone":
        solver_status = "optimal"
        note = ("Every field's own first pick already meets the limits, and nothing scores higher than that, so there "
                "was nothing to trade." if any_limit else
                "No farm limit was set, so every field keeps its own first pick.")
    elif solution.proven:
        solver_status = "optimal"
        note = (f"Solved to proven optimality in {seconds:.2f} seconds: no other choice of rotations and start years "
                + ("meets the limits" if solution.limits_met else "gets this close to the limits")
                + " with a higher farm score.")
    else:
        solver_status = "time_limit"
        which = "No plan that meets the limits" if solution.limits_met else "No plan that gets this close to the limits"
        note = (f"The solver stopped at its time limit after {seconds:.2f} seconds, so this is the best plan it found, "
                "not a proven best. "
                + (f"{which} can score more than {gap:.4f} above it." if gap >= 0.00005
                   else f"{which} can score noticeably above it."))
    solver = {
        "name": f"Google OR-Tools {ortools.__version__} (mixed-integer program: SCIP, then CP-SAT)",
        "status": solver_status,
        "optimal": solver_status == "optimal",
        "stopped_early": bool(solution.stopped_early),
        "gap": gap,
        "gap_pct": round(100.0 * solution.gap / max(solution.bound, 1e-9), 3),
        "bound": round(solution.bound, 6),
        "score": round(solution.score, 6),
        "time_s": seconds,
        "time_limit_s": time_limit_s,
        "stages": solution.stages,
        "groups": n_groups,
        "note": note,
    }

    # --- the summary, one fact a sentence
    n = len(farm)
    summary = [
        f"{_count(n, 'field')}, {round(area):,} acres ("
        + (f"{_count(len(wet), 'irrigated field')} on {round(summary_farm['irrigated_acres']):,} acres, "
           f"{_count(len(dry), 'dryland field')} on {round(summary_farm['dryland_acres']):,} acres"
           if wet and dry else "all irrigated" if wet else "all dryland")
        + f"), planned together over {SPAN_YEARS} years."
    ]
    if not any_limit:
        summary.append("No farm limit was set, so every field keeps its own first pick.")
    elif solution.limits_met:
        both = budget is not None and minimum is not None
        summary.append(("Both limits are" if both else "The water budget is" if budget is not None
                        else "The minimum return is") + f" met in every one of the {SPAN_YEARS} years.")
    else:
        summary += [limits[key]["sentence"] for key in ("water", "return") if limits[key]["met"] is False]
        summary.append("The plan below is the closest the farm can get"
                       + (", keeping the water budget first." if budget is not None and minimum is not None
                          and limits["water"]["met"] else "."))
    if any_limit:
        if changed == 0 and shifted == 0:
            summary.append("Every field keeps its own first pick, in the order its plan card shows.")
        else:
            parts = []
            of_all = f" of the {n} fields" if n > 1 else " field"
            if changed:
                who = f"all {n} fields" if changed == n and n > 1 else f"{changed}{of_all}"
                parts.append(f"{who} {'takes' if changed == 1 else 'take'} a different rotation from "
                             f"{'its' if changed == 1 else 'their'} own first pick")
            if shifted:
                parts.append(f"{shifted}{' more' if changed else of_all} "
                             f"{'keeps its' if shifted == 1 else 'keep their'} first pick but "
                             f"{'starts' if shifted == 1 else 'start'} it in a different year")
            text = " and ".join(parts) + "."
            summary.append(text[0].upper() + text[1:])
        if cost > 0:
            summary.append(f"The farm's ranking score is {plan['score']:.3f}, against {alone['score']:.3f} with each "
                           "field planned alone.")
        elif changed:
            summary.append("The farm's ranking score is the same as with each field planned alone at the precision "
                           f"shown ({plan['score']:.3f}).")
        else:
            summary.append(f"The farm's ranking score stays at {plan['score']:.3f}: no field gives up its first pick.")
        broken = []
        if alone["water_ok"] is False:
            tight = alone["peak_water"]
            broken.append(f"pump up to {tight['value']:,} acre-inches in a year ({-tight['margin']:,} over the budget; "
                          f"over in {len(alone['water_years_over'])} of {SPAN_YEARS} years)")
        if alone["return_ok"] is False:
            tight = alone["worst_return"]
            broken.append(f"return as little as {_money(tight['value'])} in a year ({_money(-tight['margin'])} under "
                          f"the minimum; under in {len(alone['return_years_under'])} of {SPAN_YEARS} years)")
        summary.append("Each field planned alone would " + " and ".join(broken) + "." if broken
                       else "Each field planned alone meets the limits too.")
    if solver_status == "time_limit":
        summary.append(note)

    notes = [NATURAL_START_NOTE, YEARS_NOTE, MODELS_NOTE, settings["profit_basis"]]
    if dry:
        notes.append(DRYLAND_MONEY_NOTE)
    if wet and engine.irrigation_limit_mm > 0:
        notes.append(f"No field pumps more than {engine.irrigation_limit_mm / MM_PER_INCH:.0f} inches an acre in any "
                     "year: the single-field planner's per-acre limit is inside every plan.")
    notes.append(SHARED_NOTE)
    if status == "limits_not_met" and budget is not None and minimum is not None:
        notes.append(ORDER_NOTE)
    if any(stage["stage"] == "start_years" and stage["status"] != "optimal" for stage in solution.stages):
        notes.append(START_YEARS_NOTE)

    keep = ("weights", "scenario", "horizon", "window", "models", "ranking_rule", "profit_basis",
            "irrigation_limit_mm", "irrigation_limit_in", "soil_source")
    out_settings = {key: settings[key] for key in keep}
    out_settings.update(span_years=SPAN_YEARS, water_budget_acre_in=budget, min_income=minimum,
                        max_fields=MAX_FIELDS)
    return {
        "engine": "smart",
        "settings": out_settings,
        "farm": summary_farm,
        "status": status,
        "limits_met": bool(solution.limits_met),
        "summary": summary,
        "limits": limits,
        "plan": plan,
        "alone": alone,
        "score_cost": cost,
        "fields": entries,
        "solver": solver,
        "notes": notes,
        "not_modelled": list(NOT_MODELLED),
    }
