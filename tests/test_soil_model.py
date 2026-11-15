"""Tests for the yearly soil table: pipeline/soil/{features,model,confidence,exposure,noise,build}.py
and ssurgo.field_mapunits. Offline, synthetic data only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from shapely.geometry import box  # noqa: E402

from pipeline.soil import bare_soil, build, exposure, features, health_schema, model, noise, ssurgo  # noqa: E402
from pipeline.soil import confidence as conf  # noqa: E402

YEARS = list(health_schema.YEARS)
FAST = {
    "small": dict(learning_rate=0.2, max_iter=25, max_leaf_nodes=4, min_samples_leaf=5, l2_regularization=1.0),
    "medium": dict(learning_rate=0.2, max_iter=40, max_leaf_nodes=8, min_samples_leaf=5, l2_regularization=1.0),
}


@pytest.fixture(autouse=True)
def fast_models(monkeypatch):
    """Small boosting models and 4 folds: the tests check the plumbing, not the accuracy."""
    monkeypatch.setattr(model, "HGB_CONFIGS", FAST)
    monkeypatch.setattr(model, "N_SPLITS", 4)
    monkeypatch.setattr(model, "BOOTSTRAP", 200)


# --------------------------------------------------------------------------- synthetic data


def spectrum(darkness, rng=None, noise_sd=0.0):
    """Six bands of a plausible bare soil: darker (more organic matter) = lower everywhere."""
    base = np.array([0.10, 0.14, 0.19, 0.27, 0.38, 0.33])
    out = base * (1.0 - 0.5 * np.asarray(darkness)[..., None])
    if rng is not None and noise_sd:
        out = out + rng.normal(0, noise_sd, out.shape)
    return np.clip(out, 0.01, 0.99)


def family_frame(n_units=12, fields_per_unit=6, years=6, seed=0, label="unit_noise", row_noise=0.004, field_jitter=0.02):
    """Usable composites of one sensor family with FEATURES, om, mukey, block, field_id, year.

    As in SSURGO country, the fields of one map unit look alike: every map unit has its own
    spectral signature, a field differs from it by `field_jitter` and a year by `row_noise`.
    label="unit_noise": om is a random number per map unit, unrelated to the spectra.
    label="signal": om follows the darkness of the map unit's spectrum.
    """
    rng = np.random.default_rng(seed)
    rows = []
    unit_om = rng.uniform(0.6, 3.0, n_units)
    for u in range(n_units):
        darkness = rng.uniform(0, 1)
        unit_signature = spectrum(darkness) * rng.uniform(0.85, 1.15, 6)
        om = unit_om[u] if label == "unit_noise" else 0.7 + 2.0 * darkness
        for f in range(fields_per_unit):
            signature = unit_signature * (1 + rng.normal(0, field_jitter, 6)) if field_jitter else unit_signature
            for y in range(years):
                bands = np.clip(signature + rng.normal(0, row_noise, 6), 0.01, 0.99) if row_noise else signature
                rows.append([f"KS-{u * 100 + f + 1:06d}", 2016 + y, f"mu{u}", f"b{u % 4}", om, *bands])
    frame = pd.DataFrame(rows, columns=["field_id", "year", "mukey", "block", "om", *features.BANDS])
    return frame[["field_id", "year", "mukey", "block", "om"]].join(features.add_features(frame))


def make_fields(n_units=8, fields_per_unit=6, seed=1):
    """A valid fields GeoDataFrame, its map units, and each field's darkness."""
    rng = np.random.default_rng(seed)
    ids, geoms, om, hist, mukeys, dark = [], [], [], [], [], []
    rotations = [["corn"], ["winter wheat", "fallow"], ["sorghum", "fallow", "winter wheat"], ["alfalfa"], ["corn", "sorghum"], ["fallow"]]
    k = 0
    for u in range(n_units):
        unit_dark = (u + 0.5) / n_units
        for f in range(fields_per_unit):
            k += 1
            x0, y0 = -101.0 + 0.012 * (k % 12), 37.9 + 0.012 * (k // 12)
            ids.append(f"KS-{k:06d}")
            geoms.append(box(x0, y0, x0 + 0.004, y0 + 0.004))
            d = float(np.clip(unit_dark + rng.normal(0, 0.03), 0, 1))
            dark.append(d)
            om.append(round(0.7 + 2.0 * unit_dark + rng.normal(0, 0.02), 3))
            rot = rotations[k % len(rotations)]
            hist.append(json.dumps({str(y): rot[(y + k) % len(rot)] for y in range(2008, 2025)}))
            mukeys.append(f"{1000 + u}")
    fields = gpd.GeoDataFrame(
        {"field_id": ids, "crop_history": hist, "organic_matter_pct": om, "slope_pct": 0.5, "area_ha": 16.0}, geometry=geoms, crs="EPSG:4326"
    )[["field_id", "geometry", "crop_history", "organic_matter_pct", "slope_pct", "area_ha"]]
    mapunits = pd.DataFrame(
        {"dominant_mukey": mukeys, "dominant_share": 0.95, "dominant_series": [f"Series{(int(m) - 1000) // 2}" for m in mukeys]},
        index=pd.Index(ids, name="field_id"),
    )  # two sister map units per soil series
    return fields, mapunits, dict(zip(ids, dark))


def make_obs(fields, darkness, seed=2, noise_sd=0.01, sensors=("landsat", "s2")):
    """A bare-soil table shaped like bare_soil.py's output: some rows without a bare view, some snow."""
    rng = np.random.default_rng(seed)
    histories = {fid: json.loads(h) for fid, h in zip(fields["field_id"], fields["crop_history"])}
    rows = []
    for fid in fields["field_id"]:
        for sensor in sensors:
            for year in YEARS:
                if sensor == "s2" and year < 2017:
                    continue
                crop = histories[fid][str(year)]
                days = {"fallow": 140, "corn": 60, "sorghum": 45, "winter wheat": 30, "alfalfa": 3}[crop] + rng.normal(0, 8)
                n_clear = rng.uniform(20, 30) if sensor == "landsat" else rng.uniform(50, 70)
                row = dict(field_id=fid, year=year, sensor=sensor, n_px=40, n_clear=n_clear, n_bare=0.0, bare_px_frac=0.0,
                           bare_days=float(np.clip(days, 0, 366)), clear_months=12)  # fmt: skip
                bands = dict.fromkeys(features.BANDS, np.nan)
                kind = rng.choice(["bare", "none", "snow"], p=[0.8, 0.15, 0.05])
                if crop == "alfalfa" and rng.random() < 0.5:
                    kind = "none"
                if kind != "none":
                    row["n_bare"] = float(rng.uniform(0.5, 20))
                    row["bare_px_frac"] = float(rng.uniform(0.5, 1.0))
                    values = [0.8, 0.8, 0.8, 0.7, 0.1, 0.08] if kind == "snow" else spectrum(darkness[fid], rng, noise_sd) + (0.02 if sensor == "s2" else 0.0)
                    bands = dict(zip(features.BANDS, values))
                rows.append({**row, **bands})
    obs = pd.DataFrame(rows)
    obs.loc[obs.sample(5, random_state=seed).index, "bare_days"] = np.nan
    return obs


def write_inputs(tmp_path, **kwargs):
    fields, mapunits, darkness = make_fields(**kwargs)
    obs = make_obs(fields, darkness)
    fields_path, obs_path = tmp_path / "fields.parquet", tmp_path / "obs.parquet"
    fields.to_parquet(fields_path)
    obs.to_parquet(obs_path, index=False)
    return fields, mapunits, obs, fields_path, obs_path


def run_build(tmp_path, mapunits, fields_path, obs_path, **kwargs):
    paths = dict(out_path=tmp_path / "health.parquet", meta_path=tmp_path / "meta.json", report_path=tmp_path / "report.md")
    meta = build.build(obs_path, fields_path, **paths, mapunits=mapunits, satellite_path=None, log_path=tmp_path / "build.log", **kwargs)
    return meta, paths


# --------------------------------------------------------------------------- features


def test_snow_limit_is_the_one_bare_soil_uses():
    assert features.NDSI_MAX == bare_soil.NDSI_MAX
    assert features.SENSORS == bare_soil.SENSORS


def test_indices_follow_their_formulas():
    row = pd.DataFrame([dict(zip(features.BANDS, [0.10, 0.15, 0.20, 0.30, 0.40, 0.35]))])
    f = features.add_features(row).iloc[0]
    assert list(f.index) == list(features.FEATURES)
    assert f["nbr2"] == pytest.approx((0.40 - 0.35) / 0.75)
    assert f["ndvi"] == pytest.approx((0.30 - 0.20) / 0.50)
    assert f["bsi"] == pytest.approx(((0.40 + 0.20) - (0.30 + 0.10)) / ((0.40 + 0.20) + (0.30 + 0.10)))
    assert f["brightness"] == pytest.approx(np.sqrt(np.mean(np.square([0.10, 0.15, 0.20, 0.30, 0.40, 0.35]))))
    assert f["swir_ratio"] == pytest.approx(0.35 / 0.40)
    assert f["redness"] == pytest.approx(0.05 / 0.35)
    assert not any(name in features.FEATURES for name in ("lon", "lat", "x", "y", "mukey", "om"))


def test_usable_needs_a_bare_view_valid_bands_and_no_snow():
    good = dict(field_id="KS-000001", year=2020, sensor="landsat", n_px=10, n_clear=20.0, n_bare=3.0, bare_px_frac=1.0, bare_days=50.0,
                **dict(zip(features.BANDS, [0.10, 0.15, 0.20, 0.30, 0.40, 0.35])))  # fmt: skip
    rows = [
        good,
        {**good, "year": 2021, "n_bare": 0.0, **dict.fromkeys(features.BANDS, np.nan)},  # never seen bare
        {**good, "year": 2022, "green": 0.8, "swir1": 0.1},  # snow: NDSI far above -0.25
        {**good, "year": 2023, "swir2": 1.4},  # out of range
        {**good, "year": 2024, "n_px": 0},  # field too thin
    ]
    obs = pd.DataFrame(rows)
    assert features.usable(obs).tolist() == [True, False, False, False, False]
    prepared = features.prepare(obs)
    assert prepared.loc[~prepared["usable"], list(features.FEATURES)].isna().all().all()  # an unusable composite cannot leak in
    assert prepared.loc[prepared["usable"], list(features.FEATURES)].notna().all().all()


def test_a_table_that_is_not_bare_soil_output_is_refused():
    with pytest.raises(features.ObsError, match="no column"):
        features.prepare(pd.DataFrame({"field_id": ["KS-000001"], "year": [2020]}))


# --------------------------------------------------------------------------- model: metrics and folds


def test_sensitivity_and_calibration_slope_are_different_things():
    rng = np.random.default_rng(0)
    y = rng.normal(1.5, 0.5, 4000)
    shrunk = 1.5 + 0.3 * (y - 1.5)  # a model that moves 0.3 for every 1 of truth
    m = model.metrics(y, shrunk)
    assert m["sensitivity"] == pytest.approx(0.3, abs=1e-6)
    assert m["calibration_slope"] == pytest.approx(1 / 0.3, rel=1e-6)
    assert m["spearman"] == pytest.approx(1.0)
    assert m["r2"] == pytest.approx(1 - 0.7**2, abs=0.01)
    noisy = model.metrics(y, y + rng.normal(0, 0.5, len(y)))
    assert noisy["sensitivity"] == pytest.approx(1.0, abs=0.05) and noisy["calibration_slope"] == pytest.approx(0.5, abs=0.05)
    assert model.metrics([1.0, 2.0], [1.0, 2.0])["r2"] is None  # too few to say


def test_group_folds_never_split_a_group_and_skip_missing_groups():
    groups = np.array(["a"] * 30 + ["b"] * 5 + ["c"] * 5 + ["d"] * 4 + ["e"] * 3 + [None] * 2, dtype=object)
    fold = model.group_folds(groups, 3)
    assert (fold[-2:] == -1).all() and (fold[:-2] >= 0).all()
    for g in "abcde":
        assert len(set(fold[groups == g])) == 1
    assert len(set(fold[:-2])) == 3
    two = model.group_folds(np.array(["a", "a", "b"], dtype=object), 10)  # fewer groups than folds: leave one group out
    assert two[0] == two[1] != two[2]
    with pytest.raises(model.ModelError):
        model.group_folds(np.array(["a", "a"], dtype=object))


def test_out_of_fold_guarantee_no_prediction_comes_from_a_model_that_saw_its_group():
    rng = np.random.default_rng(1)
    groups = np.array([f"mu{g}" for g in rng.integers(0, 9, 300)], dtype=object)
    groups[:7] = None  # fields without a map unit
    fold = model.group_folds(groups, 4)
    calls = []

    def fit_predict(train, test):
        calls.append((set(groups[train]), set(groups[test]), set(train.tolist()), set(test.tolist())))
        return np.full(len(test), float(len(calls)))

    pred = model.oof_predict(len(groups), fold, fit_predict)
    assert np.isfinite(pred).all()  # every row gets a prediction
    predicted = [i for _, _, _, test in calls for i in test]
    assert sorted(predicted) == list(range(300))  # ... exactly once
    for train_groups, test_groups, train_rows, test_rows in calls:
        assert not (train_groups & test_groups)  # the map unit of a predicted row was never trained on
        assert not (train_rows & test_rows)
        assert None not in train_groups  # rows without a group are never trained on
    assert calls[-1][1] == {None} and len(calls) == 5


def test_leak_grouped_r2_is_zero_when_the_label_is_pure_map_unit_noise_but_random_folds_look_great():
    frame = family_frame(n_units=12, fields_per_unit=6, years=6, label="unit_noise")
    result = model.train_family(frame, "landsat", schemes=("map_unit", "field", "random"))
    y = frame["om"].to_numpy()
    grouped = model.metrics(y, result.preds["map_unit"]["hgb"])
    random = model.metrics(y, result.preds["random"]["hgb"])
    by_field = model.metrics(y, result.preds["field"]["hgb"])
    assert random["r2"] > 0.7  # recognising the place is enough to look accurate
    assert by_field["r2"] > 0.7  # holding out whole fields does not help: the neighbours on the same map unit give it away
    assert grouped["r2"] < 0.1  # on a map unit the model has not seen it knows nothing, which is the truth here
    assert len(result.configs) == 4 and set(result.configs) <= set(FAST)  # model size was chosen once per outer fold


def test_a_real_signal_survives_the_map_unit_grouping():
    frame = family_frame(n_units=12, fields_per_unit=6, years=4, label="signal")
    result = model.train_family(frame, "landsat", schemes=("map_unit",))
    assert model.metrics(frame["om"].to_numpy(), result.preds["map_unit"]["hgb"])["r2"] > 0.6


class Memoriser:
    """Stands in for gradient boosting: knows every training row by heart and nothing else."""

    def __init__(self, X, y):
        self.known = {row.tobytes(): value for row, value in zip(np.ascontiguousarray(X), y)}

    def predict(self, X):
        return np.array([self.known.get(row.tobytes(), -1.0) for row in np.ascontiguousarray(X)])


def test_a_model_that_memorises_map_units_gets_nothing_under_the_map_unit_scheme(monkeypatch):
    # every field of a map unit has the same spectrum in every year: a twin of each row sits in every other field of the unit
    frame = family_frame(n_units=8, fields_per_unit=5, years=5, label="unit_noise", row_noise=0.0, field_jitter=0.0)
    monkeypatch.setattr(model, "fit_hgb", lambda X, y, config=None, seed=0: Memoriser(X, y))
    monkeypatch.setattr(model, "choose_config", lambda X, y, groups, seed=0: ("small", {}))
    result = model.train_family(frame, "landsat", schemes=("map_unit", "field", "random"))
    truth = frame["om"].to_numpy()
    assert (result.preds["map_unit"]["hgb"] == -1.0).all()  # no row of the field's map unit was in training
    assert (result.preds["field"]["hgb"] == truth).all()  # holding out the field alone hands it the answer
    assert (result.preds["random"]["hgb"] == truth).all()


def test_unlabelled_fields_are_predicted_but_never_trained_on():
    frame = family_frame(n_units=8, fields_per_unit=4, years=3, label="signal")
    hidden = frame["field_id"].isin(frame["field_id"].unique()[:3])
    truth = frame.loc[hidden, "om"].to_numpy()
    frame.loc[hidden, "om"] = np.nan
    result = model.train_family(frame, "landsat", schemes=("map_unit",))
    assert (result.folds["map_unit"][hidden.to_numpy()] == -1).all()
    pred = result.preds["map_unit"]["hgb"][hidden.to_numpy()]
    assert np.isfinite(pred).all() and np.abs(pred - truth).mean() < 0.6


def test_r2_interval_resamples_map_units_not_rows():
    rng = np.random.default_rng(3)
    groups = np.repeat(np.arange(10), 200).astype(str).astype(object)
    y = np.repeat(rng.normal(1.5, 0.5, 10), 200)
    pred = y + np.repeat(rng.normal(0, 0.3, 10), 200)  # the error is shared by a whole map unit
    lo, hi = model.r2_group_interval(y, pred, groups, n_boot=400)
    assert hi - lo > 0.1  # 2,000 rows, but only 10 pieces of evidence
    assert lo < model.metrics(y, pred)["r2"] < hi
    assert model.r2_group_interval(y[:400], pred[:400], groups[:400]) is None  # two groups: no interval


def test_label_summary_counts_map_units_and_how_lopsided_they_are():
    fields = pd.DataFrame({"om": [1.0] * 8 + [2.0, 2.1], "mukey": ["a"] * 8 + ["b", "c"], "dominant_share": 0.9})
    s = model.label_summary(fields)
    assert (s["n_fields"], s["n_map_units"], s["n_label_values"]) == (10, 3, 3)
    assert s["largest_map_unit_share"] == pytest.approx(0.8) and s["map_units_with_one_field"] == 2
    assert s["label_variance_between_map_units"] == pytest.approx(1.0)


def test_sensitivity_interval_resamples_map_units_and_is_wide_when_few_carry_the_slope():
    rng = np.random.default_rng(3)
    groups = np.repeat([f"mu{k}" for k in range(30)], 20)
    y = np.repeat(rng.uniform(0.6, 3.0, 30), 20)
    pred = 1.5 + 0.3 * (y - y.mean()) + rng.normal(0, 0.05, len(y))
    lo, hi = model.sensitivity_group_interval(y, pred, groups)
    assert lo < 0.3 < hi and hi - lo < 0.1  # every map unit agrees: tight
    # all loams look alike and two sandy map units carry the whole slope: the interval says how thin that is
    y2 = np.where(np.isin(groups, ["mu0", "mu1"]), 0.7, 1.8) + rng.normal(0, 0.1, len(y))
    pred2 = np.where(np.isin(groups, ["mu0", "mu1"]), 1.3, 1.7) + rng.normal(0, 0.05, len(y))
    lo2, hi2 = model.sensitivity_group_interval(y2, pred2, groups)
    assert hi2 - lo2 > 0.2 and lo2 < 0.1
    assert model.sensitivity_group_interval(y[:40], pred[:40], groups[:40]) is None  # two groups: no interval


def test_within_type_sensitivity_is_zero_for_a_model_that_only_tells_sand_from_loam():
    rng = np.random.default_rng(4)
    n = 600
    sandy = rng.random(n) < 0.25
    y = np.where(sandy, rng.uniform(0.6, 1.0, n), rng.uniform(1.4, 2.2, n))
    kind = np.where(sandy, "sand", "loam")
    groups = np.array([f"{k}{i % 12}" for i, k in enumerate(kind)], dtype=object)
    two_heaps = np.where(sandy, 1.4, 1.7) + rng.normal(0, 0.03, n)  # knows the soil type and nothing else
    overall = model.metrics(y, two_heaps)["sensitivity"]
    within = model.within_type_sensitivity(y, two_heaps, kind, groups)
    assert overall > 0.2 and abs(within["sensitivity"]) < 0.03 and abs(within["spearman"]) < 0.1
    assert within["sensitivity_interval90"][0] < noise.MIN_SENSITIVITY
    follows = 1.5 + 0.4 * (y - y.mean()) + rng.normal(0, 0.03, n)  # follows organic matter inside each type too
    inner = model.within_type_sensitivity(y, follows, kind, groups)
    assert inner["sensitivity"] == pytest.approx(0.4, abs=0.03) and inner["sensitivity_interval90"][0] > 0.3


def test_series_scheme_keeps_sister_map_units_together_and_is_skipped_when_unknown():
    frame = family_frame(n_units=12, fields_per_unit=4, years=4, label="signal")
    frame["series"] = "S" + (frame["mukey"].str[2:].astype(int) // 2).astype(str)  # two map units per series
    result = model.train_family(frame, "landsat", schemes=("map_unit", "series"))
    fold = result.folds["series"]
    assert (pd.Series(fold).groupby(frame["series"].to_numpy()).nunique() == 1).all()
    assert (pd.Series(result.folds["map_unit"]).groupby(frame["series"].to_numpy()).nunique() > 1).any()  # the headline split does separate sisters
    assert np.isfinite(result.preds["series"]["hgb"]).all()
    said = []
    without = model.train_family(frame.drop(columns="series"), "landsat", schemes=("map_unit", "series"), say=said.append)
    assert "series" not in without.preds and any("series: skipped" in line for line in said)


def test_a_model_that_knows_soil_series_gets_nothing_under_the_series_scheme(monkeypatch):
    """Sister map units share spectra and label: the map-unit split leaks through them, the series split does not."""
    rng = np.random.default_rng(7)
    rows = []
    for series in range(10):
        signature = spectrum(rng.uniform(0, 1)) * rng.uniform(0.85, 1.15, 6)
        om = rng.uniform(0.6, 3.0)  # unrelated to the spectrum
        for unit in range(2):
            for f in range(5):
                for y in range(4):
                    bands = np.clip(signature * (1 + rng.normal(0, 0.01, 6)), 0.01, 0.99)
                    rows.append([f"KS-{series * 100 + unit * 10 + f + 1:06d}", 2016 + y, f"mu{series}_{unit}", f"S{series}", "b0", om + 0.02 * unit, *bands])
    frame = pd.DataFrame(rows, columns=["field_id", "year", "mukey", "series", "block", "om", *features.BANDS])
    frame = frame[["field_id", "year", "mukey", "series", "block", "om"]].join(features.add_features(frame))
    result = model.train_family(frame, "landsat", schemes=("map_unit", "series"))
    y = frame["om"].to_numpy()
    assert model.metrics(y, result.preds["map_unit"]["hgb"])["r2"] > 0.8  # recognises the sister map unit
    assert model.metrics(y, result.preds["series"]["hgb"])["r2"] < 0.1


def test_spatial_blocks_are_grid_cells():
    blocks = model.spatial_blocks([0, 7499, 7501, -1], [0, 0, 0, 0], 7.5)
    assert blocks[0] == blocks[1] != blocks[2] and blocks[3] != blocks[0]


# --------------------------------------------------------------------------- confidence and combining the sensors


def wide_frame(n_fields=120, years=9, sd=None, seed=4):
    """Two families measuring the same field level with known noise: variance = a + b / sqrt(views)."""
    rng = np.random.default_rng(seed)
    rows = []
    for f in range(n_fields):
        level = rng.normal(1.5, 0.3)
        for y in range(years):
            row = {"field_id": f"KS-{f:06d}", "year": 2016 + y}
            for s, (a, b) in {"landsat": (0.005, 0.03), "s2": (0.01, 0.02)}.items():
                n = float(rng.choice([1, 2, 4, 9, 16, 36]))
                row[f"n_{s}"], row[f"q_{s}"] = n, 1.0
                row[f"p_{s}"] = level + rng.normal(0, np.sqrt(a + b / np.sqrt(n)))
            rows.append(row)
    return pd.DataFrame(rows)


def test_noise_model_recovers_how_noise_falls_with_bare_views():
    wide = wide_frame()
    fitted = conf.fit_noise_model(wide["field_id"], wide["p_landsat"], wide["n_landsat"], wide["q_landsat"])
    one = conf.noise_variance(fitted, [1.0], [1.0])[0]
    many = conf.noise_variance(fitted, [36.0], [1.0])[0]
    assert one == pytest.approx(0.005 + 0.03, rel=0.25)
    assert many == pytest.approx(0.005 + 0.03 / 6, rel=0.35)
    assert min(fitted["a"], fitted["b"], fitted["c"]) >= 0


def test_combined_value_leans_on_the_family_with_more_bare_views():
    wide = wide_frame()
    out, info = conf.combine(wide)
    few_landsat = (wide["n_landsat"] == 1) & (wide["n_s2"] == 36)
    assert out.loc[few_landsat, "w_s2"].mean() > 0.6
    assert np.allclose(out["w_landsat"] + out["w_s2"], 1.0)
    lo, hi = np.minimum(wide["p_landsat"], wide["p_s2"]), np.maximum(wide["p_landsat"], wide["p_s2"])
    assert ((out["om_pred"] >= lo - 1e-9) & (out["om_pred"] <= hi + 1e-9)).all()
    assert 0.0 <= info["rho"] < 0.2  # the two synthetic families share no noise
    # the combined value is closer to the field's level than either family alone
    def scatter(col):
        return float((col - col.groupby(wide["field_id"]).transform("mean")).std())

    assert scatter(out["om_pred"]) < min(scatter(wide["p_landsat"]), scatter(wide["p_s2"]))


def test_one_family_alone_passes_through_and_disagreement_lowers_trust():
    wide = wide_frame(n_fields=60)
    wide.loc[wide["year"] == 2016, ["p_s2"]] = np.nan  # Sentinel-2 starts a year later
    out, _ = conf.combine(wide)
    alone = wide["year"] == 2016
    assert np.allclose(out.loc[alone, "om_pred"], wide.loc[alone, "p_landsat"])
    assert (out.loc[alone, "agreement"] == 1.0).all() and (out.loc[alone, "n_sensors"] == 1).all()
    both = ~alone
    gap = (wide["p_s2"] - wide["p_landsat"]).abs()
    far, near = both & (gap > gap[both].quantile(0.9)), both & (gap < gap[both].quantile(0.1))
    assert out.loc[far, "agreement"].min() > out.loc[near, "agreement"].max()
    assert out["agreement"].between(conf.G_MIN, conf.G_MAX).all()
    wide["p_landsat"] = np.nan
    wide["p_s2"] = np.nan
    empty, _ = conf.combine(wide)
    assert empty["om_pred"].isna().all()


def test_confidence_is_between_0_and_1_and_0_where_om_pred_is_null():
    om = np.array([1.5, 1.6, np.nan, 1.4, 1.7])
    var = np.array([0.001, 0.05, np.nan, 5.0, 0.0])
    c = conf.confidence(om, var, tau2=0.04, skill=0.5)
    assert ((c >= 0) & (c <= 1)).all()
    assert c[2] == 0.0  # no om_pred, no confidence
    assert c[0] > c[1] > c[3]  # more noise, less confidence
    assert c.max() <= 0.5 + 1e-12  # never above the model's skill
    assert (conf.confidence(om, var, tau2=0.04, skill=0.0) == 0).all()
    assert (conf.confidence(om, var, tau2=0.04, skill=float("nan")) == 0).all()
    assert conf.confidence(om, var, tau2=0.04, skill=7.0).max() <= 1.0


# --------------------------------------------------------------------------- bare days


def test_bare_days_are_weighted_by_clear_views_on_landsats_scale():
    rng = np.random.default_rng(5)
    truth = rng.uniform(0, 200, 400)
    wide = pd.DataFrame({"bd_landsat": truth + rng.normal(0, 5, 400), "bd_s2": 10 + 1.1 * truth + rng.normal(0, 5, 400), "nc_landsat": 20.0, "nc_s2": 60.0})
    wide.loc[:9, "bd_s2"] = np.nan
    wide.loc[10:19, "bd_landsat"] = np.nan
    wide.loc[20:24, ["bd_landsat", "bd_s2"]] = np.nan
    value, info = exposure.combine_bare_days(wide)
    assert info["s2_to_landsat"]["b"] == pytest.approx(1 / 1.1, abs=0.03)  # Sentinel-2 is brought back to Landsat's scale
    assert np.allclose(value[:10], wide["bd_landsat"][:10])  # Landsat only
    assert np.abs(value[10:20] - truth[10:20]).mean() < 8  # Sentinel-2 only, rescaled
    assert np.isnan(value[20:25]).all() and info["rows_null"] == 5
    both = slice(25, None)
    assert np.abs(value[both] - truth[both]).mean() < np.abs(wide["bd_landsat"][both] - truth[both]).mean()
    assert ((value[both] >= 0) & (value[both] <= 366)).all()


def test_snowy_sentinel2_bare_days_are_set_aside_only_in_an_unscreened_table():
    wide = pd.DataFrame({"bd_landsat": [10.0, 10.0, np.nan], "bd_s2": [60.0, 60.0, 60.0], "nc_landsat": 20.0, "nc_s2": 20.0, "snowy_s2": [True, False, True]})
    unscreened, info = exposure.combine_bare_days(wide, snow_screened=False)
    assert unscreened.tolist() == [10.0, 35.0, 60.0] and info["s2_rows_dropped_for_snow"] == 1  # kept where Landsat has nothing
    screened, _ = exposure.combine_bare_days(wide, snow_screened=True)
    assert screened.tolist() == [35.0, 35.0, 60.0]


def bare_table(n_fields=240, seed=9, shuffle=False):
    """bare_days by crop as in Finney County: fallow high and jumpy, alfalfa at zero, corn in between."""
    rng = np.random.default_rng(seed)
    level = {"fallow": (144, 35), "corn": (57, 24), "sorghum": (37, 14), "winter wheat": (24, 21), "alfalfa": (0.5, 0.6)}
    rotations = [["fallow"], ["corn"], ["sorghum"], ["winter wheat"], ["alfalfa"], ["winter wheat", "fallow"]]
    rows = []
    for f in range(n_fields):
        rot = rotations[f % len(rotations)]
        for y in YEARS:
            crop, prev = rot[y % len(rot)], rot[(y - 1) % len(rot)]
            mean, sd = level[crop]
            rows.append({"field_id": f"KS-{f:06d}", "year": y, "crop": crop, "prev_crop": prev, "bare_days": float(np.clip(rng.normal(mean, sd), 0, 366))})
    frame = pd.DataFrame(rows)
    if shuffle:  # what the sceptic did: the values no longer belong to their fields
        frame["bare_days"] = rng.permutation(frame["bare_days"].to_numpy())
    wide = pd.DataFrame({"bd_landsat": frame["bare_days"] + rng.normal(0, 9, len(frame)), "bd_s2": frame["bare_days"] + rng.normal(0, 9, len(frame))})
    cover = frame[["field_id", "year"]].assign(cover_frac=np.clip(0.8 - frame["bare_days"] / 250 + rng.normal(0, 0.1, len(frame)), 0, 1))
    if shuffle:
        cover["cover_frac"] = rng.permutation(cover["cover_frac"].to_numpy())
    return frame[["field_id", "year", "bare_days"]], wide, frame[["field_id", "year", "crop", "prev_crop"]], cover


def test_bare_days_noise_is_reported_per_crop_because_the_pooled_figure_flatters():
    table, wide, crops, cover = bare_table()
    ck = exposure.checks(table, wide, crops, cover)
    stable = ck["stable_crop"]
    by = stable["by_crop"]
    assert by["fallow"]["robust_sd_of_jump"] == pytest.approx(35 * np.sqrt(2), rel=0.2)
    assert by["alfalfa"]["robust_sd_of_jump"] < 2
    assert stable["robust_sd_of_jump"] < 0.75 * by["fallow"]["robust_sd_of_jump"]  # alfalfa pulls the pooled figure down
    assert stable["sd_of_jump"] > stable["robust_sd_of_jump"] and set(stable["crop_mix"]) == set(by)
    # fallow against a cropped year: the gap in units of the noise of one such comparison, with the noisiest crop
    fallow = ck["fallow_vs_cropped"]
    worst = max(by[c]["robust_sd_of_jump"] for c in ("corn", "sorghum", "winter wheat", "alfalfa"))
    assert fallow["noise_sd_of_one_comparison"] == pytest.approx(np.sqrt((by["fallow"]["robust_sd_of_jump"] ** 2 + worst**2) / 2))
    assert 2 < fallow["gap_in_noise_sds"] < 4 and fallow["field_years_needed_per_group"] <= 5
    # corn against sorghum is inside one field-year's noise and needs many field-years
    pair = next(r for r in ck["separation"] if (r["high"], r["low"]) == ("corn", "sorghum"))
    assert pair["gap_in_noise_sds"] < 1.5 and pair["field_years_needed_per_group"] > 10
    assert len(ck["separation"]) == 10  # every pair of the five core crops present
    passed = exposure.passes(ck)
    assert passed["all_pass"] is True and passed["failed"] == [] and passed["untested"] == []


def test_bare_days_that_do_not_belong_to_their_fields_fail_the_checks():
    table, wide, crops, cover = bare_table(shuffle=True)
    passed = exposure.passes(exposure.checks(table, wide, crops, cover))
    assert passed["all_pass"] is False
    assert {"fallow_stands_out", "follows_green_cover"} <= set(passed["failed"])
    assert passed["sensors_agree"] is True  # both sensors were shuffled together: agreement alone proves nothing


def test_bare_days_checks_that_cannot_run_are_not_counted_as_passed():
    table, wide, crops, cover = bare_table()
    one_sensor = wide.assign(bd_s2=np.nan)
    passed = exposure.passes(exposure.checks(table, one_sensor, crops, None))
    assert passed["sensors_agree"] is None and passed["follows_green_cover"] is None
    assert passed["untested"] == ["sensors_agree", "follows_green_cover"] and passed["all_pass"] is True
    nothing = exposure.passes(exposure.checks(table, one_sensor, None, None))
    assert nothing["all_pass"] is None and len(nothing["untested"]) == 4


# --------------------------------------------------------------------------- noise


def noise_table(n_fields=400, sd=0.2, year_effect=None, trend=0.0, corn_shift=0.0, seed=6, crops_of=None):
    """om_pred = field level + year effect + trend + crop shift + N(0, sd), with blocks of 10 fields."""
    rng = np.random.default_rng(seed)
    year_effect = year_effect or {}
    rows, crop_rows = [], []
    for f in range(n_fields):
        fid = f"KS-{f:06d}"
        level = rng.normal(1.5, 0.3)
        for y in YEARS:
            crop = crops_of(f, y) if crops_of else "corn"
            prev = crops_of(f, y - 1) if crops_of else "corn"
            value = level + year_effect.get(y, 0.0) + trend * (y - YEARS[0]) / 10 + (corn_shift if crop == "corn" else 0.0) + rng.normal(0, sd)
            rows.append({"field_id": fid, "year": y, "om_pred": value, "n_obs": 5.0, "block": f"b{f // 10}"})
            crop_rows.append({"field_id": fid, "year": y, "crop": crop, "prev_crop": prev})
    return pd.DataFrame(rows), pd.DataFrame(crop_rows)


def test_jump_sd_on_unchanged_fields_recovers_known_noise():
    table, crops = noise_table(sd=0.2)
    stats = noise.stable_crop_noise(table, "om_pred", crops)
    assert stats["raw"]["n"] == 400 * 8
    assert stats["raw"]["robust_sd"] == pytest.approx(0.2 * np.sqrt(2), rel=0.06)  # a jump is two noises
    assert stats["year_effect_removed"]["robust_sd"] == pytest.approx(0.2 * np.sqrt(2), rel=0.06)
    mono = noise.single_crop_noise(table, "om_pred", crops)
    assert mono["n_fields"] == 400 and mono["equivalent_jump_sd"] == pytest.approx(0.2 * np.sqrt(2), rel=0.06)


def test_county_wide_year_effect_inflates_raw_jumps_and_is_removed():
    effect = {y: (0.3 if y % 2 else -0.3) for y in YEARS}
    table, crops = noise_table(sd=0.1, year_effect=effect)
    stats = noise.stable_crop_noise(table, "om_pred", crops)
    assert stats["raw"]["robust_sd"] > 0.4  # every field jumps by 0.6 with the county
    assert stats["year_effect_removed"]["robust_sd"] == pytest.approx(0.1 * np.sqrt(2), rel=0.08)
    assert stats["year_effect_sd"] == pytest.approx(0.3, rel=0.15)
    comp = noise.variance_components(table, "om_pred")
    assert comp["sd_county_year"] == pytest.approx(0.3, rel=0.2)
    assert comp["sd_field_year"] == pytest.approx(0.1, rel=0.1)
    assert comp["sd_block_year"] < 0.03
    assert comp["share_county_year"] > 0.8


def test_only_pairs_with_the_same_crop_count_as_unchanged():
    table, crops = noise_table(n_fields=40, crops_of=lambda f, y: "corn" if f < 10 else ("winter wheat" if y % 2 else "fallow"))
    stats = noise.stable_crop_noise(table, "om_pred", crops)
    assert stats["raw"]["n"] == 10 * 8  # the wheat-fallow fields change crop every year
    assert stats["raw"]["all_pairs"]["n"] == 40 * 8
    assert list(noise.single_crop_fields(crops).unique()) == ["corn"] and len(noise.single_crop_fields(crops)) == 10


def test_minimum_detectable_change_follows_the_trend_standard_error_and_the_sensitivity():
    table, crops = noise_table(sd=0.2)
    det = noise.detectability(table, "om_pred", sensitivity=0.5, jump_sd=0.2 * np.sqrt(2))
    se = 10 * 0.2 / np.sqrt(60)  # nine yearly values, independent noise: sum of squared year offsets = 60
    assert det["trend_se_one_field_from_jump_sd"] == pytest.approx(se, rel=1e-6)
    assert det["trend_se_one_field"] == pytest.approx(se, rel=0.12)  # measured from the fitted trends themselves
    by_n = {row["n_fields"]: row for row in det["by_n_fields"]}
    assert by_n[1]["mdc_real_om"] == pytest.approx(noise.Z_DETECT * se / 0.5, rel=0.12)
    assert by_n[100]["mdc_real_om"] == pytest.approx(by_n[1]["mdc_real_om"] / 10, rel=0.35)  # no shared noise here: averages out
    assert by_n[1]["mdc_real_om"] == pytest.approx(by_n[1]["mdc_om_pred"] / 0.5)
    blind = noise.detectability(table, "om_pred", sensitivity=0.0, jump_sd=0.28)
    assert all(row["mdc_real_om"] is None for row in blind["by_n_fields"])  # a model that does not follow the label detects nothing


def test_shared_yearly_noise_does_not_average_out():
    rng = np.random.default_rng(8)
    effect = {y: float(rng.normal(0, 0.1)) for y in YEARS}
    table, _ = noise_table(sd=0.1, year_effect=effect)
    det = noise.detectability(table, "om_pred", sensitivity=1.0, jump_sd=None)
    by_n = {row["n_fields"]: row for row in det["by_n_fields"]}
    assert by_n[1000]["mdc_real_om"] > 0.5 * by_n[10]["mdc_real_om"]  # a thousand fields are hardly better than ten
    assert by_n[1000]["mdc_real_om_difference"] < 0.3 * by_n[1000]["mdc_real_om"]  # ... unless two groups are compared in the same years
    assert det["floor_real_om"] == pytest.approx(by_n[1000]["mdc_real_om"], rel=0.15)


def test_field_trend_is_recovered():
    table, _ = noise_table(n_fields=300, sd=0.05, trend=0.4)
    slopes = noise.field_trends(table, "om_pred")
    assert len(slopes) == 300 and slopes.median() == pytest.approx(0.4, abs=0.02)
    table.loc[table["year"] > 2019, "om_pred"] = np.nan  # four values left: too few for a trend
    assert len(noise.field_trends(table, "om_pred")) == 0


ROTATIONS = [["corn", "fallow", "winter wheat"], ["corn", "winter wheat", "fallow"], ["corn", "corn", "fallow", "winter wheat"]]


def rotating(f, y):
    rotation = ROTATIONS[f % 3]
    return rotation[(f + y) % len(rotation)]


def test_crop_artefact_is_found_with_field_and_year_effects_in_the_way():
    effect = {y: 0.05 * (y - 2020) for y in YEARS}
    table, crops = noise_table(n_fields=300, sd=0.05, corn_shift=-0.2, year_effect=effect, crops_of=rotating)
    art = noise.crop_artefacts(table, "om_pred", crops, sensitivity=0.5)
    assert art["reference_this_year"] == "fallow" and art["last_year_identified"]
    assert art["this_year"]["corn"]["shift"] == pytest.approx(-0.2, abs=0.02)
    assert abs(art["this_year"]["winter wheat"]["shift"]) < 0.02
    assert all(abs(v["shift"]) < 0.02 for v in art["last_year"].values())  # last year's crop does nothing in this world
    assert art["largest_clear_shift"]["crop"] == "corn" and art["largest_clear_shift"]["when"] == "this_year"
    assert art["clear_shift_as_real_om"] == pytest.approx(0.4, abs=0.05)  # 0.2 in om_pred = 0.4 of real organic matter at sensitivity 0.5
    assert art["clear_shift_over_decade_signal"] == pytest.approx(0.2 / (noise.HOPED_CENTRAL * 0.5), rel=0.1)


def test_crop_artefact_with_one_fixed_rotation_reports_this_years_crop_only():
    rotation = ROTATIONS[0]  # every field: last year's crop is known from this year's
    table, crops = noise_table(n_fields=150, sd=0.05, corn_shift=-0.2, crops_of=lambda f, y: rotation[(f + y) % 3])
    art = noise.crop_artefacts(table, "om_pred", crops, sensitivity=0.5)
    assert not art["last_year_identified"] and list(art["last_year"]) == ["fallow"]
    assert art["this_year"]["corn"]["shift"] == pytest.approx(-0.2, abs=0.02)
    assert art["largest_clear_shift"]["crop"] == "corn"


def test_sensor_repeatability_measures_the_noise_of_one_family():
    wide = wide_frame(n_fields=200)
    rep = noise.sensor_repeatability(wide)
    expected = np.sqrt((wide["p_s2"] - wide["p_landsat"]).var())
    assert rep["n_pairs"] == len(wide) and rep["robust_sd_of_difference"] == pytest.approx(expected, rel=0.1)
    assert abs(rep["bias_s2_minus_landsat"]) < 0.02 and rep["r_of_field_means"] > 0.9 and abs(rep["r_of_yearly_deviations"]) < 0.1


def verdict_case(mdc, artefact, sensitivity=0.2, interval=None, within=0.2, within_interval=None, bare=None):
    """noise.verdict() on hand-made numbers: mdc = minimum detectable real change for 1, 10, 100, 1000 fields."""
    usable = sensitivity is not None and sensitivity >= noise.MIN_SENSITIVITY
    rows = [{"n_fields": n, "mdc_real_om": m, "mdc_om_pred": m * sensitivity if usable and m is not None else None} for n, m in zip(noise.N_FIELDS, mdc)]
    art = {"clear_shift_as_real_om": artefact}
    if artefact is not None:
        art["largest_clear_shift"] = {"crop": "corn", "when": "this_year", "shift": -artefact * sensitivity, "se": 0.001}
    numbers = {
        "detectability": {"by_n_fields": rows, "sensitivity": sensitivity, "sensitivity_interval90": interval},
        "artefacts": art,
        "om_noise_sd": 0.17,
        "within_type": None if within is None else {"sensitivity": within, "spearman": 0.3 if within > 0.02 else 0.0, "sensitivity_interval90": within_interval},
    }
    return noise.verdict(numbers, bare)


def test_verdict_grades_and_recommendation():
    buried = verdict_case([2.3, 0.8, 0.44, 0.40], 0.25)
    assert (buried["single_field"], buried["hundred_fields"], buried["thousand_fields"]) == ("buried", "buried", "buried")
    assert buried["recommended"] == "bare_days" and buried["artefact_rivals_signal"]
    assert "buried in noise" in buried["text"] and "bare days" in buried["text"]
    assert buried["text"].count(". ") == 1 and "0.17" in buried["text"]  # two plain sentences
    confounded = verdict_case([1.0, 0.4, 0.15, 0.08], 0.5)
    assert confounded["hundred_fields"] == "detectable" and confounded["recommended"] == "bare_days"  # steady, but it follows the crop
    good = verdict_case([0.5, 0.25, 0.12, 0.05], 0.05)
    assert good["single_field"] == "buried" and good["ten_fields"] == "marginal" and good["recommended"] == "om_pred"
    assert good["trustworthy"]["om_pred"] and good["follows_organic_matter_within_soil_type"]
    blind = verdict_case([None, None, None, None], None, sensitivity=-0.05)  # steady only because it predicts the same everywhere
    assert blind["single_field"] == "buried" and blind["recommended"] == "bare_days" and not blind["model_has_skill"]
    assert "does not follow" in blind["text"] and "jumps" not in blind["text"]
    for v in (buried, confounded, good, blind):
        assert v["recommended"] in health_schema.RECOMMENDED
        assert v["text"].count(". ") == 1  # two sentences
    for v in (buried, confounded, blind):  # the fallback is named for what it is
        assert "exposed" in v["text"] and "not what the soil has become" in v["text"]


def test_a_high_sensitivity_between_soils_cannot_flip_the_recommendation_on_its_own():
    """The sceptic's case: the same noise and the same crop shift, read through a larger sand-against-loam sensitivity."""
    steady = [0.5, 0.25, 0.12, 0.05]
    # between soils it looks fine, within a soil type the model follows nothing
    sand_vs_loam = verdict_case(steady, 0.05, sensitivity=0.25, within=0.0)
    assert sand_vs_loam["hundred_fields"] == "detectable" and sand_vs_loam["recommended"] == "bare_days"
    assert sand_vs_loam["follows_organic_matter_within_soil_type"] is False and not sand_vs_loam["trustworthy"]["om_pred"]
    assert "does not follow organic matter" in sand_vs_loam["text"] and "sand from loam" in sand_vs_loam["text"]
    # a point value above zero whose interval reaches zero is not enough either, and the text says "cannot be shown"
    unsure = verdict_case(steady, 0.05, sensitivity=0.25, within=0.08, within_interval=[-0.05, 0.21])
    assert unsure["recommended"] == "bare_days" and "cannot be shown to follow" in unsure["text"]
    assert unsure["sensitivity_for_change"] == pytest.approx(-0.05)
    # nothing known about the sensitivity within a soil type: not recommended
    assert verdict_case(steady, 0.05, within=None)["recommended"] == "bare_days"
    # the lower end of the between-soil interval is what counts: 0.12 x 0.25 / 0.04 = 0.75 > 0.3
    wide = verdict_case(steady, 0.05, sensitivity=0.25, interval=[0.04, 0.3], within=0.25, within_interval=[0.2, 0.3])
    assert wide["recommended"] == "bare_days" and wide["hundred_fields_mdc_at_cautious_sensitivity"] == pytest.approx(0.75)
    # skill within a soil type, a tight interval, steady enough, no artefact: only then
    sound = verdict_case(steady, 0.05, sensitivity=0.25, interval=[0.22, 0.28], within=0.24, within_interval=[0.2, 0.28])
    assert sound["recommended"] == "om_pred" and sound["hundred_fields_mdc_at_cautious_sensitivity"] == pytest.approx(0.15)
    # an artefact that is small at the measured sensitivity but rivals the signal at the cautious one
    shifty = verdict_case(steady, 0.15, sensitivity=0.25, interval=[0.22, 0.28], within=0.24, within_interval=[0.15, 0.28])
    assert not shifty["artefact_rivals_signal"] and shifty["artefact_rivals_signal_at_cautious_sensitivity"]
    assert shifty["recommended"] == "bare_days" and "crop residue" in shifty["text"]


def test_verdict_says_so_when_bare_days_fails_its_own_checks():
    ok = {"all_pass": True, "failed": []}
    bad = {"all_pass": False, "failed": ["fallow_stands_out", "follows_green_cover"]}
    fine = verdict_case([2.3, 0.8, 0.44, 0.40], 0.25, bare=ok)
    assert fine["bare_days_checks_pass"] is True and fine["trustworthy"] == {"om_pred": False, "bare_days": True}
    assert "compare rotations by bare days" in fine["text"]
    broken = verdict_case([2.3, 0.8, 0.44, 0.40], 0.25, bare=bad)
    assert broken["recommended"] == "bare_days"  # the contract has no third value ...
    assert broken["trustworthy"] == {"om_pred": False, "bare_days": False}  # ... so the meta and the text carry it
    assert "compare rotations by" not in broken["text"] and "do not rely on either" in broken["text"]
    assert "fallow year does not stand out" in broken["text"] and "green cover" in broken["text"]
    assert broken["text"].count(". ") == 1
    blind = verdict_case([None] * 4, None, sensitivity=0.0, bare=bad)
    assert "do not rely on either" in blind["text"] and blind["text"].count(". ") == 1


def test_group_figures_use_the_plain_sd_and_carry_the_sensitivity_interval():
    """A few fields with wild trends: the typical field does not feel them, a mean of fields does."""
    rng = np.random.default_rng(11)
    table, _ = noise_table(n_fields=400, sd=0.2, seed=11)
    wild = table["field_id"].isin(table["field_id"].unique()[:40])  # a tenth of the fields is four times as noisy
    table.loc[wild, "om_pred"] = 1.5 + rng.normal(0, 0.8, int(wild.sum()))
    det = noise.detectability(table, "om_pred", sensitivity=0.2, jump_sd=None, sensitivity_interval=[0.05, 0.25])
    assert det["trend_sd_plain"] > 1.2 * det["trend_se_one_field"]
    rows = {r["n_fields"]: r for r in det["by_n_fields"]}
    assert rows[1]["trend_se"] == pytest.approx(det["trend_se_one_field"])  # one field: the typical one
    comp = det["variance_components"]
    expected = det["trend_sd_plain"] * np.sqrt(comp["share_county_year"] + comp["share_block_year"] / min(100, comp["n_blocks"]) + comp["share_field_year"] / 100)
    assert rows[100]["trend_se"] == pytest.approx(expected)
    low, high = rows[100]["mdc_real_om_range"]
    assert low == pytest.approx(rows[100]["mdc_om_pred"] / 0.25) and high == pytest.approx(rows[100]["mdc_om_pred"] / 0.05)
    assert low < rows[100]["mdc_real_om"] < high
    # the single-field verdict needs no sensitivity: 2.8 x SE in om_pred's own units against the top of the hoped range
    assert det["mdc_one_field_om_pred_units"] == pytest.approx(noise.Z_DETECT * det["trend_se_one_field"])
    assert det["single_field_buried_at_any_sensitivity"] == (det["mdc_one_field_om_pred_units"] > noise.HOPED_PER_DECADE[1])
    # where the interval reaches zero there is no upper limit
    open_ended = noise.detectability(table, "om_pred", 0.2, None, sensitivity_interval=[-0.01, 0.25])["by_n_fields"][0]["mdc_real_om_range"]
    assert open_ended[1] is None and open_ended[0] > 0


def test_unchanged_crop_noise_reports_its_crop_mix():
    table, crops = noise_table(n_fields=300, sd=0.2, seed=8, crops_of=lambda f, y: "corn" if f % 3 else ("fallow" if f % 2 else "alfalfa"))
    out = noise.stable_crop_noise(table, "om_pred", crops)
    assert out["crop_mix"]["corn"] == pytest.approx(2 / 3, abs=0.01) and set(out["by_crop"]) == {"corn", "fallow", "alfalfa"}
    assert out["crop_balanced_robust_sd"] == pytest.approx(np.mean([v["robust_sd"] for v in out["by_crop"].values()]))
    assert out["by_crop"]["corn"]["sd"] == pytest.approx(0.2 * np.sqrt(2), rel=0.1)


def test_crop_table_reads_the_history_and_the_year_before():
    fields = pd.DataFrame({"field_id": ["KS-000001"], "crop_history": [json.dumps({"2015": "corn", "2016": "fallow", "2018": "corn"})]})
    crops = noise.crop_table(fields, [2016, 2017, 2018]).set_index("year")
    assert crops.loc[2016, ["crop", "prev_crop"]].tolist() == ["fallow", "corn"]
    assert pd.isna(crops.loc[2017, "crop"]) and crops.loc[2017, "prev_crop"] == "fallow"
    assert pd.isna(crops.loc[2018, "prev_crop"])


# --------------------------------------------------------------------------- build


def test_build_writes_a_valid_table_meta_and_report(tmp_path):
    fields, mapunits, obs, fields_path, obs_path = write_inputs(tmp_path)
    meta, paths = run_build(tmp_path, mapunits, fields_path, obs_path)
    table = health_schema.load_soil_health(paths["out_path"])  # raises if the contract is broken
    assert list(table.columns) == health_schema.COLUMNS
    assert len(table) == len(fields) * len(YEARS) and not table.duplicated(["field_id", "year"]).any()
    assert set(table["field_id"]) == set(fields["field_id"]) and set(table["year"]) == set(YEARS)

    none = table["om_pred"].isna()
    assert 0 < none.sum() < len(table)
    assert (table.loc[none, "confidence"] == 0).all() and (table.loc[none, "n_obs"] == 0).all()
    assert table["confidence"].between(0, 1).all() and (table.loc[~none, "confidence"] > 0).all()
    assert table.loc[~none, "om_pred"].between(0.3, 3.5).all()
    assert table["bare_days"].dropna().between(0, 366).all() and table["bare_days"].notna().mean() > 0.95

    # n_obs counts the bare views of the composites that were used; a snow composite is not one of them
    usable = obs[features.usable(obs)].groupby(["field_id", "year"])["n_bare"].sum()
    got = table.set_index(["field_id", "year"])["n_obs"]
    assert np.allclose(got[usable.index], usable.round(3), atol=1e-3)
    assert (got.drop(usable.index) == 0).all()
    assert meta["inputs"]["composites_dropped_as_snow"]["landsat"] > 0

    stored = health_schema.load_meta(paths["meta_path"])
    assert stored["recommended"] in health_schema.RECOMMENDED and stored["om_noise_sd"] >= 0
    assert 1 <= stored["verdict"].count(". ") + 1 <= 2
    assert {"model", "noise", "confidence", "bare_days", "sentinel2_adds", "notes", "trustworthy"} <= set(stored)
    assert set(stored["notes"]) == {"om_pred", "bare_days", "confidence"}
    assert "not accuracy" in stored["notes"]["confidence"] and "not what the soil has become" in stored["notes"]["bare_days"]
    assert stored["trustworthy"] == stored["noise"]["verdict"]["trustworthy"]
    assert stored["bare_days"]["passes"]["all_pass"] is True  # synthetic bare days follow the crops
    assert stored["bare_days"]["checks"]["stable_crop"]["by_crop"] and stored["bare_days"]["checks"]["fallow_vs_cropped"]["gap_in_noise_sds"] > 2
    check = stored["confidence"]["check"]
    for key in ("constant_guess", "lower_heap", "upper_heap", "within_field"):
        assert f"spearman_confidence_vs_abs_error_{key}" in check
    assert "spearman_confidence_vs_n_obs" in check and "median_abs_error_constant_guess" in check["bins"][0]
    detect = stored["noise"]["detectability"]
    assert len(detect["sensitivity_interval90"]) == 2 and detect["by_n_fields"][2]["mdc_real_om_range"] is not None
    assert stored["noise"]["within_type"]["n_fields"] == len(fields)
    assert stored["model"]["labels"]["n_series"] == 4 and len(stored["model"]["honest_r2_range"]) == 2
    assert stored["sentinel2_adds"]["bare_days_coverage_both"] >= stored["sentinel2_adds"]["bare_days_coverage_landsat_alone"]
    assert stored["model"]["labels"]["n_map_units"] == 8 and stored["model"]["labels"]["n_fields"] == len(fields)
    assert set(stored["model"]["combined"]) == set(model.SCHEMES)
    assert "NaN" not in paths["meta_path"].read_text()  # strict JSON for the browser
    assert stored["model"]["coverage"] == pytest.approx(1 - none.mean())

    report = paths["report_path"].read_text()
    headings = (
        "## Verdict", "Accuracy against SSURGO", "random K-fold (leaks)", "grouped by soil series", "Honest accuracy", "Signal against noise",
        "Artefacts", "bare_days", "repeatability, not accuracy", "What one field-year can tell", "What Sentinel-2 adds", "field-years needed",
    )  # fmt: skip
    for heading in headings:
        assert heading in report
    assert (tmp_path / "build.log").exists()
    assert not list(tmp_path.glob("*.part"))


def test_table_values_are_out_of_fold_by_map_unit(tmp_path, monkeypatch):
    """Every om_pred in the table comes from a model whose training set held no field of that field's map unit."""
    fields, mapunits, darkness = make_fields()
    obs = make_obs(fields, darkness, sensors=("landsat",))  # one family: om_pred is that family's prediction, unmixed
    fields_path, obs_path = tmp_path / "fields.parquet", tmp_path / "obs.parquet"
    fields.to_parquet(fields_path)
    obs.to_parquet(obs_path, index=False)
    trained_on: list[set] = []  # labels each fitted model saw

    class Tagged:
        def __init__(self, tag):
            self.tag = tag

        def predict(self, X):
            return np.full(len(X), 0.5 + 0.01 * self.tag)  # the prediction names the model that made it

    def fit(X, y, config=None, seed=0):
        trained_on.append(set(np.round(y, 3).tolist()))
        return Tagged(len(trained_on) - 1)

    monkeypatch.setattr(model, "fit_hgb", fit)
    monkeypatch.setattr(model, "choose_config", lambda X, y, groups, seed=0: ("small", {}))
    meta, paths = run_build(tmp_path, mapunits, fields_path, obs_path, schemes=("map_unit",))
    table = health_schema.load_soil_health(paths["out_path"]).dropna(subset=["om_pred"])
    assert len(table) > 200
    unit_of = mapunits["dominant_mukey"].to_dict()
    unit_labels: dict[str, set] = {}
    for fid, om in zip(fields["field_id"], fields["organic_matter_pct"]):
        unit_labels.setdefault(unit_of[fid], set()).add(round(float(om), 3))
    assert len(set.union(*unit_labels.values())) == sum(len(v) for v in unit_labels.values())  # no label is shared between map units
    for fid, om_pred in zip(table["field_id"], table["om_pred"]):
        maker = int(round((om_pred - 0.5) / 0.01))
        assert not (unit_labels[unit_of[fid]] & trained_on[maker]), f"{fid} was predicted by a model that saw its map unit"
    assert len({int(round((v - 0.5) / 0.01)) for v in table["om_pred"]}) == 4  # one model per fold filled the table


def test_build_reports_fields_missing_from_the_observations(tmp_path):
    fields, mapunits, obs, fields_path, obs_path = write_inputs(tmp_path)
    keep = sorted(obs["field_id"].unique())[:40]
    obs[obs["field_id"].isin(keep)].to_parquet(obs_path, index=False)
    meta, paths = run_build(tmp_path, mapunits, fields_path, obs_path, schemes=("map_unit", "random"))
    table = pd.read_parquet(paths["out_path"])
    assert set(table["field_id"]) == set(keep)
    assert any("8 of 48 fields" in w and "no row" in w for w in meta["warnings"])


def test_build_works_with_landsat_alone(tmp_path):
    fields, mapunits, darkness = make_fields()
    obs = make_obs(fields, darkness, sensors=("landsat",))
    fields_path, obs_path = tmp_path / "fields.parquet", tmp_path / "obs.parquet"
    fields.to_parquet(fields_path)
    obs.to_parquet(obs_path, index=False)
    meta, paths = run_build(tmp_path, mapunits, fields_path, obs_path, schemes=("map_unit", "random"))
    table = health_schema.load_soil_health(paths["out_path"])
    assert table["om_pred"].notna().mean() > 0.5
    assert meta["noise"]["repeatability"]["n_pairs"] == 0 and "sensor_step" not in meta["noise"]
    assert any("Sentinel-2" in w for w in meta["warnings"])
    # the first stage of a county run: a complete table, with the two-sensor checks marked as not run
    assert meta["sentinel2_adds"] is None and meta["bare_days"]["passes"]["sensors_agree"] is None
    assert meta["bare_days"]["passes"]["all_pass"] is True and table["bare_days"].notna().mean() > 0.95
    assert "one sensor family" in paths["report_path"].read_text()


def test_build_without_soil_series_skips_that_scheme(tmp_path):
    fields, mapunits, obs, fields_path, obs_path = write_inputs(tmp_path)
    meta, _ = run_build(tmp_path, mapunits.drop(columns="dominant_series"), fields_path, obs_path)
    assert set(meta["model"]["combined"]) == set(model.SCHEMES) - {"series"}
    assert meta["model"]["honest_r2_range"] is None and meta["model"]["labels"]["n_series"] == 0


def test_build_says_when_neither_column_can_be_trusted(tmp_path):
    """Bare days that do not follow the crops: still named (the contract has two values), and flagged everywhere."""
    fields, mapunits, darkness = make_fields()
    obs = make_obs(fields, darkness, noise_sd=0.15)  # spectra this noisy say nothing: om_pred is out as well
    rng = np.random.default_rng(0)
    for sensor in ("landsat", "s2"):
        rows = obs["sensor"] == sensor
        obs.loc[rows, "bare_days"] = rng.permutation(obs.loc[rows, "bare_days"].to_numpy())
    fields_path, obs_path = tmp_path / "fields.parquet", tmp_path / "obs.parquet"
    fields.to_parquet(fields_path)
    obs.to_parquet(obs_path, index=False)
    meta, paths = run_build(tmp_path, mapunits, fields_path, obs_path, schemes=("map_unit", "random"))
    assert meta["bare_days"]["passes"]["all_pass"] is False and "fallow_stands_out" in meta["bare_days"]["passes"]["failed"]
    assert meta["recommended"] == "bare_days" and meta["recommended"] in health_schema.RECOMMENDED
    assert meta["trustworthy"]["bare_days"] is False and "do not rely on either" in meta["verdict"]
    assert "Neither column is trustworthy" in paths["report_path"].read_text()
    assert any("fails its own checks" in w for w in meta["warnings"])


def test_confidence_that_only_knows_how_often_a_field_is_seen_is_exposed_by_the_checks():
    """Loam fields near the county mean are seen bare often: confidence then 'predicts' error without knowing anything."""
    rng = np.random.default_rng(12)
    rows, labels = [], []
    for f in range(200):
        sandy = f % 4 == 0
        label = 0.8 if sandy else 1.8
        seen = rng.uniform(0.05, 0.12) if sandy else rng.uniform(0.15, 0.27)  # sands are seen bare less often
        for y in YEARS:
            pred = (1.45 if sandy else 1.65) + rng.normal(0, 0.15)
            rows.append({"field_id": f"KS-{f:06d}", "year": y, "om_pred": pred, "n_obs": 40 * seen, "confidence": seen + rng.normal(0, 0.005), "bare_days": 50.0})
            labels.append(label)
    table = pd.DataFrame(rows)
    crops = table[["field_id", "year"]].assign(crop="corn", prev_crop="corn")
    chk = build.confidence_check(table, np.array(labels), crops)
    assert chk["spearman_confidence_vs_abs_error"] < -0.3  # looks like a working accuracy score ...
    assert chk["spearman_confidence_vs_abs_error_constant_guess"] < -0.3  # ... but so does it for a constant guess
    assert abs(chk["spearman_confidence_vs_abs_error_within_field"]) < 0.1  # and it cannot rank a field's years
    assert abs(chk["spearman_confidence_vs_abs_error_upper_heap"]) < 0.1 and chk["n_rows_lower_heap"] == 50 * len(YEARS)
    assert chk["spearman_confidence_vs_n_obs"] > 0.9
    assert chk["bins"][0]["median_abs_error_constant_guess"] > chk["bins"][-1]["median_abs_error_constant_guess"]


def test_cli_prints_a_short_summary_and_fails_cleanly(tmp_path, capsys, monkeypatch):
    fields, mapunits, obs, fields_path, obs_path = write_inputs(tmp_path)
    monkeypatch.setattr(build, "field_frame", lambda sub, mu, cache=None, _real=build.field_frame: _real(sub, mapunits))
    args = ["--obs", str(obs_path), "--fields", str(fields_path), "--out", str(tmp_path / "h.parquet"), "--meta", str(tmp_path / "m.json"),
            "--report", str(tmp_path / "r.md"), "--satellite", str(tmp_path / "none.parquet"), "--log", str(tmp_path / "b.log")]  # fmt: skip
    assert build.main(args) == 0
    out = capsys.readouterr().out
    assert 10 <= len(out.strip().splitlines()) <= 20
    assert "grouped by map unit" in out and "random K-fold" in out and "verdict:" in out
    assert health_schema.main([str(tmp_path / "h.parquet")]) == 0
    capsys.readouterr()
    failed_log = tmp_path / "failed.log"
    assert build.main(["--obs", str(tmp_path / "missing.parquet"), "--fields", str(fields_path), "--log", str(failed_log)]) == 1
    assert "does not exist" in capsys.readouterr().out
    assert "FAILED: BuildError" in failed_log.read_text()  # the log is written even when the build fails


# --------------------------------------------------------------------------- SSURGO map units, cache only


class CacheFiller:
    """Answers the tile and attribute queries of ssurgo.py for two map units side by side."""

    def __init__(self):
        self.queries = 0
        self.stats = {"queries": 0, "http_requests": 0, "retries": 0}
        self.url = "fake"

    def query(self, sql, what=""):
        self.queries += 1
        if "FROM mapunit" in sql:
            cols = ssurgo.ATTR_COLUMNS
            rows = [
                ["101", "A", "Alpha loam", "Consociation", "KS055", "c1", "Alpha", "Series", "Yes", "h1", "Ap", "A", "100", "0", "15", "1.0"],
                ["202", "B", "Beta-Gamma complex", "Complex", "KS055", "c2", "Beta", "Series", "Yes", "h2", "A", "A", "60", "0", "20", "3.0"],
                ["202", "B", "Beta-Gamma complex", "Complex", "KS055", "c3", "Gamma", "Series", "Yes", "h3", "A", "A", "40", "0", "20", "3.0"],
            ]
            order = ssurgo.ATTR_STR_COLUMNS + ssurgo.ATTR_NUM_COLUMNS
            return pd.DataFrame(rows, columns=order)[cols]
        west, east = box(-101.0, 37.8, -100.85, 38.0), box(-100.85, 37.8, -100.7, 38.0)
        return pd.DataFrame([["p1", "101", "KS055", "A", west.wkt], ["p2", "202", "KS055", "B", east.wkt]], columns=["mupolygonkey", "mukey", "areasymbol", "musym", "wkt"])


def test_field_mapunits_gives_the_dominant_map_unit_from_the_cache_alone(tmp_path):
    fields = gpd.GeoDataFrame(
        {"field_id": ["KS-000001", "KS-000002", "KS-000003"]},
        geometry=[box(-100.95, 37.90, -100.93, 37.92), box(-100.80, 37.90, -100.78, 37.92), box(-100.86, 37.90, -100.82, 37.92)],
        crs="EPSG:4326",
    )
    with pytest.raises(ssurgo.SoilDataError, match="offline"):
        ssurgo.field_mapunits(fields, cache_dir=tmp_path)  # nothing cached and no network allowed
    filler = CacheFiller()
    first = ssurgo.field_mapunits(fields, cache_dir=tmp_path, client=filler)
    assert filler.queries > 0
    again = ssurgo.field_mapunits(fields, cache_dir=tmp_path)  # offline by default: served from the cache
    pd.testing.assert_frame_equal(first, again)
    assert again["dominant_mukey"].tolist() == ["101", "202", "202"]
    assert again["dominant_share"].tolist() == pytest.approx([1.0, 1.0, 0.75], abs=0.01)
    assert again["n_mapunits"].tolist() == [1, 1, 2]
    assert again["dominant_muname"].tolist() == ["Alpha loam", "Beta-Gamma complex", "Beta-Gamma complex"]
    assert again["dominant_series"].tolist() == ["Alpha", "Beta", "Beta"]  # the largest component names the series
    assert again["dominant_om_pct"].tolist() == [1.0, 3.0, 3.0]
    assert again["organic_matter_pct"].tolist() == pytest.approx([1.0, 3.0, 2.5], abs=0.01)
