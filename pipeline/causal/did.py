"""Difference-in-differences on matched pairs, cluster-bootstrap CIs, and the placebo tests.

Year windows come from config (defaults: switch year 2020, soil years 2016-2024):
    baseline = the first half of the pre-switch soil years (2016-2017)   used only for matching
    pre      = the second half (2018-2019)                               the "before"
    post     = the switch year onwards (2020-2024)                       the "after"
Keeping the matched-on years out of `pre` keeps the noise in the baseline out of the "before",
which avoids the regression-to-the-mean bias of matching and differencing on the same years.

Per pair (A = field with rotation A), using ONLY the years in which both fields have a value, so
every comparison is made within the same year (a dry year lifts bare days for everyone):
    did = (post_A - pre_A) - (post_B - pre_B)
A comparison's effect is the mean `did` over its pairs: how much more the outcome moved under
rotation A than under rotation B, on look-alike neighbours.

Weights: 1 for every field-year unless the outcome names a weight column (om_pred: `confidence`);
a pair-year then weighs sqrt(weight_A * weight_B).

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


def year_mask(years: np.ndarray, span: tuple[int, int]) -> np.ndarray:
    return (years >= span[0]) & (years <= span[1])


def field_windows(values: np.ndarray, weights: np.ndarray, years: np.ndarray) -> dict[str, np.ndarray]:
    """baseline / pre / post means and counts for every field (rows of `values`), each over the
    field's own observed years in config.baseline_years() / pre_years() / post_years()."""
    baseline, n_baseline = wmean(values, weights, year_mask(years, config.baseline_years())[None, :])
    pre, n_pre = wmean(values, weights, year_mask(years, config.pre_years())[None, :])
    post, n_post = wmean(values, weights, year_mask(years, config.post_years())[None, :])
    return {"baseline": baseline, "pre": pre, "post": post, "n_baseline": n_baseline, "n_pre": n_pre, "n_post": n_post}


def pair_windows(
    values: np.ndarray,
    weights: np.ndarray,
    years: np.ndarray,
    a_rows: np.ndarray,
    b_rows: np.ndarray,
    before: tuple[int, int] | None = None,
    after: tuple[int, int] | None = None,
    min_before: int = config.MIN_PAIR_PRE_YEARS,
    min_after: int = config.MIN_PAIR_POST_YEARS,
) -> dict[str, np.ndarray]:
    """Per pair, over the years BOTH fields have a value: the mean of each field `before` and
    `after` (default: config.pre_years() and post_years()), how many shared years went into each,
    and did = (after_A - before_A) - (after_B - before_B). `did` is NaN for a pair with fewer
    than min_before / min_after shared years.
    """
    before = before or config.pre_years()
    after = after or config.post_years()
    va, vb = values[a_rows], values[b_rows]
    both = np.isfinite(va) & np.isfinite(vb)
    w = np.sqrt(weights[a_rows] * weights[b_rows])
    out = {}
    for name, span in (("pre", before), ("post", after)):
        mask = both & year_mask(years, span)[None, :]
        out[f"{name}_a"], out[f"n_{name}"] = wmean(va, w, mask)
        out[f"{name}_b"], _ = wmean(vb, w, mask)
    did = (out["post_a"] - out["pre_a"]) - (out["post_b"] - out["pre_b"])
    did[(out["n_pre"] < min_before) | (out["n_post"] < min_after)] = np.nan
    out["did"] = did
    return out


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


def placebo_did(values, weights, years, a_rows, b_rows, fake_year: int) -> np.ndarray:
    """Per pair, the same `did` with a fake switch at `fake_year`, from pre-switch soil years only:
    (soil years from fake_year up to the real switch) against (soil years before fake_year).

    Unlike the main estimate, the "before" here always contains the baseline years the pairs were
    matched on (there are no earlier soil years to use). The gap is small there by selection and
    springs back afterwards, so a non-zero placebo is partly that bounce and not only a trend."""
    early = (config.SOIL_FIRST_YEAR, fake_year - 1)
    late = (fake_year, config.SWITCH_YEAR - 1)
    return pair_windows(values, weights, years, a_rows, b_rows, early, late, 1, 1)["did"]
