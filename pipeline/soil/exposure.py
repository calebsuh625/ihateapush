"""bare_days: one "days the soil sat bare and exposed" value per field-year from the two sensor families.

pipeline/soil/bare_soil.py gives a bare_days per (field, year, sensor family). This module merges
them (combine_bare_days) and checks that the result behaves like soil exposure (checks).

Combination rule
----------------
* Sentinel-2 is first put on Landsat's scale: landsat = A + B x sentinel2, fitted on the field-years
  where both families have a value (identity when there are fewer than MIN_PAIRS). The line is the
  reduced major axis, B = sd(landsat) / sd(sentinel2), not least squares: both sides are noisy, and
  least squares would pull every high value down. Landsat is the anchor because 2016 has Landsat
  only; without this the series would step by the bias between the sensors when Sentinel-2 joins.
* Both families have a value: their mean weighted by n_clear, the clear views per pixel that year.
  bare_days is a sum of monthly shares "bare views / clear views", so the family that looked more
  often has the less noisy share.
* One family has a value: that value.  Neither: null (too few clear views to tell).
* A table built without the per-view snow screen (its Parquet metadata says snow_screen = false):
  a Sentinel-2 composite that fails the snow test (features.usable) had mostly snow counted as
  bare soil, so that row's Sentinel-2 bare_days is not used when Landsat has a value.
* The result is clipped to 0-366.

What it is and is not
---------------------
bare_days counts days with dry, residue-free soil showing (NDVI and NBR2 both low). A wet bare
field does not count that day, and neither does a field under stubble. It measures what the
rotation does to the soil's cover, which is a cause of soil change (erosion, crusting, loss of
organic matter), not the soil's condition itself.

How much one value is worth (checks(), passes())
------------------------------------------------
The year-to-year jump on fields whose crop did not change is given per crop and as both a robust
and a plain SD, because the pooled figure flatters: alfalfa and grass sit at zero every year and
pull it down, while the crops whose soil is ever bare (fallow, corn, wheat) jump two to three
times as much. `separation` sets the gap between two crops against the noise of comparing one
year of each on the same field, sqrt((jump_a^2 + jump_b^2) / 2), and gives the number of
field-years per group that a comparison needs (plain SDs, 5 % false alarms, 80 % power, same
years). The expected picture: one field-year tells fallow from a cropped year and nothing finer;
rotations have to be compared on multi-year means over many fields within the same years.

passes() turns the checks into yes / no / not tested, so that the verdict can say when the
fallback itself fails: the two sensor families agree (r >= SENSOR_R_MIN), fallow is the barest
core crop and above alfalfa, a fallow year stands at least FALLOW_MIN_SDS comparison-noise SDs
above a cropped year, and bare_days falls when green cover rises (r <= COVER_R_MAX).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_PAIRS = 50
MIN_CROP_PAIRS = 20  # unchanged-crop pairs a crop needs for a jump figure of its own
SENSOR_R_MIN = 0.9
FALLOW_MIN_SDS = 2.0
COVER_R_MAX = -0.3
Z_DETECT = 1.96 + 0.84  # 5 % two-sided false alarms, 80 % power (as in noise.py)
CORE_CROPS = ("corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow")


def robust_sd(values) -> float | None:
    """1.4826 x median absolute deviation: the standard deviation of the bulk, deaf to outliers."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 2:
        return None
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def fit_scale(landsat, s2) -> dict:
    """landsat = a + b x s2 (reduced major axis) on rows where both are present.

    Identity below MIN_PAIRS, when either side is constant, when the two do not move together
    (r < 0.5) or when the slope is outside 0.5-2.
    """
    x = np.asarray(s2, dtype=float)
    y = np.asarray(landsat, dtype=float)
    keep = np.isfinite(x) & np.isfinite(y)
    n = int(keep.sum())
    identity = {"a": 0.0, "b": 1.0, "n_pairs": n, "fitted": False}
    if n < MIN_PAIRS or x[keep].std() == 0 or y[keep].std() == 0:
        return identity
    if np.corrcoef(x[keep], y[keep])[0, 1] < 0.5:
        return identity
    b = float(y[keep].std() / x[keep].std())
    a = float(y[keep].mean() - b * x[keep].mean())
    if not (0.5 <= b <= 2.0):
        return identity
    return {"a": a, "b": b, "n_pairs": n, "fitted": True}


def combine_bare_days(wide: pd.DataFrame, snow_screened: bool = True) -> tuple[np.ndarray, dict]:
    """One bare_days per row of `wide`.

    `wide`: one row per (field, year) with bd_landsat, bd_s2 (bare_days, NaN when null), nc_landsat,
    nc_s2 (n_clear) and, when `snow_screened` is False, snowy_s2 (True where the Sentinel-2 composite
    failed the snow test). Returns (values, info).
    """
    land = wide["bd_landsat"].to_numpy(dtype=float)
    s2 = wide["bd_s2"].to_numpy(dtype=float).copy()
    dropped = 0
    if not snow_screened and "snowy_s2" in wide:
        drop = wide["snowy_s2"].to_numpy(dtype=bool) & np.isfinite(land) & np.isfinite(s2)
        dropped = int(drop.sum())
        s2[drop] = np.nan
    scale = fit_scale(land, s2)
    s2_adj = np.clip(scale["a"] + scale["b"] * s2, 0.0, 366.0)
    w_l = np.where(np.isfinite(land), np.maximum(np.nan_to_num(wide["nc_landsat"].to_numpy(dtype=float)), 1e-6), 0.0)
    w_s = np.where(np.isfinite(s2_adj), np.maximum(np.nan_to_num(wide["nc_s2"].to_numpy(dtype=float)), 1e-6), 0.0)
    total = w_l + w_s
    with np.errstate(divide="ignore", invalid="ignore"):
        value = np.where(total > 0, (np.nan_to_num(land) * w_l + np.nan_to_num(s2_adj) * w_s) / total, np.nan)
    value = np.clip(value, 0.0, 366.0)
    info = {
        "s2_to_landsat": scale,
        "s2_rows_dropped_for_snow": dropped,
        "snow_screened_input": bool(snow_screened),
        "rows_both": int((np.isfinite(land) & np.isfinite(s2_adj)).sum()),
        "rows_landsat_only": int((np.isfinite(land) & ~np.isfinite(s2_adj)).sum()),
        "rows_s2_only": int((~np.isfinite(land) & np.isfinite(s2_adj)).sum()),
        "rows_null": int((~np.isfinite(value)).sum()),
    }
    return value, info


def _corr(a, b) -> float | None:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    keep = np.isfinite(a) & np.isfinite(b)
    if keep.sum() < 3 or a[keep].std() == 0 or b[keep].std() == 0:
        return None
    return float(np.corrcoef(a[keep], b[keep])[0, 1])


def _within(frame: pd.DataFrame, col: str) -> np.ndarray:
    return (frame[col] - frame.groupby("field_id")[col].transform("mean")).to_numpy(dtype=float)


def checks(table: pd.DataFrame, wide: pd.DataFrame, crops: pd.DataFrame | None, cover: pd.DataFrame | None) -> dict:
    """Does bare_days behave like soil exposure?

    `table`: field_id, year, bare_days (combined). `wide`: bd_landsat, bd_s2 on the same rows.
    `crops`: field_id, year, crop, prev_crop (noise.crop_table) or None. `cover`: field_id, year,
    cover_frac (data/field_satellite.parquet) or None.

    by_crop        bare_days by that year's crop: fallow should be high, alfalfa low
    sensors        Landsat against Sentinel-2 on the same field-year: its measurement noise
    cover          against green cover: more green cover must mean fewer bare days
    year_effect    county-wide median per year: bare_days needs dry soil, so a dry year lifts the
                   fields that have bare periods (not a uniform shift)
    stable_crop    year-to-year jump on fields whose crop did not change (robust and plain SD,
                   pooled and per crop), to set against by_crop
    separation     gap between each pair of core crops against that noise (module docstring)
    fallow_vs_cropped  the same for fallow against the other core crops taken together
    """
    out: dict = {}
    land, s2 = wide["bd_landsat"].to_numpy(dtype=float), wide["bd_s2"].to_numpy(dtype=float)
    both = np.isfinite(land) & np.isfinite(s2)
    diff = s2[both] - land[both]
    out["sensors"] = {
        "n_pairs": int(both.sum()),
        "r": _corr(land, s2),
        "bias_s2_minus_landsat": float(diff.mean()) if both.any() else None,
        "robust_sd_of_difference": robust_sd(diff),
        "median_abs_difference": float(np.median(np.abs(diff))) if both.any() else None,
        # two independent readings differ by sqrt(2) x the noise of one; the weighted mean of two is better still
        "noise_sd_one_sensor": (robust_sd(diff) or 0.0) / np.sqrt(2) if both.sum() > 1 else None,
    }
    by_year = table[table["bare_days"].notna()].groupby("year")["bare_days"].median()
    out["year_effect"] = {
        "median_by_year": {int(y): float(v) for y, v in by_year.items()},
        "sd": float(by_year.std(ddof=1)) if len(by_year) > 1 else None,
    }
    if crops is not None and len(crops):
        j = table.merge(crops, on=["field_id", "year"], how="left")
        j = j[j["bare_days"].notna() & j["crop"].notna()]
        by = j.groupby("crop")["bare_days"].agg(n="size", median="median", mean="mean")
        by = by[by["n"] >= 20].sort_values("median", ascending=False)
        out["by_crop"] = {str(c): {"n": int(r["n"]), "median": float(r["median"]), "mean": float(r["mean"])} for c, r in by.iterrows()}
        med = {c: v["median"] for c, v in out["by_crop"].items()}
        out["crop_order_ok"] = (
            bool(med["fallow"] > med["alfalfa"]) and bool(med["fallow"] == max(med[c] for c in med if c in CORE_CROPS))
            if "fallow" in med and "alfalfa" in med
            else None
        )
        same = j[(j["crop"] == j["prev_crop"])].merge(
            table.assign(year=table["year"] + 1).rename(columns={"bare_days": "bare_prev"})[["field_id", "year", "bare_prev"]],
            on=["field_id", "year"],
            how="inner",
        )
        same = same.assign(jump=same["bare_days"] - same["bare_prev"])
        same = same[np.isfinite(same["jump"])]
        jump = same["jump"].to_numpy(dtype=float)
        per_crop = {}
        for crop, part in same.groupby("crop"):
            if len(part) >= MIN_CROP_PAIRS:
                values = part["jump"].to_numpy(dtype=float)
                per_crop[str(crop)] = {"n_pairs": int(len(values)), "robust_sd_of_jump": robust_sd(values), "sd_of_jump": float(values.std(ddof=1))}
        # No "year effect removed" figure here on purpose: a dry year adds bare days where there is
        # bare soil to dry and none under alfalfa or irrigated corn, so subtracting one county-wide
        # number per year makes the unchanged fields look noisier, not steadier.
        out["stable_crop"] = {
            "n_pairs": int(len(jump)),
            "robust_sd_of_jump": robust_sd(jump),
            "sd_of_jump": float(jump.std(ddof=1)) if len(jump) > 1 else None,
            "median_abs_jump": float(np.median(np.abs(jump))) if len(jump) else None,
            "by_crop": per_crop,
            "crop_mix": {str(c): float(n / len(same)) for c, n in same["crop"].value_counts().items()} if len(same) else {},
        }
        pooled = {"robust_sd_of_jump": robust_sd(jump), "sd_of_jump": float(jump.std(ddof=1)) if len(jump) > 1 else None}

        def compare(high: str, gap: float, a: dict, b: dict) -> dict:
            """Gap between two crops against the noise of one year of each on the same field."""
            typical = _pair_noise(a["robust_sd_of_jump"], b["robust_sd_of_jump"])
            plain = _pair_noise(a["sd_of_jump"], b["sd_of_jump"])
            return {
                "high": high,
                "gap_days": float(gap),
                "noise_sd_of_one_comparison": typical,
                "gap_in_noise_sds": float(gap / typical) if typical else None,
                # field-years in each group for the gap to be told from zero (plain SDs: a mean feels the tails)
                "field_years_needed_per_group": int(np.ceil((Z_DETECT * plain / gap) ** 2)) if plain and gap > 0 else None,
            }

        core = [c for c in med if c in CORE_CROPS and c in per_crop]  # crops with a jump figure of their own
        out["separation"] = [
            {"low": low, **compare(high, med[high] - med[low], per_crop.get(high, pooled), per_crop.get(low, pooled))}
            for k, high in enumerate(core)
            for low in core[k + 1 :]
        ]
        if "fallow" in med and len(med) > 1:
            others = [c for c in med if c in CORE_CROPS and c != "fallow"]
            out["fallow_minus_other_core_median"] = float(med["fallow"] - np.median([med[c] for c in others])) if others else None
            if others:
                # the other crops taken at their noisiest, so the comparison is not flattered by alfalfa
                noisiest = max((per_crop.get(c, pooled) for c in others), key=lambda v: v["robust_sd_of_jump"] or 0.0)
                out["fallow_vs_cropped"] = compare("fallow", out["fallow_minus_other_core_median"], per_crop.get("fallow", pooled), noisiest)
    if cover is not None and len(cover):
        j = table.merge(cover[["field_id", "year", "cover_frac"]], on=["field_id", "year"], how="inner")
        j = j[j["bare_days"].notna() & j["cover_frac"].notna()]
        jw = wide.assign(field_id=table["field_id"].to_numpy(), year=table["year"].to_numpy()).merge(
            cover[["field_id", "year", "cover_frac"]], on=["field_id", "year"], how="inner"
        )
        out["cover"] = {
            "n": int(len(j)),
            "r": _corr(j["bare_days"], j["cover_frac"]),
            "r_within_field": _corr(_within(j, "bare_days"), _within(j, "cover_frac")) if len(j) else None,
            # cover_frac is Landsat's; the Sentinel-2 value shares no image with it, so this is the independent check
            "r_s2_only": _corr(jw["bd_s2"], jw["cover_frac"]),
            "r_landsat_only": _corr(jw["bd_landsat"], jw["cover_frac"]),
        }
    return out


def _pair_noise(jump_a: float | None, jump_b: float | None) -> float | None:
    """SD of (one year of crop a - one year of crop b) on a field, from the two year-to-year jump SDs.

    A jump is the difference of two years, so one year carries jump / sqrt(2).
    """
    if not jump_a or not jump_b:
        return None
    return float(np.sqrt((jump_a**2 + jump_b**2) / 2.0))


def passes(checks_out: dict) -> dict:
    """Which of its own checks bare_days passes: True, False, or None where a check could not be run.

    all_pass is False if any check fails, True if none fails and at least one ran, None if none ran.
    """
    sensors = checks_out.get("sensors", {})
    r = sensors.get("r")
    cover = (checks_out.get("cover") or {}).get("r")
    ratio = (checks_out.get("fallow_vs_cropped") or {}).get("gap_in_noise_sds")
    result = {
        "sensors_agree": bool(r >= SENSOR_R_MIN) if r is not None and sensors.get("n_pairs", 0) >= MIN_PAIRS else None,
        "crop_order": checks_out.get("crop_order_ok"),
        "fallow_stands_out": bool(ratio >= FALLOW_MIN_SDS) if ratio is not None else None,
        "follows_green_cover": bool(cover <= COVER_R_MAX) if cover is not None else None,
    }
    failed = [name for name, ok in result.items() if ok is False]
    untested = [name for name, ok in result.items() if ok is None]
    ran = len(result) - len(untested)
    return {**result, "failed": failed, "untested": untested, "all_pass": False if failed else (True if ran else None)}
