"""Settings of the data-driven planner (Phase 3), all in one place.

Two kinds of things live here:

* what the environment decides: which soil-effect source to use and where the data files are.
  `settings()` reads the environment each time it is called, so a test (or a restart with other
  variables) gets what it asked for. Same override style as api/main.py: `$<NAME>_PATH` wins,
  otherwise the file under data/.

      SOIL_EFFECT_SOURCE      crop_average (default) | effects
      CLIMATE_FUTURES_PATH    data/climate_futures.parquet
      CLIMATE_MONTHLY_PATH    data/climate_monthly.parquet
      CLIMATE_HISTORY_PATH    data/climate_history.parquet
      FIELD_TRAITS_PATH       data/field_traits.parquet
      SOIL_HEALTH_PATH        data/soil_health.parquet
      PLANNER_FIELDS_PATH     data/fields.parquet   (crop history for the bare-day table; the real fields)
      EFFECTS_PATH            data/effects.json
      NASS_PATH               data/nass_finney.parquet
      PLANNER_SOIL_CACHE_PATH data/planner_bare_days.json
      ECONOMICS_PATH          api/planner/economics.json

* the planner's own numbers: default weights, the window rule, the reference scales of the four
  scores, the holds-up rule, the ranking blend. They are constants, not fitted, and each one has
  its reason next to it. Changing one changes every answer, so they are here and nowhere else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
PLANNER_DIR = Path(__file__).resolve().parent

SOIL_SOURCES = ("crop_average", "effects")
DEFAULT_SOIL_SOURCE = "crop_average"

_PATHS = {
    # attribute            (environment variable,        default)
    "climate_futures_path": ("CLIMATE_FUTURES_PATH", DATA_DIR / "climate_futures.parquet"),
    "climate_monthly_path": ("CLIMATE_MONTHLY_PATH", DATA_DIR / "climate_monthly.parquet"),
    "climate_history_path": ("CLIMATE_HISTORY_PATH", DATA_DIR / "climate_history.parquet"),
    "field_traits_path": ("FIELD_TRAITS_PATH", DATA_DIR / "field_traits.parquet"),
    "soil_health_path": ("SOIL_HEALTH_PATH", DATA_DIR / "soil_health.parquet"),
    "fields_path": ("PLANNER_FIELDS_PATH", DATA_DIR / "fields.parquet"),
    "effects_path": ("EFFECTS_PATH", DATA_DIR / "effects.json"),
    "nass_path": ("NASS_PATH", DATA_DIR / "nass_finney.parquet"),
    "soil_cache_path": ("PLANNER_SOIL_CACHE_PATH", DATA_DIR / "planner_bare_days.json"),
    "economics_path": ("ECONOMICS_PATH", PLANNER_DIR / "economics.json"),
}


@dataclass(frozen=True)
class Settings:
    soil_effect_source: str
    climate_futures_path: Path
    climate_monthly_path: Path
    climate_history_path: Path
    field_traits_path: Path
    soil_health_path: Path
    fields_path: Path
    effects_path: Path
    nass_path: Path
    soil_cache_path: Path
    economics_path: Path


def settings(env: Mapping[str, str] | None = None) -> Settings:
    """The settings the environment asks for right now. An unknown soil source is an error, not a guess."""
    env = os.environ if env is None else env
    source = (env.get("SOIL_EFFECT_SOURCE") or DEFAULT_SOIL_SOURCE).strip().lower()
    if source not in SOIL_SOURCES:
        raise ValueError(f"SOIL_EFFECT_SOURCE={source!r}: expected one of {', '.join(SOIL_SOURCES)}")
    paths = {name: Path(env.get(var) or default) for name, (var, default) in _PATHS.items()}
    return Settings(soil_effect_source=source, **paths)


# --- the farmer's priorities ---------------------------------------------------------------------------
# Used when the request gives no weights. Soil and money lead; any non-negative numbers may replace them.
DEFAULT_WEIGHTS = {"soil": 0.30, "water": 0.20, "money": 0.30, "risk": 0.20}
SCORE_NAMES = ("soil", "water", "money", "risk")

# --- climate futures -----------------------------------------------------------------------------------
SCENARIOS = ("ssp245", "ssp585")
DEFAULT_SCENARIO = "ssp245"
TODAY = 2025                      # the horizon every change is measured against
HORIZON_MIN, HORIZON_MAX = 2025, 2060
SUMMARY_HORIZONS = tuple(range(2025, 2061, 5))   # the stops of the app's future slider
# Window rule (design note addendum): about twenty years around the horizon, cut at the ends of the
# scenario runs: window(h) = [max(2015, h - 10), min(2060, h + 9)]. Today = 2015-2034; 2060 = 2050-2060
# (eleven years, so noisier). Ten-year windows of six single model runs were too noisy.
WINDOW_BACK, WINDOW_FORWARD = 10, 9
WINDOW_FIRST_YEAR, WINDOW_LAST_YEAR = 2015, 2060

# The one-line label says "about the same summer rain" unless the change is at least this big AND at
# least this share of the models agree on its direction (5 of 6). The models do not agree on rain.
RAIN_SMALL_PCT = 3.0
RAIN_AGREE_SHARE = 0.8

# --- bare days (soil_effect.py) ------------------------------------------------------------------------
# A (previous crop -> crop) cell is pulled towards its crop's average as if the average were this many
# extra field-years: a cell of 30 field-years counts half, one of 3,000 keeps its own mean. Field-years
# of one field are not independent and one field-year is noisy (it tells fallow from crop, not more), so
# a cell of a few dozen should not be believed on its own.
SHRINK_FIELD_YEARS = 30.0

# effects.json rows used by the `effects` provider must have at least this many matched pairs.
EFFECTS_MIN_PAIRS = 20

# --- the four scores: fixed reference scales -----------------------------------------------------------
# Every score is 0-1 on a scale that does not depend on the request, so a hotter future lowers it
# instead of being normalised away. Each scale spans what any candidate plan does in any model run,
# scenario and horizon to 2060 on a typical field (range in brackets, from sanity.py), rounded outward,
# so scores do not pile up at 0 or 1 where plans could no longer be told apart. (The first version had
# narrower scales; in the hottest model at 2060 most dryland plans sat at 0 on risk and money.)

# soil = 0.6 x bare-days part + 0.4 x wind-erosion part:
#   bare-days part = 1 at 0 bare days a year, 0 at BARE_DAYS_WORST or more [candidates: 6 to 138 days;
#     a dryland fallow year alone is 143].
#   erosion part   = 1 at index 0, 0 at EROSION_WORST or more (erosion.py: 1.0 = the county's commonest
#     soil bare all year in the 2000-2024 wind climate) [loam: 0.02 to 0.6; the sandiest 7% of fields have
#     4.5 times the loam's index, so 2.0 keeps wheat-sorghum on sand (about 0.6) apart from
#     sorghum-fallow on sand (about 1.6), and makes the erosion part matter more the sandier the field].
SOIL_BARE_SHARE, SOIL_EROSION_SHARE = 0.6, 0.4
BARE_DAYS_WORST = 150.0
EROSION_WORST = 2.0

# water:
#   irrigated: 1 when nothing is pumped, 0 at IRRIGATION_WORST_MM pumped a year, averaged over the
#     plan's years [154 to 894 mm; alfalfa today about 690 mm].
#   dryland: the share of the crops' water need that rain and stored soil water cover, as it is
#     [0.38 to 0.80].
IRRIGATION_WORST_MM = 1000.0

# money: profit is return over direct costs, $ per acre per year (money.py). One fixed scale per
# practice, because an irrigated acre and a dryland acre differ by a factor of about ten and a field is
# one or the other: (profit at score 0, profit at score 1) [irrigated 5 to 400; dryland -104 to 82].
MONEY_SCALE = {"irrigated": (-100.0, 500.0), "dryland": (-125.0, 125.0)}

# risk: failed share = expected share of planted crops that fail (yield under half of normal; fallow
# years are not planted and not counted). The score is simply the share that does not fail: 1 at no
# failures, 0 at RISK_WORST_SHARE [dryland 0.03 to 0.67; irrigated 0 to 0.11].
RISK_WORST_SHARE = 1.0

# --- "holds up" ----------------------------------------------------------------------------------------
# A plan holds up in a future (one climate model, the chosen scenario and window) when both are true.
# One in four is clearly worse than the record: over 2000-2020 the county's dryland corn, sorghum and
# wheat came in under half of normal in 9 of 60 crop-years, about one in seven (B1's calibration counts).
HOLDS_MAX_FAILED_SHARE = 0.25     # no more than one planted crop in four fails
HOLDS_MIN_PROFIT = 0.0            # and it at least covers its direct costs on average, $ per acre per year

# --- ranking -------------------------------------------------------------------------------------------
# rank value = ROBUST_MEAN_SHARE x mean overall score over the futures
#            + (1 - ROBUST_MEAN_SHARE) x mean overall score of the worst WORST_FRACTION of them
ROBUST_MEAN_SHARE = 0.5
WORST_FRACTION = 1.0 / 3.0
# Two plans are the same choice when they differ by no more than this many years in four (comparing
# the share of years each crop gets, whatever the order): reorderings and one-year tweaks. Two 4-year
# plans therefore have to differ in two years; a 3-year cycle differs from a 4-year one by thirds of a
# year (wheat-sorghum-fallow against wheat-sorghum is 1.33: a different choice).
SAME_CHOICE_YEARS = 1.0
TOP_PLANS = 3

MAX_REASON_CHARS = 200
