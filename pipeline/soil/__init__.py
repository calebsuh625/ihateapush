"""Soil data.

    ssurgo.py         static organic matter per field from SSURGO (the organic_matter_pct column in schema.md)
    health_schema.py  contract for data/soil_health.parquet, the yearly satellite soil table (schema.md)
    bare_soil.py      Earth Engine step: bare-soil satellite views per field and year -> data/soil_bare_obs.parquet
    build.py          model + noise analysis -> data/soil_health.parquet, its meta file and a report
                      (features.py, model.py, confidence.py, exposure.py, noise.py, report.py)
    bare_proto.py, bare_check.py   the 300-field prototype sample and a raw-pixel recompute check

`from pipeline.soil import organic_matter_pct` and the other SSURGO names keep working: they are
looked up in ssurgo.py on first use, so importing this package stays cheap.
"""


_SUBMODULES = {"ssurgo", "health_schema"}


def __getattr__(name: str):
    import importlib

    if name.startswith("__"):
        raise AttributeError(name)
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    try:
        return getattr(importlib.import_module(f"{__name__}.ssurgo"), name)
    except AttributeError:
        raise AttributeError(f"module 'pipeline.soil' has no attribute {name!r}") from None
