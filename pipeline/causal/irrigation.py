"""Infer irrigated vs dryland per field, from its shape and its 2008-2015 crops only.

Only the history period is used on purpose: the 2016-2024 crops are the "treatment", and a
stratum defined from them would bias the comparison.

Signals
-------
* circle fill = field area / area of its smallest enclosing circle. A centre-pivot circle is
  close to 1 (about 0.9 once the 30 m outline steps are counted); a square quarter section is
  0.64. Field outlines that trace a pivot circle are the strongest irrigation sign we have.
* water-hungry crops = share of years in corn, soybeans or alfalfa. In Finney County these are
  grown almost only under irrigation.
* fallow = share of years left fallow. Fallow is the dryland moisture-saving practice.

Rule (first match wins)
-----------------------
1. irrigated   circle fill >= 0.85 and fallow share < 0.25          ("pivot circle")
2. dryland     fallow share >= 0.25                                 ("fallow in history")
3. irrigated   water-hungry share >= 0.5 and fallow share <= 0.125  ("irrigated crops")
4. dryland     water-hungry share <= 0.125                          ("dryland crops")
5. uncertain   everything else (left out of the matching)
"""
from __future__ import annotations

import numpy as np
import shapely

from pipeline.causal.rotations import window

PIVOT_FILL = 0.85
FALLOW_DRY = 0.25
THIRSTY_IRRIGATED = 0.5
FALLOW_IRRIGATED_MAX = 0.125
THIRSTY_DRY_MAX = 0.125


def circle_fill(geoms_equal_area: np.ndarray) -> np.ndarray:
    """Area / area of the minimum bounding circle, per polygon (equal-area CRS, metres)."""
    radius = shapely.minimum_bounding_radius(geoms_equal_area)
    with np.errstate(divide="ignore", invalid="ignore"):
        fill = shapely.area(geoms_equal_area) / (np.pi * radius**2)
    return np.nan_to_num(fill, nan=0.0)


def classify(fill: float, history: dict[str, str], first: int, last: int) -> tuple[str, str]:
    """(irrigation, reason) for one field."""
    crops = window(history, first, last)
    n = max(len(crops), 1)
    thirsty = sum(c in ("corn", "soybeans") for c in crops) + sum(
        h == "alfalfa" for y, h in history.items() if first <= int(y) <= last
    )
    thirsty /= n
    fallow = sum(c == "fallow" for c in crops) / n
    if fill >= PIVOT_FILL and fallow < FALLOW_DRY:
        return "irrigated", "pivot circle"
    if fallow >= FALLOW_DRY:
        return "dryland", "fallow in history"
    if thirsty >= THIRSTY_IRRIGATED and fallow <= FALLOW_IRRIGATED_MAX:
        return "irrigated", "irrigated crops"
    if thirsty <= THIRSTY_DRY_MAX:
        return "dryland", "dryland crops"
    return "uncertain", "mixed signals"
