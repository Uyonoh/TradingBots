"""
tick_data_analyzer.py
=====================
Comprehensive tick data analysis script for quantitative finance and
high-frequency trading data stored in Parquet format.

Timezone Assumption:
    All input data is assumed to be in UTC. If your data is in a different
    timezone, set CONFIG["TIMEZONE"] to the appropriate pytz/zoneinfo string
    (e.g., "America/New_York") and the loader will convert automatically.

Column Name Assumption:
    Expected columns: 'time' (or 'timestamp'), 'symbol', 'bid', 'ask', 'last'.
    If 'last' is absent, mid-price = (bid + ask) / 2 is used instead.
    Modify COLUMN_MAP in CONFIG to adapt to your data's actual column names.
"""

# ─────────────────────────────────────────────
# Standard library
# ─────────────────────────────────────────────
import os
import glob
import warnings
import logging
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────
# Third-party
# ─────────────────────────────────────────────
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

# ─────────────────────────────────────────────
# Logging configuration
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ── edit here, not in the functions below
# ══════════════════════════════════════════════════════════════════════════════
symbol = "ger40"
symbol = symbol.upper()
CONFIG: dict = {
    # ── I/O paths ─────────────────────────────────────────────────────────────
    "DATA_DIR": f"./tick_data/{symbol}",           # Directory containing *.parquet files
    "OUTPUT_DIR": f"./output/{symbol}",       # Directory for CSV results

    # ── Target instrument ─────────────────────────────────────────────────────
    "TARGET_SYMBOL": symbol,      # Symbol value as it appears in 'symbol' col

    # ── Resampling / time granularity ─────────────────────────────────────────
    "AGGREGATION_INTERVAL": "1s",   # pandas offset alias: '1s', '1min', '5min'…

    # ── Session / open-period definition (24-hour UTC hours) ──────────────────
    "OPEN_PERIOD_START_HOUR": 1,    # e.g. 08:00 UTC – London open
    "OPEN_PERIOD_END_HOUR": 23,     # e.g. 17:00 UTC – London close
    "OPEN_PERIOD_DURATION_MINUTES": 60*22,  # Initial "high-activity" burst window

    # ── Volatility thresholds (multiples of the session average) ──────────────
    "HIGH_VOLATILITY_THRESHOLD_MULTIPLIER": 1.5,
    "LOW_VOLATILITY_THRESHOLD_MULTIPLIER": 0.5,

    # ── Rolling-window sizes ──────────────────────────────────────────────────
    "VOLATILITY_WINDOW_SECONDS": 600,    # 10-minute rolling vol window
    "TICK_DENSITY_WINDOW_SECONDS": 60,   # 1-minute rolling tick-density window

    # ── Timezone ──────────────────────────────────────────────────────────────
    "TIMEZONE": "Europe/Helsinki",              # Change to e.g. 'America/New_York' if needed

    # ── Column name mapping (LHS = canonical name, RHS = actual column name) ──
    # "sa": ['bid', 'ask', 'last', 'volume', 'time_msc', 'flags', 'volume_real'],
    "COLUMN_MAP": {
        "time":   "time_msc",           # also accepts 'timestamp' automatically
        "symbol": "symbol",
        "bid":    "bid",
        "ask":    "ask",
        "last":   None,           # set to None if column does not exist
    },
}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPER UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_time_column(df: pd.DataFrame, col_map: dict) -> str:
    """
    Return the actual time-column name present in *df*, checking the
    canonical name first, then the common alias 'timestamp'.

    Raises ValueError when neither is found.
    """
    canonical = col_map.get("time", "time")
    if canonical in df.columns:
        return canonical
    if "timestamp" in df.columns:
        log.info("Column '%s' not found; using 'timestamp' instead.", canonical)
        return "timestamp"
    raise ValueError(
        f"No time column found. Expected '{canonical}' or 'timestamp'. "
        f"Available columns: {list(df.columns)}"
    )


def _resolve_price_column(df: pd.DataFrame, col_map: dict) -> str:
    """
    Decide which price column to use for return / volatility calculations:
      1. 'last' (or its alias) if present and non-null
      2. Mid-price (bid + ask) / 2 synthesised as a new column '__mid__'

    Returns the column name that callers should use.
    """
    last_col = col_map.get("last", "last")
    if last_col and last_col in df.columns and df[last_col].notna().any():
        return last_col

    bid_col  = col_map.get("bid", "bid")
    ask_col  = col_map.get("ask", "ask")
    if bid_col in df.columns and ask_col in df.columns:
        log.info(
            "Column '%s' absent or all-NaN; synthesising mid-price from "
            "bid + ask.", last_col
        )
        df["__mid__"] = (df[bid_col] + df[ask_col]) / 2.0
        return "__mid__"

    raise ValueError(
        "Cannot determine price column. Ensure 'last' (or mid via bid/ask) "
        "is available."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_tick_data(
    file_path: str,
    symbol: str,
    config: dict = CONFIG,
) -> Optional[pd.DataFrame]:
    """
    Load tick data for a single *symbol* from a Parquet file.

    Parameters
    ----------
    file_path : str
        Absolute or relative path to a Parquet file.
    symbol : str
        The financial instrument identifier to filter on.
    config : dict
        Global configuration dictionary.

    Returns
    -------
    pd.DataFrame or None
        DataFrame indexed by a UTC-aware DatetimeIndex, with at least a
        'price' column (either 'last' or synthesised mid). Returns None when
        the file is missing, empty, or does not contain the requested symbol.
    """
    col_map    = config["COLUMN_MAP"]
    timezone   = config["TIMEZONE"]
    sym_col    = col_map.get("symbol", "symbol")

    # ── 1. Load file ──────────────────────────────────────────────────────────
    if not os.path.exists(file_path):
        log.warning("File not found: %s", file_path)
        return None

    try:
        df = pd.read_parquet(file_path)
    except Exception as exc:
        log.error("Failed to read '%s': %s", file_path, exc)
        return None

    if df.empty:
        log.warning("File is empty: %s", file_path)
        return None

    # ── 2. Filter to the target symbol ────────────────────────────────────────
    # if sym_col not in df.columns:
    #     log.warning(
    #         "Symbol column '%s' not found in %s. Columns: %s",
    #         sym_col, file_path, list(df.columns)
    #     )
    #     return None

    # df = df[df[sym_col] == symbol].copy()

    # if df.empty:
    #     log.info("Symbol '%s' not found in %s.", symbol, file_path)
    #     return None

    # ── 3. Parse and set time index ───────────────────────────────────────────
    time_col = _resolve_time_column(df, col_map)
    # df[time_col] = pd.to_datetime(df[time_col], utc=False, errors="coerce")
    df.index = pd.to_datetime(df.index).tz_localize(timezone)
    # df.dropna(subset=[time_col], inplace=True)          # drop unparseable rows

    # if timezone != "UTC":
    #     # df[time_col] = df[time_col].dt.tz_convert(timezone)
    #     df[time_col] = df[time_col].dt.tz_localize(timezone)

    # df.set_index(time_col, inplace=True)
    # df.sort_index(inplace=True)

    # ── 4. Resolve price column ───────────────────────────────────────────────
    price_col = _resolve_price_column(df, col_map)      # may create '__mid__'
    df["price"] = df[price_col]                         # canonical alias

    # ── 5. Drop rows with no usable price ────────────────────────────────────
    df.dropna(subset=["price"], inplace=True)

    if df.empty:
        log.warning("No usable price data for '%s' in %s.", symbol, file_path)
        return None

    log.info(
        "Loaded %d ticks for '%s' from '%s' (%s → %s).",
        len(df), symbol, Path(file_path).name,
        df.index.min(), df.index.max(),
    )
    return df


# ══════════════════════════════════════════════════════════════════════════════
#  METRIC CALCULATION FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_volatility(
    df: pd.DataFrame,
    window_seconds: int,
) -> pd.Series:
    """
    Compute rolling annualised-style volatility from log returns.

    Uses a time-based rolling window so uneven tick spacing is handled
    naturally. A minimum of 2 observations is required; otherwise NaN is
    returned for that window.

    Parameters
    ----------
    df : pd.DataFrame
        Tick DataFrame with a DatetimeIndex and a 'price' column.
    window_seconds : int
        Rolling window width in seconds.

    Returns
    -------
    pd.Series
        Rolling standard deviation of log returns (same index as *df*).
    """
    if "price" not in df.columns or df.empty:
        return pd.Series(dtype=float, name="volatility")

    log_returns = np.log(df["price"] / df["price"].shift(1))
    window      = f"{window_seconds}s"

    vol = (
        log_returns
        .rolling(window=window, min_periods=2)
        .std()
    )
    vol.name = "volatility"
    return vol


def calculate_tick_density(
    df: pd.DataFrame,
    window_seconds: int,
) -> pd.Series:
    """
    Compute the number of ticks within a rolling time window.

    Parameters
    ----------
    df : pd.DataFrame
        Tick DataFrame with a DatetimeIndex.
    window_seconds : int
        Rolling window width in seconds.

    Returns
    -------
    pd.Series
        Count of ticks per window (same index as *df*).
    """
    if df.empty:
        return pd.Series(dtype=float, name="tick_density")

    window = f"{window_seconds}s"
    # A constant series of 1s rolled up gives us the tick count per window
    density = (
        pd.Series(1, index=df.index, name="tick_density")
        .rolling(window=window, min_periods=1)
        .count()
    )
    return density


def calculate_tick_value_change(df: pd.DataFrame) -> pd.Series:
    """
    Compute the absolute price change between consecutive ticks.

    Returns
    -------
    pd.Series
        Absolute tick-to-tick price differences, plus a summary is printed.
    """
    if "price" not in df.columns or len(df) < 2:
        return pd.Series(dtype=float, name="tick_value_change")

    changes = df["price"].diff().abs()
    changes.name = "tick_value_change"
    return changes


def calculate_range_metrics(
    df: pd.DataFrame,
    config: dict = CONFIG,
) -> pd.DataFrame:
    """
    Compute daily high-low price range and related statistics.

    Resamples to a daily frequency and calculates:
      - daily_high, daily_low, daily_range (high − low)
      - daily_open, daily_close, daily_return (close/open − 1)

    Parameters
    ----------
    df : pd.DataFrame
        Tick DataFrame with DatetimeIndex and 'price' column.
    config : dict
        Global config (currently unused but kept for API consistency).

    Returns
    -------
    pd.DataFrame
        One row per calendar day with the range metrics above.
    """
    if "price" not in df.columns or df.empty:
        return pd.DataFrame()

    daily = df["price"].resample("1D").agg(
        daily_open="first",
        daily_high="max",
        daily_low="min",
        daily_close="last",
        tick_count="count",
    )
    daily["daily_range"]  = daily["daily_high"] - daily["daily_low"]
    daily["daily_return"] = (daily["daily_close"] / daily["daily_open"]) - 1

    # Drop days with no data at all
    daily.dropna(subset=["daily_open"], inplace=True)
    return daily


def calculate_average_metrics(
    df: pd.DataFrame,
    config: dict = CONFIG,
) -> dict:
    """
    Compute overall (session-wide) average volatility and tick density.

    Returns
    -------
    dict with keys:
        avg_volatility, avg_tick_density, total_ticks,
        session_start, session_end, session_duration_hours
    """
    if df.empty:
        return {}

    vol_series     = calculate_volatility(df, config["VOLATILITY_WINDOW_SECONDS"])
    density_series = calculate_tick_density(df, config["TICK_DENSITY_WINDOW_SECONDS"])

    duration = df.index[-1] - df.index[0]

    return {
        "avg_volatility":        vol_series.mean(skipna=True),
        "avg_tick_density":      density_series.mean(skipna=True),
        "total_ticks":           len(df),
        "session_start":         df.index[0],
        "session_end":           df.index[-1],
        "session_duration_hours": duration.total_seconds() / 3600,
    }


def identify_high_volatility_open_periods(
    df: pd.DataFrame,
    config: dict = CONFIG,
) -> pd.DataFrame:
    """
    Identify high-volatility bursts during the defined open period.

    Algorithm
    ---------
    1. Restrict to ticks between OPEN_PERIOD_START_HOUR and OPEN_PERIOD_END_HOUR.
    2. Further restrict to the first OPEN_PERIOD_DURATION_MINUTES after each
       day's OPEN_PERIOD_START_HOUR (the "opening burst" window).
    3. Calculate rolling volatility within the burst window.
    4. Flag timestamps where volatility > HIGH_VOLATILITY_THRESHOLD_MULTIPLIER
       × the session-wide average volatility.
    5. Collapse consecutive flagged timestamps into contiguous intervals.

    Returns
    -------
    pd.DataFrame
        Columns: period_start, period_end, duration_seconds, peak_volatility
    """
    if df.empty:
        return pd.DataFrame()
    if not df.index.is_unique:
        df = df.groupby(level=0).max()   # last() or mean(), or max()

    start_h   = config["OPEN_PERIOD_START_HOUR"]
    end_h     = config["OPEN_PERIOD_END_HOUR"]
    burst_min = config["OPEN_PERIOD_DURATION_MINUTES"]
    mult      = config["HIGH_VOLATILITY_THRESHOLD_MULTIPLIER"]
    vol_win   = config["VOLATILITY_WINDOW_SECONDS"]

    # ── Step 1 & 2: filter to opening burst window ───────────────────────────
    hour    = df.index.hour
    minute  = df.index.minute
    in_session = (hour >= start_h) & (hour < end_h)

    # Minutes elapsed since start_h on each day
    mins_since_open = (hour - start_h) * 60 + minute
    in_burst = in_session & (mins_since_open < burst_min)

    burst_df = df[in_burst].copy()
    if burst_df.empty:
        log.info("No data found in the open-period burst window.")
        return pd.DataFrame()

    # ── Step 3: compute rolling volatility inside burst window ───────────────
    burst_vol = calculate_volatility(burst_df, vol_win)

    # ── Step 4: session-wide average as baseline ──────────────────────────────
    session_df  = df[in_session]
    session_vol = calculate_volatility(session_df, vol_win)
    avg_vol     = session_vol.mean(skipna=True)

    if pd.isna(avg_vol) or avg_vol == 0:
        log.warning("Cannot compute average volatility for threshold calculation.")
        return pd.DataFrame()

    threshold = mult * avg_vol
    high_vol_mask = burst_vol > threshold

    # ── Step 5: collapse consecutive flags into intervals ─────────────────────
    return _collapse_boolean_series_to_intervals(
        high_vol_mask, burst_vol, "peak_volatility"
    )


def identify_low_volatility_periods(
    df: pd.DataFrame,
    config: dict = CONFIG,
) -> pd.DataFrame:
    """
    Identify periods of sustained low volatility across the full session.

    Algorithm
    ---------
    1. Calculate rolling volatility over the full dataset.
    2. Flag timestamps where volatility < LOW_VOLATILITY_THRESHOLD_MULTIPLIER
       × overall average volatility.
    3. Collapse consecutive flagged timestamps into contiguous intervals.

    Returns
    -------
    pd.DataFrame
        Columns: period_start, period_end, duration_seconds, min_volatility
    """
    if df.empty:
        return pd.DataFrame()
    if not df.index.is_unique:
        df = df.groupby(level=0).max()

    mult    = config["LOW_VOLATILITY_THRESHOLD_MULTIPLIER"]
    vol_win = config["VOLATILITY_WINDOW_SECONDS"]

    vol_series = calculate_volatility(df, vol_win)
    avg_vol    = vol_series.mean(skipna=True)

    if pd.isna(avg_vol) or avg_vol == 0:
        log.warning("Cannot compute average volatility for threshold calculation.")
        return pd.DataFrame()

    threshold    = mult * avg_vol
    low_vol_mask = vol_series < threshold

    return _collapse_boolean_series_to_intervals(
        low_vol_mask, vol_series, "min_volatility", agg_fn="min"
    )

def calculate_hourly_metrics(
    df: pd.DataFrame,
    config: dict = CONFIG,
) -> pd.DataFrame:
    """
    Compute average volatility and tick density broken down by hour-of-day
    for a single day (or any contiguous slice of tick data).

    Method
    ------
    1. Compute the rolling volatility and tick-density series on the full df
       (same windows used everywhere else in the pipeline).
    2. Extract the hour label from the DatetimeIndex.
    3. Group by hour and take the mean of both series.

    Returns
    -------
    pd.DataFrame
        Index   : hour (int, 0-23)
        Columns : avg_volatility, avg_tick_density, tick_count
    """
    if df.empty or "price" not in df.columns:
        return pd.DataFrame(
            columns=["avg_volatility", "avg_tick_density", "tick_count"]
        )

    vol_series     = calculate_volatility(df, config["VOLATILITY_WINDOW_SECONDS"])
    density_series = calculate_tick_density(df, config["TICK_DENSITY_WINDOW_SECONDS"])

    combined = pd.DataFrame({
        "volatility":   vol_series,
        "tick_density": density_series,
    }, index=df.index)
    combined["hour"] = combined.index.hour

    hourly = combined.groupby("hour").agg(
        avg_volatility  =("volatility",   "mean"),
        avg_tick_density=("tick_density", "mean"),
        tick_count      =("volatility",   "count"),   # non-NaN ticks per hour
    )
    hourly.index.name = "hour"
    return hourly


def summarise_hourly_metrics(
    daily_frames: list[pd.DataFrame],
) -> pd.DataFrame:
    """
    Aggregate a list of per-day hourly metric DataFrames into a single
    cross-day summary.

    Each hour's summary values are weighted by the number of non-NaN ticks
    that contributed to them so that thin hours (e.g. partial trading days)
    don't skew the average equally with full hours.

    Parameters
    ----------
    daily_frames : list of DataFrames returned by calculate_hourly_metrics,
                   each optionally carrying a 'date' column added by the caller.

    Returns
    -------
    pd.DataFrame
        Index   : hour (int, 0-23)
        Columns : avg_volatility, avg_tick_density,
                  total_tick_count, days_observed
    """
    if not daily_frames:
        return pd.DataFrame()

    combined = pd.concat(daily_frames)          # rows: (date × hour) pairs

    # Weighted mean: weight each day's hourly average by its tick_count
    def weighted_mean(group: pd.DataFrame, col: str) -> float:
        w = group["tick_count"]
        total_w = w.sum()
        if total_w == 0:
            return float("nan")
        return (group[col] * w).sum() / total_w

    summary = (
        combined.groupby("hour")
        .apply(
            lambda g: pd.Series({
                "avg_volatility":   weighted_mean(g, "avg_volatility"),
                "avg_tick_density": weighted_mean(g, "avg_tick_density"),
                "total_tick_count": g["tick_count"].sum(),
                "days_observed":    g["tick_count"].notna().sum(),
            })
        )
    )
    summary.index.name = "hour"
    return summary


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helper: boolean → interval table
# ─────────────────────────────────────────────────────────────────────────────

def _collapse_boolean_series_to_intervals(
    mask: pd.Series,
    value_series: pd.Series,
    value_col_name: str,
    agg_fn: str = "max",
) -> pd.DataFrame:
    """
    Convert a boolean mask aligned to a DatetimeIndex into a table of
    contiguous True-intervals with start, end, duration, and an aggregate
    of *value_series* within each interval.

    Parameters
    ----------
    mask : pd.Series[bool]
    value_series : pd.Series[float]
    value_col_name : str      Column name for the aggregated metric.
    agg_fn : str              'max' or 'min'.

    Returns
    -------
    pd.DataFrame
    """
    if mask.empty or not mask.any():
        return pd.DataFrame(
            columns=["period_start", "period_end",
                     "duration_seconds", value_col_name]
        )

    # Assign a group id each time the mask transitions True → False or v.v.
    groups = (mask != mask.shift()).cumsum()
    groups = groups[mask]            # keep only True groups

    records = []
    agg_func = np.max if agg_fn == "max" else np.min

    for _, grp_idx in groups.groupby(groups):
        idx        = grp_idx.index
        start      = idx[0]
        end        = idx[-1]
        duration   = (end - start).total_seconds()
        vals       = value_series.reindex(idx).dropna()
        agg_val    = agg_func(vals) if len(vals) > 0 else np.nan
        records.append({
            "period_start":    start,
            "period_end":      end,
            "duration_seconds": duration,
            value_col_name:    agg_val,
        })

    return pd.DataFrame(records)


# ══════════════════════════════════════════════════════════════════════════════
#  OUTPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _save_csv(df: pd.DataFrame, path: str, label: str) -> None:
    """Save *df* to *path*, creating parent directories as needed."""
    if df.empty:
        log.info("No data to save for '%s'.", label)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path)
    log.info("Saved %s → %s (%d rows).", label, path, len(df))


def _print_section(title: str, width: int = 72) -> None:
    print(f"\n{'─' * width}")
    print(f"  {title}")
    print(f"{'─' * width}")


def _print_avg_metrics(metrics: dict, date_str: str) -> None:
    _print_section(f"Average Metrics  [{date_str}]")
    if not metrics:
        print("  (no data)")
        return
    print(f"  Total ticks          : {metrics['total_ticks']:,}")
    print(f"  Session start        : {metrics['session_start']}")
    print(f"  Session end          : {metrics['session_end']}")
    print(f"  Duration (hours)     : {metrics['session_duration_hours']:.2f}")
    print(f"  Avg rolling vol      : {metrics['avg_volatility']:.8f}")
    print(f"  Avg tick density/win : {metrics['avg_tick_density']:.2f}")


def _print_period_table(df: pd.DataFrame, title: str) -> None:
    _print_section(title)
    if df.empty:
        print("  (none identified)")
        return
    print(df.to_string(index=False))


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATION
# ══════════════════════════════════════════════════════════════════════════════

def main(config: dict = CONFIG) -> None:
    """
    Orchestrate the full analysis pipeline.

    For each Parquet file found in CONFIG["DATA_DIR"]:
      1. Load and filter tick data for TARGET_SYMBOL.
      2. Calculate all metrics.
      3. Print results to console.
      4. Save per-day CSV outputs to OUTPUT_DIR.

    A summary CSV containing daily range metrics across all files is also
    written to OUTPUT_DIR.
    """
    data_dir   = config["DATA_DIR"]
    output_dir = config["OUTPUT_DIR"]
    symbol     = config["TARGET_SYMBOL"]

    os.makedirs(output_dir, exist_ok=True)

    # ── Discover Parquet files ────────────────────────────────────────────────
    pattern    = os.path.join(data_dir, "*.parquet")
    file_paths = sorted(glob.glob(pattern))

    # file_paths: list[str] = os.listdir(data_dir)
    # file_paths = [x for x in file_paths if x.endswith(".parquet")]

    if not file_paths:
        log.error(
            "No Parquet files found in '%s'. "
            "Update CONFIG['DATA_DIR'] and try again.", data_dir
        )
        return

    log.info("Found %d Parquet file(s) in '%s'.", len(file_paths), data_dir)

    all_range_metrics:  list[pd.DataFrame] = []
    all_hourly_metrics: list[pd.DataFrame] = []

    # ══════════════════════════════════════════════════════════════════════════
    for file_path in file_paths:
        date_str = Path(file_path).stem      # e.g. 'data_2023-01-01'
        print(f"\n{'═' * 72}")
        print(f"  Processing: {file_path}")
        print(f"{'═' * 72}")

        # ── Load ──────────────────────────────────────────────────────────────
        df = load_tick_data(file_path, symbol, config)
        if df is None:
            print(f"  ⚠  Skipped (no usable data for '{symbol}').")
            continue

        # ── Average metrics ────────────────────────────────────────────────────
        avg_metrics = calculate_average_metrics(df, config)
        _print_avg_metrics(avg_metrics, date_str)

        # ── Range metrics (daily roll-up) ─────────────────────────────────────
        range_df = calculate_range_metrics(df, config)
        _print_section(f"Daily Range Metrics  [{date_str}]")
        print(range_df.to_string() if not range_df.empty else "  (no data)")
        range_df["file"] = date_str
        all_range_metrics.append(range_df)

        # ── Tick value changes ─────────────────────────────────────────────────
        tick_changes = calculate_tick_value_change(df)
        _print_section(f"Tick Value Change Summary  [{date_str}]")
        if not tick_changes.empty:
            print(f"  Mean   abs change : {tick_changes.mean():.8f}")
            print(f"  Median abs change : {tick_changes.median():.8f}")
            print(f"  Max    abs change : {tick_changes.max():.8f}")
            print(f"  95th percentile   : {tick_changes.quantile(0.95):.8f}")
        else:
            print("  (insufficient data)")

        # ── High-volatility open periods ──────────────────────────────────────
        high_vol_periods = identify_high_volatility_open_periods(df, config)
        _print_period_table(
            high_vol_periods,
            f"High-Volatility Open Periods  [{date_str}]  "
            f"(threshold: {config['HIGH_VOLATILITY_THRESHOLD_MULTIPLIER']}× avg)"
        )

        # ── Low-volatility periods ─────────────────────────────────────────────
        low_vol_periods = identify_low_volatility_periods(df, config)
        _print_period_table(
            low_vol_periods,
            f"Low-Volatility Periods  [{date_str}]  "
            f"(threshold: {config['LOW_VOLATILITY_THRESHOLD_MULTIPLIER']}× avg)"
        )

        # ── NEW: hourly metrics for this file ─────────────────────────────
        hourly_df = calculate_hourly_metrics(df, config)
        hourly_df["date"] = date_str                 # tag with source date
        all_hourly_metrics.append(hourly_df)

        _print_section(f"Hourly Metrics  [{date_str}]")
        print(hourly_df.to_string())

        # ── Per-file CSV output ────────────────────────────────────────────────
        stem = f"{symbol}_{date_str}"
        _save_csv(
            high_vol_periods,
            os.path.join(output_dir, f"{stem}_high_vol_periods.csv"),
            "high-vol periods",
        )
        _save_csv(
            low_vol_periods,
            os.path.join(output_dir, f"{stem}_low_vol_periods.csv"),
            "low-vol periods",
        )
        _save_csv(
            hourly_df,
            os.path.join(output_dir, f"{stem}_hourly_metrics.csv"),
            "hourly metrics",
        )

        # Attach tick-change and vol stats to range_df and save
        if not range_df.empty and not tick_changes.empty:
            range_df["avg_abs_tick_change"] = tick_changes.mean()
            range_df["avg_volatility"]      = avg_metrics.get("avg_volatility")
            range_df["avg_tick_density"]    = avg_metrics.get("avg_tick_density")
        _save_csv(
            range_df,
            os.path.join(output_dir, f"{stem}_range_metrics.csv"),
            "range metrics",
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Cross-file summary
    # ══════════════════════════════════════════════════════════════════════════
    if all_range_metrics:
        summary = pd.concat(all_range_metrics)
        summary_path = os.path.join(output_dir, f"{symbol}_summary.csv")
        _save_csv(summary, summary_path, "cross-file summary")
        _print_section("MULTI-FILE SUMMARY")
        print(summary.to_string())
    else:
        log.warning("No range metrics collected – check your data and symbol name.")

    if all_hourly_metrics:
        hourly_summary = summarise_hourly_metrics(all_hourly_metrics)
        _print_section("HOURLY SUMMARY  (all files, weighted by tick count)")
        print(hourly_summary.to_string())
        _save_csv(
            hourly_summary,
            os.path.join(output_dir, f"{symbol}_hourly_summary.csv"),
            "cross-day hourly summary",
        )

    print(f"\n✓  Analysis complete. Results saved to '{output_dir}'.\n")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ── Optional: override config values from environment variables ───────────
    # Useful when running in CI/CD or containerised environments.
    if os.getenv("DATA_DIR"):
        CONFIG["DATA_DIR"] = os.environ["DATA_DIR"]
    if os.getenv("TARGET_SYMBOL"):
        CONFIG["TARGET_SYMBOL"] = os.environ["TARGET_SYMBOL"]

    # ── Demo / smoke-test with synthetic data ─────────────────────────────────
    # If no real Parquet files are present this block generates a tiny synthetic
    # dataset so you can confirm the pipeline runs end-to-end without errors.
    # Remove or comment out this section in production.

    _DEMO_DIR = os.path.join(CONFIG["DATA_DIR"])
    os.makedirs(_DEMO_DIR, exist_ok=True)
    _DEMO_FILE = os.path.join(_DEMO_DIR, "data_2024-01-15.parquet")

    if not glob.glob(os.path.join(_DEMO_DIR, "*.parquet")):
        log.info("No Parquet files found – generating synthetic demo data …")
        rng    = np.random.default_rng(seed=42)
        n      = 50_000                                     # ~14 hours of ticks
        times  = pd.date_range(
            "2024-01-15 02:00:00", periods=n, freq="1s", tz="UTC"
        )
        # Simulate a random-walk mid-price starting near 1.09 for EURUSD
        mid    = 1.09 + np.cumsum(rng.normal(0, 0.00005, n))
        spread = rng.uniform(0.00005, 0.00020, n)
        demo   = pd.DataFrame({
            "time":   times,
            "symbol": CONFIG["TARGET_SYMBOL"],
            "bid":    mid - spread / 2,
            "ask":    mid + spread / 2,
            "last":   mid + rng.normal(0, 0.00001, n),     # slight noise vs mid
        })
        demo.to_parquet(_DEMO_FILE, index=False)
        log.info("Synthetic demo file written → %s", _DEMO_FILE)

    # ── Run the pipeline ──────────────────────────────────────────────────────
    main(CONFIG)
