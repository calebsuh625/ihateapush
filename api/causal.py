"""Phase 2B endpoints: rotation comparisons (days the soil sits bare) and each field's look-alike neighbours.

    GET /effects                    data/effects.json as written by `make causal`
    GET /field/{field_id}/neighbors the field's matched look-alike fields and how they diverged

Files (all from `make causal`, see schema.md): data/effects.json, data/pairs.parquet,
data/causal_fields.parquet, plus the soil table it used (data/soil_health.parquet, else the fake).
They are optional: the rest of the API works without them, and these two endpoints answer 503
with a "run make causal" message until they exist. They are read on first use and re-read when
a file changes, so re-running `make causal` needs no API restart.

Every year named in a response comes from the `design` block of effects.json, and every
"better / worse" comes from the outcome's `higher_is_better` (bare days: lower is better).

A pair carries two different numbers, and they can point opposite ways (they do in about 3 pairs
in 10): `after_gap` is a level (this field minus its look-alike in the "after" years) and
`divergence` is a change (how much more this field moved than its look-alike since the "before"
years). `verdict` is about the level only.
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
from pipeline.region import REGION

router = APIRouter()

# module globals so tests can point them elsewhere
EFFECTS_PATH: Path = config.EFFECTS_PATH
PAIRS_PATH: Path = config.PAIRS_PATH
CAUSAL_FIELDS_PATH: Path = config.CAUSAL_FIELDS_PATH
MAX_NEIGHBORS = 5

NOT_READY = "No causal results yet. Run `make causal` (writes data/effects.json and data/pairs.parquet), then try again."
NOT_MEASURED_HERE = (
    f"Neighbour comparisons are not measured in {REGION.label} yet: they need this county's own satellite "
    "soil data. The planner uses the rotation effects measured in Finney County meanwhile."
)


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
        raise HTTPException(status_code=503, detail=NOT_READY if REGION.is_default else NOT_MEASURED_HERE)
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
        try:
            soil = soil_data.series_by_field(soil_data.load_soil(soil_path, column))
        except ValueError:  # the soil table changed since `make causal`: serve the results without timelines
            soil = {}
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


def _span(years: Any) -> str:
    """[2020, 2024] -> "2020–2024" (en dash); anything else -> ""."""
    if not isinstance(years, (list, tuple)) or len(years) != 2:
        return ""
    return str(years[0]) if years[0] == years[1] else f"{years[0]}–{years[1]}"


def _design(effects: dict[str, Any]) -> dict[str, Any]:
    """The year ranges of the run that wrote effects.json (config values only for older files)."""
    d = effects.get("design", {})
    return {
        "switch_year": d.get("switch_year", config.SWITCH_YEAR),
        "history_years": d.get("history_years", list(config.history_years())),
        "rotation_years": d.get("rotation_years", list(config.rotation_years())),
        "baseline_years": d.get("baseline_years", list(config.baseline_years())),
        "pre_years": d.get("pre_years", list(config.pre_years())),
        "post_years": d.get("post_years", list(config.post_years())),
    }


def _why_unmatched(info: dict[str, Any] | None, design: dict[str, Any]) -> str:
    if info is None:
        return "This field was not part of the causal run."
    if info["irrigation"] == "uncertain":
        return "We could not tell whether this field is irrigated, so it was left out of the matching."
    if not info["eligible"]:
        return (
            f"Too few usable soil readings before or after {design['switch_year']} "
            "(or no slope or mapped-soil value) to compare this field."
        )
    if info["rotation_post"] == "other":
        return f"Its {_span(design['rotation_years'])} crops do not fit one of the rotation types we compare."
    return (
        "No nearby field was similar enough in mapped soil, slope, starting level and past crops "
        f"while growing a different kind of rotation in {_span(design['rotation_years'])}."
    )


def _verdict(after_gap: float | None, outcome: dict[str, Any] | None) -> str | None:
    """"better" / "worse": where this field stood against its look-alike in the "after" years.
    `after_gap` = this field's "after" mean minus the look-alike's. Bare days are better when lower,
    so a negative gap is "better" there. It is a level, never the change (`divergence`): a field
    whose bare days fell a lot can still sit bare more days than its look-alike."""
    if after_gap is None or after_gap == 0 or not outcome:
        return None
    higher_is_better = bool(outcome.get("higher_is_better", True))
    return "better" if (after_gap > 0) == higher_is_better else "worse"


def _gap(this: Any, other: Any) -> float | None:
    a, b = _num(this), _num(other)
    return None if a is None or b is None else round(a - b, 4)


@router.get("/effects", summary="Rotation comparisons on days the soil sits bare, from matched neighbouring fields")
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
    outcome = store.effects.get("outcome")
    design = _design(store.effects)

    out = []
    for row in mine.to_dict("records"):
        is_a = row["field_a"] == field_id
        other = row["field_b"] if is_a else row["field_a"]
        side, other_side = ("a", "b") if is_a else ("b", "a")
        overall = store.overall.get(row["comparison"])
        other_record = records.get(other)
        after_gap = _gap(row.get(f"post_{side}"), row.get(f"post_{other_side}"))
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
                # each field's mean before (pre years) and after (post years), over the years both have
                "before": {"this": _num(row.get(f"pre_{side}"), 2), "neighbor": _num(row.get(f"pre_{other_side}"), 2)},
                "after": {"this": _num(row.get(f"post_{side}"), 2), "neighbor": _num(row.get(f"post_{other_side}"), 2)},
                # a level: this field's "after" mean minus the neighbour's
                "after_gap": after_gap,
                # a change: how much more this field's value moved than the neighbour's (before -> after)
                "divergence": _num(row["did"] if is_a else -row["did"]),
                # which of the two stood better in the "after" years (from after_gap, not from divergence)
                "verdict": _verdict(after_gap, outcome),
                "crop_history": other_record.crop_history if other_record else {},
                "soil": _timeline(store, other),
                "comparison_effect": None
                if overall is None
                else {
                    k: overall.get(k)
                    for k in ("a", "b", "effect", "ci_low", "ci_high", "n_pairs", "p_value", "ci_excludes_zero",
                              "favours", "from_crops", "beyond_crops")
                },
            }
        )

    return JSONResponse(
        {
            "field_id": field_id,
            "is_fake": bool(store.effects.get("is_fake")),
            "outcome": outcome,
            "switch_year": design["switch_year"],
            "design": design,
            "field": info,
            "crop_history": record.crop_history,
            "soil": _timeline(store, field_id),
            "neighbors": out,
            "reason": None if out else _why_unmatched(info, design),
            # when this field has no look-alikes: fields that do, to explore instead
            "examples": [] if out else [d["field_a"] for d in store.effects.get("demo_pairs", [])],
        }
    )
