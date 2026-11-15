"""Which county the pipeline builds and the API serves: one environment variable, `REGION`.

    REGION=20069 make pipeline       # Gray County, KS -> data/regions/20069/fields.parquet
    REGION=20069 make dev            # serve Gray County
    make pipeline                    # no REGION: Finney County, KS, exactly as before (data/)

`REGION` is a 5-digit county FIPS code (state 2 + county 3). Finney County (20055), the original
region, keeps every output where it always was, directly under `data/`, so nothing that already
reads `data/fields.parquet` changes. Any other county writes its outputs under
`data/regions/<fips>/`. Downloads stay shared under `data/raw/`; every cached raw file is already
keyed by county (FIPS in the name) or by location, so counties never overwrite each other.

Data that needs Google Earth Engine (satellite bare days, climate-model futures, OpenET) is
built per county only where someone has run those jobs. Where a non-Finney county lacks a file,
`fallback_path` lends it Finney's file for the pieces where that is defensible (neighbouring
counties in the same climate), and `borrowed()` says which pieces were borrowed, so the API
and the app can say "from Finney County" instead of passing it off as local.

Field ids keep the schema's `KS-000123` form. They are unique within one region, not across
regions; the API serves one region at a time.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_DATA_DIR = ROOT / "data"
RAW_DIR = BASE_DATA_DIR / "raw"  # shared by every region (cache keys include the county)

DEFAULT_FIPS = "20055"  # Finney County, KS

# State FIPS -> postal abbreviation (50 states + DC).
STATE_ABBR_BY_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE",
    "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA",
    "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM",
    "36": "NY", "37": "NC", "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY",
}

# Counties whose names we know without opening the Census file. Others: REGION_NAME, else the
# Census boundary file (pipeline/county.py), else "County <fips>".
KNOWN_COUNTIES = {
    "20055": "Finney",
    "20069": "Gray",
    "20081": "Haskell",
    "20093": "Kearny",
}

# USDA NASS agricultural district (ASD) of each known county; others need $NASS_ASD.
# Southwest Kansas (30) holds Finney, Gray, Haskell and Kearny.
KNOWN_NASS_DISTRICTS = {"20055": "30", "20069": "30", "20081": "30", "20093": "30"}

# What a non-Finney county may borrow from Finney when it has no file of its own, and why.
# Gray, Haskell and Kearny all touch Finney; their centres are 35-50 km from Finney's, about two
# cells of the 0.25-degree climate-model grid, so the climate tables are close to the same.
BORROWABLE = {
    "climate": "climate futures and weather history (Earth Engine): Finney County's, a neighbouring county",
    "effects": "rotation effects on bare soil, measured on matched field pairs in Finney County",
    "nass": "county crop yields and prices: Finney County's table (same NASS Southwest district)",
}

_FIPS_RE = re.compile(r"^\d{5}$")


@dataclass(frozen=True)
class Region:
    fips: str
    state_fips: str
    county_fips: str
    state_abbr: str
    county_name: str
    data_dir: Path

    @property
    def is_default(self) -> bool:
        return self.fips == DEFAULT_FIPS

    @property
    def label(self) -> str:
        return f"{self.county_name} County, {self.state_abbr}"

    def as_dict(self) -> dict:
        return {
            "fips": self.fips,
            "state": self.state_abbr,
            "county": self.county_name,
            "label": self.label,
            "is_default": self.is_default,
        }


def data_dir_for(fips: str) -> Path:
    return BASE_DATA_DIR if fips == DEFAULT_FIPS else BASE_DATA_DIR / "regions" / fips


def _county_name(fips: str) -> str:
    name = os.environ.get("REGION_NAME", "").strip()
    if name:
        return name
    if fips in KNOWN_COUNTIES:
        return KNOWN_COUNTIES[fips]
    cached = RAW_DIR / "county" / f"county_{fips}.geojson"
    if cached.is_file():
        import json

        try:
            props = json.loads(cached.read_text())["features"][0]["properties"]
            for key in ("NAME", "name", "BASENAME"):
                if props.get(key):
                    return str(props[key])
        except (KeyError, IndexError, ValueError):
            pass
    return f"County {fips}"


def get_region(fips: str | None = None) -> Region:
    """The region from `fips`, else $REGION, else Finney County. Raises ValueError on a bad code."""
    raw = (fips if fips is not None else os.environ.get("REGION", "")).strip() or DEFAULT_FIPS
    if not _FIPS_RE.match(raw):
        raise ValueError(f"REGION must be a 5-digit county FIPS code like 20055, got {raw!r}")
    state = raw[:2]
    if state not in STATE_ABBR_BY_FIPS:
        raise ValueError(f"REGION {raw}: unknown state FIPS {state}")
    return Region(
        fips=raw,
        state_fips=state,
        county_fips=raw[2:],
        state_abbr=STATE_ABBR_BY_FIPS[state],
        county_name=_county_name(raw),
        data_dir=data_dir_for(raw),
    )


REGION = get_region()
DATA_DIR = REGION.data_dir


def nass_district(region: Region | None = None) -> str:
    """The county's NASS agricultural district code: $NASS_ASD, else the known table. ValueError if unknown."""
    region = region or REGION
    code = os.environ.get("NASS_ASD", "").strip() or KNOWN_NASS_DISTRICTS.get(region.fips, "")
    if not code:
        raise ValueError(f"no NASS district known for {region.fips}; set NASS_ASD (e.g. 30 for Southwest Kansas)")
    return code


def nass_path(region: Region | None = None) -> Path:
    """The NASS table: data/nass_finney.parquet for Finney (its historic name), <data dir>/nass.parquet otherwise."""
    region = region or REGION
    return region.data_dir / ("nass_finney.parquet" if region.is_default else "nass.parquet")


def fallback_path(path: Path, kind: str, region: Region | None = None) -> Path:
    """`path` if it exists or the region is Finney; else Finney's file of the same name when it exists.

    `kind` must be a key of BORROWABLE. `path` must sit under the region's data dir.
    """
    region = region or REGION
    if kind not in BORROWABLE:
        raise ValueError(f"unknown borrowable kind {kind!r}")
    path = Path(path)
    if region.is_default or path.exists():
        return path
    try:
        rel = path.relative_to(region.data_dir)
    except ValueError:
        return path
    finney = BASE_DATA_DIR / rel
    if kind == "nass" and rel.name == "nass.parquet":
        finney = BASE_DATA_DIR / "nass_finney.parquet"
    return finney if finney.exists() else path


def is_borrowed(path: Path, region: Region | None = None) -> bool:
    """True when `path` is a Finney file standing in for a non-Finney region."""
    region = region or REGION
    if region.is_default:
        return False
    try:
        Path(path).resolve().relative_to(region.data_dir.resolve())
        return False
    except ValueError:
        return Path(path).resolve().parent == BASE_DATA_DIR.resolve()


def built_regions() -> list[dict]:
    """Every region with a fields.parquet on disk (Finney first), for the API's region list."""
    out = []
    for fips in [DEFAULT_FIPS] + sorted(
        p.name for p in (BASE_DATA_DIR / "regions").glob("*") if p.is_dir() and _FIPS_RE.match(p.name)
    ):
        if (data_dir_for(fips) / "fields.parquet").is_file():
            out.append(get_region(fips).as_dict())
    return out


if __name__ == "__main__":
    print(REGION.as_dict(), DATA_DIR)
