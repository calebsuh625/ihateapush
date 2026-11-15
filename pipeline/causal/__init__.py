"""Phase 2B: the causal effect of crop rotations on soil, learned from matched neighbouring fields.

    python -m pipeline.causal.run_causal        # also: make causal

Modules: `config` (every setting, incl. which soil column is the outcome), `soil_data` (loads
data/soil_health.parquet, falling back to the fake one), `make_fake_soil` (the fake),
`rotations` (2016-2024 rotation types), `irrigation` (irrigated vs dryland), `matching`
(look-alike pairs), `did` (difference-in-differences, bootstrap, placebos).
"""
