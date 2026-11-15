"""Classify a field's crop history into a rotation type.

    rotation_type(history, 2016, 2024) -> "continuous corn" | "corn-soy" | "wheat-sorghum-fallow"
                                          | "diverse" | "alfalfa/perennial" | "other"

Rules are checked in this order; the first that fits wins. `n` is the number of years with a
crop on record in the window (a 9-year window: 2016-2024).

1. alfalfa/perennial     >= 2 years of alfalfa or another perennial (hay, grass/pasture). A single
                         grass/pasture year does not count: the 2024 CDL over-maps it (README).
2. continuous corn       corn in >= 75% of the years
3. corn-soy              corn + soybeans in >= 75% of the years, each at least 2 years
4. wheat-sorghum-fallow  winter wheat + sorghum + fallow in >= 75% of the years, with wheat >= 2
                         years and fallow >= 1 year (the dryland system; wheat-fallow counts too)
5. diverse               >= 3 different crops, each grown >= 2 years
6. other                 everything else (also: fewer than 5 years on record)

`crop_mix` is the share of years in each crop group, used to compare 2008-2015 histories.
"""
from __future__ import annotations

from collections import Counter

ROTATION_TYPES = [
    "alfalfa/perennial",
    "diverse",
    "corn-soy",
    "wheat-sorghum-fallow",
    "continuous corn",
    "other",
]  # also the order comparisons are written in: "A vs B" has A earlier in this list
COMPARABLE_TYPES = [t for t in ROTATION_TYPES if t != "other"]

PERENNIALS = {"alfalfa", "other hay", "other hay/non alfalfa", "grass/pasture", "clover/wildflowers", "sod/grass seed"}
MIX_GROUPS = ["corn", "soybeans", "winter wheat", "sorghum", "fallow", "perennial", "other"]
SHARE = 0.75
MIN_YEARS = 5


def _crop_group(crop: str) -> str:
    """Collapse a CDL name into one of MIX_GROUPS. Double crops count as their first crop."""
    crop = crop.strip().lower()
    if crop.startswith("double crop:"):
        crop = crop.split(":", 1)[1].split("/")[0].strip()
    if crop in PERENNIALS:
        return "perennial"
    if crop in {"corn", "soybeans", "winter wheat", "sorghum", "fallow"}:
        return crop
    if crop in {"fallow/idle cropland"}:
        return "fallow"
    return "other"


def window(history: dict[str, str], first: int, last: int) -> list[str]:
    """Crop groups for the years in [first, last] that are on record, in year order."""
    return [_crop_group(history[str(y)]) for y in range(first, last + 1) if str(y) in history]


def rotation_type(history: dict[str, str], first: int, last: int) -> str:
    crops = window(history, first, last)
    n = len(crops)
    if n < MIN_YEARS:
        return "other"
    c = Counter(crops)
    if c["perennial"] >= 2:
        return "alfalfa/perennial"
    if c["corn"] >= SHARE * n:
        return "continuous corn"
    if c["corn"] + c["soybeans"] >= SHARE * n and c["corn"] >= 2 and c["soybeans"] >= 2:
        return "corn-soy"
    if (
        c["winter wheat"] + c["sorghum"] + c["fallow"] >= SHARE * n
        and c["winter wheat"] >= 2
        and c["fallow"] >= 1
    ):
        return "wheat-sorghum-fallow"
    if sum(1 for group, k in c.items() if group != "other" and k >= 2) >= 3:
        return "diverse"
    return "other"


def crop_mix(history: dict[str, str], first: int, last: int) -> list[float]:
    """Share of years in each of MIX_GROUPS (sums to 1; all zeros if no year is on record)."""
    crops = window(history, first, last)
    if not crops:
        return [0.0] * len(MIX_GROUPS)
    c = Counter(crops)
    return [c[g] / len(crops) for g in MIX_GROUPS]


def comparison_key(a: str, b: str) -> tuple[str, str]:
    """(A, B) in ROTATION_TYPES order, so each comparison has one name and one sign."""
    return (a, b) if ROTATION_TYPES.index(a) <= ROTATION_TYPES.index(b) else (b, a)
