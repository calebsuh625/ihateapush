"""Rotation planner: rank a handful of 4-year crop rotations for one field.

This is a deliberately simple, transparent, rule-based model. Everything it knows is in
three places you can read top to bottom: the crop table (`CROPS`), the candidate
rotations (`ROTATIONS`) and the scoring rules (`score_rotation`).

THE SEAM. The API depends on exactly this and nothing else in the module:

    FieldInput, Plan, Scores      small dataclasses (what goes in, what comes out)
    rank_plans(field) -> list[Plan]

To swap in a smarter model later (learned from satellite soil-health data, neighbour
comparisons, ...) write another function with the `Planner` signature and either point
`rank_plans` at it or pass it to `api.main.create_app(planner=...)`. Nothing else changes.

How a rotation is scored (every score is 0..1, higher is better):

    soil    crop diversity + nitrogen balance + ground cover.
            On a slope (erosion), plans that keep the ground covered gain points and plans
            that leave it bare lose them.
            A plan that keeps planting the crop the field is already stuck in loses points.
    water   low water need (mostly) + heat tolerance.
            On a field with a dryland history (wheat, sorghum, fallow), every year of a
            thirsty crop (corn, soybeans, alfalfa) loses points: without irrigation it is a gamble.
    money   typical profit. A cash crop the field has never grown earns a bit less
            (new equipment, new buyers, a learning curve).
    overall weighted average of the three. The weights move with the field:
            low organic matter or a slope  -> soil counts for more;
            a dryland history              -> water counts for more.

A missing organic-matter or slope value (None) is neutral: it nudges nothing. So is an empty
crop history. Crop names the table does not know (cotton, grass/pasture, ...) are fine in a
history: they count as "something else was grown" and never raise an error.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

# --------------------------------------------------------------------------------------
# Types — the public interface
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldInput:
    """What the planner needs to know about one field (same meaning as schema.md)."""

    field_id: str
    crop_history: dict[str, str]  # year ("2008") -> lowercase crop name; years may be missing
    organic_matter_pct: float | None
    slope_pct: float | None
    area_ha: float


@dataclass(frozen=True)
class Scores:
    """Each 0..1, higher is better."""

    soil: float
    water: float
    money: float
    overall: float


@dataclass(frozen=True)
class Plan:
    name: str  # short human label, e.g. "Corn – Soybeans – Winter wheat – Sorghum"
    rotation: list[str]  # 4 lowercase crop names, one per year
    scores: Scores
    reason: str  # one plain-English sentence, specific to the field


class Planner(Protocol):
    """Anything with this signature can replace `rank_plans`."""

    def __call__(self, field: FieldInput) -> list[Plan]: ...


# --------------------------------------------------------------------------------------
# Crop table
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Crop:
    water_need: float  # 0 = needs almost no water, 1 = thirstiest
    nitrogen_effect: float  # -1 = drains soil nitrogen hard ... +1 = adds a lot
    soil_cover: float  # 0 = bare ground, 1 = covered all year
    heat_tolerance: float  # 0 = fails in a hot dry summer, 1 = shrugs it off
    typical_profit: float  # 0 = no income, 1 = best net return per hectare


# Values are relative (0..1) and chosen for semi-arid southwest Kansas, where summers are hot,
# rain is ~450 mm a year and most high-value crops are irrigated from the shrinking Ogallala aquifer.
CROPS: dict[str, Crop] = {
    # Irrigated corn: the money crop, and the thirstiest annual (~600+ mm). Heavy nitrogen
    # feeder. Lots of residue, but heat at pollination cuts yields badly.
    "corn": Crop(water_need=0.90, nitrogen_effect=-0.80, soil_cover=0.60, heat_tolerance=0.35, typical_profit=0.90),
    # Soybeans: fix their own nitrogen and leave some for the next crop. Still need a fair amount
    # of water, and leave little, fragile residue, so the soil is poorly covered afterwards.
    "soybeans": Crop(water_need=0.65, nitrogen_effect=0.50, soil_cover=0.35, heat_tolerance=0.50, typical_profit=0.65),
    # Winter wheat: grows in the cool season and is harvested before the worst heat, so it gets
    # by on little water. Covers the soil through winter and leaves good stubble. Modest income.
    "winter wheat": Crop(water_need=0.40, nitrogen_effect=-0.40, soil_cover=0.75, heat_tolerance=0.70, typical_profit=0.45),
    # Grain sorghum (milo): the drought and heat specialist of the High Plains. Uses much less
    # water than corn, good residue, lower income than corn.
    "sorghum": Crop(water_need=0.35, nitrogen_effect=-0.50, soil_cover=0.65, heat_tolerance=0.90, typical_profit=0.50),
    # Alfalfa: a deep-rooted perennial. Uses more water than anything else here, but fixes a lot
    # of nitrogen and covers the soil all year. Hay sells well to local dairies and feedlots,
    # though the seeding year earns little.
    "alfalfa": Crop(water_need=1.00, nitrogen_effect=0.80, soil_cover=0.95, heat_tolerance=0.60, typical_profit=0.65),
    # --- the spec's "cover crop / fallow" row, split in two because soil cover is opposite ---
    # Fallow: nothing planted. Stores some rain for the next crop and earns nothing; the soil sits
    # exposed to wind and sun (heat tolerance is neutral: nothing to scorch, but bare soil bakes).
    "fallow": Crop(water_need=0.05, nitrogen_effect=0.00, soil_cover=0.10, heat_tolerance=0.50, typical_profit=0.00),
    # Cover crop: planted to protect and feed the soil, not to sell (a little grazing value).
    # Uses some water, keeps the ground covered, and legumes in the mix add nitrogen.
    "cover crop": Crop(water_need=0.25, nitrogen_effect=0.40, soil_cover=0.90, heat_tolerance=0.65, typical_profit=0.05),
}
NOT_CASH_CROPS = frozenset({"fallow", "cover crop"})

# --------------------------------------------------------------------------------------
# Candidate rotations (4 years each). Every one uses a different mix of crops.
# --------------------------------------------------------------------------------------

ROTATIONS: list[tuple[str, str, str, str]] = [
    ("corn", "corn", "corn", "corn"),  # continuous irrigated corn: the baseline many pivots run
    ("corn", "soybeans", "corn", "soybeans"),  # classic corn/soy
    ("corn", "corn", "soybeans", "winter wheat"),  # corn-heavy, with a two-year break
    ("corn", "soybeans", "winter wheat", "sorghum"),  # four different cash crops
    ("soybeans", "corn", "winter wheat", "cover crop"),  # legume first, cover crop after wheat
    ("winter wheat", "sorghum", "fallow", "winter wheat"),  # dryland wheat-sorghum-fallow
    ("winter wheat", "corn", "sorghum", "fallow"),  # dryland with one corn year
    ("winter wheat", "sorghum", "cover crop", "winter wheat"),  # wheat-sorghum, cover crop replaces fallow
    ("alfalfa", "alfalfa", "alfalfa", "corn"),  # alfalfa stand, then corn uses the nitrogen it left
    ("sorghum", "soybeans", "winter wheat", "cover crop"),  # low-water, no corn, legume + cover crop
]
assert len(set(ROTATIONS)) == len(ROTATIONS)
assert all(len(r) == 4 and all(c in CROPS for c in r) for r in ROTATIONS)

# --------------------------------------------------------------------------------------
# Scoring knobs (all in one place so they are easy to argue about)
# --------------------------------------------------------------------------------------

BASE_WEIGHTS = {"soil": 0.30, "water": 0.30, "money": 0.40}  # for a field we know nothing special about

# soil score = these three parts (they sum to 1)
SOIL_DIVERSITY, SOIL_NITROGEN, SOIL_COVER = 0.35, 0.30, 0.35
# water score = these two parts (they sum to 1)
WATER_LOW_NEED, WATER_HEAT = 0.65, 0.35

TYPICAL_OM_PCT = 2.0  # neutral organic matter for this region
LOW_OM_PCT = 1.5  # at or below this the sentence says "low-organic-matter soil"
GOOD_OM_PCT = 2.5  # at or above this the sentence says the soil is in good shape
SOIL_WEIGHT_FOR_LOW_OM = 0.25  # added to the soil weight at 1% organic matter (half of it removed at 2.5%+)
FLAT_SLOPE_PCT, STEEP_SLOPE_PCT = 1.0, 6.0  # erosion risk goes 0 -> 1 between these
SOIL_WEIGHT_FOR_SLOPE = 0.15  # added to the soil weight at full erosion risk
TYPICAL_COVER = 0.60  # ground cover of a middling plan; on a slope, more than this gains, less loses
SLOPE_COVER_SWING = 0.80  # soil points per unit of cover above/below typical, at full erosion risk
WATER_WEIGHT_FOR_DRYLAND = 0.60  # added to the water weight for a clearly dryland history
THIRSTY_WATER_NEED = 0.60  # crops at or above this (corn, soybeans, alfalfa) are irrigated crops here
DRYLAND_THIRST_PENALTY = 0.60  # water points lost by a plan made only of thirsty crops, on a dryland field
STUCK_PENALTY = 0.45  # taken off the soil score of a plan that just repeats the stuck crop
NEW_CROP_DISCOUNT = 0.25  # share of profit lost on a cash crop the field has never grown

RECENT_YEARS = 10  # "what has this field been growing lately" window
MIN_YEARS_FOR_SHARE = 5  # need at least this many recorded years in the window to judge the mix
# How much a long run of one crop matters. Corn-on-corn is the case we care most about; a long
# alfalfa stand is normal farming, so it counts least.
STUCK_IMPORTANCE = {"corn": 1.0, "alfalfa": 0.5}
STUCK_IMPORTANCE_DEFAULT = 0.7
STUCK_WORTH_MENTIONING = 0.3  # stuck_pressure at which the sentence talks about it

MAX_REASON_CHARS = 160


# --------------------------------------------------------------------------------------
# What the field's own data says
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldContext:
    """Everything the nudges need, worked out once per field."""

    organic_matter_pct: float | None
    slope_pct: float | None
    soil_need: float  # -0.5 (rich soil) .. 0 (typical or unknown) .. 1 (very low organic matter)
    erosion_risk: float  # 0 (flat or unknown) .. 1 (steep)
    dryness: float  # 0 (thirsty-crop or unknown history) .. 1 (history of low-water crops: dryland)
    grown: frozenset[str]  # every crop in the recorded history (both halves of a double crop)
    run_crop: str | None  # crop in the most recent recorded year
    run_years: int  # how many consecutive years, ending with the most recent, it was grown
    top_crop: str | None  # most common crop over the recent window
    top_count: int  # years of top_crop in the window
    window_years: int  # recorded years in the window
    stuck_crop: str | None  # the crop the field is stuck in, if any
    stuck_pressure: float  # 0 (no rut) .. 1 (years of the same crop)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(value) -> float | None:
    """A usable float, or None for None / NaN / infinity / junk."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _history_by_year(crop_history) -> dict[int, str]:
    """{2008: "corn", ...}; silently skips anything that is not a year -> crop-name pair."""
    out: dict[int, str] = {}
    if not isinstance(crop_history, dict):
        return out
    for year, crop in crop_history.items():
        try:
            year_number = int(year)
        except (TypeError, ValueError):
            continue
        if isinstance(crop, str) and crop.strip():
            out[year_number] = crop.strip().lower()
    return out


def _crops_in(name: str) -> list[str]:
    """The crops behind one history entry: "double crop: winter wheat/sorghum" is two of them."""
    if name.startswith("double crop:"):
        return [part.strip() for part in name.split(":", 1)[1].split("/") if part.strip()]
    return [name]


def analyse_field(field: FieldInput) -> FieldContext:
    """Turn the raw field values into the handful of 0..1 signals the scoring rules use."""
    om = _number(field.organic_matter_pct)
    slope = _number(field.slope_pct)
    soil_need = 0.0 if om is None else _clamp(TYPICAL_OM_PCT - om, -0.5, 1.0)
    erosion_risk = 0.0 if slope is None else _clamp((slope - FLAT_SLOPE_PCT) / (STEEP_SLOPE_PCT - FLAT_SLOPE_PCT))

    history = _history_by_year(field.crop_history)
    run_crop: str | None = None
    run_years = 0
    top_crop: str | None = None
    top_count = 0
    window: dict[int, str] = {}
    if history:
        latest = max(history)
        run_crop = history[latest]
        year = latest
        while history.get(year) == run_crop:  # consecutive calendar years only
            run_years += 1
            year -= 1
        window = {y: c for y, c in history.items() if y > latest - RECENT_YEARS}
        counts = Counter(window.values())
        # most common; ties go to the crop grown most recently, then alphabetical (deterministic)
        top_crop = min(counts, key=lambda c: (-counts[c], c != run_crop, c))
        top_count = counts[top_crop]

    # Dryland signal: the average thirst of what the field has been growing lately (known crops only).
    # Wheat-sorghum-fallow averages 0.27 (-> 1), wheat-corn-sorghum-fallow 0.43 (-> 0.5), and anything
    # from 0.55 up (corn-sorghum-wheat, corn/soy at 0.78) is not dryland (-> 0). Nobody fallows a pivot.
    thirst = [CROPS[c].water_need for c in window.values() if c in CROPS]
    dryness = 0.0
    if len(thirst) >= MIN_YEARS_FOR_SHARE:
        dryness = _clamp((0.55 - sum(thirst) / len(thirst)) / 0.25)

    # Is the field stuck in one crop? Either a long unbroken run, or one crop in most recent years.
    def importance(crop: str | None) -> float:
        return STUCK_IMPORTANCE.get(crop, STUCK_IMPORTANCE_DEFAULT) if crop else 0.0

    run_pressure = _clamp((run_years - 1) / 5) * importance(run_crop)  # 2 years: 0.2 ... 6+ years: 1
    share_pressure = 0.0
    if len(window) >= MIN_YEARS_FOR_SHARE:
        share = top_count / len(window)
        share_pressure = _clamp((share - 0.5) / 0.35) * importance(top_crop)  # half the years: 0 ... 85%: 1
    if share_pressure > run_pressure:
        stuck_crop, stuck_pressure = top_crop, share_pressure
    else:
        stuck_crop, stuck_pressure = run_crop, run_pressure
    if stuck_pressure <= 0:
        stuck_crop = None

    return FieldContext(
        organic_matter_pct=om,
        slope_pct=slope,
        soil_need=soil_need,
        erosion_risk=erosion_risk,
        dryness=dryness,
        grown=frozenset(crop for entry in history.values() for crop in _crops_in(entry)),
        run_crop=run_crop,
        run_years=run_years,
        top_crop=top_crop,
        top_count=top_count,
        window_years=len(window),
        stuck_crop=stuck_crop,
        stuck_pressure=stuck_pressure,
    )


# --------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values)


def repeats_stuck_crop(rotation, ctx: FieldContext) -> float:
    """0 = a real break from the stuck crop ... 1 = the plan is nothing but that crop.

    One year in four of the stuck crop, later in the plan, still counts as a real break.
    """
    if not ctx.stuck_crop:
        return 0.0
    share = sum(1 for c in rotation if c == ctx.stuck_crop) / len(rotation)
    starts_with_it = 1.0 if rotation[0] == ctx.stuck_crop else 0.0
    return 0.6 * _clamp((share - 0.25) / 0.75) + 0.4 * starts_with_it


def weights_for(ctx: FieldContext) -> dict[str, float]:
    """Overall-score weights for this field (they sum to 1)."""
    soil = BASE_WEIGHTS["soil"] + SOIL_WEIGHT_FOR_LOW_OM * ctx.soil_need + SOIL_WEIGHT_FOR_SLOPE * ctx.erosion_risk
    water = BASE_WEIGHTS["water"] + WATER_WEIGHT_FOR_DRYLAND * ctx.dryness
    money = BASE_WEIGHTS["money"]
    total = soil + water + money
    return {"soil": soil / total, "water": water / total, "money": money / total}


def score_rotation(rotation, ctx: FieldContext) -> Scores:
    """Unrounded scores for one rotation on one field."""
    crops = [CROPS[c] for c in rotation]

    # --- soil ---
    planted = {c for c in rotation if c != "fallow"}  # a bare year adds no diversity
    diversity = _clamp((len(planted) - 1) / 3)  # 1 crop: 0 ... 4 different crops: 1
    nitrogen = _clamp(0.5 + _mean(c.nitrogen_effect for c in crops))  # balanced: 0.5, building: more
    cover = _mean(c.soil_cover for c in crops)
    soil = SOIL_DIVERSITY * diversity + SOIL_NITROGEN * nitrogen + SOIL_COVER * cover
    soil += SLOPE_COVER_SWING * ctx.erosion_risk * (cover - TYPICAL_COVER)  # on a slope, bare ground washes away
    soil -= STUCK_PENALTY * ctx.stuck_pressure * repeats_stuck_crop(rotation, ctx)
    soil = _clamp(soil)

    # --- water ---
    low_need = 1 - _mean(c.water_need for c in crops)
    water = WATER_LOW_NEED * low_need + WATER_HEAT * _mean(c.heat_tolerance for c in crops)
    thirsty_years = sum(1 for c in crops if c.water_need >= THIRSTY_WATER_NEED) / len(crops)
    water -= DRYLAND_THIRST_PENALTY * ctx.dryness * thirsty_years  # no irrigation: a thirsty crop is a gamble
    water = _clamp(water)

    # --- money ---
    def profit(name: str) -> float:
        new_to_field = bool(ctx.grown) and name not in ctx.grown and name not in NOT_CASH_CROPS
        return CROPS[name].typical_profit * (1 - NEW_CROP_DISCOUNT if new_to_field else 1)

    money = _clamp(_mean(profit(name) for name in rotation))

    weights = weights_for(ctx)
    overall = _clamp(weights["soil"] * soil + weights["water"] * water + weights["money"] * money)
    return Scores(soil=soil, water=water, money=money, overall=overall)


# --------------------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------------------


def rotation_name(rotation) -> str:
    """Short label: "Corn – Soybeans – Winter wheat – Sorghum", "Continuous corn", "Alfalfa (3 yrs) – Corn"."""
    rotation = list(rotation)
    if len(set(rotation)) == 1:
        return f"Continuous {rotation[0]}"
    if len(rotation) == 4 and rotation[:2] == rotation[2:]:
        return f"{rotation[0].capitalize()} – {rotation[1].capitalize()} (alternating)"
    parts: list[str] = []
    i = 0
    while i < len(rotation):
        j = i
        while j < len(rotation) and rotation[j] == rotation[i]:
            j += 1
        label = rotation[i].capitalize()
        parts.append(f"{label} ({j - i} yrs)" if j - i > 1 else label)
        i = j
    return " – ".join(parts)


def _and(words) -> str:
    words = list(words)
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _unique(rotation) -> list[str]:
    return list(dict.fromkeys(rotation))


@dataclass(frozen=True)
class Clause:
    text: str
    topics: frozenset[str]  # two clauses that share a topic would say the same thing twice


def _clause(text: str, *topics: str) -> Clause:
    return Clause(text, frozenset(topics))


def _field_clauses(rotation, scores: Scores, ctx: FieldContext) -> list[Clause]:
    """What about THIS field the plan answers, strongest driver first."""
    found: list[tuple[float, Clause]] = []  # (strength of the driver, clause)
    crops = _unique(rotation)

    # 1. gets the field out of a rut
    stuck = ctx.stuck_crop
    breaks_it = rotation[0] != stuck and list(rotation).count(stuck) <= 1
    if stuck in CROPS and ctx.stuck_pressure >= STUCK_WORTH_MENTIONING and breaks_it:
        if stuck == ctx.run_crop and ctx.run_years >= 3:
            if stuck == "alfalfa":
                text = f"rotates out of your {ctx.run_years}-year alfalfa stand"
            else:
                text = f"breaks your {ctx.run_years}-year {stuck} run"
            found.append((ctx.stuck_pressure, _clause(text, "break")))
        elif stuck == ctx.top_crop:
            text = f"gives the field a break from {stuck} (grown {ctx.top_count} of the last {ctx.window_years} years)"
            found.append((ctx.stuck_pressure, _clause(text, "break")))

    # 2. rebuilds thin soil
    om = ctx.organic_matter_pct
    if om is not None and om <= LOW_OM_PCT:
        builders = [c for c in ("alfalfa", "soybeans", "cover crop") if c in crops]
        if "alfalfa" in builders:
            text = f"uses alfalfa to rebuild nitrogen on low-organic-matter soil ({om:.1f}%)"
            found.append((ctx.soil_need, _clause(text, "soil")))
        elif builders:
            verb = "adds" if all(c not in ctx.grown for c in builders) else "uses"
            named = _and("a cover crop" if c == "cover crop" else c for c in builders)
            goal = "rebuild nitrogen on" if "soybeans" in builders else "feed"
            text = f"{verb} {named} to {goal} low-organic-matter soil ({om:.1f}%)"
            found.append((ctx.soil_need, _clause(text, "soil")))

    # 3. holds the soil on a slope
    if ctx.slope_pct is not None and ctx.erosion_risk >= 0.4:
        if "fallow" not in crops and _mean(CROPS[c].soil_cover for c in rotation) >= 0.6:
            text = f"keeps the ground covered on your {round(ctx.slope_pct, 1):g}% slope"
            found.append((ctx.erosion_risk, _clause(text, "cover")))

    # 4. fits a dryland field
    if ctx.dryness >= 0.5 and scores.water >= 0.55:
        found.append((ctx.dryness, _clause("fits a field that has mostly grown low-water crops", "water")))

    # 5. the soil is in good shape, so income can lead
    if om is not None and om >= GOOD_OM_PCT and scores.money >= 0.6:
        earners = [c for c in crops if CROPS[c].typical_profit >= 0.65]
        if earners:
            text = f"leans on {_and(earners)} for income because the soil is already in good shape ({om:.1f}% organic matter)"
            found.append((0.5, _clause(text, "money", "soil")))

    found.sort(key=lambda item: -item[0])  # stable: equal strengths keep the order above
    clauses = [clause for _, clause in found]

    # 6. nothing special about the field? at least tie the plan to what it already grows
    if not clauses:
        familiar = [c for c in crops if c in ctx.grown and c != "fallow"]
        if familiar:
            verb = "sticks with" if len(familiar) == len(crops) else "builds on"
            clauses.append(_clause(f"{verb} the {_and(familiar)} you already grow", "familiar"))
    return clauses


def _plan_clauses(rotation, scores: Scores, ctx: FieldContext) -> list[Clause]:
    """What the plan is good at whatever the field, best trait first. Only real strengths."""
    crops = _unique(rotation)
    by_trait: dict[str, Clause] = {}

    if "fallow" in crops:
        pass  # a bare year is nothing to boast about for the soil
    elif "alfalfa" in crops:
        by_trait["soil"] = _clause("builds the soil with deep-rooted alfalfa", "soil")
    elif "cover crop" in crops:
        by_trait["soil"] = _clause("protects the soil with a cover crop", "soil", "cover")
    elif len(crops) >= 4:
        by_trait["soil"] = _clause("spreads risk across four different crops", "diversity")
    elif "soybeans" in crops:
        by_trait["soil"] = _clause("uses soybeans to add nitrogen for the next crop", "soil")
    else:
        by_trait["soil"] = _clause("leaves crop stubble on the ground", "cover")

    if scores.water >= 0.45:
        thrifty = [c for c in crops if c in ("sorghum", "winter wheat")]
        if "fallow" in crops:
            by_trait["water"] = _clause("rests the field for a year to store rain", "fallow")
        elif thrifty:
            by_trait["water"] = _clause(f"saves water with drought-hardy {_and(thrifty)}", "water")

    stuck = ctx.stuck_crop
    if stuck in crops and ctx.stuck_pressure >= STUCK_WORTH_MENTIONING and rotation[0] != stuck:
        by_trait["money"] = _clause(f"brings {stuck} back in year {list(rotation).index(stuck) + 1}", "money")
    elif "corn" in crops:
        by_trait["money"] = _clause("earns well from corn", "money")
    elif "alfalfa" in crops:
        by_trait["money"] = _clause("earns steady income from alfalfa hay", "money")
    elif "soybeans" in crops:
        by_trait["money"] = _clause("earns steady income from soybeans", "money")

    order = sorted(by_trait, key=lambda trait: -getattr(scores, trait))  # stable
    return [by_trait[trait] for trait in order]


def reason_for(rotation, scores: Scores, ctx: FieldContext, taken: frozenset[str] | set[str] = frozenset()) -> str:
    """One plain sentence: what about this field the plan answers, then what the plan is good at.

    `taken` holds sentences already used for better-ranked plans, so three plans for one
    field do not all get the identical sentence.
    """
    field_clauses = _field_clauses(rotation, scores, ctx)
    plan_clauses = _plan_clauses(rotation, scores, ctx)

    def sentence(*clauses: Clause) -> str:
        text = " and ".join(clause.text for clause in clauses)
        return f"{text[0].upper()}{text[1:]}."

    def pairs(lead: Clause, extras: list[Clause]) -> list[str]:
        return [sentence(lead, extra) for extra in extras if not (lead.topics & extra.topics)]

    # Most field-specific first: two things about the field, then one plus a plan strength, ...
    options: list[str] = []
    if len(field_clauses) >= 2:
        options.append(sentence(field_clauses[0], field_clauses[1]))
    for lead in field_clauses[:2]:
        options += pairs(lead, plan_clauses)
    options += [sentence(clause) for clause in field_clauses[:1]]
    for i, lead in enumerate(plan_clauses):
        options += pairs(lead, plan_clauses[i + 1 :])
    options += [sentence(clause) for clause in plan_clauses[:1]]
    if not options:  # cannot happen with the rotations above; never return an empty sentence
        options.append("A balanced mix of crops for this field.")

    fitting = [text for text in options if len(text) <= MAX_REASON_CHARS] or [options[-1][:MAX_REASON_CHARS]]
    for text in fitting:
        if text not in taken:
            return text
    return fitting[0]


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def rank_plans(field: FieldInput) -> list[Plan]:
    """The top 3 rotations for this field, best first. Pure and deterministic."""
    ctx = analyse_field(field)
    scored = [(score_rotation(rotation, ctx), index, rotation) for index, rotation in enumerate(ROTATIONS)]
    # best overall first; exact ties keep the order of ROTATIONS
    scored.sort(key=lambda item: (-round(item[0].overall, 6), item[1]))
    plans: list[Plan] = []
    taken: set[str] = set()
    for scores, _, rotation in scored[:3]:
        reason = reason_for(rotation, scores, ctx, taken)
        taken.add(reason)
        plans.append(
            Plan(
                name=rotation_name(rotation),
                rotation=list(rotation),
                scores=Scores(
                    soil=round(scores.soil, 3),
                    water=round(scores.water, 3),
                    money=round(scores.money, 3),
                    overall=round(scores.overall, 3),
                ),
                reason=reason,
            )
        )
    return plans
