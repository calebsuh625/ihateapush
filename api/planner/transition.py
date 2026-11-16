"""Transition path (Phase 4, item 2): how a field gets from what it grows now to a chosen rotation.

    from api.planner.transition import transition_path
    body = transition_path(field, plan=1, years=8, weights=..., scenario="ssp245", horizon=2050)
    body = transition_path(field, target=["corn", "soybeans"], years=8)        # GET /field/{id}/transition

A farm cannot switch overnight: next year's crop follows this year's. So the path starts from the
field's real last years (crop_history), picks the year of the target cycle to start in and, when no
year of the cycle can follow the field's last crops, up to two BRIDGE years first.

The rules on the way in
-----------------------
Every step from the last real crop onward is checked against the planner's rules (candidates.py) in
the form that involves the crops before it (`step_breaks`):

    no winter wheat / fallow / soybeans straight after itself;
    no more than two years in a row of corn, or of sorghum (the two years before count);
    an alfalfa stand is not ended before its third year (the real stand's years count; a stand that
        reaches back to the start of the record may be older, so it may be ended);
    dryland: no winter wheat straight after corn, sorghum or soybeans; no alfalfa, no soybeans; no corn
        within two years of corn ("one corn year in a cycle": the shortest cycle is three years);
    irrigated: no fallow within three years of fallow ("one fallow year in four").

What the target cycle breaks by itself, year after year once it is established, is the farmer's
choice and is not held against the path (`seam_breaks` takes it out; the answer lists it under
`target.rules_broken`). Among the valid paths the one with the fewest bridge years wins, then the one
that returns the most over the span, then the fewer bare days. When the field's last years already
follow the cycle there is nothing to choose: the path is the cycle's next years. When nothing valid
exists within two bridge years the answer says so and names the rules in the way.

The numbers
-----------
Each year is the engine's own figure for that crop after the crop before it (`Engine.crop_year`):
return over direct costs per acre with the lowest and highest of the six climate models, water pumped
(irrigated) or the share of the crop's water need rain covers (dryland), bare days, the chance the
crop fails. The weather is the average of the climate window of the chosen future, the same settings
as GET /field/{id}/plans, in every year: it is not a forecast for a calendar year.

"Carry on as now" is the field's current pattern continued and scored the same way: the last four
real years repeat with period 1, 2 or 3, or with period 4 when the years before confirm it; otherwise
those four years are repeated as they are and the answer says so. A field that is already in the
target cycle carries on with that cycle, so its difference is zero.

The difference (path minus carry on) is paired: both face the same weather in each climate model, so
its low and high are the lowest and highest model of the difference itself. `payback` is the first
year from which the running total stays at or above zero to the end of the span. `long_run` compares
the two as repeating cycles (the plan cards' own numbers), and names a winner on returns only when
every climate model agrees, as custom.py does.
"""
from __future__ import annotations

from itertools import product
from typing import Callable, Mapping, Sequence

import numpy as np

from api.planner import candidates as cand
from api.planner import config, custom, futures, smart
from api.planner.crops import CROP_TABLE, CROPS
from api.planner.legacy import FieldInput, rotation_name
from api.planner.smart import MM_PER_INCH, Engine, Weights
from api.planner.traits import FieldTraits

MIN_SPAN, MAX_SPAN, DEFAULT_SPAN = 4, 12, 8
MAX_BRIDGE_YEARS = 2
HISTORY_SHOWN = 3          # real years shown in front of the path
ALREADY_YEARS = 3          # the last years that must follow the cycle to say "already there" (or its length, if longer)
MIN_STAND_YEARS = 3        # an alfalfa stand (candidates.py: "a stand of three or more years")
CORN_GAP_YEARS = 2         # dryland: no corn within this many years of corn (one corn year in a cycle of three or more)
FALLOW_GAP_YEARS = 3       # irrigated: no fallow within this many years of fallow (one fallow year in four)
SUMMER_BEFORE_WHEAT = ("corn", "sorghum", "soybeans")

_RULES = {rule.key: rule for rule in cand.RULES}
_RULE_ORDER = [rule.key for rule in cand.RULES]
STEP_RULES = ("no_wheat_after_wheat", "no_fallow_after_fallow", "no_soybeans_after_soybeans", "alfalfa_stand",
              "no_long_grain_run", "dryland_no_alfalfa", "dryland_no_soybeans", "dryland_one_corn_year",
              "dryland_wheat_after_fallow", "irrigated_one_fallow_year")
assert set(STEP_RULES) == set(_RULES), "candidates.RULES changed: decide how each rule reads year by year"

DRYLAND_MONEY_NOTE = (
    "On dryland the sign of these returns depends on the price years and the cost of fallow, so read the dollar "
    "figures as a rough guide."
)
ROUNDING_NOTE = (
    "Figures are rounded to the dollar, so a running total can differ by a few dollars from the sum of the "
    "rounded years."
)
LONG_RUN_NOTE = "The long-run figures are each rotation as a repeating cycle, the way the plan cards score it."
NOT_MODELLED = (
    "Equipment, storage, contracts, lease and crop insurance terms, and the learning curve of a crop that is new "
    "to the farm.",
    "Carry-over from the crop before other than the soil water it leaves: residue, weeds, disease and herbicide "
    "plant-back intervals.",
    "A run of dry or wet years: every year gets the average weather of the climate window.",
    "Changes in prices and costs: they stay at today's levels in every year.",
)
ALREADY_SHORT_TERM = "Short term: there is nothing to switch, so there is no cost; this field is already in this rotation."
ALREADY_LONG_TERM = "Long term: carrying on as now is this rotation, so the two return the same."
ALFALFA_NOT_MODELLED = (
    "The weaker first year of a new alfalfa stand and the cost of seeding it: every alfalfa year gets the same "
    "yield and the same cost."
)


class TransitionError(ValueError):
    """The request cannot be answered as asked. The message is one plain sentence."""


# --- the field's real years --------------------------------------------------------------------------

def real_years(crop_history) -> tuple:
    """(run, shown): `run` is the unbroken stretch of years ending at the last recorded year in which the
    field grew one of the six crops, oldest first, as [(year, crop)]; empty when the last recorded year
    is something else. `shown` is the last recorded calendar years as they are (at most HISTORY_SHOWN)."""
    years = {}
    for year, crop in (crop_history or {}).items():
        try:
            years[int(year)] = str(crop).strip().lower()
        except (TypeError, ValueError):
            continue
    if not years:
        return [], []
    last = max(years)
    run, year = [], last
    while year in years and years[year] in CROPS:
        run.append((year, years[year]))
        year -= 1
    run.reverse()
    shown = [(y, years[y]) for y in range(last - HISTORY_SHOWN + 1, last + 1) if y in years]
    return run, shown


# --- the rules, one step at a time -------------------------------------------------------------------

def _trailing(before: Sequence[str], crop: str) -> int:
    """How many of the last years in a row were `crop`."""
    n = 0
    for c in reversed(before):
        if c != crop:
            break
        n += 1
    return n


def step_breaks(before: Sequence[str], crop: str, irrigated: bool, open_start: bool = True) -> list:
    """Keys of the planner's rules (candidates.RULES) that planting `crop` breaks after the years in
    `before` (oldest first). [] = allowed.

    `open_start`: what grew before `before[0]` is not known, so an alfalfa stand that reaches back to
    the start may be older than it looks and may be ended.
    """
    before = list(before)
    last = before[-1] if before else None
    broken = set()
    for same, key in (("winter wheat", "no_wheat_after_wheat"), ("fallow", "no_fallow_after_fallow"),
                      ("soybeans", "no_soybeans_after_soybeans")):
        if crop == same and last == same:
            broken.add(key)
    if last == "alfalfa" and crop != "alfalfa":
        stand = _trailing(before, "alfalfa")
        if stand < MIN_STAND_YEARS and not (open_start and stand == len(before)):
            broken.add("alfalfa_stand")
    if crop in cand.GRAIN_CROPS and _trailing(before, crop) >= cand.MAX_GRAIN_RUN:
        broken.add("no_long_grain_run")
    if irrigated:
        if crop == "fallow" and "fallow" in before[-FALLOW_GAP_YEARS:]:
            broken.add("irrigated_one_fallow_year")
    else:
        if crop == "alfalfa":
            broken.add("dryland_no_alfalfa")
        if crop == "soybeans":
            broken.add("dryland_no_soybeans")
        if crop == "corn" and "corn" in before[-CORN_GAP_YEARS:]:
            broken.add("dryland_one_corn_year")
        if crop == "winter wheat" and last in SUMMER_BEFORE_WHEAT:
            broken.add("dryland_wheat_after_fallow")
    return [key for key in _RULE_ORDER if key in broken]


def rule_sentences(keys) -> list:
    """The "Our plans avoid ..." sentences of the given rule keys, in the planner's order."""
    keys = set(keys)
    return [_RULES[key].gentle or _RULES[key].text for key in _RULE_ORDER if key in keys]


def cycle_from(unit: Sequence[str], phase: int, years: int) -> list:
    """`years` crops of the cycle, starting at its year `phase` (0-based)."""
    return [unit[(phase + i) % len(unit)] for i in range(years)]


def seam_breaks(history: Sequence[str], bridge: Sequence[str], unit: Sequence[str], phase: int,
                irrigated: bool) -> list:
    """[(year of the path, 1-based; crop; rule key)]: what the way in breaks. Checked for the bridge years
    and two rounds of the cycle, by when every crop before a year is the cycle's own.

    A rule the cycle breaks in the same year once it is established (the years before are the cycle
    itself) is the cycle's own doing and is left out.
    """
    unit = tuple(unit)
    length = len(unit)
    path = list(bridge) + cycle_from(unit, phase, 2 * length)
    seen = list(history)
    out = []
    for i, crop in enumerate(path):
        broken = step_breaks(seen, crop, irrigated)
        if broken and i >= len(bridge):
            at = (phase + i - len(bridge)) % length
            established = [unit[(at - k) % length] for k in range(3 * length, 0, -1)]
            own = set(step_breaks(established, crop, irrigated, open_start=False))
            broken = [key for key in broken if key not in own]
        out.extend((i + 1, crop, key) for key in broken)
        seen.append(crop)
    return out


def already_in(history: Sequence[str], unit: Sequence[str]) -> int | None:
    """The cycle year (0-based) that comes next when the field's last years already follow the cycle:
    its last max(len(unit), ALREADY_YEARS) years are the cycle read through. None otherwise, also when
    fewer years than that are on record."""
    unit = tuple(unit)
    length, n = len(unit), max(len(unit), ALREADY_YEARS)
    if len(history) < n:
        return None
    tail = list(history)[-n:]
    for phase in range(length):
        if all(tail[n - 1 - k] == unit[(phase - 1 - k) % length] for k in range(n)):
            return phase
    return None


def find_path(history: Sequence[str], unit: Sequence[str], irrigated: bool,
              value: Callable[[list], tuple] | None = None, years: int = DEFAULT_SPAN) -> dict:
    """The way from the field's last years into the cycle.

    history  the real crops, oldest first (at least one)
    unit     the target cycle (its shortest repeating unit)
    value    path (list of `years` crops) -> something to sort on, larger is better; None = first found
    Returns {"status": "already_there" | "path" | "no_path", "bridge": [...], "phase": int | None,
             "options": how many valid ways in there were with that many bridge years,
             "blocked": rule keys in the way of starting the cycle straight away (when a bridge was needed
                        or nothing was found; with nothing found, also what waiting on the last crop breaks)}.
    """
    history, unit = list(history), tuple(unit)
    phase = already_in(history, unit)
    if phase is not None:
        return {"status": "already_there", "bridge": [], "phase": phase, "options": 1, "blocked": []}
    blocked: set = set()
    waiting: set = set()
    for n_bridge in range(MAX_BRIDGE_YEARS + 1):
        found = []
        for bridge in product(CROPS, repeat=n_bridge):
            for start in range(len(unit)):
                broken = seam_breaks(history, bridge, unit, start, irrigated)
                if not broken:
                    found.append((list(bridge), start))
                elif n_bridge == 0 or set(bridge) == set(history[-1:]):
                    # in the way of starting straight away, or of waiting on the crop that is there now
                    (blocked if n_bridge == 0 else waiting).update(key for _, _, key in broken)
        if found:
            if value is not None:
                order = {crop: i for i, crop in enumerate(CROPS)}
                # larger value first; then the earlier cycle year and the bridge crops in the fixed crop order
                found.sort(key=lambda option: (tuple(-v for v in value(
                    (option[0] + cycle_from(unit, option[1], years))[:years])), option[1],
                    tuple(order[crop] for crop in option[0])))
            bridge, start = found[0]
            return {"status": "path", "bridge": bridge, "phase": start, "options": len(found),
                    "blocked": [key for key in _RULE_ORDER if key in blocked] if n_bridge else []}
    return {"status": "no_path", "bridge": [], "phase": None, "options": 0,
            "blocked": [key for key in _RULE_ORDER if key in blocked | waiting]}


# --- carry on as now ---------------------------------------------------------------------------------

def carry_on_pattern(history: Sequence[str]) -> tuple:
    """(pattern, repeating): the crops "carry on as now" repeats, oldest first, so that next year is
    pattern[0]. From the last four real years: their shortest period (1, 2 or 3), or all four when the
    years before them (up to four) confirm a 4-year pattern. Otherwise the four years as they are, with
    `repeating` False. (None, False) with fewer than four years."""
    history = list(history)
    if len(history) < cand.YEARS:
        return None, False
    last = history[-cand.YEARS:]
    for period in (1, 2, 3):
        if all(last[i] == last[i - period] for i in range(period, cand.YEARS)):
            return tuple(last[-period:]), True
    earlier = history[:-cand.YEARS][-cand.YEARS:]
    confirmed = bool(earlier) and all(crop == last[(i - len(earlier)) % cand.YEARS] for i, crop in enumerate(earlier))
    return tuple(last), confirmed


# --- numbers -----------------------------------------------------------------------------------------

def _money(value: float) -> str:
    return custom._dollars(value)


def _spread(values: np.ndarray, digits: int = 0) -> dict:
    """{mid, low, high}: the mean over the climate models, the lowest and the highest model."""
    out = {"mid": round(float(values.mean()), digits), "low": round(float(values.min()), digits),
           "high": round(float(values.max()), digits)}
    return {k: (int(v) if digits == 0 else v) for k, v in out.items()}


def _range_words(spread: dict) -> str:
    """"−$60 to −$30 across the six climate models", or that they all say the same."""
    if spread["low"] == spread["high"]:
        return "the same in every climate model"
    return f"{_money(spread['low'])} to {_money(spread['high'])} across the climate models"


def score_years(engine: Engine, crops: Sequence[str], first_prev: str, irrigated: bool, scenario: str,
                horizon: int, first_year: int, roles: Sequence[str], cycle_years: Sequence) -> tuple:
    """(rows, profit [year, model]): each crop after the one before it (the first after `first_prev`)."""
    rows, profits = [], []
    prev = first_prev
    for i, crop in enumerate(crops):
        y = engine.crop_year(irrigated, scenario, horizon, crop, prev)
        profits.append(np.asarray(y["profit"], dtype=float))
        irrigation = float(np.mean(y["irrigation_mm"]))
        planted = crop != "fallow"
        rows.append({
            "n": i + 1, "year": first_year + i, "crop": crop, "after": prev,
            "role": roles[i], "cycle_year": cycle_years[i],
            "return": _spread(profits[-1]),
            "yield": round(float(np.mean(y["yield"])), 1) if planted else None,
            "yield_unit": CROP_TABLE[crop].unit if planted else None,
            "irrigation_mm": round(irrigation, 0), "irrigation_in": round(irrigation / MM_PER_INCH, 1),
            "water_met_share": round(float(np.mean(y["water_met"])), 3) if planted else None,
            "bare_days": round(y["bare_days"], 1),
            "fail_chance": round(float(np.mean(y["fail_prob"])), 3) if planted else None,
        })
        prev = crop
    return rows, np.array(profits)


def payback(cumulative: Sequence[float]) -> dict:
    """When the switch has paid back, from the running total of the difference (one figure a year).

    status  "no_difference"   the two are level in every year
            "never_behind"    the running total is never below zero
            "paid_back"       it is below zero at some point and at or above zero from year `n` to the end
            "not_within_span" it is below zero at the end
    `n` is that year (1-based) for "paid_back", else None. `lowest` is the lowest running total and its year.
    """
    cumulative = [float(v) for v in cumulative]
    low = min(cumulative)
    lowest = {"n": cumulative.index(low) + 1, "value": low}
    if all(v == 0 for v in cumulative):
        return {"status": "no_difference", "n": None, "lowest": lowest}
    if low >= 0:
        return {"status": "never_behind", "n": None, "lowest": lowest}
    if cumulative[-1] < 0:
        return {"status": "not_within_span", "n": None, "lowest": lowest}
    n = len(cumulative)
    while n > 1 and cumulative[n - 2] >= 0:
        n -= 1
    return {"status": "paid_back", "n": n, "lowest": lowest}


def difference(path_rows: list, path_profit: np.ndarray, carry_rows: list, carry_profit: np.ndarray,
               irrigated: bool) -> dict:
    """Path minus carry on, year by year and as a running total, with the payback year.

    A year's `return.mid` is the difference of the two rounded returns shown, so the three figures add
    up; its low and high, and the running totals, come from the climate models one by one (both face
    the same weather in a model)."""
    gap = path_profit - carry_profit                    # [year, model]
    running = np.cumsum(gap, axis=0)
    bare = 0.0
    years = []
    for i, (p, c) in enumerate(zip(path_rows, carry_rows)):
        each = _spread(gap[i])
        each["mid"] = p["return"]["mid"] - c["return"]["mid"]
        bare += p["bare_days"] - c["bare_days"]
        row = {"n": p["n"], "year": p["year"], "return": each, "cumulative": _spread(running[i]),
               "bare_days": round(p["bare_days"] - c["bare_days"], 1), "cumulative_bare_days": round(bare, 1)}
        if irrigated:
            row["irrigation_in"] = round(p["irrigation_in"] - c["irrigation_in"], 1)
        years.append(row)
    back = payback([row["cumulative"]["mid"] for row in years])
    back["year"] = years[back["n"] - 1]["year"] if back["n"] is not None else None
    back["lowest"]["year"] = years[back["lowest"]["n"] - 1]["year"]
    back["lowest"]["value"] = int(back["lowest"]["value"])
    end = running[-1]
    back["models_ahead_at_end"] = int((np.round(end) >= 0).sum())
    back["models"] = int(end.size)
    return {"years": years, "payback": back, "total": years[-1]["cumulative"]}


def _long_run_side(engine: Engine, cycle: Sequence[str], irrigated: bool, scenario: str, horizon: int,
                   field_numbers: dict) -> tuple:
    """(figures, profit per model) of a rotation as a repeating cycle: what a plan card shows for it."""
    form = custom.normal_form(cycle)
    ev = engine.evaluate(irrigated, scenario, horizon, rotations=[form], max_years=custom.MAX_YEARS, **field_numbers)
    planted = not custom.nothing_planted(form)
    irrigation = float(ev.irrigation[0].mean())
    ret = _spread(ev.profit[0])
    ret["low"], ret["high"] = smart.profit_extremes(ev, 0)       # the climate models and the rotation effects' range
    figures = {
        "return": ret,
        "bare_days": round(float(ev.bare[0]), 1),
        "irrigation_mm": round(irrigation, 0), "irrigation_in": round(irrigation / MM_PER_INCH, 1),
        "water_met_share": round(float(ev.water_met[0].mean()), 3) if planted else None,
        "failed_share": round(float(ev.failed[0].mean()), 3) if planted else None,
    }
    return figures, ev.profit[0]


def long_run(engine: Engine, target: Sequence[str], carry: Sequence[str], irrigated: bool, scenario: str,
             horizon: int, field_numbers: dict) -> dict:
    plan, plan_profit = _long_run_side(engine, target, irrigated, scenario, horizon, field_numbers)
    carry_on, carry_profit = _long_run_side(engine, carry, irrigated, scenario, horizon, field_numbers)
    gap = _spread(plan_profit - carry_profit)
    gap["mid"] = plan["return"]["mid"] - carry_on["return"]["mid"]
    v = custom.versus(plan_profit, carry_profit, same=gap["mid"] == 0)
    verdict = {"yours": "plan", "ours": "carry_on"}.get(v["verdict"], v["verdict"])
    diff = {
        "return": gap,
        "verdict": verdict,          # "plan" / "carry_on": ahead in every climate model; "too_close"; "same"
        "models": {"plan_ahead": v["yours_ahead"], "carry_on_ahead": v["ours_ahead"], "total": v["total"]},
        "bare_days": round(plan["bare_days"] - carry_on["bare_days"], 1),
    }
    if irrigated:
        diff["irrigation_in"] = round(plan["irrigation_in"] - carry_on["irrigation_in"], 1)
    if plan["failed_share"] is not None and carry_on["failed_share"] is not None:
        diff["failed_share"] = round(plan["failed_share"] - carry_on["failed_share"], 3)
    return {"plan": plan, "carry_on": carry_on, "difference": diff}


# --- words -------------------------------------------------------------------------------------------

def _years_words(n: int) -> str:
    return "1 year" if n == 1 else f"{n} years"


def _crop_list(crops: Sequence[str]) -> str:
    return custom._list(list(crops))


def bridge_words(rows: list) -> str:
    """"1 bridge year (alfalfa in 2025)" / "2 bridge years (alfalfa in 2025 and 2026)"."""
    bridge = [row for row in rows if row["role"] == "bridge"]
    if not bridge:
        return ""
    if len({row["crop"] for row in bridge}) == 1:
        what = f"{bridge[0]['crop']} in {custom._list([str(row['year']) for row in bridge])}"
    else:
        what = custom._list([f"{row['crop']} in {row['year']}" for row in bridge])
    return f"{len(bridge)} bridge {'year' if len(bridge) == 1 else 'years'} ({what})"


def short_term_sentence(diff: dict, path_rows: list) -> str:
    """What the switch costs at first, from the running total of the difference."""
    span = len(diff["years"])
    back = diff["payback"]
    bridge = bridge_words(path_rows)
    lead = f"Short term: the switch takes {bridge} before the plan starts, and " if bridge else "Short term: "
    if back["status"] == "no_difference":
        return f"{lead}this path returns the same as carrying on as now in every one of the {span} years."
    if back["status"] == "never_behind":
        return (f"{lead}no cost shows in these numbers, as this path is level with or ahead of carrying on as now "
                f"in every one of the {span} years.")
    low = diff["years"][back["lowest"]["n"] - 1]
    amount = f"${abs(low['cumulative']['mid']):,.0f}"
    if back["status"] == "not_within_span":
        return (f"{lead}this path falls behind carrying on as now and has not caught up within the {span} years; at "
                f"its lowest point, after {low['year']} (year {low['n']}), it is about {amount} an acre behind "
                f"({_range_words(low['cumulative'])}).")
    return (f"{lead}at its lowest point, after {low['year']} (year {low['n']}), this path is about {amount} an acre "
            f"behind carrying on as now ({_range_words(low['cumulative'])}).")


def long_term_sentence(run: dict, diff: dict, irrigated: bool, what: str = "the plan") -> str:
    """What the plan gains (or does not) once it is established, and whether the switch has paid back.
    `what`: how the target is called ("the plan" for one of ours, "your rotation" for the farmer's own)."""
    d = run["difference"]
    gap, models = d["return"], d["models"]
    amount = f"${abs(gap['mid']):,.0f}"
    spread = _range_words(gap)
    if d["verdict"] == "plan":
        money = f"{what} returns about {amount} an acre a year more than carrying on as now ({spread})"
    elif d["verdict"] == "carry_on":
        money = f"{what} returns about {amount} an acre a year less than carrying on as now ({spread})"
    elif d["verdict"] == "same":
        money = f"{what} returns about the same as carrying on as now"
    else:
        money = (f"returns are too close to call: {what} returns more in {models['plan_ahead']} of the "
                 f"{models['total']} climate models, carrying on as now in {models['carry_on_ahead']} "
                 f"(the difference runs from {_money(gap['low'])} to {_money(gap['high'])} an acre a year)")
    extras = []
    days = custom._half_up(abs(d["bare_days"]))
    if days >= 1:
        extras.append(f"leaves the soil bare about {days:.0f} {'fewer' if d['bare_days'] < 0 else 'more'} "
                      f"{'day' if days == 1 else 'days'} a year")
    if irrigated and abs(d.get("irrigation_in", 0.0)) >= 0.5:
        inches = custom._inches(abs(d["irrigation_in"]) * MM_PER_INCH)
        extras.append(f"pumps about {inches} {'inch' if inches == '1' else 'inches'} "
                      f"{'less' if d['irrigation_in'] < 0 else 'more'} water a year")
    also = f"; it also {custom._list(extras)}" if extras else ""
    back = diff["payback"]["status"]
    span = len(diff["years"])
    by = f"by {diff['payback']['year']} (year {diff['payback']['n']})"
    if back == "no_difference":
        end = ""
    elif d["verdict"] == "carry_on":
        end = (", so on these numbers the switch does not pay back in dollars" if back == "not_within_span"
               else f", so although this path is not behind after {span} years, on these numbers it falls behind in "
                    "the long run")
    elif back == "paid_back":
        end = f", and the switch has paid back {by}"
    elif back == "not_within_span":
        end = f", {'but' if d['verdict'] == 'plan' else 'and'} the switch has not paid back within these {span} years"
    elif d["verdict"] == "plan":
        end = ", and this path is never behind, so there is nothing to pay back"
    else:
        end = f", and this path is never behind over the {span} years"
    return f"Long term: once established, {money}{end}{also}."


def total_sentence(diff: dict) -> str:
    """The running total at the end of the span, with its range and in how many models it is ahead."""
    span, total, back = len(diff["years"]), diff["total"], diff["payback"]
    if back["status"] == "no_difference":
        return f"After {span} years the two are level."
    side = "ahead of" if total["mid"] > 0 else "behind" if total["mid"] < 0 else "level with"
    amount = "" if total["mid"] == 0 else f"about ${abs(total['mid']):,.0f} an acre "
    return (f"After {span} years this path is {amount}{side} carrying on as now ({_range_words(total)}); "
            f"it is level or ahead in {back['models_ahead_at_end']} of the {back['models']} climate models.")


def path_sentence(found: dict, rows: list, unit: Sequence[str], history_years: list, span: int) -> str:
    """One or two plain sentences on how the path gets in and why this way."""
    last_year, last_crop = history_years[-1]
    if found["status"] == "already_there":
        n = max(len(unit), ALREADY_YEARS)
        return (f"This field is already in this rotation: its last {n} years ({history_years[-n][0]}–{last_year}) "
                "follow it. These are its next years.")
    first = rows[len(found["bridge"])]
    start = f"{first['crop']} (year {first['cycle_year']} of the cycle)"
    if found["bridge"]:
        return (f"No year of this rotation can follow the field's last crops ({last_crop} in {last_year}) without "
                f"breaking what our plans avoid, so the path takes {bridge_words(rows)} and then starts the "
                f"rotation in {first['year']} with {start}.")
    if found["options"] == 1:
        why = (f"the only year of the cycle that can follow {last_crop} ({last_year}) without breaking what our "
               "plans avoid") if len(unit) > 1 else f"it can follow {last_crop} ({last_year})"
        return f"The rotation can start in {first['year']} with {start}: {why}."
    return (f"The rotation can start in {first['year']} with {start}. Of the {found['options']} years of the cycle "
            f"that can follow {last_crop} ({last_year}), this start returns the most over the {span} years.")


# --- the entry point ---------------------------------------------------------------------------------

def check_span(years) -> int:
    if isinstance(years, bool) or not isinstance(years, int) or not MIN_SPAN <= years <= MAX_SPAN:
        raise TransitionError(f"The number of years must be a whole number from {MIN_SPAN} to {MAX_SPAN}.")
    return years


def transition_path(field: FieldInput, *, target: Sequence[str] | None = None, plan: int | None = None,
                    years: int = DEFAULT_SPAN, weights: Weights | Mapping | None = None,
                    scenario: str = config.DEFAULT_SCENARIO, horizon: int = config.TODAY,
                    irrigated: bool | None = None, traits: FieldTraits | None = None,
                    engine: Engine | None = None) -> dict:
    """The body of GET /field/{id}/transition.

    target  the rotation to get to: 2 to 6 of the six crop names (custom.parse_rotation reads typed text), or
    plan    1, 2 or 3: that pick of GET /field/{id}/plans for the same settings (the default is plan 1)
    years   how many years ahead to score, 4 to 12
    The other arguments are those of `rank_plans_smart`. TransitionError / RotationError (both ValueError)
    carry one plain sentence.
    """
    span = check_span(years)
    if target is not None and plan is not None:
        raise TransitionError("Give either a target rotation or a plan number, not both.")
    engine = engine if engine is not None else smart.get_engine()
    result = smart.rank_plans_smart(field, weights=weights, scenario=scenario, horizon=horizon,
                                    irrigated=irrigated, traits=traits, engine=engine)
    is_irrigated = bool(result.settings["irrigated"])
    horizon = result.settings["horizon"]
    traits = traits if traits is not None else engine.traits_for(field.field_id)
    field_numbers = {"wei": traits.wei if traits is not None else None,
                     "weg": traits.weg if traits is not None else None, "slope_pct": field.slope_pct}

    if target is None:
        plan = 1 if plan is None else plan
        if isinstance(plan, bool) or not isinstance(plan, int) or not 1 <= plan <= len(result.plans):
            raise TransitionError(f"The plan must be a number from 1 to {len(result.plans)}.")
        picked = result.plans[plan - 1]
        cycle, name, source = tuple(picked.cycle), picked.name, "plan"
    else:
        cycle = custom.check_cycle(target)
        name, source, plan = custom.cycle_name(cycle), "own", None
    unit = custom.repeating_unit(cycle)
    target_out = {
        "source": source, "plan": plan, "name": name, "cycle": list(cycle),
        # what the rotation itself breaks, year after year: not the path's doing, and not fixed by it
        "rules_broken": cand.rules_broken(custom.normal_form(cycle), is_irrigated, gentle=True, any_length=True),
    }

    run, shown = real_years(field.crop_history)
    first, last = futures.window(horizon)
    notes = [f"Every year is scored with the average weather of {first}–{last} in each of the "
             f"{len(engine.climate.models)} climate models (the future chosen for the plans), not with a forecast "
             "for that calendar year."]
    body = {
        "field_id": field.field_id, "engine": "smart", "settings": result.settings, "years": span,
        "target": target_out,
        "history": [{"year": year, "crop": crop} for year, crop in shown],
        "status": "no_history", "summary": "", "path": None, "blocked": [],
        "carry_on": None, "carry_on_note": None, "difference": None, "long_run": None,
        "sentences": None, "not_modelled": list(NOT_MODELLED), "notes": notes,
    }
    if not run:
        if shown:
            year, crop = shown[-1]
            body["summary"] = (f"The last year on record ({year}) is {crop}, which the planner has no numbers for, so "
                               "we cannot work out a path from it.")
        else:
            body["summary"] = "No crop history is on record for this field, so we cannot work out a path."
        return body

    history = [crop for _, crop in run]
    last_year, last_crop = run[-1]

    def crops_of(option_bridge: list, phase: int) -> list:
        return (list(option_bridge) + cycle_from(unit, phase, span))[:span]

    def value(crops: list) -> tuple:
        rows, profit = score_years(engine, crops, last_crop, is_irrigated, scenario, horizon, last_year + 1,
                                   [""] * len(crops), [None] * len(crops))
        return (round(float(profit.sum(axis=0).mean()), 6), -round(sum(row["bare_days"] for row in rows), 6))

    found = find_path(history, unit, is_irrigated, value, span)
    body["status"] = found["status"]
    body["blocked"] = rule_sentences(found["blocked"])

    # carry on as now: the current pattern continued. A field already in the target cycle carries on with it.
    if found["status"] == "already_there":
        pattern, repeating = tuple(cycle_from(unit, found["phase"], len(unit))), True
    else:
        pattern, repeating = carry_on_pattern(history)
    carry_rows = carry_profit = None
    if pattern is None:
        body["carry_on_note"] = ("Fewer than four years in a row of the six crops are on record, so there is no "
                                 "\"carry on as now\" to compare with.")
    else:
        crops = cycle_from(pattern, 0, span)
        carry_rows, carry_profit = score_years(
            engine, crops, last_crop, is_irrigated, scenario, horizon, last_year + 1, ["carry_on"] * span,
            [i % len(pattern) + 1 for i in range(span)])
        if not repeating:
            body["carry_on_note"] = (f"The last four years ({run[-cand.YEARS][0]}–{last_year}) are not a repeating "
                                     "pattern, so \"carry on as now\" repeats those four years as they are.")
        body["carry_on"] = {
            "name": custom.cycle_name(pattern) if len(pattern) > 1 else f"{rotation_name(list(pattern))} every year",
            "pattern": list(pattern), "repeating": repeating,
            "basis_years": [run[-cand.YEARS][0], last_year],
            "years": carry_rows, "total_return": _spread(carry_profit.sum(axis=0)),
        }
        body["long_run"] = long_run(engine, cycle, pattern, is_irrigated, scenario, horizon, field_numbers)
        notes.append(LONG_RUN_NOTE)

    if found["status"] == "no_path":
        body["summary"] = (f"We found no way from this field's last crops ({last_crop} in {last_year}) into this "
                           f"rotation within {MAX_BRIDGE_YEARS} bridge years that keeps to what our plans avoid.")
        return body

    bridge, phase = found["bridge"], found["phase"]
    crops = crops_of(bridge, phase)
    roles = ["bridge" if i < len(bridge) else "plan" for i in range(span)]
    cycle_years = [None if i < len(bridge) else (phase + i - len(bridge)) % len(unit) + 1 for i in range(span)]
    path_rows, path_profit = score_years(engine, crops, last_crop, is_irrigated, scenario, horizon, last_year + 1,
                                         roles, cycle_years)
    body["path"] = {
        "bridge_years": len(bridge), "already_there": found["status"] == "already_there",
        "entry": {"n": len(bridge) + 1, "year": last_year + len(bridge) + 1, "crop": unit[phase],
                  "cycle_year": phase + 1},
        "options": found["options"],
        "years": path_rows, "total_return": _spread(path_profit.sum(axis=0)),
    }
    body["summary"] = path_sentence(found, path_rows, unit, run, span)
    if found["status"] == "path" and found["options"] > 1:
        notes.append(f"Among the ways in with the fewest bridge years, the path shown is the one that returns the most "
                     f"over the {span} years.")
    # a new alfalfa stand starts on the way: say what its first year leaves out
    starts = any(row["crop"] == "alfalfa" and row["after"] != "alfalfa" for row in path_rows)
    if starts:
        body["not_modelled"].insert(0, ALFALFA_NOT_MODELLED)

    if carry_rows is not None:
        diff = difference(path_rows, path_profit, carry_rows, carry_profit, is_irrigated)
        diff["total_sentence"] = total_sentence(diff)
        body["difference"] = diff
        if found["status"] == "already_there":
            body["sentences"] = {"short_term": ALREADY_SHORT_TERM, "long_term": ALREADY_LONG_TERM}
        else:
            body["sentences"] = {"short_term": short_term_sentence(diff, path_rows),
                                 "long_term": long_term_sentence(body["long_run"], diff, is_irrigated,
                                                                 "the plan" if source == "plan" else "your rotation")}
        notes.append(ROUNDING_NOTE)
    if not is_irrigated:
        notes.append(DRYLAND_MONEY_NOTE)
    return body
