"""Every setting of the Phase 2B causal analysis, in one place.

The outcome (which soil column the analysis measures) is chosen without code changes:

    CAUSAL_OUTCOME=bare_soil_index make causal
    .venv/bin/python -m pipeline.causal.run_causal --outcome bare_soil_index

Default: `om_pred` (satellite organic-matter estimate). Columns listed in OUTCOMES carry a label,
units and a direction; any other numeric column also works and is assumed higher-is-better unless
CAUSAL_HIGHER_IS_BETTER=0.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from pipeline.common import DATA_DIR

# --- files ------------------------------------------------------------------------------
SOIL_REAL_PATH = DATA_DIR / "soil_health.parquet"
SOIL_FAKE_PATH = DATA_DIR / "soil_health_fake.parquet"  # only used while the real one is missing
SOIL_FAKE_TRUTH_PATH = DATA_DIR / "soil_health_fake_truth.parquet"  # noiseless fake series
EFFECTS_PATH = DATA_DIR / "effects.json"
PAIRS_PATH = DATA_DIR / "pairs.parquet"
CAUSAL_FIELDS_PATH = DATA_DIR / "causal_fields.parquet"
CAUSAL_LOG_PATH = DATA_DIR / "causal.log"

# --- outcome ----------------------------------------------------------------------------
DEFAULT_OUTCOME = "om_pred"


@dataclass(frozen=True)
class Outcome:
    column: str
    label: str
    units: str
    higher_is_better: bool


OUTCOMES = {
    "om_pred": Outcome("om_pred", "Soil organic matter (satellite estimate)", "percentage points", True),
    "bare_soil_index": Outcome("bare_soil_index", "Bare soil exposure", "index points", False),
    "bare_soil_exposure": Outcome("bare_soil_exposure", "Bare soil exposure", "index points", False),
}


def outcome(column: str | None = None) -> Outcome:
    """The outcome to analyse: `column`, else $CAUSAL_OUTCOME, else om_pred."""
    name = column or os.environ.get("CAUSAL_OUTCOME") or DEFAULT_OUTCOME
    if name in OUTCOMES:
        return OUTCOMES[name]
    higher = os.environ.get("CAUSAL_HIGHER_IS_BETTER", "1") not in ("0", "false", "no")
    return Outcome(name, name.replace("_", " "), "units", higher)


# --- design -----------------------------------------------------------------------------
SWITCH_YEAR = 2016  # rotation type is read from SWITCH_YEAR..LAST_YEAR; before it is "history"
FIRST_YEAR = 2008
LAST_YEAR = 2024
MIN_PRE_OBS = 2  # soil observations a field needs before / after the switch
MIN_POST_OBS = 2

# --- matching calipers (a pair must pass every one) -------------------------------------
MAX_DISTANCE_KM = 5.0  # centroid to centroid
MAX_SLOPE_DIFF = 0.5  # slope_pct points
MAX_SSURGO_OM_DIFF = 0.3  # SSURGO organic matter, % points ("same or similar soil")
MAX_BASELINE_DIFF_SD = 0.5  # starting soil score, in county standard deviations
MAX_HISTORY_DIST = 0.25  # 2008-2015 crop mix, total-variation distance (0 = same mix)

# --- estimation ---------------------------------------------------------------------------
N_BOOTSTRAP = 2000
BOOTSTRAP_CLUSTER_KM = 10.0  # pairs in the same 10 km grid cell are resampled together
N_SHUFFLES = 200  # shuffled-label placebo: full re-match + re-estimate per shuffle
PLACEBO_SWITCH_YEARS = [2011, 2012, 2013]  # fake switch years inside the history period
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
