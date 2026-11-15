"""Phase 2B, one command: rotation comparisons on days the soil sits bare, from matched neighbouring fields.

    .venv/bin/python -m pipeline.causal.run_causal                  # also: make causal
    .venv/bin/python -m pipeline.causal.run_causal --outcome om_pred
    .venv/bin/python -m pipeline.causal.run_causal --quick          # 200 bootstraps, 20 shuffles
    .venv/bin/python -m pipeline.causal.run_causal --counts-only    # stop after matching, write nothing

Reads  data/fields.parquet (else the fake fields) and data/soil_health.parquet (else the fake soil
       file; it is made on the spot if neither exists).
Writes data/effects.json, data/pairs.parquet, data/causal_fields.parquet (see schema.md).

Steps: per-field features (rotation types, irrigation, soil windows) -> label-free look-alike
candidates -> 1:1 matching per comparison -> difference-in-differences per pair, within the same
years -> effects with cluster-bootstrap CIs per comparison, overall and by irrigation / soil
group, each split into the part the crops themselves explain and the rest -> placebos (fake
switch years inside the pre-switch soil years, shuffled labels) -> files.

The outcome is cover (bare days), not a soil measurement, and the switch year is a cut-off, not a
year in which fields changed: the rotation type is simply read from the crops grown from it on.

Every year range comes from pipeline/causal/config.py (switch year 2020 by default).
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

from pipeline.region import REGION  # noqa: E402
from pipeline.causal import config, did, irrigation, make_fake_soil, matching, rotations, soil_data  # noqa: E402
from pipeline.common import EQUAL_AREA_CRS, log  # noqa: E402
from pipeline.schema import FAKE_PATH, REAL_PATH, load_fields, parse_crop_history  # noqa: E402

GROUP_ALL = "all"


# --------------------------------------------------------------------------------------
# Per-field features
# --------------------------------------------------------------------------------------


def field_features(fields, soil: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray]:
    """Feature table (one row per field) + the field x soil-year value and weight matrices."""
    years = np.arange(config.SOIL_FIRST_YEAR, config.LAST_YEAR + 1)
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
    h_first, h_last = config.history_years()
    r_first, r_last = config.rotation_years()
    irr = [irrigation.classify(f, h, h_first, h_last) for f, h in zip(fill, hist)]
    win = did.field_windows(values, weights, years)

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
            "rotation_pre": [rotations.rotation_type(h, h_first, h_last) for h in hist],
            "rotation_post": [rotations.rotation_type(h, r_first, r_last) for h in hist],
            "mix": [np.array(rotations.crop_mix(h, h_first, h_last)) for h in hist],
            "fallow_share_post": [rotations.crop_mix(h, r_first, r_last)[rotations.MIX_GROUPS.index("fallow")] for h in hist],
            "perennial_share_post": [rotations.crop_mix(h, r_first, r_last)[rotations.MIX_GROUPS.index("perennial")] for h in hist],
            **win,
        }
    )
    f["soil_group"] = [config.soil_group(v) for v in f["ssurgo_om"]]
    f["eligible"] = (
        (f["irrigation"] != "uncertain")
        & (f["n_baseline"] >= config.MIN_BASELINE_OBS)
        & (f["n_pre"] >= config.MIN_PRE_OBS)
        & (f["n_post"] >= config.MIN_POST_OBS)
        & f[["slope", "ssurgo_om", "baseline", "pre", "post"]].notna().all(axis=1)
    )
    return f, values, weights, years


def crop_expected(fields, values: np.ndarray, years: np.ndarray, min_cell: int = 20) -> np.ndarray:
    """What each field-year would read if the field simply had the county average, that year, for
    the crop it grew that year (same cells as `values`; a crop with fewer than `min_cell` fields
    that year gets the year's all-crop average). Differences computed on this table are the part of
    an effect that the crops themselves explain, with no look at how the field was farmed.
    """
    hist = [parse_crop_history(h) for h in fields["crop_history"]]
    out = np.full(values.shape, np.nan)
    for t, year in enumerate(years):
        col = values[:, t]
        ok = np.isfinite(col)
        if not ok.any():
            continue
        crops = np.array([h.get(str(int(year)), "") for h in hist], dtype=object)[ok]
        by_crop = pd.Series(col[ok]).groupby(crops).agg(["mean", "count"])
        year_mean = float(col[ok].mean())
        lookup = {c: (float(m) if n >= min_cell else year_mean) for c, m, n in zip(by_crop.index, by_crop["mean"], by_crop["count"])}
        out[ok, t] = [lookup[c] for c in crops]
    return out


def pair_did(values: np.ndarray, weights: np.ndarray, years: np.ndarray, a_rows: np.ndarray, b_rows: np.ndarray) -> np.ndarray:
    return did.pair_windows(values, weights, years, a_rows, b_rows)["did"]


def comparison_name(a: str, b: str) -> str:
    return f"{a} vs {b}"


# --------------------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------------------


def _ci(est: dict) -> dict:
    out = {k: est[k] for k in ("effect", "ci_low", "ci_high")}
    out["ci_excludes_zero"] = bool(est["effect"] is not None and (est["ci_low"] > 0 or est["ci_high"] < 0))
    return out


def favours(effect: float | None, excludes_zero: bool, higher_is_better: bool) -> str | None:
    """"a" / "b": the rotation whose outcome moved the better way; None when the CI includes 0."""
    if effect is None or not excludes_zero or effect == 0:
        return None
    return "a" if (effect > 0) == higher_is_better else "b"


def effect_rows(pairs: pd.DataFrame, rng, n_boot: int, outcome: config.Outcome, planted: np.ndarray | None) -> list[dict]:
    """One row per (comparison, group) with >= MIN_PAIRS pairs."""
    out = []
    splits = [("irrigation", GROUP_ALL, np.ones(len(pairs), dtype=bool))]
    for g in ("irrigated", "dryland"):
        splits.append(("irrigation", g, (pairs["irrigation"] == g).to_numpy()))
    for _, _, name in config.SOIL_GROUPS:
        splits.append(("soil", name, (pairs["soil_group"] == name).to_numpy()))
    cluster = pairs["cluster"].to_numpy()
    for (a, b), sub_index in pairs.groupby(["rotation_a", "rotation_b"]).groups.items():
        in_comp = pairs.index.isin(sub_index)
        for kind, group, mask in splits:
            m = in_comp & mask
            if m.sum() < config.MIN_PAIRS:
                continue
            est = did.estimate(pairs["did"].to_numpy()[m], cluster[m], rng, n_boot)
            excludes = bool(est["ci_low"] > 0 or est["ci_high"] < 0)
            used = pairs[m & pairs["did"].notna().to_numpy()]
            row = {
                "a": a,
                "b": b,
                "comparison": comparison_name(a, b),
                "group_kind": kind,
                "group": group,
                **est,
                "ci_excludes_zero": excludes,
                "favours": favours(est["effect"], excludes, outcome.higher_is_better),
                # the plain A - B gap before and after; effect = gap_post - gap_pre (a change, not a level)
                "gap_pre": float((used["pre_a"] - used["pre_b"]).mean()),
                "gap_post": float((used["post_a"] - used["post_b"]).mean()),
                # the part the crops themselves explain, and what is left over
                "from_crops": _ci(did.estimate(pairs["did_crops"].to_numpy()[m], cluster[m], rng, n_boot)),
                "beyond_crops": _ci(did.estimate(pairs["did_beyond"].to_numpy()[m], cluster[m], rng, n_boot)),
                "fallow_share_a": float(pairs["fallow_share_a"].to_numpy()[m].mean()),
                "fallow_share_b": float(pairs["fallow_share_b"].to_numpy()[m].mean()),
            }
            if planted is not None:
                row["planted_effect"] = float(np.nanmean(planted[m]))
            out.append(row)
    return out


def by_year(pairs: pd.DataFrame, values: np.ndarray, years: np.ndarray) -> dict[str, list]:
    """Event study per comparison (all groups): mean gap (A - B) each year, relative to the pair's
    gap in the `pre` years. A year counts for a pair only when both fields have a value."""
    out = {}
    for (a, b), sub in pairs.groupby(["rotation_a", "rotation_b"]):
        if len(sub) < config.MIN_PAIRS:
            continue
        ar, br = sub["a_row"].to_numpy(), sub["b_row"].to_numpy()
        pre_gap = (sub["pre_a"] - sub["pre_b"]).to_numpy()
        gap = (values[ar] - values[br]) - pre_gap[:, None]
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


def placebo_switch_years(pairs, values, expected, weights, years, rng, n_boot) -> list[dict]:
    """Pretend the switch happened earlier, using only soil years before the real switch. If the two
    groups moved alike before the switch the 'effect' is ~0. A non-zero value has two possible
    causes, and this test cannot tell them apart: (1) the gap was already moving, for instance
    because the crops already differed (`from_crops` is the same number from the crops alone);
    (2) every fake 'before' window contains the baseline years the pairs were matched on, where
    the gap was made small by selection, so it springs back in the years after them."""
    out = []
    ar, br = pairs["a_row"].to_numpy(), pairs["b_row"].to_numpy()
    for fake in config.placebo_switch_years():
        d_all = did.placebo_did(values, weights, years, ar, br, fake)
        d_crops = did.placebo_did(expected, weights, years, ar, br, fake)
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
                    "before_years": [config.SOIL_FIRST_YEAR, fake - 1],
                    "after_years": [fake, config.SWITCH_YEAR - 1],
                    "comparison": comparison_name(a, b),
                    **{k: est[k] for k in ("effect", "ci_low", "ci_high", "p_value", "n_pairs")},
                    "ci_excludes_zero": bool(est["ci_low"] > 0 or est["ci_high"] < 0),
                    "from_crops": float(np.nanmean(d_crops[m])),
                }
            )
    return out


def placebo_shuffle(f, cands, values, weights, years, real: dict[str, float], rng, n_shuffles: int) -> list[dict]:
    """Shuffle the post-switch rotation labels within each irrigation group, re-match, re-estimate."""
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
        p["did"] = pair_did(values, weights, years, p["a_row"].to_numpy(), p["b_row"].to_numpy())
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


def typical_example(rotation: str, fallow_share: float, perennial_share: float) -> bool:
    """Is this field a fair picture of its rotation type? Only used to choose demo pairs. A "diverse"
    field that is fallow in a third or more of the rotation years is mostly a fallow field, and an
    "alfalfa/perennial" field needs a perennial in more than half of them."""
    if rotation == "diverse":
        return fallow_share < 1 / 3
    if rotation == "alfalfa/perennial":
        return perennial_share > 0.5
    return True


def demo_pairs(pairs: pd.DataFrame, effects: list[dict], outcome: config.Outcome, k: int = 3) -> list[dict]:
    """The clearest pairs for a demo: from comparisons whose overall CI excludes 0, the pair with the
    best match score among those that moved in the comparison's direction by at least its average
    and whose plain "after" levels point the same way (so the picture needs no arithmetic), with
    both fields observed in every pre and post year and, where such a pair exists, both fields a
    typical example of their rotation type; one per comparison, largest comparisons first.
    These conditions only choose what to show; they touch no estimate.
    `better` names the field ("a" / "b") with the better "after" level (outcome.higher_is_better);
    in a demo pair that is also the field whose outcome moved the better way."""
    sig = [e for e in effects if e["group"] == GROUP_ALL and e["ci_excludes_zero"]]
    sig.sort(key=lambda e: -e["n_pairs"])
    full_pre = config.pre_years()[1] - config.pre_years()[0] + 1
    full_post = config.post_years()[1] - config.post_years()[0] + 1
    out = []
    for e in sig:
        sub = pairs[(pairs["rotation_a"] == e["a"]) & (pairs["rotation_b"] == e["b"])]
        sign = np.sign(e["effect"])
        sub = sub[(np.sign(sub["did"]) == sign) & (sub["did"].abs() >= abs(e["effect"]))]
        sub = sub[np.sign(sub["post_a"] - sub["post_b"]) == sign]
        sub = sub[(sub["n_pre"] == full_pre) & (sub["n_post"] == full_post)]
        if len(sub) == 0:
            continue
        if "typical" in sub.columns and sub["typical"].any():
            sub = sub[sub["typical"]]
        best = sub.sort_values("match_score").iloc[0]
        out.append(
            {
                "pair_id": best["pair_id"],
                "comparison": e["comparison"],
                "rotation_a": e["a"],
                "rotation_b": e["b"],
                "field_a": best["field_a"],
                "field_b": best["field_b"],
                "distance_km": round(float(best["distance_km"]), 2),
                "match_score": round(float(best["match_score"]), 3),
                **{c: round(float(best[c]), 2) for c in ("pre_a", "post_a", "pre_b", "post_b")},
                "did": round(float(best["did"]), 4),
                "better": "a" if (best["post_a"] > best["post_b"]) == outcome.higher_is_better else "b",
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


def run(outcome_column: str | None = None, quick: bool = False, counts_only: bool = False) -> dict:
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
    if soil_path is None and not REGION.is_default:
        # Outside Finney the fake soil table would be planted numbers on real fields of another county.
        raise SystemExit(
            f"{REGION.label} has no soil_health.parquet of its own; build it first (make soil-obs, then "
            "make soil-health, with the same REGION). Until then the planner uses Finney County's effects."
        )
    if soil_path is None:
        log("no data/soil_health.parquet yet: writing the FAKE soil file (planted effects)")
        make_fake_soil.main()
        soil_path = config.SOIL_FAKE_PATH
    fake_soil = soil_data.is_fake(soil_path)
    problems = soil_data.contract_problems(soil_path)
    if problems:
        raise SystemExit(f"{soil_path.name} does not match pipeline/soil/health_schema.py:\n  - " + "\n  - ".join(problems))
    soil = soil_data.load_soil(soil_path, outcome.column, outcome.weight_column)
    spans = {name: config.span(fn()) for name, fn in (
        ("history", config.history_years), ("rotation", config.rotation_years),
        ("baseline", config.baseline_years), ("pre", config.pre_years), ("post", config.post_years))}
    log(f"fields: {fields_path.name} ({len(fields):,}); soil: {soil_path.name} ({len(soil):,} rows)"
        f"{' *** FAKE SOIL DATA ***' if fake_soil else ''}; outcome: {outcome.column}"
        f" ({'higher' if outcome.higher_is_better else 'lower'} is better; weights: {outcome.weight_column or 'none'})")
    log(f"design: switch {config.SWITCH_YEAR}; crop history {spans['history']}; rotation type from {spans['rotation']};"
        f" soil baseline {spans['baseline']}, pre {spans['pre']}, post {spans['post']}")

    f, values, weights, years = field_features(fields, soil)
    log(f"features: {int(f['eligible'].sum()):,} of {len(f):,} fields eligible; irrigation "
        + ", ".join(f"{k} {v:,}" for k, v in f["irrigation"].value_counts().items()))
    log("rotation types after the switch: " + ", ".join(f"{k} {v:,}" for k, v in f["rotation_post"].value_counts().items()))

    cands = matching.candidate_pairs(f, np.isfinite(values), years)
    log(f"candidates: {len(cands):,} look-alike pairs pass every caliper")
    pairs = matching.match(cands, f["rotation_post"].to_numpy())
    if len(pairs) == 0:
        raise SystemExit(
            f"no matched pairs ({int(f['eligible'].sum()):,} eligible fields, {len(cands):,} candidates). "
            "Check that the soil table covers years on both sides of the switch year."
        )
    ar, br = pairs["a_row"].to_numpy(), pairs["b_row"].to_numpy()
    for name, col in did.pair_windows(values, weights, years, ar, br).items():
        pairs[name] = col
    expected = crop_expected(fields, values, years)
    pairs["did_crops"] = pair_did(expected, weights, years, ar, br)
    pairs["did_beyond"] = pairs["did"] - pairs["did_crops"]
    pairs["fallow_share_a"] = f["fallow_share_post"].to_numpy()[ar]
    pairs["fallow_share_b"] = f["fallow_share_post"].to_numpy()[br]
    perennial = f["perennial_share_post"].to_numpy()
    pairs["typical"] = [
        typical_example(ra, fa, pa) and typical_example(rb, fb, pb)
        for ra, fa, pa, rb, fb, pb in zip(pairs["rotation_a"], pairs["fallow_share_a"], perennial[ar],
                                          pairs["rotation_b"], pairs["fallow_share_b"], perennial[br])
    ]
    pairs["cluster"] = did.cluster_ids(f["x"].to_numpy()[ar], f["y"].to_numpy()[ar])
    pairs["soil_group"] = [config.soil_group(v) for v in (f["ssurgo_om"].to_numpy()[ar] + f["ssurgo_om"].to_numpy()[br]) / 2]
    pairs["comparison"] = [comparison_name(a, b) for a, b in zip(pairs["rotation_a"], pairs["rotation_b"])]
    pairs["field_a"] = f["field_id"].to_numpy()[ar]
    pairs["field_b"] = f["field_id"].to_numpy()[br]
    pairs = pairs.sort_values(["comparison", "match_score"]).reset_index(drop=True)
    pairs["pair_id"] = [f"P{k:05d}" for k in range(len(pairs))]
    per_comparison = {k: int(v) for k, v in pairs["comparison"].value_counts().items()}
    log(f"matched: {len(pairs):,} pairs in {len(per_comparison)} comparisons: "
        + ", ".join(f"{k} {v}" for k, v in per_comparison.items()))
    if counts_only:
        log("counts only: nothing estimated, nothing written")
        return {"counts": {"eligible": int(f["eligible"].sum()), "candidates": len(cands), "pairs": len(pairs),
                           "pairs_by_comparison": per_comparison}}

    planted = None
    if fake_soil and config.SOIL_FAKE_TRUTH_PATH.is_file():
        truth = pd.read_parquet(config.SOIL_FAKE_TRUTH_PATH)
        if outcome.column in truth.columns:
            truth = truth.rename(columns={outcome.column: "value"}).assign(weight=1.0)
            _, tv, _, _ = field_features(fields, truth)
            # the noiseless series on exactly the cells (and weights) the estimate saw
            tv = np.where(np.isfinite(values), tv, np.nan)
            planted = pair_did(tv, weights, years, pairs["a_row"].to_numpy(), pairs["b_row"].to_numpy())

    effects = effect_rows(pairs, rng, n_boot, outcome, planted)
    log(f"effects: {len(effects)} (comparison, group) rows with >= {config.MIN_PAIRS} pairs")
    real_all = {e["comparison"]: e["effect"] for e in effects if e["group"] == GROUP_ALL}
    switch_placebos = placebo_switch_years(pairs, values, expected, weights, years, rng, n_boot)
    log(f"placebo: {len(switch_placebos)} fake-switch-year estimates, "
        f"{sum(p['ci_excludes_zero'] for p in switch_placebos)} with a CI that excludes 0")
    shuffles = placebo_shuffle(f, cands, values, weights, years, real_all, rng, n_shuffles)
    log(f"placebo: shuffled labels x{n_shuffles}")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "is_fake": fake_soil,
        "warning": (
            "FAKE SOIL DATA: effects below come from made-up soil numbers with planted crop effects. "
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
            "weight_column": outcome.weight_column,
            "note": outcome.note,
        },
        "design": {
            "switch_year": config.SWITCH_YEAR,
            "history_years": list(config.history_years()),
            "rotation_years": list(config.rotation_years()),
            "soil_years": [config.SOIL_FIRST_YEAR, config.LAST_YEAR],
            "baseline_years": list(config.baseline_years()),
            "pre_years": list(config.pre_years()),
            "post_years": list(config.post_years()),
            "placebo_switch_years": config.placebo_switch_years(),
            "estimator": "did = (post_A - pre_A) - (post_B - pre_B) over the years both fields have a value; "
                         "effect = mean over matched pairs",
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
            "pairs_by_comparison": per_comparison,
            "fields_in_a_pair": int(pd.concat([pairs["field_a"], pairs["field_b"]]).nunique()),
        },
        "effects": effects,
        "by_year": by_year(pairs, values, years),
        "placebos": {"fake_switch_years": switch_placebos, "shuffled_labels": shuffles},
        "demo_pairs": demo_pairs(pairs, effects, outcome),
    }
    result = _round(result)

    config.EFFECTS_PATH.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    pairs_out = pairs[
        ["pair_id", "comparison", "rotation_a", "rotation_b", "field_a", "field_b", "irrigation", "soil_group",
         "distance_km", "slope_diff", "ssurgo_om_diff", "baseline_diff", "history_distance", "match_score",
         "pre_a", "post_a", "pre_b", "post_b", "n_pre", "n_post", "did", "did_crops", "did_beyond"]
    ].copy()
    a_idx, b_idx = pairs["a_row"].to_numpy(), pairs["b_row"].to_numpy()
    for side, idx in (("a", a_idx), ("b", b_idx)):
        for col in ("rotation_pre", "baseline"):
            pairs_out[f"{col}_{side}"] = f[col].to_numpy()[idx]
    pairs_out.to_parquet(config.PAIRS_PATH, index=False)
    f_out = f[["field_id", "irrigation", "irrigation_reason", "circle_fill", "rotation_pre", "rotation_post",
               "soil_group", "eligible", "n_baseline", "n_pre", "n_post", "baseline", "pre", "post"]]
    f_out.to_parquet(config.CAUSAL_FIELDS_PATH, index=False)
    log(f"wrote {config.EFFECTS_PATH.name}, {config.PAIRS_PATH.name} ({len(pairs_out):,} rows), "
        f"{config.CAUSAL_FIELDS_PATH.name} in {time.monotonic() - t0:.1f}s")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--outcome", help=f"soil column to analyse (default: $CAUSAL_OUTCOME or {config.DEFAULT_OUTCOME})")
    parser.add_argument("--quick", action="store_true", help="fewer bootstraps and shuffles (smoke test)")
    parser.add_argument("--counts-only", action="store_true", help="stop after matching: log the counts, write nothing")
    args = parser.parse_args(argv)
    run(args.outcome, quick=args.quick, counts_only=args.counts_only)


if __name__ == "__main__":
    main()
