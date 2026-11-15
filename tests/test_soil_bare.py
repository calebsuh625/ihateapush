"""Tests for pipeline/soil/bare_soil.py (+ bare_proto.py, bare_check.py). No network: Earth Engine is faked."""
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
from shapely.geometry import Polygon, box, shape  # noqa: E402

from pipeline import satellite as sat  # noqa: E402
from pipeline.soil import bare_check as bc  # noqa: E402
from pipeline.soil import bare_proto as bp  # noqa: E402
from pipeline.soil import bare_soil as bs  # noqa: E402

COLS = ["field_id", "geometry", "crop_history", "organic_matter_pct", "slope_pct", "area_ha"]


def fields_from(geometries, crs="EPSG:4326", om=None) -> gpd.GeoDataFrame:
    n = len(geometries)
    gdf = gpd.GeoDataFrame(
        {
            "field_id": [f"KS-{i:06d}" for i in range(1, n + 1)],
            "crop_history": [json.dumps({"2016": "corn", "2017": "fallow"})] * n,
            "organic_matter_pct": list(om) if om is not None else [1.5] * n,
            "slope_pct": [0.5] * n,
            "area_ha": [16.0] * n,
        },
        geometry=list(geometries),
        crs=crs,
    )
    return gdf.to_crs("EPSG:4326")[COLS]


def make_fields(n: int = 10, seed: int = 0) -> gpd.GeoDataFrame:
    """n square fields (~350 x 440 m) scattered over a Finney-sized box, valid against schema.md."""
    rng = np.random.default_rng(seed)
    xs = rng.uniform(-101.0, -100.3, n)
    ys = rng.uniform(37.8, 38.2, n)
    return fields_from([box(x, y, x + 0.004, y + 0.004) for x, y in zip(xs, ys)])


def block_fields(nx: int = 4, ny: int = 3, x0: float = -100.9, y0: float = 37.9) -> gpd.GeoDataFrame:
    """nx * ny touching square fields: one compact block."""
    return fields_from([box(x0 + 0.005 * i, y0 + 0.005 * j, x0 + 0.005 * (i + 1), y0 + 0.005 * (j + 1)) for j in range(ny) for i in range(nx)])


def sums(sensor: str, n_px: float, bare_px: float, reflectance: float, clear: float, bare: float) -> list[float]:
    """Cached sums of a field whose every pixel had `clear` clear and `bare` bare views in every month."""
    bands = [bare_px * (reflectance + 0.01 * i) for i in range(len(bs.SENSOR_BANDS[sensor]))]
    return [n_px, bare_px, *bands, *([n_px * clear] * 12), *([n_px * bare] * 12)]


class FakeEngine:
    """Stands in for BareSoilEngine.reduce: deterministic sums, records every request."""

    def __init__(self, fail=None):
        self.calls: list[tuple[tuple[str, ...], int, str, int]] = []
        self.fail = fail  # callable(chunk, year, sensor, tile_scale, call_number) -> exception or None
        self._lock = threading.Lock()

    @staticmethod
    def value(field_id: str, year: int, sensor: str) -> list[float]:
        k = int(field_id.split("-")[1])
        return sums(sensor, 50.0 + k, 40.0, 0.05 + 0.001 * k + 0.0001 * (year - 2000), 2.0, 0.5)

    def reduce(self, chunk, year, sensor, tile_scale=1):
        with self._lock:
            self.calls.append((chunk.field_ids, year, sensor, tile_scale))
            n = len(self.calls)
        if self.fail is not None:
            exc = self.fail(chunk, year, sensor, tile_scale, n)
            if exc is not None:
                raise exc
        return {fid: self.value(fid, year, sensor) for fid in chunk.field_ids}


def no_sleep(_seconds):
    pass


@pytest.fixture(autouse=True)
def no_real_earth_engine(monkeypatch, tmp_path):
    """Any test that reaches the real Earth Engine layer fails loudly; cache and logs are isolated."""

    class Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("the test tried to initialise Earth Engine")

    monkeypatch.setattr(bs, "BareSoilEngine", Boom)
    monkeypatch.setattr(sat, "EarthEngine", Boom)
    monkeypatch.setattr(bs, "CACHE_DIR", tmp_path / "gee-soil-default")
    monkeypatch.setattr(bs, "LOG_DIR", tmp_path / "logs-default")


# ----------------------------------------------------------------------------- buffering


def test_buffer_inward_takes_30_m_off_every_side():
    square = fields_from([box(-200_000, 1_650_000, -199_600, 1_650_400)], crs="EPSG:5070")  # 400 m x 400 m
    shrunk = bs.buffer_inward(square).to_crs("EPSG:5070")
    assert shrunk.area.iloc[0] == pytest.approx(340.0 * 340.0, rel=1e-6)
    minx, miny, maxx, maxy = shrunk.total_bounds
    assert (minx, miny, maxx, maxy) == pytest.approx((-199_970, 1_650_030, -199_630, 1_650_370), abs=0.01)


def test_field_too_thin_for_the_buffer_is_reported_not_sent():
    fat, thin = box(-200_000, 1_650_000, -199_600, 1_650_400), box(-199_000, 1_650_000, -198_950, 1_650_400)  # 50 m wide
    fields = fields_from([fat, thin], crs="EPSG:5070")
    chunks, lost = bs.make_soil_chunks(fields)
    assert lost == ["KS-000002"]
    assert [c.field_ids for c in chunks] == [("KS-000001",)]


def test_all_fields_too_thin_gives_no_chunk():
    fields = fields_from([box(-199_000, 1_650_000, -198_950, 1_650_400)], crs="EPSG:5070")
    assert bs.make_soil_chunks(fields) == ([], ["KS-000001"])


def test_chunks_hold_the_shrunk_outlines_and_nothing_else():
    fields = block_fields(2, 2)
    (chunk,), lost = bs.make_soil_chunks(fields)
    assert lost == [] and sorted(chunk.field_ids) == sorted(fields["field_id"])
    originals = dict(zip(fields["field_id"], fields.geometry))
    for fid, geometry in zip(chunk.field_ids, chunk.geometries):
        assert set(geometry) == {"type", "coordinates"}  # no id, no crop, no soil value
        sent = shape(geometry)
        assert originals[fid].contains(sent) and sent.area < 0.9 * originals[fid].area
    w, s, e, n = bs.chunk_bounds(chunk)
    minx, miny, maxx, maxy = fields.total_bounds
    assert minx < w and miny < s and e < maxx and n < maxy  # the request box is inside the block


def test_field_that_splits_under_the_buffer_keeps_both_parts():
    # two 300 m squares joined by a 40 m wide neck: the neck disappears, the squares stay
    dumbbell = Polygon([
        (0, 0), (300, 0), (300, 130), (500, 130), (500, 0), (800, 0), (800, 300), (500, 300), (500, 170), (300, 170), (300, 300), (0, 300),
    ])
    fields = fields_from([Polygon([(x - 200_000, y + 1_650_000) for x, y in dumbbell.exterior.coords])], crs="EPSG:5070")
    (chunk,), lost = bs.make_soil_chunks(fields)
    assert lost == [] and chunk.geometries[0]["type"] == "MultiPolygon"
    w, s, e, n = bs.geojson_bounds(chunk.geometries[0])
    assert w < e and s < n and bs.chunk_bounds(chunk)[0] < w


def test_chunks_cover_every_field_once_whatever_the_row_order():
    fields = make_fields(40, seed=1)
    a, _ = bs.make_soil_chunks(fields, 7)
    b, _ = bs.make_soil_chunks(fields.sample(frac=1, random_state=3).reset_index(drop=True), 7)
    assert [c.digest for c in a] == [c.digest for c in b]
    assert sorted(fid for c in a for fid in c.field_ids) == sorted(fields["field_id"])
    assert max(len(c) for c in a) <= 7
    with pytest.raises(ValueError):
        bs.make_soil_chunks(fields, 0)


def test_far_apart_clusters_never_share_a_request():
    near = block_fields(3, 2, x0=-100.9, y0=37.9)
    far = block_fields(3, 2, x0=-100.4, y0=38.1)  # ~45 km away
    fields = fields_from(list(near.geometry) + list(far.geometry))
    chunks, _ = bs.make_soil_chunks(fields, 300)
    assert sorted(len(c) for c in chunks) == [6, 6]
    for chunk in chunks:
        w, s, e, n = bs.chunk_bounds(chunk)
        assert e - w < 0.05 and n - s < 0.05


def test_a_compact_block_is_one_request():
    chunks, _ = bs.make_soil_chunks(block_fields(6, 5), 300)
    assert [len(c) for c in chunks] == [30]


def test_no_request_box_is_larger_than_the_largest_one_measured():
    """Fields as thick on the ground as in the county: without the cap a chunk of 300 spans hundreds of km2."""
    # 900 fields of 0.005 degrees on a 0.011 degree grid (about 29 x 37 km, a sixth of it under fields)
    fields = fields_from([box(-101.0 + 0.011 * i, 37.8 + 0.011 * j, -100.995 + 0.011 * i, 37.805 + 0.011 * j) for j in range(30) for i in range(30)])
    capped, _ = bs.make_soil_chunks(fields)
    assert max(bs.chunk_box_km2(c) for c in capped) <= bs.MAX_BOX_KM2 <= bs.COST_MEASURED_UP_TO_KM2
    assert sorted(fid for c in capped for fid in c.field_ids) == sorted(fields["field_id"])
    free, _ = bs.make_soil_chunks(fields, max_box_km2=1e9)
    assert max(bs.chunk_box_km2(c) for c in free) > bs.MAX_BOX_KM2 and len(capped) > len(free)
    again, _ = bs.make_soil_chunks(fields.sample(frac=1, random_state=1).reset_index(drop=True))
    assert [c.digest for c in again] == [c.digest for c in capped]
    small = block_fields(6, 5)  # a chunk that was never near the cap is the same chunk as before: its cache stays valid
    assert [c.digest for c in bs.make_soil_chunks(small)[0]] == [c.digest for c in bs.make_soil_chunks(small, max_box_km2=1e9)[0]]


def test_box_area_and_cost_estimate(tmp_path):
    (chunk,), _ = bs.make_soil_chunks(block_fields(10, 10))  # 100 fields of 0.005 degrees: about 4.4 x 5.5 km
    area = bs.chunk_box_km2(chunk)
    assert area == pytest.approx(0.05 * 111.32 * np.cos(np.radians(37.925)) * 0.05 * 110.95, rel=0.05)
    assert bs.request_cost_eecu_s("s2", 0) == 15 and bs.request_cost_eecu_s("s2", 67) == 192
    assert bs.request_cost_eecu_s("s2", 103) == pytest.approx((192 + 399) / 2)  # between the measured points
    assert bs.request_cost_eecu_s("s2", 200) > bs.request_cost_eecu_s("s2", 139) > bs.request_cost_eecu_s("landsat", 139)
    est = bs.estimate_cost([chunk], [2016, 2017, 2018], bs.SENSORS, tmp_path)
    assert est["sensors"]["landsat"]["requests"] == 3 and est["sensors"]["s2"]["requests"] == 2 and est["requests"] == 5
    assert est["eecu_hours"] == pytest.approx((3 * bs.request_cost_eecu_s("landsat", area) + 2 * bs.request_cost_eecu_s("s2", area)) / 3600)
    bs.collect([chunk], [2017], ["s2"], tmp_path, compute=FakeEngine().reduce)
    after = bs.estimate_cost([chunk], [2016, 2017, 2018], bs.SENSORS, tmp_path)
    assert after["sensors"]["s2"] == {**after["sensors"]["s2"], "requests": 1, "cached": 1}
    assert bs.estimate_cost([chunk], [2017], ["s2"], tmp_path, force=True)["requests"] == 1
    assert any("EECU-hours" in line for line in bs.estimate_lines(est))


def test_spent_eecu_counts_an_unprofiled_request_at_its_sensors_mean():
    costs = [
        {"sensor": "s2", "eecu_s": 300.0}, {"sensor": "s2", "eecu_s": 500.0}, {"sensor": "s2", "eecu_s": None},
        {"sensor": "landsat", "eecu_s": 100.0}, {"sensor": "landsat", "eecu_s": None},
    ]  # fmt: skip
    assert bs.spent_eecu_s(costs) == pytest.approx(300 + 500 + 400 + 100 + 100)
    assert bs.spent_eecu_s([{"sensor": "s2", "eecu_s": None}]) == 0.0 and bs.spent_eecu_s([]) == 0.0


# ----------------------------------------------------------------------------- cache keys


def one_chunk(n: int = 4):
    (chunk,), _ = bs.make_soil_chunks(block_fields(n, 1))
    return chunk


def test_cache_key_depends_on_year_sensor_ids_geometry_and_method(monkeypatch):
    fields = block_fields(4, 1)
    (chunk,), _ = bs.make_soil_chunks(fields)
    key = bs.cache_key(chunk, 2020, "landsat")
    assert key == bs.cache_key(bs.make_soil_chunks(fields)[0][0], 2020, "landsat")
    assert key != bs.cache_key(chunk, 2021, "landsat")
    assert key != bs.cache_key(chunk, 2020, "s2")

    renamed = fields.copy()
    renamed.loc[0, "field_id"] = "KS-999999"
    assert bs.cache_key(bs.make_soil_chunks(renamed)[0][0], 2020, "landsat") != key
    moved = fields.copy()
    moved.loc[0, "geometry"] = box(-100.9, 37.9, -100.8955, 37.9045)
    assert bs.cache_key(bs.make_soil_chunks(moved)[0][0], 2020, "landsat") != key

    monkeypatch.setattr(bs, "METHOD_VERSION", bs.METHOD_VERSION + "-test")
    assert bs.cache_key(chunk, 2020, "landsat") != key


@pytest.mark.parametrize(
    "name, value",
    [
        ("NDVI_MAX", {"landsat": 0.3, "s2": 0.3}),
        ("NBR2_MAX", {"landsat": 0.05, "s2": 0.05}),
        ("NDSI_MAX", -0.3),
        ("NDSI_MAX", None),
        ("BUFFER_M", 20.0),
        ("SCENE_MAX_CLOUD_PCT", 50),
        ("VALID_MAX", 0.9),
        ("SENSOR_GRID", {"landsat": ("EPSG:32614", [60, 0, 15, 0, -60, 15]), "s2": ("EPSG:32614", [20, 0, 0, 0, -20, 0])}),
    ],
)
def test_cache_key_changes_with_every_shared_method_constant(monkeypatch, name, value):
    chunk = one_chunk()
    before = [bs.cache_key(chunk, 2020, s) for s in bs.SENSORS]
    monkeypatch.setattr(bs, name, value)
    after = [bs.cache_key(chunk, 2020, s) for s in bs.SENSORS]
    assert all(a != b for a, b in zip(after, before))


@pytest.mark.parametrize(
    "name, value, sensor",
    [
        ("LANDSAT_QA_PIXEL_REJECT", 0b111111, "landsat"),
        ("LANDSAT_QA_RADSAT_REJECT", 0, "landsat"),
        ("LANDSAT_COLLECTIONS", [("LANDSAT/LC08/C02/T1_L2", 2013)], "landsat"),
        ("S2_CS_MIN", 0.5, "s2"),
        ("S2_CS_BAND", "cs_cdf", "s2"),
        ("S2_SCL_REJECT", (0, 1, 3, 6, 11), "s2"),
    ],
)
def test_a_sensor_constant_only_invalidates_that_sensor(monkeypatch, name, value, sensor):
    chunk = one_chunk()
    before = {s: bs.cache_key(chunk, 2020, s) for s in bs.SENSORS}
    monkeypatch.setattr(bs, name, value)
    for s in bs.SENSORS:
        assert (bs.cache_key(chunk, 2020, s) != before[s]) == (s == sensor)


def test_box_padding_is_part_of_the_key_only_once_it_is_changed(monkeypatch):
    chunk = one_chunk()
    before = {s: bs.cache_key(chunk, 2020, s) for s in bs.SENSORS}
    assert "box_pad_deg" not in bs.request_signature("s2")  # the original value: results cached before the entry existed stay valid
    box_before = bs.chunk_bounds(chunk)
    monkeypatch.setattr(bs, "BOX_PAD_DEG", 0.0004)  # a full 40 m cell: which cells the box cuts changes
    assert bs.request_signature("s2")["box_pad_deg"] == 0.0004
    assert all(bs.cache_key(chunk, 2020, s) != before[s] for s in bs.SENSORS)
    assert bs.chunk_bounds(chunk)[0] == pytest.approx(box_before[0] - 0.00038)


def test_bare_days_rule_is_not_part_of_the_request_key(monkeypatch):
    chunk = one_chunk()
    key = bs.cache_key(chunk, 2020, "s2")
    monkeypatch.setattr(bs, "MIN_MONTHS", 3)
    monkeypatch.setattr(bs, "MIN_CLEAR_PER_MONTH", 2.0)
    assert bs.cache_key(chunk, 2020, "s2") == key  # applied to the cached monthly counts at assembly


def test_cache_roundtrip_and_unusable_files(tmp_path):
    chunk = one_chunk(3)
    assert bs.read_cache(tmp_path, chunk, 2020, "s2") is None
    values = {fid: FakeEngine.value(fid, 2020, "s2") for fid in chunk.field_ids}
    bs.write_cache(tmp_path, chunk, 2020, "s2", values)
    assert bs.read_cache(tmp_path, chunk, 2020, "s2") == values
    assert bs.read_cache(tmp_path, chunk, 2020, "landsat") is None
    assert bs.read_cache(tmp_path, chunk, 2021, "s2") is None
    assert not list(tmp_path.glob("*.part"))

    path = bs.cache_path(tmp_path, chunk, 2020, "s2")
    assert path.name.startswith("s2_2020_")
    path.write_text("{ truncated")
    assert bs.read_cache(tmp_path, chunk, 2020, "s2") is None
    path.write_text(json.dumps({"values": {chunk.field_ids[0]: values[chunk.field_ids[0]]}}))  # fields missing
    assert bs.read_cache(tmp_path, chunk, 2020, "s2") is None
    short = {fid: row[:-1] for fid, row in values.items()}  # a row of the wrong width
    path.write_text(json.dumps({"values": short}))
    assert bs.read_cache(tmp_path, chunk, 2020, "s2") is None


def test_cache_columns_layout():
    assert len(bs.cache_columns("landsat")) == 2 + 6 + 24 and len(bs.cache_columns("s2")) == 2 + 10 + 24
    cols = bs.cache_columns("s2")
    assert cols[:3] == ["n_px", "bare_px", "sum_blue"] and cols[12] == "clear_01" and cols[-1] == "bare_12"


# ----------------------------------------------------------------------------- collect / resume


def two_chunks():
    chunks, _ = bs.make_soil_chunks(fields_from(list(block_fields(3, 1).geometry) + list(block_fields(3, 1, x0=-100.4, y0=38.1).geometry)))
    assert len(chunks) == 2
    return chunks


def test_collect_requests_each_chunk_year_sensor_once_then_uses_the_cache(tmp_path):
    chunks = two_chunks()
    engine = FakeEngine()
    stats = sat.Stats()
    results = bs.collect(chunks, [2016, 2017], bs.SENSORS, tmp_path, compute=engine.reduce, workers=3, stats=stats)
    # landsat: 2 chunks x 2 years; s2 has no 2016
    assert sorted((y, s) for _, y, s, _ in engine.calls) == [(2016, "landsat")] * 2 + [(2017, "landsat")] * 2 + [(2017, "s2")] * 2
    assert stats.requests == 6 and stats.cache_hits == 0 and len(results) == 6 * 3
    assert results[("KS-000002", 2017, "s2")] == FakeEngine.value("KS-000002", 2017, "s2")
    assert ("KS-000002", 2016, "s2") not in results

    again = FakeEngine()
    stats = sat.Stats()
    assert bs.collect(chunks, [2016, 2017], bs.SENSORS, tmp_path, compute=again.reduce, stats=stats) == results
    assert again.calls == [] and stats.requests == 0 and stats.cache_hits == 6


def test_fully_cached_run_never_initialises_earth_engine(tmp_path):
    chunks = two_chunks()
    bs.collect(chunks, [2020], bs.SENSORS, tmp_path, compute=FakeEngine().reduce)
    # compute=None would build bs.BareSoilEngine(), which the autouse fixture turns into an error
    assert len(bs.collect(chunks, [2020], bs.SENSORS, tmp_path)) == 12
    with pytest.raises(AssertionError, match="initialise Earth Engine"):
        bs.collect(chunks, [2021], bs.SENSORS, tmp_path)


def test_interrupted_run_resumes_where_it_stopped(tmp_path):
    chunks = two_chunks()  # 2 chunks x 3 years x 2 sensors = 12 requests

    def fail_after_five(chunk, year, sensor, tile_scale, n):
        return RuntimeError("Invalid argument: something unexpected") if n > 5 else None

    with pytest.raises(sat.SatelliteError, match="something unexpected"):
        bs.collect(chunks, [2019, 2020, 2021], bs.SENSORS, tmp_path, compute=FakeEngine(fail=fail_after_five).reduce, workers=1, sleep=no_sleep)
    done = len(list(tmp_path.glob("*.json")))
    assert done == 5

    second = FakeEngine()
    results = bs.collect(chunks, [2019, 2020, 2021], bs.SENSORS, tmp_path, compute=second.reduce, workers=2)
    assert len(second.calls) == 12 - done and len(results) == 36


def test_budget_stops_new_requests_keeps_what_finished_and_a_rerun_continues(tmp_path):
    chunks = two_chunks()  # 2 chunks x 3 years x 2 sensors = 12 requests
    engine = FakeEngine()
    spent = lambda: 100.0 * len(engine.calls)  # noqa: E731 - every request costs 100 EECU-s
    with pytest.raises(bs.BudgetExceeded, match="stopped at the budget: 0.11 EECU-hours used"):
        bs.collect(chunks, [2019, 2020, 2021], bs.SENSORS, tmp_path, compute=engine.reduce, workers=1, max_eecu_s=400.0, spent=spent, sleep=no_sleep)
    assert len(engine.calls) == 4 and len(list(tmp_path.glob("*.json"))) == 4  # not retried, not split, and nothing lost
    second = FakeEngine()
    results = bs.collect(chunks, [2019, 2020, 2021], bs.SENSORS, tmp_path, compute=second.reduce, max_eecu_s=5000.0, spent=lambda: 0.0)
    assert len(second.calls) == 8 and len(results) == 36
    with pytest.raises(bs.InputError, match="no-profile"):  # a budget without a way to know the cost is refused
        bs.collect(chunks, [2022], bs.SENSORS, tmp_path, compute=FakeEngine().reduce, max_eecu_s=1.0)


def test_force_ignores_the_cache_but_rewrites_it(tmp_path):
    chunks = two_chunks()
    bs.collect(chunks, [2020], ["landsat"], tmp_path, compute=FakeEngine().reduce)
    engine = FakeEngine()
    bs.collect(chunks, [2020], ["landsat"], tmp_path, compute=engine.reduce, force=True)
    assert len(engine.calls) == 2 and bs.read_cache(tmp_path, chunks[0], 2020, "landsat") is not None


def test_busy_errors_are_retried_with_growing_waits(tmp_path):
    chunk = one_chunk()
    engine = FakeEngine(fail=lambda c, y, s, t, n: RuntimeError("Too many concurrent aggregations.") if n <= 3 else None)
    waits: list[float] = []
    stats = sat.Stats()
    values = bs.fetch(engine.reduce, chunk, 2020, "s2", tmp_path, stats=stats, sleep=waits.append)
    assert len(engine.calls) == 4 and stats.retries == 3 and waits[0] < waits[1] < waits[2] and waits[0] >= 10
    assert bs.read_cache(tmp_path, chunk, 2020, "s2") == values


def test_gives_up_after_max_attempts_with_a_resume_hint(tmp_path):
    chunk = one_chunk()
    engine = FakeEngine(fail=lambda c, y, s, t, n: RuntimeError("HTTP 429 Too Many Requests"))
    with pytest.raises(sat.SatelliteError, match="run the same command again"):
        bs.fetch(engine.reduce, chunk, 2020, "s2", tmp_path, sleep=no_sleep, max_attempts=3)
    assert len(engine.calls) == 3 and bs.read_cache(tmp_path, chunk, 2020, "s2") is None


def test_timeouts_are_retried_as_smaller_chunks_and_cached_under_the_big_key(tmp_path):
    (chunk,), _ = bs.make_soil_chunks(block_fields(4, 2))
    engine = FakeEngine(fail=lambda c, y, s, t, n: RuntimeError("Computation timed out.") if len(c) > 2 else None)
    stats = sat.Stats()
    values = bs.fetch(engine.reduce, chunk, 2020, "landsat", tmp_path, stats=stats, sleep=no_sleep)
    assert values == {fid: FakeEngine.value(fid, 2020, "landsat") for fid in chunk.field_ids}
    assert sorted(len(ids) for ids, _, _, _ in engine.calls) == [2, 2, 2, 2, 4, 4, 8] and stats.splits == 3
    again = FakeEngine()
    assert bs.fetch(again.reduce, chunk, 2020, "landsat", tmp_path) == values and again.calls == []


def test_single_field_memory_error_retries_with_larger_tile_scale(tmp_path):
    chunk = one_chunk(1)
    engine = FakeEngine(fail=lambda c, y, s, t, n: RuntimeError("User memory limit exceeded.") if t < 4 else None)
    bs.fetch(engine.reduce, chunk, 2020, "s2", tmp_path, sleep=no_sleep)
    assert [t for _, _, _, t in engine.calls] == [1, 2, 4]


def test_unknown_errors_fail_at_once_and_bad_rows_are_rejected(tmp_path):
    chunk = one_chunk(2)
    engine = FakeEngine(fail=lambda c, y, s, t, n: RuntimeError("Image.select: no such band"))
    with pytest.raises(sat.SatelliteError, match="no such band"):
        bs.fetch(engine.reduce, chunk, 2020, "s2", tmp_path, sleep=no_sleep)
    assert len(engine.calls) == 1

    def missing_one(c, year, sensor, tile_scale):
        return {fid: FakeEngine.value(fid, year, sensor) for fid in c.field_ids[:-1]}

    with pytest.raises(sat.SatelliteError, match="no usable row"):
        bs.fetch(missing_one, chunk, 2020, "s2", tmp_path, sleep=no_sleep)
    assert bs.read_cache(tmp_path, chunk, 2020, "s2") is None


# ----------------------------------------------------------------------------- bare_days arithmetic


def test_bare_days_is_the_monthly_bare_share_times_the_days_of_the_month():
    clear = [100.0] * 12
    bare = [0.0] * 12
    bare[0], bare[5] = 100.0, 50.0  # January fully bare, June half bare
    assert bs.bare_days(clear, bare, 50, 2023) == (pytest.approx(31 + 15), 12)
    assert bs.bare_days(clear, clear, 50, 2023) == (pytest.approx(365), 12)
    assert bs.bare_days(clear, clear, 50, 2024) == (pytest.approx(366), 12)  # leap year
    assert bs.bare_days(clear, [0.0] * 12, 50, 2023) == (0.0, 12)


def test_a_month_with_many_clear_views_weighs_no_more_than_one_with_few():
    bare_share = [1.0, 0.0] * 6
    few = [50.0 * 1] * 12  # one clear view per pixel every month
    lopsided = [50.0 * (20 if m % 2 else 1) for m in range(12)]  # 20 views in the never-bare months
    a, _ = bs.bare_days(few, [c * s for c, s in zip(few, bare_share)], 50, 2023)
    b, _ = bs.bare_days(lopsided, [c * s for c, s in zip(lopsided, bare_share)], 50, 2023)
    assert a == pytest.approx(b) == pytest.approx(31 + 31 + 31 + 31 + 30 + 30)  # Jan, Mar, May, Jul, Sep, Nov
    pooled = 365 * sum(c * s for c, s in zip(lopsided, bare_share)) / sum(lopsided)
    assert pooled < 0.2 * b  # what a plain "bare views / clear views" would have said


def test_months_without_clear_views_are_interpolated_and_the_ends_held_flat():
    n_px = 10
    clear = [10.0] * 12
    bare = [0.0] * 12
    # shares: Feb 0.2, May 0.8, Dec 0.4; Jan, Mar, Apr have no clear view
    for month, value in ((1, 2.0), (4, 8.0), (11, 4.0)):
        bare[month] = value
    for month in (0, 2, 3):
        clear[month] = 0.0
    days, months = bs.bare_days(clear, bare, n_px, 2023)
    assert months == 9
    expected = 31 * 0.2 + 28 * 0.2 + 31 * 0.4 + 30 * 0.6 + 31 * 0.8 + 31 * 0.4  # Jan (flat), Feb, Mar, Apr (linear), May, Dec
    assert days == pytest.approx(expected)


def test_a_month_needs_half_a_clear_view_per_pixel_to_count():
    n_px = 100
    clear = [100.0] * 12
    bare = [100.0] * 12
    clear[6], bare[6] = 49.0, 0.0  # July: 0.49 clear views per pixel, none bare -> ignored, interpolated as bare
    assert bs.bare_days(clear, bare, n_px, 2023) == (pytest.approx(365), 11)
    clear[6] = 50.0  # 0.5 per pixel -> counts, and it was not bare
    assert bs.bare_days(clear, bare, n_px, 2023) == (pytest.approx(365 - 31), 12)


def test_bare_days_is_null_when_too_few_months_have_clear_views():
    clear = [10.0] * 7 + [0.0] * 5
    assert bs.bare_days(clear, clear, 10, 2023) == (None, 7)
    clear[7] = 10.0
    days, months = bs.bare_days(clear, clear, 10, 2023)
    assert months == bs.MIN_MONTHS == 8 and days == pytest.approx(365)
    assert bs.bare_days([0.0] * 12, [0.0] * 12, 10, 2023) == (None, 0)
    assert bs.bare_days([10.0] * 12, [10.0] * 12, 0, 2023) == (None, 0)


# ----------------------------------------------------------------------------- assembly


def test_row_from_sums_means_and_shares():
    row = bs.row_from_sums("landsat", 2023, sums("landsat", 200.0, 50.0, 0.10, 3.0, 0.75))
    assert row["n_px"] == 200 and row["n_clear"] == pytest.approx(36.0) and row["n_bare"] == pytest.approx(9.0)
    assert row["bare_px_frac"] == pytest.approx(0.25)
    assert row["bare_days"] == pytest.approx(365 * 0.25) and row["clear_months"] == 12
    # band sums are divided by the pixels that had a bare view, not by all pixels
    assert [row[b] for b in bs.SENSOR_BANDS["landsat"]] == pytest.approx([0.10, 0.11, 0.12, 0.13, 0.14, 0.15])
    assert all(row[b] is None for b in ("re1", "re2", "re3", "nir8a"))
    s2 = bs.row_from_sums("s2", 2023, sums("s2", 10.0, 10.0, 0.2, 1.0, 1.0))
    assert s2["nir8a"] == pytest.approx(0.2 + 0.07) and s2["swir2"] == pytest.approx(0.29)


def test_row_from_sums_without_bare_views_or_pixels():
    none_bare = bs.row_from_sums("s2", 2023, sums("s2", 80.0, 0.0, 0.0, 4.0, 0.0))
    assert none_bare["n_bare"] == 0.0 and none_bare["bare_px_frac"] == 0.0 and none_bare["bare_days"] == 0.0
    assert none_bare["n_clear"] == pytest.approx(48.0) and all(none_bare[b] is None for b in bs.BAND_COLUMNS)
    for empty in (bs.row_from_sums("s2", 2023, [0.0] * 36), bs.row_from_sums("s2", 2023, None)):
        assert empty["n_px"] == 0
        assert all(empty[c] is None for c in ["n_clear", "n_bare", "bare_px_frac", "bare_days", "clear_months", *bs.BAND_COLUMNS])


def test_assemble_has_a_row_for_every_field_year_and_sensor():
    results = {(fid, y, s): FakeEngine.value(fid, y, s) for fid in ("KS-000002", "KS-000001") for s in bs.SENSORS for y in bs.sensor_years(s, [2016, 2017, 2018])}
    del results[("KS-000002", 2018, "s2")]  # never came back
    df = bs.assemble(["KS-000002", "KS-000001", "KS-000003"], [2018, 2016, 2017], bs.SENSORS, results)
    assert list(df.columns) == bs.COLUMNS
    assert len(df) == 3 * (3 + 2)  # landsat 2016-2018, s2 2017-2018
    assert list(zip(df["field_id"], df["sensor"], df["year"]))[:5] == [
        ("KS-000001", "landsat", 2016), ("KS-000001", "landsat", 2017), ("KS-000001", "landsat", 2018),
        ("KS-000001", "s2", 2017), ("KS-000001", "s2", 2018),
    ]
    assert df["year"].dtype == np.int64 and df["n_px"].dtype == np.int64
    assert all(df[c].dtype == np.float64 for c in ["n_clear", "n_bare", "bare_px_frac", "bare_days", *bs.BAND_COLUMNS])
    assert not ((df["sensor"] == "s2") & (df["year"] == 2016)).any()
    landsat = df[df["sensor"] == "landsat"]
    assert landsat[["re1", "re2", "re3", "nir8a"]].isna().all().all() and landsat["swir2"].notna().sum() == 6
    missing = df[(df["field_id"] == "KS-000003") | ((df["field_id"] == "KS-000002") & (df["year"] == 2018) & (df["sensor"] == "s2"))]
    assert len(missing) == 6 and (missing["n_px"] == 0).all()
    assert missing[["n_clear", "n_bare", "bare_px_frac", "bare_days", "clear_months", *bs.BAND_COLUMNS]].isna().all().all()
    assert bs.check_table(df) == []
    again = bs.assemble(["KS-000003", "KS-000001", "KS-000002"], [2016, 2017, 2018], reversed(bs.SENSORS), dict(reversed(list(results.items()))))
    pd.testing.assert_frame_equal(df, again)


def test_check_table_catches_a_broken_table():
    results = {("KS-000001", y, s): FakeEngine.value("KS-000001", y, s) for s in bs.SENSORS for y in (2020, 2021)}
    df = bs.assemble(["KS-000001"], [2020, 2021], bs.SENSORS, results)
    assert bs.check_table(df) == []
    assert any("duplicate" in p for p in bs.check_table(pd.concat([df, df], ignore_index=True)))
    bad = df.copy()
    bad["bare_days"] = 400.0
    assert any("bare_days" in p for p in bs.check_table(bad))
    bad = df.copy()
    bad.loc[bad["sensor"] == "landsat", "re1"] = 0.2
    assert any("does not have" in p for p in bs.check_table(bad))
    bad = df.copy()
    bad["swir2"] = 1.5
    assert any("reflectance" in p for p in bs.check_table(bad))
    assert any("missing column" in p for p in bs.check_table(df.drop(columns=["n_bare"])))


def test_parse_profile_eecu():
    text = " EECU·s PeakMem Count  Description\n 13.902    411k    26  Algorithm ImageCollection.reduce\n  0.098     43k   304  Loading assets\n   -       4.5M    62  Algorithm X\n"
    assert bs.parse_profile_eecu(text) == pytest.approx(14.0)
    assert bs.parse_profile_eecu("") == 0.0


# ----------------------------------------------------------------------------- build / CLI


def write_fields(tmp_path, fields=None) -> Path:
    path = tmp_path / "fields.parquet"
    (fields if fields is not None else block_fields(3, 2)).to_parquet(path)
    return path


def test_build_writes_a_valid_file_atomically_and_reruns_from_the_cache(tmp_path):
    thin = box(-100.80, 37.9, -100.7995, 37.905)  # ~44 m wide: lost to the buffer
    fields = fields_from(list(block_fields(3, 2).geometry) + [thin])
    fields_path = write_fields(tmp_path, fields)
    out = tmp_path / "out" / "bare.parquet"
    engine = FakeEngine()
    df = bs.build(fields_path, out, [2016, 2017], cache_dir=tmp_path / "cache", compute=engine.reduce, log_path=tmp_path / "logs" / "run.log")
    assert len(engine.calls) == 3  # one chunk: landsat 2016, landsat 2017, s2 2017
    loaded = pd.read_parquet(out)
    assert len(loaded) == 7 * 3 and bs.check_table(loaded) == []
    pd.testing.assert_frame_equal(loaded, df, check_dtype=False)
    assert not list(out.parent.glob("*.part"))
    lost = loaded[loaded["field_id"] == "KS-000007"]  # the thin field still has its rows, all null
    assert len(lost) == 3 and (lost["n_px"] == 0).all() and lost["n_clear"].isna().all() and lost["swir2"].isna().all()
    row = loaded[(loaded["field_id"] == "KS-000002") & (loaded["year"] == 2017) & (loaded["sensor"] == "s2")].iloc[0]
    assert row["n_px"] == 52 and row["n_clear"] == 24.0 and row["n_bare"] == 6.0 and row["bare_days"] == 91.2
    assert row["bare_px_frac"] == round(40 / 52, 4) and row["blue"] == round(0.05 + 0.002 + 0.0017, 5)
    assert "wrote" in (tmp_path / "logs" / "run.log").read_text()

    method = bs.read_table_method(out)  # the file says how it was computed
    assert method["snow_screen"] is True and method["request"]["s2"] == json.loads(json.dumps(bs.request_signature("s2")))
    assert method["bare_days"] == {"min_clear_per_month": bs.MIN_CLEAR_PER_MONTH, "min_months": bs.MIN_MONTHS}

    first = out.read_bytes()
    bs.build(fields_path, out, [2016, 2017], cache_dir=tmp_path / "cache")  # no engine: cache only
    assert out.read_bytes() == first


def fake_engine_class(engine):
    class Engine:
        def __init__(self, profile=True):
            self.reduce = engine.reduce
            self.costs = []

    return Engine


def test_cli_end_to_end_prints_a_short_summary_and_logs_to_a_file(tmp_path, monkeypatch, capsys):
    fields_path = write_fields(tmp_path)
    out = tmp_path / "bare.parquet"
    engine = FakeEngine()
    monkeypatch.setattr(bs, "BareSoilEngine", fake_engine_class(engine))
    monkeypatch.setattr(bs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(bs, "LOG_DIR", tmp_path / "logs")
    args = ["--fields", str(fields_path), "--out", str(out), "--years", "2016-2018"]
    assert bs.main(args) == 0
    assert len(engine.calls) == 3 + 2
    text = capsys.readouterr().out
    assert "rows: 30" in text and "Earth Engine requests: 5" in text and len(text.strip().splitlines()) <= 15
    logs = list((tmp_path / "logs").glob("soil_bare_*.log"))
    assert len(logs) == 1 and "requests done" in logs[0].read_text()
    first = out.read_bytes()

    assert bs.main(args) == 0  # rerun: everything cached, no request
    assert len(engine.calls) == 5 and "Earth Engine requests: 0" in capsys.readouterr().out
    assert out.read_bytes() == first

    assert bs.main(args + ["--force", "--sensors", "landsat"]) == 0
    assert len(engine.calls) == 8
    table = pd.read_parquet(out)
    assert set(table["sensor"]) == {"landsat"} and len(table) == 18


def test_cli_estimate_asks_nothing_of_earth_engine_and_budget_stop_is_exit_code_3(tmp_path, monkeypatch, capsys):
    fields_path = write_fields(tmp_path)
    out = tmp_path / "bare.parquet"
    monkeypatch.setattr(bs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(bs, "LOG_DIR", tmp_path / "logs")
    args = ["--fields", str(fields_path), "--out", str(out), "--years", "2016-2018"]
    # BareSoilEngine is still the autouse fixture's Boom: --estimate must not touch it
    assert bs.main(args + ["--estimate"]) == 0
    text = capsys.readouterr().out
    assert "nothing was requested" in text and "5 requests" in text and "EECU-hours" in text and len(text.strip().splitlines()) <= 6
    assert not out.exists() and not (tmp_path / "cache").exists()

    engine = FakeEngine()

    class Engine:  # every finished request is profiled at 900 EECU-s
        def __init__(self, profile=True):
            self.costs = []

        def reduce(self, chunk, year, sensor, tile_scale=1):
            values = engine.reduce(chunk, year, sensor, tile_scale)
            self.costs.append({"sensor": sensor, "year": year, "fields": len(chunk), "seconds": 1.0, "eecu_s": 900.0})
            return values

    monkeypatch.setattr(bs, "BareSoilEngine", Engine)
    assert bs.main(args + ["--workers", "1", "--max-eecu-hours", "0.5"]) == 3
    err = capsys.readouterr().err
    assert "STOPPED" in err and "run the same command again" in err and len(engine.calls) == 2 and not out.exists()
    log = next((tmp_path / "logs").glob("soil_bare_*.log")).read_text()
    assert "this run: 2 requests, 0.500 EECU-hours" in log
    assert bs.main(args + ["--workers", "1", "--max-eecu-hours", "5"]) == 0  # the rest; the two finished ones come from the cache
    assert len(engine.calls) == 5 and out.exists()
    assert bs.main(args + ["--max-eecu-hours", "1", "--no-profile", "--force"]) == 2
    assert "no-profile" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        bs.main(args + ["--max-eecu-hours", "0"])


def test_cli_field_ids_file_and_max_fields(tmp_path, monkeypatch, capsys):
    fields_path = write_fields(tmp_path, block_fields(4, 3))
    engine = FakeEngine()
    monkeypatch.setattr(bs, "BareSoilEngine", fake_engine_class(engine))
    ids = tmp_path / "ids.txt"
    ids.write_text("# two fields\nKS-000003\n\nKS-000010  # trailing comment\n")
    out = tmp_path / "bare.parquet"
    assert bs.main(["--fields", str(fields_path), "--out", str(out), "--years", "2020", "--field-ids-file", str(ids)]) == 0
    assert sorted(pd.read_parquet(out)["field_id"].unique()) == ["KS-000003", "KS-000010"]
    assert bs.main(["--fields", str(fields_path), "--out", str(out), "--years", "2020", "--max-fields", "5"]) == 0
    assert pd.read_parquet(out)["field_id"].nunique() == 5
    capsys.readouterr()

    ids.write_text("KS-000003\nKS-424242\n")
    assert bs.main(["--fields", str(fields_path), "--out", str(out), "--years", "2020", "--field-ids-file", str(ids)]) == 2
    assert "KS-424242" in capsys.readouterr().err
    assert bs.main(["--fields", str(fields_path), "--out", str(out), "--field-ids-file", str(tmp_path / "nope.txt")]) == 2
    assert "not found" in capsys.readouterr().err


def fake_ee_module(initialize):
    module = types.ModuleType("ee")
    module.Initialize = initialize
    module.data = types.SimpleNamespace(setDeadline=lambda ms: None)
    return module


def test_cli_not_authenticated_is_three_clear_lines_and_exit_code_2(tmp_path, monkeypatch, capsys):
    fields_path = write_fields(tmp_path)
    monkeypatch.undo()  # drop the autouse guard: this test runs the real engine constructor ...
    monkeypatch.setattr(bs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(bs, "LOG_DIR", tmp_path / "logs")

    def initialize(project=None):  # ... against a fake `ee` module that is not authenticated
        raise RuntimeError("Please authorize access to your Earth Engine account by running\n\nearthengine authenticate\n\nin your shell")

    monkeypatch.setitem(sys.modules, "ee", fake_ee_module(initialize))
    out = tmp_path / "out.parquet"
    code = bs.main(["--fields", str(fields_path), "--out", str(out), "--years", "2020"])
    captured = capsys.readouterr()
    assert code == 2 and not out.exists()
    lines = captured.err.strip().splitlines()
    assert len(lines) == 3 and lines[0].startswith("ERROR: Google Earth Engine is not available")
    assert "earthengine authenticate" in lines[1] and "set_project" in lines[1]
    assert lines[2].strip().startswith("Earth Engine said: Please authorize access")
    assert "Traceback" not in captured.err and "Traceback" not in captured.out


def test_cli_missing_fields_file_and_bad_arguments(tmp_path, capsys):
    assert bs.main(["--fields", str(tmp_path / "nope.parquet"), "--out", str(tmp_path / "o.parquet")]) == 2
    assert "not found" in capsys.readouterr().err
    for args in (["--years", "soon"], ["--max-fields", "0"], ["--sensors", "modis"], ["--sensors", ""]):
        with pytest.raises(SystemExit) as exc:
            bs.main(args)
        assert exc.value.code == 2
    capsys.readouterr()
    assert bs.parse_sensors("s2, landsat") == ["landsat", "s2"]


def test_no_snow_screen_flag_only_lasts_for_that_run(tmp_path, monkeypatch, capsys):
    fields_path = write_fields(tmp_path)
    seen = []

    class Engine:
        def __init__(self, profile=True):
            self.costs = []

        def reduce(self, chunk, year, sensor, tile_scale=1):
            seen.append(bs.NDSI_MAX)
            return {fid: FakeEngine.value(fid, year, sensor) for fid in chunk.field_ids}

    monkeypatch.setattr(bs, "BareSoilEngine", Engine)
    args = ["--fields", str(fields_path), "--out", str(tmp_path / "o.parquet"), "--years", "2020", "--sensors", "s2"]
    assert bs.main(args + ["--no-snow-screen"]) == 0
    assert bs.main(args) == 0  # a different method, so a different cache key: asked again
    assert seen == [None, -0.25] and bs.NDSI_MAX == -0.25
    assert bs.read_table_method(tmp_path / "o.parquet")["snow_screen"] is True
    assert bs.main(args + ["--no-snow-screen"]) == 0  # cached under its own key: no new request
    assert len(seen) == 2 and bs.read_table_method(tmp_path / "o.parquet")["snow_screen"] is False
    assert "WITHOUT the snow" in capsys.readouterr().out
    assert "ndsi_max" in bs.request_signature("s2")
    capsys.readouterr()


# ----------------------------------------------------------------------------- prototype fields


def test_prototype_clusters_are_reproducible_compact_and_span_the_soils():
    rng = np.random.default_rng(4)
    fields = make_fields(600, seed=5)
    fields["organic_matter_pct"] = rng.uniform(0.5, 3.5, len(fields)).round(3)
    clusters = bp.choose(fields, n_clusters=6, cluster_size=4, seed=7)
    assert clusters == bp.choose(fields.sample(frac=1, random_state=1), n_clusters=6, cluster_size=4, seed=7)
    assert clusters != bp.choose(fields, n_clusters=6, cluster_size=4, seed=8)
    ids = [fid for cluster in clusters for fid in cluster]
    assert [len(c) for c in clusters] == [4] * 6 and len(set(ids)) == 24
    cells = dict(zip(fields.sort_values("field_id")["field_id"], bp.grid_cells(fields.sort_values("field_id").reset_index(drop=True))))
    cluster_cells = [{cells[fid] for fid in cluster} for cluster in clusters]
    assert all(len(c) == 1 and None not in c for c in cluster_cells)  # each cluster inside one grid cell
    assert len({next(iter(c)) for c in cluster_cells}) == 6  # and no two clusters in the same cell
    om = fields.set_index("field_id")["organic_matter_pct"]
    assert om[ids].min() < om.quantile(0.2) and om[ids].max() > om.quantile(0.8)
    # none of the chosen fields is lost to the buffer
    chunks, lost = bs.make_soil_chunks(fields[fields["field_id"].isin(ids)])
    assert lost == [] and sorted(fid for c in chunks for fid in c.field_ids) == sorted(ids)


# ----------------------------------------------------------------------------- independent recompute


def landsat_dn(reflectance: float) -> int:
    return int(round((reflectance + 0.2) / bs.LANDSAT_SCALE))


def test_recompute_from_raw_pixels_on_a_hand_made_field():
    """Two pixels, five scenes on four days: exercises masks, the same-day mosaic, the bare rule, the
    snow screen, medians and counts of the numpy reference used to check the Earth Engine results."""
    import pyproj

    crs, transform = bs.SENSOR_GRID["landsat"]
    col0, row0 = 11_000, -140_500  # two neighbouring grid cells in Finney County
    centres = [(transform[2] + (col0 + i + 0.5) * 30, transform[5] + (row0 + 0.5) * -30) for i in range(2)]
    to_lonlat = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lonlat = [to_lonlat.transform(x, y) for x, y in centres]
    outline_grid = box(centres[0][0] - 10, centres[0][1] - 10, centres[1][0] + 10, centres[1][1] + 10)
    outline = Polygon([to_lonlat.transform(x, y) for x, y in outline_grid.exterior.coords])

    soil = dict(blue=0.10, green=0.14, red=0.18, nir=0.26, swir1=0.38, swir2=0.33)  # ndvi 0.18, nbr2 0.07, ndsi -0.46
    soil_b = dict(blue=0.12, green=0.16, red=0.20, nir=0.28, swir1=0.40, swir2=0.35)
    crop = dict(blue=0.04, green=0.08, red=0.05, nir=0.45, swir1=0.20, swir2=0.10)  # ndvi 0.8
    stubble = dict(blue=0.12, green=0.17, red=0.22, nir=0.30, swir1=0.40, swir2=0.28)  # nbr2 0.18
    snow = dict(blue=0.80, green=0.80, red=0.78, nir=0.75, swir1=0.12, swir2=0.11)  # ndsi 0.74, passes ndvi and nbr2
    day = 86_400_000
    jan10, jun5, jul20, dec2 = 19_002 * day, 19_148 * day, 19_193 * day, 19_328 * day  # all in 2022
    header = ["id", "longitude", "latitude", "time", *bs.LANDSAT_BANDS.values(), "QA_PIXEL", "QA_RADSAT"]

    def scene(scene_id, time, spectra, qa=(0, 0)):
        rows = []
        for (lon, lat), spectrum, flags in zip(lonlat, spectra, qa):
            rows.append([scene_id, lon, lat, time, *[landsat_dn(spectrum[b]) for b in bs.LANDSAT_BANDS], flags, 0])
        return rows

    rows = (
        scene("LC08_030033_20220110", jan10, [soil, soil])
        # the next row of the same overpass: cloudy over pixel 0 (keeps the first scene), clear over pixel 1 (wins)
        + scene("LC08_030034_20220110", jan10, [crop, soil_b], qa=(1 << 3, 0))
        + scene("LC09_030033_20220605", jun5, [crop, crop])
        + scene("LC08_030033_20220720", jul20, [stubble, stubble])
        + scene("LC09_030033_20221202", dec2, [snow, snow])
    )
    got = bc.recompute("landsat", 2022, outline, {"scenes": [header, *rows]})
    assert got["n_px"] == 2
    assert got["n_clear"] == pytest.approx(3.0)  # soil, crop, stubble; the snow day is not a clear view
    assert got["n_bare"] == pytest.approx(1.0) and got["bare_px_frac"] == 1.0
    assert got["clear_months"] == 3 and got["bare_days"] is None  # 3 months < MIN_MONTHS
    for band in bs.LANDSAT_BANDS:
        assert got[band] == pytest.approx((soil[band] + soil_b[band]) / 2, abs=2e-5)  # DN rounding

    bs_ndsi = bs.NDSI_MAX
    try:
        bs.NDSI_MAX = None  # without the screen the snow day is a bare view
        unscreened = bc.recompute("landsat", 2022, outline, {"scenes": [header, *rows]})
    finally:
        bs.NDSI_MAX = bs_ndsi
    assert unscreened["n_clear"] == pytest.approx(4.0) and unscreened["n_bare"] == pytest.approx(2.0)
    assert unscreened["blue"] == pytest.approx(((soil["blue"] + snow["blue"]) / 2 + (soil_b["blue"] + snow["blue"]) / 2) / 2, abs=2e-5)

    # a field the grid does not reach
    far = Polygon([(lon + 1, lat) for lon, lat in outline.exterior.coords])
    assert bc.recompute("landsat", 2022, far, {"scenes": [header, *rows]}) == {"n_px": 0}
