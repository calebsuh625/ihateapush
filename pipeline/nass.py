"""USDA NASS crop statistics for Finney County, Kansas -> data/nass_finney.parquet.

    .venv/bin/python -m pipeline.nass            # build (downloads once, then works from the cache)
    .venv/bin/python -m pipeline.nass --check    # validate the table and print the series summary

Source
------
The QuickStats API needs a key; the same data is published as a public bulk file listed at
https://www.nass.usda.gov/datasets (`qs.crops_YYYYMMDD.txt.gz`, about 1.1 GB, tab separated; the date
in the name changes every day, so the current name is read from that page). The file is streamed ONCE,
line by line, and never stored: only the Kansas survey rows we need (Finney County, the Southwest
district ASD 30, and the state; yields, planted and harvested acres, prices received; corn, sorghum,
wheat, soybeans, hay) are written to the small extract `data/raw/nass/qs_crops_ks_extract.tsv.gz`.
Everything else is built from that extract; if it exists nothing is downloaded.

Table (tidy, long), one row per (year, crop, practice, stat):

    year         int     2000-2024 for yields and acres; the last 10 marketing years for prices
    crop         str     corn | sorghum | winter wheat | soybeans | alfalfa   (fallow has no statistics)
    practice     str     irrigated | dryland | all
    stat         str     yield | planted_acres | harvested_acres | price
    value        float
    unit         str     bu/acre, tons/acre, acres, $/bu, $/ton
    level        str     county | district | state: where this value came from
    source_desc  str     the NASS short description of the series the value was read from

Decisions
---------
* Names: corn = CORN, GRAIN; sorghum = SORGHUM, GRAIN; winter wheat = WHEAT, WINTER (falling back to
  all WHEAT, which in Kansas is winter wheat); soybeans = SOYBEANS; alfalfa = HAY, ALFALFA. Planted
  acres are published for the whole crop (not "grain"), so planted uses CORN / SORGHUM.
  Alfalfa hay has no planted-acres series (a stand lasts years), so alfalfa has no `planted_acres`.
* Gap filling: for each (crop, practice, stat) and year the county value is used if NASS published
  one, otherwise the Southwest district value, otherwise the state value; `level` says which.
  Acres fill the same way, but planted and harvested of one (crop, practice, year) always come from
  the SAME level (the smallest area that published both), so abandonment = 1 - harvested/planted
  compares like with like. A district or state acreage is NOT a county acreage: filter on
  `level == "county"` before using acres as amounts.
* Prices are Kansas marketing-year prices received (state level only). NASS quotes sorghum in $/cwt;
  it is converted to $/bu at 56 lb per bushel so every grain is in $/bu.
* What NASS does not publish stays missing (no invented rows). For Kansas the irrigated / dryland
  split ends early at every level: corn 2018, winter wheat 2019, soybeans 2016, sorghum 2009; alfalfa
  hay has no split at all (practice `all` only). County numbers thin out after about 2010-2020;
  those years come from the district or state and say so in `level`.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NASS_PATH = ROOT / "data" / "nass_finney.parquet"
RAW_DIR = ROOT / "data" / "raw" / "nass"
EXTRACT_PATH = RAW_DIR / "qs_crops_ks_extract.tsv.gz"
EXTRACT_META_PATH = RAW_DIR / "qs_crops_ks_extract.json"
LOG_DIR = ROOT / "data" / "logs" / "phase3"

DATASETS_URL = "https://www.nass.usda.gov/datasets"
BASE_URL = "https://www.nass.usda.gov"
USER_AGENT = "Mozilla/5.0 (crop-rotation-tool; school project; one bulk download)"

STATE_ALPHA = "KS"
COUNTY_CODE = "055"  # Finney
ASD_CODE = "30"  # Southwest
FIRST_EXTRACT_YEAR = 1990
YEARS = (2000, 2024)
PRICE_YEARS = 10

COMMODITIES = {"CORN", "SORGHUM", "WHEAT", "SOYBEANS", "HAY"}
STAT_CATS = {"YIELD": "yield", "AREA PLANTED": "planted_acres", "AREA HARVESTED": "harvested_acres", "PRICE RECEIVED": "price"}
LEVELS = {"COUNTY": "county", "AGRICULTURAL DISTRICT": "district", "STATE": "state"}
LEVEL_ORDER = ["county", "district", "state"]
PRACTICES = {"IRRIGATED": "irrigated", "NON-IRRIGATED": "dryland", "ALL PRODUCTION PRACTICES": "all"}

CROPS = ("corn", "sorghum", "winter wheat", "soybeans", "alfalfa")
PRACTICE_NAMES = ("irrigated", "dryland", "all")
STATS = ("yield", "planted_acres", "harvested_acres", "price")
COLUMNS = ["year", "crop", "practice", "stat", "value", "unit", "level", "source_desc"]
SORGHUM_CWT_PER_BU = 0.56  # 56 lb per bushel

# (commodity, class, utilisation) -> our crop, per statistic. First entry is preferred; later ones are
# used only for years the earlier ones do not cover.
_GRAIN = "GRAIN"
_ALL_UTIL = "ALL UTILIZATION PRACTICES"
SERIES = {
    "corn": {
        "yield": [("CORN", "ALL CLASSES", _GRAIN)],
        "harvested_acres": [("CORN", "ALL CLASSES", _GRAIN)],
        "planted_acres": [("CORN", "ALL CLASSES", _ALL_UTIL)],
        "price": [("CORN", "ALL CLASSES", _GRAIN)],
    },
    "sorghum": {
        "yield": [("SORGHUM", "ALL CLASSES", _GRAIN)],
        "harvested_acres": [("SORGHUM", "ALL CLASSES", _GRAIN)],
        "planted_acres": [("SORGHUM", "ALL CLASSES", _ALL_UTIL)],
        "price": [("SORGHUM", "ALL CLASSES", _GRAIN)],
    },
    "winter wheat": {
        "yield": [("WHEAT", "WINTER", None), ("WHEAT", "ALL CLASSES", None)],
        "harvested_acres": [("WHEAT", "WINTER", None), ("WHEAT", "ALL CLASSES", None)],
        "planted_acres": [("WHEAT", "WINTER", None), ("WHEAT", "ALL CLASSES", None)],
        "price": [("WHEAT", "WINTER", None), ("WHEAT", "ALL CLASSES", None)],
    },
    "soybeans": {
        "yield": [("SOYBEANS", "ALL CLASSES", None)],
        "harvested_acres": [("SOYBEANS", "ALL CLASSES", None)],
        "planted_acres": [("SOYBEANS", "ALL CLASSES", None)],
        "price": [("SOYBEANS", "ALL CLASSES", None)],
    },
    "alfalfa": {
        "yield": [("HAY", "ALFALFA", None)],
        "harvested_acres": [("HAY", "ALFALFA", None)],
        "price": [("HAY", "ALFALFA", None)],
    },
}
UNITS = {  # NASS unit -> (our unit, multiplier), per stat
    "yield": {"BU / ACRE": ("bu/acre", 1.0), "TONS / ACRE": ("tons/acre", 1.0)},
    "planted_acres": {"ACRES": ("acres", 1.0)},
    "harvested_acres": {"ACRES": ("acres", 1.0)},
    "price": {"$ / BU": ("$/bu", 1.0), "$ / TON": ("$/ton", 1.0), "$ / CWT": ("$/bu", SORGHUM_CWT_PER_BU)},
}


class NassError(RuntimeError):
    pass


# --------------------------------------------------------------------------- extract (network)


def current_bulk_url(html: str) -> str:
    """The qs.crops_YYYYMMDD.txt.gz link in the datasets page (newest date if several)."""
    names = re.findall(r'href="([^"]*qs\.crops_(\d{8})\.txt\.gz)"', html)
    if not names:
        raise NassError(f"no qs.crops_YYYYMMDD.txt.gz link found on {DATASETS_URL}")
    href = max(names, key=lambda pair: pair[1])[0]
    return href if href.startswith("http") else BASE_URL + href


def keep_row(row: dict) -> bool:
    """True for the rows the extract keeps (one parsed line of the bulk file)."""
    if row.get("STATE_ALPHA") != STATE_ALPHA or row.get("SOURCE_DESC") != "SURVEY":
        return False
    if row.get("COMMODITY_DESC") not in COMMODITIES or row.get("STATISTICCAT_DESC") not in STAT_CATS:
        return False
    if row.get("DOMAIN_DESC") != "TOTAL":
        return False
    level = row.get("AGG_LEVEL_DESC")
    if level == "COUNTY":
        if row.get("COUNTY_CODE") != COUNTY_CODE and row.get("COUNTY_ANSI") != COUNTY_CODE:
            return False
    elif level == "AGRICULTURAL DISTRICT":
        if row.get("ASD_CODE") != ASD_CODE:
            return False
    elif level != "STATE":
        return False
    try:
        return int(row.get("YEAR", "0")) >= FIRST_EXTRACT_YEAR
    except ValueError:
        return False


def filter_lines(lines, out) -> dict:
    """Stream tab-separated text lines (header first) into `out`, keeping only keep_row() rows."""
    header = next(lines).rstrip("\r\n")
    names = header.split("\t")
    out.write(header + "\n")
    needle = f"\t{STATE_ALPHA}\t"
    n_in = n_out = 0
    for line in lines:
        n_in += 1
        if needle not in line:
            continue
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) != len(names):
            continue
        if keep_row(dict(zip(names, parts))):
            out.write("\t".join(parts) + "\n")
            n_out += 1
    return {"lines_read": n_in, "rows_kept": n_out}


def download_extract(extract_path: Path = EXTRACT_PATH, *, say=print) -> dict:
    """Stream the bulk file once and write the small extract. The full file never touches the disk."""
    import requests

    extract_path.parent.mkdir(parents=True, exist_ok=True)
    page = requests.get(DATASETS_URL, headers={"User-Agent": USER_AGENT}, timeout=60)
    page.raise_for_status()
    url = current_bulk_url(page.text)
    say(f"nass: streaming {url} (about 1.1 GB; a few minutes)")
    start = time.time()
    tmp = extract_path.with_suffix(".part")
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(30, 300)) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = False
        text = io.TextIOWrapper(gzip.GzipFile(fileobj=resp.raw), encoding="utf-8", errors="replace", newline="")
        with gzip.open(tmp, "wt", encoding="utf-8", newline="") as out:
            stats = filter_lines(iter(text), out)
    if stats["rows_kept"] == 0:
        tmp.unlink(missing_ok=True)
        raise NassError(f"{url}: no Kansas rows matched; the file layout may have changed")
    tmp.replace(extract_path)
    meta = {"url": url, "fetched": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - start, 1), **stats}
    EXTRACT_META_PATH.write_text(json.dumps(meta, indent=2))
    say(f"nass: kept {stats['rows_kept']:,} of {stats['lines_read']:,} lines in {meta['seconds']:.0f} s -> {extract_path}")
    return meta


def load_extract(extract_path: Path = EXTRACT_PATH) -> pd.DataFrame:
    return pd.read_csv(extract_path, sep="\t", dtype=str, keep_default_na=False, quoting=csv.QUOTE_NONE)


# --------------------------------------------------------------------------- tidy table (local)


def parse_value(text: str) -> float | None:
    """'1,234.5' -> 1234.5; NASS codes such as (D) withheld, (NA), (Z) -> None."""
    text = str(text).strip().replace(",", "")
    if not text or text.startswith("("):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _candidates(raw: pd.DataFrame) -> pd.DataFrame:
    """Every extract row that is one of our series, labelled with crop/practice/stat/level/unit/value."""
    need = ["COMMODITY_DESC", "CLASS_DESC", "PRODN_PRACTICE_DESC", "UTIL_PRACTICE_DESC", "STATISTICCAT_DESC",
            "UNIT_DESC", "SHORT_DESC", "DOMAIN_DESC", "AGG_LEVEL_DESC", "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE"]
    missing = [c for c in need if c not in raw.columns]
    if missing:
        raise NassError(f"extract is missing column(s): {', '.join(missing)}")
    rows = []
    for rec in raw[need].to_dict("records"):
        stat = STAT_CATS.get(rec["STATISTICCAT_DESC"])
        level = LEVELS.get(rec["AGG_LEVEL_DESC"])
        practice = PRACTICES.get(rec["PRODN_PRACTICE_DESC"])
        if stat is None or level is None or practice is None or rec["DOMAIN_DESC"] != "TOTAL":
            continue
        if rec["FREQ_DESC"] != "ANNUAL":
            continue
        period = rec["REFERENCE_PERIOD_DESC"]
        if stat == "price":
            if period != "MARKETING YEAR" or level != "state" or practice != "all":
                continue
        elif period != "YEAR":
            continue
        unit = UNITS[stat].get(rec["UNIT_DESC"])
        value = parse_value(rec["VALUE"])
        if unit is None or value is None:
            continue
        for crop, by_stat in SERIES.items():
            for rank, (commodity, klass, util) in enumerate(by_stat.get(stat, [])):
                if rec["COMMODITY_DESC"] != commodity or rec["CLASS_DESC"] != klass:
                    continue
                if util is not None and rec["UTIL_PRACTICE_DESC"] != util:
                    continue
                if util is None and rec["UTIL_PRACTICE_DESC"] not in (_GRAIN, _ALL_UTIL):
                    continue
                rows.append({
                    "year": int(rec["YEAR"]), "crop": crop, "practice": practice, "stat": stat,
                    "value": value * unit[1], "unit": unit[0], "level": level,
                    "source_desc": rec["SHORT_DESC"], "_rank": rank,
                    "_util_rank": 0 if rec["UTIL_PRACTICE_DESC"] == _GRAIN or stat == "planted_acres" else 1,
                })
    return pd.DataFrame(rows, columns=COLUMNS + ["_rank", "_util_rank"])


def tidy(raw: pd.DataFrame, years: tuple[int, int] = YEARS, price_years: int = PRICE_YEARS) -> pd.DataFrame:
    """The tidy table from the raw extract (see the module docstring for the rules)."""
    cand = _candidates(raw)
    if cand.empty:
        raise NassError("no usable NASS rows in the extract")
    cand["_level"] = cand["level"].map(LEVEL_ORDER.index)
    # one value per (series, year, level): preferred class first, then the grain utilisation
    cand = cand.sort_values(["_rank", "_util_rank"]).drop_duplicates(["crop", "practice", "stat", "level", "year"])
    in_years = cand["year"].between(*years)
    out = []
    # yields: county, else district, else state, year by year
    y = cand[(cand["stat"] == "yield") & in_years].sort_values("_level")
    out.append(y.drop_duplicates(["crop", "practice", "stat", "year"]))
    # acres: per (crop, practice, year) the smallest area that published every acre stat of the crop
    acres = cand[cand["stat"].isin(["planted_acres", "harvested_acres"]) & in_years]
    for (crop, practice, year), grp in acres.groupby(["crop", "practice", "year"]):
        wanted = set(SERIES[crop]) & {"planted_acres", "harvested_acres"}
        for lvl in LEVEL_ORDER:
            g = grp[grp["level"] == lvl]
            if set(g["stat"]) == wanted:
                out.append(g)
                break
    # prices: the last `price_years` marketing years with a value, per crop
    p = cand[cand["stat"] == "price"]
    p = p[p["year"] <= years[1]]
    for _, grp in p.groupby("crop"):
        out.append(grp.sort_values("year").tail(price_years))
    table = pd.concat(out, ignore_index=True)[COLUMNS]
    table["year"] = table["year"].astype("int64")
    table["value"] = table["value"].astype("float64")
    return table.sort_values(["stat", "crop", "practice", "year"]).reset_index(drop=True)


def check_nass(df) -> list[str]:
    """Every way `df` breaks the contract in the module docstring (empty list = valid)."""
    if not isinstance(df, pd.DataFrame):
        return [f"expected a DataFrame, got {type(df).__name__}"]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        return [f"missing column(s): {', '.join(missing)}"]
    if len(df) == 0:
        return ["table has no rows"]
    problems = []
    if not pd.api.types.is_integer_dtype(df["year"]):
        problems.append(f"year: dtype is {df['year'].dtype}, expected integer")
    if not pd.api.types.is_float_dtype(df["value"]):
        problems.append(f"value: dtype is {df['value'].dtype}, expected float")
    elif df["value"].isna().any() or (df["value"] < 0).any():
        problems.append("value: must be non-null and >= 0")
    for col, allowed in [("crop", CROPS), ("practice", PRACTICE_NAMES), ("stat", STATS), ("level", tuple(LEVEL_ORDER))]:
        bad = sorted(set(df[col].dropna().astype(str)) - set(allowed))
        if bad or df[col].isna().any():
            problems.append(f"{col}: unexpected value(s) {bad[:5]} (allowed: {', '.join(allowed)})")
    for col in ("unit", "source_desc"):
        if df[col].isna().any() or (df[col].astype(str).str.strip() == "").any():
            problems.append(f"{col}: must be non-empty text")
    dup = df.duplicated(["year", "crop", "practice", "stat"])
    if dup.any():
        problems.append(f"{int(dup.sum())} duplicate (year, crop, practice, stat) row(s)")
    return problems


def load_nass(path: str | Path = NASS_PATH) -> pd.DataFrame:
    """Read and validate the table. Raises FileNotFoundError / NassError."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_parquet(path)
    problems = check_nass(df)
    if problems:
        raise NassError(f"{path} does not match pipeline/nass.py:\n" + "\n".join(f"  - {p}" for p in problems))
    return df[COLUMNS]


def abandonment(table: pd.DataFrame) -> pd.DataFrame:
    """1 - harvested/planted per (crop, practice, year), where both exist at the same level."""
    acres = table[table["stat"].isin(["planted_acres", "harvested_acres"])]
    wide = acres.pivot_table(index=["crop", "practice", "year", "level"], columns="stat", values="value").dropna().reset_index()
    wide = wide[wide["planted_acres"] > 0]
    wide["abandonment"] = (1 - wide["harvested_acres"] / wide["planted_acres"]).clip(lower=0.0)
    return wide


def series_summary(table: pd.DataFrame) -> list[str]:
    """One line per (crop, practice, stat): years covered, level(s), median."""
    lines = []
    for (crop, practice, stat), g in table.groupby(["crop", "practice", "stat"], sort=True):
        levels = g["level"].value_counts()
        lvl = ", ".join(f"{name} {int(levels[name])}" for name in LEVEL_ORDER if name in levels)
        small = g[g["level"] == next(name for name in LEVEL_ORDER if name in levels)]
        which = "" if len(levels) == 1 or stat in ("yield", "price") else f" ({small['level'].iloc[0]} rows)"
        median = (g if not which else small)["value"].median()
        lines.append(
            f"{crop:13s} {practice:9s} {stat:15s} {int(g['year'].min())}-{int(g['year'].max())} "
            f"({len(g)} yr; {lvl}) median {median:,.1f} {g['unit'].iloc[0]}{which}"
        )
    return lines


def build(*, extract_path: Path = EXTRACT_PATH, out_path: Path = NASS_PATH, say=print) -> pd.DataFrame:
    if not extract_path.exists():
        download_extract(extract_path, say=say)
    else:
        say(f"nass: using cached extract {extract_path}")
    table = tidy(load_extract(extract_path))
    problems = check_nass(table)
    if problems:
        raise NassError("built table is invalid:\n" + "\n".join(f"  - {p}" for p in problems))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.part")
    table.to_parquet(tmp, index=False)
    tmp.replace(out_path)
    return table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="validate the existing table and print the summary")
    args = parser.parse_args(argv)
    try:
        table = load_nass() if args.check else build()
    except FileNotFoundError as exc:
        print(f"{exc} does not exist (build it with: python -m pipeline.nass)")
        return 1
    except NassError as exc:
        print(exc)
        return 1
    lines = series_summary(table)
    ab = abandonment(table)
    ab_lines = [
        f"abandonment {crop:13s} {practice:9s} {len(g)} yr ({', '.join(f'{k} {v}' for k, v in g['level'].value_counts().items())}): median {g['abandonment'].median():.1%}, worst {g['abandonment'].max():.1%} ({int(g.loc[g['abandonment'].idxmax(), 'year'])})"
        for (crop, practice), g in ab.groupby(["crop", "practice"])
    ]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "nass_summary.txt").write_text("\n".join(lines + [""] + ab_lines) + "\n")
    print(f"{NASS_PATH}: valid, {len(table):,} rows, {table.groupby(['crop', 'practice', 'stat']).ngroups} series")
    for line in lines:
        print(line)
    print(f"(abandonment lines in {LOG_DIR / 'nass_summary.txt'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
