import pandas as pd
import numpy as np
import datetime as DT
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
# Make diff vel multipliers and lookbacks for the different opens/times
SYMBOL = "GER40" #"#BTCUSD"
PRO_SETUP = {
    "bias_filter": {"enabled": True, "buy_threshold": 0.65, "sell_threshold": 0.35},
    "entry_conditions": {"15min_buffer": 10, "velocity_multiplier": 1.5, "lookback_period": "60min"},
    "risk_management": {
        "initial_sl": [50, 90], 
        "trailing_stages": [
            {"min_profit": 0, "max_profit": 50, "retention": -1},
            {"min_profit": 50, "max_profit": 100, "retention": 0.7},
            {"min_profit": 100, "max_profit": 150, "retention": 0.75},
            {"min_profit": 150, "max_profit": 300, "retention": 0.8},
            {"min_profit": 300, "max_profit": 400, "retention": 0.85},
            {"min_profit": 400, "retention": 0.9}
        ],
        "tp_override": {"fast_threshold": 30, "slow_threshold": 180}
    },
    "session_constraints": {'day_open': '09:00', "entry_start": "08:15", "mandatory_close": "17:30"}
}
PRO_SETUP = {
    # GER40
    'bias_filter': {'enabled': True, 'buy_threshold': 0.75, 'sell_threshold': 0.25}, 
    'entry_conditions': {'15min_buffer': np.int64(10), 'velocity_multiplier': 2.0, 'lookback_period': '3600S'}, 
    'risk_management': {
        'initial_sl': [np.int64(50), np.int64(50)], 
        'trailing_stages': [
            {'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, 
            {'min_profit': np.int64(30), 'max_profit': np.int64(60), 'retention': 0.55}, 
            {'min_profit': np.int64(60), 'max_profit': np.int64(90), 'retention': 0.6}, 
            {'min_profit': np.int64(90), 'max_profit': np.int64(120), 'retention': 0.65}, 
            {'min_profit': np.int64(120), 'max_profit': np.int64(150), 'retention': 0.7}, 
            {'min_profit': np.int64(150), 'retention': 0.95}
            ], 
        'tp_override': {'fast_threshold': np.int64(15), 'slow_threshold': np.int64(120)}
        }, 
    'session_constraints': {
        'day_open': '09:00',
        'entry_start': '05:50', 'mandatory_close': '23:30',
        'ghost_open': '08:00', 'ghost_close': '08:15',
    }
    # "session_constraints": {"entry_start": "08:15", "mandatory_close": "17:30"}
}

PRO_SETUP = {
    # GER40
    'bias_filter': {'enabled': True, 'buy_threshold': 0.75, 'sell_threshold': 0.25}, 
    'entry_conditions': {'15min_buffer': np.int64(5), 'velocity_multiplier': 2.2, 'lookback_period': '60min'}, 
    'risk_management': {
        'initial_sl': [np.int64(50), np.int64(50)], 
        'trailing_stages': [
            {'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, 
            {'min_profit': np.int64(30), 'max_profit': np.int64(60), 'retention': 0.55}, 
            {'min_profit': np.int64(60), 'max_profit': np.int64(90), 'retention': 0.6}, 
            {'min_profit': np.int64(90), 'max_profit': np.int64(120), 'retention': 0.65}, 
            {'min_profit': np.int64(120), 'max_profit': np.int64(150), 'retention': 0.7}, 
            {'min_profit': np.int64(150), 'retention': 0.95}
            ], 
        'tp_override': {'fast_threshold': np.int64(15), 'slow_threshold': np.int64(120)}}, 
        'session_constraints': {
            'day_open': '09:00', 'entry_start': '9:30', 'mandatory_close': '17:00',
            'ghost_open': '08:00', 'ghost_close': '08:15',
            }
        }
PRO_SETUP = {'bias_filter': {'enabled': True, 'buy_threshold': 0.6, 'sell_threshold': 0.3}, 'entry_conditions': {'15min_buffer': np.int64(20), 'velocity_multiplier': 1.9626786644578, 'lookback_period': '3600S'}, 'risk_management': {'initial_sl': [np.int64(50), np.int64(50)], 'trailing_stages': [{'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, {'min_profit': np.int64(30), 'max_profit': np.int64(60), 'retention': 0.9}, {'min_profit': np.int64(60), 'max_profit': np.int64(90), 'retention': 0.95}, {'min_profit': np.int64(90), 'max_profit': np.int64(120), 'retention': 0.95}, {'min_profit': np.int64(120), 'max_profit': np.int64(150), 'retention': 0.95}, {'min_profit': np.int64(150), 'retention': 0.95}]}, 'session_constraints': {'day_open': '9:00', 'entry_start': '10:00', 'mandatory_close': '17:00', 'ghost_open': '8:00', 'ghost_close': '8:30'}}
PRO_SETUP = {'bias_filter': {'enabled': True, 'buy_threshold': 0.6, 'sell_threshold': 0.4}, 'entry_conditions': {'15min_buffer': np.int64(10), 'velocity_multiplier': 2, 'lookback_period': '3600S'}, 'risk_management': {'initial_sl': [np.int64(50), np.int64(50)], 'trailing_stages': [{'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, {'min_profit': np.int64(30), 'max_profit': np.int64(60), 'retention': 0.9}, {'min_profit': np.int64(60), 'max_profit': np.int64(90), 'retention': 0.95}, {'min_profit': np.int64(90), 'max_profit': np.int64(120), 'retention': 0.95}, {'min_profit': np.int64(120), 'max_profit': np.int64(150), 'retention': 0.95}, {'min_profit': np.int64(150), 'retention': 0.95}]}, 'session_constraints': {'day_open': '9:00', 'entry_start': '10:00', 'mandatory_close': '17:00', 'ghost_open': '8:00', 'ghost_close': '8:30'}}
{'bias_filter': {'enabled': True, 'buy_threshold': 0.75, 'sell_threshold': 0.25}, 'entry_conditions': {'15min_buffer': np.int64(7), 'velocity_multiplier': 2, 'lookback_period': '3600S'}, 'risk_management': {'initial_sl': [np.int64(50), np.int64(50)], 'trailing_stages': [{'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, {'min_profit': np.int64(30), 'max_profit': np.int64(98), 'retention': 0.76}, {'min_profit': np.int64(98), 'max_profit': np.int64(147), 'retention': 0.81}, {'min_profit': np.int64(147), 'max_profit': np.int64(196), 'retention': 0.87}, {'min_profit': np.int64(196), 'max_profit': np.int64(245), 'retention': 0.92}, {'min_profit': np.int64(245), 'retention': 0.95}], 'tp_override': {'fast_threshold': np.int64(15), 'slow_threshold': np.int64(120)}}, 'session_constraints': {'day_open': '09:00', 'entry_start': '10:00', 'mandatory_close': '16:30', 'ghost_open': '08:00', 'ghost_close': '08:30'}}

# PRO_SETUP = {'bias_filter': {'enabled': True, 'buy_threshold': 0.860110398149475, 'sell_threshold': 0.1}, 'entry_conditions': {'15min_buffer': np.int64(7), 'velocity_multiplier': 2.2, 'lookback_period': '4694S'}, 'risk_management': {'initial_sl': [np.int64(461), np.int64(461)], 'trailing_stages': [{'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, {'min_profit': np.int64(30), 'max_profit': np.int64(60), 'retention': 0.65}, {'min_profit': np.int64(60), 'max_profit': np.int64(90), 'retention': 0.71}, {'min_profit': np.int64(90), 'max_profit': np.int64(120), 'retention': 0.77}, {'min_profit': np.int64(120), 'max_profit': np.int64(150), 'retention': 0.83}, {'min_profit': np.int64(150), 'retention': 0.95}], 'tp_override': {'fast_threshold': np.int64(29), 'slow_threshold': np.int64(138)}}, 'session_constraints': {'day_open': '09:00', 'entry_start': '07:00', 'mandatory_close': '16:30', 'ghost_open': '07:00', 'ghost_close': '07:30'}}
# PRO_SETUP = {'bias_filter': {'enabled': True, 'buy_threshold': 0.7, 'sell_threshold': 0.3}, 'entry_conditions': {'15min_buffer': np.int64(10), 'velocity_multiplier': 2, 'lookback_period': '3600S'}, 'risk_management': {'initial_sl': [np.int64(50), np.int64(50)], 'trailing_stages': [{'min_profit': 0, 'max_profit': np.int64(46), 'retention': -1}, {'min_profit': np.int64(46), 'max_profit': np.int64(92), 'retention': 0.45}, {'min_profit': np.int64(92), 'max_profit': np.int64(138), 'retention': 0.54}, {'min_profit': np.int64(138), 'max_profit': np.int64(184), 'retention': 0.63}, {'min_profit': np.int64(184), 'max_profit': np.int64(230), 'retention': 0.72}, {'min_profit': np.int64(230), 'retention': 0.95}], 'tp_override': {'fast_threshold': np.int64(25), 'slow_threshold': np.int64(279)}}, 'session_constraints': {'day_open': '03:00', 'entry_start': '07:35', 'mandatory_close': '19:00', 'ghost_open': '07:04', 'ghost_close': '07:22'}}

# PRO_SETUP = {
#     # GER40
#     'bias_filter': {'enabled': True, 'buy_threshold': 0.75, 'sell_threshold': 0.25}, 
#     'entry_conditions': {'15min_buffer': np.int64(5), 'velocity_multiplier': 2.2, 'lookback_period': '60min'}, 
#     'risk_management': {
#         'initial_sl': [np.int64(50), np.int64(50)], 
#         'trailing_stages': [
#             {'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, 
#             {'min_profit': np.int64(30), 'max_profit': np.int64(60), 'retention': 0.55}, 
#             {'min_profit': np.int64(60), 'max_profit': np.int64(90), 'retention': 0.6}, 
#             {'min_profit': np.int64(90), 'max_profit': np.int64(120), 'retention': 0.65}, 
#             {'min_profit': np.int64(120), 'max_profit': np.int64(150), 'retention': 0.7}, 
#             {'min_profit': np.int64(150), 'retention': 0.95}
#             ], 
#         'tp_override': {'fast_threshold': np.int64(15), 'slow_threshold': np.int64(120)}}, 
#         'session_constraints': {
#             'day_open': '09:00', 'entry_start': '10:00', 'mandatory_close': '16:41',
#             'ghost_open': '08:00', 'ghost_close': '08:15',
#             }
#         }

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
        biases = biases.shift(1, fill_value="straddle")
        
        return biases.to_dict()

    @staticmethod
    # price = ghost_data['ask'] if bias == "buy" else ghost_data['bid']
    def get_ghost_ranges(df: pd.DataFrame, config: dict, biases: dict):
        df_cet = df.copy()
        df_cet.index = df_cet.index.tz_convert(CET)
        
        # 1. Extract session data
        start = config['session_constraints']['ghost_open']
        end = config['session_constraints']['ghost_close']
        ghost_data = df_cet.between_time(start, end)
        
        # 2. Map the bias dict to the ghost_data index (by date)
        # This creates a Series of 'buy', 'sell', or 'straddle' aligned with the timestamps
        row_biases = ghost_data.index.date
        row_biases = pd.Series(row_biases).map(biases).values

        # 3. Define conditions based on the mapped biases
        conditions = [
            (row_biases == 'buy'),
            (row_biases == 'sell'),
            (row_biases == 'straddle')
        ]
        
        choices = [
            ghost_data['ask'],
            ghost_data['bid'],
            (ghost_data['bid'] + ghost_data['ask']) / 2
        ]
        
        # 4. Apply vectorised selection
        price_array = np.select(conditions, choices, default=(ghost_data['bid'] + ghost_data['ask']) / 2)
        price_series = pd.Series(price_array, index=ghost_data.index)
        
        # 5. Aggregate to get daily high/low (max/min)
        ranges = price_series.groupby(price_series.index.date).agg(['min', 'max'])
        return ranges.to_dict('index')

# -------------------------------------------------------------------
# 3. HIGH-SPEED ENGINE (NUMPY CORE)
# -------------------------------------------------------------------

def get_todays_open_price(df, index, biases, config):
    """
    Returns the price at the Frankfurt Open for each day based on the daily bias dict.
    """
    # 1. Map the daily biases dict to every row in the dataframe based on date
    # This aligns 'buy', 'sell', or 'straddle' with the high-frequency index
    row_dates = index.date
    row_biases = pd.Series(row_dates).map(biases).values

    # 2. Select price based on bias using boolean masking
    conditions = [
        (row_biases == 'buy'),
        (row_biases == 'sell'),
        (row_biases == 'straddle')
    ]
    choices = [
        df['ask'],
        df['bid'],
        (df['bid'] + df['ask']) / 2
    ]
    
    # Default to mid if bias is missing or 'straddle'
    selected_price = np.select(conditions, choices, default=(df['bid'] + df['ask']) / 2)
    
    # 3. Reconstruct Series with the provided CET index
    price_series = pd.Series(selected_price, index=index)
    
    # 4. Parse open time from config and filter
    open_time_str = config['session_constraints']['day_open'] # e.g., "09:00"
    open_h, open_m = [int(t) for t in open_time_str.split(":")]
    
    # Filter for ticks occurring at or after the open time
    post_open = price_series[price_series.index.time >= DT.time(open_h, open_m)]
    
    # 5. Group by date and take the first value (the opening tick)
    daily_opens = post_open.groupby(post_open.index.date).first()
    
    return daily_opens.to_dict()

def process_chunk_parallel(year, month, config):
    """
    Worker function for parallel execution.
    Handles data loading and the high-speed tick loop.
    """
    path = Path(f"./tick_data/{SYMBOL}_{year}_{month:02d}.parquet")
    if not path.exists(): 
        print(f"File not found: {path}")
        return []
    
    df = pd.read_parquet(path)
    zone = get_server_timezone(year, month, 1)
    # print(f"Server timezone: {zone}")
    df.index = pd.to_datetime(df.index).tz_localize(zone)
    
    # 1. Precompute Signals (Vectorized)
    biases = SignalPrecomputer.get_daily_bias(df, config['bias_filter']['buy_threshold'], config['bias_filter']['sell_threshold'])
    ghost_ranges = SignalPrecomputer.get_ghost_ranges(df, config, biases)
    velocity_signals = SignalPrecomputer.compute_velocity(
        df, config['entry_conditions']['velocity_multiplier'], config['entry_conditions']['lookback_period']
    )

    # 2. Prepare NumPy arrays for the loop
    # This is where the magic happens for performance
    server_idx = df.index.tz_convert(zone)
    date_times = server_idx.astype(int)
    dates = server_idx.date # Dates must be server dates
    bids = df['bid'].values
    asks = df['ask'].values
    mids = (bids + asks) / 2.0
    
    # Pre-extract time components to avoid calling .hour/.minute in loop
    df_cet_idx = df.index.tz_convert(CET)
    hours = df_cet_idx.hour
    minutes = df_cet_idx.minute
    daily_opens = get_todays_open_price(df, df_cet_idx, biases, config)
    
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
        curr_time = date_times[i]
        curr_mid = mids[i]
        curr_bid = bids[i]
        curr_ask = asks[i]
        
        # New Day Reset
        if curr_date == day_traded:
            continue
            
        

        # Session Constraints
        h, m = hours[i], minutes[i]
        is_entry_window = (h == start_h and m >= start_m) or (start_h < h < end_h) or (h == end_h and m < end_m)
        is_close_time = (h == end_h and m >= end_m)
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
            pnl = (curr_bid - entry_p) if direction == 'buy' else (entry_p - curr_ask)
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
                
                if touched_opposite and curr_ask >= g_high - buffer and velocity_signals[i] and curr_ask > daily_opens[curr_date]:
                    in_trade, direction, entry_p, entry_t, max_pnl = True, 'buy', curr_bid, curr_time, 0.0
                    # print("Entered buy")
                    # print(df.iloc[[i]])
            elif bias_str == 'sell':
                if curr_bid >= g_high - buffer: touched_opposite = True
                
                if touched_opposite and curr_bid <= g_low + buffer and velocity_signals[i] and curr_bid < daily_opens[curr_date]:
                    in_trade, direction, entry_p, entry_t, max_pnl = True, 'sell', curr_ask, curr_time, 0.0
                    # print("Entered sell")
                    # print(df.iloc[[i]])

    return trades
    
def get_lot(equity):
    lot_size = 0.01
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

# if __name__ == "__main__":
#     engine = DAXTickEngine(PRO_SETUP)
#     # Example: Run for 2025
#     results = engine.run_backtest(2025, 1, 2026, 1)
    
#     if not results.empty:
#         results = calculate_equity(results)
#         # results['server_entry_time'] = results['entry_time'].dt.tz_convert(get_server_timezone())
#         # results['server_exit_time'] = results['exit_time'].dt.tz_convert(get_server_timezone())
#         entry_time = pd.to_datetime(results['entry_time']).dt.tz_localize(get_server_timezone())
#         results['server_entry_time'] = entry_time.dt.tz_convert(CET)
#         results.to_csv("DAX_test.csv")
#         win_rate = (results['profit_ticks'] > 0).mean() * 100

#         print(f"Backtest Complete.")
#         print(f"Total Trades: {len(results)}")
#         print(f"Win Rate: {win_rate:.2f}%")
#         print(f"Total Profit: $ {results['pnl'].sum():.2f}")
#         print(f"Min profit: $ {results['pnl'].min()}")
#         print(f"Max profit: $ {results['pnl'].max()}")
#         print(f"Avg profit: $ {results['pnl'].mean()}")
        
#         plot_results(results)
#     else:
#         print("No results")
#     # input()


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
    (sl, buf, vel, #tp_fast, tp_slow,
    #  start_off, end_off, 
     p_step, r_base, r_inc, buy_t, sell_t,
    #   gopen_off, gend_off,
    #  lookback, open_m
     ) = params

    # Convert offsets to HH:MM strings
    # day_open = (datetime(2025, 1, 1, 0, 0) + timedelta(minutes=int(open_m * 30))).strftime("%H:%M")

    # start_time = (datetime(2025, 1, 1, 6, 0) + timedelta(minutes=int(start_off))).strftime("%H:%M")
    # end_time = (datetime(2025, 1, 1, 16, 0) + timedelta(minutes=int(end_off))).strftime("%H:%M")

    # ghost_open = datetime(2025, 1, 1, 6, 0) + timedelta(minutes=int(gopen_off))
    # ghost_close = ghost_open + timedelta(minutes=int(gend_off))
    # ghost_open = ghost_open.strftime("%H:%M")
    # ghost_close = ghost_close.strftime("%H:%M")

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
        "bias_filter": {"enabled": True, "buy_threshold": buy_t, "sell_threshold": sell_t},
        "entry_conditions": {"15min_buffer": buf, "velocity_multiplier": vel, "lookback_period": "3600S"},
        "risk_management": {
            "initial_sl": [sl, sl],
            "trailing_stages": stages,
            # "tp_override": {"fast_threshold": tp_fast, "slow_threshold": tp_slow}
        },
        "session_constraints": {
            'day_open': "9:00",
            "entry_start": "10:00", "mandatory_close": "17:00",
            'ghost_open': "8:00", 'ghost_close': "8:30"
            }
    }
    return config

# -------------------------------------------------------------------
# OBJECTIVE FUNCTION
# -------------------------------------------------------------------

def full_objective(params):
    current_config = create_dynamic_config(params)
    engine = DAXTickEngine(current_config)
    
    # We optimize on a 4-month window for speed on i5/8GB RAM
    results = engine.run_backtest(2025, 4, 2025, 9) 
    len_months = 9 - 4 + 1
    
    if results.empty or len(results) < len_months * 4:
        return 10.0 # Penalty for no activity
    
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
        Integer(50, 100, name='initial_sl'),
        Integer(5, 20, name='buffer_ticks'),
        Real(1.5, 2.5, name='velocity_multiplier'),
        # Integer(15, 60, name='tp_fast'),        # Fast TP override mins
        # Integer(120, 300, name='tp_slow'),      # Slow TP override mins
        # Integer(0, 240, name='start_offset'),   # Mins after 06:00
        # Integer(0, 180, name='end_offset'),     # Mins after 16:00
        Integer(30, 50, name='profit_step'),   # Ticks per stage
        Real(0.3, 0.7, name='retention_base'),  # Starting retention
        Real(0.05, 0.2, name='retention_inc'),   # How much retention grows per stage
        Real(0.6, 0.8, name='buy_threshold'),
        Real(0.2, 0.4, name='sell_threshold'),
        # Integer(0, 240, name='ghost_open'),   # Mins after 06:00
        # Integer(0, 60, name='ghost_close'),
        # Integer(60 * 10, 60 * 60 * 1.5, name='lookback_period'),
        # Integer(0, 18, name='day_open'),
    ]
    
    print("💎 Starting Professional Parameter Search...")
    res = gp_minimize(full_objective, space, n_calls=50, random_state=42, verbose=True)
    
    best_cfg = create_dynamic_config(res.x)
    print("\n✅ OPTIMIZATION COMPLETE")
    print(f"Best Session: {best_cfg['session_constraints']['entry_start']} to {best_cfg['session_constraints']['mandatory_close']}")
    # print(f"Best TP Overrides: {best_cfg['risk_management']['tp_override']}")
    # print(f"Best Trailing Stages: {best_cfg['risk_management']['trailing_stages']}")
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
        engine = DAXTickEngine(PRO_SETUP)
        # Example: Run for 2025
        results = engine.run_backtest(2025, 1, 2025, 12)
        
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




"""
1. The Importance of "Pre-Market Gaps" and The Open
The GER40 frequently experiences significant gaps between the previous day's close (5:30 PM CET) and the main open the next morning (9:00 AM CET).
The Guarded Truth: The market often spends the first 30-90 minutes of the main session "filling the gap" or consolidating the previous night's price action from U.S. and Asian markets. Many institutional traders watch how the index reacts around the previous day's closing price level. The initial market reaction (the first 15-30 mins) can often set the tone for the rest of the day.
2. The Power of "Opening Range Breakouts" (ORB)
The Guarded Truth: The index often trends strongly in the direction of the initial move after the market settles down following the initial "noise" of the open. A simple, effective strategy many use is defining the high and low of the first 30 or 60 minutes and only trading the breakout of that range, using the other side of the range as the stop loss. The volatility of the GER40 makes this pattern highly reliable on trend days.
3. Understanding the "Total Return" Bias
As mentioned, the GER40 is a performance index.
The Guarded Truth: This structural difference means that, over time, the index naturally trends slightly higher than a standard "price index" would. While this doesn't help with 15-minute chart scalping, it provides a subtle, long-term bullish bias that buy-side institutional traders are always aware of when structuring longer-term hedges or investments. The "default" trade, absent major news, is often gently long.
4. The "Pivot Time" of 3:30 PM CET
The U.S. markets (NYSE/Nasdaq) open at 3:30 PM CET.
The Guarded Truth: This time often acts as a pivot point for the GER40. The index frequently pauses, reverses, or accelerates significantly at this exact time as a massive wave of U.S. volume hits the global markets. Many experienced traders avoid taking a new position immediately before 3:30 PM CET, preferring to wait until after the initial U.S. open volatility subsides.
5. Managing Psychological "Drawdown Drag"
The Guarded Truth: The GER40's speed means losses can accumulate quickly. Experienced traders know that the hardest part isn't managing a single loss, but managing the psychology after several small losses in a row (a "drawdown"). The "closely guarded truth" here is the vital importance of reducing your position size immediately after a series of losses to regain confidence and control, rather than trying to "win back" the money with larger bets.

"""

"""You want to compare the current price (or the price during your "safe" window) to an early reference point, like the opening range high/low or the Central Pivot Range (CPR).
"""