"""Sanity checks of the data-driven planner on the real fields: `python -m api.planner.sanity`.

Writes data/planner_sanity.md (short, for people) and data/logs/phase3/planner_sanity.json (every
number). Prints a few lines. It needs the real tables under data/ (climate, fields; soil and traits
if present) and changes nothing else. Exit code 0 when every check passes, 1 when one fails, 2 when
the tables are missing.

The checks (the questions come from the Phase 3 brief; nothing is tuned to make them pass):

1. Dryland, hotter future: do plans with sorghum, and wheat-fallow, keep or gain rank and score
   against plans with dryland corn? Today -> 2060 in both scenarios, and the hottest model against
   the mildest one.
2. Irrigated: does the irrigation need rise with reference ET, and do thirsty plans lose water score?
3. Weights: soil weight up -> fewer bare days in the top plan; water up -> less irrigation (dryland:
   more of the water need met); money up -> more profit; risk up -> fewer failed crops.
4. Fallow-heavy plans score worst on soil and wind erosion; alfalfa best.
5. No recommended plan breaks a candidate rule; every score stays in 0-1; results are deterministic.

It also times the planner and looks for real fields whose recommended plan changes between today
and 2060 (for the pitch).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from api.planner import candidates as cand
from api.planner import config, money, smart, soil_effect
from api.planner.crops import CROPS, PRACTICES
from api.planner.legacy import FieldInput, rotation_name

SHORT = {"corn": "C", "soybeans": "B", "winter wheat": "W", "sorghum": "S", "alfalfa": "A", "fallow": "F"}
FAR = 2060
SETTINGS = [("ssp245", config.TODAY), ("ssp245", FAR), ("ssp585", FAR)]
TYPICAL = {"wei": 56.0, "slope_pct": 0.84}      # the county's commonest soil and its median slope
SAMPLE_PER_PRACTICE = 300
HEAVY = 0.7                                     # weight of the priority that is "turned up" in check 3
TOLERANCE = 1e-9


def code(cycle) -> str:
    return "".join(SHORT[c] for c in cycle)


def plan_label(cycle) -> str:
    cycle = list(cycle)
    return rotation_name(cycle) if len(cycle) == cand.YEARS else f"{rotation_name(cycle)} (3-year cycle)"


def load_fields(path: Path):
    import pandas as pd

    df = pd.read_parquet(path, columns=["field_id", "crop_history", "organic_matter_pct", "slope_pct", "area_ha"])
    out = []
    for row in df.itertuples(index=False):
        history = json.loads(row.crop_history) if isinstance(row.crop_history, str) else dict(row.crop_history)
        om = None if row.organic_matter_pct != row.organic_matter_pct else float(row.organic_matter_pct)
        slope = None if row.slope_pct != row.slope_pct else float(row.slope_pct)
        out.append(FieldInput(row.field_id, history, om, slope, float(row.area_ha)))
    return sorted(out, key=lambda f: f.field_id)


def sample_fields(fields, engine, per_practice: int = SAMPLE_PER_PRACTICE):
    """An evenly spaced, deterministic sample of irrigated and of dryland fields."""
    groups = {True: [], False: []}
    for f in fields:
        traits = engine.traits_for(f.field_id)
        if traits is not None and traits.irrigation in PRACTICES:
            groups[traits.irrigation == "irrigated"].append(f)
        else:
            groups[smart.guess_irrigation(f.crop_history)[0]].append(f)
    out = {}
    for irrigated, group in groups.items():
        step = max(1, len(group) // per_practice)
        out[irrigated] = group[::step][:per_practice]
    return out


def ranks(values: np.ndarray) -> np.ndarray:
    """1 = best (highest value); ties share the better rank."""
    return np.array([1 + int((values > v + 1e-12).sum()) for v in values])


# --- check 1 -----------------------------------------------------------------------------------------

def check_dryland_heat(engine, weights) -> dict:
    cycles = cand.candidates(False)
    corn = np.array(["corn" in c for c in cycles])
    sorghum = np.array(["sorghum" in c and "corn" not in c for c in cycles])
    wheat_fallow = np.array([set(c) == {"winter wheat", "fallow"} for c in cycles])
    groups = {"sorghum, no corn": sorghum, "wheat-fallow": wheat_fallow}
    out = {"n_plans": {"corn": int(corn.sum()), "sorghum, no corn": int(sorghum.sum()),
                       "wheat-fallow": int(wheat_fallow.sum())}, "horizons": {}, "models": {}}
    table = {}
    for scenario, horizon in SETTINGS:
        ev = engine.evaluate(False, scenario, horizon, **TYPICAL)
        robust = ev.robust(weights)
        rk = ranks(robust)
        row = {"corn": {"score": float(robust[corn].mean()), "rank": float(rk[corn].mean()),
                        "best_rank": int(rk[corn].min())}}
        for name, mask in groups.items():
            row[name] = {"score": float(robust[mask].mean()), "rank": float(rk[mask].mean()),
                         "best_rank": int(rk[mask].min()),
                         "score_gap": float(robust[mask].mean() - robust[corn].mean()),
                         "rank_gap": float(rk[corn].mean() - rk[mask].mean())}
        table[(scenario, horizon)] = row
        out["horizons"][f"{scenario} {horizon}"] = row
    passed = True
    lines = []
    today = table[("ssp245", config.TODAY)]
    for scenario in config.SCENARIOS:
        far = table[(scenario, FAR)]
        for name in groups:
            keeps_score = far[name]["score_gap"] >= today[name]["score_gap"] - 0.005
            keeps_rank = far[name]["rank_gap"] >= today[name]["rank_gap"] - 0.5
            passed &= keeps_score and keeps_rank
            lines.append(
                f"{scenario}, {name} against corn plans: score lead {today[name]['score_gap']:+.3f} today -> "
                f"{far[name]['score_gap']:+.3f} in {FAR}; mean rank lead {today[name]['rank_gap']:+.1f} -> "
                f"{far[name]['rank_gap']:+.1f} places ({'kept or gained' if keeps_score and keeps_rank else 'LOST'})")
    # hottest against mildest model, far horizon, high scenario
    scenario = "ssp585"
    averages = engine.climate.averages(scenario, FAR)
    ev = engine.evaluate(False, scenario, FAR, **TYPICAL)
    overall = ev.overall(weights)
    hot, mild = int(np.argmax(averages["hot_days"])), int(np.argmin(averages["hot_days"]))
    for label, m in (("hottest", hot), ("mildest", mild)):
        rk = ranks(overall[:, m])
        entry = {"model": engine.climate.models[m], "hot_days": float(averages["hot_days"][m]),
                 "rain_mm": float(averages["rain_mm"][m]), "et0_mm": float(averages["et0_mm"][m]),
                 "top": code(cycles[int(np.argmax(overall[:, m]))])}
        for name, mask in groups.items():
            entry[name] = {"score_gap": float(overall[mask, m].mean() - overall[corn, m].mean()),
                           "rank_gap": float(rk[corn].mean() - rk[mask].mean())}
        entry["corn_failed"] = float(ev.failed[corn, m].mean())
        entry["sorghum_failed"] = float(ev.failed[sorghum, m].mean())
        out["models"][label] = entry
    h, m = out["models"]["hottest"], out["models"]["mildest"]
    for name in groups:
        ok = h[name]["score_gap"] >= m[name]["score_gap"] - 0.005
        passed &= ok
        lines.append(
            f"{scenario} {FAR}, {name} against corn plans: score lead {m[name]['score_gap']:+.3f} in the mildest model "
            f"({m['model']}, {m['hot_days']:.0f} hot days, {m['rain_mm']:.0f} mm season rain) -> "
            f"{h[name]['score_gap']:+.3f} in the hottest ({h['model']}, {h['hot_days']:.0f} hot days, "
            f"{h['rain_mm']:.0f} mm) ({'kept or gained' if ok else 'LOST'})")
    out.update(passed=bool(passed), lines=lines)
    out["note"] = (
        "History of this check: on its first run the last-but-one line failed (sorghum plans led corn plans by 0.072 "
        "in the mildest model and only 0.043 in the hottest). The raw numbers already pointed the other way (profit "
        "lead $23 -> $31 an acre, failed-crop lead 4 -> 8 points); the scores hid it because the first score scales "
        "were too narrow: in the hottest model 94% of dryland plans sat at risk score 0 and 56% at money score 0. "
        "The scales were widened to span every model run (config.py) and the check rerun; nothing else was changed."
    )
    return out


# --- check 2 -----------------------------------------------------------------------------------------

def check_irrigation(engine) -> dict:
    cycles = cand.candidates(True)
    out = {"by_setting": {}, "crops": {}}
    lines, passed = [], True
    base = engine.evaluate(True, "ssp245", config.TODAY, **TYPICAL)
    order = np.argsort(base.irrigation.mean(axis=1))
    quarter = max(1, len(cycles) // 4)
    thrifty, thirsty = order[:quarter], order[-quarter:]
    d_et0, d_irr = [], []
    for scenario in config.SCENARIOS:
        now = engine.evaluate(True, scenario, config.TODAY, **TYPICAL)
        far = engine.evaluate(True, scenario, FAR, **TYPICAL)
        ch = engine.climate.changes_by_model(scenario, FAR)
        d_et0 += list(ch["et0_pct"])
        d_irr += list((far.irrigation - now.irrigation).mean(axis=0))
        w_now, w_far = engine._window(scenario, config.TODAY), engine._window(scenario, FAR)
        p = PRACTICES.index("irrigated")
        per_crop = {}
        for ci, crop in enumerate(CROPS):
            if crop == "fallow":
                continue
            a = float(w_now["irrigation_mm"][p][:, ci, 0].mean())
            b = float(w_far["irrigation_mm"][p][:, ci, 0].mean())
            per_crop[crop] = [round(a), round(b)]
            passed &= b > a
        out["crops"][scenario] = per_crop
        row = {
            "et0_pct": float(ch["et0_pct"].mean()),
            "irrigation_now": float(now.irrigation.mean()), "irrigation_far": float(far.irrigation.mean()),
            "thirsty_water_now": float(now.water[thirsty].mean()), "thirsty_water_far": float(far.water[thirsty].mean()),
            "thrifty_water_now": float(now.water[thrifty].mean()), "thrifty_water_far": float(far.water[thrifty].mean()),
            "thirsty_irrigation_now": float(now.irrigation[thirsty].mean()),
            "thirsty_irrigation_far": float(far.irrigation[thirsty].mean()),
        }
        out["by_setting"][scenario] = row
        loses = row["thirsty_water_far"] < row["thirsty_water_now"]
        below = row["thirsty_water_far"] < row["thrifty_water_far"]
        passed &= loses and below and row["irrigation_far"] > row["irrigation_now"]
        lines.append(
            f"{scenario}: reference ET {row['et0_pct']:+.1f}% by {FAR}; mean pumping over all plans "
            f"{row['irrigation_now']:.0f} -> {row['irrigation_far']:.0f} mm a year; corn {per_crop['corn'][0]} -> "
            f"{per_crop['corn'][1]} mm, alfalfa {per_crop['alfalfa'][0]} -> {per_crop['alfalfa'][1]} mm; "
            f"water score of the thirstiest quarter of plans {row['thirsty_water_now']:.2f} -> "
            f"{row['thirsty_water_far']:.2f}, of the thriftiest quarter {row['thrifty_water_now']:.2f} -> "
            f"{row['thrifty_water_far']:.2f}")
    corr = float(np.corrcoef(d_et0, d_irr)[0, 1])
    out["et0_irrigation_correlation"] = corr
    passed &= corr > 0
    lines.append(f"Across the 12 model runs, change in reference ET against change in pumping by {FAR}: "
                 f"correlation {corr:+.2f} (rain moves it too).")
    ev = engine.evaluate(True, "ssp585", FAR, **TYPICAL)
    spearman = float(np.corrcoef(np.argsort(np.argsort(ev.irrigation.mean(axis=1))),
                                 np.argsort(np.argsort(ev.water.mean(axis=1))))[0, 1])
    out["irrigation_water_rank_correlation"] = spearman
    passed &= spearman < -0.99
    lines.append(f"Across plans, pumping against water score: rank correlation {spearman:+.2f}.")
    out.update(passed=bool(passed), lines=lines)
    return out


# --- check 3 -----------------------------------------------------------------------------------------

def _top_numbers(ev, weights) -> dict:
    robust = ev.robust(weights)
    keys = np.round(robust, 6)
    i = min(range(len(keys)), key=lambda j: (-keys[j], j))
    return {"plan": code(ev.plans.cycles[i]), "bare_days": float(ev.bare[i]),
            "irrigation_mm": float(ev.irrigation[i].mean()), "water_met": float(ev.water_met[i].mean()),
            "profit": float(ev.profit[i].mean()), "failed": float(ev.failed[i].mean())}


def check_weights(engine, fields_by_practice) -> dict:
    equal = smart.Weights(1, 1, 1, 1).normalised()
    heavy = {name: smart.Weights(**{n: (HEAVY if n == name else (1 - HEAVY) / 3) for n in config.SCORE_NAMES}).normalised()
             for name in config.SCORE_NAMES}
    # what must not get worse when a priority is turned up: (number, direction), per practice
    target = {"soil": ("bare_days", -1), "money": ("profit", +1), "risk": ("failed", -1)}
    cases, lines, passed = [], [], True
    tally = {name: {"same_or_better": 0, "better": 0, "worse": 0, "n": 0} for name in config.SCORE_NAMES}
    examples = {}
    for irrigated in (True, False):
        soils = sorted({(engine.traits_for(f.field_id).wei if engine.traits_for(f.field_id) else None,
                         None if f.slope_pct is None else round(min(f.slope_pct, 6.0)))
                        for f in fields_by_practice[irrigated]}, key=lambda t: (t[0] is None, t[0], t[1] is None, t[1]))
        for scenario, horizon in SETTINGS:
            for wei, slope in soils:
                ev = engine.evaluate(irrigated, scenario, horizon, wei=wei, slope_pct=slope)
                base = _top_numbers(ev, equal)
                for name in config.SCORE_NAMES:
                    up = _top_numbers(ev, heavy[name])
                    number, sign = target.get(name, ("irrigation_mm", -1) if irrigated else ("water_met", +1))
                    delta = sign * (up[number] - base[number])
                    t = tally[name]
                    t["n"] += 1
                    t["same_or_better"] += delta >= -TOLERANCE
                    t["better"] += delta > 1e-6
                    t["worse"] += delta < -TOLERANCE
                    cases.append({"irrigated": irrigated, "scenario": scenario, "horizon": horizon, "wei": wei,
                                  "slope": slope, "weight": name, "base": base, "up": up})
                    key = (name, irrigated)
                    if key not in examples and (wei, slope) == (56.0, 1) and (scenario, horizon) == ("ssp245", config.TODAY):
                        examples[key] = (base, up, number)
    words = {"bare_days": ("bare days", "{:.0f}"), "irrigation_mm": ("mm pumped", "{:.0f}"),
             "water_met": ("share of water need met", "{:.2f}"), "profit": ("$ an acre", "{:.0f}"),
             "failed": ("share of crops failing", "{:.2f}")}
    for name in config.SCORE_NAMES:
        t = tally[name]
        passed &= t["worse"] == 0
        bits = []
        for irrigated in (True, False):
            if (name, irrigated) in examples:
                base, up, number = examples[(name, irrigated)]
                label, fmt = words[number]
                bits.append(f"{'irrigated' if irrigated else 'dryland'} {base['plan']} -> {up['plan']}, {label} "
                            f"{fmt.format(base[number])} -> {fmt.format(up[number])}")
        lines.append(f"{name} weight 0.25 -> {HEAVY}: the top plan's number moved the right way or stayed in "
                     f"{t['same_or_better']} of {t['n']} cases (strictly better in {t['better']}, worse in {t['worse']}). "
                     f"Typical loam field today: {'; '.join(bits)}.")
    return {"passed": bool(passed), "lines": lines, "tally": tally, "n_cases": len(cases)}


# --- check 4 -----------------------------------------------------------------------------------------

def check_soil_order(engine) -> dict:
    lines, passed, out = [], True, {}
    for irrigated in (True, False):
        practice = "irrigated" if irrigated else "dryland"
        cycles = list(cand.candidates(irrigated))
        extra = [] if irrigated else [("alfalfa",) * 4]          # not a dryland candidate; scored for the comparison
        ev = engine.evaluate(irrigated, "ssp245", config.TODAY, rotations=cycles + extra, **TYPICAL)
        soil, ero = ev.soil.mean(axis=1), ev.erosion.mean(axis=1)
        fallow = np.array([sum(1 for c in cy if c == "fallow") / len(cy) for cy in cycles + extra])
        alfalfa = np.array(["alfalfa" in cy for cy in cycles + extra])
        best, worst = int(np.argmax(soil)), int(np.argmin(soil))
        lowest, highest = int(np.argmin(ero)), int(np.argmax(ero))
        by_fallow = {}
        for share in sorted(set(np.round(fallow, 3))):
            mask = (np.round(fallow, 3) == share) & ~alfalfa
            if mask.any():
                by_fallow[f"{share:.2f}"] = {"soil": float(soil[mask].mean()), "erosion": float(ero[mask].mean()),
                                             "n": int(mask.sum())}
        soils = [v["soil"] for v in by_fallow.values()]
        eros = [v["erosion"] for v in by_fallow.values()]
        monotone = all(a > b for a, b in zip(soils, soils[1:])) and all(a < b for a, b in zip(eros, eros[1:]))
        most_fallow = fallow.max()
        ok = (alfalfa[best] and alfalfa[lowest] and fallow[worst] == most_fallow and fallow[highest] == most_fallow
              and monotone and soil[alfalfa].min() > soil[~alfalfa].max())
        passed &= ok
        out[practice] = {"best": code((cycles + extra)[best]), "worst": code((cycles + extra)[worst]),
                         "lowest_erosion": code((cycles + extra)[lowest]), "highest_erosion": code((cycles + extra)[highest]),
                         "by_fallow_share": by_fallow, "alfalfa_min_soil": float(soil[alfalfa].min()),
                         "other_max_soil": float(soil[~alfalfa].max())}
        steps = "; ".join(f"fallow {float(k):.0%} of years: soil {v['soil']:.2f}, erosion {v['erosion']:.2f}"
                          for k, v in by_fallow.items())
        lines.append(
            f"{practice}: best soil score {plan_label((cycles + extra)[best])} ({soil[best]:.2f}, erosion index "
            f"{ero[best]:.2f}){' [alfalfa is not a dryland candidate; scored here only to compare]' if not irrigated else ''}; "
            f"worst {plan_label((cycles + extra)[worst])} ({soil[worst]:.2f}, erosion {ero[worst]:.2f}). "
            f"Plans without alfalfa: {steps}. Every alfalfa plan scores above every other plan on soil: "
            f"{'yes' if soil[alfalfa].min() > soil[~alfalfa].max() else 'NO'}.")
    out.update(passed=bool(passed), lines=lines)
    return out


# --- check 5, speed, examples ------------------------------------------------------------------------

def check_results(engine, fields_by_practice) -> dict:
    weight_sets = [None, {"soil": 1, "water": 0, "money": 0, "risk": 0}, {"soil": 0, "water": 0, "money": 1, "risk": 0},
                   {"soil": 1, "water": 3, "money": 1, "risk": 3}]
    n_calls = n_plans = broken = out_of_range = not_deterministic = too_similar = long_reason = fewer = 0
    relaxed = 0
    third_rank = []
    for irrigated, fields in fields_by_practice.items():
        for k, f in enumerate(fields):
            scenario, horizon = SETTINGS[k % len(SETTINGS)] if k % 4 else ("ssp585", 2040)
            weights = weight_sets[k % len(weight_sets)]
            a = smart.rank_plans_smart(f, weights=weights, scenario=scenario, horizon=horizon, engine=engine)
            b = smart.rank_plans_smart(f, weights=weights, scenario=scenario, horizon=horizon, engine=engine)
            n_calls += 1
            da, db = a.to_dict(), b.to_dict()
            not_deterministic += json.dumps(da, sort_keys=True) != json.dumps(db, sort_keys=True)
            fewer += len(a.plans) < config.TOP_PLANS
            for plan in a.plans:
                n_plans += 1
                broken += bool(cand.rules_broken(plan.cycle, a.settings["irrigated"]))
                values = list(plan.scores.values()) + [plan.spread["overall"][x] for x in ("low", "mid", "high")]
                out_of_range += any(not (0.0 <= v <= 1.0) for v in values)
                long_reason += len(plan.reason) >= config.MAX_REASON_CHARS or not plan.reason.endswith(".")
            for i, p in enumerate(a.plans):
                for q in a.plans[i + 1:]:
                    d = cand.years_different(p.cycle, q.cycle)
                    too_similar += d <= 1e-9
                    relaxed += d <= config.SAME_CHOICE_YEARS + 1e-9
            if len(a.plans) >= 3:
                third_rank.append(a.plans[2].rank)
            if a.current is not None:
                values = list(a.current.scores.values())
                out_of_range += any(not (0.0 <= v <= 1.0) for v in values)
    arrays_ok = True
    for irrigated in (True, False):
        for scenario, horizon in SETTINGS:
            ev = engine.evaluate(irrigated, scenario, horizon, wei=250.0, slope_pct=6.0)
            for arr in (ev.soil, ev.water, ev.money, ev.risk):
                arrays_ok &= bool(np.isfinite(arr).all() and arr.min() >= 0.0 and arr.max() <= 1.0)
    cells = at_end = 0
    for irrigated in (True, False):          # typical field: do scores pile up at the ends of their scales?
        for scenario in config.SCENARIOS:
            for horizon in config.SUMMARY_HORIZONS:
                ev = engine.evaluate(irrigated, scenario, horizon, **TYPICAL)
                for name, arr in (("soil", ev.soil), ("water", ev.water), ("money", ev.money), ("risk", ev.risk)):
                    if name == "risk" and irrigated:
                        continue             # irrigated crops hardly ever fail in the model: 1.0 is the honest value
                    cells += arr.size
                    at_end += int(((arr <= 0.0) | (arr >= 1.0)).sum())
    passed = not (broken or out_of_range or not_deterministic or too_similar or long_reason or fewer) and arrays_ok
    lines = [
        f"{n_calls} real fields ({', '.join(f'{len(v)} ' + ('irrigated' if k else 'dryland') for k, v in fields_by_practice.items())}), "
        f"each ranked twice with rotating weights, scenarios and horizons: {n_plans} recommended plans, "
        f"{broken} break a rule, {out_of_range} have a score outside 0-1, {not_deterministic} of {n_calls} "
        f"repeated calls differ, {fewer} calls returned fewer than {config.TOP_PLANS} plans, "
        f"{long_reason} reasons are {config.MAX_REASON_CHARS}+ characters.",
        f"Top 3 are different choices (more than one year in four apart) in all but {relaxed} plan pairs; "
        f"{too_similar} pairs have the same crop mix. The third plan shown is on average number "
        f"{np.mean(third_rank):.1f} in the full ranking (worst case {max(third_rank)}).",
        f"All candidate scores on the most erodible soil and steepest slope stay within 0-1: {'yes' if arrays_ok else 'NO'}.",
        f"On a typical field, over both scenarios and every fifth year to 2060, {at_end} of {cells:,} plan-by-model "
        "scores sit exactly at 0 or 1 (the end of a scale, where plans could not be told apart).",
    ]
    return {"passed": bool(passed), "lines": lines, "calls": n_calls, "plans": n_plans, "rules_broken": broken,
            "out_of_range": out_of_range, "not_deterministic": not_deterministic, "relaxed_pairs": relaxed,
            "scores_at_scale_end": at_end, "score_cells": cells,
            "third_rank_mean": float(np.mean(third_rank)), "third_rank_max": int(max(third_rank))}


def measure_speed(fields) -> dict:
    smart.reset()
    t = time.perf_counter()
    engine = smart.get_engine()
    load_ms = (time.perf_counter() - t) * 1e3
    t = time.perf_counter()
    smart.rank_plans_smart(fields[0])
    first_ms = (time.perf_counter() - t) * 1e3
    times = []
    horizons = list(range(config.HORIZON_MIN, config.HORIZON_MAX + 1, 5))
    for k, f in enumerate(fields[:400]):
        scenario = config.SCENARIOS[k % 2]
        horizon = horizons[k % len(horizons)]
        t = time.perf_counter()
        smart.rank_plans_smart(f, scenario=scenario, horizon=horizon,
                               weights={"soil": 1 + k % 3, "water": 1, "money": 2, "risk": k % 2})
        times.append((time.perf_counter() - t) * 1e3)
    times = np.array(times)
    t = time.perf_counter()
    engine.climate_summary()
    summary_ms = (time.perf_counter() - t) * 1e3
    fresh_ms = None
    try:      # the same load in a fresh interpreter, imports included (what the first request after a restart pays)
        import subprocess
        import sys

        probe = ("import time; t = time.perf_counter(); from api.planner import smart; smart.get_engine(); "
                 "print((time.perf_counter() - t) * 1e3)")
        done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
                              cwd=str(config.ROOT))
        fresh_ms = float(done.stdout.strip().splitlines()[-1])
    except Exception:
        fresh_ms = None
    return {"load_ms": load_ms, "fresh_process_load_ms": fresh_ms, "first_call_ms": first_ms, "mean_ms": float(times.mean()),
            "p95_ms": float(np.percentile(times, 95)), "max_ms": float(times.max()), "calls": int(len(times)),
            "climate_summary_ms": summary_ms}


def _top3(result) -> list:
    return [f"{p.name} [{p.scores['robust']:.2f}]" for p in result.plans]


CLEAR_MARGIN = 0.01     # the new first plan must beat the old one by this much on the ranking score in 2060


def find_examples(engine, fields_by_practice, weights=None, want: int = 5, stats: dict | None = None) -> list:
    """Real fields whose first plan today is not their first plan in 2060, clearest first, at most one
    per (practice, soil) so the list is not five copies of one story. `stats` is filled with how many
    sampled fields change their first plan at all."""
    found = {}
    stats = stats if stats is not None else {}
    n_fields = 0
    changed_count = {s: {True: 0, False: 0} for s in config.SCENARIOS}
    for irrigated, fields in fields_by_practice.items():
        for f in fields:
            traits = engine.traits_for(f.field_id)
            key = (irrigated, traits.wei if traits else None)
            now = smart.rank_plans_smart(f, weights=weights, engine=engine)
            entry = {"field_id": f.field_id, "irrigated": irrigated, "wei": key[1], "slope_pct": f.slope_pct,
                     "today": _top3(now), "today_top": now.plans[0], "scenarios": {}, "margin": 0.0}
            w = smart.Weights.of(weights).normalised()
            for scenario in config.SCENARIOS:
                far = smart.rank_plans_smart(f, weights=weights, scenario=scenario, horizon=FAR, engine=engine)
                changed = far.plans[0].cycle != now.plans[0].cycle
                old = engine.evaluate(irrigated, scenario, FAR, rotations=[now.plans[0].cycle],
                                      wei=key[1], weg=traits.weg if traits else None, slope_pct=f.slope_pct)
                margin = far.plans[0].scores["robust"] - float(old.robust(w)[0]) if changed else 0.0
                old_n = {"profit": float(old.profit[0].mean()), "failed": float(old.failed[0].mean()),
                         "irrigation": float(old.irrigation[0].mean()), "holds": int(old.holds[0].sum())}
                entry["scenarios"][scenario] = {"top3": _top3(far), "changed": bool(changed), "margin": float(margin),
                                                "label": far.climate["label"], "new_top": far.plans[0],
                                                "old_top_then": old_n}
                entry["margin"] = max(entry["margin"], margin)
            n_fields += 1
            for scenario in config.SCENARIOS:
                changed_count[scenario][irrigated] += entry["scenarios"][scenario]["changed"]
            if entry["margin"] >= CLEAR_MARGIN and (key not in found or entry["margin"] > found[key]["margin"] + 1e-9):
                found[key] = entry
    stats["fields"] = n_fields
    stats["changed"] = {s: {("irrigated" if k else "dryland"): int(v) for k, v in d.items()} for s, d in changed_count.items()}
    stats["sample"] = {("irrigated" if k else "dryland"): len(v) for k, v in fields_by_practice.items()}
    out = sorted(found.values(), key=lambda e: (-e["margin"], e["field_id"]))[:want]
    for e in out:
        t = e.pop("today_top")
        tn = t.numbers
        whys = []
        for scenario, s in e["scenarios"].items():
            n, o = s.pop("new_top"), s.pop("old_top_then")
            if not s["changed"]:
                whys.append(f"{scenario}: no change of first plan")
                continue
            water_old = (f"pumping {tn['irrigation_mm']:.0f} -> {o['irrigation']:.0f} mm" if e["irrigated"]
                         else f"failed crops {tn['failed_share']:.0%} -> {o['failed']:.0%}")
            water_new = (f"pumps {n.numbers['irrigation_mm']:.0f} mm" if e["irrigated"]
                         else f"{n.numbers['failed_share']:.0%} of crops fail")
            whys.append(
                f"{scenario} ({s['label']}): {t.name} goes from ${tn['profit_per_acre']:.0f} to ${o['profit']:.0f} an acre, "
                f"{water_old}, holds up in {t.futures['holds']} -> {o['holds']} of 6; {n.name} then earns "
                f"${n.numbers['profit_per_acre']:.0f}, {water_new}, holds up in {n.futures['holds']} of 6 "
                f"(ahead by {s['margin']:.3f})")
        e["why"] = whys
    return out


# --- the report --------------------------------------------------------------------------------------

def write_report(path: Path, report: dict) -> None:
    c = report["checks"]
    lines = [
        "# Planner sanity checks",
        "",
        f"Written by `python -m api.planner.sanity` on {report['written']}. Real fields, real tables. "
        f"Default weights soil {config.DEFAULT_WEIGHTS['soil']}, water {config.DEFAULT_WEIGHTS['water']}, money "
        f"{config.DEFAULT_WEIGHTS['money']}, risk {config.DEFAULT_WEIGHTS['risk']} unless a check says otherwise; "
        f"\"typical field\" = wind erodibility 56, slope 0.84%. Soil source: {report['soil']['name']}. "
        "Full numbers: data/logs/phase3/planner_sanity.json.",
        "",
        f"Candidates: {report['candidates']['irrigated']} plans for irrigated fields, "
        f"{report['candidates']['dryland']} for dryland fields (4-year and 3-year cycles).",
        "",
        "| check | result |",
        "|---|---|",
    ]
    names = {"dryland_heat": "1. dryland: sorghum and wheat-fallow against corn as it gets hotter",
             "irrigation": "2. irrigated: pumping rises with ET, thirsty plans lose water score",
             "weights": "3. turning a priority up moves the top plan the right way",
             "soil_order": "4. fallow-heavy worst on soil and erosion, alfalfa best",
             "results": "5. rules kept, scores in 0-1, deterministic"}
    for key, title in names.items():
        lines.append(f"| {title} | {'PASS' if c[key]['passed'] else 'FAIL'} |")
    for key, title in names.items():
        lines += ["", f"## {title}", ""]
        lines += [f"- {line}" for line in c[key]["lines"]]
        if c[key].get("note"):
            lines += ["", c[key]["note"]]
    s = report["speed"]
    lines += [
        "", "## Speed", "",
        f"- Loading everything (climate tables, crop response for {report['model_years']} model-years, bare-day table, "
        f"field traits): {s['load_ms']:.0f} ms with the libraries already imported"
        + (f", {s['fresh_process_load_ms']:.0f} ms in a fresh process with the imports" if s.get("fresh_process_load_ms") else "")
        + f"; once per process. First call after that: {s['first_call_ms']:.1f} ms.",
        f"- {s['calls']} calls on different fields, weights, scenarios and horizons: mean {s['mean_ms']:.2f} ms, "
        f"95th percentile {s['p95_ms']:.2f} ms, slowest {s['max_ms']:.2f} ms. Budget: 150 ms.",
        f"- climate_summary(): {s['climate_summary_ms']:.1f} ms.",
        "", "## Bare days a year by crop (crop_average)", "",
        "| crop | irrigated | dryland |", "|---|---|---|",
    ]
    for crop in CROPS:
        lines.append(f"| {crop} | {report['bare_days']['irrigated'][crop]:.0f} | {report['bare_days']['dryland'][crop]:.0f} |")
    lines += ["", report["soil"]["note"], "", "## Fields where the first plan changes between today and 2060", ""]
    lines.append("How many sampled fields get a different first plan in 2060 than today (dryland / irrigated):")
    for label, st in report["first_plan_changes"].items():
        n = st["sample"]
        lines.append(f"- weights {label}: " + "; ".join(
            f"{scenario} {st['changed'][scenario]['dryland']} of {n['dryland']} / {st['changed'][scenario]['irrigated']} of {n['irrigated']}"
            for scenario in st["changed"]))
    lines += ["", f"The planner's numbers are county-level, so fields of the same practice, soil and slope change alike. "
              f"Below: the clearest case per practice and weights (new first plan ahead of the old one by at least "
              f"{CLEAR_MARGIN} on the ranking score in 2060); the number in brackets is the ranking score.", ""]
    if not report["examples"]:
        lines.append("None found in the sample.")
    for e in report["examples"]:
        lines += [
            f"- **{e['field_id']}** ({'irrigated' if e['irrigated'] else 'dryland'}, wind erodibility "
            f"{e['wei'] if e['wei'] is not None else 'unknown'}, slope "
            f"{'unknown' if e['slope_pct'] is None else format(e['slope_pct'], '.1f') + '%'}; weights: {e['weights']})",
            f"  - today: {'; '.join(e['today'])}",
        ]
        for scenario, sc in e["scenarios"].items():
            lines.append(f"  - {FAR} {scenario}: {'; '.join(sc['top3'])}")
        lines += [f"  - why, {w}" for w in e["why"]]
    path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the planner's sanity checks on the real fields.")
    parser.add_argument("--report", default=str(config.DATA_DIR / "planner_sanity.md"))
    parser.add_argument("--json", default=str(config.DATA_DIR / "logs" / "phase3" / "planner_sanity.json"))
    parser.add_argument("--no-cache-write", action="store_true", help="do not refresh data/planner_bare_days.json")
    args = parser.parse_args(argv)

    settings = config.settings()
    if not smart.available():
        print("climate tables missing or unusable: nothing to check (the API uses the old planner)")
        return 2
    if not Path(settings.fields_path).is_file():
        print(f"{settings.fields_path} is missing: the checks need the real fields")
        return 2
    if not args.no_cache_write:
        soil_effect.load_table(settings, write_cache=True, use_cache=False)
        smart.reset()
    fields = load_fields(settings.fields_path)
    speed = measure_speed(fields)
    engine = smart.get_engine()
    weights = smart.Weights().normalised()
    by_practice = sample_fields(fields, engine)

    checks = {
        "dryland_heat": check_dryland_heat(engine, weights),
        "irrigation": check_irrigation(engine),
        "weights": check_weights(engine, by_practice),
        "soil_order": check_soil_order(engine),
        "results": check_results(engine, by_practice),
    }
    examples, change_stats = [], {}
    for weights_used, label in ((None, "default"), ({"soil": 1, "water": 1, "money": 1, "risk": 3}, "risk first (1, 1, 1, 3)"),
                                ({"soil": 1, "water": 3, "money": 1, "risk": 1}, "water first (1, 3, 1, 1)")):
        change_stats[label] = {}
        for e in find_examples(engine, by_practice, weights_used, stats=change_stats[label]):
            e["weights"] = label
            if all(x["field_id"] != e["field_id"] for x in examples):
                examples.append(e)
    # default weights first, then the clearest; at most one per practice-and-weights, five in all
    examples = sorted(examples, key=lambda e: (e["weights"] != "default", -e["margin"], e["field_id"]))
    picked, seen = [], set()
    for e in examples:
        key = (e["irrigated"], e["weights"])
        if key in seen or len(picked) >= 5:
            continue
        picked.append(e)
        seen.add(key)
    describe = getattr(engine.soil, "describe", lambda: {"name": engine.soil.name, "note": ""})()
    table = getattr(engine.soil, "table", None)
    report = {
        "written": time.strftime("%Y-%m-%d"),
        "candidates": {"irrigated": len(cand.candidates(True)), "dryland": len(cand.candidates(False))},
        "model_years": int(len(engine.climate.models) * len(engine.climate.scenarios) * len(engine.climate.years)),
        "soil": describe,
        "bare_days": table.by_crop if table is not None else soil_effect.BUILTIN_BY_CROP,
        "economics": money.describe(engine.economics, money.normal_yields(engine.params)),
        "checks": checks, "speed": speed, "examples": picked, "first_plan_changes": change_stats,
    }
    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=1, default=float))
    write_report(Path(args.report), report)
    failed = [name for name, check in checks.items() if not check["passed"]]
    print(f"report: {args.report}")
    print(f"numbers: {args.json}")
    print("checks: " + ", ".join(f"{name} {'PASS' if check['passed'] else 'FAIL'}" for name, check in checks.items()))
    print(f"speed: load {speed['load_ms']:.0f} ms once; a call {speed['mean_ms']:.2f} ms mean, {speed['max_ms']:.2f} ms max")
    print(f"examples of a changed first plan: {len(picked)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
