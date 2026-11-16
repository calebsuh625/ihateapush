"""Rotation effects on yield (Phase 5, item A): what the crop grown the year before does to a crop's yield.

    from api.planner import rotation
    fx = rotation.load_effects(path)                    # the tracked rotation_effects.json
    table = fx.multipliers("mid", mix)                  # [practice, crop, previous crop] yield multipliers

Why
---
Before this, the previous crop changed a yield only through the soil water it left (water.py). So corn
after corn yielded the same as corn after soybeans, and continuous irrigated corn ranked second of 106.
`rotation_effects.json` holds the sourced yield drag of a crop after itself (corn, sorghum, soybeans,
wheat); every entry has a multiplier, a range, a source and a status, and a pair with no sourced effect
is simply not listed and stays at 1.

Levelled against the county's own mix
-------------------------------------
Each multiplier in the file is relative to a ROTATED field (previous crop a different crop = 1.0). Our
normal yields are fitted to county yields, and those already contain the county's mix of previous crops:
57% of irrigated corn in Finney follows corn. Applying 0.91 to corn after corn on top of that would
count the drag twice and leave corn after soybeans at plain 1.0. So each crop's multipliers are divided
by the mix-weighted mean multiplier of that crop and practice (`level`), which keeps the county's average
yield where the calibration put it, pushes corn after corn below it and rotated corn above it. The mix
comes from the soil table's field-year counts (`BareDayTable.n_prev`); with no mix (or fewer than
`MIN_MIX_FIELD_YEARS` field-years for a crop and practice) the file's numbers are used as they are.

Three variants share one set of numbers: "mid" (the multipliers, what every answer uses), "strong" (the
`low` end of every range: the largest drags) and "weak" (the `high` end). The planner shows a plan's
low-high spread over the climate models AND these variants, so the range of the effect widens the spread
it already has. Ranking, scores and every central figure use "mid".

`ROTATION_EFFECTS=off` turns all of this off: every multiplier is 1 and the tables are the ones the
planner had before Phase 5.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy as np

from api.planner.crops import CROPS, PRACTICES

EFFECTS_PATH = Path(__file__).with_name("rotation_effects.json")
VARIANTS = ("mid", "strong", "weak")
MIN_MIX_FIELD_YEARS = 50          # fewer field-years of a crop and practice than this: no county mix to level against
STATUSES = ("verified", "rough, unverified")

LIMIT_SENTENCE_ON = (
    "A crop grown after itself (corn after corn, sorghum after sorghum, soybeans after soybeans, wheat after wheat) "
    "yields less than after another crop, by the amounts K-State and other Great Plains trials measured, set against "
    "the county's own mix of previous crops; no nitrogen credit for soybeans or alfalfa is counted, because it saves "
    "fertilizer, not bushels."
)
LIMIT_SENTENCE_OFF = (
    "Yields do not change with the crop grown before, apart from the soil water it leaves behind (rotation effects "
    "are switched off)."
)


class RotationEffectsError(ValueError):
    """The effects file cannot be used (never silently ignored)."""


@dataclass(frozen=True)
class Effect:
    id: str
    crop: str
    after: str
    practice: str            # "all", "irrigated" or "dryland"
    multiplier: float
    low: float               # the stronger end of the range
    high: float              # the weaker end
    status: str
    basis: str
    sources: tuple

    def words(self) -> str:
        """One plain sentence: how much less this crop yields after itself, with the range the trials gave."""
        pct = lambda x: int(round((1.0 - x) * 100))      # noqa: E731
        name = f"{self.crop} after {self.after}"
        text = f"{name[0].upper()}{name[1:]} is counted at about {pct(self.multiplier)}% lower yield than after another crop"
        if self.low != self.high:
            text += f" ({pct(self.high)}% to {pct(self.low)}% in the trials)"
        return text + "."

    def covers(self, practice: str) -> bool:
        return self.practice in ("all", practice)

    def value(self, variant: str) -> float:
        return {"mid": self.multiplier, "strong": self.low, "weak": self.high}[variant]

    def to_dict(self) -> dict:
        return {"id": self.id, "crop": self.crop, "after": self.after, "practice": self.practice,
                "multiplier": self.multiplier, "low": self.low, "high": self.high, "status": self.status,
                "basis": self.basis, "sources": [dict(s) for s in self.sources]}


@dataclass(frozen=True)
class RotationEffects:
    """The effects in use. `enabled` False = off: every table is all ones."""

    enabled: bool = False
    effects: tuple = ()
    not_applied: tuple = ()
    path: str = ""
    version: int = 0
    problem: str = ""          # set when the file could not be read (then nothing applies)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.effects)

    def raw(self, variant: str) -> np.ndarray:
        """[practice, crop, previous crop] multipliers as written in the file (1 where nothing is listed)."""
        out = np.ones((len(PRACTICES), len(CROPS), len(CROPS)))
        if not self.active:
            return out
        for e in self.effects:
            for p, practice in enumerate(PRACTICES):
                if e.covers(practice):
                    out[p, CROPS.index(e.crop), CROPS.index(e.after)] = e.value(variant)
        return out

    def multipliers(self, variant: str = "mid", mix: Mapping | None = None) -> np.ndarray:
        """[practice, crop, previous crop]: the yield multiplier to apply, levelled against the county mix."""
        return level(self.raw(variant), mix)

    def ranges_differ(self) -> bool:
        return self.active and any(e.low != e.multiplier or e.high != e.multiplier for e in self.effects)

    def describe(self, full: bool = False) -> dict:
        """The `settings.rotation_effects` block of every plan answer; `full` adds the basis and sources
        (GET /rotation-effects)."""
        out = {"setting": "on" if self.enabled else "off", "applied": self.active,
               "file": Path(self.path).name if self.path else None, "effects": []}
        if self.active:
            for e in self.effects:
                row = {"id": e.id, "crop": e.crop, "after": e.after, "practice": e.practice,
                       "multiplier": e.multiplier, "low": e.low, "high": e.high, "status": e.status,
                       "sentence": e.words()}
                if full:
                    row.update(basis=e.basis, sources=[dict(s) for s in e.sources])
                out["effects"].append(row)
        if full:
            out["not_applied"] = [dict(x) for x in self.not_applied]
        if self.problem:
            out["problem"] = self.problem
        return out

    def pairs_in(self, cycle, practice: str, first_prev: str | None = None) -> list:
        """The effects a plan's years run into: each (crop, previous crop) of the cycle that has one."""
        if not self.active:
            return []
        seq = list(cycle)
        used, seen = [], set()
        for i, crop in enumerate(seq):
            before = first_prev if (i == 0 and first_prev is not None) else seq[i - 1]
            for e in self.effects:
                if e.crop == crop and e.after == before and e.covers(practice) and e.id not in seen:
                    seen.add(e.id)
                    used.append(e)
        return used


OFF = RotationEffects(enabled=False)


def level(raw: np.ndarray, mix: Mapping | None) -> np.ndarray:
    """Divide each crop's multipliers by the mix-weighted mean for that crop and practice.

    mix: {practice: {crop: {previous crop: field-years}}}. Crops with no usable mix keep the file's numbers."""
    out = raw.copy()
    if not mix:
        return out
    for p, practice in enumerate(PRACTICES):
        for c, crop in enumerate(CROPS):
            cells = ((mix.get(practice) or {}).get(crop) or {})
            weights = np.array([float(cells.get(prev, 0) or 0) for prev in CROPS])
            if weights.sum() < MIN_MIX_FIELD_YEARS or np.allclose(raw[p, c], 1.0):
                continue
            base = float((weights * raw[p, c]).sum() / weights.sum())
            if base > 0 and math.isfinite(base):
                out[p, c] = raw[p, c] / base
    return out


def _number(entry: dict, key: str) -> float:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RotationEffectsError(f"{entry.get('id', '?')}: {key} = {value!r}: expected a positive number")
    return float(value)


def parse(raw: dict, path: str = "") -> RotationEffects:
    """The effects from a loaded file. Every listed effect needs a source and a status, and low <= multiplier <= high."""
    if not isinstance(raw, dict) or not isinstance(raw.get("effects"), list):
        raise RotationEffectsError("no 'effects' list in the file")
    effects, ids = [], set()
    for entry in raw["effects"]:
        if not isinstance(entry, dict):
            raise RotationEffectsError("an effect is not an object")
        name = str(entry.get("id", ""))
        if not name or name in ids:
            raise RotationEffectsError(f"effect id {name!r} is missing or repeated")
        ids.add(name)
        crop, after = entry.get("crop"), entry.get("after")
        if crop not in CROPS or crop == "fallow" or after not in CROPS:
            raise RotationEffectsError(f"{name}: crop {crop!r} after {after!r}: expected two of {CROPS}")
        practice = entry.get("practice", "all")
        if practice not in ("all", *PRACTICES):
            raise RotationEffectsError(f"{name}: practice {practice!r}")
        mid, low, high = _number(entry, "multiplier"), _number(entry, "low"), _number(entry, "high")
        if not low <= mid <= high:
            raise RotationEffectsError(f"{name}: expected low <= multiplier <= high, got {low}, {mid}, {high}")
        if entry.get("status") not in STATUSES:
            raise RotationEffectsError(f"{name}: status {entry.get('status')!r}: expected one of {STATUSES}")
        sources = tuple(dict(s) for s in (entry.get("sources") or []) if isinstance(s, dict) and s.get("name"))
        if not sources:
            raise RotationEffectsError(f"{name}: every effect needs a named source")
        effects.append(Effect(name, crop, after, practice, mid, low, high, entry["status"],
                              str(entry.get("basis", "")), sources))
    return RotationEffects(enabled=True, effects=tuple(effects), not_applied=tuple(raw.get("not_applied") or ()),
                           path=str(path), version=int(raw.get("version", 0) or 0))


def load_effects(path: str | Path | None = None, enabled: bool = True) -> RotationEffects:
    """The effects file as a `RotationEffects`; switched off when `enabled` is False. A missing or broken
    file gives an effect-free object with `problem` set (the engine then scores as it did before)."""
    p = Path(path) if path is not None else EFFECTS_PATH
    if not enabled:
        return RotationEffects(enabled=False, path=str(p))
    try:
        return parse(json.loads(p.read_text()), str(p))
    except (OSError, ValueError) as exc:
        return RotationEffects(enabled=True, path=str(p), problem=f"{type(exc).__name__}: {exc}")


def mix_from_soil(provider) -> dict | None:
    """The county's previous-crop mix, {practice: {crop: {previous: field-years}}}, from a soil-effect provider
    (a `BareDayTable` or something that wraps one); None when it carries none."""
    seen = set()
    node = provider
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        table = getattr(node, "table", None)
        n_prev = getattr(table, "n_prev", None) or getattr(node, "n_prev", None)
        if n_prev:
            return n_prev
        node = getattr(node, "base", None) or (table if table is not node else None)
    return None
