"""Build the Phase 3 climate tables: observed weather, six climate models x two scenarios, 2000-2060.

    python -m pipeline.climate.build              # fetch what is missing, correct, write the tables + report
    python -m pipeline.climate.build --estimate   # requests still to send and rough EECU-hours; asks nothing
    python -m pipeline.climate.build --offline    # use only what is cached; stop if something is missing

Steps
1. Observed daily weather from NASA POWER at the county centre (`fetch_power`, cached).
2. Which climate models have every band and scenario on Earth Engine (`fetch_gddp.load_availability`,
   asked once); a model that does not is replaced as the design note says (`choose_models`).
3. Daily county means of every model x scenario x year from `NASA/GDDP-CMIP6` (`fetch_gddp`, one cached
   JSON per request). The first PILOT_REQUESTS requests are sent one at a time and measured; the run
   only goes on if what is already spent plus the estimate for the rest stays under --max-eecu-hours,
   and it stops cleanly if the measured spend ever reaches that limit.
4. Per (model, scenario): one daily series (historical run to 2014, then the scenario), put on the
   real calendar, bias-corrected by calendar month against the observed 2000-2024 (`bias`), reference
   ET (`et0`), yearly and monthly numbers (`indicators`).
5. Write data/climate_futures.parquet, climate_history.parquet, climate_monthly.parquet (validated by
   `schema`) and data/climate_report.md.

Decisions made here (the design note left them open):
- Wind at 2 m for reference ET is the 10 m wind through the FAO-56 log profile (x 0.748) for the
  observed series and the models alike, so both get the same treatment; POWER's own 2 m wind is 8%
  lower and would give about 3% less ET (reported as a side number).
- The Tmax spread is only scaled for a model whose corrected baseline misses the observed number of
  days above 35 C by more than 25% (mean of its two scenarios).
- The published check for reference ET is the open-pan evaporation of the K-State station at Garden
  City (GARDEN_CITY below): reference ET should be about 0.6-0.8 of it.

Screen output is a few lines; the full log goes to data/logs/phase3/climate_build_<time>.log.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.climate import bias, et0, fetch_gddp, fetch_power, indicators, schema

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs" / "phase3"
REPORT_NAME = "climate_report.md"

BASELINE = bias.BASELINE
FIRST_YEAR = schema.FIRST_YEAR
LAST_YEAR = schema.LAST_YEAR
REFERENCE_WINDOW = (2020, 2029)  # "today": horizon 2025 of the same corrected models
DECADES = {"2030s": (2030, 2039), "2040s": (2040, 2049), "2050s": (2050, 2059)}

# Published station numbers used as an outside check (1981-2010 averages).
GARDEN_CITY = {
    "source": "A. Zukoff, Weather Information for Garden City, 2023, Kansas Agricultural Experiment Station "
              "Research Reports (2024 Western Kansas Agricultural Research), doi 10.4148/2378-5977.8596",
    "period": "1981-2010",
    "pan_evaporation_apr_oct_mm": 70.26 * 25.4,
    "annual_rain_mm": 19.24 * 25.4,
    "annual_tmean_c": (53.7 - 32.0) / 1.8,
    "rain_2023_mm": 23.67 * 25.4,
    "tmean_2023_c": (55.4 - 32.0) / 1.8,
}
PAN_COEFFICIENT_RANGE = (0.6, 0.8)  # usual range of reference ET / class A pan evaporation (FAO-56 chapter 3)

# stat -> (column, "abs" or "pct", unit, decimals) for the changes table
CHANGE_STATS = {
    "tmean_c": ("tmean_c", "abs", "C", 1),
    "summer_rain_pct": ("summer_rain_mm", "pct", "%", 0),
    "gs_rain_pct": ("rain_mm", "pct", "%", 0),
    "hot_days": ("hot_days", "abs", "days", 0),
    "et0_pct": ("et0_mm", "pct", "%", 1),
    "wind_pct": ("wind_ms", "pct", "%", 1),
    "erosion_climate_pct": ("erosion_climate", "pct", "%", 0),
}


class BuildError(RuntimeError):
    pass


class RunLog:
    """Every message goes to the log file; `say` also prints it."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def log(self, message: str) -> None:
        if self.path is not None:
            with self.path.open("a") as handle:
                handle.write(f"{datetime.now():%H:%M:%S} {message}\n")

    def say(self, message: str) -> None:
        print(message, flush=True)
        self.log(message)


# --------------------------------------------------------------------------- the science, no network


def add_et0(daily: pd.DataFrame, site: dict) -> pd.DataFrame:
    out = daily.copy()
    out["et0_mm"] = et0.et0_from_daily(out, site["latitude"], site["elevation_m"])
    return out


def correct_series(raw: pd.DataFrame, observed: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Delta-method correction of one raw model series against `observed`; (corrected, details)."""
    factors, clipped = bias.correction_factors(raw, observed, BASELINE)
    corrected, notes = bias.apply_correction(raw, factors)
    return corrected, {"factors": factors, "clipped": clipped, **notes}


def build_tables(observed: pd.DataFrame, site: dict, series: dict, log=lambda m: None) -> dict:
    """Everything after the downloads.

    observed: gap-free daily frame from 1 Oct 1999 to 31 Dec 2024 (bias.DAILY_COLUMNS);
    site: {"latitude", "elevation_m"}; series: {(model, scenario): raw daily model frame on the real
    calendar from 1 Oct 1999 to 31 Dec 2060}. Returns {"futures", "history", "monthly"} (the three
    tables, valid by pipeline/climate/schema.py) and {"checks"}: what the report prints.
    """
    if not series:
        raise BuildError("no model series to build from")
    obs = add_et0(observed[bias.DAILY_COLUMNS], site)
    history = indicators.yearly(obs, FIRST_YEAR, BASELINE[1])
    k = indicators.erosion_scale(history["erosion_climate"].to_numpy())
    capped_observed = int((history["erosion_climate"].to_numpy() * k > indicators.EROSION_CAP).sum())
    history["erosion_climate"] = indicators.scale_erosion(history["erosion_climate"], k)
    obs_checks = bias.baseline_checks(obs, BASELINE)
    obs_monthly_means = bias.monthly_means(obs, BASELINE)

    corrected, details = {}, {}
    for key, raw in series.items():
        corrected[key], details[key] = correct_series(raw, obs)
        details[key]["checks_mean_shift"] = bias.baseline_checks(corrected[key], BASELINE)

    models = list(dict.fromkeys(model for model, _ in series))
    scaled_models = []
    for model in models:
        keys = [key for key in series if key[0] == model]
        hot = float(np.mean([details[key]["checks_mean_shift"]["hot_days"] for key in keys]))
        scale = bias.needs_spread_scaling(hot, obs_checks["hot_days"])
        if scale:
            scaled_models.append(model)
        for key in keys:
            details[key]["spread_scaled"] = scale
            if scale:
                factors = bias.spread_factors(corrected[key]["tmax_c"], obs["tmax_c"], BASELINE)
                corrected[key], crossed = bias.scale_tmax_spread(corrected[key], factors)
                details[key]["spread_factors"] = factors
                details[key]["tmin_above_tmax_days"] += crossed
            details[key]["checks"] = bias.baseline_checks(corrected[key], BASELINE)
        log(f"{model}: corrected baseline days above 35 C {hot:.1f} against {obs_checks['hot_days']:.1f} observed; "
            f"Tmax spread {'scaled' if scale else 'left alone'}")

    futures_parts, monthly_parts = [], []
    for (model, scenario), daily in corrected.items():
        daily = add_et0(daily, site)
        yearly = indicators.yearly(daily, FIRST_YEAR, LAST_YEAR)
        raw_c = yearly["erosion_climate"].to_numpy() * k
        yearly["erosion_climate"] = indicators.scale_erosion(yearly["erosion_climate"], k)
        yearly.insert(0, "scenario", scenario)
        yearly.insert(0, "model", model)
        futures_parts.append(yearly)
        monthly = indicators.monthly(daily)
        monthly.insert(0, "scenario", scenario)
        monthly.insert(0, "model", model)
        monthly_parts.append(monthly)
        base = yearly[yearly["year"] <= BASELINE[1]]
        d = details[(model, scenario)]
        d["erosion_capped_years"] = int((raw_c > indicators.EROSION_CAP).sum())
        d["baseline_et0_mm"] = float(base["annual_et0_mm"].mean())
        d["baseline_erosion"] = float(base["erosion_climate"].mean())
        d["monthly_mean_gap"] = (bias.monthly_means(daily, BASELINE) - obs_monthly_means).abs().max().to_dict()
    obs_monthly = indicators.monthly(obs)
    obs_monthly.insert(0, "scenario", schema.OBSERVED)
    obs_monthly.insert(0, "model", schema.OBSERVED)

    futures = pd.concat(futures_parts, ignore_index=True)[schema.FUTURES_COLUMNS]
    history = history[schema.HISTORY_COLUMNS]
    monthly = pd.concat([obs_monthly, *monthly_parts], ignore_index=True)[schema.MONTHLY_COLUMNS]
    for frame in (futures, history, monthly):
        frame["year"] = frame["year"].astype("int64")
    monthly["month"] = monthly["month"].astype("int64")
    schema.validate("futures", futures)
    schema.validate("history", history)
    schema.validate("monthly", monthly)

    in_base = (obs.index.year >= BASELINE[0]) & (obs.index.year <= BASELINE[1])
    n_years = BASELINE[1] - BASELINE[0] + 1
    ws2 = et0.et0_from_daily(observed, site["latitude"], site["elevation_m"], "wind2_ms") if "wind2_ms" in observed else None
    checks = {
        "observed": obs_checks,
        "observed_means": {c: float(history[c].mean()) for c in schema.YEARLY_COLUMNS},
        "observed_apr_oct_et0_mm": float(obs.loc[in_base & obs.index.month.isin(range(4, 11)), "et0_mm"].sum() / n_years),
        "observed_annual_et0_ws2m_mm": float(ws2[in_base].sum() / n_years) if ws2 is not None else None,
        "observed_2023": {
            "rain_mm": float(obs.loc[obs.index.year == 2023, "rain_mm"].sum()),
            "tmean_c": float(((obs["tmax_c"] + obs["tmin_c"]) / 2.0)[obs.index.year == 2023].mean()),
        },
        "erosion_scale": k, "erosion_capped_observed_years": capped_observed,
        "models": models, "spread_scaled_models": scaled_models, "series": details,
    }
    return {"futures": futures, "history": history, "monthly": monthly, "checks": checks}


def decade_changes(futures: pd.DataFrame) -> pd.DataFrame:
    """Change of each CHANGE_STATS number per scenario and decade against REFERENCE_WINDOW of the same model.

    Columns: scenario, window, stat, unit, mean, low, high (mean and range across the models).
    """
    rows = []
    for scenario, part in futures.groupby("scenario"):
        ref = part[part["year"].between(*REFERENCE_WINDOW)].groupby("model").mean(numeric_only=True)
        for window, (first, last) in DECADES.items():
            then = part[part["year"].between(first, last)].groupby("model").mean(numeric_only=True)
            for stat, (column, kind, unit, _) in CHANGE_STATS.items():
                change = then[column] - ref[column] if kind == "abs" else (then[column] / ref[column] - 1.0) * 100.0
                rows.append(
                    {"scenario": scenario, "window": window, "stat": stat, "unit": unit,
                     "mean": float(change.mean()), "low": float(change.min()), "high": float(change.max())}
                )
    return pd.DataFrame(rows)


def change_lines(changes: pd.DataFrame) -> list[str]:
    """One plain line per scenario x decade."""
    lines = []
    for (scenario, window), part in changes.groupby(["scenario", "window"], sort=True):
        by = part.set_index("stat")
        bits = []
        for stat, (_, _, unit, decimals) in CHANGE_STATS.items():
            r = by.loc[stat]
            gap = "" if unit == "%" else " "
            bits.append(f"{stat} {r['mean']:+.{decimals}f}{gap}{unit} ({r['low']:+.{decimals}f} to {r['high']:+.{decimals}f})")
        lines.append(f"{scenario} {window} vs {REFERENCE_WINDOW[0]}-{REFERENCE_WINDOW[1]}: " + "; ".join(bits))
    return lines


# --------------------------------------------------------------------------- report


def _fmt(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}"


def report_text(result: dict, context: dict) -> str:
    """data/climate_report.md: sources, models, method, checks, the changes table, limits."""
    checks, futures = result["checks"], result["futures"]
    obs, means = checks["observed"], checks["observed_means"]
    site = context["site"]
    gc = GARDEN_CITY
    pan_ratio = checks["observed_apr_oct_et0_mm"] / gc["pan_evaporation_apr_oct_mm"]
    lines = [
        "# Future weather for Finney County: build report",
        "",
        f"Written by `python -m pipeline.climate.build` on {context['built']}. Tables: `data/climate_futures.parquet`, "
        "`data/climate_history.parquet`, `data/climate_monthly.parquet` (contract in `pipeline/climate/schema.py`).",
        "",
        "## Sources",
        "",
        f"- Observed: NASA POWER daily point data at the county centre ({site['latitude']:.3f}, {site['longitude']:.3f}; "
        f"{site['elevation_m']:.0f} m), 1 Oct 1999 to 31 Dec 2024: T2M_MAX, T2M_MIN, PRECTOTCORR, WS10M, RH2M, ALLSKY_SFC_SW_DWN "
        f"(WS2M only for a side check). {sum(site.get('missing', {}).values())} missing values. One point, not an average: "
        "POWER's weather grid cell (0.5 x 0.625 degrees) is about as large as the county box.",
        f"- Future: Earth Engine `NASA/GDDP-CMIP6`, daily, mean over the county box {list(context['bounds'])} at `scale=27830`; "
        "the historical run to 2014, then `ssp245` and `ssp585` from 2015; bands tasmax, tasmin, pr, sfcWind, hurs, rsds "
        "(huss, rlds and tas are downloaded and cached but not used).",
        "",
        "## Models",
        "",
        "Six models, chosen in the design note before any result was seen: " + ", ".join(checks["models"]) + ".",
    ]
    for swap in context.get("swaps", []):
        lines.append(
            f"- **{swap['dropped']} was replaced by {swap['replacement']}**: on Earth Engine {swap['dropped']} {swap['reason']} "
            "(the design note asks for all nine bands)."
        )
    if not context.get("swaps"):
        lines.append("- No substitution was needed.")
    repeated = context.get("calendar", {})
    if repeated:
        lines += [
            "",
            "Calendars: Earth Engine returns a value for every real calendar day for every model. Models with a shorter "
            "calendar are padded by repeating a day; counted here as days identical to the day before, per year:",
        ]
        for model, text in repeated.items():
            lines.append(f"- {model}: {text}")
        lines.append("Nothing had to be filled or dropped. A repeated day is a normal day of that model's weather, so totals and counts stay fair.")
    lines += [
        "",
        "## Method",
        "",
        "- Units: K to C; pr kg/m2/s x 86,400 = mm/day; rsds W/m2 x 0.0864 = MJ/m2/day; sfcWind is 10 m wind in m/s.",
        f"- Bias correction (delta method) per model and scenario, by calendar month, baseline {BASELINE[0]}-{BASELINE[1]} against "
        "NASA POWER: Tmax, Tmin and relative humidity are shifted by (observed mean - model mean); rain, wind and solar "
        f"radiation are multiplied by (observed mean / model mean), with the ratio kept inside {bias.RATIO_LIMITS['rain_mm']} for rain and "
        f"{bias.RATIO_LIMITS['wind_ms']} for wind and solar. Humidity is kept inside 1-100 %. The corrected baseline has the observed "
        "monthly means by construction; every change after that is the model's own.",
        "- Reference ET: FAO-56 Penman-Monteith, daily, from Tmax, Tmin, mean relative humidity (FAO-56 eq. 19), solar "
        "radiation, latitude and elevation; wind at 2 m = 10 m wind x 0.748 (FAO-56 log profile), the same for observed and models. "
        "The code reproduces the FAO-56 chapter 4 daily example (3.88 mm/day).",
        "- Seasons: growing season 1 Apr - 30 Sep; summer Jun - Aug; wheat season of year Y = 1 Oct (Y-1) - 30 Jun (Y).",
        "- `erosion_climate`: wind-erosion climatic factor C = 386 x u^3 / PE^2 (u = annual mean 10 m wind in m/s, PE = Thornthwaite "
        "precipitation-effectiveness index from monthly rain and temperature), computed per year, scaled so the observed "
        f"{BASELINE[0]}-{BASELINE[1]} mean is 100 and capped at {indicators.EROSION_CAP:.0f}. The design note wrote 'u in mph' next to 386; 386 "
        "belongs to m/s (34.483 goes with mph). After the rescaling it makes no difference.",
        "",
        "## Checks",
        "",
        f"### Observed {BASELINE[0]}-{BASELINE[1]} (NASA POWER)",
        "",
        f"- Growing season: mean {_fmt(means['tmean_c'])} C, rain {_fmt(means['rain_mm'], 0)} mm (summer {_fmt(means['summer_rain_mm'], 0)} mm), "
        f"{_fmt(means['gdd'], 0)} growing degree days, {_fmt(means['hot_days'])} days above 35 C, longest dry spell {_fmt(means['dry_spell_days'])} days, "
        f"reference ET {_fmt(means['et0_mm'], 0)} mm, wind {_fmt(means['wind_ms'], 2)} m/s.",
        f"- Wheat season: rain {_fmt(means['wheat_rain_mm'], 0)} mm, reference ET {_fmt(means['wheat_et0_mm'], 0)} mm, "
        f"{_fmt(means['wheat_hot_days'])} days above 32 C in May-June; February-May wind {_fmt(means['spring_wind_ms'], 2)} m/s.",
        f"- Year: rain {_fmt(means['annual_rain_mm'], 0)} mm, reference ET {_fmt(means['annual_et0_mm'], 0)} mm, mean {_fmt(means['annual_tmean_c'])} C.",
        "",
        "### Against the Garden City station",
        "",
        f"Published numbers ({gc['source']}; {gc['period']} averages):",
        f"- Reference ET: the station's open-pan evaporation, April-October, averages {gc['pan_evaporation_apr_oct_mm']:.0f} mm (70.26 in). "
        f"Our observed April-October reference ET is {checks['observed_apr_oct_et0_mm']:.0f} mm, {pan_ratio:.2f} of the pan. Reference ET is "
        f"usually {PAN_COEFFICIENT_RANGE[0]}-{PAN_COEFFICIENT_RANGE[1]} of pan evaporation, so the number is "
        + ("inside the expected range." if PAN_COEFFICIENT_RANGE[0] <= pan_ratio <= PAN_COEFFICIENT_RANGE[1] else "OUTSIDE the expected range.")
        + " This is an indirect check: we found no published long-term grass reference ET for Garden City.",
    ]
    if checks["observed_annual_et0_ws2m_mm"] is not None:
        lines.append(
            f"- With POWER's own 2 m wind instead of the log profile the observed annual reference ET would be "
            f"{checks['observed_annual_et0_ws2m_mm']:.0f} mm instead of {means['annual_et0_mm']:.0f} mm."
        )
    lines += [
        f"- Rain: station {gc['annual_rain_mm']:.0f} mm a year ({gc['period']}); POWER {means['annual_rain_mm']:.0f} mm ({BASELINE[0]}-{BASELINE[1]}). "
        f"In 2023 the station had {gc['rain_2023_mm']:.0f} mm and POWER {checks['observed_2023']['rain_mm']:.0f} mm.",
        f"- Temperature: station annual mean {gc['annual_tmean_c']:.1f} C ({gc['period']}); POWER {means['annual_tmean_c']:.1f} C ({BASELINE[0]}-{BASELINE[1]}). "
        f"In 2023 the station had {gc['tmean_2023_c']:.1f} C and POWER {checks['observed_2023']['tmean_c']:.1f} C: POWER runs about "
        f"{checks['observed_2023']['tmean_c'] - gc['tmean_2023_c']:.1f} C warm here, mostly at night (see Limits).",
        "",
        "### Corrected model baseline against observed",
        "",
        f"What a shift of the mean does not guarantee, per year over {BASELINE[0]}-{BASELINE[1]} "
        "(growing-season days above 35 C; longest growing-season dry spell; rain days of 1 mm or more in the growing season and in the year):",
        "",
        "| series | days above 35 C | vs observed | dry spell | GS rain days | rain days | Tmax spread scaled | annual ET0 mm | erosion |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| observed | {obs['hot_days']:.1f} | | {obs['dry_spell_days']:.1f} | {obs['gs_rain_days']:.1f} | {obs['rain_days']:.1f} | | "
        f"{means['annual_et0_mm']:.0f} | {means['erosion_climate']:.0f} |",
    ]
    for (model, scenario), d in checks["series"].items():
        c, before = d["checks"], d["checks_mean_shift"]
        scaled = f"yes (before: {before['hot_days']:.1f})" if d["spread_scaled"] else "no"
        lines.append(
            f"| {model} {scenario} | {c['hot_days']:.1f} | {(c['hot_days'] / obs['hot_days'] - 1) * 100:+.0f}% | {c['dry_spell_days']:.1f} | "
            f"{c['gs_rain_days']:.1f} | {c['rain_days']:.1f} | {scaled} | {d['baseline_et0_mm']:.0f} | {d['baseline_erosion']:.0f} |"
        )
    worst_gap = max(max(d["monthly_mean_gap"].values()) for d in checks["series"].values())
    clipped = {f"{m} {s}": d["clipped"] for (m, s), d in checks["series"].items() if d["clipped"]}
    crossed = sum(d["tmin_above_tmax_days"] for d in checks["series"].values())
    rh = sum(d["rh_clipped_days"] for d in checks["series"].values())
    capped = sum(d["erosion_capped_years"] for d in checks["series"].values())
    lines += [
        "",
        f"- Tmax spread: " + (
            "scaled for " + ", ".join(checks["spread_scaled_models"]) + " (days above 35 C were off by more than 25%)."
            if checks["spread_scaled_models"] else "not scaled for any model (days above 35 C are within 25% of observed for all six)."
        ),
        f"- Corrected baseline monthly means against observed: largest gap in any month, variable and series {worst_gap:.3f} "
        "(units of the variable; not exactly 0 only because of the two repairs below).",
        f"- Repairs after the shift: {crossed} model days in all series had Tmin above Tmax (both set to their mean); "
        f"{rh} days had humidity outside 1-100 %.",
        "- Monthly ratios that hit their limit: " + ("; ".join(f"{k}: {v}" for k, v in clipped.items()) if clipped else "none") + ".",
        f"- `erosion_climate`: {checks['erosion_capped_observed_years']} observed year(s) and {capped} of {len(futures)} model years are capped at "
        f"{indicators.EROSION_CAP:.0f}.",
        "- Reference ET is not linear in its inputs, so the corrected baseline is close to, not equal to, the observed total (table above). "
        "The models have fewer rain days and longer dry spells than POWER; rain totals match by construction.",
        "",
        "## Changes",
        "",
        f"Mean over the six models (lowest to highest model), against {REFERENCE_WINDOW[0]}-{REFERENCE_WINDOW[1]} of the same models. "
        "tmean, rain, hot days, ET0 and wind are growing-season numbers.",
        "",
        "| scenario | window | tmean C | summer rain % | GS rain % | days above 35 C | ET0 % | wind % | erosion_climate % |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    changes = context["changes"]
    for (scenario, window), part in changes.groupby(["scenario", "window"], sort=True):
        by = part.set_index("stat")
        cells = []
        for stat, (_, _, _, decimals) in CHANGE_STATS.items():
            r = by.loc[stat]
            cells.append(f"{r['mean']:+.{decimals}f} ({r['low']:+.{decimals}f} to {r['high']:+.{decimals}f})")
        lines.append(f"| {scenario} | {window} | " + " | ".join(cells) + " |")
    ref = futures[futures["year"].between(*REFERENCE_WINDOW)].groupby("scenario")[["tmean_c", "summer_rain_mm", "rain_mm", "hot_days", "et0_mm", "wind_ms", "erosion_climate"]].mean()
    lines += ["", f"Levels in {REFERENCE_WINDOW[0]}-{REFERENCE_WINDOW[1]} (model mean):"]
    for scenario, r in ref.iterrows():
        lines.append(
            f"- {scenario}: {r['tmean_c']:.1f} C, summer rain {r['summer_rain_mm']:.0f} mm, growing-season rain {r['rain_mm']:.0f} mm, "
            f"{r['hot_days']:.0f} days above 35 C, ET0 {r['et0_mm']:.0f} mm, wind {r['wind_ms']:.2f} m/s, erosion_climate {r['erosion_climate']:.0f}."
        )
    cost = context.get("cost", {})
    lines += [
        "",
        "## Earth Engine cost",
        "",
        f"- {cost.get('files', 0)} cached answers, {cost.get('spent_eecu_h', 0.0):.2f} EECU-hours in total for this piece "
        f"({cost.get('files', 0) - cost.get('unmeasured', 0)} requests measured by their Earth Engine profile, {cost.get('unmeasured', 0)} whose profile did not come back counted at the mean) "
        f"(about {cost.get('eecu_s_per_request', 0.0):.1f} EECU-seconds a request; budget {cost.get('max_eecu_h', fetch_gddp.DEFAULT_MAX_EECU_HOURS):g}). "
        f"The requests took {cost.get('request_seconds', 0.0) / 60:.0f} minutes added up, less on the clock because several run at once.",
        f"- This run sent {cost.get('sent', 0)} request(s) ({cost.get('run_eecu_h', 0.0):.2f} EECU-hours, {cost.get('wall_s', 0.0) / 60:.1f} minutes).",
        "",
        "## Limits",
        "",
        "- The observed series is NASA POWER (the MERRA-2 reanalysis and satellite radiation), not a weather station. Against the Garden City "
        "station it is warmer (mostly warmer nights) and somewhat wetter, and it rains on more days. Every model is corrected to it, so "
        "levels (degree days, ET, days above 35 C) carry that offset; changes between decades do not.",
        "- The delta method fixes monthly means only. Day-to-day behaviour (rain days, dry spells, heat waves) is the model's own; the table above shows how far off it is.",
        "- One number for the whole county (a 0.25 degree model grid averaged over the county box): no difference between fields.",
        "- Six models, one run each, two scenarios. The range across models is a spread, not a probability. Single years are weather, not forecasts: use 10-year windows.",
        "- `erosion_climate` uses a formula made for long-term averages on single years, so it swings widely (observed years range from "
        "about 40 to the cap of 300). Use window means. Model wind is 10 m daily mean wind; gusts, which move soil, are not in it.",
        "- 2015-2024 is scenario output, not observed forcing, and it is part of the baseline the correction is fitted on, so the correction differs slightly between the two scenarios of a model.",
        "- Relative humidity is the daily mean (FAO-56 eq. 19), the weakest of the FAO-56 humidity options; it is what both sources provide.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- downloads


def calendar_summary(infos: dict) -> dict:
    """{model: plain text} from {(model, scenario): gddp_daily info}."""
    out = {}
    for (model, _), info in infos.items():
        counts = [n for year, n in info["repeated_by_year"].items() if year >= FIRST_YEAR]
        if model in out:
            continue
        if max(counts) == 0:
            out[model] = "no repeated days (a real calendar with leap years)"
        elif min(counts) >= 5:
            out[model] = f"{min(counts)}-{max(counts)} repeated days every year (a 360-day model calendar)"
        else:
            years = sum(1 for n in counts if n)
            out[model] = f"{max(counts)} repeated day in {years} of {len(counts)} years (a 365-day model calendar: leap years)"
        if info["filled"] or info["respread_years"]:
            out[model] += f"; {info['filled']} day(s) filled from the day before, {info['respread_years']} year(s) re-spread"
    return out


def assemble_series(models, results: dict) -> tuple[dict, dict]:
    """({(model, scenario): raw daily frame}, {(model, scenario): calendar info}) from {Request: rows}."""
    series, infos = {}, {}
    first, last = pd.Timestamp(fetch_gddp.LEAD_IN_START), pd.Timestamp(f"{LAST_YEAR}-12-31")
    for model in models:
        for scenario in fetch_gddp.SCENARIOS:
            rows = []
            for request in fetch_gddp.requests_for([model]):
                if request.scenario in (fetch_gddp.HISTORICAL, scenario):
                    if request not in results:
                        raise BuildError(f"{model} {scenario}: no data for {request.label}")
                    rows += results[request]
            daily, info = bias.gddp_daily(rows)
            if info["filled"] > 0.02 * info["days"]:  # a calendar pads a few days a year, never this many
                raise BuildError(f"{model} {scenario}: {info['filled']} of {info['days']} days had no values")
            if daily.index[0] != first or daily.index[-1] != last or len(daily) != (last - first).days + 1:
                raise BuildError(f"{model} {scenario}: daily series covers {daily.index[0].date()} to {daily.index[-1].date()}, not {first.date()} to {last.date()}")
            series[(model, scenario)], infos[(model, scenario)] = daily, info
    return series, infos


def fetch_models(args, bounds, log: RunLog) -> tuple[list[str], list[dict], dict, dict]:
    """(models, swaps, {Request: rows}, cost). Sends the pilot, checks the estimate, then the rest."""
    cache_dir = fetch_gddp.CACHE_DIR
    if args.offline:
        import json

        try:
            availability = json.loads(fetch_gddp.availability_path(cache_dir).read_text())["models"]
        except (OSError, ValueError, KeyError) as exc:
            raise BuildError("--offline: the model availability check is not cached; run once without --offline") from exc
    else:
        availability = fetch_gddp.load_availability(cache_dir, log=log.log)
    models, swaps = fetch_gddp.choose_models(availability)
    for swap in swaps:
        log.say(f"model {swap['dropped']} replaced by {swap['replacement']} ({swap['reason']})")
    requests = fetch_gddp.requests_for(models)
    max_s = args.max_eecu_hours * 3600.0
    spent_before, _, _ = fetch_gddp.budget_spent_eecu_s(cache_dir)
    est = fetch_gddp.estimate(requests, bounds, cache_dir)
    for line in fetch_gddp.estimate_lines(est):
        log.say(line)
    costs: list[dict] = []
    started = time.monotonic()
    from pipeline.satellite import Stats

    stats = Stats()
    if est["pending"] and args.offline:
        raise BuildError(f"--offline: {est['pending']} GDDP request(s) are not cached; run without --offline")
    engine = None
    if est["pending"]:
        if spent_before >= max_s:
            raise fetch_gddp.BudgetExceeded(f"budget already used: {spent_before / 3600:.2f} of {args.max_eecu_hours:.2f} EECU-hours")
        engine = fetch_gddp.GddpEngine()
        pilot = fetch_gddp.pilot_requests(requests, bounds, cache_dir)
        fetch_gddp.collect(pilot, bounds, cache_dir, compute=engine.daily, workers=1, stats=stats, log=log.log, costs=costs,
                           max_eecu_s=max_s, already_spent_eecu_s=spent_before)
        measured = [c["eecu_s"] for c in costs if c["eecu_s"] is not None]
        per_request = float(np.mean(measured)) if measured else fetch_gddp.EECU_S_PER_REQUEST
        est = fetch_gddp.estimate(requests, bounds, cache_dir, per_request)
        total_h = est["spent_eecu_h"] + est["pending_eecu_h"]
        log.say(
            f"pilot: {len(pilot)} request(s) cost {per_request:.1f} EECU-seconds each; {est['pending']} left -> about "
            f"{est['pending_eecu_h']:.2f} EECU-hours more, {total_h:.2f} for the whole piece (limit {args.max_eecu_hours:.2f})"
        )
        if total_h > args.max_eecu_hours:
            raise fetch_gddp.BudgetExceeded(
                f"the whole job would cost about {total_h:.2f} EECU-hours, over the limit of {args.max_eecu_hours:.2f}; nothing more was sent"
            )
    results = fetch_gddp.collect(
        requests, bounds, cache_dir, compute=engine.daily if engine else None, workers=args.workers, stats=stats,
        log=log.log, costs=costs, max_eecu_s=max_s, already_spent_eecu_s=spent_before,
    )
    spent, files, unmeasured = fetch_gddp.budget_spent_eecu_s(cache_dir)
    cost = {
        "unmeasured": unmeasured,
        "sent": stats.requests, "retries": stats.retries, "run_eecu_h": fetch_gddp.run_spent_eecu_s(costs) / 3600 if costs else 0.0,
        "wall_s": time.monotonic() - started if costs else 0.0, "spent_eecu_h": spent / 3600, "files": files,
        "eecu_s_per_request": spent / files if files else 0.0, "max_eecu_h": args.max_eecu_hours,
        "request_seconds": fetch_gddp.cached_request_seconds(cache_dir),
    }
    return models, swaps, results, cost


# --------------------------------------------------------------------------- command


def run_estimate(bounds) -> int:
    """No network: what is cached, what is still to ask, what it should cost."""
    import json

    lat, lon = fetch_power.site_for(bounds)
    power = fetch_power.cache_path(fetch_power.CACHE_DIR, lat, lon)
    print(f"NASA POWER: {'cached' if power.exists() else 'not cached: 1 download of about 1 MB, no Earth Engine cost'}")
    try:
        availability = json.loads(fetch_gddp.availability_path(fetch_gddp.CACHE_DIR).read_text())["models"]
        models, swaps = fetch_gddp.choose_models(availability)
        print("models: " + ", ".join(models) + "".join(f"; {s['replacement']} replaces {s['dropped']}" for s in swaps))
    except (OSError, ValueError, KeyError, fetch_gddp.GddpError):
        models = list(fetch_gddp.PREFERRED_MODELS)
        print("model availability not checked yet (one tiny Earth Engine request); assuming the six preferred models")
    for line in fetch_gddp.estimate_lines(fetch_gddp.estimate(fetch_gddp.requests_for(models), bounds, fetch_gddp.CACHE_DIR)):
        print(line)
    return 0


def write_outputs(result: dict, out_dir: Path, context: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result["futures"].to_parquet(out_dir / schema.FUTURES_PATH.name, index=False)
    result["history"].to_parquet(out_dir / schema.HISTORY_PATH.name, index=False)
    result["monthly"].to_parquet(out_dir / schema.MONTHLY_PATH.name, index=False)
    (out_dir / REPORT_NAME).write_text(report_text(result, context))


def build(args, log: RunLog) -> int:
    bounds = fetch_gddp.county_bounds()
    if args.estimate:
        return run_estimate(bounds)
    lat, lon = fetch_power.site_for(bounds)
    if args.offline and not fetch_power.cache_path(fetch_power.CACHE_DIR, lat, lon).exists():
        raise BuildError("--offline: the NASA POWER answer is not cached; run without --offline")
    observed, site = fetch_power.load_observed(bounds, log=log.log)
    log.say(f"observed: NASA POWER at {site['latitude']:.3f}, {site['longitude']:.3f} ({site['elevation_m']:.0f} m), "
            f"{len(observed):,} days, {sum(site['missing'].values())} missing")
    models, swaps, results, cost = fetch_models(args, bounds, log)
    series, infos = assemble_series(models, results)
    result = build_tables(observed, site, series, log=log.log)
    changes = decade_changes(result["futures"])
    context = {
        "built": f"{datetime.now():%Y-%m-%d %H:%M}", "site": site, "bounds": bounds, "swaps": swaps, "cost": cost,
        "calendar": calendar_summary(infos), "changes": changes,
    }
    write_outputs(result, args.out_dir, context)
    checks = result["checks"]
    log.say(
        f"wrote {len(result['futures']):,} model-year rows ({len(models)} models x 2 scenarios x {LAST_YEAR - FIRST_YEAR + 1} years), "
        f"{len(result['history'])} observed years, {len(result['monthly']):,} monthly rows to {args.out_dir}"
    )
    log.say(
        f"observed {BASELINE[0]}-{BASELINE[1]}: growing season {checks['observed_means']['tmean_c']:.1f} C, rain {checks['observed_means']['rain_mm']:.0f} mm, "
        f"ET0 {checks['observed_means']['et0_mm']:.0f} mm, {checks['observed_means']['hot_days']:.0f} days above 35 C; "
        f"Tmax spread scaled for: {', '.join(checks['spread_scaled_models']) or 'no model'}"
    )
    for line in change_lines(changes):
        log.log(line)
    late = changes[(changes["window"] == "2050s") & (changes["stat"].isin(["tmean_c", "hot_days"]))].set_index(["scenario", "stat"])
    for scenario in fetch_gddp.SCENARIOS:
        log.say(f"{scenario} 2050s vs {REFERENCE_WINDOW[0]}-{REFERENCE_WINDOW[1]}: {late.loc[(scenario, 'tmean_c'), 'mean']:+.1f} C, "
                f"{late.loc[(scenario, 'hot_days'), 'mean']:+.0f} days above 35 C (model mean)")
    log.say(f"Earth Engine: {cost['sent']} request(s) sent this run ({cost['run_eecu_h']:.2f} EECU-hours); "
            f"{cost['spent_eecu_h']:.2f} EECU-hours for the piece so far. Report: {args.out_dir / REPORT_NAME}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--estimate", action="store_true", help="requests still to send and rough EECU-hours; asks nothing")
    parser.add_argument("--offline", action="store_true", help="use only cached downloads; stop if something is missing")
    parser.add_argument("--max-eecu-hours", type=float, default=fetch_gddp.DEFAULT_MAX_EECU_HOURS,
                        help=f"Earth Engine budget for this piece, all runs together (default {fetch_gddp.DEFAULT_MAX_EECU_HOURS:g})")
    parser.add_argument("--workers", type=int, default=fetch_gddp.WORKERS, help=f"concurrent requests (default {fetch_gddp.WORKERS})")
    parser.add_argument("--out-dir", type=Path, default=DATA_DIR, help="where the tables and the report go (default: data/)")
    args = parser.parse_args(argv)
    if args.max_eecu_hours <= 0 or args.workers < 1:
        parser.error("--max-eecu-hours must be above 0 and --workers at least 1")
    log_path = None if args.estimate else LOG_DIR / f"climate_build_{datetime.now():%Y%m%d_%H%M%S}.log"
    log = RunLog(log_path)
    try:
        return build(args, log)
    except fetch_gddp.BudgetExceeded as exc:
        print(f"STOPPED: {exc}\n  full log: {log_path}", file=sys.stderr)
        return 3
    except (BuildError, fetch_gddp.GddpError, fetch_power.PowerError, schema.ClimateSchemaError) as exc:
        print(f"ERROR: {exc}\n  full log: {log_path}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted: finished requests are cached, run the same command again to resume.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - Earth Engine login and similar: one clean line, details in the log
        from pipeline.satellite import EarthEngineUnavailable

        if isinstance(exc, EarthEngineUnavailable):
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
