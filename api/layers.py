"""Phase 4, item 5: map layers. County-wide per-field values the map can colour by.

    GET /layers          what exists: per layer its status, years, unit, class breaks, a one-line method
                         note and a caveat. Small; no per-field values.
    GET /layers/{name}   the values of one layer (`?year=2020` for one year only), with county summaries.

The four layers
---------------
bare_days        number, per year 2016-2024. `bare_days` of data/soil_health.parquet rounded to whole
                 days (halves go up, as JavaScript's Math.round). A field-year with no reading stays null.
erosion_risk     number, per year. The planner's wind erosion index (api/planner/erosion.py: bare share
                 of the year x soil factor x climate factor x knoll factor) from THAT year's measured bare
                 days, the field's wind erodibility (data/field_traits.parquet) and slope, and that
                 year's observed `erosion_climate` (data/climate_history.parquet). Null where bare days
                 are null. Sent in thousandths (`value_scale` 0.001).
rotation_change  classes, one value per field. The planner (smart.rank_plans_smart) with the default
                 priorities and today's climate, exactly as GET /field/{id}/plans answers with no
                 parameters: the field's last four years ("current") against our first pick. See
                 `classify_change`. `detail` is the score gap in thousandths.
irrigation       classes (dryland / irrigated / not in the table), with `irrigated_prob` of
                 data/field_traits.parquet as a whole percent in `detail`.

How to join with GET /fields
----------------------------
Every `values` (and `detail`) array has one entry per field, in the order of the `features` array of
GET /fields: `values[i]` belongs to `features[i]`. `field_count` and `fields_etag` (the ETag of
GET /fields) are in every answer so a client can check it holds the matching outlines.

Number layers send integers: the real value is `values[i] * value_scale`. The class of a value is the
number of entries of `breaks_scaled` that are less than or equal to it (so class 0 is below the first
break). Class layers send the class index itself.

Class breaks come from the data: the 20th, 40th, 60th and 80th percentile of all the layer's values
(every year pooled, so a year-by-year animation keeps one scale), each moved to the nearest round
number (1, 2, 3 or 5 times a power of ten), duplicates dropped. Nothing is hard-coded.

Speed
-----
The three table layers are built on first use (about 0.2 s) and rebuilt when one of their files
changes. Each answer is serialised and gzipped once and carries an ETag (304 on If-None-Match), like
GET /fields. `rotation_change` needs the planner for every field (about 3 s for 7,255 fields): a
background thread computes it once, the result is cached on disk (data/layers_cache/, or
$LAYERS_CACHE_DIR) under a key made of the planner's table versions (`smart._stamp`), the fields file
and the default settings; until it is ready the layer answers 202 with `"status": "building"`. The
endpoints are plain `def` handlers, so FastAPI runs them in its thread pool and nothing here blocks
the event loop.

Missing files: a layer whose file is missing answers `"status": "unavailable"` with a `reason`
sentence (HTTP 200); the other layers still work.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from api.planner import config as planner_config
from api.planner import erosion, smart
from api.planner.traits import FieldTraits, load_traits

log = logging.getLogger("uvicorn.error")
router = APIRouter()

VERSION = 1                      # bump when a layer's method changes: old disk caches are then ignored
ACRES_PER_HA = 2.47105
LAYER_NAMES = ("bare_days", "erosion_risk", "rotation_change", "irrigation")
BREAK_PERCENTILES = (20, 40, 60, 80)
NICE_DIGITS = (1, 2, 3, 5, 10)
DEFAULT_CACHE_DIR = planner_config.DATA_DIR / "layers_cache"
RETRY_AFTER_S = 2

JOIN = ("values[i] (and detail[i]) belongs to features[i] of GET /fields: same order, same count. "
        "Check field_count and fields_etag (the ETag of GET /fields) against the outlines you hold.")
NUMBER_RULE = ("value = values[i] * value_scale; null = no reading. Class index = how many entries of "
               "breaks_scaled are less than or equal to values[i].")
CLASS_RULE = "values[i] is the index of the field's class in `classes`."

# rotation_change classes, in this order (the index is what `values` carries)
CHANGE_CLASSES = (
    ("same", "Same plan: the field already follows our first pick"),
    ("top_pick", "Among our top picks: the field follows our second or third pick"),
    ("not_higher", "Our first pick does not score higher than what the field does now"),
    ("within_spread", "Our first pick scores a little higher, within the spread between climate models"),
    ("clearly_higher", "Our first pick scores clearly higher"),
    ("unknown", "Cannot tell: no four-year pattern on record"),
)
CHANGE_INDEX = {key: i for i, (key, _label) in enumerate(CHANGE_CLASSES)}
IRRIGATION_CLASSES = (
    ("dryland", "Dryland (our best guess)"),
    ("irrigated", "Irrigated (our best guess)"),
    ("unknown", "Not in the field traits table"),
)

BARE_SOURCE = "data/soil_health.parquet"
TRAITS_SOURCE = "data/field_traits.parquet"
HISTORY_SOURCE = "data/climate_history.parquet"


def _weights_text() -> str:
    w = planner_config.DEFAULT_WEIGHTS
    return ", ".join(f"{name} {round(100 * w[name])}%" for name in planner_config.SCORE_NAMES)


META: dict[str, dict[str, Any]] = {
    "bare_days": {
        "title": "Days the soil sat bare",
        "kind": "number", "unit": "days a year", "value_scale": 1, "higher_is_worse": True, "has_years": True,
        "source": BARE_SOURCE,
        "method": ("Satellite estimate of the days in the year the soil sat bare and exposed (no crop and no "
                   "residue cover), per field and year, rounded to whole days."),
        "caveat": ("An estimate from satellite views, not a field visit; a field-year with too few clear views "
                   "has no value. Compare fields within the same year: years differ with the weather."),
    },
    "erosion_risk": {
        "title": "Wind erosion risk",
        "kind": "number", "unit": "relative index", "value_scale": 0.001, "higher_is_worse": True, "has_years": True,
        "source": f"{BARE_SOURCE}, {TRAITS_SOURCE}, {HISTORY_SOURCE}, field slope",
        "method": ("The planner's wind erosion index for each year: that year's measured bare share of the year "
                   "x the field's soil wind erodibility x that year's observed wind-erosion climate x a slope "
                   "factor. 1.0 = the county's commonest soil, bare all year, on flat ground, in the average "
                   "2000-2024 wind climate."),
        "caveat": ("A relative index, not tons of soil. It leaves out tillage, residue left standing and field "
                   "length, which we have no data for. One year's wind climate is spiky (2022 is at the cap of "
                   "3 times the average), so a single year says more about that year's weather than about the field."),
    },
    "rotation_change": {
        "title": "Rotation change recommended",
        "kind": "class", "unit": None, "value_scale": 1, "ordered": True, "has_years": False,
        "source": "the rotation planner (GET /field/{id}/plans with no parameters), for every field",
        "method": ("The field's last four years, scored like our candidate rotations for the default priorities "
                   f"({_weights_text()}) and today's climate, against our first pick. The gap is the first "
                   "pick's ranking score minus the current pattern's (scores run 0 to 1). It counts as within "
                   "the spread when it is no bigger than how far either rotation's score moves across the six "
                   "climate models (highest minus lowest), and as clearly higher when it is bigger. Cannot tell: one "
                   "of the field's last four years is missing or holds a crop outside the six the planner knows."),
        "caveat": ("A score gap is the planner's model result for these priorities, not a measured gain, and other "
                   "priorities give other picks. Apart from irrigation, soil erodibility and slope the planner's "
                   "numbers are county-level. The soil part of the score is soil protection (fewer bare days, "
                   "lower wind-erosion risk)."),
    },
    "irrigation": {
        "title": "Irrigated or dryland",
        "kind": "class", "unit": None, "value_scale": 1, "ordered": False, "has_years": False,
        "source": TRAITS_SOURCE,
        "method": ("Our best guess per field from its outline shape, the crops grown and measured water use "
                   "(OpenET); irrigated when the probability is 50% or more. The probability is sent with it."),
        "caveat": "A guess, not a record of water rights or wells; fields near 50% could be either.",
    },
}
DETAIL: dict[str, dict[str, Any]] = {
    "rotation_change": {
        "key": "score_gap", "label": "First pick's ranking score minus the current pattern's", "unit": "score points (0 to 1 scale)",
        "value_scale": 0.001,
        "note": "null when there is no four-year pattern to score; 0 when the field already follows our first pick.",
    },
    "irrigation": {
        "key": "irrigated_prob", "label": "How likely the field is irrigated", "unit": "%", "value_scale": 1,
        "note": "null when the field is not in the field traits table.",
    },
}


class Unavailable(Exception):
    """This layer cannot be built here. str(exc) is one plain sentence for the user."""


# --------------------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_layers.py)
# --------------------------------------------------------------------------------------


def dump_json(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def nice_number(value: float) -> float:
    """The nearest round number (1, 2, 3 or 5 times a power of ten), nearest on a ratio scale."""
    if not math.isfinite(value) or value <= 0:
        return 0.0
    power = 10.0 ** math.floor(math.log10(value))
    return min((digit * power for digit in NICE_DIGITS), key=lambda nice: abs(math.log(nice / value)))


def data_breaks(values: np.ndarray, percentiles: Sequence[float] = BREAK_PERCENTILES) -> list[int]:
    """Class breaks for whole-number values, from the data: each percentile moved to the nearest round
    number, at least 1, duplicates dropped, and only breaks that leave fields on both sides."""
    values = np.asarray(values)
    if values.size == 0:
        return []
    low, high = int(values.min()), int(values.max())
    breaks: list[int] = []
    for q in np.percentile(values, list(percentiles)):
        nice = int(round(nice_number(float(q))))
        if nice >= 1 and low < nice <= high and nice not in breaks:
            breaks.append(nice)
    return sorted(breaks)


def class_of(values: np.ndarray, breaks: Sequence[int]) -> np.ndarray:
    """Class index per value: how many breaks are less than or equal to it."""
    return np.searchsorted(np.asarray(breaks, dtype=float), np.asarray(values, dtype=float), side="right")


def _decimals(scale: float) -> int:
    return max(0, int(round(-math.log10(scale)))) if scale < 1 else 0


def _shown(scaled: float, scale: float) -> float | int:
    """A wire value in its real unit, rounded to what the wire carries."""
    decimals = _decimals(scale)
    value = round(float(scaled) * scale, decimals)
    return int(value) if decimals == 0 else value


def _text(scaled: int, scale: float) -> str:
    decimals = _decimals(scale)
    return f"{scaled * scale:.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{int(scaled):,}"


def number_classes(breaks: Sequence[int], scale: float) -> list[dict[str, Any]]:
    """The classes a list of breaks makes: [..., break) each, with a label in the real unit."""
    edges = [None, *breaks, None]
    classes = []
    for index, (low, high) in enumerate(zip(edges[:-1], edges[1:])):
        if low is None and high is None:
            label = "all values"
        elif low is None:
            label = f"under {_text(high, scale)}"
        elif high is None:
            label = f"{_text(low, scale)} or more"
        else:
            label = f"{_text(low, scale)} to under {_text(high, scale)}"
        classes.append({
            "index": index, "label": label,
            "min": None if low is None else _shown(low, scale), "max": None if high is None else _shown(high, scale),
            "min_scaled": low, "max_scaled": high,
        })
    return classes


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 3) if whole > 0 else None


def _quartiles(values: np.ndarray, scale: float) -> dict[str, Any]:
    if values.size == 0:
        return {"median": None, "p25": None, "p75": None}
    p25, median, p75 = np.percentile(values, [25, 50, 75])
    return {"median": _shown(median, scale), "p25": _shown(p25, scale), "p75": _shown(p75, scale)}


def _wire(values: np.ndarray, has: np.ndarray) -> list:
    """Whole numbers for the wire, None where there is no value."""
    return [int(v) if ok else None for v, ok in zip(values.tolist(), has.tolist())]


def number_summary(values: np.ndarray, has: np.ndarray, acres: np.ndarray, breaks: Sequence[int], scale: float) -> dict:
    """County summary of one year of a number layer. Shares are of the fields (acres) WITH a value."""
    got = values[has]
    got_acres = acres[has]
    which = class_of(got, breaks)
    total_acres = float(got_acres.sum())
    rows = []
    for index in range(len(breaks) + 1):
        inside = which == index
        n, a = int(inside.sum()), float(got_acres[inside].sum())
        rows.append({"index": index, "fields": n, "acres": int(round(a)),
                     "share_fields": _share(n, got.size), "share_acres": _share(a, total_acres)})
    return {
        "fields": int(values.size), "fields_with_value": int(got.size), "fields_no_value": int(values.size - got.size),
        "acres_with_value": int(round(total_acres)), **_quartiles(got, scale), "classes": rows,
    }


def class_summary(classes: np.ndarray, n_classes: int, acres: np.ndarray, no_value_index: int | None,
                  detail: np.ndarray | None = None, detail_has: np.ndarray | None = None,
                  detail_scale: float = 1.0) -> dict:
    """County summary of a class layer. Shares are of ALL fields (acres); the no-value class is one of them."""
    total_acres = float(acres.sum())
    rows = []
    for index in range(n_classes):
        inside = classes == index
        n, a = int(inside.sum()), float(acres[inside].sum())
        row = {"index": index, "fields": n, "acres": int(round(a)),
               "share_fields": _share(n, classes.size), "share_acres": _share(a, total_acres)}
        if detail is not None:
            row["detail_median"] = _quartiles(detail[inside & detail_has], detail_scale)["median"]
        rows.append(row)
    no_value = int((classes == no_value_index).sum()) if no_value_index is not None else 0
    out = {"fields": int(classes.size), "fields_with_value": int(classes.size - no_value), "fields_no_value": no_value,
           "acres": int(round(total_acres)), "classes": rows}
    if detail is not None:
        out["detail"] = {"fields": int(detail_has.sum()), **_quartiles(detail[detail_has], detail_scale)}
    return out


def classify_change(result: dict) -> tuple[str, int | None]:
    """(class key, score gap in thousandths or None) from the body of GET /field/{id}/plans.

    same            the current pattern is a candidate cycle and it is our first pick (gap 0)
    top_pick        it is our second or third pick
    unknown         no "current": a year of the last four is missing or holds a crop outside the six
    otherwise the gap = first pick's ranking score ("robust") minus the current pattern's decides:
    not_higher      gap <= 0 (the current pattern is not a candidate, or ties, and scores as high)
    within_spread   0 < gap <= the larger of the two rotations' spread of the overall score across the
                    climate models (spread.overall.high - spread.overall.low)
    clearly_higher  gap above that spread
    """
    current, plans = result.get("current"), result.get("plans") or []
    if current is None or not plans:
        return "unknown", None
    first = plans[0]
    score = lambda plan: plan["scores"].get("robust", plan["scores"]["overall"])  # noqa: E731
    gap = int(round(1000 * (score(first) - score(current))))
    if current.get("is_candidate"):
        for place, plan in enumerate(plans):
            if list(plan["cycle"]) == list(current["cycle"]):
                return ("same", 0) if place == 0 else ("top_pick", gap)
    if gap <= 0:
        return "not_higher", gap
    spread = lambda plan: plan["spread"]["overall"]["high"] - plan["spread"]["overall"]["low"]  # noqa: E731
    within = int(round(1000 * max(spread(first), spread(current))))
    return ("within_spread" if gap <= within else "clearly_higher"), gap


# --------------------------------------------------------------------------------------
# Building the layers (arrays in, plain dicts out)
# --------------------------------------------------------------------------------------


def _base(name: str, field_count: int) -> dict[str, Any]:
    meta = META[name]
    out = {"name": name, "title": meta["title"], "kind": meta["kind"], "status": "ready", "reason": None,
           "unit": meta["unit"], "value_scale": meta["value_scale"], "years": None,
           "method": meta["method"], "caveat": meta["caveat"], "source": meta["source"],
           "field_count": field_count}
    if meta["kind"] == "number":
        out["higher_is_worse"] = meta["higher_is_worse"]
        out["rule"] = NUMBER_RULE
    else:
        out["ordered"] = meta["ordered"]
        out["rule"] = CLASS_RULE
    return out


def _yearly_matrix(ids: Sequence[str], soil) -> tuple[list[int], np.ndarray]:
    """(years, [year, field] bare days with NaN for no reading) aligned to `ids`."""
    wide = soil.drop_duplicates(["field_id", "year"]).pivot(index="year", columns="field_id", values="bare_days")
    wide = wide.reindex(columns=list(ids)).sort_index()
    return [int(year) for year in wide.index], wide.to_numpy(dtype=float)


def number_layer(name: str, years: Sequence[int], matrix: np.ndarray, acres: np.ndarray, extra: dict | None = None) -> dict:
    """A yearly number layer from a [year, field] matrix in real units (NaN = no value)."""
    scale = META[name]["value_scale"]
    has = np.isfinite(matrix)
    scaled = np.where(has, np.floor(np.where(has, matrix, 0.0) / scale + 0.5), 0).astype(np.int64)
    breaks = data_breaks(scaled[has])
    layer = _base(name, matrix.shape[1])
    layer.update({
        "years": list(years),
        "breaks": [_shown(b, scale) for b in breaks], "breaks_scaled": breaks,
        "classes": number_classes(breaks, scale),
        "breaks_note": ("Breaks are the 20th, 40th, 60th and 80th percentile of every field-year pooled, each moved "
                        "to the nearest round number; the same breaks apply to every year."),
        "series": [
            {"year": int(year), "values": _wire(scaled[i], has[i]),
             "summary": number_summary(scaled[i], has[i], acres, breaks, scale)}
            for i, year in enumerate(years)
        ],
    })
    if extra:
        layer.update(extra)
    return layer


def bare_days_layer(ids: Sequence[str], acres: np.ndarray, soil) -> dict:
    years, matrix = _yearly_matrix(ids, soil)
    if not years:
        raise Unavailable("The yearly soil table has no rows.")
    return number_layer("bare_days", years, matrix, acres)


def erosion_layer(ids: Sequence[str], acres: np.ndarray, slopes: Sequence, soil, traits: dict, history) -> dict:
    """The planner's erosion index per field and year, from that year's bare days and wind climate."""
    years, matrix = _yearly_matrix(ids, soil)
    climate = {int(year): float(value) for year, value in zip(history["year"], history["erosion_climate"])
               if value is not None and math.isfinite(float(value))}
    keep = [i for i, year in enumerate(years) if year in climate]
    if not keep:
        raise Unavailable("The observed climate table has no wind-erosion factor for the years of the soil table.")
    years, matrix = [years[i] for i in keep], matrix[keep]
    factor = np.empty(len(ids))
    assumed = 0
    for i, (field_id, slope) in enumerate(zip(ids, slopes)):
        t = traits.get(field_id)
        wei, weg = (t.wei, t.weg) if t is not None else (None, None)
        assumed += wei is None and weg is None
        factor[i] = erosion.field_factor(wei, weg, slope)
    share = np.clip(matrix / erosion.DAYS_IN_YEAR, 0.0, 1.0)                      # NaN stays NaN
    year_factor = np.array([erosion.climate_factor(climate[year]) for year in years])
    index = np.clip(share * factor[None, :] * year_factor[:, None], 0.0, erosion.INDEX_CAP)
    extra = {
        "inputs": {
            "erosion_climate_by_year": {str(year): round(climate[year]) for year in years},
            "erosion_climate_note": "The observed wind-erosion climate factor of each year; 100 = the 2000-2024 average.",
            "fields_soil_assumed": int(assumed),
            "fields_soil_assumed_note": "Fields with no wind erodibility on record, counted as the county's commonest soil.",
            "index_cap": erosion.INDEX_CAP,
        },
    }
    return number_layer("erosion_risk", years, index, acres, extra)


def _class_layer(name: str, labels: Sequence[tuple], classes: np.ndarray, acres: np.ndarray, no_value_key: str,
                 detail: np.ndarray, detail_has: np.ndarray) -> dict:
    info = DETAIL[name]
    no_value_index = [key for key, _ in labels].index(no_value_key)
    layer = _base(name, int(classes.size))
    layer.update({
        "classes": [{"index": i, "key": key, "label": label, "no_value": i == no_value_index}
                    for i, (key, label) in enumerate(labels)],
        "detail": dict(info),
        "series": [{
            "year": None, "values": [int(v) for v in classes.tolist()], "detail": _wire(detail, detail_has),
            "summary": class_summary(classes, len(labels), acres, no_value_index, detail, detail_has, info["value_scale"]),
        }],
    })
    return layer


def irrigation_layer(ids: Sequence[str], acres: np.ndarray, traits: dict) -> dict:
    classes = np.full(len(ids), 2, dtype=np.int64)
    prob = np.zeros(len(ids), dtype=np.int64)
    has = np.zeros(len(ids), dtype=bool)
    for i, field_id in enumerate(ids):
        t = traits.get(field_id)
        if t is None or t.irrigation not in ("irrigated", "dryland"):
            continue
        classes[i] = 1 if t.irrigation == "irrigated" else 0
        if t.irrigated_prob is not None and math.isfinite(t.irrigated_prob):
            prob[i], has[i] = int(round(100 * t.irrigated_prob)), True
    return _class_layer("irrigation", IRRIGATION_CLASSES, classes, acres, "unknown", prob, has)


def rotation_layer(acres: np.ndarray, classes: Sequence[int], gaps: Sequence) -> dict:
    classes = np.asarray(classes, dtype=np.int64)
    has = np.array([gap is not None for gap in gaps], dtype=bool)
    gap = np.array([0 if g is None else int(g) for g in gaps], dtype=np.int64)
    layer = _class_layer("rotation_change", CHANGE_CLASSES, classes, acres, "unknown", gap, has)
    layer["settings"] = {"weights": dict(planner_config.DEFAULT_WEIGHTS), "scenario": planner_config.DEFAULT_SCENARIO,
                         "horizon": planner_config.TODAY}
    return layer


def change_for_fields(inputs: Sequence, *, fake: bool, engine=None, progress: Callable[[int], None] | None = None) -> tuple[list, list]:
    """(class index, gap) per field: the planner with its defaults, as GET /field/{id}/plans runs it."""
    engine = engine if engine is not None else smart.get_engine()
    traits = FieldTraits() if fake else None            # as in /plans: fake ids are not real fields
    classes, gaps = [], []
    for n, field in enumerate(inputs):
        key, gap = classify_change(smart.rank_plans_smart(field, traits=traits, engine=engine).to_dict())
        classes.append(CHANGE_INDEX[key])
        gaps.append(gap)
        if progress is not None and (n + 1) % 100 == 0:
            progress(n + 1)
            time.sleep(0)                               # let request threads in
    return classes, gaps


# --------------------------------------------------------------------------------------
# The service: caching, the background build, serialising
# --------------------------------------------------------------------------------------


def _stat(path: Path | None) -> tuple:
    if path is None:
        return (None, None, None)
    try:
        st = Path(path).stat()
        return (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return (str(path), None, None)


class _Packed:
    """One answer, serialised once: bytes, gzipped bytes, ETag."""

    def __init__(self, body: dict, status_code: int = 200):
        self.body = body
        self.raw = dump_json(body)
        self.gz = gzip.compress(self.raw, compresslevel=6, mtime=0)
        self.etag = f'W/"{hashlib.sha1(self.raw).hexdigest()[:20]}"'
        self.status_code = status_code


class LayerService:
    """Everything GET /layers needs for one loaded fields file. Thread-safe."""

    def __init__(self, store, *, soil_path: Path | None, traits_path: Path | None, history_path: Path | None,
                 smart_ok: Callable[[], bool], cache_dir: Path | None = None):
        self.is_fake = bool(store.is_fake)
        self.fields_etag = store.fields_etag
        records = list(store.records.values())
        self.ids = [record.field_id for record in records]
        self.acres = np.array([float(record.area_ha or 0.0) * ACRES_PER_HA for record in records], dtype=float)
        self.slopes = [record.slope_pct for record in records]
        self._records = records
        self.soil_path, self.traits_path, self.history_path = soil_path, traits_path, history_path
        self.smart_ok = smart_ok
        self.cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
        self._lock = threading.Lock()
        self._static_key: tuple | None = None
        self._static: dict[str, dict] = {}
        self._packed: dict[tuple, _Packed] = {}
        self._change: dict[str, Any] = {"key": None, "status": None, "layer": None, "reason": None, "done": 0, "thread": None}

    # --- the three table layers ---------------------------------------------------------

    def _unavailable(self, name: str, reason: str) -> dict:
        layer = _base(name, len(self.ids))
        layer.update({"status": "unavailable", "reason": reason})
        return layer

    def _missing(self, path: Path | None, what: str, make: str) -> str | None:
        if path is None:
            return (f"The made-up demo fields have no {what}." if self.is_fake
                    else f"No {what} is paired with this fields file.")
        if not Path(path).is_file():
            return f"The {what} is not on this server (run `{make}` to build it)."
        return None

    def _build_static(self) -> dict[str, dict]:
        import pandas as pd

        soil = traits = history = None
        why_soil = self._missing(self.soil_path, "yearly soil table (soil_health.parquet)", "make soil-health")
        if why_soil is None:
            try:
                soil = pd.read_parquet(self.soil_path, columns=["field_id", "year", "bare_days"])
                soil = soil[soil["field_id"].isin(set(self.ids))]
                if soil.empty:
                    why_soil = "The yearly soil table has no rows for these fields."
            except Exception as exc:
                why_soil = f"The yearly soil table could not be read ({type(exc).__name__})."
        why_traits = self._missing(self.traits_path, "field traits table (field_traits.parquet)", "make field-traits")
        if why_traits is None:
            traits = load_traits(self.traits_path)
            if not traits:
                why_traits = "The field traits table could not be read or is empty."
        why_history = None
        if self.history_path is None or not Path(self.history_path).is_file():
            why_history = "The observed climate table (climate_history.parquet) is not on this server (run `make climate` to build it)."
        else:
            try:
                history = pd.read_parquet(self.history_path, columns=["year", "erosion_climate"])
            except Exception as exc:
                why_history = f"The observed climate table could not be read ({type(exc).__name__})."

        def attempt(name: str, why: str | None, build: Callable[[], dict]) -> dict:
            if why is not None:
                return self._unavailable(name, why)
            try:
                return build()
            except Unavailable as exc:
                return self._unavailable(name, str(exc))
            except Exception as exc:  # one broken layer must not take the others down
                log.warning("layer %s failed: %s: %s", name, type(exc).__name__, exc)
                return self._unavailable(name, f"This layer could not be built ({type(exc).__name__}).")

        return {
            "bare_days": attempt("bare_days", why_soil, lambda: bare_days_layer(self.ids, self.acres, soil)),
            "erosion_risk": attempt(
                "erosion_risk", why_soil or why_traits or why_history,
                lambda: erosion_layer(self.ids, self.acres, self.slopes, soil, traits, history)),
            "irrigation": attempt("irrigation", why_traits, lambda: irrigation_layer(self.ids, self.acres, traits)),
        }

    def _static_layers(self) -> dict[str, dict]:
        key = (_stat(self.soil_path), _stat(self.traits_path), _stat(self.history_path))
        with self._lock:
            if self._static_key != key:
                started = time.perf_counter()
                self._static = self._build_static()
                self._static_key = key
                self._packed = {k: v for k, v in self._packed.items() if k[0] == "rotation_change"}
                log.info("Map layers built in %.2fs: %s", time.perf_counter() - started,
                         ", ".join(f"{name} {layer['status']}" for name, layer in self._static.items()))
            return self._static

    # --- rotation change: background build + disk cache ------------------------------------

    def _change_key(self) -> str:
        parts = (VERSION, smart._stamp(planner_config.settings()), self.fields_etag, self.is_fake,
                 sorted(planner_config.DEFAULT_WEIGHTS.items()), planner_config.DEFAULT_SCENARIO, planner_config.TODAY)
        return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()

    @property
    def cache_path(self) -> Path:
        return self.cache_dir / ("rotation_change_fake.json" if self.is_fake else "rotation_change.json")

    def _read_cache(self, key: str) -> tuple[list, list] | None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if raw.get("key") != key or raw.get("field_ids_sha1") != self._ids_hash():
                return None
            classes, gaps = raw["classes"], raw["gaps"]
            if len(classes) != len(self.ids) or len(gaps) != len(self.ids):
                return None
            if any(not isinstance(c, int) or not 0 <= c < len(CHANGE_CLASSES) for c in classes):
                return None
            return classes, gaps
        except Exception:  # missing, half-written or from another version: just rebuild
            return None

    def _ids_hash(self) -> str:
        return hashlib.sha1("\n".join(self.ids).encode("utf-8")).hexdigest()

    def _write_cache(self, key: str, classes: list, gaps: list) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_bytes(dump_json({
                "version": VERSION, "key": key, "field_ids_sha1": self._ids_hash(), "field_count": len(self.ids),
                "built": time.strftime("%Y-%m-%dT%H:%M:%S"), "class_keys": [k for k, _ in CHANGE_CLASSES],
                "classes": classes, "gaps": gaps,
            }))
            os.replace(tmp, self.cache_path)
        except OSError as exc:  # a read-only data folder only costs the next start a rebuild
            log.warning("could not write the rotation-change cache %s: %s", self.cache_path, exc)

    def _build_change(self, key: str) -> None:
        state = self._change
        started = time.perf_counter()
        try:
            def progress(done: int) -> None:
                if state["key"] == key:
                    state["done"] = done

            classes, gaps = change_for_fields([record.planner_input() for record in self._records],
                                              fake=self.is_fake, progress=progress)
            layer = rotation_layer(self.acres, classes, gaps)
            self._write_cache(key, classes, gaps)
            with self._lock:
                if state["key"] == key:
                    state.update(status="ready", layer=layer, done=len(self.ids))
                    self._packed = {k: v for k, v in self._packed.items() if k[0] != "rotation_change"}
            log.info("Map layer rotation_change built for %s fields in %.1fs", f"{len(self.ids):,}",
                     time.perf_counter() - started)
        except Exception as exc:
            log.warning("map layer rotation_change failed: %s: %s", type(exc).__name__, exc)
            with self._lock:
                if state["key"] == key:
                    state.update(status="unavailable", layer=None,
                                 reason=f"The planner could not be run for every field ({type(exc).__name__}).")

    def _change_layer(self) -> dict:
        """The rotation_change layer as it stands now; starts the background build when needed."""
        try:
            ok = bool(self.smart_ok())
        except Exception:
            ok = False
        if not ok:
            return self._unavailable("rotation_change", "This layer needs the rotation planner's future-weather tables, "
                                                        "which are not set up on this server.")
        key = self._change_key()
        state = self._change
        with self._lock:
            if state["key"] != key:
                state.update(key=key, status=None, layer=None, reason=None, done=0, thread=None)
                self._packed = {k: v for k, v in self._packed.items() if k[0] != "rotation_change"}
                cached = self._read_cache(key)
                if cached is not None:
                    state.update(status="ready", layer=rotation_layer(self.acres, *cached), done=len(self.ids))
                else:
                    thread = threading.Thread(target=self._build_change, args=(key,), name="layer-rotation-change",
                                              daemon=True)
                    state.update(status="building", thread=thread)
                    thread.start()
            if state["status"] == "ready":
                return state["layer"]
            if state["status"] == "unavailable":
                return self._unavailable("rotation_change", state["reason"])
            layer = _base("rotation_change", len(self.ids))
            layer.update({"status": "building", "reason": "Working out a plan for every field; ready in a few seconds.",
                          "progress": {"done": int(state["done"]), "total": len(self.ids)},
                          "retry_after_s": RETRY_AFTER_S})
            return layer

    def wait_for_change(self, timeout: float = 60.0) -> None:
        """Block until the background build (if one is running) has finished. For tests and scripts."""
        thread = self._change.get("thread")
        if thread is not None:
            thread.join(timeout)

    # --- answers ------------------------------------------------------------------------------

    def layer(self, name: str) -> dict:
        if name not in LAYER_NAMES:
            raise KeyError(name)
        return self._change_layer() if name == "rotation_change" else self._static_layers()[name]

    def _with_join(self, body: dict) -> dict:
        body.update({"fields_etag": self.fields_etag, "is_fake": self.is_fake, "join": JOIN})
        return body

    def listing(self) -> dict:
        """Body of GET /layers: every layer without its per-field values and summaries."""
        layers = []
        for name in LAYER_NAMES:
            layer = {k: v for k, v in self.layer(name).items() if k != "series"}
            layer["url"] = f"/layers/{name}"
            layers.append(layer)
        return self._with_join({"field_count": len(self.ids), "layers": layers})

    def packed(self, name: str, year: int | None = None) -> _Packed:
        """The serialised answer of GET /layers/{name}. KeyError: no such layer; ValueError: no such year."""
        layer = self.layer(name)
        if layer["status"] != "ready":
            return _Packed(self._with_join(dict(layer)), 202 if layer["status"] == "building" else 200)
        if year is not None and (layer["years"] is None or year not in layer["years"]):
            raise ValueError(
                f"The {layer['title']} layer has no year {year}." if layer["years"] is None
                else f"The {layer['title']} layer has the years {layer['years'][0]} to {layer['years'][-1]}.")
        cache_key = (name, year, id(layer))
        with self._lock:
            hit = self._packed.get(cache_key)
        if hit is not None:
            return hit
        body = dict(layer)
        if year is not None:
            body["series"] = [row for row in layer["series"] if row["year"] == year]
        packed = _Packed(self._with_join(body))
        with self._lock:
            self._packed[cache_key] = packed
        return packed


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


def _service(request: Request) -> LayerService:
    service = getattr(request.app.state, "layers", None)
    if service is None:
        raise HTTPException(status_code=503, detail="The map layers are not set up on this server.")
    return service


def _respond(request: Request, packed: _Packed) -> Response:
    headers = {"ETag": packed.etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if packed.body.get("status") == "building":
        headers["Retry-After"] = str(RETRY_AFTER_S)
        headers.pop("ETag")
    elif request.headers.get("if-none-match") == packed.etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(packed.gz, status_code=packed.status_code, media_type="application/json", headers=headers)
    return Response(packed.raw, status_code=packed.status_code, media_type="application/json", headers=headers)


@router.get("/layers", summary="Map layers: what exists, years, units, class breaks, method and caveat")
def layers(request: Request) -> Response:
    return _respond(request, _Packed(_service(request).listing()))


@router.get("/layers/{name}", summary="One map layer: per-field values aligned to GET /fields, with county summaries")
def layer(request: Request, name: str, year: int | None = None) -> Response:
    service = _service(request)
    try:
        return _respond(request, service.packed(name, year))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No layer called {name!r}. The layers are: {', '.join(LAYER_NAMES)}.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
