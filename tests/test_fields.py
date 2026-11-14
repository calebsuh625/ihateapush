"""pipeline.fields (ids, cropland rule, subset, the whole build on a synthetic county),
pipeline.csb (reading a county out of a zipped geodatabase) and the ranged downloader.
No network and no real data files: everything is built in tmp_path."""
from __future__ import annotations

import io
import json
import os
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import MultiPolygon, Polygon, box

from pipeline import cdl, csb, csb_download, fields
from pipeline.common import YEARS
from pipeline.schema import COLUMNS, FIELD_ID_RE, parse_crop_history, validate_fields

CRS = "EPSG:5070"
X0, Y0 = -410_000.0, 1_680_000.0  # inside Finney County, EPSG:5070 metres
PIXEL = 30.0
SIZE = 200  # the synthetic county raster is 200 x 200 pixels = 6 x 6 km
MISSING_YEAR = 2013

CORN, SORGHUM, SOY, WHEAT, ALFALFA, FALLOW, GRASS = 1, 4, 5, 24, 36, 61, 176


def px_box(col0, row0, col1, row1):
    return box(X0 + col0 * PIXEL, Y0 - row1 * PIXEL, X0 + col1 * PIXEL, Y0 - row0 * PIXEL)


def rotation(cycle, offset=0):
    return {year: cycle[(i + offset) % len(cycle)] for i, year in enumerate(YEARS)}


# CSBID -> (pixel rectangle that is painted, crop per year, CSB geometry)
WORLD = {
    "200000000000001": ((10, 10, 50, 50), rotation([WHEAT, SORGHUM, FALLOW]), None),  # 144 ha dryland rotation
    "200000000000002": ((60, 10, 90, 40), rotation([CORN]), None),  # 81 ha continuous corn
    "200000000000003": ((100, 10, 130, 40), rotation([GRASS]), None),  # pasture -> cropland rule
    "200000000000004": ((10, 60, 15, 65), rotation([CORN]), None),  # 2.25 ha -> too small
    "200000000000005": ((60, 60, 98, 90), rotation([ALFALFA]), "multipart"),
    "200000000000006": ((10, 100, 50, 140), rotation([SORGHUM, WHEAT]), "bowtie"),
    "200000000000007": ((170, 100, 200, 130), rotation([CORN]), None),  # mostly outside the county outline
    "200000000000008": (
        (100, 60, 130, 90),
        {y: (GRASS if y < 2020 else [CORN, FALLOW, WHEAT, SORGHUM, CORN][y - 2020]) for y in YEARS},  # broken out in 2020
        None,
    ),
}
COUNTY_COLS = 175  # the county outline covers columns 0..175 of the raster


def csb_geometry(rect, kind):
    c0, r0, c1, r1 = rect
    if kind == "multipart":  # a big part and a 3 x 3 pixel crumb
        return MultiPolygon([px_box(60, 60, 90, 90), px_box(95, 60, 98, 63)])
    if kind == "bowtie":  # self-intersecting ring: invalid until repaired
        x0, x1 = X0 + c0 * PIXEL, X0 + c1 * PIXEL
        y0, y1 = Y0 - r1 * PIXEL, Y0 - r0 * PIXEL
        return MultiPolygon([Polygon([(x0, y0), (x1, y1), (x1, y0), (x0, y1)])])
    return MultiPolygon([px_box(c0, r0, c1, r1)])


def paint(year):
    a = np.full((SIZE, SIZE), GRASS, dtype=np.uint8)
    for rect, crops, _kind in WORLD.values():
        c0, r0, c1, r1 = rect
        a[r0:r1, c0:c1] = crops[year]
    return a


@pytest.fixture
def county(tmp_path, monkeypatch):
    """A synthetic county: CSB polygons, 16 CDL rasters (one year missing), a county outline."""
    raster_dir = tmp_path / "cdl"
    raster_dir.mkdir()
    infos = {}
    for year in YEARS:
        if year == MISSING_YEAR:
            infos[year] = {"year": year, "path": None, "url": None, "route": None, "error": "simulated outage"}
            continue
        # like the real county: 2008/2009 sit on a grid shifted by half a pixel
        west, north = (X0 - 15.0, Y0 + 15.0) if year in (2008, 2009) else (X0, Y0)
        path = raster_dir / f"CDL_{year}_test.tif"
        with rasterio.open(
            path, "w", driver="GTiff", height=SIZE, width=SIZE, count=1, dtype="uint8", crs=CRS,
            transform=from_origin(west, north, PIXEL, PIXEL),
        ) as dst:
            dst.write(paint(year), 1)
        infos[year] = {"year": year, "path": str(path), "url": f"https://example.invalid/{year}.tif",
                       "route": "local-cache", "error": None, **cdl.describe_raster(path)}

    ids = sorted(WORLD)
    csb_frame = gpd.GeoDataFrame(
        {
            "CSBID": ids,
            "CSBYEARS": "1825",
            "CSBACRES": 1.0,
            "CNTY": "Test",
            **{f"CDL{y}": np.array([WORLD[i][1].get(y, CORN) for i in ids], dtype=np.int32) for y in range(2018, 2026)},
        },
        geometry=[csb_geometry(WORLD[i][0], WORLD[i][2]) for i in ids],
        crs=CRS,
    )
    csb_frame = csb_frame.iloc[::-1].reset_index(drop=True)  # deliberately not sorted by CSBID
    csb_frame.attrs["provenance"] = {"release": "NationalCSB_test", "window": "2018-2025", "layer": "test",
                                     "url": "https://example.invalid/csb.zip", "county_features": len(csb_frame)}
    outline = gpd.GeoDataFrame(
        {"GEOID": ["99999"], "NAME": ["Test"]},
        geometry=[box(X0 - 300, Y0 - SIZE * PIXEL - 300, X0 + COUNTY_COLS * PIXEL, Y0 + 300)],
        crs=CRS,
    ).to_crs("EPSG:4326")
    outline.attrs["source"] = "synthetic"

    state = {"csb_error": None, "csb_calls": 0}

    def fake_county_csb(fips=None, windows=None):
        state["csb_calls"] += 1
        if state["csb_error"]:
            raise RuntimeError(state["csb_error"])
        return csb_frame.copy()

    monkeypatch.setattr(csb, "county_csb", fake_county_csb)
    monkeypatch.setattr(cdl, "fetch_all", lambda years=None, fips=None, quiet=False: dict(infos))
    monkeypatch.setattr(fields, "county_boundary", lambda fips=None: outline)
    return state


def with_placeholders(gdf):
    probe = gdf.copy()
    probe["organic_matter_pct"] = np.nan
    probe["slope_pct"] = np.nan
    return probe[COLUMNS]


# --------------------------------------------------------------------------------------
# small pieces
# --------------------------------------------------------------------------------------
def test_field_ids_format():
    ids = fields.field_ids([1, 123, 15926, 999_999])
    assert ids == ["KS-000001", "KS-000123", "KS-015926", "KS-999999"]
    assert all(FIELD_ID_RE.match(i) for i in ids)
    assert fields.field_ids(np.array([7]), state="NE") == ["NE-000007"]
    assert fields.field_ids([]) == []
    for bad in ([0], [1_000_000], [-3]):
        with pytest.raises(ValueError):
            fields.field_ids(bad)


def codes_row(*pairs, n=17):
    """Row of n yearly codes, e.g. codes_row((GRASS, 12), (CORN, 5))."""
    row = [code for code, count in pairs for _ in range(count)]
    assert len(row) == n
    return row


def test_cropland_rule():
    years = list(range(2008, 2025))
    table = np.array(
        [
            codes_row((CORN, 17)),  # 0 always cropped -> keep
            codes_row((WHEAT, 9), (GRASS, 8)),  # 1 9/17 crop but none of the last 5 -> reject (not recent)
            codes_row((CORN, 6), (GRASS, 9), (CORN, 2)),  # 2 8/17 (<50%), 2 recent -> reject (too few)
            codes_row((GRASS, 12), (CORN, 5)),  # 3 5/17 but all 5 recent -> keep (recent clause)
            codes_row((GRASS, 14), (SORGHUM, 3)),  # 4 3 of the last 5 -> keep (recent clause)
            codes_row((0, 17)),  # 5 no data at all -> reject (no data)
            codes_row((FALLOW, 17)),  # 6 fallow/idle counts as cropland -> keep
            codes_row((GRASS, 17)),  # 7 never cropped -> reject (not recent)
            codes_row((0, 7), (GRASS, 5), (WHEAT, 5)),  # 8 only 10 years of data, 5 crop = 50% -> keep
            codes_row((CORN, 9), (GRASS, 7), (CORN, 1)),  # 9 10/17 crop, 1 recent -> keep (share clause)
            codes_row((GRASS, 15), (CORN, 2)),  # 10 2/17, 2 recent -> reject (too few)
            codes_row((121, 16), (CORN, 1)),  # 11 developed -> reject (too few)
        ],
        dtype=np.uint8,
    )

    keep, detail = fields.cropland_mask(table, years)

    assert keep.tolist() == [True, False, False, True, True, False, True, False, True, True, False, False]
    rejected = detail["rejected"]
    reasons = {name: np.flatnonzero(mask).tolist() for name, mask in rejected.items()}
    assert reasons["no CDL data in any year"] == [5]
    assert reasons["no crop year in the 5 most recent years"] == [1, 7]
    assert [v for k, v in reasons.items() if k.startswith("crop in <")] == [[2, 10, 11]]
    # every rejected row has exactly one reason, kept rows have none
    n_reasons = np.sum([mask for mask in rejected.values()], axis=0)
    assert n_reasons.tolist() == [0 if k else 1 for k in keep]
    assert np.flatnonzero(detail["kept_by_recent_clause"]).tolist() == [3, 4]
    assert detail["recent_years"] == [2020, 2021, 2022, 2023, 2024]

    with pytest.raises(ValueError):
        fields.cropland_mask(table[:, :5], years)


def test_cropland_rule_with_fewer_years():
    # "recent" means the last 5 columns that exist, e.g. when 2024 could not be downloaded
    years = [2019, 2020, 2021, 2022, 2023]
    keep, detail = fields.cropland_mask(np.array([[GRASS, GRASS, CORN, CORN, CORN], [GRASS] * 5], dtype=np.uint8), years)
    assert keep.tolist() == [True, False]
    assert detail["recent_years"] == years


def test_spatial_subset_is_deterministic_and_even():
    rng = np.random.default_rng(3)
    xs, ys = np.meshgrid(np.arange(40) * 1000.0, np.arange(40) * 1000.0)
    order = rng.permutation(1600)  # row order must not matter for evenness
    geoms = gpd.GeoSeries([box(x, y, x + 400, y + 400) for x, y in zip(xs.ravel()[order], ys.ravel()[order])], crs=CRS)

    picks = fields.spatial_subset(geoms, 400)

    assert len(picks) == 400 and len(set(picks.tolist())) == 400
    assert picks.tolist() == sorted(picks.tolist())
    assert np.array_equal(picks, fields.spatial_subset(geoms, 400))  # deterministic
    cx = geoms.centroid.x.to_numpy()[picks]
    cy = geoms.centroid.y.to_numpy()[picks]
    quadrants = [
        int((((cx < 20_000) == west) & ((cy < 20_000) == south)).sum()) for west in (True, False) for south in (True, False)
    ]
    assert all(85 <= q <= 115 for q in quadrants), quadrants  # 100 each if perfectly even
    # finer: every 10 x 10 km block (100 cells) gets close to its share of 25
    blocks = np.histogram2d(cx, cy, bins=[np.arange(0, 41_000, 10_000)] * 2)[0]
    assert blocks.min() >= 15 and blocks.max() <= 35, blocks

    assert fields.spatial_subset(geoms, None).tolist() == list(range(1600))
    assert fields.spatial_subset(geoms, 5000).tolist() == list(range(1600))
    assert len(fields.spatial_subset(geoms, 1)) == 1
    with pytest.raises(ValueError):
        fields.spatial_subset(geoms, 0)


def test_crop_histories_omit_years_without_data():
    codes = np.array([[CORN, 0, WHEAT], [236, GRASS, FALLOW]], dtype=np.uint8)
    out = fields.crop_histories(codes, [2008, 2009, 2010])
    assert [json.loads(h) for h in out] == [
        {"2008": "corn", "2010": "winter wheat"},
        {"2008": "double crop: winter wheat/sorghum", "2009": "grass/pasture", "2010": "fallow"},
    ]
    for h in out:
        parse_crop_history(h)
    assert fields.compact_history(out[0], [2008, 2009, 2010]) == "C.W"
    assert fields.compact_history(out[1], [2008, 2009, 2010]) == "dgF"


def test_agreement_stats():
    codes = np.array([[CORN, WHEAT], [CORN, 0], [SORGHUM, FALLOW], [GRASS, GRASS]], dtype=np.uint8)
    attrs = {2019: np.array([CORN, CORN, CORN, GRASS]), 2020: np.array([WHEAT, WHEAT, FALLOW, 0]), 2025: np.array([1, 1, 1, 1])}
    stats = fields.agreement_stats(codes, [2019, 2020], attrs)
    assert stats["per_year"]["2019"] == {"compared": 4, "agree": 3, "rate": 0.75}
    assert stats["per_year"]["2020"] == {"compared": 2, "agree": 2, "rate": 1.0}  # no-data rows are not compared
    assert "2025" not in stats["per_year"]
    assert stats["overall"] == {"compared": 6, "agree": 5, "rate": round(5 / 6, 4)}
    sub = fields.agreement_stats(codes, [2019, 2020], attrs, np.array([True, False, True, False]))
    assert sub["overall"] == {"compared": 4, "agree": 3, "rate": 0.75}


def test_to_lonlat_polygons_repairs_or_flags():
    good = px_box(0, 0, 10, 10)
    series = gpd.GeoSeries([good, px_box(20, 0, 30, 10)], index=[5, 9], crs=CRS)
    lonlat, usable = fields.to_lonlat_polygons(series)
    assert usable.tolist() == [True, True] and lonlat.crs.to_epsg() == 4326
    assert lonlat.index.tolist() == [0, 1]
    assert -101.5 < lonlat.iloc[0].centroid.x < -100 and 37 < lonlat.iloc[0].centroid.y < 39  # Finney County

    # geometries that are not single valid Polygons after reprojection (simulated with lon/lat input)
    bowtie = Polygon([(-100.9, 37.9), (-100.8, 38.0), (-100.8, 37.9), (-100.9, 38.0)])
    line_like = Polygon([(-100.9, 37.9), (-100.8, 37.9), (-100.85, 37.9)])  # zero area: nothing polygonal left
    multi = MultiPolygon([box(-100.9, 37.9, -100.8, 38.0), box(-100.7, 37.9, -100.69, 37.91)])
    messy = gpd.GeoSeries([bowtie, line_like, multi, box(-100.6, 37.9, -100.5, 38.0)], crs="EPSG:4326")
    lonlat, usable = fields.to_lonlat_polygons(messy)
    assert usable.tolist() == [True, False, True, True]
    kept = lonlat[usable]
    assert (kept.geom_type == "Polygon").all() and kept.is_valid.all()
    assert kept.iloc[1].equals(box(-100.9, 37.9, -100.8, 38.0))  # largest part of the multipolygon


# --------------------------------------------------------------------------------------
# the whole build on the synthetic county
# --------------------------------------------------------------------------------------
def test_build_from_csb(county):
    out = fields.build_fields(source="csb", min_area_ha=5.0)
    prov = out.attrs["provenance"]

    # the contract
    assert list(out.columns) == ["field_id", "geometry", "crop_history", "area_ha"]
    assert isinstance(out.index, pd.RangeIndex) and out.index.tolist() == list(range(len(out)))
    assert out.crs.to_epsg() == 4326
    assert (out.geom_type == "Polygon").all() and out.is_valid.all()
    validate_fields(with_placeholders(out), "synthetic build")
    json.dumps(prov)  # provenance must be JSON-serialisable

    # which polygons survive, and why the others do not
    assert out.attrs["source_ids"] == ["200000000000001", "200000000000002", "200000000000005", "200000000000006", "200000000000008"]
    # ids are the rank among ALL county polygons sorted by CSBID
    assert out["field_id"].tolist() == ["KS-000001", "KS-000002", "KS-000005", "KS-000006", "KS-000008"]
    steps = {f["step"].split(" (")[0].split(":")[0]: f for f in prov["filters"]}
    assert steps["geometry repair"]["dropped"] == 0
    assert "1 invalid" in steps["geometry repair"]["note"] and "1 multi-part" in steps["geometry repair"]["note"]
    assert steps["county"]["dropped"] == 1  # ...007 is mostly outside the outline
    assert steps["area >= 5 ha"]["dropped"] == 1  # ...004
    assert steps["cropland rule"]["dropped"] == 1  # ...003 is pasture
    assert prov["cropland_rule"]["kept_only_by_recent_clause"] == 1  # ...008
    assert prov["source"] == "csb" and prov["requested_source"] == "csb" and prov["fallback_reason"] is None
    assert prov["csb"]["release"] == "NationalCSB_test"

    # areas: equal-area hectares of the cleaned polygon
    area = dict(zip(out.attrs["source_ids"], out["area_ha"]))
    assert area["200000000000001"] == pytest.approx(144.0, abs=0.01)
    assert area["200000000000002"] == pytest.approx(81.0, abs=0.01)
    assert area["200000000000005"] == pytest.approx(81.0, abs=0.01)  # largest part only
    assert area["200000000000006"] == pytest.approx(36.0, abs=0.01)  # one triangle of the repaired bowtie
    assert out.to_crs(CRS).area.div(1e4).to_numpy() == pytest.approx(out["area_ha"].to_numpy(), rel=1e-4)

    # histories: every available year, the missing one omitted, names from the code table
    hist = {sid: json.loads(h) for sid, h in zip(out.attrs["source_ids"], out["crop_history"])}
    expected_years = [str(y) for y in YEARS if y != MISSING_YEAR]
    for h in hist.values():
        assert list(h) == expected_years
    assert set(hist["200000000000002"].values()) == {"corn"}
    assert set(hist["200000000000005"].values()) == {"alfalfa"}
    assert [hist["200000000000001"][str(y)] for y in (2008, 2009, 2010, 2011)] == ["winter wheat", "sorghum", "fallow", "winter wheat"]
    assert hist["200000000000008"]["2019"] == "grass/pasture" and hist["200000000000008"]["2020"] == "corn"
    assert prov["cdl"]["years_missing"] == [MISSING_YEAR]
    assert prov["cdl"]["per_year"][str(MISSING_YEAR)]["error"] == "simulated outage"
    assert len(prov["cdl"]["grids"]) == 2  # the shifted 2008/2009 grid and the rest
    assert prov["counts"]["fields"] == 5 and prov["counts"]["fields_with_all_years"] == 0

    # cross-check against the CSB attributes: perfect in this synthetic world
    agreement = prov["agreement_with_csb_attributes"]
    assert sorted(agreement["final_fields"]["per_year"]) == [str(y) for y in range(2018, 2025)]
    assert agreement["final_fields"]["overall"]["rate"] == 1.0
    assert agreement["all_county_polygons"]["overall"]["compared"] == 7 * 7  # 7 polygons in the county x 7 years


def test_build_is_deterministic_and_ids_are_stable(county):
    first = fields.build_fields(source="csb")
    second = fields.build_fields(source="csb")
    assert first["field_id"].tolist() == second["field_id"].tolist()
    assert first["crop_history"].tolist() == second["crop_history"].tolist()
    assert first.geometry.equals(second.geometry)
    assert first.attrs["source_ids"] == second.attrs["source_ids"]

    by_source = dict(zip(first.attrs["source_ids"], first["field_id"]))
    # a bigger minimum size and a cap change which fields are present, never their ids
    bigger = fields.build_fields(source="csb", min_area_ha=100.0)
    assert bigger.attrs["source_ids"] == ["200000000000001"]
    assert bigger["field_id"].tolist() == [by_source["200000000000001"]]
    capped = fields.build_fields(source="csb", max_fields=3)
    assert len(capped) == 3 and capped.attrs["provenance"]["counts"]["fields_before_max_fields"] == 5
    assert capped.index.tolist() == [0, 1, 2]
    for sid, fid in zip(capped.attrs["source_ids"], capped["field_id"]):
        assert by_source[sid] == fid
    assert capped["field_id"].tolist() == fields.build_fields(source="csb", max_fields=3)["field_id"].tolist()
    validate_fields(with_placeholders(capped))
    assert len(fields.build_fields(source="csb", max_fields=50)) == 5  # a cap above the count is a no-op


def test_auto_falls_back_to_segmentation(county, capsys):
    county["csb_error"] = "download impossible (simulated)"

    out = fields.build_fields(source="auto")
    prov = out.attrs["provenance"]

    assert prov["source"] == "cdl-segment" and prov["requested_source"] == "auto"
    assert "download impossible (simulated)" in prov["fallback_reason"]
    assert "falling back" in capsys.readouterr().out
    assert prov["agreement_with_csb_attributes"] is None and prov["csb"] is None
    assert prov["segmentation"]["window_years"] == list(range(2017, 2025))
    assert out.attrs["source_ids"] == [None] * len(out)
    validate_fields(with_placeholders(out), "synthetic segmentation build")
    assert out["field_id"].tolist() == [f"KS-{i:06d}" for i in range(1, len(out) + 1)]
    # The painted crop rectangles come back as fields: 81 ha corn, 81 ha broken out in 2020 (cropped 5 of the
    # 8 window years), 102.6 ha alfalfa, and the two 144 ha rotations. Not the pasture, not the 2 ha patch,
    # and not the corn block that lies mostly outside the county outline.
    assert sorted(out["area_ha"].tolist()) == pytest.approx([81.0, 81.0, 102.6, 144.0, 144.0], abs=2.0)
    histories = [json.loads(h) for h in out["crop_history"]]
    assert all(str(MISSING_YEAR) not in h and len(h) == 16 for h in histories)
    assert sum(set(h.values()) == {"corn"} for h in histories) == 1
    steps = [f["step"] for f in prov["filters"]]
    assert any(step.startswith("county") for step in steps)
    assert next(f for f in prov["filters"] if f["step"].startswith("county"))["dropped"] == 1
    assert not any(set(h.values()) == {"grass/pasture"} for h in histories)
    # deterministic
    again = fields.build_fields(source="auto")
    assert again["field_id"].tolist() == out["field_id"].tolist() and again.geometry.equals(out.geometry)


def test_forced_sources(county):
    seg = fields.build_fields(source="cdl-segment")
    assert seg.attrs["provenance"]["source"] == "cdl-segment" and seg.attrs["provenance"]["fallback_reason"] is None
    assert county["csb_calls"] == 0  # CSB is not even tried

    county["csb_error"] = "layer unreadable (simulated)"
    with pytest.raises(RuntimeError, match="layer unreadable"):
        fields.build_fields(source="csb")
    with pytest.raises(ValueError, match="source must be"):
        fields.build_fields(source="osm")
    with pytest.raises(ValueError):
        fields.build_fields(min_area_ha=0)


def test_build_survives_a_missing_county_outline(county, monkeypatch, capsys):
    def no_outline(fips=None):
        raise RuntimeError("census.gov unreachable (simulated)")

    monkeypatch.setattr(fields, "county_boundary", no_outline)
    for source in ("csb", "cdl-segment"):
        out = fields.build_fields(source=source)
        prov = out.attrs["provenance"]
        validate_fields(with_placeholders(out))
        assert prov["county_boundary"] == {"url": None, "area_km2": None}
        assert prov["area"]["share_of_county"] is None
        guard = next(f for f in prov["filters"] if f["step"].startswith("county"))
        assert "guard skipped" in guard["step"] and guard["dropped"] == 0
        assert fields.describe(out, samples=2)  # the report copes with the missing share
    assert "county outline unavailable" in capsys.readouterr().out
    # without the guard the polygon that is mostly outside the outline stays in
    assert "200000000000007" in fields.build_fields(source="csb").attrs["source_ids"]


def test_no_rasters_at_all_is_an_error(county, monkeypatch):
    monkeypatch.setattr(cdl, "fetch_all", lambda years=None, fips=None, quiet=False: {
        y: {"year": y, "path": None, "url": None, "route": None, "error": "down"} for y in YEARS})
    with pytest.raises(RuntimeError, match="no CDL raster"):
        fields.build_fields(source="auto")


def test_cli_writes_an_inspection_file(county, tmp_path, capsys):
    out_path = tmp_path / "out" / "fields.parquet"
    assert fields.main(["--source", "csb", "--out", str(out_path), "--samples", "3"]) == 0
    text = capsys.readouterr().out
    assert "schema check" in text and "OK" in text and "crop frequency" in text
    written = gpd.read_parquet(out_path)
    assert list(written.columns) == ["field_id", "geometry", "crop_history", "area_ha", "source_id"]
    assert len(written) == 5 and written.crs.to_epsg() == 4326
    sidecar = json.loads((tmp_path / "out" / "fields.parquet.provenance.json").read_text())
    assert sidecar["source"] == "csb"


# --------------------------------------------------------------------------------------
# pipeline.csb: county subset out of a zipped national geodatabase
# --------------------------------------------------------------------------------------
@pytest.fixture
def national_zip(tmp_path, monkeypatch):
    """A tiny 'national' CSB geodatabase, zipped like the NASS download, in a private CSB_DIR."""
    csb_dir = tmp_path / "csb"
    csb_dir.mkdir()
    gdb = tmp_path / "build" / "CSB1825.gdb"
    gdb.parent.mkdir()
    national = gpd.GeoDataFrame(
        {
            "CSBID": ["201825000000003", "201825000000001", "201825000000002", "081825000000009"],
            "CSBYEARS": "1825",
            "CSBACRES": [200.0, 100.0, 50.0, 10.0],
            "CDL2018": np.array([1, 24, 4, 36], dtype=np.int32),
            "CDL2019": np.array([24, 4, 61, 36], dtype=np.int32),
            "STATEFIPS": ["20", "20", "20", "08"],
            "CNTYFIPS": ["055", "055", "057", "055"],  # same county code in another state must not match
            "CNTY": ["Finney", "Finney", "Ford", "Other"],
        },
        geometry=[
            MultiPolygon([px_box(0, 0, 30, 30)]),
            MultiPolygon([px_box(40, 0, 60, 20)]),
            MultiPolygon([px_box(70, 0, 90, 20)]),
            MultiPolygon([px_box(100, 0, 110, 10)]),
        ],
        crs=CRS,
    )
    pyogrio.write_dataframe(national, gdb, driver="OpenFileGDB", layer="national1825")
    zip_file = csb_dir / "NationalCSB_2018-2025_rev23.zip"
    with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(gdb)):
            zf.write(gdb / name, f"CSB1825.gdb/{name}")
        zf.writestr("NationalCSB_2018-2025_rev23_metadata.htm", "<html></html>")

    outline = gpd.GeoDataFrame({"GEOID": ["20055"], "NAME": ["Finney"]}, geometry=[px_box(-10, -10, 200, 200)], crs=CRS).to_crs("EPSG:4326")
    calls = {"download": 0}

    def fake_ranged_download(url, dest, **kwargs):
        calls["download"] += 1
        if not dest.exists():
            raise RuntimeError(f"no network in tests: {url}")
        return dest

    monkeypatch.setattr(csb, "CSB_DIR", csb_dir)
    monkeypatch.setattr(csb, "county_boundary", lambda fips=None: outline)
    monkeypatch.setattr(csb, "ranged_download", fake_ranged_download)
    return calls


def test_county_csb_reads_the_zip_and_caches(national_zip, tmp_path):
    got = csb.county_csb("20055")

    assert got["CSBID"].tolist() == ["201825000000001", "201825000000003"]  # the county only, sorted by CSBID
    assert got.crs.to_epsg() == 5070
    assert list(got.columns) == ["CSBID", "CSBYEARS", "CSBACRES", "CNTY", "CDL2018", "CDL2019", "geometry"]
    assert csb.cdl_columns(got.columns) == {2018: "CDL2018", 2019: "CDL2019"}
    assert got["CDL2018"].astype(int).tolist() == [24, 1]
    prov = got.attrs["provenance"]
    assert prov["release"] == "NationalCSB_2018-2025_rev23" and prov["window"] == "2018-2025"
    assert prov["layer"] == "national1825" and prov["geodatabase"] == "CSB1825.gdb" and prov["read_via"] == "/vsizip/"
    assert prov["national_features"] == 4 and prov["county_features"] == 2 and prov["from_cache"] is False
    assert {"CSBID", "STATEFIPS", "CNTYFIPS", "CDL2018"} <= set(prov["attributes"])
    assert national_zip["download"] == 1

    parquet, sidecar = csb.cache_paths("20055")
    assert parquet.exists() and json.loads(sidecar.read_text())["county_features"] == 2

    again = csb.county_csb("20055")  # served from the parquet: the zip is not touched
    assert national_zip["download"] == 1
    assert again.attrs["provenance"]["from_cache"] is True
    assert again["CSBID"].tolist() == got["CSBID"].tolist()
    assert again.geometry.equals(got.geometry)


def test_county_csb_unpacks_when_the_zip_cannot_be_read_in_place(national_zip, monkeypatch, capsys):
    real = csb.read_county_from_gdb
    seen = []

    def no_vsizip(gdb, fips="20055"):
        seen.append(gdb)
        if gdb.startswith("/vsizip/"):
            raise RuntimeError("vsizip not supported (simulated)")
        return real(gdb, fips)

    monkeypatch.setattr(csb, "read_county_from_gdb", no_vsizip)
    got = csb.county_csb("20055")

    assert got["CSBID"].tolist() == ["201825000000001", "201825000000003"]
    assert got.attrs["provenance"]["read_via"] == "unpacked copy"
    assert seen[0].startswith("/vsizip/") and seen[1].endswith("NationalCSB_2018-2025_rev23/CSB1825.gdb")
    unpacked = csb.CSB_DIR / "NationalCSB_2018-2025_rev23" / "CSB1825.gdb"
    assert unpacked.is_dir() and not (csb.CSB_DIR / "NationalCSB_2018-2025_rev23.extracting").exists()
    assert "unpacking" in capsys.readouterr().out


def test_county_csb_fails_loudly(national_zip):
    with pytest.raises(csb.CountyNotInCSB, match="no features"):
        csb.county_csb("20999")  # a county with no polygons
    assert national_zip["download"] == 1  # ... and no other release is downloaded to look again
    assert not (csb.CSB_DIR / "NationalCSB_2018-2025_rev23").exists()  # nor is the zip unpacked
    assert not csb.cache_paths("20999")[0].exists()
    with pytest.raises(RuntimeError, match="no CSB release could be used.*no network in tests"):
        csb.county_csb("20055", windows=["2010-2017"])  # a release that is not on disk and cannot be downloaded


# --------------------------------------------------------------------------------------
# pipeline.csb_download: ranged, resumable download (the HTTP layer is replaced)
# --------------------------------------------------------------------------------------
@pytest.fixture
def range_server(monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("data.bin", np.random.default_rng(1).bytes(3_500_000))
    blob = payload.getvalue()
    state = {"blob": blob, "requests": [], "fail_from": None, "flaky": set()}

    def fake_fetch(session, url, start, end, timeout):
        state["requests"].append((start, end))
        if state["fail_from"] is not None and start >= state["fail_from"]:
            raise IOError("connection reset (simulated)")
        if start in state["flaky"]:
            state["flaky"].discard(start)
            raise IOError("one-off glitch (simulated)")
        return blob[start : end + 1]

    monkeypatch.setattr(csb_download, "remote_size", lambda url, **kw: (len(blob), True))
    monkeypatch.setattr(csb_download, "_fetch_range", fake_fetch)
    monkeypatch.setattr(csb_download.time, "sleep", lambda s: None)
    return state


def test_ranged_download_assembles_retries_and_caches(range_server, tmp_path):
    dest = tmp_path / "big.zip"
    range_server["flaky"] = {1 << 20}  # the second range fails once

    out = csb_download.ranged_download("https://example.invalid/big.zip", dest, workers=3, chunk_mb=1, quiet=True)

    assert out == dest and dest.read_bytes() == range_server["blob"]
    assert not dest.with_name("big.zip.part").exists() and not dest.with_name("big.zip.part.json").exists()
    assert len(range_server["requests"]) == 4 + 1  # 4 ranges of <= 1 MiB, one retried
    assert max(end - start + 1 for start, end in range_server["requests"]) == 1 << 20

    range_server["requests"].clear()
    csb_download.ranged_download("https://example.invalid/big.zip", dest, chunk_mb=1, quiet=True)
    assert range_server["requests"] == []  # cached: nothing is fetched


def test_ranged_download_resumes(range_server, tmp_path):
    dest = tmp_path / "big.zip"
    range_server["fail_from"] = 2 << 20  # ranges 2 and 3 never arrive

    with pytest.raises(RuntimeError, match="failed after"):
        csb_download.ranged_download("https://example.invalid/big.zip", dest, workers=1, chunk_mb=1, retries=2, quiet=True)
    assert not dest.exists()
    state = json.loads(dest.with_name("big.zip.part.json").read_text())
    assert state["done"] == [0, 1] and state["total"] == len(range_server["blob"])

    range_server["fail_from"] = None
    range_server["requests"].clear()
    csb_download.ranged_download("https://example.invalid/big.zip", dest, workers=2, chunk_mb=1, quiet=True)
    assert sorted(start >> 20 for start, _ in range_server["requests"]) == [2, 3]  # only what was missing
    assert dest.read_bytes() == range_server["blob"]


def test_ranged_download_rejects_a_broken_zip(range_server, tmp_path, monkeypatch):
    blob = b"not a zip at all " * 100_000
    monkeypatch.setattr(csb_download, "remote_size", lambda url, **kw: (len(blob), True))
    monkeypatch.setattr(csb_download, "_fetch_range", lambda session, url, start, end, timeout: blob[start : end + 1])

    with pytest.raises(RuntimeError, match="not a readable zip"):
        csb_download.ranged_download("https://example.invalid/bad.zip", tmp_path / "bad.zip", chunk_mb=1, quiet=True)

    assert list(tmp_path.iterdir()) == []  # neither the file nor a resumable .part is left behind
