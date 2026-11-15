"""Per-field traits for the planner -> data/field_traits.parquet (contract: field_traits_schema.py).

    .venv/bin/python -m pipeline.field_traits --no-openet      # shape + crops + SSURGO only (no Earth Engine)
    .venv/bin/python -m pipeline.field_traits --estimate       # OpenET pilot: 50 fields x 1 year, then the estimate
    .venv/bin/python -m pipeline.field_traits                  # build; fetches OpenET only inside the budget

Three things per field:

1. Wind erodibility (`weg`, `wei`) from SSURGO via Soil Data Access: the field's dominant map unit
   (pipeline/soil/ssurgo.py, cached tiles) and that map unit's dominant component (largest
   comppct_r, ties to the smaller cokey). If the dominant component has no group (water, pits), the
   largest component that has one is used. Cached under data/raw/ssurgo/muweg_*.parquet.

2. Growing-season water use (`et_gs_mm`) from OpenET (Earth Engine
   `OpenET/ENSEMBLE/CONUS/GRIDMET/MONTHLY/v2_0`, band `et_ensemble_mad`, monthly mm): the April-
   September sum per year, averaged over each field on a 60 m grid, then the mean over 2016-2024.
   One request = one chunk of fields x one year; the fields are painted into an image and reduced
   by group (the cheap pattern from pipeline/satellite.py). Every request is cached under
   data/raw/openet/ and its measured cost is added to data/raw/openet/cost.json, so the budget
   (`--max-eecu-hours`, default 3) holds across runs. Optional: with `--no-openet`, or when Earth
   Engine is not available, `et_gs_mm` is null and the irrigation guess uses shape and crops only.
   Growing-season rain for the same years is the county mean of gridMET (`IDAHO_EPSCOR/GRIDMET`,
   band `pr`), one small request per year, cached next to the OpenET files.

3. Irrigation (`irrigation`, `irrigated_prob`, `irrigation_basis`): evidence added up on a log-odds
   scale, then squashed to a probability. Every field gets an answer (no "uncertain").

     signal                                           points
     pivot circle (outline fills >= 0.85 of its       +2.5
       smallest enclosing circle)
     thirsty crops (share of years in corn,           +4 x (share - 0.25)    0 -> -1, 0.5 -> +1, 1 -> +3
       soybeans or alfalfa, 2008-2024)
     fallow (share of years fallow)                   -6 x share             0.25 -> -1.5
     water use (mean Apr-Sep ET minus Apr-Sep rain,   (excess - ET_EXCESS_MID) / ET_EXCESS_SCALE, within +-4
       2016-2024, if OpenET ran)

   irrigated_prob = 1 / (1 + exp(-points)); irrigated when >= 0.5. The crop and shape signals are
   the ones in pipeline/causal/irrigation.py (imported, not edited) but use the whole crop history:
   the causal analysis must only look before its switch year, the planner has no such limit.
   ET is the strongest single signal where it exists: an irrigated field uses far more water than
   the rain supplies, a dryland field cannot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.field_traits_schema import (
    COLUMNS,
    FIELD_TRAITS_PATH,
    WEG_VALUES,
    WEI_MAX,
    check_field_traits,
    summarize,
)
from pipeline.region import DATA_DIR as REGION_DATA_DIR, nass_path  # noqa: E402  (data/ for Finney, data/regions/<fips>/ otherwise)

ROOT = Path(__file__).resolve().parents[1]
FIELDS_PATH = REGION_DATA_DIR / "fields.parquet"
NASS_PATH = nass_path()
CAUSAL_FIELDS_PATH = REGION_DATA_DIR / "causal_fields.parquet"
SSURGO_CACHE = ROOT / "data" / "raw" / "ssurgo"
OPENET_CACHE = ROOT / "data" / "raw" / "openet"
LOG_DIR = REGION_DATA_DIR / "logs" / "phase3"
ET_BY_CROP_PATH = LOG_DIR / "openet_by_crop.csv"

# ---- irrigation rule
PIVOT_FILL = 0.85
PIVOT_POINTS = 2.5
THIRSTY_SLOPE, THIRSTY_MID = 4.0, 0.25
FALLOW_SLOPE = 6.0
ET_EXCESS_MID = 175.0  # mm of Apr-Sep ET above Apr-Sep rain where the ET evidence is neutral: the dip between the two
# humps of the county histogram (dryland fields centre near +70 mm, irrigated near +290 mm)
ET_EXCESS_SCALE = 40.0  # mm per point
ET_POINTS_MAX = 4.0
THIRSTY = ("corn", "soybeans", "alfalfa")

# ---- OpenET
OPENET_COLLECTION = "OpenET/ENSEMBLE/CONUS/GRIDMET/MONTHLY/v2_0"
OPENET_BAND = "et_ensemble_mad"
GRIDMET_COLLECTION = "IDAHO_EPSCOR/GRIDMET"
ET_YEARS = tuple(range(2016, 2025))
GS_MONTHS = (4, 9)  # April-September
GRID_CRS = "EPSG:32614"
GRID_M = 60
GRID_TRANSFORM = [GRID_M, 0, 0, 0, -GRID_M, 0]
OPENET_METHOD_VERSION = 2  # bump when the request changes: cached files of another version are ignored
CHUNK_SIZE = 300
PILOT_FIELDS, PILOT_YEAR = 50, 2022
DEFAULT_BUDGET_EECU_HOURS = 3.0
ESTIMATE_SAFETY = 1.3  # the pilot is small; allow this much error before starting the full job
M2_PER_ACRE = 4046.8564224

# ---- SSURGO
WEG_SQL = (
    "SELECT mu.mukey, c.cokey, c.comppct_r, c.compname, c.weg, c.wei "
    "FROM mapunit AS mu LEFT JOIN component AS c ON c.mukey = mu.mukey "
    "WHERE mu.mukey IN ({mukeys}) ORDER BY mu.mukey, c.comppct_r DESC, c.cokey"
)
WEG_COLUMNS = ["mukey", "cokey", "comppct_r", "compname", "weg", "wei"]
MUKEY_CHUNK = 400


def _sha(text: str, length: int = 10) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


_WEG_VERSION = _sha(WEG_SQL, 6)


# --------------------------------------------------------------------------- wind erodibility


def load_weg_rows(mukeys, client, cache_dir: Path = SSURGO_CACHE, *, say=lambda msg: None) -> pd.DataFrame:
    """Component weg/wei rows for `mukeys`; only map units not yet cached are requested."""
    cache_dir = Path(cache_dir)
    wanted = sorted({str(k) for k in mukeys if k is not None and str(k).isdigit()}, key=lambda k: (len(k), k))
    frames = []
    for path in sorted(cache_dir.glob(f"muweg_{_WEG_VERSION}_*.parquet")):
        try:
            frames.append(pd.read_parquet(path))
        except Exception:  # noqa: BLE001  an unreadable cache file is simply fetched again
            pass
    have = set().union(*[set(f["mukey"].astype(str)) for f in frames]) if frames else set()
    missing = [k for k in wanted if k not in have]
    for start in range(0, len(missing), MUKEY_CHUNK):
        chunk = missing[start : start + MUKEY_CHUNK]
        sql = WEG_SQL.format(mukeys=", ".join(chunk))
        raw = client.query(sql, what=f"wind erodibility for {len(chunk)} map unit(s)")
        frame = pd.DataFrame(raw, columns=WEG_COLUMNS) if len(raw) else pd.DataFrame(columns=WEG_COLUMNS)
        absent = sorted(set(chunk) - set(frame["mukey"].astype(str)))  # remember "asked, nothing there"
        if absent:
            frame = pd.concat([frame, pd.DataFrame({"mukey": absent})], ignore_index=True)
        frame = frame.astype(object).where(frame.notna(), None).astype(str).replace({"None": None, "nan": None})
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"muweg_{_WEG_VERSION}_{_sha(sql)}.parquet"
        tmp = path.with_suffix(".parquet.part")
        frame.to_parquet(tmp, index=False)
        tmp.replace(path)
        frames.append(frame)
    if missing:
        say(f"ssurgo: fetched wind erodibility for {len(missing)} map unit(s)")
    if not frames:
        return pd.DataFrame(columns=WEG_COLUMNS)
    rows = pd.concat([f[WEG_COLUMNS] for f in frames], ignore_index=True)
    rows["mukey"] = rows["mukey"].astype(str)
    return rows[rows["mukey"].isin(set(wanted))].drop_duplicates(["mukey", "cokey"], ignore_index=True)


def clean_weg(value) -> str | None:
    """SSURGO weg as one of WEG_VALUES ('4l' -> '4L', '3.0' -> '3'), else None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip().upper()
    if text.endswith(".0"):
        text = text[:-2]
    return text if text in WEG_VALUES else None


def clean_wei(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number if 0 <= number <= WEI_MAX else float("nan")


def mapunit_weg(rows: pd.DataFrame) -> pd.DataFrame:
    """mukey -> weg (text or None), wei (float or NaN): the dominant component's values.

    Dominant = largest comppct_r, a tie goes to the smaller cokey. A dominant component without a
    group (water, gravel pits) passes the turn to the largest component that has one.
    """
    comp = rows[rows["cokey"].notna()].copy()
    comp["pct"] = pd.to_numeric(comp["comppct_r"], errors="coerce").fillna(0.0)
    comp["weg"] = comp["weg"].map(clean_weg)
    comp["wei"] = comp["wei"].map(clean_wei)
    comp["has"] = comp["weg"].notna() | comp["wei"].notna()
    comp["cokey_n"] = pd.to_numeric(comp["cokey"], errors="coerce")
    comp = comp.sort_values(["mukey", "has", "pct", "cokey_n"], ascending=[True, False, False, True])
    first = comp.drop_duplicates("mukey").set_index("mukey")
    return first[["weg", "wei"]]


def field_weg(fields, *, cache_dir: Path = SSURGO_CACHE, offline: bool = False, say=print) -> pd.DataFrame:
    """weg / wei per field (index = position in `fields`). All null, with a note, if SSURGO is unavailable."""
    from pipeline.soil import ssurgo

    empty = pd.DataFrame({"weg": [None] * len(fields), "wei": np.full(len(fields), np.nan)}, index=fields.index)
    try:
        units = ssurgo.field_mapunits(fields, cache_dir=cache_dir, offline=True)
    except ssurgo.SoilDataError:
        if offline:
            say("field_traits: SSURGO tiles are not cached; weg and wei left empty")
            return empty
        units = ssurgo.field_mapunits(fields, cache_dir=cache_dir, offline=False)
    client = ssurgo._OfflineClient() if offline else ssurgo.SDAClient()
    try:
        rows = load_weg_rows(units["dominant_mukey"].dropna().unique(), client, cache_dir, say=say)
    except ssurgo.SoilDataError as exc:
        say(f"field_traits: wind erodibility not available ({str(exc)[:120]}); weg and wei left empty")
        return empty
    per_mu = mapunit_weg(rows)
    out = empty.copy()
    out["weg"] = units["dominant_mukey"].map(per_mu["weg"]).to_numpy(dtype=object)
    out["weg"] = out["weg"].where(out["weg"].notna(), None)
    out["wei"] = units["dominant_mukey"].map(per_mu["wei"]).to_numpy(dtype=float)
    return out


# --------------------------------------------------------------------------- irrigation rule


def crop_shares(history: dict) -> tuple[float, float, int]:
    """(thirsty share, fallow share, number of years) of one field's crop history."""
    crops = [str(c) for c in history.values()]
    n = len(crops)
    if n == 0:
        return 0.0, 0.0, 0
    return sum(c in THIRSTY for c in crops) / n, sum(c == "fallow" for c in crops) / n, n


def irrigation_points(fill: float, thirsty: float, fallow: float, et_excess_mm: float | None) -> dict[str, float]:
    """The evidence table of the module docstring: points per signal (positive = irrigated)."""
    points = {
        "pivot": PIVOT_POINTS if fill >= PIVOT_FILL else 0.0,
        "thirsty": THIRSTY_SLOPE * (thirsty - THIRSTY_MID),
        "fallow": -FALLOW_SLOPE * fallow,
    }
    if et_excess_mm is not None and not math.isnan(et_excess_mm):
        points["et"] = float(np.clip((et_excess_mm - ET_EXCESS_MID) / ET_EXCESS_SCALE, -ET_POINTS_MAX, ET_POINTS_MAX))
    return points


def classify_irrigation(fill: float, history: dict, et_excess_mm: float | None = None) -> tuple[str, float, str]:
    """(irrigation, irrigated_prob, irrigation_basis) for one field."""
    thirsty, fallow, n = crop_shares(history)
    points = irrigation_points(fill, thirsty, fallow, et_excess_mm)
    prob = 1.0 / (1.0 + math.exp(-sum(points.values())))
    label = "irrigated" if prob >= 0.5 else "dryland"
    sign = 1 if label == "irrigated" else -1
    words = {
        "pivot": "pivot-circle outline",
        "thirsty": f"corn/soybeans/alfalfa in {round(thirsty * n)} of {n} years",
        "fallow": f"fallow in {round(fallow * n)} of {n} years",
        "et": (f"water use {'only ' if points['et'] < 0 <= et_excess_mm else ''}{abs(et_excess_mm):.0f} mm "
               f"{'above' if et_excess_mm >= 0 else 'below'} rain" if "et" in points else ""),
    }
    backing = sorted((k for k, v in points.items() if v * sign > 0.25), key=lambda k: -abs(points[k]))
    basis = "; ".join(words[k] for k in backing[:2]) if backing else "weak evidence either way"
    if "et" not in points:
        basis += " (no water-use data)"
    return label, round(prob, 3), basis


# --------------------------------------------------------------------------- OpenET (Earth Engine)


def gs_window(year: int) -> tuple[str, str]:
    return f"{year}-{GS_MONTHS[0]:02d}-01", f"{year}-{GS_MONTHS[1] + 1:02d}-01"


def openet_cache_path(cache_dir: Path, chunk, year: int) -> Path:
    key = _sha(json.dumps([OPENET_METHOD_VERSION, OPENET_COLLECTION, OPENET_BAND, GRID_CRS, GRID_TRANSFORM, GS_MONTHS, int(year), chunk.digest]), 24)
    return Path(cache_dir) / f"et_{int(year)}_{key}.json"


def read_openet_cache(cache_dir: Path, chunk, year: int) -> dict | None:
    try:
        values = json.loads(openet_cache_path(cache_dir, chunk, year).read_text())["values"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(values, dict) or any(fid not in values for fid in chunk.field_ids):
        return None
    return {fid: values[fid] for fid in chunk.field_ids}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def sums_to_et(sums) -> float | None:
    """[sum of ET, number of pixels with data] -> mean ET in mm, None when no pixel had data."""
    total, count = float(sums[0]), float(sums[1])
    return round(total / count, 1) if count > 0 else None


class CostLedger:
    """Earth Engine cost of this piece, kept across runs in data/raw/openet/cost.json."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {"eecu_seconds": 0.0, "requests": 0, "unmeasured_requests": 0, "runs": []}
        self.run = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "eecu_seconds": 0.0, "requests": 0}

    @property
    def hours(self) -> float:
        return self.data["eecu_seconds"] / 3600.0

    def add(self, eecu_s: float | None, fallback_s: float) -> None:
        """Record one request. An unmeasured request is charged the running mean (or `fallback_s`)."""
        if eecu_s is None:
            self.data["unmeasured_requests"] += 1
            eecu_s = self.run["eecu_seconds"] / self.run["requests"] if self.run["requests"] else fallback_s
        self.data["eecu_seconds"] += eecu_s
        self.data["requests"] += 1
        self.run["eecu_seconds"] += eecu_s
        self.run["requests"] += 1

    def save(self) -> None:
        if self.run["requests"]:
            runs = [r for r in self.data["runs"] if r.get("started") != self.run["started"]]
            self.data["runs"] = runs + [dict(self.run)]
            _write_json(self.path, self.data)


class OpenETEngine:
    """The only code here that talks to Earth Engine."""

    def __init__(self):
        import ee

        ee.Initialize(project=os.environ.get("EE_PROJECT") or None)
        ee.data.setDeadline(6 * 60 * 1000)
        self.ee = ee

    def _profiled(self, request):
        from pipeline.soil.bare_soil import fetch_profile_eecu

        ids: list[str] = []
        with self.ee.data.profiling(ids.append):
            result = request.getInfo()
        return result, (fetch_profile_eecu(ids) if ids else None)

    def reduce(self, chunk, year: int) -> tuple[dict, float | None]:
        """One request: ({field_id: Apr-Sep ET in mm or None}, EECU-seconds or None)."""
        from pipeline.satellite import chunk_bounds

        ee = self.ee
        region = ee.Geometry.Rectangle(chunk_bounds(chunk), None, False)
        start, end = gs_window(year)
        months = ee.ImageCollection(OPENET_COLLECTION).filterDate(start, end).filterBounds(region).select(OPENET_BAND)
        # the collection is tiled (several images per month): mosaic each month first, so a pixel
        # that lies in two overlapping tiles is not counted twice, then add the six months
        et = ee.ImageCollection([
            months.filter(ee.Filter.calendarRange(m, m, "month")).mosaic() for m in range(GS_MONTHS[0], GS_MONTHS[1] + 1)
        ]).sum()
        features = ee.FeatureCollection([
            ee.Feature(ee.Geometry(g), {"idx": i}) for i, g in enumerate(chunk.geometries)
        ])
        stack = et.unmask(0).addBands(et.mask()).toFloat().addBands(ee.Image().toInt().paint(features, "idx"))
        request = stack.reduceRegion(
            reducer=ee.Reducer.sum().repeat(2).group(groupField=2, groupName="idx"),
            geometry=region, crs=GRID_CRS, crsTransform=GRID_TRANSFORM, maxPixels=1e9,
        )
        grouped, eecu = self._profiled(request)
        out = {fid: None for fid in chunk.field_ids}
        for group in grouped.get("groups", []):
            out[chunk.field_ids[int(group["idx"])]] = sums_to_et(group["sum"])
        return out, eecu

    def county_rain(self, bounds, year: int) -> tuple[float, float | None]:
        """(Apr-Sep gridMET rain in mm, mean over the box; EECU-seconds or None)."""
        ee = self.ee
        start, end = gs_window(year)
        rain = ee.ImageCollection(GRIDMET_COLLECTION).filterDate(start, end).select("pr").sum()
        request = rain.reduceRegion(reducer=ee.Reducer.mean(), geometry=ee.Geometry.Rectangle(list(bounds), None, False), scale=4000, maxPixels=1e8)
        value, eecu = self._profiled(request)
        return float(value["pr"]), eecu


def fetch_openet(fields, years, *, engine=None, cache_dir: Path = OPENET_CACHE, budget_hours: float = DEFAULT_BUDGET_EECU_HOURS,
                 estimate_only: bool = False, chunk_size: int = CHUNK_SIZE, say=print, log=lambda msg: None) -> tuple[pd.DataFrame, dict]:
    """Per field per year Apr-Sep ET (long frame: field_id, year, et_mm) plus a report dict.

    Order of work: read the cache; if anything is missing, run the pilot (50 fields x 1 year, cached
    like everything else), estimate the rest, and run the largest set of whole years (latest first)
    whose estimate x ESTIMATE_SAFETY fits what is left of the budget. A second, hard check stops
    the run if the measured total reaches the budget anyway. `engine=None` means cache only.
    """
    from pipeline import satellite as sat

    cache_dir = Path(cache_dir)
    ledger = CostLedger(cache_dir / "cost.json")
    chunks = sat.make_chunks(fields, chunk_size)
    results: dict[tuple[int, str], float | None] = {}
    todo: list[tuple[int, object]] = []
    for year in years:
        for chunk in chunks:
            cached = read_openet_cache(cache_dir, chunk, year)
            if cached is None:
                todo.append((year, chunk))
            else:
                results.update({(year, fid): v for fid, v in cached.items()})
    report = {"requests_total": len(years) * len(chunks), "requests_cached": len(years) * len(chunks) - len(todo),
              "requests_run": 0, "years_requested": list(years), "status": "cache only" if engine is None else "ok"}

    def run_one(chunk, year) -> float | None:
        started = time.monotonic()
        values, eecu = engine.reduce(chunk, year)
        _write_json(openet_cache_path(cache_dir, chunk, year), {"year": int(year), "method_version": OPENET_METHOD_VERSION, "unit": "mm Apr-Sep", "eecu_s": eecu, "values": values})
        ledger.add(eecu, fallback_s=30.0)
        ledger.save()
        log(f"openet {year} chunk {chunk.digest[:8]} fields={len(chunk)} seconds={time.monotonic() - started:.1f} eecu_s={eecu}")
        return eecu

    if engine is not None and todo:
        # pilot: PILOT_FIELDS neighbouring fields x one year
        order = sat.spatial_order(fields)[:PILOT_FIELDS]
        pilot_chunk = sat.make_chunks(fields.iloc[np.sort(order)], PILOT_FIELDS)[0]
        pilot_path = openet_cache_path(cache_dir, pilot_chunk, PILOT_YEAR)
        try:
            pilot_eecu = json.loads(pilot_path.read_text())["eecu_s"]
        except (OSError, ValueError, KeyError):
            pilot_eecu = run_one(pilot_chunk, PILOT_YEAR)
        report["pilot_eecu_s"] = pilot_eecu
        # a request's cost is mostly its pixels (the chunk's box), so scale by fields, with a floor
        per_field = (pilot_eecu or 30.0) / len(pilot_chunk)
        est = lambda items: sum(max(per_field * len(c), 2.0) for _, c in items)  # noqa: E731
        report["estimate_all_eecu_h"] = round(est([(y, c) for y in years for c in chunks]) / 3600.0, 3)
        report["estimate_todo_eecu_h"] = round(est(todo) / 3600.0, 3)
        left = budget_hours - ledger.hours
        by_year = {y: [(yy, c) for yy, c in todo if yy == y] for y in sorted({y for y, _ in todo}, reverse=True)}
        chosen: list[tuple[int, object]] = []
        for year, items in by_year.items():  # latest years first; drop the earliest when short
            if (est(chosen + items) * ESTIMATE_SAFETY) / 3600.0 <= left:
                chosen += items
        report["years_run"] = sorted({y for y, _ in chosen})
        say(f"openet: pilot {len(pilot_chunk)} fields x 1 year = {pilot_eecu} EECU-s; all {report['requests_total']} requests about "
            f"{report['estimate_all_eecu_h']:.2f} EECU-h, still to do {report['estimate_todo_eecu_h']:.2f}; spent so far {ledger.hours:.3f} of {budget_hours:g}")
        if estimate_only:
            report["status"] = "estimate only"
        else:
            if len(chosen) < len(todo):
                report["status"] = "partial: budget"
            for n, (year, chunk) in enumerate(chosen, 1):
                if ledger.hours >= budget_hours * 0.97:
                    report["status"] = "stopped: budget reached"
                    say(f"openet: budget reached ({ledger.hours:.2f} EECU-h); stopping")
                    break
                for attempt in range(4):
                    try:
                        run_one(chunk, year)
                        break
                    except Exception as exc:  # noqa: BLE001
                        kind = sat.classify_error(exc)
                        log(f"openet {year} chunk {chunk.digest[:8]} attempt {attempt + 1} failed ({kind}): {str(exc)[:200]}")
                        if kind == "fatal" or attempt == 3:
                            raise
                        time.sleep(5 * 2**attempt)
                cached = read_openet_cache(cache_dir, chunk, year)
                results.update({(year, fid): v for fid, v in cached.items()})
                report["requests_run"] += 1
                if n % 25 == 0:
                    say(f"openet: {n}/{len(chosen)} requests, {ledger.hours:.3f} EECU-h spent in total")
    report["eecu_hours_total"] = round(ledger.hours, 4)
    report["eecu_hours_this_run"] = round(ledger.run["eecu_seconds"] / 3600.0, 4)
    report["unmeasured_requests"] = ledger.data["unmeasured_requests"]
    frame = pd.DataFrame([(fid, y, v) for (y, fid), v in results.items()], columns=["field_id", "year", "et_mm"])
    frame["et_mm"] = frame["et_mm"].astype(float)
    return frame, report


def fetch_rain(bounds, years, *, engine=None, cache_dir: Path = OPENET_CACHE, ledger: CostLedger | None = None) -> dict[int, float]:
    """County Apr-Sep rain per year in mm (gridMET), cached; cache only when `engine` is None."""
    out = {}
    for year in years:
        path = Path(cache_dir) / f"rain_{int(year)}_{_sha(json.dumps([GRIDMET_COLLECTION, GS_MONTHS, [round(b, 4) for b in bounds]]), 10)}.json"
        try:
            out[int(year)] = float(json.loads(path.read_text())["rain_mm"])
            continue
        except (OSError, ValueError, KeyError):
            pass
        if engine is None:
            continue
        value, eecu = engine.county_rain(bounds, year)
        _write_json(path, {"year": int(year), "rain_mm": value, "eecu_s": eecu, "source": GRIDMET_COLLECTION})
        if ledger is not None:
            ledger.add(eecu, fallback_s=5.0)
            ledger.save()
        out[int(year)] = value
    return out


# --------------------------------------------------------------------------- assembling and checks


def et_summary(et_long: pd.DataFrame, rain: dict[int, float]) -> pd.DataFrame:
    """Per field: et_gs_mm (mean over years) and et_excess_mm (mean of ET - rain over years with both)."""
    if et_long.empty:
        return pd.DataFrame(columns=["et_gs_mm", "et_excess_mm", "n_years"])
    df = et_long.dropna(subset=["et_mm"]).copy()
    df["excess"] = df["et_mm"] - df["year"].map(rain)
    grouped = df.groupby("field_id")
    return pd.DataFrame({"et_gs_mm": grouped["et_mm"].mean().round(1), "et_excess_mm": grouped["excess"].mean().round(1), "n_years": grouped["et_mm"].size()})


def build_table(field_ids, fills, histories, weg: pd.DataFrame, et: pd.DataFrame) -> pd.DataFrame:
    """The contract table from already-computed pieces (no I/O; this is what the tests drive)."""
    rows = []
    et_gs = et["et_gs_mm"].to_dict() if len(et) else {}
    et_excess = et["et_excess_mm"].to_dict() if len(et) else {}
    for fid, fill, history in zip(field_ids, fills, histories):
        label, prob, basis = classify_irrigation(float(fill), history, et_excess.get(fid))
        rows.append((fid, label, prob, basis, et_gs.get(fid, np.nan)))
    table = pd.DataFrame(rows, columns=["field_id", "irrigation", "irrigated_prob", "irrigation_basis", "et_gs_mm"])
    table["weg"] = pd.Series(weg["weg"].to_numpy(dtype=object), dtype=object).where(lambda s: s.notna(), None)
    table["wei"] = weg["wei"].to_numpy(dtype=float)
    table["irrigated_prob"] = table["irrigated_prob"].astype(float)
    table["et_gs_mm"] = table["et_gs_mm"].astype(float)
    return table[COLUMNS]


def crop_years(field_ids, histories, areas_ha, labels) -> pd.DataFrame:
    """Long frame: field_id, year, crop, acres, irrigation (one row per field-year in the history)."""
    rows = [
        (fid, int(year), crop, area * 2.4710538, label)
        for fid, history, area, label in zip(field_ids, histories, areas_ha, labels)
        for year, crop in history.items()
    ]
    return pd.DataFrame(rows, columns=["field_id", "year", "crop", "acres", "irrigation"])


def compare_with_nass(cy: pd.DataFrame, nass: pd.DataFrame) -> pd.DataFrame:
    """Irrigated share of each crop's acres: our fields against NASS county harvested acres.

    Only years where NASS published BOTH irrigated and dryland county harvested acres for the crop.
    Our acres are the mapped fields only (they cover part of the county's cropland), so the shares
    are the comparable numbers; the acre totals are shown for scale.
    """
    h = nass[(nass["stat"] == "harvested_acres") & (nass["level"] == "county") & nass["practice"].isin(["irrigated", "dryland"])]
    wide = h.pivot_table(index=["crop", "year"], columns="practice", values="value").reindex(columns=["irrigated", "dryland"]).dropna()
    if wide.empty:
        return pd.DataFrame()
    ours = cy.pivot_table(index=["crop", "year"], columns="irrigation", values="acres", aggfunc="sum").reindex(columns=["irrigated", "dryland"]).fillna(0.0)
    both = wide.add_suffix("_nass").join(ours.add_suffix("_ours"), how="inner").reset_index()
    out = both.groupby("crop").agg(
        years=("year", "nunique"), first_year=("year", "min"), last_year=("year", "max"),
        nass_irrigated_acres=("irrigated_nass", "mean"), nass_dryland_acres=("dryland_nass", "mean"),
        our_irrigated_acres=("irrigated_ours", "mean"), our_dryland_acres=("dryland_ours", "mean"),
    )
    out["nass_irrigated_share"] = out["nass_irrigated_acres"] / (out["nass_irrigated_acres"] + out["nass_dryland_acres"])
    out["our_irrigated_share"] = out["our_irrigated_acres"] / (out["our_irrigated_acres"] + out["our_dryland_acres"])
    return out.round(3)


def compare_with_causal(table: pd.DataFrame, causal: pd.DataFrame) -> dict:
    both = table.merge(causal[["field_id", "irrigation"]], on="field_id", suffixes=("", "_causal"))
    sure = both[both["irrigation_causal"].isin(["irrigated", "dryland"])]
    out = {"fields_compared": int(len(sure)), "agree": float((sure["irrigation"] == sure["irrigation_causal"]).mean()) if len(sure) else None}
    for theirs in ("irrigated", "dryland", "uncertain"):
        sub = both[both["irrigation_causal"] == theirs]
        out[f"causal_{theirs}"] = {"n": int(len(sub)), "we_say_irrigated": int((sub["irrigation"] == "irrigated").sum())}
    return out


def et_by_crop(et_long: pd.DataFrame, cy: pd.DataFrame) -> pd.DataFrame:
    """Median Apr-Sep ET of the fields growing each crop that year, by practice."""
    both = et_long.dropna(subset=["et_mm"]).merge(cy[["field_id", "year", "crop", "irrigation"]], on=["field_id", "year"])
    both = both[both["crop"].isin(["corn", "soybeans", "winter wheat", "sorghum", "alfalfa", "fallow"])]
    out = both.groupby(["crop", "irrigation"])["et_mm"].agg(
        field_years="size", median_et_gs_mm="median", p25=lambda s: s.quantile(0.25), p75=lambda s: s.quantile(0.75)
    ).round(0).reset_index().rename(columns={"irrigation": "practice"})
    return out


def build(*, fields_path: Path = FIELDS_PATH, out_path: Path = FIELD_TRAITS_PATH, openet: bool = True, estimate_only: bool = False,
          budget_hours: float = DEFAULT_BUDGET_EECU_HOURS, offline: bool = False, max_fields: int | None = None, say=print) -> int:
    import geopandas as gpd

    from pipeline.causal.irrigation import circle_fill

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = (LOG_DIR / "field_traits.log").open("a")
    log = lambda msg: (log_file.write(f"{time.strftime('%H:%M:%S')} {msg}\n"), log_file.flush())  # noqa: E731
    fields = gpd.read_parquet(fields_path).reset_index(drop=True)
    if max_fields:
        fields = fields.iloc[:max_fields].reset_index(drop=True)
    histories = [json.loads(h) for h in fields["crop_history"]]
    fills = circle_fill(fields.geometry.to_crs("EPSG:5070").to_numpy())

    engine, et_long, rain, report = None, pd.DataFrame(columns=["field_id", "year", "et_mm"]), {}, {"status": "skipped (--no-openet)"}
    if openet:
        if not offline:
            try:
                engine = OpenETEngine()
            except Exception as exc:  # noqa: BLE001
                say(f"field_traits: Earth Engine not available ({' '.join(str(exc).split())[:120]}); using cached OpenET only")
        et_long, report = fetch_openet(fields, ET_YEARS, engine=engine, budget_hours=budget_hours, estimate_only=estimate_only, say=say, log=log)
        years_have = sorted(et_long["year"].unique()) if len(et_long) else []
        rain = fetch_rain(fields.total_bounds.tolist(), years_have, engine=engine, ledger=CostLedger(OPENET_CACHE / "cost.json"))
        report["eecu_hours_total"] = round(CostLedger(OPENET_CACHE / "cost.json").hours, 4)
        report["rain_mm"] = {int(k): round(v) for k, v in rain.items()}
        et_long = et_long[et_long["year"].isin(list(rain))]
    if estimate_only:
        say(json.dumps(report))
        return 0

    et = et_summary(et_long, rain)
    weg = field_weg(fields, offline=offline, say=say)
    table = build_table(fields["field_id"].tolist(), fills, histories, weg, et)
    problems = check_field_traits(table, fields["field_id"])
    if problems:
        say("field_traits: built table is invalid:\n" + "\n".join(f"  - {p}" for p in problems))
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.part")
    table.to_parquet(tmp, index=False)
    tmp.replace(out_path)

    # ---- checks (full numbers to the log folder, a few lines to the screen)
    cy = crop_years(fields["field_id"], histories, fields["area_ha"], table["irrigation"])
    checks: dict = {"openet": report}
    acres = (fields["area_ha"] * 2.4710538).groupby(table["irrigation"]).sum()
    checks["split"] = {k: {"fields": int((table["irrigation"] == k).sum()), "acres": round(float(acres.get(k, 0.0)))} for k in ("irrigated", "dryland")}
    real = Path(fields_path).resolve() == FIELDS_PATH  # the county checks mean nothing for another fields file
    if real and NASS_PATH.exists():
        cmp = compare_with_nass(cy, pd.read_parquet(NASS_PATH))
        if len(cmp):
            cmp.to_csv(LOG_DIR / "irrigation_vs_nass.csv")
            checks["nass"] = cmp[["years", "first_year", "last_year", "nass_irrigated_share", "our_irrigated_share"]].to_dict("index")
    if real and CAUSAL_FIELDS_PATH.exists():
        checks["causal"] = compare_with_causal(table, pd.read_parquet(CAUSAL_FIELDS_PATH))
    if real and len(et_long):
        by_crop = et_by_crop(et_long, cy)
        by_crop.to_csv(ET_BY_CROP_PATH, index=False)
        checks["et_by_crop"] = {f"{r.crop}/{r.practice}": int(r.median_et_gs_mm) for r in by_crop.itertuples()}
    if real:
        (LOG_DIR / "field_traits_checks.json").write_text(json.dumps(checks, indent=2, default=str))

    say(f"{out_path}: valid")
    say(summarize(table))
    say(f"openet: {report.get('status')}; {report.get('requests_run', 0)} request(s) run, total spent {report.get('eecu_hours_total', 0)} EECU-h")
    if "nass" in checks:
        say("irrigated share of acres, ours vs NASS county: " + "; ".join(f"{c} {v['our_irrigated_share']:.2f} vs {v['nass_irrigated_share']:.2f}" for c, v in checks["nass"].items()))
    if checks.get("causal", {}).get("agree") is not None:
        say(f"agreement with the causal labels (their irrigated/dryland fields): {checks['causal']['agree']:.1%} of {checks['causal']['fields_compared']:,}")
    if real:
        say(f"details: {LOG_DIR / 'field_traits_checks.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fields", type=Path, default=FIELDS_PATH)
    parser.add_argument("--out", type=Path, default=FIELD_TRAITS_PATH)
    parser.add_argument("--no-openet", action="store_true", help="do not use OpenET at all (not even the cache)")
    parser.add_argument("--estimate", action="store_true", help="OpenET pilot and cost estimate only; writes no table")
    parser.add_argument("--max-eecu-hours", type=float, default=DEFAULT_BUDGET_EECU_HOURS, help="hard stop for OpenET across all runs (default 3)")
    parser.add_argument("--offline", action="store_true", help="no network: cached SSURGO and OpenET only")
    parser.add_argument("--max-fields", type=int, default=None, help="first N fields only (testing)")
    args = parser.parse_args(argv)
    return build(fields_path=args.fields, out_path=args.out, openet=not args.no_openet, estimate_only=args.estimate,
                 budget_hours=args.max_eecu_hours, offline=args.offline, max_fields=args.max_fields)


if __name__ == "__main__":
    sys.exit(main())
