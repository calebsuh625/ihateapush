"""Phase 4, item 7: the tools the farm advisor's model may call.

Each tool runs one of the API's own endpoint functions in-process (the very function behind
GET /field/{id}/plans, /score, /transition, /twin, POST /farm/plan, GET /climate, /groundwater), so the
advisor can only say what the app itself would show. Nothing is computed here except:
  * a field's area in acres (hectares x the farm planner's own factor), and
  * a pumping limit from a share ("half as much" = 0.5 x the planner's own limit).

A tool's answer is the endpoint's answer cut down to what a model needs: the numbers with their units and
ranges and the caveat sentences; no outlines, chart series or repeats. `run(name, args, request, context)`
returns (result, one-line summary for the page). A tool never raises: what goes wrong comes back as
{"error": "<one plain sentence>"} for the model to read.

The tools that exist are listed in TOOLS; `declarations()` gives them in Gemini's function format.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request

from api.planner import config as planner_config
from api.planner import farm as farm_planner
from api.planner import smart, transition as transition_planner

TODAY = planner_config.TODAY
SCENARIO_WORDS = {"ssp245": "middle-road emissions", "ssp585": "high emissions"}
COMPARE_HORIZONS = (2040, 2060)
MAX_FARM_FIELDS_LISTED = 25
MAX_RESULT_CHARS = 14_000          # a tool answer larger than this is a bug in the trimming; it is cut with a note

PLAN_UNITS = {
    "scores": "0 to 1, higher is better. soil = soil protection (fewer bare days, lower wind-erosion risk); "
              "overall = the four weighted by the priorities",
    "holds_up_in": "in how many of the climate models the plan holds up",
    "bare_days": "days a year the soil sits bare",
    "erosion_index": "relative wind-erosion risk index, lower is better (not tons of soil)",
    "irrigation_in": "inches of water pumped on an acre a year",
    "water_met_share": "share of the crops' water need that is met",
    "return_per_acre": "dollars an acre a year, return over direct costs (not profit or income); low and "
                       "high are the lowest and highest climate model",
    "failed_share": "share of planted crops that fail (yield under half of normal)",
}
CLIMATE_UNITS = {
    "tmean_c": "growing-season mean temperature, change in degrees C",
    "hot_days": "change in growing-season days above 35 C (95 F)",
    "summer_rain_pct": "change in summer rain, percent",
    "summer_rain_agree": "how many of the climate models agree on the direction of the summer-rain change",
    "annual_rain_pct": "change in yearly rain, percent",
    "et0_pct": "change in the crops' water demand over the growing season, percent",
    "dry_spell_days": "change in the longest dry spell of the growing season, days",
    "range": "lowest and highest climate model for each change",
    "yield_change": "percent change in yield from the future weather alone, against today's model climate: "
                    "pct is the average, low and high the lowest and highest climate model",
}
CLIMATE_KEYS = ("tmean_c", "hot_days", "summer_rain_pct", "annual_rain_pct", "et0_pct", "dry_spell_days")
DRYLAND_MONEY_NOTE = (
    "Dryland: the dollar figures are small and their sign can flip with the price years and the cost of "
    "fallow. Lead with the water, bare-day and yield figures, not the dollars."
)
RAIN_NOTE = (
    "The climate models agree on heat, not on rain: only say rain goes down or up when summer_rain_agree "
    "is 5 or 6."
)
TWIN_NOTE = (
    "These are differences between look-alike neighbouring fields in days of bare soil, not proof that a "
    "rotation caused them, and not a measure of soil health."
)


class ToolError(Exception):
    """One plain sentence for the model: the tool could not answer."""


@dataclass
class Context:
    """What the farmer has on screen; a tool call that leaves a setting out gets it from here."""

    field_id: str | None = None
    farm_field_ids: list[str] = dataclass_field(default_factory=list)
    weights: dict[str, float] | None = None
    scenario: str | None = None
    horizon: int | None = None
    irrigated: dict[str, bool] = dataclass_field(default_factory=dict)      # field id -> the farmer's own answer
    limit_in: float | None = None

    def numbers(self) -> list[Any]:
        """What of this the advisor may repeat as a number (the year looked at, the pumping what-if)."""
        return [self.horizon, self.limit_in, self.weights, len(self.farm_field_ids) or None]


# --------------------------------------------------------------------------------------
# Reading arguments
# --------------------------------------------------------------------------------------


def _number(args: dict, name: str, words: str) -> float | None:
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ToolError(f"{words} must be a number.")
    return float(value)


def _whole(args: dict, name: str, words: str) -> int | None:
    value = _number(args, name, words)
    if value is None:
        return None
    if value != int(value):
        raise ToolError(f"{words} must be a whole number.")
    return int(value)


def _flag(args: dict, name: str, words: str) -> bool | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ToolError(f"{words} must be true or false.")
    return value


def _text(args: dict, name: str, words: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{words} must be text.")
    return value.strip()


def _field_id(args: dict, context: Context) -> str:
    field_id = _text(args, "field_id", "The field id") or context.field_id
    if not field_id:
        raise ToolError("No field is selected. Ask the farmer to click a field on the map or to give its id "
                        "(like KS-014376).")
    return field_id


def _weights(args: dict, context: Context) -> dict[str, float | None]:
    given = args.get("weights")
    if given is None:
        given = context.weights or {}
    if not isinstance(given, dict):
        raise ToolError("weights must be an object with any of soil, water, money and risk.")
    out: dict[str, float | None] = {}
    for name in planner_config.SCORE_NAMES:
        out[name] = _number(given, name, f"The {name} priority")
    return out


def _settings(args: dict, context: Context, field_id: str | None) -> dict[str, Any]:
    """The planner settings of one call: the model's own where it gave them, else what is on screen."""
    scenario = _text(args, "scenario", "The scenario") or context.scenario or planner_config.DEFAULT_SCENARIO
    horizon = _whole(args, "horizon", "The year")
    if horizon is None:
        horizon = context.horizon if context.horizon is not None else TODAY
    irrigated = _flag(args, "irrigated", "irrigated")
    if irrigated is None and field_id is not None:
        irrigated = context.irrigated.get(field_id)
    return {"weights": _weights(args, context), "scenario": scenario, "horizon": horizon, "irrigated": irrigated,
            "limit_in": _limit(args, context)}


def _limit(args: dict, context: Context) -> float | None:
    """The pumping what-if in inches: `limit_in`, or `limit_share` of the planner's own limit ("half as
    much" = 0.5), or what is on screen."""
    limit = _number(args, "limit_in", "The pumping limit")
    share = _number(args, "limit_share", "The share of the pumping limit")
    if limit is not None and share is not None:
        raise ToolError("Give either limit_in or limit_share, not both.")
    if share is not None:
        if not 0 < share <= 1:
            raise ToolError("limit_share must be above 0 and at most 1 (0.5 means half as much).")
        try:
            top = smart.get_engine().what_if_range_in()[1]
        except Exception:
            raise ToolError("The pumping what-if needs the future-weather tables, which are not set up.") from None
        return round(share * top, 1)
    return limit if limit is not None else context.limit_in


# --------------------------------------------------------------------------------------
# Calling the API's own functions
# --------------------------------------------------------------------------------------


def _main():
    from api import main          # here, not at the top: api.main imports the advisor's router

    return main


async def _answer(call: Awaitable) -> dict:
    """Await one endpoint function and read its JSON body; its plain error sentence becomes a ToolError."""
    try:
        response = await call
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from None
    return json.loads(response.body)


class _BodyRequest:
    """POST /farm/plan reads its settings from the request body: the same request with our body."""

    def __init__(self, request: Request, body: dict):
        self.app = request.app
        self._body = body

    async def json(self) -> dict:
        return self._body


async def _plans(request: Request, field_id: str, s: dict) -> dict:
    w = s["weights"]
    return await _answer(_main().plans(
        request, field_id, soil=w["soil"], water=w["water"], money=w["money"], risk=w["risk"],
        scenario=s["scenario"], horizon=s["horizon"], irrigated=s["irrigated"], limit_in=s["limit_in"]))


async def _score(request: Request, field_id: str, rotation: str, s: dict) -> dict:
    w = s["weights"]
    return await _answer(_main().score(
        request, field_id, rotation=rotation, soil=w["soil"], water=w["water"], money=w["money"], risk=w["risk"],
        scenario=s["scenario"], horizon=s["horizon"], irrigated=s["irrigated"], limit_in=s["limit_in"]))


# --------------------------------------------------------------------------------------
# Cutting answers down
# --------------------------------------------------------------------------------------


def _scenario_words(scenario: str | None) -> str | None:
    return SCENARIO_WORDS.get(scenario or "", scenario)


def _settings_out(settings: dict | None) -> dict:
    """The settings an answer was worked out under, for the model to name."""
    s = settings or {}
    out = {
        "priorities": s.get("weights"),
        "scenario": _scenario_words(s.get("scenario")),
        "year": s.get("horizon"),
        "climate_years_averaged": s.get("window"),
        "pumping_limit_in": s.get("irrigation_limit_in"),
    }
    if "irrigated" in s:
        out["irrigated"] = s.get("irrigated")
        out["irrigation_basis"] = (
            "the farmer's own answer" if s.get("irrigation_source") == "user"
            else f"a guess from the field's record: {s.get('irrigation_basis')}" if s.get("irrigation_basis")
            else s.get("irrigation_source"))
    return {key: value for key, value in out.items() if value is not None}


def _plan(plan: dict | None) -> dict | None:
    """One plan card: name, cycle, scores, the figures with their range, the sentence."""
    if not plan:
        return None
    numbers = plan.get("numbers") or {}
    scores = plan.get("scores") or {}
    spread = (plan.get("spread") or {}).get("profit_per_acre") or {}
    futures = plan.get("futures") or {}
    out: dict[str, Any] = {
        "name": plan.get("name"),
        "cycle": plan.get("cycle") or plan.get("rotation"),
        "rank": plan.get("rank"),
        "scores": {key: scores.get(key) for key in ("soil", "water", "money", "risk", "overall") if key in scores},
        "sentence": plan.get("reason"),
    }
    if futures:
        out["holds_up_in"] = f"{futures.get('holds')} of {futures.get('total')} climate models"
    for key in ("bare_days", "erosion_index", "irrigation_in", "water_met_share", "failed_share"):
        if numbers.get(key) is not None:
            out[key] = numbers[key]
    if numbers.get("profit_per_acre") is not None:
        out["return_per_acre"] = {"value": numbers["profit_per_acre"], "low": spread.get("low"),
                                  "high": spread.get("high")}
    if plan.get("rules_broken"):
        out["rules_broken"] = plan["rules_broken"]
    return {key: value for key, value in out.items() if value is not None}


def _climate(block: dict | None) -> dict | None:
    """A future's weather: the label, the main changes with the lowest and highest model, yield changes."""
    if not block:
        return None
    changes, spread = block.get("changes") or {}, block.get("range") or {}
    out: dict[str, Any] = {"label": block.get("label")}
    if any(changes.get(key) for key in CLIMATE_KEYS):
        out["changes"] = {key: {"value": changes.get(key), "range": spread.get(key)}
                          for key in CLIMATE_KEYS if key in changes}
        out["summer_rain_agree"] = f"{changes.get('summer_rain_agree')} of {changes.get('models')} climate models"
    if block.get("yield_change") and any(v.get("pct") for v in block["yield_change"].values()):
        out["yield_change"] = block["yield_change"]
    return out


def _water_short(outlook: dict | None) -> dict | None:
    """The part of `water_outlook` that belongs next to the plans: the what-if and one line per plan."""
    if not outlook:
        return None
    what_if = outlook.get("what_if") or {}
    return {
        "nearby_wells": outlook.get("sentence") or outlook.get("note"),
        "plan_lines": outlook.get("plan_lines"),
        "pumping_what_if": {"limit_in": what_if.get("limit_in"), "planner_limit_in": what_if.get("default_in"),
                            "on": what_if.get("active"), "note": what_if.get("note")},
    }


def _notes(settings: dict | None, *keys: str) -> list[str]:
    s = settings or {}
    return [s[key] for key in keys if s.get(key)]


def _plans_out(body: dict) -> dict:
    """GET /field/{id}/plans, cut down."""
    if body.get("engine") != "smart":
        return {
            "field_id": body.get("field_id"),
            "planner": "basic",
            "note": "The future-weather tables are not set up on this server, so these picks come from the "
                    "basic planner: no climate futures, no dollar or water figures.",
            "plans": [{"name": p.get("name"), "rotation": p.get("rotation"), "scores": p.get("scores"),
                       "sentence": p.get("reason")} for p in body.get("plans", [])],
        }
    settings = body.get("settings") or {}
    out: dict[str, Any] = {
        "field_id": body.get("field_id"),
        "settings": _settings_out(settings),
        "climate": _climate(body.get("climate")),
        "plans": [_plan(plan) for plan in body.get("plans", [])],
        "current_pattern": _plan(body.get("current")),
        "current_pattern_note": body.get("current_note"),
        "rotations_compared": body.get("candidates"),
        "water": _water_short(body.get("water_outlook")),
        "units": PLAN_UNITS,
        "notes": _notes(settings, "holds_up_rule", "ranking_rule", "profit_basis", "yield_change_note")
                 + list(settings.get("limits") or []) + [RAIN_NOTE],
    }
    if settings.get("irrigated") is False:
        out["notes"].append(DRYLAND_MONEY_NOTE)
    return {key: value for key, value in out.items() if value is not None}


def _plan_names(plans: list) -> str:
    return "; ".join(f"{i}. {p.get('name')}" for i, p in enumerate(plans, start=1)) or "no plans"


def _settings_words(settings: dict | None) -> str:
    """"irrigated, 2040 under high emissions, pumping limit 12 in" for a summary line."""
    s = settings or {}
    parts = []
    if s.get("irrigated") is not None:
        parts.append("irrigated" if s["irrigated"] else "dryland")
    if s.get("horizon") is not None:
        parts.append("today's climate" if s["horizon"] == TODAY
                     else f"{s['horizon']} under {_scenario_words(s.get('scenario'))}")
    return ", ".join(parts)


# --------------------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------------------


async def get_field(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    field_id = _field_id(args, context)
    body = await _answer(_main().field(request, field_id))
    area = body.get("area_ha")
    out = {
        "field_id": body["field_id"],
        "area_acres": round(area * farm_planner.ACRES_PER_HA, 1) if area is not None else None,
        "area_ha": area,
        "crop_history": body.get("crop_history"),
        "crop_history_source": "USDA Cropland Data Layer, one crop a year as seen from satellite",
        "organic_matter_pct": body.get("organic_matter_pct"),
        "organic_matter_note": "From the soil survey map (SSURGO): a mapped value, not a measurement of how this "
                               "field's soil has changed.",
        "slope_pct": body.get("slope_pct"),
        "stats": body.get("stats"),
    }
    years = list((body.get("crop_history") or {}).keys())
    span = f", crops {years[0]}-{years[-1]}" if years else ""
    return out, f"Looked up field {field_id}{span}"


async def get_plans(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    field_id = _field_id(args, context)
    settings = _settings(args, context, field_id)
    body = await _plans(request, field_id, settings)
    out = _plans_out(body)
    what_if = ((body.get("water_outlook") or {}).get("what_if") or {})
    limit_words = ""
    if what_if.get("active"):
        # the same plans without the what-if, so the model can say what the lower limit changed
        usual = await _plans(request, field_id, {**settings, "limit_in": None})
        out["without_the_what_if"] = {
            "pumping_limit_in": (usual.get("settings") or {}).get("irrigation_limit_in"),
            "plans": [_plan(plan) for plan in usual.get("plans", [])],
            "current_pattern": _plan(usual.get("current")),
        }
        limit_words = f", pumping limit {what_if.get('limit_in'):g} in instead of {what_if.get('default_in'):g}"
    elif settings["limit_in"] is not None and (body.get("settings") or {}).get("irrigated") is False:
        out["pumping_what_if_note"] = ("This field is treated as dryland, so a pumping limit changes nothing "
                                       "for it.")
    words = _settings_words(body.get("settings"))
    return out, f"Plans for {field_id} ({words}{limit_words}): {_plan_names(body.get('plans', []))}"


async def score_rotation(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    field_id = _field_id(args, context)
    rotation = _text(args, "rotation", "The rotation")
    if rotation is None:
        raise ToolError("Give the rotation to score: 2 to 6 crops, one a year, like \"corn, wheat, fallow\".")
    settings = _settings(args, context, field_id)
    body = await _score(request, field_id, rotation, settings)
    comparison = body.get("comparison") or {}
    rows = [{key: row.get(key) for key in ("label", "unit", "yours", "ours", "diff", "verdict", "models",
                                           "sentence", "yours_range", "ours_range") if row.get(key) is not None}
            for row in comparison.get("rows", [])]
    yours = body.get("yours") or {}
    out = {
        "field_id": body.get("field_id"),
        "settings": _settings_out(body.get("settings")),
        "rotation_scored": body.get("understood"),
        "yours": {**(_plan(yours) or {}), "rank_among": yours.get("rank_of"),
                  "one_of_our_candidates": yours.get("is_candidate")},
        "our_picks": [_plan(plan) for plan in body.get("plans", [])],
        "compared_with_our_first_pick": {
            "our_first_pick": comparison.get("against"),
            "ahead_overall": comparison.get("verdict"),
            "models": comparison.get("models"),
            "headline": comparison.get("headline"),
            "rows": rows,
            "diff_note": "diff is yours minus ours, in the row's unit; models counts in how many climate "
                         "models each is ahead",
            "priority_sentences": comparison.get("priority_sentences"),
            "notes": comparison.get("notes"),
        },
        "units": PLAN_UNITS,
        "notes": _notes(body.get("settings"), "profit_basis"),
    }
    if (body.get("settings") or {}).get("irrigated") is False:
        out["notes"].append(DRYLAND_MONEY_NOTE)
    rank = f"ranks {yours.get('rank')} of {yours.get('rank_of')}" if yours.get("rank") else "outside our candidates"
    return out, (f"Scored {' - '.join(body.get('understood') or [])} on {field_id} "
                 f"({_settings_words(body.get('settings'))}): {rank}")


def _path_year(year: dict) -> dict:
    keep = ("n", "year", "crop", "role", "return", "yield", "yield_unit", "irrigation_in", "bare_days", "fail_chance")
    return {key: year[key] for key in keep if year.get(key) is not None}


async def transition_path(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    field_id = _field_id(args, context)
    settings = _settings(args, context, field_id)
    target = _text(args, "target", "The target rotation")
    plan = _whole(args, "plan", "The plan number")
    years = _whole(args, "years", "The number of years")
    w = settings["weights"]
    body = await _answer(_main().transition_path(
        request, field_id, target=target, plan=plan,
        years=years if years is not None else transition_planner.DEFAULT_SPAN,
        soil=w["soil"], water=w["water"], money=w["money"], risk=w["risk"], scenario=settings["scenario"],
        horizon=settings["horizon"], irrigated=settings["irrigated"], limit_in=settings["limit_in"]))
    path, carry, diff = body.get("path") or {}, body.get("carry_on") or {}, body.get("difference") or {}
    out = {
        "field_id": body.get("field_id"),
        "settings": _settings_out(body.get("settings")),
        "target": body.get("target"),
        "last_crops": body.get("history"),
        "status": body.get("status"),
        "summary": body.get("summary"),
        "path": {
            "bridge_years": path.get("bridge_years"),
            "starts": path.get("entry"),
            "years": [_path_year(year) for year in path.get("years", [])],
            "total_return": path.get("total_return"),
        } if path else None,
        "blocked": body.get("blocked") or None,
        "carrying_on_as_now": {"name": carry.get("name"), "total_return": carry.get("total_return")} if carry else None,
        "carrying_on_note": body.get("carry_on_note"),
        "path_minus_carrying_on": {"payback": diff.get("payback"), "total": diff.get("total"),
                                   "sentence": diff.get("total_sentence")} if diff else None,
        "long_run": body.get("long_run"),
        "sentences": body.get("sentences"),
        "units": {
            "return": "dollars an acre, return over direct costs (not profit): mid is the average, low and high "
                      "the lowest and highest climate model; totals are sums over the years of the path",
            "irrigation_in": "inches pumped on an acre that year",
            "bare_days": "days the soil sits bare that year",
            "fail_chance": "chance the yield is under half of normal",
            "role": "bridge = a year on the way in, plan = a year of the rotation itself",
        },
        "not_modelled": body.get("not_modelled"),
        "notes": body.get("notes"),
    }
    out = {key: value for key, value in out.items() if value is not None}
    name = (body.get("target") or {}).get("name") or "the rotation"
    return out, f"Path to {name} on {field_id} over {body.get('years')} years: {body.get('status')}"


def _farm_field(row: dict) -> dict:
    plan = row.get("plan") or {}
    numbers = plan.get("numbers") or {}
    out = {
        "field_id": row.get("field_id"),
        "area_acres": row.get("area_acres"),
        "irrigated": row.get("irrigated"),
        "rotation": plan.get("name"),
        "starts_in_cycle_year": plan.get("start_year"),
        "irrigation_in": numbers.get("irrigation_in"),
        "return_per_acre": numbers.get("profit_per_acre"),
        "bare_days": numbers.get("bare_days"),
        "its_own_first_pick": not row.get("differs"),
        "gives_up": row.get("gives_up"),
        "sentence": row.get("sentence"),
    }
    return {key: value for key, value in out.items() if value is not None}


def _farm_limit(block: dict | None) -> dict | None:
    if not block:
        return None
    keep = ("set", "limit", "unit", "met", "binds", "sentence", "binds_sentence", "tightest", "achievable")
    return {key: block[key] for key in keep if block.get(key) is not None}


async def farm_plan(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    ids = args.get("field_ids")
    if ids is None:
        ids = list(context.farm_field_ids)
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise ToolError("field_ids must be a list of field ids.")
    if not ids:
        raise ToolError("The farmer has not put any fields in their farm yet. Ask them to add fields to the farm "
                        "list (or to name the field ids).")
    weights = {name: value for name, value in _weights(args, context).items() if value is not None}
    scenario = _text(args, "scenario", "The scenario") or context.scenario or planner_config.DEFAULT_SCENARIO
    horizon = _whole(args, "horizon", "The year")
    if horizon is None:
        horizon = context.horizon if context.horizon is not None else TODAY
    asked: dict[str, Any] = {
        "field_ids": ids, "weights": weights, "scenario": scenario, "horizon": horizon,
        "irrigated": {i: v for i, v in context.irrigated.items() if i in ids},
    }
    for name, words in (("water_budget_acre_in", "The water budget"), ("min_income", "The minimum yearly return")):
        value = _number(args, name, words)
        if value is not None:
            asked[name] = value
    limit = _limit(args, context)
    if limit is not None:
        asked["limit_in"] = limit
    body = await _answer(_main().farm_plan(_BodyRequest(request, asked)))
    plan, limits, s = body.get("plan") or {}, body.get("limits") or {}, body.get("settings") or {}
    fields = [_farm_field(row) for row in body.get("fields", [])]
    out = {
        "settings": {**_settings_out(s), "years_planned": s.get("span_years"),
                     "water_budget_acre_in": s.get("water_budget_acre_in"), "min_yearly_return": s.get("min_income")},
        "farm": body.get("farm"),
        "status": body.get("status"),
        "limits_met": body.get("limits_met"),
        "summary": body.get("summary"),
        "limits": {"water": _farm_limit(limits.get("water")), "return": _farm_limit(limits.get("return"))},
        "whole_farm": {
            "average_year": plan.get("average"), "heaviest_pumping_year": plan.get("peak_water"),
            "leanest_year": plan.get("worst_return"), "fields_with_another_rotation": plan.get("changed_fields"),
            "fields_with_another_start_year": plan.get("shifted_fields"),
        },
        "each_field_planned_alone": {"average_year": (body.get("alone") or {}).get("average")},
        "fields": fields[:MAX_FARM_FIELDS_LISTED],
        "fields_not_listed": max(0, len(fields) - MAX_FARM_FIELDS_LISTED) or None,
        "units": {
            "water_acre_in": "acre-inches pumped by the whole farm in a year",
            "return": "dollars a year for the whole farm, return over direct costs (not profit)",
            "return_per_acre": "dollars an acre a year, return over direct costs",
            "n": "year of the plan (1 = its first year), not a calendar year",
            "gives_up": "what the field's rotation here differs by from its own first pick",
        },
        "notes": body.get("notes"),
        "not_modelled": body.get("not_modelled"),
    }
    out = {key: value for key, value in out.items() if value is not None}
    farm = body.get("farm") or {}
    asked_limits = [f"water budget {asked['water_budget_acre_in']:g} acre-in" if "water_budget_acre_in" in asked else "",
                    f"at least ${asked['min_income']:,.0f} a year" if "min_income" in asked else ""]
    limit_words = ", ".join(word for word in asked_limits if word) or "no farm limits"
    return out, f"Farm plan for {farm.get('fields')} fields ({limit_words}): {body.get('status')}"


async def compare_futures(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    """Not an endpoint of its own: the plans (or the score of one rotation) asked for five times, today and
    in 2040 and 2060 under both scenarios, through the same functions as get_plans and score_rotation."""
    field_id = _field_id(args, context)
    rotation = _text(args, "rotation", "The rotation")
    base = _settings({key: value for key, value in args.items() if key not in ("scenario", "horizon")},
                     context, field_id)
    futures = [(TODAY, base["scenario"] if base["scenario"] in planner_config.SCENARIOS
                else planner_config.DEFAULT_SCENARIO)]
    futures += [(year, scenario) for year in COMPARE_HORIZONS for scenario in planner_config.SCENARIOS]
    rows, settings, understood = [], None, None
    for year, scenario in futures:
        s = {**base, "scenario": scenario, "horizon": year}
        if rotation:
            body = await _score(request, field_id, rotation, s)
            understood = body.get("understood")
            picked = {"rotation": _plan(body.get("yours")),
                      "our_first_pick": _plan((body.get("plans") or [None])[0])}
        else:
            body = await _plans(request, field_id, s)
            if body.get("engine") != "smart":
                raise ToolError("Comparing futures needs the future-weather tables, which are not set up on "
                                "this server.")
            picked = {"our_first_pick": _plan((body.get("plans") or [None])[0]),
                      "current_pattern": _plan(body.get("current"))}
        settings = settings or body.get("settings")
        rows.append({
            "when": "today" if year == TODAY else str(year),
            "scenario": None if year == TODAY else _scenario_words(scenario),
            "climate": _climate(body.get("climate")),
            **{key: value for key, value in picked.items() if value is not None},
        })
    out = {
        "field_id": field_id,
        "settings": {key: value for key, value in _settings_out(settings).items()
                     if key not in ("scenario", "year", "climate_years_averaged")},
        "rotation_scored": understood,
        "futures": rows,
        "units": {**PLAN_UNITS, **CLIMATE_UNITS},
        "notes": [
            "\"Today\" is the model climate of the years around 2025 and is the same under both scenarios.",
            "Each future is the average weather of about twenty years around that year in each climate model.",
            RAIN_NOTE,
        ] + _notes(settings, "yield_change_note", "profit_basis"),
    }
    if (settings or {}).get("irrigated") is False:
        out["notes"].append(DRYLAND_MONEY_NOTE)
    out = {key: value for key, value in out.items() if value is not None}
    what = " - ".join(understood) if understood else "our first pick and the current pattern"
    return out, (f"Compared {what} on {field_id} today, in {COMPARE_HORIZONS[0]} and in {COMPARE_HORIZONS[1]} "
                 f"under both scenarios")


async def get_climate(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    body = await _answer(_main().climate())
    if not body.get("available"):
        raise ToolError("The future-weather tables are not set up on this server.")
    asked = _whole(args, "horizon", "The year")
    scenario = _text(args, "scenario", "The scenario")
    if scenario is not None and scenario not in body.get("scenarios", {}):
        raise ToolError("The scenario must be ssp245 (middle road) or ssp585 (high emissions).")
    years = list(body.get("horizons") or [])
    if asked is not None and asked not in years:
        raise ToolError(f"The year must be one of {', '.join(str(y) for y in years)}.")
    wanted = [asked] if asked is not None else sorted(
        {y for y in (*COMPARE_HORIZONS, context.horizon) if y in years and y != TODAY})
    futures = []
    for name in ([scenario] if scenario else list(body["scenarios"])):
        for row in body["scenarios"][name]:
            if row.get("horizon") in wanted:
                futures.append({"scenario": _scenario_words(name), "year": row["horizon"],
                                "years_averaged": row.get("window"), **(_climate(row) or {})})
    baseline = body.get("baseline") or {}
    values = baseline.get("values") or {}
    out = {
        "source": body.get("source"),
        "climate_models": len(body.get("models") or []),
        "today": {
            "label": baseline.get("label"),
            "years_averaged": baseline.get("window"),
            "growing_season_mean_temperature_c": values.get("tmean_c"),
            "growing_season_days_above_35c": values.get("hot_days"),
            "summer_rain_mm": values.get("summer_rain_mm"),
            "yearly_rain_mm": values.get("annual_rain_mm"),
        },
        "futures": futures,
        "units": {key: CLIMATE_UNITS[key] for key in (*CLIMATE_KEYS, "summer_rain_agree", "range")},
        "notes": ["Changes are each climate model against its own today.", RAIN_NOTE,
                  body.get("window_rule")],
    }
    years_words = ", ".join(str(y) for y in wanted) or "today"
    which = _scenario_words(scenario) if scenario else "both scenarios"
    return out, f"Future weather for the county: {years_words} under {which}"


async def get_twin(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    from api.twin import twin as twin_endpoint

    field_id = _field_id(args, context)
    body = await _answer(twin_endpoint(request, field_id))
    this = body.get("this") or {}
    out: dict[str, Any] = {
        "field_id": field_id,
        "what_is_measured": (body.get("outcome") or {}).get("label"),
        "measure_note": (body.get("outcome") or {}).get("note"),
        "this_field": {"rotation_type_before": this.get("rotation_pre"), "rotation_type_after": this.get("rotation_post"),
                       "irrigation": this.get("irrigation")},
        "has_twin": body.get("has_twin"),
        "note": TWIN_NOTE,
    }
    if body.get("is_fake"):
        out["made_up"] = "The soil numbers here are made up (fake soil table): not findings."
    if not body.get("has_twin"):
        out["why_no_twin"] = body.get("reason")
        return out, f"Twin for {field_id}: no look-alike neighbour found"
    twin, match = body.get("twin") or {}, body.get("match") or {}
    measured, comparison = body.get("measured") or {}, body.get("comparison") or {}
    out["twin"] = {"field_id": twin.get("field_id"), "rotation_type_before": twin.get("rotation_pre"),
                   "rotation_type_after": twin.get("rotation_post"), "irrigation": twin.get("irrigation")}
    out["match"] = {"distance_km": match.get("distance_km"), "sentence": match.get("sentence")}
    out["this_pair"] = {
        "years_before": measured.get("pre_years"), "years_after": measured.get("post_years"),
        "this_field_bare_days": measured.get("this"), "twin_bare_days": measured.get("twin"),
        "difference_in_change": measured.get("difference_in_change"),
        "expected_from_the_crops_alone": measured.get("from_crops"),
        "sentence": measured.get("sentence"),
    }
    out["all_pairs_of_this_kind"] = {
        "comparison": comparison.get("name"),
        "pairs": comparison.get("n_pairs"),
        "average_difference": comparison.get("estimate"),
        "expected_from_the_crops_alone": comparison.get("from_crops"),
        "sentence": comparison.get("sentence"),
        "agreement": (comparison.get("agreement") or {}).get("sentence"),
        "fake_switch_test_passed": (comparison.get("placebo") or {}).get("passed"),
        "fake_switch_test": (comparison.get("placebo") or {}).get("sentence"),
    }
    out["units"] = {
        "bare_days": "days a year the soil sits bare; before, after and change",
        "difference_in_change": "this field's change minus its twin's, days a year",
        "average_difference": "days a year, with its 95% interval (ci_low to ci_high); this field's rotation type "
                              "minus the twin's",
    }
    return out, f"Twin for {field_id}: {twin.get('field_id')}, {match.get('distance_km')} km away"


def _county_water(body: dict) -> dict:
    return {
        "county": body.get("county"),
        "sentence": body.get("sentence"),
        "if_the_rate_continued": body.get("extrapolation"),
        "caveat": body.get("caveat"),
        "no_threshold": body.get("threshold_note"),
        "source": body.get("source"),
    }


async def water_outlook(args: dict, request: Request, context: Context) -> tuple[dict, str]:
    from api.groundwater import groundwater as groundwater_endpoint

    county = json.loads(groundwater_endpoint(request).body)
    if not county.get("available"):
        raise ToolError("The measured groundwater levels are not set up on this server.")
    field_id = _text(args, "field_id", "The field id") or context.field_id
    if not field_id:
        return {"county": _county_water(county)}, "Groundwater levels for the county (no field selected)"
    settings = _settings(args, context, field_id)
    body = await _plans(request, field_id, settings)
    outlook = body.get("water_outlook")
    if not outlook:
        why = ("This field is treated as dryland, so no water outlook is worked out for it; the county picture "
               "is given instead." if (body.get("settings") or {}).get("irrigated") is False
               else "No water outlook could be worked out for this field; the county picture is given instead.")
        return {"field_id": field_id, "field_outlook": None, "why": why, "county": _county_water(county)}, (
            f"Groundwater for {field_id}: no field outlook, county picture only")
    if not outlook.get("available"):
        return {"field_id": field_id, "field_outlook": None,
                "why": outlook.get("note") or "Too few wells with a long record are near this field.",
                "county": _county_water(county)}, f"Groundwater for {field_id}: too few wells nearby, county picture only"
    column = outlook.get("column") or {}
    out = {
        "field_id": field_id,
        "wells_used": outlook.get("wells_used"),
        "distance_mi": outlook.get("distance_mi"),
        "winters_measured": outlook.get("winters"),
        "decline_ft_per_year": outlook.get("decline_ft_per_year"),
        "wells_falling": outlook.get("falling"),
        "wells_rising": outlook.get("rising"),
        "depth_to_water_ft": outlook.get("depth_to_water_ft"),
        "water_column_ft": {"wells_with_a_depth_on_record": column.get("wells"), "today": column.get("now_ft"),
                            "if_the_rate_continued": [{"year": y.get("year"), "left_ft": y.get("left_ft")}
                                                      for y in column.get("years", [])]} if column else None,
        "sentence": outlook.get("sentence"),
        "if_the_rate_continued": outlook.get("extrapolation"),
        "caveat": outlook.get("caveat"),
        "water_column_note": outlook.get("column_note"),
        "no_threshold": outlook.get("threshold_note"),
        "plans": [{"name": plan.get("name"), "line": line}
                  for plan, line in zip(body.get("plans", []), outlook.get("plan_lines") or [])],
        "pumping_what_if": (_water_short(outlook) or {}).get("pumping_what_if"),
        "units": {
            "decline_ft_per_year": "feet a year the water level in the wells has dropped: the middle well, with "
                                   "the lowest and highest of the wells used",
            "depth_to_water_ft": "feet from the surface down to water, winter level",
            "water_column_ft": "feet of water standing in the well (well depth minus depth to water)",
        },
        "rule": outlook.get("rule"),
        "source": outlook.get("source"),
    }
    out = {key: value for key, value in out.items() if value is not None}
    return out, f"Groundwater near {field_id}: {outlook.get('wells_used')} wells with a long record"


# --------------------------------------------------------------------------------------
# What the model is told about the tools
# --------------------------------------------------------------------------------------

_FIELD = {"type": "STRING", "description": "Field id like KS-014376. Leave out to use the field the farmer has open."}
_SETTINGS = {
    "weights": {
        "type": "OBJECT",
        "description": "The farmer's priorities, any of soil, water, money, risk (zero or more; only their "
                       "ratio matters). Leave out to use the ones on screen.",
        "properties": {name: {"type": "NUMBER"} for name in planner_config.SCORE_NAMES},
    },
    "scenario": {"type": "STRING", "enum": list(planner_config.SCENARIOS),
                 "description": "ssp245 = middle-road emissions, ssp585 = high emissions. Leave out to use the one on screen."},
    "horizon": {"type": "INTEGER",
                "description": f"The year whose climate to use, {planner_config.HORIZON_MIN} (today) to "
                               f"{planner_config.HORIZON_MAX}. Leave out to use the one on screen."},
    "irrigated": {"type": "BOOLEAN",
                  "description": "Treat the field as irrigated (true) or dryland (false). Leave out unless the "
                                 "farmer says which it is."},
    "limit_in": {"type": "NUMBER",
                 "description": "What-if: the most inches of water that can be pumped on an acre in a year, when "
                                "the farmer names a number of inches."},
    "limit_share": {"type": "NUMBER",
                    "description": "What-if: the pumping limit as a share of the planner's own limit, above 0 up "
                                   "to 1. Use 0.5 for \"half as much water\". Do not give both this and limit_in."},
}


def _declare(name: str, description: str, properties: dict, required: tuple = ()) -> dict:
    out: dict[str, Any] = {"name": name, "description": description}
    if properties:
        out["parameters"] = {"type": "OBJECT", "properties": properties}
        if required:
            out["parameters"]["required"] = list(required)
    return out


Tool = Callable[[dict, Request, Context], Awaitable[tuple[dict, str]]]

TOOLS: dict[str, tuple[Tool, dict]] = {
    "get_field": (get_field, _declare(
        "get_field",
        "One field's record: size, the crop grown each year since 2008, mapped soil organic matter and slope.",
        {"field_id": _FIELD})),
    "get_plans": (get_plans, _declare(
        "get_plans",
        "Our top 3 rotation plans for a field and the field's current pattern, each with its scores, bare days, "
        "water pumped, return over direct costs (with the range across the climate models) and in how many "
        "climate models it holds up. Also says whether the field is treated as irrigated or dryland. With "
        "limit_in or limit_share it scores the plans again under a lower pumping limit and also returns the "
        "plans without that limit.",
        {"field_id": _FIELD, **_SETTINGS})),
    "score_rotation": (score_rotation, _declare(
        "score_rotation",
        "Score a rotation the farmer names on a field and compare it with our picks, row by row (soil "
        "protection, water, money, risk). Use it for \"why not corn?\" or \"what about wheat-fallow?\".",
        {"field_id": _FIELD,
         "rotation": {"type": "STRING",
                      "description": "2 to 6 crops, one a year, separated by commas: corn, soybeans, winter wheat, "
                                     "sorghum (milo), alfalfa, fallow. For one crop every year repeat it: \"corn, corn\"."},
         **_SETTINGS}, required=("rotation",))),
    "transition_path": (transition_path, _declare(
        "transition_path",
        "The year-by-year way from what the field grows now into a rotation: bridge years, each year's crop, "
        "return and water, and how it compares with carrying on as now (payback).",
        {"field_id": _FIELD,
         "target": {"type": "STRING", "description": "The rotation to get to, as in score_rotation. Leave out to use one of our picks."},
         "plan": {"type": "INTEGER", "description": "1, 2 or 3: which of our picks to get to (default 1). Not together with target."},
         "years": {"type": "INTEGER",
                   "description": f"How many years to lay out, {transition_planner.MIN_SPAN} to "
                                  f"{transition_planner.MAX_SPAN} (default {transition_planner.DEFAULT_SPAN})."},
         **_SETTINGS})),
    "farm_plan": (farm_plan, _declare(
        "farm_plan",
        "Plan several fields together as one farm, optionally under a yearly water budget for the whole farm "
        "and a least yearly return: which rotation each field gets, the farm's pumping and return, and whether "
        "the limits can be met.",
        {"field_ids": {"type": "ARRAY", "items": {"type": "STRING"},
                       "description": "The farm's field ids. Leave out to use the farmer's farm list."},
         "water_budget_acre_in": {"type": "NUMBER", "description": "The most the whole farm may pump in any year, in acre-inches."},
         "min_income": {"type": "NUMBER", "description": "The least the whole farm must return over direct costs in every year, in dollars."},
         **{key: value for key, value in _SETTINGS.items() if key != "irrigated"}})),
    "compare_futures": (compare_futures, _declare(
        "compare_futures",
        "The same field today, in 2040 and in 2060, under both the middle-road and the high-emissions "
        "scenario: how the weather changes, and how our first pick and the field's current pattern (or one "
        "rotation you name) score in each.",
        {"field_id": _FIELD,
         "rotation": {"type": "STRING", "description": "Optional: one rotation to follow through the futures, as in score_rotation."},
         **{key: value for key, value in _SETTINGS.items() if key not in ("scenario", "horizon")}})),
    "get_climate": (get_climate, _declare(
        "get_climate",
        "Future weather for the county from the six climate models: change in temperature, hot days, rain and "
        "crop water demand, each with the lowest and highest model, and how many models agree on rain.",
        {"scenario": {"type": "STRING", "enum": list(planner_config.SCENARIOS),
                      "description": "Leave out for both scenarios."},
         "horizon": {"type": "INTEGER", "description": "One year, a multiple of 5 from 2030 to 2060. Leave out for 2040 and 2060."}})),
    "get_twin": (get_twin, _declare(
        "get_twin",
        "The field next to its best-matched look-alike neighbour that was grown differently: days of bare soil "
        "before and after in both, and the average over all such pairs with its 95% interval. A comparison, "
        "not proof of cause.",
        {"field_id": _FIELD})),
    "water_outlook": (water_outlook, _declare(
        "water_outlook",
        "Measured groundwater levels in the wells nearest an irrigated field: how fast the water level has been "
        "dropping (with the range across the wells), the water left in the wells, and that carried forward to "
        "2040 and 2060 (not a forecast). For a dryland field or no field it gives the county picture.",
        {"field_id": _FIELD})),
}


def declarations() -> list[dict]:
    return [declaration for _, declaration in TOOLS.values()]


def _fit(result: dict) -> dict:
    """A last guard on size: an answer that is still too long loses its long lists, with a note."""
    if len(json.dumps(result, ensure_ascii=False)) <= MAX_RESULT_CHARS:
        return result
    out = dict(result)
    for key in ("not_modelled", "notes", "units", "fields", "futures"):
        if key in out and len(json.dumps(out, ensure_ascii=False)) > MAX_RESULT_CHARS:
            out.pop(key)
            out["left_out"] = out.get("left_out", []) + [key]
    return out


async def run(name: str, args: Any, request: Request, context: Context) -> tuple[dict, str, bool]:
    """Run one tool call of the model. -> (result for the model, one line for the page, worked or not)."""
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"There is no tool called {name}."}, f"No such tool: {name}", False
    if not isinstance(args, dict):
        args = {}
    try:
        result, summary = await entry[0](args, request, context)
    except ToolError as exc:
        return {"error": str(exc)}, f"{name} could not answer: {exc}", False
    except Exception as exc:                         # a bug of ours must not end the conversation
        import logging

        logging.getLogger("api.advisor").warning("advisor tool %s failed: %s: %s", name, type(exc).__name__, exc)
        return {"error": "This tool failed on the server and gave no answer."}, f"{name} failed on the server", False
    return _fit(result), summary, True
