"""deploy/vercel/scipy_shim.theilslopes must match scipy.stats.theilslopes (the hosted API uses the shim)."""
import sys
from pathlib import Path

import numpy as np
import pytest

stats = pytest.importorskip("scipy.stats")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "vercel"))
import scipy_shim  # noqa: E402


@pytest.mark.parametrize("seed", range(40))
def test_theilslopes_matches_scipy(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(3, 60))
    x = np.round(rng.uniform(1990, 2026, n), 0 if seed % 3 == 0 else 2)  # some repeated years
    y = np.round(150 + 2.2 * (x - 1990) + rng.normal(0, 6, n), 1 if seed % 2 else 3)
    for alpha in (0.95, 0.9, 0.05):
        for method in ("separate", "joint"):
            a = stats.theilslopes(y, x, alpha=alpha, method=method)
            b = scipy_shim.theilslopes(y, x, alpha=alpha, method=method)
            np.testing.assert_allclose([b.slope, b.intercept, b.low_slope, b.high_slope],
                                       [a.slope, a.intercept, a.low_slope, a.high_slope], rtol=1e-12, atol=1e-12)
