"""Is real soil change detectable in om_pred, or buried in noise?  (analyse() answers; build.py reports.)

The argument, step by step
--------------------------
1. Noise. Topsoil organic matter moves slowly: hundredths of a percentage point a year. On a field
   whose crop did not change from one year to the next, the jump of om_pred between those years is
   therefore almost pure noise (surface moisture, residue, roughness, which dates happened to be
   clear). Its size is reported as a robust standard deviation (1.4826 x median absolute deviation),
   raw and after subtracting the county-wide median of each year, per sensor family and combined,
   and by number of bare views. Fields that grew a single crop through the whole window give the
   same number a second way. om_noise_sd in the meta file is the raw jump SD of the combined om_pred.

2. Repeatability. Landsat and Sentinel-2 look at the same field in the same year with different
   instruments on different days. The spread of (Sentinel-2 om_pred - Landsat om_pred) measures
   what one sensor family's number is worth as a measurement, with no assumption about crops.

3. Signal. What a good rotation change could do (HOPED_PER_DECADE): long-term trials and reviews
   (e.g. West & Post 2002 for no-till and rotation changes; lower in semi-arid wheat-fallow
   country) put the gain from more intensive rotations, no-till or cover crops at roughly
   0.1-0.3 t C/ha/yr. In the top 15 cm (about 1,950 t soil/ha at 1.3 g/cm3) 0.2 t C/ha/yr is
   0.010 % carbon a year = 0.018 % organic matter (x 1.724) = 0.18 percentage points per decade.
   So 0.1-0.3 points per decade, centre 0.2; the top 2-5 cm that a satellite sees can change two to
   three times faster under no-till, which is the most optimistic reading, and losses under
   tillage and fallow are the same size with the other sign.

4. Sensitivity. The model shrinks towards the mean: its predictions move by only `sensitivity`
   (model.metrics) when mapped organic matter moves by 1. A real change X appears as about
   sensitivity x X. Every "in real organic matter" number below divides by it, so three things
   are carried along with it:
   * its 90 % interval from resampling map units (model.sensitivity_group_interval). It rests on
     a few dozen map units, so it is known only roughly, and every figure in real organic matter
     is given as a range over that interval;
   * the sensitivity WITHIN a soil type (model.within_type_sensitivity). The plain figure is
     measured between soils, mostly sand against loam, where brightness and texture do the work.
     A field does not change its soil type, so the figure that matters for change over time is
     the one within a type. Where that is zero, om_pred cannot show change however many fields
     are averaged, and no amount of averaging is a reason to recommend it;
   * the single-field figure in om_pred's own units, which needs no sensitivity at all: if it is
     above the top of the hoped range, a single field is buried even for a perfect model.

5. Minimum detectable change (MDC): the trend, in points per decade, that would be told from zero
   with a 5 % false-alarm rate and an 80 % chance of detection = Z_DETECT (2.80) x standard error
   of the trend. For one field the standard error is measured directly: the robust SD of the
   straight-line trends fitted to every field with at least MIN_TREND_YEARS values (real trends
   are far smaller than this spread, so the spread is the error). That is the typical field. A
   mean of N fields feels the tails as well, so for N > 1 the plain SD of the same trends is
   used (about a quarter larger on the prototype). For the mean of N fields the
   variance is split into what all fields of the county share in a year (does not shrink with N),
   what the fields of one BLOCK_KM neighbourhood share (shrinks with the number of neighbourhoods)
   and a field's own (shrinks with N): variance_components(). N fields are assumed spread over the
   neighbourhoods present in the data. `difference` is the MDC for comparing two such groups in the
   same years, where the county-wide part cancels.

6. Artefacts. om_pred is regressed on that year's crop and the previous year's crop with field
   and year fixed effects (crop_artefacts). Organic matter does not follow this year's crop; if
   om_pred does, the surface is talking. A rotation change is a change of crops, so this shift is
   a bias that does not average out however many fields are pooled. It is compared with the
   signal as it would appear in om_pred (sensitivity x HOPED). The spread between all crops is
   reported, but many noisy estimates always spread, so the headline is the largest single shift
   that is at least CLEAR_T standard errors from zero.

7. Verdict (verdict()): single field / groups of fields, "detectable", "marginal" or "buried", and
   which column to use. om_pred is recommended only if all of these hold with the sensitivity that
   matters for change, taken at its cautious end (the lower bound within a soil type, or the lower
   bound between soils if that is smaller): om_pred follows organic matter within a soil type at
   all; 100 fields can detect the top of the hoped range; and the clear crop artefact is smaller
   than the centre of it. Otherwise bare_days. The contract (health_schema.RECOMMENDED) has only
   these two values, so when bare_days fails its own checks (exposure.passes) it is still named,
   and the verdict text and `trustworthy` say that neither column should be relied on.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from pipeline.soil.exposure import CORE_CROPS, robust_sd
from pipeline.soil.features import SENSORS

HOPED_PER_DECADE = (0.1, 0.3)  # percentage points of organic matter per decade; see docstring step 3
HOPED_CENTRAL = 0.2
Z_DETECT = 1.96 + 0.84  # 5 % two-sided false alarms, 80 % power
MIN_TREND_YEARS = 6
MIN_DEV_YEARS = 3
MIN_CROP_ROWS = 30  # a crop needs this many field-years to get its own artefact estimate
MIN_SENSITIVITY = 0.02  # below this the model does not follow organic matter at all
N_FIELDS = (1, 10, 100, 1000)
N_OBS_BINS = (0, 1, 3, 6, 12, 25, np.inf)
REFERENCE_CROP = "fallow"
CLEAR_T = 3.0  # a crop shift counts as beyond doubt at this many standard errors


# --------------------------------------------------------------------------- crops


def crop_table(fields: pd.DataFrame, years) -> pd.DataFrame:
    """Long table field_id, year, crop, prev_crop for `years`, from the crop_history JSON of fields.parquet.

    crop is missing (None / NaN) where the history has no entry for that year; prev_crop is the crop of year - 1.
    """
    years = [int(y) for y in years]
    ids, yy, crop, prev = [], [], [], []
    for fid, raw in zip(fields["field_id"].to_numpy(dtype=object), fields["crop_history"].to_numpy(dtype=object)):
        history = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
        for y in years:
            ids.append(fid)
            yy.append(y)
            crop.append(history.get(str(y)))
            prev.append(history.get(str(y - 1)))
    return pd.DataFrame({"field_id": ids, "year": np.array(yy, dtype="int64"), "crop": crop, "prev_crop": prev})


def single_crop_fields(crops: pd.DataFrame) -> pd.Series:
    """field_id -> crop, for the fields that grew one and the same crop in every year of `crops`."""
    g = crops.groupby("field_id")["crop"]
    one = (g.nunique(dropna=False) == 1) & (g.count() == g.size())
    return g.first()[one]


# --------------------------------------------------------------------------- jumps


def year_effect(frame: pd.DataFrame, col: str) -> pd.Series:
    """County-wide level of `col` per year: the median over all fields that have a value."""
    return frame[frame[col].notna()].groupby("year")[col].median()


def consecutive_pairs(frame: pd.DataFrame, col: str) -> pd.DataFrame:
    """One row per (field, year) whose previous year also has a value: jump = value(year) - value(year - 1)."""
    now = frame[["field_id", "year", col]].dropna(subset=[col])
    before = now.assign(year=now["year"] + 1).rename(columns={col: "_prev"})
    pairs = now.merge(before, on=["field_id", "year"], how="inner")
    pairs["jump"] = pairs[col] - pairs["_prev"]
    return pairs[["field_id", "year", "jump"]]


def jump_summary(jump) -> dict:
    j = np.asarray(jump, dtype=float)
    j = j[np.isfinite(j)]
    if len(j) < 5:
        return {"n": int(len(j)), "robust_sd": None, "sd": None, "median_abs": None}
    return {"n": int(len(j)), "robust_sd": robust_sd(j), "sd": float(j.std(ddof=1)), "median_abs": float(np.median(np.abs(j)))}


def stable_crop_noise(frame: pd.DataFrame, col: str, crops: pd.DataFrame) -> dict:
    """Year-to-year jump of `col` on pairs of years with the same crop; raw and with the year effect removed."""
    effect = year_effect(frame, col)
    adj = frame[["field_id", "year"]].copy()
    adj[col] = frame[col] - frame["year"].map(effect)
    out = {}
    for name, source in (("raw", frame), ("year_effect_removed", adj)):
        pairs = consecutive_pairs(source, col).merge(crops, on=["field_id", "year"], how="left")
        stable = pairs[pairs["crop"].notna() & (pairs["crop"] == pairs["prev_crop"])]
        out[name] = jump_summary(stable["jump"])
        out[name]["all_pairs"] = jump_summary(pairs["jump"])
        if name == "raw":
            by = {}
            for crop, part in stable.groupby("crop"):
                if len(part) >= MIN_CROP_ROWS:
                    by[str(crop)] = jump_summary(part["jump"])
            out["by_crop"] = by
            # The unchanged-crop pairs are not a fair sample of fields (on the prototype half are
            # corn after corn, the noisiest crop), so the mix and a crop-balanced figure go with it.
            mix = stable["crop"].value_counts()
            out["crop_mix"] = {str(c): float(n / len(stable)) for c, n in mix.items()} if len(stable) else {}
            balanced = [v["robust_sd"] for v in by.values() if v["robust_sd"] is not None]
            out["crop_balanced_robust_sd"] = float(np.mean(balanced)) if balanced else None
    out["year_effect"] = {int(y): float(v) for y, v in effect.items()}
    out["year_effect_sd"] = float(effect.std(ddof=1)) if len(effect) > 1 else None
    return out


def single_crop_noise(frame: pd.DataFrame, col: str, crops: pd.DataFrame) -> dict:
    """Scatter of `col` around the field's own mean on fields in one crop for the whole window."""
    mono = single_crop_fields(crops)
    part = frame[frame["field_id"].isin(mono.index) & frame[col].notna()]
    k = part.groupby("field_id")[col].transform("size")
    part = part[k >= MIN_DEV_YEARS]
    k = k[k >= MIN_DEV_YEARS].to_numpy(dtype=float)
    dev = (part[col] - part.groupby("field_id")[col].transform("mean")).to_numpy(dtype=float) * np.sqrt(k / (k - 1)) if len(part) else np.array([])
    sd = robust_sd(dev)
    return {
        "n_fields": int(part["field_id"].nunique()),
        "n_fields_single_crop": int(len(mono)),
        "crops": {str(c): int(n) for c, n in mono.value_counts().items()},
        "n_values": int(len(dev)),
        "robust_sd_of_value": sd,
        "equivalent_jump_sd": sd * np.sqrt(2) if sd is not None else None,
    }


def jumps_by_n_obs(frame: pd.DataFrame, col: str, n_col: str, crops: pd.DataFrame) -> list[dict]:
    """Stable-crop jump SD by the smaller n_obs of the two years."""
    pairs = consecutive_pairs(frame, col).merge(crops, on=["field_id", "year"], how="left")
    n_now = frame[["field_id", "year", n_col]]
    pairs = pairs.merge(n_now, on=["field_id", "year"], how="left").merge(
        n_now.assign(year=n_now["year"] + 1).rename(columns={n_col: "_n_prev"}), on=["field_id", "year"], how="left"
    )
    stable = pairs[pairs["crop"].notna() & (pairs["crop"] == pairs["prev_crop"])].copy()
    stable["_n"] = np.minimum(stable[n_col], stable["_n_prev"])
    rows = []
    for lo, hi in zip(N_OBS_BINS[:-1], N_OBS_BINS[1:]):
        part = stable[(stable["_n"] >= lo) & (stable["_n"] < hi)]
        s = jump_summary(part["jump"])
        rows.append({"n_obs_from": float(lo), "n_obs_to": None if np.isinf(hi) else float(hi), "n": s["n"], "robust_sd": s["robust_sd"]})
    return rows


# --------------------------------------------------------------------------- repeatability


def sensor_repeatability(wide: pd.DataFrame) -> dict:
    """Landsat-based against Sentinel-2-based om_pred for the same field-year (columns p_landsat, p_s2)."""
    a, b = (wide[f"p_{s}"].to_numpy(dtype=float) for s in SENSORS)
    both = np.isfinite(a) & np.isfinite(b)
    if both.sum() < 10:
        return {"n_pairs": int(both.sum())}
    diff = b[both] - a[both]
    part = wide.loc[both, ["field_id"]].assign(a=a[both], b=b[both])
    k = part.groupby("field_id")["a"].transform("size").to_numpy()
    da = (part["a"] - part.groupby("field_id")["a"].transform("mean")).to_numpy()[k >= MIN_DEV_YEARS]
    db = (part["b"] - part.groupby("field_id")["b"].transform("mean")).to_numpy()[k >= MIN_DEV_YEARS]
    fa, fb = part.groupby("field_id")["a"].mean(), part.groupby("field_id")["b"].mean()
    rsd = robust_sd(diff)
    return {
        "n_pairs": int(both.sum()),
        "r": float(np.corrcoef(a[both], b[both])[0, 1]),
        "bias_s2_minus_landsat": float(diff.mean()),
        "robust_sd_of_difference": rsd,
        "sd_of_difference": float(diff.std(ddof=1)),
        # if the two were independent readings of one truth, each would carry this much noise
        "implied_noise_sd_one_family": rsd / np.sqrt(2) if rsd is not None else None,
        "r_of_field_means": float(np.corrcoef(fa, fb)[0, 1]) if len(fa) > 2 else None,
        # do the two families agree on WHICH year was high for a field? (what they share is the surface)
        "r_of_yearly_deviations": float(np.corrcoef(da, db)[0, 1]) if len(da) > 10 and da.std() > 0 and db.std() > 0 else None,
    }


def sensor_step(table: pd.DataFrame, wide: pd.DataFrame, first_s2_year: int) -> dict:
    """Does the combined series step in the year Sentinel-2 joins?

    Before `first_s2_year` om_pred is Landsat alone; from then on it is pulled towards Sentinel-2.
    All like for like, on field-years that have a Landsat value:
      mean_shift       mean of (om_pred - Landsat-only) from that year on: a county-level step
      field_shift_sd   spread across fields of each field's own mean shift: how far a field's
                       pre-Sentinel-2 points sit from where its later points would put them
      change_same_fields  mean change from the last Landsat-only year to the first joint year, on the
                       fields that have a Landsat value in both, for om_pred and for Landsat alone
    mean_of_available_by_year is the plain county mean of whatever exists; it also moves when
    Sentinel-2 adds fields that Landsat did not see bare (a change of who is counted, not a step).
    """
    land = wide["p_landsat"].to_numpy(dtype=float)
    frame = pd.DataFrame({"field_id": table["field_id"].to_numpy(), "year": table["year"].to_numpy(), "om": table["om_pred"].to_numpy(dtype=float), "land": land})
    later = frame[(frame["year"] >= first_s2_year) & frame["om"].notna() & frame["land"].notna()]
    if len(later) < 10:
        return {"n": int(len(later))}
    shift = later["om"] - later["land"]
    per_field = shift.groupby(later["field_id"]).mean()
    before, after = first_s2_year - 1, first_s2_year
    a = frame[(frame["year"] == before) & frame["land"].notna()]
    b = frame[(frame["year"] == after) & frame["land"].notna()]
    same = a.merge(b, on="field_id", how="inner", suffixes=("_a", "_b"))
    by_year = frame[frame["om"].notna()].groupby("year")["om"].mean()
    return {
        "n": int(len(later)),
        "first_s2_year": int(first_s2_year),
        "mean_shift": float(shift.mean()),
        "field_shift_sd": float(per_field.std(ddof=1)) if len(per_field) > 1 else None,
        "n_same_fields": int(len(same)),
        "combined_change_same_fields": float((same["om_b"] - same["om_a"]).mean()) if len(same) else None,
        "landsat_change_same_fields": float((same["land_b"] - same["land_a"]).mean()) if len(same) else None,
        "mean_of_available_by_year": {int(y): float(v) for y, v in by_year.items()},
    }


# --------------------------------------------------------------------------- variance components and trends


def variance_components(frame: pd.DataFrame, col: str) -> dict:
    """Split the year-to-year scatter of `col` into county-year, block-year and field-year parts.

    `frame` needs field_id, year, block and `col`. Works on deviations from each field's own mean
    (fields with at least MIN_DEV_YEARS values, scaled by sqrt(k / (k - 1))). The year part is the
    variance of the yearly county means less their sampling error; the block-year part comes from
    a one-way analysis of variance of what is left, within (block, year) cells.
    """
    part = frame[frame[col].notna()].copy()
    k = part.groupby("field_id")[col].transform("size")
    part = part[k >= MIN_DEV_YEARS].copy()
    if len(part) < 30:
        return {"n": int(len(part))}
    k = part.groupby("field_id")[col].transform("size").to_numpy(dtype=float)
    part["_d"] = (part[col] - part.groupby("field_id")[col].transform("mean")) * np.sqrt(k / (k - 1))
    by_year = part.groupby("year")["_d"].agg(["mean", "var", "size"])
    var_year = max(float(by_year["mean"].var(ddof=1) - (by_year["var"] / by_year["size"]).mean()), 0.0) if len(by_year) > 1 else 0.0
    part["_r"] = part["_d"] - part["year"].map(by_year["mean"])
    cell = part.groupby(["block", "year"])["_r"]
    size = cell.transform("size").to_numpy(dtype=float)
    centre = cell.transform("mean").to_numpy(dtype=float)
    n, n_cells = len(part), int(cell.ngroups)
    within = float(((part["_r"].to_numpy() - centre) ** 2).sum() / max(n - n_cells, 1))
    sizes = cell.size().to_numpy(dtype=float)
    means = cell.mean().to_numpy(dtype=float)
    between = float((sizes * (means - part["_r"].mean()) ** 2).sum() / max(n_cells - 1, 1))
    n0 = (n - (sizes**2).sum() / n) / max(n_cells - 1, 1)
    var_block = max((between - within) / n0, 0.0) if n0 > 0 else 0.0
    total = var_year + var_block + within
    return {
        "n": int(n),
        "n_blocks": int(part["block"].nunique()),
        "median_fields_per_block_year": float(np.median(sizes)),
        "sd_county_year": float(np.sqrt(var_year)),
        "sd_block_year": float(np.sqrt(var_block)),
        "sd_field_year": float(np.sqrt(within)),
        "share_county_year": var_year / total if total > 0 else None,
        "share_block_year": var_block / total if total > 0 else None,
        "share_field_year": within / total if total > 0 else None,
        "sd_total": float(np.sqrt(total)),
    }


def field_trends(frame: pd.DataFrame, col: str) -> pd.Series:
    """Least-squares trend of `col` per field, in units per decade, for fields with >= MIN_TREND_YEARS values."""
    part = frame[frame[col].notna()][["field_id", "year", col]]
    g = part.groupby("field_id")
    k = g[col].transform("size")
    t = part["year"] - g["year"].transform("mean")
    v = part[col] - g[col].transform("mean")
    sxy = (t * v).groupby(part["field_id"]).sum()
    sxx = (t * t).groupby(part["field_id"]).sum()
    count = k.groupby(part["field_id"]).first()
    slope = 10.0 * sxy / sxx.where(sxx > 0)
    return slope[(count >= MIN_TREND_YEARS) & slope.notna()]


def usable_sensitivity(value) -> bool:
    return value is not None and value == value and value >= MIN_SENSITIVITY


def real_om_range(om_pred_units: float | None, interval) -> list | None:
    """[low, high] in real organic matter of a figure in om_pred units, over the sensitivity interval.

    The high end is None (no upper limit) when the lower bound of the sensitivity is below
    MIN_SENSITIVITY: at that end the model does not follow organic matter at all.
    """
    if om_pred_units is None or not interval or not usable_sensitivity(interval[1]):
        return None
    return [float(om_pred_units / interval[1]), float(om_pred_units / interval[0]) if usable_sensitivity(interval[0]) else None]


def detectability(frame: pd.DataFrame, col: str, sensitivity: float | None, jump_sd: float | None, sensitivity_interval=None) -> dict:
    """Minimum detectable change per decade for one field and for the mean of N fields (docstring steps 4-5).

    One field uses the robust SD of the field trends (the typical field); means of N > 1 fields use
    their plain SD, because a mean feels the tails. `sensitivity_interval` ([low, high], 90 %) is
    carried into every figure in real organic matter as `..._range`.
    """
    slopes = field_trends(frame, col)
    se_one = robust_sd(slopes) if len(slopes) >= 10 else None
    sd_plain = float(slopes.std(ddof=1)) if len(slopes) >= 10 else None
    comp = variance_components(frame, col)
    years = sorted(frame["year"].unique())
    t = np.array(years, dtype=float)
    sxx = float(((t - t.mean()) ** 2).sum())
    analytic = 10.0 * (jump_sd / np.sqrt(2)) / np.sqrt(sxx) if jump_sd and sxx > 0 else None
    usable_b = usable_sensitivity(sensitivity)
    out = {
        "n_fields_with_trend": int(len(slopes)),
        "min_years_for_trend": MIN_TREND_YEARS,
        "trend_se_one_field": se_one,
        "trend_sd_plain": sd_plain,  # used for the mean of N > 1 fields
        "trend_se_one_field_from_jump_sd": analytic,  # complete series, independent years
        "median_trend": float(slopes.median()) if len(slopes) else None,
        "z_detect": Z_DETECT,
        "sensitivity": sensitivity,
        "sensitivity_interval90": list(sensitivity_interval) if sensitivity_interval else None,
        "variance_components": comp,
        "hoped_per_decade": list(HOPED_PER_DECADE),
        "hoped_in_om_pred_units": [h * sensitivity for h in HOPED_PER_DECADE] if usable_b else None,
        "by_n_fields": [],
    }
    se_base = se_one if se_one is not None else analytic
    if se_base is None or "share_field_year" not in comp or comp["share_field_year"] is None:
        return out
    se_group = max(sd_plain, se_base) if sd_plain is not None else se_base
    # needs no sensitivity: even a model that showed every real change in full would have to see this much
    out["mdc_one_field_om_pred_units"] = float(Z_DETECT * se_base)
    out["single_field_buried_at_any_sensitivity"] = bool(Z_DETECT * se_base > HOPED_PER_DECADE[1])
    n_blocks = max(comp["n_blocks"], 1)
    for n in N_FIELDS:
        blocks = min(n, n_blocks)
        base = se_base if n == 1 else se_group
        shared = comp["share_county_year"]
        local = comp["share_block_year"] / blocks + comp["share_field_year"] / n
        se = base * np.sqrt(shared + local)
        se_diff = base * np.sqrt(2.0 * local)
        row = {
            "n_fields": n,
            "trend_se": float(se),
            "mdc_om_pred": float(Z_DETECT * se),
            "mdc_om_pred_difference": float(Z_DETECT * se_diff),
            "mdc_real_om": float(Z_DETECT * se / sensitivity) if usable_b else None,
            "mdc_real_om_difference": float(Z_DETECT * se_diff / sensitivity) if usable_b else None,
            "mdc_real_om_range": real_om_range(float(Z_DETECT * se), sensitivity_interval),
            "mdc_real_om_difference_range": real_om_range(float(Z_DETECT * se_diff), sensitivity_interval),
        }
        out["by_n_fields"].append(row)
    floor = se_group * np.sqrt(comp["share_county_year"])
    out["floor_real_om"] = float(Z_DETECT * floor / sensitivity) if usable_b else None
    return out


# --------------------------------------------------------------------------- crop artefacts


def _demean_two_way(values: np.ndarray, a: np.ndarray, b: np.ndarray, tol: float = 1e-10, max_iter: int = 500) -> np.ndarray:
    """Remove the two sets of group means (alternating projections; exact for a balanced panel in one pass)."""
    x = values.astype(float).copy()
    na, nb = int(a.max()) + 1, int(b.max()) + 1
    ca, cb = np.bincount(a, minlength=na).astype(float), np.bincount(b, minlength=nb).astype(float)
    for _ in range(max_iter):
        before = x.copy()
        for col in range(x.shape[1]):
            x[:, col] -= (np.bincount(a, weights=x[:, col], minlength=na) / ca)[a]
            x[:, col] -= (np.bincount(b, weights=x[:, col], minlength=nb) / cb)[b]
        if np.abs(x - before).max() < tol:
            break
    return x


def crop_artefacts(frame: pd.DataFrame, col: str, crops: pd.DataFrame, sensitivity: float | None = None, sensitivity_interval=None) -> dict:
    """How far `col` shifts with this year's and last year's crop, with field and year fixed effects.

        col[f, t] = field_f + year_t + this[crop(f, t)] + last[crop(f, t - 1)] + error

    Shifts are relative to REFERENCE_CROP (fallow, or the commonest crop if there is no fallow).
    Crops with fewer than MIN_CROP_ROWS field-years are pooled as "other". Standard errors are
    clustered by field. `range` is the largest minus the smallest shift (the reference counts as 0).

    If every field follows one fixed rotation, last year's crop is known from this year's and the
    two sets of shifts cannot be told apart. Then only this year's crop is estimated (it carries
    both), `last_year` is empty and `last_year_identified` is False.
    """
    j = frame[["field_id", "year", col]].merge(crops, on=["field_id", "year"], how="left")
    j = j[j[col].notna() & j["crop"].notna() & j["prev_crop"].notna()].copy()
    j = j[j.groupby("field_id")[col].transform("size") >= 2]
    if len(j) < 100:
        return {"n": int(len(j))}

    def levels(series: pd.Series) -> tuple[pd.Series, list[str]]:
        counts = series.value_counts()
        keep = [c for c in counts.index if counts[c] >= MIN_CROP_ROWS]
        s = series.where(series.isin(keep), "other")
        ref = REFERENCE_CROP if REFERENCE_CROP in keep else keep[0] if keep else "other"
        names = [c for c in sorted(s.unique(), key=lambda c: (c not in CORE_CROPS, c)) if c != ref]
        return s, [ref, *names]

    this, this_levels = levels(j["crop"])
    last, last_levels = levels(j["prev_crop"])
    cols = [(this == c).to_numpy(dtype=float) for c in this_levels[1:]] + [(last == c).to_numpy(dtype=float) for c in last_levels[1:]]
    if not cols:
        return {"n": int(len(j))}
    X = np.column_stack(cols)
    f_code = pd.factorize(j["field_id"])[0]
    y_code = pd.factorize(j["year"])[0]
    Z = _demean_two_way(np.column_stack([j[col].to_numpy(dtype=float), X]), f_code, y_code)
    yd, Xd = Z[:, 0], Z[:, 1:]
    n_this = len(this_levels) - 1
    identified = np.linalg.matrix_rank(Xd, tol=1e-6 * np.sqrt(len(yd))) == Xd.shape[1]
    if not identified:  # this year's and last year's crop are tied together: keep this year's only
        Xd = Xd[:, :n_this]
        last_levels = last_levels[:1]
        if np.linalg.matrix_rank(Xd, tol=1e-6 * np.sqrt(len(yd))) < Xd.shape[1]:
            return {"n": int(len(j)), "last_year_identified": False}
    beta, *_ = np.linalg.lstsq(Xd, yd, rcond=None)
    resid = yd - Xd @ beta
    bread = np.linalg.pinv(Xd.T @ Xd)
    score = Xd * resid[:, None]
    n_f = int(f_code.max()) + 1
    per_field = np.zeros((n_f, Xd.shape[1]))
    np.add.at(per_field, f_code, score)
    cov = bread @ (per_field.T @ per_field) @ bread * (n_f / max(n_f - 1, 1))
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))

    out = {"n": int(len(j)), "n_fields": n_f, "reference_this_year": this_levels[0], "reference_last_year": last_levels[0], "last_year_identified": bool(identified)}
    for key, names, coef, err, series in (
        ("this_year", this_levels, beta[:n_this], se[:n_this], this),
        ("last_year", last_levels, beta[n_this:], se[n_this:], last),
    ):
        counts = series.value_counts()
        table = {names[0]: {"shift": 0.0, "se": 0.0, "n": int(counts[names[0]])}}
        for name, c, e in zip(names[1:], coef, err):
            table[name] = {"shift": float(c), "se": float(e), "n": int(counts[name])}
        shifts = [v["shift"] for v in table.values()]
        out[key] = table
        out[f"range_{key}"] = float(max(shifts) - min(shifts))
        out[f"clear_{key}"] = int(sum(abs(v["shift"]) > 2 * v["se"] for k, v in table.items() if v["se"] > 0))
    out["largest_range"] = max(out["range_this_year"], out["range_last_year"])
    # The range of many noisy estimates overstates. The headline is the largest single shift that is
    # beyond doubt: at least CLEAR_T standard errors from zero (14 or so are tested at once).
    clear = [
        {"crop": name, "when": key, **v}
        for key in ("this_year", "last_year")
        for name, v in out[key].items()
        if v["se"] > 0 and abs(v["shift"]) >= CLEAR_T * v["se"]
    ]
    best = max(clear, key=lambda v: abs(v["shift"])) if clear else None
    out["n_clear_shifts"] = len(clear)
    out["largest_clear_shift"] = best
    usable_b = usable_sensitivity(sensitivity)
    out["clear_shift_as_real_om_range"] = real_om_range(abs(best["shift"]) if best else None, sensitivity_interval)
    out["signal_in_om_pred_units_per_decade"] = HOPED_CENTRAL * sensitivity if usable_b else None
    out["largest_range_as_real_om"] = out["largest_range"] / sensitivity if usable_b else None
    out["range_over_decade_signal"] = out["largest_range"] / (HOPED_CENTRAL * sensitivity) if usable_b else None
    out["clear_shift_as_real_om"] = abs(best["shift"]) / sensitivity if usable_b and best else None
    out["clear_shift_over_decade_signal"] = abs(best["shift"]) / (HOPED_CENTRAL * sensitivity) if usable_b and best else None
    return out


# --------------------------------------------------------------------------- verdict


def _grade(mdc: float | None) -> str:
    if mdc is None:
        return "buried"
    if mdc <= HOPED_CENTRAL:
        return "detectable"
    if mdc <= HOPED_PER_DECADE[1]:
        return "marginal"
    return "buried"


BARE_FAILURE_TEXT = {
    "sensors_agree": "the two satellites disagree on it",
    "crop_order": "it does not rank the crops as it should",
    "fallow_stands_out": "a fallow year does not stand out from a cropped year",
    "follows_green_cover": "it does not fall when green cover rises",
}


def change_sensitivity(noise: dict) -> float | None:
    """The cautious sensitivity for change within a field: the smallest of the lower bounds we have.

    Between soils: the lower end of its interval (the point value if there is no interval). Within a
    soil type: likewise. None when nothing is known about the sensitivity within a soil type; a
    value below MIN_SENSITIVITY means "does not follow organic matter".
    """
    det = noise["detectability"]
    within = noise.get("within_type") or {}
    between = det.get("sensitivity_interval90")
    between_low = between[0] if between else det.get("sensitivity")
    inner = within.get("sensitivity_interval90")
    inner_low = inner[0] if inner else within.get("sensitivity")
    if inner_low is None or inner_low != inner_low or between_low is None or between_low != between_low:
        return None
    return float(min(between_low, inner_low))


def verdict(noise: dict, bare: dict | None = None) -> dict:
    """Plain-language verdict from the numbers of analyse(): grades, the column to use, and two sentences.

    Grades compare the minimum detectable real change per decade (at the measured between-soil
    sensitivity) with HOPED_PER_DECADE: "detectable" up to its centre, "marginal" up to its top,
    "buried" beyond. artefact_rivals_signal: the largest crop shift that is beyond doubt is worth at
    least the centre of the hoped range in real organic matter.

    om_pred is recommended only if, at the cautious sensitivity for change (change_sensitivity):
    it follows organic matter within a soil type at all, 100 fields reach the top of the hoped
    range, and the artefact does not rival the signal. A between-soil sensitivity that happens to
    come out higher on another set of map units cannot flip it on its own.

    `bare`: exposure.passes() of the same build. If bare_days fails one of its own checks the text
    says so and `trustworthy["bare_days"]` is False; `recommended` still names it, because the
    contract has no third value.
    """
    det = noise["detectability"]
    by_n = {row["n_fields"]: row for row in det.get("by_n_fields", [])}
    mdc = {n: by_n.get(n, {}).get("mdc_real_om") for n in N_FIELDS}
    art = noise.get("artefacts", {})
    art_real = art.get("clear_shift_as_real_om")
    rivals = art_real is not None and art_real >= HOPED_CENTRAL
    grades = {n: _grade(mdc.get(n)) for n in N_FIELDS}

    cautious = change_sensitivity(noise)
    within = noise.get("within_type") or {}
    follows_within = None if cautious is None else bool(usable_sensitivity(cautious))
    mdc_100_units = by_n.get(100, {}).get("mdc_om_pred")
    mdc_100_cautious = mdc_100_units / cautious if follows_within and mdc_100_units is not None else None
    best = art.get("largest_clear_shift")
    art_cautious = abs(best["shift"]) / cautious if follows_within and best else None
    rivals_cautious = bool(best) and (art_cautious is None or art_cautious >= HOPED_CENTRAL)
    ok_100 = mdc_100_cautious is not None and mdc_100_cautious <= HOPED_PER_DECADE[1]
    om_ok = bool(follows_within and ok_100 and not rivals_cautious)
    recommended = "om_pred" if om_ok else "bare_days"

    bare = bare or {}
    failed = [name for name in bare.get("failed", [])]
    bare_ok = bare.get("all_pass")  # True, False, or None when nothing could be checked

    jump = noise.get("om_noise_sd")
    lo, hi = HOPED_PER_DECADE
    jump_txt = f"about {jump:.2f} points" if jump is not None else "an unknown amount"
    sensitivity = det.get("sensitivity")
    no_skill = not usable_sensitivity(sensitivity)
    if no_skill:  # a model that predicts the same for every soil is steady and says nothing
        first = (
            "The satellite organic-matter estimate does not follow the mapped organic matter of soils it was not trained on, "
            "so it cannot show soil change, on one field or on many."
        )
    elif grades[1] == "buried":
        first = (
            f"On fields where nothing changed, the satellite organic-matter estimate still jumps by {jump_txt} from one year to the next, "
            f"while a good rotation is expected to add only {lo:g} to {hi:g} points in ten years, so on a single field real change is buried in noise."
        )
    elif grades[1] == "marginal":
        first = (
            f"The satellite organic-matter estimate jumps by {jump_txt} from year to year on unchanged fields, "
            f"about as much as a good rotation adds in ten years ({lo:g} to {hi:g} points), so a single field shows change only doubtfully."
        )
    else:
        first = (
            f"The satellite organic-matter estimate moves by only {jump_txt} from year to year on unchanged fields, "
            f"small enough to see the {lo:g} to {hi:g} points a good rotation adds in ten years on a single field."
        )
    what = "bare days (how long the soil sits uncovered each year)"
    if bare_ok is False:
        why = " and ".join(BARE_FAILURE_TEXT.get(name, name) for name in failed) or "it fails its own checks"
        use_bare = f"{what} is the fallback, but in this build {why}, so do not rely on either number"
    else:
        use_bare = f"compare rotations by {what} instead, which measures how exposed the soil is, not what the soil has become"
    reasons = []
    if follows_within is False:
        shown = usable_sensitivity(within.get("sensitivity")) and (within.get("spearman") or 0.0) > 0
        how = "cannot be shown to follow" if shown else "does not follow"  # point value above zero, interval not
        reasons.append(f"within one kind of soil the estimate {how} organic matter (it mostly tells sand from loam)")
    if rivals or (follows_within and rivals_cautious):
        reasons.append("it shifts with whatever crop residue lies on the surface")
    if no_skill:
        second = f"Do not use it; {use_bare}."
    elif recommended == "om_pred":
        second = f"Averaged over a hundred or more fields it is steady enough to compare rotations; {what} is the simpler check, of how exposed the soil is."
    elif reasons:
        second = f"Averaging many fields does not fix this, because {' and '.join(reasons)}; {use_bare}."
    elif grades[1000] == "buried":
        second = f"Even the average of a thousand fields is too unsteady; {use_bare}."
    else:
        second = f"Only averages over many hundreds of fields come close; {use_bare}."
    return {
        "single_field": grades[1],
        "ten_fields": grades[10],
        "hundred_fields": grades[100],
        "thousand_fields": grades[1000],
        "single_field_buried_at_any_sensitivity": det.get("single_field_buried_at_any_sensitivity"),
        "artefact_rivals_signal": bool(rivals),
        "model_has_skill": not no_skill,
        "sensitivity_for_change": cautious,
        "follows_organic_matter_within_soil_type": follows_within,
        "within_type_spearman": within.get("spearman"),
        "hundred_fields_mdc_at_cautious_sensitivity": mdc_100_cautious,
        "artefact_rivals_signal_at_cautious_sensitivity": bool(rivals_cautious),
        "bare_days_checks_pass": bare_ok,
        "bare_days_failed_checks": failed,
        "trustworthy": {"om_pred": om_ok, "bare_days": bare_ok},
        "recommended": recommended,
        "text": f"{first} {second}",
    }


# --------------------------------------------------------------------------- everything


def analyse(
    table: pd.DataFrame,
    wide: pd.DataFrame,
    crops: pd.DataFrame,
    sensitivity: float | None,
    first_s2_year: int | None = None,
    sensitivity_interval=None,
    within_type: dict | None = None,
    bare: dict | None = None,
) -> dict:
    """All of the above for one build.

    `table`: field_id, year, om_pred, n_obs, block (one row per field-year). `wide`: the same rows
    with p_landsat, p_s2, n_landsat, n_s2. `crops`: crop_table(). `sensitivity`: the field-level
    sensitivity of om_pred under map-unit-grouped validation; `sensitivity_interval`: its 90 %
    interval from resampling map units; `within_type`: model.within_type_sensitivity() of the same
    predictions; `bare`: exposure.passes() (docstring steps 4 and 7).
    """
    frame = table[["field_id", "year", "om_pred", "n_obs", "block"]].copy()
    for s in SENSORS:
        frame[f"p_{s}"] = wide[f"p_{s}"].to_numpy(dtype=float)
        frame[f"n_{s}"] = wide[f"n_{s}"].to_numpy(dtype=float)
    out: dict = {"jumps": {}, "single_crop": {}, "by_n_obs": {}}
    for name, col, n_col in (("combined", "om_pred", "n_obs"), ("landsat", "p_landsat", "n_landsat"), ("s2", "p_s2", "n_s2")):
        if frame[col].notna().sum() < 10:
            continue
        out["jumps"][name] = stable_crop_noise(frame, col, crops)
        out["single_crop"][name] = single_crop_noise(frame, col, crops)
        out["by_n_obs"][name] = jumps_by_n_obs(frame, col, n_col, crops)
    combined = out["jumps"].get("combined", {})
    out["om_noise_sd"] = combined.get("raw", {}).get("robust_sd")
    out["om_noise_sd_year_effect_removed"] = combined.get("year_effect_removed", {}).get("robust_sd")
    out["noise_sd_of_one_value"] = out["om_noise_sd"] / np.sqrt(2) if out["om_noise_sd"] is not None else None
    out["repeatability"] = sensor_repeatability(wide)
    if first_s2_year is not None:
        out["sensor_step"] = sensor_step(table, wide, first_s2_year)
    out["detectability"] = detectability(frame, "om_pred", sensitivity, out["om_noise_sd"], sensitivity_interval)
    out["artefacts"] = crop_artefacts(frame, "om_pred", crops, sensitivity, sensitivity_interval)
    out["within_type"] = within_type
    out["hoped_per_decade"] = list(HOPED_PER_DECADE)
    out["hoped_central"] = HOPED_CENTRAL
    out["verdict"] = verdict(out, bare)
    return out
