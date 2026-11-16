"""Phase 5, item F: price and cost what-ifs.

We do not predict prices. The farmer says "what if prices were 20% lower" and the planner scores the plans again
under that: every crop price (or one crop's) and every direct cost (seed, fertiliser, chemicals, fuel, labour,
fallow passes and the pumping cost per inch) is moved by the percentage given. 0 changes nothing: the default
engine is returned as it is.

    price_pct, cost_pct          -50 .. +100, percent change on all crop prices / all direct costs
    price_<crop>_pct             the same for one crop's price; it replaces price_pct for that crop
                                 (price_corn_pct, price_soybeans_pct, price_winter_wheat_pct, price_sorghum_pct,
                                 price_alfalfa_pct)

`Adjustment` is the checked, hashable form (the key of the engine cache, `Engine.with_adjustment`).
`break_even(...)` is the sensitivity block of the plans answer: the price change at which the top plan's return over
direct costs reaches zero. Return is linear in a uniform price change, so it is solved exactly from two runs of the
planner's own model (prices as set, and prices doubled), per climate model: no search, no new assumption.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from api.planner.crops import CROPS

PRICE_CROPS = tuple(c for c in CROPS if c != "fallow")
CROP_PARAM = {crop: f"price_{crop.replace(' ', '_')}_pct" for crop in PRICE_CROPS}
MIN_PCT, MAX_PCT = -50.0, 100.0

SENTENCE_NOTE = ("A what-if, not a forecast: we do not predict prices or costs. Costs are held where they are set "
                 "above; the crop model and the weather do not change.")


def _number(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"The {name} change must be a number of percent, from {MIN_PCT:g} to {MAX_PCT:g}.")
    if not MIN_PCT <= value <= MAX_PCT:
        raise ValueError(f"The {name} change must be between {MIN_PCT:g}% and +{MAX_PCT:g}%.")
    return round(float(value), 2)


@dataclass(frozen=True)
class Adjustment:
    price_pct: float = 0.0
    cost_pct: float = 0.0
    crop_price_pct: tuple = ()        # ((crop, pct), ...) sorted; replaces price_pct for that crop

    @property
    def is_zero(self) -> bool:
        return self.price_pct == 0 and self.cost_pct == 0 and all(p == 0 for _, p in self.crop_price_pct) \
            and not self.crop_price_pct

    def price_factor(self, crop: str) -> float:
        own = dict(self.crop_price_pct).get(crop)
        return 1.0 + (self.price_pct if own is None else own) / 100.0

    @property
    def cost_factor(self) -> float:
        return 1.0 + self.cost_pct / 100.0

    @property
    def uniform_prices(self) -> bool:
        return not self.crop_price_pct

    def describe(self) -> dict:
        return {"active": not self.is_zero, "price_pct": self.price_pct, "cost_pct": self.cost_pct,
                "crop_price_pct": {crop: pct for crop, pct in self.crop_price_pct},
                "note": SENTENCE_NOTE if not self.is_zero else None}


ZERO = Adjustment()


def parse(price_pct=None, cost_pct=None, crop_price_pct: dict | None = None) -> Adjustment:
    """Check the numbers (ValueError, one plain sentence) and build the adjustment; None = 0."""
    price = 0.0 if price_pct is None else _number("price", price_pct)
    cost = 0.0 if cost_pct is None else _number("cost", cost_pct)
    per = []
    for crop in PRICE_CROPS:
        value = (crop_price_pct or {}).get(crop)
        if value is not None:
            per.append((crop, _number(f"{crop} price", value)))
    return Adjustment(price, cost, tuple(per))


def apply_to_economics(economics, adj: Adjustment):
    """A copy of the economics with prices and direct costs moved (fallow's price stays 0; the cost lines, their
    yield slopes and the pumping cost all scale, so the yield-matched cost scales too)."""
    from dataclasses import replace

    cf = adj.cost_factor
    prices = {crop: float(p) * (adj.price_factor(crop) if crop in PRICE_CROPS else 1.0)
              for crop, p in economics.prices.items()}
    costs = {c: {p: float(v) * cf for p, v in d.items()} for c, d in economics.costs.items()}
    slopes = {c: {p: float(v) * cf for p, v in d.items()} for c, d in economics.slopes.items()}
    return replace(economics, prices=prices, costs=costs, slopes=slopes,
                   pump_per_mm=float(economics.pump_per_mm) * cf)


def scale_prices(economics, k: float):
    from dataclasses import replace

    return replace(economics, prices={c: float(p) * k for c, p in economics.prices.items()})


def _pct(x: float) -> int:
    return int(round(x))


def break_even(profit_now: np.ndarray, profit_double: np.ndarray, adj: Adjustment, name: str, rank: int = 1) -> dict:
    """The sensitivity block for one plan.

    profit_now     the plan's return over direct costs per climate model, under the prices and costs as set
    profit_double  the same with every price doubled (costs as set)
    Return = k x revenue - costs for prices scaled by k, so per model revenue = double - now, costs = revenue - now,
    and the return is zero at k = costs / revenue. Percentages are against today's prices when the prices were moved
    together (`basis` "today"), else against the prices as set ("your_prices"). Costs stay where they are set.
    """
    now = np.asarray(profit_now, dtype=float)
    rev = np.asarray(profit_double, dtype=float) - now
    cost = rev - now
    mean_now = float(now.mean())
    base = 1.0 + (adj.price_pct / 100.0 if adj.uniform_prices else 0.0)       # prices as set / today's prices
    basis = "today" if adj.uniform_prices else "your_prices"
    out = {"plan": rank, "plan_name": name, "basis": basis, "return_now": _pct(mean_now),
           "range_shown": [MIN_PCT, MAX_PCT], "note": SENTENCE_NOTE, "break_even_pct": None, "low_pct": None,
           "high_pct": None, "status": "ok"}
    label = "today's prices" if basis == "today" else "the prices you set"
    if not (rev > 1e-9).all() or float(rev.mean()) <= 1e-9:
        out["status"] = "no_revenue"
        out["sentence"] = (f"Plan {rank} brings in no crop revenue in some climate futures, so no price change "
                           "makes it cover its direct costs.")
        return out
    k = cost / rev                                         # per model: the factor on prices as set
    k_mean = float(cost.mean() / rev.mean())
    pct = lambda f: (f * base - 1.0) * 100.0              # against today's (or the set) prices
    mid, low, high = pct(k_mean), pct(float(k.min())), pct(float(k.max()))
    out.update(break_even_pct=_pct(mid), low_pct=_pct(low), high_pct=_pct(high))
    def move(x: float) -> str:
        return f"{'fall' if x < 0 else 'rise'} about {abs(_pct(x))}%"

    spread = "" if _pct(low) == _pct(high) else (
        f" (between {min(abs(_pct(low)), abs(_pct(high)))}% and {max(abs(_pct(low)), abs(_pct(high)))}% "
        "depending on the climate model)")
    if mean_now <= 0:
        out["status"] = "below_zero_now"
        side = "below" if mid < 0 else "above"
        out["sentence"] = (f"As set, plan {rank} already falls short of its direct costs. It would cover them if prices "
                           f"were about {abs(_pct(mid))}% {side} {label}{spread}.")
        if mid > MAX_PCT:
            out["sentence"] += f" That is beyond the +{MAX_PCT:g}% this control goes to."
        return out
    if pct(float(k.max())) <= MIN_PCT:
        out["status"] = "stays_above_zero"
        out["sentence"] = (f"Plan {rank} stays above its direct costs for any price drop shown here "
                           f"(down to {MIN_PCT:g}% against {label}).")
        return out
    out["sentence"] = (f"Plan {rank} stops covering its direct costs if prices {move(mid)} "
                       f"against {label}{spread}, with costs as set.")
    return out
