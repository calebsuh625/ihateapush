"""Fit the crop response to county history and write `crop_response.json`. Phase 3, piece B1.

    .venv/bin/python -m api.planner.calibrate            # fit, write the JSON and the report
    .venv/bin/python -m api.planner.calibrate --no-write # fit and print the summary only

Reads `data/nass_finney.parquet` (yields 2000-2024), `data/climate_history.parquet` and the observed
rows of `data/climate_monthly.parquet`; optionally `data/logs/phase3/openet_by_crop.csv`.
Writes `api/planner/crop_response.json` (tracked), `data/crop_response_report.md` and, under
`data/logs/phase3/`, `crop_response_fit.csv` (every series, year by year) and `calibrate.log`.

What is fitted
--------------
For each crop and practice:   yield(year) = trend(year) x scale x heat factor x water factor

* at most two response numbers: `ky` (dryland only) and `heat_loss`; irrigated crops have one
  (`heat_loss`; their water factor is 1);
* the technology trend is a straight line, fitted TOGETHER with the response, not before it. The
  design note said "remove the trend first". We deviate on purpose: the yield record starts in the
  2000-2003 drought and ends in the wet years 2014-2018, so a trend fitted first swallows weather
  (dryland corn: +3.7 % a year fitted alone, +0.9 % a year fitted together with the weather);
* `scale` is not a free number: it is 1 / (mean of heat x water over the calibration years).

Honesty rules
-------------
* Fewer than `MIN_YEARS` (10) usable years: nothing is fitted, textbook values, "weak".
* Bounds: Ky within half to one-and-a-half times the FAO value; heat loss within crops.py bounds.
  A fitted value on a bound (within 5 % of the range) is not believed: that number goes back to its
  textbook value and the other one is refitted. If that one lands on a bound too: all textbook.
* Skill is measured leaving one year out: the trend and the response are refitted without that year
  and asked to predict it; the yardstick is the trend alone, refitted the same way.
  skill = 1 - (squared error of the model) / (squared error of the trend alone).
  Skill at or below zero: all textbook, "weak".
* Verdict: good (skill 0.5 or more), fair (0.2 to 0.5), weak (below 0.2, or nothing fitted).
* Every series carries a plain `status_text`. A number that went back to its textbook value is called
  what it is: an assumption the county record did not support (dryland wheat's heat loss is the one
  that matters: fitted freely it came out near zero, and the textbook 1 % a day was kept).
* `heat_cap`: the most hot days any observed year had, plus the margin of crops.py. The planner does
  not let the heat factor see more hot days than that (response.py).

Which yields
------------
* By-practice yields from the county or the Southwest district. State rows are dropped: the Kansas
  average is mostly eastern Kansas.
* "All practices" yields extend a series only where one practice has at least 85 % of the crop's
  area in the county (NASS harvested acres in the split years and the fields' crop history): dryland
  sorghum, irrigated soybeans, irrigated alfalfa (alfalfa has no split at all). Those years are
  multiplied by the median by-practice / all-practices ratio of the overlap years, and are marked.
* County and district values are used side by side; each year's level is recorded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from api.planner import response, water
from api.planner.crops import (CROP_TABLE, CROPS, FAIL_BELOW, HEAT_LOSS_BOUNDS, MAX_YIELD_REL, PRACTICES, PREV_KINDS,
                               WHEAT_HEAT_LOSS_BOUNDS)
from pipeline.region import DATA_DIR as REGION_DATA_DIR, nass_path  # noqa: E402  (data/ for Finney, data/regions/<fips>/ otherwise)

ROOT = Path(__file__).resolve().parents[2]
NASS_PATH = nass_path()
HISTORY_PATH = REGION_DATA_DIR / "climate_history.parquet"
MONTHLY_PATH = REGION_DATA_DIR / "climate_monthly.parquet"
OPENET_PATH = REGION_DATA_DIR / "logs" / "phase3" / "openet_by_crop.csv"
REPORT_PATH = REGION_DATA_DIR / "crop_response_report.md"
LOG_DIR = REGION_DATA_DIR / "logs" / "phase3"

MIN_YEARS = 10
EDGE = 0.05                 # a fitted value this close to a bound (share of the range) counts as on it
KY_RANGE = (0.5, 1.5)       # bounds as multiples of the FAO Ky
GOOD, FAIR = 0.5, 0.2
DOMINANT_SHARE = 0.85
# Share of each crop's area under the named practice in the fields' crop history 2009-2024
# (data/fields.parquet x data/field_traits.parquet; computed once, see crops.py "CDL").
CDL_SHARE = {("sorghum", "dryland"): 0.90, ("soybeans", "irrigated"): 0.98, ("alfalfa", "irrigated"): 0.97,
             ("corn", "irrigated"): 0.72, ("winter wheat", "dryland"): 0.76}
BAD_YEARS = (2002, 2011, 2012, 2022, 2023)
BAD_BELOW = 0.75            # "a bad year" in the check: under three quarters of normal
REAL_CROPS = [c for c in CROPS if c != "fallow"]


# --- observed yield series ---------------------------------------------------------------------------

def build_series(nass: pd.DataFrame) -> dict:
    """{(crop, practice): DataFrame[year, value, level, source]} following "Which yields" above.

    `source` is "by practice" or "all practices x <ratio>". A series may be empty.
    """
    y = nass[(nass["stat"] == "yield") & (nass["level"] != "state")]
    acres = nass[(nass["stat"] == "harvested_acres") & (nass["level"] == "county")]
    out = {}
    for crop in REAL_CROPS:
        allp = y[(y["crop"] == crop) & (y["practice"] == "all")].set_index("year")
        for practice in PRACTICES:
            by = y[(y["crop"] == crop) & (y["practice"] == practice)].set_index("year")
            frame = pd.DataFrame({"value": by["value"], "level": by["level"], "source": "by practice"})
            share = None
            if (crop, practice) in CDL_SHARE and CDL_SHARE[(crop, practice)] >= DOMINANT_SHARE:
                a = acres[acres["crop"] == crop].pivot_table(index="year", columns="practice", values="value")
                if {"irrigated", "dryland"} <= set(a.columns):
                    both = a.dropna(subset=["irrigated", "dryland"])
                    if len(both):
                        share = float((both[practice] / (both["irrigated"] + both["dryland"])).median())
                dominant = share is None or share >= DOMINANT_SHARE   # no split at all (alfalfa): the CDL share decides
                if dominant:
                    overlap = frame.index.intersection(allp.index)
                    ratio = float((frame.loc[overlap, "value"] / allp.loc[overlap, "value"]).median()) if len(overlap) else 1.0
                    extra = allp[~allp.index.isin(frame.index)]
                    ext = pd.DataFrame({"value": extra["value"] * ratio, "level": extra["level"],
                                        "source": f"all practices x {ratio:.2f}"})
                    frame = pd.concat([frame, ext]).sort_index()
            frame = frame.reset_index().rename(columns={"index": "year"})
            frame.attrs["nass_share"] = share
            out[(crop, practice)] = frame
    return out


# --- model inputs for the observed years -------------------------------------------------------------

def observed_inputs(history: pd.DataFrame, monthly: pd.DataFrame, constants: water.WaterConstants) -> dict:
    """{crop: {"years", "days", "ratio": {kind: rain-fed ET / need}}} for the observed years."""
    obs = monthly
    if "model" in monthly.columns:
        obs = monthly[monthly["model"] == water.OBSERVED]
    grid, bals = water.balance(obs, constants, crops=REAL_CROPS)
    hist = history.set_index("year")
    years = np.array([y for y, ok in zip(grid.years, grid.complete[0]) if ok and y in hist.index], dtype=int)
    yi = np.searchsorted(grid.years, years)
    out = {"_grid": grid, "_bals": bals, "_years": years}
    for crop in REAL_CROPS:
        out[crop] = {
            "years": years,
            "days": hist.loc[years, CROP_TABLE[crop].heat_column].to_numpy(dtype=float),
            "ratio": {k: bals[crop].et_ratio(k)[0][yi] for k in PREV_KINDS},
        }
    return out


def raw_response(days, ratios: dict, weights: dict, ky_grid, h_grid, irrigated: bool) -> np.ndarray:
    """heat factor x water factor (county mix of previous crops) on a grid: [len(ky), len(h), years]."""
    days = np.asarray(days, dtype=float)
    heat = np.stack([response.heat_factor(days, h) for h in h_grid])                     # [H, Y]
    if irrigated:
        wat = np.ones((len(ky_grid), len(days)))
    else:
        wat = np.stack([sum(w * response.water_factor(ratios[k], ky) for k, w in weights.items())
                        for ky in ky_grid])                                              # [K, Y]
    return wat[:, None, :] * heat[None, :, :]


def fit_grid(t, y, raw) -> dict:
    """Least squares of y = (a + b t) x raw over the whole grid; the best grid cell and its line."""
    x1, x2 = raw, raw * t
    a11, a12, a22 = (x1 * x1).sum(2), (x1 * x2).sum(2), (x2 * x2).sum(2)
    b1, b2 = (x1 * y).sum(2), (x2 * y).sum(2)
    det = a11 * a22 - a12 ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        a = (b1 * a22 - b2 * a12) / det
        b = (a11 * b2 - a12 * b1) / det
        line = a[:, :, None] + b[:, :, None] * t
        sse = ((line * raw - y) ** 2).sum(2)
    sse = np.where(np.isfinite(sse) & (line > 0).all(axis=2), sse, np.inf)
    i, j = np.unravel_index(np.argmin(sse), sse.shape)
    return {"i": int(i), "j": int(j), "a": float(a[i, j]), "b": float(b[i, j]), "sse": float(sse[i, j])}


def _trend_only(t, y):
    A = np.c_[np.ones(len(t)), t]
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    return coef, A @ coef


def _on_edge(value: float, grid) -> bool:
    lo, hi = float(grid[0]), float(grid[-1])
    if hi <= lo:
        return False
    return value <= lo + EDGE * (hi - lo) or value >= hi - EDGE * (hi - lo)


def fit_structure(years, y, days, ratios, weights, ky_grid, h_grid, irrigated: bool) -> dict:
    """Fit one structure (which numbers are free is set by the grids) and score it leaving one year out."""
    years = np.asarray(years, dtype=float)
    y = np.asarray(y, dtype=float)
    t = years - years.mean()
    raw = raw_response(days, ratios, weights, ky_grid, h_grid, irrigated)
    best = fit_grid(t, y, raw)
    i, j = best["i"], best["j"]
    r = raw[i, j]
    level = float(r.mean())
    _, trend = _trend_only(t, y)
    sst = float(((y - trend) ** 2).sum())
    normal = (best["a"] + best["b"] * t) * level        # yield of a normal-weather year, year by year
    rel_model = r / level
    # leave one year out
    err_model, err_trend, rel_err = [], [], []
    n = len(y)
    for k in range(n):
        m = np.arange(n) != k
        f = fit_grid(t[m], y[m], raw[:, :, m])
        rk = raw[f["i"], f["j"], k]
        line_k = f["a"] + f["b"] * t[k]
        pred = line_k * rk
        coef, _ = _trend_only(t[m], y[m])
        base = coef[0] + coef[1] * t[k]
        err_model.append(y[k] - pred)
        err_trend.append(y[k] - base)
        rel_err.append((y[k] - pred) / (line_k * raw[f["i"], f["j"]][m].mean()))
    err_model, err_trend = np.array(err_model), np.array(err_trend)
    return {
        "ky": float(ky_grid[i]), "heat_loss": float(h_grid[j]), "scale": 1.0 / level,
        "ky_on_edge": (not irrigated) and _on_edge(float(ky_grid[i]), ky_grid),
        "h_on_edge": _on_edge(float(h_grid[j]), h_grid),
        "r2": 1.0 - best["sse"] / sst if sst > 0 else float("nan"),
        "loo_skill": 1.0 - float((err_model ** 2).sum()) / float((err_trend ** 2).sum()),
        "loo_rmse": float(np.sqrt((err_model ** 2).mean())),
        "resid_sd": float(np.sqrt(np.mean(np.square(rel_err)))),
        "normal_by_year": normal, "rel_model": rel_model, "rel_obs": y / normal,
        "normal_yield": float(normal[-1]), "trend_pct_per_year": 100.0 * best["b"] / best["a"],
    }


def _grids(crop: str, practice: str):
    c = CROP_TABLE[crop]
    lo, hi = (WHEAT_HEAT_LOSS_BOUNDS if crop == "winter wheat" else HEAT_LOSS_BOUNDS)
    h_grid = np.linspace(lo, hi, 121)
    ky_grid = np.round(np.linspace(KY_RANGE[0] * c.ky, KY_RANGE[1] * c.ky, 101), 4) if practice == "dryland" \
        else np.array([0.0])
    return ky_grid, h_grid


_TEXTBOOK_WORDS = {True: "the heat loss is the textbook value, an assumption.",
                   False: "the heat loss and Ky are textbook values, an assumption."}


def calibrate_series(crop: str, practice: str, obs: pd.DataFrame, inputs: dict) -> dict:
    """Fit one crop x practice following the honesty rules. Returns the JSON entry plus year-by-year rows."""
    c = CROP_TABLE[crop]
    irrigated = practice == "irrigated"
    weights = c.typical_prev[practice]
    ky0 = c.ky if not irrigated else 0.0
    h0 = c.heat_loss[practice]
    inp = inputs[crop]
    obs = obs[obs["year"].isin(inp["years"])].sort_values("year") if len(obs) else obs
    n = len(obs)
    idx = np.searchsorted(inp["years"], obs["year"].to_numpy(dtype=int)) if n else np.array([], dtype=int)
    years = obs["year"].to_numpy(dtype=int) if n else np.array([], dtype=int)
    y = obs["value"].to_numpy(dtype=float) if n else np.array([])
    days = inp["days"][idx]
    ratios = {k: v[idx] for k, v in inp["ratio"].items()}
    ky_grid, h_grid = _grids(crop, practice)

    entry = {
        "unit": c.unit, "n_years": int(n),
        "years": f"{years.min()}-{years.max()}" if n else "",
        "levels": {k: int(v) for k, v in obs["level"].value_counts().items()} if n else {},
        "from_all_practices": [int(v) for v in obs.loc[obs["source"] != "by practice", "year"]] if n else [],
        "splice": next((s for s in obs["source"].unique() if s != "by practice"), None) if n else None,
        "textbook": {"ky": ky0, "heat_loss": h0},
    }
    notes, attempts = [], []
    status_text = "Fitted to the county yields."

    def attempt(label, kg, hg):
        f = fit_structure(years, y, days, ratios, weights, kg, hg, irrigated)
        attempts.append(f"{label}: ky {f['ky']:.2f}, heat loss {f['heat_loss']:.4f}, R2 {f['r2']:.2f}, "
                        f"leave-one-out skill {f['loo_skill']:.2f}")
        return f

    chosen, free = None, []
    if n < MIN_YEARS:
        notes.append(f"only {n} usable years (fewer than {MIN_YEARS}): nothing fitted" if n else "no yield data: nothing fitted")
        status_text = (f"Nothing fitted ({'only ' + str(n) + ' usable years' if n else 'no yield data'}): "
                       + _TEXTBOOK_WORDS[irrigated])
    else:
        full = attempt("both free" if not irrigated else "heat loss free", ky_grid, h_grid)
        if irrigated:
            if full["h_on_edge"]:
                notes.append(f"fitted heat loss {full['heat_loss']:.4f} is on a bound: textbook kept")
                status_text = (f"Heat loss is an assumption the county record did not support: fitted freely it came out "
                               f"at {100 * full['heat_loss']:.2f}% a day, on a bound, so the textbook {100 * h0:g}% a day "
                               "was kept.")
            else:
                chosen, free = full, ["heat_loss"]
        elif not full["ky_on_edge"] and not full["h_on_edge"]:
            chosen, free = full, ["ky", "heat_loss"]
        elif full["ky_on_edge"] and full["h_on_edge"]:
            notes.append(f"fitted ky {full['ky']:.2f} and heat loss {full['heat_loss']:.4f} are both on a bound: textbook kept")
            status_text = ("Ky and heat loss are assumptions the county record did not support: fitted freely both "
                           "landed on a bound, so the textbook values were kept.")
        elif full["ky_on_edge"]:
            notes.append(f"fitted ky {full['ky']:.2f} is on a bound (allowed {ky_grid[0]:.2f}-{ky_grid[-1]:.2f}): "
                         f"FAO ky {ky0} kept, heat loss refitted")
            second = attempt("ky textbook", np.array([ky0]), h_grid)
            if second["h_on_edge"]:
                notes.append(f"refitted heat loss {second['heat_loss']:.4f} is on a bound too: textbook kept")
                status_text = ("Ky and heat loss are assumptions the county record did not support: fitted freely each "
                               "landed on a bound, so the textbook values were kept.")
            else:
                chosen, free = second, ["heat_loss"]
                status_text = (f"Ky is the FAO textbook value {ky0:g}, an assumption: fitted freely it landed on a bound "
                               f"({full['ky']:.2f}). The heat loss is fitted to the county yields.")
        else:
            notes.append(f"fitted heat loss {full['heat_loss']:.4f} is on a bound (allowed {h_grid[0]:.4f}-{h_grid[-1]:.4f}): "
                         f"textbook heat loss {h0} kept, ky refitted")
            second = attempt("heat loss textbook", ky_grid, np.array([h0]))
            if second["ky_on_edge"]:
                notes.append(f"refitted ky {second['ky']:.2f} is on a bound too: textbook kept")
                status_text = ("Ky and heat loss are assumptions the county record did not support: fitted freely each "
                               "landed on a bound, so the textbook values were kept.")
            else:
                chosen, free = second, ["ky"]
                status_text = (f"Heat loss is an assumption the county record did not support: the textbook "
                               f"{100 * h0:g}% a day was kept although, fitted freely, it came out at "
                               f"{100 * full['heat_loss']:.2f}% a day (R2 {full['r2']:.2f} against {second['r2']:.2f} with "
                               "the textbook value). Ky is fitted to the county yields.")
        if chosen is not None and not chosen["loo_skill"] > 0:
            notes.append(f"leave-one-out skill {chosen['loo_skill']:.2f} is not above zero: textbook kept")
            status_text = ("Nothing fitted (the fit predicted left-out years no better than the trend): "
                           + _TEXTBOOK_WORDS[irrigated])
            chosen, free = None, []

    rows = None
    if chosen is not None:
        f = chosen
        entry.update({
            "ky": round(f["ky"], 3), "heat_loss": round(f["heat_loss"], 5), "scale": round(f["scale"], 4),
            "normal_yield": round(f["normal_yield"], 2), "trend_pct_per_year": round(f["trend_pct_per_year"], 2),
            "resid_sd": round(f["resid_sd"], 3), "r2": round(f["r2"], 3), "loo_skill": round(f["loo_skill"], 3),
            "loo_rmse": round(f["loo_rmse"], 2), "free_parameters": free,
            "status": "fitted" if len(free) == (1 if irrigated else 2) else "partly textbook",
        })
        entry["verdict"] = "good" if f["loo_skill"] >= GOOD else "fair" if f["loo_skill"] >= FAIR else "weak"
        rows = pd.DataFrame({"year": years, "obs": y, "normal": f["normal_by_year"], "rel_obs": f["rel_obs"],
                             "rel_model": f["rel_model"]})
    else:
        # textbook response; level from the whole observed record; normal yield from the recent data
        all_raw = raw_response(inp["days"], inp["ratio"], weights, np.array([ky0]), np.array([h0]), irrigated)[0, 0]
        scale = 1.0 / float(all_raw.mean())
        normal = float(np.median(y[-10:])) if n else float(c.normal_yield[practice])
        entry.update({"ky": ky0, "heat_loss": h0, "scale": round(scale, 4), "normal_yield": round(normal, 2),
                      "trend_pct_per_year": None, "resid_sd": None, "r2": None, "loo_skill": None, "loo_rmse": None,
                      "free_parameters": [], "status": "textbook", "verdict": "weak"})
        if n >= 3:
            rel_model = scale * all_raw[idx]
            rel_obs = y / normal
            sst = float(((rel_obs - rel_obs.mean()) ** 2).sum())
            entry["resid_sd"] = round(float(np.sqrt(np.mean((rel_obs - rel_model) ** 2))), 3)
            entry["r2"] = round(1.0 - float(((rel_obs - rel_model) ** 2).sum()) / sst, 3) if sst > 0 else None
            rows = pd.DataFrame({"year": years, "obs": y, "normal": normal, "rel_obs": rel_obs, "rel_model": rel_model})
        if not n:
            notes.append(f"normal yield {normal} is the crops.py constant")
    entry["status_text"] = status_text
    entry["notes"] = notes
    entry["attempts"] = attempts
    entry["diagnostics"] = None
    if rows is not None and n >= 5:
        dry_w = c.typical_prev["dryland"]
        ratio = sum(w * ratios[k] for k, w in dry_w.items())
        miss = (rows["rel_obs"] - rows["rel_model"]).to_numpy()
        worst = int(np.argmax(np.abs(miss)))
        entry["diagnostics"] = {
            "corr_yield_water_ratio": round(float(np.corrcoef(rows["rel_obs"], ratio)[0, 1]), 2),
            "corr_yield_hot_days": round(float(np.corrcoef(rows["rel_obs"], days)[0, 1]), 2),
            "corr_water_ratio_hot_days": round(float(np.corrcoef(ratio, days)[0, 1]), 2),
            "largest_miss": {"year": int(years[worst]), "real": round(float(rows["rel_obs"].iloc[worst]), 2),
                             "model": round(float(rows["rel_model"].iloc[worst]), 2)},
        }
    if rows is not None:
        rows.insert(0, "practice", practice)
        rows.insert(0, "crop", crop)
        rows["level"] = obs["level"].to_numpy()
        rows["source"] = obs["source"].to_numpy()
    return {"entry": entry, "rows": rows}


def textbook_scales(inputs: dict) -> dict:
    """Level of the pure textbook model under the observed climate (the `_SCALE` table of crops.py)."""
    out = {}
    for crop in REAL_CROPS:
        c = CROP_TABLE[crop]
        out[crop] = {}
        for practice in PRACTICES:
            irrigated = practice == "irrigated"
            raw = raw_response(inputs[crop]["days"], inputs[crop]["ratio"], c.typical_prev[practice],
                               np.array([c.ky]), np.array([c.heat_loss[practice]]), irrigated)[0, 0]
            out[crop][practice] = round(1.0 / float(raw.mean()), 3)
    return out


def calibrate(nass: pd.DataFrame, history: pd.DataFrame, monthly: pd.DataFrame,
              constants: water.WaterConstants | None = None) -> dict:
    """Everything: {"params": the JSON content, "rows": year-by-year table, "inputs": ..., "series": ...}."""
    constants = constants or water.DEFAULTS
    series = build_series(nass)
    inputs = observed_inputs(history, monthly, constants)
    entries, rows = {}, []
    for crop in REAL_CROPS:
        entries[crop] = {}
        for practice in PRACTICES:
            res = calibrate_series(crop, practice, series[(crop, practice)], inputs)
            res["entry"]["nass_share_of_area"] = series[(crop, practice)].attrs.get("nass_share")
            entries[crop][practice] = res["entry"]
            if res["rows"] is not None:
                rows.append(res["rows"])
    entries["fallow"] = {p: {"unit": "", "ky": 0.0, "heat_loss": 0.0, "scale": 1.0, "normal_yield": 0.0,
                             "resid_sd": None, "status": "textbook", "status_text": "No crop.", "verdict": "n/a",
                             "notes": ["no crop"]}
                         for p in PRACTICES}
    heat_cap = response.heat_cap_entry(response.observed_hot_day_max(history))
    for column, cap in heat_cap.items():
        cap["years"] = f"{int(history['year'].min())}-{int(history['year'].max())}"
    params = {
        "written": dt.date.today().isoformat(),
        "written_by": "python -m api.planner.calibrate",
        "model": "yield_rel = scale x max(0, 1 - heat_loss x hot days) x water factor; water factor = "
                 "max(0, 1 - ky x (1 - rain-fed ET / water need)) for dryland, 1 for irrigated (unless the irrigation "
                 "limit of the planner binds); hot days are capped at heat_cap; yield_rel is capped at max_yield_rel; "
                 "failed = yield_rel < fail_below",
        "sources": {
            "yields": "USDA NASS, Finney County and Kansas Southwest district, 2000-2024 (data/nass_finney.parquet)",
            "weather": "NASA POWER daily, county centre, 2000-2024 (data/climate_history.parquet, data/climate_monthly.parquet)",
            "kc": "FAO-56 Table 12 (Allen et al. 1998)", "ky": "FAO-33 / FAO-56 Table 24",
            "effective_rain": "USDA-SCS monthly formula (FAO CROPWAT)",
            "stored_soil_water": "Nielsen and Vigil 2010; K-State dryland research",
            "water_check": "OpenET ensemble April-September ET 2016-2024 (data/logs/phase3/openet_by_crop.csv)",
        },
        "constants": constants.to_dict(),
        "fail_below": FAIL_BELOW,
        "max_yield_rel": MAX_YIELD_REL,
        "heat_cap": heat_cap,
        "heat_cap_note": "Hot days fed to the heat factor are capped at the most any observed year had plus a margin "
                         "(crops.py HOT_DAYS_MARGIN): the heat loss was fitted, or assumed, inside that range only.",
        "typical_prev": {crop: CROP_TABLE[crop].typical_prev for crop in REAL_CROPS},
        "calibration_rules": {"min_years": MIN_YEARS, "ky_bounds_x_fao": list(KY_RANGE),
                              "heat_loss_bounds": list(HEAT_LOSS_BOUNDS),
                              "wheat_heat_loss_bounds": list(WHEAT_HEAT_LOSS_BOUNDS), "edge_share": EDGE,
                              "verdict": {"good": GOOD, "fair": FAIR}},
        "series": entries,
    }
    table = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    return {"params": params, "rows": table, "inputs": inputs, "series": series}


# --- checks ------------------------------------------------------------------------------------------

def observed_response(history: pd.DataFrame, monthly: pd.DataFrame, params: dict) -> pd.DataFrame:
    obs = monthly[monthly["model"] == water.OBSERVED] if "model" in monthly.columns else monthly
    merged = response._merge(response.default_params(), params)
    return response.crop_response(history, obs, merged)


def water_summary(resp: pd.DataFrame) -> pd.DataFrame:
    """Observed-climate means per crop: need, effective rain, stored water, pumped irrigation, shortfall."""
    r = resp[resp["crop"] != "fallow"]
    irr = r[r["practice"] == "irrigated"].groupby("crop")
    dry = r[r["practice"] == "dryland"].groupby("crop")
    out = pd.DataFrame({
        "water_need_mm": irr["water_need_mm"].mean(), "eff_rain_mm": irr["eff_rain_mm"].mean(),
        "stored_mm_irrigated": irr["stored_mm"].mean(), "irrigation_net_mm": irr["irrigation_net_mm"].mean(),
        "irrigation_mm": irr["irrigation_mm"].mean(), "irrigation_mm_max": irr["irrigation_mm"].max(),
        "stored_mm_dryland": dry["stored_mm"].mean(), "shortfall_mm": dry["shortfall_mm"].mean(),
        "shortfall_mm_after_fallow": dry["shortfall_mm_after_fallow"].mean(),
    })
    return out.reindex(REAL_CROPS)


def openet_check(inputs: dict, constants: water.WaterConstants, openet: pd.DataFrame | None) -> pd.DataFrame | None:
    """Modelled April-September ET 2016-2024 against the OpenET medians, irrigated and dryland."""
    if openet is None or not len(openet):
        return None
    grid, bals, years = inputs["_grid"], inputs["_bals"], inputs["_years"]
    sel = np.isin(grid.years, years[years >= 2016])
    oe = openet.set_index(["crop", "practice"])["median_et_gs_mm"]
    rows = []
    for crop in REAL_CROPS:
        tp = CROP_TABLE[crop].typical_prev
        for practice in PRACTICES:
            if (crop, practice) not in oe.index:
                continue
            irrigated = practice == "irrigated"
            model = sum(w * water.window_et(grid, bals[crop], k, irrigated, constants)[0][sel].mean()
                        for k, w in tp[practice].items())
            unscaled = None
            if irrigated:
                c1 = water.WaterConstants(**{**constants.to_dict(), "et0_scale": 1.0})
                b1 = water.crop_balance(grid, crop, c1)
                unscaled = sum(w * water.window_et(grid, b1, k, True, c1)[0][sel].mean() for k, w in tp[practice].items())
            rows.append({"crop": crop, "practice": practice, "model_mm": model, "openet_mm": float(oe[(crop, practice)]),
                         "gap_pct": 100.0 * (model / float(oe[(crop, practice)]) - 1.0),
                         "unscaled_gap_pct": None if unscaled is None else 100.0 * (unscaled / float(oe[(crop, practice)]) - 1.0)})
    out = pd.DataFrame(rows)
    out.attrs["window_rain_mm"] = float(sum(grid.take(grid.rain, [m - 1])[0, sel, 0].mean() for m in range(4, 10)))
    return out


def failure_check(nass: pd.DataFrame, resp: pd.DataFrame, fit_rows: pd.DataFrame, params: dict) -> dict:
    """How the model's bad years line up with the real ones. Returns tables for the report."""
    y = nass[nass["stat"] == "yield"]
    acres = nass[nass["stat"].isin(["planted_acres", "harvested_acres"])]
    aband = acres.pivot_table(index=["crop", "practice", "year"], columns="stat", values="value")
    if {"planted_acres", "harvested_acres"} <= set(aband.columns):
        aband = (1.0 - aband["harvested_acres"] / aband["planted_acres"]).clip(lower=0)
    else:
        aband = pd.Series(dtype=float)
    mod = resp.set_index(["crop", "practice", "year"])
    fit = fit_rows.set_index(["crop", "practice", "year"]) if len(fit_rows) else pd.DataFrame()

    # "all practices" yields relative to their own straight-line trend: rough evidence for the years
    # without a by-practice number (2020-2024); state rows are kept here and marked.
    all_rel = {}
    for crop in REAL_CROPS:
        a = y[(y["crop"] == crop) & (y["practice"] == "all")].sort_values("year")
        if len(a) >= 5:
            t = a["year"].to_numpy(dtype=float)
            coef, trend = _trend_only(t - t.mean(), a["value"].to_numpy(dtype=float))
            for yr, v, tr, lev in zip(a["year"], a["value"], trend, a["level"]):
                all_rel[(crop, int(yr))] = (float(v / tr), lev)

    bad_rows = []
    for crop, practice in [("corn", "dryland"), ("sorghum", "dryland"), ("winter wheat", "dryland"),
                           ("corn", "irrigated"), ("winter wheat", "irrigated")]:
        for year in BAD_YEARS:
            if (crop, practice, year) not in mod.index:
                continue
            m = mod.loc[(crop, practice, year)]
            row = {"crop": crop, "practice": practice, "year": year, "model_rel": float(m["yield_rel"]),
                   "model_failed": bool(m["failed"]), "fail_prob": float(m["fail_prob"]), "obs_rel": None,
                   "obs_source": "", "all_rel": None, "all_level": "", "abandoned": None}
            if len(fit) and (crop, practice, year) in fit.index:
                f = fit.loc[(crop, practice, year)]
                row["obs_rel"] = float(f["rel_obs"])
                row["obs_source"] = f"{f['source']}, {f['level']}"
            if (crop, year) in all_rel:
                row["all_rel"], row["all_level"] = all_rel[(crop, year)]
            for pr in (practice, "all"):
                if (crop, pr, year) in aband.index:
                    row["abandoned"] = float(aband.loc[(crop, pr, year)])
                    row["abandoned_practice"] = pr
                    break
            bad_rows.append(row)
    bad = pd.DataFrame(bad_rows)

    tally = []
    if len(fit_rows):
        for (crop, practice), g in fit_rows.groupby(["crop", "practice"], sort=False):
            years = g["year"].to_numpy()
            m = mod.loc[[(crop, practice, int(v)) for v in years]]
            obs_fail = g["rel_obs"].to_numpy() < FAIL_BELOW
            mod_fail = m["failed"].to_numpy()
            obs_bad = g["rel_obs"].to_numpy() < BAD_BELOW
            mod_bad = m["yield_rel"].to_numpy() < BAD_BELOW
            tally.append({
                "crop": crop, "practice": practice, "years": len(g),
                "obs_failed": int(obs_fail.sum()), "model_failed": int(mod_fail.sum()),
                "both_failed": int((obs_fail & mod_fail).sum()), "expected_failures": float(m["fail_prob"].sum()),
                "obs_bad": int(obs_bad.sum()), "model_bad": int(mod_bad.sum()), "both_bad": int((obs_bad & mod_bad).sum()),
                "obs_failed_years": [int(v) for v in years[obs_fail]], "model_failed_years": [int(v) for v in years[mod_fail]],
                "missed_bad_years": [int(v) for v in years[obs_bad & ~mod_bad]],
                "false_bad_years": [int(v) for v in years[~obs_bad & mod_bad]],
            })
    return {"bad": bad, "tally": pd.DataFrame(tally)}


# --- report ------------------------------------------------------------------------------------------

def fit_lines(params: dict) -> list[str]:
    """One plain line per crop x practice (also printed by main)."""
    lines = []
    for crop in REAL_CROPS:
        for practice in PRACTICES:
            e = params["series"][crop][practice]
            lev = ", ".join(f"{k} {v}" for k, v in e.get("levels", {}).items()) or "no data"
            ext = f"; {len(e['from_all_practices'])} of them from all-practices yields" if e.get("from_all_practices") else ""
            free = ", ".join(e["free_parameters"]) or "none"
            r2 = "n/a" if e.get("r2") is None else f"{e['r2']:.2f}"
            if e["status"] == "textbook":
                skill = "not fitted"
            else:
                skill = f"{e['loo_skill']:.2f} (error {e['loo_rmse']:.1f} {e['unit']})"
            lines.append(
                f"{crop} {practice}: {e['n_years']} years {e['years']} ({lev}{ext}); free: {free}; "
                f"ky {e['ky']:.2f}, heat loss {e['heat_loss']:.4f}/day, normal {e['normal_yield']:.1f} {e['unit']}; "
                f"R2 {r2}; leave-one-out skill {skill}; {e['status']}, {e['verdict']}")
    return lines


def write_report(path: Path, result: dict, resp: pd.DataFrame, check: dict, openet: pd.DataFrame | None,
                 wsum: pd.DataFrame) -> None:
    p = result["params"]
    c = p["constants"]
    L = []
    add = L.append
    add("# Crop response to heat and water: calibration report")
    add("")
    add(f"Written by `python -m api.planner.calibrate` on {p['written']}. Parameters: `api/planner/crop_response.json`. "
        "Year-by-year numbers: `data/logs/phase3/crop_response_fit.csv`.")
    add("")
    add("## The model")
    add("")
    add("- yield = normal yield x scale x heat factor x water factor.")
    add("- heat factor = 1 - heat loss x hot days (growing-season days above 35 C; winter wheat: May-June days above 32 C).")
    caps = p.get("heat_cap") or response.heat_cap_entry()
    add("- Hot days are capped before they are used: " + "; ".join(
        f"{'summer days above 35 C' if col == 'hot_days' else 'May-June days above 32 C'} at {v['cap']:.0f} "
        f"(the most any year of {v.get('years', 'the record')} had, {v['observed_max']:.0f}, plus {v['margin']:.0f})"
        for col, v in caps.items()) + ". The heat loss was fitted, or assumed, inside the record's range; the climate "
        "models reach 120 hot days in single years, and a straight line that far out is a guess.")
    add("- water factor, dryland = 1 - Ky x (1 - rain-fed ET / water need) (FAO-33); irrigated = 1 "
        "(in the planner: unless the pumping limit binds; then the unmet share goes through the same rule with the crop's dryland Ky).")
    add(f"- water need = FAO-56 Kc x reference ET x {c['et0_scale']}; rain-fed ET = effective rain (USDA-SCS) plus soil water stored "
        f"before planting ({c['fallow_efficiency']:.0%} of the rain since the previous harvest, at most {c['awc_mm']:.0f} mm), through a monthly bucket.")
    add(f"- failed = modelled yield below {p['fail_below']:.0%} of normal. Fallow never fails. "
        f"No year is credited with more than {p['max_yield_rel']:g} times the normal yield.")
    add("- The trend (technology) is a straight line fitted together with the response, not before it: the record starts in "
        "drought and ends wet, so a trend fitted first takes weather for technology.")
    add("- Observed weather is NASA POWER, about 1.6 C warmer than the Garden City station; thresholds apply to that series, "
        "and the climate-model series were corrected to it.")
    add("")
    add("## Fit per crop and practice")
    add("")
    add("R2: share of the year-to-year variance around the trend that the model explains, fitted on all years. "
        "Leave-one-out skill: the same, but each year is predicted by a model fitted without it (0 = no better than the trend alone). "
        "Error: leave-one-out root-mean-square error.")
    add("")
    add("| crop | practice | years used | level | free numbers | Ky | heat loss / day | normal yield | trend %/yr | R2 | leave-one-out skill | error | verdict |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for crop in REAL_CROPS:
        for practice in PRACTICES:
            e = p["series"][crop][practice]
            lev = ", ".join(f"{k} {v}" for k, v in e.get("levels", {}).items()) or "-"
            if e.get("from_all_practices"):
                lev += f" ({len(e['from_all_practices'])} from all-practices)"
            fmt = lambda v, f: "-" if v is None else format(v, f)
            add(f"| {crop} | {practice} | {e['n_years']} ({e['years'] or '-'}) | {lev} | {', '.join(e['free_parameters']) or 'none'} | "
                f"{e['ky']:.2f} | {e['heat_loss']:.4f} | {e['normal_yield']:.1f} {e['unit']} | {fmt(e['trend_pct_per_year'], '+.1f')} | "
                f"{fmt(e['r2'], '.2f')} | {fmt(e['loo_skill'], '.2f')} | {fmt(e['loo_rmse'], '.1f')} | {e['verdict']} ({e['status']}) |")
    add("")
    add("For a textbook series the R2 column is the textbook model against the raw yields (no trend removed); nothing was fitted, so there is no leave-one-out number.")
    add("")
    add("### Which years came from where")
    add("")
    for crop in REAL_CROPS:
        for practice in PRACTICES:
            e = p["series"][crop][practice]
            if not e["n_years"]:
                add(f"- {crop} {practice}: no yields.")
                continue
            rows = result["rows"]
            g = rows[(rows["crop"] == crop) & (rows["practice"] == practice)] if len(rows) else pd.DataFrame()
            parts = []
            if len(g):
                for (src, lev), gg in g.groupby(["source", "level"], sort=False):
                    parts.append(f"{src}, {lev}: {_year_ranges(gg['year'])}")
            share = e.get("nass_share_of_area")
            tail = f" NASS harvested-acre share of this practice in the split years: {share:.0%}." if share is not None else ""
            add(f"- {crop} {practice}: " + "; ".join(parts) + "." + tail)
    add("")
    add("State rows were dropped everywhere (the Kansas average is mostly eastern Kansas). "
        "All-practices years were used only for dryland sorghum, irrigated soybeans and irrigated alfalfa, where that practice has at "
        f"least {DOMINANT_SHARE:.0%} of the crop's area; for corn (irrigated {CDL_SHARE[('corn', 'irrigated')]:.0%} of corn area in the fields' "
        f"history) and winter wheat (dryland {CDL_SHARE[('winter wheat', 'dryland')]:.0%}) they were not.")
    add("")
    add("## Weak spots")
    add("")
    for crop in REAL_CROPS:
        for practice in PRACTICES:
            e = p["series"][crop][practice]
            if e["status"] != "fitted" or e["verdict"] == "weak":
                why = "; ".join(e["notes"]) or "low skill"
                tried = (" Tried: " + " | ".join(e["attempts"]) + ".") if e.get("attempts") else ""
                add(f"- **{crop} {practice}** ({e['verdict']}, {e['status']}): {e.get('status_text', '')} [{why}.]{tried}")
    wheat = p["series"]["winter wheat"]["dryland"]
    if "heat_loss" not in wheat.get("free_parameters", []) and wheat.get("n_years", 0) >= MIN_YEARS:
        add(f"- **Dryland wheat's heat response is an assumption, not a finding.** The planner takes "
            f"{100 * wheat['heat_loss']:.1f} % of the yield off for every May-June day above 32 C because that is the textbook "
            "starting value; the county record did not support it (fitted freely the heat loss came out near zero and the "
            "water term explained the bad years). It is kept on purpose, as the cautious choice for a hotter future, and it "
            "decides part of the dryland ranking in 2060: with the fitted value wheat-fallow plans lose less.")
    diag = {(crop, practice): p["series"][crop][practice].get("diagnostics") for crop in REAL_CROPS for practice in PRACTICES}
    diag = {k: v for k, v in diag.items() if v and p["series"][k[0]][k[1]]["n_years"] >= MIN_YEARS}
    if diag:
        cors = [v["corr_water_ratio_hot_days"] for v in diag.values()]
        add(f"- Heat and drought come together here: over the calibration years hot days and the rain-fed ET ratio correlate at "
            f"{min(cors):.2f} to {max(cors):.2f}. The record therefore cannot say how much of a bad year was heat and how much was water, "
            "and the split between `ky` and `heat_loss` leans on the FAO values. It matters for the future: hot days rise in every "
            "climate model, rain does not fall in every one.")
        irr = {k: v for k, v in diag.items() if k[1] == "irrigated" and p["series"][k[0]][k[1]]["status"] != "textbook"}
        if irr:
            txt = "; ".join(f"{k[0]} {v['corr_yield_water_ratio']:+.2f} with the dryland water ratio, {v['corr_yield_hot_days']:+.2f} with hot days"
                            for k, v in irr.items())
            add("- Irrigated yields follow dry years too (correlation of the yield, relative to normal, " + txt + "). The model gives irrigated "
                "crops all the water they need and carries those years through the heat term alone; where the water ratio does as well as "
                "the hot days, part of what is booked as heat is really water that the wells or the irrigation plan did not deliver.")
        dry = {k: v for k, v in diag.items() if k[1] == "dryland"}
        if dry:
            add("- Largest single miss per dryland series: " + "; ".join(
                f"{k[0]} {v['largest_miss']['year']} (model {v['largest_miss']['model']:.2f}, real {v['largest_miss']['real']:.2f})"
                for k, v in dry.items()) + ".")
            if any(v["largest_miss"]["year"] == 2000 for v in dry.values()):
                add("- About 2000: the climate table starts in October 1999, so the soil water stored before the 2000 season is a long-term "
                    "mean, not that year's. If 1999 was wet, that is why the real 2000 yields held up through a hot, dry summer.")
    add("")
    add("## Water")
    add("")
    add("Observed climate 2000-2024, mean per year (mm). Irrigation is what has to be pumped (net need / "
        f"{c['pivot_efficiency']}); dryland columns use the county's usual previous crop.")
    add("")
    add("| crop | water need | effective rain in season | irrigation, net | irrigation, pumped | pumped, driest year | dryland stored at planting | dryland shortfall | shortfall after fallow |")
    add("|---|---|---|---|---|---|---|---|---|")
    for crop, r in wsum.iterrows():
        add(f"| {crop} | {r['water_need_mm']:.0f} | {r['eff_rain_mm']:.0f} | {r['irrigation_net_mm']:.0f} | {r['irrigation_mm']:.0f} | "
            f"{r['irrigation_mm_max']:.0f} | {r['stored_mm_dryland']:.0f} | {r['shortfall_mm']:.0f} | {r['shortfall_mm_after_fallow']:.0f} |")
    add("")
    if openet is not None:
        add("### Against OpenET (satellite ET, April-September, medians over field-years 2016-2024)")
        add("")
        add("Model: April-September of 2016-2024; months without the crop count the rain that is not stored. "
            "Irrigated = full need; dryland = what rain and soil supply.")
        add("")
        add("| crop | practice | model mm | OpenET mm | gap | gap without the 0.85 factor |")
        add("|---|---|---|---|---|---|")
        for _, r in openet.iterrows():
            un = "-" if r["unscaled_gap_pct"] is None or pd.isna(r["unscaled_gap_pct"]) else f"{r['unscaled_gap_pct']:+.0f}%"
            add(f"| {r['crop']} | {r['practice']} | {r['model_mm']:.0f} | {r['openet_mm']:.0f} | {r['gap_pct']:+.0f}% | {un} |")
        add("")
        irr = openet[openet["practice"] == "irrigated"]
        dry = openet[openet["practice"] == "dryland"]
        add(f"- Irrigated: with textbook Kc and unscaled reference ET the need was {irr['unscaled_gap_pct'].min():+.0f}% to "
            f"{irr['unscaled_gap_pct'].max():+.0f}% above OpenET. One factor for all crops (`et0_scale` = {c['et0_scale']}) leaves "
            f"{irr['gap_pct'].min():+.0f}% to {irr['gap_pct'].max():+.0f}%. No per-crop factor was used: "
            f"{', '.join(irr.loc[irr['gap_pct'] > 0, 'crop']) or 'no crop'} stay high, {', '.join(irr.loc[irr['gap_pct'] <= 0, 'crop']) or 'no crop'} low.")
        rain = openet.attrs.get("window_rain_mm")
        rain_txt = f" (about {rain:.0f} mm in 2016-2024)" if rain is not None else ""
        add(f"- Dryland: the model's rain-fed ET is {dry['gap_pct'].min():+.0f}% to {dry['gap_pct'].max():+.0f}% against OpenET's dryland medians "
            f"({dry['openet_mm'].min():.0f}-{dry['openet_mm'].max():.0f} mm). Those medians are well above the April-September rain itself{rain_txt}, "
            "more than rain and stored soil water can supply year after year, so they are an upper bound (some fields we call dryland get water, "
            "and satellite ET tends to read high over dry land). Nothing was tuned to them.")
        add("")
    add("## Failure check")
    add("")
    add(f"Rule: failed = modelled yield below {p['fail_below']:.0%} of normal. `fail_prob` is the chance of that given the model's own "
        "leave-one-out error. Below, 'bad' means under 75 % of normal.")
    add("")
    t = check["tally"]
    if len(t):
        add("| crop | practice | years | real failures | modelled failures | both | expected failures (sum of fail_prob) | real bad years | modelled bad | both | bad years the model missed | modelled bad, really not |")
        add("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for _, r in t.iterrows():
            add(f"| {r['crop']} | {r['practice']} | {r['years']} | {r['obs_failed']} {_yl(r['obs_failed_years'])} | {r['model_failed']} {_yl(r['model_failed_years'])} | "
                f"{r['both_failed']} | {r['expected_failures']:.1f} | {r['obs_bad']} | {r['model_bad']} | {r['both_bad']} | "
                f"{_yl(r['missed_bad_years']) or '-'} | {_yl(r['false_bad_years']) or '-'} |")
        add("")
    add("### The known bad years")
    add("")
    add("Observed = by-practice yield / its normal where NASS has one; otherwise the all-practices yield against its own trend "
        "(rough: it mixes irrigated and dryland, and state rows are eastern Kansas). Abandoned = 1 - harvested / planted "
        "(overstated for corn and sorghum: silage counts as not harvested).")
    add("")
    add("| crop | practice | year | model | failed? | fail_prob | observed (by practice) | all-practices vs trend | abandoned |")
    add("|---|---|---|---|---|---|---|---|---|")
    for _, r in check["bad"].iterrows():
        obs = "-" if r["obs_rel"] is None or pd.isna(r["obs_rel"]) else f"{r['obs_rel']:.2f} ({r['obs_source']})"
        al = "-" if r["all_rel"] is None or pd.isna(r["all_rel"]) else f"{r['all_rel']:.2f} ({r['all_level']})"
        ab = "-" if r["abandoned"] is None or pd.isna(r["abandoned"]) else f"{r['abandoned']:.0%} ({r.get('abandoned_practice', '')})"
        add(f"| {r['crop']} | {r['practice']} | {r['year']} | {r['model_rel']:.2f} | {'yes' if r['model_failed'] else 'no'} | {r['fail_prob']:.2f} | {obs} | {al} | {ab} |")
    add("")
    for line in bad_year_sentences(check["bad"]):
        add(f"- {line}")
    add("")
    add("## Limits")
    add("")
    add("- 10-25 county yields per series; two response numbers at most. The leave-one-out skill is the number to trust.")
    add("- County and district averages hide field-to-field spread: a county yield at 60 % of normal means many fields failed outright. "
        "The 50 % rule on a county average is therefore a strict test; use `fail_prob` for rates.")
    add("- One weather series for the whole county; no hail, freeze, disease or planting-date effects. Water is added up over the "
        "season, so rain that comes too late still counts.")
    add("- In this calibration irrigated crops are fully watered. The planner adds a pumping limit per acre per year on "
        "top (the unmet share goes through the FAO-33 rule); the limit was not part of the fit, and a well that weakens "
        "over the years is not modelled.")
    add("- Hot days beyond the cap count as the cap: the model says nothing about what a year with 100 or 120 hot days "
        "does that a year with 85 does not.")
    add("- The textbook heat-loss values in crops.py are our rounded reading of the literature, which reports losses per degree-day, not per hot day.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n")


def _yl(years) -> str:
    years = list(years)
    return "(" + ", ".join(str(v) for v in years) + ")" if years else ""


def _year_ranges(years) -> str:
    ys = sorted(int(v) for v in years)
    out, start, prev = [], ys[0], ys[0]
    for v in ys[1:]:
        if v != prev + 1:
            out.append(f"{start}-{prev}" if prev > start else f"{start}")
            start = v
        prev = v
    out.append(f"{start}-{prev}" if prev > start else f"{start}")
    return ", ".join(out)


def _evidence(r) -> tuple:
    """(relative yield, label) of the best record for one row of the bad-year table; (None, "") if none.

    A by-practice yield is the record. Failing that, the all-practices yield against its own trend,
    but only where this practice has at least 70 % of the crop's area (otherwise it says nothing about
    this practice); a state row is labelled, because the Kansas average is mostly eastern Kansas.
    """
    if r["obs_rel"] is not None and not pd.isna(r["obs_rel"]):
        return float(r["obs_rel"]), ""
    share = CDL_SHARE.get((r["crop"], r["practice"]), 0.0)
    if r["all_rel"] is not None and not pd.isna(r["all_rel"]) and share >= 0.7:
        label = "all practices" + (", state row only" if r["all_level"] == "state" else "")
        return float(r["all_rel"]), label
    return None, ""


def bad_year_sentences(bad: pd.DataFrame) -> list[str]:
    """Plain sentences: per known bad year, where the model agrees with the record and where it misses."""
    out = []
    if not len(bad):
        return out
    for year in BAD_YEARS:
        g = bad[bad["year"] == year]
        hits, misses, quiet, unknown = [], [], [], []
        for _, r in g.iterrows():
            obs, label = _evidence(r)
            name = f"{r['practice']} {r['crop']}"
            ab = r.get("abandoned")
            ab_txt = "" if ab is None or pd.isna(ab) else f", {ab:.0%} of {r.get('abandoned_practice', '')} acres abandoned"
            if obs is None:
                if r["model_rel"] < BAD_BELOW:
                    unknown.append(f"{name} (model {r['model_rel']:.2f}; no yield record for this practice{ab_txt})")
                continue
            pair = f"{name} (model {r['model_rel']:.2f}, real {obs:.2f}{', ' + label if label else ''}{ab_txt})"
            if obs < BAD_BELOW and r["model_rel"] < BAD_BELOW:
                hits.append(pair)
            elif obs < BAD_BELOW:
                misses.append(pair)
            elif r["model_rel"] < BAD_BELOW:
                quiet.append(pair)
        parts = []
        if hits:
            parts.append("model and record both bad: " + "; ".join(hits))
        if misses:
            parts.append("MISSED (really bad, model not): " + "; ".join(misses))
        if quiet:
            parts.append("model bad, record not: " + "; ".join(quiet))
        if unknown:
            parts.append("model bad, cannot be checked: " + "; ".join(unknown))
        out.append(f"{year}: " + (". ".join(parts) if parts else "neither the model nor the record is under 75 % for these series") + ".")
    wheat23 = bad[(bad["year"] == 2023) & (bad["crop"] == "winter wheat") & (bad["practice"] == "dryland")]
    if len(wheat23):
        r = wheat23.iloc[0]
        ab = r.get("abandoned")
        if r["model_rel"] >= BAD_BELOW and ab is not None and not pd.isna(ab) and ab >= 0.2:
            out.append(f"2023 wheat: {ab:.0%} of wheat acres were abandoned ({r.get('abandoned_practice', '')} practices, the worst of the record) "
                       f"but the model gives dryland wheat {r['model_rel']:.2f}. The autumn and winter after the 2022 drought were very dry and the rain "
                       "came in May and June 2023, too late for a crop already lost. The model adds up water over the season and credits "
                       "that late rain: a miss.")
    return out


# --- command line ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", default=str(response.PARAMS_PATH), help="where to write the parameters")
    ap.add_argument("--report", default=str(REPORT_PATH))
    ap.add_argument("--log-dir", default=str(LOG_DIR))
    ap.add_argument("--no-write", action="store_true", help="fit and print the summary; write nothing")
    args = ap.parse_args(argv)

    missing = [p for p in (NASS_PATH, HISTORY_PATH, MONTHLY_PATH) if not p.exists()]
    if missing:
        print("calibrate: missing " + ", ".join(str(p.relative_to(ROOT)) for p in missing)
              + " (build them with `python -m pipeline.nass` and `python -m pipeline.climate.build`)", file=sys.stderr)
        return 2
    nass = pd.read_parquet(NASS_PATH)
    history = pd.read_parquet(HISTORY_PATH)
    monthly = pd.read_parquet(MONTHLY_PATH)
    monthly = monthly[monthly["model"] == water.OBSERVED]
    openet_raw = pd.read_csv(OPENET_PATH) if OPENET_PATH.exists() else None

    result = calibrate(nass, history, monthly)
    params = result["params"]
    constants = water.WaterConstants.from_dict(params["constants"])
    resp = observed_response(history, monthly, params)
    check = failure_check(nass, resp, result["rows"], params)
    openet = openet_check(result["inputs"], constants, openet_raw)
    wsum = water_summary(resp)
    scales = textbook_scales(result["inputs"])

    lines = fit_lines(params)
    cap_line = "hot-day cap: " + ", ".join(f"{col} {v['observed_max']:.0f} + {v['margin']:.0f} = {v['cap']:.0f}"
                                           for col, v in params["heat_cap"].items())
    log = [f"calibrate {dt.datetime.now().isoformat(timespec='seconds')}", ""] + lines + [""]
    log += [cap_line, "", "textbook scale (for crops.py _SCALE): " + json.dumps(scales), ""]
    for crop in REAL_CROPS:
        for practice in PRACTICES:
            e = params["series"][crop][practice]
            log.append(f"{crop} {practice}: " + json.dumps({k: v for k, v in e.items() if k not in ("textbook",)}))
    log += ["", "water summary (observed 2000-2024 means, mm):", wsum.round(0).to_string(), ""]
    if openet is not None:
        log += ["OpenET check:", openet.round(1).to_string(), ""]
    log += ["failure tally:", check["tally"].to_string() if len(check["tally"]) else "(none)", "",
            "known bad years:", check["bad"].round(2).to_string() if len(check["bad"]) else "(none)", ""]
    log += bad_year_sentences(check["bad"])

    if not args.no_write:
        Path(args.json).write_text(json.dumps(params, indent=2) + "\n")
        write_report(Path(args.report), result, resp, check, openet, wsum)
        log_dir = Path(args.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "calibrate.log").write_text("\n".join(log) + "\n")
        if len(result["rows"]):
            fit = result["rows"].merge(
                resp[["crop", "practice", "year", "yield_rel", "failed", "fail_prob", "heat_factor", "water_factor",
                      "water_need_mm", "stored_mm", "shortfall_mm", "irrigation_mm"]],
                on=["crop", "practice", "year"], how="left")
            fit.round(3).to_csv(log_dir / "crop_response_fit.csv", index=False)
        resp.round(3).to_csv(log_dir / "crop_response_observed.csv", index=False)

    for line in lines:
        print(line)
    print(cap_line)
    if openet is not None:
        irr = openet[openet["practice"] == "irrigated"]
        print(f"water need vs OpenET irrigated: {irr['gap_pct'].min():+.0f}% to {irr['gap_pct'].max():+.0f}% "
              f"(et0_scale {constants.et0_scale})")
    if not args.no_write:
        print(f"wrote {args.json}, {args.report}, {args.log_dir}/calibrate.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
