"""
DAX Tick-Based Trading Engine - Institutional Flow & Momentum Dynamics
Optimized for Colab/Kaggle (8GB RAM / i5 processor)
Author: Trading Engine AI
Date: 2026-01-27
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
import MetaTrader5 as mt5
import os
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

import concurrent.futures
# from tqdm.notebook import tqdm  # Specialized for Colab/Jupyter
import multiprocessing

# -------------------------------------------------------------------
# 1. CONFIGURATION
# -------------------------------------------------------------------
PRO_SETUP = {
    "bias_filter": {
        "enabled": True,
        "buy_threshold": 0.75,
        "sell_threshold": 0.25,
        "neutral_zone": [0.25, 0.75]
    },
    "entry_conditions": {
        "15min_buffer": 10,           # ticks
        "velocity_multiplier": 1.5,
        "lookback_period": "60min"    # for velocity baseline
    },
    "risk_management": {
        "initial_sl": [70, 90],       # ticks to optimize
        "trailing_stages": [
            {"min_profit": 0, "max_profit": 50, "retention": -1},
            {"min_profit": 50, "max_profit": 150, "retention": 0.8},
            {"min_profit": 150, "retention": 0.9}
        ],
        "tp_override": {
            "fast_threshold": 30,     # minutes
            "slow_threshold": 180     # minutes
        }
    },
    "session_constraints": {
        "entry_start": "08:15 CET",
        "mandatory_close": "17:30 CET",
        "velocity_calc_hours": ["08:00", "17:30"]
    }
}

# Timezone handling
CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc

# -------------------------------------------------------------------
# 2. CORE ENGINE CLASSES
# -------------------------------------------------------------------

class DAXTickDataLoader:
    """
    Memory-optimized loading of MT5 tick data.
    Fetches data in monthly chunks, downcasts, saves as Parquet.
    """
    def __init__(self, symbol='GER40', data_dir='./tick_data'):
        self.symbol = symbol
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
    def fetch_ticks_chunk(self, start_dt, end_dt):
        """Fetch ticks for a given chunk (max 1 month) from MT5."""
        if not mt5.initialize():
            raise ConnectionError("MT5 initialize failed")
        ticks = mt5.copy_ticks_range(self.symbol, start_dt, end_dt, mt5.COPY_TICKS_ALL)
        mt5.shutdown()
        if ticks is None or len(ticks) == 0:
            return None
        df = pd.DataFrame(ticks)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        # Downcast to save memory
        df[['bid','ask','last']] = df[['bid','ask','last']].astype('float32')
        df[['volume','flags']] = df[['volume','flags']].astype('int32')
        return df
    
    def fetch_and_store_range(self, start_date, end_date):
        """Fetch ticks in monthly chunks and store as partitioned Parquet."""
        current = start_date.replace(day=1, hour=0, minute=0, second=0)
        end_date = end_date.replace(hour=23, minute=59, second=59)
        while current < end_date:
            month_end = (current + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
            month_end = min(month_end, end_date)
            print(f"Processing {current.date()} to {month_end.date()}")
            year, month = current.year, current.month
            path = self.data_dir / f"{self.symbol}_{year}_{month:02d}.parquet"

            if not os.path.exists(path):
                df = self.fetch_ticks_chunk(current, month_end)
                if df is not None:   
                    df.to_parquet(path, compression='zstd')
            else:
                print("    File already exists")
            current = (month_end + timedelta(seconds=1)).replace(day=1)
        print("Data fetch complete.")
    
    def load_chunk(self, year, month):
        """Load a single monthly parquet file."""
        path = self.data_dir / f"{self.symbol}_{year}_{month:02d}.parquet"
        if not os.path.exists(path): #path.exists():
            raise FileNotFoundError(f"The file at {path} cannot be found!. \nThe current directory is {os.getcwd()}")
        df = pd.read_parquet(path)
        # Ensure UTC index
        df.index = pd.to_datetime(df.index).tz_localize(UTC)
        return df


class AnchorCloseAnalyzer:
    """
    Calculates the Relative Close (RC) of the previous day to determine institutional bias.
    RC = (Close - Low) / (High - Low)
    """
    def __init__(self, buy_threshold=0.75, sell_threshold=0.25):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
    
    def calculate_anchor_close(self, day_data):
        """day_data: DataFrame with 'bid' and 'ask' for a single day."""
        # Use mid price for calculations
        mid = (day_data['bid'] + day_data['ask']) / 2
        high = mid.max()
        low = mid.min()
        close = mid.iloc[-1]
        if high == low:
            return 0.5
        rc = (close - low) / (high - low)
        return rc
    
    def get_bias(self, rc):
        """Return 'buy', 'sell', or 'straddle' based on RC thresholds."""
        if rc > self.buy_threshold:
            return 'buy'
        elif rc < self.sell_threshold:
            return 'sell'
        else:
            return 'straddle'


class TickVelocityValidator:
    """
    Validates tick velocity (ticks per second) against a rolling baseline.
    """
    def __init__(self, velocity_multiplier=1.5, lookback_window='60min'):
        self.velocity_multiplier = velocity_multiplier
        self.lookback_window = pd.Timedelta(lookback_window)
    
    def compute_tick_density(self, tick_data, window_seconds=30):
        """Compute ticks per second in the last `window_seconds`."""
        # Resample to 1-second bins, count ticks
        counts = tick_data.resample('1S').size()
        # Rolling sum over last window_seconds
        density = counts.rolling(f'{window_seconds}S', min_periods=1).sum() / window_seconds
        return density
    
    def validate_velocity(self, tick_data):
        """
        Check if current tick density > multiplier × rolling average.
        Returns boolean series where True indicates valid velocity trigger.
        """
        density = self.compute_tick_density(tick_data)
        rolling_avg = density.rolling(self.lookback_window, min_periods=1).mean()
        signal = density > (rolling_avg * self.velocity_multiplier)
        return signal


class EntryValidator:
    """
    Two-layer entry validation: 15-minute trap + tick velocity.
    """
    def __init__(self, buffer_ticks=10, velocity_validator=None):
        self.buffer_ticks = buffer_ticks
        self.velocity_validator = velocity_validator
        self.ghost_range = None  # will store (low, high) of 08:00-08:15
        
    def update_ghost_range(self, tick_data):
        """Extract the high/low of the first 15 minutes (08:00-08:15 CET)."""
        # Filter data for ghost range (assuming tick data index is UTC)
        ghost_mask = (tick_data.index.time >= pd.Timestamp('08:00').time()) & \
                     (tick_data.index.time < pd.Timestamp('08:15').time())
        ghost_data = tick_data[ghost_mask]
        if len(ghost_data) == 0:
            return
        mid = (ghost_data['bid'] + ghost_data['ask']) / 2
        self.ghost_range = (mid.min(), mid.max())
    
    def validate_15min_trap(self, price, direction):
        """
        Rule: Price must test opposite side of 15-min range first.
        For buy: price must touch within buffer of 15-min low, then break 15-min high.
        For sell: opposite.
        """
        if self.ghost_range is None:
            return False
        low, high = self.ghost_range
        buffer = self.buffer_ticks #* 0.01  # assuming 1 tick = 0.01 for DAX
        if direction == 'buy':
            touch_low = price <= (low + buffer)
            break_high = price >= (high - buffer)
            return touch_low and break_high
        else:  # sell
            touch_high = price >= (high - buffer)
            break_low = price <= (low + buffer)
            return touch_high and break_low
    
    def validate_entry(self, tick_data, direction):
        """
        Combine 15‑min trap and velocity validation.
        Returns True if both conditions are satisfied.
        """
        if self.ghost_range is None:
            self.update_ghost_range(tick_data)
        # Use the latest price
        latest_mid = (tick_data.iloc[-1]['bid'] + tick_data.iloc[-1]['ask']) / 2
        trap_ok = self.validate_15min_trap(latest_mid, direction)
        if not trap_ok:
            return False
        if self.velocity_validator:
            velocity_ok = self.velocity_validator.validate_velocity(tick_data).iloc[-1]
            return velocity_ok
        return True


class MomentumTrailingManager:
    """
    Dynamic trailing stops based on profit thresholds.
    """
    def __init__(self, trailing_stages, tp_override, sl_pips=PRO_SETUP['risk_management']['initial_sl'][0]):
        self.stages = trailing_stages
        self.tp_override = tp_override
        self.sl_pips = sl_pips
        self.max_profit_ticks = 0
        self.entry_time = None
        self.tp_hit_time = None
    
    def calculate_trailing_level(self, current_price, entry_price, direction):
        """
        Compute current stop loss level based on max profit reached.
        """
        if direction == 'buy':
            profit_ticks = (current_price - entry_price)   # assume 1 tick = 0.01
        else:
            profit_ticks = (entry_price - current_price) 
        self.max_profit_ticks = max(self.max_profit_ticks, profit_ticks)
        

        def get_sl():
            if direction == 'buy':
                sl = entry_price - self.sl_pips
            else:
                sl = entry_price + self.sl_pips
            return sl
        # SL
        if profit_ticks < 0:
            get_sl()

        # Find applicable retention rate
        retention = 0.6  # default
        for stage in self.stages:
            if stage['min_profit'] <= self.max_profit_ticks:
                if 'max_profit' in stage and self.max_profit_ticks <= stage['max_profit']:
                    retention = stage['retention']
                    break
                elif 'max_profit' not in stage:
                    retention = stage['retention']
                    break
        if retention < 0:
            return get_sl()
        # Compute trailing distance
        trail_distance_ticks = self.max_profit_ticks * (1 - retention)
        if direction == 'buy':
            stop_price = entry_price + trail_distance_ticks #* 0.01
        else:
            stop_price = entry_price - trail_distance_ticks #* 0.01
        return stop_price
    
    def time_speed_override(self, entry_time, tp_hit_time):
        """
        Decide whether to cancel TP based on time speed.
        Returns True if TP should be cancelled (use trailing only).
        """
        if tp_hit_time is None:
            return False
        duration = (tp_hit_time - entry_time).total_seconds() / 60  # minutes
        if duration < self.tp_override['fast_threshold']:
            return True  # cancel TP, use trailing only
        if duration > self.tp_override['slow_threshold']:
            return False  # take fixed TP immediately
        # else use standard trailing rules
        return False


class SessionConstraintEnforcer:
    """
    Enforces session‑based constraints: mandatory close, spread protection, etc.
    """
    def __init__(self, entry_start='08:15 CET', mandatory_close='17:30 CET'):
        self.entry_start = pd.Timestamp(entry_start).time()
        self.mandatory_close = pd.Timestamp(mandatory_close).time()
    
    def is_entry_allowed(self, dt_utc):
        """Check if current time is after entry start."""
        dt_cet = dt_utc.astimezone(CET)
        return dt_cet.time() >= self.entry_start
    
    def is_mandatory_close(self, dt_utc):
        """Check if current time is at or after mandatory close."""
        dt_cet = dt_utc.astimezone(CET)
        return dt_cet.time() >= self.mandatory_close
    
    def is_spread_widening_period(self, dt_utc):
        """Avoid execution during spread widening periods."""
        dt_cet = dt_utc.astimezone(CET)
        time = dt_cet.time()
        # 08:00-08:05 and 17:25-17:30 CET
        if (pd.Timestamp('08:00').time() <= time <= pd.Timestamp('08:05').time()) or \
           (pd.Timestamp('17:25').time() <= time <= pd.Timestamp('17:30').time()):
            return True
        return False


# -------------------------------------------------------------------
# 3. VECTORIZED BACKTEST ENGINE
# -------------------------------------------------------------------

class DAXTickEngine:
    """
    Optimized tick‑by‑tick backtest engine that processes data in chunks.
    """
    def __init__(self, config=PRO_SETUP):
        self.config = config
        self.data_loader = DAXTickDataLoader()
        self.anchor_analyzer = AnchorCloseAnalyzer(
            buy_threshold=config['bias_filter']['buy_threshold'],
            sell_threshold=config['bias_filter']['sell_threshold']
        )
        self.velocity_validator = TickVelocityValidator(
            velocity_multiplier=config['entry_conditions']['velocity_multiplier'],
            lookback_window=config['entry_conditions']['lookback_period']
        )
        self.entry_validator = EntryValidator(
            buffer_ticks=config['entry_conditions']['15min_buffer'],
            velocity_validator=self.velocity_validator
        )
        self.trailing_manager = MomentumTrailingManager(
            trailing_stages=config['risk_management']['trailing_stages'],
            tp_override=config['risk_management']['tp_override']
        )
        self.session_enforcer = SessionConstraintEnforcer(
            entry_start=config['session_constraints']['entry_start'],
            mandatory_close=config['session_constraints']['mandatory_close']
        )
        self.results = []
        
    def process_chunk(self, tick_data: pd.DataFrame):
        """
        Process one month of tick data through the strategy.
        Returns a DataFrame with trade records.
        """
        # 1. Determine daily anchor bias
        # Group by date (CET)
        tick_data_cet = tick_data.index.tz_convert(CET)
        daily_groups = tick_data.groupby(tick_data_cet.date)
        stradle_dates = []
        daily_bias = {}
        for date, group in daily_groups:
            rc = self.anchor_analyzer.calculate_anchor_close(group)
            bias = self.anchor_analyzer.get_bias(rc)
            if bias == "straddle":
                stradle_dates.append(date)
                continue
            daily_bias[date] = bias

        stradle_mask = tick_data.index.normalize().to_series().dt.date.isin(stradle_dates)
        stradle_mask.index = tick_data.index

        cleaned_tick_data = tick_data[~stradle_mask]
        cleaned_tick_data = cleaned_tick_data.between_time(self.session_enforcer.entry_start, self.session_enforcer.mandatory_close)
        
        # 2. Vectorized entry/exit simulation (simplified loop for clarity)
        trades = []
        traded_days = set()
        in_trade = False
        entry_price = None
        entry_time = None
        direction = None
        max_profit_ticks = 0
        
        for i, (ts, row) in enumerate(cleaned_tick_data.iterrows()):
            if ts.date().isoformat() in traded_days:
                continue
            # Mandatory session close
            if i % 1000 == 0:
                print(f"Processed {i}/{len(cleaned_tick_data)}")
            if self.session_enforcer.is_mandatory_close(ts):
                print("Mandatory session close")
                if in_trade:
                    # Close trade at current mid price
                    print(f"Closed {direction}")
                    mid = (row['bid'] + row['ask']) / 2
                    trades.append({
                        'entry_time': entry_time,
                        'exit_time': ts,
                        'direction': direction,
                        'entry_price': entry_price,
                        'exit_price': mid,
                        'profit_ticks': (mid - entry_price)  if direction == 'buy' else (entry_price - mid) 
                    })
                    traded_days.add(ts.date().isoformat())
                    in_trade = False
                continue
            
            # Skip spread widening periods
            if self.session_enforcer.is_spread_widening_period(ts):
                print("Skipping wide spread")
                continue
            
            # Determine bias for current day
            current_date = ts.astimezone(CET).date()
            bias = daily_bias.get(current_date, 'straddle')
            # print(f"{bias=}")
            
            # Entry logic
            if not in_trade and bias in ('buy', 'sell'):
                # Check entry start time
                if not self.session_enforcer.is_entry_allowed(ts):
                    print("Entry not allowed")
                    continue
                # Validate 15‑min trap and velocity
                if self.entry_validator.validate_entry(cleaned_tick_data.iloc[:i+1], bias):
                    print(f"Entered {bias}")
                    in_trade = True
                    entry_price = (row['bid'] + row['ask']) / 2
                    entry_time = ts
                    direction = bias
                    max_profit_ticks = 0
                    self.trailing_manager.entry_time = entry_time
                    continue
                # else:
                #     print("No 15 min confirm")
            
            # Exit logic (trailing stop)
            if in_trade:
                # print("In trade")
                current_mid = (row['bid'] + row['ask']) / 2
                # Calculate trailing stop level
                stop_level = self.trailing_manager.calculate_trailing_level(
                    current_mid, entry_price, direction
                )
                # Check stop hit
                if (direction == 'buy' and current_mid <= stop_level) or \
                   (direction == 'sell' and current_mid >= stop_level):
                    print(f"Closed {direction}")
                    trades.append({
                        'entry_time': entry_time,
                        'exit_time': ts,
                        'direction': direction,
                        'entry_price': entry_price,
                        'exit_price': current_mid,
                        'profit_ticks': (current_mid - entry_price)  if direction == 'buy' else (entry_price - current_mid)
                    })
                    traded_days.add(ts.date().isoformat())
                    in_trade = False
        
        return pd.DataFrame(trades) if trades else pd.DataFrame()
    
    def run_backtest(self, start_date, end_date):
        """
        Run backtest over multiple months, chunk by chunk.
        """
        all_trades = []
        current = start_date.replace(day=1)
        while current < end_date:
            year, month = current.year, current.month
            print(f"Backtesting {year}-{month:02d}")
            chunk = self.data_loader.load_chunk(year, month)
            filtered_chunk = chunk.loc[start_date: end_date]
            if chunk is not None:
                trades = self.process_chunk(filtered_chunk)
                all_trades.append(trades)
            current = (current + timedelta(days=32)).replace(day=1)
        
        if all_trades:
            return pd.concat(all_trades, ignore_index=True)
        else:
            return pd.DataFrame()


# -------------------------------------------------------------------
# 4. BAYESIAN OPTIMIZATION SETUP
# -------------------------------------------------------------------

from skopt import gp_minimize
from skopt.space import Integer, Real

def objective_function(params):
    """
    Objective function for Bayesian optimization.
    params: [sl_distance, buffer_ticks, velocity_multiplier]
    """
    sl_distance, buffer_ticks, velocity_multiplier = params
    # Update config with candidate parameters
    config = PRO_SETUP.copy()
    config['risk_management']['initial_sl'] = [sl_distance, sl_distance]
    config['entry_conditions']['15min_buffer'] = buffer_ticks
    config['entry_conditions']['velocity_multiplier'] = velocity_multiplier
    
    # Run backtest with updated config (simplified – in practice use a full run)
    engine = DAXTickEngine(config)
    trades = engine.run_backtest(
        start_date=datetime(2025,1,1, tzinfo=UTC),
        end_date=datetime(2025,6,1, tzinfo=UTC)
    )
    if len(trades) == 0:
        return -1.0  # penalty for no trades
    # Maximize Sharpe ratio (simplified)
    returns = trades['profit_ticks'] #* 0.01  # assume 1 tick = 0.01
    sharpe = returns.mean() / (returns.std() + 1e-6)
    return -sharpe  # minimize negative Sharpe

def run_optimization():
    """Bayesian optimization loop."""
    space = [
        Integer(50, 120, name='sl_distance'),
        Integer(5, 20, name='buffer_ticks'),
        Real(1.2, 2.0, name='velocity_multiplier')
    ]
    res = gp_minimize(
        objective_function,
        space,
        n_calls=50,          # hardware‑constrained limit
        random_state=42,
        verbose=True
    )
    print(f"Best parameters: SL={res.x[0]}, buffer={res.x[1]}, velocity multiplier={res.x[2]}")
    print(f"Best Sharpe: {-res.fun}")
    return res

# -------------------------------------------------------------------
# 5. PERFORMANCE METRICS & VISUALIZATION
# -------------------------------------------------------------------

def calculate_metrics(trades):
    """Calculate standard performance metrics."""
    if len(trades) == 0:
        return {}
    # Convert profit ticks to monetary value (assume 1 tick = €1 for simplicity)
    trades['profit'] = trades['profit_ticks'] * 1.0
    total_return = trades['profit'].sum()
    win_rate = (trades['profit'] > 0).mean() * 100
    profit_factor = trades[trades['profit'] > 0]['profit'].sum() / abs(trades[trades['profit'] < 0]['profit'].sum())
    # Sharpe ratio (daily)
    daily_returns = trades.groupby(trades['exit_time'].dt.date)['profit'].sum()
    sharpe = daily_returns.mean() / (daily_returns.std() + 1e-6) * np.sqrt(252)
    # Max drawdown
    cumulative = trades['profit'].cumsum()
    running_max = cumulative.expanding().max()
    drawdown = (cumulative - running_max) / (running_max + 1e-6)
    max_dd = drawdown.min() * 100
    # Average holding time
    hold_times = (trades['exit_time'] - trades['entry_time']).dt.total_seconds() / 60  # minutes
    avg_hold = hold_times.mean()
    
    return {
        'Total Return (€)': total_return,
        'Win Rate (%)': win_rate,
        'Profit Factor': profit_factor,
        'Sharpe Ratio (daily)': sharpe,
        'Max Drawdown (%)': max_dd,
        'Avg Holding Time (min)': avg_hold,
        'Total Trades': len(trades)
    }

def plot_equity_curve(trades):
    """Plot equity curve with drawdowns."""
    import matplotlib.pyplot as plt
    trades = trades.sort_values('exit_time')
    trades['equity'] = trades['profit'].cumsum()
    trades['running_max'] = trades['equity'].expanding().max()
    trades['drawdown'] = (trades['equity'] - trades['running_max']) / trades['running_max']
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    ax1.plot(trades['exit_time'], trades['equity'], label='Equity')
    ax1.set_ylabel('Equity (€)')
    ax1.legend()
    ax1.grid(True)
    
    ax2.fill_between(trades['exit_time'], trades['drawdown'], 0, color='red', alpha=0.3)
    ax2.set_ylabel('Drawdown')
    ax2.set_xlabel('Date')
    ax2.grid(True)
    plt.suptitle('Equity Curve & Drawdown')
    plt.show()

# -------------------------------------------------------------------
# 6. EXAMPLE USAGE
# -------------------------------------------------------------------

def main():
    """End‑to‑end example."""
    # 1. Fetch and store data (run once)
    loader = DAXTickDataLoader(symbol='GER40', data_dir='./dax_ticks')
    loader.fetch_and_store_range(
        start_date=datetime(2025,1,1, tzinfo=UTC),
        end_date=datetime(2025,12,31, tzinfo=UTC)
    )
    
    # 2. Run backtest
    engine = DAXTickEngine(PRO_SETUP)
    trades = engine.run_backtest(
        start_date=datetime(2025,1,1, tzinfo=UTC),
        end_date=datetime(2025,12,31, tzinfo=UTC)
    )
    
    # 3. Calculate metrics
    if len(trades) > 0:
        metrics = calculate_metrics(trades)
        for k, v in metrics.items():
            print(f"{k}: {v:.2f}")
        plot_equity_curve(trades)
    else:
        print("No trades generated.")
    
    # 4. Optional: Bayesian optimization
    # res = run_optimization()

if __name__ == '__main__':
    main()