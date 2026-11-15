"""Phase 3, piece A1: future-weather tables for Finney County.

    python -m pipeline.climate.build     # fetch what is missing, correct, write the three tables + report
    python -m pipeline.climate.schema    # validate the three tables

Modules: `fetch_power` (observed daily weather, NASA POWER), `fetch_gddp` (daily climate-model output,
NASA/GDDP-CMIP6 via Earth Engine), `bias` (calendar handling and the delta-method correction), `et0`
(FAO-56 Penman-Monteith), `indicators` (yearly and monthly numbers), `build` (the one command),
`schema` (the contract and validator of the three tables; light imports, safe for the API).
"""
