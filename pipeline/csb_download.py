"""Resumable, ranged, multi-connection download for the multi-GB national CSB zips.

Why not `pipeline.common.download`: on the NASS web server a plain (non-Range) GET of
the 3.5-4.3 GB CSB archives stalls without ever sending a byte (observed 2026-11-14),
while HTTP Range requests are answered at once. This helper therefore fetches the file
as fixed-size byte ranges over a few parallel connections, writes each range at its
offset in `<dest>.part`, and remembers finished ranges in `<dest>.part.json`, so an
interrupted download resumes where it stopped. `dest` only appears once every byte is
there (and, for zips, once the central directory can be read).

    python pipeline/csb_download.py URL DEST [--workers 6] [--chunk-mb 16]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # allow `python pipeline/csb_download.py`
    sys.path.insert(0, str(_ROOT))

from pipeline.common import ROOT, USER_AGENT, download, log  # noqa: E402

DEFAULT_WORKERS = 6
DEFAULT_CHUNK_MB = 16


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def remote_size(url: str, *, timeout: float = 60, retries: int = 4) -> tuple[int | None, bool]:
    """(Content-Length, accepts byte ranges?) from a HEAD request, following redirects."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.head(url, allow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            length = resp.headers.get("Content-Length")
            ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            return (int(length) if length and length.isdigit() else None), ranges
        except requests.RequestException as exc:
            last = exc
            if attempt < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"HEAD {url} failed after {retries} attempts: {last}")


def _fetch_range(session: requests.Session, url: str, start: int, end: int, timeout: float) -> bytes:
    """Bytes [start, end] inclusive. Raises IOError unless the server returns exactly that range."""
    headers = {"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}
    with session.get(url, headers=headers, timeout=timeout, stream=True) as resp:
        if resp.status_code != 206:
            raise IOError(f"expected HTTP 206 for bytes {start}-{end}, got {resp.status_code}")
        content_range = resp.headers.get("Content-Range", "")
        if not content_range.startswith(f"bytes {start}-{end}/"):
            raise IOError(f"unexpected Content-Range {content_range!r} for bytes {start}-{end}")
        data = b"".join(resp.iter_content(chunk_size=1 << 20))
    if len(data) != end - start + 1:
        raise IOError(f"short read for bytes {start}-{end}: got {len(data)}")
    return data


def ranged_download(
    url: str,
    dest: str | Path,
    *,
    workers: int = DEFAULT_WORKERS,
    chunk_mb: int = DEFAULT_CHUNK_MB,
    timeout: float = 120,
    retries: int = 6,
    quiet: bool = False,
) -> Path:
    """Download `url` to `dest` unless `dest` already exists (the cache). Returns `dest`.

    Falls back to `pipeline.common.download` if the server does not report a size or
    does not accept byte ranges.
    """
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        if not quiet:
            log(f"cached   {_rel(dest)}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    total, accepts_ranges = remote_size(url)
    if not total or not accepts_ranges:
        if not quiet:
            log(f"server gives no size / no byte ranges for {url}; using a plain download")
        return download(url, dest, timeout=timeout, quiet=quiet)

    chunk = chunk_mb << 20
    n_chunks = (total + chunk - 1) // chunk
    part = dest.with_name(dest.name + ".part")
    state_path = dest.with_name(dest.name + ".part.json")

    done: set[int] = set()
    if part.exists() and state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            if (
                state.get("url") == url
                and state.get("total") == total
                and state.get("chunk") == chunk
                and part.stat().st_size == total
            ):
                done = {int(i) for i in state.get("done", []) if 0 <= int(i) < n_chunks}
        except (ValueError, OSError):
            done = set()
    if not done:
        with open(part, "wb") as fh:  # sparse file of the final size
            fh.truncate(total)

    todo = [i for i in range(n_chunks) if i not in done]
    if not quiet:
        resumed = f", resuming at {100 * len(done) / n_chunks:.0f}%" if done else ""
        log(f"download {url} ({total / 1e6:,.1f} MB in {n_chunks} ranges x {workers} connections{resumed})")

    lock = threading.Lock()
    local = threading.local()
    fd = os.open(part, os.O_RDWR)
    progress = {"last_log": time.monotonic(), "last_save": time.monotonic(), "bytes": 0, "t0": time.monotonic()}

    def save_state() -> None:
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps({"url": url, "total": total, "chunk": chunk, "done": sorted(done)}))
        tmp.replace(state_path)

    def work(i: int) -> int:
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
        start = i * chunk
        end = min(start + chunk, total) - 1
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                data = _fetch_range(session, url, start, end, timeout)
                os.pwrite(fd, data, start)
                with lock:
                    done.add(i)
                    progress["bytes"] += len(data)
                    now = time.monotonic()
                    if now - progress["last_save"] > 5:
                        save_state()
                        progress["last_save"] = now
                    if not quiet and now - progress["last_log"] > 20:
                        rate = progress["bytes"] / max(now - progress["t0"], 1e-6) / 1e6
                        log(f"  {100 * len(done) / n_chunks:5.1f}%  ({rate:.1f} MB/s)")
                        progress["last_log"] = now
                return i
            except (requests.RequestException, IOError) as exc:
                last = exc
                local.session = session = requests.Session()
                if attempt < retries:
                    time.sleep(min(2**attempt, 30))
        raise RuntimeError(f"range {start}-{end} of {url} failed after {retries} attempts: {last}")

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(work, i) for i in todo]
            try:
                for fut in as_completed(futures):
                    fut.result()
            except BaseException:
                for fut in futures:
                    fut.cancel()
                raise
    finally:
        os.fsync(fd)
        os.close(fd)
        with lock:
            save_state()

    if len(done) != n_chunks or part.stat().st_size != total:
        raise RuntimeError(f"incomplete download of {url}: {len(done)}/{n_chunks} ranges")
    if dest.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(part) as zf:
                if not zf.namelist():
                    raise zipfile.BadZipFile("empty archive")
        except zipfile.BadZipFile as exc:
            part.unlink(missing_ok=True)  # do not let a later run "resume" a corrupt file
            state_path.unlink(missing_ok=True)
            raise RuntimeError(f"{url} downloaded but is not a readable zip: {exc}") from exc
    part.replace(dest)
    state_path.unlink(missing_ok=True)
    if not quiet:
        secs = time.monotonic() - progress["t0"]
        log(f"  done: {_rel(dest)} ({total / 1e6:,.1f} MB in {secs:.0f}s)")
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("dest")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--chunk-mb", type=int, default=DEFAULT_CHUNK_MB)
    args = ap.parse_args(argv)
    ranged_download(args.url, args.dest, workers=args.workers, chunk_mb=args.chunk_mb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
