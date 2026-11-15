"""Climate futures: which years and which model runs stand for "the climate around year h".

A FUTURE is one climate model under one scenario, averaged over a window of about twenty years
around the chosen horizon. Six models give six futures per (scenario, horizon); a plan is scored in
each of them, and the spread between them is the honest uncertainty.

Window rule (design note addendum):

    window(h) = [max(2015, h - 10), min(2060, h + 9)]

"Today" is h = 2025, so 2015-2034. h = 2060 gives 2050-2060: eleven years instead of twenty, so that
end is noisier. Ten-year windows of six single model runs were too noisy to use.

"What changes" numbers: each model is compared with ITS OWN today window (so a model that runs wet
is compared with its own wet baseline), then the six changes are averaged; `range` gives the lowest
and highest model. Temperatures and day counts change by a difference, rain and reference ET by a
percentage.

The one-line label reads like "+1.2°C, about the same summer rain, 15 more 35°C+ days". Summer rain
is called "less" or "more" only when the mean change is at least 3% AND at least 5 of the 6 models
agree on the direction; otherwise it says "about the same summer rain", because the models do not
agree on rain and the label must not pretend they do. (`changes["summer_rain_agree"]` says how many
models share the direction of the mean, so the app can show the disagreement.)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from api.planner import config

# yearly columns the planner and the summaries use (all are in data/climate_futures.parquet)
COLUMNS = ("tmean_c", "rain_mm", "summer_rain_mm", "hot_days", "dry_spell_days", "et0_mm", "wheat_rain_mm",
           "wheat_hot_days", "spring_wind_ms", "erosion_climate", "annual_rain_mm", "annual_et0_mm",
           "annual_tmean_c")
# change key -> (column, "diff" or "pct")
CHANGES = {
    "tmean_c": ("tmean_c", "diff"),
    "summer_rain_pct": ("summer_rain_mm", "pct"),
    "hot_days": ("hot_days", "diff"),
    "rain_pct": ("rain_mm", "pct"),
    "annual_rain_pct": ("annual_rain_mm", "pct"),
    "wheat_rain_pct": ("wheat_rain_mm", "pct"),
    "et0_pct": ("et0_mm", "pct"),
    "annual_et0_pct": ("annual_et0_mm", "pct"),
    "wheat_hot_days": ("wheat_hot_days", "diff"),
    "dry_spell_days": ("dry_spell_days", "diff"),
    "erosion_climate_pct": ("erosion_climate", "pct"),
}


def check_scenario(scenario: str) -> str:
    if scenario not in config.SCENARIOS:
        raise ValueError(f"scenario {scenario!r}: expected one of {', '.join(config.SCENARIOS)}")
    return scenario


def check_horizon(horizon) -> int:
    if isinstance(horizon, bool) or not isinstance(horizon, (int, np.integer)):
        raise ValueError(f"horizon {horizon!r}: expected a whole year {config.HORIZON_MIN}-{config.HORIZON_MAX}")
    if not config.HORIZON_MIN <= int(horizon) <= config.HORIZON_MAX:
        raise ValueError(f"horizon {horizon}: expected a year {config.HORIZON_MIN}-{config.HORIZON_MAX}")
    return int(horizon)


def window(horizon: int) -> tuple[int, int]:
    """(first year, last year) of the climate window around `horizon`, both included."""
    horizon = check_horizon(horizon)
    return (max(config.WINDOW_FIRST_YEAR, horizon - config.WINDOW_BACK),
            min(config.WINDOW_LAST_YEAR, horizon + config.WINDOW_FORWARD))


@dataclass(frozen=True)
class Future:
    model: str
    scenario: str
    first_year: int
    last_year: int


def _round(value: float, digits: int = 1) -> float:
    return round(float(value), digits)


def label_for(changes: dict, horizon: int) -> str:
    """The one-line label for a `changes` dict (see the module docstring for the rain rule)."""
    if horizon == config.TODAY:
        return "Today's climate"
    parts = [f"{changes['tmean_c']:+.1f}°C"]
    pct = changes["summer_rain_pct"]
    agree = changes.get("summer_rain_agree", 0) / max(1, changes.get("models", 1))
    if abs(pct) >= config.RAIN_SMALL_PCT and agree >= config.RAIN_AGREE_SHARE:
        parts.append(f"{abs(round(pct)):.0f}% {'less' if pct < 0 else 'more'} summer rain")
    else:
        parts.append("about the same summer rain")
    hot = round(changes["hot_days"])
    if hot >= 1:
        parts.append(f"{hot:.0f} more 35°C+ days")
    elif hot <= -1:
        parts.append(f"{-hot:.0f} fewer 35°C+ days")
    else:
        parts.append("about as many 35°C+ days")
    return ", ".join(parts)


class Climate:
    """The yearly climate table as arrays [scenario, model, year], with the window arithmetic on top."""

    def __init__(self, yearly):
        need = {"model", "scenario", "year", *COLUMNS}
        missing = sorted(need - set(yearly.columns))
        if missing:
            raise ValueError(f"climate table lacks column(s): {', '.join(missing)}")
        self.scenarios = tuple(s for s in config.SCENARIOS if (yearly["scenario"] == s).any())
        if not self.scenarios:
            raise ValueError(f"climate table has no rows for scenarios {config.SCENARIOS}")
        yearly = yearly[yearly["scenario"].isin(self.scenarios)]
        self.models = tuple(sorted(yearly["model"].unique()))
        self.years = np.array(sorted(int(y) for y in yearly["year"].unique()))
        si = yearly["scenario"].map({s: i for i, s in enumerate(self.scenarios)}).to_numpy()
        mi = yearly["model"].map({m: i for i, m in enumerate(self.models)}).to_numpy()
        yi = np.searchsorted(self.years, yearly["year"].to_numpy(dtype=int))
        shape = (len(self.scenarios), len(self.models), len(self.years))
        filled = np.zeros(shape, dtype=int)
        np.add.at(filled, (si, mi, yi), 1)
        if (filled != 1).any():
            raise ValueError("climate table must have exactly one row per scenario, model and year")
        self.data = {}
        for column in COLUMNS:
            arr = np.zeros(shape)
            arr[si, mi, yi] = yearly[column].to_numpy(dtype=float)
            self.data[column] = arr

    # --- windows ---
    def year_slice(self, horizon: int) -> slice:
        first, last = window(horizon)
        lo, hi = np.searchsorted(self.years, first), np.searchsorted(self.years, last, side="right")
        if hi <= lo:
            raise ValueError(f"climate table has no year in {first}-{last}")
        return slice(int(lo), int(hi))

    def futures(self, scenario: str, horizon: int) -> list[Future]:
        """The futures a plan is scored in: one per model, in a fixed (alphabetical) order."""
        check_scenario(scenario)
        first, last = window(horizon)
        return [Future(model, scenario, first, last) for model in self.models]

    def _scenario_index(self, scenario: str) -> int:
        check_scenario(scenario)
        if scenario not in self.scenarios:
            raise ValueError(f"climate table has no rows for scenario {scenario!r}")
        return self.scenarios.index(scenario)

    def averages(self, scenario: str, horizon: int) -> dict:
        """{column: array over models} of window means: the per-future averages."""
        s, years = self._scenario_index(scenario), self.year_slice(horizon)
        return {column: arr[s, :, years].mean(axis=1) for column, arr in self.data.items()}

    def changes_by_model(self, scenario: str, horizon: int) -> dict:
        """{change key: array over models}, each model against its own today window."""
        now, then = self.averages(scenario, config.TODAY), self.averages(scenario, horizon)
        out = {}
        for key, (column, kind) in CHANGES.items():
            if kind == "diff":
                out[key] = then[column] - now[column]
            else:
                out[key] = (then[column] / np.maximum(now[column], 1e-9) - 1.0) * 100.0
        return out

    def changes(self, scenario: str, horizon: int) -> dict:
        """Mean change over the models, plus `range` (lowest, highest model) and the rain agreement."""
        by_model = self.changes_by_model(scenario, horizon)
        out = {key: _round(values.mean(), 2 if key == "tmean_c" else 1) for key, values in by_model.items()}
        rain = by_model["summer_rain_pct"]
        direction = np.sign(rain.mean())
        out["summer_rain_agree"] = int((np.sign(rain) == direction).sum()) if direction != 0 else 0
        out["models"] = len(self.models)
        out["range"] = {key: [_round(values.min(), 2 if key == "tmean_c" else 1),
                              _round(values.max(), 2 if key == "tmean_c" else 1)] for key, values in by_model.items()}
        return out

    def label(self, scenario: str, horizon: int) -> str:
        return label_for(self.changes(scenario, horizon), check_horizon(horizon))

    def values(self, scenario: str, horizon: int) -> dict:
        """Window means averaged over the models (levels, not changes)."""
        return {column: _round(values.mean(), 2 if column.endswith("_c") else 1)
                for column, values in self.averages(scenario, horizon).items()}

    def describe(self, scenario: str, horizon: int) -> dict:
        """What the plans endpoint returns under "climate" for one (scenario, horizon)."""
        changes = self.changes(scenario, horizon)
        value_range = changes.pop("range")
        return {"label": label_for(changes, horizon), "changes": changes, "range": value_range,
                "window": list(window(horizon)), "models": len(self.models)}

    def summary(self, source: str = "", observed: dict | None = None) -> dict:
        """Everything GET /climate needs."""
        scenarios = {}
        for scenario in self.scenarios:
            rows = []
            for horizon in config.SUMMARY_HORIZONS:
                d = self.describe(scenario, horizon)
                rows.append({"horizon": horizon, "window": d["window"], "label": d["label"],
                             "changes": d["changes"], "values": self.values(scenario, horizon), "range": d["range"]})
            scenarios[scenario] = rows
        first = self.scenarios[0]
        out = {
            "available": True,
            "source": source,
            "models": list(self.models),
            "window_rule": (f"about twenty years around the chosen year: from {config.WINDOW_BACK} years before to "
                            f"{config.WINDOW_FORWARD} after, cut at {config.WINDOW_FIRST_YEAR} and "
                            f"{config.WINDOW_LAST_YEAR}; changes are each model against its own "
                            f"{window(config.TODAY)[0]}-{window(config.TODAY)[1]}"),
            "baseline": {"horizon": config.TODAY, "window": list(window(config.TODAY)),
                         "values": {s: self.values(s, config.TODAY) for s in self.scenarios}[first],
                         "values_by_scenario": {s: self.values(s, config.TODAY) for s in self.scenarios}},
            "horizons": list(config.SUMMARY_HORIZONS),
            "scenarios": scenarios,
        }
        if observed:
            out["observed"] = observed
        return out


def observed_values(history) -> dict:
    """{"years": [first, last], "values": {column: mean}} of the observed table (levels for context)."""
    columns = [c for c in COLUMNS if c in history.columns]
    return {"years": [int(history["year"].min()), int(history["year"].max())],
            "values": {c: _round(history[c].mean(), 2 if c.endswith("_c") else 1) for c in columns}}
