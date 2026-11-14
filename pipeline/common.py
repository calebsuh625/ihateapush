"""Shared constants and the cached-download helper used by every pipeline step."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/<script>.py`
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"  # every download is cached here

# Region: Finney County, Kansas
STATE_ABBR = "KS"
STATE_FIPS = "20"
COUNTY_FIPS = "055"
FIPS = STATE_FIPS + COUNTY_FIPS  # "20055"
COUNTY_NAME = "Finney"

YEARS = list(range(2008, 2025))  # crop history years, inclusive
MIN_FIELD_HA = 5.0
EQUAL_AREA_CRS = "EPSG:5070"  # NAD83 / CONUS Albers — also the CDL's native CRS

USER_AGENT = "crop-rotation-tool/0.1 (NASA Space Apps project; python-requests)"

_T0 = time.monotonic()


def log(msg: str) -> None:
    """Progress line with seconds since start, flushed so it shows up under `make`."""
    print(f"[{time.monotonic() - _T0:7.1f}s] {msg}", flush=True)


def download(
    url: str,
    dest: str | Path,
    *,
    timeout: float = 120,
    retries: int = 4,
    headers: dict | None = None,
    quiet: bool = False,
) -> Path:
    """Download `url` to `dest` unless `dest` already exists (the cache). Returns `dest`.

    Writes to `<dest>.part` and renames at the end, so an interrupted run never leaves a
    truncated file that looks complete. A leftover `.part` is resumed with an HTTP Range
    request when the server supports it. Retries with exponential backoff.
    """
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        if not quiet:
            log(f"cached   {dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    base_headers = {"User-Agent": USER_AGENT, **(headers or {})}

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            have = part.stat().st_size if part.exists() else 0
            req_headers = dict(base_headers)
            if have:
                req_headers["Range"] = f"bytes={have}-"
            with requests.get(url, stream=True, timeout=timeout, headers=req_headers) as resp:
                if have and resp.status_code == 416:  # .part is already the whole file (or stale)
                    part.unlink()
                    continue
                resp.raise_for_status()
                resuming = have and resp.status_code == 206
                if not resuming:
                    have = 0
                expected = resp.headers.get("Content-Length")
                total = have + int(expected) if expected and expected.isdigit() else None
                if not quiet:
                    size = f"{total / 1e6:,.1f} MB" if total else "unknown size"
                    log(f"download {url} ({size}{', resuming' if resuming else ''})")
                with open(part, "ab" if resuming else "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                got = part.stat().st_size
                if total is not None and resp.headers.get("Content-Encoding") is None and got != total:
                    raise IOError(f"incomplete download: got {got} of {total} bytes")
            part.replace(dest)
            return dest
        except (requests.RequestException, IOError) as exc:
            last_error = exc
            if attempt < retries:
                wait = 2**attempt
                if not quiet:
                    log(f"  attempt {attempt}/{retries} failed ({exc}); retrying in {wait}s")
                time.sleep(wait)
    raise RuntimeError(f"could not download {url} after {retries} attempts: {last_error}")
