"""Wind erosion risk index for a plan on a field in a climate window.

    index = bare share of the year  x  soil factor  x  climate factor  x  knoll factor

It follows the shape of the USDA Wind Erosion Equation (WEQ: erosion = f(soil erodibility I, ridge
roughness K, climate C, field length L, vegetative cover V)), keeping the parts we have data for.
It is a RELATIVE index, not tons of soil: 1.0 means "the county's commonest soil, bare all year, on
flat ground, in the 2000-2024 wind climate". Typical plans sit between 0.02 (an alfalfa stand) and
0.4 (wheat-fallow on a loam); sandy fields go higher. Capped at `INDEX_CAP`.

* bare share   = the plan's mean bare days a year / 365 (soil_effect.py). Stands in for WEQ's cover V.
* soil factor  = the field's SSURGO wind erodibility index `wei` (WEQ's I, tons/acre/year) divided by
                 `WEI_REFERENCE` = 56, the value of 43% of the county's fields (loams, group 5 and 6).
                 When `wei` is missing it is read from the wind erodibility group (`WEG_TO_WEI`, the
                 NRCS National Agronomy Manual table); when both are missing the factor is 1 (neutral).
* climate factor = `erosion_climate` / 100, averaged over the climate window. `erosion_climate` is
                 WEQ's C (wind speed cubed over effective moisture squared), scaled so the observed
                 2000-2024 mean is 100. Single years are very spiky (40 to 300), so only window means
                 are ever used. A drier, windier future raises it.
* knoll factor = 1 up to a 2% mean slope, then +0.15 per extra percent, capped at 1.5 (reached at
                 5.3%). WEQ raises I on windward knolls (National Agronomy Manual knoll table, quoted
                 from memory and not re-checked: about x1.3 at 3% slope, x1.6 at 4%, x1.9 at 5%); our
                 slope is the mean of the whole field, not of its knolls, so we take half that rise.
                 Modest on purpose: 90% of the county's fields are under 3%. A missing slope is
                 neutral (1.0).

Roughness (K) and field length (L) are left out: we do not know the tillage or the wind fetch.
"""
from __future__ import annotations

import math

WEI_REFERENCE = 56.0
# NRCS National Agronomy Manual, wind erodibility groups -> soil erodibility index I (tons/acre/year).
# Group 1 spans 160-310 depending on the sand; 220 is its usual middle value. Only used when `wei` is
# missing, which is no field in the current field_traits.parquet (its values: 48, 56, 86, 134, 250).
WEG_TO_WEI = {"1": 220.0, "2": 134.0, "3": 86.0, "4": 86.0, "4L": 86.0, "5": 56.0, "6": 48.0, "7": 38.0, "8": 0.0}
CLIMATE_REFERENCE = 100.0
KNOLL_FLAT_PCT = 2.0
KNOLL_PER_PCT = 0.15
KNOLL_CAP = 1.5
INDEX_CAP = 3.0
DAYS_IN_YEAR = 365.0


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def soil_factor(wei=None, weg=None) -> float:
    """wei / 56; from the group when wei is missing; 1.0 when neither is known."""
    value = _number(wei)
    if value is None and weg is not None:
        value = WEG_TO_WEI.get(str(weg).strip().upper())
    if value is None:
        return 1.0
    return max(0.0, value) / WEI_REFERENCE


def knoll_factor(slope_pct=None) -> float:
    slope = _number(slope_pct)
    if slope is None or slope <= KNOLL_FLAT_PCT:
        return 1.0
    return min(KNOLL_CAP, 1.0 + KNOLL_PER_PCT * (slope - KNOLL_FLAT_PCT))


def climate_factor(erosion_climate) -> float:
    """Window-mean `erosion_climate` / 100. Works on a number or a numpy array."""
    return erosion_climate / CLIMATE_REFERENCE


def field_factor(wei=None, weg=None, slope_pct=None) -> float:
    """The part of the index that belongs to the field: soil factor x knoll factor."""
    return soil_factor(wei, weg) * knoll_factor(slope_pct)


def erosion_index(bare_days, erosion_climate, wei=None, weg=None, slope_pct=None):
    """The index for a plan's mean bare days a year and a window-mean `erosion_climate`.

    `bare_days` and `erosion_climate` may be numbers or numpy arrays (they broadcast).
    """
    import numpy as np

    share = np.clip(np.asarray(bare_days, dtype=float) / DAYS_IN_YEAR, 0.0, 1.0)
    index = share * field_factor(wei, weg, slope_pct) * climate_factor(np.asarray(erosion_climate, dtype=float))
    index = np.clip(index, 0.0, INDEX_CAP)
    return float(index) if index.ndim == 0 else index
