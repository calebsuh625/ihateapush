"""Contract for the three Phase 3 climate tables (see schema.md, "Phase 3 climate tables").

Built by `python -m pipeline.climate.build`. This module is the single validator. It imports nothing
heavy (pandas only: no Earth Engine, no network), so the API can use it.

Yearly columns (YEARLY_COLUMNS, all float, no nulls), the same in the first two tables:

    tmean_c          growing-season (1 Apr - 30 Sep) mean of daily (Tmax + Tmin) / 2, C
    rain_mm          growing-season rain total, mm
    summer_rain_mm   June - August rain total, mm
    gdd              growing-season growing degree days, base 10 C (Tmax capped at 30 C, Tmin floored at 10 C)
    hot_days         growing-season days with Tmax above 35 C
    dry_spell_days   longest run of growing-season days with rain below 1 mm
    et0_mm           growing-season reference ET total (FAO-56 Penman-Monteith), mm
    wind_ms          growing-season mean wind speed at 10 m, m/s
    wheat_rain_mm    rain total of the wheat season, 1 Oct (year - 1) to 30 Jun (year), mm
    wheat_hot_days   days with Tmax above 32 C in May - June (grain fill)
    wheat_et0_mm     wheat-season reference ET total, mm
    spring_wind_ms   February - May mean wind speed at 10 m, m/s
    erosion_climate  wind-erosion climatic factor, observed 2000-2024 mean = 100, single years capped at 300
    annual_rain_mm, annual_et0_mm, annual_tmean_c   calendar-year totals / mean

data/climate_futures.parquet   model (str), scenario (ssp245 / ssp585), year (int) + YEARLY_COLUMNS;
                               one row for every model x scenario x year 2000-2060. Years to 2024 are
                               the model's own corrected baseline; futures proper are 2025-2060.
data/climate_history.parquet   year (int, 2000-2024) + YEARLY_COLUMNS, observed (NASA POWER).
data/climate_monthly.parquet   model, scenario, year, month (1-12) + MONTHLY_COLUMNS (tmax_c, tmin_c,
                               tmean_c monthly means in C; rain_mm, et0_mm monthly totals; wind_ms mean
                               at 10 m; hot_days = days above 35 C). Observed rows have model and
                               scenario "observed" and cover 2000-2024; model rows cover 2000-2060.
                               October - December 1999 is there too (the first wheat season).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from pipeline.region import DATA_DIR as REGION_DATA_DIR  # noqa: E402  (data/ for Finney, data/regions/<fips>/ otherwise)

ROOT = Path(__file__).resolve().parents[2]
FUTURES_PATH = REGION_DATA_DIR / "climate_futures.parquet"
HISTORY_PATH = REGION_DATA_DIR / "climate_history.parquet"
MONTHLY_PATH = REGION_DATA_DIR / "climate_monthly.parquet"

SCENARIOS = ("ssp245", "ssp585")
OBSERVED = "observed"
FIRST_YEAR = 2000
LAST_YEAR = 2060
HISTORY_LAST_YEAR = 2024
LEAD_IN = (1999, (10, 11, 12))  # the only months allowed before FIRST_YEAR in the monthly table
SOURCE = "NASA/GDDP-CMIP6 daily climate-model output (Earth Engine), bias-corrected by month against NASA POWER 2000-2024"

YEARLY_COLUMNS = [
    "tmean_c", "rain_mm", "summer_rain_mm", "gdd", "hot_days", "dry_spell_days", "et0_mm", "wind_ms",
    "wheat_rain_mm", "wheat_hot_days", "wheat_et0_mm", "spring_wind_ms", "erosion_climate",
    "annual_rain_mm", "annual_et0_mm", "annual_tmean_c",
]
FUTURES_COLUMNS = ["model", "scenario", "year", *YEARLY_COLUMNS]
HISTORY_COLUMNS = ["year", *YEARLY_COLUMNS]
MONTHLY_VALUE_COLUMNS = ["tmax_c", "tmin_c", "tmean_c", "rain_mm", "et0_mm", "wind_ms", "hot_days"]
MONTHLY_COLUMNS = ["model", "scenario", "year", "month", *MONTHLY_VALUE_COLUMNS]

# allowed range of every value column: (low, high), None = open
YEARLY_RANGES = {
    "tmean_c": (0.0, 45.0), "rain_mm": (0.0, 3000.0), "summer_rain_mm": (0.0, 3000.0), "gdd": (0.0, 3660.0),
    "hot_days": (0.0, 183.0), "dry_spell_days": (0.0, 183.0), "et0_mm": (1.0, 4000.0), "wind_ms": (0.1, 30.0),
    "wheat_rain_mm": (0.0, 3000.0), "wheat_hot_days": (0.0, 61.0), "wheat_et0_mm": (1.0, 4000.0),
    "spring_wind_ms": (0.1, 30.0), "erosion_climate": (0.0, 300.0), "annual_rain_mm": (0.0, 5000.0),
    "annual_et0_mm": (1.0, 6000.0), "annual_tmean_c": (-10.0, 40.0),
}
MONTHLY_RANGES = {
    "tmax_c": (-40.0, 60.0), "tmin_c": (-50.0, 45.0), "tmean_c": (-45.0, 50.0), "rain_mm": (0.0, 1500.0),
    "et0_mm": (0.0, 600.0), "wind_ms": (0.1, 30.0), "hot_days": (0.0, 31.0),
}


class ClimateSchemaError(ValueError):
    pass


def _check_shape(df, columns: list[str]) -> list[str] | None:
    if not isinstance(df, pd.DataFrame):
        return [f"expected a DataFrame, got {type(df).__name__}"]
    missing = [c for c in columns if c not in df.columns]
    if missing:
        return [f"missing column(s): {', '.join(missing)}"]
    if len(df) == 0:
        return ["table has no rows"]
    return None


def _check_int(df, column: str, lo: int, hi: int) -> list[str]:
    if not pd.api.types.is_integer_dtype(df[column]):
        return [f"{column}: dtype is {df[column].dtype}, expected integer"]
    if ((df[column] < lo) | (df[column] > hi)).any():
        return [f"{column}: values outside {lo}-{hi}"]
    return []


def _check_values(df, ranges: dict) -> list[str]:
    problems = []
    for column, (lo, hi) in ranges.items():
        s = df[column]
        if not pd.api.types.is_float_dtype(s):
            problems.append(f"{column}: dtype is {s.dtype}, expected float")
            continue
        if s.isna().any():
            problems.append(f"{column}: {int(s.isna().sum())} null value(s); the table must have none")
        bad = (s < lo) | (s > hi)
        if bad.any():
            problems.append(f"{column}: {int(bad.sum())} value(s) outside [{lo:g}, {hi:g}]")
    return problems


def _check_text(df, column: str) -> list[str]:
    if df[column].isna().any() or not df[column].map(lambda v: isinstance(v, str) and bool(v)).all():
        return [f"{column}: must be non-null, non-empty strings"]
    return []


def check_futures(df) -> list[str]:
    """Every way `df` breaks the contract of data/climate_futures.parquet (empty list = valid)."""
    shape = _check_shape(df, FUTURES_COLUMNS)
    if shape:
        return shape
    problems = _check_text(df, "model") + _check_text(df, "scenario")
    bad_scenarios = sorted(set(map(str, df["scenario"].dropna())) - set(SCENARIOS))
    if bad_scenarios:
        problems.append(f"scenario: unknown value(s) {', '.join(bad_scenarios)} (allowed: {', '.join(SCENARIOS)})")
    if (df["model"] == OBSERVED).any():
        problems.append(f'model: "{OBSERVED}" belongs in the history table, not here')
    year_problems = _check_int(df, "year", FIRST_YEAR, LAST_YEAR)
    problems += year_problems
    if df.duplicated(["model", "scenario", "year"]).any():
        problems.append(f"{int(df.duplicated(['model', 'scenario', 'year']).sum())} duplicate (model, scenario, year) row(s)")
    if not year_problems and not bad_scenarios:
        have = set(zip(df["model"], df["scenario"], df["year"].astype(int)))
        want = {(m, s, y) for m in df["model"].dropna().unique() for s in SCENARIOS for y in range(FIRST_YEAR, LAST_YEAR + 1)}
        gone = sorted(want - have)
        if gone:
            m, s, y = gone[0]
            problems.append(
                f"{len(gone)} (model, scenario, year) row(s) missing: every model needs both scenarios and "
                f"every year {FIRST_YEAR}-{LAST_YEAR} (first missing: {m} {s} {y})"
            )
    return problems + _check_values(df, YEARLY_RANGES)


def check_history(df) -> list[str]:
    """Every way `df` breaks the contract of data/climate_history.parquet (empty list = valid)."""
    shape = _check_shape(df, HISTORY_COLUMNS)
    if shape:
        return shape
    problems = _check_int(df, "year", FIRST_YEAR, HISTORY_LAST_YEAR)
    if not problems:
        if df["year"].duplicated().any():
            problems.append(f"{int(df['year'].duplicated().sum())} duplicate year row(s)")
        gone = sorted(set(range(FIRST_YEAR, HISTORY_LAST_YEAR + 1)) - set(df["year"].astype(int)))
        if gone:
            problems.append(f"{len(gone)} year(s) missing from {FIRST_YEAR}-{HISTORY_LAST_YEAR} (first missing: {gone[0]})")
    return problems + _check_values(df, YEARLY_RANGES)


def check_monthly(df) -> list[str]:
    """Every way `df` breaks the contract of data/climate_monthly.parquet (empty list = valid)."""
    shape = _check_shape(df, MONTHLY_COLUMNS)
    if shape:
        return shape
    problems = _check_text(df, "model") + _check_text(df, "scenario")
    bad_scenarios = sorted(set(map(str, df["scenario"].dropna())) - set(SCENARIOS) - {OBSERVED})
    if bad_scenarios:
        problems.append(f"scenario: unknown value(s) {', '.join(bad_scenarios)} (allowed: {', '.join(SCENARIOS)}, {OBSERVED})")
    mixed = (df["model"] == OBSERVED) != (df["scenario"] == OBSERVED)
    if mixed.any():
        problems.append(f'{int(mixed.sum())} row(s) where only one of model and scenario is "{OBSERVED}"')
    key_problems = _check_int(df, "year", LEAD_IN[0], LAST_YEAR) + _check_int(df, "month", 1, 12)
    problems += key_problems
    if df.duplicated(["model", "scenario", "year", "month"]).any():
        problems.append(f"{int(df.duplicated(['model', 'scenario', 'year', 'month']).sum())} duplicate (model, scenario, year, month) row(s)")
    if not key_problems and not bad_scenarios and not mixed.any():
        year, month = df["year"].astype(int), df["month"].astype(int)
        early = (year < FIRST_YEAR) & ~((year == LEAD_IN[0]) & month.isin(LEAD_IN[1]))
        if early.any():
            problems.append(f"{int(early.sum())} row(s) before {FIRST_YEAR} other than October-December {LEAD_IN[0]}")
        late = (df["model"] == OBSERVED) & (year > HISTORY_LAST_YEAR)
        if late.any():
            problems.append(f"{int(late.sum())} observed row(s) after {HISTORY_LAST_YEAR}")
        have = set(zip(df["model"], df["scenario"], year, month))
        models = [m for m in df["model"].dropna().unique() if m != OBSERVED]
        series = [(m, s, LAST_YEAR) for m in models for s in SCENARIOS] + [(OBSERVED, OBSERVED, HISTORY_LAST_YEAR)]
        want = {(m, s, y, mo) for m, s, last in series for y in range(FIRST_YEAR, last + 1) for mo in range(1, 13)}
        gone = sorted(want - have)
        if gone:
            m, s, y, mo = gone[0]
            problems.append(
                f"{len(gone)} (model, scenario, year, month) row(s) missing: observed needs every month of "
                f"{FIRST_YEAR}-{HISTORY_LAST_YEAR}, every model both scenarios and every month of {FIRST_YEAR}-{LAST_YEAR} "
                f"(first missing: {m} {s} {y}-{mo:02d})"
            )
    return problems + _check_values(df, MONTHLY_RANGES)


TABLES = {
    "futures": (FUTURES_PATH, FUTURES_COLUMNS, check_futures),
    "history": (HISTORY_PATH, HISTORY_COLUMNS, check_history),
    "monthly": (MONTHLY_PATH, MONTHLY_COLUMNS, check_monthly),
}


def validate(name: str, df, source: str = "") -> None:
    problems = TABLES[name][2](df)
    if problems:
        where = f" ({source})" if source else ""
        raise ClimateSchemaError(
            f"climate {name} table{where} does not match pipeline/climate/schema.py:\n" + "\n".join(f"  - {p}" for p in problems)
        )


def _load(name: str, path: str | Path | None) -> pd.DataFrame:
    default, columns, _ = TABLES[name]
    path = Path(path) if path is not None else default
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        raise ClimateSchemaError(f"{path}: could not read as Parquet: {exc}") from exc
    validate(name, df, str(path))
    return df[columns]


def load_futures(path: str | Path | None = None) -> pd.DataFrame:
    """Read and validate data/climate_futures.parquet. Raises FileNotFoundError / ClimateSchemaError."""
    return _load("futures", path)


def load_history(path: str | Path | None = None) -> pd.DataFrame:
    """Read and validate data/climate_history.parquet. Raises FileNotFoundError / ClimateSchemaError."""
    return _load("history", path)


def load_monthly(path: str | Path | None = None) -> pd.DataFrame:
    """Read and validate data/climate_monthly.parquet. Raises FileNotFoundError / ClimateSchemaError."""
    return _load("monthly", path)


def available(directory: str | Path | None = None) -> bool:
    """True when all three tables exist (it does not validate them)."""
    base = Path(directory) if directory is not None else FUTURES_PATH.parent
    return all((base / path.name).exists() for path, _, _ in TABLES.values())


def summarize(futures: pd.DataFrame, history: pd.DataFrame, monthly: pd.DataFrame) -> str:
    models = sorted(futures["model"].unique())
    late = futures[futures["year"].between(2050, 2059)].groupby("scenario")[["tmean_c", "hot_days"]].mean()
    now = futures[futures["year"].between(2020, 2029)].groupby("scenario")[["tmean_c", "hot_days"]].mean()
    lines = [
        f"futures: {len(futures):,} rows = {len(models)} models x {futures['scenario'].nunique()} scenarios x "
        f"{futures['year'].nunique()} years ({futures['year'].min()}-{futures['year'].max()}); models: {', '.join(models)}",
        f"history: {len(history)} observed years ({history['year'].min()}-{history['year'].max()}); growing season mean "
        f"{history['tmean_c'].mean():.1f} C, rain {history['rain_mm'].mean():.0f} mm, ET0 {history['et0_mm'].mean():.0f} mm, "
        f"{history['hot_days'].mean():.0f} days above 35 C",
        f"monthly: {len(monthly):,} rows ({monthly['model'].nunique() - 1} models x 2 scenarios + observed)",
    ]
    for scenario in SCENARIOS:
        if scenario in late.index and scenario in now.index:
            lines.append(
                f"{scenario}, 2050s against 2020s (model mean): {late.loc[scenario, 'tmean_c'] - now.loc[scenario, 'tmean_c']:+.1f} C, "
                f"{late.loc[scenario, 'hot_days'] - now.loc[scenario, 'hot_days']:+.0f} days above 35 C"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Validate the three tables (optionally in another directory) and print a short summary."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    base = Path(args[0]) if args else FUTURES_PATH.parent
    loaded = {}
    status = 0
    for name, (default, _, _) in TABLES.items():
        path = base / default.name
        try:
            loaded[name] = _load(name, path)
            print(f"{path}: valid")
        except FileNotFoundError:
            print(f"{path} does not exist (build it with: python -m pipeline.climate.build)")
            status = 1
        except ClimateSchemaError as exc:
            print(exc)
            status = 1
    if status == 0:
        print(summarize(loaded["futures"], loaded["history"], loaded["monthly"]))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
