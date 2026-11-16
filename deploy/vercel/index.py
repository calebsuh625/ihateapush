"""Vercel entry point: the API for every county the site offers, under /api.

    /api/<path>              Finney County (the default), served in this process by api/main.py
    /api/r/<fips>/<path>     another county, served by its own API process (REGION=<fips>), started on
                             first use and kept while there is room (MAX_COUNTIES at a time)
    /api/regions             the counties on offer (deploy/vercel/regions.json, from make_regions.py)

The app's routes have no /api prefix (local dev: Vite strips it), so it is stripped here too; the
same file runs locally behind Vite (`cd deploy/vercel && ../../.venv/bin/python -m uvicorn index:app
--port 8010`). A county's fields.parquet and field_traits.parquet are not in the function bundle
(that would push it past Vercel's size limit): they are published as static files under
/data/regions/<fips>/ and copied to /tmp the first time the county is opened. Locally the files in
data/regions/ are used as they are. Everything else a county needs it borrows from Finney County, as
pipeline/region.py describes.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # Vercel does not put the entry's folder on the path

import runtime  # noqa: E402

runtime.setup()

import httpx  # noqa: E402
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402

from api.main import app as finney  # noqa: E402
from pipeline.region import data_dir_for  # noqa: E402

MAX_COUNTIES = int(os.environ.get("MAX_COUNTY_PROCESSES", "3"))
START_TIMEOUT_S = 90
REGIONS = json.loads((runtime.HERE / "regions.json").read_text())
OFFERED = {r["fips"]: r for r in REGIONS["regions"]}
NO_FARM = (
    "The whole-farm planner is not available on the hosted site: its solver (Google OR-Tools) is too "
    "large for the host. Run the app locally to use it."
)
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length", "host",
              "accept-encoding", "te", "trailer", "upgrade"}

_finney_lock = asyncio.Lock()
_finney_lifespan = None


async def _ensure_finney() -> None:
    """Run the app's startup (load the fields etc.) if the platform did not send ASGI lifespan."""
    global _finney_lifespan
    if hasattr(finney.state, "store"):
        return
    async with _finney_lock:
        if not hasattr(finney.state, "store"):
            _finney_lifespan = finney.router.lifespan_context(finney)
            await _finney_lifespan.__aenter__()


# --- one API process per county ------------------------------------------------------------------

class County:
    def __init__(self, fips: str):
        self.fips = fips
        self.proc: asyncio.subprocess.Process | None = None
        self.port = 0
        self.used = 0.0
        self.lock = asyncio.Lock()
        self.log: Path | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def stop(self) -> None:
        if self.alive:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


_counties: dict[str, County] = {}
_client: httpx.AsyncClient | None = None


def _client_() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5))
    return _client


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _county_files(fips: str, origin: str) -> dict[str, str]:
    """Env vars that point the county's API at its files: local data/regions/, else copies in /tmp."""
    local = data_dir_for(fips)
    if (local / "fields.parquet").is_file():
        return {}
    dest = runtime.TMP / "regions" / fips
    dest.mkdir(parents=True, exist_ok=True)
    env = {}
    for name, keys in (("fields.parquet", ("FIELDS_PATH", "PLANNER_FIELDS_PATH")),
                       ("field_traits.parquet", ("FIELD_TRAITS_PATH",))):
        path = dest / name
        if not path.is_file():
            r = await _client_().get(f"{origin}/data/regions/{fips}/{name}")
            if r.status_code != 200:
                if name == "fields.parquet":
                    raise RuntimeError(f"could not fetch {name} for {fips}: HTTP {r.status_code}")
                continue  # traits are optional: the API guesses irrigation from the crop history
            tmp = path.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(path)
        for k in keys:
            env[k] = str(path)
    return env


async def _start(c: County, origin: str) -> None:
    # make room: stop the least recently used county
    running = [x for x in _counties.values() if x.alive and x is not c]
    while len(running) >= MAX_COUNTIES:
        oldest = min(running, key=lambda x: x.used)
        await oldest.stop()
        running.remove(oldest)
    tmp = runtime.TMP / "county" / c.fips
    tmp.mkdir(parents=True, exist_ok=True)
    # Finney's own file overrides (if any) never reach another county
    base = {k: v for k, v in os.environ.items() if k not in ("FIELDS_PATH", "PLANNER_FIELDS_PATH", "FIELD_TRAITS_PATH")}
    env = {
        **base,
        "REGION": c.fips,
        # the platform adds its installed packages to sys.path at run time, so hand the whole path down
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(
            [str(runtime.HERE), str(runtime.HERE / "_vendor"), str(runtime.ROOT)] + [p for p in sys.path if p and os.path.isdir(p)])),
        "PLANNER_SOIL_CACHE_PATH": str(tmp / "planner_bare_days.json"),
        "LAYERS_CACHE_DIR": str(tmp / "layers_cache"),
        **(await _county_files(c.fips, origin)),
    }
    c.port = _free_port()
    c.log = tmp / "api.log"
    c.proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "region_app:app", "--host", "127.0.0.1", "--port", str(c.port),
        "--log-level", "warning", cwd=str(runtime.HERE), env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=open(c.log, "wb"),
    )
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if not c.alive:
            print(f"county {c.fips} API exited while starting:\n" + _tail(c.log), file=sys.stderr, flush=True)
            raise RuntimeError(f"the API for county {c.fips} stopped while starting")
        try:
            r = await _client_().get(f"http://127.0.0.1:{c.port}/health", timeout=2)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.25)
    await c.stop()
    raise RuntimeError(f"the API for county {c.fips} did not start in {START_TIMEOUT_S} s")


def _tail(path: Path, n: int = 3000) -> str:
    try:
        return path.read_bytes()[-n:].decode("utf-8", "replace")
    except OSError:
        return "(no log)"


async def _county(fips: str, origin: str) -> County:
    c = _counties.setdefault(fips, County(fips))
    async with c.lock:
        if not c.alive:
            await _start(c, origin)
    c.used = time.monotonic()
    return c


def _origin(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}"


async def _proxy_app(scope, receive, send) -> None:
    request = Request(scope, receive)
    _, _, fips, *rest = scope["path"].split("/", 3)  # "", "r", "<fips>", "<path>"
    path = "/" + (rest[0] if rest else "")
    if fips not in OFFERED or fips == REGIONS["default"]:
        response = JSONResponse({"detail": f"County {fips} is not on this site."}, status_code=404)
        await response(scope, receive, send)
        return
    if path.rstrip("/") == "/farm/plan" and not runtime.has_ortools():
        await JSONResponse({"detail": NO_FARM}, status_code=503)(scope, receive, send)
        return
    try:
        c = await _county(fips, _origin(request))
    except Exception as exc:  # noqa: BLE001
        label = OFFERED[fips]["label"]
        await JSONResponse({"detail": f"{label} could not be opened right now ({exc}). Try again in a moment."},
                           status_code=503)(scope, receive, send)
        return
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    url = f"http://127.0.0.1:{c.port}{path}"
    if scope.get("query_string"):
        url += "?" + scope["query_string"].decode("latin-1")
    try:
        r = await _client_().request(request.method, url, headers=headers, content=await request.body())
    except httpx.HTTPError as exc:
        await c.stop()
        await JSONResponse({"detail": f"No answer from the API for {OFFERED[fips]['label']} ({type(exc).__name__}). "
                                      "Try again."}, status_code=502)(scope, receive, send)
        return
    out = {k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP}
    await Response(r.content, status_code=r.status_code, headers=out)(scope, receive, send)


proxy = GZipMiddleware(_proxy_app, minimum_size=1024, compresslevel=6)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await finney(scope, receive, send)
        return
    path = scope.get("path", "")
    if path == "/api" or path.startswith("/api/"):
        path = path[4:] or "/"
        scope = dict(scope, path=path, raw_path=path.encode())
    if path.startswith("/r/"):
        await proxy(scope, receive, send)
        return
    if path.rstrip("/") == "/regions":
        await JSONResponse({"current": REGIONS["default"], "regions": REGIONS["regions"]})(scope, receive, send)
        return
    if path.rstrip("/") == "/farm/plan" and not runtime.has_ortools():
        await JSONResponse({"detail": NO_FARM}, status_code=503)(scope, receive, send)
        return
    await _ensure_finney()
    await finney(scope, receive, send)
