"""Tests for pipeline/slope.py and pipeline/dem.py (step 5.3: mean terrain slope per field).

No network and no real data: every function in pipeline.dem that talks to the network is
replaced by a generator of synthetic terrain (see the `net` fixture), and the cache lives in
pytest's tmp_path.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from affine import Affine
from pyproj import Geod, Proj, Transformer
from rasterio.features import geometry_mask
from shapely.geometry import Point, Polygon, box

from pipeline import dem
from pipeline.slope import (
    DEFAULT_SOURCES,
    DemUnavailable,
    SlopeBands,
    field_slopes,
    horn_slope_pct,
    main,
    mean_slope_pct,
    pixel_size_m,
    zonal_mean,
)

ROOT = Path(__file__).resolve().parents[1]
GEOD = Geod(ellps="GRS80")


# ==============================================================================================
# slope maths
# ==============================================================================================

def test_horn_hand_computed_window():
    # a b c      1 2 4
    # d e f  =   3 9 7      dz/dx = ((4 + 2*7 + 8) - (1 + 2*3 + 5)) / (8 * 10) = 14/80
    # g h i      5 6 8      dz/dy = ((5 + 2*6 + 8) - (1 + 2*2 + 4)) / (8 * 20) = 16/160
    z = np.array([[1, 2, 4], [3, 9, 7], [5, 6, 8]], dtype=float)
    s = horn_slope_pct(z, 10.0, 20.0)
    assert s.shape == (3, 3) and s.dtype == np.float32
    assert s[1, 1] == pytest.approx(100 * math.hypot(14 / 80, 16 / 160), rel=1e-6)
    border = np.ones((3, 3), bool)
    border[1, 1] = False
    assert np.isnan(s[border]).all()  # no 3x3 window on the edge


@pytest.mark.parametrize("gx, gy", [(0.03, 0.0), (0.0, 0.02), (0.012, -0.016), (-0.05, 0.05), (0.0, 0.0)])
def test_tilted_plane_projected_grid(gx, gy):
    """A plane z = gx*x + gy*y on a 30 m metric grid has slope 100*sqrt(gx^2 + gy^2) everywhere."""
    y, x = np.mgrid[0:60, 0:80]
    z = 850.0 + gx * (x * 30.0) + gy * (-y * 30.0)  # row 0 is north
    s = horn_slope_pct(z, 30.0, 30.0)
    inner = s[1:-1, 1:-1]
    assert np.isfinite(inner).all()
    assert inner == pytest.approx(100 * math.hypot(gx, gy), rel=2e-4, abs=2e-4)
    assert np.isnan(s[0]).all() and np.isnan(s[-1]).all() and np.isnan(s[:, 0]).all() and np.isnan(s[:, -1]).all()


def test_tilted_plane_rectangular_pixels_and_chunking():
    y, x = np.mgrid[0:700, 0:50]
    z = 1000.0 + 0.04 * (x * 8.0) + 0.03 * (-y * 10.0)  # 8 m x 10 m pixels, 5 % slope
    whole = horn_slope_pct(z, 8.0, 10.0, chunk_rows=10_000)
    chunked = horn_slope_pct(z, 8.0, 10.0, chunk_rows=7)  # chunk boundaries must not show
    assert whole[1:-1, 1:-1] == pytest.approx(5.0, rel=2e-4)
    assert np.array_equal(whole, chunked, equal_nan=True)


def test_pixel_size_matches_geodesic_distance():
    """Per-row pixel sizes agree with pyproj's geodesic distances on GRS80."""
    for lat in (0.0, 37.9, 38.27, 60.0):
        for res in (1 / 3600, 1 / 10800):
            dx, dy = pixel_size_m(lat, res)
            _, _, ew = GEOD.inv(-100.8 - res / 2, lat, -100.8 + res / 2, lat)
            _, _, ns = GEOD.inv(-100.8, lat - res / 2, -100.8, lat + res / 2)
            assert float(dx) == pytest.approx(ew, rel=1e-6)
            assert float(dy) == pytest.approx(ns, rel=1e-6)
    dx, dy = pixel_size_m(38.0, 1 / 3600)
    assert 24.3 < float(dx) < 24.5 and 30.8 < float(dy) < 30.9  # a 1" pixel in Finney County is not square


@pytest.mark.parametrize("px_per_deg", [3600, 10800])
@pytest.mark.parametrize("ge, gn", [(0.02, 0.0), (0.0, 0.02), (0.015, 0.02), (-0.004, 0.003)])
def test_tilted_plane_geographic_grid(px_per_deg, ge, gn):
    """A plane tilted by (ge, gn) metres per metre of UTM 14N easting/northing, sampled on a
    lon/lat grid at this latitude, must come out at 100*sqrt(ge^2 + gn^2) (times the UTM point
    scale, 0.9999 here) — which only works if degrees are converted to metres per row."""
    res = 1.0 / px_per_deg
    h, w = 90, 120
    north, west = 37.93, -100.83
    lat = north - (np.arange(h) + 0.5) * res
    lon = west + (np.arange(w) + 0.5) * res
    lon2, lat2 = np.meshgrid(lon, lat)
    e, n = Transformer.from_crs("EPSG:4326", "EPSG:26914", always_xy=True).transform(lon2, lat2)
    z = 880.0 + ge * (e - e.mean()) + gn * (n - n.mean())
    k = Proj("EPSG:26914").get_factors(float(lon.mean()), float(lat.mean())).meridional_scale
    truth = 100 * math.hypot(ge, gn) * k  # ground gradient = grid gradient * point scale

    dx, dy = pixel_size_m(lat, res)
    s = horn_slope_pct(z, dx, dy)[1:-1, 1:-1]
    assert s == pytest.approx(truth, rel=5e-4, abs=1e-3)

    # the classic mistakes are far outside that tolerance:
    as_degrees = 100 * np.hypot(*np.gradient(z, res, res))  # gradients per degree
    assert np.nanmedian(as_degrees) > 1000 * truth
    if ge and not gn:  # square-pixel assumption (dx := dy) underestimates an east-west slope by cos(lat)
        square = horn_slope_pct(z, dy, dy)[1:-1, 1:-1]
        assert np.nanmedian(square) == pytest.approx(truth * math.cos(math.radians(37.9)), rel=1e-2)


def test_nodata_does_not_create_cliffs():
    y, x = np.mgrid[0:40, 0:40]
    plane = 900.0 + 0.02 * (x * 30.0)
    z = plane.copy()
    z[10, 10] = -999999.0  # 3DEP nodata
    z[25:27, 30:33] = -999999.0
    s = horn_slope_pct(z, 30.0, 30.0, nodata=-999999.0)
    hole = np.zeros_like(z, bool)
    hole[9:12, 9:12] = True  # the missing cell and everything whose 3x3 window contains it
    hole[24:28, 29:34] = True
    interior = np.zeros_like(z, bool)
    interior[1:-1, 1:-1] = True
    assert np.isnan(s[hole & interior]).all()
    assert s[~hole & interior] == pytest.approx(2.0, rel=2e-4)
    assert np.nanmax(s) < 2.01  # without the mask the cliff would be ~1e6 %

    # the same holes given as NaN behave identically
    zn = plane.copy()
    zn[10, 10] = np.nan
    zn[25:27, 30:33] = np.nan
    assert np.array_equal(horn_slope_pct(zn, 30.0, 30.0), s, equal_nan=True)

    # and if the sentinel is NOT declared, it shows — the guard is real
    assert np.nanmax(horn_slope_pct(z, 30.0, 30.0)) > 1e5


def test_clean_elevation_sentinels_and_units():
    raw = np.array([[850.5, -999999.0, -32768.0], [-9999.0, np.nan, -3.4e38], [0.0, -86.0, 4400.0]], dtype=np.float64)
    z = dem.clean_elevation(raw, nodata=-999999.0)
    assert z.dtype == np.float32
    assert np.isnan(z).tolist() == [[False, True, True], [True, True, True], [False, False, False]]
    assert z[0, 0] == pytest.approx(850.5) and z[2, 1] == pytest.approx(-86.0)  # below sea level is fine
    feet = dem.clean_elevation(np.array([[1000.0, -999999.0]]), nodata=-999999.0, scale=dem.vertical_scale("ft"))
    assert feet[0, 0] == pytest.approx(304.8) and np.isnan(feet[0, 1])
    assert dem.vertical_scale(None) == 1.0 and dem.vertical_scale("metre") == 1.0
    with pytest.raises(dem.DemFetchError):
        dem.vertical_scale("fathoms")


def test_slope_rejects_bad_input():
    with pytest.raises(ValueError):
        horn_slope_pct(np.zeros(5), 30.0, 30.0)
    with pytest.raises(ValueError):
        horn_slope_pct(np.zeros((5, 5)), 0.0, 30.0)
    assert np.isnan(horn_slope_pct(np.zeros((2, 9)), 30.0, 30.0)).all()  # too small for a 3x3 window


# ==============================================================================================
# zonal mean
# ==============================================================================================

def _grid(h=20, w=30):
    """values[r, c] = 100*r + c on a grid whose pixel (r, c) is the unit square [c, c+1] x [-r-1, -r]."""
    r, c = np.mgrid[0:h, 0:w]
    return (100.0 * r + c).astype(np.float32), Affine(1.0, 0.0, 0.0, 0.0, -1.0, 0.0)


def test_zonal_mean_counts_pixel_centres():
    values, t = _grid()
    geoms = [
        box(2.0, -5.0, 6.0, -1.0),  # exactly pixels r 1..4, c 2..5
        box(10.4, -8.6, 13.6, -6.4),  # centres inside: rows 6..8 (y -6.5 .. -8.5), columns 10..13 (x 10.5 .. 13.5)
        Polygon([(20, -10), (26, -10), (20, -16)]),  # a triangle
    ]
    res = zonal_mean(values, t, geoms)
    assert res.mean[0] == pytest.approx(values[1:5, 2:6].mean())
    assert res.n_pixels[0] == 16
    assert res.mean[1] == pytest.approx(values[6:9, 10:14].mean())
    assert res.n_pixels[1] == 12
    tri = geometry_mask([geoms[2]], out_shape=values.shape, transform=t, invert=True)
    assert res.mean[2] == pytest.approx(values[tri].mean()) and res.n_pixels[2] == tri.sum()
    assert list(res.method) == ["pixel_centre"] * 3


def test_zonal_mean_tiny_polygons_fall_back_to_touched_pixels():
    values, t = _grid()
    geoms = [
        box(3.1, -4.4, 3.4, -4.1),  # inside pixel (r 4, c 3), misses its centre (3.5, -4.5)
        box(7.8, -9.3, 8.3, -9.1),  # straddles pixels (r 9, c 7) and (r 9, c 8), no centre
        box(3.6, -4.9, 3.9, -4.6),  # a second sliver in pixel (4, 3): must not steal it from the first
        box(12.0, -3.0, 16.0, -1.0),  # a normal field next to them
    ]
    res = zonal_mean(values, t, geoms)
    assert res.mean[0] == pytest.approx(values[4, 3]) and res.n_pixels[0] == 1
    assert res.mean[1] == pytest.approx((values[9, 7] + values[9, 8]) / 2) and res.n_pixels[1] == 2
    assert res.mean[2] == pytest.approx(values[4, 3])
    assert res.mean[3] == pytest.approx(values[1:3, 12:16].mean())
    assert list(res.method) == ["all_touched", "all_touched", "all_touched", "pixel_centre"]


def test_zonal_mean_nan_only_without_data():
    values, t = _grid()
    values[0:6, 0:6] = np.nan  # a hole in the data
    geoms = [
        box(1.0, -4.0, 4.0, -1.0),  # entirely in the hole
        box(4.0, -8.0, 8.0, -4.0),  # half in the hole: mean of the valid pixels only
        box(100.0, -5.0, 104.0, -1.0),  # outside the raster
        None,  # no geometry
        Polygon(),  # empty geometry
        box(2.2, -2.4, 2.4, -2.2),  # tiny and in the hole
    ]
    res = zonal_mean(values, t, geoms)
    assert np.isnan(res.mean[[0, 2, 3, 4, 5]]).all()
    valid = values[4:8, 4:8]
    assert res.mean[1] == pytest.approx(np.nanmean(valid)) and res.n_pixels[1] == np.isfinite(valid).sum() == 12
    assert list(res.method) == ["no_data", "pixel_centre", "no_data", "no_geometry", "no_geometry", "no_data"]
    assert res.n_pixels[[0, 2, 3, 4, 5]].tolist() == [0, 0, 0, 0, 0]


def test_zonal_mean_overlapping_polygons_each_get_all_their_pixels():
    values, t = _grid()
    outer = box(2.0, -12.0, 12.0, -2.0)
    inner = box(4.0, -8.0, 8.0, -4.0)  # nested: one label raster could give these pixels to one field only
    twin = box(2.0, -12.0, 12.0, -2.0)  # an exact duplicate of `outer`
    crossing = box(10.0, -6.0, 18.0, -3.0)  # partial overlap
    res = zonal_mean(values, t, [outer, inner, twin, crossing])
    assert res.mean[0] == pytest.approx(values[2:12, 2:12].mean()) and res.n_pixels[0] == 100
    assert res.mean[1] == pytest.approx(values[4:8, 4:8].mean()) and res.n_pixels[1] == 16
    assert res.mean[2] == pytest.approx(res.mean[0]) and res.n_pixels[2] == 100
    assert res.mean[3] == pytest.approx(values[3:6, 10:18].mean()) and res.n_pixels[3] == 24


def test_zonal_mean_matches_per_polygon_masks():
    """The vectorised label-raster result equals the obvious one-polygon-at-a-time computation."""
    rng = np.random.default_rng(7)
    values = rng.gamma(2.0, 1.0, size=(120, 160)).astype(np.float32)
    values[rng.random(values.shape) < 0.02] = np.nan
    t = Affine(0.001, 0.0, -100.9, 0.0, -0.0008, 38.0)
    geoms = []
    for _ in range(300):  # random quadrilaterals, freely overlapping, a few sub-pixel
        cx, cy = rng.uniform(-100.9, -100.74), rng.uniform(37.904, 38.0)
        half = rng.choice([0.0002, 0.004, 0.009])
        geoms.append(Point(cx, cy).buffer(half, quad_segs=1).intersection(box(-100.9, 37.904, -100.74, 38.0)))
    res = zonal_mean(values, t, geoms)
    for i, g in enumerate(geoms):
        mask = geometry_mask([g], out_shape=values.shape, transform=t, invert=True)
        if not mask.any():
            mask = geometry_mask([g], out_shape=values.shape, transform=t, invert=True, all_touched=True)
        expected = np.nanmean(values[mask]) if np.isfinite(values[mask]).any() else np.nan
        assert res.mean[i] == pytest.approx(expected, rel=1e-5, nan_ok=True), i
        assert res.n_pixels[i] == np.isfinite(values[mask]).sum()
    assert set(res.method) <= {"pixel_centre", "all_touched", "no_data"}
    assert (res.method == "all_touched").any() and (res.method == "pixel_centre").any()


def test_banded_slope_and_zonal_equal_the_whole_raster_computation():
    """The pipeline never builds the full slope raster: it must not change a single number."""
    rng = np.random.default_rng(11)
    z = (900.0 + 0.02 * rng.normal(size=(150, 90)).cumsum(axis=0).cumsum(axis=1)).astype(np.float32)
    z[40:43, 20:25] = np.nan
    dx, dy = pixel_size_m(38.0 - (np.arange(150) + 0.5) / 3600, 1 / 3600)
    whole = horn_slope_pct(z, dx, dy)
    bands = SlopeBands(z, dx, dy)
    assert bands.shape == z.shape
    for a, b in [(0, 1), (0, 150), (1, 2), (37, 61), (148, 150), (149, 150), (60, 60)]:
        assert np.array_equal(bands.rows(a, b), whole[a:b], equal_nan=True), (a, b)

    t = Affine(1 / 3600, 0.0, -100.9, 0.0, -1 / 3600, 38.0)
    geoms = [box(-100.9 + 0.002 * i, 37.96 + 0.001 * (i % 5), -100.8965 + 0.002 * i, 37.985 + 0.001 * (i % 3))
             for i in range(11)]  # overlapping neighbours, each spanning ~100 rows
    geoms.append(box(-100.89001, 37.97001, -100.89000, 37.97002))  # sub-pixel
    geoms.append(box(-100.8945, 37.9885, -100.8935, 37.9892))  # inside the NaN hole (rows 40-42, cols 20-24)
    ref = zonal_mean(whole, t, geoms)
    assert set(ref.method) == {"pixel_centre", "all_touched", "no_data"}
    for band_px in (1, 90, 700, 10_000_000):  # one row per band ... everything in one band
        got = zonal_mean(bands, t, geoms, band_px=band_px)
        np.testing.assert_allclose(got.mean, ref.mean, rtol=1e-9, equal_nan=True)
        assert got.n_pixels.tolist() == ref.n_pixels.tolist() and got.method.tolist() == ref.method.tolist()


# ==============================================================================================
# DEM sources, cache and fallback — the network functions are replaced
# ==============================================================================================

GX, GY = 1756.0, 1665.0  # synthetic terrain: metres per degree of longitude / latitude (~2 % and ~1.5 %)


def plane(lon, lat):
    return 900.0 + GX * (lon + 101.0) + GY * (lat - 38.0)


def plane_slope_pct(lat: float) -> float:
    """Slope of `plane` at a latitude, from geodesic metres per degree — independent of pipeline.slope."""
    _, _, m_lon = GEOD.inv(-100.8005, lat, -100.7995, lat)
    _, _, m_lat = GEOD.inv(-100.8, lat - 0.0005, -100.8, lat + 0.0005)
    return 100 * math.hypot(GX / (1000 * m_lon), GY / (1000 * m_lat))


def srtm_steps(lon, lat):
    """Integer-metre terrain for the HGT fallback: exactly 1 m up per arc-second northwards."""
    return 2000.0 + np.rint((lat - 37.0) * 3600.0) + 0.0 * lon


def srtm_steps_slope_pct(lat: float) -> float:
    _, _, m = GEOD.inv(-100.8, lat - 0.5 / 3600, -100.8, lat + 0.5 / 3600)
    return 100 * 1.0 / m


class FakeNet:
    """Stand-ins for the three network functions of pipeline.dem, generating synthetic terrain.

    The tile layouts are written out here from the product documentation / the real files, not
    taken from pipeline.dem: a 3DEP tile "n38w101" has its NW corner at 38N 101W and carries a
    6-pixel collar; an SRTM tile "N37W101" has its SW corner at 37N 101W, 3601 x 3601 samples
    on whole arc-seconds, first row = northern edge.
    """

    def __init__(self):
        self.fail: set[str] = set()
        self.blocks: list[tuple] = []  # (source key, tile, block row, block col) fetched
        self.heads: list[str] = []
        self.srtm: list[str] = []
        self.etag = "v1"
        self.terrain = plane
        self.srtm_terrain = srtm_steps
        self.poison: tuple[int, int] | None = None  # tile-local (row, col) set to -999999, as nodata

    def fetch_3dep_blocks(self, source, k_r, k_c, blocks):
        if source.key in self.fail:
            raise OSError("simulated network failure")
        tile = source.tile_name(k_r, k_c)
        m = re.fullmatch(r"n(\d\d)w(\d\d\d)", tile)
        assert m, tile
        res = 1.0 / source.px_per_deg
        north, west = int(m[1]) + 6 * res, -int(m[2]) - 6 * res
        size = source.px_per_deg + 12
        out = []
        for bi, bj in blocks:
            self.blocks.append((source.key, tile, int(bi), int(bj)))
            rows = np.arange(bi * source.block, min((bi + 1) * source.block, size))
            cols = np.arange(bj * source.block, min((bj + 1) * source.block, size))
            lat = north - (rows + 0.5) * res
            lon = west + (cols + 0.5) * res
            z = self.terrain(lon[None, :], lat[:, None]).astype(np.float32)
            if self.poison and self.poison[0] in rows and self.poison[1] in cols:
                z[self.poison[0] - rows[0], self.poison[1] - cols[0]] = -999999.0
            out.append(dem.clean_elevation(z, nodata=-999999.0))
        return out

    def head_tile(self, url):
        key = "3dep13" if "/13/TIFF/" in url else "3dep1"
        if key in self.fail:
            raise OSError("simulated network failure")
        self.heads.append(url)
        return {"etag": self.etag, "last_modified": "Wed, 14 Jun 2023 14:30:25 GMT", "content_length": 123}

    def download_srtm(self, url, dest):
        if "srtm" in self.fail:
            raise OSError("simulated network failure")
        self.srtm.append(url)
        m = re.search(r"/skadi/N(\d\d)/N(\d\d)W(\d\d\d)\.hgt\.gz$", url)
        assert m and m[1] == m[2], url
        lat_s, lon_w = int(m[2]), -int(m[3])
        i = np.arange(3601)
        z = self.srtm_terrain(lon_w + i[None, :] / 3600.0, lat_s + 1 - i[:, None] / 3600.0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(dest, "wb", compresslevel=1) as fh:
            fh.write(np.rint(z).astype(">i2").tobytes())
        return dest


@pytest.fixture
def net(monkeypatch):
    fake = FakeNet()
    monkeypatch.setattr(dem, "_fetch_3dep_blocks", fake.fetch_3dep_blocks)
    monkeypatch.setattr(dem, "_head_tile", fake.head_tile)
    monkeypatch.setattr(dem, "_download_srtm", fake.download_srtm)
    return fake


def make_fields(boxes, start=1) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"field_id": [f"KS-{i:06d}" for i in range(start, start + len(boxes))]},
        geometry=[box(*b) for b in boxes], crs="EPSG:4326",
        index=pd.Index([f"row{i}" for i in range(len(boxes))], name="rowname"),
    )


#: four ~40 ha fields near the fake-field site (37.9N, 100.8W)
FIELDS = [(-100.812, 37.895, -100.805, 37.901), (-100.801, 37.895, -100.794, 37.901),
          (-100.812, 37.903, -100.805, 37.909), (-100.801, 37.903, -100.793, 37.909)]


def test_default_order_is_3dep_then_srtm():
    assert DEFAULT_SOURCES == ("3dep13", "3dep1", "srtm")
    assert set(DEFAULT_SOURCES) == set(dem.SOURCES)
    s13, s1, srtm = (dem.SOURCES[k] for k in DEFAULT_SOURCES)
    k_r, k_c = 90 - 38, 180 - 101  # the 1x1 degree cell whose NW corner is 38N 101W
    assert s1.url(k_r, k_c) == ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current/"
                                "n38w101/USGS_1_n38w101.tif")
    assert s13.url(k_r, k_c) == ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/"
                                 "n38w101/USGS_13_n38w101.tif")
    assert srtm.url(k_r, k_c) == "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N37/N37W101.hgt.gz"
    assert (s13.tile_size, s1.tile_size, srtm.tile_size) == (10812, 3612, 3601)
    # the real USGS_1_n38w101.tif starts at (-101.0016667, 38.0016667); N37W101.hgt's first sample is at (-101, 38)
    t = s1.transform(k_r * 3600 - 6, k_c * 3600 - 6)
    assert (t.c, t.f) == pytest.approx((-101.0 - 6 / 3600, 38.0 + 6 / 3600), abs=1e-12)
    t = srtm.transform(k_r * 3600, k_c * 3600)
    assert (t.c + 0.5 * t.a, t.f + 0.5 * t.e) == pytest.approx((-101.0, 38.0), abs=1e-12)  # centre of the first pixel


def test_uses_3dep_and_reports_provenance(net, tmp_path):
    fields = make_fields(FIELDS)
    slope = mean_slope_pct(fields, cache_dir=tmp_path, quiet=True)

    assert isinstance(slope, pd.Series) and slope.name == "slope_pct" and slope.dtype == np.float64
    assert slope.index.equals(fields.index)
    assert slope.notna().all()
    assert slope.to_numpy() == pytest.approx(plane_slope_pct(37.902), rel=1e-3)

    prov = slope.attrs["provenance"]
    json.dumps(prov)  # must be serialisable
    assert prov["source"] == "3dep13" and prov["fallback_used"] is False
    assert prov["resolution_arcsec"] == pytest.approx(1 / 3)
    assert prov["pixel_size_m"] == pytest.approx([8.14, 10.28], abs=0.02)
    assert prov["urls"] == ["https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/"
                            "n38w101/USGS_13_n38w101.tif"]
    assert prov["tiles"][0]["tile"] == "n38w101" and prov["tiles"][0]["etag"] == "v1"
    assert prov["attempts"] == [{"source": "3dep13", "ok": True, "error": None}]
    assert prov["fields_by_method"] == {"pixel_centre": 4} and prov["n_nan"] == 0
    assert "Horn" in prov["method"]
    assert {k for k, *_ in net.blocks} == {"3dep13"} and not net.srtm
    assert prov["cache_units_fetched_this_run"] == len(net.blocks) == prov["cache_units_used"]


def test_second_run_is_served_from_the_cache(net, tmp_path, monkeypatch):
    fields = make_fields(FIELDS)
    first = mean_slope_pct(fields, cache_dir=tmp_path, quiet=True)
    n_blocks = len(net.blocks)
    assert n_blocks > 0

    def no_network(*a, **k):
        raise AssertionError("network used although everything is cached")

    for name in ("_fetch_3dep_blocks", "_head_tile", "_download_srtm"):
        monkeypatch.setattr(dem, name, no_network)
    second = mean_slope_pct(fields, cache_dir=tmp_path, quiet=True)
    pd.testing.assert_series_equal(first, second)
    assert second.attrs["provenance"]["cache_units_fetched_this_run"] == 0
    assert second.attrs["provenance"]["source"] == "3dep13"
    # a different set of fields inside the blocks already on disk needs no network either
    other = make_fields([(-100.80, 37.89, -100.79, 37.90), (-100.83, 37.905, -100.82, 37.912)])
    assert mean_slope_pct(other, cache_dir=tmp_path, quiet=True).notna().all()


def test_cache_is_on_a_fixed_grid_not_keyed_on_the_fields(net, tmp_path):
    a = make_fields(FIELDS[:1])
    mean_slope_pct(a, sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    first = list(net.blocks)
    # 512 px blocks in the tile's own pixel space: this one spans 37.86-38.00N, 100.86-100.72W
    assert first == [("3dep1", "n38w101", 0, 1)]
    files = sorted(p.name for p in (tmp_path / "3dep1" / "n38w101").iterdir())
    assert files == ["USGS_1_n38w101_b512_r00_c01.tif", "tile.json"]

    # a bigger extent re-uses that block and fetches only the new ones
    b = make_fields(FIELDS + [(-100.60, 37.80, -100.59, 37.81)])
    out = mean_slope_pct(b, sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    new = net.blocks[len(first):]
    assert new and ("3dep1", "n38w101", 0, 1) not in new
    assert len(set(net.blocks)) == len(net.blocks)  # nothing fetched twice
    assert out.to_numpy() == pytest.approx([plane_slope_pct(g.centroid.y) for g in b.geometry], rel=1e-3)


def test_falls_back_to_coarser_3dep_then_srtm(net, tmp_path):
    fields = make_fields(FIELDS)

    net.fail = {"3dep13"}
    s = mean_slope_pct(fields, cache_dir=tmp_path / "a", quiet=True)
    prov = s.attrs["provenance"]
    assert prov["source"] == "3dep1" and prov["fallback_used"] is True
    assert [a["source"] for a in prov["attempts"]] == ["3dep13", "3dep1"]
    assert prov["attempts"][0]["ok"] is False and "simulated network failure" in prov["attempts"][0]["error"]
    assert s.to_numpy() == pytest.approx(plane_slope_pct(37.902), rel=1e-3)

    net.fail = {"3dep13", "3dep1"}
    s = mean_slope_pct(fields, cache_dir=tmp_path / "b", quiet=True)
    prov = s.attrs["provenance"]
    assert prov["source"] == "srtm" and prov["fallback_used"] is True
    assert [(a["source"], a["ok"]) for a in prov["attempts"]] == [("3dep13", False), ("3dep1", False), ("srtm", True)]
    assert prov["urls"] == ["https://s3.amazonaws.com/elevation-tiles-prod/skadi/N37/N37W101.hgt.gz"]
    assert net.srtm == prov["urls"]
    assert s.to_numpy() == pytest.approx(srtm_steps_slope_pct(37.902), rel=1e-3)  # 1 m per arc-second ≈ 3.24 %
    assert (tmp_path / "b" / "srtm" / "N37W101.hgt.gz").exists()
    json.dumps(prov)

    # SRTM is cached too: a rerun downloads nothing
    again = mean_slope_pct(fields, cache_dir=tmp_path / "b", quiet=True)
    assert len(net.srtm) == 1 and again.attrs["provenance"]["cache_units_fetched_this_run"] == 0
    pd.testing.assert_series_equal(s, again)


def test_cached_source_wins_over_failing_network(net, tmp_path):
    """Once 3DEP is cached, a dead network does not push the run onto the fallback."""
    fields = make_fields(FIELDS)
    first = mean_slope_pct(fields, cache_dir=tmp_path, quiet=True)
    net.fail = {"3dep13", "3dep1", "srtm"}
    second = mean_slope_pct(fields, cache_dir=tmp_path, quiet=True)
    assert second.attrs["provenance"]["source"] == "3dep13"
    pd.testing.assert_series_equal(first, second)


def test_no_source_at_all(net, tmp_path):
    net.fail = {"3dep13", "3dep1", "srtm"}
    fields = make_fields(FIELDS)
    with pytest.raises(DemUnavailable) as err:
        mean_slope_pct(fields, cache_dir=tmp_path, quiet=True)
    assert [a["source"] for a in err.value.attempts] == ["3dep13", "3dep1", "srtm"]

    s = mean_slope_pct(fields, cache_dir=tmp_path, quiet=True, allow_unavailable=True)
    assert s.isna().all() and s.index.equals(fields.index) and s.dtype == np.float64
    prov = s.attrs["provenance"]
    assert prov["source"] is None and "no elevation source" in prov["error"] and len(prov["attempts"]) == 3

    with pytest.raises(ValueError):
        mean_slope_pct(fields, sources=["lidar"], cache_dir=tmp_path, quiet=True)


def test_too_large_for_fine_dem_uses_the_coarser_one(net, tmp_path, monkeypatch):
    monkeypatch.setattr(dem, "MAX_MOSAIC_PIXELS", 20_000)  # the 4 fields span ~160 x 210 px at 1/3", ~57 x 75 at 1"
    s = mean_slope_pct(make_fields(FIELDS), cache_dir=tmp_path, quiet=True)
    prov = s.attrs["provenance"]
    assert prov["source"] == "3dep1" and "split the fields" in prov["attempts"][0]["error"]
    assert not [b for b in net.blocks if b[0] == "3dep13"]  # nothing was downloaded for the rejected source


@pytest.mark.parametrize("key", ["3dep1", "3dep13", "srtm"])
def test_fields_across_tile_borders(net, tmp_path, key):
    """Fields around 38N 101W need four tiles; the mosaic must be seamless (a one-pixel slip at
    a tile edge would show up as a step in the plane and a wrong slope)."""
    boxes = [(-101.004, 37.996, -100.996, 38.004),  # on the four-tile corner
             (-101.004, 37.970, -100.996, 37.976),  # across the 101W meridian
             (-100.960, 37.997, -100.952, 38.003),  # across the 38N parallel
             (-101.030, 38.020, -101.022, 38.026)]  # inside the NW tile only
    fields = make_fields(boxes)
    table = field_slopes(fields, sources=[key], cache_dir=tmp_path, quiet=True)
    prov = table.attrs["provenance"]
    if key == "srtm":
        assert sorted(t["tile"] for t in prov["tiles"]) == ["N37W101", "N37W102", "N38W101", "N38W102"]
        expected = [srtm_steps_slope_pct(g.centroid.y) for g in fields.geometry]
    else:
        assert sorted(t["tile"] for t in prov["tiles"]) == ["n38w101", "n38w102", "n39w101", "n39w102"]
        expected = [plane_slope_pct(g.centroid.y) for g in fields.geometry]
    assert table["slope_pct"].to_numpy() == pytest.approx(expected, rel=1e-3)
    assert (table["method"] == "pixel_centre").all()
    # pixel counts match the field size at this resolution
    px = prov["pixel_size_m"][0] * prov["pixel_size_m"][1]
    area = fields.geometry.to_crs("EPSG:26914").area.to_numpy()
    assert table["n_pixels"].to_numpy() == pytest.approx(area / px, rel=0.08)


def test_mosaic_matches_terrain_at_pixel_centres(net, tmp_path):
    """Georeferencing of the mosaic: every pixel holds the terrain at its own centre."""
    bounds = [(-101.01, 37.99, -100.99, 38.01)]
    for key in ("3dep1", "3dep13"):
        m = dem.read_mosaic(dem.SOURCES[key], bounds, cache_dir=tmp_path, quiet=True)
        h, w = m.z.shape
        cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
        lon, lat = m.transform.c + cols * m.transform.a, m.transform.f + rows * m.transform.e
        assert np.isfinite(m.z).all()
        assert np.abs(m.z - plane(lon, lat)).max() < 2e-3  # float32 metres
        west, south, east, north = m.bounds
        assert west < -101.01 and east > -100.99 and south < 37.99 and north > 38.01
        assert m.row_latitudes()[0] == pytest.approx(lat[0, 0])
    # whole metres on the arc-second lattice, so the int16 tiles hold the terrain exactly
    net.srtm_terrain = lambda lon, lat: 900.0 + 3600.0 * (lon + 101.0) + 7200.0 * (lat - 38.0)
    m = dem.read_mosaic(dem.SOURCES["srtm"], bounds, cache_dir=tmp_path, quiet=True)
    h, w = m.z.shape
    cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    lon, lat = m.transform.c + cols * m.transform.a, m.transform.f + rows * m.transform.e
    assert np.abs(m.z - net.srtm_terrain(lon, lat)).max() < 1e-3


def test_nodata_in_the_dem_is_ignored_not_averaged(net, tmp_path):
    # one missing cell of n38w101 (1"), in the middle of the first field: tile row = (38 + 6/3600 - lat) * 3600
    lat, lon = 37.898, -100.8085
    net.poison = (int((38.0 + 6 / 3600 - lat) * 3600), int((lon + 101.0 + 6 / 3600) * 3600))
    table = field_slopes(make_fields(FIELDS), sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    assert table["slope_pct"].to_numpy() == pytest.approx(plane_slope_pct(37.902), rel=1e-3)  # no cliff
    assert table["slope_pct"].max() < 3.0
    # the 3x3 block of slope pixels around the hole is missing from that field's count
    net.poison = None
    full = field_slopes(make_fields(FIELDS), sources=["3dep1"], cache_dir=tmp_path / "clean", quiet=True)
    assert (full["n_pixels"] - table["n_pixels"]).tolist() == [9, 0, 0, 0]


def test_tile_republished_upstream_refetches_blocks(net, tmp_path):
    a = make_fields(FIELDS[:1])
    mean_slope_pct(a, sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    assert net.blocks == [("3dep1", "n38w101", 0, 1)]
    manifest = json.loads((tmp_path / "3dep1" / "n38w101" / "tile.json").read_text())
    assert manifest["etag"] == "v1" and manifest["url"].endswith("USGS_1_n38w101.tif")

    # USGS replaces the tile; the next run needs one more block of it
    net.etag = "v2"
    net.terrain = lambda lon, lat: plane(lon, lat) * 2.0
    b = make_fields(FIELDS[:1] + [(-100.60, 37.80, -100.59, 37.81)])
    out = mean_slope_pct(b, sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    assert ("3dep1", "n38w101", 0, 1) in net.blocks[1:]  # the old block was fetched again, not mixed in
    assert out.to_numpy() == pytest.approx([2 * plane_slope_pct(g.centroid.y) for g in b.geometry], rel=1e-3)
    assert json.loads((tmp_path / "3dep1" / "n38w101" / "tile.json").read_text())["etag"] == "v2"


def test_corrupt_cache_entries_are_refetched(net, tmp_path):
    fields = make_fields(FIELDS[:1])
    good = mean_slope_pct(fields, sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    block = tmp_path / "3dep1" / "n38w101" / "USGS_1_n38w101_b512_r00_c01.tif"
    block.write_bytes(block.read_bytes()[:1000])  # truncated file
    again = mean_slope_pct(fields, sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    pd.testing.assert_series_equal(good, again)
    assert len(net.blocks) == 2

    good = mean_slope_pct(fields, sources=["srtm"], cache_dir=tmp_path, quiet=True)
    hgt = tmp_path / "srtm" / "N37W101.hgt.gz"
    hgt.write_bytes(b"not a gzip file")
    again = mean_slope_pct(fields, sources=["srtm"], cache_dir=tmp_path, quiet=True)
    pd.testing.assert_series_equal(good, again)
    assert len(net.srtm) == 2


def test_odd_inputs(net, tmp_path):
    # tiny field, missing geometry, a field in another CRS, a GeoSeries instead of a GeoDataFrame
    fields = gpd.GeoDataFrame(
        {"field_id": ["KS-000001", "KS-000002", "KS-000003"]},
        geometry=[box(*FIELDS[0]), box(-100.80010, 37.90003, -100.80008, 37.90004), None], crs="EPSG:4326",
        index=[10, 20, 30],
    )
    table = field_slopes(fields, sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    assert table.index.tolist() == [10, 20, 30]
    assert table["method"].tolist() == ["pixel_centre", "all_touched", "no_geometry"]
    assert table["slope_pct"].iloc[:2].to_numpy() == pytest.approx(plane_slope_pct(37.9), rel=1e-3)
    assert np.isnan(table["slope_pct"].iloc[2]) and table["n_pixels"].tolist()[1:] == [1, 0]
    assert table.attrs["provenance"]["fields_by_method"] == {"pixel_centre": 1, "all_touched": 1, "no_geometry": 1}

    utm = fields.iloc[:2].to_crs("EPSG:26914")
    assert mean_slope_pct(utm, sources=["3dep1"], cache_dir=tmp_path, quiet=True).to_numpy() == pytest.approx(
        table["slope_pct"].iloc[:2].to_numpy(), rel=1e-6)
    series = mean_slope_pct(fields.geometry.iloc[:2], sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    assert series.index.tolist() == [10, 20] and series.notna().all()

    no_crs = gpd.GeoSeries(list(utm.geometry))  # metres, but no CRS to say so
    with pytest.raises(ValueError, match="lon/lat"):
        mean_slope_pct(no_crs, sources=["3dep1"], cache_dir=tmp_path, quiet=True)

    empty = mean_slope_pct(fields.iloc[2:], sources=["3dep1"], cache_dir=tmp_path, quiet=True)
    assert empty.isna().all() and empty.index.tolist() == [30] and empty.attrs["provenance"]["source"] is None


def test_cli_prints_values_and_summary(net, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dem, "DEM_DIR", tmp_path / "cache")
    path = tmp_path / "fields.parquet"
    make_fields(FIELDS).reset_index(drop=True).to_parquet(path)
    assert main([str(path), "--compare", "3dep1,srtm"]) == 0
    out = capsys.readouterr().out
    assert "slope_pct for 4 fields" in out
    assert "USGS 3DEP seamless DEM, 1/3 arc-second" in out and "[3dep13]" in out
    for fid in ("KS-000001", "KS-000004"):
        assert fid in out
    assert "median" in out and "3dep1_pct" in out and "srtm_pct" in out and "Pearson r" in out
    assert f"{plane_slope_pct(37.898):.2f}" in out
    assert main([str(tmp_path / "missing.parquet")]) == 1


@pytest.mark.parametrize("cmd", [["pipeline/slope.py"], ["-m", "pipeline.slope"]])
def test_cli_runs_as_script_and_as_module(cmd):
    proc = subprocess.run([sys.executable, *cmd, "--help"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "Mean terrain slope" in proc.stdout and "fields_fake.parquet" in proc.stdout
