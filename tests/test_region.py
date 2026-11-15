"""pipeline/region.py: which county, where its files live, what it may borrow from Finney County."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from pipeline import region
from pipeline.region import BASE_DATA_DIR, ROOT, get_region


def test_default_is_finney_in_data_dir(monkeypatch):
    monkeypatch.delenv("REGION", raising=False)
    r = get_region()
    assert (r.fips, r.state_abbr, r.county_name, r.label) == ("20055", "KS", "Finney", "Finney County, KS")
    assert r.is_default and r.data_dir == BASE_DATA_DIR


def test_other_county_gets_its_own_folder():
    r = get_region("20069")
    assert (r.county_name, r.state_fips, r.county_fips) == ("Gray", "20", "069")
    assert r.data_dir == BASE_DATA_DIR / "regions" / "20069"
    assert not r.is_default


@pytest.mark.parametrize("bad", ["2005", "200555", "KS055", "99001"])
def test_bad_codes_are_errors(bad):
    with pytest.raises(ValueError):
        get_region(bad)


def test_region_name_env_wins(monkeypatch):
    monkeypatch.setenv("REGION_NAME", "Somewhere")
    assert get_region("31001").county_name == "Somewhere"
    assert get_region("31001").state_abbr == "NE"


def test_nass_path_and_district(monkeypatch):
    monkeypatch.delenv("NASS_ASD", raising=False)
    assert region.nass_path(get_region("20055")).name == "nass_finney.parquet"
    assert region.nass_path(get_region("20081")) == BASE_DATA_DIR / "regions" / "20081" / "nass.parquet"
    assert region.nass_district(get_region("20093")) == "30"
    with pytest.raises(ValueError):
        region.nass_district(get_region("31001"))
    monkeypatch.setenv("NASS_ASD", "70")
    assert region.nass_district(get_region("31001")) == "70"


def test_fallback_borrows_finney_only_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(region, "BASE_DATA_DIR", tmp_path)
    finney = tmp_path / "climate_futures.parquet"
    finney.write_text("x")
    gray = region.Region("20069", "20", "069", "KS", "Gray", tmp_path / "regions" / "20069")
    own = gray.data_dir / "climate_futures.parquet"
    assert region.fallback_path(own, "climate", gray) == finney  # missing -> Finney's
    assert region.is_borrowed(finney, gray)
    own.parent.mkdir(parents=True)
    own.write_text("y")
    assert region.fallback_path(own, "climate", gray) == own      # its own file wins
    assert not region.is_borrowed(own, gray)
    nass_finney = tmp_path / "nass_finney.parquet"
    nass_finney.write_text("z")
    assert region.fallback_path(gray.data_dir / "nass.parquet", "nass", gray) == nass_finney
    with pytest.raises(ValueError):
        region.fallback_path(own, "fields", gray)                 # fields are never borrowed


def test_finney_never_borrows():
    finney = get_region("20055")
    path = finney.data_dir / "no_such_file.parquet"
    assert region.fallback_path(path, "climate", finney) == path


def test_env_region_moves_every_output_path():
    """With REGION set, the pipeline, planner and causal outputs all land in the county's folder."""
    code = (
        "from pipeline import common, schema, nass, satellite_schema, field_traits_schema\n"
        "from pipeline.soil import health_schema\n"
        "from pipeline.climate import schema as cs\n"
        "from pipeline.causal import config as cc\n"
        "from api.planner import config as pc\n"
        "paths = [common.DATA_DIR, schema.REAL_PATH, nass.NASS_PATH, satellite_schema.SATELLITE_PATH,\n"
        "         field_traits_schema.FIELD_TRAITS_PATH, health_schema.SOIL_HEALTH_PATH, cs.FUTURES_PATH,\n"
        "         cc.EFFECTS_PATH, cc.PAIRS_PATH, pc.DATA_DIR]\n"
        "print(common.FIPS, common.COUNTY_NAME, all('regions/20081' in str(p) for p in paths), common.RAW_DIR)\n"
    )
    env = {**os.environ, "REGION": "20081"}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    fips, name, moved, raw = out.stdout.strip().split(maxsplit=3)  # the path may contain spaces
    assert (fips, name, moved) == ("20081", "Haskell", "True")
    assert raw.endswith("data/raw")  # downloads stay shared
