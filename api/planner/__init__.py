"""Rotation planners.

`legacy.py`   the original rule-based planner (Phase 1): ten hand-picked rotations scored from a
              relative crop table. Still the fallback when the climate tables are not built.
(Phase 3 adds the data-driven planner next to it; see `smart.py` once it exists.)

THE SEAM is unchanged. The API and the tests import these from `api.planner`:

    FieldInput, Plan, Scores, Planner      small types (what goes in, what comes out)
    rank_plans(field) -> list[Plan]        the original planner

Every other name of the original module (CROPS, ROTATIONS, score_rotation, ...) is still reachable
as `api.planner.<name>`; it is looked up in `legacy.py` on first use.
"""
from __future__ import annotations

from api.planner.legacy import FieldInput, Plan, Planner, Scores, rank_plans

__all__ = ["FieldInput", "Plan", "Planner", "Scores", "rank_plans"]


def __getattr__(name: str):
    import importlib

    if name.startswith("__"):
        raise AttributeError(name)
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise
    try:
        return getattr(importlib.import_module(f"{__name__}.legacy"), name)
    except AttributeError:
        raise AttributeError(f"module 'api.planner' has no attribute {name!r}") from None
