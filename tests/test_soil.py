"""Tests for pipeline/soil/ssurgo.py. No network, no real data: Soil Data Access is faked."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
import requests  # noqa: E402
import shapely  # noqa: E402
from shapely.geometry import Polygon, box  # noqa: E402

from pipeline.soil import ssurgo as soil  # noqa: E402
from pipeline.soil.ssurgo import (  # noqa: E402
    SDAClient,
    SoilDataError,
    field_organic_matter,
    field_soil_table,
    mapunit_organic_matter,
    organic_matter_pct,
    surface_horizons,
    tiles_for_bounds,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any test that reaches the real `requests.post` fails loudly."""

    def refuse(*args, **kwargs):
        raise AssertionError("test tried to use the network")

    monkeypatch.setattr(soil.requests, "post", refuse)


# ----------------------------------------------------------------------------- helpers


def attrs(rows) -> pd.DataFrame:
    """Attribute table from (mukey, cokey, comppct_r, majcompflag, chkey, hzname, desgnmaster, top, bottom, om_r)."""
    cols = ["mukey", "cokey", "comppct_r", "majcompflag", "chkey", "hzname", "desgnmaster", "hzdept_r", "hzdepb_r", "om_r"]
    frame = pd.DataFrame(rows, columns=cols)
    frame["musym"] = "s" + frame["mukey"].astype(str)
    frame["muname"] = "unit " + frame["mukey"].astype(str)
    for col in ("comppct_r", "hzdept_r", "hzdepb_r", "om_r"):
        frame[col] = pd.to_numeric(frame[col]).astype(float)
    for col in ("mukey", "cokey", "majcompflag", "chkey", "hzname", "desgnmaster"):
        frame[col] = np.array([None if v is None or v != v else str(v) for v in frame[col]], dtype=object)
    return frame


def mupolygons(items, crs="EPSG:5070") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"mukey": [k for k, _ in items]}, geometry=[g for _, g in items], crs=crs)


def fields_of(geoms, crs="EPSG:5070", index=None) -> gpd.GeoDataFrame:
    ids = [f"KS-{i + 1:06d}" for i in range(len(geoms))]
    return gpd.GeoDataFrame({"field_id": ids}, geometry=list(geoms), crs=crs, index=index)


# ----------------------------------------------------------------------------- surface horizon


def test_surface_horizon_is_the_shallowest_whatever_the_row_order():
    table = attrs(
        [
            ("1", "c1", 100, "Yes", "h3", "C", "C", 60, 150, 0.2),
            ("1", "c1", 100, "Yes", "h1", "Ap", "A", 0, 18, 1.7),
            ("1", "c1", 100, "Yes", "h2", "Bt", "B", 18, 60, 0.9),
        ]
    )
    surf = surface_horizons(table)
    assert surf.loc["c1", "hzname"] == "Ap"
    assert surf.loc["c1", "om_r"] == 1.7
    assert not surf.loc["c1", "organic_layer_skipped"]


def test_thin_organic_litter_layer_is_skipped_for_the_mineral_surface():
    table = attrs(
        [
            ("1", "c1", 100, "Yes", "h1", "Oi", "O", 0, 5, 80.0),
            ("1", "c1", 100, "Yes", "h2", "A", "A", 5, 20, 3.0),
            ("1", "c1", 100, "Yes", "h3", "Bw", "B", 20, 80, 1.0),
        ]
    )
    surf = surface_horizons(table)
    assert surf.loc["c1", "hzname"] == "A"
    assert surf.loc["c1", "om_r"] == 3.0
    assert surf.loc["c1", "organic_layer_skipped"]
    assert mapunit_organic_matter(table).loc["1", "om_pct"] == pytest.approx(3.0)
    assert mapunit_organic_matter(table).loc["1", "organic_layers_skipped"] == 1
    # the count also works next to a component that has no horizons at all
    mixed = pd.concat([table, attrs([("1", "c2", 10, "No", None, None, None, None, None, None)])], ignore_index=True)
    unit = mapunit_organic_matter(mixed).loc["1"]
    assert unit["organic_layers_skipped"] == 1 and unit["n_components"] == 2 and unit["om_pct"] == pytest.approx(3.0)


def test_organic_layer_is_recognised_by_name_when_the_master_designation_is_missing():
    table = attrs(
        [
            ("1", "c1", 100, "Yes", "h1", "Oe", None, 0, 8, 70.0),
            ("1", "c1", 100, "Yes", "h2", "A", None, 8, 30, 4.0),
        ]
    )
    assert surface_horizons(table).loc["c1", "om_r"] == 4.0


def test_thick_organic_soil_keeps_its_organic_surface():
    # 40 cm of muck over mineral material: the organic layer IS the soil surface.
    table = attrs(
        [
            ("1", "c1", 100, "Yes", "h1", "Oa", "O", 0, 40, 60.0),
            ("1", "c1", 100, "Yes", "h2", "Cg", "C", 40, 150, 2.0),
            ("2", "c2", 100, "Yes", "h3", "Oa", "O", 0, 60, 55.0),  # organic horizons only
            ("2", "c2", 100, "Yes", "h4", "Oe", "O", 60, 200, 45.0),
        ]
    )
    surf = surface_horizons(table)
    assert surf.loc["c1", "om_r"] == 60.0 and not surf.loc["c1", "organic_layer_skipped"]
    assert surf.loc["c2", "om_r"] == 55.0 and not surf.loc["c2", "organic_layer_skipped"]


def test_null_surface_om_is_never_replaced_by_a_deeper_horizon():
    table = attrs(
        [
            ("1", "c1", 100, "Yes", "h1", "H1", None, 0, 20, None),
            ("1", "c1", 100, "Yes", "h2", "H2", None, 20, 100, 0.5),
        ]
    )
    assert np.isnan(surface_horizons(table).loc["c1", "om_r"])
    assert np.isnan(mapunit_organic_matter(table).loc["1", "om_pct"])


def test_component_without_horizons_has_no_surface_horizon():
    table = attrs([("1", "c1", 100, "Yes", None, None, None, None, None, None)])
    assert len(surface_horizons(table)) == 0


# ----------------------------------------------------------------------------- map unit weighting


def test_mapunit_value_is_the_comppct_weighted_mean_of_surface_om():
    table = attrs(
        [
            ("1", "c1", 93, "Yes", "h1", "Ap", "A", 0, 16, 1.7),
            ("1", "c1", 93, "Yes", "h2", "Bt", "B", 16, 60, 1.0),
            ("1", "c2", 5, "No ", "h3", "Ap", "A", 0, 20, 1.8),
            ("1", "c3", 2, "No ", "h4", "Ap", "A", 0, 7, 3.6),
        ]
    )
    unit = mapunit_organic_matter(table).loc["1"]
    assert unit["om_pct"] == pytest.approx((93 * 1.7 + 5 * 1.8 + 2 * 3.6) / 100)  # 1.743
    assert unit["comppct_with_om"] == 100 and unit["n_components"] == 3 and unit["n_components_with_om"] == 3


def test_mapunit_weights_are_renormalised_over_components_that_have_data():
    table = attrs(
        [
            ("1", "c1", 60, "Yes", "h1", "A", "A", 0, 15, 2.0),
            ("1", "c2", 30, "Yes", None, None, None, None, None, None),  # rock outcrop: no horizons
            ("1", "c3", 10, "No", "h2", "A", "A", 0, 10, 4.0),
        ]
    )
    unit = mapunit_organic_matter(table).loc["1"]
    assert unit["om_pct"] == pytest.approx((60 * 2.0 + 10 * 4.0) / 70)
    assert unit["comppct_with_om"] == 70 and unit["comppct_total"] == 100
    assert unit["n_components"] == 3 and unit["n_components_with_om"] == 2


def test_mapunit_without_any_data_is_null():
    table = attrs(
        [
            ("1", "c1", 100, "Yes", None, None, None, None, None, None),  # water
            ("2", None, None, None, None, None, None, None, None, None),  # no component at all
            ("3", "c3", 100, "Yes", "h1", "R", "R", 0, 25, None),  # horizon without om_r
            ("4", "c4", 100, "Yes", "h2", "A", "A", 0, 20, 1.5),
        ]
    )
    units = mapunit_organic_matter(table)
    assert list(units.index) == ["1", "2", "3", "4"]
    assert units["om_pct"].isna().tolist() == [True, True, True, False]
    assert units.loc["2", "n_components"] == 0


def test_components_with_null_or_zero_percentage_carry_no_weight():
    table = attrs(
        [
            ("1", "c1", 80, "Yes", "h1", "A", "A", 0, 20, 2.0),
            ("1", "c2", None, "No", "h2", "A", "A", 0, 20, 50.0),
            ("1", "c3", 0, "No", "h3", "A", "A", 0, 20, 50.0),
        ]
    )
    assert mapunit_organic_matter(table).loc["1", "om_pct"] == pytest.approx(2.0)


def test_major_components_only_is_an_option():
    table = attrs(
        [
            ("1", "c1", 85, "Yes", "h1", "A", "A", 0, 10, 0.75),
            ("1", "c2", 10, "No ", "h2", "A", "A", 0, 25, 0.75),  # SDA pads the flag: "No "
            ("1", "c3", 5, "No ", "h3", "C", "C", 0, 203, 0.20),
        ]
    )
    assert mapunit_organic_matter(table, "all").loc["1", "om_pct"] == pytest.approx(0.7225)
    assert mapunit_organic_matter(table, "major").loc["1", "om_pct"] == pytest.approx(0.75)
    with pytest.raises(ValueError):
        mapunit_organic_matter(table, "dominant")


# ----------------------------------------------------------------------------- overlay

OM = pd.Series({"A": 1.0, "B": 3.0, "W": np.nan})


def test_field_value_is_the_area_weighted_mean_of_its_map_units():
    units = mupolygons([("A", box(0, 0, 500, 1000)), ("B", box(500, 0, 2000, 1000))])
    table = field_organic_matter(fields_of([box(250, 100, 750, 900), box(400, 0, 800, 500)]), units, OM)
    assert table["organic_matter_pct"].tolist() == pytest.approx([2.0, 0.25 * 1.0 + 0.75 * 3.0])
    assert table["om_coverage"].tolist() == pytest.approx([1.0, 1.0])
    assert table["ssurgo_coverage"].tolist() == pytest.approx([1.0, 1.0])
    assert table["n_mapunits"].tolist() == [2, 2]
    assert table["dominant_mukey"].tolist() == ["A", "B"]  # an exact tie goes to the smaller key
    assert table["dominant_share"].tolist() == pytest.approx([0.5, 0.75])


def test_null_map_units_are_ignored_and_the_weights_renormalised():
    units = mupolygons([("A", box(0, 0, 500, 100)), ("W", box(500, 0, 800, 100)), ("B", box(800, 0, 1000, 100))])
    table = field_organic_matter(fields_of([box(0, 0, 1000, 100)]), units, OM)
    assert table["organic_matter_pct"].iloc[0] == pytest.approx((0.5 * 1.0 + 0.2 * 3.0) / 0.7)
    assert table["om_coverage"].iloc[0] == pytest.approx(0.7)
    assert table["ssurgo_coverage"].iloc[0] == pytest.approx(1.0)
    assert table["n_mapunits"].iloc[0] == 3
    assert table["dominant_mukey"].iloc[0] == "A"


def test_nan_only_when_the_field_has_no_coverage_with_a_value():
    units = mupolygons([("A", box(0, 0, 100, 100)), ("W", box(100, 0, 300, 100)), ("Z", box(300, 0, 400, 100))])
    fields = fields_of(
        [
            box(120, 10, 280, 90),  # entirely on the null unit
            box(1000, 0, 1100, 100),  # outside every polygon
            box(99, 0, 199, 100),  # 1 % on A, 99 % on the null unit
            box(310, 10, 390, 90),  # on a map unit that is missing from the attribute table
            box(50, 0, 150, 200),  # a quarter on A, a quarter on W, half outside every polygon
        ]
    )
    table = field_organic_matter(fields, units, OM)
    assert table["organic_matter_pct"].isna().tolist() == [True, True, False, True, False]
    assert table["organic_matter_pct"].iloc[2] == pytest.approx(1.0)
    assert table["om_coverage"].tolist() == pytest.approx([0.0, 0.0, 0.01, 0.0, 0.25])
    assert table["ssurgo_coverage"].tolist() == pytest.approx([1.0, 0.0, 1.0, 1.0, 0.5])
    assert table["dominant_mukey"].isna().tolist() == [False, True, False, False, False]
    assert table["dominant_mukey"].dropna().tolist() == ["W", "W", "Z", "A"]  # W/A tie in the last field -> "A"


def test_min_coverage_turns_thinly_covered_fields_into_nan():
    units = mupolygons([("A", box(0, 0, 100, 100)), ("W", box(100, 0, 300, 100))])
    fields = fields_of([box(99, 0, 199, 100), box(60, 0, 160, 100), box(0, 0, 100, 100)])  # 1 %, 40 %, 100 % on A
    default = field_organic_matter(fields, units, OM)
    strict = field_organic_matter(fields, units, OM, min_coverage=0.5)
    assert default["organic_matter_pct"].tolist() == pytest.approx([1.0, 1.0, 1.0])
    assert strict["organic_matter_pct"].isna().tolist() == [True, True, False]
    assert strict["om_coverage"].tolist() == pytest.approx([0.01, 0.4, 1.0])  # coverage is reported either way


def test_floating_point_slivers_do_not_count_as_coverage():
    # the field only shares an edge with unit A, give or take a rounding error of a nanometre
    units = mupolygons([("A", box(0, 0, 100, 100)), ("W", box(100, 0, 300, 100))])
    table = field_organic_matter(fields_of([box(100 - 1e-9, 0, 200, 100)]), units, OM)
    assert np.isnan(table["organic_matter_pct"].iloc[0]) and table["om_coverage"].iloc[0] < 1e-9


def test_pieces_of_the_same_map_unit_add_up():
    units = mupolygons([("A", box(0, 0, 300, 100)), ("B", box(300, 0, 600, 100)), ("A", box(600, 0, 1000, 100))])
    table = field_organic_matter(fields_of([box(0, 0, 1000, 100)]), units, OM)
    assert table["organic_matter_pct"].iloc[0] == pytest.approx(0.7 * 1.0 + 0.3 * 3.0)
    assert table["n_mapunits"].iloc[0] == 2
    assert table["dominant_mukey"].iloc[0] == "A" and table["dominant_share"].iloc[0] == pytest.approx(0.7)


def test_invalid_geometries_are_repaired_before_the_overlay():
    bowtie_field = Polygon([(0, 0), (200, 200), (200, 0), (0, 200)])  # two triangles, 10,000 m2 each
    bowtie_unit = Polygon([(0, 0), (0, 200), (100, 200), (100, 0), (0, 0), (0, 200)])  # doubles back on itself
    assert not bowtie_field.is_valid and not bowtie_unit.is_valid
    units = mupolygons([("A", bowtie_unit), ("B", box(100, 0, 200, 200))])
    table = field_organic_matter(fields_of([bowtie_field]), units, OM)
    assert table["organic_matter_pct"].iloc[0] == pytest.approx(2.0)  # left triangle on A, right triangle on B
    assert table["om_coverage"].iloc[0] == pytest.approx(1.0)


def test_polygonal_keeps_only_the_polygon_part():
    mixed = shapely.GeometryCollection([box(0, 0, 1, 1), shapely.LineString([(5, 5), (6, 6)])])
    cleaned = soil._polygonal([mixed, None, shapely.Point(1, 1), Polygon()])
    assert cleaned[0].equals(box(0, 0, 1, 1))
    assert cleaned[1] is None and cleaned[2] is None and cleaned[3] is None


def test_index_and_order_of_the_fields_are_preserved_and_missing_geometry_is_nan():
    units = mupolygons([("A", box(0, 0, 500, 1000)), ("B", box(500, 0, 2000, 1000))])
    fields = fields_of([box(600, 0, 700, 100), None, box(0, 0, 100, 100)], index=["z", "m", "a"])
    table = field_organic_matter(fields, units, OM)
    assert list(table.index) == ["z", "m", "a"]
    assert table["organic_matter_pct"].iloc[0] == 3.0 and table["organic_matter_pct"].iloc[2] == 1.0
    assert np.isnan(table["organic_matter_pct"].iloc[1]) and table["om_coverage"].iloc[1] == 0.0


def test_no_map_unit_polygons_at_all_gives_nan_not_an_exception():
    units = mupolygons([("A", box(0, 0, 1, 1))]).iloc[:0]
    table = field_organic_matter(fields_of([box(0, 0, 100, 100)]), units, OM)
    assert np.isnan(table["organic_matter_pct"].iloc[0]) and table["n_mapunits"].iloc[0] == 0


def test_lonlat_inputs_are_weighted_by_equal_area_not_by_degrees():
    # Two map units split along the 38 N parallel; the field spans 36-38 N (unit A) and 38-40 N (unit B).
    # Equal in degrees, but in true area the southern half is larger: the mean must lean towards A.
    units = mupolygons([("A", box(-100.6, 35, -100.4, 38)), ("B", box(-100.6, 38, -100.4, 41))], crs="EPSG:4326")
    field = fields_of([box(-100.6, 36, -100.4, 40)], crs="EPSG:4326")
    table = field_organic_matter(field, units, OM)
    halves = gpd.GeoSeries([box(-100.6, 36, -100.4, 38), box(-100.6, 38, -100.4, 40)], crs="EPSG:4326")
    areas = halves.to_crs("EPSG:5070").area
    expected = (areas[0] * 1.0 + areas[1] * 3.0) / areas.sum()
    assert areas[0] > 1.02 * areas[1]
    assert table["organic_matter_pct"].iloc[0] == pytest.approx(expected, rel=1e-6)
    assert table["organic_matter_pct"].iloc[0] < 1.99
    assert table["om_coverage"].iloc[0] == pytest.approx(1.0, abs=1e-6)


# ----------------------------------------------------------------------------- tiles


def test_tiles_are_a_fixed_grid_aligned_to_round_coordinates():
    assert tiles_for_bounds([(-100.83, 37.88, -100.77, 37.92)]) == [(-1009, 378), (-1009, 379), (-1008, 378), (-1008, 379)]
    assert soil.tile_bounds(-1009, 379) == (-100.9, 37.9, -100.8, 38.0)
    assert soil.tile_wkt(-1009, 379) == "POLYGON((-100.9 37.9, -100.8 37.9, -100.8 38, -100.9 38, -100.9 37.9))"
    assert soil.tile_wkt(-1010, 380) == "POLYGON((-101 38, -100.9 38, -100.9 38.1, -101 38.1, -101 38))"


def test_tiles_of_a_field_do_not_depend_on_the_other_fields():
    a = [(-100.83, 37.88, -100.77, 37.92)]
    b = [(-101.05, 38.21, -101.04, 38.22), (-100.31, 37.74, -100.29, 37.76)]
    assert set(tiles_for_bounds(a + b)) == set(tiles_for_bounds(a)) | set(tiles_for_bounds(b))
    assert tiles_for_bounds([(np.nan, np.nan, np.nan, np.nan)]) == []


def test_tile_cache_file_is_keyed_by_position_and_query(tmp_path):
    path = soil.tile_path(tmp_path, -1009, 379)
    assert re.fullmatch(r"mupolygon_-100\.9_37\.9_0\.1deg_[0-9a-f]{10}\.parquet", path.name)
    assert path == soil.tile_path(tmp_path, -1009, 379)
    assert path.name != soil.tile_path(tmp_path, -1009, 380).name
    assert path.name.split("_")[-1] != soil.tile_path(tmp_path, -1009, 379, tiles_per_degree=20).name.split("_")[-1]


def test_one_field_spanning_the_planet_is_refused():
    with pytest.raises(ValueError, match="tiles"):
        tiles_for_bounds([(-170, -80, 170, 80)])


# ----------------------------------------------------------------------------- fake Soil Data Access


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


HTML_ERROR = FakeResponse(503, text="<html><body><h1>Service Unavailable</h1></body></html>")
HTML_200 = FakeResponse(200, text="<html><body>The service is being updated</body></html>")


def service_exception(message: str) -> FakeResponse:
    body = f"<?xml version='1.0'?><ServiceExceptionReport><ServiceException>\r\n{message}</ServiceException></ServiceExceptionReport>"
    return FakeResponse(400, text=body)


# Three map units side by side: A west of -100.85, B up to -100.7, water up to -100.6.
WORLD = [
    ("9001", "101", "KS055", "A", box(-101.0, 37.8, -100.85, 38.0)),
    ("9002", "202", "KS055", "B", box(-100.85, 37.8, -100.7, 38.0)),
    ("9003", "303", "KS081", "W", box(-100.7, 37.8, -100.6, 38.0)),
]
# mukey -> [(cokey, compname, comppct, majcompflag, [(chkey, hzname, master, top, bottom, om)])]
COMPONENTS = {
    "101": [
        ("c1", "Alpha", "90", "Yes", [("h1", "Ap", "A", "0", "15", "1"), ("h2", "Bt", "B", "15", "60", "0.4")]),
        ("c2", "Rock outcrop", "10", "No ", []),
    ],
    "202": [("c3", "Beta", "100", "Yes", [("h3", "A", "A", "0", "20", "3")])],
    "303": [("c4", "Water", "100", "Yes", [])],
}
ATTR_QUERY_COLUMNS = [
    "mukey", "musym", "muname", "mukind", "areasymbol", "cokey", "compname", "compkind", "comppct_r", "majcompflag",
    "chkey", "hzname", "desgnmaster", "hzdept_r", "hzdepb_r", "om_r",
]  # fmt: skip
F_STRADDLE = box(-100.87, 37.90, -100.83, 37.92)  # half on A (1 %), half on B (3 %)  -> tile (-1009, 379)
F_WEST = box(-100.95, 37.85, -100.93, 37.87)  # on A                                   -> tile (-1010, 378)
F_WATER = box(-100.66, 37.90, -100.64, 37.92)  # on water                              -> tile (-1007, 379)
F_SHORE = box(-100.72, 37.90, -100.68, 37.92)  # half on B, half on water              -> tiles (-1008|-1007, 379)


class FakeSDA:
    """Answers the three kinds of query ssurgo.py sends, from WORLD / COMPONENTS, and counts them."""

    def __init__(self):
        self.world = list(WORLD)
        self.calls: list[str] = []  # "tile", "attr", "survey", "spot" in arrival order
        self.tile_requests: list[tuple[float, float]] = []
        self.scripted: list = []  # responses / exceptions served first, one per request
        self.broken_tiles: set[tuple[float, float]] = set()  # SW corners that always fail
        self.lock = threading.Lock()

    def post(self, url, data=None, timeout=None, headers=None):
        sql = data["query"]
        assert data["format"] == "JSON+COLUMNNAME"
        with self.lock:
            if self.scripted:
                item = self.scripted.pop(0)
                self.calls.append("scripted")
                if isinstance(item, Exception):
                    raise item
                return item
        if sql.startswith("WITH hit AS"):
            with self.lock:
                self.calls.append("spot")
            rows = [["101", "A", "Alpha", "0.0004", "1"], ["303", "W", "Water", "0.0002", None], ["202", "B", "Beta", "0.0004", "3"]]
            return FakeResponse(payload={"Table": [["mukey", "musym", "muname", "area_deg2", "om_pct"], *rows]})
        if "SDA_Get_Mupolygonkey_from_intersection_with_WktWgs84" in sql:
            coords = re.search(r"POLYGON\(\((.*?)\)\)", sql).group(1)
            xs, ys = zip(*[tuple(map(float, pair.split())) for pair in coords.split(",")])
            corner = (min(xs), min(ys))
            with self.lock:
                self.calls.append("tile")
                self.tile_requests.append(corner)
            if corner in self.broken_tiles:
                return HTML_ERROR
            tile = box(min(xs), min(ys), max(xs), max(ys))
            rows = [[key, mukey, area, sym, geom.wkt] for key, mukey, area, sym, geom in self.world if geom.intersects(tile)]
            if not rows:
                return FakeResponse(payload={})
            return FakeResponse(payload={"Table": [["mupolygonkey", "mukey", "areasymbol", "musym", "wkt"], *rows]})
        if "FROM mapunit" in sql:
            with self.lock:
                self.calls.append("attr")
            wanted = re.search(r"IN \((.*?)\)", sql).group(1).split(", ")
            rows = []
            for mukey in wanted:
                for cokey, name, pct, major, horizons in COMPONENTS.get(mukey, []):
                    base = [mukey, "s" + mukey, "unit " + mukey, "Consociation", "KS055", cokey, name, "Series", pct, major]
                    if not horizons:
                        rows.append([*base, None, None, None, None, None, None])
                    for chkey, hzname, master, top, bottom, om in horizons:
                        rows.append([*base, chkey, hzname, master, top, bottom, om])
            return FakeResponse(payload={"Table": [ATTR_QUERY_COLUMNS, *rows]} if rows else {})
        if "sacatalog" in sql:
            with self.lock:
                self.calls.append("survey")
            symbols = re.findall(r"'([A-Z0-9]+)'", sql)
            rows = [[s, f"{s} County", "24", "8/27/2026"] for s in symbols]
            return FakeResponse(payload={"Table": [["areasymbol", "areaname", "saversion", "saverest"], *rows]})
        raise AssertionError(f"unexpected query: {sql[:80]}")

    def client(self, **kwargs) -> SDAClient:
        self.sleeps: list[float] = []
        kwargs.setdefault("sleep", self.sleeps.append)
        return SDAClient(post=self.post, **kwargs)


def lonlat_fields(*geoms) -> gpd.GeoDataFrame:
    return fields_of(geoms, crs="EPSG:4326")


def run(fake, fields, cache_dir, **kwargs):
    return organic_matter_pct(fields, client=fake.client(**kwargs.pop("client_options", {})), cache_dir=cache_dir, verbose=False, **kwargs)


# ----------------------------------------------------------------------------- end to end (faked SDA)


def test_end_to_end_values_index_and_provenance(tmp_path):
    fake = FakeSDA()
    fields = lonlat_fields(F_STRADDLE, F_WEST, F_WATER, F_SHORE)
    fields.index = [10, 20, 30, 40]
    om = run(fake, fields, tmp_path)

    assert isinstance(om, pd.Series) and om.name == "organic_matter_pct" and om.dtype == float
    assert list(om.index) == [10, 20, 30, 40]
    assert om.tolist()[:2] == pytest.approx([2.0, 1.0], abs=1e-3)
    assert np.isnan(om.loc[30])  # water only: SSURGO has no value
    assert om.loc[40] == pytest.approx(3.0)  # half water: the value comes from the other half

    prov = om.attrs["provenance"]
    json.dumps(prov, allow_nan=False)  # plain, JSON-serialisable
    assert prov["endpoint"] == soil.SDA_URL
    assert prov["tile_scheme"]["n_tiles"] == 4 and prov["tile_scheme"]["tiles_fetched"] == 4
    assert prov["counts"]["fields"] == 4 and prov["counts"]["fields_with_value"] == 3 and prov["counts"]["fields_nan"] == 1
    assert prov["counts"]["mupolygons"] == 3  # polygons returned by several tiles are counted once
    assert prov["counts"]["mupolygon_rows_in_tiles"] == 7
    assert prov["counts"]["mukeys"] == 3 and prov["counts"]["mukeys_with_om"] == 2
    assert [m["mukey"] for m in prov["counts"]["mukeys_without_om"]] == ["303"]
    assert prov["coverage"]["full_ge_0.999"] == 2 and prov["coverage"]["partial"] == 1 and prov["coverage"]["none"] == 1
    assert prov["coverage"]["distribution"]["min"] == 0.0
    assert {a["areasymbol"] for a in prov["survey_areas"]} == {"KS055", "KS081"}
    assert "SDA_Get_Mupolygonkey_from_intersection_with_WktWgs84" in prov["queries"]["mupolygon_tile"]
    assert prov["http"]["http_requests"] == 6  # 4 tiles + 1 attribute query + 1 version query: never one per field
    assert sorted(fake.calls) == ["attr", "survey", "tile", "tile", "tile", "tile"]


def test_detail_table_reports_coverage_per_field(tmp_path):
    fake = FakeSDA()
    table = field_soil_table(lonlat_fields(F_STRADDLE, F_SHORE, F_WATER), client=fake.client(), cache_dir=tmp_path, verbose=False)
    assert table["om_coverage"].tolist() == pytest.approx([1.0, 0.5, 0.0], abs=1e-6)
    assert table["ssurgo_coverage"].tolist() == pytest.approx([1.0, 1.0, 1.0], abs=1e-6)
    assert table["dominant_muname"].tolist()[2] == "unit 303"
    assert table["n_mapunits"].tolist() == [2, 2, 1]


def test_second_run_is_served_from_the_cache_without_any_request(tmp_path):
    fake = FakeSDA()
    fields = lonlat_fields(F_STRADDLE, F_WEST, F_SHORE)
    first = run(fake, fields, tmp_path)
    n_calls = len(fake.calls)
    second = run(fake, fields, tmp_path)
    assert len(fake.calls) == n_calls
    assert second.attrs["provenance"]["http"]["http_requests"] == 0
    assert second.attrs["provenance"]["tile_scheme"]["tiles_from_cache"] == 4  # F_SHORE spans two tiles
    pd.testing.assert_series_equal(first, second)


def test_a_new_extent_only_fetches_the_missing_tiles(tmp_path):
    fake = FakeSDA()
    run(fake, lonlat_fields(F_STRADDLE), tmp_path)
    assert fake.calls.count("tile") == 1 and fake.tile_requests == [(-100.9, 37.9)]
    om = run(fake, lonlat_fields(F_STRADDLE, F_WEST), tmp_path)
    assert fake.tile_requests == [(-100.9, 37.9), (-101.0, 37.8)]  # only the new tile
    assert fake.calls.count("attr") == 1  # its map unit (101) was already cached
    assert om.attrs["provenance"]["http"]["http_requests"] == 1
    assert om.tolist() == pytest.approx([2.0, 1.0], abs=1e-3)
    # a tile that brings a new map unit asks for that map unit only
    run(fake, lonlat_fields(F_STRADDLE, F_WEST, F_SHORE), tmp_path)
    assert fake.calls.count("attr") == 2 and fake.calls.count("tile") == 4


def test_a_subset_of_a_cached_extent_needs_no_request_at_all(tmp_path):
    # This is what the county-wide warm-up buys: any later field set inside the same tiles is offline.
    fake = FakeSDA()
    run(fake, lonlat_fields(F_STRADDLE, F_WEST, F_WATER, F_SHORE), tmp_path)
    n_calls = len(fake.calls)
    om = run(fake, lonlat_fields(F_SHORE), tmp_path)
    assert len(fake.calls) == n_calls and om.attrs["provenance"]["http"]["http_requests"] == 0
    assert om.iloc[0] == pytest.approx(3.0)
    assert [a["areasymbol"] for a in om.attrs["provenance"]["survey_areas"]] == ["KS055", "KS081"]


def test_map_unit_unknown_to_sda_is_null_and_is_not_asked_for_again(tmp_path):
    fake = FakeSDA()
    fake.world.append(("9004", "404", "KS055", "X", box(-100.6, 37.8, -100.5, 38.0)))  # no mapunit record for 404
    fields = lonlat_fields(box(-100.61, 37.90, -100.59, 37.92), F_WEST)  # half water, half the unknown unit
    om = run(fake, fields, tmp_path)
    assert np.isnan(om.iloc[0]) and om.iloc[1] == pytest.approx(1.0)
    assert [m["mukey"] for m in om.attrs["provenance"]["counts"]["mukeys_without_om"]] == ["303", "404"]
    n_calls = len(fake.calls)
    run(fake, fields, tmp_path)
    assert len(fake.calls) == n_calls


def test_refresh_ignores_the_cache(tmp_path):
    fake = FakeSDA()
    run(fake, lonlat_fields(F_STRADDLE), tmp_path)
    before = len(fake.calls)
    om = run(fake, lonlat_fields(F_STRADDLE), tmp_path, refresh=True)
    assert len(fake.calls) == before + 3
    assert om.iloc[0] == pytest.approx(2.0, abs=1e-3)
    assert len(list(tmp_path.glob("muattr_*.parquet"))) == 1


def test_fields_in_another_crs_give_the_same_answer(tmp_path):
    fake = FakeSDA()
    fields = lonlat_fields(F_STRADDLE, F_WEST)
    a = run(fake, fields, tmp_path)
    b = run(fake, fields.to_crs("EPSG:5070"), tmp_path)
    assert b.tolist() == pytest.approx(a.tolist(), abs=1e-3)
    with pytest.raises(ValueError, match="CRS"):
        run(fake, fields_of([F_STRADDLE], crs=None), tmp_path)


def test_empty_field_table_returns_an_empty_series_without_requests(tmp_path):
    fake = FakeSDA()
    om = run(fake, lonlat_fields(F_WEST).iloc[:0], tmp_path)
    assert len(om) == 0 and fake.calls == []


# ----------------------------------------------------------------------------- robustness


def test_transient_failures_are_retried_with_backoff(tmp_path):
    fake = FakeSDA()
    fake.scripted = [HTML_200, requests.exceptions.ReadTimeout("read timed out"), HTML_ERROR]
    om = run(fake, lonlat_fields(F_STRADDLE), tmp_path, max_workers=1)
    assert om.iloc[0] == pytest.approx(2.0, abs=1e-3)
    assert fake.sleeps == [2.0, 4.0, 8.0]
    assert om.attrs["provenance"]["http"] == {"queries": 3, "http_requests": 6, "retries": 3}


def test_a_tile_that_keeps_failing_raises_a_clear_error_and_never_all_nan(tmp_path):
    fake = FakeSDA()
    fake.broken_tiles = {(-100.9, 37.9)}
    with pytest.raises(SoilDataError) as err:
        # the pause between attempts gives the healthy tile time to arrive before the broken one gives up
        slow = {"retries": 3, "sleep": lambda seconds: time.sleep(0.05)}
        run(fake, lonlat_fields(F_STRADDLE, F_WEST), tmp_path, client_options=slow)
    message = str(err.value)
    assert "lon -100.9..-100.8, lat 37.9..38" in message and "3 time(s)" in message and "HTTP 503" in message
    assert fake.tile_requests.count((-100.9, 37.9)) == 3
    # the tile that did arrive is cached, the broken one is not
    assert [p.name.split("_")[1:3] for p in tmp_path.glob("mupolygon_*.parquet")] == [["-101", "37.8"]]
    # once SDA recovers, only the missing tile is requested
    fake.broken_tiles = set()
    fake.tile_requests.clear()
    om = run(fake, lonlat_fields(F_STRADDLE, F_WEST), tmp_path)
    assert fake.tile_requests == [(-100.9, 37.9)]
    assert om.tolist() == pytest.approx([2.0, 1.0], abs=1e-3)


def test_no_new_tile_request_is_started_once_a_tile_has_failed_for_good(tmp_path):
    fake = FakeSDA()
    fake.broken_tiles = {(-100.9, 37.9), (-101.0, 37.8), (-100.8, 37.9), (-100.7, 37.9)}
    fields = lonlat_fields(F_STRADDLE, F_WEST, F_WATER, F_SHORE)  # 4 tiles, all broken
    with pytest.raises(SoilDataError, match="map unit polygons for tile"):
        run(fake, fields, tmp_path, max_workers=1, client_options={"retries": 2})
    assert len(fake.tile_requests) == 2  # the first tile's two attempts; the other three were never asked for
    assert list(tmp_path.glob("*")) == []


def test_failing_attribute_query_raises(tmp_path):
    fake = FakeSDA()
    client = fake.client(retries=2)
    run(fake, lonlat_fields(F_STRADDLE), tmp_path)  # tiles + attributes for 101, 202 cached
    for path in tmp_path.glob("muattr_*.parquet"):
        path.unlink()
    fake.scripted = [HTML_ERROR, HTML_ERROR]
    with pytest.raises(SoilDataError, match="component/horizon attributes for 2 map unit"):
        organic_matter_pct(lonlat_fields(F_STRADDLE), client=client, cache_dir=tmp_path, verbose=False)
    assert list(tmp_path.glob("muattr_*.parquet")) == []


def test_a_rejected_query_is_not_retried():
    fake = FakeSDA()
    fake.scripted = [service_exception("Invalid query: Incorrect syntax near &#39;FRM&#39;.")]
    client = fake.client()
    with pytest.raises(SoilDataError, match="rejected the query .*Incorrect syntax near 'FRM'"):
        client.query("SELEC 1 FRM x", what="test")
    assert client.stats == {"queries": 1, "http_requests": 1, "retries": 0} and fake.sleeps == []


def test_server_side_timeouts_reported_as_invalid_query_are_retried():
    fake = FakeSDA()
    fake.scripted = [
        service_exception("Invalid query: Execution Timeout Expired."),
        FakeResponse(payload={"Table": [["a", "b"], ["1", None]]}),
    ]
    client = fake.client()
    frame = client.query("SELECT 1", what="test")
    assert frame.to_dict("records") == [{"a": "1", "b": None}] and fake.sleeps == [2.0]


def test_empty_answer_is_an_empty_table_and_odd_json_is_retried():
    fake = FakeSDA()
    fake.scripted = [FakeResponse(payload={})]
    assert fake.client().query("SELECT 1").empty
    fake.scripted = [FakeResponse(payload={"Message": "busy"}), FakeResponse(payload=[1, 2]), FakeResponse(payload={})]
    client = fake.client()
    assert client.query("SELECT 1").empty and client.stats["retries"] == 2


def test_no_value_for_any_field_raises_instead_of_returning_all_nan(tmp_path):
    fake = FakeSDA()
    with pytest.raises(SoilDataError, match="no organic matter value for any of the 1 field"):
        run(fake, lonlat_fields(F_WATER), tmp_path)
    far_away = lonlat_fields(box(-90.01, 30.0, -90.0, 30.01))  # the fake SDA has no polygons there
    with pytest.raises(SoilDataError, match="0 map unit polygon"):
        run(fake, far_away, tmp_path)


def test_unparsable_geometry_from_sda_raises(tmp_path):
    fake = FakeSDA()
    bad = {"Table": [["mupolygonkey", "mukey", "areasymbol", "musym", "wkt"], ["1", "101", "KS055", "A", "POLYGON ((0 0, 1 1"]]}
    fake.scripted = [FakeResponse(payload=bad)]
    with pytest.raises(SoilDataError, match="cannot be parsed"):
        run(fake, lonlat_fields(F_STRADDLE), tmp_path)
    assert list(tmp_path.glob("mupolygon_*.parquet")) == []


def test_unreadable_cache_file_is_fetched_again(tmp_path):
    fake = FakeSDA()
    run(fake, lonlat_fields(F_STRADDLE), tmp_path)
    (tile_file,) = tmp_path.glob("mupolygon_*.parquet")
    tile_file.write_bytes(b"not a parquet file")
    om = run(fake, lonlat_fields(F_STRADDLE), tmp_path)
    assert fake.calls.count("tile") == 2 and om.iloc[0] == pytest.approx(2.0, abs=1e-3)


# ----------------------------------------------------------------------------- spot check + CLI


def test_spot_check_reference_is_aggregated_from_sdas_own_rows():
    fake = FakeSDA()
    ref = soil.sda_field_organic_matter(box(0, 0, 0.05, 0.02), fake.client())  # 0.001 square degrees
    assert ref["om_pct"] == pytest.approx((0.0004 * 1 + 0.0004 * 3) / 0.0008)
    assert ref["om_coverage"] == pytest.approx(0.8) and ref["n_mapunits"] == 3
    assert fake.calls == ["spot"]


def test_spot_check_compares_local_values_with_the_reference(tmp_path):
    fake = FakeSDA()
    fields = lonlat_fields(F_STRADDLE, F_WEST, F_WATER)
    client = fake.client()
    table = field_soil_table(fields, client=client, cache_dir=tmp_path, verbose=False)
    check = soil.spot_check(fields, table, 5, client=client)
    assert len(check) == 2 and set(check["field"]) == {"KS-000001", "KS-000002"}  # the NaN field is not checked
    assert fake.calls.count("spot") == 2
    assert check["om_sda"].tolist() == pytest.approx([2.0, 2.0])
    assert check["diff"].tolist() == pytest.approx((check["om_local"] - 2.0).tolist())


def test_cli_prints_per_field_values_and_summary(tmp_path, monkeypatch, capsys):
    fake = FakeSDA()
    monkeypatch.setattr(soil.requests, "post", fake.post)
    path = tmp_path / "fields.parquet"
    lonlat_fields(F_STRADDLE, F_WEST, F_WATER).to_parquet(path)
    assert soil.main([str(path), "--cache-dir", str(tmp_path / "cache")]) == 0
    out = capsys.readouterr().out
    assert re.search(r"KS-000001\s+2\.000\s+1\.000\s+2\s+unit (101|202)", out)
    assert re.search(r"KS-000003\s+NaN\s+0\.000\s+1\s+unit 303", out)
    assert "n=2  NaN=1  min=1.000" in out and "full (>=0.999): 2   partial: 0   none: 1   below 0.5: 0" in out
    assert "KS055 KS055 County: version 24" in out
    assert soil.main([str(tmp_path / "missing.parquet")]) == 1


def test_cli_reports_failure_with_a_nonzero_exit_code(tmp_path, monkeypatch, capsys):
    fake = FakeSDA()
    fake.broken_tiles = {(-100.9, 37.9)}
    monkeypatch.setattr(soil.requests, "post", fake.post)
    monkeypatch.setattr(soil.time, "sleep", lambda seconds: None)
    path = tmp_path / "fields.parquet"
    lonlat_fields(F_STRADDLE).to_parquet(path)
    assert soil.main([str(path), "--cache-dir", str(tmp_path / "cache")]) == 2
    assert "FAILED:" in capsys.readouterr().err
