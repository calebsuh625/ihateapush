"""What the planner knows about one field beyond the old `FieldInput`: irrigation and soil erodibility.

These reach the planner as their own small object (`FieldTraits`), so the old `FieldInput` and the
old planner stay as they are. They come from `data/field_traits.parquet` (contract:
pipeline/field_traits_schema.py). When that file is missing, or the field is not in it (the fake
fields), irrigation is guessed from the crop history alone and erodibility is unknown, which the
erosion index treats as the county's commonest soil.

Irrigation from crop history alone (`guess_irrigation`): irrigated when the share of years in corn,
soybeans or alfalfa is at least 0.25 above the share of years in fallow. On the real fields this
agrees with field_traits.parquet (which also uses field shape and measured water use) for 94% of
fields. No history at all counts as dryland (the larger group, and the cautious plans).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

THIRSTY_CROPS = ("corn", "soybeans", "alfalfa")
THIRSTY_MINUS_FALLOW = 0.25


@dataclass(frozen=True)
class FieldTraits:
    irrigation: str | None = None          # "irrigated" / "dryland"; None = not known, guess from history
    irrigated_prob: float | None = None
    irrigation_basis: str | None = None    # short plain text: what the guess rests on
    weg: str | None = None                 # SSURGO wind erodibility group ("1" .. "8", "4L")
    wei: float | None = None               # SSURGO wind erodibility index, tons/acre/year


def _clean(value):
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    return value


def guess_irrigation(crop_history) -> tuple[bool, str]:
    """(irrigated?, what the guess rests on) from a {year: crop} history alone."""
    crops = [str(c).strip().lower() for c in (crop_history or {}).values() if isinstance(c, str) and c.strip()]
    n = len(crops)
    if n == 0:
        return False, "no crop history on record; assumed dryland"
    thirsty = sum(1 for c in crops if c in THIRSTY_CROPS)
    fallow = sum(1 for c in crops if c == "fallow")
    irrigated = (thirsty - fallow) / n >= THIRSTY_MINUS_FALLOW
    basis = f"crop history: corn/soybeans/alfalfa in {thirsty} of {n} years, fallow in {fallow}"
    return irrigated, basis


def load_traits(path: str | Path) -> dict[str, FieldTraits]:
    """{field_id: FieldTraits} from field_traits.parquet; {} when the file is missing or unreadable."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        import pandas as pd

        df = pd.read_parquet(path)
        out = {}
        columns = {c: (df[c].tolist() if c in df.columns else [None] * len(df))
                   for c in ("irrigation", "irrigated_prob", "irrigation_basis", "weg", "wei")}
        for i, field_id in enumerate(df["field_id"].tolist()):
            irrigation = _clean(columns["irrigation"][i])
            wei = _clean(columns["wei"][i])
            prob = _clean(columns["irrigated_prob"][i])
            out[str(field_id)] = FieldTraits(
                irrigation=irrigation if irrigation in ("irrigated", "dryland") else None,
                irrigated_prob=None if prob is None else float(prob),
                irrigation_basis=_clean(columns["irrigation_basis"][i]),
                weg=None if _clean(columns["weg"][i]) is None else str(columns["weg"][i]),
                wei=None if wei is None else float(wei),
            )
        return out
    except Exception:   # a broken side file must not take the planner down: fall back to guessing
        return {}
