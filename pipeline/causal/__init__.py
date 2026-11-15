"""Phase 2B: crop rotations compared on days the soil sits bare, using matched neighbouring fields.

    python -m pipeline.causal.run_causal        # also: make causal

Modules: `config` (every setting, incl. which soil column is the outcome), `soil_data` (loads
data/soil_health.parquet, falling back to the fake one), `make_fake_soil` (the fake),
`rotations` (rotation types), `irrigation` (irrigated vs dryland), `matching`
(look-alike pairs), `did` (difference-in-differences, bootstrap, placebos).
"""
