"""Which county the API serves, and which counties have been built.

    GET /region     the county this API serves ($REGION, default Finney County, KS), plus which inputs
                    it borrows from Finney County because it has none of its own yet
    GET /regions    every county with a built data/<...>/fields.parquet

One API process serves one county; start another with `REGION=<fips> make dev` to serve another.
See pipeline/region.py for where each county's files live.
"""
from __future__ import annotations

from fastapi import APIRouter

from api.planner import config as planner_config
from pipeline.region import BORROWABLE, REGION, built_regions

router = APIRouter(tags=["region"])


def current() -> dict:
    """The served county and the Finney-borrowed inputs, each with a plain sentence."""
    try:
        kinds = planner_config.settings().borrowed
    except ValueError:  # a bad planner setting is reported by the planner, not here
        kinds = ()
    return {
        **REGION.as_dict(),
        "borrowed": [{"kind": k, "note": BORROWABLE[k]} for k in kinds],
    }


@router.get("/region", summary="The county this API serves")
async def region() -> dict:
    return current()


@router.get("/regions", summary="Counties with built data")
async def regions() -> dict:
    return {"current": REGION.fips, "regions": built_regions()}
