"""CDL code table, per-field majority (zonal) logic, download fallbacks and the CDL
segmentation - all on tiny synthetic rasters, no network, no real data files."""
from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pytest
import rasterio
import shapely
from rasterio.transform import from_origin
from shapely.geometry import box

from pipeline import cdl, cdl_codes, cdl_segment
from pipeline.schema import CORE_CROPS, parse_crop_history

CRS = "EPSG:5070"
# a spot inside Finney County in EPSG:5070 metres, so reprojection tests are realistic
X0, Y0 = -410_000.0, 1_680_000.0

CORN, SORGHUM, SOY, WHEAT, ALFALFA, FALLOW, GRASS, DEVELOPED = 1, 4, 5, 24, 36, 61, 176, 121


def write_raster(path, data, *, pixel=30.0, west=X0, north=Y0, nodata=None, crs=CRS):
    data = np.asarray(data, dtype=np.uint8)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype="uint8",
        crs=crs,
        transform=from_origin(west, north, pixel, pixel),
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path


def px_box(col0, row0, col1, row1, *, pixel=30.0, west=X0, north=Y0):
    """Polygon covering pixels [col0, col1) x [row0, row1) of a grid."""
    return box(west + col0 * pixel, north - row1 * pixel, west + col1 * pixel, north - row0 * pixel)


def series(*geoms, crs=CRS):
    return gpd.GeoSeries(list(geoms), crs=crs)


# --------------------------------------------------------------------------------------
# cdl_codes
# --------------------------------------------------------------------------------------
def test_core_names_are_fixed():
    assert cdl_codes.crop_name(1) == "corn"
    assert cdl_codes.crop_name(5) == "soybeans"
    assert cdl_codes.crop_name(24) == "winter wheat"
    assert cdl_codes.crop_name(4) == "sorghum"
    assert cdl_codes.crop_name(36) == "alfalfa"
    assert cdl_codes.crop_name(61) == "fallow"
    assert set(cdl_codes.CORE_CODES.values()) == set(CORE_CROPS)
    # no other code may produce a core name
    for code, name in cdl_codes.CDL_NAMES.items():
        if name in CORE_CROPS:
            assert code in cdl_codes.CORE_CODES, (code, name)


def test_normalised_names():
    assert cdl_codes.crop_name(236) == "double crop: winter wheat/sorghum"
    assert cdl_codes.crop_name(26) == "double crop: winter wheat/soybeans"
    assert cdl_codes.crop_name(225) == "double crop: winter wheat/corn"
    assert cdl_codes.crop_name(230) == "double crop: lettuce/durum wheat"
    assert cdl_codes.crop_name(228) == "double crop: triticale/corn"
    assert cdl_codes.crop_name(176) == "grass/pasture"
    assert cdl_codes.crop_name(37) == "other hay"
    assert cdl_codes.crop_name(205) == "triticale"
    assert cdl_codes.crop_name(2) == "cotton"
    assert cdl_codes.crop_name(6) == "sunflower"
    assert cdl_codes.crop_name(28) == "oats"
    assert {cdl_codes.crop_name(c) for c in (82, 121, 122, 123, 124)} == {"developed"}
    assert {cdl_codes.crop_name(c) for c in (83, 111)} == {"water"}
    assert {cdl_codes.crop_name(c) for c in (62, 171, 181)} == {"grass/pasture"}  # legacy codes


def test_every_code_gives_a_valid_history_value():
    assert len(cdl_codes.NAME_LUT) == 256
    for code in range(256):
        name = cdl_codes.crop_name(code)
        assert name and name == name.strip() == name.lower(), (code, name)
        parse_crop_history(json.dumps({"2020": name}))  # the schema accepts it
    assert cdl_codes.crop_name(7) == "cdl class 7"  # not in the legend
    assert cdl_codes.crop_name(999) == "cdl class 999"
    assert cdl_codes.crop_name(np.uint8(24)) == "winter wheat"


def test_legend_is_complete():
    legend = cdl_codes.CDL_CLASSES
    assert len(legend) == 134  # NASS metadata table: 0 + 133 classes
    assert min(legend) == 0 and max(legend) == 254
    # every block of the NASS legend is there
    for code in list(range(1, 7)) + list(range(10, 15)) + list(range(21, 40)) + list(range(41, 62)):
        assert code in legend, code
    for code in (63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 74, 75, 76, 77, 81, 82, 83, 87, 88, 92):
        assert code in legend, code
    for code in (111, 112, 121, 122, 123, 124, 131, 141, 142, 143, 152, 176, 190, 195):
        assert code in legend, code
    for code in list(range(204, 251)) + [254]:
        assert code in legend, code
    for code in (7, 8, 9, 15, 20, 40, 62, 73, 78, 100, 171, 181, 200, 251, 252, 253, 255):
        assert code not in legend, code
    assert legend[61] == "Fallow/Idle Cropland"
    assert legend[236] == "Dbl Crop WinWht/Sorghum"
    assert legend[176] == "Grassland/Pasture"


def test_is_crop():
    for code in (1, 2, 4, 5, 6, 21, 24, 28, 36, 37, 42, 43, 53, 59, 61, 66, 77, 204, 205, 225, 236, 254):
        assert cdl_codes.is_crop(code), code
    for code in (0, 63, 64, 65, 81, 82, 83, 87, 88, 92, 111, 112, 121, 122, 123, 124, 131, 141, 142, 143, 152, 176, 190, 195):
        assert not cdl_codes.is_crop(code), code
    for code in (62, 171, 181, 7, 100, 200, 255, 256, 1000, -1):  # legacy grass, undefined, out of range
        assert not cdl_codes.is_crop(code), code
    assert cdl_codes.is_crop(np.uint8(61))
    assert cdl_codes.IS_CROP_LUT.shape == (256,) and cdl_codes.IS_CROP_LUT.dtype == bool
    assert int(cdl_codes.IS_CROP_LUT.sum()) == len(cdl_codes.CROP_CODES) == 110
    assert cdl_codes.is_crop_name("fallow") and cdl_codes.is_crop_name("double crop: winter wheat/sorghum")
    assert not cdl_codes.is_crop_name("grass/pasture") and not cdl_codes.is_crop_name("developed")
    # no name is both a crop name and a non-crop name
    non_crop_names = {n for c, n in cdl_codes.CDL_NAMES.items() if c not in cdl_codes.CROP_CODES}
    assert not (non_crop_names & cdl_codes.CROP_NAMES)


# --------------------------------------------------------------------------------------
# majority / zonal logic
# --------------------------------------------------------------------------------------
def test_majority_per_field_and_year(tmp_path):
    a = np.full((10, 10), GRASS)
    a[0:5, 0:5] = CORN  # field 0 -> corn
    a[0:5, 5:10] = WHEAT  # field 1 -> 15 wheat, 10 sorghum
    a[0:2, 5:10] = SORGHUM
    b = np.full((10, 10), FALLOW)
    b[0:5, 0:5] = SOY
    rasters = {2020: write_raster(tmp_path / "a.tif", a), 2021: write_raster(tmp_path / "b.tif", b)}
    geoms = series(px_box(0, 0, 5, 5), px_box(5, 0, 10, 5), px_box(0, 5, 10, 10))

    z = cdl.zonal_majority(geoms, rasters, quiet=True)

    assert z.years == [2020, 2021]
    assert z.codes.tolist() == [[CORN, SOY], [WHEAT, FALLOW], [GRASS, FALLOW]]
    assert z.pixels.tolist() == [[25, 25], [25, 25], [50, 50]]
    assert z.share[1, 0] == pytest.approx(15 / 25)
    assert z.share[0, 0] == pytest.approx(1.0)
    assert len(z.grids) == 1 and z.grids[0]["years"] == [2020, 2021]  # one shared label image
    assert z.per_year[2020]["fields_with_data"] == 3


def test_background_nodata_and_clouds_do_not_vote(tmp_path):
    a = np.zeros((10, 10))
    a[0:5, 0:5] = 0
    a[0:5, 0:2] = CORN  # field 0: 10 corn, 15 background -> corn
    a[0:5, 5:10] = 255  # field 1: 20 nodata(255), 5 wheat -> wheat
    a[0:1, 5:10] = WHEAT
    a[5:10, 0:5] = 81  # field 2: 20 "Clouds/No Data", 5 sorghum -> sorghum
    a[5:6, 0:5] = SORGHUM
    a[5:10, 5:10] = 0  # field 3: nothing but background -> no data
    rasters = {2012: write_raster(tmp_path / "a.tif", a, nodata=255)}
    geoms = series(px_box(0, 0, 5, 5), px_box(5, 0, 10, 5), px_box(0, 5, 5, 10), px_box(5, 5, 10, 10))

    z = cdl.zonal_majority(geoms, rasters, quiet=True)

    assert z.codes[:, 0].tolist() == [CORN, WHEAT, SORGHUM, 0]
    assert z.pixels[:, 0].tolist() == [10, 5, 5, 0]
    assert z.share[:, 0].tolist() == pytest.approx([1.0, 1.0, 1.0, 0.0])
    assert z.per_year[2012]["fields_without_data"] == 1

    # without a nodata tag, 255 is just an (unknown) class and wins field 1
    rasters = {2012: write_raster(tmp_path / "b.tif", a, nodata=None)}
    assert cdl.zonal_majority(geoms, rasters, quiet=True).codes[1, 0] == 255


def test_ties_go_to_the_lowest_code(tmp_path):
    a = np.zeros((4, 6))
    a[:, 0:3] = WHEAT
    a[:, 3:6] = SORGHUM  # 12 vs 12
    b = np.zeros((4, 6))
    b[0:2, :] = FALLOW
    b[2:4, :] = CORN
    rasters = {2015: write_raster(tmp_path / "a.tif", a), 2016: write_raster(tmp_path / "b.tif", b)}
    geoms = series(px_box(0, 0, 6, 4))

    first = cdl.zonal_majority(geoms, rasters, quiet=True)
    second = cdl.zonal_majority(geoms, rasters, quiet=True)

    assert first.codes.tolist() == [[SORGHUM, CORN]]  # 4 < 24, 1 < 61
    assert first.share.tolist() == [[0.5, 0.5]]
    assert np.array_equal(first.codes, second.codes)


def test_tiny_and_narrow_polygons_fall_back_to_all_touched(tmp_path):
    a = np.full((10, 10), CORN)
    a[:, 5:] = WHEAT
    a[9, 9] = ALFALFA
    rasters = {2020: write_raster(tmp_path / "a.tif", a)}
    big = px_box(0, 0, 5, 10)  # normal field, all corn
    tiny = box(X0 + 9 * 30 + 2, Y0 - 10 * 30 + 2, X0 + 9 * 30 + 8, Y0 - 10 * 30 + 8)  # 6 m square in pixel (9, 9), off-centre
    # 2 m wide strip running along the boundary between rows 2 and 3, columns 6..9: no pixel centre inside
    strip = box(X0 + 6 * 30, Y0 - 3 * 30 - 1, X0 + 9 * 30, Y0 - 3 * 30 + 1)
    # sliver inside the big field, between pixel centres: must not change the big field's result
    sliver = box(X0 + 1 * 30 + 1, Y0 - 5 * 30, X0 + 1 * 30 + 3, Y0 - 2 * 30)

    z = cdl.zonal_majority(series(big, tiny, strip, sliver), rasters, quiet=True)

    assert z.codes[:, 0].tolist() == [CORN, ALFALFA, WHEAT, CORN]
    assert z.pixels[0, 0] == 50  # the big field keeps exactly its own pixel-centre pixels
    assert z.pixels[1, 0] == 1
    assert z.pixels[2, 0] == 6  # 3 columns x the 2 rows the strip touches
    assert z.pixels[3, 0] == 3
    assert z.grids[0]["fields_all_touched_fallback"] == 3
    assert z.grids[0]["fields_outside_raster"] == 0


def test_all_touched_fallback_is_only_for_polygons_without_pixel_centres(tmp_path):
    # a field whose edge cuts through wheat pixels: those pixels must NOT vote (their centres are outside)
    a = np.full((6, 6), WHEAT)
    a[1:4, 1:4] = CORN
    rasters = {2020: write_raster(tmp_path / "a.tif", a)}
    field = box(X0 + 20, Y0 - 4 * 30 - 10, X0 + 4 * 30 + 10, Y0 - 20)  # covers the 3x3 corn block + slivers of wheat

    z = cdl.zonal_majority(series(field), rasters, quiet=True)

    assert z.codes[0, 0] == CORN
    assert z.pixels[0, 0] == 9 and z.share[0, 0] == pytest.approx(1.0)
    assert z.grids[0]["fields_all_touched_fallback"] == 0


def test_mixed_resolutions_and_grids(tmp_path):
    # 2008: 56 m pixels on a shifted origin; 2010 + 2011: 30 m; 2012: 30 m but one column narrower
    field_a = box(X0 + 0, Y0 - 600, X0 + 600, Y0 - 0)  # west block
    field_b = box(X0 + 600, Y0 - 600, X0 + 1200, Y0 - 0)  # east block

    coarse = np.full((13, 24), GRASS)  # 56 m grid starting 30 m west / 40 m north of X0, Y0
    xs = (X0 - 30) + (np.arange(24) + 0.5) * 56
    coarse[:, xs < X0 + 600] = SORGHUM
    coarse[:, xs >= X0 + 600] = FALLOW
    fine = np.full((20, 40), CORN)
    fine[:, 20:] = WHEAT
    fine2 = np.full((20, 40), SOY)
    fine2[:, 20:] = ALFALFA
    narrow = np.full((20, 39), WHEAT)
    narrow[:, 20:] = CORN
    rasters = {
        2008: write_raster(tmp_path / "c.tif", coarse, pixel=56.0, west=X0 - 30, north=Y0 + 40),
        2010: write_raster(tmp_path / "f1.tif", fine),
        2011: write_raster(tmp_path / "f2.tif", fine2),
        2012: write_raster(tmp_path / "n.tif", narrow),
    }

    z = cdl.zonal_majority(series(field_a, field_b), rasters, quiet=True)

    assert z.years == [2008, 2010, 2011, 2012]
    assert z.codes.tolist() == [[SORGHUM, CORN, SOY, WHEAT], [FALLOW, WHEAT, ALFALFA, CORN]]
    assert len(z.grids) == 3
    assert sorted(g["years"] for g in z.grids) == [[2008], [2010, 2011], [2012]]
    assert z.per_year[2008]["pixel_size_m"] == 56.0 and z.per_year[2010]["pixel_size_m"] == 30.0
    # ~ (600 / 56)^2 pixels at 56 m, exactly 400 at 30 m, 380 on the narrower grid for the east block
    assert 100 <= z.pixels[0, 0] <= 135
    assert z.pixels[:, 1].tolist() == [400, 400]
    assert z.pixels[:, 3].tolist() == [400, 380]


def test_polygons_in_another_crs_are_reprojected(tmp_path):
    a = np.full((40, 40), GRASS)
    a[5:25, 5:25] = CORN
    a[5:25, 25:38] = WHEAT
    rasters = {2020: write_raster(tmp_path / "a.tif", a)}
    native = series(px_box(5, 5, 25, 25), px_box(25, 5, 38, 25))
    lonlat = native.to_crs("EPSG:4326")
    assert abs(lonlat.iloc[0].centroid.x) < 180

    z_native = cdl.zonal_majority(native, rasters, quiet=True)
    z_lonlat = cdl.zonal_majority(lonlat, rasters, quiet=True)

    assert z_native.codes[:, 0].tolist() == [CORN, WHEAT]
    assert z_lonlat.codes[:, 0].tolist() == [CORN, WHEAT]
    assert np.abs(z_native.pixels - z_lonlat.pixels).max() <= 2
    with pytest.raises(ValueError, match="CRS"):
        cdl.zonal_majority(gpd.GeoSeries([px_box(0, 0, 5, 5)]), rasters, quiet=True)


def test_field_outside_the_raster_and_row_order(tmp_path):
    a = np.full((10, 10), CORN)
    a[5:, :] = WHEAT
    rasters = {2020: write_raster(tmp_path / "a.tif", a)}
    outside = box(X0 + 5_000, Y0 - 300, X0 + 5_300, Y0)
    geoms = gpd.GeoSeries([px_box(0, 5, 10, 10), outside, px_box(0, 0, 10, 5)], index=[42, 7, 99], crs=CRS)

    z = cdl.zonal_majority(geoms, rasters, quiet=True)

    assert z.codes[:, 0].tolist() == [WHEAT, 0, CORN]  # row order, not index order
    assert z.pixels[:, 0].tolist() == [50, 0, 50]
    assert z.grids[0]["fields_outside_raster"] == 1


def test_many_fields_match_a_slow_reference(tmp_path):
    rng = np.random.default_rng(0)
    data = rng.choice([0, CORN, SORGHUM, WHEAT, FALLOW, GRASS, 81], size=(60, 60), p=[0.1, 0.2, 0.15, 0.2, 0.15, 0.15, 0.05])
    rasters = {2019: write_raster(tmp_path / "a.tif", data)}
    cells = [(c, r) for r in range(0, 60, 6) for c in range(0, 60, 6)]
    geoms = series(*[px_box(c, r, c + 6, r + 6) for c, r in cells])

    z = cdl.zonal_majority(geoms, rasters, quiet=True)

    for i, (c, r) in enumerate(cells):
        block = data[r : r + 6, c : c + 6].ravel()
        block = block[(block != 0) & (block != 81)]
        counts = np.bincount(block, minlength=256)
        assert z.pixels[i, 0] == len(block)
        assert z.codes[i, 0] == (int(counts.argmax()) if len(block) else 0)


def test_class_counts_rejects_a_mismatched_raster():
    labels = cdl.rasterize_labels([px_box(0, 0, 2, 2)], from_origin(X0, Y0, 30, 30), (4, 4))
    with pytest.raises(ValueError, match="differ"):
        cdl.class_counts(labels, np.zeros((5, 4), dtype=np.uint8), 1)


def test_majority_of_an_empty_count_row():
    counts = np.zeros((2, 256), dtype=np.int64)
    counts[1, 24] = 3
    codes, share, pixels = cdl.majority(counts)
    assert codes.tolist() == [0, 24] and share.tolist() == [0.0, 1.0] and pixels.tolist() == [0, 3]


# --------------------------------------------------------------------------------------
# download fallbacks (the network is replaced)
# --------------------------------------------------------------------------------------
@pytest.fixture
def fake_cdl_server(tmp_path, monkeypatch):
    """Replaces pipeline.cdl's two network functions. `state` controls what 'the server' does."""
    monkeypatch.setattr(cdl, "CDL_DIR", tmp_path / "cdl")
    monkeypatch.setattr(cdl.time, "sleep", lambda s: None)
    state = {"cache_url_ok": True, "service_ok": True, "downloads": [], "service_calls": 0}
    good = np.full((8, 8), CORN, dtype=np.uint8)

    def fake_download(url, dest, **kwargs):
        state["downloads"].append(url)
        from_cache_url = "nass_data_cache/byfips" in url and "via-service" not in url
        if from_cache_url and not state["cache_url_ok"]:
            raise RuntimeError("HTTP 404")
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_raster(dest, good)
        return dest

    def fake_service(year, fips, timeout):
        state["service_calls"] += 1
        if not state["service_ok"]:
            raise RuntimeError("service timed out")
        return f"https://example.invalid/via-service/CDL_{year}_{fips}.tif"

    monkeypatch.setattr(cdl, "download", fake_download)
    monkeypatch.setattr(cdl, "_service_return_url", fake_service)
    return state


def test_fetch_prefers_the_service_cache_url(fake_cdl_server):
    info = cdl.fetch_cdl(2020, "20055", quiet=True)
    assert info["route"] == "cropscape-cache-url" and info["error"] is None
    assert info["path"].endswith("CDL_2020_20055.tif")
    assert info["pixel_size_m"] == [30.0, 30.0] and info["width"] == 8
    assert fake_cdl_server["service_calls"] == 0

    again = cdl.fetch_cdl(2020, "20055", quiet=True)  # now cached locally: no download at all
    assert again["route"] == "local-cache"
    assert len(fake_cdl_server["downloads"]) == 1
    # ... and it still knows where the file came from
    assert again["downloaded_via"] == "cropscape-cache-url" and again["url"] == info["url"]
    assert again["downloaded_at"] == info["downloaded_at"] is not None


def test_fetch_falls_back_to_getcdlfile(fake_cdl_server):
    fake_cdl_server["cache_url_ok"] = False
    info = cdl.fetch_cdl(2021, "20055", quiet=True)
    assert info["route"] == "GetCDLFile" and info["error"] is None and info["path"]
    assert fake_cdl_server["service_calls"] == 1
    assert "via-service" in info["url"]


def test_a_year_that_cannot_be_downloaded_is_reported_not_raised(fake_cdl_server, capsys):
    fake_cdl_server["cache_url_ok"] = False
    fake_cdl_server["service_ok"] = False
    info = cdl.fetch_cdl(2022, "20055", quiet=True)
    assert info["path"] is None and info["route"] is None and info["url"] is None
    assert not cdl.cdl_path(2022, "20055").exists()
    assert "service timed out" in info["error"] and "HTTP 404" in info["error"]
    assert fake_cdl_server["service_calls"] == 3  # retried
    assert "COULD NOT BE DOWNLOADED" in capsys.readouterr().out

    fake_cdl_server["cache_url_ok"] = True
    infos = cdl.fetch_all([2021, 2022], "20055", quiet=True)
    fake_cdl_server["cache_url_ok"] = False
    infos[2023] = cdl.fetch_cdl(2023, "20055", quiet=True)
    assert sorted(cdl.available_rasters(infos)) == [2021, 2022]  # 2023 is simply left out


def test_getcdlfile_response_is_parsed(monkeypatch):
    # the service's real answer for year=2023&fips=20055 (captured 2026-11-14)
    xml = (
        '<ns1:GetCDLFileResponse xmlns:ns1="http://cropscape.csiss.gmu.edu/CDLService/"><returnURL>'
        "https://nassgeodata.gmu.edu/webservice/nass_data_cache/byfips/CDL_2023_20055.tif"
        "</returnURL></ns1:GetCDLFileResponse>"
    )
    calls = []

    class FakeResponse:
        def __init__(self, text, status=200):
            self.text, self.status_code = text, status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise cdl.requests.HTTPError(f"{self.status_code} Server Error")

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append((url, params, timeout))
        return FakeResponse(*answers.pop(0))

    monkeypatch.setattr(cdl.requests, "get", fake_get)
    answers = [(xml,), ("<html>busy</html>",), ("", 500)]

    assert cdl._service_return_url(2023, "20055", 99) == cdl.CDL_CACHE_URL.format(year=2023, fips="20055")
    assert calls[0] == (cdl.CDL_SERVICE_URL, {"year": 2023, "fips": "20055"}, 99)
    with pytest.raises(RuntimeError, match="no <returnURL>"):
        cdl._service_return_url(2023, "20055", 99)
    with pytest.raises(cdl.requests.HTTPError):  # the service answers HTTP 500 at times
        cdl._service_return_url(2023, "20055", 99)


def test_a_corrupt_cached_file_is_replaced(fake_cdl_server):
    path = cdl.cdl_path(2019, "20055")
    path.parent.mkdir(parents=True)
    path.write_text("<html>503 Service Unavailable</html>")
    info = cdl.fetch_cdl(2019, "20055", quiet=True)
    assert info["route"] == "cropscape-cache-url" and info["error"] is None
    assert cdl.describe_raster(path)["width"] == 8


# --------------------------------------------------------------------------------------
# segmentation fallback
# --------------------------------------------------------------------------------------
def synthetic_stack(tmp_path, *, noise=0.0, seed=0):
    """8 years, 100 x 100 pixels of grass with three 30 x 30 px fields (81 ha each) and a 4 x 4 px patch."""
    rng = np.random.default_rng(seed)
    sequences = {
        "wsf": [WHEAT, SORGHUM, FALLOW, WHEAT, SORGHUM, FALLOW, WHEAT, SORGHUM],
        "corn": [CORN] * 8,
        "alfalfa": [CORN, CORN, ALFALFA, ALFALFA, ALFALFA, ALFALFA, ALFALFA, WHEAT],
    }
    rasters = {}
    for k in range(8):
        a = np.full((100, 100), GRASS)
        a[5:35, 5:35] = sequences["wsf"][k]
        a[5:35, 35:65] = sequences["corn"][k]  # shares an edge with the first field
        a[50:80, 20:50] = sequences["alfalfa"][k]
        a[90:94, 90:94] = CORN  # 0.36 ha: too small to be a field
        if noise:
            flip = rng.random(a.shape) < noise
            a[flip] = rng.choice([CORN, WHEAT, SORGHUM, GRASS], size=int(flip.sum()))
        width = 99 if k == 3 else 100  # one year on a slightly different grid
        rasters[2017 + k] = write_raster(tmp_path / f"y{2017 + k}.tif", a[:, :width])
    truth = [px_box(5, 5, 35, 35), px_box(35, 5, 65, 35), px_box(20, 50, 50, 80)]
    return rasters, truth


@pytest.mark.parametrize("noise", [0.0, 0.03])
def test_segmentation_recovers_fields(tmp_path, noise):
    rasters, truth = synthetic_stack(tmp_path, noise=noise)

    seg, stats = cdl_segment.segment_fields(rasters, min_area_ha=5.0, quiet=True)

    assert len(seg) == 3
    assert seg.crs.to_epsg() == 5070
    assert (seg.geom_type == "Polygon").all() and seg.is_valid.all()
    assert stats["window_years"] == list(range(2017, 2025))
    assert stats["years_resampled_to_reference_grid"] == 1
    for true_poly in truth:
        iou = [g.intersection(true_poly).area / g.union(true_poly).area for g in seg.geometry]
        assert max(iou) > 0.9, (noise, iou)
    # neighbours share an edge but do not overlap
    geoms = list(seg.geometry)
    for i in range(3):
        for j in range(i + 1, 3):
            assert geoms[i].intersection(geoms[j]).area < 1.0
    # grass and the tiny patch are not fields
    assert sum(g.area for g in geoms) / 1e4 == pytest.approx(3 * 81.0, rel=0.05)


def test_segmentation_respects_min_area_and_uses_recent_years(tmp_path):
    rasters, _ = synthetic_stack(tmp_path)
    with pytest.raises(RuntimeError, match="minimum field size"):  # the fields are 81 ha
        cdl_segment.segment_fields(rasters, min_area_ha=100.0, quiet=True)
    seg, _ = cdl_segment.segment_fields(rasters, min_area_ha=80.0, quiet=True)
    assert len(seg) == 3

    # a 9th, older year must be ignored (window = most recent 8)
    old = np.full((100, 100), CORN)
    rasters[2010] = write_raster(tmp_path / "old.tif", old)
    seg, stats = cdl_segment.segment_fields(rasters, min_area_ha=5.0, quiet=True)
    assert stats["window_years"][0] == 2017 and len(seg) == 3

    with pytest.raises(RuntimeError):
        cdl_segment.segment_fields({}, quiet=True)


def test_clean_polygons_keeps_the_largest_valid_part():
    bowtie = shapely.Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])  # self-intersecting
    multi = shapely.MultiPolygon([box(0, 0, 10, 10), box(20, 0, 22, 2)])
    good = box(0, 0, 3, 3)
    out = cdl_segment.clean_polygons([bowtie, multi, good, None, shapely.Polygon()])
    assert out[3] is None and out[4] is None
    assert all(g.geom_type == "Polygon" and g.is_valid for g in out[:3])
    assert out[0].area == pytest.approx(25.0)  # one of the two triangles
    assert out[1].area == pytest.approx(100.0)  # the big part
    assert out[2].equals(good)
