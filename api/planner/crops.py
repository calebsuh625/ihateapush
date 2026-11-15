"""Crop agronomy constants for southwest Kansas (Finney County). Phase 3, piece B1.

Everything here is a textbook or local-statistics number with its source next to it. Nothing in this
file is fitted. The fitted numbers live in `crop_response.json` (written by
`python -m api.planner.calibrate`); when that file is missing, `response.py` runs from the constants
below, so a fresh clone always has a working (textbook) crop model.

Sources, by short name:

* FAO-56: Allen, Pereira, Raes, Smith (1998), "Crop evapotranspiration", FAO Irrigation and Drainage
  Paper 56. Table 11 (growth-stage lengths), Table 12 (single crop coefficients Kc for a grass
  reference), Table 24 (seasonal yield response factors Ky), Annex 6 (weather measured over dry
  land overstates reference ET).
* FAO-33: Doorenbos and Kassam (1979), "Yield response to water", FAO Irrigation and Drainage Paper 33:
  relative yield loss = Ky x relative ET shortfall. FAO-56 Table 24 repeats its Ky values.
* SCS: USDA Soil Conservation Service monthly effective-rainfall formula as used in FAO CROPWAT
  ("USDA S.C.S. method").
* K-State: Kansas State University Research and Extension crop production handbooks (corn C-560,
  grain sorghum C-687, wheat C-529, soybean C-449, alfalfa C-683) for planting and harvest dates in
  western Kansas, and its dryland research at Tribune and Garden City (e.g. Schlegel et al.) for how
  much off-season rain a fallow period stores.
* Nielsen and Vigil (2010), "Precipitation storage efficiency during fallow in wheat-fallow systems",
  Agronomy Journal 102: 537-543: 20-35 % of fallow-period precipitation is stored under reduced and
  no-till on the central Great Plains.
* NASS: USDA NASS yields for Finney County / the Kansas Southwest district 2000-2024
  (`data/nass_finney.parquet`, `data/logs/phase3/nass_summary.txt`).
* CDL: the fields' own crop history 2008-2024 (`data/fields.parquet`, USDA Cropland Data Layer)
  split by `data/field_traits.parquet` irrigation.
* Heat: Schlenker and Roberts (2009, PNAS 106: 15594) (corn and soybean yields fall steeply above
  29-30 C); Lobell et al. (2013, Nature Climate Change 3: 497) (extreme heat and corn); Tack, Barkley
  and Nalley (2015, PNAS 112: 6931) (Kansas wheat variety trials, spring heat above about 34 C);
  Tack, Lingenfelser and Jagadish (2017, PNAS 114: 9296) (Kansas sorghum, heat above about 33 C);
  Tack, Barkley and Hendricks (2017, Environmental Research Letters 12: 114027) (irrigation offsets
  much of the heat loss in Kansas wheat). These papers give losses per degree-day, not per "day above
  35 C" of the warm NASA POWER series we use, so the per-day starting values below are our own rounded
  reading of them (order of magnitude), and that is exactly why they are the calibrated parameters.

The six crop names are fixed project-wide.
"""
from __future__ import annotations

from dataclasses import dataclass

CROPS = ("corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow")
PRACTICES = ("irrigated", "dryland")

# What a crop is to the crop that FOLLOWS it (decides how long the soil can collect rain before planting).
#   "summer": harvested in autumn (September-October)     corn, soybeans, sorghum, alfalfa
#   "wheat" : harvested in June, so the next summer crop gets about ten months of stored rain
#   "fallow": no crop that year, so the next crop gets a further twelve months
PREV_KINDS = ("summer", "wheat", "fallow")


@dataclass(frozen=True)
class Crop:
    name: str
    unit: str                       # yield unit
    season: tuple                   # ((year offset, calendar month), ...) from planting to harvest; offset -1 = year before harvest
    kc: tuple                       # FAO-56 crop coefficient for each season month (same order as `season`)
    ky: float                       # FAO-33 seasonal yield response factor
    heat_column: str                # column of the yearly climate table that counts this crop's hot days
    heat_loss: dict                 # share of yield lost per hot day, by practice (textbook starting value)
    normal_yield: dict              # yield in a normal year, by practice (yield unit per acre)
    scale: dict                     # textbook-model level so that the observed 2000-2024 mean of yield_rel is 1
    typical_prev: dict              # {practice: {prev kind: share}}: what usually precedes this crop in the county
    as_prev: str = "summer"         # this crop's PREV_KINDS class when it is the previous crop
    notes: str = ""

    @property
    def months(self) -> tuple:
        return tuple(m for _, m in self.season)


def _season(first_month: int, last_month: int, start_offset: int = 0) -> tuple:
    """((offset, month), ...) from first_month to last_month, crossing New Year when start_offset is -1."""
    out, off, m = [], start_offset, first_month
    while True:
        out.append((off, m))
        if off == 0 and m == last_month:
            return tuple(out)
        m += 1
        if m == 13:
            m, off = 1, off + 1


# --- Kc by month -------------------------------------------------------------------------------------
# FAO-56 Table 12 gives Kc for the initial, middle and end stages; Table 11 gives stage lengths. We laid
# those four stages on the western Kansas calendar (K-State planting and harvest dates) and averaged the
# daily Kc curve within each calendar month. A month the crop occupies only partly carries the share.
#
# corn:     Kc 0.30 / 1.20 / 0.60 (grain, harvested at high moisture); planted about 1 May;
#           stages 30 / 40 / 50 / 30 days (FAO-56 Table 11, maize grain, 150 days).
# soybeans: Kc 0.40 / 1.15 / 0.50; planted about 20 May; stages 20 / 30 / 60 / 25 days.
# sorghum:  Kc 0.30 / 1.05 / 0.55 (FAO-56 gives 1.00-1.10 for the middle stage); planted about 1 June;
#           stages 20 / 35 / 45 / 30 days; harvested in early October.
# winter wheat: Kc 0.40 while small or dormant (FAO-56: 0.4 where the ground freezes), 1.15 in the
#           middle stage, 0.25 at harvest; planted about 1 October, green-up in March, heading in May,
#           harvested about 25 June.
# alfalfa:  Kc 0.95 over a season of cuttings (FAO-56 "alfalfa hay, averaged cutting effects"), 0.40
#           when dormant; green-up in April, last cutting in early October. An established stand.
_KC = {
    "corn": ((5, 0.30), (6, 0.66), (7, 1.17), (8, 1.19), (9, 0.85)),
    "soybeans": ((5, 0.15), (6, 0.58), (7, 1.12), (8, 1.15), (9, 0.92)),
    "sorghum": ((6, 0.33), (7, 0.82), (8, 1.05), (9, 0.90), (10, 0.15)),
    "winter wheat": ((10, 0.50), (11, 0.50), (12, 0.40), (1, 0.40), (2, 0.40), (3, 0.70), (4, 1.00), (5, 1.15),
                     (6, 0.60)),
    "alfalfa": ((4, 0.70), (5, 0.95), (6, 0.95), (7, 0.95), (8, 0.95), (9, 0.95), (10, 0.60)),
}

# --- Ky ----------------------------------------------------------------------------------------------
# FAO-33 / FAO-56 Table 24, whole-season values.
_KY = {"corn": 1.25, "soybeans": 0.85, "sorghum": 0.90, "winter wheat": 1.05, "alfalfa": 1.10}

# --- heat --------------------------------------------------------------------------------------------
# Share of yield lost per hot day: heat factor = 1 - loss x days, never below 0.
# Summer crops and alfalfa count growing-season (April-September) days above 35 C (`hot_days`, about
# 45 a year in the observed NASA POWER series, which runs warm); winter wheat counts May-June days
# above 32 C (`wheat_hot_days`, about 21 a year: grain fill). Starting values, see "Heat" above:
# corn loses most, sorghum least, irrigated crops about half as much as dryland ones (a watered
# canopy cools itself).
_HEAT_COLUMN = {"winter wheat": "wheat_hot_days"}
_HEAT_LOSS = {
    "corn": {"dryland": 0.006, "irrigated": 0.003},
    "soybeans": {"dryland": 0.005, "irrigated": 0.0025},
    "sorghum": {"dryland": 0.004, "irrigated": 0.002},
    "winter wheat": {"dryland": 0.010, "irrigated": 0.005},
    "alfalfa": {"dryland": 0.002, "irrigated": 0.001},
}

# --- hot days beyond the record ----------------------------------------------------------------------
# The heat loss per day was fitted (or assumed) on the hot days of the 2000-2024 record: 25 to 75
# growing-season days above 35 C, 2 to 33 May-June days above 32 C. The climate models go well past
# that (one run averages 101 summer hot days in 2050-2060), where a straight line per day is a guess.
# So the hot days fed to the heat factor are capped at the record's maximum plus a margin: beyond
# 75 + 10 summer days (33 + 5 for wheat) a year counts as that many and no more. The maxima below are
# fallbacks: `python -m api.planner.calibrate` writes the record's own maxima into crop_response.json
# (`heat_cap`), and the planner reads them again from data/climate_history.parquet when it is there.
# The margins are ours: about one year-to-year standard deviation (14 and 8 days) or less.
HOT_DAYS_OBSERVED_MAX = {"hot_days": 75.0, "wheat_hot_days": 33.0}
HOT_DAYS_MARGIN = {"hot_days": 10.0, "wheat_hot_days": 5.0}

# --- normal yields -----------------------------------------------------------------------------------
# NASS medians 2000-2024 by practice (bu/acre; alfalfa tons/acre): corn, wheat 2000-2018/19, soybeans
# 2000-2016 and sorghum 2000-2009 are the years NASS split by practice. Alfalfa has no split: 5.3 is
# the "all practices" median, and nearly all alfalfa here is irrigated (CDL: 97 % of alfalfa area), so
# it stands for irrigated; dryland alfalfa (rare) is set to 40 % of it, our assumption, not a statistic.
# `crop_response.json` replaces these with the detrended calibration levels.
_NORMAL = {
    "corn": {"irrigated": 198.0, "dryland": 62.0},
    "soybeans": {"irrigated": 56.0, "dryland": 19.0},
    "sorghum": {"irrigated": 98.0, "dryland": 61.5},
    "winter wheat": {"irrigated": 56.0, "dryland": 35.0},
    "alfalfa": {"irrigated": 5.3, "dryland": 2.1},
}

# --- level of the textbook model ---------------------------------------------------------------------
# yield_rel = scale x heat factor x water factor. `scale` is 1 / (observed 2000-2024 mean of heat x
# water) with the textbook Ky and heat loss above, so that a normal year gives 1. Printed by
# `python -m api.planner.calibrate` ("textbook scale" lines in its log) and pasted here; the JSON
# carries the calibrated ones.
_SCALE = {
    "corn": {"irrigated": 1.155, "dryland": 3.354},
    "soybeans": {"irrigated": 1.126, "dryland": 2.129},
    "sorghum": {"irrigated": 1.099, "dryland": 1.723},
    "winter wheat": {"irrigated": 1.119, "dryland": 1.889},
    "alfalfa": {"irrigated": 1.047, "dryland": 2.993},
}

# --- what usually comes before each crop -------------------------------------------------------------
# Share of the crop's area by what grew on the field the year before (CDL 2009-2024, by irrigation
# guess; "other" crops counted as summer crops). The county yields we calibrate against are this mix:
# dryland wheat is almost always planted after fallow and dryland sorghum after wheat (the
# wheat-sorghum-fallow rotation), so their county yields already include the stored water.
_TYPICAL_PREV = {
    "corn": {"dryland": {"summer": 0.52, "wheat": 0.45, "fallow": 0.03},
             "irrigated": {"summer": 0.74, "wheat": 0.26, "fallow": 0.00}},
    "soybeans": {"dryland": {"summer": 0.72, "wheat": 0.28, "fallow": 0.00},
                 "irrigated": {"summer": 0.93, "wheat": 0.07, "fallow": 0.00}},
    "sorghum": {"dryland": {"summer": 0.16, "wheat": 0.80, "fallow": 0.04},
                "irrigated": {"summer": 0.77, "wheat": 0.22, "fallow": 0.01}},
    "winter wheat": {"dryland": {"summer": 0.07, "wheat": 0.07, "fallow": 0.86},
                     "irrigated": {"summer": 0.73, "wheat": 0.23, "fallow": 0.04}},
    # an alfalfa stand follows itself; "summer" = the stand only collects the winter's rain
    "alfalfa": {"dryland": {"summer": 1.0, "wheat": 0.0, "fallow": 0.0},
                "irrigated": {"summer": 1.0, "wheat": 0.0, "fallow": 0.0}},
}

_SEASON = {
    "corn": _season(5, 9),
    "soybeans": _season(5, 9),
    "sorghum": _season(6, 10),
    "winter wheat": _season(10, 6, start_offset=-1),
    "alfalfa": _season(4, 10),
}

_UNIT = {"alfalfa": "tons/acre"}


def _crop(name: str) -> Crop:
    season = _SEASON[name]
    kc = tuple(k for _, k in _KC[name])
    assert tuple(m for _, m in season) == tuple(m for m, _ in _KC[name]), name
    return Crop(
        name=name, unit=_UNIT.get(name, "bu/acre"), season=season, kc=kc, ky=_KY[name],
        heat_column=_HEAT_COLUMN.get(name, "hot_days"), heat_loss=dict(_HEAT_LOSS[name]),
        normal_yield=dict(_NORMAL[name]), scale=dict(_SCALE[name]), typical_prev=_TYPICAL_PREV[name],
        as_prev="wheat" if name == "winter wheat" else "summer",
    )


CROP_TABLE: dict[str, Crop] = {name: _crop(name) for name in CROPS if name != "fallow"}

# Fallow grows nothing: no yield, no water need, no irrigation, and it cannot fail. What it does is
# let the soil collect rain for the next crop (see water.py, "stored soil water").
CROP_TABLE["fallow"] = Crop(
    name="fallow", unit="", season=(), kc=(), ky=0.0, heat_column="hot_days",
    heat_loss={p: 0.0 for p in PRACTICES}, normal_yield={p: 0.0 for p in PRACTICES},
    scale={p: 1.0 for p in PRACTICES},
    typical_prev={p: {"summer": 1.0, "wheat": 0.0, "fallow": 0.0} for p in PRACTICES},
    as_prev="fallow", notes="no crop",
)

# A crop "fails" in a year when its modelled yield is below this share of its normal yield.
FAIL_BELOW = 0.5

# No year is credited with more than twice the normal yield. The best real years of the record reached
# 1.5-1.8 times normal (dryland corn 2009, dryland wheat 2016); the climate models contain a few wetter,
# cooler years than anything observed, and there the formula would promise dryland corn three times
# its normal yield. A guard against extrapolation, not a fitted number.
MAX_YIELD_REL = 2.0

# Bounds for the calibration (calibrate.py). A fitted value on a bound is not trusted.
KY_BOUNDS = (0.2, 2.0)
HEAT_LOSS_BOUNDS = (0.0, 0.012)   # per summer hot day; 0.012 would wipe a crop out at 83 hot days
WHEAT_HEAT_LOSS_BOUNDS = (0.0, 0.025)   # per May-June day above 32 C (0.025 = nothing left at 40 days)


def prev_kind(prev_crop: str | None) -> str:
    """'summer', 'wheat' or 'fallow' for a previous crop name; 'typical' when it is unknown (None)."""
    if prev_crop is None:
        return "typical"
    if prev_crop not in CROP_TABLE:
        raise KeyError(f"unknown crop {prev_crop!r}; expected one of {CROPS}")
    return CROP_TABLE[prev_crop].as_prev
