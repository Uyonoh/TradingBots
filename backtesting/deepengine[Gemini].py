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
            {"min_profit": 50, "max_profit": 100, "retention": -1},
            {"min_profit": 100, "max_profit": 150, "retention": 0.4},
            {"min_profit": 150, "max_profit": 300, "retention": 0.7},
            {"min_profit": 300, "max_profit": 400, "retention": 0.8},
            {"min_profit": 400, "retention": -1}
        ],
        "tp_override": {"fast_threshold": 30, "slow_threshold": 180}
    },
    "session_constraints": {"entry_start": "08:15", "mandatory_close": "17:30"}
}

OP_CONFIG = {'bias_filter': {'enabled': True, 'buy_threshold': 0.75, 'sell_threshold': 0.25}, 
             'entry_conditions': {
                 '15min_buffer': 10, 
                 'velocity_multiplier': 1.5, 
                 'lookback_period': '60min'
                 }, 
             'risk_management': {
                 'initial_sl': [80, 80], 
                 'trailing_stages': [
                     {'min_profit': 0, 'max_profit': np.int64(40), 'retention': -1}, 
                     {'min_profit': np.int64(40), 'max_profit': np.int64(80), 'retention': 0.86}, 
                     {'min_profit': np.int64(80), 'max_profit': np.int64(120), 'retention': 0.95}, 
                     {'min_profit': np.int64(120), 'max_profit': np.int64(160), 'retention': 0.95}, 
                     {'min_profit': np.int64(160), 'max_profit': np.int64(200), 'retention': 0.95}, 
                     {'min_profit': np.int64(200), 'retention': 0.95}
                     ], 
                     'tp_override': {'fast_threshold': np.int64(56), 'slow_threshold': np.int64(295)}}, 
             'session_constraints': {'entry_start': '06:30', 'mandatory_close': '17:00'}}
OP_CONFIG = PRO_SETUP
# OP_CONFIG = {'bias_filter': {'enabled': True, 'buy_threshold': 0.75, 'sell_threshold': 0.25}, 
#              'entry_conditions': {
#                  '15min_buffer': 15, 
#                  'velocity_multiplier': 1.3385697777045447, 
#                  'lookback_period': '60min'
#                  }, 
#              'risk_management': {
#                  'initial_sl': [80, 80], 
#                  'trailing_stages': [
#                      {'min_profit': 0, 'max_profit': np.int64(40), 'retention': -1}, 
#                      {'min_profit': np.int64(40), 'max_profit': np.int64(80), 'retention': 0.86}, 
#                      {'min_profit': np.int64(80), 'max_profit': np.int64(120), 'retention': 0.95}, 
#                      {'min_profit': np.int64(120), 'max_profit': np.int64(160), 'retention': 0.95}, 
#                      {'min_profit': np.int64(160), 'max_profit': np.int64(200), 'retention': 0.95}, 
#                      {'min_profit': np.int64(200), 'retention': 0.95}
#                      ], 
#                      'tp_override': {'fast_threshold': np.int64(56), 'slow_threshold': np.int64(295)}}, 
#              'session_constraints': {'entry_start': '06:43', 'mandatory_close': '16:29'}}

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
def plot_bias_comparison(calc_biases, real_biases):
    # Convert dictionaries to a single DataFrame for easy plotting
    print(calc_biases)
    print(real_biases)
    df_compare = pd.DataFrame({
        'Calculated': pd.Series(calc_biases),
        'Real': pd.Series(real_biases)
    }).dropna()

    # Map categories to numbers for the Y-axis
    mapping = {"buy": 1, "straddle": 0, "sell": -1}
    df_compare['calc_num'] = df_compare['Calculated'].map(mapping)
    df_compare['real_num'] = df_compare['Real'].map(mapping)

    plt.figure(figsize=(12, 6))
    
    # Plot Real Bias as a background step line
    plt.step(df_compare.index, df_compare['real_num'], where='post', 
             label='Real Daily Bias', alpha=0.3, color='gray', linestyle='--')
    
    # Plot Calculated Bias as points
    # Green for correct buy, Red for correct sell, Blue for others
    correct = df_compare['Calculated'] == df_compare['Real']
    plt.scatter(df_compare.index[correct], df_compare['calc_num'][correct], 
                color='green', label='Correct Prediction', zorder=5)
    plt.scatter(df_compare.index[~correct], df_compare['calc_num'][~correct], 
                color='red', label='Incorrect Prediction', zorder=5)

    plt.yticks([-1, 0, 1], ['Sell', 'Straddle', 'Buy'])
    plt.title("Calculated Bias vs. Real Market Direction")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.show()
    
    # Calculate accuracy
    accuracy = (df_compare['Calculated'] == df_compare['Real']).mean() * 100
    print(f"Bias Prediction Accuracy: {accuracy:.2f}%")

class SignalPrecomputer:
    @staticmethod
    def compute_velocity(df, multiplier, lookback):
        """
        Optimized for 8GB RAM: Resampling can be heavy. 
        We use 'limit' to prevent memory bloat during reindexing.
        """
        # Calculate tick density per second
        counts = df.resample('1S').size()
        density = counts.rolling(window=30, min_periods=1).mean() # 30s density
        
        rolling_avg = density.rolling(window=lookback, min_periods=1).mean()
        signal_s = density > (rolling_avg * multiplier)
        
        # Reindex back to tick level efficiently
        return signal_s.reindex(df.index, method='ffill').fillna(False).values

    @staticmethod
    def get_market_levels(df):
        """
        Extracts PDH, PDL, and Asian Range (00:00-08:00 CET).
        Essential for GER40 'Judas Swing' detection.
        """
        # Ensure index is CET for session logic
        # print("="*80)
        # print("LEVELS")
        # print("="*80)
        df_cet = df.copy()
        if df_cet.index.tz is None:
            df_cet.index = df_cet.index.tz_localize('UTC').tz_convert('CET')
        
        # 1. Previous Day High/Low
        daily = df_cet['bid'].resample('D').agg(['max', 'min'])
        levels = daily.shift(1) # We need yesterday's levels for today
        levels.columns = ['pdh', 'pdl']
        levels.index = levels.index.date
        # print(f"{levels=}")

        # 2. Asian Range (00:00 - 08:00 CET)
        asian_data = df_cet.between_time("00:00", "08:00")
        asian_range = asian_data['bid'].groupby(asian_data.index.date).agg(['max', 'min'])
        asian_range.columns = ['asian_h', 'asian_l']
        # print(f"{asian_range=}")
        
        # Combine levels
        combined = pd.concat([levels, asian_range], axis=1)
        # combined.index = pd.to_datetime(combined.index).date
        filled = combined.ffill().bfill()
        return filled.to_dict('index')
    
    @staticmethod
    def get_biases(df, buy_t, sell_t, shift=True):
        """
        Enhanced Bias: Combines Relative Close (RC) with 
        Previous Day Range position.
        """
        mid = (df['bid'] + df['ask']) / 2
        days = mid.groupby(mid.index.date)
        
        def calc_advanced_bias(x):
            h, l, c = x.max(), x.min(), x.iloc[-1]
            if h == l: return 0.5
            
            # Relative Close (Your original logic)
            rc = (c - l) / (h - l)
            
            # Mechanical Rule: If we close in the top 25%, bias is Bullish
            if rc >= buy_t: return 1  # Buy
            if rc <= sell_t: return -1 # Sell
            return 0 # Straddle

        daily_val = days.apply(calc_advanced_bias)
        
        # Shift to apply yesterday's result to today's trading
        if shift:
            daily_val = daily_val.shift(1).fillna(0)
        return daily_val
    
    def get_real_bias(df):
        # Resample to daily OHLC
        daily = df['bid'].resample('D').ohlc().dropna()
        daily.index = daily.index.date
        
        # Real bias: Bullish if Close > Open, Bearish if Close < Open
        conditions = [
            (daily['close'] > daily['open']),
            (daily['close'] < daily['open'])
        ]
        choices = ["buy", "sell"]
        
        daily['real_bias'] = np.select(conditions, choices, default="straddle")
        return daily['real_bias'].to_dict()

    @staticmethod
    def get_daily_bias(df, buy_t, sell_t):
        
        """
        Enhanced Bias: Combines Relative Close (RC) with 
        Previous Day Range position.
        """
        mid = (df['bid'] + df['ask']) / 2
        days = mid.groupby(mid.index.date)
        
        def calc_advanced_bias(x):
            o, h, l, c = x.iloc[0], x.max(), x.min(), x.iloc[-1] # (10 1) (2,4), (9,7)
            if h == l: return 0.5
            
            # Relative Close (Your original logic)
            rc = (c - l) / (h - l)
            
            # Mechanical Rule: If we close in the top 25%, bias is Bullish
            if rc >= buy_t: return 1  # Buy
            if rc <= sell_t: return -1 # Sell
            return 0 # Straddle

        daily_val = days.apply(calc_advanced_bias)
        
        # Shift to apply yesterday's result to today's trading
        bias_map = daily_val.shift(1).fillna(0)
        mapping = {1: "buy", -1: "sell", 0: "straddle"}
        return {k: mapping[v] for k, v in bias_map.items()}

    @staticmethod
    def get_ghost_ranges(df):
        # 08:00-08:15 is the Frankfurt Pre-Market 'Ghost' range
        df_cet = df.copy()
        if df_cet.index.tz is None:
            df_cet.index = df_cet.index.tz_localize('UTC').tz_convert('CET')
            
        ghost_data = df_cet.between_time("08:00", "08:15")
        mid = (ghost_data['bid'] + ghost_data['ask']) / 2
        return mid.groupby(mid.index.date).agg(['min', 'max']).to_dict('index')

# -------------------------------------------------------------------
# 3. HIGH-SPEED ENGINE (NUMPY CORE)
# -------------------------------------------------------------------
def determine_execution_bias(bid, time_cet, levels, daily_rc_bias):
    """
    Refines the Daily RC bias with real-time Liquidity Sweeps.
    levels: dict containing 'asian_h', 'asian_l', 'pdh', 'pdl'
    daily_rc_bias: 'buy', 'sell', or 'straddle' from your previous method
    """
    price = bid #current_tick['bid']
    # time_cet = current_tick['time_cet'] # Assuming you've handled the TZ
    
    # 1. Initialize with your pre-computed Daily RC bias
    if daily_rc_bias == "sell":
        refined_bias = "buy"
    elif daily_rc_bias == "buy":
        refined_bias = "sell"
    
    # 2. Check for Judas Swing (08:00 - 09:30 CET)
    # This is the 'Manipulation' phase of the GER40
    is_london_open = "08:00" <= time_cet.strftime("%H:%M") <= "09:30"
    
    if is_london_open:
        # print("ASIAN")
        # print(levels)
        # Manipulation Move: Sweep High, then break lower = Bearish Bias
        if price > levels['asian_h']:
            # We are sweeping liquidity above the Asian Range
            # If your RC bias was 'buy', this is a warning (Potential Fakeout)
            refined_bias = "potential_sell_sweep" 
            
        elif price < levels['asian_l']:
            # We are sweeping liquidity below the Asian Range
            refined_bias = "potential_buy_sweep"

    return refined_bias

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
    levels = SignalPrecomputer.get_market_levels(df)
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

    start = config['session_constraints']['entry_start']
    start_h, start_m = [int(t) for t in start.split(":")]
    end = config['session_constraints']['mandatory_close']
    end_h, end_m = [int(t) for t in end.split(":")]
    
    
    for i in range(len(mids)):
        curr_date = dates[i]
        curr_time = times[i]
        curr_mid = mids[i]
        curr_bid = bids[i]
        curr_ask = asks[i]
        
        # New Day Reset
        if curr_date == day_traded:
            continue
            

        # Get pre-computed levels for today
        current_levels = levels.get(curr_date)
        # print(levels)
        # print(f"Today = {curr_date}")
        # print(current_levels)
        # raise ValueError()
        rc_bias = biases.get(curr_date, "straddle")
        
        

        # Session Constraints
        h, m = hours[i], minutes[i]
        is_entry_window = (h == 8 and m >= 15) or (8 < h < 17) or (h == 17 and m < 30)
        is_close_time = (h == 17 and m >= 30)
        is_spread_wide = (h == 8 and m < 5) or (h == 17 and m > 25)
        # is_entry_window = (h == start_h and m >= start_m) or (start_h < h < end_h) or (h == end_h and m < end_m)
        # is_close_time = (h == end_h and m >= end_m)
        # is_spread_wide = (h == 8 and m < 5) or (h == 17 and m > 25)
        
        bias_str = biases.get(curr_date, 'straddle')
        if bias_str == 'straddle': continue
        # Update bias based on session price action
        active_bias = determine_execution_bias(curr_bid, df_cet_idx[i], current_levels, rc_bias)
        # bias_str = active_bias
        
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
    lot_size= 0.01
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

    # First row with 0 equity
    new_row = trades.iloc[[0]].copy()
    new_row.at[new_row.index[0], 'profit_ticks'] = 0.0
    # new_row.index = [new_row.index[0] - pd.Timedelta(hours=1)]
    trades = pd.concat([new_row, trades], ignore_index=True)

    
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
    
    
    # trades = pd.concat([t1, trades[:1], trades[1:]])
        
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

def optimize_trails():
    profits = range(100, 101, 50)
    rs = range(1, 10)
    retentions = [{"min_profit": p, "retention": r/10} for p in profits for r in rs]
    optimization_results = {}

    for retention in retentions:
        ret = [{"min_profit": 0, "max_profit": retention["min_profit"], "retention": -1}] + [retention]
        PRO_SETUP['risk_management']['trailing_stages'] = ret

        engine = DAXTickEngine(PRO_SETUP)
        results = engine.run_backtest(2025, 1, 2025, 3)
        results = calculate_equity(results)
        print(ret)
        print(f"Total Profit: $ {results['pnl'].sum():.2f}")
        optimization_results[retention["retention"]] = results['pnl'].sum()
    
    print(optimization_results)


# if __name__ == "__main__":
    # engine = DAXTickEngine(PRO_SETUP)
    # # Example: Run for 2025
    # results = engine.run_backtest(2025, 1, 2026, 1)
    
    # if not results.empty:
    #     results = calculate_equity(results)
    #     # results['server_entry_time'] = results['entry_time'].dt.tz_convert(get_server_timezone())
    #     # results['server_exit_time'] = results['exit_time'].dt.tz_convert(get_server_timezone())
    #     entry_time = pd.to_datetime(results['entry_time']).dt.tz_localize(get_server_timezone())
    #     results['server_entry_time'] = entry_time.dt.tz_convert(CET)
    #     results.to_csv("DAX_test.csv")
    #     win_rate = (results['profit_ticks'] > 0).mean() * 100

    #     print(f"Backtest Complete.")
    #     print(f"Total Trades: {len(results)}")
    #     print(f"Win Rate: {win_rate:.2f}%")
    #     print(f"Total Profit: $ {results['pnl'].sum():.2f}")
    #     print(f"Min profit: $ {results['pnl'].min()}")
    #     print(f"Max profit: $ {results['pnl'].max()}")
    #     print(f"Avg profit: $ {results['pnl'].mean()}")
        
    #     plot_results(results)
    # else:
    #     print("No results")
    
    # optimize_trails()

from skopt import gp_minimize
from skopt.space import Integer, Real
from skopt.plots import plot_convergence, plot_objective
import numpy as np

# -------------------------------------------------------------------
# DYNAMIC CONFIG GENERATOR
# -------------------------------------------------------------------

def create_dynamic_config(params):
    """
    Converts the flat list of optimization parameters into the PRO_SETUP dict.
    """
    (sl, buf, vel, tp_fast, tp_slow, start_off, end_off, 
     p_step, r_base, r_inc) = params

    # Convert offsets to HH:MM strings
    start_time = (datetime(2025, 1, 1, 6, 0) + timedelta(minutes=int(start_off))).strftime("%H:%M")
    end_time = (datetime(2025, 1, 1, 16, 0) + timedelta(minutes=int(end_off))).strftime("%H:%M")

    # Generate 5 Trailing Stages based on Profile
    # Stage 0 is always retention -1 (Initial SL)
    stages = [{"min_profit": 0, "max_profit": p_step, "retention": -1}]
    for i in range(1, 5):
        m_profit = p_step * i
        # Clamp retention between 0.1 and 0.95
        retention = min(0.95, r_base + (i * r_inc))
        stages.append({
            "min_profit": m_profit, 
            "max_profit": m_profit + p_step, 
            "retention": round(retention, 2)
        })
    # Final 'runner' stage
    stages.append({"min_profit": p_step * 5, "retention": 0.95})

    config = {
        "bias_filter": {"enabled": True, "buy_threshold": 0.75, "sell_threshold": 0.25},
        "entry_conditions": {"15min_buffer": buf, "velocity_multiplier": vel, "lookback_period": "60min"},
        "risk_management": {
            "initial_sl": [sl, sl],
            "trailing_stages": stages,
            "tp_override": {"fast_threshold": tp_fast, "slow_threshold": tp_slow}
        },
        "session_constraints": {"entry_start": start_time, "mandatory_close": end_time}
    }
    return config

# -------------------------------------------------------------------
# OBJECTIVE FUNCTION
# -------------------------------------------------------------------

def full_objective(params):
    current_config = create_dynamic_config(params)
    engine = DAXTickEngine(current_config)
    
    # We optimize on a 4-month window for speed on i5/8GB RAM
    results = engine.run_backtest(2025, 1, 2025, 6) 
    
    if results.empty or len(results) < 10:
        return 0.0 # Penalty for no activity
    
    # Calculate Risk-Adjusted Return
    returns = results['profit_ticks']
    
    # Using the formula for Sharpe Ratio:
    # $$S = \frac{E[R_p - R_f]}{\sigma_p}$$
    # Here we simplify to Mean Profit / Std Dev
    sharpe = returns.mean() / (returns.std() + 1e-6)
    
    # Add a small penalty for Max Drawdown to keep it "stable"
    cumulative = returns.cumsum()
    max_dd = (cumulative.expanding().max() - cumulative).max()
    score = sharpe - (max_dd * 0.001) 
    
    return -score

def plot_trailing_logic(config):
    stages = config['risk_management']['trailing_stages']
    profits = [s.get('min_profit', 0) for s in stages]
    retention = [s['retention'] if s['retention'] != -1 else 0 for s in stages]
    
    plt.step(profits, retention, where='post', color='orange', label='Retention Rate')
    plt.title("Optimized Trailing Stop Profile")
    plt.xlabel("Profit (Ticks)")
    plt.ylabel("Retention (0.0 - 1.0)")
    plt.grid(True, alpha=0.3)
    plt.show()

# -------------------------------------------------------------------
# RUN OPTIMIZATION
# -------------------------------------------------------------------

def run_pro_optimization():
    space = [
        Integer(50, 120, name='initial_sl'),
        Integer(5, 20, name='buffer_ticks'),
        Real(1.2, 2.2, name='velocity_multiplier'),
        Integer(15, 60, name='tp_fast'),        # Fast TP override mins
        Integer(120, 300, name='tp_slow'),      # Slow TP override mins
        Integer(0, 240, name='start_offset'),   # Mins after 06:00
        Integer(0, 180, name='end_offset'),     # Mins after 16:00
        Integer(30, 100, name='profit_step'),   # Ticks per stage
        Real(0.3, 0.7, name='retention_base'),  # Starting retention
        Real(0.05, 0.2, name='retention_inc')   # How much retention grows per stage
    ]
    
    print("💎 Starting Professional Parameter Search...")
    res = gp_minimize(full_objective, space, n_calls=50, random_state=42, verbose=True)
    
    best_cfg = create_dynamic_config(res.x)
    print("\n✅ OPTIMIZATION COMPLETE")
    print(f"Best Session: {best_cfg['session_constraints']['entry_start']} to {best_cfg['session_constraints']['mandatory_close']}")
    print(f"Best TP Overrides: {best_cfg['risk_management']['tp_override']}")
    print(f"Best Trailing Stages: {best_cfg['risk_management']['trailing_stages']}")
    print("="*80)
    print("BEST CoNFIG")
    print(best_cfg)
    print("="*80)
    
    return res, best_cfg

if __name__ == "__main__":
    # Choose your path:
    choice = input("Enter 'B' for Backtest or 'O' for Optimize: ").upper()
    
    if choice == 'O':
        res, best_params = run_pro_optimization()

        # 1. Convergence Plot: Shows how the AI learned over time
        plt.figure(figsize=(10, 5))
        plot_convergence(res)
        plt.title("Optimizer Convergence (Finding the Peak)")
        plt.show()
        
        # 2. Objective Plot: Shows the 'heat map' of which parameters worked best
        # This helps you identify if a parameter is too sensitive
        plot_objective(res)
        plt.show()

        plot_trailing_logic(best_params)
        # Update your PRO_SETUP with best_params here if you want to run a final test
    else:
        engine = DAXTickEngine(OP_CONFIG)
        # Example: Run for 2025
        results = engine.run_backtest(2025, 1, 2025, 6)
        
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