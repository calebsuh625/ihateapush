"""How many days a year the soil sits bare under each crop: the planner's soil signal.

One interface, two implementations, one setting (`SOIL_EFFECT_SOURCE`, read in config.py):

    class SoilEffectProvider(Protocol):
        name: str
        def bare_days(self, crop: str, prev_crop: str | None, irrigated: bool) -> float: ...

`bare_days` is the satellite estimate of days in the year with no crop and no residue cover
(data/soil_health.parquet, Phase 2A). It measures exposure, not what the soil has become, and one
field-year only tells a fallow year from a cropped one; averaged over thousands of field-years it
says how bare each crop leaves the ground. Lower is better.

crop_average (default)
----------------------
Average bare days by crop, and by (previous crop -> crop), separately for irrigated and dryland
fields, from the 2016-2024 soil table joined to each field's crop history (data/fields.parquet) and
its irrigation guess (data/field_traits.parquet; crop history alone when that file is missing).

* Year effects removed. The county-wide level moves a lot from year to year (2022 and 2023 were about
  25 days above the others), so each field-year is compared within its own year: a two-way fit
  (cell + year, by alternating averages) gives one shift per year, centred so the nine years average
  zero, and the shift is taken off before averaging. It is one shift for all crops, which is a
  simplification: a dry year adds bare days where the ground is bare at times and none under alfalfa.
* Small cells shrunk. A (previous crop -> crop) cell is pulled towards its crop's average as if the
  average were `SHRINK_FIELD_YEARS` (30) more field-years; a crop's own average is pulled the same way
  towards the crop's average over both practices (this matters for dryland soybeans, 19 field-years).
* Built once from the tables (about a second), kept in memory; `python -m api.planner.soil_effect`
  also writes the small result to data/planner_bare_days.json, which is used instead of rebuilding
  while the three source files have not changed. When the soil table or the fields file is missing,
  the built-in constants below are used (the same numbers by crop, without the previous-crop detail).

effects
-------
The crop averages, plus what Phase 2B measured between matched neighbouring fields
(data/effects.json; schema.md). Phase 2B compares rotation TYPES (alfalfa/perennial, diverse,
corn-soy, wheat-sorghum-fallow, continuous corn), not crops, so this provider adds a shift per
rotation type on top of the per-crop numbers (`rotation_shift`; `plan_bare_days` applies it).

The number taken from a comparison is its `beyond_crops` effect: the part of the measured gap that
the crops grown do not explain. The crop averages already carry the part they do explain
(`from_crops`), so using the whole `effect` would count it twice. A comparison is usable when all of
these hold: the results are from the real soil table (`is_fake` false) and about `bare_days`; there is
a row for the field's own practice (irrigated / dryland) with at least 20 matched pairs; that row's
`beyond_crops` interval excludes zero; and none of the comparison's fake-switch placebos found an
"effect" where there cannot be one. A comparison measures a gap between two types, not a level, so
the gap is split evenly around the crop-average level (type A gets +gap/2, type B gets -gap/2; with
several usable comparisons, the least-squares shifts with the smallest total). Everything else falls
back to the crop averages unchanged. A plan's type is read with pipeline/causal/rotations.py from the
plan's cycle run for twelve years.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from api.planner import config
from api.planner.crops import CROPS, PRACTICES

# Bare days a year by crop, year effects removed, from the real tables on 2026-11-14 (65,000
# field-years, 2016-2024; `python -m api.planner.soil_effect` prints the current ones). Used only when
# the soil table or the fields file is missing. Fallow is the barest, alfalfa the least bare.
BUILTIN_BY_CROP = {
    "irrigated": {"corn": 54.6, "soybeans": 50.4, "winter wheat": 31.0, "sorghum": 47.6, "alfalfa": 6.9,
                  "fallow": 72.8},
    "dryland": {"corn": 73.6, "soybeans": 51.3, "winter wheat": 51.8, "sorghum": 59.9, "alfalfa": 16.6,
                "fallow": 143.3},
}

CACHE_VERSION = 1


def _practice(irrigated: bool) -> str:
    return "irrigated" if irrigated else "dryland"


class SoilEffectProvider(Protocol):
    name: str

    def bare_days(self, crop: str, prev_crop: str | None, irrigated: bool) -> float: ...


# --- the table ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BareDayTable:
    by_crop: dict                      # practice -> crop -> days
    by_prev: dict                      # practice -> crop -> previous crop -> days (shrunk); may lack cells
    n_crop: dict = field(default_factory=dict)   # practice -> crop -> field-years
    n_prev: dict = field(default_factory=dict)   # practice -> crop -> previous crop -> field-years
    source: str = "builtin"            # "tables", "cache" or "builtin"
    info: dict = field(default_factory=dict)

    def bare_days(self, crop: str, prev_crop: str | None, irrigated: bool) -> float:
        practice = _practice(irrigated)
        if crop not in self.by_crop[practice]:
            raise KeyError(f"unknown crop {crop!r}; expected one of {CROPS}")
        if prev_crop is not None:
            cell = self.by_prev.get(practice, {}).get(crop, {}).get(prev_crop)
            if cell is not None:
                return float(cell)
        return float(self.by_crop[practice][crop])

    def to_json(self) -> dict:
        return {"version": CACHE_VERSION, "by_crop": self.by_crop, "by_prev": self.by_prev,
                "n_crop": self.n_crop, "n_prev": self.n_prev, "info": self.info}

    @staticmethod
    def from_json(raw: dict, source: str = "cache") -> "BareDayTable":
        by_crop = {p: {c: float(raw["by_crop"][p][c]) for c in CROPS} for p in PRACTICES}
        by_prev = {p: {c: {q: float(v) for q, v in (raw.get("by_prev", {}).get(p, {}).get(c) or {}).items()}
                       for c in CROPS} for p in PRACTICES}
        return BareDayTable(by_crop=by_crop, by_prev=by_prev, n_crop=raw.get("n_crop") or {},
                            n_prev=raw.get("n_prev") or {}, source=source, info=raw.get("info") or {})


def builtin_table() -> BareDayTable:
    return BareDayTable(by_crop={p: dict(BUILTIN_BY_CROP[p]) for p in PRACTICES},
                        by_prev={p: {c: {} for c in CROPS} for p in PRACTICES}, source="builtin",
                        info={"note": "built-in constants (soil table or fields file missing)"})


def build_table(soil, histories: dict, irrigation: dict | None = None,
                shrink: float = config.SHRINK_FIELD_YEARS) -> BareDayTable:
    """The bare-day table from a soil table and crop histories. Pure: nothing is read or written.

    soil        DataFrame with `field_id`, `year`, `bare_days` (rows with a null value are skipped)
    histories   {field_id: {"2016": "corn", ...}} (year -> lowercase crop name)
    irrigation  {field_id: "irrigated" | "dryland"}; a field not in it is guessed from its history
    """
    import numpy as np
    import pandas as pd

    from api.planner.traits import guess_irrigation

    irrigation = irrigation or {}
    rows = []
    for field_id, history in histories.items():
        practice = irrigation.get(field_id)
        if practice not in PRACTICES:
            practice = _practice(guess_irrigation(history)[0])
        years = {}
        for year, crop in (history or {}).items():
            try:
                years[int(year)] = str(crop).strip().lower()
            except (TypeError, ValueError):
                continue
        for year, crop in years.items():
            if crop in CROPS:
                rows.append((field_id, year, crop, years.get(year - 1), practice))
    hist = pd.DataFrame(rows, columns=["field_id", "year", "crop", "prev", "practice"])
    obs = soil[["field_id", "year", "bare_days"]].dropna(subset=["bare_days"])
    d = obs.merge(hist, on=["field_id", "year"], how="inner")
    if len(d) == 0:
        table = builtin_table()
        return BareDayTable(table.by_crop, table.by_prev, source="builtin",
                            info={"note": "no field-year could be joined to a crop: built-in constants"})
    d["prev_cell"] = d["prev"].where(d["prev"].isin(CROPS), "other")
    y = d["bare_days"].to_numpy(dtype=float)

    # year effects: cell + year, by alternating averages (a few rounds are plenty: years and cells mix well)
    cell = d.groupby(["practice", "crop", "prev_cell"], sort=False).ngroup().to_numpy()
    year_codes, year_values = pd.factorize(d["year"], sort=True)
    shift = np.zeros(len(year_values))
    for _ in range(30):
        resid = y - shift[year_codes]
        mu = np.bincount(cell, weights=resid) / np.bincount(cell)
        resid = y - mu[cell]
        new = np.bincount(year_codes, weights=resid, minlength=len(year_values)) / np.bincount(
            year_codes, minlength=len(year_values))
        new = new - new.mean()
        done = np.max(np.abs(new - shift)) < 1e-6
        shift = new
        if done:
            break
    d["adj"] = y - shift[year_codes]

    pooled = d.groupby("crop")["adj"].agg(["mean", "size"])
    crop_stats = d.groupby(["practice", "crop"])["adj"].agg(["mean", "size"])
    prev_stats = d[d["prev"].isin(CROPS)].groupby(["practice", "crop", "prev"])["adj"].agg(["mean", "size"])

    by_crop, by_prev, n_crop, n_prev = {}, {}, {}, {}
    for practice in PRACTICES:
        by_crop[practice], by_prev[practice], n_crop[practice], n_prev[practice] = {}, {}, {}, {}
        for crop in CROPS:
            anchor = float(pooled.loc[crop, "mean"]) if crop in pooled.index else BUILTIN_BY_CROP[practice][crop]
            if (practice, crop) in crop_stats.index:
                m, n = float(crop_stats.loc[(practice, crop), "mean"]), int(crop_stats.loc[(practice, crop), "size"])
            else:
                m, n = anchor, 0
            level = max(0.0, (n * m + shrink * anchor) / (n + shrink)) if n + shrink > 0 else anchor
            by_crop[practice][crop] = round(level, 1)
            n_crop[practice][crop] = n
            by_prev[practice][crop], n_prev[practice][crop] = {}, {}
            for prev in CROPS:
                if (practice, crop, prev) in prev_stats.index:
                    pm = float(prev_stats.loc[(practice, crop, prev), "mean"])
                    pn = int(prev_stats.loc[(practice, crop, prev), "size"])
                else:
                    pm, pn = level, 0
                value = (pn * pm + shrink * level) / (pn + shrink) if pn + shrink > 0 else level
                by_prev[practice][crop][prev] = round(max(0.0, value), 1)
                n_prev[practice][crop][prev] = pn
    info = {
        "field_years": int(len(d)),
        "years": [int(year_values.min()), int(year_values.max())],
        "year_shift_days": {str(int(yv)): round(float(s), 1) for yv, s in zip(year_values, shift)},
        "shrink_field_years": float(shrink),
    }
    return BareDayTable(by_crop=by_crop, by_prev=by_prev, n_crop=n_crop, n_prev=n_prev, source="tables", info=info)


def _signature(paths: Sequence[Path]) -> list:
    out = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            st = p.stat()
            out.append([p.name, st.st_size, st.st_mtime_ns])
        else:
            out.append([p.name, None, None])
    return out


def _read_sources(settings: config.Settings):
    """(soil, histories, irrigation) from the files; raises when the soil table or fields are unusable."""
    import pandas as pd

    soil = pd.read_parquet(settings.soil_health_path, columns=["field_id", "year", "bare_days"])
    fields = pd.read_parquet(settings.fields_path, columns=["field_id", "crop_history"])
    histories = {}
    for field_id, text in zip(fields["field_id"].tolist(), fields["crop_history"].tolist()):
        try:
            value = json.loads(text) if isinstance(text, str) else dict(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            histories[field_id] = value
    irrigation = {}
    if Path(settings.field_traits_path).is_file():
        try:
            traits = pd.read_parquet(settings.field_traits_path, columns=["field_id", "irrigation"])
            irrigation = dict(zip(traits["field_id"].tolist(), traits["irrigation"].tolist()))
        except Exception:
            irrigation = {}
    return soil, histories, irrigation


def load_table(settings: config.Settings | None = None, *, write_cache: bool = False,
               use_cache: bool = True) -> BareDayTable:
    """The table for these settings: the cache when it matches the source files, else built from the
    tables, else the built-in constants. Only writes the cache when asked to (`write_cache`)."""
    settings = settings or config.settings()
    sources = [settings.soil_health_path, settings.fields_path, settings.field_traits_path]
    if not (Path(settings.soil_health_path).is_file() and Path(settings.fields_path).is_file()):
        return builtin_table()
    signature = _signature(sources)
    cache = Path(settings.soil_cache_path)
    if use_cache and cache.is_file():
        try:
            raw = json.loads(cache.read_text())
            if raw.get("version") == CACHE_VERSION and raw.get("signature") == signature \
                    and raw.get("info", {}).get("shrink_field_years") == config.SHRINK_FIELD_YEARS:
                return BareDayTable.from_json(raw, source="cache")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        table = build_table(*_read_sources(settings))
    except Exception as exc:     # an unreadable table must not take the planner down
        fallback = builtin_table()
        return BareDayTable(fallback.by_crop, fallback.by_prev, source="builtin",
                            info={"note": f"could not build from the tables ({type(exc).__name__}): built-in constants"})
    if write_cache and table.source == "tables":
        raw = table.to_json()
        raw["signature"] = signature
        raw["built"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = cache.with_suffix(cache.suffix + ".tmp")
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(raw, indent=1))
            tmp.replace(cache)
        except OSError:
            pass
    return table


# --- providers ---------------------------------------------------------------------------------------

class CropAverageProvider:
    """Bare days from the crop (and previous-crop) averages."""

    name = "crop_average"

    def __init__(self, table: BareDayTable | None = None):
        self.table = table if table is not None else builtin_table()

    def bare_days(self, crop: str, prev_crop: str | None, irrigated: bool) -> float:
        return self.table.bare_days(crop, prev_crop, irrigated)

    def rotation_shift(self, rotation: Sequence[str], irrigated: bool) -> float:
        return 0.0

    def describe(self) -> dict:
        return {"name": self.name, "table": self.table.source, "adjustments": [],
                "note": _table_note(self.table)}


def _table_note(table: BareDayTable) -> str:
    if table.source == "builtin":
        return "Bare days per crop are built-in county averages (the soil table is not available)."
    n = table.info.get("field_years")
    years = table.info.get("years") or ["?", "?"]
    return (f"Bare days per crop are county averages from {n:,} satellite field-years "
            f"({years[0]}-{years[1]}), compared within the same year.")


def plan_rotation_type(rotation: Sequence[str]) -> str:
    """Phase 2B's rotation type of a plan, read from the cycle run for twelve years (three rounds of a
    4-year cycle, four of a 3-year one)."""
    from pipeline.causal.rotations import rotation_type

    history = {str(2001 + i): rotation[i % len(rotation)] for i in range(12)}
    return rotation_type(history, 2001, 2012)


def usable_comparisons(effects: dict, min_pairs: int = config.EFFECTS_MIN_PAIRS) -> tuple[dict, list]:
    """({practice: [(type a, type b, gap in days, pairs)]}, [reasons the file as a whole is not usable])."""
    out = {p: [] for p in PRACTICES}
    if not isinstance(effects, dict):
        return out, ["effects.json is not a JSON object"]
    if effects.get("is_fake"):
        return out, ["effects.json was computed from made-up soil values (is_fake)"]
    if (effects.get("outcome") or {}).get("column") != "bare_days":
        return out, ["effects.json is not about bare_days"]
    failed_placebo = {p.get("comparison") for p in (effects.get("placebos") or {}).get("fake_switch_years") or []
                      if p.get("ci_excludes_zero")}
    for row in effects.get("effects") or []:
        try:
            if row.get("group_kind") != "irrigation" or row.get("group") not in PRACTICES:
                continue
            beyond = row.get("beyond_crops") or {}
            if int(row.get("n_pairs") or 0) < min_pairs or not beyond.get("ci_excludes_zero"):
                continue
            if row.get("comparison", f"{row['a']} vs {row['b']}") in failed_placebo:
                continue
            out[row["group"]].append((row["a"], row["b"], float(beyond["effect"]), int(row["n_pairs"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out, []


def type_shifts(comparisons: list) -> dict:
    """{rotation type: shift in days} from [(a, b, gap, pairs)]: least squares on shift_a - shift_b = gap,
    smallest total shift (one comparison alone gives +gap/2 and -gap/2)."""
    if not comparisons:
        return {}
    import numpy as np

    types = sorted({t for a, b, _, _ in comparisons for t in (a, b)})
    index = {t: i for i, t in enumerate(types)}
    A = np.zeros((len(comparisons), len(types)))
    rhs = np.zeros(len(comparisons))
    for i, (a, b, gap, _) in enumerate(comparisons):
        A[i, index[a]], A[i, index[b]], rhs[i] = 1.0, -1.0, gap
    solution = np.linalg.lstsq(A, rhs, rcond=None)[0]
    return {t: round(float(solution[index[t]]), 2) for t in types}


class EffectsProvider:
    """Crop averages plus the rotation-type shifts Phase 2B measured beyond the crops grown."""

    name = "effects"

    def __init__(self, base: CropAverageProvider | None = None, effects: dict | None = None,
                 problem: str | None = None):
        self.base = base if base is not None else CropAverageProvider()
        self.table = self.base.table
        self.problem = problem
        self.comparisons, reasons = usable_comparisons(effects) if effects is not None else ({p: [] for p in PRACTICES}, [])
        if reasons and not self.problem:
            self.problem = reasons[0]
        self.shifts = {p: type_shifts(self.comparisons[p]) for p in PRACTICES}

    @classmethod
    def from_file(cls, base: CropAverageProvider, path: str | Path) -> "EffectsProvider":
        path = Path(path)
        if not path.is_file():
            return cls(base, None, problem="effects.json is missing")
        try:
            return cls(base, json.loads(path.read_text()))
        except (OSError, ValueError):
            return cls(base, None, problem="effects.json could not be read")

    def bare_days(self, crop: str, prev_crop: str | None, irrigated: bool) -> float:
        return self.base.bare_days(crop, prev_crop, irrigated)

    def rotation_shift(self, rotation: Sequence[str], irrigated: bool) -> float:
        shifts = self.shifts[_practice(irrigated)]
        if not shifts:
            return 0.0
        return float(shifts.get(plan_rotation_type(rotation), 0.0))

    def describe(self) -> dict:
        adjustments = [
            {"practice": p, "a": a, "b": b, "gap_days": round(gap, 1), "pairs": n,
             "shift_a": self.shifts[p].get(a, 0.0), "shift_b": self.shifts[p].get(b, 0.0)}
            for p in PRACTICES for a, b, gap, n in self.comparisons[p]
        ]
        if adjustments:
            note = _table_note(self.table) + " Shifted by the gaps measured between matched neighbouring fields " \
                "where one stands out: " + "; ".join(
                    f"{x['a']} vs {x['b']} ({x['practice']}): {x['gap_days']:+.1f} days" for x in adjustments) + "."
        else:
            why = self.problem or "no neighbour comparison stands out beyond what the crops explain"
            note = _table_note(self.table) + f" No neighbour-comparison shift applied ({why})."
        return {"name": self.name, "table": self.table.source, "adjustments": adjustments, "note": note}


def plan_bare_days(provider: SoilEffectProvider, rotation: Sequence[str], irrigated: bool,
                   first_prev: str | None = None) -> float:
    """Mean bare days a year over the cycle (each year after the year before it; year 1 after year 4),
    plus the provider's rotation-type shift when it has one. Never below zero.

    `first_prev`: what really grew before year 1 (a field's own last four years are not a cycle).
    """
    n = len(rotation)
    prev = [first_prev if (i == 0 and first_prev is not None) else rotation[i - 1] for i in range(n)]
    days = sum(provider.bare_days(rotation[i], prev[i], irrigated) for i in range(n)) / n
    shift = getattr(provider, "rotation_shift", None)
    if shift is not None:
        days += shift(rotation, irrigated)
    return max(0.0, days)


def load_provider(settings: config.Settings | None = None, *, write_cache: bool = False) -> SoilEffectProvider:
    """The provider the settings ask for (`SOIL_EFFECT_SOURCE`)."""
    settings = settings or config.settings()
    base = CropAverageProvider(load_table(settings, write_cache=write_cache))
    if settings.soil_effect_source == "effects":
        return EffectsProvider.from_file(base, settings.effects_path)
    return base


# --- a table a human can check -----------------------------------------------------------------------

def format_table(table: BareDayTable) -> str:
    lines = [f"Bare days a year by crop (source: {table.source}; lower is better)", ""]
    lines.append(f"{'crop':<14}{'irrigated':>10}{'n':>8}{'dryland':>10}{'n':>8}")
    for crop in CROPS:
        cells = []
        for practice in PRACTICES:
            n = (table.n_crop.get(practice) or {}).get(crop)
            cells.append(f"{table.by_crop[practice][crop]:>10.1f}{(str(n) if n is not None else '-'):>8}")
        lines.append(f"{crop:<14}" + "".join(cells))
    for practice in PRACTICES:
        if not any(table.by_prev.get(practice, {}).get(c) for c in CROPS):
            continue
        lines += ["", f"{practice}: bare days by (previous crop -> crop), shrunk; field-years in brackets",
                  f"{'crop / after':<14}" + "".join(f"{p[:12]:>18}" for p in CROPS)]
        for crop in CROPS:
            row = f"{crop:<14}"
            for prev in CROPS:
                value = table.by_prev[practice][crop].get(prev)
                n = ((table.n_prev.get(practice) or {}).get(crop) or {}).get(prev, 0)
                row += f"{(f'{value:.1f} [{n}]' if value is not None else '-'):>18}"
            lines.append(row)
    if table.info.get("year_shift_days"):
        lines += ["", "year shift removed (days): " + ", ".join(f"{y} {s:+.1f}" for y, s in table.info["year_shift_days"].items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build the planner's bare-day table and write its cache.")
    parser.add_argument("--no-write", action="store_true", help="do not write the cache file")
    parser.add_argument("--report", default=str(config.DATA_DIR / "logs" / "phase3" / "bare_days_table.txt"))
    args = parser.parse_args(argv)
    settings = config.settings()
    table = load_table(settings, write_cache=not args.no_write, use_cache=False)
    text = format_table(table)
    if table.source != "builtin" and not args.no_write:
        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text + "\n")
        print(f"full table: {report}")
        print(f"cache: {settings.soil_cache_path}")
    for line in text.splitlines()[:9]:
        print(line)
    order = {p: sorted(CROPS, key=lambda c: table.by_crop[p][c]) for p in PRACTICES}
    for p in PRACTICES:
        print(f"{p}: least bare {order[p][0]}, barest {order[p][-1]}")
    provider = EffectsProvider.from_file(CropAverageProvider(table), settings.effects_path)
    print("effects provider:", provider.describe()["note"].split(". ")[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
