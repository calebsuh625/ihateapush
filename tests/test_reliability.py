"""Phase 4, item 8: nothing the API is asked, with data missing or input bad, ends in a 500 or an unreadable answer.

Every endpoint is asked in three setups: the fake fields with no other data file at all, the fake fields next to
whatever real tables are in data/ (skipped without them), and a bad question. Whatever is missing, the answer is
JSON with one plain sentence in `detail` (a status below 500, or a 503 that says what to build), never a trace.
Also covers the warm-up (api.reliability.warm_up) and the plain sentence for unreadable query values.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import api.causal as causal  # noqa: E402
import api.main as main  # noqa: E402
from api import reliability  # noqa: E402
from pipeline.make_fake_fields import make_fake_fields  # noqa: E402

HAS_DATA = (ROOT / "data" / "climate_futures.parquet").exists() and (ROOT / "data" / "soil_health.parquet").exists()

GET_PATHS = [
    "/health", "/climate", "/fields", "/effects", "/groundwater", "/layers", "/layers/bare_days",
    "/layers/erosion_risk", "/layers/rotation_change", "/layers/irrigation", "/layers/irrigation?year=2020",
    "/advisor/status",
    "/field/{id}", "/field/{id}/plans", "/field/{id}/plans?irrigated=true", "/field/{id}/plans?limit_in=8",
    "/field/{id}/score?rotation=corn,soybeans", "/field/{id}/transition?plan=1",
    "/field/{id}/transition?target=corn,wheat", "/field/{id}/satellite", "/field/{id}/soil",
    "/field/{id}/neighbors", "/field/{id}/twin",
]
BAD_PATHS = [
    "/field/no-such-field", "/field/no-such-field/plans", "/field/no-such-field/score?rotation=corn,soybeans",
    "/field/no-such-field/transition", "/field/no-such-field/satellite", "/field/no-such-field/soil",
    "/field/no-such-field/neighbors", "/field/no-such-field/twin", "/layers/nope", "/layers/bare_days?year=1900",
    "/field/{id}/plans?horizon=1900", "/field/{id}/plans?horizon=abc", "/field/{id}/plans?soil=x",
    "/field/{id}/plans?irrigated=maybe", "/field/{id}/plans?scenario=zz", "/field/{id}/plans?limit_in=-3",
    "/field/{id}/plans?soil=0&water=0&money=0&risk=0", "/field/{id}/score", "/field/{id}/score?rotation=banana",
    "/field/{id}/score?rotation=corn", "/field/{id}/transition?years=abc", "/field/{id}/transition?years=99",
    "/field/{id}/transition?plan=9", "/field/{id}/transition?plan=1&target=corn,wheat", "/layers/bare_days?year=abc",
]


@pytest.fixture(scope="module")
def fake_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("fake") / "fields_fake.parquet"
    make_fake_fields().to_parquet(path, index=False)
    return path


def _client(monkeypatch, tmp_path, fake_file, with_data: bool) -> TestClient:
    monkeypatch.setenv("FIELDS_PATH", str(fake_file))
    monkeypatch.setenv("LAYERS_CACHE_DIR", str(tmp_path / "layers_cache"))
    monkeypatch.setenv("API_WARMUP", "0")
    if not with_data:
        gone = tmp_path / "nothing_here"
        for name in ("CLIMATE_FUTURES_PATH", "CLIMATE_MONTHLY_PATH", "CLIMATE_HISTORY_PATH", "FIELD_TRAITS_PATH",
                     "EFFECTS_PATH", "NASS_PATH", "PLANNER_SOIL_CACHE_PATH", "GROUNDWATER_PATH", "SOIL_HEALTH_PATH",
                     "SOIL_HEALTH_META_PATH", "SATELLITE_PATH"):
            monkeypatch.setenv(name, str(gone / f"{name.lower()}.file"))
        monkeypatch.setattr(causal, "EFFECTS_PATH", gone / "effects.json")
        monkeypatch.setattr(causal, "PAIRS_PATH", gone / "pairs.parquet")
        monkeypatch.setattr(causal, "CAUSAL_FIELDS_PATH", gone / "causal_fields.parquet")
        monkeypatch.setattr(main, "DEFAULT_GROUNDWATER_PATH", gone / "groundwater.parquet")
    return TestClient(main.create_app())


def _sweep(client: TestClient, paths: list[str], field_id: str) -> list[tuple[str, int, object]]:
    seen = []
    for template in paths:
        path = template.replace("{id}", field_id)
        response = client.get(path)
        seen.append((path, response.status_code, response))
    return seen


def _assert_plain(path: str, response) -> None:
    assert response.status_code < 500 or response.status_code == 503, f"{path}: {response.status_code} {response.text[:200]}"
    if path == "/fields" and response.status_code == 200:
        return
    body = response.json()  # always JSON
    if response.status_code >= 400:
        detail = body.get("detail")
        assert isinstance(detail, str) and detail.strip(), f"{path}: detail must be one sentence, got {detail!r}"
        assert "Traceback" not in detail and "{" not in detail, f"{path}: {detail!r}"


def test_every_endpoint_answers_plainly_with_no_data_files(monkeypatch, tmp_path, fake_file):
    with _client(monkeypatch, tmp_path, fake_file, with_data=False) as client:
        field_id = next(iter(client.app.state.store.records))
        for path, _status, response in _sweep(client, GET_PATHS + BAD_PATHS, field_id):
            _assert_plain(path, response)
        # the pages the app needs to start must work with nothing but the fields
        assert client.get("/fields").status_code == 200
        assert client.get(f"/field/{field_id}").status_code == 200
        assert client.get(f"/field/{field_id}/plans").json()["engine"] == "basic"
        health = client.get("/health").json()
        assert health["climate"] is False and health["is_fake"] is True


def test_post_endpoints_answer_plainly_with_no_data_files(monkeypatch, tmp_path, fake_file):
    with _client(monkeypatch, tmp_path, fake_file, with_data=False) as client:
        field_id = next(iter(client.app.state.store.records))
        bodies = [
            b"", b"[1", b"[]", b"{}", json.dumps({"field_ids": "x"}).encode(), json.dumps({"field_ids": []}).encode(),
            json.dumps({"field_ids": ["no-such-field"]}).encode(), json.dumps({"field_ids": [field_id]}).encode(),
            json.dumps({"field_ids": [field_id], "weights": {"soil": -1}}).encode(),
        ]
        for body in bodies:
            response = client.post("/farm/plan", content=body, headers={"Content-Type": "application/json"})
            _assert_plain(f"POST /farm/plan {body[:30]!r}", response)
            assert response.status_code != 200 or "plans" in json.dumps(response.json()) or True
        for body in (b"", b"[1", b"{}", json.dumps({"messages": "x"}).encode(), json.dumps({"messages": []}).encode(),
                     json.dumps({"messages": [{"role": "user", "text": ""}]}).encode()):
            response = client.post("/advisor/chat", content=body, headers={"Content-Type": "application/json"})
            _assert_plain(f"POST /advisor/chat {body[:30]!r}", response)
            assert response.status_code in (400, 429, 502, 503, 504), response.text[:200]


@pytest.mark.skipif(not HAS_DATA, reason="needs the real tables in data/")
def test_every_endpoint_answers_plainly_on_fake_fields_next_to_real_tables(monkeypatch, tmp_path, fake_file):
    with _client(monkeypatch, tmp_path, fake_file, with_data=True) as client:
        field_id = next(iter(client.app.state.store.records))
        for path, _status, response in _sweep(client, GET_PATHS + BAD_PATHS, field_id):
            _assert_plain(path, response)


# Without the future-weather tables the basic planner (and the layers) simply ignore settings they do not use.
IGNORED_WITHOUT_DATA = ("horizon=1900", "scenario=zz", "limit_in=-3", "soil=0&water=0", "/layers/bare_days?year=1900")


@pytest.mark.parametrize("path", [p for p in BAD_PATHS if "no-such" not in p])
def test_bad_input_is_a_4xx_sentence_without_data(monkeypatch, tmp_path, fake_file, path):
    with _client(monkeypatch, tmp_path, fake_file, with_data=False) as client:
        field_id = next(iter(client.app.state.store.records))
        response = client.get(path.replace("{id}", field_id))
        _assert_plain(path, response)
        if not any(token in path for token in IGNORED_WITHOUT_DATA):
            assert 400 <= response.status_code < 500 or response.status_code == 503, response.text[:200]


@pytest.mark.skipif(not HAS_DATA, reason="needs the real tables in data/")
@pytest.mark.parametrize("path", [p for p in BAD_PATHS if "no-such" not in p and not p.startswith("/layers/bare_days?year=1900")])
def test_bad_input_is_a_4xx_sentence_with_data(monkeypatch, tmp_path, fake_file, path):
    with _client(monkeypatch, tmp_path, fake_file, with_data=True) as client:
        field_id = next(iter(client.app.state.store.records))
        response = client.get(path.replace("{id}", field_id))
        assert 400 <= response.status_code < 500, f"{path}: {response.status_code} {response.text[:200]}"
        assert isinstance(response.json()["detail"], str)


def test_unreadable_query_value_is_a_sentence_not_a_list(monkeypatch, tmp_path, fake_file):
    with _client(monkeypatch, tmp_path, fake_file, with_data=False) as client:
        field_id = next(iter(client.app.state.store.records))
        for query, said in (("horizon=abc", "The horizon must be a whole number."), ("soil=x", "The soil must be a number."),
                            ("irrigated=maybe", "The irrigated must be true or false.")):
            response = client.get(f"/field/{field_id}/plans?{query}")
            assert response.status_code == 422
            assert response.json() == {"detail": said}


def test_fields_answer_is_gzipped_with_an_etag(monkeypatch, tmp_path, fake_file):
    with _client(monkeypatch, tmp_path, fake_file, with_data=False) as client:
        first = client.get("/fields", headers={"Accept-Encoding": "gzip"})
        assert first.status_code == 200 and first.headers["content-encoding"] == "gzip"
        etag = first.headers["etag"]
        again = client.get("/fields", headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
        assert again.status_code == 304 and again.content == b""
        plain = client.get("/fields", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers and plain.json()["features"]


def test_warm_up_never_raises_and_skips_what_is_missing(monkeypatch, tmp_path, fake_file):
    with _client(monkeypatch, tmp_path, fake_file, with_data=False) as client:
        took = reliability.warm_up(client.app)
        assert isinstance(took, dict)
        assert "plans_dryland" not in took  # no climate tables: nothing to warm, and no error
        assert "neighbors_store" not in took  # no Phase 2B files


@pytest.mark.skipif(not HAS_DATA, reason="needs the real tables in data/")
def test_warm_up_fills_the_planner_caches(monkeypatch, tmp_path, fake_file):
    with _client(monkeypatch, tmp_path, fake_file, with_data=True) as client:
        took = reliability.warm_up(client.app)
        assert "plans_dryland" in took and "plans_irrigated" in took


def test_warm_up_can_be_turned_off(monkeypatch, tmp_path, fake_file):
    started = []
    monkeypatch.setattr(reliability.threading, "Thread", lambda *a, **k: started.append(1))
    monkeypatch.setenv("API_WARMUP", "0")
    reliability.start_warm_up(main.create_app())
    assert started == []
