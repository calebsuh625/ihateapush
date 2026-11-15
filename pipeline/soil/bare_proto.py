"""Choose the prototype fields for the soil work -> data/soil_proto_fields.txt.

    python -m pipeline.soil.bare_proto            # 30 clusters of 10 neighbouring fields, seed 2055

Seeded and reproducible. The fields are compact clusters spread over the county, chosen so that
they span the county's range of SSURGO organic matter:

* Fields are ranked by organic_matter_pct and cut into as many equal strata as there are clusters.
  Each stratum contributes one seed field, drawn at random, and the cluster is that seed plus its
  nearest neighbours (centroid distance) - whatever their soil.
* Every cluster lies inside one 7.68 km cell (256 x 256 pixels) of the 30 m grid that
  pipeline/soil/bare_soil.py groups fields by, and no two clusters share a cell: a cluster is then
  one small Earth Engine request, and the clusters end up in different parts of the county. Fields
  that straddle a cell edge, or that do not survive the inward buffer, cannot be chosen (neither
  has anything to do with their soil).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.common import DATA_DIR  # noqa: E402
from pipeline.satellite import GRID_CRS, GRID_TRANSFORM, positive_int  # noqa: E402
from pipeline.schema import REAL_PATH, SchemaError, load_fields  # noqa: E402
from pipeline.soil.bare_soil import TILE_PX, buffer_inward  # noqa: E402

PROTO_FIELDS_PATH = DATA_DIR / "soil_proto_fields.txt"
N_CLUSTERS = 30
CLUSTER_SIZE = 10
SEED = 2055


def grid_cells(fields):
    """Per field: the (column, row) of the grid cell that holds its whole shrunk outline, or None."""
    shrunk = buffer_inward(fields)
    bounds = shrunk.to_crs(GRID_CRS).bounds.to_numpy()
    size = GRID_TRANSFORM[0] * TILE_PX
    x0, y0 = GRID_TRANSFORM[2], GRID_TRANSFORM[5]
    cells = []
    for empty, (minx, miny, maxx, maxy) in zip(shrunk.is_empty.to_numpy(), bounds):
        if empty or not np.isfinite(minx):
            cells.append(None)
            continue
        lo = (int(np.floor((minx - x0) / size)), int(np.floor((miny - y0) / size)))
        hi = (int(np.floor((maxx - x0) / size)), int(np.floor((maxy - y0) / size)))
        cells.append(lo if lo == hi else None)
    return cells


def choose(fields, n_clusters: int = N_CLUSTERS, cluster_size: int = CLUSTER_SIZE, seed: int = SEED) -> list[list[str]]:
    """The clusters (lists of field ids), one per organic-matter stratum, lowest stratum first."""
    fields = fields.sort_values("field_id").reset_index(drop=True)
    cells = grid_cells(fields)
    centroids = fields.geometry.to_crs(GRID_CRS).centroid
    xy = np.column_stack([centroids.x.to_numpy(), centroids.y.to_numpy()])
    om = fields["organic_matter_pct"].to_numpy(dtype=float)
    by_cell: dict[tuple[int, int], list[int]] = {}
    for i, cell in enumerate(cells):
        if cell is not None:
            by_cell.setdefault(cell, []).append(i)
    eligible = np.array([i for i, c in enumerate(cells) if c is not None and np.isfinite(om[i]) and len(by_cell[c]) >= cluster_size])
    if len(eligible) < n_clusters:
        raise ValueError("not enough fields to choose from")
    ranked = eligible[np.argsort(om[eligible], kind="stable")]
    rng = np.random.default_rng(seed)
    used_cells: set[tuple[int, int]] = set()
    clusters: list[list[str]] = []
    for stratum in np.array_split(ranked, n_clusters):
        for i in rng.permutation(stratum):
            cell = cells[i]
            if cell in used_cells:
                continue
            members = np.array(by_cell[cell])
            nearest = members[np.argsort(np.hypot(*(xy[members] - xy[i]).T), kind="stable")[:cluster_size]]
            used_cells.add(cell)
            clusters.append(sorted(fields["field_id"].to_numpy()[nearest].tolist()))
            break
    if len(clusters) < n_clusters:
        raise ValueError(f"only {len(clusters)} of {n_clusters} clusters could be placed in different grid cells")
    return clusters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fields", type=Path, default=REAL_PATH)
    parser.add_argument("--out", type=Path, default=PROTO_FIELDS_PATH)
    parser.add_argument("--clusters", type=positive_int, default=N_CLUSTERS)
    parser.add_argument("--size", type=positive_int, default=CLUSTER_SIZE)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    try:
        fields = load_fields(args.fields)
        clusters = choose(fields, args.clusters, args.size, args.seed)
    except FileNotFoundError as exc:
        print(f"ERROR: fields file not found: {exc}", file=sys.stderr)
        return 2
    except (SchemaError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    ids = sorted(fid for cluster in clusters for fid in cluster)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_name(args.out.name + ".part")
    tmp.write_text(
        f"# {len(ids)} prototype fields: {len(clusters)} clusters of {args.size} neighbours, seed {args.seed}\n"
        f"# written by python -m pipeline.soil.bare_proto from {args.fields.name}\n" + "\n".join(ids) + "\n"
    )
    tmp.replace(args.out)

    chosen = fields[fields["field_id"].isin(ids)]
    om_all, om = fields["organic_matter_pct"], chosen["organic_matter_pct"]
    quantiles = [0.0, 0.05, 0.25, 0.5, 0.75, 0.95, 1.0]
    bounds = chosen.total_bounds
    full = fields.total_bounds
    print(f"wrote {args.out}: {len(ids)} fields in {len(clusters)} clusters")
    print("organic_matter_pct  min   p5    p25   p50   p75   p95   max")
    print("  county          " + " ".join(f"{om_all.quantile(q):5.2f}" for q in quantiles))
    print("  prototype       " + " ".join(f"{om.quantile(q):5.2f}" for q in quantiles))
    print(f"distinct organic_matter_pct values: {om.round(3).nunique()} (county {om_all.round(3).nunique()})")
    print(
        f"extent covered: {(bounds[2] - bounds[0]) / (full[2] - full[0]):.0%} of the county's width, "
        f"{(bounds[3] - bounds[1]) / (full[3] - full[1]):.0%} of its height; "
        f"area_ha median {chosen['area_ha'].median():.1f} (county {fields['area_ha'].median():.1f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
