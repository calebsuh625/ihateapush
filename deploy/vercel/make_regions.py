"""Write deploy/vercel/regions.json: the counties the hosted site offers (Finney first).

Run before `vercel deploy`: .venv/bin/python deploy/vercel/make_regions.py [--require-traits]
A county is listed when data/regions/<fips>/fields.parquet exists; with --require-traits (used while
counties are still being built) only once its field_traits.parquet exists too.
"""
from __future__ import annotations

import json
import time
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

import pandas as pd  # noqa: E402

from pipeline.region import BASE_DATA_DIR, DEFAULT_FIPS, data_dir_for, get_region  # noqa: E402


def main() -> None:
    require_traits = "--require-traits" in sys.argv[1:]
    out = []
    dirs = sorted(p.name for p in (BASE_DATA_DIR / "regions").glob("[0-9]" * 5) if p.is_dir())
    for fips in [DEFAULT_FIPS] + dirs:
        fields = data_dir_for(fips) / "fields.parquet"
        if not fields.is_file():
            continue
        traits = data_dir_for(fips) / "field_traits.parquet"
        if require_traits and fips != DEFAULT_FIPS and (not traits.is_file() or time.time() - traits.stat().st_mtime < 15):
            continue
        n = len(pd.read_parquet(fields, columns=["field_id"]))
        r = get_region(fips)
        out.append({**r.as_dict(), "field_count": n,
                    "has_traits": (data_dir_for(fips) / "field_traits.parquet").is_file()})
    (HERE / "regions.json").write_text(json.dumps({"default": DEFAULT_FIPS, "regions": out}, indent=1) + "\n")
    print(f"{len(out)} counties: " + ", ".join(f"{r['county']} ({r['field_count']:,})" for r in out))


if __name__ == "__main__":
    main()
