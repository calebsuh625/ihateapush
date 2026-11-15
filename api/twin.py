"""Phase 4, item 4: the twin view. One field next to its best-matched look-alike neighbour.

    GET /field/{field_id}/twin   the field, its twin (the pair with the smallest match score), both
                                 crop histories and bare-days series, the pair's measured difference,
                                 and where that pair sits in its comparison (estimate and interval from
                                 effects.json, how many other pairs point the same way, the placebo).

Built only from the Phase 2B files that api/causal.py already loads (effects.json, pairs.parquet,
causal_fields.parquet, and the soil table named in effects.json), through the same store, so it has
the same 503 until `make causal` has run and re-reads files when they change. Nothing here is
computed from anything else: counts come from pairs.parquet, the interval and the placebos from
effects.json.

What the numbers are (wording is fixed here, so every screen says the same thing): a difference
between look-alike neighbours in the days the soil sits bare. The fake-switch placebo fails for
Phase 2B and the crops grown explain the alfalfa differences, so no sentence says a rotation
caused or improved anything; each says what was measured, the interval, and how many pairs agree.
A single pair is noisy, and the answer says so wherever one pair's numbers appear.

All differences are "this field minus its twin". A pair is stored as A minus B (`did`, `effect`);
when this field is the B side the sign flips and an interval swaps its ends. `effects.json` rows
are also given as stored (`as_stored`), so nothing has to be taken on trust.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.causal import CausalStore, _design, _field_info, _num, _span, _why_unmatched, load_causal

router = APIRouter()

MIN_BASELINE_CHECK = 0.01  # how closely the rebuilt match score must agree before we quote the baseline limit


def rotation_label(kind: str | None) -> str:
    """Rotation type as a person reads it ("diverse" is shown as "mixed (3+ crops)")."""
    if not kind:
        return "unknown"
    if kind == "diverse":
        return "mixed (3+ crops)"
    return kind.replace("/", " / ")


def _one(value: Any) -> float | None:
    return _num(value, 1)


def _signed(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "?"
    text = f"{abs(value):.{decimals}f}"
    if float(text) == 0:
        return f"{0:.{decimals}f}"
    return ("−" if value < 0 else "+") + text


def _days(value: float | None) -> str:
    """"12 days" / "1 day": the size of a gap, never signed."""
    if value is None:
        return "?"
    text = f"{abs(value):.1f}"
    return f"{text} {'day' if text == '1.0' else 'days'}"


def _more_or_fewer(value: float | None) -> str:
    """"12.0 more days" / "3.4 fewer days" / "the same number of days"."""
    if value is None or round(abs(value), 1) == 0:
        return "the same number of days"
    return f"{abs(value):.1f} {'more' if value > 0 else 'fewer'} days"


def _flip(value: float | None) -> float | None:
    return None if value is None else _unsigned_zero(-value)


def _unsigned_zero(value: float | None) -> float | None:
    """A rounded zero is sent as 0.0, never as -0.0."""
    return 0.0 if value == 0 else value


def _series(store: CausalStore, field_id: str, years: list[int]) -> list[dict[str, Any]]:
    """Every soil year, in order; a year with no reading is kept with `value: null` (a gap)."""
    have = store.soil.get(field_id, {})
    return [{"year": y, "value": _one(have.get(y))} for y in years]


def _soil_years(store: CausalStore) -> list[int]:
    span = store.effects.get("design", {}).get("soil_years")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        return list(range(int(span[0]), int(span[1]) + 1))
    seen = sorted({y for series in store.soil.values() for y in series})
    return list(range(seen[0], seen[-1] + 1)) if seen else []


def _side(store: CausalStore, records: dict[str, Any], field_id: str, years: list[int]) -> dict[str, Any]:
    record = records.get(field_id)
    info = _field_info(store, field_id) or {}
    return {
        "field_id": field_id,
        "rotation_pre": info.get("rotation_pre"),
        "rotation_post": info.get("rotation_post"),
        "irrigation": info.get("irrigation"),
        "soil_group": info.get("soil_group"),
        "crop_history": record.crop_history if record is not None else {},
        "bare_days": _series(store, field_id, years),
    }


def _baseline_limit(store: CausalStore, pair: dict[str, Any]) -> float | None:
    """The 2016-2017 bare-days limit the run used (0.5 county SD of that baseline), or None.

    effects.json keeps only the multiplier. The SD is that of the `baseline` column of
    causal_fields.parquet; it is quoted only when the match score of this pair rebuilt from it agrees
    with the stored one (it does for the real run), so a file that changed cannot make us state a wrong limit.
    """
    cal = store.effects.get("design", {}).get("calipers", {})
    try:
        base = np.array([f.get("baseline") for f in store.fields.values()], dtype=float)
        sd = float(np.nanstd(base))
        limit = float(cal["max_baseline_diff_sd"]) * sd
        rebuilt = (
            pair["distance_km"] / float(cal["max_distance_km"])
            + pair["slope_diff"] / float(cal["max_slope_diff"])
            + pair["ssurgo_om_diff"] / float(cal["max_ssurgo_om_diff"])
            + pair["baseline_diff"] / limit
            + pair["history_distance"] / float(cal["max_history_distance"])
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not (math.isfinite(limit) and limit > 0 and math.isfinite(rebuilt)):
        return None
    return limit if abs(rebuilt - float(pair["match_score"])) <= MIN_BASELINE_CHECK else None


def _matched_on(store: CausalStore, pair: dict[str, Any], design: dict[str, Any]) -> list[dict[str, Any]]:
    cal = store.effects.get("design", {}).get("calipers", {})
    hist, base = _span(design["history_years"]), _span(design["baseline_years"])
    base_limit = _baseline_limit(store, pair)
    shared = _num(100 * (1 - float(pair["history_distance"])), 0)
    items: list[dict[str, Any]] = [
        {
            "key": "irrigation",
            "label": "Irrigated or dryland",
            "value": pair["irrigation"],
            "limit": None,
            "unit": "",
            "text": f"both {pair['irrigation']} (a guess from the fields' shape and {hist} crops)",
        },
        {
            "key": "distance",
            "label": "Distance apart",
            "value": _num(pair["distance_km"], 1),
            "limit": cal.get("max_distance_km"),
            "unit": "km",
            "text": f"{float(pair['distance_km']):.1f} km apart (limit {cal.get('max_distance_km', '?')} km)",
        },
        {
            "key": "slope",
            "label": "Slope",
            "value": _num(pair["slope_diff"], 2),
            "limit": cal.get("max_slope_diff"),
            "unit": "points",
            "text": f"slope within {float(pair['slope_diff']):.2f} points (limit {cal.get('max_slope_diff', '?')})",
        },
        {
            "key": "mapped_soil",
            "label": "Mapped soil organic matter",
            "value": _num(pair["ssurgo_om_diff"], 2),
            "limit": cal.get("max_ssurgo_om_diff"),
            "unit": "points",
            "text": f"mapped soil organic matter within {float(pair['ssurgo_om_diff']):.2f} points (limit {cal.get('max_ssurgo_om_diff', '?')})",
        },
        {
            "key": "baseline",
            "label": f"Bare days in {base}",
            "value": _one(pair["baseline_diff"]),
            "limit": _one(base_limit),
            "unit": "days",
            "text": f"{base} bare days within {float(pair['baseline_diff']):.1f}"
            + (f" (limit {base_limit:.0f})" if base_limit is not None else ""),
        },
        {
            "key": "crop_mix",
            "label": f"Crops grown {hist}",
            "value": _num(pair["history_distance"], 3),
            "limit": cal.get("max_history_distance"),
            "unit": "share different",
            "text": f"{shared:.0f}% the same crops in {hist}"
            + (f" (at least {100 - 100 * float(cal['max_history_distance']):.0f}% needed)" if "max_history_distance" in cal else ""),
        },
    ]
    return items


def _closeness(store: CausalStore, pair: dict[str, Any]) -> dict[str, Any]:
    scores = store.pairs["match_score"].astype(float)
    score = float(pair["match_score"])
    n = int(len(scores))
    closer = round(100 * float((scores > score).sum()) / n) if n > 1 else None
    return {
        "score": _num(score, 2),
        "worst_possible": 5,
        "closer_than_pct": closer,
        "n_pairs_in_run": n,
        "median_score": _num(float(scores.median()), 2),
    }


def _oriented_interval(row: dict[str, Any] | None, flip: bool) -> dict[str, Any] | None:
    if not row or row.get("effect") is None:
        return None
    effect, low, high = _one(row["effect"]), _one(row.get("ci_low")), _one(row.get("ci_high"))
    if flip:
        effect, low, high = _flip(effect), _flip(high), _flip(low)
    return {"effect": effect, "ci_low": low, "ci_high": high, "ci_excludes_zero": bool(row.get("ci_excludes_zero"))}


def _placebo(store: CausalStore, comparison: str) -> dict[str, Any]:
    plc = store.effects.get("placebos", {}) or {}
    fake_all = plc.get("fake_switch_years", []) or []
    mine = [r for r in fake_all if r.get("comparison") == comparison]
    by_comp: dict[str, bool] = {}
    for r in fake_all:
        by_comp[r["comparison"]] = by_comp.get(r["comparison"], False) or bool(r.get("ci_excludes_zero"))
    failed_total = sum(by_comp.values())
    shuffled = next((r for r in plc.get("shuffled_labels", []) or [] if r.get("comparison") == comparison), None)
    rows = [
        {
            "fake_switch_year": r.get("fake_switch_year"),
            "effect": _one(r.get("effect")),
            "ci_low": _one(r.get("ci_low")),
            "ci_high": _one(r.get("ci_high")),
            "ci_excludes_zero": bool(r.get("ci_excludes_zero")),
        }
        for r in mine
    ]
    n_failed = sum(r["ci_excludes_zero"] for r in rows)
    county = {"comparisons_tested": len(by_comp), "comparisons_failed": failed_total}
    if not rows:
        sentence = "The fake-switch test was not run for this comparison."
        passed = None
    else:
        names = [str(r["fake_switch_year"]) for r in rows]
        years = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        passed = n_failed == 0
        sentence = (
            f"Fake-switch test: we pretend the switch happened in {years} (the answer should be about zero). "
            f"{n_failed} of {len(rows)} fake switches gave an interval that leaves out zero, so this comparison "
            + ("passes it" if passed else "fails it")
            + f". Across all the comparisons we ran, this test fails for {failed_total} of {len(by_comp)}: "
            f"the gap between look-alikes was already moving before {store.effects.get('design', {}).get('switch_year', 2020)}."
        )
    return {
        "fake_switch": {"rows": rows, "n_failed": n_failed, "n": len(rows)},
        "shuffled_labels": None
        if shuffled is None
        else {
            "p_value": _num(shuffled.get("p_value"), 3),
            "null_p95_abs": _one(shuffled.get("null_p95_abs")),
            "n_shuffles": shuffled.get("n_shuffles"),
        },
        "county": county,
        "passed": passed,
        "sentence": sentence,
    }


def _agreement(store: CausalStore, pair: dict[str, Any], flip: bool, this_type: str, twin_type: str) -> dict[str, Any]:
    """How many OTHER pairs of the same comparison point the same way as this pair's change."""
    p = store.pairs
    rest = p[(p["comparison"] == pair["comparison"]) & (p["pair_id"] != pair["pair_id"])]
    did = float(pair["did"])
    sign = np.sign(did)
    other = np.sign(rest["did"].astype(float).to_numpy())
    same = int((other == sign).sum()) if sign != 0 else 0
    zero = int((other == 0).sum())
    n = int(len(rest))
    opposite = n - same - zero
    spread = None
    if n:
        d = rest["did"].astype(float)
        if flip:
            d = -d
        spread = {
            "n": n,
            "median": _one(d.median()),
            "p25": _one(d.quantile(0.25)),
            "p75": _one(d.quantile(0.75)),
            "min": _one(d.min()),
            "max": _one(d.max()),
        }
    this_change = -did if flip else did
    if n == 0:
        sentence = "This is the only pair in its comparison, so there is nothing to compare it with."
    elif sign == 0:
        sentence = (
            f"This pair's change is zero, so no direction to match; {zero} of the {n} other pairs in this "
            "comparison are exactly zero too."
        )
    else:
        way = "fewer" if this_change < 0 else "more"
        sentence = (
            f"{same} of {n} other pairs point the same way: in each, the {rotation_label(this_type)} field "
            f"moved to {way} bare days than its look-alike."
        )
        if opposite:
            sentence += f" {opposite} {'points' if opposite == 1 else 'point'} the other way"
            sentence += f" and {zero} show no change." if zero else "."
        elif zero:
            sentence += f" {zero} show no change."
    return {
        "other_pairs": n,
        "same_way": same,
        "opposite_way": opposite,
        "no_change": zero,
        "this_pair_direction": None if sign == 0 else ("fewer" if this_change < 0 else "more"),
        "spread_of_other_pairs": spread,
        "sentence": sentence,
    }


def build_twin(store: CausalStore, records: dict[str, Any], field_id: str) -> dict[str, Any]:
    design = _design(store.effects)
    outcome = store.effects.get("outcome")
    years = _soil_years(store)
    p = store.pairs
    mine = p[(p["field_a"] == field_id) | (p["field_b"] == field_id)]
    base: dict[str, Any] = {
        "field_id": field_id,
        "is_fake": bool(store.effects.get("is_fake")),
        "outcome": outcome,
        "switch_year": design["switch_year"],
        "design": design,
        "soil_years": years,
        "this": _side(store, records, field_id, years),
    }
    if mine.empty:
        base.update(
            {
                "has_twin": False,
                "reason": _why_unmatched(_field_info(store, field_id), design),
                "examples": [d["field_a"] for d in store.effects.get("demo_pairs", [])],
                "twin": None,
                "match": None,
                "measured": None,
                "comparison": None,
            }
        )
        return base

    best = mine.sort_values(["match_score", "distance_km", "pair_id"], kind="stable").iloc[0].to_dict()
    is_a = best["field_a"] == field_id
    me, you = ("a", "b") if is_a else ("b", "a")
    flip = not is_a
    twin_id = best[f"field_{you}"]
    this_type, twin_type = best[f"rotation_{me}"], best[f"rotation_{you}"]
    sgn = -1.0 if flip else 1.0

    pre_t, post_t = _one(best[f"pre_{me}"]), _one(best[f"post_{me}"])
    pre_w, post_w = _one(best[f"pre_{you}"]), _one(best[f"post_{you}"])
    pre_span, post_span = _span(design["pre_years"]), _span(design["post_years"])
    change_t = None if pre_t is None or post_t is None else round(post_t - pre_t, 1)
    change_w = None if pre_w is None or post_w is None else round(post_w - pre_w, 1)
    level_gap = None if post_t is None or post_w is None else round(post_t - post_w, 1)
    diff_in_change = _unsigned_zero(_one(sgn * float(best["did"])))
    from_crops = _unsigned_zero(_one(sgn * float(best["did_crops"]))) if pd.notna(best.get("did_crops")) else None
    beyond = _unsigned_zero(_one(sgn * float(best["did_beyond"]))) if pd.notna(best.get("did_beyond")) else None

    sentence = (
        f"In {pre_span} this field sat bare {pre_t} days a year and its twin {pre_w}; in {post_span} it sat bare "
        f"{post_t} days and its twin {post_w}. This field's bare days moved by {_signed(change_t)} and its twin's by "
        f"{_signed(change_w)}, so the two changes differ by {_signed(diff_in_change)} days a year "
        "(this field minus its twin). "
        + (f"From the crops grown alone we would expect a difference of {_signed(from_crops)}. " if from_crops is not None else "")
        + "One pair alone is noisy: read it as one example, not as a finding."
    )

    comp_name = best["comparison"]
    effect_row = store.overall.get(comp_name)
    interval = _oriented_interval(effect_row, flip)
    n_in_comp = int((p["comparison"] == comp_name).sum())
    crops_interval = _oriented_interval(effect_row.get("from_crops") if effect_row else None, flip)
    if interval is None:
        est_sentence = (
            f"Only {n_in_comp} pairs have this comparison; we give no average for fewer than "
            f"{store.effects.get('design', {}).get('min_pairs', 10)}."
        )
    else:
        reading = (
            "The interval leaves out zero."
            if interval["ci_excludes_zero"]
            else "The interval includes zero, so this average cannot be told from zero."
        )
        crops_part = (
            f" The crops grown alone would predict {_signed(crops_interval['effect'])}."
            if crops_interval is not None
            else ""
        )
        est_sentence = (
            f"Across all {n_in_comp} pairs of {rotation_label(this_type)} against {rotation_label(twin_type)}, "
            f"the change in bare days between look-alikes differs by {_signed(interval['effect'])} days a year on average "
            f"(95% interval {_signed(interval['ci_low'])} to {_signed(interval['ci_high'])}; this type minus the other). "
            f"{reading}{crops_part}"
        )

    comparison = {
        "name": comp_name,
        "a": best["rotation_a"],
        "b": best["rotation_b"],
        "this_is": me,
        "this_rotation": this_type,
        "twin_rotation": twin_type,
        "n_pairs": n_in_comp,
        # this field's type minus the twin's type, in days a year; an interval, or null under the minimum pair count
        "estimate": interval,
        "from_crops": crops_interval,
        "as_stored": None
        if effect_row is None
        else {k: effect_row.get(k) for k in ("effect", "ci_low", "ci_high", "n_pairs", "p_value", "ci_excludes_zero", "favours")},
        "agreement": _agreement(store, best, flip, this_type, twin_type),
        "placebo": _placebo(store, comp_name),
        "sentence": est_sentence,
    }

    base.update(
        {
            "has_twin": True,
            "reason": None,
            "examples": [],
            "twin": _side(store, records, twin_id, years),
            "match": {
                "pair_id": best["pair_id"],
                "pairs_of_this_field": int(len(mine)),
                "matched_on": _matched_on(store, best, design),
                "closeness": _closeness(store, best),
                "distance_km": _num(best["distance_km"], 1),
                "sentence": None,
            },
            "measured": {
                "pre_years": design["pre_years"],
                "post_years": design["post_years"],
                "this": {"before": pre_t, "after": post_t, "change": change_t},
                "twin": {"before": pre_w, "after": post_w, "change": change_w},
                "level_gap_after": level_gap,
                "difference_in_change": diff_in_change,
                "from_crops": from_crops,
                "beyond_crops": beyond,
                "years_used": {"before": int(best["n_pre"]), "after": int(best["n_post"])},
                "sentence": sentence,
            },
            "comparison": comparison,
        }
    )
    close = base["match"]["closeness"]
    pct = close["closer_than_pct"]
    base["match"]["sentence"] = (
        f"Matched on irrigation, distance, slope, mapped soil, {_span(design['baseline_years'])} bare days and "
        f"{_span(design['history_years'])} crops, and grown differently in {_span(design['rotation_years'])}. "
        f"Match score {close['score']} (0 is identical, {close['worst_possible']} is the edge of every limit)"
        + (f": closer than {pct}% of the {close['n_pairs_in_run']:,} matched pairs." if pct is not None else ".")
    )
    if base["is_fake"]:
        base["measured"]["sentence"] += " The soil numbers here are made up (fake soil table): not findings."
    return base


@router.get("/field/{field_id}/twin", summary="A field next to its best-matched look-alike neighbour")
async def twin(request: Request, field_id: str) -> JSONResponse:
    records = request.app.state.store.records
    if field_id not in records:
        raise HTTPException(status_code=404, detail=f"No field with id {field_id!r}.")
    store = load_causal(request)  # 503 with a "run make causal" message until the Phase 2B files exist
    return JSONResponse(build_twin(store, records, field_id))
