"""How each crop's yield and failure risk respond to heat and water. Phase 3, piece B1.

    from api.planner.response import crop_response
    table = crop_response(yearly, monthly)      # one row per (model, scenario, year, crop, practice)

The rules
---------
    yield_rel = scale x heat factor x water factor            (1.0 = a normal year for that practice)
    yield     = normal yield x yield_rel                      (bu/acre; alfalfa tons/acre)

* heat factor = 1 - heat_loss x hot days, never below 0. Summer crops and alfalfa count growing-season
  days above 35 C (`hot_days`); winter wheat counts May-June days above 32 C (`wheat_hot_days`).
  Applies to irrigated and dryland crops.
* water factor (dryland) = 1 - Ky x (1 - rain-fed ET / water need), never below 0 (FAO-33). Rain-fed ET
  comes from water.py: effective rain plus the soil water stored before planting, through a monthly
  bucket. Irrigated crops get the water they need: water factor = 1, so irrigated yield does not
  depend on rain; what rain changes for them is how much has to be pumped.
* scale puts the level right: it is 1 / (mean of heat x water over the calibration years), so a
  normal year gives 1. It is a level, not a response.
* yield_rel is capped at 2 (`MAX_YIELD_REL`): no year is credited with more than twice the normal
  yield (the best real years reached 1.5-1.8; some model years are wetter than anything observed).
* failed = yield_rel below 0.5 (`FAIL_BELOW`): less than half the normal yield. Fallow never fails.
* fail_prob = chance that the real yield is below half of normal, given that the model misses by
  `resid_sd` (its leave-one-year-out error in the calibration): Phi((0.5 - yield_rel) / resid_sd).
  The model's yields swing less than real ones, so the hard `failed` flag under-counts bad years;
  `fail_prob` averaged over years is the fairer failure rate. Without a `resid_sd` it equals `failed`.

What was on the field before matters for dryland crops (stored soil water; water.py rule 3), so the
table carries each number three more times, `<name>_after_summer`, `<name>_after_wheat`,
`<name>_after_fallow`, next to the plain columns. The plain columns are the county's usual mix of
previous crops (crops.py `typical_prev`): that mix is what the county yields we calibrate against
contain, and it is what to use when the previous crop is unknown. `column(name, prev_crop)` picks
the right column for a rotation.

Parameters come from `crop_response.json` next to this file (written by
`python -m api.planner.calibrate`); when the file is missing or unreadable the textbook constants of
crops.py are used, so the function always works.

Levels: the observed weather is NASA POWER, which runs about 1.6 C warmer than the Garden City
station. Thresholds (35 C, 32 C) are applied to that series, and the model series were corrected to
it, so observed and model rows are consistent with each other.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from api.planner import water
from api.planner.crops import CROP_TABLE, CROPS, FAIL_BELOW, MAX_YIELD_REL, PRACTICES, PREV_KINDS, prev_kind

PARAMS_PATH = Path(__file__).with_name("crop_response.json")

KEYS = ["model", "scenario", "year", "crop", "practice"]
# columns that exist once per previous-crop kind as `<name>_after_<kind>`
PREV_COLUMNS = ["yield_rel", "yield", "failed", "fail_prob", "water_factor", "stored_mm", "irrigation_mm",
                "irrigation_net_mm", "shortfall_mm"]
BASE_COLUMNS = ["unit", "normal_yield", "yield_rel", "yield", "failed", "fail_prob", "water_need_mm",
                "eff_rain_mm", "stored_mm", "irrigation_mm", "irrigation_net_mm", "shortfall_mm", "heat_days",
                "heat_factor", "water_factor"]
COLUMNS = KEYS + BASE_COLUMNS + [f"{c}_after_{k}" for k in PREV_KINDS for c in PREV_COLUMNS]


def column(name: str, prev_crop: str | None) -> str:
    """Column of the response table to read for `name` when `prev_crop` grew on the field the year before.

    `column("yield", "fallow")` -> "yield_after_fallow"; `column("yield", None)` -> "yield" (county mix).
    Names without a previous-crop version (`water_need_mm`, `heat_factor`, ...) come back unchanged.
    """
    kind = prev_kind(prev_crop)
    if kind == "typical" or name not in PREV_COLUMNS:
        return name
    return f"{name}_after_{kind}"


# --- parameters --------------------------------------------------------------------------------------

def default_params() -> dict:
    """Textbook parameters from crops.py, in the same shape as `crop_response.json`."""
    series = {}
    for name in CROPS:
        crop = CROP_TABLE[name]
        series[name] = {
            p: {"ky": crop.ky if p == "dryland" else 0.0, "heat_loss": crop.heat_loss[p], "scale": crop.scale[p],
                "normal_yield": crop.normal_yield[p], "resid_sd": None, "status": "textbook"}
            for p in PRACTICES
        }
    return {"source": "builtin", "constants": water.DEFAULTS.to_dict(), "fail_below": FAIL_BELOW,
            "max_yield_rel": MAX_YIELD_REL, "series": series}


def _merge(defaults: dict, loaded: dict) -> dict:
    out = dict(defaults)
    out["constants"] = {**defaults["constants"], **(loaded.get("constants") or {})}
    out["fail_below"] = float(loaded.get("fail_below", defaults["fail_below"]))
    out["max_yield_rel"] = float(loaded.get("max_yield_rel", defaults["max_yield_rel"]))
    series = {}
    for name in CROPS:
        series[name] = {}
        for p in PRACTICES:
            entry = dict(defaults["series"][name][p])
            got = ((loaded.get("series") or {}).get(name) or {}).get(p) or {}
            for key in ("ky", "heat_loss", "scale", "normal_yield", "resid_sd", "status"):
                if key in got:
                    entry[key] = got[key]
            series[name][p] = entry
    out["series"] = series
    for key in ("written", "verdicts"):
        if key in loaded:
            out[key] = loaded[key]
    return out


def load_params(path: str | Path | None = None) -> dict:
    """Parameters from the JSON file; the crops.py constants when it is missing or broken.

    The result's `source` is "json" or "builtin". Values missing from the file fall back one by one.
    """
    defaults = default_params()
    p = Path(path) if path is not None else PARAMS_PATH
    try:
        loaded = json.loads(p.read_text())
        if not isinstance(loaded, dict) or not isinstance(loaded.get("series"), dict):
            raise ValueError("no 'series' in the file")
        merged = _merge(defaults, loaded)
        for name in CROPS:           # every number must be usable
            for pr in PRACTICES:
                e = merged["series"][name][pr]
                for key in ("ky", "heat_loss", "scale", "normal_yield"):
                    e[key] = float(e[key])
                    if not np.isfinite(e[key]) or e[key] < 0:
                        raise ValueError(f"{name} {pr} {key} = {e[key]}")
                e["resid_sd"] = None if e["resid_sd"] is None else float(e["resid_sd"])
    except (OSError, ValueError, TypeError, KeyError):
        return defaults
    merged["source"] = "json"
    return merged


# --- the pieces --------------------------------------------------------------------------------------

def heat_factor(days, heat_loss: float):
    """1 - heat_loss x days, between 0 and 1."""
    return np.clip(1.0 - heat_loss * np.maximum(np.asarray(days, dtype=float), 0.0), 0.0, 1.0)


def water_factor(et_ratio, ky: float):
    """FAO-33: 1 - Ky x (1 - actual ET / needed ET), between 0 and 1."""
    r = np.clip(np.asarray(et_ratio, dtype=float), 0.0, 1.0)
    return np.clip(1.0 - ky * (1.0 - r), 0.0, 1.0)


def _phi(x):
    """Standard normal distribution function (Abramowitz and Stegun 7.1.26; error below 1e-7)."""
    x = np.asarray(x, dtype=float)
    z = np.abs(x) / np.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = 1.0 - poly * np.exp(-z * z)
    return 0.5 * (1.0 + np.sign(x) * erf)


def fail_probability(yield_rel, resid_sd: float | None, fail_below: float = FAIL_BELOW):
    """Chance the real yield is under `fail_below` of normal when the model errs by `resid_sd`."""
    y = np.asarray(yield_rel, dtype=float)
    if not resid_sd or resid_sd <= 0:
        return (y < fail_below).astype(float)
    return _phi((fail_below - y) / resid_sd)


def _with_series_columns(df: pd.DataFrame) -> pd.DataFrame:
    if "model" in df.columns and "scenario" in df.columns:
        return df
    return df.assign(model=df["model"] if "model" in df.columns else water.OBSERVED,
                     scenario=df["scenario"] if "scenario" in df.columns else water.OBSERVED)


# --- the entry point ---------------------------------------------------------------------------------

def crop_response(yearly: pd.DataFrame, monthly: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Yield, failure and water numbers for every crop and practice in every climate year.

    yearly   `data/climate_history.parquet` (observed; no `model` / `scenario` columns needed) or
             `data/climate_futures.parquet` (or any rows of them). Used columns: `year`, `hot_days`
             (growing-season days above 35 C), `wheat_hot_days` (May-June days above 32 C), and
             `model`, `scenario` when present.
    monthly  `data/climate_monthly.parquet` rows for the same series: `model`, `scenario`, `year`,
             `month`, `rain_mm`, `et0_mm`. Observed rows are model = scenario = "observed". Every year
             of `yearly` needs its twelve months here; earlier months are used when present. Observed
             and model rows may be mixed in one call (the whole table as it is on disk is fine).
    params   from `load_params()`; loaded from `crop_response.json` (or the built-in constants) if None.

    Returns one row per (model, scenario, year, crop, practice), sorted by model, scenario, year, then
    crop in the fixed order and practice (irrigated, dryland). Columns: see `COLUMNS` and the module
    docstring; depths in mm, yields per acre.
    """
    params = params if params is not None else load_params()
    constants = water.WaterConstants.from_dict(params.get("constants"))
    fail_below = float(params.get("fail_below", FAIL_BELOW))
    cap = float(params.get("max_yield_rel", MAX_YIELD_REL))

    yearly = _with_series_columns(yearly)
    for col in ("year", "hot_days", "wheat_hot_days"):
        if col not in yearly.columns:
            raise ValueError(f"yearly table lacks column {col!r}")
    yearly = yearly.sort_values(["model", "scenario", "year"], kind="stable").reset_index(drop=True)
    if yearly.duplicated(["model", "scenario", "year"]).any():
        raise ValueError("yearly table has more than one row for a (model, scenario, year)")
    if len(yearly) == 0:
        return pd.DataFrame(columns=COLUMNS)

    monthly = _with_series_columns(monthly)
    wanted = set(zip(yearly["model"], yearly["scenario"]))
    have = monthly[["model", "scenario"]].drop_duplicates()
    keep = [k in wanted for k in zip(have["model"], have["scenario"])]
    have = have[keep]
    if len(have) < len(wanted):
        lacking = sorted(wanted - set(zip(have["model"], have["scenario"])))
        raise ValueError(f"monthly table has no rows for {lacking[:3]}")
    if len(have) < monthly[["model", "scenario"]].drop_duplicates().shape[0]:
        monthly = monthly.merge(have, on=["model", "scenario"])
    grid, bals = water.balance(monthly, constants)

    key_index = {k: i for i, k in enumerate(grid.keys)}
    si = np.fromiter((key_index[k] for k in zip(yearly["model"], yearly["scenario"])), dtype=int, count=len(yearly))
    years = yearly["year"].to_numpy(dtype=int)
    yi = np.searchsorted(grid.years, years)
    yi_safe = np.minimum(yi, len(grid.years) - 1)
    bad = (yi >= len(grid.years)) | (grid.years[yi_safe] != years) | ~grid.complete[si, yi_safe]
    if bad.any():
        raise ValueError(f"monthly table does not cover all twelve months of year(s) {sorted(set(years[bad]))[:5]}")

    n, n_c, n_p = len(yearly), len(CROPS), len(PRACTICES)
    shape = (n, n_c, n_p)
    cols = {c: np.zeros(shape) for c in BASE_COLUMNS if c not in ("unit", "failed")}
    for k in PREV_KINDS:
        for c in PREV_COLUMNS:
            if c != "failed":
                cols[f"{c}_after_{k}"] = np.zeros(shape)
    unit = np.empty((n_c, n_p), dtype=object)
    is_crop = np.ones((n_c, n_p), dtype=bool)

    for ci, name in enumerate(CROPS):
        crop = CROP_TABLE[name]
        b = bals[name]
        need = b.need[si, yi]
        eff_rain = b.eff_rain.sum(axis=2)[si, yi] if b.eff_rain.shape[2] else np.zeros(n)
        days = yearly[crop.heat_column].to_numpy(dtype=float)
        for pi, practice in enumerate(PRACTICES):
            par = params["series"][name][practice]
            unit[ci, pi] = crop.unit
            cols["normal_yield"][:, ci, pi] = par["normal_yield"]
            if not crop.season:      # fallow: nothing grows, nothing fails
                is_crop[ci, pi] = False
                for c in ["yield_rel", "heat_factor", "water_factor"] + \
                        [f"{c}_after_{k}" for k in PREV_KINDS for c in ("yield_rel", "water_factor")]:
                    cols[c][:, ci, pi] = 1.0
                continue
            irrigated = practice == "irrigated"
            heat = heat_factor(days, par["heat_loss"])
            cols["water_need_mm"][:, ci, pi] = need
            cols["eff_rain_mm"][:, ci, pi] = eff_rain
            cols["heat_days"][:, ci, pi] = days
            cols["heat_factor"][:, ci, pi] = heat
            weights = crop.typical_prev[practice]
            for k in PREV_KINDS:
                gap = b.gap(k)[si, yi]
                wf = np.ones(n) if irrigated else water_factor(b.et_ratio(k)[si, yi], par["ky"])
                rel = np.minimum(par["scale"] * heat * wf, cap)
                stored = b.stored[k][si, yi]
                per_kind = {
                    "yield_rel": rel, "yield": par["normal_yield"] * rel, "water_factor": wf, "stored_mm": stored,
                    "fail_prob": fail_probability(rel, par["resid_sd"], fail_below),
                    "irrigation_net_mm": gap if irrigated else 0.0,
                    "irrigation_mm": gap / constants.pivot_efficiency if irrigated else 0.0,
                    "shortfall_mm": 0.0 if irrigated else gap,
                }
                w = float(weights.get(k, 0.0))
                for c, v in per_kind.items():
                    cols[f"{c}_after_{k}"][:, ci, pi] = v
                    if c != "fail_prob":
                        cols[c][:, ci, pi] += w * v
            cols["fail_prob"][:, ci, pi] = fail_probability(cols["yield_rel"][:, ci, pi], par["resid_sd"], fail_below)

    out = {
        "model": np.repeat(yearly["model"].to_numpy(), n_c * n_p),
        "scenario": np.repeat(yearly["scenario"].to_numpy(), n_c * n_p),
        "year": np.repeat(years, n_c * n_p),
        "crop": np.tile(np.repeat(np.array(CROPS, dtype=object), n_p), n),
        "practice": np.tile(np.array(PRACTICES, dtype=object), n * n_c),
        "unit": np.tile(unit.ravel(), n),
    }
    crop_mask = np.tile(is_crop.ravel(), n)
    for c in BASE_COLUMNS:
        if c == "unit":
            continue
        if c == "failed":
            out[c] = (out["yield_rel"] < fail_below) & crop_mask
        else:
            out[c] = cols[c].reshape(-1)
    for k in PREV_KINDS:
        for c in PREV_COLUMNS:
            name = f"{c}_after_{k}"
            if c == "failed":
                out[name] = (out[f"yield_rel_after_{k}"] < fail_below) & crop_mask
            else:
                out[name] = cols[name].reshape(-1)
    return pd.DataFrame(out, columns=COLUMNS)
