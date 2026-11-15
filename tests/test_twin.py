"""Tests for api/twin.py (GET /field/{id}/twin), on made-up Phase 2B files.

Nothing under data/ is read or written: effects.json, pairs.parquet, causal_fields.parquet and the soil
table are written to a temp folder with numbers chosen so every count and sign can be checked by hand.
One last test reads the real files and skips without them.
"""
from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient  # noqa: E402

import api.causal as api_causal  # noqa: E402
from api.main import create_app  # noqa: E402
from api.twin import rotation_label  # noqa: E402

YEARS = list(range(2016, 2025))
CAL = {"max_distance_km": 5.0, "max_slope_diff": 0.5, "max_ssurgo_om_diff": 0.3, "max_baseline_diff_sd": 0.5, "max_history_distance": 0.25}
FORBIDDEN = re.compile(r"\b(caus\w*|improv\w*|soil health|profit|income)\b", re.I)


def _pair(i, comp, a, b, fa, fb, score, did, **extra):
    row = {
        "pair_id": f"P{i:05d}", "comparison": comp, "rotation_a": a, "rotation_b": b, "field_a": fa, "field_b": fb,
        "irrigation": "dryland", "soil_group": "mid OM", "distance_km": 1.0, "slope_diff": 0.1, "ssurgo_om_diff": 0.05,
        "baseline_diff": 5.0, "history_distance": 0.1, "match_score": score,
        "pre_a": 100.0, "post_a": 100.0 + did, "pre_b": 100.0, "post_b": 100.0, "n_pre": 2, "n_post": 5, "did": did,
        "did_crops": did / 2, "did_beyond": did / 2, "rotation_pre_a": a, "baseline_a": 100.0, "rotation_pre_b": b, "baseline_b": 100.0,
    }
    row.update(extra)
    return row


@pytest.fixture()
def made_up(tmp_path, monkeypatch):
    comp = "alfalfa/perennial vs diverse"
    small = "corn-soy vs continuous corn"
    rows = []
    # F1 is alfalfa (A) in two pairs; the closer one (score 1.0, P2) is its twin F3. Its did is -130.
    rows.append(_pair(1, comp, "alfalfa/perennial", "diverse", "F1", "F2", 2.0, -10.0))
    rows.append(_pair(2, comp, "alfalfa/perennial", "diverse", "F1", "F3", 1.0, -130.0, pre_a=200.0, post_a=170.0, pre_b=50.0, post_b=150.0, did_crops=-12.0, did_beyond=-118.0))
    # ten more pairs: 5 negative, 4 positive, 1 zero. With P1, P2 and P50 the comparison has 13 pairs.
    others = [-5.0, -8.0, -20.0, -1.0, -50.0, 5.0, 8.0, 20.0, 1.0, 0.0]
    for k, d in enumerate(others):
        rows.append(_pair(10 + k, comp, "alfalfa/perennial", "diverse", f"A{k}", f"B{k}", 3.0 + k / 10, d))
    # F5 is the B side (diverse) of P50 and has one pair only
    rows.append(_pair(50, comp, "alfalfa/perennial", "diverse", "F4", "F5", 4.0, 12.0, pre_a=100.0, post_a=130.0, pre_b=100.0, post_b=118.0))
    # a comparison with too few pairs for an estimate
    for k in range(3):
        rows.append(_pair(60 + k, small, "corn-soy", "continuous corn", f"C{k}", f"D{k}", 2.0, 4.0 if k else -4.0))
    pairs = pd.DataFrame(rows)
    pairs.to_parquet(tmp_path / "pairs.parquet")

    ids = ["F1", "F2", "F3", "F4", "F5", "F6"] + [f"{p}{k}" for k in range(10) for p in "AB"] + [f"{p}{k}" for k in range(3) for p in "CD"]
    fields = pd.DataFrame(
        {
            "field_id": ids, "irrigation": "dryland", "irrigation_reason": "test", "circle_fill": 0.1,
            "rotation_pre": "diverse", "rotation_post": "diverse", "soil_group": "mid OM",
            "eligible": True, "n_baseline": 2, "n_pre": 2, "n_post": 5,
            "baseline": np.linspace(60, 140, len(ids)), "pre": 100.0, "post": 100.0,
        }
    )
    fields.loc[fields.field_id == "F1", "rotation_post"] = "alfalfa/perennial"
    fields.loc[fields.field_id == "F5", "rotation_post"] = "diverse"
    fields.to_parquet(tmp_path / "causal_fields.parquet")

    soil_rows = []
    for fid in ("F1", "F3", "F5"):
        for y in YEARS:
            v = float(100 + y - 2016 + (7 if fid == "F3" else 0))
            if (fid, y) == ("F1", 2018):
                continue  # no row at all
            if (fid, y) == ("F3", 2021):
                v = float("nan")  # a row without a reading
            soil_rows.append({"field_id": fid, "year": y, "om_pred": 1.0, "n_obs": 5.0, "confidence": 0.5, "bare_days": v})
    pd.DataFrame(soil_rows).to_parquet(tmp_path / "soil_health.parquet")

    def row(a, b, comp_name, effect, lo, hi, n):
        return {
            "a": a, "b": b, "comparison": comp_name, "group_kind": "irrigation", "group": "all", "effect": effect,
            "ci_low": lo, "ci_high": hi, "n_pairs": n, "p_value": 0.01, "ci_excludes_zero": lo > 0 or hi < 0,
            "favours": "a", "from_crops": {"effect": -9.0, "ci_low": -12.0, "ci_high": -6.0, "ci_excludes_zero": True},
            "beyond_crops": {"effect": 1.0, "ci_low": -3.0, "ci_high": 5.0, "ci_excludes_zero": False},
        }

    effects = {
        "is_fake": False, "warning": None, "sources": {"soil": "soil_health.parquet"},
        "outcome": {"column": "bare_days", "label": "Days the soil sits bare each year", "units": "days per year", "higher_is_better": False},
        "design": {
            "switch_year": 2020, "history_years": [2008, 2019], "rotation_years": [2020, 2024], "soil_years": [2016, 2024],
            "baseline_years": [2016, 2017], "pre_years": [2018, 2019], "post_years": [2020, 2024], "calipers": CAL, "min_pairs": 10,
        },
        "effects": [row("alfalfa/perennial", "diverse", comp, -16.0, -30.0, -2.0, 13)],
        "placebos": {
            "fake_switch_years": [
                {"fake_switch_year": 2017, "comparison": comp, "effect": -10.0, "ci_low": -18.0, "ci_high": -3.0, "ci_excludes_zero": True},
                {"fake_switch_year": 2018, "comparison": comp, "effect": -1.0, "ci_low": -9.0, "ci_high": 5.0, "ci_excludes_zero": False},
                {"fake_switch_year": 2017, "comparison": small, "effect": 1.0, "ci_low": -9.0, "ci_high": 5.0, "ci_excludes_zero": False},
            ],
            "shuffled_labels": [{"comparison": comp, "real_effect": -16.0, "null_mean": 0.0, "null_sd": 2.0, "null_p95_abs": 4.0, "p_value": 0.005, "n_shuffles": 200}],
        },
        "demo_pairs": [{"field_a": "F1"}],
    }
    (tmp_path / "effects.json").write_text(json.dumps(effects))
    monkeypatch.setattr(api_causal, "EFFECTS_PATH", tmp_path / "effects.json")
    monkeypatch.setattr(api_causal, "PAIRS_PATH", tmp_path / "pairs.parquet")
    monkeypatch.setattr(api_causal, "CAUSAL_FIELDS_PATH", tmp_path / "causal_fields.parquet")

    app = create_app()
    history = {str(y): ("alfalfa" if y > 2019 else "corn") for y in range(2008, 2025)}
    app.state.store = SimpleNamespace(records={i: SimpleNamespace(crop_history=dict(history)) for i in ids + ["F_UNMATCHED"]})
    return TestClient(app), tmp_path


def _fields_without_pair(client):
    return client.get("/field/F_UNMATCHED/twin").json()


def test_twin_is_the_pair_with_the_smallest_match_score(made_up):
    client, _ = made_up
    body = client.get("/field/F1/twin").json()
    assert body["has_twin"] and body["twin"]["field_id"] == "F3" and body["match"]["pair_id"] == "P00002"
    assert body["match"]["pairs_of_this_field"] == 2
    assert body["match"]["closeness"]["score"] == 1.0
    assert body["match"]["closeness"]["closer_than_pct"] == round(100 * 15 / 16)  # 16 pairs in the run, 15 have a larger score


def test_what_they_were_matched_on_and_how_close(made_up):
    client, _ = made_up
    items = {m["key"]: m for m in client.get("/field/F1/twin").json()["match"]["matched_on"]}
    assert set(items) == {"irrigation", "distance", "slope", "mapped_soil", "baseline", "crop_mix"}
    assert items["distance"]["value"] == 1.0 and items["distance"]["limit"] == 5.0
    assert items["slope"]["limit"] == 0.5 and items["mapped_soil"]["value"] == 0.05
    assert items["crop_mix"]["value"] == pytest.approx(0.1)
    # the made-up match score does not follow from the made-up baseline spread, so the baseline limit is not quoted
    assert items["baseline"]["limit"] is None and "limit" not in items["baseline"]["text"]


def test_series_keep_gaps_and_crop_histories_run_to_2024(made_up):
    client, _ = made_up
    body = client.get("/field/F1/twin").json()
    this = {p["year"]: p["value"] for p in body["this"]["bare_days"]}
    assert list(this) == YEARS and this[2018] is None and this[2016] == 100.0
    twin = {p["year"]: p["value"] for p in body["twin"]["bare_days"]}
    assert twin[2021] is None and twin[2022] == 113.0
    for side in ("this", "twin"):
        assert sorted(body[side]["crop_history"]) == [str(y) for y in range(2008, 2025)]


def test_measured_difference_before_and_after(made_up):
    client, _ = made_up
    m = client.get("/field/F1/twin").json()["measured"]
    assert m["this"] == {"before": 200.0, "after": 170.0, "change": -30.0}
    assert m["twin"] == {"before": 50.0, "after": 150.0, "change": 100.0}
    assert m["difference_in_change"] == -130.0
    assert m["level_gap_after"] == 20.0
    assert m["from_crops"] == -12.0 and m["beyond_crops"] == -118.0
    assert m["years_used"] == {"before": 2, "after": 5}
    assert "noisy" in m["sentence"]


def test_comparison_estimate_and_agreement_counts(made_up):
    client, _ = made_up
    c = client.get("/field/F1/twin").json()["comparison"]
    assert c["name"] == "alfalfa/perennial vs diverse" and c["this_is"] == "a" and c["n_pairs"] == 13
    assert c["estimate"] == {"effect": -16.0, "ci_low": -30.0, "ci_high": -2.0, "ci_excludes_zero": True}
    ag = c["agreement"]
    # this pair is negative. The 12 other pairs: P1 (-10) and five more negative = 6 the same way; 4 positive and P50 (+12) the other way; one zero
    assert ag["other_pairs"] == 12
    assert ag["same_way"] == 6 and ag["opposite_way"] == 4 + 1 and ag["no_change"] == 1
    assert ag["same_way"] + ag["opposite_way"] + ag["no_change"] == ag["other_pairs"]
    assert ag["this_pair_direction"] == "fewer"
    assert ag["sentence"].startswith("6 of 12 other pairs point the same way")
    assert ag["spread_of_other_pairs"]["n"] == 12


def test_placebo_for_the_comparison(made_up):
    client, _ = made_up
    p = client.get("/field/F1/twin").json()["comparison"]["placebo"]
    assert p["fake_switch"]["n"] == 2 and p["fake_switch"]["n_failed"] == 1 and p["passed"] is False
    assert p["county"] == {"comparisons_tested": 2, "comparisons_failed": 1}
    assert p["shuffled_labels"]["p_value"] == 0.005
    assert "fails it" in p["sentence"] and "2017 or 2018" in p["sentence"]


def test_seen_from_the_other_side_the_signs_flip_but_the_counts_do_not(made_up):
    client, _ = made_up
    body = client.get("/field/F5/twin").json()  # the diverse (B) side of P00050, did +12 seen from A
    assert body["twin"]["field_id"] == "F4" and body["comparison"]["this_is"] == "b"
    assert body["measured"]["difference_in_change"] == -12.0
    assert body["measured"]["level_gap_after"] == -12.0
    est = body["comparison"]["estimate"]
    assert est == {"effect": 16.0, "ci_low": 2.0, "ci_high": 30.0, "ci_excludes_zero": True}
    assert body["comparison"]["as_stored"]["effect"] == -16.0  # the stored row is untouched
    ag = body["comparison"]["agreement"]
    # pair did is +12 as stored: the positives among the other 12 are 5 8 20 1 (4); seven are negative, one is zero
    assert ag["other_pairs"] == 12 and ag["same_way"] == 4 and ag["no_change"] == 1 and ag["opposite_way"] == 7
    assert ag["this_pair_direction"] == "fewer"  # this field (B) minus twin is -12


def test_a_small_comparison_has_no_estimate(made_up):
    client, _ = made_up
    body = client.get("/field/C1/twin").json()
    c = body["comparison"]
    assert c["n_pairs"] == 3 and c["estimate"] is None and c["as_stored"] is None
    assert "fewer than 10" in c["sentence"]
    assert c["agreement"]["other_pairs"] == 2 and c["agreement"]["same_way"] == 1


def test_a_field_without_a_match_gets_a_plain_answer(made_up):
    client, _ = made_up
    res = client.get("/field/F_UNMATCHED/twin")
    assert res.status_code == 200
    body = res.json()
    assert body["has_twin"] is False and body["twin"] is None and body["match"] is None and body["comparison"] is None
    assert body["reason"] and body["examples"] == ["F1"]
    assert len(body["this"]["bare_days"]) == 9 and all(p["value"] is None for p in body["this"]["bare_days"])


def test_unknown_field_is_404_and_missing_files_are_503(made_up, monkeypatch, tmp_path):
    client, _ = made_up
    assert client.get("/field/NOPE/twin").status_code == 404
    monkeypatch.setattr(api_causal, "EFFECTS_PATH", tmp_path / "missing.json")
    res = client.get("/field/F1/twin")
    assert res.status_code == 503 and "make causal" in res.json()["detail"]


def test_no_sentence_says_a_rotation_caused_or_improved_anything(made_up):
    client, _ = made_up
    for fid in ("F1", "F5", "C1", "F_UNMATCHED"):
        body = client.get(f"/field/{fid}/twin").json()
        texts = []  # `reason` is api/causal.py's own wording for an unmatched field
        if body["has_twin"]:
            c = body["comparison"]
            texts += [body["measured"]["sentence"], body["match"]["sentence"], c["sentence"], c["agreement"]["sentence"], c["placebo"]["sentence"]]
            texts += [m["text"] for m in body["match"]["matched_on"]]
        for t in texts:
            assert not FORBIDDEN.search(t), t


def test_rotation_labels():
    assert rotation_label("diverse") == "mixed (3+ crops)"
    assert rotation_label("alfalfa/perennial") == "alfalfa / perennial"
    assert rotation_label(None) == "unknown"


def test_real_files(monkeypatch):
    for name in ("effects.json", "pairs.parquet", "causal_fields.parquet", "fields.parquet", "soil_health.parquet"):
        if not (ROOT / "data" / name).is_file():
            pytest.skip(f"data/{name} not built")
    pairs = pd.read_parquet(ROOT / "data" / "pairs.parquet")
    with TestClient(create_app()) as client:
        # every field in a pair answers, and its twin is the closest of its pairs
        for fid in list(pairs["field_a"].head(3)) + list(pairs["field_b"].head(3)):
            body = client.get(f"/field/{fid}/twin").json()
            mine = pairs[(pairs.field_a == fid) | (pairs.field_b == fid)]
            assert body["match"]["closeness"]["score"] == pytest.approx(mine["match_score"].min(), abs=0.01)
            n = body["comparison"]["n_pairs"]
            assert n == int((pairs["comparison"] == body["comparison"]["name"]).sum())
            ag = body["comparison"]["agreement"]
            assert ag["other_pairs"] == n - 1 and ag["same_way"] + ag["opposite_way"] + ag["no_change"] == n - 1
            assert [p["year"] for p in body["this"]["bare_days"]] == YEARS


def test_a_zero_difference_is_never_sent_as_minus_zero():
    import json

    from api.twin import _flip, _unsigned_zero

    assert json.dumps([_flip(0.0), _unsigned_zero(-0.0), _flip(None), _flip(2.5)]) == "[0.0, 0.0, null, -2.5]"


def test_sentences_use_no_project_jargon(made_up):
    client, _ = made_up
    for fid in ("F5", "F4"):
        body = client.get(f"/field/{fid}/twin").json()
        text = " ".join([body["measured"]["sentence"], body["comparison"]["sentence"], body["comparison"]["placebo"]["sentence"]])
        assert "Phase 2B" not in text and "of that" not in text
