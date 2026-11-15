"""Phase 4, item 6: the water reality check. Measured groundwater levels next to the irrigated plans.

    GET /groundwater     the county picture: how fast the water in the wells has been dropping, the
                         spread across wells, how many wells, which winters, the water column left
                         where the well's depth is on record, and that column in 2040 and 2060 if the
                         measured rate simply continued (an extrapolation, not a forecast).

    `GroundwaterService.outlook(lon, lat, horizon)` is the same for the wells nearest one field; the
    API adds it to GET /field/{id}/plans of an irrigated field as `water_outlook`, with one sentence per
    plan (`plan_lines`) that puts the plan's pumping next to the measured decline.

Everything is read from data/groundwater.parquet (pipeline/groundwater.py: where it comes from, the
winter-level and trend rules). The file is optional: without it GET /groundwater answers
{"available": false, "reason": "not_built"} and the plans carry no outlook.

What the numbers are (the wording is fixed here so every screen says the same):
  * a rate is the change in a well's winter depth to water per year, measured, with the range across
    the wells; it is not caused by, or tied to, any one field or plan;
  * "water column" is well depth minus depth to water: what stands in the well, which can be less than
    the water-bearing thickness under it;
  * the later years are that column minus the rate times the years, a straight line. No sentence gives
    a year in which a well or a field runs out, and no minimum usable column is drawn: we could not
    source one figure in feet (`THRESHOLD_NOTE`).

Nearby wells: the `NEAREST_WELLS` (5) wells that meet the trend rule closest to the field's centre and
no farther than `MAX_MILES` (10); fewer than `MIN_WELLS` (3) within that distance = no outlook. Among
irrigated fields the fifth such well is 4.6 miles away for the middle field and 6.4 for nine in ten.
"""
from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pipeline import groundwater as gw
from pipeline import groundwater_schema
from pipeline.common import COUNTY_NAME, FIPS

log = logging.getLogger("uvicorn.error")
router = APIRouter()

NEAREST_WELLS = 5
MAX_MILES = 10.0
MIN_WELLS = 3
EARTH_RADIUS_MI = 3958.8
STEADY_FT_PER_YEAR = 0.05      # a rate that rounds to 0.0 ft a year is "about steady"

RULE_TEXT = (
    f"A well counts when it has at least {gw.MIN_WINTERS} winter measurements (December to March, when the pumps "
    f"are off) since {gw.TREND_FROM}, the first and last at least {gw.MIN_SPAN_YEARS} years apart and the last "
    f"within the past {gw.MAX_STALE_WINTERS} winters. Its rate is the middle one of the slopes between every pair "
    "of winters (a Theil-Sen slope), with a 95% interval."
)
COLUMN_NOTE = (
    "Water column is the well's depth minus the depth to water: what stands in the well. A well does not always "
    "reach the bottom of the aquifer, so there can be more water-bearing ground under it."
)
THRESHOLD_NOTE = (
    "No minimum usable water column is drawn. The Kansas Geological Survey's own projections assume enough "
    "saturated thickness to pump at least 400 gallons a minute through the growing season (Public Information "
    "Circular 18), but the circular gives no figure in feet, so we show what is left and no cut-off."
)
LIMITS = [
    "Rates are measured in the wells, winter to winter. They are not caused by, or tied to, any one field or plan.",
    "A handful of wells stands in for the water under a field; the wells can be miles away.",
    "The winter readings are mostly taken in January, when levels have recovered from the pumping season; summer "
    "levels are lower.",
    COLUMN_NOTE,
    gw.NOT_A_FORECAST,
]


# --- small pure helpers ---------------------------------------------------------------------------------

def miles_between(lon: float, lat: float, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """Great-circle distance in miles from one point to many."""
    p1, p2 = math.radians(lat), np.radians(np.asarray(lats, dtype=float))
    dp = p2 - p1
    dl = np.radians(np.asarray(lons, dtype=float) - lon)
    a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _r1(value: float) -> float:
    return round(float(value) + 0.0, 1) + 0.0          # + 0.0: never "-0.0"


def _whole(value: float) -> int:
    return int(round(float(value)))


def _band(q: dict | None, digits: int) -> dict | None:
    """A quantile block rounded for the wire: rates to 0.1 ft a year, depths and columns to whole feet."""
    if q is None:
        return None
    r = _r1 if digits == 1 else _whole
    return {key: r(q[key]) for key in ("median", "p25", "p75", "min", "max")}


def rate_words(rate: float) -> str:
    """"dropping about 2.6 feet a year" / "rising about 0.3 feet a year" / "holding about steady"."""
    shown = _r1(rate)
    if abs(rate) < STEADY_FT_PER_YEAR or shown == 0:
        return "holding about steady"
    unit = "foot" if abs(shown) == 1 else "feet"
    return f"{'dropping' if shown > 0 else 'rising'} about {abs(shown):g} {unit} a year"


def _feet(value: float) -> str:
    n = _whole(value)
    return f"{n:,} {'foot' if n == 1 else 'feet'}"


def _count(n: int, noun: str = "well") -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _span_words(low: float, high: float, digits: int) -> str:
    """"1.1 to 3.9" (or just "2.6" when both ends round to the same)."""
    fmt = (lambda v: f"{_r1(v):g}") if digits == 1 else (lambda v: f"{_whole(v):,}")
    a, b = fmt(low), fmt(high)
    return a if a == b else f"{a} to {b}"


def column_years(trends: pd.DataFrame, years: list) -> list:
    """For each year: the water column of the wells with a known depth if each well's own measured rate
    simply continued: [{"year", "left_ft": {median, p25, p75, min, max}, "at_bottom": wells whose straight
    line has reached the bottom of the well}]."""
    known = trends[trends["column_ft"].notna()]
    out = []
    for year in years:
        left = np.array([gw.column_in(row.column_ft, row.slope_ft_per_year, row.last_winter, year, row.well_depth_ft)
                         for row in known.itertuples()], dtype=float)
        if len(left) == 0:
            continue
        out.append({"year": int(year), "left_ft": _band(gw._quantiles(left), 0), "at_bottom": int((left <= 0).sum())})
    return out


def summary_block(trends: pd.DataFrame, years: list) -> dict | None:
    """`gw.summarize_trends` rounded for the wire, with the later years (`column_years`)."""
    raw = gw.summarize_trends(trends, ())
    if raw is None:
        return None
    column = None
    if raw["column"] is not None:
        column = {"wells": raw["column"]["wells"], "now_ft": _band(raw["column"]["now_ft"], 0),
                  "years": column_years(trends, years)}
    return {
        "wells": raw["wells"],
        "winters": [raw["first_winter"], raw["last_winter"]],
        "decline_ft_per_year": _band(raw["decline_ft_per_year"], 1),
        "falling": raw["falling"],
        "rising": raw["rising"],
        "depth_to_water_ft": _band(raw["depth_to_water_ft"], 0),
        "column": column,
    }


def plan_line(irrigation_in: float | None, outlook: dict | None) -> str | None:
    """One sentence that puts a plan's pumping next to the measured decline; None without both."""
    if irrigation_in is None or not outlook or not outlook.get("available"):
        return None
    inches = math.floor(float(irrigation_in) + 0.5)        # halves go up: 10.5 on the card reads "about 11" here
    pumped = ("This plan needs almost no pumped water" if irrigation_in < 0.5
              else f"This plan pumps about {inches} {'inch' if inches == 1 else 'inches'} a year")
    rate = outlook["decline_ft_per_year"]["median"]
    wells = ("wells near this field have been " + rate_words(rate)).replace("have been holding", "have held")
    return f"{pumped}; {wells}."


# --- the service -----------------------------------------------------------------------------------------

class GroundwaterService:
    """The groundwater table in memory: trends per well, the county picture, the wells near a point.

    Built on first use and again when the file changes. Never raises to a request: a missing or broken
    file is one log line and `available` false."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._tried = False
        self._state: dict | None = None

    def _file_stamp(self) -> tuple | None:
        if self.path is None:
            return None
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def state(self) -> dict | None:
        stamp = self._file_stamp()
        with self._lock:
            if stamp != self._stamp or not self._tried:
                self._stamp, self._state, self._tried = stamp, None, True
                if stamp is not None:
                    try:
                        self._state = self._build()
                    except Exception as exc:          # optional data must never stop a request
                        log.warning("groundwater table ignored, serving without it: %s: %s", type(exc).__name__,
                                    str(exc).replace("\n", " "))
            return self._state

    def _build(self) -> dict:
        df = groundwater_schema.load_groundwater(self.path)
        trends = gw.well_trends(df)
        levels = gw.winter_levels(df)
        levels = levels[(levels["winter"] >= gw.TREND_FROM) & levels["well_id"].isin(set(trends["well_id"]))]
        points = {well_id: [[int(w), _r1(d)] for w, d in zip(group["winter"], group["depth_ft"])]
                  for well_id, group in levels.groupby("well_id", sort=False)}
        dates = pd.to_datetime(df["date"])
        table = {
            "measurements": int(len(df)), "wells": int(df["well_id"].nunique()),
            "first_year": int(dates.dt.year.min()), "last_year": int(dates.dt.year.max()),
            "from_usgs": int((df["source"] == "usgs").sum()), "from_kgs": int((df["source"] == "kgs").sum()),
        }
        newest = int(trends["last_winter"].max()) if len(trends) else None
        log.info("Loaded groundwater levels: %s measurements, %s wells meet the trend rule (%s)",
                 f"{table['measurements']:,}", len(trends), self.path.name)
        return {"trends": trends.reset_index(drop=True), "points": points, "table": table, "newest": newest}

    # --- GET /groundwater ---
    def county_body(self) -> dict:
        state = self.state()
        if state is None:
            return {"available": False, "reason": "not_built", "source": groundwater_schema.SOURCE}
        trends = state["trends"]
        county = trends[trends["county_fips"] == FIPS]
        years = [y for y in gw.EXTRAPOLATE_TO if state["newest"] is None or y > state["newest"]]
        block = summary_block(county, years)
        body: dict[str, Any] = {
            "available": block is not None,
            "source": groundwater_schema.SOURCE,
            "county": f"{COUNTY_NAME} County, Kansas",
            "table": state["table"],
            "rule": {"text": RULE_TEXT, "winter_months": list(gw.WINTER_MONTHS), "trend_from": gw.TREND_FROM,
                     "min_winters": gw.MIN_WINTERS, "min_span_years": gw.MIN_SPAN_YEARS,
                     "max_stale_winters": gw.MAX_STALE_WINTERS},
            "summary": block,
            "nearby_summary": summary_block(trends, years),      # the county's wells and those next to it
            "sentence": None,
            "extrapolation": None,
            "caveat": gw.NOT_A_FORECAST,
            "threshold": None,
            "threshold_note": THRESHOLD_NOTE,
            "limits": LIMITS,
            "wells": [self._well_row(row, in_county=row.county_fips == FIPS) for row in trends.itertuples()],
        }
        if block is None:
            body["reason"] = "no_wells"
            return body
        d = block["decline_ft_per_year"]
        body["sentence"] = (
            f"In {COUNTY_NAME} County, {_count(block['wells'])} have a long enough record of winter measurements "
            f"({block['winters'][0]} to {block['winters'][1]}). The water in the middle well has been "
            f"{rate_words(d['median'])}; in the middle half of the wells the change is "
            f"{_span_words(d['p25'], d['p75'], 1)} feet a year. "
            f"{block['falling']} of {block['wells']} are falling by more than their own uncertainty"
            + (f", {block['rising']} {'is' if block['rising'] == 1 else 'are'} rising." if block["rising"] else ".")
        ).replace("has been holding", "has held")
        body["extrapolation"] = self._extrapolation_words(block, "middle half", "p25", "p75")
        return body

    @staticmethod
    def _well_row(row, **extra) -> dict:
        known = row.column_ft == row.column_ft
        return {
            "well_id": row.well_id, "lon": round(float(row.lon), 5), "lat": round(float(row.lat), 5),
            "winters": int(row.n_winters), "first_winter": int(row.first_winter), "last_winter": int(row.last_winter),
            "decline_ft_per_year": _r1(row.slope_ft_per_year),
            "interval": [_r1(row.slope_low), _r1(row.slope_high)],
            "depth_to_water_ft": _whole(row.last_depth_ft),
            "well_depth_ft": _whole(row.well_depth_ft) if known else None,
            "column_ft": _whole(row.column_ft) if known else None,
            **extra,
        }

    @staticmethod
    def _extrapolation_words(block: dict, spread_name: str, low_key: str, high_key: str) -> str | None:
        """"The N wells with a depth on record hold about X feet of water today (...). If each well's measured
        rate simply continued, that would be about Y feet in 2040 (...) and ..."""
        column = block.get("column")
        if not column:
            return None
        now = column["now_ft"]
        text = (f"The {_count(column['wells'])} with a depth on record hold about {_feet(now['median'])} of water "
                f"today in the middle well ({_span_words(now[low_key], now[high_key], 0)} across "
                f"{'the wells' if spread_name == 'range' else 'the ' + spread_name}).")
        later = []
        for entry in column["years"]:
            left = entry["left_ft"]
            later.append(f"about {_feet(left['median'])} in {entry['year']} "
                         f"({_span_words(left[low_key], left[high_key], 0)})")
        if later:
            listed = later[0] if len(later) == 1 else ", ".join(later[:-1]) + " and " + later[-1]
            text += " If each well's measured rate simply continued, that would be " + listed + "."
        return text

    # --- the wells near one field ---
    def outlook(self, lon: float, lat: float, horizon: int | None = None) -> dict:
        """The `water_outlook` block for a field centred on (lon, lat). `horizon`: the chosen future year;
        when it is later than the newest winter it is added to the years the column is carried to."""
        state = self.state()
        if state is None:
            return {"available": False, "reason": "not_built", "source": groundwater_schema.SOURCE}
        trends = state["trends"]
        if len(trends) == 0:
            return {"available": False, "reason": "no_wells_nearby", "source": groundwater_schema.SOURCE}
        miles = miles_between(lon, lat, trends["lon"].to_numpy(), trends["lat"].to_numpy())
        order = np.argsort(miles, kind="stable")
        picked = [int(i) for i in order[:NEAREST_WELLS] if miles[i] <= MAX_MILES]
        base = {"source": groundwater_schema.SOURCE, "rule": RULE_TEXT,
                "search": {"nearest": NEAREST_WELLS, "max_miles": MAX_MILES, "min_wells": MIN_WELLS}}
        if len(picked) < MIN_WELLS:
            return {"available": False, "reason": "no_wells_nearby", **base,
                    "note": f"Fewer than {MIN_WELLS} wells with a long enough record lie within "
                            f"{MAX_MILES:g} miles of this field, so no water outlook is shown for it."}
        near = trends.iloc[picked]
        distance = miles[picked]
        newest = state["newest"]
        years = sorted({int(y) for y in (*gw.EXTRAPOLATE_TO, *((horizon,) if horizon else ())) if y > newest})
        block = summary_block(near, years)
        d = block["decline_ft_per_year"]
        span = _span_words(float(distance.min()), float(distance.max()), 1)
        rates = (f"; the slowest about {_r1(d['min']):g} and the fastest about {_r1(d['max']):g}"
                 if _r1(d["min"]) != _r1(d["max"]) else "")
        sentence = (
            f"The {_count(block['wells'], 'nearest well')} with a long record ({span} miles from this field) have been "
            f"{rate_words(d['median'])} in the middle well, winters {block['winters'][0]} to {block['winters'][1]}{rates}."
        ).replace("have been holding", "have held")
        wells = []
        for row, dist in zip(near.itertuples(), distance):
            entry = self._well_row(row, distance_mi=_r1(dist))
            first, last = int(row.first_winter), int(row.last_winter)
            entry["points"] = state["points"].get(row.well_id, [])
            entry["fit"] = [[first, _r1(row.intercept_ft + row.slope_ft_per_year * first)],
                            [last, _r1(row.intercept_ft + row.slope_ft_per_year * last)]]
            wells.append(entry)
        return {
            "available": True, **base,
            "wells_used": block["wells"],
            "distance_mi": {"nearest": _r1(distance.min()), "farthest": _r1(distance.max())},
            "winters": block["winters"],
            "decline_ft_per_year": {"median": d["median"], "low": d["min"], "high": d["max"]},
            "falling": block["falling"], "rising": block["rising"],
            "depth_to_water_ft": {"median": block["depth_to_water_ft"]["median"],
                                  "low": block["depth_to_water_ft"]["min"], "high": block["depth_to_water_ft"]["max"]},
            "column": (None if block["column"] is None else {
                "wells": block["column"]["wells"],
                "now_ft": {"median": block["column"]["now_ft"]["median"], "low": block["column"]["now_ft"]["min"],
                           "high": block["column"]["now_ft"]["max"]},
                "years": [{"year": e["year"], "at_bottom": e["at_bottom"],
                           "left_ft": {"median": e["left_ft"]["median"], "low": e["left_ft"]["min"],
                                       "high": e["left_ft"]["max"]}} for e in block["column"]["years"]],
            }),
            "sentence": sentence,
            "extrapolation": self._extrapolation_words(block, "range", "min", "max"),
            "caveat": gw.NOT_A_FORECAST,
            "column_note": COLUMN_NOTE,
            "threshold": None,
            "threshold_note": THRESHOLD_NOTE,
            "wells": wells,
        }


def what_if_block(engine, default_engine) -> dict:
    """The pumping-limit part of `water_outlook`: what the plans were scored under, and what may be asked."""
    low, high = default_engine.what_if_range_in()
    applied = engine.irrigation_limit_mm / 25.4 if engine.irrigation_limit_mm > 0 else None
    default = default_engine.irrigation_limit_mm / 25.4 if default_engine.irrigation_limit_mm > 0 else None
    return {
        "limit_in": round(applied, 1) if applied is not None else None,
        "default_in": round(default, 1) if default is not None else None,
        "active": engine is not default_engine,
        "min_in": low, "max_in": high,
        "note": "A what-if: the plans are scored again as if no more than this could be pumped on an acre in a "
                "year. It is your number, not a measurement or a rule.",
    }


def attach_outlook(body: dict, outlook: dict, what_if: dict) -> dict:
    """Add `water_outlook` to a plans answer of an irrigated field: the wells near it, the what-if block,
    and one sentence per plan (`plan_lines`, in the order of `plans`; `current_line` for the current
    pattern) that puts the plan's pumping next to the measured decline. The plans themselves are not
    touched (other answers repeat them key for key). Returns `body`."""
    def line(plan) -> str | None:
        numbers = (plan or {}).get("numbers") or {}
        return plan_line(numbers.get("irrigation_in"), outlook)

    body["water_outlook"] = {**outlook, "what_if": what_if,
                             "plan_lines": [line(plan) for plan in body.get("plans", [])],
                             "current_line": line(body.get("current"))}
    return body


@router.get("/groundwater", summary="Measured groundwater levels: the county picture")
def groundwater(request: Request) -> JSONResponse:
    service: GroundwaterService = request.app.state.groundwater
    return JSONResponse(service.county_body())
