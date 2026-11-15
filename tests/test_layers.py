"""Tests for api/layers.py (GET /layers, GET /layers/{name}; Phase 4, item 5).

The pure builders run on small made-up tables whose answers can be worked out by hand. The API tests
write made-up soil, traits and climate tables to tmp_path (the fields and the planner's climate tables
are the ones tests/test_api_smart.py makes), so nothing under data/ is read or written. The last tests
use the real files and skip without them.
"""
from __future__ import annotations

import gzip
import json
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

import api.main as main  # noqa: E402
from api import layers  # noqa: E402
from api.planner import erosion, smart  # noqa: E402
from api.planner.traits import FieldTraits  # noqa: E402

from test_api_smart import clean_engine, fields_file, no_tables, tables  # noqa: E402,F401  (fixtures)

DATA = ROOT / "data"
YEARS = [2016, 2017, 2018]


def half_up(value: float) -> int:
    return int(np.floor(value + 0.5))


# --- class breaks ------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, nice", [(1.2, 1), (1.6, 2), (4.1, 5), (7.9, 10), (19, 20), (38.6, 30), (44, 50),
                                         (106, 100), (160, 200), (0.014, 0.01), (0, 0), (-3, 0)])
def test_nice_number_is_the_nearest_round_number(value, nice):
    assert layers.nice_number(value) == pytest.approx(nice)


def test_breaks_come_from_the_data_and_are_round():
    values = np.arange(0, 201)                       # percentiles 40, 80, 120, 160
    assert layers.data_breaks(values) == [50, 100, 200]          # 40 -> 50, 80 -> 100, 120 -> 100 (dropped), 160 -> 200
    assert layers.data_breaks(values * 10) == [500, 1000, 2000]  # the breaks follow the data
    assert layers.data_breaks(np.array([], dtype=int)) == []
    assert layers.data_breaks(np.array([7, 7, 7])) == []         # no break leaves fields on both sides


def test_class_of_counts_the_breaks_at_or_below_the_value():
    assert layers.class_of(np.array([0, 19, 20, 49, 50, 500]), [20, 50]).tolist() == [0, 0, 1, 1, 2, 2]


def test_number_classes_carry_labels_and_both_units():
    rows = layers.number_classes([20, 50], 0.001)
    assert [r["label"] for r in rows] == ["under 0.02", "0.02 to under 0.05", "0.05 or more"]
    assert rows[1] == {"index": 1, "label": "0.02 to under 0.05", "min": 0.02, "max": 0.05, "min_scaled": 20, "max_scaled": 50}
    assert [r["label"] for r in layers.number_classes([1, 1000], 1)] == ["under 1", "1 to under 1,000", "1,000 or more"]


def test_number_summary_shares_are_of_the_fields_with_a_value():
    values = np.array([0, 10, 30, 0, 100])
    has = np.array([True, True, True, False, True])
    acres = np.array([100.0, 100.0, 200.0, 999.0, 600.0])
    s = layers.number_summary(values, has, acres, [20, 50], 1)
    assert (s["fields"], s["fields_with_value"], s["fields_no_value"], s["acres_with_value"]) == (5, 4, 1, 1000)
    assert (s["median"], s["p25"], s["p75"]) == (20, 8, 48)      # of 0, 10, 30, 100, to whole days
    assert s["classes"] == [
        {"index": 0, "fields": 2, "acres": 200, "share_fields": 0.5, "share_acres": 0.2},
        {"index": 1, "fields": 1, "acres": 200, "share_fields": 0.25, "share_acres": 0.2},
        {"index": 2, "fields": 1, "acres": 600, "share_fields": 0.25, "share_acres": 0.6},
    ]
    empty = layers.number_summary(values, np.zeros(5, dtype=bool), acres, [20, 50], 1)
    assert empty["median"] is None and empty["classes"][0]["share_fields"] is None and empty["fields_no_value"] == 5


# --- the table layers on made-up tables -----------------------------------------------------------------

IDS = ["F3", "F1", "F2", "F4"]                       # the map's order, on purpose not sorted
ACRES = np.array([10.0, 20.0, 30.0, 40.0])
SLOPES = [0.5, 4.0, None, 1.0]                       # F1 is on a slope (knoll factor 1.3)
TRAITS = {
    "F1": FieldTraits(irrigation="dryland", irrigated_prob=0.104, wei=134.0, weg="2"),
    "F2": FieldTraits(irrigation="irrigated", irrigated_prob=0.996, wei=None, weg="6"),
    "F3": FieldTraits(irrigation="irrigated", irrigated_prob=0.5, wei=56.0, weg="5"),
    # F4 is not in the table
}


def soil_table() -> pd.DataFrame:
    bare = {
        "F1": [100.4, 0.0, 200.6],
        "F2": [36.5, None, 12.0],
        "F3": [5.0, 365.0, 80.0],
        "F4": [None, None, 50.0],
    }
    rows = [{"field_id": f, "year": y, "bare_days": b} for f, series in bare.items() for y, b in zip(YEARS, series)]
    rows.append({"field_id": "ELSEWHERE", "year": 2016, "bare_days": 300.0})     # not one of our fields
    return pd.DataFrame(rows).sample(frac=1.0, random_state=3)                    # row order must not matter


def history_table() -> pd.DataFrame:
    return pd.DataFrame({"year": [2015, 2016, 2017, 2018], "erosion_climate": [90.0, 50.0, 300.0, 100.0]})


def test_bare_days_follow_the_field_order_and_nulls_stay_null():
    soil = soil_table()
    layer = layers.bare_days_layer(IDS, ACRES, soil[soil["field_id"].isin(IDS)])
    assert layer["years"] == YEARS and layer["kind"] == "number" and layer["value_scale"] == 1
    by_year = {row["year"]: row["values"] for row in layer["series"]}
    assert by_year[2016] == [5, 100, 37, None]       # F3, F1, F2, F4; halves go up
    assert by_year[2017] == [365, 0, None, None]
    assert by_year[2018] == [80, 201, 12, 50]
    assert all(isinstance(v, int) for values in by_year.values() for v in values if v is not None)
    summary = layer["series"][1]["summary"]
    assert (summary["fields_with_value"], summary["fields_no_value"]) == (2, 2)
    assert layer["breaks_scaled"] == sorted(set(layer["breaks_scaled"])) and layer["breaks"] == layer["breaks_scaled"]
    assert len(layer["classes"]) == len(layer["breaks"]) + 1
    for row in layer["series"]:                      # the class counts are the rule applied to the values
        got = [v for v in row["values"] if v is not None]
        counts = np.bincount(layers.class_of(np.array(got), layer["breaks_scaled"]), minlength=len(layer["classes"]))
        assert [c["fields"] for c in row["summary"]["classes"]] == counts.tolist()


def test_erosion_is_the_planners_index_for_that_years_bare_days_and_wind():
    soil = soil_table()
    soil = soil[soil["field_id"].isin(IDS)]
    layer = layers.erosion_layer(IDS, ACRES, SLOPES, soil, TRAITS, history_table())
    assert layer["years"] == YEARS and layer["value_scale"] == 0.001
    bare = {(r.field_id, r.year): r.bare_days for r in soil.itertuples()}
    climate = dict(zip(history_table()["year"], history_table()["erosion_climate"]))
    for row in layer["series"]:
        for field_id, slope, sent in zip(IDS, SLOPES, row["values"]):
            days = bare[(field_id, row["year"])]
            if days is None or np.isnan(days):
                assert sent is None
                continue
            t = TRAITS.get(field_id)
            wanted = erosion.erosion_index(days, climate[row["year"]], t.wei if t else None, t.weg if t else None, slope)
            assert sent == half_up(1000 * wanted), (field_id, row["year"])
    values_2017 = layer["series"][1]["values"]
    assert values_2017[0] == 3000                    # F3 bare all year in a 3x wind year: 1 x 1 x 3, at the cap
    assert values_2017[1] == 0                       # F1 was never bare that year
    assert layer["inputs"]["erosion_climate_by_year"] == {"2016": 50, "2017": 300, "2018": 100}
    assert layer["inputs"]["fields_soil_assumed"] == 1           # F4 has no erodibility on record


def test_erosion_keeps_only_years_with_a_wind_factor():
    soil = soil_table()
    history = history_table()[lambda d: d["year"] != 2017]
    layer = layers.erosion_layer(IDS, ACRES, SLOPES, soil[soil["field_id"].isin(IDS)], TRAITS, history)
    assert layer["years"] == [2016, 2018]
    with pytest.raises(layers.Unavailable):
        layers.erosion_layer(IDS, ACRES, SLOPES, soil, TRAITS, history[history["year"] < 2016])


def test_irrigation_classes_and_whole_percent():
    layer = layers.irrigation_layer(IDS, ACRES, TRAITS)
    row = layer["series"][0]
    assert row["year"] is None and layer["years"] is None
    assert row["values"] == [1, 0, 1, 2]             # F3 irrigated, F1 dryland, F2 irrigated, F4 unknown
    assert row["detail"] == [50, 10, 100, None]
    assert [c["key"] for c in layer["classes"]] == ["dryland", "irrigated", "unknown"]
    s = row["summary"]
    assert (s["fields"], s["fields_no_value"], s["acres"]) == (4, 1, 100)
    assert [c["fields"] for c in s["classes"]] == [1, 2, 1]
    assert [c["share_acres"] for c in s["classes"]] == [0.2, 0.4, 0.4]       # shares of all fields
    assert s["classes"][1]["detail_median"] == 75 and s["classes"][2]["detail_median"] is None


# --- rotation change ------------------------------------------------------------------------------------

def _plan(cycle, robust, low=0.60, high=0.66, **extra):
    return {"cycle": list(cycle), "scores": {"overall": robust + 0.01, "robust": robust},
            "spread": {"overall": {"low": low, "mid": (low + high) / 2, "high": high}}, **extra}


PICKS = [_plan(("a", "b", "c"), 0.700), _plan(("a", "b", "d"), 0.690), _plan(("e", "f", "g", "h"), 0.650)]


@pytest.mark.parametrize("current, wanted", [
    (None, ("unknown", None)),
    (_plan(("a", "b", "c"), 0.700, is_candidate=True), ("same", 0)),
    (_plan(("e", "f", "g", "h"), 0.650, is_candidate=True), ("top_pick", 50)),
    (_plan(("a", "b", "c"), 0.640, is_candidate=False), ("within_spread", 60)),       # same crops but not the cycle
    (_plan(("x", "y", "z", "w"), 0.640, is_candidate=True), ("within_spread", 60)),   # gap 0.06 = the 0.06 spread
    (_plan(("x", "y", "z", "w"), 0.639, is_candidate=True), ("clearly_higher", 61)),
    (_plan(("x", "y", "z", "w"), 0.600, low=0.5, high=0.62, is_candidate=False), ("within_spread", 100)),  # its own spread is wider
    (_plan(("x", "y", "z", "w"), 0.700, is_candidate=False), ("not_higher", 0)),
    (_plan(("x", "y", "z", "w"), 0.750, is_candidate=False), ("not_higher", -50)),
])
def test_classify_change(current, wanted):
    assert layers.classify_change({"plans": PICKS, "current": current}) == wanted


def test_classify_change_without_plans_cannot_tell():
    assert layers.classify_change({"plans": [], "current": _plan(("a",), 0.5)}) == ("unknown", None)


def test_rotation_layer_counts_and_gap_medians():
    index = layers.CHANGE_INDEX
    classes = [index["same"], index["within_spread"], index["within_spread"], index["unknown"]]
    layer = layers.rotation_layer(ACRES, classes, [0, 20, 40, None])
    row = layer["series"][0]
    assert row["values"] == classes and row["detail"] == [0, 20, 40, None]
    assert layer["detail"]["value_scale"] == 0.001 and layer["classes"][index["unknown"]]["no_value"] is True
    s = row["summary"]
    assert s["fields_no_value"] == 1 and s["classes"][index["within_spread"]]["detail_median"] == 0.03
    assert s["classes"][index["within_spread"]]["share_acres"] == 0.5 and s["detail"]["fields"] == 3
    assert sum(c["fields"] for c in s["classes"]) == 4


# --- the API on made-up tables ----------------------------------------------------------------------------

def _field_ids(client) -> list[str]:
    return [f["id"] for f in client.get("/fields").json()["features"]]


@pytest.fixture()
def side_tables(tables, monkeypatch, tmp_path):
    """Made-up soil, traits and observed-climate tables next to the planner's made-up climate tables."""
    monkeypatch.setenv("LAYERS_CACHE_DIR", str(tmp_path / "layers_cache"))
    with TestClient(main.create_app()) as c:
        ids = _field_ids(c)
    rng = np.random.default_rng(5)
    soil = pd.DataFrame([
        {"field_id": f, "year": y, "om_pred": 1.5, "n_obs": 4.0, "confidence": 0.3,
         "bare_days": None if (i + y) % 7 == 0 else round(float(rng.uniform(0, 250)), 1)}
        for i, f in enumerate(ids) for y in YEARS
    ])
    soil.to_parquet(tables / "soil_health.parquet", index=False)
    pd.DataFrame({
        "field_id": ids, "irrigation": ["irrigated" if i % 3 == 0 else "dryland" for i in range(len(ids))],
        "irrigated_prob": [0.9 if i % 3 == 0 else 0.1 for i in range(len(ids))],
        "irrigation_basis": "made up", "weg": "5", "wei": [56.0 if i % 2 else 134.0 for i in range(len(ids))],
        "et_gs_mm": 500.0,
    }).to_parquet(tables / "field_traits.parquet", index=False)
    pd.DataFrame({"year": YEARS, "erosion_climate": [60.0, 140.0, 100.0]}).to_parquet(
        tables / "climate_history.parquet", index=False)
    smart.reset()
    return SimpleNamespace(folder=tables, ids=ids, soil=soil)


@pytest.fixture()
def client(side_tables):
    with TestClient(main.create_app()) as c:
        yield c


def _ready(client, name="rotation_change"):
    """GET the layer; if it is still building, wait for the background thread and ask again."""
    response = client.get(f"/layers/{name}")
    if response.status_code == 202:
        assert response.json()["status"] == "building" and response.headers["retry-after"]
        client.app.state.layers.wait_for_change()
        response = client.get(f"/layers/{name}")
    assert response.status_code == 200
    return response.json()


def test_listing_names_every_layer_and_how_to_join(client, side_tables):
    body = client.get("/layers").json()
    assert [layer["name"] for layer in body["layers"]] == list(layers.LAYER_NAMES)
    assert body["field_count"] == len(side_tables.ids) and "features[i]" in body["join"]
    assert body["fields_etag"] == client.get("/fields").headers["etag"]
    for layer in body["layers"]:
        assert "series" not in layer and layer["url"] == f"/layers/{layer['name']}"
        assert layer["method"] and layer["caveat"] and layer["title"] and layer["status"] in ("ready", "building")
    by_name = {layer["name"]: layer for layer in body["layers"]}
    assert by_name["bare_days"]["years"] == YEARS and by_name["erosion_risk"]["years"] == YEARS
    assert by_name["bare_days"]["unit"] == "days a year" and by_name["bare_days"]["classes"]
    assert by_name["irrigation"]["years"] is None and by_name["rotation_change"]["kind"] == "class"


def test_bare_days_values_join_to_the_fields_order(client, side_tables):
    body = client.get("/layers/bare_days").json()
    table = {(r.field_id, r.year): r.bare_days for r in side_tables.soil.itertuples()}
    assert len(body["series"]) == len(YEARS)
    nulls = 0
    for row in body["series"]:
        assert len(row["values"]) == body["field_count"] == len(side_tables.ids)
        for field_id, sent in zip(side_tables.ids, row["values"]):
            raw = table[(field_id, row["year"])]
            if raw is None or np.isnan(raw):
                nulls += 1
                assert sent is None
            else:
                assert sent == half_up(raw)
        assert row["summary"]["fields_no_value"] == sum(v is None for v in row["values"])
    assert nulls > 0                                 # the made-up table has gaps, and they stayed gaps


def test_erosion_matches_the_planners_index_field_by_field(client, side_tables):
    body = client.get("/layers/erosion_risk").json()
    table = {(r.field_id, r.year): r.bare_days for r in side_tables.soil.itertuples()}
    wind = {2016: 60.0, 2017: 140.0, 2018: 100.0}
    for row in body["series"]:
        for i, (field_id, sent) in enumerate(zip(side_tables.ids, row["values"])):
            raw = table[(field_id, row["year"])]
            if raw is None or np.isnan(raw):
                assert sent is None
                continue
            slope = client.get(f"/field/{field_id}").json()["slope_pct"]
            wanted = erosion.erosion_index(raw, wind[row["year"]], 56.0 if i % 2 else 134.0, "5", slope)
            assert sent == half_up(1000 * wanted)


def test_one_year_only(client):
    body = client.get("/layers/bare_days", params={"year": 2017}).json()
    assert [row["year"] for row in body["series"]] == [2017] and body["years"] == YEARS
    assert body["series"][0] == client.get("/layers/bare_days").json()["series"][1]
    assert client.get("/layers/bare_days", params={"year": 1999}).status_code == 400
    assert client.get("/layers/irrigation", params={"year": 2017}).status_code == 400


def test_irrigation_layer_is_the_traits_table(client, side_tables):
    row = client.get("/layers/irrigation").json()["series"][0]
    assert row["values"] == [1 if i % 3 == 0 else 0 for i in range(len(side_tables.ids))]
    assert row["detail"] == [90 if i % 3 == 0 else 10 for i in range(len(side_tables.ids))]


def test_rotation_change_is_what_the_plans_endpoint_says_for_each_field(client, side_tables):
    body = _ready(client)
    row = body["series"][0]
    keys = [c["key"] for c in body["classes"]]
    assert keys == [key for key, _ in layers.CHANGE_CLASSES] and len(row["values"]) == len(side_tables.ids)
    seen = set()
    for field_id, cls, gap in zip(side_tables.ids, row["values"], row["detail"]):
        plans = client.get(f"/field/{field_id}/plans").json()
        assert (keys[cls], gap) == layers.classify_change(plans), field_id
        assert (plans["current"] is None) == (keys[cls] == "unknown")
        seen.add(keys[cls])
    assert len(seen) >= 2                            # the made-up fields do not all land in one class
    assert sum(c["fields"] for c in row["summary"]["classes"]) == len(side_tables.ids)
    assert body["settings"]["horizon"] == 2025


def test_rotation_change_is_cached_on_disk_and_reused(client, side_tables, tmp_path):
    first = _ready(client)
    cache = tmp_path / "layers_cache" / "rotation_change.json"
    stored = json.loads(cache.read_text())
    assert stored["classes"] == first["series"][0]["values"] and stored["gaps"] == first["series"][0]["detail"]
    with TestClient(main.create_app()) as again:     # a fresh server: straight from the file, no thread
        response = again.get("/layers/rotation_change")
        assert response.status_code == 200 and response.json()["series"] == first["series"]
        assert again.app.state.layers._change["thread"] is None
    # a planner table changes (new size and time stamp): the key no longer matches, so it is rebuilt
    history = pd.read_parquet(side_tables.folder / "climate_history.parquet")
    pd.concat([history, history.assign(year=history["year"] - 10)]).to_parquet(
        side_tables.folder / "climate_history.parquet", index=False)
    with TestClient(main.create_app()) as changed:
        response = changed.get("/layers/rotation_change")
        assert response.status_code == 202 and changed.app.state.layers._change["thread"] is not None
        assert _ready(changed)["series"][0]["values"] == first["series"][0]["values"]
    assert json.loads(cache.read_text())["key"] != stored["key"]


def test_a_broken_cache_file_is_rebuilt(client, tmp_path):
    cache = tmp_path / "layers_cache" / "rotation_change.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{not json")
    body = _ready(client)
    assert json.loads(cache.read_text())["classes"] == body["series"][0]["values"]


def test_etag_gzip_and_small_integers(client):
    response = client.get("/layers/bare_days")
    etag = response.headers["etag"]
    assert response.headers["content-encoding"] == "gzip" and response.headers["cache-control"] == "no-cache"
    again = client.get("/layers/bare_days", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    plain = client.get("/layers/bare_days", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.json() == response.json()
    assert b"NaN" not in plain.content and b'"values":[' in plain.content      # compact, strict JSON
    assert all(v is None or isinstance(v, int) for row in plain.json()["series"] for v in row["values"])
    assert len(gzip.compress(plain.content)) < len(plain.content)


def test_unknown_layer_is_404(client):
    response = client.get("/layers/soil_health")
    assert response.status_code == 404 and "bare_days" in response.json()["detail"]


def test_a_missing_file_only_takes_its_own_layers_down(client, side_tables):
    (side_tables.folder / "field_traits.parquet").unlink()
    by_name = {layer["name"]: layer for layer in client.get("/layers").json()["layers"]}
    assert by_name["bare_days"]["status"] == "ready"
    for name in ("irrigation", "erosion_risk"):
        assert by_name[name]["status"] == "unavailable" and "field traits" in by_name[name]["reason"]
        body = client.get(f"/layers/{name}")
        assert body.status_code == 200 and body.json()["status"] == "unavailable" and "series" not in body.json()
    assert client.get("/layers/bare_days").json()["series"]
    (side_tables.folder / "soil_health.parquet").unlink()
    by_name = {layer["name"]: layer for layer in client.get("/layers").json()["layers"]}
    assert by_name["bare_days"]["status"] == "unavailable" and "soil table" in by_name["bare_days"]["reason"]
    assert by_name["rotation_change"]["status"] in ("ready", "building")     # the planner still runs


def test_without_the_observed_climate_table_only_erosion_is_out(client, side_tables):
    (side_tables.folder / "climate_history.parquet").unlink()
    by_name = {layer["name"]: layer for layer in client.get("/layers").json()["layers"]}
    assert by_name["erosion_risk"]["status"] == "unavailable" and "climate" in by_name["erosion_risk"]["reason"]
    assert by_name["bare_days"]["status"] == "ready" and by_name["irrigation"]["status"] == "ready"


def test_without_the_planner_tables_the_rotation_layer_says_why(no_tables, monkeypatch, tmp_path):
    monkeypatch.setenv("LAYERS_CACHE_DIR", str(tmp_path / "layers_cache"))
    with TestClient(main.create_app()) as c:
        body = c.get("/layers").json()
        assert {layer["name"]: layer["status"] for layer in body["layers"]} == dict.fromkeys(layers.LAYER_NAMES, "unavailable")
        assert all(layer["reason"] for layer in body["layers"])
        response = c.get("/layers/rotation_change")
        assert response.status_code == 200 and "future-weather" in response.json()["reason"]
        assert c.get("/health").status_code == 200 and c.get("/fields").status_code == 200
    assert not (tmp_path / "layers_cache").exists()


def test_the_wording_rules_hold_in_every_layer_text(client):
    text = json.dumps(client.get("/layers").json()).lower()
    for banned in ("soil health", "improves the soil", "improve the soil", "profit", "income"):
        assert banned not in text


# --- the real files (skipped without data/) -------------------------------------------------------------------

REAL = ["fields.parquet", "soil_health.parquet", "field_traits.parquet", "climate_history.parquet",
        "climate_futures.parquet", "climate_monthly.parquet"]


@pytest.fixture()
def real_client(monkeypatch, tmp_path):
    if not all((DATA / name).is_file() for name in REAL):
        pytest.skip("needs the real tables under data/")
    for var in ("FIELDS_PATH", "SOIL_HEALTH_PATH", "FIELD_TRAITS_PATH", "CLIMATE_HISTORY_PATH", "PLANNER_ENGINE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LAYERS_CACHE_DIR", str(tmp_path / "layers_cache"))      # never the real cache
    smart.reset()
    with TestClient(main.create_app()) as c:
        yield c


def test_real_layers_cover_the_county(real_client):
    ids = _field_ids(real_client)
    listing = real_client.get("/layers").json()
    assert listing["field_count"] == len(ids)
    bare = real_client.get("/layers/bare_days").json()
    wind = real_client.get("/layers/erosion_risk").json()
    assert bare["years"] == list(range(2016, 2025)) == wind["years"]
    assert 3 <= len(bare["breaks"]) <= 4 and 3 <= len(wind["breaks"]) <= 4
    soil = pd.read_parquet(DATA / "soil_health.parquet", columns=["field_id", "year", "bare_days"])
    for row in bare["series"]:
        assert len(row["values"]) == len(ids)
        assert row["summary"]["fields_no_value"] == int(soil[soil["year"] == row["year"]]["bare_days"].isna().sum())
        shares = [c["share_fields"] for c in row["summary"]["classes"]]
        assert abs(sum(shares) - 1) < 0.01 and max(shares) < 0.6        # the breaks split the county
    for b, w in zip(bare["series"], wind["series"]):                   # erosion is null exactly where bare days are
        assert [v is None for v in b["values"]] == [v is None for v in w["values"]]
    # a few fields against the per-field endpoints
    picks = [0, len(ids) // 3, len(ids) // 2, len(ids) - 1]
    for i in picks:
        years = {y["year"]: y["bare_days"] for y in real_client.get(f"/field/{ids[i]}/soil").json()["years"]}
        for row in bare["series"]:
            raw = years[row["year"]]
            assert row["values"][i] == (None if raw is None else half_up(raw))
    change = _ready(real_client)
    row = change["series"][0]
    keys = [c["key"] for c in change["classes"]]
    assert len(row["values"]) == len(ids) and len(set(row["values"])) >= 4
    for i in picks + list(range(0, len(ids), 500)):
        plans = real_client.get(f"/field/{ids[i]}/plans").json()
        assert (keys[row["values"][i]], row["detail"][i]) == layers.classify_change(plans), ids[i]
    irrigation = real_client.get("/layers/irrigation").json()["series"][0]
    traits = pd.read_parquet(DATA / "field_traits.parquet").set_index("field_id")
    for i in picks:
        assert irrigation["values"][i] == (1 if traits.loc[ids[i], "irrigation"] == "irrigated" else 0)
        assert irrigation["detail"][i] == int(round(100 * traits.loc[ids[i], "irrigated_prob"]))
    for name in layers.LAYER_NAMES:                                    # small on the wire
        sent = real_client.get(f"/layers/{name}")
        assert sent.headers["content-encoding"] == "gzip"
        assert int(sent.headers["content-length"]) < 150_000, name
