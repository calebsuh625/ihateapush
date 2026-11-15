"""Combine the two sensor families into one om_pred per field-year and say how far to trust it.

Input: one row per (field, year) with, per family s in ("landsat", "s2"), the out-of-fold prediction
p_s (NaN when that family had no usable composite), its bare views per pixel n_s (n_bare) and the
share of the field seen bare q_s (bare_px_frac).

1. How noisy is one family's prediction?  (fit_noise_model)
   Real organic matter hardly moves in nine years, so the scatter of a field's yearly predictions
   around the field's own mean is noise (surface moisture, residue, roughness, few dates, a part
   of the field). For every field with at least MIN_YEARS predictions of a family:
       d2 = (p - field mean)^2 x k / (k - 1)        k = that field's number of predictions
   and the noise variance is modelled, per family, with three non-negative coefficients fitted by
   least squares on d2 (capped at its 99th percentile so a few cloud-struck rows do not rule):
       v_s = a_s + b_s / sqrt(m) + c_s x (1 - q)    m = n / q = bare views per pixel that was seen
                                                    bare at all (at least 1)
   a = what never averages out within a year, b = fewer dates, c = only part of the field seen.
   No label is used, so this cannot leak.

2. One value per field-year.  (combine)
       om_pred = sum_s w_s p_s,   w_s proportional to 1 / v_s over the families that have a value
   so the family with more bare views that year counts for more. Its noise variance is
       v = sum_s w_s^2 v_s + 2 w_L w_S rho sqrt(v_L v_S)
   where rho is the measured correlation of the two families' deviations in the same field-year
   (what they share: the state of the surface). With one family, v = v_s.

3. Agreement between the sensors.
   Where both families have a value, their disagreement delta = p_S - p_L is direct evidence about
   this very field-year. Its expected square is e = kappa x (v_L + v_S), kappa fitted so that the
   mean of delta^2 / e is 1. The variance is scaled by
       g = (1 + delta^2 / e) / 2,   limited to [G_MIN, G_MAX]
   i.e. half prior, half this one observation: sensors that agree well halve it at most, sensors
   that disagree by twice the usual amount multiply it by 2.5.

4. confidence (0-1).
       reliability = tau2 / (tau2 + v x g)
   tau2 is the variance between fields of the stable part of om_pred (variance of the field means
   minus the noise left in them), so reliability is the share of a row's variation that is the
   field and not the year. Then
       confidence = skill x reliability
   skill = squared correlation between a field's multi-year mean out-of-fold prediction and its
   SSURGO value (fields grouped by map unit, so no leak). If a prediction is "field signal + noise",
   this product is the squared correlation between rows this noisy and mapped organic matter over
   the whole range of soils: 0.2 means about a fifth of what the number says is about organic
   matter, and the rest is other soil properties, the surface that day, and noise. It is
   deliberately not stretched to reach 1: the best rows of a weak model are still weak, and the
   largest value any row can get is `skill`. confidence is 0 where om_pred is null.

   What it is: REPEATABILITY, not accuracy. Everything in it except the constant `skill` comes
   from how often and how fully the field was seen bare and from whether the two sensor families
   agreed (its rank correlation with n_obs is about 0.7). Measured on the 300 prototype fields:
   * it tracks the year-to-year jump on unchanged fields (higher confidence, smaller jump);
   * its rank correlation with the error against SSURGO (-0.27) is mostly soil mix: fields seen
     bare often lie on loam near the county mean, and a constant guess of the county mean shows
     -0.17 against it;
   * on sandy soils the sign turns (+0.22): the model reads sand as loam, and a well-seen sandy
     field is confidently wrong;
   * between the years of one field it is about -0.08: it does not tell a field's good years
     from its bad ones. Do not use it to rank the years of one field.
   Use it to drop rows that will not repeat. build.confidence_check recomputes all of these in
   every build and the report shows them (section 5).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.soil.features import SENSORS

MIN_YEARS = 3  # a field needs this many predictions of a family to say anything about its scatter
D2_CAP_QUANTILE = 0.99
G_MIN, G_MAX = 0.5, 8.0
RHO_MAX = 0.9
TAU2_FLOOR_SHARE = 0.1  # tau2 is never set below this share of the variance of the field means


def noise_design(n_bare, bare_px_frac) -> np.ndarray:
    """Columns [1, 1 / sqrt(m), 1 - q] of the noise model (see the module docstring)."""
    n = np.asarray(n_bare, dtype=float)
    q = np.clip(np.asarray(bare_px_frac, dtype=float), 1e-6, 1.0)
    m = np.maximum(n / q, 1.0)
    return np.column_stack([np.ones(len(n)), 1.0 / np.sqrt(m), 1.0 - q])


def field_deviation(field_id, values) -> tuple[np.ndarray, np.ndarray]:
    """(deviation from the field's own mean, unbiased squared deviation); NaN where the field has < MIN_YEARS values."""
    v = np.asarray(values, dtype=float)
    frame = pd.DataFrame({"f": np.asarray(field_id, dtype=object), "v": v})
    grouped = frame.groupby("f", sort=False)["v"]
    k = grouped.transform("count").to_numpy(dtype=float)
    dev = v - grouped.transform("mean").to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        d2 = np.where(k >= MIN_YEARS, dev**2 * k / (k - 1), np.nan)
    dev = np.where(k >= MIN_YEARS, dev, np.nan)
    return dev, d2


def fit_noise_model(field_id, pred, n_bare, bare_px_frac) -> dict:
    """Coefficients (a, b, c) of v = a + b / sqrt(m) + c (1 - q) for one family, by non-negative least squares."""
    from scipy.optimize import nnls

    _, d2 = field_deviation(field_id, pred)
    keep = np.isfinite(d2)
    if keep.sum() < 30:  # too little to fit three numbers: one flat variance
        flat = float(np.nanmean(d2)) if keep.any() else float("nan")
        return {"a": flat, "b": 0.0, "c": 0.0, "n_rows": int(keep.sum()), "flat": True}
    target = np.minimum(d2[keep], np.quantile(d2[keep], D2_CAP_QUANTILE))
    design = noise_design(np.asarray(n_bare, dtype=float)[keep], np.asarray(bare_px_frac, dtype=float)[keep])
    coef, _ = nnls(design, target)
    if coef.sum() <= 0:
        coef = np.array([float(target.mean()), 0.0, 0.0])
    return {"a": float(coef[0]), "b": float(coef[1]), "c": float(coef[2]), "n_rows": int(keep.sum()), "flat": False}


def noise_variance(model: dict, n_bare, bare_px_frac) -> np.ndarray:
    design = noise_design(n_bare, bare_px_frac)
    v = design @ np.array([model["a"], model["b"], model["c"]])
    return np.maximum(v, 1e-6)


def combine(wide: pd.DataFrame, extra: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """Steps 1-3 of the module docstring.

    `wide`: one row per (field_id, year) with columns p_<s>, n_<s>, q_<s> for s in SENSORS (a family
    that is absent altogether may be all-NaN). `extra`: {name: {sensor: column prefix}} of further
    prediction sets (other validation schemes) to combine with the same weights, e.g.
    {"random": {"landsat": "random_landsat", "s2": "random_s2"}} -> column "random".

    Returns (frame, info). frame has the index of `wide` and the columns om_pred, noise_var (v x g),
    v_base (v before the agreement factor), agreement (g; 1 with a single family), w_<s>, v_<s>,
    n_sensors and one column per `extra` name. info holds the fitted numbers.
    """
    field_id = wide["field_id"].to_numpy(dtype=object)
    info: dict = {"noise_model": {}}
    p, v, dev = {}, {}, {}
    for s in SENSORS:
        p[s] = wide[f"p_{s}"].to_numpy(dtype=float) if f"p_{s}" in wide else np.full(len(wide), np.nan)
        has = np.isfinite(p[s])
        v[s] = np.full(len(wide), np.nan)
        dev[s] = np.full(len(wide), np.nan)
        if not has.any():
            continue
        n, q = wide[f"n_{s}"].to_numpy(dtype=float), wide[f"q_{s}"].to_numpy(dtype=float)
        model = fit_noise_model(field_id[has], p[s][has], n[has], q[has])
        if not np.isfinite(model["a"]):  # no field with enough years: fall back to the overall variance
            model.update(a=float(np.var(p[s][has])) if has.sum() > 1 else 1.0)
        info["noise_model"][s] = model
        v[s][has] = noise_variance(model, n[has], q[has])
        dev[s][has] = field_deviation(field_id[has], p[s][has])[0]

    a, b = SENSORS
    both = np.isfinite(p[a]) & np.isfinite(p[b])
    z_ok = both & np.isfinite(dev[a]) & np.isfinite(dev[b])
    rho = 0.0
    if z_ok.sum() >= 30:
        za, zb = dev[a][z_ok] / np.sqrt(v[a][z_ok]), dev[b][z_ok] / np.sqrt(v[b][z_ok])
        if za.std() > 0 and zb.std() > 0:
            rho = float(np.clip(np.corrcoef(za, zb)[0, 1], 0.0, RHO_MAX))
    info["rho"] = rho
    info["n_both"] = int(both.sum())

    w = {s: np.where(np.isfinite(p[s]), 1.0 / v[s], 0.0) for s in SENSORS}
    total = w[a] + w[b]
    with np.errstate(divide="ignore", invalid="ignore"):
        for s in SENSORS:
            w[s] = np.where(total > 0, w[s] / total, np.nan)
    om = np.where(total > 0, np.nan_to_num(p[a]) * np.nan_to_num(w[a]) + np.nan_to_num(p[b]) * np.nan_to_num(w[b]), np.nan)
    v_base = np.where(
        both,
        w[a] ** 2 * v[a] + w[b] ** 2 * v[b] + 2 * w[a] * w[b] * rho * np.sqrt(v[a] * v[b]),
        np.where(np.isfinite(p[a]), v[a], v[b]),
    )

    g = np.ones(len(wide))
    kappa = None
    if both.sum() >= 30:
        delta2 = (p[b][both] - p[a][both]) ** 2
        delta2 = np.minimum(delta2, np.quantile(delta2, D2_CAP_QUANTILE))
        expected = v[a][both] + v[b][both]
        kappa = float(delta2.mean() / expected.mean())
        if kappa > 0:
            g[both] = np.clip((1.0 + (p[b][both] - p[a][both]) ** 2 / (kappa * expected)) / 2.0, G_MIN, G_MAX)
    info["kappa"] = kappa
    info["mean_offset_s2_minus_landsat"] = float((p[b][both] - p[a][both]).mean()) if both.any() else None

    out = pd.DataFrame(index=wide.index)
    out["om_pred"] = om
    out["noise_var"] = np.where(np.isfinite(om), v_base * g, np.nan)
    out["v_base"] = np.where(np.isfinite(om), v_base, np.nan)
    out["agreement"] = g
    for s in SENSORS:
        out[f"w_{s}"] = w[s]
        out[f"v_{s}"] = v[s]
    out["n_sensors"] = np.isfinite(p[a]).astype(int) + np.isfinite(p[b]).astype(int)
    for name, columns in (extra or {}).items():
        num = np.zeros(len(wide))
        den = np.zeros(len(wide))
        for s in SENSORS:
            col = columns.get(s)
            if col is None or col not in wide:
                continue
            x = wide[col].to_numpy(dtype=float)
            ok = np.isfinite(x) & np.isfinite(v[s])
            num[ok] += x[ok] / v[s][ok]
            den[ok] += 1.0 / v[s][ok]
        with np.errstate(divide="ignore", invalid="ignore"):
            out[name] = np.where(den > 0, num / den, np.nan)
    return out, info


def stable_variance(field_id, om_pred, noise_var) -> dict:
    """tau2: variance between fields of the stable part of om_pred (step 4 of the module docstring)."""
    frame = pd.DataFrame({"f": np.asarray(field_id, dtype=object), "p": np.asarray(om_pred, float), "v": np.asarray(noise_var, float)})
    frame = frame[np.isfinite(frame["p"]) & np.isfinite(frame["v"])]
    g = frame.groupby("f").agg(p=("p", "mean"), v=("v", "mean"), k=("p", "size"))
    if len(g) < 2:
        return {"tau2": 0.0, "var_field_means": 0.0, "noise_in_field_means": 0.0, "floored": True}
    var_means = float(g["p"].var())
    left = float((g["v"] / g["k"]).mean())
    tau2 = var_means - left
    floor = TAU2_FLOOR_SHARE * var_means
    return {"tau2": float(max(tau2, floor)), "var_field_means": var_means, "noise_in_field_means": left, "floored": bool(tau2 < floor)}


def confidence(om_pred, noise_var, tau2: float, skill: float) -> np.ndarray:
    """confidence = skill x tau2 / (tau2 + noise_var), in [0, 1]; 0 where om_pred is null."""
    p = np.asarray(om_pred, dtype=float)
    v = np.asarray(noise_var, dtype=float)
    skill = float(np.clip(skill if skill is not None and skill == skill else 0.0, 0.0, 1.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        reliability = np.where(np.isfinite(v) & (tau2 + v > 0), tau2 / (tau2 + v), 0.0)
    return np.where(np.isfinite(p), np.clip(skill * reliability, 0.0, 1.0), 0.0)
