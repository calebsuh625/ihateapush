"""Build-your-own rotation (Phase 4, item 1): score any rotation a farmer types, like our plans.

    from api.planner.custom import parse_rotation, score_rotation
    cycle = parse_rotation("corn, milo, wheat, fallow")       # ["corn", "sorghum", "winter wheat", "fallow"]
    body = score_rotation(field, cycle, weights=..., scenario="ssp245", horizon=2050)   # GET /field/{id}/score

What is scored
--------------
The rotation is 2 to 6 years of the six crops and is read as a CYCLE: after its last year comes its
first again, exactly like the plans the planner recommends (smart.py). It gets the same engine, the
same field numbers (irrigated or dryland, erodibility, slope), the same climate futures and the same
priorities as GET /field/{id}/plans, so every number is comparable with the cards.

Before scoring, the cycle is put in its normal form (`normal_form`): the shortest repeating unit
(corn, soybeans, corn, soybeans is corn-soybeans), a 1- or 2-year unit written over four years (so it
is the same thing as the planner's "alternating" and "continuous" candidates), and one fixed starting
year. So the score does not change when the farmer starts the same cycle in another year. When the
normal form is one of the planner's candidates the rotation gets that candidate's own numbers and its
own place in the list; otherwise it is scored on its own and `rank` says where it would stand.

The comparison
--------------
Against our first pick, row by row (soil, water, money, risk, holds up). A row names a winner only
when the same side is ahead in EVERY climate model (each model is one future; both rotations face the
same weather in it). When the models do not agree on which is ahead the row says "too close to call"
and gives the count; when the two figures are the same at the precision shown it says they are the
same. The same test decides which figures the app's side-by-side table marks (`marks`).

`comparison.priorities` answers "when would mine come first?": each of the four priorities is tried
on its own (that one is all that matters) and the rotation's place among the candidates is reported.

Nothing planted
---------------
A rotation that is fallow every year is NOT rankable. With no crop, the engine's water and risk scores
are 1.00 (nothing can run short of water or fail), which once put it first of all on dryland. It gets
no rank, no comparison rows, no "would come first" sentences and no marks in its column; the answer
carries `yours.rankable = false` and one plain sentence (`nothing_planted_sentence`) with its own
figures: what the fallow costs and how many days a year the soil sits bare.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Mapping, Sequence

import numpy as np

from api.planner import candidates as cand
from api.planner import config, smart
from api.planner.crops import CROPS
from api.planner.legacy import FieldInput, rotation_name
from api.planner.smart import MM_PER_INCH, Engine, Evaluation, Weights
from api.planner.traits import FieldTraits

MIN_YEARS, MAX_YEARS = 2, 6

# What a farmer may call each of the six crops. Lower case; matched as whole words.
ALIASES: dict = {
    "corn": ("corn", "maize", "field corn", "grain corn"),
    "soybeans": ("soybeans", "soybean", "soy", "soya", "soy beans", "soy bean", "beans", "bean"),
    "winter wheat": ("winter wheat", "wheat", "hard red winter wheat", "hard red winter", "hrw", "hrww", "ww"),
    "sorghum": ("sorghum", "milo", "grain sorghum"),
    "alfalfa": ("alfalfa", "alfalfa hay", "lucerne"),
    "fallow": ("fallow", "idle", "summer fallow", "chem fallow", "chemfallow", "rest"),
}
_ALIAS = {alias: crop for crop, names in ALIASES.items() for alias in names}
_MAX_ALIAS_WORDS = max(len(alias.split()) for alias in _ALIAS)
_SEPARATORS = re.compile(r"[,;>/|\n→–—]+|\s-+\s|\bthen\b|\band\b")
_SIX = "corn, soybeans, winter wheat, sorghum, alfalfa and fallow"

PRIORITY_WORDS = {"soil": "soil protection", "water": "water", "money": "money", "risk": "risk"}
ROW_LABELS = {"soil": "Soil protection", "water": "Water", "money": "Money", "risk": "Risk", "holds": "Holds up"}
ROW_WORDS = {"soil": "soil protection", "water": "water", "money": "money", "risk": "risk",
             "holds": "holding up across the futures"}
DRYLAND_MONEY_NOTE = (
    "On dryland the sign of these returns depends on the price years and the cost of fallow, so read the money "
    "row as a rough guide."
)
CYCLE_NOTE = "Your rotation is scored as a repeating cycle: after its last year comes its first again."
MARKS_NOTE = (
    "A mark means no other column is ahead of that figure in every climate model. A row has no mark when "
    "the columns cannot be told apart."
)


class RotationError(ValueError):
    """The rotation as typed cannot be scored. The message is one plain sentence for the farmer."""


# --- reading what was typed --------------------------------------------------------------------------

def _words_to_crops(words: list) -> list:
    """Crops from a run of words, longest names first ("winter wheat" before "wheat")."""
    out, i = [], 0
    while i < len(words):
        for size in range(min(_MAX_ALIAS_WORDS, len(words) - i), 0, -1):
            crop = _ALIAS.get(" ".join(words[i:i + size]))
            if crop is not None:
                out.append(crop)
                i += size
                break
        else:
            raise RotationError(f"We do not have numbers for \"{words[i]}\". A rotation here is made of {_SIX}.")
    return out


def check_cycle(crops: Sequence[str]) -> tuple:
    """The crops as a tuple, or RotationError when a name is not one of the six or the length is off."""
    cycle = tuple(str(crop).strip().lower() for crop in crops)
    unknown = [crop for crop in cycle if crop not in CROPS]
    if unknown:
        raise RotationError(f"We do not have numbers for \"{unknown[0]}\". A rotation here is made of {_SIX}.")
    if not MIN_YEARS <= len(cycle) <= MAX_YEARS:
        years = "1 year" if len(cycle) == 1 else f"{len(cycle)} years"
        raise RotationError(f"A rotation needs {MIN_YEARS} to {MAX_YEARS} years, one crop a year; this one has {years}.")
    return cycle


def parse_rotation(text: str | Sequence[str] | None) -> list:
    """["corn", "sorghum", "winter wheat", "fallow"] from "corn, milo, wheat, fallow".

    Crops are separated by commas, semicolons, slashes, arrows, " - ", "then", "and", new lines or just
    spaces. Case and extra spaces do not matter. Common names are accepted (ALIASES). RotationError
    (a ValueError) with one plain sentence when a crop is not one of the six, when nothing was given,
    or when there are fewer than 2 or more than 6 years.
    """
    if text is None or isinstance(text, str):
        raw = (text or "").lower()
        parts = [part for part in _SEPARATORS.split(raw) if part.strip()]
    else:
        parts = [str(part).lower() for part in text if str(part).strip()]
    crops: list = []
    for part in parts:
        words = re.sub(r"[^a-z\s]", " ", part.replace("-", " ")).split()
        crops.extend(_words_to_crops(words))
    if not crops:
        raise RotationError(f"Give one crop for each year, for example: corn, sorghum, wheat, fallow. Crops: {_SIX}.")
    return list(check_cycle(crops))


# --- the cycle's normal form -------------------------------------------------------------------------

def repeating_unit(cycle: Sequence[str]) -> tuple:
    """The shortest stretch the cycle is made of: (corn, soybeans, corn, soybeans) -> (corn, soybeans)."""
    cycle = tuple(cycle)
    n = len(cycle)
    for size in range(1, n + 1):
        if n % size == 0 and cycle[:size] * (n // size) == cycle:
            return cycle[:size]
    return cycle


def normal_form(cycle: Sequence[str]) -> tuple:
    """One way to write the cycle whichever year it starts in and however often it is repeated.

    The shortest repeating unit; a 1- or 2-year unit is written over four years (the planner's own
    candidates are 3 or 4 years long); 3- and 4-year units start where the planner's do
    (`candidates.canonical`), longer ones at the shift that comes first in the fixed crop order.
    """
    unit = repeating_unit(cycle)
    if len(unit) in (1, 2):
        unit = unit * (cand.YEARS // len(unit))
    if len(unit) in cand.CYCLE_LENGTHS:
        return cand.canonical(unit)
    order = {crop: i for i, crop in enumerate(CROPS)}
    shifts = [unit[i:] + unit[:i] for i in range(len(unit))]
    return min(shifts, key=lambda shift: tuple(order[crop] for crop in shift))


def cycle_name(cycle: Sequence[str]) -> str:
    cycle = list(cycle)
    return rotation_name(cycle) if len(cycle) == cand.YEARS else f"{rotation_name(cycle)} ({len(cycle)}-year cycle)"


# --- comparing two rotations model by model ----------------------------------------------------------

def versus(yours, ours, higher_is_better: bool = True, same: bool = False) -> dict:
    """Who is ahead, future by future. `yours` and `ours`: one value per climate model (or one value).

    verdict  "yours" / "ours": that side is ahead in every model where they differ;
             "too_close": the models do not agree on which is ahead;
             "same": no difference in any model, or `same` (the figures shown are equal).
    """
    gap = np.atleast_1d(np.asarray(yours, dtype=float)) - np.atleast_1d(np.asarray(ours, dtype=float))
    if not higher_is_better:
        gap = -gap
    yours_ahead, ours_ahead = int((gap > 1e-9).sum()), int((gap < -1e-9).sum())
    if same or (yours_ahead == 0 and ours_ahead == 0):
        verdict = "same"
    elif ours_ahead == 0:
        verdict = "yours"
    elif yours_ahead == 0:
        verdict = "ours"
    else:
        verdict = "too_close"
    return {"verdict": verdict, "yours_ahead": yours_ahead, "ours_ahead": ours_ahead, "total": int(gap.size)}


def _too_close(v: dict, what: str) -> str:
    """The tail of a sentence when the models disagree: built from the counts alone."""
    return (f" Too close to call: ours {what} in {v['ours_ahead']} of the {v['total']} climate models, "
            f"yours in {v['yours_ahead']}.")


def _both_return_words(profit: float) -> str:
    amount = round(abs(profit))
    if profit >= 0.5:
        return f"Both return about ${amount:,.0f} an acre a year over direct costs"
    if profit <= -0.5:
        return f"Both fall about ${amount:,.0f} an acre a year short of their direct costs"
    return "Both about cover their direct costs"


def _fail_words(pct: int) -> str:
    return "fewer than 1 in 100" if pct == 0 else f"about {pct:.0f}%"


def _dollars(value: float) -> str:
    amount = f"${abs(round(value)):,.0f}"
    return f"\u2212{amount}" if round(value) < 0 else amount      # a real minus sign, as in the app


def _return_words(profit: float) -> str:
    """Return over direct costs, never "earns" or "profit" (smart._money_words, with "a year" placed for two)."""
    amount = round(abs(profit))
    if profit >= 0.5:
        return f"returns about ${amount:,.0f} an acre a year over direct costs"
    if profit <= -0.5:
        return f"falls about ${amount:,.0f} an acre a year short of its direct costs"
    return "about covers its direct costs"


def _half_up(x: float) -> int:
    """Whole number, halves up, as the app's own rounding does (Python's round sends 54.5 to 54)."""
    return math.floor(x + 0.5 + 1e-9)


def _pct(share: float) -> int:
    """Whole percent of a share, from the three decimals the API sends (so words and cards agree)."""
    return _half_up(100 * round(share, 3))


def _days(bare: float) -> int:
    return _half_up(round(bare, 1))


def _score(value: float) -> float:
    """A 0-1 score at the two decimals the app shows."""
    return _half_up(100 * round(value, 3)) / 100


def _inches(mm: float) -> str:
    value = round(mm / MM_PER_INCH, 1)
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


class _Side:
    """One rotation's numbers in every future, read from an Evaluation row."""

    def __init__(self, ev: Evaluation, i: int, weights: Weights):
        self.soil, self.water, self.money, self.risk = ev.soil[i], ev.water[i], ev.money[i], ev.risk[i]
        self.overall = ev.overall(weights)[i]
        self.robust = float(smart.robust_value(self.overall[None, :])[0])
        self.bare = float(ev.bare[i])
        self.irrigation, self.water_met = ev.irrigation[i], ev.water_met[i]
        self.profit, self.failed = ev.profit[i], ev.failed[i]
        self.holds, self.total = int(ev.holds[i].sum()), int(ev.holds.shape[1])


def _row(key: str, v: dict, yours: float, ours: float, sentence: str, unit: str, digits: int,
         score_yours: float | None = None, score_ours: float | None = None, **more) -> dict:
    # `models`: in how many climate models each side is ahead; None for a figure that is one number, not six
    models = ({"yours_ahead": v["yours_ahead"], "ours_ahead": v["ours_ahead"], "total": v["total"]}
              if v["total"] > 1 else None)
    # `diff` is taken between the two rounded values, so the three numbers always add up as shown
    yours, ours = round(yours, digits), round(ours, digits)
    row = {"key": key, "label": ROW_LABELS[key], "unit": unit,
           "yours": yours, "ours": ours, "diff": round(yours - ours, digits),
           "verdict": v["verdict"], "models": models, "sentence": sentence}
    if score_yours is not None and score_ours is not None:
        row["score_yours"], row["score_ours"] = round(score_yours, 3), round(score_ours, 3)
        row["score_diff"] = round(row["score_yours"] - row["score_ours"], 3)
    row.update(more)
    return row


def compare_rows(y: _Side, o: _Side, irrigated: bool, plants_nothing: bool = False) -> list:
    """The five comparison rows, yours against ours: values, verdict and one sentence each. Every figure
    in a sentence is one of the row's own values. `plants_nothing`: yours is fallow every year."""
    rows = []

    # soil: bare days a year (the erosion part of the score follows the bare days on the same field)
    yb, ob = _days(y.bare), _days(o.bare)
    v = versus(y.soil, o.soil, same=yb == ob)
    text = (f"Both leave the soil bare about {ob:.0f} days a year." if yb == ob
            else f"Ours leaves the soil bare about {ob:.0f} days a year, yours about {yb:.0f}.")
    rows.append(_row("soil", v, y.bare, o.bare, text, "bare days a year", 1,
                     float(y.soil.mean()), float(o.soil.mean())))

    # water: inches pumped (irrigated) or the share of the crops' need that rain covers (dryland)
    if irrigated:
        ym, om = float(y.irrigation.mean()), float(o.irrigation.mean())
        yi, oi = _inches(ym), _inches(om)
        v = versus(y.irrigation, o.irrigation, higher_is_better=False, same=yi == oi)
        text = (f"Both pump about {oi} inches of water a year." if yi == oi
                else f"Ours pumps about {oi} inches of water a year, yours about {yi}.")
        if v["verdict"] == "too_close":
            text += _too_close(v, "pumps less")
        rows.append(_row("water", v, ym / MM_PER_INCH, om / MM_PER_INCH, text, "inches pumped a year", 1,
                         float(y.water.mean()), float(o.water.mean())))
    else:
        ym, om = float(y.water_met.mean()), float(o.water_met.mean())
        yp, op = _pct(ym), _pct(om)
        v = versus(y.water_met, o.water_met, same=yp == op)
        if plants_nothing:
            text = f"Yours plants no crop, so it needs no water; rain covers about {op:.0f}% of the water ours needs."
        elif yp == op:
            text = f"In both, rain covers about {op:.0f}% of the crops' water need."
        else:
            text = f"Rain covers about {op:.0f}% of the water ours needs and about {yp:.0f}% of what yours needs."
        if v["verdict"] == "too_close":
            text += _too_close(v, "has more of its water need covered by rain")
        rows.append(_row("water", v, ym, om, text, "share of crop water need met by rain", 3,
                         float(y.water.mean()), float(o.water.mean())))

    # money: return over direct costs, with the lowest and highest climate model
    ym, om = float(y.profit.mean()), float(o.profit.mean())
    v = versus(y.profit, o.profit, same=round(ym) == round(om))

    def span(side: _Side) -> str:
        low, high = _dollars(float(side.profit.min())), _dollars(float(side.profit.max()))
        return "the same in every climate model" if low == high else f"{low} to {high} across the climate models"

    if round(ym) == round(om):
        text = f"{_both_return_words(om)} (ours: {span(o)}; yours: {span(y)})."
    else:
        text = f"Ours {_return_words(om)} ({span(o)}). Yours {_return_words(ym)} ({span(y)})."
    if v["verdict"] == "too_close":
        text += _too_close(v, "returns more")
    rows.append(_row("money", v, ym, om, text, "$ an acre a year over direct costs", 0,
                     float(y.money.mean()), float(o.money.mean()),
                     yours_range=[round(float(y.profit.min())), round(float(y.profit.max()))],
                     ours_range=[round(float(o.profit.min())), round(float(o.profit.max()))]))

    # risk: share of planted crops that fail
    ym, om = float(y.failed.mean()), float(o.failed.mean())
    yp, op = _pct(ym), _pct(om)
    v = versus(y.failed, o.failed, higher_is_better=False, same=yp == op)
    ours_fail = f"{_fail_words(op)} of the crops ours plants fail (a yield under half of normal)"
    if plants_nothing:
        text = f"Yours plants no crop, so none can fail; {ours_fail}."
    elif yp == op == 0:
        text = "In both, fewer than 1 planted crop in 100 fails (a yield under half of normal)."
    elif yp == op:
        text = f"In both, about {op:.0f}% of the planted crops fail (a yield under half of normal)."
    else:
        text = f"{ours_fail[0].upper()}{ours_fail[1:]}, against {_fail_words(yp)} of yours."
    if v["verdict"] == "too_close":
        text += _too_close(v, "loses fewer crops")
    rows.append(_row("risk", v, ym, om, text, "share of planted crops that fail", 3,
                     float(y.risk.mean()), float(o.risk.mean())))

    # holds up: a count of the futures, so there is nothing to average
    v = versus(y.holds, o.holds)
    text = (f"Both hold up in {o.holds} of {o.total} climate futures." if y.holds == o.holds
            else f"Ours holds up in {o.holds} of {o.total} climate futures, yours in {y.holds}.")
    rows.append(_row("holds", v, y.holds, o.holds, text, f"of {o.total} climate futures", 0))
    return rows


def table_marks(sides: Sequence[_Side], irrigated: bool, plants_nothing: bool = False) -> dict:
    """{row: [columns to mark]} for the side-by-side table (column 0 is yours, then our picks in order).

    A column is marked unless some other column is ahead of it in every climate model with a different
    figure at the precision shown; a row where nothing separates the columns has no marks. The ranking
    score is the exception: the highest one is marked, because that is what the order is.
    `plants_nothing`: yours is fallow every year, so it is left out: no mark in column 0 in any row, and
    our picks are marked among themselves.
    """
    def shown_water(side: _Side):
        return _inches(float(side.irrigation.mean())) if irrigated else _pct(float(side.water_met.mean()))

    rows = {
        "soil": (lambda s: s.soil, True, lambda s: _score(float(s.soil.mean()))),
        "water": (lambda s: s.water, True, lambda s: _score(float(s.water.mean()))),
        "money": (lambda s: s.money, True, lambda s: _score(float(s.money.mean()))),
        "risk": (lambda s: s.risk, True, lambda s: _score(float(s.risk.mean()))),
        "holds": (lambda s: s.holds, True, lambda s: s.holds),
        "bare_days": (lambda s: s.bare, False, lambda s: _days(s.bare)),
        "water_use": ((lambda s: s.irrigation, False, shown_water) if irrigated
                      else (lambda s: s.water_met, True, shown_water)),
        "profit": (lambda s: s.profit, True, lambda s: round(float(s.profit.mean()))),
        "failed": (lambda s: s.failed, False, lambda s: _pct(float(s.failed.mean()))),
    }
    # yours (column 0) plants nothing: it is not compared, so it gets no mark and cannot take one from ours
    compared = [(j, side) for j, side in enumerate(sides) if not (plants_nothing and j == 0)]
    marks = {}
    for key, (values, higher, shown) in rows.items():
        best = []
        for j, side in compared:
            beaten = any(
                k != j and shown(other) != shown(side)
                and versus(values(side), values(other), higher)["verdict"] == "ours"
                for k, other in compared)
            if not beaten:
                best.append(j)
        marks[key] = best if len(best) < len(compared) else []
    top = max(round(side.robust, 6) for _, side in compared)
    best = [j for j, side in compared if round(side.robust, 6) == top]
    marks["robust"] = best if len(best) < len(compared) else []
    return marks


# --- words about the whole comparison ----------------------------------------------------------------

def _list(words: Sequence[str]) -> str:
    words = list(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def nothing_planted(cycle: Sequence[str]) -> bool:
    """True when no year of the rotation plants a crop (fallow every year)."""
    return all(crop == "fallow" for crop in cycle)


def nothing_planted_sentence(profit: float, bare_days: float) -> str:
    """The one sentence a rotation with no planted crop gets instead of a rank. `profit` is its return
    over direct costs ($ an acre a year: minus the fallow charge), `bare_days` its bare days a year."""
    cost = round(abs(profit))
    charge = f" (it costs about ${cost:,.0f} an acre a year to keep fallow)" if profit <= -0.5 else ""
    return (f"This rotation plants no crop in any year, so it returns nothing{charge} and leaves the soil bare "
            f"about {_days(bare_days):.0f} days a year; it is not ranked or compared with our plans as a cropping plan.")


def headline(yours: dict, pick: int | None, candidates: int, overall: dict, rows: list) -> list:
    """Two or three plain sentences: what the rotation is to us, where it ranks, and who is ahead."""
    rank, of = yours["rank"], yours["rank_of"]
    if pick == 1:
        return ["Yours is our first pick for this field with these priorities."]
    if pick is not None:
        first = f"Yours is our pick number {pick}: it ranks number {rank} of the {candidates} rotations we compare."
    elif yours["is_candidate"]:
        first = f"Yours is one of the {candidates} rotations we compare and ranks number {rank} with these priorities."
    else:
        why = ("it breaks what our plans avoid, listed below" if yours["rules_broken"]
               else f"we compare 3- and 4-year cycles and this one runs {len(yours['cycle'])} years")
        if rank == 1:
            place = f"it would come first, ahead of all {candidates}"
        elif rank > candidates:
            place = f"it would come after all {candidates}"
        else:
            place = f"it would be number {rank} of {of}"
        first = (f"Yours is not one of the {candidates} rotations we compare ({why}). Scored the same way, {place} "
                 "with these priorities.")
    out = [first]
    n = overall["total"]
    if overall["verdict"] == "ours":
        out.append(f"Our first pick scores higher overall in all {n} climate models.")
    elif overall["verdict"] == "yours":
        out.append(f"Yours scores higher overall than our first pick in all {n} climate models.")
    elif overall["verdict"] == "too_close":
        out.append(f"Overall it is too close to call against our first pick: yours scores higher in "
                   f"{overall['yours_ahead']} of the {n} climate models, ours in {overall['ours_ahead']}.")
    else:
        out.append("Overall it scores the same as our first pick.")
    ours = [ROW_WORDS[r["key"]] for r in rows if r["verdict"] == "ours"]
    theirs = [ROW_WORDS[r["key"]] for r in rows if r["verdict"] == "yours"]
    if ours and theirs:
        out.append(f"Ours is ahead on {_list(ours)}; yours is ahead on {_list(theirs)}.")
    elif ours:
        out.append(f"Ours is ahead on {_list(ours)}; yours is not ahead on anything.")
    elif theirs:
        out.append(f"Yours is ahead on {_list(theirs)}; ours is not ahead on anything.")
    return out


def priority_places(ev: Evaluation, mine: Evaluation, m: int, is_candidate: bool) -> tuple:
    """([{priority, rank, of, first, tied}], [sentences]): where the rotation stands among the candidates
    when one priority is all that matters."""
    n = len(ev.plans.cycles)
    places = []
    for name in config.SCORE_NAMES:
        only = Weights(**{key: (1.0 if key == name else 0.0) for key in config.SCORE_NAMES})
        keys = np.round(ev.robust(only), 6)
        value = round(float(mine.robust(only)[m]), 6)
        rank = 1 + int((keys > value).sum())
        tied = int((keys == value).sum()) - (1 if is_candidate else 0)
        places.append({"priority": name, "rank": rank, "of": n if is_candidate else n + 1,
                       "first": rank == 1, "tied": tied})
    sentences = []
    for place in places:
        if place["first"]:
            level = ""
            if place["tied"] == 1:
                level = " (level with 1 of ours)"
            elif place["tied"] > 1:
                level = f" (level with {place['tied']} of ours)"
            sentences.append(f"Yours comes first if {PRIORITY_WORDS[place['priority']]} is all that matters{level}.")
    if not sentences:
        best = min(places, key=lambda place: place["rank"])
        sentences.append(
            f"Yours does not come first under any single priority. It gets closest when "
            f"{PRIORITY_WORDS[best['priority']]} is all that matters: number {best['rank']} of {best['of']}.")
    return places, sentences


# --- the entry point ---------------------------------------------------------------------------------

def score_rotation(field: FieldInput, rotation: Sequence[str], *, weights: Weights | Mapping | None = None,
                   scenario: str = config.DEFAULT_SCENARIO, horizon: int = config.TODAY,
                   irrigated: bool | None = None, top: int = config.TOP_PLANS,
                   traits: FieldTraits | None = None, engine: Engine | None = None) -> dict:
    """The body of GET /field/{id}/score: the rotation scored like our plans, our top picks for the same
    settings, and the comparison with our first pick. The arguments are those of `rank_plans_smart`;
    `rotation` is 2 to 6 of the six crop names (RotationError otherwise; ValueError for bad settings)."""
    cycle = check_cycle(rotation)
    engine = engine if engine is not None else smart.get_engine()
    result = smart.rank_plans_smart(field, weights=weights, scenario=scenario, horizon=horizon,
                                    irrigated=irrigated, top=top, traits=traits, engine=engine)
    weights = Weights.of(weights).normalised()
    is_irrigated = bool(result.settings["irrigated"])
    traits = traits if traits is not None else engine.traits_for(field.field_id)
    field_numbers = {"wei": traits.wei if traits is not None else None,
                     "weg": traits.weg if traits is not None else None, "slope_pct": field.slope_pct}

    ev = engine.evaluate(is_irrigated, scenario, result.settings["horizon"], **field_numbers)
    keys = np.round(ev.robust(weights), 6)
    order = sorted(range(len(keys)), key=lambda i: (-keys[i], i))
    index = {plan: i for i, plan in enumerate(ev.plans.cycles)}
    n_candidates = len(ev.plans.cycles)

    form = normal_form(cycle)
    at = index.get(form) if len(form) in cand.CYCLE_LENGTHS and cand.allowed(form, is_irrigated) else None
    if at is not None:                 # one of our candidates: its own numbers and its own place
        mine, m, rank, broken = ev, at, order.index(at) + 1, []
    else:
        mine = engine.evaluate(is_irrigated, scenario, result.settings["horizon"], rotations=[form],
                               max_years=MAX_YEARS, **field_numbers)
        m = 0
        rank = 1 + int((keys > round(float(mine.robust(weights)[0]), 6)).sum())
        broken = cand.rules_broken(form, is_irrigated, gentle=True, any_length=True)

    pick_at = [index[tuple(plan.cycle)] for plan in result.plans]
    pick = pick_at.index(at) + 1 if at in pick_at else None
    scored = smart._plan_result(mine, m, weights, rank, is_irrigated,
                                effects=engine.rotation.pairs_in(form, "irrigated" if is_irrigated else "dryland"))
    yours = replace(scored, name=cycle_name(cycle), cycle=list(cycle), rotation=list(cand.show(cycle))).to_dict()
    yours.update({
        "rules_broken": broken,
        "is_candidate": at is not None,
        "same_as_pick": pick,
        "rank_of": n_candidates if at is not None else n_candidates + 1,
        "scored_as": list(form),
    })

    side = _Side(mine, m, weights)
    ours = [_Side(ev, i, weights) for i in pick_at]
    nothing = nothing_planted(cycle)
    notes = [CYCLE_NOTE]
    if not is_irrigated:
        notes.append(DRYLAND_MONEY_NOTE)
    yours["rankable"] = not nothing
    if nothing:
        # no crop in any year: not a cropping plan, so no rank, no comparison and no "would come first"
        yours.update({"rank": None, "rank_of": None})
        comparison = {
            "against": result.plans[0].name, "verdict": "not_compared", "models": None,
            "headline": [nothing_planted_sentence(float(side.profit.mean()), side.bare)],
            "rows": [], "priorities": [], "priority_sentences": [], "notes": notes,
        }
    else:
        rows = compare_rows(side, ours[0], is_irrigated)
        overall = versus(side.overall, ours[0].overall, same=pick == 1)
        places, place_sentences = priority_places(ev, mine, m, at is not None)
        comparison = {
            "against": result.plans[0].name,
            "verdict": overall["verdict"],
            "models": {"yours_ahead": overall["yours_ahead"], "ours_ahead": overall["ours_ahead"],
                       "total": overall["total"]},
            "headline": headline(yours, pick, n_candidates, overall, rows),
            "rows": rows,
            "priorities": places,
            "priority_sentences": place_sentences,
            "notes": notes,
        }
    return {
        "field_id": field.field_id,
        "engine": "smart",
        "understood": list(cycle),
        "settings": result.settings,
        "yours": yours,
        "plans": [plan.to_dict() for plan in result.plans],
        "candidates": n_candidates,
        "marks": table_marks([side, *ours], is_irrigated, plants_nothing=nothing),
        "marks_note": MARKS_NOTE,
        "comparison": comparison,
    }
