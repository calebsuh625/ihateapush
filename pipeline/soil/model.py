"""The organic-matter model: bare-soil reflectance -> SSURGO organic matter, with honest validation.

What is predicted, and what "accuracy" can mean here
----------------------------------------------------
The label is `organic_matter_pct` of data/fields.parquet: SSURGO's om_r of the surface horizon,
area-weighted over the map units under a field. om_r is the soil survey's *representative value*
for a soil series, not a measurement of the field. Every field on one map unit carries (almost)
the same number, it does not change from year to year, and it does not know what the farmer did.
So accuracy against it says how well a spectrum tells which kind of soil a field sits on (in
Finney County mostly: dune sand at 0.7 % against silt loam at 1.5-2.0 %). It cannot say how well
the model follows organic matter that changes over time; nothing in this repo can.

Why the folds are grouped
-------------------------
One field gives up to nine rows a family with the same label, and all fields of a map unit share
it. With rows of one field or one map unit on both sides of a split, a flexible model only has to
recognise the place to be "right". So the headline number comes from GroupKFold grouped by the
field's dominant map unit (pipeline.soil.ssurgo.field_mapunits): every prediction is made by a
model that saw no field of that map unit. Reported next to it, for contrast:

    random      plain K-fold over rows                       the number that lies
    field       grouped by field                             still leaks through the map unit
    block       grouped by a BLOCK_KM grid of field centres  spatial neighbours kept together
    map_unit    grouped by dominant map unit                 the headline; om_pred comes from here
    series      grouped by soil series                       stricter: "Richfield silt loam, 0 to 1
                                                             percent slopes" and "Richfield-Spearville
                                                             complex" are two map units of one series
                                                             and no longer sit on both sides of a split

The honest accuracy is the range between `series` and `map_unit`. Under the map-unit split a
sister map unit of the same series, with nearly the same label and spectra, is in the training
set. On the 300 prototype fields that is where all of the skill comes from: grouped by series the
model is no better than the training mean (R2 -0.2 for both), against 0.14 grouped by map unit.

A field's minor map units may still be in the training set (its label is an area-weighted mix);
`pure_fields` in the report repeats the headline on fields that are at least 90 % one map unit.

Models
------
* HistGradientBoostingRegressor on FEATURES (bands + indices), one model per sensor family. The
  headline scheme picks one of HGB_CONFIGS inside each training fold by an inner GroupKFold on map
  units (never on the held-out fold). The contrast schemes use DEFAULT_CONFIG, and so does the
  `map_unit_fixed` row, which is the like-for-like partner of `random`.
* Baseline "mean": the training fold's mean label for every row.
* Baseline "ridge": standardised ridge regression on the six bands; alpha chosen on the training fold.

Metrics (metrics()): R2 (1 - SSE / SST around the mean of the evaluated labels, all folds pooled),
RMSE, MAE, Spearman rank correlation, and two slopes that are easy to confuse:

    calibration_slope   least-squares slope of label on prediction (1 = calibrated)
    sensitivity         least-squares slope of prediction on label: how far the prediction moves
                        when the label moves by 1. A model that shrinks towards the mean has a
                        sensitivity well below 1, and a real difference X shows up as about
                        sensitivity x X. noise.py uses it to translate "detectable in om_pred"
                        into "detectable in real organic matter".

Row level = one composite; field level = a field's predictions averaged over its years.

field_median_check() is a diagnostic, not part of the table: the same validation on one multi-year
median spectrum per field. It answers "would averaging the years first make a better model?".
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from pipeline.soil.features import BANDS, FEATURES

SEED = 20055
N_SPLITS = 10
INNER_SPLITS = 3
INNER_MAX_ROWS = 20_000  # the inner (tuning) folds use at most this many training rows
BLOCK_KM = 7.5
PURE_SHARE = 0.9
BOOTSTRAP = 2000
# Threads for gradient boosting. On tables of this size (thousands to tens of thousands of rows)
# two threads are 3-5 times faster than all cores, which mostly wait on each other.
HGB_THREADS = 2
MIN_YEARS_FOR_MEDIAN = 3

HGB_CONFIGS = {
    "small": dict(learning_rate=0.05, max_iter=100, max_leaf_nodes=6, min_samples_leaf=40, l2_regularization=1.0),
    "medium": dict(learning_rate=0.05, max_iter=200, max_leaf_nodes=15, min_samples_leaf=20, l2_regularization=1.0),
    "large": dict(learning_rate=0.05, max_iter=300, max_leaf_nodes=31, min_samples_leaf=20, l2_regularization=0.1),
}
DEFAULT_CONFIG = "medium"
RIDGE_ALPHAS = tuple(float(a) for a in np.logspace(-3, 3, 13))

SCHEMES = {
    "map_unit": "grouped by dominant soil map unit, model size chosen inside each training fold (HEADLINE)",
    "map_unit_fixed": "grouped by dominant soil map unit, fixed model size (like-for-like with the rows below)",
    "series": "grouped by soil series: map units of one series share a fold (stricter than the headline)",
    "block": f"grouped by {BLOCK_KM:g} km spatial blocks",
    "field": "grouped by field (leaks through the map unit)",
    "random": "plain random K-fold over rows (leaks; the number that lies)",
}
MODELS = ("hgb", "ridge", "mean")


class ModelError(ValueError):
    pass


# --------------------------------------------------------------------------- metrics


def metrics(y, pred) -> dict:
    """R2, RMSE, MAE, Spearman, calibration slope and sensitivity of `pred` against `y` (see module docstring)."""
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    keep = np.isfinite(y) & np.isfinite(pred)
    y, pred = y[keep], pred[keep]
    n = int(len(y))
    out = {"n": n}
    if n < 3:
        return {**out, "r2": None, "rmse": None, "mae": None, "spearman": None, "calibration_slope": None, "sensitivity": None, "bias": None}
    err = pred - y
    sst = float(((y - y.mean()) ** 2).sum())
    var_p = float(((pred - pred.mean()) ** 2).sum())
    cov = float(((y - y.mean()) * (pred - pred.mean())).sum())
    out["r2"] = 1.0 - float((err**2).sum()) / sst if sst > 0 else None
    out["rmse"] = float(np.sqrt((err**2).mean()))
    out["mae"] = float(np.abs(err).mean())
    out["spearman"] = _spearman(y, pred)
    out["calibration_slope"] = cov / var_p if var_p > 0 else None
    out["sensitivity"] = cov / sst if sst > 0 else None
    out["bias"] = float(err.mean())
    return out


def _spearman(a, b) -> float | None:
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    sa, sb = ra.std(), rb.std()
    if sa == 0 or sb == 0:
        return None
    return float(((ra - ra.mean()) * (rb - rb.mean())).mean() / (sa * sb))


def field_means(field_id, y, pred) -> tuple[np.ndarray, np.ndarray]:
    """Per field: its label and its predictions averaged over years (rows with a prediction only)."""
    frame = pd.DataFrame({"f": np.asarray(field_id, dtype=object), "y": np.asarray(y, float), "p": np.asarray(pred, float)})
    frame = frame[np.isfinite(frame["p"])]
    g = frame.groupby("f", sort=True).agg(y=("y", "first"), p=("p", "mean"))
    return g["y"].to_numpy(), g["p"].to_numpy()


def r2_group_interval(y, pred, groups, n_boot: int = BOOTSTRAP, seed: int = SEED, level: float = 0.90) -> list[float] | None:
    """Bootstrap interval of the pooled R2, resampling whole groups (map units), not rows.

    Rows of one map unit share a label, so they are one piece of evidence; resampling rows would
    pretend there are thousands. With a few dozen map units the interval is wide, as it should be.
    """
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    keep = np.isfinite(y) & np.isfinite(pred)
    if keep.sum() < 3:
        return None
    frame = pd.DataFrame({"g": np.asarray(groups, dtype=object)[keep], "n": 1.0, "y": y[keep], "yy": y[keep] ** 2, "se": (pred[keep] - y[keep]) ** 2})
    sums = frame.groupby("g", sort=True)[["n", "y", "yy", "se"]].sum().to_numpy()
    k = len(sums)
    if k < 3:
        return None
    rng = np.random.default_rng(seed)
    counts = rng.multinomial(k, np.full(k, 1.0 / k), size=n_boot).astype(float)
    tot = counts @ sums  # n, sum y, sum y^2, sse
    with np.errstate(divide="ignore", invalid="ignore"):
        sst = tot[:, 2] - tot[:, 1] ** 2 / tot[:, 0]
        r2 = 1.0 - tot[:, 3] / sst
    r2 = r2[np.isfinite(r2)]
    if len(r2) < n_boot // 2:
        return None
    lo, hi = np.quantile(r2, [(1 - level) / 2, 1 - (1 - level) / 2])
    return [float(lo), float(hi)]


def sensitivity_group_interval(y, pred, groups, n_boot: int | None = None, seed: int = SEED, level: float = 0.90) -> list[float] | None:
    """Bootstrap interval of the sensitivity (slope of prediction on label), resampling whole groups.

    The sensitivity is what turns "detectable in om_pred" into "detectable in real organic matter",
    and it rests on a few dozen map units, a handful of them sandy. Resampling map units shows how
    far it can move when another county's worth of map units is drawn.
    """
    n_boot = BOOTSTRAP if n_boot is None else n_boot
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    keep = np.isfinite(y) & np.isfinite(pred)
    if keep.sum() < 3:
        return None
    frame = pd.DataFrame(
        {"g": np.asarray(groups, dtype=object)[keep], "n": 1.0, "y": y[keep], "p": pred[keep], "yy": y[keep] ** 2, "yp": y[keep] * pred[keep]}
    )
    frame = frame[frame["g"].notna()]
    sums = frame.groupby("g", sort=True)[["n", "y", "p", "yy", "yp"]].sum().to_numpy()
    k = len(sums)
    if k < 3:
        return None
    rng = np.random.default_rng(seed)
    counts = rng.multinomial(k, np.full(k, 1.0 / k), size=n_boot).astype(float)
    tot = counts @ sums
    with np.errstate(divide="ignore", invalid="ignore"):
        sst = tot[:, 3] - tot[:, 1] ** 2 / tot[:, 0]
        slope = (tot[:, 4] - tot[:, 1] * tot[:, 2] / tot[:, 0]) / sst
    slope = slope[np.isfinite(slope) & (sst > 1e-12)]
    if len(slope) < n_boot // 2:
        return None
    lo, hi = np.quantile(slope, [(1 - level) / 2, 1 - (1 - level) / 2])
    return [float(lo), float(hi)]


def within_type_sensitivity(y, pred, kind, groups=None) -> dict:
    """Sensitivity and rank correlation after removing the mean of each soil type (`kind`) from label and prediction.

    One row per field in. The plain sensitivity is mostly "sand is bright, loam is dark". A change
    of organic matter in one field happens within one soil type, so this is the number that says
    whether om_pred could follow it. The interval resamples map units (`groups`).
    """
    y = np.asarray(y, dtype=float)
    pred = np.asarray(pred, dtype=float)
    kind = np.asarray(kind, dtype=object)
    keep = np.isfinite(y) & np.isfinite(pred)
    out = {"n_fields": int(keep.sum()), "sensitivity": None, "spearman": None, "sensitivity_interval90": None}
    if keep.sum() < 10:
        return out
    frame = pd.DataFrame({"k": kind[keep], "y": y[keep], "p": pred[keep]})
    dy = (frame["y"] - frame.groupby("k")["y"].transform("mean")).to_numpy()
    dp = (frame["p"] - frame.groupby("k")["p"].transform("mean")).to_numpy()
    if dy.std() == 0:
        return out
    out["sensitivity"] = float((dy * dp).sum() / (dy**2).sum())
    out["spearman"] = _spearman(dy, dp)
    if groups is not None:
        out["sensitivity_interval90"] = sensitivity_group_interval(dy, dp, np.asarray(groups, dtype=object)[keep])
    return out


# --------------------------------------------------------------------------- folds


def spatial_blocks(x_m, y_m, size_km: float = BLOCK_KM) -> np.ndarray:
    """A label per point: the cell of a `size_km` grid (metres in, any projected CRS) it falls in."""
    size = size_km * 1000.0
    ix = np.floor(np.asarray(x_m, dtype=float) / size).astype(np.int64)
    iy = np.floor(np.asarray(y_m, dtype=float) / size).astype(np.int64)
    return np.array([f"{a}_{b}" for a, b in zip(ix, iy)], dtype=object)


def group_folds(groups, n_splits: int | None = None) -> np.ndarray:
    """Fold number per row such that every group lies in exactly one fold (-1 where the group is missing).

    GroupKFold: deterministic, folds balanced by row count. With fewer groups than `n_splits`
    (default N_SPLITS) it becomes leave-one-group-out.
    """
    from sklearn.model_selection import GroupKFold

    n_splits = N_SPLITS if n_splits is None else n_splits

    groups = np.asarray(groups, dtype=object)
    known = np.array([g is not None and g == g for g in groups], dtype=bool)
    fold = np.full(len(groups), -1, dtype=np.int64)
    n_groups = len(set(groups[known]))
    if n_groups < 2:
        raise ModelError(f"need at least 2 groups to hold one out, got {n_groups}")
    idx = np.flatnonzero(known)
    splitter = GroupKFold(n_splits=min(n_splits, n_groups))
    for k, (_, test) in enumerate(splitter.split(idx, groups=groups[idx])):
        fold[idx[test]] = k
    return fold


def random_folds(n_rows: int, n_splits: int | None = None, seed: int = SEED) -> np.ndarray:
    n_splits = N_SPLITS if n_splits is None else n_splits
    fold = np.empty(n_rows, dtype=np.int64)
    order = np.random.default_rng(seed).permutation(n_rows)
    fold[order] = np.arange(n_rows) % max(2, min(n_splits, n_rows))
    return fold


# --------------------------------------------------------------------------- fitting


def _thread_limit():
    """Context manager: at most HGB_THREADS OpenMP threads.

    scikit-learn is imported first on purpose: threadpoolctl can only limit a thread pool that is
    already loaded, and gradient boosting brings its OpenMP runtime with it.
    """
    import sklearn.ensemble  # noqa: F401
    from threadpoolctl import threadpool_limits

    return threadpool_limits(limits=HGB_THREADS, user_api="openmp")


def fit_hgb(X, y, config: str | dict = DEFAULT_CONFIG, seed: int = SEED):
    from sklearn.ensemble import HistGradientBoostingRegressor

    params = HGB_CONFIGS[config] if isinstance(config, str) else config
    # early_stopping stays off: its validation split is random over rows, which is the leak again
    return HistGradientBoostingRegressor(**params, early_stopping=False, random_state=seed).fit(X, y)


def fit_ridge(X, y):
    from sklearn.linear_model import RidgeCV
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(StandardScaler(), RidgeCV(alphas=RIDGE_ALPHAS)).fit(X, y)


def choose_config(X, y, groups, seed: int = SEED) -> tuple[str, dict]:
    """The HGB_CONFIGS entry with the lowest RMSE in an inner GroupKFold on `groups` (training rows only)."""
    groups = np.asarray(groups, dtype=object)
    if len(set(groups)) < 2:
        return DEFAULT_CONFIG, {}
    if len(y) > INNER_MAX_ROWS:  # thin the rows, keep every group
        pick = np.sort(np.random.default_rng(seed).choice(len(y), INNER_MAX_ROWS, replace=False))
        X, y, groups = X[pick], y[pick], groups[pick]
    fold = group_folds(groups, INNER_SPLITS)
    scores = {}
    for name in HGB_CONFIGS:
        sse = 0.0
        for k in range(fold.max() + 1):
            test = fold == k
            sse += float(((fit_hgb(X[~test], y[~test], name, seed).predict(X[test]) - y[test]) ** 2).sum())
        scores[name] = float(np.sqrt(sse / len(y)))
    best = min(scores, key=lambda name: (round(scores[name], 6), list(HGB_CONFIGS).index(name)))
    return best, scores


def oof_predict(n_rows: int, fold, fit_predict) -> np.ndarray:
    """Out-of-fold predictions: row i is predicted by fit_predict(train_rows, test_rows) with i in test_rows only.

    `fold` is a fold number per row; rows with fold -1 (no label or no group) are never trained on
    and are predicted by a model fitted on every row that has a fold. `fit_predict(train, test)`
    gets two index arrays and returns the predictions for `test`.
    """
    fold = np.asarray(fold)
    pred = np.full(n_rows, np.nan)
    in_cv = fold >= 0
    for k in sorted(set(fold[in_cv].tolist())):
        test = np.flatnonzero(fold == k)
        train = np.flatnonzero(in_cv & (fold != k))
        pred[test] = fit_predict(train, test)
    rest = np.flatnonzero(~in_cv)
    if len(rest):
        pred[rest] = fit_predict(np.flatnonzero(in_cv), rest)
    return pred


@dataclass
class FamilyResult:
    """Out-of-fold predictions of one sensor family under every scheme, and the tuning that was done."""

    sensor: str
    frame: pd.DataFrame  # field_id, year, om (label), mukey, block, dominant_share + FEATURES
    preds: dict = field(default_factory=dict)  # scheme -> {model -> array aligned with frame}
    configs: list = field(default_factory=list)  # headline scheme: the config chosen in each outer fold
    folds: dict = field(default_factory=dict)  # scheme -> fold number per row


def train_family(frame: pd.DataFrame, sensor: str, schemes=tuple(SCHEMES), say=lambda msg: None, seed: int = SEED) -> FamilyResult:
    """Cross-validate one sensor family.

    `frame`: usable composites of one family, one row per (field, year), with FEATURES, `om` (label,
    may be NaN), `mukey` (dominant map unit, may be None), `block` and `field_id`. Rows without a
    label or map unit are in no fold: they are predicted by a model trained on all the others.
    """
    frame = frame.reset_index(drop=True)
    X = frame[list(FEATURES)].to_numpy(dtype=float)
    Xb = frame[list(BANDS)].to_numpy(dtype=float)
    y = frame["om"].to_numpy(dtype=float)
    labelled = np.isfinite(y)
    mukey = frame["mukey"].to_numpy(dtype=object)
    mukey = np.where(labelled, mukey, None)
    if len(set(mukey[[m is not None for m in mukey]])) < 2:
        raise ModelError(f"{sensor}: need labelled fields on at least 2 soil map units to train and validate")
    result = FamilyResult(sensor=sensor, frame=frame)

    def folds_for(scheme: str) -> np.ndarray:
        if scheme in ("map_unit", "map_unit_fixed"):
            return group_folds(mukey)
        if scheme == "series":
            return group_folds(np.where(labelled, frame["series"].to_numpy(dtype=object), None))
        if scheme == "block":
            return group_folds(np.where(labelled, frame["block"].to_numpy(dtype=object), None))
        if scheme == "field":
            return group_folds(np.where(labelled, frame["field_id"].to_numpy(dtype=object), None))
        if scheme == "random":
            fold = np.full(len(y), -1, dtype=np.int64)
            fold[labelled] = random_folds(int(labelled.sum()), seed=seed)
            return fold
        raise ModelError(f"unknown scheme {scheme!r}")

    def ridge(train, test):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return fit_ridge(Xb[train], y[train]).predict(Xb[test])

    def mean(train, test):
        return np.full(len(test), y[train].mean())

    def run(scheme: str) -> None:
        fold = folds_for(scheme)

        def hgb(train, test):
            config = DEFAULT_CONFIG
            if scheme == "map_unit":  # the headline: model size chosen on the training rows only
                config, scores = choose_config(X[train], y[train], mukey[train], seed)
                result.configs.append(config)
                say(f"  {sensor} {scheme}: inner folds chose {config} ({', '.join(f'{k} {v:.3f}' for k, v in scores.items())})")
            return fit_hgb(X[train], y[train], config, seed).predict(X[test])

        result.folds[scheme] = fold
        result.preds[scheme] = {
            "hgb": oof_predict(len(y), fold, hgb),
            "ridge": oof_predict(len(y), fold, ridge),
            "mean": oof_predict(len(y), fold, mean),
        }
        m = metrics(y, result.preds[scheme]["hgb"])
        say(f"  {sensor} {scheme}: {int(fold.max()) + 1} folds, HGB row R2 {_f(m['r2'])} RMSE {_f(m['rmse'])}")

    series = frame["series"].to_numpy(dtype=object) if "series" in frame else np.array([], dtype=object)
    n_series = len({v for v, ok in zip(series, labelled) if ok and isinstance(v, str)})
    with _thread_limit():
        for scheme in schemes:
            if scheme == "series" and n_series < 2:
                say(f"  {sensor} series: skipped (the soil series of the map units is not known)")
                continue
            run(scheme)
    return result


def field_median_check(frame: pd.DataFrame, seed: int = SEED) -> dict:
    """Would averaging the years first help? A diagnostic, not used for the table.

    Each field's composites are reduced to one spectrum (the median of each band over its years,
    fields with at least MIN_YEARS_FOR_MEDIAN composites), the features are recomputed from it, and
    the same models are validated with the same map-unit folds (model size again chosen inside the
    training folds). If this were much better than the yearly model, the weakness would be yearly
    noise; if it is not, the reflectance simply says little about mapped organic matter here.
    """
    from pipeline.soil.features import add_features

    grouped = frame.groupby("field_id", sort=True)
    spectra = grouped[list(BANDS)].median()
    spectra["om"] = grouped["om"].first()
    spectra["mukey"] = grouped["mukey"].first()
    spectra = spectra[(grouped.size() >= MIN_YEARS_FOR_MEDIAN) & spectra["om"].notna() & spectra["mukey"].notna()].reset_index()
    if len(spectra) < 30 or spectra["mukey"].nunique() < 3:
        return {"n_fields": int(len(spectra))}
    X = add_features(spectra).to_numpy(dtype=float)
    Xb = spectra[list(BANDS)].to_numpy(dtype=float)
    y = spectra["om"].to_numpy(dtype=float)
    mukey = spectra["mukey"].to_numpy(dtype=object)
    fold = group_folds(mukey)

    def hgb(train, test):
        config, _ = choose_config(X[train], y[train], mukey[train], seed)
        return fit_hgb(X[train], y[train], config, seed).predict(X[test])

    def ridge(train, test):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return fit_ridge(Xb[train], y[train]).predict(Xb[test])

    with _thread_limit():
        out = {"n_fields": int(len(spectra)), "hgb": metrics(y, oof_predict(len(y), fold, hgb)), "ridge": metrics(y, oof_predict(len(y), fold, ridge))}
    out["band_correlation_with_label"] = {b: float(np.corrcoef(spectra[b], y)[0, 1]) for b in BANDS}
    return out


def _f(value, digits: int = 3) -> str:
    return "n/a" if value is None or value != value else f"{value:.{digits}f}"


# --------------------------------------------------------------------------- reporting


def evaluate(field_id, y, pred, groups=None) -> dict:
    """Row-level and field-level metrics of one set of predictions (+ a map-unit bootstrap interval of the row R2)."""
    fy, fp = field_means(field_id, y, pred)
    out = {"row": metrics(y, pred), "field": metrics(fy, fp)}
    if groups is not None:
        out["row"]["r2_interval90"] = r2_group_interval(y, pred, groups)
        keep = np.isfinite(np.asarray(pred, dtype=float))
        first = pd.DataFrame({"f": np.asarray(field_id, dtype=object)[keep], "g": np.asarray(groups, dtype=object)[keep]}).drop_duplicates("f").sort_values("f")
        field_groups = first["g"].to_numpy(dtype=object)
        out["field"]["r2_interval90"] = r2_group_interval(fy, fp, field_groups)
        out["field"]["sensitivity_interval90"] = sensitivity_group_interval(fy, fp, field_groups)
    return out


def label_summary(fields: pd.DataFrame) -> dict:
    """How many map units and label values there are and how lopsided the groups are (one row per field in)."""
    f = fields[fields["om"].notna() & fields["mukey"].notna()]
    sizes = f["mukey"].value_counts()
    om = f["om"].to_numpy(dtype=float)
    within = om - f.groupby("mukey")["om"].transform("mean").to_numpy()
    total_var = float(om.var()) if len(om) else float("nan")
    return {
        "n_fields": int(len(f)),
        "n_map_units": int(len(sizes)),
        "n_label_values": int(f["om"].round(3).nunique()),
        "largest_map_unit_share": float(sizes.iloc[0] / len(f)) if len(f) else None,
        "largest_map_units": [[str(k), int(v)] for k, v in sizes.head(5).items()],
        "map_units_with_one_field": int((sizes == 1).sum()),
        "map_units_with_under_5_fields": int((sizes < 5).sum()),
        "effective_map_units": float(len(f) ** 2 / (sizes.to_numpy(dtype=float) ** 2).sum()) if len(f) else None,
        "label_mean": float(om.mean()) if len(om) else None,
        "label_sd": float(om.std()) if len(om) else None,
        "label_min": float(om.min()) if len(om) else None,
        "label_max": float(om.max()) if len(om) else None,
        "label_variance_between_map_units": float(1 - within.var() / total_var) if total_var and total_var > 0 else None,
        "median_dominant_share": float(f["dominant_share"].median()) if "dominant_share" in f else None,
    }
