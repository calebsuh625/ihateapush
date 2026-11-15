"""Look-alike pairs: neighbouring fields that started alike but grew a different rotation type from the
switch year on (config.SWITCH_YEAR; "history" below = config.history_years(), the crop years before it).

Step 1, `candidate_pairs` (labels not used): every pair of fields that
  * are in the same irrigation group (irrigated / dryland; "uncertain" fields are left out),
  * are less than MAX_DISTANCE_KM apart (centroids),
  * differ by at most MAX_SLOPE_DIFF in slope and MAX_SSURGO_OM_DIFF in SSURGO organic matter,
  * started at a similar soil value (mean over config.baseline_years(), within MAX_BASELINE_DIFF_SD
    county SDs),
  * had a similar crop mix over the history years (total-variation distance <= MAX_HISTORY_DIST),
  * share enough soil years to be compared within the same years (MIN_PAIR_PRE_YEARS before the
    switch, MIN_PAIR_POST_YEARS after).
Each candidate gets a match score: the sum of each difference divided by its caliper (0 = identical,
5 = at the edge of every caliper).

Step 2, `match` (labels used): for each comparison "rotation A vs rotation B" within an irrigation
group, keep candidates whose rotation types after the switch are A and B, then greedily take the
best-scoring pair whose fields are both still unused for that comparison (1:1 matching without
replacement). A field can be in more than one comparison. Step 2 is cheap, so the shuffled-label
placebo re-runs it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from pipeline.causal import config
from pipeline.causal.rotations import COMPARABLE_TYPES, comparison_key


def _shared_years(observed: np.ndarray, years: np.ndarray, span: tuple[int, int], i, j) -> np.ndarray:
    cols = (years >= span[0]) & (years <= span[1])
    return (observed[i][:, cols] & observed[j][:, cols]).sum(axis=1)


def candidate_pairs(f: pd.DataFrame, observed: np.ndarray | None = None, years: np.ndarray | None = None) -> pd.DataFrame:
    """All label-free look-alike candidates. `f` is the per-field feature table (run_causal);
    `observed` (fields x years, bool) says which soil years each field has."""
    sd = float(np.nanstd(f["baseline"]))
    max_base = config.MAX_BASELINE_DIFF_SD * (sd if sd > 0 else 1.0)
    out = []
    for irrigation in ("irrigated", "dryland"):
        g = f[(f["irrigation"] == irrigation) & f["eligible"]].reset_index(drop=True)
        if len(g) < 2:
            continue
        tree = cKDTree(np.c_[g["x"], g["y"]])
        ij = tree.query_pairs(config.MAX_DISTANCE_KM * 1000, output_type="ndarray")
        if len(ij) == 0:
            continue
        i, j = ij[:, 0], ij[:, 1]
        dist = np.hypot(g["x"].values[i] - g["x"].values[j], g["y"].values[i] - g["y"].values[j]) / 1000
        d_slope = np.abs(g["slope"].values[i] - g["slope"].values[j])
        d_om = np.abs(g["ssurgo_om"].values[i] - g["ssurgo_om"].values[j])
        d_base = np.abs(g["baseline"].values[i] - g["baseline"].values[j])
        mix = np.stack(g["mix"].values)
        d_hist = 0.5 * np.abs(mix[i] - mix[j]).sum(axis=1)
        ok = (
            (d_slope <= config.MAX_SLOPE_DIFF)
            & (d_om <= config.MAX_SSURGO_OM_DIFF)
            & (d_base <= max_base)
            & (d_hist <= config.MAX_HISTORY_DIST)
        )
        if observed is not None:
            ri, rj = g["row"].values[i], g["row"].values[j]
            ok &= _shared_years(observed, years, config.pre_years(), ri, rj) >= config.MIN_PAIR_PRE_YEARS
            ok &= _shared_years(observed, years, config.post_years(), ri, rj) >= config.MIN_PAIR_POST_YEARS
        score = (
            dist / config.MAX_DISTANCE_KM
            + d_slope / config.MAX_SLOPE_DIFF
            + d_om / config.MAX_SSURGO_OM_DIFF
            + d_base / max_base
            + d_hist / config.MAX_HISTORY_DIST
        )
        out.append(
            pd.DataFrame(
                {
                    "i": g["row"].values[i][ok],
                    "j": g["row"].values[j][ok],
                    "irrigation": irrigation,
                    "distance_km": dist[ok],
                    "slope_diff": d_slope[ok],
                    "ssurgo_om_diff": d_om[ok],
                    "baseline_diff": d_base[ok],
                    "history_distance": d_hist[ok],
                    "match_score": score[ok],
                }
            )
        )
    if not out:
        return pd.DataFrame(columns=["i", "j", "irrigation", "match_score"])
    return pd.concat(out, ignore_index=True).sort_values("match_score", kind="stable").reset_index(drop=True)


def match(cands: pd.DataFrame, labels: np.ndarray) -> pd.DataFrame:
    """Greedy 1:1 matched pairs per (irrigation, A, B). `labels[row]` = rotation type after the switch.

    Returns the chosen candidate rows plus `a_row`, `b_row`, `rotation_a`, `rotation_b`
    (a_row is the field with rotation A, the earlier type in ROTATION_TYPES).
    """
    if len(cands) == 0:
        return cands.assign(a_row=[], b_row=[], rotation_a=[], rotation_b=[])
    li, lj = labels[cands["i"].values], labels[cands["j"].values]
    comparable = set(COMPARABLE_TYPES)
    keep = (li != lj) & np.isin(li, list(comparable)) & np.isin(lj, list(comparable))
    c = cands[keep]
    li, lj = li[keep], lj[keep]
    chosen, a_rows, b_rows, ras, rbs = [], [], [], [], []
    used: dict[tuple, set] = {}
    # candidates are already sorted by score, so one pass is the greedy match
    for idx, i, j, irr, la, lb in zip(c.index.values, c["i"].values, c["j"].values, c["irrigation"].values, li, lj):
        a, b = comparison_key(la, lb)
        key = (irr, a, b)
        taken = used.setdefault(key, set())
        if i in taken or j in taken:
            continue
        taken.add(i)
        taken.add(j)
        ai, bi = (i, j) if la == a else (j, i)
        chosen.append(idx)
        a_rows.append(ai)
        b_rows.append(bi)
        ras.append(a)
        rbs.append(b)
    out = c.loc[chosen].copy()
    out["a_row"], out["b_row"], out["rotation_a"], out["rotation_b"] = a_rows, b_rows, ras, rbs
    return out.reset_index(drop=True)
