"""Tests for pipeline/satellite.py. No network, no Earth Engine: the Earth Engine layer is faked."""
from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from shapely.geometry import box  # noqa: E402

from pipeline import satellite as sat  # noqa: E402
from pipeline.satellite_schema import (  # noqa: E402
    COLUMNS,
    SatelliteSchemaError,
    load_satellite,
    validate_satellite,
)


def make_fields(n: int = 10, seed: int = 0) -> gpd.GeoDataFrame:
    """n small square fields scattered over a Finney-sized box, valid against schema.md."""
    rng = np.random.default_rng(seed)
    xs = rng.uniform(-101.0, -100.3, n)
    ys = rng.uniform(37.8, 38.2, n)
    return gpd.GeoDataFrame(
        {
            "field_id": [f"KS-{i:06d}" for i in range(1, n + 1)],
            "crop_history": [json.dumps({"2015": "corn", "2016": "fallow"})] * n,
            "organic_matter_pct": [1.5] * n,
            "slope_pct": [0.5] * n,
            "area_ha": [16.0] * n,
        },
        geometry=[box(x, y, x + 0.004, y + 0.004) for x, y in zip(xs, ys)],
        crs="EPSG:4326",
    )[["field_id", "geometry", "crop_history", "organic_matter_pct", "slope_pct", "area_ha"]]


class FakeEngine:
    """Stands in for EarthEngine.reduce: deterministic numbers, records every request."""

    def __init__(self, fail=None):
        self.calls: list[tuple[tuple[str, ...], int, int]] = []
        self.fail = fail  # callable(chunk, year, tile_scale, call_number) -> exception or None
        self._lock = threading.Lock()

    @staticmethod
    def value(field_id: str, year: int) -> list[float]:
        k = int(field_id.split("-")[1])
        return [0.2 + 0.001 * k + 0.0001 * (year - 2000), 0.1 + 0.002 * k, 20.0 + k]

    def reduce(self, chunk, year, tile_scale=1):
        with self._lock:
            self.calls.append((chunk.field_ids, year, tile_scale))
            n = len(self.calls)
        if self.fail is not None:
            exc = self.fail(chunk, year, tile_scale, n)
            if exc is not None:
                raise exc
        return {fid: self.value(fid, year) for fid in chunk.field_ids}


def no_sleep(_seconds):
    pass


@pytest.fixture(autouse=True)
def no_real_earth_engine(monkeypatch, tmp_path):
    """Any test that reaches the real Earth Engine layer or the real cache fails loudly / is isolated."""

    class Boom:
        def __init__(self):
            raise AssertionError("the test tried to initialise Earth Engine")

    monkeypatch.setattr(sat, "EarthEngine", Boom)
    monkeypatch.setattr(sat, "CACHE_DIR", tmp_path / "gee-default")


# ----------------------------------------------------------------------------- chunking


def test_chunks_cover_every_field_once_and_respect_size():
    fields = make_fields(23)
    chunks = sat.make_chunks(fields, 5)
    assert [len(c) for c in chunks] == [5, 5, 5, 5, 3]
    ids = [fid for c in chunks for fid in c.field_ids]
    assert sorted(ids) == sorted(fields["field_id"])
    assert all(len(c.field_ids) == len(c.geometries) for c in chunks)


def test_chunks_do_not_depend_on_row_order():
    fields = make_fields(23)
    shuffled = fields.sample(frac=1, random_state=3).reset_index(drop=True)
    a, b = sat.make_chunks(fields, 5), sat.make_chunks(shuffled, 5)
    assert [c.field_ids for c in a] == [c.field_ids for c in b]
    assert [c.digest for c in a] == [c.digest for c in b]


def test_chunks_are_spatially_compact():
    fields = make_fields(200, seed=1)
    chunks = sat.make_chunks(fields, 20)
    full = fields.total_bounds
    full_area = (full[2] - full[0]) * (full[3] - full[1])
    areas = []
    for c in chunks:
        w, s, e, n = sat.chunk_bounds(c)
        areas.append((e - w) * (n - s))
    assert np.mean(areas) < 0.3 * full_area  # 10 chunks; random chunks would each span ~the whole box


def test_chunk_geometry_is_rounded_geojson_and_bounds_contain_it():
    fields = make_fields(3)
    (chunk,) = sat.make_chunks(fields, 10)
    for geometry in chunk.geometries:
        assert geometry["type"] == "Polygon"
        for x, y in geometry["coordinates"][0]:
            assert round(x, sat.COORD_DECIMALS) == x and round(y, sat.COORD_DECIMALS) == y
    w, s, e, n = sat.chunk_bounds(chunk)
    minx, miny, maxx, maxy = fields.total_bounds
    assert w < minx and s < miny and e > maxx and n > maxy


def test_chunk_split_halves_keep_all_fields():
    (chunk,) = sat.make_chunks(make_fields(7), 10)
    a, b = chunk.split()
    assert len(a) == 3 and len(b) == 4
    assert a.field_ids + b.field_ids == chunk.field_ids
    assert a.digest != b.digest != chunk.digest


def test_chunk_size_must_be_positive():
    with pytest.raises(ValueError):
        sat.make_chunks(make_fields(3), 0)


# ----------------------------------------------------------------------------- cache keys


def test_cache_key_is_stable_and_depends_on_year_ids_geometry_and_method(monkeypatch):
    fields = make_fields(4)
    (chunk,) = sat.make_chunks(fields, 10)
    key = sat.cache_key(chunk, 2015)
    assert key == sat.cache_key(sat.make_chunks(fields, 10)[0], 2015)
    assert key != sat.cache_key(chunk, 2016)

    renamed = fields.copy()
    renamed.loc[0, "field_id"] = "KS-999999"
    assert sat.cache_key(sat.make_chunks(renamed, 10)[0], 2015) != key

    moved = fields.copy()
    moved.loc[0, "geometry"] = box(-100.5, 38.0, -100.49, 38.01)
    assert sat.cache_key(sat.make_chunks(moved, 10)[0], 2015) != key

    monkeypatch.setattr(sat, "METHOD_VERSION", sat.METHOD_VERSION + "-test")
    assert sat.cache_key(chunk, 2015) != key


def test_cache_key_changes_with_the_contract_constants(monkeypatch):
    (chunk,) = sat.make_chunks(make_fields(4), 10)
    key = sat.cache_key(chunk, 2015)
    monkeypatch.setattr(sat, "GREEN_NDVI", 0.4)
    assert sat.cache_key(chunk, 2015) != key


def test_cache_roundtrip_and_unusable_files(tmp_path):
    (chunk,) = sat.make_chunks(make_fields(3), 10)
    assert sat.read_cache(tmp_path, chunk, 2015) is None
    values = {fid: [0.5, 0.25, 30.0] for fid in chunk.field_ids}
    values[chunk.field_ids[0]] = [None, None, 0.0]
    sat.write_cache(tmp_path, chunk, 2015, values)
    assert sat.read_cache(tmp_path, chunk, 2015) == values
    assert sat.read_cache(tmp_path, chunk, 2016) is None
    assert not list(tmp_path.glob("*.part"))

    path = sat.cache_path(tmp_path, chunk, 2015)
    path.write_text("{ truncated")
    assert sat.read_cache(tmp_path, chunk, 2015) is None
    path.write_text(json.dumps({"values": {chunk.field_ids[0]: [0.1, 0.1, 1.0]}}))  # a field is missing
    assert sat.read_cache(tmp_path, chunk, 2015) is None


# ----------------------------------------------------------------------------- collect / resume


def test_collect_requests_each_chunk_year_once_then_uses_the_cache(tmp_path):
    chunks = sat.make_chunks(make_fields(10), 4)  # 3 chunks
    engine = FakeEngine()
    stats = sat.Stats()
    results = sat.collect(chunks, [2015, 2016], tmp_path, compute=engine.reduce, workers=3, stats=stats)
    assert len(engine.calls) == 6 and stats.requests == 6 and stats.cache_hits == 0
    assert len(results) == 20
    assert results[("KS-000003", 2016)] == FakeEngine.value("KS-000003", 2016)

    again = FakeEngine()
    stats = sat.Stats()
    assert sat.collect(chunks, [2015, 2016], tmp_path, compute=again.reduce, stats=stats) == results
    assert again.calls == [] and stats.requests == 0 and stats.cache_hits == 6


def test_fully_cached_run_never_initialises_earth_engine(tmp_path):
    chunks = sat.make_chunks(make_fields(6), 3)
    sat.collect(chunks, [2015], tmp_path, compute=FakeEngine().reduce)
    # compute=None would build sat.EarthEngine(), which the autouse fixture turns into an error
    results = sat.collect(chunks, [2015], tmp_path)
    assert len(results) == 6


def test_interrupted_run_resumes_where_it_stopped(tmp_path):
    chunks = sat.make_chunks(make_fields(12), 3)  # 4 chunks x 2 years = 8 requests

    def fail_after_three(chunk, year, tile_scale, n):
        return RuntimeError("Invalid argument: something unexpected") if n > 3 else None

    first = FakeEngine(fail=fail_after_three)
    with pytest.raises(sat.SatelliteError, match="something unexpected"):
        sat.collect(chunks, [2015, 2016], tmp_path, compute=first.reduce, workers=1, sleep=no_sleep)
    done = len(list(tmp_path.glob("*.json")))
    assert done == 3

    second = FakeEngine()
    results = sat.collect(chunks, [2015, 2016], tmp_path, compute=second.reduce, workers=2)
    assert len(second.calls) == 8 - done
    assert len(results) == 24


def test_force_ignores_the_cache_but_rewrites_it(tmp_path):
    chunks = sat.make_chunks(make_fields(4), 2)
    sat.collect(chunks, [2015], tmp_path, compute=FakeEngine().reduce)
    engine = FakeEngine()
    sat.collect(chunks, [2015], tmp_path, compute=engine.reduce, force=True)
    assert len(engine.calls) == 2
    assert sat.read_cache(tmp_path, chunks[0], 2015) is not None


# ----------------------------------------------------------------------------- retries


@pytest.mark.parametrize(
    "message, kind",
    [
        ("Too many concurrent aggregations.", "busy"),
        ("HTTP 429: Too Many Requests", "busy"),
        ("Quota exceeded: Earth Engine", "busy"),
        ("Computation timed out.", "too_big"),
        ("User memory limit exceeded.", "too_big"),
        ("An internal error has occurred", "transient"),
        ("The service is currently unavailable.", "transient"),
        ("Connection reset by peer", "transient"),
        ("Image.select: Band pattern 'SR_B9' did not match any bands.", "fatal"),
    ],
)
def test_classify_error(message, kind):
    assert sat.classify_error(RuntimeError(message)) == kind


def test_busy_errors_are_retried_with_growing_waits(tmp_path):
    (chunk,) = sat.make_chunks(make_fields(3), 10)
    engine = FakeEngine(
        fail=lambda c, y, t, n: RuntimeError("Too many concurrent aggregations.") if n <= 3 else None
    )
    waits = []
    stats = sat.Stats()
    values = sat.fetch_chunk_year(engine.reduce, chunk, 2015, tmp_path, stats=stats, sleep=waits.append)
    assert set(values) == set(chunk.field_ids)
    assert len(engine.calls) == 4 and stats.retries == 3 and stats.requests == 4
    assert len(waits) == 3 and waits[0] < waits[1] < waits[2] and waits[0] >= 10
    assert sat.read_cache(tmp_path, chunk, 2015) == values


def test_gives_up_after_max_attempts_with_a_resume_hint(tmp_path):
    (chunk,) = sat.make_chunks(make_fields(3), 10)
    engine = FakeEngine(fail=lambda c, y, t, n: RuntimeError("HTTP 429 Too Many Requests"))
    with pytest.raises(sat.SatelliteError, match="run the same command again"):
        sat.fetch_chunk_year(engine.reduce, chunk, 2015, tmp_path, sleep=no_sleep, max_attempts=4)
    assert len(engine.calls) == 4
    assert sat.read_cache(tmp_path, chunk, 2015) is None


def test_timeouts_are_retried_as_smaller_chunks_and_cached_under_the_big_key(tmp_path):
    (chunk,) = sat.make_chunks(make_fields(8), 10)
    engine = FakeEngine(fail=lambda c, y, t, n: RuntimeError("Computation timed out.") if len(c) > 2 else None)
    stats = sat.Stats()
    values = sat.fetch_chunk_year(engine.reduce, chunk, 2015, tmp_path, stats=stats, sleep=no_sleep)
    assert values == {fid: FakeEngine.value(fid, 2015) for fid in chunk.field_ids}
    assert sorted(len(ids) for ids, _, _ in engine.calls) == [2, 2, 2, 2, 4, 4, 8]
    assert stats.splits == 3
    assert sat.read_cache(tmp_path, chunk, 2015) == values  # the original chunk is cached too...

    again = FakeEngine()
    assert sat.fetch_chunk_year(again.reduce, chunk, 2015, tmp_path) == values
    assert again.calls == []  # ...so a rerun makes no request


def test_single_field_memory_error_retries_with_larger_tile_scale(tmp_path):
    (chunk,) = sat.make_chunks(make_fields(1), 10)
    engine = FakeEngine(fail=lambda c, y, t, n: RuntimeError("User memory limit exceeded.") if t < 4 else None)
    values = sat.fetch_chunk_year(engine.reduce, chunk, 2015, tmp_path, sleep=no_sleep)
    assert [t for _, _, t in engine.calls] == [1, 2, 4]
    assert list(values) == list(chunk.field_ids)

    hopeless = FakeEngine(fail=lambda c, y, t, n: RuntimeError("User memory limit exceeded."))
    with pytest.raises(sat.SatelliteError, match="could not compute field"):
        sat.fetch_chunk_year(hopeless.reduce, chunk, 2016, tmp_path, sleep=no_sleep)


def test_unknown_errors_fail_at_once(tmp_path):
    (chunk,) = sat.make_chunks(make_fields(2), 10)
    engine = FakeEngine(fail=lambda c, y, t, n: RuntimeError("Image.select: no such band"))
    with pytest.raises(sat.SatelliteError, match="no such band"):
        sat.fetch_chunk_year(engine.reduce, chunk, 2015, tmp_path, sleep=no_sleep)
    assert len(engine.calls) == 1


def test_a_field_missing_from_the_response_is_an_error(tmp_path):
    (chunk,) = sat.make_chunks(make_fields(3), 10)

    def compute(c, year, tile_scale):
        return {fid: [0.5, 0.5, 10.0] for fid in c.field_ids[:-1]}

    with pytest.raises(sat.SatelliteError, match="returned no row"):
        sat.fetch_chunk_year(compute, chunk, 2015, tmp_path, sleep=no_sleep)
    assert sat.read_cache(tmp_path, chunk, 2015) is None


# ----------------------------------------------------------------------------- Earth Engine helpers


def test_sums_to_means():
    assert sat.sums_to_means([30.0, 60, 12.0, 60, 1500.0, 60]) == [0.5, 0.2, 25.0]
    # 100 pixels, 60 of them never clear: the two means use the clear pixels, n_obs uses all of them
    peak, cover, n_obs = sat.sums_to_means([20.0, 40, 10.0, 40, 400.0, 100])
    assert (peak, cover, n_obs) == (0.5, 0.25, 4.0)
    assert sat.sums_to_means([0, 0, 0, 0, 0.0, 50]) == [None, None, 0.0]


def test_season_window_is_march_through_october():
    assert sat.season_window(2012) == ("2012-03-01", "2012-11-01")


def test_landsat_7_is_not_used_after_2019_and_oli_starts_2013():
    def used(year):
        return {s[0].split("/")[1] for s in sat.SENSORS if s[5] <= year <= s[6]}

    assert used(2008) == {"LT05", "LE07"}
    assert used(2012) == {"LT05", "LE07"}
    assert used(2013) == {"LE07", "LC08"}
    assert used(2019) == {"LE07", "LC08"}
    assert used(2020) == {"LC08"}
    assert used(2024) == {"LC08", "LC09"}


# ----------------------------------------------------------------------------- assembly


def test_assemble_has_every_pair_sorted_with_nulls_for_missing():
    results = {
        ("KS-000002", 2016): [0.81234567, 0.45678, 41.23456],
        ("KS-000002", 2015): [0.5, 0.25, 30.0],
        ("KS-000001", 2015): [None, None, 0.0],  # Earth Engine: no clear view
        # ("KS-000001", 2016) never came back
    }
    df = sat.assemble(["KS-000002", "KS-000001"], [2016, 2015], results)
    assert list(df.columns) == COLUMNS
    assert list(zip(df["field_id"], df["year"])) == [
        ("KS-000001", 2015), ("KS-000001", 2016), ("KS-000002", 2015), ("KS-000002", 2016),
    ]
    assert df["year"].dtype == np.int64
    assert all(df[c].dtype == np.float64 for c in ["ndvi_peak", "cover_frac", "n_obs"])
    assert df["ndvi_peak"].isna().tolist() == [True, True, False, False]
    assert df["cover_frac"].isna().tolist() == [True, True, False, False]
    assert df["n_obs"].tolist() == [0.0, 0.0, 30.0, 41.235]
    assert df["ndvi_peak"].iloc[3] == 0.812 and df["cover_frac"].iloc[3] == 0.457
    validate_satellite(df)


def test_assemble_is_deterministic_and_keeps_values_in_range():
    results = {("KS-000001", 2015): [1.0000001, -0.0000001, 12.0], ("KS-000001", 2016): [0.3, 0.2, float("nan")]}
    a = sat.assemble(["KS-000001"], [2015, 2016], results)
    b = sat.assemble(["KS-000001"], [2016, 2015], dict(reversed(list(results.items()))))
    pd.testing.assert_frame_equal(a, b)
    assert a["ndvi_peak"].iloc[0] == 1.0 and a["cover_frac"].iloc[0] == 0.0
    assert a["n_obs"].iloc[1] == 0.0 and np.isnan(a["ndvi_peak"].iloc[1])  # no n_obs -> no clear view
    validate_satellite(a)


def test_validation_rejects_a_broken_table():
    df = sat.assemble(["KS-000001"], [2015], {("KS-000001", 2015): [0.5, 0.5, 10.0]})
    validate_satellite(df)
    bad = df.copy()
    bad["cover_frac"] = 1.5
    with pytest.raises(SatelliteSchemaError, match="cover_frac"):
        validate_satellite(bad)
    with pytest.raises(SatelliteSchemaError, match="duplicate"):
        validate_satellite(pd.concat([df, df], ignore_index=True))


# ----------------------------------------------------------------------------- build / CLI


def test_build_writes_a_valid_file_atomically(tmp_path):
    fields_path = tmp_path / "fields.parquet"
    make_fields(10).to_parquet(fields_path)
    out = tmp_path / "out" / "field_satellite.parquet"
    engine = FakeEngine()
    df = sat.build(fields_path, out, [2015, 2016], cache_dir=tmp_path / "cache", compute=engine.reduce, chunk_size=4)
    assert len(engine.calls) == 6
    loaded = load_satellite(out)
    pd.testing.assert_frame_equal(loaded, df, check_dtype=False)  # parquet gives pandas string dtype
    assert len(loaded) == 20 and loaded["n_obs"].min() > 0
    assert not list(out.parent.glob("*.part"))
    row = loaded[(loaded["field_id"] == "KS-000007") & (loaded["year"] == 2016)].iloc[0]
    assert [row["ndvi_peak"], row["cover_frac"], row["n_obs"]] == [round(v, 3) for v in FakeEngine.value("KS-000007", 2016)]

    first = out.read_bytes()
    sat.build(fields_path, out, [2015, 2016], cache_dir=tmp_path / "cache", chunk_size=4)  # no engine: cache only
    assert out.read_bytes() == first


def test_build_max_fields_takes_neighbours(tmp_path):
    fields_path = tmp_path / "fields.parquet"
    make_fields(40, seed=2).to_parquet(fields_path)
    engine = FakeEngine()
    df = sat.build(
        fields_path, tmp_path / "o.parquet", [2015], max_fields=5, cache_dir=tmp_path / "c", compute=engine.reduce
    )
    assert df["field_id"].nunique() == 5 and len(engine.calls) == 1


def test_parse_years():
    assert sat.parse_years("2008-2010") == [2008, 2009, 2010]
    assert sat.parse_years("2015") == [2015]
    assert sat.parse_years("2013,2011") == [2011, 2013]
    for bad in ["", "abc", "2010-2008", "1900", "3000"]:
        with pytest.raises(Exception):
            sat.parse_years(bad)


def fake_ee_module(initialize):
    module = types.ModuleType("ee")
    module.Initialize = initialize
    module.data = types.SimpleNamespace(setDeadline=lambda ms: None)
    return module


def test_cli_not_authenticated_is_a_clear_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    fields_path = tmp_path / "fields.parquet"
    make_fields(3).to_parquet(fields_path)
    monkeypatch.undo()  # drop the autouse guard: this test exercises the real EarthEngine.__init__ ...
    monkeypatch.setattr(sat, "CACHE_DIR", tmp_path / "cache")

    def initialize(project=None):  # ... against a fake `ee` module that is not authenticated
        raise RuntimeError("Please authorize access to your Earth Engine account by running\n\nearthengine authenticate")

    monkeypatch.setitem(sys.modules, "ee", fake_ee_module(initialize))
    out = tmp_path / "out.parquet"
    code = sat.main(["--fields", str(fields_path), "--out", str(out), "--years", "2015"])
    captured = capsys.readouterr()
    assert code == 2
    assert "earthengine authenticate" in captured.err and "set_project" in captured.err
    assert "Traceback" not in captured.err and "Traceback" not in captured.out
    assert not out.exists()


def test_cli_honours_ee_project_env(tmp_path, monkeypatch):
    monkeypatch.undo()
    seen = []
    monkeypatch.setitem(sys.modules, "ee", fake_ee_module(lambda project=None: seen.append(project)))
    monkeypatch.setenv("EE_PROJECT", "my-project")
    sat.EarthEngine()
    monkeypatch.delenv("EE_PROJECT")
    sat.EarthEngine()
    monkeypatch.setenv("EE_PROJECT", "")
    sat.EarthEngine()
    assert seen == ["my-project", None, None]


def test_cli_missing_fields_file(tmp_path, capsys):
    code = sat.main(["--fields", str(tmp_path / "nope.parquet"), "--out", str(tmp_path / "o.parquet")])
    assert code == 2
    assert "fields file not found" in capsys.readouterr().err


def test_cli_end_to_end_with_fake_engine_then_from_cache(tmp_path, monkeypatch, capsys):
    fields_path = tmp_path / "fields.parquet"
    make_fields(6).to_parquet(fields_path)
    out = tmp_path / "sat.parquet"
    engine = FakeEngine()

    class Engine:
        def __init__(self):
            self.reduce = engine.reduce

    monkeypatch.setattr(sat, "EarthEngine", Engine)
    monkeypatch.setattr(sat, "CACHE_DIR", tmp_path / "cache")
    args = ["--fields", str(fields_path), "--out", str(out), "--years", "2015-2016"]
    assert sat.main(args) == 0
    assert len(engine.calls) == 2
    text = capsys.readouterr().out
    assert "rows: 12" in text and "Earth Engine requests: 2" in text
    first = out.read_bytes()

    assert sat.main(args) == 0  # rerun: everything cached
    assert len(engine.calls) == 2
    assert "Earth Engine requests: 0" in capsys.readouterr().out
    assert out.read_bytes() == first

    assert sat.main(args + ["--force"]) == 0
    assert len(engine.calls) == 4
    assert len(load_satellite(out)) == 12


def test_cli_bad_arguments(capsys):
    for args in (["--years", "soon"], ["--max-fields", "0"]):
        with pytest.raises(SystemExit) as exc:
            sat.main(args)
        assert exc.value.code == 2
    capsys.readouterr()
