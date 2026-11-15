"""Phase 2B endpoints: rotation effects and each field's look-alike neighbours.

    GET /effects                    data/effects.json as written by `make causal`
    GET /field/{field_id}/neighbors the field's matched look-alike fields and how they diverged

Files (all from `make causal`, see schema.md): data/effects.json, data/pairs.parquet,
data/causal_fields.parquet, plus the soil table it used (data/soil_health.parquet, else the fake).
They are optional: the rest of the API works without them, and these two endpoints answer 503
with a "run make causal" message until they exist. They are read on first use and re-read when
a file changes, so re-running `make causal` needs no API restart.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pipeline.causal import config, soil_data

router = APIRouter()

# module globals so tests can point them elsewhere
EFFECTS_PATH: Path = config.EFFECTS_PATH
PAIRS_PATH: Path = config.PAIRS_PATH
CAUSAL_FIELDS_PATH: Path = config.CAUSAL_FIELDS_PATH
MAX_NEIGHBORS = 5

NOT_READY = "No causal results yet. Run `make causal` (writes data/effects.json and data/pairs.parquet), then try again."


def _num(value: Any, decimals: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, decimals) if math.isfinite(number) else None


@dataclass
class CausalStore:
    stamp: tuple
    effects: dict[str, Any]
    pairs: pd.DataFrame
    fields: dict[str, dict[str, Any]]
    soil: dict[str, dict[int, float]]
    overall: dict[str, dict[str, Any]]  # comparison -> its "all" effect row


def _stamp() -> tuple | None:
    paths = [EFFECTS_PATH, PAIRS_PATH, CAUSAL_FIELDS_PATH]
    if not all(Path(p).is_file() for p in paths):
        return None
    return tuple(Path(p).stat().st_mtime_ns for p in paths)


def load_causal(request: Request) -> CausalStore:
    stamp = _stamp()
    if stamp is None:
        raise HTTPException(status_code=503, detail=NOT_READY)
    cached: CausalStore | None = getattr(request.app.state, "causal", None)
    if cached is not None and cached.stamp == stamp:
        return cached
    effects = json.loads(Path(EFFECTS_PATH).read_text())
    pairs = pd.read_parquet(PAIRS_PATH)
    fields = pd.read_parquet(CAUSAL_FIELDS_PATH).set_index("field_id").to_dict("index")
    soil: dict[str, dict[int, float]] = {}
    soil_name = effects.get("sources", {}).get("soil")
    soil_path = Path(EFFECTS_PATH).parent / soil_name if soil_name else None
    if soil_path is not None and soil_path.is_file():
        column = effects.get("outcome", {}).get("column", config.DEFAULT_OUTCOME)
        soil = soil_data.series_by_field(soil_data.load_soil(soil_path, column))
    overall = {e["comparison"]: e for e in effects.get("effects", []) if e.get("group") == "all"}
    store = CausalStore(stamp, effects, pairs, fields, soil, overall)
    request.app.state.causal = store
    return store


def _timeline(store: CausalStore, field_id: str) -> list[dict[str, Any]]:
    return [{"year": y, "value": _num(v)} for y, v in store.soil.get(field_id, {}).items()]


def _field_info(store: CausalStore, field_id: str) -> dict[str, Any] | None:
    info = store.fields.get(field_id)
    if info is None:
        return None
    return {
        "rotation_pre": info["rotation_pre"],
        "rotation_post": info["rotation_post"],
        "irrigation": info["irrigation"],
        "irrigation_reason": info["irrigation_reason"],
        "soil_group": info["soil_group"],
        "eligible": bool(info["eligible"]),
    }


def _why_unmatched(info: dict[str, Any] | None) -> str:
    if info is None:
        return "This field was not part of the causal run."
    if info["irrigation"] == "uncertain":
        return "We could not tell whether this field is irrigated, so it was left out of the matching."
    if not info["eligible"]:
        return "Too few soil observations before or after 2016 to compare this field."
    if info["rotation_post"] == "other":
        return "Its 2016–2024 crops do not fit one of the rotation types we compare."
    return "No nearby field was similar enough in soil, slope, starting soil score and past crops while being farmed differently."


@router.get("/effects", summary="Effect of each rotation on soil, from matched neighbouring fields")
async def effects(request: Request) -> JSONResponse:
    return JSONResponse(load_causal(request).effects)


@router.get("/field/{field_id}/neighbors", summary="A field's matched look-alike neighbours")
async def neighbors(request: Request, field_id: str) -> JSONResponse:
    records = request.app.state.store.records
    record = records.get(field_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No field with id {field_id!r}.")
    store = load_causal(request)
    p = store.pairs
    mine = p[(p["field_a"] == field_id) | (p["field_b"] == field_id)].sort_values("match_score").head(MAX_NEIGHBORS)
    info = _field_info(store, field_id)

    out = []
    for row in mine.to_dict("records"):
        is_a = row["field_a"] == field_id
        other = row["field_b"] if is_a else row["field_a"]
        side, other_side = ("a", "b") if is_a else ("b", "a")
        overall = store.overall.get(row["comparison"])
        other_record = records.get(other)
        out.append(
            {
                "pair_id": row["pair_id"],
                "field_id": other,
                "comparison": row["comparison"],
                "rotation": row[f"rotation_{other_side}"],
                "rotation_pre": row[f"rotation_pre_{other_side}"],
                "this_rotation": row[f"rotation_{side}"],
                "irrigation": row["irrigation"],
                "distance_km": _num(row["distance_km"], 2),
                "match_score": _num(row["match_score"], 3),
                "similarity": {
                    "slope_diff": _num(row["slope_diff"], 2),
                    "ssurgo_om_diff": _num(row["ssurgo_om_diff"], 2),
                    "baseline_diff": _num(row["baseline_diff"]),
                    "history_distance": _num(row["history_distance"], 3),
                },
                # how much more this field's score moved than the neighbour's (pre -> post)
                "divergence": _num(row["did"] if is_a else -row["did"]),
                "crop_history": other_record.crop_history if other_record else {},
                "soil": _timeline(store, other),
                "comparison_effect": None
                if overall is None
                else {
                    k: overall.get(k)
                    for k in ("a", "b", "effect", "ci_low", "ci_high", "n_pairs", "p_value", "ci_excludes_zero")
                },
            }
        )

    return JSONResponse(
        {
            "field_id": field_id,
            "is_fake": bool(store.effects.get("is_fake")),
            "outcome": store.effects.get("outcome"),
            "switch_year": store.effects.get("design", {}).get("switch_year", config.SWITCH_YEAR),
            "field": info,
            "crop_history": record.crop_history,
            "soil": _timeline(store, field_id),
            "neighbors": out,
            "reason": None if out else _why_unmatched(info),
            # when this field has no look-alikes: fields that do, to explore instead
            "examples": [] if out else [d["field_a"] for d in store.effects.get("demo_pairs", [])],
        }
    )
