"""pipeline/nass.py: filtering the bulk file, mapping NASS names, gap filling. No network."""
import io

import pandas as pd
import pytest

from pipeline import nass

HEADER = [
    "SOURCE_DESC", "SECTOR_DESC", "GROUP_DESC", "COMMODITY_DESC", "CLASS_DESC", "PRODN_PRACTICE_DESC",
    "UTIL_PRACTICE_DESC", "STATISTICCAT_DESC", "UNIT_DESC", "SHORT_DESC", "DOMAIN_DESC", "DOMAINCAT_DESC",
    "AGG_LEVEL_DESC", "STATE_ANSI", "STATE_FIPS_CODE", "STATE_ALPHA", "STATE_NAME", "ASD_CODE", "ASD_DESC",
    "COUNTY_ANSI", "COUNTY_CODE", "COUNTY_NAME", "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE",
]
ALL, IRR, DRY = "ALL PRODUCTION PRACTICES", "IRRIGATED", "NON-IRRIGATED"
ANY_UTIL = "ALL UTILIZATION PRACTICES"


def row(commodity, klass, practice, util, statcat, unit, short, level, year, value, *, state="KS", asd="30",
        county="055", source="SURVEY", period="YEAR", freq="ANNUAL", domain="TOTAL"):
    asd = "" if level == "STATE" else asd
    county = county if level == "COUNTY" else ""
    return dict(zip(HEADER, [
        source, "CROPS", "FIELD CROPS", commodity, klass, practice, util, statcat, unit, short, domain, "NOT SPECIFIED",
        level, "20", "20", state, "KANSAS", asd, "SOUTHWEST", county, county, "FINNEY", str(year), freq, period, value,
    ]))


def corn_yield(level, year, value, practice=ALL, **kw):
    short = "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE" if practice == ALL else f"CORN, GRAIN, {practice} - YIELD, MEASURED IN BU / ACRE"
    return row("CORN", "ALL CLASSES", practice, "GRAIN", "YIELD", "BU / ACRE", short, level, year, value, **kw)


def frame(rows):
    return pd.DataFrame(rows, columns=HEADER)


def lookup(table, **keys):
    sub = table
    for k, v in keys.items():
        sub = sub[sub[k] == v]
    assert len(sub) == 1, f"{keys}: {len(sub)} rows"
    return sub.iloc[0]


def test_current_bulk_url_picks_the_crops_file():
    html = (
        '<a href="/sites/default/files/datasets/qs.animals_products_20261114.txt.gz">a</a>'
        '<a href="/sites/default/files/datasets/qs.crops_20261113.txt.gz">old</a>'
        '<a href="/sites/default/files/datasets/qs.crops_20261114.txt.gz">new</a>'
    )
    assert nass.current_bulk_url(html) == "https://www.nass.usda.gov/sites/default/files/datasets/qs.crops_20261114.txt.gz"
    with pytest.raises(nass.NassError):
        nass.current_bulk_url("<html>nothing here</html>")


def test_filter_lines_keeps_only_the_rows_we_need():
    rows = [
        corn_yield("COUNTY", 2010, "180"),
        corn_yield("COUNTY", 2010, "170", county="057"),  # another county
        corn_yield("AGRICULTURAL DISTRICT", 2010, "175"),
        corn_yield("AGRICULTURAL DISTRICT", 2010, "150", asd="10"),  # another district
        corn_yield("STATE", 2010, "125"),
        corn_yield("STATE", 2010, "140", state="NE"),  # another state
        corn_yield("STATE", 2010, "126", source="CENSUS"),
        corn_yield("STATE", 1985, "110"),  # too old
        row("BARLEY", "ALL CLASSES", ALL, "GRAIN", "YIELD", "BU / ACRE", "BARLEY - YIELD", "STATE", 2010, "50"),
        row("CORN", "ALL CLASSES", ALL, "GRAIN", "PRODUCTION", "BU", "CORN, GRAIN - PRODUCTION", "STATE", 2010, "5"),
    ]
    text = "\t".join(HEADER) + "\n" + "".join("\t".join(r[h] for h in HEADER) + "\n" for r in rows) + "broken line\tKS\t\n"
    out = io.StringIO()
    stats = nass.filter_lines(iter(text.splitlines(keepends=True)), out)
    assert stats == {"lines_read": 11, "rows_kept": 3}
    kept = pd.read_csv(io.StringIO(out.getvalue()), sep="\t", dtype=str, keep_default_na=False)
    assert sorted(kept["AGG_LEVEL_DESC"]) == ["AGRICULTURAL DISTRICT", "COUNTY", "STATE"]
    assert set(kept["STATE_ALPHA"]) == {"KS"}


def test_parse_value():
    assert nass.parse_value("1,234.5") == 1234.5
    assert nass.parse_value(" 42 ") == 42.0
    assert nass.parse_value("(D)") is None
    assert nass.parse_value("") is None


def test_names_units_and_practices_map_to_ours():
    raw = frame([
        corn_yield("COUNTY", 2010, "180.5", practice=IRR),
        corn_yield("COUNTY", 2010, "55", practice=DRY),
        row("CORN", "ALL CLASSES", ALL, "SILAGE", "YIELD", "TONS / ACRE", "CORN, SILAGE - YIELD", "COUNTY", 2010, "20"),
        row("CORN", "ALL CLASSES", ALL, "GRAIN", "YIELD", "BU / NET PLANTED ACRE", "CORN, GRAIN - YIELD, NET", "COUNTY", 2010, "99"),
        row("SORGHUM", "ALL CLASSES", ALL, "GRAIN", "YIELD", "BU / ACRE", "SORGHUM, GRAIN - YIELD, MEASURED IN BU / ACRE", "COUNTY", 2010, "70"),
        row("WHEAT", "WINTER", DRY, "GRAIN", "YIELD", "BU / ACRE", "WHEAT, WINTER, NON-IRRIGATED - YIELD, MEASURED IN BU / ACRE", "COUNTY", 2010, "35"),
        row("WHEAT", "SPRING, (EXCL DURUM)", ALL, "GRAIN", "YIELD", "BU / ACRE", "WHEAT, SPRING - YIELD", "STATE", 2010, "30"),
        row("SOYBEANS", "ALL CLASSES", ALL, ANY_UTIL, "YIELD", "BU / ACRE", "SOYBEANS - YIELD, MEASURED IN BU / ACRE", "COUNTY", 2010, "50"),
        row("HAY", "ALFALFA", ALL, ANY_UTIL, "YIELD", "TONS / ACRE", "HAY, ALFALFA - YIELD, MEASURED IN TONS / ACRE", "COUNTY", 2010, "5.2"),
        row("HAY", "(EXCL ALFALFA)", ALL, ANY_UTIL, "YIELD", "TONS / ACRE", "HAY, (EXCL ALFALFA) - YIELD", "COUNTY", 2010, "1.5"),
        corn_yield("COUNTY", 2011, "150", period="YEAR - AUG FORECAST"),  # forecasts are not results
        corn_yield("COUNTY", 2012, "(D)"),  # withheld
    ])
    table = nass.tidy(raw)
    assert nass.check_nass(table) == []
    assert len(table) == 6
    got = lookup(table, crop="corn", practice="irrigated")
    assert (got["value"], got["unit"], got["level"], got["stat"]) == (180.5, "bu/acre", "county", "yield")
    assert got["source_desc"] == "CORN, GRAIN, IRRIGATED - YIELD, MEASURED IN BU / ACRE"
    assert lookup(table, crop="corn", practice="dryland")["value"] == 55
    assert lookup(table, crop="winter wheat")["practice"] == "dryland"
    assert lookup(table, crop="alfalfa")["unit"] == "tons/acre"
    assert set(table["crop"]) == {"corn", "sorghum", "winter wheat", "soybeans", "alfalfa"}


def test_all_wheat_is_used_only_where_winter_wheat_is_missing():
    wheat = lambda klass, year, value: row("WHEAT", klass, ALL, "GRAIN", "YIELD", "BU / ACRE", f"WHEAT, {klass} - YIELD", "STATE", year, value)  # noqa: E731
    table = nass.tidy(frame([wheat("WINTER", 2010, "40"), wheat("ALL CLASSES", 2010, "41"), wheat("ALL CLASSES", 2011, "38")]))
    assert lookup(table, year=2010)["value"] == 40
    assert lookup(table, year=2011)["value"] == 38


def test_yield_gaps_fill_from_district_then_state():
    raw = frame([
        corn_yield("COUNTY", 2010, "180"), corn_yield("AGRICULTURAL DISTRICT", 2010, "170"), corn_yield("STATE", 2010, "120"),
        corn_yield("AGRICULTURAL DISTRICT", 2011, "160"), corn_yield("STATE", 2011, "110"),
        corn_yield("STATE", 2012, "100"),
        corn_yield("STATE", 1999, "90"), corn_yield("STATE", 2025, "95"),  # outside 2000-2024
    ])
    table = nass.tidy(raw)
    assert [(r.year, r.level, r.value) for r in table.itertuples()] == [(2010, "county", 180.0), (2011, "district", 160.0), (2012, "state", 100.0)]


def test_planted_and_harvested_come_from_the_same_level():
    def acres(stat, level, year, value):
        if stat == "AREA PLANTED":
            return row("CORN", "ALL CLASSES", ALL, ANY_UTIL, stat, "ACRES", "CORN - ACRES PLANTED", level, year, value)
        return row("CORN", "ALL CLASSES", ALL, "GRAIN", stat, "ACRES", "CORN, GRAIN - ACRES HARVESTED", level, year, value)

    raw = frame([
        acres("AREA PLANTED", "COUNTY", 2010, "100,000"), acres("AREA HARVESTED", "COUNTY", 2010, "90,000"),
        acres("AREA PLANTED", "COUNTY", 2011, "100,000"),  # county harvested withheld in 2011
        acres("AREA PLANTED", "AGRICULTURAL DISTRICT", 2011, "800,000"), acres("AREA HARVESTED", "AGRICULTURAL DISTRICT", 2011, "400,000"),
        acres("AREA PLANTED", "AGRICULTURAL DISTRICT", 2010, "800,000"), acres("AREA HARVESTED", "AGRICULTURAL DISTRICT", 2010, "700,000"),
    ])
    table = nass.tidy(raw)
    assert set(table[table["year"] == 2010]["level"]) == {"county"}
    assert set(table[table["year"] == 2011]["level"]) == {"district"}
    ab = nass.abandonment(table).set_index("year")["abandonment"]
    assert ab[2010] == pytest.approx(0.10)
    assert ab[2011] == pytest.approx(0.50)


def test_prices_are_state_marketing_year_last_ten_and_sorghum_is_per_bushel():
    def price(commodity, unit, year, value, period="MARKETING YEAR", level="STATE", freq="ANNUAL"):
        return row(commodity, "ALL CLASSES", ALL, "GRAIN", "PRICE RECEIVED", unit, f"{commodity}, GRAIN - PRICE RECEIVED, MEASURED IN {unit}",
                   level, year, value, period=period, freq=freq)

    rows = [price("CORN", "$ / BU", y, str(3 + (y - 2000) / 10)) for y in range(2005, 2027)]
    rows += [price("CORN", "$ / BU", 2024, "9.99", period="JAN", freq="MONTHLY"), price("SORGHUM", "$ / CWT", 2024, "10")]
    table = nass.tidy(frame(rows))
    corn = table[table["crop"] == "corn"]
    assert list(corn["year"]) == list(range(2015, 2025))
    assert set(corn["level"]) == {"state"} and set(corn["practice"]) == {"all"} and set(corn["unit"]) == {"$/bu"}
    assert lookup(corn, year=2024)["value"] == pytest.approx(5.4)
    sorghum = lookup(table, crop="sorghum")
    assert sorghum["unit"] == "$/bu" and sorghum["value"] == pytest.approx(5.6)


def test_check_nass_reports_problems():
    table = nass.tidy(frame([corn_yield("COUNTY", 2010, "180"), corn_yield("COUNTY", 2011, "170")]))
    assert nass.check_nass(table) == []
    assert nass.check_nass(table.drop(columns=["level"])) == ["missing column(s): level"]
    assert any("duplicate" in p for p in nass.check_nass(pd.concat([table, table])))
    assert any(p.startswith("crop") for p in nass.check_nass(table.assign(crop="maize")))
    assert any(p.startswith("value") for p in nass.check_nass(table.assign(value=-1.0)))


def test_build_uses_the_cached_extract_and_writes_only_where_told(tmp_path, monkeypatch):
    extract = tmp_path / "extract.tsv.gz"
    frame([corn_yield("COUNTY", 2010, "180")]).to_csv(extract, sep="\t", index=False)
    monkeypatch.setattr(nass, "download_extract", lambda *a, **k: pytest.fail("must not download when the extract is cached"))
    out = tmp_path / "nass.parquet"
    table = nass.build(extract_path=extract, out_path=out, say=lambda msg: None)
    assert len(table) == 1
    assert nass.load_nass(out).equals(table)
    assert len(nass.series_summary(table)) == 1
