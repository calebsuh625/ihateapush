"""Daily climate-model output for the county from Earth Engine `NASA/GDDP-CMIP6`.

One request = one model x one scenario x one calendar year: every daily image of that year is
reduced to the mean over the county box (`scale=27830`, the collection's 0.25 degree grid) and the
365/366 rows come back in one `getInfo`. Each answer is cached as one JSON file under
`data/raw/climate/gddp/`, so nothing is ever asked twice; the file also keeps what the request cost.

Cost is measured the way `pipeline/soil/bare_soil.py` does it (Earth Engine computation profiles,
`parse_profile_eecu`). `budget_spent_eecu_s` adds up the cost stored in every cache file, so
`--max-eecu-hours` is a limit for the whole piece across reruns, not just for one run.

Models. The design note picked six before looking at any result; one that lacks a band or a scenario
on Earth Engine is replaced by the next unused name of SUBSTITUTES (`choose_models`).

Calendars. Counted on what Earth Engine actually returns: one image with values for every real
calendar date, for every model. A model whose own calendar is shorter is padded by repeating a day:
a 365-day model (GFDL-ESM4, NorESM2-MM) has its 365 days laid on days 1-365 of a leap year and
30 December repeated on the 31st; the 360-day UKESM1-0-LL has 5 or 6 days a year, evenly spaced,
that repeat the day before. So nothing is missing. `pipeline/climate/bias.py` counts the repeated
days for the report, and `to_real_calendar` there still fills any day that would come back empty.

    python -m pipeline.climate.fetch_gddp --check      # which models have all bands and scenarios
    python -m pipeline.climate.fetch_gddp --estimate   # requests still to send, rough EECU-hours
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from pipeline.region import DATA_DIR as REGION_DATA_DIR  # noqa: E402  (data/ for Finney, data/regions/<fips>/ otherwise)

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REGION_DATA_DIR
FIELDS_PATH = DATA_DIR / "fields.parquet"
CACHE_DIR = DATA_DIR / "raw" / "climate" / "gddp"
LOG_DIR = DATA_DIR / "logs" / "phase3"

COLLECTION = "NASA/GDDP-CMIP6"
BANDS = ("hurs", "huss", "pr", "rlds", "rsds", "sfcWind", "tas", "tasmax", "tasmin")
PREFERRED_MODELS = ("GFDL-ESM4", "IPSL-CM6A-LR", "MPI-ESM1-2-HR", "MRI-ESM2-0", "UKESM1-0-LL", "MIROC6")
SUBSTITUTES = ("ACCESS-CM2", "NorESM2-MM", "CNRM-ESM2-1", "EC-Earth3")
SCENARIOS = ("ssp245", "ssp585")
HISTORICAL = "historical"
FIRST_YEAR = 2000
LAST_YEAR = 2060
HISTORICAL_LAST_YEAR = 2014  # the historical run ends here; the scenarios start in 2015
LEAD_IN_YEAR = 1999  # only 1 Oct - 31 Dec, for the first wheat season
LEAD_IN_START = "1999-10-01"
SCALE_M = 27830
METHOD_VERSION = "1"  # bump when the request changes: old cache files are then ignored

# County box used when data/fields.parquet is missing: its total bounds, rounded to 0.01 degree.
FINNEY_BOX = (-101.10, 37.74, -100.23, 38.27)

EECU_S_PER_REQUEST = 13.8  # the lead's pilot; replaced by this run's own measurements once it has some
WORKERS = 4
PILOT_REQUESTS = 3
DEFAULT_MAX_EECU_HOURS = 5.0
CHECK_YEARS = {HISTORICAL: 2012, "ssp245": 2016, "ssp585": 2016}  # leap years, one per scenario


class GddpError(RuntimeError):
    pass


class BudgetExceeded(GddpError):
    pass


@dataclass(frozen=True)
class Request:
    model: str
    scenario: str  # historical, ssp245 or ssp585
    year: int
    start: str  # first day, ISO
    end: str  # day after the last day, ISO

    @property
    def label(self) -> str:
        return f"{self.model} {self.scenario} {self.year}"


def county_bounds(fields_path: Path = FIELDS_PATH) -> tuple[float, float, float, float]:
    """(west, south, east, north) of the fields table, rounded to 0.01 degree; FINNEY_BOX without it."""
    try:
        import pyarrow.parquet as pq

        meta = json.loads(pq.read_schema(fields_path).metadata[b"geo"])
        col = meta["columns"][meta["primary_column"]]
        if "bbox" in col:
            return tuple(round(float(v), 2) for v in col["bbox"])
        import geopandas as gpd

        return tuple(round(float(v), 2) for v in gpd.read_parquet(fields_path).total_bounds)
    except Exception:  # noqa: BLE001 - no file, no geo metadata: the documented fallback
        return FINNEY_BOX


def year_request(model: str, scenario: str, year: int) -> Request:
    start = LEAD_IN_START if year == LEAD_IN_YEAR else f"{year}-01-01"
    return Request(model, scenario, int(year), start, f"{year + 1}-01-01")


def requests_for(models) -> list[Request]:
    """Every request of the full job: historical 1999 (Oct-Dec) to 2014, then both scenarios 2015-2060."""
    out = []
    for model in models:
        for year in range(LEAD_IN_YEAR, HISTORICAL_LAST_YEAR + 1):
            out.append(year_request(model, HISTORICAL, year))
        for scenario in SCENARIOS:
            for year in range(HISTORICAL_LAST_YEAR + 1, LAST_YEAR + 1):
                out.append(year_request(model, scenario, year))
    return out


# --------------------------------------------------------------------------- cache


def _signature(request: Request, bounds) -> str:
    payload = {
        "collection": COLLECTION, "bands": list(BANDS), "scale_m": SCALE_M, "method": METHOD_VERSION,
        "bounds": [float(b) for b in bounds], "model": request.model, "scenario": request.scenario,
        "start": request.start, "end": request.end,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def cache_path(cache_dir: Path, request: Request, bounds) -> Path:
    return Path(cache_dir) / f"{request.model}__{request.scenario}__{request.year}__{_signature(request, bounds)}.json"


def check_rows(rows, request: Request) -> list[list]:
    """`rows` as [[date, 9 values or nulls], ...] sorted by date, or GddpError when the answer is unusable."""
    if not isinstance(rows, list) or not rows:
        raise GddpError(f"Earth Engine returned no days for {request.label}")
    width = 1 + len(BANDS)
    clean = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != width or not isinstance(row[0], str):
            raise GddpError(f"Earth Engine returned a malformed day for {request.label}")
        if not request.start <= row[0] < request.end:
            raise GddpError(f"Earth Engine returned {row[0]}, outside {request.start}..{request.end} ({request.label})")
        values = [None if v is None or v != v else float(v) for v in row[1:]]
        clean.append([row[0], *values])
    clean.sort(key=lambda r: r[0])
    dates = [r[0] for r in clean]
    if len(set(dates)) != len(dates):
        raise GddpError(f"Earth Engine returned a date twice for {request.label}")
    full = [r for r in clean if all(v is not None for v in r[1:])]
    partial = [r for r in clean if any(v is not None for v in r[1:]) and any(v is None for v in r[1:])]
    if partial:
        raise GddpError(f"{request.label}: {len(partial)} day(s) have some bands but not all, e.g. {partial[0][0]}")
    if len(full) < 0.95 * len(clean):  # a 360-day calendar fills 360 of 366 days; anything emptier is a gap
        raise GddpError(f"{request.label}: only {len(full)} of {len(clean)} days have values")
    return clean


def read_cache(cache_dir: Path, request: Request, bounds) -> dict | None:
    """The cached answer ({"rows": ..., "eecu_s": ..., ...}) or None if absent or unusable."""
    try:
        doc = json.loads(cache_path(cache_dir, request, bounds).read_text())
        doc["rows"] = check_rows(doc["rows"], request)
    except (OSError, ValueError, KeyError, TypeError, GddpError):
        return None
    return doc


def write_cache(cache_dir: Path, request: Request, bounds, rows, eecu_s: float | None, seconds: float | None) -> None:
    path = cache_path(cache_dir, request, bounds)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    tmp.write_text(
        json.dumps(
            {
                "collection": COLLECTION, "model": request.model, "scenario": request.scenario, "year": request.year,
                "start": request.start, "end": request.end, "bounds": list(bounds), "scale_m": SCALE_M,
                "method_version": METHOD_VERSION, "columns": ["date", *BANDS], "eecu_s": eecu_s, "seconds": seconds,
                "rows": rows,
            }
        )
    )
    os.replace(tmp, path)


def budget_spent_eecu_s(cache_dir: Path | None = None) -> tuple[float, int, int]:
    """(EECU-seconds, files, files without a measured cost) over every cache file of this piece.

    A file whose profile did not come back counts as the mean of the measured ones.
    """
    cache_dir = CACHE_DIR if cache_dir is None else cache_dir
    known, unknown = [], 0
    for path in Path(cache_dir).glob("*__*.json"):
        try:
            cost = json.loads(path.read_text()).get("eecu_s")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(cost, (int, float)):
            known.append(float(cost))
        else:
            unknown += 1
    mean = sum(known) / len(known) if known else EECU_S_PER_REQUEST
    return sum(known) + unknown * mean, len(known) + unknown, unknown


def cached_request_seconds(cache_dir: Path | None = None) -> float:
    """Seconds the cached requests took, added up (they ran several at a time, so wall time was shorter)."""
    cache_dir = CACHE_DIR if cache_dir is None else cache_dir
    total = 0.0
    for path in Path(cache_dir).glob("*__*.json"):
        try:
            seconds = json.loads(path.read_text()).get("seconds")
        except (OSError, ValueError, AttributeError):
            continue
        total += float(seconds) if isinstance(seconds, (int, float)) else 0.0
    return total


def estimate(requests: list[Request], bounds, cache_dir: Path | None = None, eecu_s_per_request: float | None = None) -> dict:
    """What is still to ask and what it should cost. Touches no network."""
    cache_dir = CACHE_DIR if cache_dir is None else cache_dir
    pending = [r for r in requests if read_cache(cache_dir, r, bounds) is None]
    spent, files, _ = budget_spent_eecu_s(cache_dir)
    if eecu_s_per_request is None:
        eecu_s_per_request = spent / files if files else EECU_S_PER_REQUEST
    return {
        "requests": len(requests), "cached": len(requests) - len(pending), "pending": len(pending),
        "eecu_s_per_request": eecu_s_per_request, "pending_eecu_h": len(pending) * eecu_s_per_request / 3600,
        "spent_eecu_h": spent / 3600, "cache_files": files,
    }


def estimate_lines(est: dict) -> list[str]:
    return [
        f"GDDP requests: {est['requests']} needed, {est['cached']} cached, {est['pending']} to send",
        f"cost: about {est['eecu_s_per_request']:.1f} EECU-seconds a request -> about {est['pending_eecu_h']:.2f} "
        f"EECU-hours still to spend; {est['spent_eecu_h']:.2f} EECU-hours already spent ({est['cache_files']} cached answers)",
    ]


# --------------------------------------------------------------------------- models


def choose_models(availability: dict, preferred=PREFERRED_MODELS, substitutes=SUBSTITUTES) -> tuple[list[str], list[dict]]:
    """(the models to use, substitutions made).

    `availability` is {model: {scenario: {"n": images in the check year, "bands": [...]}}}. A model is
    usable when historical, ssp245 and ssp585 each have images and all of BANDS. An unusable preferred
    model is replaced, in order, by the next unused usable name of `substitutes`.
    """

    def problem(model: str) -> str | None:
        info = availability.get(model) or {}
        for scenario in (HISTORICAL, *SCENARIOS):
            entry = info.get(scenario) or {}
            if not entry.get("n"):
                return f"no {scenario} images"
            missing = [b for b in BANDS if b not in (entry.get("bands") or [])]
            if missing:
                return f"{scenario} lacks {', '.join(missing)}"
        return None

    spare = [m for m in substitutes if problem(m) is None]
    models, swaps = [], []
    for model in preferred:
        why = problem(model)
        if why is None:
            models.append(model)
            continue
        if not spare:
            raise GddpError(f"{model} is unusable ({why}) and no substitute model is left")
        models.append(spare.pop(0))
        swaps.append({"dropped": model, "reason": why, "replacement": models[-1]})
    return models, swaps


def availability_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / f"_availability_v{METHOD_VERSION}.json"


def load_availability(cache_dir: Path | None = None, engine_factory=None, log=lambda msg: None) -> dict:
    """Bands and image counts per candidate model and scenario; asked once (metadata only), then cached."""
    cache_dir = CACHE_DIR if cache_dir is None else cache_dir
    path = availability_path(cache_dir)
    try:
        doc = json.loads(path.read_text())
        if set(doc["models"]) >= set(PREFERRED_MODELS + SUBSTITUTES):
            return doc["models"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    engine = (engine_factory or GddpEngine)()
    found = engine.availability(PREFERRED_MODELS + SUBSTITUTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"check_years": CHECK_YEARS, "models": found}, indent=1))
    log(f"model availability asked from Earth Engine and cached in {path}")
    return found


# --------------------------------------------------------------------------- Earth Engine layer


class GddpEngine:
    """The only code here that talks to Earth Engine. `daily(request, bounds)` is one request."""

    def __init__(self, profile: bool = True) -> None:
        from pipeline import satellite as sat

        self.ee = sat.EarthEngine().ee  # ee.Initialize(project=EE_PROJECT or None), with the clean login error
        self.profile = profile
        self._lock = threading.Lock()
        self.costs: list[dict] = []  # one entry per finished request: label, seconds, eecu_s

    def availability(self, models) -> dict:
        ee = self.ee
        col = ee.ImageCollection(COLLECTION)
        ask = {}
        for model in models:
            for scenario, year in CHECK_YEARS.items():
                part = (
                    col.filter(ee.Filter.eq("model", model)).filter(ee.Filter.eq("scenario", scenario))
                    .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                )
                bands = ee.Algorithms.If(part.size().gt(0), ee.Image(part.first()).bandNames(), ee.List([]))
                ask[f"{model}|{scenario}"] = ee.Dictionary({"n": part.size(), "bands": bands})
        got = ee.Dictionary(ask).getInfo()
        out: dict = {}
        for key, value in got.items():
            model, scenario = key.split("|")
            out.setdefault(model, {})[scenario] = {"n": int(value["n"]), "bands": list(value["bands"])}
        return out

    def daily(self, request: Request, bounds) -> tuple[list[list], float | None, float]:
        """(rows, EECU-seconds or None, seconds). rows = [[date, 9 band means or None], ...]."""
        ee = self.ee
        region = ee.Geometry.Rectangle([float(b) for b in bounds], None, False)
        col = (
            ee.ImageCollection(COLLECTION)
            .filter(ee.Filter.eq("model", request.model)).filter(ee.Filter.eq("scenario", request.scenario))
            .filterDate(request.start, request.end)
        )

        def per_day(img):
            means = img.reduceRegion(reducer=ee.Reducer.mean(), geometry=region, scale=SCALE_M)
            return ee.Feature(None, means).set("date", img.date().format("YYYY-MM-dd"))

        days = ee.FeatureCollection(col.map(per_day))
        started = time.monotonic()
        profile_ids: list[str] = []
        try:
            if self.profile:
                with ee.data.profiling(profile_ids.append):
                    info = days.getInfo()
            else:
                info = days.getInfo()
        finally:
            seconds = time.monotonic() - started
            eecu = _profile_eecu(profile_ids) if profile_ids else None
            with self._lock:
                self.costs.append({"label": request.label, "seconds": seconds, "eecu_s": eecu})
        rows = []
        for feature in info.get("features", []):
            props = feature.get("properties", {})
            rows.append([props.get("date"), *[props.get(b) for b in BANDS]])
        return rows, eecu, seconds


def _profile_eecu(profile_ids: list[str]) -> float | None:
    from pipeline.soil.bare_soil import fetch_profile_eecu

    return fetch_profile_eecu(profile_ids)


def run_spent_eecu_s(costs: list[dict]) -> float:
    """EECU-seconds of this run's finished requests; one without a profile counts as the mean of the others."""
    known = [c["eecu_s"] for c in costs if c["eecu_s"] is not None]
    unknown = len(costs) - len(known)
    return float(sum(known) + unknown * (sum(known) / len(known) if known else EECU_S_PER_REQUEST))


# --------------------------------------------------------------------------- retries and orchestration


def fetch_one(
    compute, request: Request, bounds, cache_dir: Path, *, force: bool = False, stats=None, sleep=time.sleep,
    max_attempts: int = 6, log=lambda msg: None,
) -> list[list]:
    """Rows of one request: from the cache, else from `compute(request, bounds) -> (rows, eecu_s, seconds)`.

    Quota, concurrency, time-out and network errors are retried with exponential backoff (the same
    classes as `pipeline/satellite.py:classify_error`); anything else stops the run. The answer is
    checked (`check_rows`) before it is cached.
    """
    from pipeline.satellite import classify_error

    if not force:
        cached = read_cache(cache_dir, request, bounds)
        if cached is not None:
            if stats:
                stats.add("cache_hits")
            return cached["rows"]
    attempt = 0
    while True:
        attempt += 1
        if stats:
            stats.add("requests")
        try:
            rows, eecu_s, seconds = compute(request, bounds)
            break
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below, anything unknown is re-raised
            kind = classify_error(exc)
            reason = " ".join(str(exc).split())[:160]
            if kind == "fatal":
                raise GddpError(f"Earth Engine request failed ({request.label}): {reason}") from exc
            if attempt >= max_attempts:
                raise GddpError(
                    f"Earth Engine request still failing after {attempt} attempts ({request.label}): {reason}\n"
                    "Finished requests are cached: run the same command again later to resume."
                ) from exc
            base = 15 if kind == "busy" else 4
            delay = min(240.0, base * 2 ** (attempt - 1)) * (0.75 + 0.5 * random.random())
            log(f"{request.label}: {reason}; attempt {attempt}/{max_attempts}, waiting {delay:.0f}s")
            if stats:
                stats.add("retries")
            sleep(delay)
    rows = check_rows(rows, request)
    write_cache(cache_dir, request, bounds, rows, eecu_s, seconds)
    if stats:
        stats.add("computed")
    return rows


def collect(
    requests: list[Request], bounds, cache_dir: Path | None = None, *, compute=None, engine_factory=None,
    force: bool = False, workers: int = WORKERS, stats=None, sleep=time.sleep, log=lambda msg: None,
    max_eecu_s: float | None = None, already_spent_eecu_s: float = 0.0, costs: list | None = None,
) -> dict[Request, list[list]]:
    """{request: rows} for every request, asking Earth Engine only for what is not cached.

    `max_eecu_s`: once `already_spent_eecu_s` plus what this call has spent reaches it, no further
    request is sent and BudgetExceeded is raised. Requests already running finish and are cached, so
    the overshoot is at most `workers` requests. `costs` (a list) receives one entry per request sent.
    """
    cache_dir = CACHE_DIR if cache_dir is None else cache_dir
    results: dict[Request, list[list]] = {}
    pending: list[Request] = []
    for request in requests:
        cached = None if force else read_cache(cache_dir, request, bounds)
        if cached is None:
            pending.append(request)
        else:
            if stats:
                stats.add("cache_hits")
            results[request] = cached["rows"]
    log(f"{len(requests)} requests needed: {len(requests) - len(pending)} cached, {len(pending)} to send")
    if not pending:
        return results

    costs = costs if costs is not None else []
    lock = threading.Lock()
    if compute is None:
        engine = (engine_factory or GddpEngine)()
        log("Earth Engine initialised")
        raw = engine.daily
    else:
        raw = compute

    def guarded(request, bounds_):
        if max_eecu_s is not None:
            with lock:
                used = already_spent_eecu_s + run_spent_eecu_s(costs)
            if used >= max_eecu_s:
                raise BudgetExceeded(
                    f"stopped at the budget: {used / 3600:.2f} EECU-hours used by this piece, limit {max_eecu_s / 3600:.2f}. "
                    "Finished requests are cached: run the same command again (with a new budget) to continue."
                )
        rows, eecu_s, seconds = raw(request, bounds_)
        with lock:
            costs.append({"label": request.label, "seconds": seconds, "eecu_s": eecu_s})
        return rows, eecu_s, seconds

    started = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(fetch_one, guarded, r, bounds, cache_dir, force=force, stats=stats, sleep=sleep, log=log): r
            for r in pending
        }
        try:
            not_done = set(futures)
            while not_done:
                finished, not_done = wait(not_done, return_when=FIRST_COMPLETED)
                for future in finished:
                    request = futures[future]
                    results[request] = future.result()  # raises the first failure
                    done += 1
                    if done % 20 == 0 or done == len(pending):
                        elapsed = time.monotonic() - started
                        with lock:
                            used = run_spent_eecu_s(costs)
                        log(
                            f"{done}/{len(pending)} requests done ({request.label}); {elapsed / 60:.1f} min elapsed, "
                            f"~{elapsed / done * (len(pending) - done) / 60:.1f} min left; {used / 3600:.2f} EECU-hours this call"
                        )
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return results


def pilot_requests(requests: list[Request], bounds, cache_dir: Path, n: int = PILOT_REQUESTS) -> list[Request]:
    """Up to `n` uncached full-year requests, from different models where possible."""
    pending = [r for r in requests if r.year != LEAD_IN_YEAR and read_cache(cache_dir, r, bounds) is None]
    picked, seen = [], set()
    for r in pending:
        if r.model not in seen:
            picked.append(r)
            seen.add(r.model)
    picked += [r for r in pending if r not in picked]
    return picked[:n]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="ask (once) which models have all bands and scenarios")
    parser.add_argument("--estimate", action="store_true", help="requests still to send and rough EECU-hours; asks nothing")
    args = parser.parse_args(argv)
    bounds = county_bounds()
    if args.check:
        models, swaps = choose_models(load_availability())
        print("models: " + ", ".join(models))
        for s in swaps:
            print(f"  {s['dropped']} replaced by {s['replacement']} ({s['reason']})")
        return 0
    try:
        doc = json.loads(availability_path(CACHE_DIR).read_text())
        models, _ = choose_models(doc["models"])
        note = ""
    except (OSError, ValueError, KeyError, TypeError, GddpError):
        models, note = list(PREFERRED_MODELS), " (model availability not checked yet: the model list may change)"
    for line in estimate_lines(estimate(requests_for(models), bounds)):
        print(line)
    if note:
        print(note.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
