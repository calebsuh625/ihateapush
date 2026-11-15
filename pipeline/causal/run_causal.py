"""Phase 2B, one command: rotation effects on soil from matched neighbouring fields.

    .venv/bin/python -m pipeline.causal.run_causal                  # also: make causal
    .venv/bin/python -m pipeline.causal.run_causal --outcome bare_soil_index
    .venv/bin/python -m pipeline.causal.run_causal --quick          # 200 bootstraps, 20 shuffles

Reads  data/fields.parquet (else the fake fields) and data/soil_health.parquet (else the fake soil
       file; it is made on the spot if neither exists).
Writes data/effects.json, data/pairs.parquet, data/causal_fields.parquet (see schema.md).

Steps: per-field features (rotation types, irrigation, soil windows) -> label-free look-alike
candidates -> 1:1 matching per comparison -> difference-in-differences per pair -> effects with
cluster-bootstrap CIs per comparison, overall and by irrigation / soil group -> placebos (fake
switch years, shuffled labels) -> files.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402

from pipeline.causal import config, did, irrigation, make_fake_soil, matching, rotations, soil_data  # noqa: E402
from pipeline.common import EQUAL_AREA_CRS, log  # noqa: E402
from pipeline.schema import FAKE_PATH, REAL_PATH, load_fields, parse_crop_history  # noqa: E402

GROUP_ALL = "all"


# --------------------------------------------------------------------------------------
# Per-field features
# --------------------------------------------------------------------------------------


def field_features(fields, soil: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    """Feature table (one row per field) + the field x year value and weight matrices."""
    years = np.arange(config.FIRST_YEAR, config.LAST_YEAR + 1)
    ids = fields["field_id"].astype(str).tolist()
    index = {fid: k for k, fid in enumerate(ids)}
    values = np.full((len(ids), len(years)), np.nan)
    weights = np.zeros_like(values)
    s = soil[soil["field_id"].isin(index) & soil["year"].between(years[0], years[-1])]
    rows = s["field_id"].map(index).to_numpy()
    cols = s["year"].to_numpy() - years[0]
    values[rows, cols] = s["value"].to_numpy()
    weights[rows, cols] = np.asarray(s["weight"], dtype=float)

    geoms = shapely.force_2d(fields.to_crs(EQUAL_AREA_CRS).geometry.values)
    cent = shapely.centroid(geoms)
    fill = irrigation.circle_fill(geoms)
    hist = [parse_crop_history(h) for h in fields["crop_history"]]
    pre_last = config.SWITCH_YEAR - 1
    irr = [irrigation.classify(f, h, config.FIRST_YEAR, pre_last) for f, h in zip(fill, hist)]
    win = did.field_windows(values, weights, years, config.SWITCH_YEAR)

    f = pd.DataFrame(
        {
            "row": np.arange(len(ids)),
            "field_id": ids,
            "x": shapely.get_x(cent),
            "y": shapely.get_y(cent),
            "slope": fields["slope_pct"].astype(float).to_numpy(),
            "ssurgo_om": fields["organic_matter_pct"].astype(float).to_numpy(),
            "circle_fill": fill,
            "irrigation": [i for i, _ in irr],
            "irrigation_reason": [r for _, r in irr],
            "rotation_pre": [rotations.rotation_type(h, config.FIRST_YEAR, pre_last) for h in hist],
            "rotation_post": [rotations.rotation_type(h, config.SWITCH_YEAR, config.LAST_YEAR) for h in hist],
            "mix": [np.array(rotations.crop_mix(h, config.FIRST_YEAR, pre_last)) for h in hist],
            **win,
        }
    )
    f["soil_group"] = [config.soil_group(v) for v in f["ssurgo_om"]]
    f["eligible"] = (
        (f["irrigation"] != "uncertain")
        & (f["n_pre"] >= config.MIN_PRE_OBS)
        & (f["n_post"] >= config.MIN_POST_OBS)
        & f[["slope", "ssurgo_om", "baseline", "pre", "post"]].notna().all(axis=1)
    )
    return f, values, weights, years


def pair_did(f: pd.DataFrame, a_rows: np.ndarray, b_rows: np.ndarray) -> np.ndarray:
    change = (f["post"] - f["pre"]).to_numpy()
    return change[a_rows] - change[b_rows]


def comparison_name(a: str, b: str) -> str:
    return f"{a} vs {b}"


# --------------------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------------------


def effect_rows(pairs: pd.DataFrame, rng, n_boot: int, planted: np.ndarray | None) -> list[dict]:
    """One row per (comparison, group) with >= MIN_PAIRS pairs."""
    out = []
    splits = [("irrigation", GROUP_ALL, np.ones(len(pairs), dtype=bool))]
    for g in ("irrigated", "dryland"):
        splits.append(("irrigation", g, (pairs["irrigation"] == g).to_numpy()))
    for _, _, name in config.SOIL_GROUPS:
        splits.append(("soil", name, (pairs["soil_group"] == name).to_numpy()))
    for (a, b), sub_index in pairs.groupby(["rotation_a", "rotation_b"]).groups.items():
        in_comp = pairs.index.isin(sub_index)
        for kind, group, mask in splits:
            m = in_comp & mask
            if m.sum() < config.MIN_PAIRS:
                continue
            est = did.estimate(pairs["did"].to_numpy()[m], pairs["cluster"].to_numpy()[m], rng, n_boot)
            row = {
                "a": a,
                "b": b,
                "comparison": comparison_name(a, b),
                "group_kind": kind,
                "group": group,
                **est,
                "ci_excludes_zero": bool(est["ci_low"] > 0 or est["ci_high"] < 0),
            }
            if planted is not None:
                row["planted_effect"] = float(np.nanmean(planted[m]))
            out.append(row)
    return out


def by_year(pairs: pd.DataFrame, f: pd.DataFrame, values: np.ndarray, years: np.ndarray) -> dict[str, list]:
    """Event study per comparison (all groups): mean gap (A - B) each year relative to `pre`."""
    out = {}
    pre = f["pre"].to_numpy()
    for (a, b), sub in pairs.groupby(["rotation_a", "rotation_b"]):
        if len(sub) < config.MIN_PAIRS:
            continue
        ar, br = sub["a_row"].to_numpy(), sub["b_row"].to_numpy()
        gap = (values[ar] - pre[ar, None]) - (values[br] - pre[br, None])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # a year with no pair observed -> NaN
            mean = np.nanmean(gap, axis=0)
        n = np.isfinite(gap).sum(axis=0)
        out[comparison_name(a, b)] = [
            {"year": int(y), "gap": None if not np.isfinite(m) else round(float(m), 4), "n_pairs": int(k)}
            for y, m, k in zip(years, mean, n)
        ]
    return out


# --------------------------------------------------------------------------------------
# Placebos
# --------------------------------------------------------------------------------------


def placebo_switch_years(pairs, values, weights, years, rng, n_boot) -> list[dict]:
    """Pretend the switch happened inside the history period; the 'effect' should be ~0."""
    out = []
    for fake in config.PLACEBO_SWITCH_YEARS:
        change = did.placebo_switch(values, weights, years, fake, config.SWITCH_YEAR)
        d_all = change[pairs["a_row"].to_numpy()] - change[pairs["b_row"].to_numpy()]
        for (a, b), sub in pairs.groupby(["rotation_a", "rotation_b"]):
            if len(sub) < config.MIN_PAIRS:
                continue
            m = pairs.index.isin(sub.index)
            est = did.estimate(d_all[m], pairs["cluster"].to_numpy()[m], rng, n_boot)
            if est["n_pairs"] < config.MIN_PAIRS:
                continue
            out.append(
                {
                    "fake_switch_year": fake,
                    "comparison": comparison_name(a, b),
                    **{k: est[k] for k in ("effect", "ci_low", "ci_high", "p_value", "n_pairs")},
                    "ci_excludes_zero": bool(est["ci_low"] > 0 or est["ci_high"] < 0),
                }
            )
    return out


def placebo_shuffle(f, cands, real: dict[str, float], rng, n_shuffles: int) -> list[dict]:
    """Shuffle 2016-2024 rotation labels within each irrigation group, re-match, re-estimate."""
    labels = f["rotation_post"].to_numpy().copy()
    groups = [np.flatnonzero((f["irrigation"] == g).to_numpy() & f["eligible"].to_numpy()) for g in ("irrigated", "dryland")]
    null: dict[str, list[float]] = {name: [] for name in real}
    for _ in range(n_shuffles):
        shuffled = labels.copy()
        for idx in groups:
            shuffled[idx] = rng.permutation(labels[idx])
        p = matching.match(cands, shuffled)
        if len(p) == 0:
            continue
        p["did"] = pair_did(f, p["a_row"].to_numpy(), p["b_row"].to_numpy())
        means = p.groupby(["rotation_a", "rotation_b"])["did"].mean()
        for (a, b), value in means.items():
            name = comparison_name(a, b)
            if name in null:
                null[name].append(float(value))
    out = []
    for name, effect in real.items():
        vals = np.array(null[name])
        if len(vals) == 0:
            continue
        out.append(
            {
                "comparison": name,
                "real_effect": effect,
                "null_mean": float(vals.mean()),
                "null_sd": float(vals.std(ddof=1)) if len(vals) > 1 else None,
                "null_p95_abs": float(np.percentile(np.abs(vals), 95)),
                "p_value": float((1 + np.sum(np.abs(vals) >= abs(effect))) / (1 + len(vals))),
                "n_shuffles": int(len(vals)),
            }
        )
    return out


# --------------------------------------------------------------------------------------
# Demo pairs
# --------------------------------------------------------------------------------------


def demo_pairs(pairs: pd.DataFrame, effects: list[dict], f: pd.DataFrame, k: int = 3) -> list[dict]:
    """The clearest pairs for a pitch: from comparisons whose overall CI excludes 0, the pair with the
    best match score among those that moved in the comparison's direction by at least its average,
    with both fields well observed; one per comparison, largest comparisons first."""
    sig = [e for e in effects if e["group"] == GROUP_ALL and e["ci_excludes_zero"]]
    sig.sort(key=lambda e: -e["n_pairs"])
    n_post = f["n_post"].to_numpy()
    out = []
    for e in sig:
        sub = pairs[(pairs["rotation_a"] == e["a"]) & (pairs["rotation_b"] == e["b"])]
        sign = np.sign(e["effect"])
        sub = sub[(np.sign(sub["did"]) == sign) & (sub["did"].abs() >= abs(e["effect"]))]
        sub = sub[(n_post[sub["a_row"]] >= 6) & (n_post[sub["b_row"]] >= 6)]
        if len(sub) == 0:
            continue
        best = sub.sort_values("match_score").iloc[0]
        out.append(
            {
                "pair_id": best["pair_id"],
                "comparison": e["comparison"],
                "field_a": best["field_a"],
                "field_b": best["field_b"],
                "distance_km": round(float(best["distance_km"]), 2),
                "match_score": round(float(best["match_score"]), 3),
                "did": round(float(best["did"]), 4),
            }
        )
        if len(out) == k:
            break
    return out


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def _round(obj, nd: int = 4):
    if isinstance(obj, float):
        return round(obj, nd) if np.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v, nd) for v in obj]
    return obj


def run(outcome_column: str | None = None, quick: bool = False) -> dict:
    t0 = time.monotonic()
    n_boot = 200 if quick else config.N_BOOTSTRAP
    n_shuffles = 20 if quick else config.N_SHUFFLES
    rng = np.random.default_rng(config.SEED)
    outcome = config.outcome(outcome_column)

    fields_path = Path(os.environ["FIELDS_PATH"]) if os.environ.get("FIELDS_PATH") else (
        REAL_PATH if REAL_PATH.is_file() else FAKE_PATH
    )
    fields = load_fields(fields_path)
    soil_path = soil_data.resolve_soil_path()
    if soil_path is None:
        log("no data/soil_health.parquet yet: writing the FAKE soil file (planted effects)")
        make_fake_soil.main()
        soil_path = config.SOIL_FAKE_PATH
    fake_soil = soil_data.is_fake(soil_path)
    soil = soil_data.load_soil(soil_path, outcome.column)
    log(f"fields: {fields_path.name} ({len(fields):,}); soil: {soil_path.name} ({len(soil):,} rows)"
        f"{' *** FAKE SOIL DATA ***' if fake_soil else ''}; outcome: {outcome.column}")

    f, values, weights, years = field_features(fields, soil)
    log(f"features: {int(f['eligible'].sum()):,} of {len(f):,} fields eligible; irrigation "
        + ", ".join(f"{k} {v:,}" for k, v in f["irrigation"].value_counts().items()))

    cands = matching.candidate_pairs(f)
    log(f"candidates: {len(cands):,} look-alike pairs pass every caliper")
    pairs = matching.match(cands, f["rotation_post"].to_numpy())
    if len(pairs) == 0:
        raise SystemExit("no matched pairs: loosen the calipers in pipeline/causal/config.py")
    ar, br = pairs["a_row"].to_numpy(), pairs["b_row"].to_numpy()
    pairs["did"] = pair_did(f, ar, br)
    pairs["cluster"] = did.cluster_ids(f["x"].to_numpy()[ar], f["y"].to_numpy()[ar])
    pairs["soil_group"] = [config.soil_group(v) for v in (f["ssurgo_om"].to_numpy()[ar] + f["ssurgo_om"].to_numpy()[br]) / 2]
    pairs["comparison"] = [comparison_name(a, b) for a, b in zip(pairs["rotation_a"], pairs["rotation_b"])]
    pairs["field_a"] = f["field_id"].to_numpy()[ar]
    pairs["field_b"] = f["field_id"].to_numpy()[br]
    pairs = pairs.sort_values(["comparison", "match_score"]).reset_index(drop=True)
    pairs["pair_id"] = [f"P{k:05d}" for k in range(len(pairs))]
    log(f"matched: {len(pairs):,} pairs in {pairs['comparison'].nunique()} comparisons")

    planted = None
    if fake_soil and config.SOIL_FAKE_TRUTH_PATH.is_file():
        truth = pd.read_parquet(config.SOIL_FAKE_TRUTH_PATH).rename(columns={"om_true": "value"}).assign(weight=1.0)
        if outcome.column == "om_pred":
            _, tv, _, _ = field_features(fields, truth)
            # the noiseless series on exactly the cells (and weights) the estimate saw
            tv = np.where(np.isfinite(values), tv, np.nan)
            tw_ = did.field_windows(tv, weights, years, config.SWITCH_YEAR)
            change = tw_["post"] - tw_["pre"]
            planted = change[pairs["a_row"].to_numpy()] - change[pairs["b_row"].to_numpy()]

    effects = effect_rows(pairs, rng, n_boot, planted)
    log(f"effects: {len(effects)} (comparison, group) rows with >= {config.MIN_PAIRS} pairs")
    real_all = {e["comparison"]: e["effect"] for e in effects if e["group"] == GROUP_ALL}
    switch_placebos = placebo_switch_years(pairs, values, weights, years, rng, n_boot)
    log(f"placebo: {len(switch_placebos)} fake-switch-year estimates, "
        f"{sum(p['ci_excludes_zero'] for p in switch_placebos)} with a CI that excludes 0")
    shuffles = placebo_shuffle(f, cands, real_all, rng, n_shuffles)
    log(f"placebo: shuffled labels x{n_shuffles}")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "is_fake": fake_soil,
        "warning": (
            "FAKE SOIL DATA: effects below come from made-up soil scores with planted crop effects. "
            "They test the method; they are NOT findings."
            if fake_soil
            else None
        ),
        "sources": {"fields": fields_path.name, "soil": soil_path.name},
        "outcome": {
            "column": outcome.column,
            "label": outcome.label,
            "units": outcome.units,
            "higher_is_better": outcome.higher_is_better,
        },
        "design": {
            "switch_year": config.SWITCH_YEAR,
            "history_years": [config.FIRST_YEAR, config.SWITCH_YEAR - 1],
            "rotation_years": [config.SWITCH_YEAR, config.LAST_YEAR],
            "estimator": "did = (post_A - pre_A) - (post_B - pre_B); effect = mean over matched pairs",
            "calipers": {
                "max_distance_km": config.MAX_DISTANCE_KM,
                "max_slope_diff": config.MAX_SLOPE_DIFF,
                "max_ssurgo_om_diff": config.MAX_SSURGO_OM_DIFF,
                "max_baseline_diff_sd": config.MAX_BASELINE_DIFF_SD,
                "max_history_distance": config.MAX_HISTORY_DIST,
            },
            "n_bootstrap": n_boot,
            "bootstrap_cluster_km": config.BOOTSTRAP_CLUSTER_KM,
            "min_pairs": config.MIN_PAIRS,
        },
        "counts": {
            "fields": len(f),
            "eligible": int(f["eligible"].sum()),
            "irrigation": {k: int(v) for k, v in f["irrigation"].value_counts().items()},
            "rotation_post": {k: int(v) for k, v in f["rotation_post"].value_counts().items()},
            "candidates": len(cands),
            "pairs": len(pairs),
        },
        "effects": effects,
        "by_year": by_year(pairs, f, values, years),
        "placebos": {"fake_switch_years": switch_placebos, "shuffled_labels": shuffles},
        "demo_pairs": demo_pairs(pairs, effects, f),
    }
    result = _round(result)

    config.EFFECTS_PATH.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    pairs_out = pairs[
        ["pair_id", "comparison", "rotation_a", "rotation_b", "field_a", "field_b", "irrigation", "soil_group",
         "distance_km", "slope_diff", "ssurgo_om_diff", "baseline_diff", "history_distance", "match_score", "did"]
    ].copy()
    a_idx, b_idx = pairs["a_row"].to_numpy(), pairs["b_row"].to_numpy()
    for side, idx in (("a", a_idx), ("b", b_idx)):
        for col in ("rotation_pre", "baseline", "pre", "post"):
            pairs_out[f"{col}_{side}"] = f[col].to_numpy()[idx]
    pairs_out.to_parquet(config.PAIRS_PATH, index=False)
    f_out = f[["field_id", "irrigation", "irrigation_reason", "circle_fill", "rotation_pre", "rotation_post",
               "soil_group", "eligible", "n_pre", "n_post", "baseline", "pre", "post"]]
    f_out.to_parquet(config.CAUSAL_FIELDS_PATH, index=False)
    log(f"wrote {config.EFFECTS_PATH.name}, {config.PAIRS_PATH.name} ({len(pairs_out):,} rows), "
        f"{config.CAUSAL_FIELDS_PATH.name} in {time.monotonic() - t0:.1f}s")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--outcome", help=f"soil column to analyse (default: $CAUSAL_OUTCOME or {config.DEFAULT_OUTCOME})")
    parser.add_argument("--quick", action="store_true", help="fewer bootstraps and shuffles (smoke test)")
    args = parser.parse_args(argv)
    run(args.outcome, quick=args.quick)


if __name__ == "__main__":
    main()
