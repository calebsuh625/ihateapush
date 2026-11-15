"""GET /field/{id}/plans (smart planner), GET /climate and /health["climate"].

The smart path runs on small made-up climate tables written to tmp_path, so these tests always run, with or
without data/. The last tests use the real tables and skip when data/ is missing.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient  # noqa: E402

import api.main as main  # noqa: E402
from api.planner import config, smart  # noqa: E402
from api.planner.legacy import FieldInput, Plan, Scores  # noqa: E402

import test_api as base  # noqa: E402  (fake fields and helpers)
import test_planner_smart as made_up  # noqa: E402  (made-up climate tables)

DRY, WET = "KS-000005", "KS-000001"       # wheat-sorghum-fallow history (dryland), corn-heavy history (irrigated)
DATA = ROOT / "data"
OLD_KEYS = ["name", "rotation", "scores", "reason"]
ENV_PATHS = {
    "CLIMATE_FUTURES_PATH": "climate_futures.parquet", "CLIMATE_MONTHLY_PATH": "climate_monthly.parquet",
    "CLIMATE_HISTORY_PATH": "climate_history.parquet", "FIELD_TRAITS_PATH": "field_traits.parquet",
    "SOIL_HEALTH_PATH": "soil_health.parquet", "PLANNER_FIELDS_PATH": "fields.parquet",
    "EFFECTS_PATH": "effects.json", "NASS_PATH": "nass.parquet", "PLANNER_SOIL_CACHE_PATH": "planner_bare_days.json",
}


def point_planner_at(monkeypatch, folder: Path) -> None:
    monkeypatch.delenv("PLANNER_ENGINE", raising=False)
    for var, name in ENV_PATHS.items():
        monkeypatch.setenv(var, str(folder / name))
    smart.reset()


@pytest.fixture(autouse=True)
def clean_engine():
    smart.reset()
    yield
    smart.reset()


@pytest.fixture()
def fields_file(tmp_path, monkeypatch, no_data=None):
    path = base.write_fields(tmp_path / "fields.parquet")
    monkeypatch.setenv("FIELDS_PATH", str(path))
    monkeypatch.delenv("SATELLITE_PATH", raising=False)
    monkeypatch.delenv("SOIL_HEALTH_PATH", raising=False)
    monkeypatch.setattr(main, "DEFAULT_SATELLITE_PATH", tmp_path / "no_such_satellite.parquet")
    monkeypatch.setattr(main, "DEFAULT_SOIL_HEALTH_PATH", tmp_path / "no_such_soil_health.parquet")
    monkeypatch.setattr(main, "DEFAULT_SOIL_HEALTH_META_PATH", tmp_path / "no_such_soil_meta.json")
    return path


@pytest.fixture()
def tables(tmp_path, fields_file, monkeypatch) -> Path:
    """Made-up climate tables in a temporary folder; the planner reads them instead of data/."""
    folder = tmp_path / "climate"
    folder.mkdir()
    yearly, monthly = made_up.climate_tables(years=range(2000, 2061))   # the file schema wants 2000-2060
    obs = monthly[(monthly["model"] == "mid") & (monthly["scenario"] == "ssp245") & (monthly["year"] <= 2024)]
    monthly = pd.concat([monthly, obs.assign(model="observed", scenario="observed")], ignore_index=True)
    yearly = yearly.assign(gdd=2000.0, wind_ms=4.0, wheat_et0_mm=500.0)         # columns the file schema wants
    monthly = monthly.assign(tmax_c=25.0, tmin_c=10.0, tmean_c=17.5, wind_ms=4.0, hot_days=1.0)
    yearly.to_parquet(folder / "climate_futures.parquet", index=False)
    monthly.to_parquet(folder / "climate_monthly.parquet", index=False)
    point_planner_at(monkeypatch, folder)
    return folder


@pytest.fixture()
def no_tables(tmp_path, fields_file, monkeypatch) -> Path:
    folder = tmp_path / "empty"
    folder.mkdir()
    point_planner_at(monkeypatch, folder)
    return folder


@pytest.fixture()
def client(tables):
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def basic_client(no_tables):
    with TestClient(main.create_app()) as c:
        yield c


def plans(client, field=DRY, **params):
    return client.get(f"/field/{field}/plans", params=params)


# --- the smart path ----------------------------------------------------------------------------------

def test_default_call_has_the_smart_shape_and_the_old_keys(client):
    body = plans(client).json()
    assert body["engine"] == "smart" and body["field_id"] == DRY
    for key in ("settings", "climate", "plans", "current", "current_note", "candidates"):
        assert key in body
    assert len(body["plans"]) == 3
    for plan in body["plans"]:
        assert all(k in plan for k in OLD_KEYS)
        assert {"soil", "water", "money", "overall"} <= set(plan["scores"])
        assert len(plan["rotation"]) in (3, 4) and plan["reason"]
    overall = [p["scores"]["overall"] for p in body["plans"]]
    assert overall == sorted(overall, reverse=True)
    assert body["settings"]["weights"] == config.DEFAULT_WEIGHTS
    assert body["settings"]["scenario"] == "ssp245" and body["settings"]["horizon"] == 2025


def test_weights_are_echoed_normalised_and_change_the_order(client):
    body = plans(client, soil=2, water=2, money=4, risk=2).json()
    assert body["settings"]["weights"] == {"soil": 0.2, "water": 0.2, "money": 0.4, "risk": 0.2}
    orders = set()
    for name in ("soil", "water", "money", "risk"):
        only = plans(client, **{n: (1 if n == name else 0) for n in ("soil", "water", "money", "risk")}).json()
        assert only["settings"]["weights"][name] == 1.0
        orders.add(tuple(p["name"] for p in only["plans"]))
    assert len(orders) > 1, "different priorities should rank differently"


def test_a_missing_weight_takes_its_default(client):
    w = plans(client, soil=0).json()["settings"]["weights"]
    assert w["soil"] == 0 and w["water"] > 0 and w["money"] > w["water"]
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)


def test_scenario_and_horizon_are_echoed_with_the_window(client):
    s = plans(client, scenario="ssp585", horizon=2050).json()["settings"]
    assert s["scenario"] == "ssp585" and s["horizon"] == 2050 and s["window"] == [2040, 2059]
    s = plans(client, horizon=2060).json()["settings"]
    assert s["window"] == [2050, 2060]


def test_irrigated_override(client):
    guess = plans(client, WET).json()["settings"]
    assert guess["irrigated"] is True and guess["irrigation_source"] == "guess"
    off = plans(client, WET, irrigated="false").json()["settings"]
    assert off["irrigated"] is False and off["irrigation_source"] == "override"
    on = plans(client, DRY, irrigated="true").json()["settings"]
    assert on["irrigated"] is True and on["irrigation_source"] == "override"


@pytest.mark.parametrize("params", [
    {"soil": -1}, {"water": -0.5, "money": 1},
    {"soil": 0, "water": 0, "money": 0, "risk": 0},
    {"scenario": "ssp126"}, {"scenario": ""},
    {"horizon": 2024}, {"horizon": 2061}, {"horizon": 1999},
    {"soil": "nan"}, {"money": "inf"},
])
def test_bad_values_are_400_with_one_plain_sentence(client, params):
    response = plans(client, **params)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail.endswith(".") and "\n" not in detail


def test_not_a_number_is_422(client):
    assert plans(client, soil="lots").status_code == 422
    assert plans(client, horizon="soon").status_code == 422


def test_unknown_field_is_404(client):
    response = plans(client, "KS-999999")
    assert response.status_code == 404 and "KS-999999" in response.json()["detail"]
    assert plans(client, "KS-999999", soil=-1).status_code == 404


def test_a_planner_value_error_is_400(client, monkeypatch):
    def refuse(*a, **k):
        raise ValueError("weight soil=-1: expected a number >= 0")
    monkeypatch.setattr(smart, "rank_plans_smart", refuse)
    response = plans(client)
    assert response.status_code == 400 and response.json()["detail"].startswith("weight soil")


def test_an_unexpected_error_falls_back_to_basic_and_logs(client, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("table exploded")
    monkeypatch.setattr(smart, "rank_plans_smart", boom)
    with caplog.at_level("WARNING"):
        response = plans(client)
    assert response.status_code == 200 and response.json()["engine"] == "basic"
    assert len(response.json()["plans"]) == 3
    assert "table exploded" in caplog.text


# --- the basic path ----------------------------------------------------------------------------------

def old_body(field_id, planner=None):
    from api.planner import rank_plans
    record_input = base.default_rows()
    row = next(r for r in record_input if r["field_id"] == field_id)
    import json
    history = json.loads(row["crop_history"])
    ranked = (planner or rank_plans)(FieldInput(field_id, history, row["organic_matter_pct"],
                                                row["slope_pct"], row["area_ha"]))
    return {"field_id": field_id, "plans": [main.plan_to_json(p) for p in ranked]}


def test_basic_fallback_when_the_climate_tables_are_missing(basic_client):
    response = plans(basic_client, soil=3, scenario="nonsense", horizon=1)      # parameters are ignored
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "basic"
    expected = old_body(DRY)
    assert {k: v for k, v in body.items() if k != "engine"} == expected
    assert list(body) == ["field_id", "engine", "plans"]
    assert all(list(p) == OLD_KEYS for p in body["plans"])


def test_a_custom_planner_always_uses_the_basic_path(tables):
    def stub(field: FieldInput) -> list[Plan]:
        return [Plan("Stub", ["corn"] * 4, Scores(0.5, 0.5, 0.5, 0.5), "A stub.")]
    with TestClient(main.create_app(planner=stub)) as c:
        body = plans(c, soil=1).json()
    assert body["engine"] == "basic" and body["plans"][0]["name"] == "Stub"
    assert smart.available(), "tables are there; only the custom planner kept the basic path"


def test_planner_engine_basic_switches_the_smart_planner_off(tables, monkeypatch):
    monkeypatch.setenv("PLANNER_ENGINE", "basic")
    with TestClient(main.create_app()) as c:
        body = plans(c).json()
        assert body["engine"] == "basic" and list(body) == ["field_id", "engine", "plans"]
        assert plans(c, soil=-1).status_code == 200
        assert c.get("/health").json()["climate"] is True       # the tables are still there
    monkeypatch.setenv("PLANNER_ENGINE", "smart")
    with TestClient(main.create_app()) as c:
        assert plans(c).json()["engine"] == "smart"


# --- /climate and /health ----------------------------------------------------------------------------

def test_climate_when_built(client):
    body = client.get("/climate").json()
    assert body["available"] is True and set(body["scenarios"]) == {"ssp245", "ssp585"}
    assert [row["horizon"] for row in body["scenarios"]["ssp245"]] == list(range(2025, 2061, 5))
    assert body == smart.climate_summary()


def test_climate_when_not_built(basic_client):
    response = basic_client.get("/climate")
    assert response.status_code == 200 and response.json() == {"available": False, "reason": "not_built"}


def test_health_climate_flag_true(client):
    assert client.get("/health").json()["climate"] is True


def test_health_climate_flag_false(basic_client):
    body = basic_client.get("/health").json()
    assert body["climate"] is False and body["status"] == "ok"


def test_startup_warms_the_planner_once(tables, monkeypatch):
    smart.reset()
    calls = []
    real = smart.load_engine
    monkeypatch.setattr(smart, "load_engine", lambda *a, **k: calls.append(1) or real(*a, **k))
    with TestClient(main.create_app()) as c:
        assert calls == [1]
        plans(c)
        plans(c, horizon=2040)
        assert calls == [1]


def test_startup_works_with_no_climate_data(no_tables):
    with TestClient(main.create_app()) as c:
        assert c.get("/health").status_code == 200


# --- the real tables ---------------------------------------------------------------------------------

needs_real = pytest.mark.skipif(
    not all((DATA / n).is_file() for n in ("climate_futures.parquet", "climate_monthly.parquet", "fields.parquet")),
    reason="real climate tables or fields not built",
)


@needs_real
def test_real_tables_serve_a_real_field(monkeypatch):
    for var in list(ENV_PATHS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("PLANNER_ENGINE", raising=False)
    monkeypatch.delenv("FIELDS_PATH", raising=False)
    smart.reset()
    with TestClient(main.create_app()) as c:
        if c.get("/health").json()["is_fake"]:
            pytest.skip("real fields not loaded")
        body = c.get("/field/KS-013847/plans", params={"scenario": "ssp585", "horizon": 2060}).json()
        assert body["engine"] == "smart" and len(body["plans"]) == 3
        assert body["settings"]["window"] == [2050, 2060]
        assert c.get("/climate").json()["available"] is True
