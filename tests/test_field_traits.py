"""pipeline/field_traits.py and its validator. No network, no Earth Engine."""
import numpy as np
import pandas as pd
import pytest

from pipeline import field_traits as ft
from pipeline import field_traits_schema as schema

YEARS = [str(y) for y in range(2008, 2025)]


def history(*crops):
    """A 17-year history cycling through `crops`."""
    return {y: crops[i % len(crops)] for i, y in enumerate(YEARS)}


# --------------------------------------------------------------------------- irrigation rule


def test_crop_shares():
    thirsty, fallow, n = ft.crop_shares({"2020": "corn", "2021": "fallow", "2022": "alfalfa", "2023": "winter wheat"})
    assert (thirsty, fallow, n) == (0.5, 0.25, 4)
    assert ft.crop_shares({}) == (0.0, 0.0, 0)


def test_continuous_corn_is_irrigated_and_wheat_fallow_is_dryland_without_et():
    label, prob, basis = ft.classify_irrigation(0.6, history("corn"))
    assert label == "irrigated" and prob > 0.9
    assert "corn/soybeans/alfalfa in 17 of 17 years" in basis and "no water-use data" in basis
    label, prob, basis = ft.classify_irrigation(0.6, history("winter wheat", "fallow"))
    assert label == "dryland" and prob < 0.05
    assert "fallow in 8 of 17 years" in basis


def test_pivot_circle_tips_a_wheat_sorghum_field():
    square = ft.classify_irrigation(0.64, history("winter wheat", "sorghum"))
    circle = ft.classify_irrigation(0.90, history("winter wheat", "sorghum"))
    assert square[0] == "dryland" and circle[0] == "irrigated"
    assert "pivot-circle outline" in circle[2]


def test_water_use_overrides_crops():
    # wheat-sorghum that uses 350 mm more than the rain: irrigated wheat and sorghum exist here
    label, prob, basis = ft.classify_irrigation(0.6, history("winter wheat", "sorghum"), et_excess_mm=350.0)
    assert label == "irrigated" and prob > 0.9 and "water use 350 mm above rain" in basis
    # corn every other year but no more water than the rain: dryland corn
    label, prob, basis = ft.classify_irrigation(0.6, history("corn", "winter wheat"), et_excess_mm=-20.0)
    assert label == "dryland" and "water use 20 mm below rain" in basis and "no water-use data" not in basis
    assert "water use only 60 mm above rain" in ft.classify_irrigation(0.6, history("winter wheat", "fallow"), 60.0)[2]


def test_probability_rises_with_water_use_and_is_capped():
    probs = [ft.classify_irrigation(0.6, history("winter wheat", "sorghum"), x)[1] for x in (0, 100, 200, 300, 2000)]
    assert probs == sorted(probs) and probs[0] < 0.5 < probs[3]
    assert ft.irrigation_points(0.6, 0, 0, 2000.0)["et"] == ft.ET_POINTS_MAX
    assert ft.irrigation_points(0.6, 0, 0, -2000.0)["et"] == -ft.ET_POINTS_MAX
    assert "et" not in ft.irrigation_points(0.6, 0, 0, float("nan"))


def test_every_field_gets_an_answer():
    label, prob, basis = ft.classify_irrigation(0.5, {})
    assert label in ("irrigated", "dryland") and 0 <= prob <= 1 and basis


# --------------------------------------------------------------------------- SSURGO wind erodibility


def test_clean_weg_and_wei():
    assert [ft.clean_weg(v) for v in ("4l", "3", "3.0", " 6 ", None, "", "9", float("nan"))] == ["4L", "3", "3", "6", None, None, None, None]
    assert ft.clean_wei("86") == 86.0
    assert np.isnan(ft.clean_wei(None)) and np.isnan(ft.clean_wei("999")) and np.isnan(ft.clean_wei("x"))


def test_mapunit_weg_takes_the_dominant_component():
    rows = pd.DataFrame(
        [
            ("1", "11", "85", "Richfield", "6", "48"),
            ("1", "12", "15", "Ulysses", "4L", "86"),
            ("2", "22", "50", "B", "3", "86"),  # tie: the smaller cokey wins
            ("2", "21", "50", "A", "5", "56"),
            ("3", "31", "90", "Water", None, None),  # dominant has no group: next component
            ("3", "32", "10", "Valent", "1", "220"),
            ("4", None, None, None, None, None),  # map unit without components
        ],
        columns=ft.WEG_COLUMNS,
    )
    out = ft.mapunit_weg(rows)
    assert out.loc["1"].tolist() == ["6", 48.0]
    assert out.loc["2"].tolist() == ["5", 56.0]
    assert out.loc["3"].tolist() == ["1", 220.0]
    assert "4" not in out.index


class FakeSDA:
    def __init__(self):
        self.calls = 0

    def query(self, sql, what=""):
        self.calls += 1
        return pd.DataFrame([("101", "9", "100", "Richfield", "6", "48")], columns=ft.WEG_COLUMNS)


def test_weg_rows_are_cached(tmp_path):
    client = FakeSDA()
    first = ft.load_weg_rows(["101", "102"], client, tmp_path)
    assert client.calls == 1 and len(list(tmp_path.glob("muweg_*.parquet"))) == 1
    again = ft.load_weg_rows(["101", "102"], client, tmp_path)
    assert client.calls == 1  # 102 has no component; that answer is cached too
    assert first["mukey"].tolist() == again["mukey"].tolist() == ["101", "102"]
    assert ft.mapunit_weg(again).loc["101"].tolist() == ["6", 48.0]


# --------------------------------------------------------------------------- OpenET pieces (no Earth Engine)


def test_sums_to_et_and_window():
    assert ft.sums_to_et([6000.0, 10.0]) == 600.0
    assert ft.sums_to_et([0.0, 0.0]) is None
    assert ft.gs_window(2020) == ("2020-04-01", "2020-10-01")


def test_et_summary_averages_years_and_subtracts_rain():
    et_long = pd.DataFrame({"field_id": ["a", "a", "b", "b"], "year": [2020, 2021, 2020, 2021], "et_mm": [700.0, 600.0, 300.0, None]})
    out = ft.et_summary(et_long, {2020: 350.0, 2021: 250.0})
    assert out.loc["a"].tolist() == [650.0, 350.0, 2]
    assert out.loc["b"].tolist() == [300.0, -50.0, 1]
    assert ft.et_summary(et_long.iloc[:0], {}).empty


class FakeChunkEngine:
    def __init__(self):
        self.requests = []

    def reduce(self, chunk, year):
        self.requests.append((len(chunk), year))
        return {fid: 500.0 + year - 2000 for fid in chunk.field_ids}, 1.0


def make_fields(n=6):
    import geopandas as gpd
    from shapely.geometry import box

    geoms = [box(-100.9 + 0.01 * i, 37.9, -100.895 + 0.01 * i, 37.905) for i in range(n)]
    return gpd.GeoDataFrame({"field_id": [f"KS-{i:06d}" for i in range(n)]}, geometry=geoms, crs="EPSG:4326")


def test_fetch_openet_caches_and_never_asks_twice(tmp_path):
    fields = make_fields()
    engine = FakeChunkEngine()
    frame, report = ft.fetch_openet(fields, [2020, 2021], engine=engine, cache_dir=tmp_path, chunk_size=3, say=lambda m: None)
    assert len(frame) == 12 and frame["et_mm"].notna().all()
    assert report["requests_run"] == 4 and report["status"] == "ok"
    assert len(engine.requests) == 5  # the pilot + 4
    assert report["eecu_hours_total"] == pytest.approx(5 / 3600, abs=1e-4)
    again, report2 = ft.fetch_openet(fields, [2020, 2021], engine=None, cache_dir=tmp_path, chunk_size=3, say=lambda m: None)
    assert report2["requests_cached"] == 4 and len(again) == 12
    engine2 = FakeChunkEngine()
    ft.fetch_openet(fields, [2020, 2021], engine=engine2, cache_dir=tmp_path, chunk_size=3, say=lambda m: None)
    assert engine2.requests == []


def test_fetch_openet_respects_the_budget(tmp_path):
    fields = make_fields()

    class Dear(FakeChunkEngine):
        def reduce(self, chunk, year):
            values, _ = super().reduce(chunk, year)
            return values, 1800.0  # half an EECU-hour per request

    engine = Dear()
    frame, report = ft.fetch_openet(fields, [2020, 2021], engine=engine, cache_dir=tmp_path, budget_hours=1.0, chunk_size=6, say=lambda m: None)
    assert report["requests_run"] == 0 and report["status"] == "partial: budget"
    assert len(engine.requests) == 1  # only the pilot
    assert frame.empty

    engine = FakeChunkEngine()
    _, report = ft.fetch_openet(fields, [2020], engine=engine, cache_dir=tmp_path / "b", estimate_only=True, chunk_size=6, say=lambda m: None)
    assert report["status"] == "estimate only" and len(engine.requests) == 1


# --------------------------------------------------------------------------- table, checks, validator


def small_table():
    ids = ["KS-000001", "KS-000002", "KS-000003"]
    weg = pd.DataFrame({"weg": ["6", None, "4L"], "wei": [48.0, np.nan, 86.0]})
    et = pd.DataFrame({"et_gs_mm": [720.0, 310.0], "et_excess_mm": [400.0, -10.0]}, index=ids[:2])
    histories = [history("corn"), history("winter wheat", "fallow"), history("winter wheat", "sorghum")]
    return ids, histories, ft.build_table(ids, [0.9, 0.6, 0.6], histories, weg, et)


def test_build_table_matches_the_contract():
    ids, _, table = small_table()
    assert list(table.columns) == schema.COLUMNS
    assert schema.check_field_traits(table, ids) == []
    assert table["irrigation"].tolist() == ["irrigated", "dryland", "dryland"]
    assert table["weg"].tolist() == ["6", None, "4L"]
    assert np.isnan(table["et_gs_mm"].iloc[2]) and table["et_gs_mm"].iloc[0] == 720.0
    assert "no water-use data" in table["irrigation_basis"].iloc[2]


def test_table_survives_parquet(tmp_path):
    ids, _, table = small_table()
    path = tmp_path / "traits.parquet"
    table.to_parquet(path, index=False)
    assert schema.load_field_traits(path)["weg"].isna().tolist() == [False, True, False]
    assert schema.main([str(path)]) == 0
    assert schema.main([str(tmp_path / "missing.parquet")]) == 1
    with pytest.raises(FileNotFoundError):
        schema.load_field_traits(tmp_path / "missing.parquet")


@pytest.mark.parametrize(
    "change, word",
    [
        (lambda t: t.drop(columns=["wei"]), "missing column"),
        (lambda t: t.assign(irrigation="uncertain"), "irrigation:"),
        (lambda t: t.assign(irrigated_prob=1.5), "irrigated_prob"),
        (lambda t: t.assign(irrigated_prob=0.9), "disagree"),
        (lambda t: t.assign(irrigation_basis=""), "irrigation_basis"),
        (lambda t: t.assign(weg="9"), "weg"),
        (lambda t: t.assign(wei=500.0), "wei"),
        (lambda t: t.assign(et_gs_mm=-1.0), "et_gs_mm"),
        (lambda t: pd.concat([t, t]), "duplicate"),
        (lambda t: t.iloc[:0], "no rows"),
    ],
)
def test_validator_catches(change, word):
    _, _, table = small_table()
    problems = schema.check_field_traits(change(table))
    assert problems and any(word in p for p in problems), problems
    with pytest.raises(schema.FieldTraitsSchemaError):
        schema.validate_field_traits(change(table))


def test_validator_checks_the_field_list():
    ids, _, table = small_table()
    assert any("have no row" in p for p in schema.check_field_traits(table, ids + ["KS-000009"]))
    assert any("not in the fields table" in p for p in schema.check_field_traits(table, ids[:2]))


def test_compare_with_nass_uses_county_years_with_both_practices():
    ids, histories, table = small_table()
    cy = ft.crop_years(ids, histories, [40.0, 40.0, 40.0], table["irrigation"])
    assert len(cy) == 51 and cy["acres"].iloc[0] == pytest.approx(98.84, abs=0.01)
    rows = [
        (2008, "corn", "irrigated", "harvested_acres", 90000.0, "acres", "county", "x"),
        (2008, "corn", "dryland", "harvested_acres", 10000.0, "acres", "county", "x"),
        (2009, "corn", "irrigated", "harvested_acres", 90000.0, "acres", "county", "x"),  # no dryland figure: skipped
        (2010, "corn", "irrigated", "harvested_acres", 700000.0, "acres", "district", "x"),  # not the county
        (2010, "corn", "dryland", "harvested_acres", 60000.0, "acres", "district", "x"),
    ]
    nass_table = pd.DataFrame(rows, columns=["year", "crop", "practice", "stat", "value", "unit", "level", "source_desc"])
    out = ft.compare_with_nass(cy, nass_table)
    assert out.index.tolist() == ["corn"] and out.loc["corn", "years"] == 1
    assert out.loc["corn", "nass_irrigated_share"] == pytest.approx(0.9)
    assert out.loc["corn", "our_irrigated_share"] == pytest.approx(1.0)
    assert ft.compare_with_nass(cy, nass_table.iloc[2:3]).empty


def test_compare_with_causal_and_et_by_crop():
    ids, histories, table = small_table()
    causal = pd.DataFrame({"field_id": ids, "irrigation": ["irrigated", "dryland", "uncertain"]})
    out = ft.compare_with_causal(table, causal)
    assert out["fields_compared"] == 2 and out["agree"] == 1.0
    assert out["causal_uncertain"] == {"n": 1, "we_say_irrigated": 0}
    cy = ft.crop_years(ids, histories, [40.0] * 3, table["irrigation"])
    et_long = pd.DataFrame({"field_id": ids * 2, "year": [2020] * 3 + [2021] * 3, "et_mm": [700.0, 300.0, 350.0, 720.0, 280.0, None]})
    by_crop = ft.et_by_crop(et_long, cy).set_index(["crop", "practice"])
    assert by_crop.loc[("corn", "irrigated"), "median_et_gs_mm"] == 710.0
    assert by_crop.loc[("corn", "irrigated"), "field_years"] == 2
