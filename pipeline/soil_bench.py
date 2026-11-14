"""Scale proof for pipeline/soil/ssurgo.py: ~6,000 synthetic fields spread over all of Finney County.

    python pipeline/soil_bench.py [--n 6000] [--seed 20055] [--spot-check 8] [--cache-dir DIR]

Generates seeded rectangles of 20-70 ha inside the county's bounding box, computes their
organic matter twice (first run: whatever is not cached is fetched from Soil Data Access;
second run: cache only) and prints timings, HTTP request counts and the distributions.
Because the tile grid is fixed, this also warms `data/raw/ssurgo/` for the real county run.
The fields are generated in memory and never written anywhere.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/soil_bench.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402

from pipeline.common import EQUAL_AREA_CRS  # noqa: E402
from pipeline.soil import SoilDataError, field_soil_table, format_report, spot_check  # noqa: E402

FINNEY_BBOX = (-101.11, 37.73, -100.22, 38.27)  # lon min, lat min, lon max, lat max


def synthetic_fields(n: int = 6000, seed: int = 20055, bbox=FINNEY_BBOX) -> gpd.GeoDataFrame:
    """`n` axis-aligned rectangles of 20-70 ha, uniformly scattered inside `bbox` (EPSG:4326)."""
    rng = np.random.default_rng(seed)
    x0, y0, x1, y1 = bbox
    area_m2 = rng.uniform(20, 70, n) * 10_000
    aspect = rng.uniform(0.5, 2.0, n)  # width / height
    width = np.sqrt(area_m2 * aspect)
    height = area_m2 / width
    m_per_deg_lat = 111_320.0
    half_h = height / 2 / m_per_deg_lat
    cy = rng.uniform(y0 + half_h, y1 - half_h)
    half_w = width / 2 / (m_per_deg_lat * np.cos(np.radians(cy)))
    cx = rng.uniform(x0 + half_w, x1 - half_w)
    return gpd.GeoDataFrame(
        {"field_id": [f"KS-{i + 1:06d}" for i in range(n)]},
        geometry=shapely.box(cx - half_w, cy - half_h, cx + half_w, cy + half_h),
        crs="EPSG:4326",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=20055)
    parser.add_argument("--cache-dir", default=None, help="default data/raw/ssurgo (use a scratch dir to time a cold run)")
    parser.add_argument("--spot-check", type=int, default=0, metavar="N", help="check N fields against SDA's own SQL")
    args = parser.parse_args(argv)

    fields = synthetic_fields(args.n, args.seed)
    area_ha = fields.geometry.to_crs(EQUAL_AREA_CRS).area / 10_000
    print(
        f"{len(fields):,} synthetic fields, {area_ha.min():.1f}-{area_ha.max():.1f} ha (mean {area_ha.mean():.1f}), "
        f"bounds {np.round(fields.total_bounds, 4).tolist()}"
    )
    runs = []
    table = None
    for label in ("first run", "second run (cache only)"):
        t0 = time.perf_counter()
        try:
            table = field_soil_table(fields, cache_dir=args.cache_dir)
        except SoilDataError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 2
        prov = table.attrs["provenance"]
        runs.append(
            {
                "run": label,
                "seconds": round(time.perf_counter() - t0, 2),
                "http_requests": prov["http"]["http_requests"],
                "retries": prov["http"]["retries"],
                "tiles_fetched": prov["tile_scheme"]["tiles_fetched"],
                "tiles_from_cache": prov["tile_scheme"]["tiles_from_cache"],
                "polygons_s": prov["timings_s"]["polygons"],
                "attributes_s": prov["timings_s"]["attributes"],
                "overlay_s": prov["timings_s"]["overlay"],
            }
        )
    print()
    print(pd.DataFrame(runs).to_string(index=False))
    prov = table.attrs["provenance"]
    cache = prov["cache"]
    print(f"cache: {cache['files']} files, {cache['bytes'] / 1e6:.1f} MB in {cache['dir']}")
    print(f"map unit polygons: {prov['counts']['mupolygons']:,} unique ({prov['counts']['mupolygon_rows_in_tiles']:,} rows over all tiles)")
    print()
    print(format_report(fields, table))
    if args.spot_check:
        check = spot_check(fields, table, args.spot_check)
        print(f"\nspot check against SDA's own SQL for {len(check)} field(s):")
        with pd.option_context("display.width", 220, "display.max_columns", 20, "display.float_format", "{:.4f}".format):
            print(check.to_string(index=False))
        print(f"max |diff| = {check['diff'].abs().max():.5f} percentage points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
