"""Step 1 safety net: write data/fields_fake.parquet.

30 non-overlapping rectangular fields near 37.9N, -100.8W (Finney County, KS) with
plausible 2008–2024 crop histories. Same contract as the real file (schema.md), so the
API and frontend can be built and demoed without the real pipeline.

    python pipeline/make_fake_fields.py
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `python pipeline/make_fake_fields.py`
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
from shapely.geometry import box  # noqa: E402

from pipeline.schema import COLUMNS, FAKE_PATH, summarize, validate_fields  # noqa: E402

SEED = 20055  # Finney County FIPS
CENTER_LAT, CENTER_LON = 37.9, -100.8
COLS, ROWS = 6, 5  # 30 fields
PITCH_M = 1000.0  # one grid cell per field
MARGIN_M = 40.0  # keeps neighbours apart (roads / field borders)
YEARS = list(range(2008, 2025))
EQUAL_AREA_CRS = "EPSG:5070"  # CONUS Albers


def corn_on_corn(rng: random.Random) -> list[str]:
    """Irrigated continuous corn with the odd break year."""
    return [rng.choice(["soybeans", "sorghum"]) if rng.random() < 0.08 else "corn" for _ in YEARS]


def corn_soy(rng: random.Random) -> list[str]:
    """Corn/soybean alternation; sometimes corn is planted two years running."""
    out, crop = [], rng.choice(["corn", "soybeans"])
    for _ in YEARS:
        out.append(crop)
        if crop == "corn" and rng.random() < 0.12:
            continue  # second-year corn
        crop = "soybeans" if crop == "corn" else "corn"
    return out


def wheat_sorghum_fallow(rng: random.Random) -> list[str]:
    """Dryland winter wheat → sorghum → fallow, with occasional substitutions."""
    cycle = ["winter wheat", "sorghum", "fallow"]
    phase = rng.randrange(3)
    out = []
    for i, _ in enumerate(YEARS):
        crop = cycle[(i + phase) % 3]
        roll = rng.random()
        if crop == "sorghum" and roll < 0.15:
            crop = "corn"
        elif crop == "fallow" and roll < 0.10:
            crop = "winter wheat"
        out.append(crop)
    return out


def alfalfa_stretch(rng: random.Random) -> list[str]:
    """Annual crops with one or two multi-year alfalfa stands."""
    annual = rng.choice([["corn", "winter wheat"], ["corn", "sorghum", "winter wheat"], ["corn", "corn", "soybeans"]])
    out: list[str] = []
    i = rng.randrange(len(annual))
    in_alfalfa = rng.random() < 0.4
    while len(out) < len(YEARS):
        if in_alfalfa:
            out.extend(["alfalfa"] * rng.randint(4, 6))
        else:
            for _ in range(rng.randint(3, 5)):
                out.append(annual[i % len(annual)])
                i += 1
        in_alfalfa = not in_alfalfa
    return out[: len(YEARS)]


ARCHETYPES = [corn_on_corn] * 6 + [corn_soy] * 8 + [wheat_sorghum_fallow] * 9 + [alfalfa_stretch] * 7


def make_fake_fields(seed: int = SEED) -> gpd.GeoDataFrame:
    rng = random.Random(seed)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(CENTER_LAT))
    x0 = -COLS * PITCH_M / 2  # metres east of centre
    y0 = -ROWS * PITCH_M / 2  # metres north of centre

    archetypes = ARCHETYPES[:]
    rng.shuffle(archetypes)

    rows = []
    for n in range(COLS * ROWS):
        col, row = n % COLS, n // COLS
        width = rng.uniform(550, PITCH_M - 2 * MARGIN_M)
        height = rng.uniform(550, PITCH_M - 2 * MARGIN_M)
        west = x0 + col * PITCH_M + MARGIN_M + rng.uniform(0, PITCH_M - 2 * MARGIN_M - width)
        south = y0 + row * PITCH_M + MARGIN_M + rng.uniform(0, PITCH_M - 2 * MARGIN_M - height)
        geom = box(
            CENTER_LON + west / m_per_deg_lon,
            CENTER_LAT + south / m_per_deg_lat,
            CENTER_LON + (west + width) / m_per_deg_lon,
            CENTER_LAT + (south + height) / m_per_deg_lat,
        )
        history = archetypes[n](rng)
        rows.append(
            {
                "field_id": f"KS-{n + 1:06d}",
                "geometry": geom,
                "crop_history": json.dumps({str(y): c for y, c in zip(YEARS, history)}),
                "organic_matter_pct": round(rng.uniform(1.0, 3.5), 2),
                "slope_pct": round(rng.uniform(0.0, 6.0), 2),
            }
        )

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    gdf["area_ha"] = (gdf.geometry.to_crs(EQUAL_AREA_CRS).area / 10_000).round(2)
    gdf = gdf[COLUMNS]

    # Belt and braces: the grid construction already guarantees this.
    for i in range(len(gdf)):
        for j in range(i + 1, len(gdf)):
            assert not gdf.geometry.iloc[i].intersects(gdf.geometry.iloc[j]), (i, j)
    return gdf


def main() -> None:
    gdf = make_fake_fields()
    validate_fields(gdf, "fake fields")
    FAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(FAKE_PATH, index=False)
    print(f"wrote {FAKE_PATH.relative_to(ROOT)}\n")
    print(summarize(gdf))


if __name__ == "__main__":
    main()
