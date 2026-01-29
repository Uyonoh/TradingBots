import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import calendar
import pytz
import os
from pathlib import Path
import warnings
from concurrent.futures import ProcessPoolExecutor
import matplotlib.pyplot as plt

warnings.filterwarnings('ignore')

# -------------------------------------------------------------------
# 1. CONFIGURATION (Same as original)
# -------------------------------------------------------------------
PRO_SETUP = {
    "bias_filter": {"enabled": True, "buy_threshold": 0.75, "sell_threshold": 0.25},
    "entry_conditions": {"15min_buffer": 10, "velocity_multiplier": 1.5, "lookback_period": "60min"},
    "risk_management": {
        "initial_sl": [70, 90], 
        "trailing_stages": [
            {"min_profit": 0, "max_profit": 50, "retention": -1},
            #{"min_profit": 50, "max_profit": 100, "retention": -1},
            #{"min_profit": 100, "max_profit": 150, "retention": 0.5},
            #{"min_profit": 150, "max_profit": 300, "retention": 0.7},
            #{"min_profit": 300, "max_profit": 400, "retention": 0.8},
            {"min_profit": 50, "retention": -1}
        ],
        "tp_override": {"fast_threshold": 30, "slow_threshold": 180}
    },
    "session_constraints": {"entry_start": "08:15", "mandatory_close": "17:30"}
}

CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc
UTC2 = pytz.timezone('Europe/Athens') # EET / CAT
UTC3 = pytz.timezone('Asia/Baghdad') # EAT / MST

def get_server_timezone(year=None, month=None, day=None):
    """
    Returns the current server time based on:
    Winter: GMT+2
    Summer: GMT+3 (Last Sunday March to Last Sunday October)
    """
    if (year is None) or (month is None) or (day is None):
        now_utc = datetime.now(timezone.utc)
        year = now_utc.year
    else:
        now_utc = datetime(year, month, day, tzinfo=timezone.utc)

    # Helper to find the last Sunday of a given month
    def last_sunday(year, month):
        # Get the last day of the month
        last_day = calendar.monthrange(year, month)[1]
        dt = datetime(year, month, last_day, tzinfo=timezone.utc)
        # Weekday 6 is Sunday in Python's weekday()
        offset = (dt.weekday() + 1) % 7 
        return dt - timedelta(days=offset)

    # DST boundaries (usually starts/ends at 01:00 UTC for EU-style rules)
    dst_start = last_sunday(year, 3).replace(hour=1)
    dst_end = last_sunday(year, 10).replace(hour=1)

    # Determine offset: Summer (GMT+3) if within range, else Winter (GMT+2)
    if dst_start <= now_utc < dst_end:
        offset_hours = 3
        zone = UTC3
    else:
        offset_hours = 2
        zone = UTC2

    server_time = now_utc + timedelta(hours=offset_hours)
    return zone

# -------------------------------------------------------------------
# 2. VECTORIZED SIGNAL ENGINE
# -------------------------------------------------------------------

class SignalPrecomputer:
    """Computes all technical signals in bulk to avoid O(N^2) complexity."""
    
    @staticmethod
    def compute_velocity(df, multiplier, lookback):
        # Resample to 1S, calculate rolling density
        counts = df.resample('1S').size()
        density = counts.rolling('30S', min_periods=1).sum() / 30
        rolling_avg = density.rolling(lookback, min_periods=1).mean()
        signal = density > (rolling_avg * multiplier)
        # Reindex back to tick level
        return signal.reindex(df.index, method='ffill').fillna(False).values

    @staticmethod
    def get_daily_bias(df, buy_t, sell_t):
        mid = (df['bid'] + df['ask']) / 2
        # Group by CET Date
        days = mid.groupby(mid.index.tz_convert(CET).date)
        
        def calc_rc(x):
            h, l, c = x.max(), x.min(), x.iloc[-1]
            return (c - l) / (h - l) if not h == l else 0.5
            
            
        rc = days.apply(calc_rc)
        # 4. Vectorised Bias Mapping (2026 Standard)
        conditions = [
            (rc >= buy_t),
            (rc <= sell_t)
        ]
        choices = ["buy", "sell"]
        
        # default="straddle" handles the 'else' case
        biases = pd.Series(np.select(conditions, choices, default="straddle"), index=rc.index)
        
        return biases.to_dict()

    @staticmethod
    def get_ghost_ranges(df):
        df_cet = df.copy()
        df_cet.index = df_cet.index.tz_convert(CET)
        ghost_data = df_cet.between_time("08:00", "08:15")
        mid = (ghost_data['bid'] + ghost_data['ask']) / 2
        ranges = mid.groupby(mid.index.date).agg(['min', 'max'])
        return ranges.to_dict('index')

# -------------------------------------------------------------------
# 3. HIGH-SPEED ENGINE (NUMPY CORE)
# -------------------------------------------------------------------

def process_chunk_parallel(year, month, config):
    """
    Worker function for parallel execution.
    Handles data loading and the high-speed tick loop.
    """
    path = Path(f"./dax_ticks/GER40_{year}_{month:02d}.parquet")
    if not path.exists(): 
        print(f"File not found: {path}")
        return []
    
    df = pd.read_parquet(path)
    zone = get_server_timezone(year, month, 1)
    # print(f"Server timezone: {zone}")
    df.index = pd.to_datetime(df.index).tz_localize(zone)
    
    # 1. Precompute Signals (Vectorized)
    biases = SignalPrecomputer.get_daily_bias(df, config['bias_filter']['buy_threshold'], config['bias_filter']['sell_threshold'])
    ghost_ranges = SignalPrecomputer.get_ghost_ranges(df)
    velocity_signals = SignalPrecomputer.compute_velocity(
        df, config['entry_conditions']['velocity_multiplier'], config['entry_conditions']['lookback_period']
    )

    # 2. Prepare NumPy arrays for the loop
    # This is where the magic happens for performance
    times = df.index.tz_convert(zone).tz_localize(None).values
    bids = df['bid'].values
    asks = df['ask'].values
    mids = (bids + asks) / 2.0
    
    # Pre-extract time components to avoid calling .hour/.minute in loop
    df_cet_idx = df.index.tz_convert(CET)
    hours = df_cet_idx.hour
    minutes = df_cet_idx.minute
    dates = df_cet_idx.date
    
    trades = []
    in_trade = False
    day_traded = None
    
    # Trade State variables
    entry_p = 0.0
    entry_t = None
    direction = None
    max_pnl = 0.0
    touched_opposite = False # For 15-min trap logic
    
    sl_initial = config['risk_management']['initial_sl'][0]
    stages = config['risk_management']['trailing_stages']
    buffer = config['entry_conditions']['15min_buffer']
    
    
    
    for i in range(len(mids)):
        curr_date = dates[i]
        curr_time = times[i]
        curr_mid = mids[i]
        curr_bid = bids[i]
        curr_ask = asks[i]
        
        # New Day Reset
        if curr_date == day_traded:
            continue
            
        

        # Session Constraints
        h, m = hours[i], minutes[i]
        is_entry_window = (h == 8 and m >= 15) or (8 < h < 17) or (h == 17 and m < 30)
        is_close_time = (h == 17 and m >= 30)
        is_spread_wide = (h == 8 and m < 5) or (h == 17 and m > 25)
        
        bias_str = biases.get(curr_date, 'straddle')
        if bias_str == 'straddle': continue
        
        ghost = ghost_ranges.get(curr_date)
        if not ghost: continue
        g_low, g_high = ghost['min'], ghost['max']

        # EXIT LOGIC
        if in_trade:
            # Mandatory Close
            if is_close_time:
                pnl = (curr_bid - entry_p) if direction == 'buy' else (entry_p - curr_ask)
                trade = {'date': curr_date, 'entry_time': entry_t, 'entry_price': entry_p, 'exit_time': curr_time, ' exit_price': curr_bid if direction == 'buy' else curr_ask,
                'direction': direction, 'profit_ticks': pnl, 'reason': 'Mandatory close'}
                trades.append(trade)
                
                print(trade)
                # print(df.iloc[[i]])
                
                in_trade = False
                touched_opposite = False
                day_traded = curr_date
                continue
            
            # Trailing Stop Calculation
            pnl = (curr_mid - entry_p) if direction == 'buy' else (entry_p - curr_mid)
            max_pnl = max(max_pnl, pnl)
            
            # Dynamic Retention
            retention = 0.6 # default
            for s in stages:
                if max_pnl >= s['min_profit']:
                    if 'max_profit' not in s or max_pnl <= s['max_profit']:
                        retention = s['retention']
            
            if retention == -1:
                stop_level = (entry_p - sl_initial) if direction == 'buy' else (entry_p + sl_initial)
            else:
                trail_dist = max_pnl * retention
                stop_level = (entry_p + trail_dist) if direction == 'buy' else (entry_p - trail_dist)
            
            # Check Stop Hit
            if (direction == 'buy' and curr_bid <= stop_level) or (direction == 'sell' and curr_ask >= stop_level):
                trade = {'date': curr_date, 'entry_time': entry_t, 'entry_price': entry_p, 'exit_time': curr_time, ' exit_price': curr_bid if direction == 'buy' else curr_ask,
                'direction': direction, 'profit_ticks': pnl, 'reason': 'stop', 'retention': retention}
                trades.append(trade)
                
                print(trade)
                # print(df.iloc[[i]])

                in_trade = False
                touched_opposite = False
                #day_traded = curr_date
                continue
                
        # ENTRY LOGIC
        elif is_entry_window and not is_spread_wide:
            if bias_str == 'buy':
                if curr_ask <= g_low + buffer: touched_opposite = True
                
                if touched_opposite and curr_ask >= g_high - buffer and velocity_signals[i]:
                    in_trade, direction, entry_p, entry_t, max_pnl = True, 'buy', curr_bid, curr_time, 0.0
                    # print("Entered buy")
                    # print(df.iloc[[i]])
            elif bias_str == 'sell':
                if curr_bid >= g_high - buffer: touched_opposite = True
                
                if touched_opposite and curr_bid <= g_low + buffer and velocity_signals[i]:
                    in_trade, direction, entry_p, entry_t, max_pnl = True, 'sell', curr_ask, curr_time, 0.0
                    # print("Entered sell")
                    # print(df.iloc[[i]])

    return trades
    
def get_lot(equity):
    lot_maps = {
        0: 0.04, # 1.4
        15: 0.05, # 2.8
        20: 0.07, # 4.2
        30: 0.11, # 7
        50: 0.18, # 14
        100: 0.36, # 28
        200: 0.71, # 70
        500: 1.79, # 105
        1000: 3.57,
        5000: 17.86,
    }
    
    for lot in lot_maps:
            if equity >= lot:
                lot_size = lot_maps[lot]
            else:
                break
    return lot_size
    
def calculate_equity(trades):
    # Starting balance
    current_equity = 10.0
    
    # 1. Create columns if they don't exist
    trades['pnl'] = 0.0
    trades['equity'] = 0.0
    
    # 2. Sequential calculation (Equity affects Lot Size)
    for index, row in trades.iterrows():
        # Get lot size based on current balance
        lot_size = get_lot(current_equity)
        
        # Calculate profit for this trade
        # Note: 'profit_ticks' must be pre-calculated in your df
        trade_pnl = row['profit_ticks'] * lot_size
        
        # Update equity
        current_equity += trade_pnl
        
        # Write back to DataFrame using .at for speed
        trades.at[index, 'pnl'] = trade_pnl
        trades.at[index, 'equity'] = current_equity
        
    return trades

                

# -------------------------------------------------------------------
# 4. MAIN EXECUTION & VISUALIZATION
# -------------------------------------------------------------------
class DAXTickEngine:
    def __init__(self, config=PRO_SETUP):
        self.config = config

    def run_backtest(self, start_year=2025, start_month=1, end_year=2025, end_month=12):
        tasks = []
        for y in range(start_year, end_year + 1):
            for m in range(1, 13):
                # Skip months before start_month in the first year
                if y == start_year and m < start_month:
                    continue
                # Skip months after end_month in the last year
                if y == end_year and m > end_month:
                    continue
                    
                tasks.append((y, m))


        all_trades = []
        print(tasks)
        # Use 3 workers to stay safe with 8GB RAM/i5
        cores = os.cpu_count()
        use_cores = cores - 1
        print(f"Starting parallel engine on {cores} cores (Limited to {use_cores} for RAM safety)...")
        with ProcessPoolExecutor(max_workers=use_cores) as executor:
            futures = [executor.submit(process_chunk_parallel, y, m, self.config) for y, m in tasks]
            for f in futures:
                all_trades.extend(f.result())
        
        return pd.DataFrame(all_trades)

def plot_results(trades):
    import matplotlib.dates as mdates
    if trades.empty: 
        print("No trades to plot.")
        return
    #trades['equity'] = trades['profit_ticks'].cumsum()
    
    
    plt.figure(figsize=(12, 6))
    x_dates = trades['date']
    x = [datetime.combine(d, datetime.min.time()) for d in x_dates]
    y = trades['equity']
    plt.plot(x, y, color='#2ecc71', linewidth=2)
    plt.title('DAX Institutional Momentum - Equity Curve (Ticks)', fontsize=14)
    plt.xlabel('Date')
    plt.ylabel('Cumulative Profit (Ticks)')
    plt.grid(True, alpha=0.3)
    plt.show()

if __name__ == "__main__":
    engine = DAXTickEngine(PRO_SETUP)
    # Example: Run for 2025
    results = engine.run_backtest(2026, 1, 2026, 1)
    
    if not results.empty:
        results = calculate_equity(results)
        # results['server_entry_time'] = results['entry_time'].dt.tz_convert(get_server_timezone())
        # results['server_exit_time'] = results['exit_time'].dt.tz_convert(get_server_timezone())
        entry_time = pd.to_datetime(results['entry_time']).dt.tz_localize(get_server_timezone())
        results['server_entry_time'] = entry_time.dt.tz_convert(CET)
        results.to_csv("DAX_test.csv")
        win_rate = (results['profit_ticks'] > 0).mean() * 100

        print(f"Backtest Complete.")
        print(f"Total Trades: {len(results)}")
        print(f"Win Rate: {win_rate:.2f}%")
        print(f"Total Profit: $ {results['pnl'].sum():.2f}")
        print(f"Min profit: $ {results['pnl'].min()}")
        print(f"Max profit: $ {results['pnl'].max()}")
        print(f"Avg profit: $ {results['pnl'].mean()}")
        
        plot_results(results)
    else:
        print("No results")
    # input()