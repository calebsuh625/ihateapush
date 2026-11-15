"""Candidate plans: the crop cycles the planner may recommend, and the rules that filter them.

A plan is a CYCLE of crops that repeats: after its last year comes its first year again, and the
planner scores it in all its phases. So "what came before year 1" is the last year, the rules look
across that seam too, and two sequences that are the same cycle started in a different year
(corn-wheat-sorghum-fallow, wheat-sorghum-fallow-corn) are one plan.

Candidates are
* every 4-year cycle of the six crops: of the 6^4 = 1,296 sequences, 336 different cycles (this
  includes one crop every year and two crops alternating), and
* every 3-year cycle that is not just one crop every year (70 of them).

The 3-year cycles are a deliberate addition to the design note, which asked for 4-year sequences
only. The county's main dryland rotation is the three-year wheat-sorghum-fallow (about 3,000 of 7,255
fields), and it is not a 4-year cycle: written over four years (wheat, sorghum, fallow, wheat) it puts
wheat after wheat at the seam, so the no-wheat-after-wheat rule would throw it out and the planner
could never recommend, or fairly score, what most dryland farmers do. A 3-year plan is still shown
as the next four years (`show`), so every plan has four entries as before.

The rules (`RULES`): each is one predicate with one sentence. "One year in four" rules are read as a
share of the cycle (a 3-year cycle with a corn year grows corn one year in three, which is more than
one in four). `rules_broken(sequence, irrigated)` returns the sentences a cycle breaks. With
`cyclic=False` the years are read as they happened, not as a cycle (no seam, and an alfalfa run that
touches either end may be part of a longer stand): that is how a field's last four years are told
what they break. `cover crop` is not a candidate crop: there is no satellite, yield or history data
for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import Callable, Sequence

from api.planner.crops import CROPS

YEARS = 4                    # every plan is shown as its next four years
CYCLE_LENGTHS = (4, 3)       # the cycles that are candidates, in listing order


def _pairs(sequence: Sequence[str], cyclic: bool = True):
    """(previous crop, crop) for every year; with `cyclic`, year 1 follows the last year (the seam)."""
    n = len(sequence)
    return [(sequence[i - 1], sequence[i]) for i in range(0 if cyclic else 1, n)]


def _no_repeat(crop: str) -> Callable[..., bool]:
    return lambda sequence, cyclic=True: not any(
        prev == crop and this == crop for prev, this in _pairs(sequence, cyclic))


def _at_most_one_year_in_four(crop: str) -> Callable[..., bool]:
    return lambda sequence, cyclic=True: 4 * sum(1 for c in sequence if c == crop) <= len(sequence)


def _alfalfa_is_a_stand(sequence: Sequence[str], cyclic: bool = True) -> bool:
    """Alfalfa years form one unbroken run of at least three (or there is no alfalfa, or only alfalfa).

    Not cyclic: only a run with another crop on both sides inside the sequence can be judged (in four
    years it is at most two years long, so it breaks the rule). A run touching either end may be longer.
    """
    n = len(sequence)
    years = sum(1 for crop in sequence if crop == "alfalfa")
    if years == 0 or years == n:
        return True
    if cyclic:
        starts = sum(1 for prev, this in _pairs(sequence) if this == "alfalfa" and prev != "alfalfa")
        return years >= 3 and starts == 1
    runs = sum(1 for i in range(n) if sequence[i] == "alfalfa" and (i == 0 or sequence[i - 1] != "alfalfa"))
    touches_an_end = sequence[0] == "alfalfa" or sequence[-1] == "alfalfa"
    return runs == 1 and (touches_an_end or years >= 3)


@dataclass(frozen=True)
class Rule:
    key: str
    applies: str                  # "all", "irrigated" or "dryland"
    text: str                     # one plain sentence
    ok: Callable[..., bool]       # ok(sequence, cyclic=True)

    def applies_to(self, irrigated: bool) -> bool:
        return self.applies == "all" or self.applies == ("irrigated" if irrigated else "dryland")


RULES: tuple[Rule, ...] = (
    Rule("no_wheat_after_wheat", "all",
         "No winter wheat straight after winter wheat (disease and weeds build up).",
         _no_repeat("winter wheat")),
    Rule("no_fallow_after_fallow", "all",
         "No fallow straight after fallow (two bare years in a row).",
         _no_repeat("fallow")),
    Rule("no_soybeans_after_soybeans", "all",
         "No soybeans straight after soybeans (disease builds up and little residue is left).",
         _no_repeat("soybeans")),
    Rule("alfalfa_stand", "all",
         "Alfalfa only as a stand of three or more years in a row (it takes a year to establish).",
         _alfalfa_is_a_stand),
    Rule("dryland_no_alfalfa", "dryland",
         "No alfalfa without irrigation (rain covers well under half of what it needs here).",
         lambda sequence, cyclic=True: "alfalfa" not in sequence),
    Rule("dryland_no_soybeans", "dryland",
         "No soybeans without irrigation (dryland soybeans are hardly grown in the county).",
         lambda sequence, cyclic=True: "soybeans" not in sequence),
    Rule("dryland_one_corn_year", "dryland",
         "Without irrigation, at most one corn year in four.",
         _at_most_one_year_in_four("corn")),
    Rule("dryland_no_corn_after_corn", "dryland",
         "Without irrigation, no corn straight after corn.",
         _no_repeat("corn")),
    Rule("irrigated_one_fallow_year", "irrigated",
         "Under irrigation, at most one fallow year in four.",
         _at_most_one_year_in_four("fallow")),
)


def _check(sequence: Sequence[str]) -> tuple:
    sequence = tuple(sequence)
    if len(sequence) not in CYCLE_LENGTHS:
        raise ValueError(f"a plan is a cycle of {' or '.join(str(n) for n in sorted(CYCLE_LENGTHS))} years, "
                         f"got {len(sequence)}")
    unknown = [crop for crop in sequence if crop not in CROPS]
    if unknown:
        raise ValueError(f"unknown crop(s) {unknown}; plans use {', '.join(CROPS)}")
    return sequence


def rules_broken(sequence: Sequence[str], irrigated: bool, cyclic: bool = True) -> list[str]:
    """The sentences of the rules this sequence breaks on an irrigated / dryland field ([] = allowed).

    cyclic=True reads it as a plan (year 1 follows the last year); cyclic=False as years that happened.
    """
    sequence = _check(sequence)
    return [rule.text for rule in RULES if rule.applies_to(irrigated) and not rule.ok(sequence, cyclic)]


def allowed(sequence: Sequence[str], irrigated: bool) -> bool:
    sequence = _check(sequence)
    return all(rule.ok(sequence, True) for rule in RULES if rule.applies_to(irrigated))


def _shifts(sequence: tuple) -> list[tuple]:
    return [sequence[i:] + sequence[:i] for i in range(len(sequence))]


def canonical(sequence: Sequence[str]) -> tuple:
    """The one way a cycle is written, whichever year it was started in.

    It starts the year after fallow when there is a fallow year, at the first year of the alfalfa
    stand when there is one, and otherwise with the crop that comes first in the fixed crop order
    (corn, soybeans, winter wheat, sorghum).
    """
    sequence = _check(sequence)
    order = {crop: i for i, crop in enumerate(CROPS)}

    def key(shift: tuple):
        after_fallow = 0 if shift[-1] == "fallow" else 1
        stand_start = 0 if (shift[0] == "alfalfa" and shift[-1] != "alfalfa") or "alfalfa" not in shift else 1
        return (after_fallow, stand_start, tuple(order[crop] for crop in shift))

    return min(_shifts(sequence), key=key)


def show(cycle: Sequence[str]) -> tuple:
    """The plan's next four years: the cycle itself, or a 3-year cycle run on into a fourth year."""
    cycle = tuple(cycle)
    return tuple(cycle[i % len(cycle)] for i in range(YEARS))


@lru_cache(maxsize=None)
def all_cycles(length: int = YEARS) -> tuple:
    """Every different cycle of that length, before any rule, in a fixed order. 3-year cycles of one
    crop are left out: they are the same plan as that crop every year in the 4-year list."""
    seen = {canonical(seq) for seq in product(CROPS, repeat=length)}
    if length != YEARS:
        seen = {seq for seq in seen if len(set(seq)) > 1}
    return tuple(sorted(seen, key=lambda seq: tuple(CROPS.index(crop) for crop in seq)))


@lru_cache(maxsize=None)
def candidates(irrigated: bool) -> tuple:
    """The cycles the planner may recommend on an irrigated (True) or dryland (False) field:
    the 4-year ones first, then the 3-year ones, each group in a fixed order."""
    return tuple(cycle for length in CYCLE_LENGTHS for cycle in all_cycles(length) if allowed(cycle, bool(irrigated)))


def crop_shares(cycle: Sequence[str]) -> dict:
    """{crop: share of the cycle's years}."""
    n = len(cycle)
    return {crop: sum(1 for c in cycle if c == crop) / n for crop in CROPS}


def years_different(a: Sequence[str], b: Sequence[str]) -> float:
    """How many years in four grow a different crop, comparing each crop's share of the cycle and
    ignoring order. For two 4-year cycles it is a whole number 0-4."""
    sa, sb = crop_shares(a), crop_shares(b)
    return YEARS * sum(abs(sa[crop] - sb[crop]) for crop in CROPS) / 2


def matching_candidate(last_years: Sequence[str], irrigated: bool, before: str | None = None) -> tuple | None:
    """The candidate cycle that four years in a row are a stretch of, or None.

    Two readings are possible. As a 3-year cycle, when year 4 repeats year 1 and years 1-3 are an
    allowed cycle (wheat, sorghum, fallow, wheat -> wheat-sorghum-fallow). As a 4-year cycle, when the
    four years are allowed as one. When both fit (sorghum, fallow, wheat, sorghum is a stretch of
    wheat-sorghum-fallow and also the cycle wheat-sorghum-sorghum-fallow), the crop grown the year
    before decides: a 3-year cycle had year 3's crop there, a 4-year cycle had year 4's. Without that
    year, or when it fits neither, the 3-year reading wins (it does not need the same crop twice in a row).
    """
    last_years = tuple(last_years)
    if len(last_years) != YEARS or any(crop not in CROPS for crop in last_years):
        return None
    head = last_years[:3]
    three = (canonical(head) if last_years[3] == last_years[0] and len(set(head)) > 1 and allowed(head, irrigated)
             else None)
    four = canonical(last_years) if allowed(last_years, irrigated) else None
    if three is not None and four is not None:
        return four if (before == last_years[3] and before != last_years[2]) else three
    return three if three is not None else four


def describe_rules() -> list[dict]:
    """The rule list for docs and the API: key, who it applies to, sentence."""
    return [{"key": rule.key, "applies": rule.applies, "text": rule.text} for rule in RULES]
