"""Weather replay (Phase 5, item B): how a plan would have done in the real weather of 2000-2024.

    from api.planner.replay import replay
    body = replay(field, plan=1, weights=..., irrigated=None, engine=engine)      # GET /field/{id}/replay
    body = replay(field, rotation=["corn", "soybeans"], engine=engine)

The planner scores a plan in six climate-model futures. This module scores the same plan in the weather that
really happened, year by year, with the same machinery: the observed rows of the climate tables
(data/climate_history.parquet and the "observed" rows of data/climate_monthly.parquet, NASA POWER) go through
the planner's own crop response, soil-water balance, irrigation limit, rotation effects, prices and costs
(`Engine`, built on the observed years instead of the model years). Nothing is fitted again and nothing is
added: a year's yield, whether the crop failed (yield under half of normal), the water pumped or the share of
the crop's water need met, and the return over direct costs are the engine's numbers for that crop after the
crop before it in that year's weather.

Phases
------
A rotation is a cycle, and the real weather fell on every starting year. The cycle is run in ALL its phases
(a 4-year cycle in 4 phases: each phase is the cycle shifted one year). One phase matches the field: the one
where the cycle's first year, as the plan card lists it, is the year after the field's last year on record,
so the cycle is lined up backwards in time from there ("the same order, ending where the field is now").
That phase makes the bars when the field has a crop history; without a history the bars show the average of
all the phases and say so. Either way the spread across phases is returned as well (`low`, `high`, and the
`all_starts` summary over every phase and year).

Drought years are marked by our own rule, never by name: a year is dry when its April-September rain
(`rain_mm` of the observed table) is among the driest fifth of the observed years (the 5 lowest of 25).

"Carry on as now" is the field's current pattern (transition.carry_on_pattern) run the same way, lined up on
its real years, and the plan is compared with it year by year in the same weather.

What it is not: a forecast; a price history (prices and costs are today's, as everywhere in the planner);
a field's own weather (the observed table is one NASA POWER grid cell for the county); hail, disease, wind
or anything else the crop model does not have.
"""
from __future__ import annotations

import math
import threading
import weakref
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from api.planner import config, custom, smart
from api.planner.crops import CROP_TABLE, CROPS, PRACTICES
from api.planner.legacy import FieldInput, rotation_name
from api.planner.smart import MM_PER_INCH, Engine
from api.planner.traits import FieldTraits
from api.planner import transition

DRY_SHARE = 0.2            # a dry year is one of the driest fifth of the observed seasons
WORST_MARKED = 3           # the worst years marked on the chart
OBSERVED = "observed"
REPLAY_SCENARIO = "ssp245"  # the label the observed rows carry inside the replay engine (the scenario is not used)
_CROP_INDEX = {crop: i for i, crop in enumerate(CROPS)}

NEEDS_CLIMATE = ("Replaying a plan in the real weather needs the climate tables, which are not set up on this server.")
NEEDS_HISTORY = ("Replaying a plan in the real weather needs the observed weather record (2000-2024), which is not "
                 "set up on this server.")

HISTORY_NOT_FORECAST = ("This is what the weather of the years on record would have done to the plan. It is not a "
                        "forecast: the next years will bring other weather.")
DRYLAND_MONEY_NOTE = ("On dryland the sign of these returns depends on the price years and the cost of fallow, so the "
                      "yield figures are firmer than the dollars.")
NOT_MODELLED = (
    "Prices and costs are today's in every year. A short crop in a drought year sold at that year's price, and "
    "costs were different then.",
    "Only heat and water move a yield here. Hail, wind, frost, insects, disease and anything else are not in it.",
    "The weather is one record for the whole county (NASA POWER), not this field's own rain gauge, so a storm that "
    "missed or hit this field is not in it.",
    "Crop insurance, the farm's own equipment and the learning curve of a crop that is new to it are not in it.",
)


class ReplayUnavailable(RuntimeError):
    """The observed tables are missing or unusable. The message is one plain sentence."""


class ReplayError(ValueError):
    """The request cannot be answered as asked. The message is one plain sentence."""


# --- the observed years as an engine ---------------------------------------------------------------------

@dataclass(frozen=True)
class Observed:
    engine: Engine           # the planner's engine built on the observed years (one "model": observed)
    years: np.ndarray        # the observed years, oldest first
    weather: tuple           # one dict per year: year, season_rain_mm, rain_vs_normal_pct, hot_days, dry
    dry_years: tuple
    normal_rain_mm: float


_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_LOCK = threading.Lock()


def dry_year_set(years: Sequence[int], rain: Sequence[float], share: float = DRY_SHARE) -> list:
    """The driest `share` of the years by season rain (the lowest 5 of 25), oldest first; at least one year,
    ties at the edge going to the earlier year. The rule is ours; no year is named in advance."""
    n = len(years)
    k = max(1, int(math.floor(n * share + 1e-9)))
    order = sorted(range(n), key=lambda i: (float(rain[i]), int(years[i])))
    return sorted(int(years[i]) for i in order[:k])


def _load_history():
    from pipeline.climate import schema as climate_schema

    path = config.settings().climate_history_path
    try:
        return climate_schema.load_history(path)
    except FileNotFoundError as exc:
        raise ReplayUnavailable(NEEDS_HISTORY) from exc
    except Exception as exc:
        raise ReplayUnavailable(NEEDS_HISTORY) from exc


def build_observed(engine: Engine, history=None) -> Observed:
    """The engine for the observed years, from the history table (loaded from the settings' path when not given)
    and the observed rows of the monthly table the engine was built from. ReplayUnavailable when they are missing."""
    import pandas as pd

    history = _load_history() if history is None else history
    monthly = engine._tables[1]
    rows = monthly[(monthly["model"] == OBSERVED) & (monthly["scenario"] == OBSERVED)]
    if not len(rows) or not len(history):
        raise ReplayUnavailable(NEEDS_HISTORY)
    yearly = history.assign(model=OBSERVED, scenario=REPLAY_SCENARIO)
    rows = rows.assign(scenario=REPLAY_SCENARIO)
    try:
        built = Engine(yearly, rows, params=engine.params, economics=engine.economics, soil=engine.soil,
                       traits={}, irrigation_limit_mm=engine.irrigation_limit_mm, rotation_effects=engine.rotation)
    except Exception as exc:
        raise ReplayUnavailable(NEEDS_HISTORY) from exc
    years = np.array(sorted(int(y) for y in history["year"]))
    by_year = history.set_index("year")
    rain = np.array([float(by_year.loc[y, "rain_mm"]) for y in years])
    normal = float(rain.mean())
    dry = dry_year_set(years, rain)
    weather = tuple({
        "year": int(y), "season_rain_mm": int(round(r)), "rain_vs_normal_pct": int(round((r / normal - 1) * 100)),
        "hot_days": int(round(float(by_year.loc[y, "hot_days"]))), "dry": int(y) in dry,
    } for y, r in zip(years, rain))
    return Observed(engine=built, years=years, weather=weather, dry_years=tuple(dry), normal_rain_mm=normal)


def observed_for(engine: Engine, history=None) -> Observed:
    """`build_observed`, kept per engine (an engine is rebuilt when a table changes, and its replay with it)."""
    if history is not None:
        return build_observed(engine, history)
    with _LOCK:
        hit = _CACHE.get(engine)
    if hit is not None:
        return hit
    built = build_observed(engine)
    with _LOCK:
        return _CACHE.setdefault(engine, built)


# --- one cycle in every phase ----------------------------------------------------------------------------

def _grids(obs: Observed, unit: Sequence[str], irrigated: bool, variant: str = "mid") -> dict:
    """{name: array [phase, year]} for the cycle `unit` run in every phase. Phase p is the cycle shifted so that
    the first observed year holds unit[p]; the previous crop of a year is the cycle's crop before it."""
    engine = obs.engine
    resp = engine._response_for(variant)
    p = PRACTICES.index("irrigated" if irrigated else "dryland")
    n, t = len(unit), len(obs.years)
    crop_idx = np.array([_CROP_INDEX[c] for c in unit])
    pos = (np.arange(t)[None, :] + np.arange(n)[:, None]) % n               # [phase, year]
    c, prev = crop_idx[pos], crop_idx[(pos - 1) % n]
    cols = np.arange(t)[None, :]

    def pick(name):
        return np.asarray(resp[name][p, 0, 0][cols, c, prev], dtype=float)

    out = {name: pick(name) for name in ("profit", "yield", "yield_rel", "fail_prob", "irrigation_mm", "shortfall_mm")}
    out["need_mm"] = np.asarray(resp["need_mm"][p, 0, 0][cols, c], dtype=float)
    out["crop"] = c
    out["planted"] = c != _CROP_INDEX["fallow"]
    out["fail_below"] = float(engine.params.get("fail_below", 0.5))
    out["failed"] = out["planted"] & (out["yield_rel"] < out["fail_below"])
    return out


def shown_phase(n: int, first_year: int, anchor: int | None) -> int | None:
    """The phase in which the cycle's first crop falls in year `anchor` (None: no anchor). With the phases of
    `_grids` (phase p holds unit[p] in `first_year`), year Y holds unit[(Y - first_year + p) % n]."""
    return None if anchor is None else (-(anchor - first_year)) % n


def _round_dollars(x) -> np.ndarray:
    return np.floor(np.asarray(x, dtype=float) + 0.5 + 1e-9).astype(int)


def _dollars(value: float) -> str:
    return custom._dollars(value)


def _median(values: np.ndarray) -> int:
    return int(_round_dollars(float(np.median(values))))


def _reduce(arr: np.ndarray, mask: np.ndarray, phase: int | None):
    """One value a year from [phase, year] values: the shown phase's, or the mean over the phases where `mask`
    holds (None where it holds in none)."""
    if phase is not None:
        return np.where(mask[phase], arr[phase], np.nan)
    count = mask.sum(axis=0)
    total = np.where(mask, arr, 0.0).sum(axis=0)
    return np.where(count > 0, total / np.maximum(count, 1), np.nan)


def _summary(returns: np.ndarray, years: np.ndarray, dry: np.ndarray, planted: np.ndarray, failed: np.ndarray,
             crops: Sequence | None = None, yield_pct: np.ndarray | None = None) -> dict:
    """The headline numbers of a set of crop-years. `returns` [k] in whole dollars, `years` [k] their calendar years
    (repeats allowed when several phases are pooled), `dry` [k] bool, `planted`, `failed` [k] bool, `yield_pct` [k]
    the yield as a percent of normal (read where the crop is planted)."""
    k = len(returns)
    worst = int(np.argmin(returns))
    best = int(np.argmax(returns))
    below = int((returns < 0).sum())
    dry_n, other_n = int(dry.sum()), int((~dry).sum())
    out = {
        "years": int(len(np.unique(years))), "crop_years": k,
        "worst_year": {"year": int(years[worst]), "return": int(returns[worst]),
                       **({"crop": crops[worst]} if crops is not None else {})},
        "best_year": {"year": int(years[best]), "return": int(returns[best]),
                      **({"crop": crops[best]} if crops is not None else {})},
        "median_return": _median(returns),
        "mean_return": int(_round_dollars(returns.mean())),
        "below_cost_years": below, "below_cost_share": round(below / k, 3),
        "planted_years": int(planted.sum()), "failed_years": int(failed.sum()),
        "failed_share": round(float(failed.sum()) / max(1, int(planted.sum())), 3),
        "dry": {
            "years": dry_n,
            "mean_return": int(_round_dollars(returns[dry].mean())) if dry_n else None,
            "other_mean_return": int(_round_dollars(returns[~dry].mean())) if other_n else None,
            "below_cost_years": int((returns[dry] < 0).sum()),
            "failed_years": int(failed[dry].sum()),
        },
    }
    if yield_pct is not None:
        for key, pick in (("mean_yield_pct", dry & planted), ("other_mean_yield_pct", ~dry & planted)):
            out["dry"][key] = int(round(float(yield_pct[pick].mean()))) if pick.any() else None
    return out


def _series(obs: Observed, unit: Sequence[str], irrigated: bool, anchor: int | None, name: str,
            engine: Engine) -> dict:
    """One rotation replayed: the year rows, the headline numbers for the bars shown, the same over every phase,
    and the range the rotation effects' own range puts around the headline."""
    n, years = len(unit), obs.years
    first = int(years[0])
    phase = shown_phase(n, first, anchor)
    g = _grids(obs, unit, irrigated)
    ret = _round_dollars(g["profit"])                                    # [phase, year]
    dry = np.array([w["dry"] for w in obs.weather])
    planted = g["planted"]

    # the bars: the shown phase, else the average of the phases
    bar = ret[phase].astype(float) if phase is not None else ret.mean(axis=0)
    bar_r = _round_dollars(bar)
    low, high = ret.min(axis=0), ret.max(axis=0)
    y_pct = _reduce(100.0 * g["yield_rel"], planted, phase)
    chance = _reduce(g["fail_prob"], planted, phase)
    water_in = _reduce(g["irrigation_mm"] / MM_PER_INCH, planted, phase) if irrigated else None
    met_raw = 1.0 - g["shortfall_mm"] / np.maximum(g["need_mm"], 1e-9)
    met_shown = _reduce(met_raw, planted & (g["need_mm"] > 0), phase)
    amount = _reduce(g["yield"], planted, phase)
    failed_phases = g["failed"].sum(axis=0)

    worst_order = sorted(range(len(years)), key=lambda i: (bar_r[i], int(years[i])))
    worst_rank = {i: r + 1 for r, i in enumerate(worst_order[:WORST_MARKED])}

    def num(x, digits=0):
        return None if x is None or not np.isfinite(x) else (int(round(x)) if digits == 0 else round(float(x), digits))

    rows = []
    for i, year in enumerate(years):
        crops_here = [CROPS[int(g["crop"][q, i])] for q in range(n)]
        row = {
            "year": int(year), "dry": bool(dry[i]), "worst_rank": worst_rank.get(i),
            "crop": crops_here[phase] if phase is not None else (crops_here[0] if n == 1 else None),
            "return": int(bar_r[i]), "low": int(low[i]), "high": int(high[i]),
            "yield_pct": num(y_pct[i]),
            "yield": num(amount[i], 1),
            "fail_chance": num(chance[i], 2),
            "failed": (bool(g["failed"][phase, i]) if phase is not None else None),
            "failed_phases": int(failed_phases[i]),
            "water_in": num(water_in[i], 1) if water_in is not None else None,
            "water_met": num(met_shown[i], 2),
            "below_cost": bool(bar_r[i] < 0),
            "crops_by_phase": crops_here, "returns_by_phase": [int(v) for v in ret[:, i]],
        }
        rows.append(row)

    # headline: the shown phase when there is one, else every phase and year pooled
    year_grid = np.broadcast_to(years[None, :], ret.shape)
    dry_grid = np.broadcast_to(dry[None, :], ret.shape)
    crop_names = np.array(CROPS, dtype=object)[g["crop"]]
    yld = 100.0 * g["yield_rel"]
    pooled = _summary(ret.ravel(), year_grid.ravel(), dry_grid.ravel(), planted.ravel(), g["failed"].ravel(),
                      list(crop_names.ravel()), yld.ravel())
    if phase is not None:
        head = _summary(ret[phase], years, dry, planted[phase], g["failed"][phase], list(crop_names[phase]), yld[phase])
        basis = "this_start"
    else:
        head, basis = pooled, "all_starts"

    # the effects' own range: the same headline under the strong and the weak end of every rotation effect
    spread = None
    if engine.rotation.ranges_differ():
        picks = []
        for variant in ("strong", "weak"):
            gv = _grids(obs, unit, irrigated, variant)
            r = _round_dollars(gv["profit"])
            picks.append(r[phase] if phase is not None else r.ravel())
        mine = ret[phase] if phase is not None else ret.ravel()
        every = [mine, *picks]
        worst_all = [int(r.min()) for r in every]
        median_all = [_median(r) for r in every]
        below_all = [float((r < 0).mean()) for r in every]
        if min(worst_all) != max(worst_all) or min(median_all) != max(median_all):
            spread = {"worst_return": [min(worst_all), max(worst_all)], "median_return": [min(median_all), max(median_all)],
                      "below_cost_share": [round(min(below_all), 3), round(max(below_all), 3)]}

    marked = [rows[i]["year"] for i in sorted(worst_rank, key=worst_rank.get)]
    worst_dry = sum(1 for i in worst_rank if dry[i])
    cycle_crops = [c for c in dict.fromkeys(unit) if c != "fallow"]
    return {
        "name": name, "cycle": list(unit), "phases": n,
        "phase": {"known": phase is not None, "count": n,
                  "starts_with": unit[0], "shown": phase,
                  "first_year": int(anchor) if anchor is not None else None},
        "basis": basis,
        "years": rows,
        "summary": head,
        "all_starts": pooled,
        "effects_range": spread,
        "worst_years": marked, "worst_in_dry_years": worst_dry,
        "model_error": [model_error(engine, crop, irrigated) for crop in cycle_crops],
    }


def model_error(engine: Engine, crop: str, irrigated: bool) -> dict:
    """How far the crop model typically misses one year's yield, as a share of a normal yield (its own
    leave-one-year-out error in the calibration), and how well that series is fitted."""
    entry = engine.params["series"][crop]["irrigated" if irrigated else "dryland"]
    sd = entry.get("resid_sd")
    return {"crop": crop, "typical_miss_pct": None if sd is None else int(round(100 * float(sd))),
            "status": entry.get("status")}


# --- words -----------------------------------------------------------------------------------------------

def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _year_list(values: Sequence[int]) -> str:
    values = list(values)
    if len(values) <= 1:
        return "".join(str(v) for v in values)
    return ", ".join(str(v) for v in values[:-1]) + " and " + str(values[-1])


def sentences_for(series: dict, obs: Observed, irrigated: bool) -> list:
    """Plain sentences from the numbers of one replay, the way the app shows them."""
    s = series["summary"]
    first, last = int(obs.years[0]), int(obs.years[-1])
    n_years = len(obs.years)
    out = []
    worst, median = s["worst_year"], s["median_return"]
    if series["basis"] == "this_start":
        lead = (f"In the real weather of {first}-{last}, this rotation would have returned about {_dollars(median)} an "
                f"acre over direct costs in the middle year and {_dollars(worst['return'])} in the worst year "
                f"({worst['year']}).")
    else:
        lead = (f"In the real weather of {first}-{last}, over every year it could have started in, this rotation would "
                f"have returned about {_dollars(median)} an acre over direct costs in the middle crop-year and "
                f"{_dollars(worst['return'])} in the worst ({worst['year']}, {worst['crop']}).")
    out.append(lead)
    k = s["below_cost_years"]
    if k == 0:
        out.append("It would not have come in under its direct costs in any year.")
    else:
        what = "year" if series["basis"] == "this_start" else "crop-year"
        total = s["years"] if series["basis"] == "this_start" else s["crop_years"]
        out.append(f"It would have come in under its direct costs in {k} of {total} {what}s "
                   f"({round(100 * s['below_cost_years'] / total)}%).")
    if s["planted_years"]:
        f = s["failed_years"]
        out.append(f"A crop would have failed (under half of a normal yield in our crop model) in {f} of "
                   f"{s['planted_years']} planted years." if f else
                   "No crop would have failed (under half of a normal yield in our crop model) in any planted year.")
    d = s["dry"]
    if d["years"] and d["mean_return"] is not None and d["other_mean_return"] is not None:
        dry_list = _year_list(obs.dry_years)
        out.append(f"In the {len(obs.dry_years)} driest growing seasons on record ({dry_list}) the average return was "
                   f"{_dollars(d['mean_return'])}, against {_dollars(d['other_mean_return'])} in the other years.")
        if not irrigated and d.get("mean_yield_pct") is not None and d.get("other_mean_yield_pct") is not None:
            out.append(f"Crops yielded about {d['mean_yield_pct']}% of a normal year in those dry seasons, against "
                       f"{d['other_mean_yield_pct']}% in the others.")
    return out


def compare(plan: dict, carry: dict, obs: Observed) -> dict:
    """The plan against carrying on as now, year by year in the same weather (both lined up on the field)."""
    a = np.array([r["return"] for r in plan["years"]])
    b = np.array([r["return"] for r in carry["years"]])
    diff = a - b
    ahead, behind, same = int((diff > 0).sum()), int((diff < 0).sum()), int((diff == 0).sum())
    sp, sc = plan["summary"], carry["summary"]
    n = len(diff)
    mean = int(_round_dollars(diff.mean()))
    out = {
        "against": carry["name"], "years": [int(y) for y in obs.years],
        "difference": [int(v) for v in diff], "carry_return": [int(v) for v in b],
        "mean_difference": mean, "median_difference": _median(diff),
        "years_ahead": ahead, "years_behind": behind, "years_same": same, "total": n,
        "worst_year": {"plan": sp["worst_year"]["return"], "carry_on": sc["worst_year"]["return"]},
        "median_return": {"plan": sp["median_return"], "carry_on": sc["median_return"]},
        "below_cost_share": {"plan": sp["below_cost_share"], "carry_on": sc["below_cost_share"]},
        "below_cost_years": {"plan": sp["below_cost_years"], "carry_on": sc["below_cost_years"]},
        "failed_years": {"plan": sp["failed_years"], "carry_on": sc["failed_years"]},
    }
    if ahead == behind == 0:
        text = "In every year the two would have returned the same: this is what the field does now."
    else:
        verdict = (f"about {_dollars(mean)} an acre a year more" if mean > 0
                   else f"about {_dollars(-mean)} an acre a year less" if mean < 0 else "about the same on average")
        text = (f"Against carrying on as now ({carry['name']}), it would have come out ahead in {ahead} of {n} years and "
                f"behind in {behind}, returning {verdict} over the {n} years.")
    worse = sc["worst_year"]["return"] < sp["worst_year"]["return"]
    better = sc["worst_year"]["return"] > sp["worst_year"]["return"]
    worst = (f"The worst year would have been {_dollars(sp['worst_year']['return'])} for the plan against "
             f"{_dollars(sc['worst_year']['return'])} for carrying on" +
             (", so the plan would have been steadier in the bad years." if worse else
              ", so carrying on would have been steadier in the bad years." if better else "."))
    out["sentences"] = [text, worst]
    return out


# --- the entry point -------------------------------------------------------------------------------------

def _last_recorded_year(crop_history) -> int | None:
    years = []
    for year in (crop_history or {}):
        try:
            years.append(int(year))
        except (TypeError, ValueError):
            continue
    return max(years) if years else None


def replay(field: FieldInput, *, plan: int | None = None, rotation: Sequence[str] | None = None,
           weights=None, scenario: str = config.DEFAULT_SCENARIO, horizon: int = config.TODAY,
           irrigated: bool | None = None, traits: FieldTraits | None = None, engine: Engine | None = None,
           history=None) -> dict:
    """The body of GET /field/{id}/replay.

    plan      1, 2 or 3: that pick of /plans for the same settings (the default is 1); or
    rotation  the farmer's own rotation, 2 to 6 crops, the first crop being next year's;
    The other arguments are those of `rank_plans_smart`. ReplayError / RotationError (ValueError) carry one plain
    sentence; ReplayUnavailable when the observed tables are missing. `history`: the observed table, for tests.
    """
    if rotation is not None and plan is not None:
        raise ReplayError("Give either a rotation or a plan number, not both.")
    engine = engine if engine is not None else smart.get_engine()
    obs = observed_for(engine, history)
    result = smart.rank_plans_smart(field, weights=weights, scenario=scenario, horizon=horizon,
                                    irrigated=irrigated, traits=traits, engine=engine)
    is_irrigated = bool(result.settings["irrigated"])
    if rotation is None:
        plan = 1 if plan is None else plan
        if isinstance(plan, bool) or not isinstance(plan, int) or not 1 <= plan <= len(result.plans):
            raise ReplayError(f"The plan must be a number from 1 to {len(result.plans)}.")
        picked = result.plans[plan - 1]
        cycle, name, source = tuple(picked.cycle), picked.name, "plan"
    else:
        cycle = custom.check_cycle(rotation)
        unit = custom.repeating_unit(cycle)
        name = rotation_name(list(unit)) if len(unit) == 1 else custom.cycle_name(cycle)
        source, plan = "own", None

    last_recorded = _last_recorded_year(field.crop_history)
    anchor = None if last_recorded is None else last_recorded + 1
    subject = _series(obs, custom.repeating_unit(cycle), is_irrigated, anchor, name, engine)
    subject["sentences"] = sentences_for(subject, obs, is_irrigated)

    carry = None
    carry_note = None
    run, _ = transition.real_years(field.crop_history)
    history_crops = [crop for _, crop in run]
    pattern, repeating = transition.carry_on_pattern(history_crops)
    if pattern is None:
        carry_note = ("Fewer than four years in a row of the six crops are on record, so there is no \"carry on as "
                      "now\" to compare with.")
    else:
        carry_name = custom.cycle_name(pattern) if len(pattern) > 1 else f"{rotation_name(list(pattern))} every year"
        carry = _series(obs, pattern, is_irrigated, run[-1][0] + 1, carry_name, engine)
        carry["sentences"] = sentences_for(carry, obs, is_irrigated)
        carry["repeating"] = bool(repeating)
        if not repeating:
            carry_note = (f"The last four years ({run[-4][0]}-{run[-1][0]}) are not a repeating pattern, so \"carry on "
                          "as now\" repeats those four years as they are.")

    comparison = None
    if carry is not None and subject["phase"]["known"] and carry["phase"]["known"]:
        comparison = compare(subject, carry, obs)

    first, last = int(obs.years[0]), int(obs.years[-1])
    if subject["phase"]["known"]:
        phase_note = (f"The bars show the rotation in the order shown on the card, lined up so that its first year is "
                      f"{anchor}, the year after the last one on record for this field. The thin lines show the best and "
                      f"worst of the {subject['phases']} possible starting years." if subject["phases"] > 1 else
                      "This rotation grows the same crop every year, so there is one starting year.")
    else:
        phase_note = ("No crop history is on record for this field, so the bars show the average of every possible "
                      "starting year of the rotation and the thin lines the best and worst of them.")
    settings = {k: result.settings[k] for k in ("irrigated", "irrigation_source", "irrigation_basis", "weights",
                                                "scenario", "horizon", "profit_basis", "irrigation_limit_mm",
                                                "irrigation_limit_in", "rotation_effects")}
    notes = [HISTORY_NOT_FORECAST, phase_note,
             f"The weather is the observed record {first}-{last}; a year is marked dry when its April-September rain is "
             f"among the driest {len(obs.dry_years)} of the {len(obs.years)} years.",
             "Dollars are returns over direct costs, not profit; prices and costs are today's, the same as on the plan "
             "cards."]
    if is_irrigated:
        limit = settings["irrigation_limit_in"]
        notes.append(f"Irrigated years pump what the crop needs up to {limit:g} inches an acre a year; a crop that needs "
                     "more goes short in the replay as it does in the plans." if limit else
                     "Irrigated years pump all the water the crop needs.")
    else:
        notes.append(DRYLAND_MONEY_NOTE)
    if carry_note:
        notes.append(carry_note)
    return {
        "field_id": field.field_id, "engine": "smart", "status": "ok", "settings": settings,
        "subject": {"source": source, "plan": plan, "name": name, "cycle": list(cycle)},
        "weather": {"source": "NASA POWER observed weather (data/climate_history.parquet)",
                    "first_year": first, "last_year": last, "years": len(obs.years),
                    "normal_season_rain_mm": int(round(obs.normal_rain_mm)),
                    "season": "April-September",
                    "dry_rule": (f"A year is dry when its April-September rain is among the driest "
                                 f"{len(obs.dry_years)} of the {len(obs.years)} years on record (the lowest fifth)."),
                    "dry_years": list(obs.dry_years), "rows": [dict(w) for w in obs.weather]},
        "plan": subject,
        "carry_on": carry,
        "comparison": comparison,
        "notes": notes, "not_modelled": list(NOT_MODELLED),
    }
