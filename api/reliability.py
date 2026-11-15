"""Phase 4, item 8: reliability helpers kept apart from api/main.py.

* `install_plain_errors(app)`: a query value FastAPI cannot read ("horizon=abc") gets one plain sentence in
  `detail`, not the default list of machine-readable errors. The status stays 422.
* `warm_up(app)` / `start_warm_up(app)`: the first-use work done once at startup on a background thread.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("uvicorn.error")

_PARSE_WORDS = {
    "int_parsing": "a whole number", "int_from_float": "a whole number", "float_parsing": "a number",
    "bool_parsing": "true or false", "int_type": "a whole number", "float_type": "a number",
}


def plain_validation_message(exc: RequestValidationError) -> str:
    """One plain sentence for a request FastAPI could not read (Phase 4, item 8). By default the answer is a
    list of machine-readable errors; a farmer or the advisor should get a sentence, like every other error."""
    errors = exc.errors()
    if not errors:
        return "That request could not be read."
    first = errors[0]
    where = first.get("loc", ())
    name = str(where[-1]) if where else "value"
    if first.get("type") == "missing":
        return f"The {name} is missing."
    wanted = _PARSE_WORDS.get(str(first.get("type")))
    if wanted:
        return f"The {name} must be {wanted}."
    return f"The {name} is not valid."


def install_plain_errors(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _plain_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": plain_validation_message(exc)})



def warm_up(app: FastAPI) -> dict[str, float]:
    """Phase 4, item 8: do the first-use work now, so the first farmer's click is as quick as the hundredth.

    Measured before this: the first /plans took about 0.5 s (the planner's caches fill on first use), the first
    /neighbors about 60 ms and the first /layers about 50 ms; afterwards every answer takes a few milliseconds.
    Runs on a background thread after startup, answers nothing, and never raises: a step that fails is
    skipped (the request that needs it will meet the same problem and answer it properly). Returns the
    seconds each step took (for the log and the tests). `API_WARMUP=0` turns it off.
    """
    took: dict[str, float] = {}

    def step(name: str, work) -> None:
        began = time.perf_counter()
        try:
            work()
        except Exception as exc:  # a missing table is a normal state here, not an error
            log.info("warm-up step %s skipped: %s: %s", name, type(exc).__name__, exc)
            return
        took[name] = round(time.perf_counter() - began, 3)

    from api import main  # late: api.main imports this module
    from api.causal import load_causal
    from api.layers import LAYER_NAMES
    from api.planner import config as planner_config
    from api.planner import smart
    from api.planner.traits import FieldTraits

    store = getattr(app.state, "store", None)
    records = list(store.records.values())[:1] if store is not None else []
    if records and getattr(app.state, "default_planner", False) and getattr(app.state, "climate_ready", False):
        traits = FieldTraits() if store.is_fake else None
        for irrigated in (False, True):  # both traits paths, so the water-outlook code is warm too
            step(f"plans_{'irrigated' if irrigated else 'dryland'}", lambda irrigated=irrigated: main._with_water_outlook(
                SimpleNamespace(app=app), records[0],
                smart.rank_plans_smart(records[0].planner_input(), weights=dict(planner_config.DEFAULT_WEIGHTS),
                                       irrigated=irrigated, traits=traits, engine=smart.get_engine()).to_dict(),
                smart.get_engine()))
    step("neighbors_store", lambda: load_causal(SimpleNamespace(app=app)))
    layers_service = getattr(app.state, "layers", None)
    if layers_service is not None:
        step("layers_listing", layers_service.listing)
        for name in LAYER_NAMES:
            step(f"layer_{name}", lambda name=name: layers_service.packed(name, None))
    return took


def start_warm_up(app: FastAPI) -> None:
    if os.environ.get("API_WARMUP", "1").strip().lower() in ("0", "false", "no", "off"):
        return

    def run() -> None:
        began = time.perf_counter()
        took = warm_up(app)
        log.info("Warm-up done in %.2fs: %s", time.perf_counter() - began, ", ".join(f"{k} {v:.2f}s" for k, v in took.items()))

    threading.Thread(target=run, name="warm-up", daemon=True).start()
