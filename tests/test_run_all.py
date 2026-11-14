"""pipeline.run_all: assembly, validation, the atomic write, the failure policy and the sanity
checks. No network and no real data: the three step functions (fields, soil, slope) and the
county outline are replaced by small synthetic ones, and everything is written to tmp_path."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from shapely.geometry import MultiPolygon, box

from pipeline import run_all
from pipeline.common import YEARS
from pipeline.dem import DemUnavailable
from pipeline.schema import COLUMNS, SchemaError, load_fields, validate_fields
from pipeline.soil import SoilDataError

ROOT = Path(__file__).resolve().parents[1]
CRS_M = "EPSG:5070"
X0, Y0 = -410_000.0, 1_680_000.0  # inside Finney County, EPSG:5070 metres
COLS, ROWS = 8, 5  # 40 fields
PITCH, SIDE = 800.0, 600.0  # one 36 ha field per 64 ha cell
FIELD_HA = SIDE * SIDE / 10_000


def rotation(cycle, offset=0):
    return {str(year): cycle[(i + offset) % len(cycle)] for i, year in enumerate(YEARS)}


def plausible_history(i: int) -> dict:
    """Histories that look like southwest Kansas: 24 wheat-sorghum-fallow, 8 corn/soybeans,
    6 alfalfa stands inside a corn/wheat rotation, 2 continuous corn."""
    if i < 24:
        return rotation(["winter wheat", "sorghum", "fallow"], i)
    if i < 32:
        return rotation(["corn", "soybeans"], i)
    if i < 38:
        return rotation(["alfalfa"] * 5 + ["corn", "winter wheat", "corn", "winter wheat"], 3 * i)
    return rotation(["corn"])


def make_fields(histories=None, n: int = COLS * ROWS) -> gpd.GeoDataFrame:
    """What `build_fields` returns: field_id, geometry, crop_history, area_ha (EPSG:4326) + attrs.
    Rows are deliberately NOT in field_id order, and area_ha is a wrong value on purpose."""
    cells = [(i % COLS, i // COLS) for i in range(n)]
    geoms = gpd.GeoSeries(
        [box(X0 + c * PITCH, Y0 + r * PITCH, X0 + c * PITCH + SIDE, Y0 + r * PITCH + SIDE) for c, r in cells], crs=CRS_M
    ).to_crs("EPSG:4326")
    histories = [plausible_history(i) for i in range(n)] if histories is None else histories
    frame = gpd.GeoDataFrame(
        {
            "field_id": [f"KS-{i + 1:06d}" for i in range(n)],
            "crop_history": [json.dumps(h) for h in histories],
            "area_ha": np.full(n, 999.0),
        },
        geometry=geoms.values,
        crs="EPSG:4326",
    )[["field_id", "geometry", "crop_history", "area_ha"]]
    frame = frame.iloc[::-1].reset_index(drop=True)  # descending ids
    frame.attrs["source_ids"] = [f"CSB{int(fid[3:]):09d}" for fid in frame["field_id"]]
    frame.attrs["provenance"] = {
        "source": "csb",
        "requested_source": "auto",
        "fallback_reason": None,
        "county_boundary": {"url": "https://example.test/county.zip", "area_km2": 26.0},
        "csb": {"release": "NationalCSB_test", "window": "2018-2025", "url": "https://example.test/csb.zip", "from_cache": True},
        "cdl": {
            "service": "https://example.test/cdl",
            "years_missing": [],
            "per_year": {str(y): {"url": f"https://example.test/CDL_{y}.tif", "route": "local-cache",
                                  "downloaded_via": "cropscape-cache-url"} for y in YEARS},
        },  # fmt: skip
        "filters": [{"step": "area >= 5 ha", "in": 50, "dropped": 10, "out": 40, "note": ""}],
        "agreement_with_csb_attributes": {
            "final_fields": {
                "per_year": {"2018": {"compared": 40, "agree": 40, "rate": 1.0}, "2019": {"compared": 40, "agree": 39, "rate": 0.975}},
                "overall": {"compared": 80, "agree": 79, "rate": 0.9875},
            }
        },
        "counts": {"fields_before_max_fields": n},
        "max_fields_rule": None,
        "not_json_safe": float("nan"),
    }
    return frame


def number(field_id: str) -> int:
    digits = "".join(ch for ch in field_id if ch.isdigit())  # also works for the deliberately bad ids
    return int(digits) if digits else 0


def om_for(field_id: str) -> float:
    return round(1.0 + number(field_id) / 100, 3)  # KS-000007 -> 1.07


def slope_for(field_id: str) -> float:
    return 0.2 + number(field_id) / 50 + 0.001234  # more decimals than the file keeps


def county_frame() -> gpd.GeoDataFrame:
    outline = gpd.GeoDataFrame(
        {"GEOID": ["20055"], "NAME": ["Finney"]},
        geometry=gpd.GeoSeries([box(X0 - 100, Y0 - 100, X0 + COLS * PITCH - 100, Y0 + ROWS * PITCH - 100)], crs=CRS_M).to_crs("EPSG:4326").values,
        crs="EPSG:4326",
    )
    outline.attrs["source"] = "https://example.test/county.zip"
    return outline


class Steps:
    """The fake step functions, with switches for the failure cases and a record of the calls."""

    def __init__(self):
        self.fields = make_fields()
        self.calls: list[dict] = []
        self.soil_error: Exception | None = None
        self.slope_error: Exception | None = None
        self.fields_error: Exception | None = None
        self.soil_result = None  # callable(fields) -> Series, overrides the default
        self.slope_result = None

    def build_fields(self, source="auto", min_area_ha=5.0, max_fields=None):
        self.calls.append({"source": source, "min_area_ha": min_area_ha, "max_fields": max_fields})
        if self.fields_error:
            raise self.fields_error
        return self.fields

    def organic_matter_pct(self, fields, **options):
        if self.soil_error:
            raise self.soil_error
        if self.soil_result is not None:
            return self.soil_result(fields)
        series = pd.Series([om_for(f) for f in fields["field_id"]], index=fields.index, name="organic_matter_pct")
        series.attrs["provenance"] = {"endpoint": "https://example.test/sda", "http": {"http_requests": 0},
                                      "tile_scheme": {"n_tiles": 1, "tiles_from_cache": 1, "tiles_fetched": 0}}  # fmt: skip
        return series

    def mean_slope_pct(self, fields, **options):
        if self.slope_error:
            raise self.slope_error
        if self.slope_result is not None:
            return self.slope_result(fields)
        series = pd.Series([slope_for(f) for f in fields["field_id"]], index=fields.index, name="slope_pct")
        series.attrs["provenance"] = {"source": "3dep13", "source_label": "test DEM", "fallback_used": False,
                                      "attempts": [{"source": "3dep13", "ok": True, "error": None}],
                                      "urls": ["https://example.test/dem.tif"], "cache_units_fetched_this_run": 0}  # fmt: skip
        return series


@pytest.fixture
def steps(monkeypatch):
    fake = Steps()
    monkeypatch.setattr(run_all, "build_fields", fake.build_fields)
    monkeypatch.setattr(run_all, "organic_matter_pct", fake.organic_matter_pct)
    monkeypatch.setattr(run_all, "mean_slope_pct", fake.mean_slope_pct)
    monkeypatch.setattr(run_all, "county_boundary", lambda fips=None: county_frame())
    return fake


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if ".tmp-" in p.name)


def status(checks, name: str) -> str:
    found = [c for c in checks if c.name == name]
    assert len(found) == 1, f"no single check called {name!r}: {[c.name for c in checks]}"
    return found[0].status


# --------------------------------------------------------------------------------------------
# the whole run
# --------------------------------------------------------------------------------------------
def test_run_writes_a_valid_sorted_file(steps, tmp_path, capsys):
    out = tmp_path / "data" / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 0
    printed = capsys.readouterr().out

    table = load_fields(out)  # raises if the file does not match schema.md
    assert list(table.columns) == COLUMNS
    assert table.crs.to_epsg() == 4326
    assert len(table) == 40
    assert table["field_id"].tolist() == [f"KS-{i:06d}" for i in range(1, 41)]  # sorted, although the input was not
    for col in ("organic_matter_pct", "slope_pct", "area_ha"):
        assert table[col].dtype == np.float64

    # every value sits in the row of the field it was computed for
    assert table["organic_matter_pct"].tolist() == [om_for(f) for f in table["field_id"]]
    assert table["slope_pct"].tolist() == [round(slope_for(f), 2) for f in table["field_id"]]
    for field_id, history in zip(table["field_id"], table["crop_history"]):
        assert json.loads(history) == plausible_history(number(field_id) - 1)

    # area is recomputed from the outline in EPSG:5070, not taken from build_fields (999)
    assert table["area_ha"].tolist() == pytest.approx([FIELD_HA] * 40, abs=0.006)
    recomputed = table.geometry.to_crs("EPSG:5070").area / 10_000
    assert np.abs(table["area_ha"] - recomputed).max() <= 0.005 + 1e-9

    assert leftovers(out.parent) == []
    assert "sanity checks" in printed and "fields: 40" in printed
    assert "step 10/10" in printed
    assert "network this run" in printed


def test_side_files(steps, tmp_path):
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out), "--samples", "0"]) == 0
    provenance_path, ids_path = run_all.side_paths(out)
    assert provenance_path.name == "fields_provenance.json" and ids_path.name == "fields_source_ids.csv"

    def no_constants(name):  # NaN / Infinity are not JSON; json.dumps would have written them without allow_nan=False
        raise AssertionError(f"provenance contains {name}")

    prov = json.loads(provenance_path.read_text(), parse_constant=no_constants)
    assert prov["output"]["fields"] == 40
    assert prov["output"]["sha256"] == run_all.sha256_of(out)
    assert prov["output"]["bytes"] == out.stat().st_size
    assert prov["fields_source"] == "csb" and prov["csb_release"] == "NationalCSB_test"
    assert prov["filters"][0]["dropped"] == 10
    assert prov["csb_vs_raster_agreement"]["final_fields"]["overall"]["rate"] == 0.9875
    assert prov["fallbacks_taken"] == [] and prov["columns_allowed_missing"] == {}
    assert prov["downloads_this_run"] == {"csb": "cached county extract", "cdl_years_downloaded": [],
                                          "ssurgo_http_requests": 0, "dem_units_fetched": 0}  # fmt: skip
    assert set(prov["steps"]) == {"fields", "soil", "slope"}
    assert prov["steps"]["fields"]["not_json_safe"] is None
    assert prov["steps"]["slope"]["source"] == "3dep13"
    assert {"fields", "soil", "slope", "area", "assemble", "validate", "write", "reread", "summary", "total"} <= set(prov["timings_s"])
    assert prov["versions"]["geopandas"] and prov["versions"]["python"]
    assert prov["run"]["arguments"]["max_fields"] is None
    assert {c["status"] for c in prov["sanity_checks"]} <= {"PASS", "WARN"}
    urls = json.dumps(prov["sources_used"])
    for needle in ("example.test/csb.zip", "example.test/sda", "example.test/dem.tif", "example.test/CDL_2008.tif", "example.test/county.zip"):
        assert needle in urls

    with open(ids_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["field_id"] for r in rows] == load_fields(out)["field_id"].tolist()
    assert all(r["source_id"] == f"CSB{number(r['field_id']):09d}" for r in rows)  # ids follow the sort
    assert {r["source"] for r in rows} == {"csb:NationalCSB_test"}


def test_cli_arguments_reach_build_fields(steps, tmp_path):
    out = tmp_path / "x.parquet"
    assert run_all.main(["--out", str(out), "--max-fields", "7", "--min-area-ha", "12.5", "--fields-source", "cdl-segment"]) == 0
    assert steps.calls == [{"source": "cdl-segment", "min_area_ha": 12.5, "max_fields": 7}]
    assert run_all.side_paths(out)[0].name == "x_provenance.json"


def test_defaults(steps, tmp_path):
    assert run_all.main(["--out", str(tmp_path / "f.parquet")]) == 0
    assert steps.calls == [{"source": "auto", "min_area_ha": 5.0, "max_fields": None}]  # no cap by default


def test_rerun_is_identical_and_replaces_the_file(steps, tmp_path):
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 0
    first = out.read_bytes()
    assert run_all.main(["--out", str(out)]) == 0
    assert out.read_bytes() == first  # same inputs, same file
    steps.fields = make_fields(n=30)
    assert run_all.main(["--out", str(out)]) == 0
    assert len(load_fields(out)) == 30
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("bad", ["soils", "soil,rain", "fields"])
def test_allow_missing_rejects_unknown_steps(steps, tmp_path, bad, capsys):
    with pytest.raises(SystemExit) as exit_info:
        run_all.main(["--out", str(tmp_path / "f.parquet"), "--allow-missing", bad])
    assert exit_info.value.code == 2
    assert not (tmp_path / "f.parquet").exists()


def test_parse_allow_missing():
    assert run_all.parse_allow_missing("soil, Slope") == {"soil", "slope"}
    assert run_all.parse_allow_missing("") == frozenset()


@pytest.mark.parametrize("args", [["--max-fields", "0"], ["--min-area-ha", "0"], ["--fields-source", "magic"]])
def test_bad_arguments(steps, tmp_path, args):
    with pytest.raises(SystemExit) as exit_info:
        run_all.main(["--out", str(tmp_path / "f.parquet"), *args])
    assert exit_info.value.code == 2
    assert steps.calls == []


# --------------------------------------------------------------------------------------------
# failure policy
# --------------------------------------------------------------------------------------------
def write_previous(out: Path) -> bytes:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"the previous good file")
    return out.read_bytes()


@pytest.mark.parametrize(
    "step, error, column",
    [
        ("soil", SoilDataError("Soil Data Access failed 5 time(s) in a row"), "organic_matter_pct"),
        ("slope", DemUnavailable([{"source": "3dep13", "error": "host unreachable"}]), "slope_pct"),
        ("soil", ZeroDivisionError("a bug, not the network"), "organic_matter_pct"),
    ],
)
def test_soil_or_slope_failure_is_fatal_by_default(steps, tmp_path, capsys, step, error, column):
    setattr(steps, f"{step}_error", error)
    out = tmp_path / "fields.parquet"
    previous = write_previous(out)
    assert run_all.main(["--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert out.read_bytes() == previous  # untouched
    assert leftovers(tmp_path) == []
    assert not run_all.side_paths(out)[0].exists()
    assert f"FAILED at step '{step}'" in captured.err
    assert f"--allow-missing {step}" in captured.err and column in captured.err
    assert "NOT written" in captured.err and "is unchanged" in captured.err
    assert str(error) in captured.err


def test_failure_without_a_previous_file_writes_nothing(steps, tmp_path, capsys):
    steps.soil_error = SoilDataError("down")
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 1
    assert list(tmp_path.iterdir()) == []
    assert "does not exist" in capsys.readouterr().err


def test_allow_missing_soil_writes_nulls_loudly(steps, tmp_path, capsys):
    steps.soil_error = SoilDataError("Soil Data Access failed 5 time(s) in a row")
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out), "--allow-missing", "soil"]) == 0
    printed = capsys.readouterr().out
    table = load_fields(out)
    assert table["organic_matter_pct"].isna().all() and table["organic_matter_pct"].dtype == np.float64
    assert table["slope_pct"].notna().all()
    assert printed.count("WARNING") >= 2 and "NULL FOR EVERY FIELD" in printed
    assert "WARN  nulls" in printed and "SOIL STEP FAILED" in printed
    prov = json.loads(run_all.side_paths(out)[0].read_text())
    assert "Soil Data Access failed" in prov["columns_allowed_missing"]["soil"]
    assert any("soil step failed" in f for f in prov["fallbacks_taken"])
    assert prov["steps"]["soil"]["allowed_missing"] is True


def test_allow_missing_slope_and_both(steps, tmp_path):
    steps.slope_error = DemUnavailable([{"source": "3dep13", "error": "host unreachable"}])
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out), "--allow-missing", "slope"]) == 0
    table = load_fields(out)
    assert table["slope_pct"].isna().all() and table["organic_matter_pct"].notna().all()

    steps.soil_error = SoilDataError("down")
    assert run_all.main(["--out", str(out), "--allow-missing", "soil"]) == 1  # slope is not allowed to be missing
    assert load_fields(out)["organic_matter_pct"].notna().all()  # the earlier file is still there
    assert run_all.main(["--out", str(out), "--allow-missing", "soil,slope"]) == 0
    table = load_fields(out)
    assert table["slope_pct"].isna().all() and table["organic_matter_pct"].isna().all()


def test_allow_missing_does_not_hide_values_when_the_step_works(steps, tmp_path):
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out), "--allow-missing", "soil,slope"]) == 0
    table = load_fields(out)
    assert table["organic_matter_pct"].notna().all() and table["slope_pct"].notna().all()


def test_a_step_that_returns_nothing_counts_as_failed(steps, tmp_path, capsys):
    steps.slope_result = lambda fields: pd.Series(np.nan, index=fields.index, name="slope_pct")
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 1
    assert "no value for any" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    "result",
    [
        lambda fields: pd.Series(1.5, index=fields.index[::-1]),  # same labels, other order
        lambda fields: pd.Series(1.5, index=fields.index + 1),
        lambda fields: pd.Series(1.5, index=fields.index[:-1]),
        lambda fields: np.full(len(fields), 1.5),  # not a Series
        lambda fields: pd.Series(np.inf, index=fields.index),
    ],
)
def test_a_misaligned_or_malformed_column_is_never_written(steps, tmp_path, result):
    steps.soil_result = result
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 1
    assert not out.exists()


def test_partial_nulls_are_kept_and_reported(steps, tmp_path, capsys):
    def some_water(fields):
        values = pd.Series([om_for(f) for f in fields["field_id"]], index=fields.index)
        values[fields["field_id"].isin(["KS-000003", "KS-000004"]).to_numpy()] = np.nan
        return values

    steps.soil_result = some_water
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 0
    table = load_fields(out)
    assert table.loc[table["organic_matter_pct"].isna(), "field_id"].tolist() == ["KS-000003", "KS-000004"]
    assert "WARN  nulls" in capsys.readouterr().out  # 2 of 40 is more than 1 %


def test_fields_failure_is_fatal_whatever_is_allowed(steps, tmp_path, capsys):
    steps.fields_error = RuntimeError("no CDL raster could be downloaded for any year")
    out = tmp_path / "fields.parquet"
    previous = write_previous(out)
    assert run_all.main(["--out", str(out), "--allow-missing", "soil,slope"]) == 1
    err = capsys.readouterr().err
    assert "FAILED at step 'fields'" in err and "no CDL raster" in err
    assert out.read_bytes() == previous


def spoil(fields: gpd.GeoDataFrame, how: str) -> gpd.GeoDataFrame:
    attrs = dict(fields.attrs)
    fields = fields.copy()
    if how == "duplicate id":
        fields.loc[1, "field_id"] = fields.loc[0, "field_id"]
    elif how == "bad id":
        fields.loc[0, "field_id"] = "Kansas-1"
    elif how == "multipolygon":
        geom = fields.geometry.iloc[0]
        fields.loc[[0], "geometry"] = gpd.GeoSeries([MultiPolygon([geom])], index=[0], crs=fields.crs)
    elif how == "history is not json":
        fields.loc[0, "crop_history"] = "corn, corn, corn"
    elif how == "uppercase crop":
        fields.loc[0, "crop_history"] = json.dumps({"2008": "Corn"})
    fields.attrs = attrs
    return fields


@pytest.mark.parametrize("how", ["duplicate id", "bad id", "multipolygon", "history is not json", "uppercase crop"])
def test_a_table_that_breaks_the_contract_is_not_written(steps, tmp_path, capsys, how):
    steps.fields = spoil(steps.fields, how)
    with pytest.raises(SchemaError):  # the fixture really is invalid
        probe = steps.fields.copy()
        probe["organic_matter_pct"], probe["slope_pct"], probe["area_ha"] = 1.0, 1.0, 36.0
        validate_fields(probe[COLUMNS])
    out = tmp_path / "fields.parquet"
    previous = write_previous(out)
    assert run_all.main(["--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "FAILED at step 'validate'" in err and "schema.md" in err
    assert out.read_bytes() == previous
    assert leftovers(tmp_path) == []


def test_side_file_failure_does_not_fail_the_run(steps, tmp_path, monkeypatch, capsys):
    def refuse(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(run_all, "write_text_atomic", refuse)
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 0
    assert len(load_fields(out)) == 40
    assert "could not write the side files" in capsys.readouterr().out


def test_a_crash_while_reporting_does_not_fail_a_written_file(steps, tmp_path, monkeypatch, capsys):
    def crash(*args, **kwargs):
        raise KeyError("a bug in a sanity check")

    monkeypatch.setattr(run_all, "sanity_checks", crash)
    out = tmp_path / "fields.parquet"
    assert run_all.main(["--out", str(out)]) == 0  # the file is written and valid: exit code 0
    printed = capsys.readouterr().out
    assert len(load_fields(out)) == 40
    assert "the sanity checks crashed" in printed and "WARN  sanity checks  COULD NOT BE COMPLETED" in printed
    prov = json.loads(run_all.side_paths(out)[0].read_text())
    assert prov["sanity_checks"][0]["status"] == "WARN" and prov["output"]["fields"] == 40

    monkeypatch.undo()
    monkeypatch.setattr(run_all, "build_fields", steps.build_fields)
    monkeypatch.setattr(run_all, "organic_matter_pct", steps.organic_matter_pct)
    monkeypatch.setattr(run_all, "mean_slope_pct", steps.mean_slope_pct)
    monkeypatch.setattr(run_all, "county_boundary", lambda fips=None: county_frame())
    monkeypatch.setattr(run_all, "package_versions", crash)  # building the provenance fails
    out2 = tmp_path / "second.parquet"
    assert run_all.main(["--out", str(out2)]) == 0
    assert len(load_fields(out2)) == 40 and not run_all.side_paths(out2)[0].exists()
    assert "could not write the side files" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------
# assembly and the atomic write
# --------------------------------------------------------------------------------------------
def healthy_table(histories=None, n: int = COLS * ROWS) -> gpd.GeoDataFrame:
    fields = make_fields(histories, n)
    om = np.array([om_for(f) for f in fields["field_id"]])
    slope = np.array([slope_for(f) for f in fields["field_id"]])
    table, _ = run_all.assemble(fields, om, slope, run_all.equal_area_ha(fields.geometry))
    validate_fields(table)
    return table


def county_dict() -> dict:
    geom = county_frame().to_crs(CRS_M).geometry.iloc[0]
    return {"geometry": geom, "area_km2": geom.area / 1e6, "source": "test"}


def test_assemble_orders_rounds_and_types():
    fields = make_fields()
    n = len(fields)
    om = np.linspace(1.0, 2.0, n) + 0.00049
    slope = np.linspace(0.5, 3.0, n) + 0.0049
    area = run_all.equal_area_ha(fields.geometry)
    table, order = run_all.assemble(fields, om, slope, area)
    assert list(table.columns) == COLUMNS and isinstance(table, gpd.GeoDataFrame)
    assert table["field_id"].is_monotonic_increasing
    assert fields["field_id"].to_numpy()[order].tolist() == table["field_id"].tolist()
    assert np.array_equal(table["organic_matter_pct"].to_numpy(), np.round(om[order], 3))
    assert np.array_equal(table["slope_pct"].to_numpy(), np.round(slope[order], 2))
    assert np.array_equal(table["area_ha"].to_numpy(), np.round(area[order], 2))
    assert table.geometry.geom_equals_exact(fields.geometry.iloc[order].reset_index(drop=True), 0).all()
    assert isinstance(table.index, pd.RangeIndex)
    validate_fields(table)

    with pytest.raises(ValueError, match="values for"):
        run_all.assemble(fields, om[:-1], slope, area)


def test_assemble_reprojects_and_keeps_tiny_areas_positive():
    fields = make_fields(n=4)
    metric = fields.to_crs(CRS_M)
    metric["geometry"] = metric.geometry.scale(0.001, 0.001)  # 0.6 m squares: 0.000036 ha
    area = run_all.equal_area_ha(metric.geometry)
    assert area.max() < 0.005
    table, _ = run_all.assemble(metric, np.ones(4), np.ones(4), area)
    assert table.crs.to_epsg() == 4326
    assert (table["area_ha"] > 0).all()  # rounding to 0.01 ha must not produce the forbidden 0
    validate_fields(table)


def test_equal_area_matches_known_squares():
    area = run_all.equal_area_ha(make_fields().geometry)
    assert area == pytest.approx(np.full(40, FIELD_HA), abs=1e-3)
    with pytest.raises(ValueError):
        run_all.equal_area_ha(gpd.GeoSeries([box(0, 0, 1, 1)]))


def test_write_fields_atomic_round_trip(tmp_path):
    table = healthy_table()
    out = tmp_path / "deep" / "fields.parquet"
    run_all.write_fields_atomic(table, out)
    back = load_fields(out)
    assert run_all.table_differences(table, back) == []
    assert leftovers(out.parent) == []
    geo = json.loads(pq.read_schema(out).metadata[b"geo"])  # it is GeoParquet, not plain parquet
    assert geo["primary_column"] == "geometry" and geo["columns"]["geometry"]["encoding"] == "WKB"
    assert pq.read_schema(out).names == COLUMNS  # no index column sneaks in


def test_write_failure_keeps_the_previous_file(tmp_path, monkeypatch):
    table = healthy_table()
    out = tmp_path / "fields.parquet"
    run_all.write_fields_atomic(table, out)
    good = out.read_bytes()

    def half_written(self, path, *args, **kwargs):
        Path(path).write_bytes(b"PAR1 truncated")
        raise OSError("No space left on device")

    monkeypatch.setattr(gpd.GeoDataFrame, "to_parquet", half_written)
    with pytest.raises(OSError, match="No space left"):
        run_all.write_fields_atomic(healthy_table(n=10), out)
    assert out.read_bytes() == good
    assert leftovers(tmp_path) == []


def test_a_file_that_does_not_read_back_is_not_installed(tmp_path, monkeypatch):
    table = healthy_table()
    out = tmp_path / "fields.parquet"
    run_all.write_fields_atomic(table, out)
    good = out.read_bytes()

    real_load = run_all.load_fields

    def altered(path):
        back = real_load(path)
        back.loc[3, "slope_pct"] += 1.0
        return back

    monkeypatch.setattr(run_all, "load_fields", altered)
    with pytest.raises(IOError, match="did not read back as written: slope_pct differs"):
        run_all.write_fields_atomic(table, out)
    assert out.read_bytes() == good and leftovers(tmp_path) == []

    def unreadable(path):
        raise SchemaError(["could not read as GeoParquet"], str(path))

    monkeypatch.setattr(run_all, "load_fields", unreadable)
    with pytest.raises(SchemaError):
        run_all.write_fields_atomic(table, out)
    assert out.read_bytes() == good and leftovers(tmp_path) == []


def test_run_reports_a_write_failure(steps, tmp_path, monkeypatch, capsys):
    out = tmp_path / "fields.parquet"
    previous = write_previous(out)

    def refuse(self, path, *args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(gpd.GeoDataFrame, "to_parquet", refuse)
    assert run_all.main(["--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "FAILED at step 'write'" in err and "read-only file system" in err
    assert out.read_bytes() == previous


def test_table_differences_spots_every_kind_of_change():
    table = healthy_table()
    assert run_all.table_differences(table, table.copy()) == []
    changed = table.copy()
    changed.loc[0, "crop_history"] = json.dumps({"2008": "corn"})
    assert run_all.table_differences(table, changed) == ["crop_history differs"]
    changed = table.copy()
    changed.loc[0, "area_ha"] = np.nan
    assert run_all.table_differences(table, changed) == ["area_ha differs"]
    changed = table.copy()
    changed["slope_pct"] = changed["slope_pct"].round().astype(int)
    assert run_all.table_differences(table, changed) == ["slope_pct came back as int64"]
    changed = table.copy()
    changed["geometry"] = changed.geometry.translate(1e-9, 0)
    assert run_all.table_differences(table, changed) == ["geometry differs in 40 row(s)"]
    assert run_all.table_differences(table, table.iloc[:-1]) == ["39 rows, expected 40"]
    assert "columns are" in run_all.table_differences(table, table.drop(columns="slope_pct"))[0]
    both_null = table.copy()
    both_null["organic_matter_pct"] = np.nan
    assert run_all.table_differences(both_null, both_null.copy()) == []  # NaN equals NaN here


def test_jsonable():
    obj = {
        1: np.float64("nan"), "inf": float("inf"), "i": np.int64(3), "b": np.bool_(True), "a": np.array([1.5, np.nan]),
        "t": (1, 2), "s": {"x"}, "p": Path("data/x"), "n": None, "f": 0.25, "ts": pd.Timestamp("2026-11-14T00:00:00Z"),
    }  # fmt: skip
    out = run_all.jsonable(obj)
    assert out == {"1": None, "inf": None, "i": 3, "b": True, "a": [1.5, None], "t": [1, 2], "s": ["x"],
                   "p": "data/x", "n": None, "f": 0.25, "ts": "2026-11-14T00:00:00+00:00"}  # fmt: skip
    json.dumps(out, allow_nan=False)


# --------------------------------------------------------------------------------------------
# sanity checks
# --------------------------------------------------------------------------------------------
def checks_for(table, **kwargs):
    kwargs.setdefault("county", county_dict())
    return run_all.sanity_checks(table, **kwargs)[0]


def test_sanity_checks_pass_on_plausible_fields():
    checks, numbers = run_all.sanity_checks(healthy_table(), county=county_dict(), context={"fallbacks": []})
    assert [c.name for c in checks if not c.ok] == []
    assert {c.status for c in checks} == {"PASS"}
    names = [c.name for c in checks]
    for expected in ("core crops", "corn / wheat / sorghum", "non-crop classes", "years per field", "fields that rotate",
                     "longest same-crop streak", "per-year class mix", "non-crop share by year", "organic matter", "slope",
                     "nulls", "duplicate ids", "geometry", "overlapping fields", "field area", "area recomputation",
                     "total area vs county", "inside the county", "data sources"):  # fmt: skip
        assert expected in names
    assert numbers["share_of_county"] == pytest.approx(40 * FIELD_HA / 100 / (COLS * ROWS * PITCH * PITCH / 1e6), rel=1e-3)
    assert numbers["overlaps"]["pairs_beyond_sliver"] == 0
    text = run_all.format_checks(checks)
    assert f"sanity: {len(checks)} PASS, 0 WARN" in text and text.count("\n  PASS  ") == len(checks)


def test_sanity_all_grass():
    checks = checks_for(healthy_table([rotation(["grass/pasture"])] * 40))
    assert status(checks, "core crops") == "WARN"
    assert status(checks, "corn / wheat / sorghum") == "WARN"
    assert status(checks, "non-crop classes") == "WARN"
    assert status(checks, "fields that rotate") == "WARN"
    assert status(checks, "longest same-crop streak") == "WARN"
    assert status(checks, "per-year class mix") == "WARN"  # every year is one class
    assert status(checks, "geometry") == "PASS"


def test_sanity_a_year_where_everything_flips_to_one_class():
    histories = [plausible_history(i) for i in range(40)]
    for h in histories:
        h["2015"] = "sorghum"  # a broken 2015 raster
    checks = checks_for(healthy_table(histories))
    broken = next(c for c in checks if c.name == "per-year class mix")
    assert broken.status == "WARN" and "2015" in broken.detail and "sorghum 100%" in broken.detail
    assert status(checks, "core crops") == "PASS"


def test_sanity_non_crop_jump_in_one_year():
    histories = [plausible_history(i) for i in range(40)]
    for h in histories[:4]:
        h["2024"] = "grass/pasture"  # 10 % of the fields turn to grass in the last year only
    checks = checks_for(healthy_table(histories))
    jump = next(c for c in checks if c.name == "non-crop share by year")
    assert jump.status == "WARN" and "JUMP in 2024 (0.0% -> 10.0%)" in jump.detail
    assert status(checks, "per-year class mix") == "PASS"
    assert status(checks, "non-crop classes") == "PASS"  # still under 10 % of all field-years


def test_sanity_missing_years():
    histories = [plausible_history(i) for i in range(40)]
    for h in histories:
        del h["2013"]
    checks = checks_for(healthy_table(histories))
    years = next(c for c in checks if c.name == "years per field")
    assert years.status == "WARN" and "MISSING FROM EVERY FIELD: 2013" in years.detail

    histories = [plausible_history(i) for i in range(40)]
    for year in ("2008", "2009", "2010"):
        del histories[0][year]  # one field with only 14 years
    assert status(checks_for(healthy_table(histories)), "years per field") == "WARN"

    histories = [plausible_history(i) for i in range(40)]
    assert status(checks_for(healthy_table(histories), expected_years=range(2008, 2026)), "years per field") == "WARN"


def test_sanity_monocultures():
    checks = checks_for(healthy_table([rotation([crop]) for crop in ["corn", "winter wheat", "sorghum", "alfalfa"] * 10]))
    assert status(checks, "fields that rotate") == "WARN"
    streak = next(c for c in checks if c.name == "longest same-crop streak")
    assert streak.status == "WARN" and "40 field(s) (100.00%) have one class in every year" in streak.detail
    assert status(checks, "core crops") == "PASS"


def test_sanity_soil_and_slope_ranges():
    table = healthy_table()
    table.loc[:4, "organic_matter_pct"] = 12.0  # 5 of 40 outside 0.3-4 %
    table.loc[:9, "slope_pct"] = 9.0  # 25 % of the fields steeper than 5 %
    checks = checks_for(table)
    assert status(checks, "organic matter") == "WARN" and status(checks, "slope") == "WARN"
    table = healthy_table()
    table["organic_matter_pct"] = np.nan
    table["slope_pct"] = np.nan
    checks = checks_for(table, context={"missing": {"soil": "SDA down", "slope": "USGS down"}})
    assert status(checks, "organic matter") == "WARN" and status(checks, "slope") == "WARN"
    nulls = next(c for c in checks if c.name == "nulls")
    assert nulls.status == "WARN" and "SOIL STEP FAILED" in nulls.detail and "USGS down" in nulls.detail


def test_sanity_overlapping_fields():
    table = healthy_table()
    metric = table.geometry.to_crs(CRS_M)
    grown = metric.iloc[[5]].buffer(250, join_style="mitre")  # 250 m over the 200 m gap: overlaps its neighbours
    table.loc[[5], "geometry"] = grown.to_crs("EPSG:4326").values
    table["area_ha"] = np.round(run_all.equal_area_ha(table.geometry), 2)
    checks, numbers = run_all.sanity_checks(table, county=county_dict())
    overlap = next(c for c in checks if c.name == "overlapping fields")
    assert overlap.status == "WARN" and "KS-000006" in overlap.detail
    assert numbers["overlaps"]["pairs_beyond_sliver"] >= 2 and numbers["overlaps"]["largest_m2"] > 10_000


def test_sanity_touching_fields_do_not_count_as_overlapping():
    geoms = np.array([box(0, 0, 600, 600), box(600, 0, 1200, 600), box(0, 600, 600, 1200)], dtype=object)  # shared edges
    stats = run_all.overlap_stats(geoms)
    assert stats["pairs_checked"] == 3 and stats["pairs_overlapping"] == 0 and stats["largest_m2"] == 0
    geoms = np.array([box(0, 0, 600, 600), box(599.9, 0, 1200, 600)], dtype=object)  # a 60 m2 sliver
    stats = run_all.overlap_stats(geoms)
    assert stats["pairs_overlapping"] == 1 and stats["pairs_beyond_sliver"] == 0
    geoms = np.array([box(0, 0, 600, 600), box(100, 100, 200, 200)], dtype=object)  # one inside the other
    assert run_all.overlap_stats(geoms)["pairs_beyond_sliver"] == 1
    assert run_all.overlap_stats(geoms[:1])["pairs_checked"] == 0


def test_sanity_area_checks():
    table = healthy_table()
    assert status(checks_for(table, min_area_ha=40.0), "field area") == "WARN"  # 36 ha fields under a 40 ha minimum
    assert status(checks_for(table, min_area_ha=36.0), "field area") == "PASS"  # equal within rounding

    stale = table.copy()
    stale["area_ha"] = stale["area_ha"] + 1.0
    assert status(checks_for(stale), "area recomputation") == "WARN"
    drifted = checks_for(table, context={"build_area_ha": np.full(40, FIELD_HA + 0.5)})
    assert status(drifted, "area recomputation") == "WARN"
    assert status(checks_for(table, context={"build_area_ha": np.full(40, FIELD_HA)}), "area recomputation") == "PASS"

    giant = table.copy()
    giant.loc[[0], "geometry"] = gpd.GeoSeries([box(X0, Y0 - 5000, X0 + 4000, Y0 - 1000)], index=[0], crs=CRS_M).to_crs("EPSG:4326").values
    giant["area_ha"] = np.round(run_all.equal_area_ha(giant.geometry), 2)
    checks = checks_for(giant)
    assert status(checks, "field area") == "WARN"  # 1,600 ha
    assert status(checks, "inside the county") == "WARN"  # and it lies outside the outline


def test_sanity_county_checks():
    table = healthy_table()
    small = dict(county_dict(), area_km2=5.0)
    assert status(checks_for(table, county=small), "total area vs county") == "WARN"  # fields "cover" 288 %
    huge = dict(county_dict(), area_km2=3374.0)
    assert status(checks_for(table, county=huge), "total area vs county") == "WARN"  # 0.4 % of the county
    # a capped run holds a sample of the fields: the share is judged for the whole set
    capped = {"fields_provenance": {"counts": {"fields_before_max_fields": 400}}}
    ten_times = dict(county_dict(), area_km2=county_dict()["area_km2"] * 10)
    assert status(checks_for(table, county=ten_times), "total area vs county") == "WARN"  # 5.6 %
    sampled = next(c for c in checks_for(table, county=ten_times, context=capped) if c.name == "total area vs county")
    assert sampled.status == "PASS" and "--max-fields kept 40 of 400 fields" in sampled.detail and "56.2%" in sampled.detail
    not_capped = {"fields_provenance": {"counts": {"fields_before_max_fields": 40}}}
    assert "--max-fields" not in next(c for c in checks_for(table, context=not_capped) if c.name == "total area vs county").detail
    without = run_all.sanity_checks(table, county=None)[0]
    assert status(without, "inside the county") == "WARN" and "not checked" in next(c for c in without if c.name == "inside the county").detail
    assert "nominal area" in next(c for c in without if c.name == "total area vs county").detail


def test_inside_share():
    outline = box(0, 0, 1000, 1000)
    geoms = np.array([box(100, 100, 200, 200), box(900, 0, 1100, 100), box(2000, 0, 2100, 100)], dtype=object)
    share, reach = run_all.inside_share(geoms, outline)
    assert share.tolist() == [1.0, 0.5, 0.0]
    assert reach == 1100.0  # the far corner of the third box
    share, reach = run_all.inside_share(geoms[:1], outline)
    assert share.tolist() == [1.0] and reach == 0.0


def test_sanity_context_checks():
    table = healthy_table()
    prov = make_fields().attrs["provenance"]
    good = run_all.sanity_checks(table, county=county_dict(), context={"fields_provenance": prov, "fallbacks": []})[0]
    assert status(good, "CSB vs raster") == "PASS" and status(good, "data sources") == "PASS"

    prov["agreement_with_csb_attributes"]["final_fields"]["per_year"]["2019"]["rate"] = 0.80
    prov["agreement_with_csb_attributes"]["final_fields"]["overall"]["rate"] = 0.90
    bad = run_all.sanity_checks(table, county=county_dict(), context={
        "fields_provenance": prov, "fallbacks": ["slope: the first-choice DEM failed; srtm was used instead"]})[0]  # fmt: skip
    agreement = next(c for c in bad if c.name == "CSB vs raster")
    assert agreement.status == "WARN" and "lowest year 2019: 80.00%" in agreement.detail
    sources = next(c for c in bad if c.name == "data sources")
    assert sources.status == "WARN" and "srtm" in sources.detail
    assert "CSB vs raster" not in [c.name for c in checks_for(table)]  # no CSB cross-check for segmented fields


def test_sources_and_fallbacks():
    prov = make_fields().attrs["provenance"]
    slope = {"source": "srtm", "fallback_used": True, "urls": ["u"], "cache_units_fetched_this_run": 4,
             "attempts": [{"source": "3dep13", "ok": False, "error": "timeout"}, {"source": "srtm", "ok": True, "error": None}]}  # fmt: skip
    prov["fallback_reason"] = "RuntimeError: no CSB release could be used"
    prov["source"] = "cdl-segment"
    prov["cdl"]["years_missing"] = [2011]
    prov["cdl"]["per_year"]["2012"].update(route="GetCDLFile", downloaded_via="GetCDLFile")
    sources, fallbacks, downloads = run_all.sources_and_fallbacks(prov, {}, slope, {"soil": "SDA down"})
    text = " | ".join(fallbacks)
    for needle in ("CSB failed", "no raster for [2011]", "GetCDLFile", "soil step failed", "3dep13: timeout", "srtm was used"):
        assert needle in text
    assert downloads["cdl_years_downloaded"] == ["2012"] and downloads["dem_units_fetched"] == 4
    assert "ssurgo_http_requests" not in downloads
    assert any(s.get("method", "").startswith("segmented") for s in sources)
    assert "DEM: 4 block(s) fetched" in run_all.format_downloads(downloads)


def test_history_helpers():
    hist = run_all.history_frame([
        json.dumps({"2008": "corn", "2009": "corn", "2010": "fallow", "2011": "corn"}),
        json.dumps({"2008": "alfalfa", "2010": "alfalfa", "2011": "alfalfa"}),  # 2009 missing
        json.dumps({"2009": "winter wheat"}),
    ])  # fmt: skip
    assert list(hist.columns) == ["2008", "2009", "2010", "2011"]
    assert run_all.longest_streaks(hist).tolist() == [2, 2, 1]  # the gap in 2009 ends the alfalfa run
    per_year = run_all.year_table(hist)
    assert per_year.loc["corn", "2008"] == 0.5 and per_year.loc["corn", "2009"] == 0.5
    assert per_year.loc["alfalfa", "2010"] == 0.5 and per_year.loc["fallow", "2010"] == 0.5
    assert np.allclose(per_year.sum(axis=0), 1.0)
    groups = run_all.year_groups(run_all.year_table(run_all.history_frame([
        json.dumps({"2008": "corn"}), json.dumps({"2008": "triticale"}), json.dumps({"2008": "grass/pasture"}),
        json.dumps({"2008": "developed"}),
    ])))  # fmt: skip
    assert groups["2008"].to_dict() == {
        "corn": 0.25, "soybeans": 0.0, "winter wheat": 0.0, "sorghum": 0.0, "alfalfa": 0.0, "fallow": 0.0,
        "grass/pasture": 0.25, "other crops": 0.25, "other non-crop": 0.25,
    }  # fmt: skip
    assert "2008" in run_all.format_year_table(groups)


def test_format_samples():
    table = healthy_table()
    table.loc[0, "organic_matter_pct"] = np.nan
    text = run_all.format_samples(table, 40)
    lines = text.splitlines()
    assert len(lines) == 42 and "KS-000001" in lines[2] and "null" in lines[2]
    assert "WSFWSFWSFWSFWSFWS" in lines[2]
    assert run_all.format_samples(table, 0) == ""
    assert run_all.format_samples(table, 3) == run_all.format_samples(table, 3)  # seeded


def test_county_outline_survives_a_missing_boundary(monkeypatch, capsys):
    def nothing(fips=None):
        raise RuntimeError("could not get the county boundary")

    monkeypatch.setattr(run_all, "county_boundary", nothing)
    assert run_all.county_outline() is None
    assert "county outline unavailable" in capsys.readouterr().out
    monkeypatch.setattr(run_all, "county_boundary", lambda fips=None: county_frame())
    outline = run_all.county_outline()
    assert outline["area_km2"] == pytest.approx(COLS * ROWS * PITCH * PITCH / 1e6, rel=1e-6)


# --------------------------------------------------------------------------------------------
# both ways of starting it
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("command", [["pipeline/run_all.py"], ["-m", "pipeline.run_all"]])
def test_starts_as_a_script_and_as_a_module(command):
    done = subprocess.run([sys.executable, *command, "--help"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "--allow-missing" in done.stdout and "--max-fields" in done.stdout and "--fields-source" in done.stdout
