"""Every setting of the Phase 2B causal analysis, in one place.

The outcome (which soil column the analysis measures) is chosen without code changes:

    CAUSAL_OUTCOME=om_pred make causal
    .venv/bin/python -m pipeline.causal.run_causal --outcome om_pred

Default: `bare_days` (days a year the soil sits bare; lower is better). It is the column Phase 2A
found trustworthy; `om_pred` cannot show soil change (README "Soil over time"). Columns listed in
OUTCOMES carry a label, units, a direction and how to weight a field-year; any other numeric column
also works, unweighted, and is assumed higher-is-better unless CAUSAL_HIGHER_IS_BETTER=0.

Timing: every year range is derived from SWITCH_YEAR and the years the two input tables cover.
Nothing else in the package names a year.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from pipeline.common import DATA_DIR
from pipeline.soil.health_schema import YEARS as SOIL_YEARS

# --- files ------------------------------------------------------------------------------
SOIL_REAL_PATH = DATA_DIR / "soil_health.parquet"
SOIL_FAKE_PATH = DATA_DIR / "soil_health_fake.parquet"  # only used while the real one is missing
SOIL_FAKE_TRUTH_PATH = DATA_DIR / "soil_health_fake_truth.parquet"  # noiseless fake series
EFFECTS_PATH = DATA_DIR / "effects.json"
PAIRS_PATH = DATA_DIR / "pairs.parquet"
CAUSAL_FIELDS_PATH = DATA_DIR / "causal_fields.parquet"
CAUSAL_LOG_PATH = DATA_DIR / "causal.log"  # where `make causal` sends its output

# --- outcome ----------------------------------------------------------------------------
DEFAULT_OUTCOME = "bare_days"


@dataclass(frozen=True)
class Outcome:
    column: str
    label: str
    units: str
    higher_is_better: bool
    # Column that weights a field-year when averaging, or None for equal weights. `confidence`
    # describes om_pred only (it is 0 where om_pred is null), so it must not weight anything else.
    weight_column: str | None = None
    # One plain sentence on how to read a gap in this outcome (shown next to the numbers).
    note: str | None = None


OUTCOMES = {
    "bare_days": Outcome(
        "bare_days",
        "Days the soil sits bare each year",
        "days per year",
        False,
        None,
        "Bare days mostly follow which crop is grown that year (a fallow year is bare by definition). "
        "A gap here is a difference in soil cover, not a measured change in the soil itself.",
    ),
    "om_pred": Outcome(
        "om_pred",
        "Soil organic matter (satellite estimate)",
        "percentage points",
        True,
        "confidence",
        "The satellite organic-matter estimate is too noisy to show soil change and shifts with crop "
        "residue on the surface. Do not read a gap here as soil change.",
    ),
}


def outcome(column: str | None = None) -> Outcome:
    """The outcome to analyse: `column`, else $CAUSAL_OUTCOME, else bare_days."""
    name = column or os.environ.get("CAUSAL_OUTCOME") or DEFAULT_OUTCOME
    if name in OUTCOMES:
        return OUTCOMES[name]
    higher = os.environ.get("CAUSAL_HIGHER_IS_BETTER", "1") not in ("0", "false", "no")
    return Outcome(name, name.replace("_", " "), "units", higher)


# --- design -----------------------------------------------------------------------------
# Crop history covers FIRST_YEAR..LAST_YEAR (schema.md); the soil table covers SOIL_FIRST_YEAR..LAST_YEAR
# (pipeline/soil/health_schema.py). The switch year has to sit inside the soil years, with soil years
# on both sides. 2020 is the choice (README "Phase 2B" has the reasoning); $CAUSAL_SWITCH_YEAR
# overrides it for design checks.
FIRST_YEAR = 2008
SOIL_FIRST_YEAR = min(SOIL_YEARS)
LAST_YEAR = max(SOIL_YEARS)
SWITCH_YEAR = int(os.environ.get("CAUSAL_SWITCH_YEAR") or 2020)
MIN_BASELINE_OBS = 1  # soil years a field needs in each window
MIN_PRE_OBS = 1
MIN_POST_OBS = 2
MIN_PAIR_PRE_YEARS = 1  # years BOTH fields of a pair must have, before / after the switch
MIN_PAIR_POST_YEARS = 2


def history_years() -> tuple[int, int]:
    """Crop years used for matching and the irrigation guess: everything before the switch."""
    return FIRST_YEAR, SWITCH_YEAR - 1


def rotation_years() -> tuple[int, int]:
    """Crop years the rotation type (the "treatment") is read from."""
    return SWITCH_YEAR, LAST_YEAR


def _soil_split() -> int:
    """First soil year of the `pre` window: the pre-switch soil years are cut in half."""
    n = SWITCH_YEAR - SOIL_FIRST_YEAR
    if n < 2 or SWITCH_YEAR > LAST_YEAR:
        raise ValueError(
            f"switch year {SWITCH_YEAR} needs at least two soil years before it and one from it on "
            f"(soil years are {SOIL_FIRST_YEAR}-{LAST_YEAR})"
        )
    return SOIL_FIRST_YEAR + n // 2


def baseline_years() -> tuple[int, int]:
    """Soil years the starting level is matched on (first half of the pre-switch soil years)."""
    return SOIL_FIRST_YEAR, _soil_split() - 1


def pre_years() -> tuple[int, int]:
    """Soil years that are the "before" of the difference-in-differences (second half)."""
    return _soil_split(), SWITCH_YEAR - 1


def post_years() -> tuple[int, int]:
    """Soil years that are the "after"."""
    return SWITCH_YEAR, LAST_YEAR


def placebo_switch_years() -> list[int]:
    """Fake switch years: every year that leaves a soil year on both sides before the real switch."""
    return list(range(SOIL_FIRST_YEAR + 1, SWITCH_YEAR))


def span(years: tuple[int, int]) -> str:
    """(2016, 2017) -> "2016-2017"; (2016, 2016) -> "2016"."""
    return str(years[0]) if years[0] == years[1] else f"{years[0]}-{years[1]}"


# --- matching calipers (a pair must pass every one) -------------------------------------
MAX_DISTANCE_KM = 5.0  # centroid to centroid
MAX_SLOPE_DIFF = 0.5  # slope_pct points
MAX_SSURGO_OM_DIFF = 0.3  # SSURGO organic matter, % points ("same or similar soil")
MAX_BASELINE_DIFF_SD = 0.5  # starting soil score, in county standard deviations
MAX_HISTORY_DIST = 0.25  # crop mix over history_years(), total-variation distance (0 = same mix)

# --- estimation ---------------------------------------------------------------------------
N_BOOTSTRAP = 2000
BOOTSTRAP_CLUSTER_KM = 10.0  # pairs in the same 10 km grid cell are resampled together
N_SHUFFLES = 200  # shuffled-label placebo: full re-match + re-estimate per shuffle
MIN_PAIRS = 10  # comparisons with fewer matched pairs are not reported
SEED = 20055

# SSURGO organic matter bins used to report effects by soil group
SOIL_GROUPS = [(0.0, 1.5, "low OM (<1.5%)"), (1.5, 2.0, "mid OM (1.5-2%)"), (2.0, 100.0, "high OM (>=2%)")]


def soil_group(ssurgo_om: float | None) -> str:
    if ssurgo_om is None or ssurgo_om != ssurgo_om:
        return "unknown"
    for low, high, name in SOIL_GROUPS:
        if low <= ssurgo_om < high:
            return name
    return "unknown"
