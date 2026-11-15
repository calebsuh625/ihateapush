"""Build the yearly soil table data/soil_health.parquet from the bare-soil views (schema.md).

    python -m pipeline.soil.build                                   # whole county
    python -m pipeline.soil.build --obs data/soil_bare_obs_proto.parquet --out data/soil_health_proto.parquet \
        --meta data/soil_health_proto_meta.json --report data/soil_health_proto_report.md

No network: it reads data/soil_bare_obs.parquet (pipeline/soil/bare_soil.py), data/fields.parquet
and the SSURGO cache in data/raw/ssurgo/. It prints about 15 lines; everything else goes to the
report (--report) and to data/logs/soil_build_<time>.log. The whole county takes about a minute.

What it does
------------
1. features.prepare: usable composites and their features, per sensor family (Landsat, Sentinel-2).
2. model.train_family: one gradient-boosting model per family, validated four ways. The value
   that goes into the table is OUT-OF-FOLD under the map-unit grouping: each field is predicted by
   a model that never saw a field of its (dominant) soil map unit. Nothing else would do: a model
   that has seen a field can memorise its SSURGO value, which makes the yearly numbers look
   steadier and more accurate than they are and blind to change.
3. confidence.combine: the two families merged into om_pred, weighted by measured noise.
   n_obs = bare views per pixel behind om_pred (Landsat + Sentinel-2 composites that were used).
   confidence = skill x reliability (pipeline/soil/confidence.py has the formula). It says how
   repeatable a row is, not how accurate (confidence_check measures what it does and does not track).
4. exposure.combine_bare_days: one bare_days per field-year, and the checks it has to pass.
5. noise.analyse: year-to-year noise against the change we hope to see, crop artefacts, verdict.
   The verdict also needs the sensitivity's interval, the sensitivity within a soil type and
   whether bare_days passed its checks.
6. Writes the table (validated, atomic), the meta JSON (recommended, om_noise_sd, verdict, plus
   "notes" - one plain sentence per column for the app - and the "model", "noise", "confidence",
   "bare_days" and "sentinel2_adds" blocks with every number) and the markdown report
   (pipeline/soil/report.py), which shows nothing that is not in the meta file.

It works on a table with one sensor family only (e.g. after `bare_soil --sensors landsat`): the
checks that need two families are then reported as not run.

The table has every field present in --obs for every year of health_schema.YEARS. Fields of
--fields that are missing from --obs are reported and left out.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # allow `python pipeline/soil/build.py`
    sys.path.insert(0, str(ROOT))

from pipeline.common import DATA_DIR, EQUAL_AREA_CRS  # noqa: E402
from pipeline.soil import confidence as conf  # noqa: E402
from pipeline.soil import exposure, features, model, noise  # noqa: E402
from pipeline.soil.features import SENSOR_LABEL, SENSORS  # noqa: E402
from pipeline.soil.health_schema import (  # noqa: E402
    COLUMNS,
    SOIL_HEALTH_META_PATH,
    SOIL_HEALTH_PATH,
    YEARS,
    SoilHealthSchemaError,
    check_meta,
    validate_soil_health,
)
from pipeline.soil.report import render_report, summary_lines  # noqa: E402

OBS_PATH = DATA_DIR / "soil_bare_obs.parquet"
FIELDS_PATH = DATA_DIR / "fields.parquet"
REPORT_PATH = DATA_DIR / "soil_health_report.md"
SATELLITE_PATH = DATA_DIR / "field_satellite.parquet"
LOG_DIR = DATA_DIR / "logs"
METHOD_VERSION = "2"  # 2: series scheme, sensitivity interval and within-soil-type rule in the verdict, bare_days checks
OBS_METHOD_KEY = b"soil_bare_method"  # written by pipeline/soil/bare_soil.py
SAND_LOAM_SPLIT = 1.2  # Finney County labels fall in two heaps: sands at 0.5-1.1 %, loams at 1.4-2.1 %
CONFIDENCE_BINS = 4
MIN_COMPOSITES, MIN_FIELDS = 50, 10  # a sensor family with less than this gets no model
WHAT_CONFIDENCE_MEANS = (
    "confidence says how repeatable om_pred is in that row (how often and how fully the field was seen bare, and whether "
    "the two satellites agreed), scaled by the model's overall skill. It is not accuracy: it does not tell a good year "
    "from a bad year of the same field, and on sandy soils higher confidence goes with a larger error."
)
WHAT_BARE_DAYS_MEANS = (
    "bare_days measures how long the soil sat uncovered, which is exposure, not what the soil has become. One field-year "
    "only tells a fallow year from a cropped year; compare rotations on multi-year means over many fields, within the same years."
)
WHAT_OM_PRED_MEANS = (
    "om_pred is a rough picture of the kind of soil (sand against loam). Its year-to-year movement and its trend on a "
    "field are noise and crop residue, not soil change."
)
WHAT_ACCURACY_MEANS = (
    "SSURGO om_r is the soil survey's representative value for a mapped soil, not a measurement of the field. "
    "Accuracy here = how well bare-soil reflectance tells which mapped soil a field lies on, for a soil map unit "
    "the model has never seen. It says nothing about following organic matter that changes over time."
)


class BuildError(RuntimeError):
    pass


# --------------------------------------------------------------------------- small helpers


def _clean(obj):
    """JSON-safe copy: numpy scalars -> Python, NaN / inf -> None, keys -> str."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    return obj


def _atomic_write(path: Path, writer) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        writer(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def obs_method(path: Path) -> dict | None:
    """The method block bare_soil.py stored in the Parquet metadata (None if there is none)."""
    import pyarrow.parquet as pq

    raw = (pq.read_schema(path).metadata or {}).get(OBS_METHOD_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- assembling the inputs


def field_frame(fields, mapunits: pd.DataFrame | None, ssurgo_cache=None) -> pd.DataFrame:
    """One row per field: field_id, om (label), mukey, series, dominant_share, block, crop_history."""
    if mapunits is None:
        from pipeline.soil import ssurgo

        mapunits = ssurgo.field_mapunits(fields, cache_dir=ssurgo_cache, offline=True)
        mapunits = mapunits.assign(field_id=fields["field_id"].to_numpy(dtype=object)).set_index("field_id")
    centre = fields.geometry.to_crs(EQUAL_AREA_CRS).centroid
    out = pd.DataFrame(
        {
            "field_id": fields["field_id"].to_numpy(dtype=object),
            "om": fields["organic_matter_pct"].to_numpy(dtype=float),
            "crop_history": fields["crop_history"].to_numpy(dtype=object),
            # coordinates are used for this and nothing else: which fields share a validation fold
            "block": model.spatial_blocks(centre.x.to_numpy(), centre.y.to_numpy()),
        }
    )
    mukey = out["field_id"].map(mapunits["dominant_mukey"]).to_numpy(dtype=object)
    out["mukey"] = [m if isinstance(m, str) else None for m in mukey]
    # soil series of the dominant map unit (sister map units share it); None when not known
    series = out["field_id"].map(mapunits["dominant_series"]).to_numpy(dtype=object) if "dominant_series" in mapunits else [None] * len(out)
    out["series"] = [v if isinstance(v, str) and v else None for v in series]
    out["dominant_share"] = out["field_id"].map(mapunits["dominant_share"]).to_numpy(dtype=float)
    return out


def wide_table(prepared: pd.DataFrame, field_ids: list[str], results: dict) -> pd.DataFrame:
    """One row per (field, year in YEARS), sorted, with per sensor family s:

    p_s (the headline out-of-fold prediction), n_s (bare views; 0 unless the composite is usable),
    q_s (bare_px_frac), bd_s (bare_days), nc_s (n_clear), usable_s, snowy_s (had bare views but
    failed the screen) and one column `<scheme>_<model>_s` per validation scheme and model.
    """
    index = pd.MultiIndex.from_product([field_ids, list(YEARS)], names=["field_id", "year"])
    wide = pd.DataFrame(index=index).reset_index()
    for s in SENSORS:
        part = prepared[prepared["sensor"] == s]
        cols = part[["field_id", "year", "n_bare", "bare_px_frac", "bare_days", "n_clear", "usable"]].copy()
        cols["snowy"] = (part["n_bare"].to_numpy(dtype=float) > 0) & ~part["usable"].to_numpy(dtype=bool)
        names = {"n_bare": "n", "bare_px_frac": "q", "bare_days": "bd", "n_clear": "nc", "usable": "usable", "snowy": "snowy"}
        wide = wide.merge(cols.rename(columns={k: f"{v}_{s}" for k, v in names.items()}), on=["field_id", "year"], how="left")
        for flag in (f"usable_{s}", f"snowy_{s}"):
            wide[flag] = wide[flag].eq(True).to_numpy(dtype=bool)  # NaN (no row for that year) -> False
        wide[f"n_{s}"] = np.where(wide[f"usable_{s}"], wide[f"n_{s}"].astype(float), 0.0)
        result = results.get(s)
        if result is None:
            wide[f"p_{s}"] = np.nan
            continue
        preds = result.frame[["field_id", "year"]].copy()
        for scheme, by_model in result.preds.items():
            for name, values in by_model.items():
                preds[f"{scheme}_{name}_{s}"] = values
        preds[f"p_{s}"] = result.preds["map_unit"]["hgb"]  # out-of-fold by map unit, never anything else
        wide = wide.merge(preds, on=["field_id", "year"], how="left")
    return wide.sort_values(["field_id", "year"]).reset_index(drop=True)


# --------------------------------------------------------------------------- evaluation blocks


def scheme_block(wide: pd.DataFrame, info: pd.DataFrame, schemes, suffix: str) -> dict:
    """{scheme: {model: {row, field}}} for the prediction columns `<scheme>_<model><suffix>`."""
    ids = wide["field_id"].to_numpy(dtype=object)
    y = wide["field_id"].map(info["om"]).to_numpy(dtype=float)
    groups = wide["field_id"].map(info["mukey"]).to_numpy(dtype=object)
    out: dict = {}
    for scheme in schemes:
        out[scheme] = {}
        for name in model.MODELS:
            col = f"{scheme}_{name}{suffix}"
            if col in wide:
                out[scheme][name] = model.evaluate(ids, y, wide[col].to_numpy(dtype=float), groups if name == "hgb" else None)
    return out


def confidence_check(table: pd.DataFrame, y: np.ndarray, crops: pd.DataFrame) -> dict:
    """What does confidence track? Error against SSURGO and year-to-year jumps, cut several ways.

    Rows are cut into CONFIDENCE_BINS equal groups by confidence. A pair of consecutive years
    counts in the bin of the lower of its two confidences. Besides the plain rank correlation with
    the absolute error, four checks say how much of it is real:

      upper / lower heap   the same correlation on loams and on sands alone (SAND_LOAM_SPLIT).
                           Fields seen bare often are mostly loam fields near the county mean,
                           where any cautious model has a small error whatever its confidence.
      constant guess       the correlation of confidence with the error of predicting the county
                           mean for every row. Whatever shows up here is soil mix, not the model.
      within a field       deviations of confidence and of error from the field's own means: does
                           confidence tell a field's good years from its bad ones?
      n_obs                how much of confidence is simply how often the field was seen bare.
    """
    pred = table["om_pred"].to_numpy(dtype=float)
    has = np.isfinite(pred)
    c = table["confidence"].to_numpy(dtype=float)
    err = np.abs(pred - y)
    if has.sum() < 40:
        return {"n": int(has.sum())}
    ok = has & np.isfinite(err)
    county_mean = float(np.mean(y[ok])) if ok.any() else float("nan")
    err_const = np.abs(county_mean - y)
    edges = np.unique(np.quantile(c[has], np.linspace(0, 1, CONFIDENCE_BINS + 1)))
    now = table[["field_id", "year", "confidence"]]
    before = now.assign(year=now["year"] + 1).rename(columns={"confidence": "_c_prev"})
    pairs = noise.consecutive_pairs(table, "om_pred").merge(crops, on=["field_id", "year"], how="left")
    pairs = pairs.merge(now, on=["field_id", "year"]).merge(before, on=["field_id", "year"])
    pairs["_c"] = np.minimum(pairs["confidence"], pairs["_c_prev"])
    stable = pairs[pairs["crop"].notna() & (pairs["crop"] == pairs["prev_crop"])]
    rows = []
    for k, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        last = k == len(edges) - 2
        in_bin = has & (c >= lo) & ((c <= hi) if last else (c < hi))
        part = stable[(stable["_c"] >= lo) & ((stable["_c"] <= hi) if last else (stable["_c"] < hi))]
        upper = in_bin & (y >= SAND_LOAM_SPLIT)
        lower = in_bin & (y < SAND_LOAM_SPLIT)
        rows.append(
            {
                "from": float(lo),
                "to": float(hi),
                "n": int(in_bin.sum()),
                "mean_confidence": float(c[in_bin].mean()) if in_bin.any() else None,
                "median_abs_error": float(np.nanmedian(err[in_bin])) if in_bin.any() else None,
                "median_abs_error_constant_guess": float(np.nanmedian(err_const[in_bin])) if in_bin.any() else None,
                "rmse": model.metrics(y[in_bin], pred[in_bin])["rmse"],
                "share_upper_heap": float(upper.sum() / in_bin.sum()) if in_bin.any() else None,
                "median_abs_error_upper_heap": float(np.nanmedian(err[upper])) if upper.any() else None,
                "median_abs_error_lower_heap": float(np.nanmedian(err[lower])) if lower.any() else None,
                "n_stable_pairs": int(len(part)),
                "jump_robust_sd": noise.jump_summary(part["jump"])["robust_sd"],
            }
        )
    up = ok & (y >= SAND_LOAM_SPLIT)
    low = ok & (y < SAND_LOAM_SPLIT)
    jumps = np.abs(stable["jump"].to_numpy(dtype=float))
    # within a field: which of its years has the higher confidence, and is that year closer to SSURGO?
    inner = pd.DataFrame({"f": table["field_id"].to_numpy(dtype=object)[ok], "c": c[ok], "e": err[ok]})
    inner = inner[inner.groupby("f")["c"].transform("size") >= noise.MIN_DEV_YEARS]
    dc = (inner["c"] - inner.groupby("f")["c"].transform("mean")).to_numpy(dtype=float)
    de = (inner["e"] - inner.groupby("f")["e"].transform("mean")).to_numpy(dtype=float)
    n_obs = table["n_obs"].to_numpy(dtype=float)
    return {
        "n": int(has.sum()),
        "bins": rows,
        "upper_heap_from": SAND_LOAM_SPLIT,
        "spearman_confidence_vs_abs_error": model._spearman(c[ok], err[ok]),
        "spearman_confidence_vs_abs_error_upper_heap": model._spearman(c[up], err[up]) if up.sum() > 10 else None,
        "spearman_confidence_vs_abs_error_lower_heap": model._spearman(c[low], err[low]) if low.sum() > 10 else None,
        "n_rows_lower_heap": int(low.sum()),
        "spearman_confidence_vs_abs_error_constant_guess": model._spearman(c[ok], err_const[ok]),
        "spearman_confidence_vs_abs_error_within_field": model._spearman(dc, de) if len(dc) > 10 else None,
        "n_rows_within_field": int(len(dc)),
        "spearman_confidence_vs_n_obs": model._spearman(c[has], n_obs[has]),
        "spearman_confidence_vs_abs_jump": model._spearman(stable["_c"].to_numpy(), jumps) if len(stable) > 10 else None,
    }


def split_check(field_id, y, pred, groups=None) -> dict:
    """The table's predictions inside each heap of the label: does the model only tell sand from loam?

    `within_type` pools the two heaps after removing each heap's own means (model.within_type_sensitivity):
    the sensitivity that matters for change in one field, with its interval from resampling map units.
    """
    ids = np.asarray(field_id, dtype=object)
    out: dict = {"split_at": SAND_LOAM_SPLIT}
    for name, mask in (("below", y < SAND_LOAM_SPLIT), ("above", y >= SAND_LOAM_SPLIT)):
        fy, fp = model.field_means(ids[mask], y[mask], pred[mask])
        m = model.metrics(fy, fp)
        out[name] = {
            "n_fields": m["n"],
            "spearman": m["spearman"],
            "sensitivity": m["sensitivity"],
            "rmse": m["rmse"],
            "mean_label": float(np.mean(fy)) if len(fy) else None,
            "mean_pred": float(np.mean(fp)) if len(fp) else None,
        }
    frame = pd.DataFrame({"f": ids, "y": y, "p": pred, "g": np.asarray(groups, dtype=object) if groups is not None else None})
    frame = frame[np.isfinite(frame["p"]) & np.isfinite(frame["y"])]
    per = frame.groupby("f", sort=True).agg(y=("y", "first"), p=("p", "mean"), g=("g", "first"))
    kind = np.where(per["y"].to_numpy(dtype=float) < SAND_LOAM_SPLIT, "below", "above")
    out["within_type"] = model.within_type_sensitivity(per["y"], per["p"], kind, per["g"].to_numpy(dtype=object) if groups is not None else None)
    return out


def sentinel2_adds(table: pd.DataFrame, wide: pd.DataFrame, block_model: dict, block_noise: dict, bare_checks: dict) -> dict | None:
    """What the table gains from Sentinel-2 over Landsat alone (None unless both families are present).

    Sentinel-2 is roughly two thirds of the Earth Engine cost of the extraction, so the build says
    what it buys: coverage, a second reading of bare_days, and nothing for the organic-matter model.
    """
    if not (wide["p_s2"].notna().any() or wide["bd_s2"].notna().any()) or not wide["bd_landsat"].notna().any():
        return None
    land_bd = wide["bd_landsat"].notna()
    land_om = wide["p_landsat"].notna()
    years = wide["year"].to_numpy()

    def r2(family: str, scheme: str):
        return block_model["families"].get(family, {}).get("schemes", {}).get(scheme, {}).get("hgb", {}).get("row", {}).get("r2")

    def jump(name: str):
        return block_noise["jumps"].get(name, {}).get("raw", {}).get("robust_sd")

    one = bare_checks.get("sensors", {}).get("noise_sd_one_sensor")
    return {
        "bare_days_coverage_landsat_alone": float(land_bd.mean()),
        "bare_days_coverage_both": float(table["bare_days"].notna().mean()),
        "bare_days_missing_landsat_alone_by_year": {int(y): float(1 - land_bd[years == y].mean()) for y in sorted(set(years.tolist()))},
        "bare_days_r_between_sensors": bare_checks.get("sensors", {}).get("r"),
        "bare_days_noise_sd_one_sensor": one,
        "bare_days_noise_sd_two_sensors": one / np.sqrt(2) if one is not None else None,
        "om_pred_coverage_landsat_alone": float(land_om.mean()),
        "om_pred_coverage_both": float(table["om_pred"].notna().mean()),
        "r2_map_unit": {"landsat": r2("landsat", "map_unit"), "s2": r2("s2", "map_unit"), "combined": block_model["headline"]["row"]["r2"]},
        "r2_series": {"landsat": r2("landsat", "series"), "s2": r2("s2", "series")},
        "om_pred_jump_robust_sd": {"landsat": jump("landsat"), "s2": jump("s2"), "combined": jump("combined")},
    }


def model_block(table: pd.DataFrame, wide: pd.DataFrame, info: pd.DataFrame, results: dict, schemes) -> dict:
    """The "model" block of the meta file: labels, accuracy under every scheme, and the checks on it."""
    ids = table["field_id"].to_numpy(dtype=object)
    pred = table["om_pred"].to_numpy(dtype=float)
    y = table["field_id"].map(info["om"]).to_numpy(dtype=float)
    groups = table["field_id"].map(info["mukey"]).to_numpy(dtype=object)
    pure = table["field_id"].map(info["dominant_share"]).to_numpy(dtype=float) >= model.PURE_SHARE
    covered = table.groupby("year")["om_pred"].agg(lambda s: s.notna().mean())
    families = {}
    for s, result in results.items():
        chosen = pd.Series(result.configs).value_counts().to_dict() if result.configs else {}
        families[s] = {
            "n_composites": int(len(result.frame)),
            "n_fields": int(result.frame["field_id"].nunique()),
            "configs_chosen_in_headline_folds": {str(k): int(v) for k, v in chosen.items()},
            "schemes": scheme_block(wide, info, schemes, f"_{s}"),
            "field_median_check": model.field_median_check(result.frame),
        }
    headline = {
        "scheme": "map_unit",
        "what": "om_pred of the table (both families combined), out-of-fold by dominant map unit",
        **model.evaluate(ids, y, pred, groups),
    }
    combined = scheme_block(wide, info, schemes, "_combined")
    series_r2 = combined.get("series", {}).get("hgb", {}).get("row", {}).get("r2")
    return {
        "label": "organic_matter_pct of data/fields.parquet: SSURGO om_r, surface horizon, area-weighted over the field's map units",
        "what_accuracy_means": WHAT_ACCURACY_MEANS,
        "method_version": METHOD_VERSION,
        "features": list(features.FEATURES),
        "hgb_configs": model.HGB_CONFIGS,
        "default_config": model.DEFAULT_CONFIG,
        "n_splits": model.N_SPLITS,
        "block_km": model.BLOCK_KM,
        "schemes_explained": model.SCHEMES,
        "labels": {**model.label_summary(info.reset_index()), "n_series": int(info["series"].dropna().nunique()) if "series" in info else 0},
        "headline": headline,
        "combined": combined,
        # the honest accuracy is a range: series-grouped (stricter) to map-unit-grouped (the table's own scheme)
        "honest_r2_range": sorted(v for v in (series_r2, headline["row"]["r2"]) if v is not None) if series_r2 is not None else None,
        "families": families,
        "pure_fields": {"min_dominant_share": model.PURE_SHARE, **model.evaluate(ids[pure], y[pure], pred[pure])},
        "sand_loam": split_check(ids, y, pred, groups),
        "coverage": float(np.isfinite(pred).mean()),
        "coverage_by_year": {int(year): float(share) for year, share in covered.items()},
        "fields_never_predicted": int((table.groupby("field_id")["om_pred"].count() == 0).sum()),
        "pred_sd": float(np.nanstd(pred)) if np.isfinite(pred).any() else None,
    }


# --------------------------------------------------------------------------- the build


def build(
    obs_path: Path = OBS_PATH,
    fields_path: Path = FIELDS_PATH,
    out_path: Path = SOIL_HEALTH_PATH,
    meta_path: Path = SOIL_HEALTH_META_PATH,
    report_path: Path = REPORT_PATH,
    *,
    satellite_path: Path | None = SATELLITE_PATH,
    log_path: Path | None = None,
    mapunits: pd.DataFrame | None = None,
    ssurgo_cache: Path | None = None,
    schemes=tuple(model.SCHEMES),
) -> dict:
    """Run everything and write the table, the meta file and the report. Returns the meta dict plus "summary_lines".

    `mapunits` (index field_id; columns dominant_mukey, dominant_share) replaces the SSURGO cache
    lookup; the tests use it. `schemes` must contain "map_unit", the scheme the table is made from.
    The log is written whether the build succeeds or not.
    """
    t0 = time.monotonic()
    log_lines: list[str] = []
    if log_path is None:
        log_path = LOG_DIR / f"soil_build_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
    log_path = Path(log_path)

    def say(msg: str) -> None:
        log_lines.append(f"[{time.monotonic() - t0:7.1f}s] {msg}")

    try:
        meta, table, report = _compute(Path(obs_path), Path(fields_path), out_path, satellite_path, mapunits, ssurgo_cache, schemes, say)
        _atomic_write(Path(out_path), lambda tmp: table[COLUMNS].to_parquet(tmp, index=False))
        _atomic_write(Path(meta_path), lambda tmp: tmp.write_text(json.dumps(meta, indent=1, allow_nan=False) + "\n"))
        _atomic_write(Path(report_path), lambda tmp: tmp.write_text(report))
        lines = summary_lines(meta, out_path, meta_path, report_path, time.monotonic() - t0)
        log_lines += ["", *lines]
    except Exception as exc:
        say(f"FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(log_lines) + "\n")
    meta["summary_lines"] = [*lines, f"log: {log_path}"]
    return meta


def _compute(obs_path: Path, fields_path: Path, out_path, satellite_path, mapunits, ssurgo_cache, schemes, say):
    """Everything up to, but not including, writing files: (meta, table, report text)."""
    from pipeline.schema import load_fields

    if "map_unit" not in schemes:
        raise BuildError('schemes must include "map_unit": the table is made from its out-of-fold predictions')
    if not obs_path.exists():
        raise BuildError(f"{obs_path} does not exist (build it with: python -m pipeline.soil.bare_soil)")
    obs = pd.read_parquet(obs_path)
    features.check_obs(obs)
    method = obs_method(obs_path)
    snow_screened = not (method is not None and method.get("snow_screen") is False)
    fields = load_fields(fields_path)
    warnings_out: list[str] = []

    known = set(fields["field_id"])
    stray = sorted(set(obs["field_id"]) - known)
    if stray:
        warnings_out.append(f"{len(stray)} field(s) of {obs_path.name} are not in {fields_path.name} and were dropped (e.g. {stray[0]})")
        obs = obs[obs["field_id"].isin(known)]
    field_ids = sorted(set(obs["field_id"]))
    if not field_ids:
        raise BuildError(f"{obs_path} has no field of {fields_path}")
    if len(known) > len(field_ids):
        warnings_out.append(
            f"{len(known) - len(field_ids):,} of {len(known):,} fields in {fields_path.name} have no row in {obs_path.name}: "
            f"the table covers {len(field_ids):,} fields"
        )
    if not snow_screened:
        warnings_out.append(
            f"{obs_path.name} was built without the per-view snow screen: mostly-snow composites are dropped here, "
            "but Sentinel-2 n_obs and bare_days stay inflated in snowy winters"
        )
    obs = obs[obs["year"].isin(YEARS)]
    say(f"obs {obs_path}: {len(obs):,} rows, {len(field_ids):,} fields; snow screen in the table: {snow_screened}")

    sub = fields[fields["field_id"].isin(field_ids)].reset_index(drop=True)
    info = field_frame(sub, mapunits, ssurgo_cache).set_index("field_id")
    prepared = features.prepare(obs)
    prepared["om"] = prepared["field_id"].map(info["om"]).to_numpy(dtype=float)
    prepared["mukey"] = prepared["field_id"].map(info["mukey"]).to_numpy(dtype=object)
    prepared["block"] = prepared["field_id"].map(info["block"]).to_numpy(dtype=object)
    prepared["series"] = prepared["field_id"].map(info["series"]).to_numpy(dtype=object)

    # ---- one model per sensor family
    results: dict = {}
    for s in SENSORS:
        part = prepared[(prepared["sensor"] == s) & prepared["usable"]]
        if len(part) < MIN_COMPOSITES or part["field_id"].nunique() < MIN_FIELDS:
            warnings_out.append(f"{SENSOR_LABEL[s]}: only {len(part)} usable composite(s); no model for this family")
            continue
        say(f"training {s}: {len(part):,} usable composites of {part['field_id'].nunique():,} fields")
        results[s] = model.train_family(part, s, schemes=schemes, say=say)
    if not results:
        raise BuildError("no sensor family has enough usable bare-soil composites to train a model")
    schemes = tuple(scheme for scheme in schemes if any(scheme in result.preds for result in results.values()))

    # ---- combine the families, confidence, bare days
    wide = wide_table(prepared, field_ids, results)
    extra = {f"{scheme}_{name}": {s: f"{scheme}_{name}_{s}" for s in results} for scheme in schemes for name in model.MODELS}
    combined, combine_info = conf.combine(wide, extra)
    for col in extra:
        wide[f"{col}_combined"] = combined[col].to_numpy()
    ids = wide["field_id"].to_numpy(dtype=object)
    y = wide["field_id"].map(info["om"]).to_numpy(dtype=float)
    om_pred = np.clip(combined["om_pred"].to_numpy(dtype=float), 0.0, 100.0)
    noise_var = combined["noise_var"].to_numpy(dtype=float)

    fy, fp = model.field_means(ids, y, om_pred)
    ok = np.isfinite(fy) & np.isfinite(fp)
    r_field = float(np.corrcoef(fy[ok], fp[ok])[0, 1]) if ok.sum() > 2 and fp[ok].std() > 0 and fy[ok].std() > 0 else 0.0
    skill = max(r_field, 0.0) ** 2
    stable = conf.stable_variance(ids, om_pred, noise_var)
    confidence = conf.confidence(om_pred, noise_var, stable["tau2"], skill)
    bare_days, bare_info = exposure.combine_bare_days(wide, snow_screened=snow_screened)

    table = pd.DataFrame(
        {
            "field_id": ids,
            "year": wide["year"].to_numpy(dtype="int64"),
            "om_pred": np.round(om_pred, 3),
            "n_obs": np.round(sum(wide[f"n_{s}"].to_numpy(dtype=float) for s in SENSORS), 3),
            "confidence": np.round(confidence, 3),
            "bare_days": np.round(bare_days, 1),
        }
    )
    table.loc[table["om_pred"].isna(), "n_obs"] = 0.0
    validate_soil_health(table[COLUMNS], str(out_path))

    # ---- accuracy, bare-days checks, noise, verdict
    block_model = model_block(table, wide, info, results, schemes)
    crops = noise.crop_table(sub, YEARS)
    cover = None
    if satellite_path is not None and Path(satellite_path).exists():
        cover = pd.read_parquet(satellite_path, columns=["field_id", "year", "cover_frac"])
        cover = cover[cover["field_id"].isin(set(field_ids))]
    bare_checks = exposure.checks(table, wide, crops, cover)
    bare_passes = exposure.passes(bare_checks)
    block_bare = {
        **bare_info,
        "coverage": float(table["bare_days"].notna().mean()),
        "checks": bare_checks,
        "passes": bare_passes,
    }
    s2_years = wide.loc[wide["p_s2"].notna(), "year"]
    joins_later = len(s2_years) and wide["p_landsat"].notna().any() and int(s2_years.min()) > min(YEARS)
    block_noise = noise.analyse(
        table.assign(block=table["field_id"].map(info["block"]).to_numpy(dtype=object)),
        wide,
        crops,
        sensitivity=block_model["headline"]["field"]["sensitivity"],
        first_s2_year=int(s2_years.min()) if joins_later else None,
        sensitivity_interval=block_model["headline"]["field"].get("sensitivity_interval90"),
        within_type=block_model["sand_loam"]["within_type"],
        bare=bare_passes,
    )
    has = np.isfinite(om_pred)
    quantiles = (("p05", 0.05), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p95", 0.95), ("max", 1.0))
    block_conf = {
        "formula": "confidence = skill x tau2 / (tau2 + v x g); see pipeline/soil/confidence.py",
        "meaning": WHAT_CONFIDENCE_MEANS,
        "skill": skill,
        "r_field_means_vs_ssurgo": r_field,
        **stable,
        **combine_info,
        "quantiles": {name: float(np.quantile(confidence[has], q)) for name, q in quantiles} if has.any() else {},
        "check": confidence_check(table, y, crops),
    }
    if bare_passes["all_pass"] is False:
        warnings_out.append(f"bare_days fails its own checks ({', '.join(bare_passes['failed'])}): neither column is trustworthy in this build")

    verdict = block_noise["verdict"]
    meta = {
        "recommended": verdict["recommended"],
        "om_noise_sd": block_noise["om_noise_sd"],
        "verdict": verdict["text"],
        "trustworthy": verdict["trustworthy"],
        "notes": {"om_pred": WHAT_OM_PRED_MEANS, "bare_days": WHAT_BARE_DAYS_MEANS, "confidence": WHAT_CONFIDENCE_MEANS},
        "method_version": METHOD_VERSION,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": {
            "obs": str(obs_path),
            "fields": str(fields_path),
            "n_fields": len(field_ids),
            "n_fields_in_fields_file": len(known),
            "years": [min(YEARS), max(YEARS)],
            "obs_method": method,
            "snow_screened_input": snow_screened,
            "composites": {s: int((prepared["sensor"] == s).sum()) for s in SENSORS},
            "usable_composites": {s: int((prepared["usable"] & (prepared["sensor"] == s)).sum()) for s in SENSORS},
            "composites_dropped_as_snow": {s: int(wide[f"snowy_{s}"].sum()) for s in SENSORS},
        },
        "warnings": warnings_out,
        "model": block_model,
        "confidence": block_conf,
        "noise": block_noise,
        "bare_days": block_bare,
        "sentinel2_adds": sentinel2_adds(table, wide, block_model, block_noise, bare_checks),
    }
    meta = _clean(meta)
    problems = check_meta(meta)
    if problems:
        raise SoilHealthSchemaError("meta does not match pipeline/soil/health_schema.py:\n" + "\n".join(f"  - {p}" for p in problems))
    return meta, table, render_report(meta, out_path)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    from pipeline.schema import SchemaError
    from pipeline.soil.ssurgo import SoilDataError

    parser = argparse.ArgumentParser(description="Build data/soil_health.parquet (yearly soil table) from the bare-soil views.")
    parser.add_argument("--obs", type=Path, default=OBS_PATH, help="bare-soil table from pipeline/soil/bare_soil.py")
    parser.add_argument("--fields", type=Path, default=FIELDS_PATH)
    parser.add_argument("--out", type=Path, default=SOIL_HEALTH_PATH)
    parser.add_argument("--meta", type=Path, default=SOIL_HEALTH_META_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--satellite", type=Path, default=SATELLITE_PATH, help="field_satellite.parquet for the green-cover check (skipped if absent)")
    parser.add_argument("--log", type=Path, default=None, help="log file (default data/logs/soil_build_<time>.log)")
    args = parser.parse_args(argv)
    known_failures = (BuildError, features.ObsError, model.ModelError, SoilHealthSchemaError, SchemaError, SoilDataError, FileNotFoundError)
    try:
        meta = build(args.obs, args.fields, args.out, args.meta, args.report, satellite_path=args.satellite, log_path=args.log)
    except known_failures as exc:
        print(f"soil build failed: {exc}")
        return 1
    print("\n".join(meta["summary_lines"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
