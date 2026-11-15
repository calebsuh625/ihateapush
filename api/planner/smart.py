"""The data-driven planner (Phase 3): rank rotation plans for one field across climate futures.

    from api.planner.smart import rank_plans_smart, climate_summary, available, Weights
    result = rank_plans_smart(field, weights=Weights(soil=2, water=1, money=2, risk=1),
                              scenario="ssp585", horizon=2050)
    result.to_dict()          # the body of GET /field/{id}/plans (design note section 4)

How a plan is scored
--------------------
A plan is a cycle of the six crops, four years long or three (candidates.py lists them and the
rules; a 3-year cycle is shown as its next four years). A FUTURE is one climate model under the
chosen scenario, averaged over the window around the chosen year (futures.py). The plan is cycled
through the window in all its phases, so no crop is tied to a lucky year; that is the same as
averaging, for each year of the cycle, that crop after that previous crop over every year of the
window. In every future the plan gets four scores, each 0-1, higher is better, each on a FIXED scale
(config.py), so a hotter future lowers them instead of being normalised away:

    soil   0.6 x (1 - bare days a year / 150)  +  0.4 x (1 - wind erosion index / 2.0)
           bare days: soil_effect.py (satellite averages by crop and previous crop);
           erosion index: erosion.py (bare share x the field's soil erodibility x the window's wind
           climate x a slope factor). The climate enters the soil score only through the erosion index.
    water  irrigated: 1 - mm pumped a year / 1000     dryland: share of the crops' water need that rain
           and stored soil water cover. Irrigated plans pump what the crops need up to the irrigation
           limit (24 inches = 610 mm per acre per year unless IRRIGATION_LIMIT_MM says otherwise); a
           crop that needs more goes short, yields less and can fail (response.py).
    money  return over direct costs, $ per acre per year (money.py), on a fixed scale per practice:
           irrigated -100 .. 500, dryland -125 .. 125
    risk   1 - expected share of planted crops that fail; a crop fails when its yield is under half of
           normal; the share uses the crop model's `fail_prob`, which allows for the model's own error
           (response.py), so it is fairer than counting hard failures. Fallow years are not counted.

The scales span what any candidate does in any model run to 2060 on a typical field (config.py), so
scores do not pile up at 0 or 1.

    overall = weighted mean of the four with the farmer's weights (normalised to sum to 1).

Holds up: a plan holds up in a future when no more than one in four of its planted crops fails AND it
covers its direct costs on average (`HOLDS_UP_RULE` is returned with every result).

Today (horizon 2025) is one climate whichever scenario is chosen: each model's two runs pooled over
2015-2034 (futures.py). Every future is measured against that same Today, also the modelled yield
change per crop that the result carries (`climate.yield_change`).

Ranking: not on the mean alone. rank value ("robust") = half the mean overall score over the futures
plus half the mean of the worst third of them (the 2 worst of 6). A plan that is fine on average but
bad in the hot, dry models loses to one that is a little worse on average and steady.

Top 3: the best plan, then the next best that differs from every plan already picked by more than
one year in four (comparing the share of years each crop gets). Reorderings and one-year tweaks of a
plan already shown are not real choices.

The field enters through: irrigated or dryland (which plans are allowed, which yields, costs and
water numbers apply), its soil erodibility and slope (erosion index), and its last four years, scored
the same way as "current". When those four years are a stretch of a candidate cycle (wheat, sorghum,
fallow, wheat is the 3-year wheat-sorghum-fallow), "current" is that cycle and gets exactly its
numbers; otherwise it is scored as the four years that happened (year 1 follows the crop that really
came before it). `rank` says where it would stand among the candidates. Everything else is
county-level: two dryland fields on the same soil and slope get the same plans.

Speed: all crop x practice x model x scenario x year numbers are computed once, on first use (about
40 ms, 0.25 s in a fresh process with the imports); one call afterwards takes about 0.4 ms (measured
in sanity.py). The default engine is rebuilt by itself when a source file or setting changes.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field as dataclass_field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from api.planner import candidates as cand
from api.planner import config, erosion, futures, money, soil_effect
from api.planner.crops import CROP_TABLE, CROPS, PRACTICES, PREV_KINDS
from api.planner.legacy import FieldInput, Plan, Scores, rotation_name
from api.planner.traits import FieldTraits, guess_irrigation, load_traits

HOLDS_UP_RULE = (
    "A plan holds up in a future (one climate model) when no more than 1 in 4 of its planted crops fails "
    "(yield under half of normal) and it still covers its direct costs on average."
)
assert config.HOLDS_MAX_FAILED_SHARE == 0.25 and config.HOLDS_MIN_PROFIT == 0.0, "update HOLDS_UP_RULE"

RANKING_RULE = (
    "Plans are ranked on half their average overall score across the climate models plus half their average "
    "in the worst third of the models."
)
YIELD_CHANGE_NOTE = (
    "Yield changes are what the crop model makes of the future weather alone, against today's model climate. "
    "They do not include new varieties or any other adaptation."
)
MM_PER_INCH = money.MM_PER_INCH


def limits_for(irrigation_limit_mm: float) -> list:
    """What the scoring leaves out, in plain sentences (the app shows them under "How this is scored")."""
    first, last = futures.window(config.TODAY)
    if irrigation_limit_mm > 0:
        well = (f"Irrigated plans assume the well can deliver up to {irrigation_limit_mm / MM_PER_INCH:.0f} inches of water "
                "an acre every year.")
    else:
        well = "Irrigated plans assume the well always delivers all the water the crops need."
    return [
        "Yields do not change with the crop grown before, apart from the soil water it leaves behind.",
        well,
        "Prices and costs stay at today's levels in every future.",
        "On dryland the water, money and risk scores move together, because all three follow how much of the crops' "
        "water need rain covers.",
        f"\"Today\" is a model climate for {first}-{last}, a little hotter than the 2000-2024 record.",
    ]


_RESPONSE_COLUMNS = ("yield", "fail_prob", "irrigation_mm", "shortfall_mm")
_KIND_INDEX = {kind: i for i, kind in enumerate(PREV_KINDS)}
_CROP_INDEX = {crop: i for i, crop in enumerate(CROPS)}


# --- small public types ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Weights:
    """The farmer's priorities. Any non-negative numbers; they are normalised to sum to 1."""

    soil: float = config.DEFAULT_WEIGHTS["soil"]
    water: float = config.DEFAULT_WEIGHTS["water"]
    money: float = config.DEFAULT_WEIGHTS["money"]
    risk: float = config.DEFAULT_WEIGHTS["risk"]

    @staticmethod
    def of(value: "Weights | Mapping | None") -> "Weights":
        if value is None:
            return Weights()
        if isinstance(value, Weights):
            return value
        unknown = set(value) - set(config.SCORE_NAMES)
        if unknown:
            raise ValueError(f"unknown weight(s) {sorted(unknown)}; expected {', '.join(config.SCORE_NAMES)}")
        return Weights(**{name: value.get(name, 0.0) for name in config.SCORE_NAMES})

    def normalised(self) -> "Weights":
        values = []
        for name in config.SCORE_NAMES:
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"weight {name}={v!r}: expected a number >= 0")
            values.append(float(v))
        total = sum(values)
        if total <= 0:
            raise ValueError("all weights are zero: at least one of soil, water, money, risk must be above 0")
        return Weights(*(v / total for v in values))

    def as_dict(self, digits: int = 4) -> dict:
        return {name: round(getattr(self, name), digits) for name in config.SCORE_NAMES}


@dataclass(frozen=True)
class PlanResult:
    name: str
    rotation: list     # the plan's next four years (always four crops)
    cycle: list        # the repeating unit: the same four crops, or three for a 3-year cycle
    scores: dict       # soil, water, money, risk, overall (their weighted mean), robust (what plans are ranked on)
    reason: str
    futures: dict      # {"holds": 5, "total": 6}
    spread: dict       # {"overall": {low, mid, high}, "profit_per_acre": {low, mid, high}} across the futures
    numbers: dict      # bare_days, erosion_index, irrigation_mm, irrigation_in, water_met_share, profit_per_acre, failed_share
    rank: int          # place among all candidates on the robust score (1 = best)
    rules_broken: list = dataclass_field(default_factory=list)   # "current" only: rule sentences it breaks
    extra: dict = dataclass_field(default_factory=dict)   # "current" only: years, previous_crop, is_candidate

    def to_dict(self) -> dict:
        out = {"name": self.name, "rotation": list(self.rotation), "cycle": list(self.cycle),
               "scores": dict(self.scores),
               "reason": self.reason, "futures": dict(self.futures), "spread": self.spread,
               "numbers": dict(self.numbers), "rank": self.rank}
        if self.extra:            # the field's current pattern only
            out["rules_broken"] = list(self.rules_broken)
            out.update(self.extra)
        return out

    def as_plan(self) -> Plan:
        """The old planner's `Plan` (name, rotation, soil / water / money / overall, reason)."""
        s = self.scores
        return Plan(name=self.name, rotation=list(self.rotation),
                    scores=Scores(soil=s["soil"], water=s["water"], money=s["money"], overall=s["overall"]),
                    reason=self.reason)


@dataclass(frozen=True)
class SmartResult:
    field_id: str
    settings: dict
    climate: dict
    plans: list
    current: PlanResult | None
    current_note: str | None
    candidates: int
    engine: str = "smart"

    def to_dict(self) -> dict:
        return {
            "field_id": self.field_id,
            "engine": self.engine,
            "settings": self.settings,
            "climate": self.climate,
            "plans": [plan.to_dict() for plan in self.plans],
            "current": self.current.to_dict() if self.current is not None else None,
            "current_note": self.current_note,
            "candidates": self.candidates,
        }

    def as_plans(self) -> list:
        return [plan.as_plan() for plan in self.plans]


# --- the engine: everything that does not depend on the field ------------------------------------------

@dataclass(frozen=True)
class PlanSet:
    cycles: tuple             # n cycles (3 or 4 crops each)
    rotations: tuple          # the same plans as their next four years
    crop: np.ndarray          # [n, 4] crop index of each year of the cycle (a 3-year cycle is padded)
    kind: np.ndarray          # [n, 4] index of the previous crop's kind (summer / wheat / fallow)
    weight: np.ndarray        # [n, 4] 1 / cycle length for a real year, 0 for padding
    planted: np.ndarray       # [n, 4] 1.0 where a crop is planted (a real year that is not fallow)
    shares: np.ndarray        # [n, 6] share of the cycle's years in each crop
    bare: np.ndarray          # [n] mean bare days a year


@dataclass(frozen=True)
class Evaluation:
    """Scores and numbers of a set of plans in every future: arrays [plan, future] unless noted."""

    plans: PlanSet
    models: tuple
    bare: np.ndarray           # [plan]
    erosion: np.ndarray
    irrigation: np.ndarray
    water_met: np.ndarray      # share of water need met (irrigated: below 1 only when the irrigation limit binds)
    profit: np.ndarray
    failed: np.ndarray
    soil: np.ndarray
    water: np.ndarray
    money: np.ndarray
    risk: np.ndarray
    holds: np.ndarray          # bool

    def overall(self, weights: Weights) -> np.ndarray:
        return (weights.soil * self.soil + weights.water * self.water
                + weights.money * self.money + weights.risk * self.risk)

    def robust(self, weights: Weights) -> np.ndarray:
        return robust_value(self.overall(weights))


def robust_value(overall: np.ndarray) -> np.ndarray:
    """[plan] from [plan, future]: half the mean, half the mean of the worst third of the futures."""
    n_futures = overall.shape[1]
    worst = max(1, math.ceil(n_futures * config.WORST_FRACTION - 1e-9))
    worst_mean = np.sort(overall, axis=1)[:, :worst].mean(axis=1)
    return config.ROBUST_MEAN_SHARE * overall.mean(axis=1) + (1 - config.ROBUST_MEAN_SHARE) * worst_mean


def _clip01(x):
    return np.clip(x, 0.0, 1.0)


class Engine:
    """The planner's tables in memory. Build one from the climate tables; ask it to rank plans."""

    def __init__(self, yearly, monthly, *, params: dict | None = None, economics: money.Economics | None = None,
                 soil: soil_effect.SoilEffectProvider | None = None, traits: Mapping | None = None,
                 observed: dict | None = None, source: str = "", irrigation_limit_mm: float | None = None):
        """`irrigation_limit_mm`: the most mm pumped per acre per year; None = the economics file's value
        (610 mm), 0 = no limit."""
        from api.planner.response import crop_response, load_params

        self.climate = futures.Climate(yearly)
        self.params = params if params is not None else load_params()
        self.economics = economics if economics is not None else money.load_economics()
        limit = self.economics.irrigation_limit_mm if irrigation_limit_mm is None else irrigation_limit_mm
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit < 0:
            raise ValueError(f"irrigation limit {limit!r}: expected mm per acre per year, 0 for no limit")
        self.irrigation_limit_mm = float(limit)
        self.soil = soil if soil is not None else soil_effect.CropAverageProvider()
        self.traits = dict(traits or {})
        self.observed = observed
        self.source = source
        self._lock = threading.Lock()
        self._plan_sets: dict = {}

        c = self.climate
        rows = yearly[yearly["scenario"].isin(c.scenarios)]
        table = crop_response(rows, monthly, self.params, irrigation_limit_mm=self.irrigation_limit_mm)
        si = table["scenario"].map({s: i for i, s in enumerate(c.scenarios)}).to_numpy()
        mi = table["model"].map({m: i for i, m in enumerate(c.models)}).to_numpy()
        yi = np.searchsorted(c.years, table["year"].to_numpy(dtype=int))
        ci = table["crop"].map(_CROP_INDEX).to_numpy()
        pi = table["practice"].map({p: i for i, p in enumerate(PRACTICES)}).to_numpy()
        shape = (len(PRACTICES), len(c.scenarios), len(c.models), len(c.years), len(CROPS))
        self._response = {}
        for name in _RESPONSE_COLUMNS:
            arr = np.zeros(shape + (len(PREV_KINDS),))
            for k, kind in enumerate(PREV_KINDS):
                arr[pi, si, mi, yi, ci, k] = table[f"{name}_after_{kind}"].to_numpy(dtype=float)
            self._response[name] = arr
        need = np.zeros(shape)
        need[pi, si, mi, yi, ci] = table["water_need_mm"].to_numpy(dtype=float)
        self._response["need_mm"] = need
        typical = np.zeros(shape)       # yield after the county's usual previous crop: for the yield-change note
        typical[pi, si, mi, yi, ci] = table["yield"].to_numpy(dtype=float)
        self._response["yield_typical"] = typical
        # 1.0 in a year the irrigation limit leaves a crop short (irrigated only; the mean is how often it binds)
        self._response["short"] = (self._response["shortfall_mm"] > 1e-6).astype(float)
        price = np.array([self.economics.price(crop) for crop in CROPS])
        normals = money.normal_yields(self.params)
        cost = np.array([[self.economics.cost(crop, practice, normals[crop][practice]) for crop in CROPS]
                         for practice in PRACTICES])
        # profit [practice, scenario, model, year, crop, kind]
        self._response["profit"] = (price[None, None, None, None, :, None] * self._response["yield"]
                                    - cost[:, None, None, None, :, None]
                                    - self.economics.pump_per_mm * self._response["irrigation_mm"])
        self._window = lru_cache(maxsize=256)(self._window_uncached)
        self._base = lru_cache(maxsize=512)(self._base_uncached)

    # --- plans ---
    def plan_set(self, rotations: Sequence, irrigated: bool, first_prev: str | None = None,
                 max_years: int = cand.YEARS) -> PlanSet:
        """Index arrays for a list of cycles (3 or 4 crops each). Year 1 follows the cycle's last year
        unless `first_prev` names the crop that really grew before year 1 (a field's own last years).

        `max_years`: the longest cycle taken (the candidates and a field's own years stop at four; a
        farmer's own rotation, custom.py, may run to six). The arrays are as wide as the longest cycle,
        never under four."""
        cycles = tuple(tuple(r) for r in rotations)
        n, width = len(cycles), max([cand.YEARS, *(len(cycle) for cycle in cycles)])
        prev_kind = {c: _KIND_INDEX[CROP_TABLE[c].as_prev] for c in CROPS}
        crop = np.zeros((n, width), dtype=int)
        kind = np.zeros((n, width), dtype=int)
        weight = np.zeros((n, width))
        planted = np.zeros((n, width))
        shares = np.zeros((n, len(CROPS)))
        for i, cycle in enumerate(cycles):
            length = len(cycle)
            if not 1 <= length <= max_years:
                raise ValueError(f"a plan is a cycle of at most {max_years} years, got {length}")
            for j, name in enumerate(cycle):
                before = first_prev if (j == 0 and first_prev is not None) else cycle[j - 1]
                crop[i, j], kind[i, j], weight[i, j] = _CROP_INDEX[name], prev_kind[before], 1.0 / length
                planted[i, j] = 0.0 if name == "fallow" else 1.0
                shares[i, _CROP_INDEX[name]] += 1.0 / length
        bare = np.array([soil_effect.plan_bare_days(self.soil, cycle, irrigated, first_prev) for cycle in cycles])
        return PlanSet(cycles, tuple(cand.show(cycle) for cycle in cycles), crop, kind, weight, planted, shares, bare)

    def candidate_set(self, irrigated: bool) -> PlanSet:
        irrigated = bool(irrigated)
        with self._lock:
            if irrigated not in self._plan_sets:
                self._plan_sets[irrigated] = self.plan_set(cand.candidates(irrigated), irrigated)
            return self._plan_sets[irrigated]

    # --- window means ---
    def _window_uncached(self, scenario: str, horizon: int) -> dict:
        """{name: [practice, model, crop, (kind)]} window means, plus "erosion_climate": [model]."""
        s = self.climate._scenario_index(scenario)
        years = self.climate.year_slice(horizon)
        if horizon == config.TODAY:     # one Today: every scenario's run of a model pooled (futures.py)
            out = {name: arr[:, :, :, years].mean(axis=(1, 3)) for name, arr in self._response.items()}
        else:
            out = {name: arr[:, s, :, years].mean(axis=2) for name, arr in self._response.items()}
        out["erosion_climate"] = self.climate.averages(scenario, horizon)["erosion_climate"]
        return out

    def yield_change(self, irrigated: bool, scenario: str, horizon: int) -> dict:
        """{crop: {"pct", "low", "high"}}: modelled yield in this future against Today, in percent, for the
        crops this practice's plans grow: the mean over the models and the lowest and highest model. Each
        model is compared with its own Today; the yield is the one after the county's usual previous crop."""
        p = PRACTICES.index("irrigated" if irrigated else "dryland")
        now = self._window(scenario, config.TODAY)["yield_typical"][p]          # [model, crop]
        then = self._window(scenario, horizon)["yield_typical"][p]
        grown = self.candidate_set(irrigated).shares.sum(axis=0) > 0
        out = {}
        for ci, crop in enumerate(CROPS):
            if crop == "fallow" or not grown[ci]:
                continue
            pct = (then[:, ci] / np.maximum(now[:, ci], 1e-9) - 1.0) * 100.0
            out[crop] = {"pct": int(round(float(pct.mean()))), "low": int(round(float(pct.min()))),
                         "high": int(round(float(pct.max())))}
        return out

    def limit_binds(self, scenario: str, horizon: int) -> dict:
        """{crop: share of irrigated crop-years in which the irrigation limit leaves the crop short}
        (after the county's usual kind of previous crop for irrigated fields, a summer crop)."""
        w = self._window(scenario, horizon)["short"][PRACTICES.index("irrigated")]     # [model, crop, kind]
        return {crop: float(w[:, ci, _KIND_INDEX["summer"]].mean()) for ci, crop in enumerate(CROPS) if crop != "fallow"}

    def _base_uncached(self, irrigated: bool, scenario: str, horizon: int, plans_key) -> dict:
        """Everything about a plan set in a (scenario, horizon) that does not depend on the field."""
        plans = self.candidate_set(irrigated) if plans_key is None else self.plan_set(plans_key, irrigated)
        return self._base_for(plans, irrigated, scenario, horizon)

    def _base_for(self, plans: PlanSet, irrigated: bool, scenario: str, horizon: int) -> dict:
        w = self._window(scenario, horizon)
        p = PRACTICES.index("irrigated" if irrigated else "dryland")

        def over_years(name):            # [model, plan, year-of-plan]
            return w[name][p][:, plans.crop, plans.kind]

        real = (plans.weight > 0).astype(float)
        profit = (over_years("profit") * plans.weight[None]).sum(axis=2).T
        irrigation = (over_years("irrigation_mm") * plans.weight[None]).sum(axis=2).T
        shortfall = (over_years("shortfall_mm") * real[None]).sum(axis=2).T
        need = (w["need_mm"][p][:, plans.crop] * real[None]).sum(axis=2).T
        n_planted = np.maximum(plans.planted.sum(axis=1), 1.0)
        failed = (over_years("fail_prob") * plans.planted[None]).sum(axis=2).T / n_planted[:, None]
        # share of the crops' water need that is met; under irrigation it is below 1 only when the limit binds
        water_met = np.where(need > 0, 1.0 - shortfall / np.maximum(need, 1e-9), 1.0)
        water = _clip01(1.0 - irrigation / config.IRRIGATION_WORST_MM) if irrigated else _clip01(water_met)
        lo, hi = config.MONEY_SCALE["irrigated" if irrigated else "dryland"]
        return {
            "plans": plans, "profit": profit, "irrigation": irrigation, "water_met": water_met, "failed": failed,
            "water": water, "money": _clip01((profit - lo) / (hi - lo)),
            "risk": _clip01(1.0 - failed / config.RISK_WORST_SHARE),
            "holds": (failed <= config.HOLDS_MAX_FAILED_SHARE + 1e-12) & (profit >= config.HOLDS_MIN_PROFIT),
            "erosion_climate": w["erosion_climate"],
        }

    def evaluate(self, irrigated: bool, scenario: str = config.DEFAULT_SCENARIO, horizon: int = config.TODAY, *,
                 rotations: Sequence | None = None, first_prev: str | None = None,
                 wei=None, weg=None, slope_pct=None, max_years: int = cand.YEARS) -> Evaluation:
        """Score the candidates (or the given rotations) in every future of (scenario, horizon).

        `first_prev` (with `rotations`): the crop that really grew before year 1, instead of year 4.
        `max_years` (with `rotations`): the longest cycle taken, four unless the caller says otherwise.
        """
        futures.check_scenario(scenario)
        horizon = futures.check_horizon(horizon)
        irrigated = bool(irrigated)
        if rotations is None:
            base = self._base(irrigated, scenario, horizon, None)
        else:
            base = self._base_for(self.plan_set(rotations, irrigated, first_prev, max_years), irrigated, scenario,
                                  horizon)
        plans = base["plans"]
        index = erosion.erosion_index(plans.bare[:, None], base["erosion_climate"][None, :], wei, weg, slope_pct)
        soil = (config.SOIL_BARE_SHARE * _clip01(1.0 - plans.bare / config.BARE_DAYS_WORST)[:, None]
                + config.SOIL_EROSION_SHARE * _clip01(1.0 - index / config.EROSION_WORST))
        return Evaluation(plans=plans, models=self.climate.models, bare=plans.bare, erosion=index,
                          irrigation=base["irrigation"], water_met=base["water_met"], profit=base["profit"],
                          failed=base["failed"], soil=soil, water=base["water"], money=base["money"],
                          risk=base["risk"], holds=base["holds"])

    # --- the field ---
    def traits_for(self, field_id: str) -> FieldTraits | None:
        return self.traits.get(field_id)

    def climate_summary(self) -> dict:
        return self.climate.summary(self.source, self.observed)


# --- words ---------------------------------------------------------------------------------------------

def _money_words(profit: float) -> str:
    """Return over direct costs, never "earns": land and machinery ownership are not taken off."""
    amount = round(abs(profit))
    if profit >= 0.5:
        return f"returns about ${amount:,.0f} an acre a year over direct costs"
    if profit <= -0.5:
        return f"falls about ${amount:,.0f} an acre short of its direct costs"
    return "about covers its direct costs"


def _failure_words(failed: float) -> str:
    """How often crops fail, never rounded down across the holds-up line (more than 1 in 4)."""
    for n in (2, 3, 4):
        if failed > 1.0 / n + 1e-9:
            return f"more than 1 crop in {n} fails"
    return f"about 1 crop in {max(4, round(1 / failed)):.0f} fails"


def reason_for(numbers: dict, scores: dict, holds: int, total: int, irrigated: bool,
               crop_failures: int = 0) -> str:
    """One plain sentence from the plan's own numbers, its strongest side first, under
    `config.MAX_REASON_CHARS` (220) characters.

    `crop_failures`: in how many futures too many crops fail (then the sentence says how often they do;
    when a plan fails to hold up only because it loses money, the money clause already says so).
    Water is given in inches, the unit the county's irrigators use (the mm stay in `numbers`).
    """
    clauses = {
        "soil": f"leaves the soil bare about {numbers['bare_days']:.0f} days a year",
        "money": _money_words(numbers["profit_per_acre"]),
    }
    if irrigated:
        inches = numbers["irrigation_mm"] / MM_PER_INCH
        clauses["water"] = (f"pumps about {round(inches):.0f} inches of water a year" if inches >= 0.5
                            else "needs almost no pumped water")
    else:
        clauses["water"] = f"rain covers about {100 * numbers['water_met_share']:.0f}% of its crops' water need"
    names = ("soil", "water", "money")
    parts = [clauses[name] for name in sorted(names, key=lambda name: (-scores[name], names.index(name)))]
    last = f"holds up in {holds} of {total} climate futures"
    failed = numbers["failed_share"]
    extra = f" ({_failure_words(failed)})" if crop_failures > 0 and failed > 0.02 else ""

    def sentence(clauses_used, tail):
        text = ", ".join(clauses_used) + " and " + tail + "."
        return text[0].upper() + text[1:]

    for candidate in (sentence(parts, last + extra), sentence(parts, last), sentence(parts[:2], last)):
        if len(candidate) < config.MAX_REASON_CHARS:
            return candidate
    return sentence(parts[:1], last)


def _spread(values: np.ndarray, digits: int) -> dict:
    return {"low": round(float(values.min()), digits), "mid": round(float(np.median(values)), digits),
            "high": round(float(values.max()), digits)}


def _plan_result(ev: Evaluation, i: int, weights: Weights, rank: int, irrigated: bool, rotation=None,
                 rules_broken: list | None = None, extra: dict | None = None) -> PlanResult:
    overall = ev.overall(weights)[i]
    scores = {
        "soil": round(float(ev.soil[i].mean()), 3), "water": round(float(ev.water[i].mean()), 3),
        "money": round(float(ev.money[i].mean()), 3), "risk": round(float(ev.risk[i].mean()), 3),
        "overall": round(float(overall.mean()), 3),
        "robust": round(float(robust_value(overall[None, :])[0]), 3),
    }
    numbers = {
        "bare_days": round(float(ev.bare[i]), 1),
        "erosion_index": round(float(ev.erosion[i].mean()), 3),
        "irrigation_mm": round(float(ev.irrigation[i].mean()), 0),
        "irrigation_in": round(float(ev.irrigation[i].mean()) / MM_PER_INCH, 1),
        "water_met_share": round(float(ev.water_met[i].mean()), 3),
        "profit_per_acre": round(float(ev.profit[i].mean()), 0),
        "failed_share": round(float(ev.failed[i].mean()), 3),
    }
    holds, total = int(ev.holds[i].sum()), int(ev.holds.shape[1])
    crop_failures = int((ev.failed[i] > config.HOLDS_MAX_FAILED_SHARE + 1e-12).sum())
    cycle = list(ev.plans.cycles[i])
    rotation = list(rotation if rotation is not None else ev.plans.rotations[i])
    name = rotation_name(rotation) if len(cycle) == cand.YEARS else f"{rotation_name(cycle)} (3-year cycle)"
    return PlanResult(
        name=name, rotation=rotation, cycle=cycle, scores=scores,
        reason=reason_for(numbers, scores, holds, total, irrigated, crop_failures),
        futures={"holds": holds, "total": total},
        spread={"overall": _spread(overall, 3), "profit_per_acre": _spread(ev.profit[i], 0)},
        numbers=numbers, rank=rank, rules_broken=list(rules_broken or []), extra=dict(extra or {}),
    )


def pick_different(order: Sequence[int], shares: np.ndarray, top: int) -> list:
    """Indices of the first `top` plans in `order` that are different choices from each other.

    Different = more than `SAME_CHOICE_YEARS` (one) year in four grows another crop (`shares` is each
    plan's share of years per crop). If the list runs out, the bar drops to "any difference in the
    crops", then to none, so `top` plans come back whenever there are that many.
    """
    picked: list = []
    for more_than in (config.SAME_CHOICE_YEARS, 0.0, -1.0):
        for i in order:
            if len(picked) >= top:
                break
            if i in picked:
                continue
            if all(cand.YEARS * float(np.abs(shares[i] - shares[j]).sum()) / 2 > more_than + 1e-9 for j in picked):
                picked.append(i)
        if len(picked) >= top:
            break
    position = {i: n for n, i in enumerate(order)}
    return sorted(picked, key=lambda i: position[i])


def current_pattern(crop_history) -> tuple:
    """(rotation or None, years, note, previous crop or None): the field's last four recorded calendar
    years, oldest first, and the crop of the year before them when it is one of the six."""
    years = {}
    for year, crop in (crop_history or {}).items():
        try:
            years[int(year)] = str(crop).strip().lower()
        except (TypeError, ValueError):
            continue
    if not years:
        return None, [], "No crop history on record for this field.", None
    last = max(years)
    span = list(range(last - cand.YEARS + 1, last + 1))
    missing = [y for y in span if y not in years]
    if missing:
        return None, span, f"The last four years ({span[0]}-{span[-1]}) are not all on record.", None
    rotation = tuple(years[y] for y in span)
    other = sorted({c for c in rotation if c not in CROPS})
    if other:
        return None, span, (f"The last four years ({span[0]}-{span[-1]}) include {', '.join(other)}, "
                            "which the planner has no numbers for."), None
    before = years.get(span[0] - 1)
    return rotation, span, None, before if before in CROPS else None


# --- the entry point -----------------------------------------------------------------------------------

def rank_plans_smart(field: FieldInput, *, weights: Weights | Mapping | None = None,
                     scenario: str = config.DEFAULT_SCENARIO, horizon: int = config.TODAY,
                     irrigated: bool | None = None, top: int = config.TOP_PLANS,
                     traits: FieldTraits | None = None, engine: Engine | None = None) -> SmartResult:
    """Rank the candidate plans for one field. Pure and deterministic for the same inputs and tables.

    weights    the farmer's priorities (any scale; None = the defaults). ValueError when negative or all zero.
    scenario   "ssp245" or "ssp585"; horizon: a year 2025-2060 (2025 = today). ValueError otherwise.
    irrigated  True / False overrides our guess; None uses the field's traits, else its crop history.
    traits     the field's row of field_traits.parquet; None looks the field up by id (and guesses if absent).
    engine     the tables to use; None = the default engine built from the files config.py points to.
    """
    engine = engine if engine is not None else get_engine()
    weights = Weights.of(weights).normalised()
    futures.check_scenario(scenario)
    horizon = futures.check_horizon(horizon)
    if isinstance(top, bool) or not isinstance(top, int) or top < 1:
        raise ValueError(f"top={top!r}: expected a whole number of plans, 1 or more")

    traits = traits if traits is not None else engine.traits_for(field.field_id)
    if irrigated is not None:
        is_irrigated, source, basis = bool(irrigated), "override", "set by the request"
    elif traits is not None and traits.irrigation in PRACTICES:
        is_irrigated, source = traits.irrigation == "irrigated", "guess"
        basis = traits.irrigation_basis or "field traits table"
    else:
        is_irrigated, basis = guess_irrigation(field.crop_history)
        source = "guess"
    wei = traits.wei if traits is not None else None
    weg = traits.weg if traits is not None else None

    ev = engine.evaluate(is_irrigated, scenario, horizon, wei=wei, weg=weg, slope_pct=field.slope_pct)
    robust = ev.robust(weights)
    keys = np.round(robust, 6)
    order = sorted(range(len(keys)), key=lambda i: (-keys[i], i))
    rank_of = {i: n + 1 for n, i in enumerate(order)}
    picked = pick_different(order, ev.plans.shares, top)
    plans = [_plan_result(ev, i, weights, rank_of[i], is_irrigated) for i in picked]

    current, current_note = None, None
    rotation, years, note, before = current_pattern(field.crop_history)
    if rotation is None:
        current_note = note
    else:
        match = cand.matching_candidate(rotation, is_irrigated, before)
        if match is not None:      # a stretch of a candidate cycle: it is that plan, with that plan's numbers
            one = engine.evaluate(is_irrigated, scenario, horizon, rotations=[match], wei=wei, weg=weg,
                                  slope_pct=field.slope_pct)
            broken = []
        else:                      # the four years as they happened: year 1 follows what really came before
            one = engine.evaluate(is_irrigated, scenario, horizon, rotations=[rotation], first_prev=before,
                                  wei=wei, weg=weg, slope_pct=field.slope_pct)
            broken = cand.rules_broken(rotation, is_irrigated, cyclic=False, gentle=True)
        value = round(float(one.robust(weights)[0]), 6)
        rank = 1 + int((keys > value).sum())
        current = _plan_result(one, 0, weights, rank, is_irrigated, rotation=rotation, rules_broken=broken,
                               extra={"years": [years[0], years[-1]], "previous_crop": before,
                                      "is_candidate": match is not None})

    describe = getattr(engine.soil, "describe", None)
    soil_info = describe() if describe is not None else {"name": engine.soil.name, "note": ""}
    settings = {
        "weights": weights.as_dict(),
        "scenario": scenario,
        "horizon": horizon,
        "window": list(futures.window(horizon)),
        "irrigated": is_irrigated,
        "irrigation_source": source,
        "irrigation_basis": basis,
        "soil_source": engine.soil.name,
        "soil_note": soil_info.get("note", ""),
        "holds_up_rule": HOLDS_UP_RULE,
        "ranking_rule": RANKING_RULE,
        "profit_basis": money.profit_basis(engine.economics),
        "irrigation_limit_mm": round(engine.irrigation_limit_mm, 0) if engine.irrigation_limit_mm > 0 else None,
        "irrigation_limit_in": (round(engine.irrigation_limit_mm / MM_PER_INCH, 1)
                                if engine.irrigation_limit_mm > 0 else None),
        "yield_change_note": YIELD_CHANGE_NOTE,
        "limits": limits_for(engine.irrigation_limit_mm),
        "models": list(engine.climate.models),
    }
    climate = engine.climate.describe(scenario, horizon)
    climate["yield_change"] = engine.yield_change(is_irrigated, scenario, horizon)
    return SmartResult(field_id=field.field_id, settings=settings, climate=climate,
                       plans=plans, current=current, current_note=current_note, candidates=len(ev.plans.cycles))


# --- the default engine, loaded lazily and once --------------------------------------------------------

_ENGINE_LOCK = threading.Lock()
_ENGINE: dict = {"key": None, "engine": None, "error": None}


def _stamp(settings: config.Settings) -> tuple:
    from api.planner.response import PARAMS_PATH

    paths = [settings.climate_futures_path, settings.climate_monthly_path, settings.climate_history_path,
             settings.field_traits_path, settings.soil_health_path, settings.fields_path, settings.effects_path,
             settings.soil_cache_path, settings.economics_path, PARAMS_PATH]
    out = [settings.soil_effect_source, settings.irrigation_limit_mm]
    for path in paths:
        try:
            st = Path(path).stat()
            out.append((str(path), st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((str(path), None, None))
    return tuple(out)


def load_engine(settings: config.Settings | None = None) -> Engine:
    """Build an engine from the files the settings point to. Raises when the climate tables are unusable."""
    from pipeline.climate import schema as climate_schema

    settings = settings or config.settings()
    yearly = climate_schema.load_futures(settings.climate_futures_path)
    monthly = climate_schema.load_monthly(settings.climate_monthly_path)
    from api.planner.response import load_params, with_observed_heat_cap

    observed, history = None, None
    try:
        history = climate_schema.load_history(settings.climate_history_path)
        observed = futures.observed_values(history)
    except Exception:   # the observed table is context for GET /climate and the source of the hot-day cap
        observed = None
    # the hot-day cap from the record itself when the table is there; else what the calibration wrote
    params = with_observed_heat_cap(load_params(), history)
    return Engine(yearly, monthly, params=params, economics=money.load_economics(settings.economics_path),
                  soil=soil_effect.load_provider(settings), traits=load_traits(settings.field_traits_path),
                  observed=observed, source=climate_schema.SOURCE, irrigation_limit_mm=settings.irrigation_limit_mm)


def get_engine() -> Engine:
    """The default engine: built on first use, rebuilt when a source file or setting changes."""
    settings = config.settings()
    key = _stamp(settings)
    with _ENGINE_LOCK:
        if _ENGINE["key"] != key:
            _ENGINE.update(key=key, engine=None, error=None)
            try:
                _ENGINE["engine"] = load_engine(settings)
            except Exception as exc:
                _ENGINE["error"] = exc
        if _ENGINE["engine"] is None:
            raise RuntimeError(f"the smart planner is not available: {_ENGINE['error']}") from _ENGINE["error"]
        return _ENGINE["engine"]


def reset() -> None:
    """Forget the default engine (tests; or after rebuilding a table within the same process)."""
    with _ENGINE_LOCK:
        _ENGINE.update(key=None, engine=None, error=None)


def available() -> bool:
    """True when the climate tables exist and the engine can be built from them. False otherwise
    (then the API keeps using the old planner)."""
    settings = config.settings()
    if not (Path(settings.climate_futures_path).is_file() and Path(settings.climate_monthly_path).is_file()):
        return False
    try:
        get_engine()
    except Exception:
        return False
    return True


def climate_summary() -> dict:
    """The body of GET /climate: {"available": true, ...} or {"available": false, "reason": "not_built"}."""
    if not available():
        return {"available": False, "reason": "not_built"}
    return get_engine().climate_summary()
