"""Money: what a crop-year earns, in dollars per acre.

    profit = price x yield  -  direct costs per acre  -  pumping cost per mm x mm pumped

This is the "return over direct costs" of the Kansas State University crop budgets: seed, fertiliser,
chemicals, fuel, repairs, labour, crop insurance, custom work and interest on those. Land rent and
the yearly cost of owning machinery and the pivot are NOT taken off. They are the same whichever
rotation the field grows, so they do not change which plan is better; they do mean the number is not
the farmer's take-home (K-State puts them at about $100 an acre on dryland and $430-490 irrigated).

All numbers are in `economics.json` next to this file (tracked, so a fresh clone works), each with
its source:

* prices: Kansas marketing-year price received, USDA NASS, median of the years named in the file
  (2020-2024), from data/nass_finney.parquet. `python -m api.planner.money --update-prices` rewrites
  that part from the NASS table; `--years 2015-2024` picks other years.
* direct costs: K-State AgManager "Farm Management Guide" cost-return budgets for 2027 (dated
  1 Nov 2026), southwest / western Kansas, middle-yield column, total direct expenses. For irrigated
  crops the two lines that grow with the water applied (irrigation energy and irrigation labour) are
  taken out of the per-acre figure and charged per mm pumped instead.
* matched to the county's yields: K-State writes its budgets for yield goals above the county's normal
  yields (irrigated corn 225 bu against 198, dryland wheat 54 against 37) and scales fertiliser and
  harvest with the goal. So the cost used is the middle budget moved to the crop's normal yield
  (crop_response.json) along the line through the low- and high-yield columns of the same budget:
  cost = per_acre + per_yield_unit x (normal yield - budget yield), never below half of per_acre.
  It is a fixed cost per acre for the crop and practice: it does not move with the weather.
* pumping: the same budgets: natural gas $3.27 per acre-inch (300-foot well, $4.58 per Mcf) plus
  irrigation labour $0.89 per acre-inch = $4.16 per acre-inch = $0.164 per mm.
* fallow: K-State publishes no fallow budget; $55 an acre is our own estimate (three herbicide passes
  priced from the chemical and spraying lines of the wheat-sorghum-fallow budgets). Marked
  "rough, unverified" in the file.

Things this leaves out on purpose: costs do not fall in a bad year (the crop is paid for before
the weather is known), crop insurance pays nothing back in a failed year, there are no government
payments, and prices and costs do not change in the future.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from api.planner import config
from api.planner.crops import CROPS, PRACTICES

MM_PER_INCH = 25.4
PRICE_CROPS = tuple(c for c in CROPS if c != "fallow")
DEFAULT_PRICE_YEARS = (2020, 2024)

# The same numbers as economics.json, used only when that file is missing or broken.
BUILTIN_PRICES = {"corn": 4.73, "soybeans": 12.30, "winter wheat": 6.74, "sorghum": 4.96, "alfalfa": 178.0}
BUILTIN_COSTS = {
    "corn": {"irrigated": 518.41, "dryland": 281.05},
    "soybeans": {"irrigated": 218.91, "dryland": 168.12},
    "winter wheat": {"irrigated": 259.34, "dryland": 233.92},
    "sorghum": {"irrigated": 353.65, "dryland": 225.85},
    "alfalfa": {"irrigated": 465.65, "dryland": 275.15},
    "fallow": {"irrigated": 55.0, "dryland": 55.0},
}
BUILTIN_SLOPES = {      # $ per acre per yield unit (bu, or ton for alfalfa)
    "corn": {"irrigated": 1.4647, "dryland": 1.5505},
    "soybeans": {"irrigated": 1.679, "dryland": 2.5286},
    "winter wheat": {"irrigated": 1.723, "dryland": 1.7319},
    "sorghum": {"irrigated": 1.0821, "dryland": 1.0345},
    "alfalfa": {"irrigated": 23.2455, "dryland": 25.07},
    "fallow": {"irrigated": 0.0, "dryland": 0.0},
}
BUILTIN_BUDGET_YIELDS = {
    "corn": {"irrigated": 225.0, "dryland": 82.0},
    "soybeans": {"irrigated": 65.0, "dryland": 25.0},
    "winter wheat": {"irrigated": 70.0, "dryland": 54.0},
    "sorghum": {"irrigated": 160.0, "dryland": 85.0},
    "alfalfa": {"irrigated": 5.0, "dryland": 3.0},
    "fallow": {"irrigated": 0.0, "dryland": 0.0},
}
BUILTIN_PUMP_PER_MM = 0.1639
MIN_COST_SHARE = 0.5     # the yield-matched cost never drops below this share of the middle budget


@dataclass(frozen=True)
class Economics:
    prices: dict                 # crop -> $ per yield unit (fallow 0)
    costs: dict                  # crop -> practice -> $ per acre at the budget's yield, without the water lines
    pump_per_mm: float           # $ per acre per mm pumped
    slopes: dict = field(default_factory=dict)          # crop -> practice -> $ per acre per yield unit
    budget_yields: dict = field(default_factory=dict)   # crop -> practice -> the budget's yield goal
    source: str = "builtin"      # "json" or "builtin"
    raw: dict = field(default_factory=dict, compare=False)

    def price(self, crop: str) -> float:
        return float(self.prices.get(crop, 0.0))

    def cost(self, crop: str, practice: str, normal_yield: float | None = None) -> float:
        """Direct cost per acre; with `normal_yield`, matched to that yield (module docstring)."""
        base = float(self.costs[crop][practice])
        slope = float((self.slopes.get(crop) or {}).get(practice) or 0.0)
        budget = (self.budget_yields.get(crop) or {}).get(practice)
        if normal_yield is None or not slope or not budget:
            return base
        return max(MIN_COST_SHARE * base, base + slope * (float(normal_yield) - float(budget)))


def builtin_economics() -> Economics:
    prices = dict(BUILTIN_PRICES)
    prices["fallow"] = 0.0
    return Economics(prices=prices, costs={c: dict(BUILTIN_COSTS[c]) for c in CROPS},
                     pump_per_mm=BUILTIN_PUMP_PER_MM, slopes={c: dict(BUILTIN_SLOPES[c]) for c in CROPS},
                     budget_yields={c: dict(BUILTIN_BUDGET_YIELDS[c]) for c in CROPS}, source="builtin")


def load_economics(path: str | Path | None = None) -> Economics:
    """Prices and costs from economics.json; the built-in copy when the file is missing or broken."""
    path = Path(path) if path is not None else config.settings().economics_path
    try:
        raw = json.loads(path.read_text())
        prices = {c: float(raw["prices"]["per_unit"][c]["price"]) for c in PRICE_CROPS}
        prices["fallow"] = 0.0
        costs = {c: {p: float(raw["costs"][c][p]["per_acre"]) for p in PRACTICES} for c in CROPS}
        pump = float(raw["pumping"]["per_mm"])
        slopes = {c: {p: float(raw["costs"][c][p].get("per_yield_unit") or 0.0) for p in PRACTICES} for c in CROPS}
        budget_yields = {c: {p: float(raw["costs"][c][p].get("budget_yield") or 0.0) for p in PRACTICES} for c in CROPS}
        numbers = (list(prices.values()) + [v for c in costs.values() for v in c.values()] + [pump]
                   + [v for c in slopes.values() for v in c.values()]
                   + [v for c in budget_yields.values() for v in c.values()])
        if any(not (n == n) or n < 0 or n == float("inf") for n in numbers):
            raise ValueError("negative or non-finite number")
    except (OSError, ValueError, TypeError, KeyError):
        return builtin_economics()
    return Economics(prices=prices, costs=costs, pump_per_mm=pump, slopes=slopes, budget_yields=budget_yields,
                     source="json", raw=raw)


def profit(crop: str, practice: str, yield_per_acre, irrigation_mm, economics: Economics,
           normal_yield: float | None = None):
    """$ per acre for one crop-year. `yield_per_acre` and `irrigation_mm` may be numbers or arrays.

    `normal_yield` is the crop's normal yield for that practice (crop_response.json); the cost is
    matched to it. Without it the cost is the K-State middle budget as printed.
    """
    return (economics.price(crop) * yield_per_acre - economics.cost(crop, practice, normal_yield)
            - economics.pump_per_mm * irrigation_mm)


def normal_yields(params: dict) -> dict:
    """{crop: {practice: normal yield}} from the crop-response parameters (response.load_params())."""
    return {c: {p: float(params["series"][c][p]["normal_yield"]) for p in PRACTICES} for c in CROPS}


def describe(economics: Economics, normals: dict | None = None) -> dict:
    """Prices, costs and their sources in one small object (for docs and the API)."""
    raw = economics.raw or {}
    out = {
        "source": economics.source,
        "basis": raw.get("basis", "return over direct costs, $ per acre per year"),
        "price_years": (raw.get("prices") or {}).get("years", list(DEFAULT_PRICE_YEARS)),
        "prices": {c: economics.price(c) for c in PRICE_CROPS},
        "budget_costs": {c: dict(economics.costs[c]) for c in CROPS},
        "pump_per_mm": economics.pump_per_mm,
    }
    if normals is not None:
        out["costs"] = {c: {p: round(economics.cost(c, p, normals[c][p]), 2) for p in PRACTICES} for c in CROPS}
    return out


# --- the price part, from the NASS table -------------------------------------------------------------

def prices_from_nass(nass, years: tuple = DEFAULT_PRICE_YEARS, statistic: str = "median") -> dict:
    """{crop: {"price", "unit", "n_years", "level"}} from the tidy NASS table (stat == "price").

    State rows are used (Kansas marketing-year price received); a crop without one falls back to
    whatever level the table has. Raises ValueError when a crop has no price in the years asked for.
    """
    first, last = int(years[0]), int(years[1])
    rows = nass[(nass["stat"] == "price") & (nass["year"] >= first) & (nass["year"] <= last)]
    out = {}
    for crop in PRICE_CROPS:
        sub = rows[rows["crop"] == crop]
        if (sub["level"] == "state").any():
            sub = sub[sub["level"] == "state"]
        per_year = sub.groupby("year")["value"].mean()
        if len(per_year) == 0:
            raise ValueError(f"no {crop} price in {first}-{last} in the NASS table")
        value = float(per_year.median() if statistic == "median" else per_year.mean())
        out[crop] = {"price": round(value, 2), "unit": str(sub["unit"].iloc[0]), "n_years": int(len(per_year)),
                     "level": str(sub["level"].iloc[0])}
    return out


def update_prices(economics_path: str | Path, nass_path: str | Path, years: tuple = DEFAULT_PRICE_YEARS,
                  statistic: str = "median") -> dict:
    """Rewrite the `prices` part of economics.json from the NASS table; everything else is kept."""
    import time

    import pandas as pd

    economics_path = Path(economics_path)
    raw = json.loads(economics_path.read_text())
    per_unit = prices_from_nass(pd.read_parquet(nass_path), years, statistic)
    old = raw.get("prices") or {}
    raw["prices"] = {
        "years": [int(years[0]), int(years[1])],
        "statistic": statistic,
        "source": old.get("source", "USDA NASS, Kansas marketing-year price received (data/nass_finney.parquet)"),
        "why_these_years": old.get("why_these_years", ""),
        "written": time.strftime("%Y-%m-%d"),
        "per_unit": per_unit,
    }
    economics_path.write_text(json.dumps(raw, indent=1) + "\n")
    return per_unit


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Show the planner's prices and costs, or refresh the prices from NASS.")
    parser.add_argument("--update-prices", action="store_true", help="rewrite the price part of economics.json")
    parser.add_argument("--years", default=f"{DEFAULT_PRICE_YEARS[0]}-{DEFAULT_PRICE_YEARS[1]}")
    parser.add_argument("--statistic", default="median", choices=("median", "mean"))
    args = parser.parse_args(argv)
    settings = config.settings()
    if args.update_prices:
        first, last = (int(x) for x in args.years.split("-"))
        if not Path(settings.nass_path).is_file():
            print(f"{settings.nass_path} is missing: prices left as they are")
            return 1
        per_unit = update_prices(settings.economics_path, settings.nass_path, (first, last), args.statistic)
        print(f"prices {first}-{last} ({args.statistic}) written to {settings.economics_path}")
        print(", ".join(f"{c} {v['price']:g} {v['unit']}" for c, v in per_unit.items()))
        return 0
    from api.planner.response import load_params

    economics = load_economics(settings.economics_path)
    normals = normal_yields(load_params())
    info = describe(economics, normals)
    print(f"economics from {economics.source}; prices {info['price_years'][0]}-{info['price_years'][1]}")
    print("prices: " + ", ".join(f"{c} {p:g}" for c, p in info["prices"].items()))
    for practice in PRACTICES:
        print(f"{practice} direct costs $/acre, K-State middle budget -> at the county's normal yield: "
              + ", ".join(f"{c} {economics.cost(c, practice):.0f} -> {info['costs'][c][practice]:.0f}" for c in CROPS))
    print(f"pumping: ${economics.pump_per_mm:.3f} per mm (${economics.pump_per_mm * MM_PER_INCH:.2f} per acre-inch)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
