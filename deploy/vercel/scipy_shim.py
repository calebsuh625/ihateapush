"""`scipy.stats.theilslopes` in numpy, for the hosted API only (scipy is ~100 MB, too large for the
function). runtime.setup() registers this as `scipy` / `scipy.stats` only when scipy is not installed.
It follows scipy's own code (method="separate", Sen 1968 eq. 2.6 for the interval); tests/test_deploy_shim.py
checks it against scipy.
"""
from __future__ import annotations

from collections import namedtuple
from statistics import NormalDist

import numpy as np

TheilslopesResult = namedtuple("TheilslopesResult", ("slope", "intercept", "low_slope", "high_slope"))


def _repeat_counts(a: np.ndarray) -> np.ndarray:
    _, counts = np.unique(a, return_counts=True)
    return counts[counts > 1]


def theilslopes(y, x=None, alpha=0.95, method="separate"):
    if method not in ("joint", "separate"):
        raise ValueError(f"method must be either 'joint' or 'separate'. '{method}' is invalid.")
    y = np.array(y, dtype=float).ravel()
    x = np.arange(len(y), dtype=float) if x is None else np.array(x, dtype=float).ravel()
    if len(x) != len(y):
        raise ValueError(f"Incompatible lengths ! ({len(y)}<>{len(x)})")
    deltax = x[:, np.newaxis] - x
    deltay = y[:, np.newaxis] - y
    slopes = deltay[deltax > 0] / deltax[deltax > 0]
    if not slopes.size:
        raise ValueError("All `x` coordinates are identical.")
    slopes.sort()
    medslope = np.median(slopes)
    if method == "joint":
        medinter = np.median(y - medslope * x)
    else:
        medinter = np.median(y) - medslope * np.median(x)
    if alpha > 0.5:
        alpha = 1.0 - alpha
    z = NormalDist().inv_cdf(alpha / 2.0)
    nt = len(slopes)
    ny = len(y)
    sigsq = 1 / 18.0 * (
        ny * (ny - 1) * (2 * ny + 5)
        - sum(k * (k - 1) * (2 * k + 5) for k in _repeat_counts(x))
        - sum(k * (k - 1) * (2 * k + 5) for k in _repeat_counts(y))
    )
    try:
        sigma = np.sqrt(sigsq)
        ru = min(int(np.round((nt - z * sigma) / 2.0)), len(slopes) - 1)
        rl = max(int(np.round((nt + z * sigma) / 2.0)) - 1, 0)
        delta = slopes[[rl, ru]]
    except (ValueError, IndexError):
        delta = (np.nan, np.nan)
    return TheilslopesResult(slope=medslope, intercept=medinter, low_slope=delta[0], high_slope=delta[1])
