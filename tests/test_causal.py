"""Tests for pipeline/causal (Phase 2B) and api/causal.py, on small synthetic data.

No network and no real data, and nothing under data/ is written: every output path is pointed at a
temp folder. The end-to-end test builds a grid of look-alike corn fields where half switch to
alfalfa in the switch year (2020) and half stay in corn, makes a fake soil table in the REAL
contract with a planted crop effect, and checks that the run finds it (for bare_days, the default,
and for om_pred), that the placebos come out null, and that the API serves the results.
"""
from __future__ import annotations

import json
import shutil
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
from pipeline.soil.health_schema import COLUMNS, YEARS, check_soil_health  # noqa: E402

SWITCH = 2020  # the default; the tests below are written for it
H_FIRST, H_LAST = 2008, SWITCH - 1  # crop history years
N_HISTORY = H_LAST - H_FIRST + 1
N_POST = 2024 - SWITCH + 1
# files `make causal` and the fallback write; no test may touch them
REAL_OUTPUTS = [ROOT / "data" / name for name in (
    "effects.json", "pairs.parquet", "causal_fields.parquet", "soil_health.parquet",
    "soil_health_fake.parquet", "soil_health_fake_truth.parquet",
)]


def _stamps() -> dict[str, int | None]:
    return {p.name: (p.stat().st_mtime_ns if p.exists() else None) for p in REAL_OUTPUTS}


def test_defaults_are_the_documented_design():
    assert config.SWITCH_YEAR == SWITCH
    assert config.history_years() == (2008, 2019) and config.rotation_years() == (2020, 2024)
    assert config.baseline_years() == (2016, 2017) and config.pre_years() == (2018, 2019)
    assert config.post_years() == (2020, 2024) and config.placebo_switch_years() == [2017, 2018, 2019]
    assert (config.SOIL_FIRST_YEAR, config.LAST_YEAR) == (min(YEARS), max(YEARS))


def test_every_year_range_follows_the_switch_year(monkeypatch):
    monkeypatch.setattr(config, "SWITCH_YEAR", 2019)
    assert config.history_years() == (2008, 2018) and config.rotation_years() == (2019, 2024)
    assert config.baseline_years() == (2016, 2016) and config.pre_years() == (2017, 2018)
    assert config.post_years() == (2019, 2024) and config.placebo_switch_years() == [2017, 2018]
    assert config.span(config.baseline_years()) == "2016" and config.span(config.pre_years()) == "2017-2018"
    for bad in (2016, 2017, 2025):  # no soil year before it / only one / none from it on
        monkeypatch.setattr(config, "SWITCH_YEAR", bad)
        with pytest.raises(ValueError, match="soil years"):
            config.pre_years()


def hist(crops: list[str], start: int) -> dict[str, str]:
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
        (["sorghum"] * 7 + ["fallow", "winter wheat"], "other"),  # one wheat year in nine is not the system
        (["corn", "winter wheat", "sorghum"] * 3, "diverse"),
        (["corn"] * 4 + ["winter wheat"] * 4 + ["sorghum"], "other"),  # third crop only one year in nine
        (["alfalfa"] * 4 + ["corn"] * 5, "alfalfa/perennial"),
        (["corn"] * 5 + ["winter wheat"] * 4, "other"),
        (["corn"] * 3, "other"),  # too few years on record
    ],
)
def test_rotation_types_nine_year_window(crops, expected):
    assert rotation_type(hist(crops, 2016), 2016, 2024) == expected


WSF = ["winter wheat", "sorghum", "fallow"]


@pytest.mark.parametrize(
    "crops, expected",
    [
        (["corn"] * 5, "continuous corn"),
        (["corn"] * 4 + ["sorghum"], "continuous corn"),
        (["corn", "soybeans", "corn", "soybeans", "corn"], "corn-soy"),
        # all three phases of the 3-year cycle are the same rotation, whichever year the window starts
        ((WSF * 2)[0:5], "wheat-sorghum-fallow"),
        ((WSF * 2)[1:6], "wheat-sorghum-fallow"),
        ((WSF * 3)[2:7], "wheat-sorghum-fallow"),
        (["winter wheat", "fallow", "winter wheat", "fallow", "winter wheat"], "wheat-sorghum-fallow"),
        (["sorghum", "fallow", "sorghum", "fallow", "sorghum"], "other"),  # no wheat at all
        (["corn", "corn", "corn", "winter wheat", "sorghum"], "diverse"),
        (["corn", "corn", "corn", "winter wheat", "winter wheat"], "other"),  # two crops only
        (["alfalfa", "alfalfa", "corn", "corn", "corn"], "alfalfa/perennial"),
        (["corn"] * 4, "other"),  # a year missing from the 5-year window
    ],
)
def test_rotation_types_five_year_window(crops, expected):
    assert rotation_type(hist(crops, SWITCH), SWITCH, 2024) == expected


def test_one_grass_year_is_not_perennial():
    # the 2024 CDL over-maps grass/pasture; a single year must not make a field "perennial"
    assert rotation_type(hist(["corn"] * 4 + ["grass/pasture"], SWITCH), SWITCH, 2024) == "continuous corn"
    assert rotation_type(hist(["corn"] * 8 + ["grass/pasture"], 2016), 2016, 2024) == "continuous corn"


def test_crop_mix_and_comparison_order():
    mix = crop_mix(hist(["corn", "corn", "fallow", "cotton"], H_FIRST), H_FIRST, H_LAST)
    assert pytest.approx(sum(mix)) == 1.0
    assert mix[0] == 0.5  # corn
    assert comparison_key("continuous corn", "alfalfa/perennial") == ("alfalfa/perennial", "continuous corn")


# --------------------------------------------------------------------------------------
# Irrigation
# --------------------------------------------------------------------------------------


def test_irrigation_rules():
    pre_corn = hist(["corn"] * N_HISTORY, H_FIRST)
    pre_wsf = hist(WSF * (N_HISTORY // 3), H_FIRST)
    pre_mixed = hist(["corn", "winter wheat", "sorghum", "winter wheat"] * (N_HISTORY // 4), H_FIRST)
    assert irrigation.classify(0.9, pre_mixed, H_FIRST, H_LAST) == ("irrigated", "pivot circle")
    assert irrigation.classify(0.6, pre_wsf, H_FIRST, H_LAST) == ("dryland", "fallow in history")
    assert irrigation.classify(0.6, pre_corn, H_FIRST, H_LAST) == ("irrigated", "irrigated crops")
    assert irrigation.classify(0.6, pre_mixed, H_FIRST, H_LAST)[0] == "uncertain"


def test_circle_fill_tells_a_pivot_from_a_square():
    fill = irrigation.circle_fill(np.array([Point(0, 0).buffer(400, 64), box(0, 0, 800, 800)], dtype=object))
    assert fill[0] > 0.95
    assert abs(fill[1] - 2 / np.pi) < 0.01


# --------------------------------------------------------------------------------------
# Estimation helpers
# --------------------------------------------------------------------------------------


SOIL_YEARS = np.arange(min(YEARS), max(YEARS) + 1)


def test_field_windows_use_the_configured_years():
    values = np.where(SOIL_YEARS < 2018, 1.0, np.where(SOIL_YEARS < SWITCH, 2.0, 5.0))[None, :].astype(float)
    win = did.field_windows(values, np.ones_like(values), SOIL_YEARS)
    assert win["baseline"][0] == 1.0 and win["pre"][0] == 2.0 and win["post"][0] == 5.0
    assert win["n_baseline"][0] == 2 and win["n_pre"][0] == 2 and win["n_post"][0] == 5


def test_pairs_are_compared_only_in_years_both_fields_have():
    #                 2016 2017 2018 2019 | 2020 2021 2022 2023 2024
    a = np.array([10.0, 10, 10, 10, 20, 20, 20, 200, 20])  # 2023: a dry year lifts everyone
    b = np.array([10.0, 10, 10, 10, 10, 10, 10, np.nan, 10])  # ... but B was not seen that year
    values = np.vstack([a, b])
    ones = np.ones_like(values)
    win = did.pair_windows(values, ones, SOIL_YEARS, np.array([0]), np.array([1]))
    assert win["n_pre"][0] == 2 and win["n_post"][0] == 4  # 2023 dropped for both
    assert win["post_a"][0] == 20 and win["did"][0] == 10
    # swapping the two fields flips the sign and nothing else
    assert did.pair_windows(values, ones, SOIL_YEARS, np.array([1]), np.array([0]))["did"][0] == -10
    # too few shared years after the switch -> no estimate for the pair
    values[1, SOIL_YEARS >= 2021] = np.nan
    assert np.isnan(did.pair_windows(values, ones, SOIL_YEARS, np.array([0]), np.array([1]))["did"][0])


def test_placebo_uses_only_years_before_the_switch():
    a = np.array([10.0, 10, 30, 30, 999, 999, 999, 999, 999])
    b = np.array([10.0, 10, 10, 10, 0, 0, 0, 0, 0])
    values = np.vstack([a, b])
    ones = np.ones_like(values)
    rows = (np.array([0]), np.array([1]))
    assert did.placebo_did(values, ones, SOIL_YEARS, *rows, 2018)[0] == 20  # 2018-2019 against 2016-2017
    assert did.placebo_did(values, ones, SOIL_YEARS, *rows, 2017)[0] == pytest.approx(40 / 3)
    assert did.placebo_did(values, ones, SOIL_YEARS, *rows, 2019)[0] == pytest.approx(20 - 20 / 3)


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
    default = config.outcome()
    assert default.column == "bare_days" and not default.higher_is_better
    assert default.units == "days per year" and default.weight_column is None and default.note
    monkeypatch.setenv("CAUSAL_OUTCOME", "om_pred")
    assert config.outcome().column == "om_pred" and config.outcome().higher_is_better
    assert config.outcome().weight_column == "confidence"
    other = config.outcome("my_index")
    assert other.column == "my_index" and other.weight_column is None


def test_favours_respects_the_direction_of_the_outcome():
    assert run_causal.favours(-12.0, True, higher_is_better=False) == "a"  # fewer bare days under A
    assert run_causal.favours(-12.0, True, higher_is_better=True) == "b"
    assert run_causal.favours(+12.0, True, higher_is_better=False) == "b"
    assert run_causal.favours(-12.0, False, higher_is_better=False) is None  # CI includes 0: no call


def test_load_soil_weights_and_missing_column(tmp_path):
    path = tmp_path / "soil_health.parquet"
    pd.DataFrame(
        {
            "field_id": ["KS-000001"] * 3,
            "year": [2016, 2017, 2018],
            "om_pred": [1.2, np.nan, 1.3],
            "confidence": [0.3, 0.0, 0.01],
            "bare_days": [40.0, 0.0, np.nan],
        }
    ).to_parquet(path)
    # bare_days: confidence describes om_pred, so it must not weight anything else;
    # the alfalfa-like year (om_pred null, bare_days 0) counts in full
    bare = soil_data.load_soil(path, "bare_days", config.outcome("bare_days").weight_column)
    assert bare["year"].tolist() == [2016, 2017] and bare["weight"].tolist() == [1.0, 1.0]
    om = soil_data.load_soil(path, "om_pred", config.outcome("om_pred").weight_column)
    assert om["year"].tolist() == [2016, 2018] and om["weight"].tolist() == [0.3, 0.05]
    with pytest.raises(ValueError, match="my_index"):
        soil_data.load_soil(path, "my_index")


def test_crop_expected_is_the_county_average_for_that_years_crop():
    fields = pd.DataFrame({"crop_history": [json.dumps({"2016": c, "2017": "corn"}) for c in ("corn", "corn", "fallow", "fallow")]})
    years = np.array([2016, 2017])
    values = np.array([[40.0, 50], [60.0, 70], [140.0, np.nan], [160.0, 90]])
    expected = run_causal.crop_expected(fields, values, years, min_cell=1)
    assert expected[:, 0].tolist() == [50.0, 50.0, 150.0, 150.0]
    assert expected[0, 1] == 70.0 and np.isnan(expected[2, 1])  # same cells as the data
    # a crop seen on too few fields falls back to the year's average
    assert run_causal.crop_expected(fields, values, years, min_cell=3)[:, 0].tolist() == [100.0] * 4


# --------------------------------------------------------------------------------------
# End to end on a synthetic grid
# --------------------------------------------------------------------------------------


def grid_fields(rows: int = 14, cols: int = 14) -> gpd.GeoDataFrame:
    """Look-alike irrigated fields (corn until the switch); checkerboard: alfalfa or corn after it."""
    recs = []
    for r in range(rows):
        for c in range(cols):
            x0, y0 = -100.9 + c * 0.006, 37.9 + r * 0.005
            post = ["alfalfa"] * N_POST if (r + c) % 2 else ["corn"] * N_POST
            recs.append(
                {
                    "field_id": f"KS-{r * cols + c + 1:06d}",
                    "geometry": box(x0, y0, x0 + 0.005, y0 + 0.004),
                    "crop_history": json.dumps({**hist(["corn"] * N_HISTORY, H_FIRST), **hist(post, SWITCH)}),
                    "organic_matter_pct": 1.8,
                    "slope_pct": 1.0,
                    "area_ha": 40.0,
                }
            )
    return gpd.GeoDataFrame(recs, geometry="geometry", crs="EPSG:4326")


def test_fake_soil_follows_the_real_contract():
    fields = grid_fields(4, 4)
    soil, truth = make_fake_soil.build(fields)
    assert check_soil_health(soil) == []
    assert list(soil.columns) == COLUMNS
    assert sorted(soil["year"].unique()) == list(YEARS)
    assert len(soil) == len(fields) * len(YEARS)  # every field, every year
    never_bare = soil["om_pred"].isna()
    assert never_bare.any() and (soil.loc[never_bare, ["n_obs", "confidence"]] == 0).all().all()
    assert set(truth.columns) == {"field_id", "year", "om_pred", "bare_days"}
    # the planted crop effect: alfalfa years sit bare far less than corn years
    post = soil[soil["year"] >= SWITCH].merge(fields[["field_id", "crop_history"]], on="field_id")
    is_alfalfa = post["crop_history"].str.contains("alfalfa")
    assert post.loc[is_alfalfa, "bare_days"].mean() < post.loc[~is_alfalfa, "bare_days"].mean() - 20


def _run(tmp: Path, fields_path: Path, outcome: str | None):
    """One causal run with every input and output under `tmp` (never data/)."""
    out = tmp if outcome is None else tmp / outcome  # the API looks for the soil table next to effects.json
    out.mkdir(exist_ok=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("FIELDS_PATH", str(fields_path))
    mp.setenv("SOIL_PATH", str(tmp / "soil_health_fake.parquet"))
    mp.delenv("CAUSAL_OUTCOME", raising=False)
    mp.delenv("CAUSAL_SWITCH_YEAR", raising=False)
    for name, value in {
        "SOIL_REAL_PATH": tmp / "soil_health.parquet",
        "SOIL_FAKE_PATH": tmp / "soil_health_fake.parquet",
        "SOIL_FAKE_TRUTH_PATH": tmp / "soil_health_fake_truth.parquet",
        "EFFECTS_PATH": out / "effects.json",
        "PAIRS_PATH": out / "pairs.parquet",
        "CAUSAL_FIELDS_PATH": out / "causal_fields.parquet",
    }.items():
        mp.setattr(config, name, value)
    try:
        return out, run_causal.run(outcome, quick=True)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def causal_run(tmp_path_factory):
    before = _stamps()
    tmp = tmp_path_factory.mktemp("causal")
    fields = grid_fields()
    fields_path = tmp / "fields.parquet"
    fields.to_parquet(fields_path)
    soil, truth = make_fake_soil.build(fields)
    soil.to_parquet(tmp / "soil_health_fake.parquet", index=False)
    truth.to_parquet(tmp / "soil_health_fake_truth.parquet", index=False)
    out, result = _run(tmp, fields_path, None)
    _, om_result = _run(tmp, fields_path, "om_pred")
    yield {"out": out, "fields_path": fields_path, "result": result, "om_result": om_result, "before": before}


ALFALFA_VS_CORN = "alfalfa/perennial vs continuous corn"


def _overall(result: dict) -> dict:
    rows = [e for e in result["effects"] if e["comparison"] == ALFALFA_VS_CORN and e["group"] == "all"]
    assert len(rows) == 1
    return rows[0]


def test_tests_do_not_touch_the_real_outputs(causal_run):
    assert _stamps() == causal_run["before"]
    assert str(causal_run["out"]).startswith(str(causal_run["fields_path"].parent))


def test_run_recovers_the_planted_bare_days_effect(causal_run):
    result = causal_run["result"]
    assert result["is_fake"] and "FAKE" in result["warning"]
    assert result["outcome"]["column"] == "bare_days" and result["outcome"]["higher_is_better"] is False
    assert result["outcome"]["weight_column"] is None
    design = result["design"]
    assert design["switch_year"] == SWITCH and design["history_years"] == [2008, 2019]
    assert design["baseline_years"] == [2016, 2017] and design["pre_years"] == [2018, 2019]
    assert design["post_years"] == [2020, 2024] and design["placebo_switch_years"] == [2017, 2018, 2019]
    e = _overall(result)
    assert e["n_pairs"] >= 50
    # alfalfa sits bare far fewer days than corn: a large NEGATIVE effect, which favours A (alfalfa)
    assert e["ci_high"] < -20 and e["favours"] == "a"
    assert e["ci_low"] - 3 <= e["planted_effect"] <= e["ci_high"] + 3
    # here the whole effect is the crops themselves, and the split says so
    assert e["from_crops"]["ci_excludes_zero"] and abs(e["from_crops"]["effect"] - e["effect"]) < 5
    assert abs(e["beyond_crops"]["effect"]) < 5
    assert e["fallow_share_a"] == 0 and e["fallow_share_b"] == 0
    # the effect is a change in the plain A - B gap, and both levels are reported next to it
    assert e["gap_post"] - e["gap_pre"] == pytest.approx(e["effect"], abs=1e-3)


def test_run_recovers_the_planted_om_effect_with_confidence_weights(causal_run):
    result = causal_run["om_result"]
    assert result["outcome"]["column"] == "om_pred" and result["outcome"]["weight_column"] == "confidence"
    e = _overall(result)
    assert e["ci_low"] > 0 and e["favours"] == "a"
    assert e["ci_low"] - 0.02 <= e["planted_effect"] <= e["ci_high"] + 0.02


def test_placebos_are_null(causal_run):
    result = causal_run["result"]
    switch = result["placebos"]["fake_switch_years"]
    assert {p["fake_switch_year"] for p in switch} == {2017, 2018, 2019}
    for p in switch:
        assert p["before_years"][0] == 2016 and p["after_years"][1] == SWITCH - 1  # pre-switch years only
        assert abs(p["effect"]) < 6  # against a real effect of about -50
    shuffled = result["placebos"]["shuffled_labels"][0]
    assert abs(shuffled["null_mean"]) < abs(shuffled["real_effect"]) / 3
    assert shuffled["p_value"] <= 0.1


def test_output_files(causal_run):
    out, result = causal_run["out"], causal_run["result"]
    pairs = pd.read_parquet(out / "pairs.parquet")
    assert len(pairs) == result["counts"]["pairs"]
    assert pairs["distance_km"].max() < config.MAX_DISTANCE_KM
    assert set(pairs["rotation_a"]) == {"alfalfa/perennial"}
    # every field is used at most once per comparison
    assert not pd.concat([pairs.field_a, pairs.field_b]).duplicated().any()
    identity = (pairs.post_a - pairs.pre_a) - (pairs.post_b - pairs.pre_b)
    assert np.allclose(pairs["did"], identity) and np.allclose(pairs["did"], pairs["did_crops"] + pairs["did_beyond"])
    assert json.loads((out / "effects.json").read_text())["outcome"]["column"] == "bare_days"
    fields = pd.read_parquet(out / "causal_fields.parquet")
    assert {"n_baseline", "n_pre", "n_post", "baseline", "pre", "post", "eligible"} <= set(fields.columns)
    demo = result["demo_pairs"][0]
    assert demo["better"] == "a" and demo["did"] < 0 and demo["post_a"] < demo["post_b"]


def test_counts_only_writes_nothing(causal_run, tmp_path, monkeypatch):
    monkeypatch.setenv("FIELDS_PATH", str(causal_run["fields_path"]))
    monkeypatch.setenv("SOIL_PATH", str(causal_run["fields_path"].parent / "soil_health_fake.parquet"))
    for name in ("EFFECTS_PATH", "PAIRS_PATH", "CAUSAL_FIELDS_PATH"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    counts = run_causal.run(counts_only=True)["counts"]
    assert counts["pairs"] == causal_run["result"]["counts"]["pairs"]
    assert list(tmp_path.iterdir()) == []


def test_a_soil_table_off_contract_stops_the_run(causal_run, tmp_path, monkeypatch):
    bad = pd.read_parquet(causal_run["fields_path"].parent / "soil_health_fake.parquet").drop(columns=["bare_days"])
    bad.to_parquet(tmp_path / "soil_health_fake.parquet", index=False)
    monkeypatch.setenv("FIELDS_PATH", str(causal_run["fields_path"]))
    monkeypatch.setenv("SOIL_PATH", str(tmp_path / "soil_health_fake.parquet"))
    for name in ("EFFECTS_PATH", "PAIRS_PATH", "CAUSAL_FIELDS_PATH"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    with pytest.raises(SystemExit, match="bare_days"):
        run_causal.run(quick=True)


def _point_api_at(out: Path, monkeypatch) -> None:
    monkeypatch.setattr(api_causal, "EFFECTS_PATH", out / "effects.json")
    monkeypatch.setattr(api_causal, "PAIRS_PATH", out / "pairs.parquet")
    monkeypatch.setattr(api_causal, "CAUSAL_FIELDS_PATH", out / "causal_fields.parquet")


def test_api_effects_and_neighbors(causal_run, monkeypatch):
    out = causal_run["out"]
    monkeypatch.setenv("FIELDS_PATH", str(causal_run["fields_path"]))
    _point_api_at(out, monkeypatch)
    pairs = pd.read_parquet(out / "pairs.parquet")
    first = pairs.iloc[0]
    with TestClient(create_app()) as client:
        assert client.get("/effects").json()["is_fake"] is True
        body = client.get(f"/field/{first['field_b']}/neighbors").json()  # the corn field of the pair
        assert body["is_fake"] and body["field"]["rotation_post"] == "continuous corn"
        assert body["outcome"]["column"] == "bare_days" and body["outcome"]["higher_is_better"] is False
        assert body["switch_year"] == SWITCH and body["design"]["history_years"] == [2008, 2019]
        assert body["design"]["pre_years"] == [2018, 2019] and body["design"]["post_years"] == [2020, 2024]
        n = body["neighbors"][0]
        assert n["field_id"] == first["field_a"] and n["rotation"] == "alfalfa/perennial"
        assert n["divergence"] == pytest.approx(-first["did"], abs=1e-3)
        # the corn field sat bare MORE days than its alfalfa look-alike afterwards: worse, although the gap is positive
        assert n["after_gap"] == pytest.approx(first["post_b"] - first["post_a"], abs=0.01)
        assert n["after_gap"] > 0 and n["verdict"] == "worse"
        assert n["before"]["this"] == pytest.approx(first["pre_b"], abs=0.01)
        assert n["after"]["neighbor"] == pytest.approx(first["post_a"], abs=0.01)
        soil_years = [p["year"] for p in n["soil"]]
        assert len(soil_years) >= 7 and set(soil_years) <= set(YEARS) and n["crop_history"]
        effect = n["comparison_effect"]
        assert effect["n_pairs"] >= 50 and effect["favours"] == "a" and "from_crops" in effect
        # and seen from the alfalfa side it is the mirror image
        mirror = client.get(f"/field/{first['field_a']}/neighbors").json()["neighbors"][0]
        assert mirror["divergence"] == pytest.approx(first["did"], abs=1e-3) and mirror["verdict"] == "better"
        assert client.get("/field/KS-999999/neighbors").status_code == 404


def test_api_verdict_follows_the_level_not_the_change(causal_run, tmp_path, monkeypatch):
    """A field whose bare days fell a lot can still sit bare more days than its look-alike (in the
    county run, KS-007027: 224 -> 158 against 53 -> 133). The verdict must then say "worse"."""
    out = causal_run["out"]
    for name in ("effects.json", "causal_fields.parquet"):
        shutil.copy(out / name, tmp_path / name)
    pairs = pd.read_parquet(out / "pairs.parquet")
    row = pairs.index[0]
    pairs.loc[row, ["pre_a", "post_a", "pre_b", "post_b"]] = [224.2, 157.9, 53.4, 133.0]
    pairs.loc[row, "did"] = (157.9 - 224.2) - (133.0 - 53.4)
    pairs.to_parquet(tmp_path / "pairs.parquet", index=False)
    monkeypatch.setenv("FIELDS_PATH", str(causal_run["fields_path"]))
    _point_api_at(tmp_path, monkeypatch)
    with TestClient(create_app()) as client:
        a = client.get(f"/field/{pairs.loc[row, 'field_a']}/neighbors").json()["neighbors"][0]
        assert a["divergence"] == pytest.approx(-145.9, abs=0.01)  # the change says "fell more" ...
        assert a["after_gap"] == pytest.approx(24.9, abs=0.01)  # ... the level says "bare more days"
        assert a["verdict"] == "worse"
        b = client.get(f"/field/{pairs.loc[row, 'field_b']}/neighbors").json()["neighbors"][0]
        assert b["divergence"] == pytest.approx(145.9, abs=0.01) and b["after_gap"] == pytest.approx(-24.9, abs=0.01)
        assert b["verdict"] == "better"


def test_api_verdict_follows_higher_is_better():
    # the argument is the "after" gap (this field minus its look-alike), never the change
    assert api_causal._verdict(-12.0, {"higher_is_better": False}) == "better"
    assert api_causal._verdict(24.9, {"higher_is_better": False}) == "worse"
    assert api_causal._verdict(-0.1, {"higher_is_better": True}) == "worse"
    assert api_causal._verdict(0.0, {"higher_is_better": False}) is None
    assert api_causal._verdict(None, {"higher_is_better": False}) is None
    assert api_causal._gap(10.0, None) is None and api_causal._gap(float("nan"), 3.0) is None


def test_demo_pairs_prefer_typical_examples():
    """A "diverse" field that is mostly fallow, or a "perennial" one with 2 grass years in 5, is a
    poor picture of its type; the demo pair is the best match among the typical ones if any exist."""
    assert run_causal.typical_example("diverse", 0.2, 0.0)
    assert not run_causal.typical_example("diverse", 0.6, 0.2)  # sorghum, fallow x3, grass/pasture
    assert run_causal.typical_example("alfalfa/perennial", 0.0, 0.8)
    assert not run_causal.typical_example("alfalfa/perennial", 0.0, 0.4)
    assert run_causal.typical_example("wheat-sorghum-fallow", 0.4, 0.0)
    base = {"rotation_a": "alfalfa/perennial", "rotation_b": "diverse", "distance_km": 1.0, "pre_a": 20.0, "post_a": 5.0,
            "pre_b": 20.0, "post_b": 40.0, "did": -35.0, "n_pre": 2, "n_post": 5}
    pairs = pd.DataFrame([
        {**base, "pair_id": "P1", "field_a": "a1", "field_b": "b1", "match_score": 0.1, "typical": False},
        {**base, "pair_id": "P2", "field_a": "a2", "field_b": "b2", "match_score": 0.5, "typical": True},
    ])
    effects = [{"a": "alfalfa/perennial", "b": "diverse", "comparison": "alfalfa/perennial vs diverse", "group": "all",
                "effect": -20.0, "ci_excludes_zero": True, "n_pairs": 2}]
    outcome = config.outcome("bare_days")
    assert [d["pair_id"] for d in run_causal.demo_pairs(pairs, effects, outcome)] == ["P2"]
    # no typical pair at all: still show the best match instead of nothing
    assert [d["pair_id"] for d in run_causal.demo_pairs(pairs.assign(typical=False), effects, outcome)] == ["P1"]


def test_api_explains_an_unmatched_field_with_the_run_years():
    design = {"switch_year": 2020, "rotation_years": [2020, 2024]}
    info = {"irrigation": "dryland", "eligible": True, "rotation_post": "other"}
    assert "2020–2024" in api_causal._why_unmatched(info, design)
    assert "2020" in api_causal._why_unmatched({**info, "eligible": False}, design)
    assert "irrigated" in api_causal._why_unmatched({**info, "irrigation": "uncertain"}, design)


def test_api_says_run_make_causal_when_results_are_missing(tmp_path, monkeypatch, causal_run):
    monkeypatch.setenv("FIELDS_PATH", str(causal_run["fields_path"]))
    monkeypatch.setattr(api_causal, "EFFECTS_PATH", tmp_path / "nope.json")
    with TestClient(create_app()) as client:
        res = client.get("/effects")
        assert res.status_code == 503 and "make causal" in res.json()["detail"]
        assert client.get("/health").status_code == 200  # the rest of the API is unaffected
