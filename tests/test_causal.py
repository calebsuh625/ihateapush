"""Tests for pipeline/causal (Phase 2B) and api/causal.py, on small synthetic data.

No network and no real data. The end-to-end test builds a grid of look-alike fields where half
switch to alfalfa in 2016 and half stay in corn, plants a known soil effect, and checks that the
run finds it, that the placebos come out null, and that the API serves the results.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from shapely.geometry import Point, box  # noqa: E402

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient  # noqa: E402

import api.causal as api_causal  # noqa: E402
from api.main import create_app  # noqa: E402
from pipeline.causal import config, did, irrigation, make_fake_soil, run_causal, soil_data  # noqa: E402
from pipeline.causal.rotations import comparison_key, crop_mix, rotation_type  # noqa: E402


def hist(crops: list[str], start: int = 2016) -> dict[str, str]:
    return {str(start + i): c for i, c in enumerate(crops)}


# --------------------------------------------------------------------------------------
# Rotation types
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crops, expected",
    [
        (["corn"] * 9, "continuous corn"),
        (["corn"] * 7 + ["sorghum"] * 2, "continuous corn"),
        (["corn", "soybeans"] * 4 + ["corn"], "corn-soy"),
        (["winter wheat", "sorghum", "fallow"] * 3, "wheat-sorghum-fallow"),
        (["winter wheat", "fallow"] * 4 + ["winter wheat"], "wheat-sorghum-fallow"),
        (["corn", "winter wheat", "sorghum"] * 3, "diverse"),
        (["alfalfa"] * 4 + ["corn"] * 5, "alfalfa/perennial"),
        (["corn"] * 5 + ["winter wheat"] * 4, "other"),
        (["corn"] * 3, "other"),  # too few years on record
    ],
)
def test_rotation_types(crops, expected):
    assert rotation_type(hist(crops), 2016, 2024) == expected


def test_one_grass_year_is_not_perennial():
    # the 2024 CDL over-maps grass/pasture; a single year must not make a field "perennial"
    assert rotation_type(hist(["corn"] * 8 + ["grass/pasture"]), 2016, 2024) == "continuous corn"


def test_crop_mix_and_comparison_order():
    mix = crop_mix(hist(["corn", "corn", "fallow", "cotton"], 2008), 2008, 2015)
    assert pytest.approx(sum(mix)) == 1.0
    assert mix[0] == 0.5  # corn
    assert comparison_key("continuous corn", "alfalfa/perennial") == ("alfalfa/perennial", "continuous corn")


# --------------------------------------------------------------------------------------
# Irrigation
# --------------------------------------------------------------------------------------


def test_irrigation_rules():
    pre_corn = hist(["corn"] * 8, 2008)
    pre_wsf = hist(["winter wheat", "sorghum", "fallow"] * 3, 2008)
    pre_mixed = hist(["corn", "winter wheat", "sorghum", "winter wheat"] * 2, 2008)
    assert irrigation.classify(0.9, pre_mixed, 2008, 2015) == ("irrigated", "pivot circle")
    assert irrigation.classify(0.6, pre_wsf, 2008, 2015) == ("dryland", "fallow in history")
    assert irrigation.classify(0.6, pre_corn, 2008, 2015) == ("irrigated", "irrigated crops")
    assert irrigation.classify(0.6, pre_mixed, 2008, 2015)[0] == "uncertain"


def test_circle_fill_tells_a_pivot_from_a_square():
    fill = irrigation.circle_fill(np.array([Point(0, 0).buffer(400, 64), box(0, 0, 800, 800)], dtype=object))
    assert fill[0] > 0.95
    assert abs(fill[1] - 2 / np.pi) < 0.01


# --------------------------------------------------------------------------------------
# Estimation helpers
# --------------------------------------------------------------------------------------


def test_field_windows_split_history_in_halves():
    years = np.arange(2008, 2025)
    values = np.where(years < 2016, np.where(years < 2012, 1.0, 2.0), 5.0)[None, :].astype(float)
    win = did.field_windows(values, np.ones_like(values), years, 2016)
    assert win["baseline"][0] == 1.0 and win["pre"][0] == 2.0 and win["post"][0] == 5.0
    assert win["n_pre"][0] == 8 and win["n_post"][0] == 9


def test_estimate_ci_covers_the_mean_and_detects_zero():
    rng = np.random.default_rng(0)
    clusters = np.repeat(np.arange(40), 5)
    real = did.estimate(0.1 + rng.normal(0, 0.05, 200), clusters, rng, 500)
    assert real["ci_low"] < real["effect"] < real["ci_high"]
    assert real["ci_low"] > 0 and real["p_value"] < 0.01
    null = did.estimate(rng.normal(0, 0.05, 200), clusters, rng, 500)
    assert null["ci_low"] < 0 < null["ci_high"]


def test_outcome_is_a_setting(monkeypatch):
    monkeypatch.delenv("CAUSAL_OUTCOME", raising=False)
    assert config.outcome().column == "om_pred"
    monkeypatch.setenv("CAUSAL_OUTCOME", "bare_soil_index")
    assert config.outcome().column == "bare_soil_index" and not config.outcome().higher_is_better
    assert config.outcome("my_index").column == "my_index"


def test_load_soil_names_a_missing_outcome_column(tmp_path):
    path = tmp_path / "soil_health.parquet"
    pd.DataFrame({"field_id": ["KS-000001"], "year": [2016], "om_pred": [1.2]}).to_parquet(path)
    assert soil_data.load_soil(path, "om_pred")["weight"].tolist() == [1.0]
    with pytest.raises(ValueError, match="bare_soil_index"):
        soil_data.load_soil(path, "bare_soil_index")


# --------------------------------------------------------------------------------------
# End to end on a synthetic grid
# --------------------------------------------------------------------------------------


def grid_fields(rows: int = 14, cols: int = 14) -> gpd.GeoDataFrame:
    """Look-alike irrigated fields (corn 2008-2015); checkerboard: alfalfa or corn from 2016."""
    recs = []
    for r in range(rows):
        for c in range(cols):
            x0, y0 = -100.9 + c * 0.006, 37.9 + r * 0.005
            post = ["alfalfa"] * 9 if (r + c) % 2 else ["corn"] * 9
            recs.append(
                {
                    "field_id": f"KS-{r * cols + c + 1:06d}",
                    "geometry": box(x0, y0, x0 + 0.005, y0 + 0.004),
                    "crop_history": json.dumps({**hist(["corn"] * 8, 2008), **hist(post)}),
                    "organic_matter_pct": 1.8,
                    "slope_pct": 1.0,
                    "area_ha": 40.0,
                }
            )
    return gpd.GeoDataFrame(recs, geometry="geometry", crs="EPSG:4326")


@pytest.fixture(scope="module")
def causal_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("causal")
    fields = grid_fields()
    fields_path = tmp / "fields.parquet"
    fields.to_parquet(fields_path)
    soil, truth = make_fake_soil.build(fields)
    soil.to_parquet(tmp / "soil_health_fake.parquet", index=False)
    truth.to_parquet(tmp / "soil_health_fake_truth.parquet", index=False)

    mp = pytest.MonkeyPatch()
    mp.setenv("FIELDS_PATH", str(fields_path))
    mp.setenv("SOIL_PATH", str(tmp / "soil_health_fake.parquet"))
    mp.delenv("CAUSAL_OUTCOME", raising=False)
    for name, value in {
        "SOIL_FAKE_TRUTH_PATH": tmp / "soil_health_fake_truth.parquet",
        "EFFECTS_PATH": tmp / "effects.json",
        "PAIRS_PATH": tmp / "pairs.parquet",
        "CAUSAL_FIELDS_PATH": tmp / "causal_fields.parquet",
    }.items():
        mp.setattr(config, name, value)
    result = run_causal.run(quick=True)
    yield tmp, fields_path, result
    mp.undo()


def test_run_recovers_the_planted_effect(causal_run):
    _, _, result = causal_run
    assert result["is_fake"] and "FAKE" in result["warning"]
    rows = [e for e in result["effects"] if e["comparison"] == "alfalfa/perennial vs continuous corn" and e["group"] == "all"]
    assert len(rows) == 1
    e = rows[0]
    assert e["n_pairs"] >= 50
    assert e["ci_low"] > 0
    assert e["ci_low"] - 0.02 <= e["planted_effect"] <= e["ci_high"] + 0.02


def test_placebos_are_null(causal_run):
    _, _, result = causal_run
    switch = result["placebos"]["fake_switch_years"]
    assert switch and all(abs(p["effect"]) < 0.03 for p in switch)
    shuffled = result["placebos"]["shuffled_labels"][0]
    assert abs(shuffled["null_mean"]) < abs(shuffled["real_effect"]) / 3
    assert shuffled["p_value"] <= 0.1


def test_output_files(causal_run):
    tmp, _, result = causal_run
    pairs = pd.read_parquet(tmp / "pairs.parquet")
    assert len(pairs) == result["counts"]["pairs"]
    assert pairs["distance_km"].max() < config.MAX_DISTANCE_KM
    assert set(pairs["rotation_a"]) == {"alfalfa/perennial"}
    # every field is used at most once per comparison
    assert not pd.concat([pairs.field_a, pairs.field_b]).duplicated().any()
    assert json.loads((tmp / "effects.json").read_text())["outcome"]["column"] == "om_pred"


def test_api_effects_and_neighbors(causal_run, monkeypatch):
    tmp, fields_path, _ = causal_run
    monkeypatch.setenv("FIELDS_PATH", str(fields_path))
    monkeypatch.setattr(api_causal, "EFFECTS_PATH", tmp / "effects.json")
    monkeypatch.setattr(api_causal, "PAIRS_PATH", tmp / "pairs.parquet")
    monkeypatch.setattr(api_causal, "CAUSAL_FIELDS_PATH", tmp / "causal_fields.parquet")
    pairs = pd.read_parquet(tmp / "pairs.parquet")
    fid = pairs.iloc[0]["field_b"]
    with TestClient(create_app()) as client:
        assert client.get("/effects").json()["is_fake"] is True
        body = client.get(f"/field/{fid}/neighbors").json()
        assert body["is_fake"] and body["field"]["rotation_post"] == "continuous corn"
        n = body["neighbors"][0]
        assert n["field_id"] == pairs.iloc[0]["field_a"] and n["rotation"] == "alfalfa/perennial"
        assert n["divergence"] == pytest.approx(-pairs.iloc[0]["did"], abs=1e-3)
        assert n["soil"] and n["crop_history"] and n["comparison_effect"]["n_pairs"] >= 50
        assert client.get("/field/KS-999999/neighbors").status_code == 404


def test_api_says_run_make_causal_when_results_are_missing(tmp_path, monkeypatch, causal_run):
    _, fields_path, _ = causal_run
    monkeypatch.setenv("FIELDS_PATH", str(fields_path))
    monkeypatch.setattr(api_causal, "EFFECTS_PATH", tmp_path / "nope.json")
    with TestClient(create_app()) as client:
        res = client.get("/effects")
        assert res.status_code == 503 and "make causal" in res.json()["detail"]
        assert client.get("/health").status_code == 200  # the rest of the API is unaffected
