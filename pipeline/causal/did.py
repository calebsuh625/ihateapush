"""Difference-in-differences on matched pairs, cluster-bootstrap CIs, and the placebo tests.

Per field, from its yearly soil scores (weighted by `confidence`):
    baseline = mean of the first half of its pre-2016 observations   (used only for matching)
    pre      = mean of the second half of its pre-2016 observations
    post     = mean of its 2016-2024 observations
Splitting the history this way keeps the noise in the matched-on baseline out of `pre`, which
avoids the regression-to-the-mean bias of matching and differencing on the same years.

Per pair (A = field with rotation A):   did = (post_A - pre_A) - (post_B - pre_B)
A comparison's effect is the mean `did` over its pairs: how much more the soil score moved
under rotation A than under rotation B, on look-alike neighbours.

Uncertainty: pairs are resampled in spatial clusters (BOOTSTRAP_CLUSTER_KM grid cells), because
neighbouring pairs share weather and soil and are not independent. 95% percentile interval.
p_value: cluster-level sign-flip permutation test (swapping which field is "A" within a pair is
exactly a label shuffle inside that pair).
"""
from __future__ import annotations

import numpy as np

from pipeline.causal import config


def wmean(values: np.ndarray, weights: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Row-wise weighted mean of `values` over the True cells of `mask` (NaN cells skipped).

    Returns (mean, count); mean is NaN for rows with no usable cell.
    """
    ok = mask & np.isfinite(values)
    w = np.where(ok, weights, 0.0)
    total = w.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(total > 0, (np.where(ok, values, 0.0) * w).sum(axis=1) / total, np.nan)
    return mean, ok.sum(axis=1)


def field_windows(values: np.ndarray, weights: np.ndarray, years: np.ndarray, switch: int) -> dict[str, np.ndarray]:
    """baseline / pre / post means and counts for every field (rows of `values`)."""
    observed = np.isfinite(values)
    pre_mask = observed & (years[None, :] < switch)
    n_pre = pre_mask.sum(axis=1)
    # rank of each pre observation within its row: 0, 1, 2, ...
    rank = np.cumsum(pre_mask, axis=1) - 1
    first_half = pre_mask & (rank < (n_pre // 2)[:, None])
    second_half = pre_mask & ~first_half
    baseline, _ = wmean(values, weights, first_half)
    pre, _ = wmean(values, weights, second_half)
    post, n_post = wmean(values, weights, years[None, :] >= switch)
    return {"baseline": baseline, "pre": pre, "post": post, "n_pre": n_pre, "n_post": n_post}


def cluster_ids(x: np.ndarray, y: np.ndarray, km: float = config.BOOTSTRAP_CLUSTER_KM) -> np.ndarray:
    cell = km * 1000
    keys = np.floor(x / cell).astype(np.int64) * 1_000_003 + np.floor(y / cell).astype(np.int64)
    return np.unique(keys, return_inverse=True)[1]


def estimate(d: np.ndarray, clusters: np.ndarray, rng: np.random.Generator, n_boot: int = config.N_BOOTSTRAP) -> dict:
    """Mean of `d` with a cluster-bootstrap 95% CI and a sign-flip permutation p-value."""
    d = np.asarray(d, dtype=float)
    ok = np.isfinite(d)
    d, clusters = d[ok], np.asarray(clusters)[ok]
    n = len(d)
    if n == 0:
        return {"effect": None, "ci_low": None, "ci_high": None, "se": None, "p_value": None, "n_pairs": 0, "n_clusters": 0}
    _, cl = np.unique(clusters, return_inverse=True)
    k = cl.max() + 1
    sums = np.bincount(cl, weights=d, minlength=k)
    counts = np.bincount(cl, minlength=k).astype(float)
    effect = d.mean()
    draw = rng.integers(0, k, size=(n_boot, k))
    boot = sums[draw].sum(axis=1) / counts[draw].sum(axis=1)
    signs = rng.choice([-1.0, 1.0], size=(n_boot, k))
    perm = (signs * sums).sum(axis=1) / n
    p = (1 + np.sum(np.abs(perm) >= abs(effect) - 1e-12)) / (1 + n_boot)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {
        "effect": float(effect),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "se": float(boot.std(ddof=1)) if n_boot > 1 else None,
        "p_value": float(p),
        "n_pairs": int(n),
        "n_clusters": int(k),
    }


def placebo_switch(values: np.ndarray, weights: np.ndarray, years: np.ndarray, fake_year: int, switch: int):
    """Per field: (mean of pre-switch years >= fake_year) - (mean of years < fake_year), plus counts."""
    observed = np.isfinite(values)
    late, n_late = wmean(values, weights, observed & (years[None, :] >= fake_year) & (years[None, :] < switch))
    early, n_early = wmean(values, weights, observed & (years[None, :] < fake_year))
    change = late - early
    change[(n_late < 1) | (n_early < 1)] = np.nan
    return change
