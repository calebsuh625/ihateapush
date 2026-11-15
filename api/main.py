"""FastAPI backend: serves the fields table (schema.md) and the rotation plans.

    .venv/bin/python -m uvicorn api.main:app --port 8010      (or: make api)

Endpoints (the frontend is written against exactly these shapes):

    GET /health                    which data file is loaded, and how many fields
    GET /fields                    every field outline, simplified, as one GeoJSON FeatureCollection
    GET /field/{field_id}          one field: full outline, crop history, soil numbers, history stats
    GET /field/{field_id}/plans    the planner's top 3 rotations for that field
    GET /field/{field_id}/satellite  yearly Landsat greenness for that field (optional side file)
    GET /field/{field_id}/soil     yearly satellite organic matter and bare-soil days (optional side file)

Data file, chosen once at startup: $FIELDS_PATH if set, else data/fields.parquet (real), else
data/fields_fake.parquet. The file is validated against schema.md; a bad or missing file stops
the server with a short, readable message instead of a traceback.

Everything is parsed once at startup into plain dicts, and the /fields payload is serialised
(and gzipped) once, so requests are dictionary lookups. Numbers that are missing in the data
(NaN) come out as JSON null; organic matter, slope and area are rounded to 2 decimals.

The satellite numbers live in a side file (data/field_satellite.parquet, or $SATELLITE_PATH; contract in
pipeline/satellite_schema.py). It is optional: missing or invalid, the server logs one line and carries on.
It is only paired with the real fields file unless SATELLITE_PATH is set explicitly.

The only thing this module knows about the planner is `rank_plans(FieldInput) -> list[Plan]`
(see api/planner.py). Pass another function to `create_app(planner=...)` to swap the model.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response

from api.causal import router as causal_router
from api.planner import FieldInput, Plan, Planner, rank_plans
from pipeline import satellite_schema
from pipeline.soil import health_schema
from pipeline.schema import FAKE_PATH, REAL_PATH, ROOT, SchemaError, load_fields, parse_crop_history

log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn console

GEOJSON_MEDIA_TYPE = "application/geo+json"
# Map outlines: drop vertices that move the outline by less than ~3 m, then snap to ~1 m.
MAP_SIMPLIFY_TOLERANCE_DEG = 0.00003  # degrees; ~3.3 m north-south, ~2.6 m east-west at 38°N
MAP_COORD_DECIMALS = 5  # 0.00001° is about 1 m


DEFAULT_SATELLITE_PATH = satellite_schema.SATELLITE_PATH
DEFAULT_SOIL_HEALTH_PATH = health_schema.SOIL_HEALTH_PATH
DEFAULT_SOIL_HEALTH_META_PATH = health_schema.SOIL_HEALTH_META_PATH


class StartupError(RuntimeError):
    """The API cannot start. str(exc) is the whole story, written for a human."""


# --------------------------------------------------------------------------------------
# Choosing and loading the data file
# --------------------------------------------------------------------------------------


def _show(path: Path) -> str:
    """Path relative to the repo when possible (shorter in logs and messages)."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def resolve_fields_path(
    env_value: str | None = None, real_path: Path = REAL_PATH, fake_path: Path = FAKE_PATH
) -> Path:
    """$FIELDS_PATH if set, else the real file, else the fake file. StartupError if there is none."""
    if env_value:
        path = Path(env_value).expanduser()
        if not path.is_file():
            raise StartupError(
                f"Cannot start the API: FIELDS_PATH points to {path}, which does not exist.\n"
                "Unset FIELDS_PATH to use data/fields.parquet (or the fake fields), or fix the path."
            )
        return path
    for path in (Path(real_path), Path(fake_path)):
        if path.is_file():
            return path
    raise StartupError(
        "Cannot start the API: there is no fields file to serve.\n"
        f"  looked for {_show(real_path)} (real data)\n"
        f"  and then   {_show(fake_path)} (fake data)\n"
        "Run `make fake` (30 made-up fields, instant) or `make pipeline` (real data), then start the API again."
    )


def is_fake_file(path: Path) -> bool:
    return "fake" in Path(path).stem.lower()


def schema_error_message(path: Path, exc: SchemaError) -> str:
    shown = _show(path)
    count = len(exc.problems)
    problems = "\n".join(f"  - {problem}" for problem in exc.problems)
    return (
        f"Cannot start the API: {shown} does not match schema.md "
        f"({count} problem{'s' if count != 1 else ''}):\n"
        f"{problems}\n"
        f"Rebuild the file (`make pipeline`, or `make fake` for the fake fields) and start the API again.\n"
        f"To re-check it: .venv/bin/python pipeline/schema.py {shown}"
    )


# --------------------------------------------------------------------------------------
# Small pure helpers (unit-tested in tests/test_api.py)
# --------------------------------------------------------------------------------------


def clean_number(value: Any, decimals: int = 2) -> float | None:
    """A JSON-safe float rounded to `decimals`, or None for None / NaN / infinity."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, decimals)


def ordered_history(history: dict[str, str]) -> dict[str, str]:
    """The same year -> crop mapping with the years ascending."""
    return dict(sorted(history.items(), key=lambda item: int(item[0])))


def compute_stats(history: dict[str, str]) -> dict[str, Any]:
    """The `stats` block of GET /field/{id}.

    A streak is the same crop in consecutive calendar years, so a missing year ends it.
    If two streaks are equally long the most recent one wins.
    """
    by_year = sorted((int(year), crop) for year, crop in history.items())
    counts = Counter(crop for _, crop in by_year)
    distinct = sorted(counts, key=lambda crop: (-counts[crop], crop))

    longest: dict[str, Any] | None = None
    start = previous_year = previous_crop = None
    for year, crop in by_year:
        if previous_year is None or year != previous_year + 1 or crop != previous_crop:
            start = year  # a new streak begins
        length = year - start + 1
        if longest is None or length >= longest["years"]:  # >= : ties go to the later streak
            longest = {"crop": crop, "years": length, "start_year": start, "end_year": year}
        previous_year, previous_crop = year, crop

    covered = len(by_year)
    return {
        "years_covered": covered,
        "distinct_crops": distinct,
        "distinct_crop_count": len(distinct),
        "longest_streak": longest,  # None only for an empty history, which schema.md does not allow
        "pct_years_corn": round(100 * counts.get("corn", 0) / covered, 1) if covered else 0.0,
    }


def _usable(geoms: np.ndarray) -> np.ndarray:
    """True where a geometry is a real, valid, non-empty single Polygon."""
    return (
        (shapely.get_type_id(geoms) == 3)  # 3 = Polygon
        & ~shapely.is_empty(geoms)
        & shapely.is_valid(geoms)
        & (shapely.area(geoms) > 0)
    )


def _simplified(geoms: np.ndarray, tolerance: float, decimals: int) -> np.ndarray:
    return _rounded(shapely.simplify(geoms, tolerance, preserve_topology=True), decimals)


def _rounded(geoms: np.ndarray, decimals: int) -> np.ndarray:
    return shapely.set_coordinates(geoms.copy(), np.round(shapely.get_coordinates(geoms), decimals))


def _simplify_one(polygon, tolerance: float, decimals: int):
    """The lightest usable version of one outline: simplified + rounded, else rounded, else as it is."""
    one = np.array([polygon], dtype=object)
    attempts = (lambda: _simplified(one, tolerance, decimals), lambda: _rounded(one, decimals))
    for attempt in attempts:
        try:
            candidate = attempt()
            if _usable(candidate)[0]:
                return candidate[0]
        except shapely.errors.GEOSException:
            continue
    return polygon


def counter_clockwise(geoms: np.ndarray) -> np.ndarray:
    """Exterior rings counter-clockwise, holes clockwise: what RFC 7946 GeoJSON asks for."""
    if hasattr(shapely, "orient_polygons"):  # shapely >= 2.1
        return shapely.orient_polygons(geoms)
    from shapely.geometry.polygon import orient

    return np.array([orient(polygon, 1.0) for polygon in geoms], dtype=object)


def simplify_for_map(
    geoms: np.ndarray, tolerance: float = MAP_SIMPLIFY_TOLERANCE_DEG, decimals: int = MAP_COORD_DECIMALS
) -> np.ndarray:
    """Lighter outlines for the overview map: simplified, coordinates rounded to `decimals`.

    Never returns an empty or invalid polygon. If simplifying + rounding breaks one (a field
    only a metre or two wide, say), that polygon falls back to its original outline rounded,
    and if even rounding breaks it, to the original untouched.
    """
    geoms = np.asarray(geoms, dtype=object)
    try:  # all at once (fast path)
        result = _simplified(geoms, tolerance, decimals)
        redo = ~_usable(result)
    except shapely.errors.GEOSException:  # one odd outline must not take the whole map down
        result, redo = geoms.copy(), np.ones(len(geoms), dtype=bool)
    for index in np.flatnonzero(redo):
        result[index] = _simplify_one(geoms[index], tolerance, decimals)
    return result


def polygon_coordinates(polygon, drop_repeats: bool = False) -> list[list[list[float]]]:
    """GeoJSON `coordinates` of a shapely Polygon: [exterior, hole, ...], each a list of [lon, lat].

    `drop_repeats` removes a point identical to the one before it (rounding creates those).
    """
    rings = []
    for ring in (polygon.exterior, *polygon.interiors):
        xy = shapely.get_coordinates(ring)
        if drop_repeats and len(xy) > 1:
            keep = np.ones(len(xy), dtype=bool)
            keep[1:] = (xy[1:] != xy[:-1]).any(axis=1)
            xy = xy[keep]
        rings.append(xy.tolist())
    return rings


def dump_json(payload: Any) -> bytes:
    """Compact, strictly valid JSON (raises rather than emit NaN or Infinity)."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def plan_to_json(plan: Plan) -> dict[str, Any]:
    return {"name": plan.name, "rotation": list(plan.rotation), "scores": asdict(plan.scores), "reason": plan.reason}


# --------------------------------------------------------------------------------------
# Satellite side file
# --------------------------------------------------------------------------------------


def resolve_satellite_path(
    env_value: str | None, fields_source: Path, real_path: Path = REAL_PATH, default: Path = DEFAULT_SATELLITE_PATH
) -> Path | None:
    """$SATELLITE_PATH if set; else the default file, but only next to the real fields file."""
    if env_value:
        return Path(env_value).expanduser()
    if is_fake_file(fields_source):
        return None
    try:
        is_real = Path(fields_source).resolve() == Path(real_path).resolve()
    except OSError:
        is_real = False
    return Path(default) if is_real else None


def load_satellite_index(path: Path | None) -> dict[str, list[dict[str, Any]]] | None:
    """field_id -> that field's yearly rows (ascending, already rounded), or None when unavailable.

    Never raises: the satellite file is optional, so every problem is a log line.
    """
    if path is None:
        return None
    try:
        df = satellite_schema.load_satellite(path)
    except FileNotFoundError:
        log.info("no satellite data: run make satellite (looked for %s)", _show(path))
        return None
    except satellite_schema.SatelliteSchemaError as exc:
        log.warning("satellite file ignored, serving without it: %s", str(exc).replace("\n", " "))
        return None
    except Exception as exc:  # optional data must never stop the server
        log.warning("satellite file ignored, serving without it: %s: %s", _show(path), exc)
        return None
    index: dict[str, list[dict[str, Any]]] = {}
    df = df.sort_values(["field_id", "year"])
    for field_id, year, peak, cover, n_obs in zip(
        df["field_id"], df["year"], df["ndvi_peak"], df["cover_frac"], df["n_obs"]
    ):
        index.setdefault(str(field_id), []).append(
            {
                "year": int(year),
                "ndvi_peak": clean_number(peak, 3),
                "cover_frac": clean_number(cover, 3),
                "n_obs": clean_number(n_obs, 1),
            }
        )
    n_rows = sum(len(rows) for rows in index.values())
    log.info("Loaded satellite data for %s fields (%s field-years) from %s", f"{len(index):,}", f"{n_rows:,}", _show(path))
    return index


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def satellite_summary(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    peaks = [(r["year"], r["ndvi_peak"]) for r in rows if r["ndvi_peak"] is not None]
    covers = [r["cover_frac"] for r in rows if r["cover_frac"] is not None]
    trend = None
    if len(peaks) >= 5:
        x = np.array([y for y, _ in peaks], dtype=float)
        v = np.array([p for _, p in peaks], dtype=float)
        slope = float(np.polyfit(x, v, 1)[0]) * 10
        trend = clean_number(slope, 3)
    return {
        "ndvi_peak_mean": _mean([p for _, p in peaks]),
        "cover_frac_mean": _mean(covers),
        "ndvi_peak_trend_per_decade": trend,
    }


def satellite_body(field_id: str, index: dict[str, list[dict[str, Any]]] | None) -> dict[str, Any]:
    """Body of GET /field/{id}/satellite."""
    rows = index.get(field_id) if index is not None else None
    if not rows:
        return {
            "field_id": field_id,
            "available": False,
            "reason": "not_built" if index is None else "no_data_for_field",
            "source": satellite_schema.SOURCE,
            "years": [],
            "summary": satellite_summary([]),
        }
    return {
        "field_id": field_id,
        "available": True,
        "source": satellite_schema.SOURCE,
        "years": rows,
        "summary": satellite_summary(rows),
    }


# --------------------------------------------------------------------------------------
# Soil health side file (yearly satellite organic-matter estimate and bare-soil days)
# --------------------------------------------------------------------------------------


def resolve_soil_health_path(
    env_value: str | None, fields_source: Path, real_path: Path = REAL_PATH, default: Path = DEFAULT_SOIL_HEALTH_PATH
) -> Path | None:
    """$SOIL_HEALTH_PATH if set; else the default file, but only next to the real fields file."""
    return resolve_satellite_path(env_value, fields_source, real_path, default)


def resolve_soil_health_meta_path(
    env_value: str | None, fields_source: Path, real_path: Path = REAL_PATH, default: Path = DEFAULT_SOIL_HEALTH_META_PATH
) -> Path | None:
    return resolve_satellite_path(env_value, fields_source, real_path, default)


def load_soil_health_index(path: Path | None) -> dict[str, list[dict[str, Any]]] | None:
    """field_id -> that field's yearly rows (ascending, rounded), or None. Never raises."""
    if path is None:
        return None
    try:
        df = health_schema.load_soil_health(path)
    except FileNotFoundError:
        log.info("no soil health data: run make soil-health (looked for %s)", _show(path))
        return None
    except health_schema.SoilHealthSchemaError as exc:
        log.warning("soil health file ignored, serving without it: %s", str(exc).replace("\n", " "))
        return None
    except Exception as exc:  # optional data must never stop the server
        log.warning("soil health file ignored, serving without it: %s: %s", _show(path), exc)
        return None
    index: dict[str, list[dict[str, Any]]] = {}
    df = df.sort_values(["field_id", "year"])
    for field_id, year, om, n_obs, conf, bare in zip(
        df["field_id"], df["year"], df["om_pred"], df["n_obs"], df["confidence"], df["bare_days"]
    ):
        index.setdefault(str(field_id), []).append(
            {
                "year": int(year),
                "om_pred": clean_number(om, 2),
                "confidence": clean_number(conf, 2),
                "n_obs": clean_number(n_obs, 1),
                "bare_days": clean_number(bare, 1),
            }
        )
    n_rows = sum(len(rows) for rows in index.values())
    log.info("Loaded soil health for %s fields (%s field-years) from %s", f"{len(index):,}", f"{n_rows:,}", _show(path))
    return index


def load_soil_health_meta(path: Path | None) -> dict[str, Any]:
    """The meta file, validated; {} when missing or invalid (recommended/noise/verdict then null)."""
    if path is None:
        return {}
    try:
        return health_schema.load_meta(path)
    except FileNotFoundError:
        log.info("no soil health meta file (looked for %s)", _show(path))
    except health_schema.SoilHealthSchemaError as exc:
        log.warning("soil health meta ignored: %s", str(exc).replace("\n", " "))
    except Exception as exc:
        log.warning("soil health meta ignored: %s: %s", _show(path), exc)
    return {}


def soil_health_summary(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    oms = [r["om_pred"] for r in rows if r["om_pred"] is not None]
    bare = [(r["year"], r["bare_days"]) for r in rows if r["bare_days"] is not None]
    trend = None
    if len(bare) >= 5:
        x = np.array([y for y, _ in bare], dtype=float)
        v = np.array([b for _, b in bare], dtype=float)
        trend = clean_number(float(np.polyfit(x, v, 1)[0]) * 10, 2)
    return {
        "om_pred_mean": _mean(oms),
        "om_pred_years": len(oms) if rows else None,
        "bare_days_mean": _mean([b for _, b in bare]),
        "bare_days_trend_per_decade": trend,
    }


def soil_health_body(
    field_id: str,
    index: dict[str, list[dict[str, Any]]] | None,
    meta: dict[str, Any] | None,
    ssurgo_om_pct: float | None,
) -> dict[str, Any]:
    """Body of GET /field/{id}/soil."""
    meta = meta or {}
    noise = meta.get("om_noise_sd")
    body: dict[str, Any] = {
        "field_id": field_id,
        "available": False,
        "source": health_schema.SOURCE,
        "recommended": meta.get("recommended"),
        "om_noise_sd": clean_number(noise, 3) if noise is not None else None,
        "verdict": meta.get("verdict"),
        "ssurgo_om_pct": clean_number(ssurgo_om_pct, 2) if ssurgo_om_pct is not None else None,
        "years": [],
        "summary": soil_health_summary([]),
    }
    rows = index.get(field_id) if index is not None else None
    if not rows:
        body["reason"] = "not_built" if index is None else "no_data_for_field"
        return body
    body["available"] = True
    body["years"] = rows
    body["summary"] = soil_health_summary(rows)
    return body


# --------------------------------------------------------------------------------------
# The in-memory store
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRecord:
    field_id: str
    geometry: dict[str, Any]  # full-resolution GeoJSON Polygon
    crop_history: dict[str, str]  # years ascending
    organic_matter_pct: float | None
    slope_pct: float | None
    area_ha: float
    stats: dict[str, Any]

    def detail(self) -> dict[str, Any]:
        """Body of GET /field/{field_id}."""
        return {
            "field_id": self.field_id,
            "geometry": self.geometry,
            "crop_history": self.crop_history,
            "organic_matter_pct": self.organic_matter_pct,
            "slope_pct": self.slope_pct,
            "area_ha": self.area_ha,
            "stats": self.stats,
        }

    def planner_input(self) -> FieldInput:
        return FieldInput(
            field_id=self.field_id,
            crop_history=self.crop_history,
            organic_matter_pct=self.organic_matter_pct,
            slope_pct=self.slope_pct,
            area_ha=self.area_ha,
        )


@dataclass(frozen=True)
class FieldStore:
    source: Path
    is_fake: bool
    records: dict[str, FieldRecord]
    bbox: list[float]  # [minLon, minLat, maxLon, maxLat]
    fields_geojson: bytes  # body of GET /fields, serialised once
    fields_geojson_gzip: bytes  # the same, gzipped once
    fields_etag: str

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "source": self.source.name, "is_fake": self.is_fake, "field_count": len(self.records)}


def build_store(gdf, source: Path) -> FieldStore:
    """Turn a validated fields GeoDataFrame into the in-memory store."""
    ids = [str(value) for value in gdf["field_id"].tolist()]
    geoms = counter_clockwise(shapely.force_2d(np.asarray(gdf.geometry.values, dtype=object)))  # lon/lat only
    map_geoms = simplify_for_map(geoms)
    areas = [clean_number(value) for value in gdf["area_ha"].tolist()]

    records: dict[str, FieldRecord] = {}
    rows = zip(
        ids,
        geoms,
        gdf["crop_history"].tolist(),
        gdf["organic_matter_pct"].tolist(),
        gdf["slope_pct"].tolist(),
        areas,
    )
    for field_id, geom, history_json, organic_matter, slope, area in rows:
        history = ordered_history(parse_crop_history(history_json))
        records[field_id] = FieldRecord(
            field_id=field_id,
            geometry={"type": "Polygon", "coordinates": polygon_coordinates(geom)},
            crop_history=history,
            organic_matter_pct=clean_number(organic_matter),
            slope_pct=clean_number(slope),
            area_ha=area,
            stats=compute_stats(history),
        )

    bbox = [round(float(value), MAP_COORD_DECIMALS) for value in shapely.total_bounds(map_geoms)]
    collection = {
        "type": "FeatureCollection",
        "bbox": bbox,
        "features": [
            {
                "type": "Feature",
                "id": field_id,
                "properties": {"field_id": field_id, "area_ha": area},
                "geometry": {"type": "Polygon", "coordinates": polygon_coordinates(geom, drop_repeats=True)},
            }
            for field_id, geom, area in zip(ids, map_geoms, areas)
        ],
    }
    payload = dump_json(collection)
    return FieldStore(
        source=Path(source),
        is_fake=is_fake_file(source),
        records=records,
        bbox=bbox,
        fields_geojson=payload,
        fields_geojson_gzip=gzip.compress(payload, compresslevel=6, mtime=0),
        fields_etag=f'W/"{hashlib.sha1(payload).hexdigest()[:20]}"',
    )


def load_store(path: Path) -> FieldStore:
    """Read + validate the file (pipeline.schema.load_fields) and build the store.

    Any problem with the file becomes a StartupError whose message names the file and
    lists what is wrong.
    """
    path = Path(path)
    try:
        gdf = load_fields(path)
    except FileNotFoundError:
        raise StartupError(f"Cannot start the API: {_show(path)} does not exist.") from None
    except SchemaError as exc:
        raise StartupError(schema_error_message(path, exc)) from None
    return build_store(gdf, path)


# --------------------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------------------

router = APIRouter()


def _store(request: Request) -> FieldStore:
    return request.app.state.store


def _record(request: Request, field_id: str) -> FieldRecord:
    record = _store(request).records.get(field_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No field with id {field_id!r}.")
    return record


@router.get("/health", summary="Which data file is loaded")
async def health(request: Request) -> JSONResponse:
    body = _store(request).health()
    body["satellite"] = request.app.state.satellite is not None
    body["soil_health"] = request.app.state.soil_health is not None
    return JSONResponse(body)


@router.get("/fields", summary="All field outlines (simplified) as GeoJSON")
async def fields(request: Request) -> Response:
    store = _store(request)
    headers = {"ETag": store.fields_etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if request.headers.get("if-none-match") == store.fields_etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(store.fields_geojson_gzip, media_type=GEOJSON_MEDIA_TYPE, headers=headers)
    return Response(store.fields_geojson, media_type=GEOJSON_MEDIA_TYPE, headers=headers)


@router.get("/field/{field_id}", summary="One field: outline, crop history, soil, stats")
async def field(request: Request, field_id: str) -> JSONResponse:
    return JSONResponse(_record(request, field_id).detail())


@router.get("/field/{field_id}/plans", summary="Top 3 rotation plans for one field")
async def plans(request: Request, field_id: str) -> JSONResponse:
    record = _record(request, field_id)
    planner: Planner = request.app.state.planner
    ranked = planner(record.planner_input())
    return JSONResponse({"field_id": record.field_id, "plans": [plan_to_json(plan) for plan in ranked]})


@router.get("/field/{field_id}/satellite", summary="Yearly satellite greenness for one field")
async def satellite(request: Request, field_id: str) -> JSONResponse:
    record = _record(request, field_id)
    return JSONResponse(satellite_body(record.field_id, request.app.state.satellite))


@router.get("/field/{field_id}/soil", summary="Yearly satellite soil condition for one field")
async def soil(request: Request, field_id: str) -> JSONResponse:
    record = _record(request, field_id)
    return JSONResponse(
        soil_health_body(
            record.field_id,
            request.app.state.soil_health,
            request.app.state.soil_health_meta,
            clean_number(record.organic_matter_pct, 2) if record.organic_matter_pct is not None else None,
        )
    )


class ReadableStartupErrors:
    """ASGI middleware: make a StartupError the last, clearest thing in the console.

    When startup fails, Starlette reports the failure to the server as a full traceback and
    uvicorn prints it. For our own StartupError the message already says everything, so we
    report just that. Any other exception is passed through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        held: list[dict] = []  # the "startup failed" report, kept back until we know why it failed

        async def hold_failure(message):
            if message["type"] == "lifespan.startup.failed":
                held.append(message)
            else:
                await send(message)

        try:
            await self.app(scope, receive, hold_failure)
        except StartupError as exc:
            if held:
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
            raise
        except BaseException:
            for message in held:
                await send(message)
            raise


def create_app(planner: Planner = rank_plans) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        started = time.perf_counter()
        # module globals are looked up now, so tests can point REAL_PATH / FAKE_PATH elsewhere
        path = resolve_fields_path(os.environ.get("FIELDS_PATH"), REAL_PATH, FAKE_PATH)
        store = load_store(path)
        app.state.store = store
        app.state.satellite = load_satellite_index(
            resolve_satellite_path(os.environ.get("SATELLITE_PATH"), store.source, REAL_PATH, DEFAULT_SATELLITE_PATH)
        )
        app.state.soil_health = load_soil_health_index(
            resolve_soil_health_path(os.environ.get("SOIL_HEALTH_PATH"), store.source, REAL_PATH, DEFAULT_SOIL_HEALTH_PATH)
        )
        app.state.soil_health_meta = load_soil_health_meta(
            resolve_soil_health_meta_path(
                os.environ.get("SOIL_HEALTH_META_PATH"), store.source, REAL_PATH, DEFAULT_SOIL_HEALTH_META_PATH
            )
        ) if app.state.soil_health is not None else {}
        log.info(
            "Loaded %s fields from %s in %.2fs (/fields payload: %s kB, %s kB gzipped)",
            f"{len(store.records):,}",
            _show(store.source),
            time.perf_counter() - started,
            f"{len(store.fields_geojson) / 1000:,.1f}",
            f"{len(store.fields_geojson_gzip) / 1000:,.1f}",
        )
        if store.is_fake:
            log.warning(
                "*** FAKE DATA *** serving the made-up fields in %s. "
                "Run `make pipeline` to build the real data/fields.parquet, then restart.",
                _show(store.source),
            )
        yield

    app = FastAPI(
        title="Crop rotation decision support",
        description="Fields, their crop history and soil, and rotation plans (Phase 1: Finney County, Kansas).",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.planner = planner
    app.include_router(router)
    app.include_router(causal_router)  # Phase 2B: /effects, /field/{id}/neighbors
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    # Local dev: the Vite page on :5173 calls this API on another port. No cookies, so "*" is fine.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(ReadableStartupErrors)  # added last = outermost
    return app


app = create_app()


if __name__ == "__main__":  # python -m api.main
    import uvicorn

    uvicorn.run("api.main:app", host="127.0.0.1", port=int(os.environ.get("API_PORT", "8010")))
