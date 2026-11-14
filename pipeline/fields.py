"""Step 5.1: field outlines + crop history for the county (Finney County, KS).

    from pipeline.fields import build_fields
    fields = build_fields(source="auto", min_area_ha=5.0, max_fields=None)

returns a GeoDataFrame (EPSG:4326, RangeIndex) with exactly these columns

    field_id      "KS-000123"
    geometry      a single valid Polygon
    crop_history  JSON string, year -> lowercase crop name, 2008-2024
    area_ha       area in hectares, measured in EPSG:5070 (equal-area)

plus `fields.attrs["provenance"]` (a JSON-serialisable dict: source used, URLs, CSB
release, what every filter dropped, per-year CDL facts, the CSB cross-check, timings) and
`fields.attrs["source_ids"]` (list aligned with the rows: the CSBID of each field, or
None for fields segmented from the CDL).

Where the outlines come from
----------------------------
`source="csb"`: USDA NASS Crop Sequence Boundaries (`pipeline/csb.py`).

* **County clip**: polygons are selected by CSB's own county attribute
  (STATEFIPS/CNTYFIPS) and kept whole. Nothing is cut at the county line, so there are no
  slivers; a field straddling the line belongs to exactly one county. As a guard, a
  polygon with less than half of its area inside the Census county outline is dropped
  (none in Finney County).
* **Geometry**: `make_valid`, then the largest Polygon part is kept (CSB stores
  MultiPolygons; in Finney County 4 of 15,926 really have a second part, all < 2.2 ha).
  One field per CSBID.
* **Size**: fields smaller than `min_area_ha` are dropped.

`source="cdl-segment"`: contiguous cropland segmented from the CDL rasters themselves
(`pipeline/cdl_segment.py`) - the fallback. `source="auto"` tries CSB and falls back to
the segmentation on any failure, logging and recording why.

Crop history
------------
Every year 2008-2024 comes from the county CDL rasters: the crop of a field in a year
is the majority CDL class of the pixels inside it (`pipeline/cdl.py`). A year whose
raster could not be downloaded, or in which a field has no valid pixel, is omitted from
that field's history. For CSB fields the result is cross-checked against CSB's own
per-year CDL attributes; the agreement rates are stored in the provenance.

Cropland rule
-------------
A field is kept when its majority class is a crop class (`cdl_codes.is_crop`: cultivated
crops and fallow/idle cropland) in **at least half of the years with data, or in at
least 3 of the 5 most recent years**, and in any case **at least once in the 5 most
recent years**. The first clause keeps long-term cropland, the second keeps land broken
out of grass (e.g. expired CRP) that is cropped now. Non-crop years stay in the history
under their real name (e.g. "grass/pasture").

Field ids
---------
CSB: "KS-" + the 6-digit rank of the polygon among *all* of the county's CSB polygons
sorted by CSBID (before any filter). An id therefore does not depend on `min_area_ha`,
on the cropland rule or on `max_fields`; ids have gaps. Segmented fields: rank of the
kept fields ordered north to south, then west to east.

`max_fields`
------------
None keeps everything. Otherwise the fields are ordered along a Hilbert curve through
their centroids and every (n / max_fields)-th one is taken: deterministic, evenly spread
over the county, blind to size and crop.

    python pipeline/fields.py [--source auto|csb|cdl-segment] [--max-fields N]
                              [--min-area-ha 5] [--out path.parquet] [--samples 20]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/fields.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import shapely  # noqa: E402

from pipeline import cdl, csb  # noqa: E402
from pipeline.cdl_codes import IS_CROP_LUT, NAME_LUT  # noqa: E402
from pipeline.cdl_segment import clean_polygons, segment_fields  # noqa: E402
from pipeline.common import (  # noqa: E402
    COUNTY_NAME,
    EQUAL_AREA_CRS,
    FIPS,
    MIN_FIELD_HA,
    STATE_ABBR,
    YEARS,
    log,
)
from pipeline.county import county_boundary  # noqa: E402

SOURCES = ("auto", "csb", "cdl-segment")
OUTPUT_COLUMNS = ["field_id", "geometry", "crop_history", "area_ha"]

# cropland rule (see module docstring)
CROP_SHARE_MIN = 0.5
RECENT_YEARS = 5
RECENT_CROP_YEARS_MIN = 3
MIN_SHARE_IN_COUNTY = 0.5

CROPLAND_RULE = (
    f"majority CDL class is a crop class (cultivated crops or fallow/idle) in >= {CROP_SHARE_MIN:.0%} of the "
    f"years with data OR in >= {RECENT_CROP_YEARS_MIN} of the {RECENT_YEARS} most recent years; and in any case "
    f"in >= 1 of the {RECENT_YEARS} most recent years"
)
FIELD_ID_RULE = {
    "csb": "state abbreviation + 6-digit rank among ALL county CSB polygons sorted by CSBID (before any filter; ids have gaps)",
    "cdl-segment": "state abbreviation + 6-digit rank of the kept fields ordered north to south, then west to east",
}
MAX_FIELDS_RULE = (
    "fields ordered along a Hilbert curve through their centroids; systematic sample of max_fields of them "
    "(deterministic, spatially even, independent of size and crop); field ids are assigned before sampling"
)


# --------------------------------------------------------------------------------------
# small, separately testable pieces
# --------------------------------------------------------------------------------------
def cropland_mask(codes: np.ndarray, years: list[int]) -> tuple[np.ndarray, dict]:
    """Which rows of the (n_fields, n_years) majority-code table are cropland fields.

    `years` are the calendar years of the columns (ascending). Code 0 = no data that
    year. Returns (keep mask, detail) where detail["rejected"] maps each reason to the
    boolean mask of the rows it rejects (every rejected row has exactly one reason) and
    detail["kept_by_recent_clause"] marks rows kept although crop years are < 50%.
    """
    codes = np.asarray(codes)
    if codes.ndim != 2 or codes.shape[1] != len(years):
        raise ValueError("codes must be (n_fields, n_years)")
    has_data = codes != 0
    is_crop = IS_CROP_LUT[codes] & has_data
    n_data = has_data.sum(axis=1)
    n_crop = is_crop.sum(axis=1)
    recent = is_crop[:, -RECENT_YEARS:].sum(axis=1)
    share = np.divide(n_crop, n_data, out=np.zeros(len(codes), dtype=float), where=n_data > 0)

    no_data = n_data == 0
    not_recent = ~no_data & (recent < 1)
    too_few = ~no_data & ~not_recent & (share < CROP_SHARE_MIN) & (recent < RECENT_CROP_YEARS_MIN)
    keep = ~(no_data | not_recent | too_few)
    detail = {
        "rejected": {
            "no CDL data in any year": no_data,
            f"no crop year in the {RECENT_YEARS} most recent years": not_recent,
            f"crop in < {CROP_SHARE_MIN:.0%} of years and < {RECENT_CROP_YEARS_MIN} of the {RECENT_YEARS} most recent": too_few,
        },
        "kept_by_recent_clause": keep & (share < CROP_SHARE_MIN),
        "recent_years": [int(y) for y in years[-RECENT_YEARS:]],
    }
    return keep, detail


def field_ids(ranks, state: str = STATE_ABBR) -> list[str]:
    """ "KS-000123" for rank 123. Ranks must be 1..999999."""
    ranks = [int(r) for r in ranks]
    if ranks and (min(ranks) < 1 or max(ranks) > 999_999):
        raise ValueError("field ranks must be between 1 and 999999")
    return [f"{state}-{r:06d}" for r in ranks]


def spatial_subset(geoms: gpd.GeoSeries, max_fields: int | None) -> np.ndarray:
    """Row positions (ascending) of a deterministic, spatially even subset of `max_fields` rows."""
    n = len(geoms)
    if max_fields is None or max_fields >= n:
        return np.arange(n)
    if max_fields < 1:
        raise ValueError("max_fields must be >= 1 (or None)")
    points = geoms.reset_index(drop=True).centroid
    distance = points.hilbert_distance().to_numpy()
    order = np.lexsort((points.y.to_numpy(), points.x.to_numpy(), distance))  # ties: by coordinates
    picks = np.floor((np.arange(max_fields) + 0.5) * n / max_fields).astype(int)
    return np.sort(order[picks])


def crop_histories(codes: np.ndarray, years: list[int]) -> list[str]:
    """JSON strings year -> crop name; years with code 0 (no data) are left out."""
    out = []
    for row in np.asarray(codes):
        out.append(json.dumps({str(y): NAME_LUT[c] for y, c in zip(years, row) if c}))
    return out


def agreement_stats(codes: np.ndarray, years: list[int], attrs: dict[int, np.ndarray], rows=None) -> dict:
    """Share of fields whose raster-majority class equals the CSB per-year CDL attribute."""
    rows = slice(None) if rows is None else rows
    per_year = {}
    agree_all = n_all = 0
    for j, year in enumerate(years):
        if year not in attrs:
            continue
        mine = np.asarray(codes)[rows, j]
        theirs = np.asarray(attrs[year])[rows]
        comparable = (mine != 0) & (theirs > 0)
        n = int(comparable.sum())
        agree = int((mine[comparable] == theirs[comparable]).sum())
        per_year[str(year)] = {"compared": n, "agree": agree, "rate": round(agree / n, 4) if n else None}
        agree_all += agree
        n_all += n
    return {
        "per_year": per_year,
        "overall": {"compared": n_all, "agree": agree_all, "rate": round(agree_all / n_all, 4) if n_all else None},
    }


def _step(filters: list, name: str, n_in: int, n_out: int, note: str = "") -> None:
    filters.append({"step": name, "in": int(n_in), "dropped": int(n_in - n_out), "out": int(n_out), "note": note})
    log(f"  {name}: {n_in:,} -> {n_out:,} (dropped {n_in - n_out:,}){' - ' + note if note else ''}")


# --------------------------------------------------------------------------------------
# candidate polygons (EPSG:5070) from the two sources
# --------------------------------------------------------------------------------------
def _county_outline(crs):
    """(outline geometry in `crs`, source URL, area in km2) or (None, None, None) if it cannot be had."""
    try:
        outline = county_boundary(FIPS)
        geom = outline.to_crs(crs).geometry.iloc[0]
        km2 = float(outline.to_crs(EQUAL_AREA_CRS).area.iloc[0]) / 1e6
        return geom, outline.attrs.get("source"), km2
    except Exception as exc:
        log(f"!!! fields: county outline unavailable ({exc})")
        return None, None, None


def to_lonlat_polygons(geoms: gpd.GeoSeries) -> tuple[gpd.GeoSeries, np.ndarray]:
    """Reproject to EPSG:4326 and make sure every geometry is a single valid Polygon.

    Returns (GeoSeries aligned with the input, boolean mask of usable rows). Reprojection
    can, very rarely, make a ring touch itself; such polygons are repaired (largest valid
    Polygon part) and a row is only unusable if nothing polygonal is left.
    """
    lonlat = geoms.reset_index(drop=True).to_crs("EPSG:4326")
    fine = lonlat.is_valid.to_numpy() & (lonlat.geom_type.to_numpy() == "Polygon") & ~lonlat.is_empty.to_numpy()
    if fine.all():
        return lonlat, fine
    cleaned = clean_polygons(lonlat.values)
    good = np.array([g is not None for g in cleaned])
    return gpd.GeoSeries(cleaned, crs="EPSG:4326"), good


def _county_guard(cand: gpd.GeoDataFrame, filters: list, label: str) -> tuple[np.ndarray, int]:
    """Mask of polygons with >= 50% of their area inside the county outline, and how many straddle it."""
    outline, _, _ = _county_outline(cand.crs)
    if outline is None:
        _step(filters, f"county ({label}; guard skipped: no county outline)", len(cand), len(cand))
        return np.ones(len(cand), dtype=bool), 0
    geom_arr = cand.geometry.values
    shapely.prepare(outline)
    inside = shapely.contains(outline, geom_arr)
    share_inside = np.ones(len(cand))
    crossing = np.flatnonzero(~inside)
    if len(crossing):
        share_inside[crossing] = shapely.area(shapely.intersection(geom_arr[crossing], outline)) / shapely.area(
            geom_arr[crossing]
        )
    in_county = share_inside >= MIN_SHARE_IN_COUNTY
    straddling = int(((share_inside < 0.999) & in_county).sum())
    _step(
        filters,
        f"county ({label}; drop if < {MIN_SHARE_IN_COUNTY:.0%} of area inside the Census outline)",
        len(cand),
        int(in_county.sum()),
        f"{straddling} polygon(s) straddle the county line and are kept whole",
    )
    return in_county, straddling


def _csb_candidates(min_area_ha: float) -> tuple[gpd.GeoDataFrame, dict, list, dict]:
    """County CSB polygons, cleaned. Returns (candidates, csb provenance, filters, attrs by year).

    The candidates frame has columns source_id, rank, area_ha, eligible, geometry and
    still contains the polygons below `min_area_ha` (eligible=False): they are needed
    for the cross-check on all county polygons and are dropped by the caller.
    """
    raw = csb.county_csb(FIPS)
    prov = dict(raw.attrs.get("provenance", {}))
    filters: list = []
    n_county = len(raw)
    raw = raw.sort_values("CSBID", kind="stable").reset_index(drop=True)
    if not raw["CSBID"].is_unique:
        raise RuntimeError("CSBID is not unique within the county")

    geoms = raw.geometry.values
    n_parts = shapely.get_num_geometries(geoms)
    n_invalid = int((~shapely.is_valid(geoms)).sum())
    cleaned = clean_polygons(geoms)
    ok = np.array([g is not None for g in cleaned])
    cand = gpd.GeoDataFrame(
        {"source_id": raw["CSBID"].astype(str).to_numpy(), "rank": np.arange(1, n_county + 1)},
        geometry=gpd.GeoSeries(cleaned, crs=raw.crs),
    )
    attr_cols = csb.cdl_columns(raw.columns)
    attrs = {year: raw[col].fillna(0).to_numpy().astype(np.int64) for year, col in attr_cols.items()}
    area_before = float(shapely.area(geoms).sum())
    cand, attrs = cand[ok].reset_index(drop=True), {y: a[ok] for y, a in attrs.items()}
    area_after = float(cand.geometry.area.sum())
    _step(
        filters,
        "geometry repair (make_valid, largest Polygon part)",
        n_county,
        len(cand),
        f"{n_invalid} invalid polygon(s) repaired; {int((n_parts > 1).sum())} multi-part polygon(s) reduced to their "
        f"largest part ({(area_before - area_after) / 1e4:.2f} ha discarded)",
    )
    prov["invalid_polygons_repaired"] = n_invalid
    prov["multipart_polygons"] = int((n_parts > 1).sum())

    cand["area_ha"] = cand.geometry.area / 1e4

    # county guard: CSB's county attribute already assigns every polygon to one county
    in_county, straddling = _county_guard(cand, filters, "CSB county attribute")
    cand, attrs = cand[in_county].reset_index(drop=True), {y: a[in_county] for y, a in attrs.items()}
    prov["polygons_straddling_county_line"] = straddling

    cand["eligible"] = cand["area_ha"] >= min_area_ha
    _step(filters, f"area >= {min_area_ha:g} ha (EPSG:5070)", len(cand), int(cand["eligible"].sum()))
    return cand, prov, filters, attrs


def _segment_candidates(rasters: dict[int, Path], min_area_ha: float) -> tuple[gpd.GeoDataFrame, dict, list]:
    seg, stats = segment_fields(rasters, min_area_ha)
    seg = seg.to_crs(EQUAL_AREA_CRS) if seg.crs != EQUAL_AREA_CRS else seg.set_crs(EQUAL_AREA_CRS, allow_override=True)
    points = seg.geometry.representative_point()
    order = np.lexsort((np.round(points.x.to_numpy(), 1), -np.round(points.y.to_numpy(), 1)))
    seg = seg.iloc[order].reset_index(drop=True)
    cand = gpd.GeoDataFrame(
        {"source_id": [None] * len(seg), "rank": np.zeros(len(seg), dtype=int)}, geometry=seg.geometry.values, crs=seg.crs
    )
    cand["area_ha"] = cand.geometry.area / 1e4
    filters: list = []
    _step(
        filters,
        f"segmentation: connected cropland components >= {min_area_ha:g} ha",
        stats["components_after_sieve"],
        len(cand),
        "components smaller than the minimum field size dropped",
    )
    # the CropScape rasters are already clipped to the county; this only matters for other rasters
    in_county, _ = _county_guard(cand, filters, "rasters are clipped to the county")
    cand = cand[in_county].reset_index(drop=True)
    cand["eligible"] = cand["area_ha"] >= min_area_ha
    return cand, stats, filters


# --------------------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------------------
def build_fields(source: str = "auto", min_area_ha: float = MIN_FIELD_HA, max_fields: int | None = None) -> gpd.GeoDataFrame:
    """Field outlines + 2008-2024 crop histories for the county. See the module docstring."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    if min_area_ha <= 0:
        raise ValueError("min_area_ha must be > 0")
    t_start = time.monotonic()
    timings: dict[str, float] = {}

    def lap(name: str, t0: float) -> None:
        timings[name] = round(time.monotonic() - t0, 2)

    # --- CDL rasters (needed by both sources) ---
    t0 = time.monotonic()
    log(f"fields: CDL rasters {YEARS[0]}-{YEARS[-1]} for county {FIPS}")
    cdl_infos = cdl.fetch_all(YEARS, FIPS)
    rasters = cdl.available_rasters(cdl_infos)
    missing_years = [y for y in YEARS if y not in rasters]
    if not rasters:
        raise RuntimeError("no CDL raster could be downloaded for any year - cannot build crop histories")
    if missing_years:
        log(f"!!! fields: crop histories will NOT contain {missing_years} (CDL download failed)")
    lap("cdl_rasters", t0)

    # --- outlines ---
    t0 = time.monotonic()
    used, fallback_reason, csb_prov, seg_stats, attrs = None, None, None, None, {}
    if source in ("auto", "csb"):
        try:
            log("fields: outlines from USDA NASS Crop Sequence Boundaries")
            cand, csb_prov, filters, attrs = _csb_candidates(min_area_ha)
            if not cand["eligible"].any():
                raise RuntimeError("no CSB polygon in the county reaches the minimum field size")
            used = "csb"
        except Exception as exc:
            if source == "csb":
                raise
            fallback_reason = f"{type(exc).__name__}: {exc}"
            log(f"!!! fields: CSB path failed ({fallback_reason}); falling back to CDL segmentation")
    if used is None:
        log("fields: outlines by segmenting contiguous cropland from the CDL rasters")
        cand, seg_stats, filters = _segment_candidates(rasters, min_area_ha)
        used = "cdl-segment"
    lap("outlines", t0)

    # --- majority CDL class per polygon and year ---
    t0 = time.monotonic()
    zonal = cdl.zonal_majority(cand.geometry, rasters, quiet=True)
    years = zonal.years
    lap("zonal_majority", t0)

    eligible = cand["eligible"].to_numpy()
    agreement = None
    if used == "csb" and attrs:
        agreement = {
            "what": "raster-majority CDL class (this pipeline) vs the CSB polygon's own CDL<year> attribute",
            "all_county_polygons": agreement_stats(zonal.codes, years, attrs),
            f"polygons_ge_{min_area_ha:g}_ha": agreement_stats(zonal.codes, years, attrs, eligible),
        }

    # --- cropland rule ---
    crop_ok, crop_detail = cropland_mask(zonal.codes, years)
    keep = eligible & crop_ok
    crop_stats = {
        "rejected": {reason: int((eligible & mask).sum()) for reason, mask in crop_detail["rejected"].items()},
        "kept_only_by_recent_clause": int((eligible & crop_detail["kept_by_recent_clause"]).sum()),
        "recent_years": crop_detail["recent_years"],
    }
    _step(
        filters,
        "cropland rule",
        int(eligible.sum()),
        int(keep.sum()),
        "; ".join(f"{reason}: {n}" for reason, n in crop_stats["rejected"].items())
        + f"; kept only by the recent-years clause: {crop_stats['kept_only_by_recent_clause']}",
    )
    if not keep.any():
        raise RuntimeError("no field passes the cropland rule")

    rows = np.flatnonzero(keep)
    out = cand.iloc[rows].reset_index(drop=True)
    codes = zonal.codes[rows]
    share = zonal.share[rows]
    if used == "cdl-segment":
        out["rank"] = np.arange(1, len(out) + 1)
    if agreement is not None:
        agreement["final_fields"] = agreement_stats(zonal.codes, years, attrs, rows)

    # --- ids, then the optional cap ---
    out["field_id"] = field_ids(out["rank"])
    n_uncapped = len(out)
    picks = spatial_subset(out.geometry, max_fields)
    if len(picks) < len(out):
        out, codes, share = out.iloc[picks].reset_index(drop=True), codes[picks], share[picks]
        _step(filters, f"max_fields={max_fields}", n_uncapped, len(out), MAX_FIELDS_RULE)

    # --- output frame ---
    t0 = time.monotonic()
    geom_ll, good = to_lonlat_polygons(out.geometry)
    if not good.all():
        n_before = len(out)
        out, codes, share = out[good].reset_index(drop=True), codes[good], share[good]
        geom_ll = geom_ll[good].reset_index(drop=True)
        _step(filters, "geometry unusable after reprojection to EPSG:4326", n_before, len(out))
    fields = gpd.GeoDataFrame(
        {
            "field_id": out["field_id"].to_numpy(),
            "crop_history": crop_histories(codes, years),
            "area_ha": out["area_ha"].to_numpy().astype(float).round(4),
        },
        geometry=gpd.GeoSeries(geom_ll.values, crs="EPSG:4326"),
    )[OUTPUT_COLUMNS]
    fields = fields.reset_index(drop=True)
    lap("output", t0)

    # --- provenance ---
    _, outline_url, county_km2 = _county_outline(EQUAL_AREA_CRS)
    freq = Counter(NAME_LUT[c] for c in codes.ravel() if c)
    area = fields["area_ha"].to_numpy()
    has = codes != 0
    caveats = [
        "CDL is a satellite classification, not ground truth: single-year errors (especially grass/pasture vs "
        "fallow/alfalfa/other hay) appear in histories as they are.",
        "The crop of a field-year is the plurality class of its pixels; mixed or split fields get the largest "
        "share's class (see majority_share in the provenance).",
        "Ties between classes go to the lowest CDL code.",
    ]
    if used == "csb":
        caveats.append(
            "CSB outlines are derived by NASS from 8 years of CDL at 30 m, not surveyed boundaries; they describe "
            f"the {csb_prov.get('window', '?')} crop sequence, while histories cover {years[0]}-{years[-1]}."
        )
    else:
        caveats.append(
            "Outlines were segmented from 30 m CDL pixels without a road network: neighbouring fields with the same "
            "crop sequence can be merged, and outlines are only accurate to about one pixel."
        )
    if missing_years:
        caveats.append(f"CDL could not be downloaded for {missing_years}; those years are missing from every history.")

    per_year = {}
    for y, info in sorted(cdl_infos.items()):
        entry = {
            k: info.get(k)
            for k in ("url", "route", "downloaded_via", "downloaded_at", "error", "pixel_size_m", "width", "height", "origin", "crs", "bytes")
        }
        if y in years:
            col = years.index(y)
            entry["fields_with_data"] = int(has[:, col].sum())
            entry["mean_majority_share"] = round(float(share[has[:, col], col].mean()), 4) if has[:, col].any() else None
        per_year[str(y)] = entry

    provenance = {
        "source": used,
        "requested_source": source,
        "fallback_reason": fallback_reason,
        "state": STATE_ABBR,
        "county": COUNTY_NAME,
        "fips": FIPS,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parameters": {"min_area_ha": min_area_ha, "max_fields": max_fields, "years": [YEARS[0], YEARS[-1]]},
        "county_boundary": {"url": outline_url, "area_km2": round(county_km2, 1) if county_km2 else None},
        "csb": csb_prov,
        "segmentation": seg_stats,
        "cdl": {
            "service": cdl.CDL_SERVICE_URL,
            "file_url_pattern": cdl.CDL_CACHE_URL,
            "years_available": [int(y) for y in years],
            "years_missing": [int(y) for y in missing_years],
            "per_year": per_year,
            "grids": zonal.grids,
            "majority_rule": "plurality of pixels whose centre is inside the polygon; values 0/nodata/81 do not vote; "
            "polygons with no pixel centre use all touched pixels; ties -> lowest code",
        },
        "filters": filters,
        "cropland_rule": {"text": CROPLAND_RULE, **crop_stats},
        "agreement_with_csb_attributes": agreement,
        "field_id_rule": FIELD_ID_RULE[used],
        "max_fields_rule": MAX_FIELDS_RULE if max_fields is not None else None,
        "counts": {
            "fields": int(len(fields)),
            "fields_before_max_fields": int(n_uncapped),
            "field_years": int(has.sum()),
            "fields_with_all_years": int((has.sum(axis=1) == len(YEARS)).sum()),
        },
        "area": {
            "total_ha": round(float(area.sum()), 1),
            "share_of_county": round(float(area.sum()) / 100 / county_km2, 4) if county_km2 else None,
            "min_ha": round(float(area.min()), 2),
            "p25_ha": round(float(np.quantile(area, 0.25)), 2),
            "median_ha": round(float(np.median(area)), 2),
            "mean_ha": round(float(area.mean()), 2),
            "p75_ha": round(float(np.quantile(area, 0.75)), 2),
            "p95_ha": round(float(np.quantile(area, 0.95)), 2),
            "max_ha": round(float(area.max()), 2),
        },
        "crop_frequency_field_years": dict(freq.most_common()),
        "timings_s": {**timings, "total": round(time.monotonic() - t_start, 2)},
        "caveats": caveats,
    }
    fields.attrs["provenance"] = provenance
    fields.attrs["source_ids"] = [None if (s is None or s != s) else str(s) for s in out["source_id"].tolist()]
    share_txt = f" ({provenance['area']['share_of_county']:.0%} of the county)" if county_km2 else ""
    log(f"fields: {len(fields):,} fields from {used}, {area.sum():,.0f} ha{share_txt}, {provenance['timings_s']['total']:.1f}s")
    return fields


# --------------------------------------------------------------------------------------
# reporting / CLI
# --------------------------------------------------------------------------------------
_LETTERS = {
    "corn": "C",
    "winter wheat": "W",
    "sorghum": "S",
    "fallow": "F",
    "alfalfa": "A",
    "soybeans": "B",
    "triticale": "T",
    "grass/pasture": "g",
    "other hay": "h",
    "developed": "x",
}


def compact_history(history: str | dict, years: list[int] | None = None) -> str:
    """One character per year (see `_LETTERS`; d = double crop, ? = anything else, . = no data)."""
    hist = json.loads(history) if isinstance(history, str) else history
    years = list(YEARS) if years is None else years
    chars = []
    for year in years:
        name = hist.get(str(year))
        if name is None:
            chars.append(".")
        elif name.startswith("double crop"):
            chars.append("d")
        else:
            chars.append(_LETTERS.get(name, "?"))
    return "".join(chars)


def describe(fields: gpd.GeoDataFrame, samples: int = 20) -> str:
    """Human-readable report of a `build_fields` result."""
    prov = fields.attrs["provenance"]
    out = [f"source: {prov['source']} (requested: {prov['requested_source']})"]
    if prov["fallback_reason"]:
        out.append(f"  FELL BACK because: {prov['fallback_reason']}")
    if prov["csb"]:
        c = prov["csb"]
        out.append(f"CSB release: {c.get('release')}  layer {c.get('layer')}  ({c.get('county_features'):,} county polygons)")
        out.append(f"  {c.get('url')}")
    if prov["segmentation"]:
        s = prov["segmentation"]
        out.append(f"segmentation window: {s['window_years'][0]}-{s['window_years'][-1]}, {s['cropland_ha']:,.0f} ha cropland pixels")
    out.append(f"CDL years: {len(prov['cdl']['years_available'])} available, missing: {prov['cdl']['years_missing'] or 'none'}")
    out.append("filters:")
    for f in prov["filters"]:
        out.append(f"  {f['step']}: {f['in']:,} -> {f['out']:,} (dropped {f['dropped']:,})" + (f"  [{f['note']}]" if f["note"] else ""))
    out.append(f"cropland rule: {prov['cropland_rule']['text']}")
    agreement = prov["agreement_with_csb_attributes"]
    if agreement:
        out.append("agreement of raster majority with CSB's CDL attributes (rate, n compared):")
        scopes = [k for k in agreement if k != "what"]
        out.append("  year     " + "".join(f"{s:<26}" for s in scopes))
        for year in agreement[scopes[0]]["per_year"]:
            cells = [agreement[s]["per_year"][year] for s in scopes]
            out.append(f"  {year}     " + "".join(f"{c['rate']:.4f} ({c['compared']:>6,})        " for c in cells))
        cells = [agreement[s]["overall"] for s in scopes]
        out.append("  overall  " + "".join(f"{c['rate']:.4f} ({c['compared']:>6,})        " for c in cells))

    a = prov["area"]
    share_txt = f" = {a['share_of_county']:.1%} of the county" if a["share_of_county"] is not None else ""
    out.append(f"fields: {len(fields):,}   total {a['total_ha']:,.0f} ha{share_txt}")
    out.append(
        f"area_ha: min {a['min_ha']} / p25 {a['p25_ha']} / median {a['median_ha']} / mean {a['mean_ha']} / "
        f"p75 {a['p75_ha']} / p95 {a['p95_ha']} / max {a['max_ha']}"
    )
    area = fields["area_ha"].to_numpy()
    bins = [0, 5, 10, 20, 40, 70, 150, 1e9]
    labels = ["<5", "5-10", "10-20", "20-40", "40-70", "70-150", ">150"]
    hist = np.histogram(area, bins=bins)[0]
    out.append("area classes (ha): " + "  ".join(f"{lab}: {n:,}" for lab, n in zip(labels, hist)))

    freq = prov["crop_frequency_field_years"]
    total = sum(freq.values())
    out.append(f"crop frequency ({total:,} field-years):")
    for name, n in list(freq.items())[:16]:
        out.append(f"  {name:<36} {n:>8,}  {100 * n / total:5.1f}%")
    if len(freq) > 16:
        rest = sum(list(freq.values())[16:])
        out.append(f"  ({len(freq) - 16} other classes){'':<17} {rest:>8,}  {100 * rest / total:5.1f}%")
    out.append("mean share of the winning class per year: " + "  ".join(
        f"{y}:{v['mean_majority_share']:.2f}" for y, v in prov["cdl"]["per_year"].items() if v.get("mean_majority_share") is not None
    ))

    if samples > 0:
        rng = np.random.default_rng(20055)
        picks = np.sort(rng.choice(len(fields), size=min(samples, len(fields)), replace=False))
        years = list(range(prov["parameters"]["years"][0], prov["parameters"]["years"][1] + 1))
        out.append(
            f"{len(picks)} random histories {years[0]}-{years[-1]} "
            "(C corn, W winter wheat, S sorghum, F fallow, A alfalfa, B soybeans, T triticale, g grass/pasture, "
            "h other hay, d double crop, x developed, ? other, . no data):"
        )
        for i in picks:
            out.append(f"  {fields['field_id'].iloc[i]}  {area[i]:7.1f} ha  {compact_history(fields['crop_history'].iloc[i], years)}")
    out.append("timings (s): " + ", ".join(f"{k} {v}" for k, v in prov["timings_s"].items()))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build field outlines + crop histories for the county.")
    ap.add_argument("--source", choices=SOURCES, default="auto")
    ap.add_argument("--max-fields", type=int, default=None, help="keep a spatially even subset of this many fields")
    ap.add_argument("--min-area-ha", type=float, default=MIN_FIELD_HA)
    ap.add_argument("--out", type=Path, default=None, help="write a GeoParquet (+ .provenance.json) for inspection")
    ap.add_argument("--samples", type=int, default=20, help="number of random field histories to print")
    args = ap.parse_args(argv)

    fields = build_fields(source=args.source, min_area_ha=args.min_area_ha, max_fields=args.max_fields)
    print()
    print(describe(fields, samples=args.samples))

    # the contract check, with placeholder soil / slope columns (those are other steps' job)
    from pipeline.schema import COLUMNS, SchemaError, validate_fields

    probe = fields.copy()
    probe["organic_matter_pct"] = np.nan
    probe["slope_pct"] = np.nan
    try:
        validate_fields(probe[COLUMNS], "build_fields output + placeholder soil/slope columns")
        print("schema check (with placeholder organic_matter_pct / slope_pct): OK")
    except SchemaError as exc:
        print(f"schema check FAILED: {exc}", file=sys.stderr)
        return 1

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        inspect = fields.copy()
        inspect["source_id"] = fields.attrs["source_ids"]
        inspect.to_parquet(args.out, index=False)
        sidecar = args.out.with_name(args.out.name + ".provenance.json")
        sidecar.write_text(json.dumps(fields.attrs["provenance"], indent=2))
        print(f"wrote {args.out} ({len(inspect):,} rows; extra column source_id) and {sidecar.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
