import argparse
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, timedelta, timezone, date, time
from typing import Optional, Dict, Any, List, Tuple, Callable, Union
import calendar
import time as t_mod
from collections import deque
import os
import sys
import dotenv
import logging
from functools import wraps, lru_cache

if sys.platform == "linux":
    from mt5linux import MetaTrader5
    mt5 = MetaTrader5()
    islinux = True
    MAGIC_NUM = "10"
elif sys.platform == "win32":
    import MetaTrader5 as mt5
    islinux = False
    MAGIC_NUM = "20"
else:
    raise RuntimeError(f"Unknown platform {sys.platform}. Must be 'win32' or 'linux'")

dotenv.load_dotenv()

# Error: Check direction during high velocity, might be opposing

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------
logger = logging.getLogger()

LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]
MAX_RETRIES = 5
TIMEOUT = 1

# symbol = "GBPUSD"  # Update for your broker (e.g., DE40, DAX40)
VOLUME = 0.01      # Lot size
DEVIATION = 10    # Slippage tolerance in points
MAGIC_NUM += "02"
MAGIC_NUM = int(MAGIC_NUM)

# Strategy Parameters (Matching your backtest)
r1 = 5

CONFIG = {
    'bias_filter': {'buy_threshold': 0.51, 'sell_threshold': 0.49}, 
    'entry_conditions': {'buffer_pips': 20, 'velocity_multiplier': 2, 'lookback_seconds': 60*60}, 
    'risk_management': {
        'initial_sl_pips': 5, 
        'trailing_stages': [
            {'min_profit': 0, 'max_profit': r1, 'retention': -1},             
            {'min_profit': r1, 'retention': 0.95}
            ], 
        },
    'session': {
        'day_open': time(9, 0),
        'start_hour': 8, 'start_minute': 0, # Entry Window Start
        'end_hour': 21, 'end_minute': 00,   # Mandatory Close
        'ghost_start': time(3, 0),
        'ghost_end': time(4, 0)
        }, 
    }
# Timezones
CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc
UTC1 = pytz.timezone('Africa/Lagos')
UTC2 = pytz.timezone('Europe/Athens') # EET / CAT
UTC3 = pytz.timezone('Asia/Baghdad') # EAT / MST
LOCAL_ZONE = datetime.now(UTC).astimezone().tzinfo.tzname

# -------------------------------------------------------------------
# CUSTOM EXCEPTIONS
# -------------------------------------------------------------------
class TradingError(Exception):
    """Base exception for all trading-related errors."""
    pass

class MT5ConnectionError(TradingError):
    """Raised when MT5 connection fails."""
    pass

class MT5OperationError(TradingError):
    """Raised when MT5 operation fails."""
    pass

class ConfigurationError(TradingError):
    """Raised when configuration is invalid."""
    pass

class OrderExecutionError(TradingError):
    """Raised when order execution fails."""
    pass

class SymbolError(TradingError):
    """Raised when symbol operations fail."""
    pass

class TimezoneError(TradingError):
    """Raised when timezone operations fail."""
    pass

# -------------------------------------------------------------------
# RETRY DECORATOR
# -------------------------------------------------------------------
def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
    logger: Optional[logging.Logger] = None
):
    """
    Retry decorator with exponential backoff.
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        if logger:
                            logger.error(f"Operation failed after {max_attempts} attempts: {e}")
                        raise
                    
                    if logger:
                        logger.warning(f"Attempt {attempt}/{max_attempts} failed: {e}. "
                                      f"Retrying in {current_delay:.1f}s...")
                    
                    t_mod.sleep(current_delay)
                    current_delay *= backoff
            return None
        return wrapper
    return decorator

# -------------------------------------------------------------------
# HELPER CLASSES
# -------------------------------------------------------------------

class VelocityMonitor:
    """
    Live implementation of the density/velocity logic.
    Uses a deque to track tick timestamps efficiently.
    """
    def __init__(self, symbol, lookback_seconds=60):
        self.symbol = symbol
        self.tick_history = deque(maxlen=500)
        self.min_pip_threshold = 5 # Min pip movement within tick hist in biased direcion

        self.tick_timestamps = deque()
        self.density_history = deque(maxlen=lookback_seconds)
        self.last_update = t_mod.time()

    def get_server_timestamp(self):
        for attempt in range(MAX_RETRIES + 1):
            tick = mt5.symbol_info_tick(self.symbol)
            
            if tick is not None:
                return tick.time_msc / 1000 # Returning MS is better for high-frequency
                
            if attempt < MAX_RETRIES:
                # Exponential backoff or simple delay
                print(f"Retry {attempt + 1}/{MAX_RETRIES} for {self.symbol}...")
                time.sleep(0.1 * (attempt + 1)) 
            else:
                print(f"Failed to get tick for {self.symbol} after {MAX_RETRIES} retries.")
                
        return None
            

    def get_ticks(self, server_time=None):
        server_time = datetime.fromtimestamp(server_time, tz=timezone.utc)
        ticks = np.asarray([t for t in mt5.copy_ticks_from(self.symbol, server_time, 5000, mt5.COPY_TICKS_ALL) if t['time_msc']/1000 > server_time.timestamp()])
        if ticks is None:
            return np.array([])
        
        self.tick_history.extend(ticks)
        return ticks
    
    def get_tick_timestamps(self, server_time=None):
        ticks = self.get_ticks(server_time)
        timestamps = [int(t["time_msc"])/1000 for t in ticks]

        return timestamps
    
    def get_ticks_range(self, server_time=None):
        if server_time is None:
            server_time = self.get_server_timestamp()
            if server_time is None:
                return np.array([])
        
        # Use a larger batch size but limit frequency
        server_time = datetime.fromtimestamp(server_time, tz=timezone.utc)
        from_time = server_time - timedelta(seconds=self.density_history.maxlen//2)
        ticks = np.asarray([t for t in mt5.copy_ticks_range(self.symbol, from_time, server_time, mt5.COPY_TICKS_ALL)])
        if ticks is None or len(ticks) == 0:
            return np.array([])
        
        # Store history
        self.tick_history.extend(ticks)

        # Update density
        multiplier = self.density_history.maxlen//2
        current_density = len(ticks) / multiplier
        density_arr = [current_density] * self.density_history.maxlen
        self.density_history.extend(density_arr)

        # Extract timestamps efficiently using numpy
        timestamps = ticks['time_msc'] / 1000.0
        return timestamps


    def on_tick(self):
        now = self.get_server_timestamp()
        if len(self.tick_timestamps) == 0:
            timestamps = self.get_ticks_range(now)
        else:
            last_timestamp = self.tick_timestamps[-1]
            timestamps = self.get_tick_timestamps(last_timestamp)
        
        self.tick_timestamps.extend(timestamps)
        self.cleanup(now)

    def cleanup(self, now):
        # Remove ticks older than 30 seconds (Rolling 30S density)
        while self.tick_timestamps and self.tick_timestamps[0] < (now - 30):
            self.tick_timestamps.popleft()

    def update_history(self):
        # Called once per second to record density for the Moving Average
        now = self.get_server_timestamp()
        self.cleanup(now)
        current_density = len(self.tick_timestamps) / 30.0
        self.density_history.append(current_density)
        # print(f"HIST: {list(self.density_history)[-10:]}")

    def is_high_velocity(self, multiplier):
        if len(self.density_history) < 10: return False
        if t_mod.time() - self.last_update < 60 * 10: return False
        
        current_density = len(self.tick_timestamps) / 30.0
        avg_density = sum(self.density_history) / len(self.density_history)
        
        if avg_density == 0: return False
        return current_density > (avg_density * multiplier)
    
    def velocity_bias(self, contract_size=1):
        """ Gets the biasof price based on n ticks in history """

        n = 50
        sum_threshold = 10

        n_ticks = np.asarray(self.tick_history)[-n:]
        n_ticks = [(t["ask"] + t["bid"])/2 for t in n_ticks]
        n_ticks = n_ticks - n_ticks[0]

        m1 = n_ticks.sum() > sum_threshold
        m3 = n_ticks.sum() < -sum_threshold

        n = 500
        sum_threshold = 50
        n_ticks = np.asarray(self.tick_history)[-n:]
        n_ticks = [(t["ask"] + t["bid"])/2 for t in n_ticks]
        n_ticks = n_ticks - n_ticks[0]

        m2 = n_ticks.sum() > sum_threshold
        m4 = n_ticks.sum() < -sum_threshold

        n = 10
        sum_threshold = 5
        n_ticks = np.asarray(self.tick_history)[-n:]
        n_ticks = [(t["ask"] + t["bid"])/2 for t in n_ticks]
        n_ticks = n_ticks - n_ticks[0]

        m5 = n_ticks.sum() < -sum_threshold
        m6 = n_ticks.sum() > sum_threshold

        history = [(t["ask"] + t["bid"]) / 2 for t in self.tick_history]
        hist_sum = (history - history[0]).sum()
        # print(f"{m1} | {m2} | {m3} | {m4}")
        if m2 and m3 and m6:
            return "buy"
        elif m4 and m1 and m5:
            return "sell"
        else:
            return "straddle"

class StrategyState:
    """Keeps track of daily state to survive loop cycles."""
    def __init__(self):
        self.current_date = None
        self.bias = "straddle"
        self.ghost_high = None
        self.ghost_low = None
        self.daily_open_price = None
        self.touched_opposite = False
        
        # Trade Management
        self.in_trade = False
        self.max_pnl = 0.0
        self.current_profit_points = 0.0
        self.entry_price = 0.0
        self.direction = None # 'buy' or 'sell'

    def reset(self, new_date):
        print(f"--- NEW DAY: {new_date} ---")
        self.current_date = new_date
        self.bias = "straddle"
        self.ghost_high = None
        self.ghost_low = None
        self.daily_open_price = None
        # self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0
        self.current_profit_points = 0.00
    
    def close_trade(self):
        print(f"{self.bias} trade closed")
        self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0
        self.current_profit_points = 0.0

# -------------------------------------------------------------------
# CORE LOGIC
# -------------------------------------------------------------------
# Error: incorrect implementation, should use UTC2/3 for server
# Might not need server time? Trade CET
@retry(max_attempts=10, delay=1.0, logger=logger)
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

@retry(max_attempts=10, delay=1.0, logger=logger)
def get_server_time(symbol):
        utc_now = datetime.now(UTC)
        # Error: remove hardcodded hours, should be dynamic
        i = 0
        tick = mt5.symbol_info_tick(symbol)
        while tick is None and i < MAX_RETRIES:
            tick = mt5.symbol_info_tick(symbol)
            i += 1
        if tick is None:
            raise ConnectionError("Failed to get symbol data")
        # server_time = datetime.fromtimestamp(tick.time)
        server_time = pd.to_datetime(tick.time, unit='s')
        zone = get_server_timezone()
        server_time = zone.localize(server_time)

        return server_time

@retry(max_attempts=10, delay=1.0, logger=logger)
def get_server_time_cet(symbol):
    """Gets MT5 server time and converts to CET."""
    # Note: MT5 Usually returns time in Broker Time. 
    # We assume Broker Time is aligned with EU markets or we convert.
    # For safety, we trust the broker's current time struct.
    server_time = get_server_time(symbol)
    return server_time.astimezone(CET)

@retry(max_attempts=10, delay=1.0, logger=logger)
def calculate_daily_bias(symbol):
    """
    Calculates bias based on YESTERDAY'S D1 Candle.
    (c - l) / (h - l)
    """
    # Get 2 days of D1 data to ensure we have yesterday completed
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, 1)
    if rates is None or len(rates) == 0:
        print("Error fetching D1 data for Bias")
        return "straddle"

    r = rates[0]
    high, low, close = r['high'], r['low'], r['close']
    
    if high == low: return "straddle"
    
    rc = (close - low) / (high - low)
    
    if rc >= CONFIG['bias_filter']['buy_threshold']:
        return "buy"
    elif rc <= CONFIG['bias_filter']['sell_threshold']:
        return "sell"
    return "straddle"

@retry(max_attempts=3, delay=1.0, logger=logger)
def touched_opposite(symbol, bias, target):
    today = datetime.now()
    start_dt = UTC.localize(datetime.combine(today.date(), CONFIG['session']['day_open']))
    end_dt = UTC.localize(today)

    zones = {
        UTC2: 2,
        UTC3: 3,
    }
    server_zone = get_server_timezone()
    # Linux ser is in Londono (UTC) -1 from Local machine(UTC+1)
    offset = 1 if islinux else 0
    start_dt = start_dt + timedelta(hours=zones[server_zone] - offset)
    end_dt   = end_dt   + timedelta(hours=zones[server_zone] - offset)
    
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        print(f"Error fetching M1 data for opposite confirmation: {mt5.last_error()}")
        if get_server_time(symbol) < start_dt:
            print(f"    Server behind start time: {get_server_time(symbol).time()} < {start_dt.time()}")
        return False

    for r in rates:
        high, low, dt = r['high'], r['low'], r['time']
        dt = datetime.fromtimestamp(dt) - timedelta(hours=zones[server_zone] - offset)

        if bias == "buy":
            if low <= target:
                print(f"Touched Opposite (Buy setup). {target} at {dt.time()}")
                return True
        else:
            if high >= target:
                print(f"Touched Opposite (Sell setup). {target} at {dt.time()}")
                return True
    return False

@retry(max_attempts=3, delay=1.0, logger=logger)
def get_ghost_range(symbol, today_date):
    """
    Fetches M1 bars from CET to determine range.
    """
    # Construct CET times
    start_dt = datetime.combine(today_date, CONFIG['session']['ghost_start'])
    end_dt = datetime.combine(today_date, CONFIG['session']['ghost_end'])
    
    # We need to localize these to get correct UTC query for MT5
    # Assuming the machine is running in correct TZ or using timezone aware objects
    # tz = CET
    # start_dt = tz.localize(start_dt)
    # end_dt = tz.localize(end_dt)
    zones = {
        UTC2: 2,
        UTC3: 3,
    }
    server_zone = get_server_timezone()
    offset = 1 if islinux else 0
    start_dt = start_dt + timedelta(hours=zones[server_zone] - offset)
    end_dt   = end_dt   + timedelta(hours=zones[server_zone] - offset)

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        return None, None

    # Calculate Min/Max from the bars
    g_min = min(rates['low'])
    g_max = max(rates['high'])
    
    return g_min, g_max

@retry(max_attempts=3, delay=1.0, logger=logger)
def get_frankfurt_open(symbol, today_date):
    """
    Fetches M1 bars open price.
    """
    
    start_dt = datetime.combine(today_date, CONFIG['session']['day_open'])

    zones = {
        UTC2: 2,
        UTC3: 3,
    }
    server_zone = get_server_timezone()
    offset = 1 if islinux else 0
    start_dt = start_dt + timedelta(hours=zones[server_zone] - offset)

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, start_dt)
    if rates is None or len(rates) == 0:
        return None
    open_price = rates[0][1]

    return open_price

def get_minute_bias(symbol):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, 1)
    
    r = rates[0]
    high, low, close = r['high'], r['low'], r['close']
    
    if high == low: return "straddle"
    
    rc = (close - low) / (high - low)
    
    if rc >= CONFIG['bias_filter']['buy_threshold']:
        return "buy"
    elif rc <= CONFIG['bias_filter']['sell_threshold']:
        return "sell"
    return "straddle"


def get_filling_type(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    
    # Check bitmask for allowed modes
    # symbol_FILLING_FOK = 1
    # symbol_FILLING_IOC = 2
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    elif info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    else:
        # Default for Market Execution symbols
        return mt5.ORDER_FILLING_RETURN

@retry(max_attempts=3, delay=1.0, exceptions=(OrderExecutionError,), logger=logger)
def execute_trade(symbol, contract_size, direction, sl_pips):
    """Sends order to MT5"""
    tick = mt5.symbol_info_tick(symbol)
    spread = tick.ask - tick.bid
    filling = get_filling_type(symbol)

    sl_points = sl_pips / contract_size
    sl_points += spread
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": VOLUME,
        "type": mt5.ORDER_TYPE_BUY if direction == 'buy' else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if direction == 'buy' else tick.bid, # Error: Might need to add padding as broker might not allow entry close to current price
        "sl": (tick.ask - sl_points) if direction == 'buy' else (tick.bid + sl_points),
        "deviation": DEVIATION,
        "magic": MAGIC_NUM,
        "comment": f"Minute [{sys.platform}]",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise OrderExecutionError(f"Order Failed: {result.comment}")
        print(request)
        return False, 0.0
    
    print(f"Trade Executed: {direction} at {result.price}")
    return True, result.price

@retry(max_attempts=3, delay=1.0, exceptions=(OrderExecutionError,), logger=logger)
def close_position(symbol):
    """Closes all positions with our Magic Number"""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        print(f"Nothing to close on {symbol}")
        return
    for pos in positions:
        if 1:#pos.magic == MAGIC_NUM:
            tick = mt5.symbol_info_tick(symbol)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "price": tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask, # Error: Might need to add padding
                "deviation": DEVIATION,
                "magic": MAGIC_NUM,
                "comment": "Mandatory Close"
            }
            mt5.order_send(request)
            print("Mandatory Close Executed")


#@retry(max_attempts=3, delay=1.0, exceptions=(OrderExecutionError,), logger=logger)
def modify_sl(symbol, ticket, new_sl):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": new_sl,
        "magic": MAGIC_NUM
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"SL modification Failed: {result.comment}")
        print(f"{new_sl}")
        return False
    return True
    
        

# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def main(magic_num=0):
    parser = argparse.ArgumentParser(description="Live trading momentum based bot for HFM")

    parser.add_argument("symbol", help="symbol to be traded")
    parser.add_argument("--magic", type=int, default=magic_num, help="Unique magic number for bot")

    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    MAGIC_NUM = args.magic

    if not mt5.initialize():
        err = mt5.last_error()
        print(f"MT5 terminal initialization failed: {err}")
        raise ConnectionError(f"Could not connect to MT5 terminal: {err}")
    
    if not mt5.login(login=LOGIN, password=PASSWORD, server=SERVER):
        err = mt5.last_error()
        print(f"Login failed for account {LOGIN}: {err}")
        mt5.shutdown()
        raise PermissionError(f"MT5 login failed: {err}")

    # Check symbol
    if not mt5.symbol_select(symbol, True):
        print(f"symbol {symbol} not found")
        return

    state = StrategyState()
    velocity = VelocityMonitor(symbol, lookback_seconds=CONFIG['entry_conditions']['lookback_seconds'])
    contract_size = mt5.symbol_info(symbol).trade_contract_size
    buffer = CONFIG['entry_conditions']['buffer_pips']
    buffer /= contract_size
    
    print(f"Live Trading Started on {symbol}...")
    tick = mt5.symbol_info_tick(symbol)
    spread = tick.ask - tick.bid
    print(f"Spread at open is {spread}")
    
    last_update_seconds = t_mod.time()
    logged_m = 0


    start_t = time(CONFIG['session']['start_hour'], CONFIG['session']['start_minute'])
    end_t = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])

    bias = "straddle"
    now_cet = get_server_time_cet(symbol)
    entry_time = (now_cet - timedelta(minutes=5)).time()
    sl_mod = True

    while True:
        now_cet = get_server_time_cet(symbol)

        tick = mt5.symbol_info_tick(symbol)
        if tick is None: continue
        
        if (now_cet.minute % 1 == 0) and (now_cet.minute != logged_m) and (now_cet.time().second <= 1):
                logged_m = now_cet.minute
                bias = get_minute_bias(symbol)
                print(f"Bias for {now_cet.time()} = {bias}")
        
        # C. Check for existing positions (Recovery/Management)
        positions = mt5.positions_get(symbol=symbol)
        my_pos = [p for p in positions if p.magic == MAGIC_NUM]
        open_pos = len(my_pos) > 0
        # Reset trade metrics
        if state.in_trade and not open_pos:
            bias = "straddle"
            if state.current_profit_points < 0:
                pass
                #t_mod.sleep(30)
            state.close_trade()
        # elif not state.in_trade and len(my_pos) > 1:
        #     print("Unknown position open: ")
        #     for p in my_pos:
        #         print(p)
        state.in_trade = open_pos
        
        # -----------------------------------------------------------
        # EXIT / RISK MANAGEMENT LOGIC
        # -----------------------------------------------------------
        if state.in_trade:
            pos = my_pos[0]
            # Mandatory Close (Time)
            close_time = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
            if now_cet.time() >= close_time:
                print(f"{now_cet.time()} -> {close_time}")
                print("CLosing all Positions")
                close_position(symbol)
                state.in_trade = False
                continue
            # Trailing Stop Logic
            current_profit_points = (tick.bid - pos.price_open) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - tick.ask)
            # Adjust for point value
            current_profit_points *= contract_size # in pips
            
            if now_cet.time().second <= 1 and entry_time.minute != now_cet.time().minute:
                old_bias = bias
                bias = get_minute_bias(symbol)
                bias = old_bias if bias == "straddle" else bias
                
                if (old_bias != bias) or (current_profit_points > 0):
                    close_position(symbol)
                    print(f"[{now_cet.time()}]")
                    state.in_trade = False
                else:
                    spread = tick.ask - tick.bid
                    sl_points = CONFIG['risk_management']['initial_sl_pips'] / contract_size
                    sl_points += spread
                    new_sl = (tick.ask - sl_points) if pos.type == mt5.ORDER_TYPE_BUY else (tick.bid + sl_points)
                    
                    should_mod = False
                    if pos.type == mt5.ORDER_TYPE_BUY and (pos.sl == 0 or new_sl > pos.sl): should_mod = True
                    if pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or new_sl < pos.sl): should_mod = True
                    
                    if should_mod:
                        print(f"Modified SL: {pos.sl} => {new_sl}")
                        modify_sl(symbol, pos.ticket, new_sl)
            
            if current_profit_points > state.max_pnl:
                print(f"Max profit pips: {state.max_pnl} -> {current_profit_points}")
            
            
            if not sl_mod: # sl_mod prev failed
                state.max_pnl = 0.0
                
            state.max_pnl = max(state.max_pnl, current_profit_points)
            state.current_profit_points = current_profit_points
            
            # Check Stages
            best_retention = 0.0
            triggered = False
            
            for s in CONFIG['risk_management']['trailing_stages']:
                # print(f"{state.max_pnl} || {s['min_profit']}")
                if state.max_pnl >= s['min_profit']:
                    best_retention = s['retention']
                    triggered = True
            # best_retention = 0.5
            if triggered:
                # Calculate new SL
                # print(f"{best_retention=}")
                if best_retention == -1:
                    # Leave sl at original
                    pass
                else:
                    trail_dist = state.max_pnl * best_retention
                    trail_dist /= contract_size
                    new_sl = (pos.price_open + trail_dist) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - trail_dist)
                    new_sl = round(new_sl, 2)
                    # print(f"{trail_dist=}")
                    # print(f"{new_sl=}")
                    # Only modify if new SL is better (Higher for Buy, Lower for Sell)
                    should_mod = False
                    if pos.type == mt5.ORDER_TYPE_BUY and (pos.sl == 0 or new_sl > pos.sl): should_mod = True
                    if pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or new_sl < pos.sl): should_mod = True
                    
                    if should_mod:
                        print(f"Modified SL: {pos.sl} => {new_sl}")
                        sl_mod = modify_sl(symbol, pos.ticket, new_sl)

        # -----------------------------------------------------------
        # ENTRY LOGIC
        # -----------------------------------------------------------
        else:
            # bias = get_minute_bias(symbol)
            # Time Window Check
            if start_t <= now_cet.time() < end_t:
                # if velocity.velocity_bias() != bias:
                #     continue

                # BUY LOGIC
                if bias == 'buy':
                    success, price = execute_trade(symbol, contract_size, 'buy', CONFIG['risk_management']['initial_sl_pips'])
                    print(f"Entered Buy at {now_cet} [{tick.ask}]")
                    if success:
                        state.in_trade = True
                        state.entry_price = price
                        entry_time = now_cet.time()

                # SELL LOGIC
                elif bias == 'sell':
                    success, price = execute_trade(symbol, contract_size, 'sell', CONFIG['risk_management']['initial_sl_pips'])
                    print(f"Entered Sell at {now_cet} [{tick.bid}]")
                    if success:
                        state.in_trade = True
                        state.entry_price = price
                        entry_time = now_cet.time()
            else:
                t = datetime.now().astimezone(UTC1).time()
                t = str(t).split(".")[0]
                print(f"[ {t} ] Outside trading hours")
                t_mod.sleep(60)

if __name__ == "__main__":
    try:
        main(magic_num=MAGIC_NUM)
    except KeyboardInterrupt:
        print("Stopping Bot...")
        mt5.shutdown()
