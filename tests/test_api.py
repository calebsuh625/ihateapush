"""Tests for api/main.py: every endpoint against the HTTP contract, on tiny synthetic GeoParquet files.

No network and no real data: each test writes its own file under tmp_path and points the
app at it with FIELDS_PATH (or by redirecting REAL_PATH / FAKE_PATH for the fallback tests).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import re
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
import shapely  # noqa: E402
from shapely.geometry import Polygon, box, shape  # noqa: E402

with warnings.catch_warnings():  # starlette nags about httpx vs httpx2 at import time
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient  # noqa: E402

import api.main as main  # noqa: E402
from api.main import (  # noqa: E402
    StartupError,
    clean_number,
    compute_stats,
    create_app,
    is_fake_file,
    ordered_history,
    polygon_coordinates,
    resolve_fields_path,
    simplify_for_map,
)
from api.planner import FieldInput, Plan, Scores, rank_plans  # noqa: E402
from pipeline.schema import COLUMNS, check_fields  # noqa: E402

ROTATION_CROPS = {"corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow", "cover crop"}
NAN = float("nan")

# --------------------------------------------------------------------------------------
# Synthetic data
# --------------------------------------------------------------------------------------

CORN_HEAVY = (
    ["sorghum"] + ["corn"] * 9 + ["soybeans"] + ["corn"] * 3 + ["soybeans"] + ["corn"] * 2
)  # 2008..2024: corn 2009-2017 is the longest run
CORN_HEAVY_HISTORY = {str(2008 + i): crop for i, crop in enumerate(CORN_HEAVY)}
GAPPY_HISTORY = {  # missing years + crops outside the core six
    "2008": "cotton",
    "2009": "cotton",
    "2011": "cotton",
    "2012": "grass/pasture",
    "2014": "double crop: winter wheat/sorghum",
    "2015": "triticale",
    "2016": "triticale",
}


def circle(lon: float, lat: float, radius: float = 0.004, points: int = 200) -> Polygon:
    """A centre-pivot style field with a small square hole (a pond, say) in the middle."""
    angles = np.linspace(0, 2 * np.pi, points + 1)[:-1]
    shell = np.column_stack([lon + radius * np.cos(angles), lat + radius * np.sin(angles)])
    hole = list(box(lon - 0.0005, lat - 0.0005, lon + 0.0005, lat + 0.0005).exterior.coords)
    return Polygon(shell, holes=[hole])


SQUARE_1 = box(-100.8234075176, 37.8783858772, -100.8163123456, 37.8851224281)
# stored clockwise on purpose: the API must hand out counter-clockwise exteriors
SQUARE_2 = Polygon(list(box(-100.8134075176, 37.8783858772, -100.8063123456, 37.8851224281).exterior.coords)[::-1])
SQUARE_3 = box(-100.8034075176, 37.8783858772, -100.7963123456, 37.8851224281)
CIRCLE_4 = circle(-100.7850123, 37.8820456)
WITH_Z_5 = Polygon(
    [(-100.7734075, 37.8783858, 870.0), (-100.7663123, 37.8783858, 871.0), (-100.7663123, 37.8851224, 872.0),
     (-100.7734075, 37.8851224, 873.0)]
)  # fmt: skip


def default_rows() -> list[dict]:
    return [
        {
            "field_id": "KS-000001",
            "geometry": SQUARE_1,
            # written newest-first on purpose: the API must return the years ascending
            "crop_history": json.dumps(dict(reversed(list(CORN_HEAVY_HISTORY.items())))),
            "organic_matter_pct": 1.19,
            "slope_pct": 0.7,
            "area_ha": 46.2,
        },
        {
            "field_id": "KS-000002",
            "geometry": SQUARE_2,
            "crop_history": json.dumps(GAPPY_HISTORY),
            "organic_matter_pct": NAN,
            "slope_pct": NAN,
            "area_ha": 44.71,
        },
        {
            "field_id": "KS-000003",
            "geometry": SQUARE_3,
            "crop_history": json.dumps({"2024": "sorghum"}),
            "organic_matter_pct": 2.5,
            "slope_pct": NAN,
            "area_ha": 61.0649,
        },
        {
            "field_id": "KS-000004",
            "geometry": CIRCLE_4,
            "crop_history": json.dumps({str(y): ("corn" if y % 2 else "soybeans") for y in range(2008, 2025)}),
            "organic_matter_pct": 3.1234567,
            "slope_pct": 4.5678901,
            "area_ha": 39.5,
        },
        {
            "field_id": "KS-000005",
            "geometry": WITH_Z_5,
            "crop_history": json.dumps(
                {str(y): ["winter wheat", "sorghum", "fallow"][i % 3] for i, y in enumerate(range(2008, 2025))}
            ),
            "organic_matter_pct": 1.4,
            "slope_pct": 5.5,
            "area_ha": 52.3,
        },
    ]


def write_fields(path: Path, rows: list[dict] | None = None, validate: bool = True) -> Path:
    gdf = gpd.GeoDataFrame(default_rows() if rows is None else rows, geometry="geometry", crs="EPSG:4326")[COLUMNS]
    if validate:
        assert check_fields(gdf) == [], "the test fixture itself must satisfy schema.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path, index=False)
    return path


def strict_json(text: str):
    """json.loads that fails on NaN / Infinity / -Infinity (which Python would otherwise accept)."""

    def reject(token):
        raise AssertionError(f"invalid JSON constant {token}")

    return json.loads(text, parse_constant=reject)


def all_positions(geometry: dict):
    for ring in geometry["coordinates"]:
        yield from ring


@pytest.fixture()
def fields_file(tmp_path, monkeypatch) -> Path:
    path = write_fields(tmp_path / "fields.parquet")
    monkeypatch.setenv("FIELDS_PATH", str(path))
    return path


@pytest.fixture()
def client(fields_file):
    with TestClient(create_app()) as test_client:
        yield test_client


def start(app=None) -> TestClient:
    return TestClient(create_app() if app is None else app)


# --------------------------------------------------------------------------------------
# GET /health
# --------------------------------------------------------------------------------------


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok", "source": "fields.parquet", "is_fake": False, "field_count": 5}


def test_health_reports_fake_data(tmp_path, monkeypatch):
    path = write_fields(tmp_path / "fields_fake.parquet", default_rows()[:2])
    monkeypatch.setenv("FIELDS_PATH", str(path))
    with start() as c:
        assert c.get("/health").json() == {
            "status": "ok",
            "source": "fields_fake.parquet",
            "is_fake": True,
            "field_count": 2,
        }


# --------------------------------------------------------------------------------------
# GET /fields
# --------------------------------------------------------------------------------------


def test_fields_is_a_feature_collection_exactly_per_contract(client):
    response = client.get("/fields")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in ("application/geo+json", "application/json")
    body = strict_json(response.text)
    assert set(body) == {"type", "bbox", "features"}
    assert body["type"] == "FeatureCollection"
    assert [f["id"] for f in body["features"]] == [f"KS-00000{i}" for i in range(1, 6)]
    areas = {"KS-000001": 46.2, "KS-000002": 44.71, "KS-000003": 61.06, "KS-000004": 39.5, "KS-000005": 52.3}
    for feature in body["features"]:
        assert set(feature) == {"type", "id", "properties", "geometry"}
        assert feature["type"] == "Feature"
        assert feature["properties"] == {"field_id": feature["id"], "area_ha": areas[feature["id"]]}
        assert isinstance(feature["properties"]["area_ha"], float)
        assert set(feature["geometry"]) == {"type", "coordinates"}
        assert feature["geometry"]["type"] == "Polygon"


def test_fields_bbox_is_min_lon_min_lat_max_lon_max_lat(client):
    body = client.get("/fields").json()
    bbox = body["bbox"]
    assert len(bbox) == 4 and all(isinstance(v, float) for v in bbox)
    positions = [p for f in body["features"] for p in all_positions(f["geometry"])]
    lons, lats = [p[0] for p in positions], [p[1] for p in positions]
    assert bbox == [min(lons), min(lats), max(lons), max(lats)]
    assert bbox[0] < bbox[2] and bbox[1] < bbox[3]
    assert -101 < bbox[0] < -100 and 37 < bbox[1] < 38, "longitude first, then latitude"
    # and it matches the real extent of the data to within the rounding
    true_bounds = shapely.total_bounds(np.array([SQUARE_1, SQUARE_2, SQUARE_3, CIRCLE_4, WITH_Z_5], dtype=object))
    assert bbox == pytest.approx(list(true_bounds), abs=2e-5)


def test_fields_geometry_is_valid_simplified_and_rounded_to_5_decimals(client):
    response = client.get("/fields")
    assert not re.search(r"\d\.\d{6,}", response.text), "no coordinate (or area) with more than 5 decimals"
    originals = {"KS-000001": SQUARE_1, "KS-000002": SQUARE_2, "KS-000003": SQUARE_3, "KS-000004": CIRCLE_4,
                 "KS-000005": shapely.force_2d(WITH_Z_5)}  # fmt: skip
    for feature in response.json()["features"]:
        geometry = feature["geometry"]
        for ring in geometry["coordinates"]:
            assert len(ring) >= 4 and ring[0] == ring[-1], "rings are closed"
            assert all(a != b for a, b in zip(ring, ring[1:])), "no repeated points"
            for position in ring:
                assert len(position) == 2, "lon/lat only, no z"
                assert all(isinstance(v, float) and round(v, 5) == v for v in position)
        polygon = shape(geometry)
        original = originals[feature["id"]]
        assert polygon.geom_type == "Polygon" and polygon.is_valid and not polygon.is_empty and polygon.area > 0
        assert polygon.exterior.is_ccw, "exterior ring counter-clockwise (RFC 7946)"
        assert polygon.hausdorff_distance(original) < 5e-5, "within a few metres of the real outline"
        assert abs(polygon.area - original.area) / original.area < 0.01

    by_id = {f["id"]: f["geometry"] for f in response.json()["features"]}
    assert not SQUARE_2.exterior.is_ccw and shape(by_id["KS-000002"]).exterior.is_ccw, "clockwise input is re-wound"
    assert not shape(by_id["KS-000004"]).interiors[0].is_ccw, "holes run clockwise (RFC 7946)"
    circle_ring = by_id["KS-000004"]["coordinates"][0]
    assert 12 <= len(circle_ring) < 100, "the 200-vertex circle is simplified, but still a circle"
    assert len(by_id["KS-000004"]["coordinates"]) == 2, "the hole survives"
    assert len(by_id["KS-000001"]["coordinates"][0]) == 5, "a square stays a square"


def test_fields_is_precomputed_and_served_gzipped_or_plain(client):
    store = client.app.state.store
    zipped = client.get("/fields", headers={"Accept-Encoding": "gzip"})
    plain = client.get("/fields", headers={"Accept-Encoding": "identity"})
    assert zipped.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    assert "accept-encoding" in zipped.headers["vary"].lower()
    assert plain.content == zipped.content == store.fields_geojson, "the bytes built at startup, untouched"
    assert gzip.decompress(store.fields_geojson_gzip) == store.fields_geojson
    assert int(plain.headers["content-length"]) == len(store.fields_geojson)
    # revalidation: same data -> 304 with no body
    again = client.get("/fields", headers={"If-None-Match": plain.headers["etag"]})
    assert again.status_code == 304 and again.content == b""


def test_other_large_responses_are_gzipped_by_the_middleware(client):
    response = client.get("/field/KS-000004", headers={"Accept-Encoding": "gzip"})  # 200-vertex outline
    assert response.status_code == 200 and response.headers.get("content-encoding") == "gzip"
    assert len(response.json()["geometry"]["coordinates"][0]) == 201


# --------------------------------------------------------------------------------------
# GET /field/{field_id}
# --------------------------------------------------------------------------------------


def test_field_detail_exactly_per_contract(client):
    response = client.get("/field/KS-000001")
    assert response.status_code == 200
    body = strict_json(response.text)
    assert list(body) == ["field_id", "geometry", "crop_history", "organic_matter_pct", "slope_pct", "area_ha", "stats"]
    assert body["field_id"] == "KS-000001"
    assert body["crop_history"] == CORN_HEAVY_HISTORY
    assert isinstance(body["crop_history"], dict), "a real JSON object, not a string"
    assert list(body["crop_history"]) == [str(y) for y in range(2008, 2025)], "keys ascending"
    assert body["organic_matter_pct"] == 1.19 and body["slope_pct"] == 0.7 and body["area_ha"] == 46.2
    assert body["stats"] == {
        "years_covered": 17,
        "distinct_crops": ["corn", "soybeans", "sorghum"],
        "distinct_crop_count": 3,
        "longest_streak": {"crop": "corn", "years": 9, "start_year": 2009, "end_year": 2017},
        "pct_years_corn": 82.4,
    }
    assert list(body["stats"]) == [
        "years_covered", "distinct_crops", "distinct_crop_count", "longest_streak", "pct_years_corn",
    ]  # fmt: skip
    assert list(body["stats"]["longest_streak"]) == ["crop", "years", "start_year", "end_year"]


def test_field_detail_geometry_is_full_resolution(client):
    for field_id, original in (("KS-000002", SQUARE_2), ("KS-000001", SQUARE_1), ("KS-000004", CIRCLE_4)):
        geometry = client.get(f"/field/{field_id}").json()["geometry"]
        assert set(geometry) == {"type", "coordinates"} and geometry["type"] == "Polygon"
        polygon = shape(geometry)
        assert polygon.is_valid and polygon.exterior.is_ccw
        assert all(not hole.is_ccw for hole in polygon.interiors)
        assert shapely.get_num_coordinates(polygon) == shapely.get_num_coordinates(original), "no vertex dropped"
        assert polygon.equals(original)
        assert polygon.hausdorff_distance(original) == 0.0, "coordinates are not rounded"
    # more detail than the overview map
    overview = {f["id"]: f["geometry"] for f in client.get("/fields").json()["features"]}
    assert len(overview["KS-000004"]["coordinates"][0]) < len(geometry["coordinates"][0]) == 201
    # z values are dropped: GeoJSON positions are [lon, lat]
    z_field = client.get("/field/KS-000005").json()["geometry"]
    assert all(len(position) == 2 for position in all_positions(z_field))


def test_nan_in_the_file_becomes_null_never_invalid_json(client):
    response = client.get("/field/KS-000002")
    assert response.status_code == 200
    assert "NaN" not in response.text and "Infinity" not in response.text
    body = strict_json(response.text)
    assert body["organic_matter_pct"] is None and body["slope_pct"] is None
    assert '"organic_matter_pct":null' in response.text.replace(" ", "")
    assert body["area_ha"] == 44.71
    half = strict_json(client.get("/field/KS-000003").text)
    assert half["organic_matter_pct"] == 2.5 and half["slope_pct"] is None
    # every endpoint, every field: strictly valid JSON
    for path in ["/health", "/fields"] + [f"/field/KS-00000{i}{tail}" for i in range(1, 6) for tail in ("", "/plans")]:
        strict_json(client.get(path).text)


def test_numbers_are_rounded_to_2_decimals(client):
    body = client.get("/field/KS-000004").json()
    assert (body["organic_matter_pct"], body["slope_pct"], body["area_ha"]) == (3.12, 4.57, 39.5)
    assert client.get("/field/KS-000003").json()["area_ha"] == 61.06


def test_field_detail_with_missing_years_and_unknown_crops(client):
    body = client.get("/field/KS-000002").json()
    assert body["crop_history"] == GAPPY_HISTORY
    assert body["stats"] == {
        "years_covered": 7,
        "distinct_crops": ["cotton", "triticale", "double crop: winter wheat/sorghum", "grass/pasture"],
        "distinct_crop_count": 4,
        # cotton 2008-2009 and triticale 2015-2016 are both 2 years: the most recent wins.
        # cotton 2011 does not extend the first run because 2010 is missing.
        "longest_streak": {"crop": "triticale", "years": 2, "start_year": 2015, "end_year": 2016},
        "pct_years_corn": 0.0,
    }


def test_field_detail_with_single_year_history(client):
    body = client.get("/field/KS-000003").json()
    assert body["crop_history"] == {"2024": "sorghum"}
    assert body["stats"] == {
        "years_covered": 1,
        "distinct_crops": ["sorghum"],
        "distinct_crop_count": 1,
        "longest_streak": {"crop": "sorghum", "years": 1, "start_year": 2024, "end_year": 2024},
        "pct_years_corn": 0.0,
    }


@pytest.mark.parametrize("field_id", ["KS-999999", "KS-0000010", "ks-000001", "nope", "KS-000001 "])
def test_unknown_field_is_404_with_detail(client, field_id):
    for path in (f"/field/{field_id}", f"/field/{field_id}/plans"):
        response = client.get(path)
        assert response.status_code == 404
        body = strict_json(response.text)
        assert set(body) == {"detail"} and isinstance(body["detail"], str) and body["detail"]


# --------------------------------------------------------------------------------------
# GET /field/{field_id}/plans
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field_id", [f"KS-00000{i}" for i in range(1, 6)])
def test_plans_exactly_per_contract(client, field_id):
    response = client.get(f"/field/{field_id}/plans")
    assert response.status_code == 200
    body = strict_json(response.text)
    assert list(body) == ["field_id", "plans"]
    assert body["field_id"] == field_id
    plans = body["plans"]
    assert isinstance(plans, list) and len(plans) == 3
    for plan in plans:
        assert list(plan) == ["name", "rotation", "scores", "reason"]
        assert isinstance(plan["name"], str) and plan["name"]
        assert isinstance(plan["rotation"], list) and len(plan["rotation"]) == 4
        assert set(plan["rotation"]) <= ROTATION_CROPS
        assert list(plan["scores"]) == ["soil", "water", "money", "overall"]
        for value in plan["scores"].values():
            assert isinstance(value, float) and 0.0 <= value <= 1.0 and round(value, 3) == value
        assert isinstance(plan["reason"], str) and 0 < len(plan["reason"]) <= 160
    overall = [plan["scores"]["overall"] for plan in plans]
    assert overall == sorted(overall, reverse=True), "sorted by scores.overall descending"
    assert len({tuple(plan["rotation"]) for plan in plans}) == 3


def test_plans_are_the_planners_answer_for_that_field(client):
    for field_id in ("KS-000001", "KS-000002", "KS-000005"):
        detail = client.get(f"/field/{field_id}").json()
        expected = rank_plans(
            FieldInput(
                field_id=field_id,
                crop_history=detail["crop_history"],
                organic_matter_pct=detail["organic_matter_pct"],
                slope_pct=detail["slope_pct"],
                area_ha=detail["area_ha"],
            )
        )
        got = client.get(f"/field/{field_id}/plans").json()["plans"]
        assert [p["rotation"] for p in got] == [p.rotation for p in expected]
        assert [p["name"] for p in got] == [p.name for p in expected]
        assert [p["reason"] for p in got] == [p.reason for p in expected]
        assert [p["scores"]["overall"] for p in got] == [p.scores.overall for p in expected]
    # stable across calls
    assert client.get("/field/KS-000001/plans").json() == client.get("/field/KS-000001/plans").json()


def test_plans_depend_on_the_field(client):
    corn_rut = client.get("/field/KS-000001/plans").json()["plans"]  # corn 14 of 17 years, low OM
    dryland = client.get("/field/KS-000005/plans").json()["plans"]  # wheat-sorghum-fallow, 5.5% slope
    assert [p["rotation"] for p in corn_rut] != [p["rotation"] for p in dryland]
    assert all(p["rotation"][0] != "corn" for p in corn_rut)
    assert any("corn" in p["reason"] and "8 of the last 10 years" in p["reason"] for p in corn_rut)
    assert any("1.2%" in p["reason"] for p in corn_rut)
    assert any("5.5% slope" in p["reason"] for p in dryland)
    assert "–" in corn_rut[0]["name"], "names use an en dash, sent as real UTF-8"


def test_the_planner_is_a_swappable_seam(fields_file):
    seen: list[FieldInput] = []

    def stub_planner(field: FieldInput) -> list[Plan]:
        seen.append(field)
        return [Plan("Only wheat", ["winter wheat"] * 4, Scores(0.1, 0.2, 0.3, 0.4), "Because the test says so.")]

    with start(create_app(planner=stub_planner)) as c:
        body = c.get("/field/KS-000002/plans").json()
    assert body == {
        "field_id": "KS-000002",
        "plans": [
            {
                "name": "Only wheat",
                "rotation": ["winter wheat"] * 4,
                "scores": {"soil": 0.1, "water": 0.2, "money": 0.3, "overall": 0.4},
                "reason": "Because the test says so.",
            }
        ],
    }
    (field,) = seen
    assert isinstance(field, FieldInput) and field.field_id == "KS-000002"
    assert field.crop_history == GAPPY_HISTORY and isinstance(field.crop_history, dict)
    assert field.organic_matter_pct is None and field.slope_pct is None, "NaN reaches the planner as None"
    assert field.area_ha == 44.71


# --------------------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173"])
def test_cors_is_open_for_local_dev(client, origin):
    for path in ("/health", "/fields", "/field/KS-000001", "/field/KS-000001/plans", "/field/KS-999999"):
        assert client.get(path, headers={"Origin": origin}).headers["access-control-allow-origin"] == "*"
    preflight = client.options("/fields", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in preflight.headers


# --------------------------------------------------------------------------------------
# Startup: which file, logging, refusing bad files
# --------------------------------------------------------------------------------------


def test_startup_logs_which_file_was_loaded(fields_file, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    with start():
        pass
    messages = [r.getMessage() for r in caplog.records if r.name == "uvicorn.error"]
    assert any("Loaded 5 fields from" in m and "fields.parquet" in m for m in messages), messages
    assert not any("FAKE" in m for m in messages)


def test_startup_warns_loudly_about_fake_data(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FIELDS_PATH", str(write_fields(tmp_path / "fields_fake.parquet")))
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    with start():
        pass
    records = [r for r in caplog.records if r.name == "uvicorn.error"]
    assert any("Loaded 5 fields from" in r.getMessage() and "fields_fake.parquet" in r.getMessage() for r in records)
    warnings_logged = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert len(warnings_logged) == 1 and "FAKE DATA" in warnings_logged[0] and "make pipeline" in warnings_logged[0]


def test_resolve_fields_path_order(tmp_path):
    real, fake, other = tmp_path / "fields.parquet", tmp_path / "fields_fake.parquet", tmp_path / "other.parquet"
    with pytest.raises(StartupError):
        resolve_fields_path(None, real, fake)
    fake.write_bytes(b"x")
    assert resolve_fields_path(None, real, fake) == fake
    assert resolve_fields_path("", real, fake) == fake, "an empty FIELDS_PATH counts as unset"
    real.write_bytes(b"x")
    assert resolve_fields_path(None, real, fake) == real, "the real file wins over the fake one"
    other.write_bytes(b"x")
    assert resolve_fields_path(str(other), real, fake) == other, "FIELDS_PATH wins over both"
    with pytest.raises(StartupError, match="FIELDS_PATH"):
        resolve_fields_path(str(tmp_path / "missing.parquet"), real, fake)
    assert is_fake_file(fake) and not is_fake_file(real) and not is_fake_file(other)


def test_app_falls_back_from_real_to_fake(tmp_path, monkeypatch):
    real, fake = tmp_path / "data" / "fields.parquet", tmp_path / "data" / "fields_fake.parquet"
    monkeypatch.delenv("FIELDS_PATH", raising=False)
    monkeypatch.setattr(main, "REAL_PATH", real)
    monkeypatch.setattr(main, "FAKE_PATH", fake)

    write_fields(fake, default_rows()[:2])
    with start() as c:
        assert c.get("/health").json() == {
            "status": "ok", "source": "fields_fake.parquet", "is_fake": True, "field_count": 2,
        }  # fmt: skip

    write_fields(real)  # the real file appears: it wins
    with start() as c:
        assert c.get("/health").json() == {
            "status": "ok", "source": "fields.parquet", "is_fake": False, "field_count": 5,
        }  # fmt: skip

    other = write_fields(tmp_path / "elsewhere.parquet", default_rows()[:1])
    monkeypatch.setenv("FIELDS_PATH", str(other))  # FIELDS_PATH beats both
    with start() as c:
        assert c.get("/health").json()["source"] == "elsewhere.parquet"
        assert c.get("/health").json()["field_count"] == 1


def startup_failure(app) -> tuple[BaseException, list[dict]]:
    """Run the ASGI lifespan startup the way a server does.

    Returns the exception that came out and the messages the app sent to the server
    (uvicorn prints the text of "lifespan.startup.failed" to the console).
    """
    sent: list[dict] = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        sent.append(message)

    async def run():
        try:
            await app({"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}, receive, send)
        except BaseException as exc:  # noqa: BLE001
            return exc
        return None

    return asyncio.run(run()), sent


def bad_rows() -> list[dict]:
    rows = default_rows()
    rows[1]["field_id"] = "KS-000001"  # duplicate id
    rows[2]["area_ha"] = -1.0  # not > 0
    rows[3]["crop_history"] = json.dumps({"2008": "Corn"})  # not lowercase
    return rows


def test_schema_invalid_file_refuses_to_start_with_a_readable_message(tmp_path, monkeypatch):
    path = write_fields(tmp_path / "fields.parquet", bad_rows(), validate=False)
    monkeypatch.setenv("FIELDS_PATH", str(path))

    with pytest.raises(StartupError) as caught:
        with start():
            pytest.fail("the app must not start on a schema-invalid file")
    message = str(caught.value)
    lines = message.splitlines()
    assert lines[0].startswith("Cannot start the API:")
    assert str(path) in lines[0] and "does not match schema.md (3 problems)" in lines[0]
    assert "  - field_id: 1 duplicate(s), e.g. KS-000001" in lines
    assert "  - area_ha: 1 not > 0, e.g. KS-000003" in lines
    assert any(line.startswith("  - crop_history: 1 bad value(s), e.g. KS-000004") for line in lines)
    assert len(lines) <= 8 and "Traceback" not in message

    # what the server console gets: exactly that message, not a traceback
    error, sent = startup_failure(create_app())
    assert isinstance(error, StartupError)
    assert [m["type"] for m in sent] == ["lifespan.startup.failed"]
    assert sent[0]["message"] == message
    assert "Traceback" not in sent[0]["message"] and 'File "' not in sent[0]["message"]


def test_file_that_is_not_geoparquet_refuses_to_start(tmp_path, monkeypatch):
    plain = tmp_path / "fields.parquet"
    pd.DataFrame({"field_id": ["KS-000001"], "area_ha": [1.0]}).to_parquet(plain)
    garbage = tmp_path / "garbage.parquet"
    garbage.write_text("this is not a parquet file")
    for path in (plain, garbage):
        monkeypatch.setenv("FIELDS_PATH", str(path))
        with pytest.raises(StartupError) as caught:
            with start():
                pytest.fail("must not start")
        assert str(path) in str(caught.value) and "could not read as GeoParquet" in str(caught.value)


def test_invalid_real_file_is_not_silently_replaced_by_the_fake_one(tmp_path, monkeypatch):
    real, fake = tmp_path / "fields.parquet", tmp_path / "fields_fake.parquet"
    write_fields(real, bad_rows(), validate=False)
    write_fields(fake)
    monkeypatch.delenv("FIELDS_PATH", raising=False)
    monkeypatch.setattr(main, "REAL_PATH", real)
    monkeypatch.setattr(main, "FAKE_PATH", fake)
    with pytest.raises(StartupError, match="does not match schema.md") as caught:
        with start():
            pytest.fail("must not start")
    assert str(real) in str(caught.value) and str(fake) not in str(caught.value)


def test_missing_file_refuses_to_start_with_a_readable_message(tmp_path, monkeypatch):
    # FIELDS_PATH pointing nowhere
    missing = tmp_path / "nope.parquet"
    monkeypatch.setenv("FIELDS_PATH", str(missing))
    with pytest.raises(StartupError) as caught:
        with start():
            pytest.fail("must not start")
    assert str(missing) in str(caught.value) and "FIELDS_PATH" in str(caught.value)

    # no FIELDS_PATH, and neither the real nor the fake file exists
    monkeypatch.delenv("FIELDS_PATH")
    monkeypatch.setattr(main, "REAL_PATH", tmp_path / "data" / "fields.parquet")
    monkeypatch.setattr(main, "FAKE_PATH", tmp_path / "data" / "fields_fake.parquet")
    with pytest.raises(StartupError) as caught:
        with start():
            pytest.fail("must not start")
    message = str(caught.value)
    assert "make fake" in message and "make pipeline" in message
    assert "fields.parquet" in message and "fields_fake.parquet" in message

    error, sent = startup_failure(create_app())
    assert isinstance(error, StartupError)
    assert [m["type"] for m in sent] == ["lifespan.startup.failed"]
    assert sent[0]["message"] == message and "Traceback" not in sent[0]["message"]


def test_other_startup_exceptions_keep_their_traceback(fields_file, monkeypatch):
    """Only our own StartupError is condensed; a real bug must stay debuggable."""

    def boom(path):
        raise ZeroDivisionError("a genuine bug")

    monkeypatch.setattr(main, "load_store", boom)
    error, sent = startup_failure(create_app())
    assert isinstance(error, ZeroDivisionError)
    assert [m["type"] for m in sent] == ["lifespan.startup.failed"]
    assert "Traceback" in sent[0]["message"] and "a genuine bug" in sent[0]["message"]


def test_successful_lifespan_reports_startup_and_shutdown(fields_file):
    error, sent = startup_failure(create_app())
    assert error is None
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def years(crops, start=2008) -> dict[str, str]:
    return {str(start + i): crop for i, crop in enumerate(crops)}


def test_compute_stats_basic():
    stats = compute_stats(years(["corn", "soybeans"] * 8 + ["corn"]))
    assert stats == {
        "years_covered": 17,
        "distinct_crops": ["corn", "soybeans"],
        "distinct_crop_count": 2,
        "longest_streak": {"crop": "corn", "years": 1, "start_year": 2024, "end_year": 2024},
        "pct_years_corn": 52.9,
    }


def test_compute_stats_missing_year_breaks_a_streak():
    history = years(["corn"] * 4, 2008) | years(["corn"] * 3, 2013)  # 2012 missing
    stats = compute_stats(history)
    assert stats["years_covered"] == 7
    assert stats["longest_streak"] == {"crop": "corn", "years": 4, "start_year": 2008, "end_year": 2011}
    assert stats["pct_years_corn"] == 100.0
    # without the gap it is one 8-year run
    assert compute_stats(years(["corn"] * 8))["longest_streak"]["years"] == 8


def test_compute_stats_ties_go_to_the_most_recent_run():
    history = years(["corn"] * 3 + ["soybeans"] + ["sorghum"] * 3 + ["soybeans"] + ["winter wheat"] * 2)
    assert compute_stats(history)["longest_streak"] == {
        "crop": "sorghum", "years": 3, "start_year": 2012, "end_year": 2014,
    }  # fmt: skip
    # the same crop twice, same length: still the later one
    history = years(["corn", "corn", "soybeans", "corn", "corn", "soybeans"])
    assert compute_stats(history)["longest_streak"] == {
        "crop": "corn", "years": 2, "start_year": 2011, "end_year": 2012,
    }  # fmt: skip
    # all different: every run is 1 year, the last year wins
    assert compute_stats(years(["corn", "soybeans", "sorghum"]))["longest_streak"] == {
        "crop": "sorghum", "years": 1, "start_year": 2010, "end_year": 2010,
    }  # fmt: skip


def test_compute_stats_distinct_crops_by_frequency_then_name():
    history = years(["sorghum", "corn", "alfalfa", "corn", "sorghum", "winter wheat", "fallow"])
    stats = compute_stats(history)
    assert stats["distinct_crops"] == ["corn", "sorghum", "alfalfa", "fallow", "winter wheat"]
    assert stats["distinct_crop_count"] == 5


def test_compute_stats_pct_corn_counts_exactly_corn():
    history = years(["corn", "sweet corn", "pop or orn corn", "double crop: winter wheat/corn", "corn", "sorghum"])
    stats = compute_stats(history)
    assert stats["pct_years_corn"] == 33.3
    assert compute_stats(years(["soybeans"] * 3))["pct_years_corn"] == 0.0
    assert compute_stats(years(["corn"] * 3))["pct_years_corn"] == 100.0
    assert isinstance(compute_stats(years(["corn"]))["pct_years_corn"], float)


def test_compute_stats_does_not_care_about_key_order():
    history = years(["corn"] * 5 + ["soybeans"] * 2)
    shuffled = dict(sorted(history.items(), reverse=True))
    assert compute_stats(shuffled) == compute_stats(history)
    assert list(ordered_history(shuffled)) == sorted(history)


def test_clean_number():
    assert clean_number(1.234567) == 1.23
    assert clean_number(np.float64(2.5)) == 2.5 and isinstance(clean_number(np.float64(2.5)), float)
    assert clean_number(0.0) == 0.0
    for missing in (None, float("nan"), np.nan, float("inf"), -math.inf, pd.NA, "n/a"):
        assert clean_number(missing) is None


def test_simplify_for_map_never_returns_an_empty_or_invalid_polygon():
    tiny = Polygon([(-100.8000001, 37.9000001), (-100.8000031, 37.9000002), (-100.8000012, 37.9000033)])  # ~30 cm
    sliver = Polygon([(-100.8, 37.9), (-100.79, 37.9), (-100.79, 37.900003), (-100.8, 37.900003)])  # 30 cm wide
    spike = Polygon([(-100.8, 37.9), (-100.79, 37.9), (-100.79, 37.91), (-100.795, 37.900004), (-100.8, 37.91)])
    normal = box(-100.8100004, 37.9000004, -100.8000004, 37.9100004)
    geoms = np.array([tiny, sliver, spike, normal, CIRCLE_4], dtype=object)
    assert shapely.is_valid(geoms).all()

    out = simplify_for_map(geoms)
    assert len(out) == len(geoms)
    for original, result in zip(geoms, out):
        assert result.geom_type == "Polygon" and result.is_valid and not result.is_empty and result.area > 0
        assert result.hausdorff_distance(original) < 5e-5
    # the three that cannot survive rounding to ~1 m come back as the original outline
    for original, result in zip(geoms[:3], out[:3]):
        assert result.equals(original)
    # ordinary fields are rounded to 5 decimals and simplified
    for result in out[3:]:
        coords = shapely.get_coordinates(result)
        assert np.array_equal(coords, np.round(coords, 5))
    assert shapely.get_num_coordinates(out[4]) < shapely.get_num_coordinates(CIRCLE_4) / 2
    # the input array is not modified
    assert geoms[3].equals(normal) and shapely.get_coordinates(geoms[3])[0][0] != round(shapely.get_coordinates(geoms[3])[0][0], 5)


def test_degenerate_field_outlines_do_not_break_the_app(tmp_path, monkeypatch):
    rows = default_rows()[:2]
    rows[0]["geometry"] = Polygon([(-100.8000001, 37.9000001), (-100.8000031, 37.9000002), (-100.8000012, 37.9000033)])
    rows[0]["area_ha"] = 0.01
    monkeypatch.setenv("FIELDS_PATH", str(write_fields(tmp_path / "fields.parquet", rows)))
    with start() as c:
        features = strict_json(c.get("/fields").text)["features"]
        assert len(features) == 2
        for feature in features:
            polygon = shape(feature["geometry"])
            assert polygon.is_valid and not polygon.is_empty and polygon.area > 0
        assert c.get("/field/KS-000001").status_code == 200


def test_polygon_coordinates():
    assert polygon_coordinates(box(0, 0, 1, 2)) == [[[1.0, 0.0], [1.0, 2.0], [0.0, 2.0], [0.0, 0.0], [1.0, 0.0]]]
    with_hole = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], holes=[[(1, 1), (1, 2), (2, 2), (2, 1)]])
    rings = polygon_coordinates(with_hole)
    assert len(rings) == 2 and rings[0][0] == rings[0][-1] and rings[1][0] == rings[1][-1]
    repeated = Polygon([(0, 0), (1, 0), (1, 0), (1, 1), (0, 1), (0, 1)])
    assert len(polygon_coordinates(repeated)[0]) == 7
    assert polygon_coordinates(repeated, drop_repeats=True) == [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]


def test_many_fields_are_served_from_memory(tmp_path, monkeypatch):
    """A few hundred fields: ids stay unique and in file order, lookups work for the first and the last."""
    rows = []
    for n in range(400):
        col, row = n % 20, n // 20
        rows.append(
            {
                "field_id": f"KS-{n + 1:06d}",
                "geometry": box(-100.9 + col * 0.01, 37.8 + row * 0.01, -100.892 + col * 0.01, 37.808 + row * 0.01),
                "crop_history": json.dumps({"2023": "corn", "2024": "soybeans"}),
                "organic_matter_pct": NAN if n % 7 == 0 else 1.0 + (n % 5) * 0.5,
                "slope_pct": NAN if n % 11 == 0 else (n % 9) * 0.7,
                "area_ha": 60.0,
            }
        )
    monkeypatch.setenv("FIELDS_PATH", str(write_fields(tmp_path / "fields.parquet", rows)))
    with start() as c:
        assert c.get("/health").json()["field_count"] == 400
        body = strict_json(c.get("/fields").text)
        assert [f["id"] for f in body["features"]] == [f"KS-{n + 1:06d}" for n in range(400)]
        assert body["bbox"] == [-100.9, 37.8, -100.702, 37.998]
        for field_id in ("KS-000001", "KS-000400"):
            assert c.get(f"/field/{field_id}").json()["field_id"] == field_id
            assert len(c.get(f"/field/{field_id}/plans").json()["plans"]) == 3
        assert c.get("/field/KS-000001").json()["organic_matter_pct"] is None
