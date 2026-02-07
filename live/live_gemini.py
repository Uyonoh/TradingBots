import argparse
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, timedelta, timezone, date, time
import calendar
import time as t_mod
from collections import deque
import os
import dotenv
dotenv.load_dotenv()

# Error: Check direction during high velocity, might be opposing

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------
LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]
MAX_RETRIES = 5
TIMEOUT = 1

# symbol = "GBPUSD"  # Update for your broker (e.g., DE40, DAX40)
VOLUME = 0.01      # Lot size
DEVIATION = 10    # Slippage tolerance in points
MAGIC_NUM = 1234569

# Strategy Parameters (Matching your backtest)
CONFIG = {
    'bias_filter': {'buy_threshold': 0.75, 'sell_threshold': 0.25},
    'entry_conditions': {
        'velocity_multiplier': 1.8,
        'lookback_period': 60 * 60,  # In seconds (approx matching rolling window)
        '15min_buffer': 10.0     # Points buffer for ghost range
    },
    'risk_management': {
        'initial_sl': 50.0,     # Points
        'trailing_stages': [
            {'min_profit': 0, 'max_profit': np.int64(30), 'retention': -1}, 
            {'min_profit': np.int64(30), 'max_profit': np.int64(60), 'retention': 0.55}, 
            {'min_profit': np.int64(60), 'max_profit': np.int64(90), 'retention': 0.6}, 
            {'min_profit': np.int64(90), 'max_profit': np.int64(120), 'retention': 0.65}, 
            {'min_profit': np.int64(120), 'max_profit': np.int64(150), 'retention': 0.7}, 
            {'min_profit': np.int64(150), 'retention': 0.95}
            ],
    },
    'session': {
        'start_hour': 5, 'start_minute': 0, # Entry Window Start #Error: convert these to time()
        'end_hour': 23, 'end_minute': 30,   # Mandatory Close
        'ghost_start': time(8, 0),
        'ghost_end': time(8, 15)
    }
}
CONFIG = {
    'bias_filter': {'buy_threshold': 0.6, 'sell_threshold': 0.4}, 
    'entry_conditions': {'15min_buffer': 10, 'velocity_multiplier': 2, 'lookback_period': 60*60}, 
    'risk_management': {
        'initial_sl': 50, 
        'trailing_stages': [
            {'min_profit': 0,   'max_profit': 30,  'retention': -1}, 
            {'min_profit': 30,  'max_profit': 60,  'retention': 0.5}, 
            {'min_profit': 60,  'max_profit': 90,  'retention': 0.7}, 
            {'min_profit': 90,  'max_profit': 120, 'retention': 0.8}, 
            {'min_profit': 120, 'max_profit': 150, 'retention': 0.9}, 
            {'min_profit': 150, 'retention': 0.95}
            ]
        },
    'session': {
        'day_open': time(9, 0),
        'start_hour': 10, 'start_minute': 0, # Entry Window Start
        'end_hour': 17, 'end_minute': 00,   # Mandatory Close
        'ghost_start': time(8, 0),
        'ghost_end': time(8, 30)
        }, 
    }
# Timezones
CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc
UTC2 = pytz.timezone('Europe/Athens') # EET / CAT
UTC3 = pytz.timezone('Asia/Baghdad') # EAT / MST

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
        ticks = mt5.copy_ticks_from(self.symbol, server_time, 10000, mt5.COPY_TICKS_ALL)
        return ticks[1:]
    
    def get_tick_timestamps(self, server_time=None):
        ticks = self.get_ticks(server_time)
        timestamps = [int(t["time_msc"])/1000 for t in ticks]

        return timestamps


    def on_tick(self):
        now = self.get_server_timestamp()
        if len(self.tick_timestamps) == 0:
            timestamps = self.get_tick_timestamps(now)
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

    def is_high_velocity(self, multiplier):
        if len(self.density_history) < 10: return False
        if t_mod.time() - self.last_update < 60 * 10: return False
        
        current_density = len(self.tick_timestamps) / 30.0
        avg_density = sum(self.density_history) / len(self.density_history)
        
        if avg_density == 0: return False
        return current_density > (avg_density * multiplier)

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
        self.entry_price = 0.0
        self.direction = None # 'buy' or 'sell'

    def reset(self, new_date):
        print(f"--- NEW DAY: {new_date} ---")
        self.current_date = new_date
        self.bias = "straddle"
        self.ghost_high = None
        self.ghost_low = None
        self.daily_open_price = None
        self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0
    
    def close_trade(self):
        print(f"{self.bias} trade closed")
        self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0

# -------------------------------------------------------------------
# CORE LOGIC
# -------------------------------------------------------------------
# Error: incorrect implementation, should use UTC2/3 for server
# Might not need server time? Trade CET
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

def get_server_time(symbol):
        utc_now = datetime.now(UTC)
        # Error: remove hardcodded hours, should be dynamic
        tick = mt5.symbol_info_tick(symbol)
        # server_time = datetime.fromtimestamp(tick.time)
        server_time = pd.to_datetime(tick.time, unit='s')
        zone = get_server_timezone()
        server_time = zone.localize(server_time)

        return server_time

def get_server_time_cet(symbol):
    """Gets MT5 server time and converts to CET."""
    # Note: MT5 Usually returns time in Broker Time. 
    # We assume Broker Time is aligned with EU markets or we convert.
    # For safety, we trust the broker's current time struct.
    server_time = get_server_time(symbol)
    return server_time.astimezone(CET)

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

def touched_opposite(symbol, bias, target):
    today = datetime.now()
    start_dt = datetime.combine(today, CONFIG['session']['day_open'])
    end_dt = today

    zones = {
        UTC2: 2,
        UTC3: 3,
    }
    server_zone = get_server_timezone()
    start_dt = start_dt + timedelta(hours=zones[server_zone])
    end_dt   = end_dt   + timedelta(hours=zones[server_zone])

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        print("Error fetching M1 data for opposite confirmation")
        return False

    for r in rates:
        high, low, close = r['high'], r['low'], r['close']

        if bias == "buy":
            if low <= target:
                return True
        else:
            if high >= target:
                return True
    return False

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
    start_dt = start_dt + timedelta(hours=zones[server_zone])
    end_dt   = end_dt   + timedelta(hours=zones[server_zone])

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        return None, None

    # Calculate Min/Max from the bars
    g_min = min(rates['low'])
    g_max = max(rates['high'])
    
    return g_min, g_max

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
    start_dt = start_dt + timedelta(hours=zones[server_zone])

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, start_dt)
    if rates is None or len(rates) == 0:
        return None
    open_price = rates[0][1]

    return open_price

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

def execute_trade(symbol, contract_size, direction, sl_pips):
    """Sends order to MT5"""
    tick = mt5.symbol_info_tick(symbol)
    filling = get_filling_type(symbol)

    sl_points = sl_pips / contract_size
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": VOLUME,
        "type": mt5.ORDER_TYPE_BUY if direction == 'buy' else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if direction == 'buy' else tick.bid, # Error: Might need to add padding as broker might not allow entry close to current price
        "sl": (tick.ask - sl_points) if direction == 'buy' else (tick.bid + sl_points),
        "deviation": DEVIATION,
        "magic": MAGIC_NUM,
        "comment": "LiveDemo_Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"Order Failed: {result.comment}")
        print(request)
        return False, 0.0
    
    print(f"Trade Executed: {direction} at {result.price}")
    return True, result.price

def close_position(symbol):
    """Closes all positions with our Magic Number"""
    positions = mt5.positions_get(symbol)
    for pos in positions:
        if pos.magic == MAGIC_NUM:
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
    
        

# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Live trading momentum based bot for HFM")

    parser.add_argument("symbol", help="symbol to be traded")

    args = parser.parse_args()

    symbol = args.symbol.strip().upper()

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
    velocity = VelocityMonitor(symbol, lookback_seconds=CONFIG['entry_conditions']['lookback_period'])
    contract_size = mt5.symbol_info(symbol).trade_contract_size
    
    print(f"Live Trading Started on {symbol}...")
    
    last_update_seconds = t_mod.time()

    while True:
        # 1. Hardware Efficiency: Sleep to reduce CPU usage
        t_mod.sleep(0.1) 
        
        # 2. Update Time
        now = get_server_time(symbol)
        now_cet = get_server_time_cet(symbol)
        today_date = now.date()
        # print(f"{now=}")
        # print(f"{now_cet=}")

        # session_start = time(CONFIG['session']['start_hour'], CONFIG['session']['start_minute'])
        # session_end = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
        # session_start = CET.localize(datetime.combine(date.today(), session_start))
        # session_end = CET.localize(datetime.combine(date.today(), session_end))
        # if now_cet.time() < (session_start - timedelta(minutes=65)).time():
        #     print(now_cet)
        #     print(f"Session starts at {session_start}: T - {(session_start - now_cet)} to initiation")
        #     t_mod.sleep(60)
        #     continue
        # if now_cet.time() > (session_end + timedelta(minutes=5)).time():
        #     print("Session Ended")
        #     print(now_cet)
        #     t_mod.sleep(60 * 5)
        #     continue
        
        # 3. New Day Logic
        if state.current_date != today_date:
            state.reset(today_date)
            state.bias = calculate_daily_bias(symbol)
            print(f"Daily Bias Calculated: {state.bias}")

        # 4. Data Processing (Tick)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None: continue
        
        velocity.on_tick()
        # print(f"30S tikcs: {len(velocity.tick_timestamps)}")
        
        # Update Velocity History every 1 second
        if t_mod.time() - last_update_seconds >= 1.0:
            avg_vel = sum(velocity.density_history)  / max(len(velocity.density_history), 1) #prevent division by zero
            velocity.update_history()
            last_update_seconds = t_mod.time()

            if (datetime.now().time().minute % 5 == 0) and (datetime.now().time().second == 0):
                print(f"Tick velosity density at {datetime.now().time()}: {velocity.density_history[-1]} || AVG: {avg_vel}")

        # 5. Logic Gates
        
        # A. Capture Ghost Range (Runs once after 08:15)
        if state.ghost_high is None:
            if now_cet.time() > CONFIG['session']['ghost_end']:
                g_min, g_max = get_ghost_range(symbol, today_date)
                if g_min:
                    state.ghost_low = g_min
                    state.ghost_high = g_max
                    print(f"Ghost Range Locked: {g_min} - {g_max}")
                else:
                    print("Waiting for Ghost Data...")
                    t_mod.sleep(5)
                    continue

        # B. Capture Daily Open (Frankfurt 09:00)
        if state.daily_open_price is None:
            # If it is 09:00 or later
            target_open = CONFIG['session']['day_open']
            if now_cet.time() >= target_open:
                print(now_cet)
                # Error: add error handling
                state.daily_open_price = get_frankfurt_open(symbol, today_date) #tick.ask if state.bias == "buy" else tick.bid # Approximate open with current Ask
                print(f"Market Open Price Recorded: {state.daily_open_price}")

        # C. Check for existing positions (Recovery/Management)
        positions = mt5.positions_get(symbol=symbol)
        my_pos = [p for p in positions if p.magic == MAGIC_NUM]
        open_pos = len(my_pos) > 0

        # Reset trade metrics
        if state.in_trade and not open_pos:
            state.close_trade()
        elif not state.in_trade and len(my_pos) > 1:
            print("Unknown position open: ")
            for p in my_pos:
                print(p)

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
            
            if current_profit_points > state.max_pnl:
                print(f"Max profit pips: {state.max_pnl} -> {current_profit_points}")
            
            state.max_pnl = max(state.max_pnl, current_profit_points)
            
            # Check Stages
            best_retention = 0.0
            triggered = False
            
            for s in CONFIG['risk_management']['trailing_stages']:
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
                        modify_sl(symbol, pos.ticket, new_sl)

        # -----------------------------------------------------------
        # ENTRY LOGIC
        # -----------------------------------------------------------
        elif state.bias != "straddle" and state.ghost_high is not None and state.daily_open_price is not None:
            
            # Time Window Check
            start_t = time(CONFIG['session']['start_hour'], CONFIG['session']['start_minute'])
            end_t = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
            
            if start_t <= now_cet.time() < end_t:
                
                buffer = CONFIG['entry_conditions']['15min_buffer']
                buffer /= contract_size
                
                # BUY LOGIC
                if state.bias == 'buy':
                    # 1. Touch Opposite (Trap)
                    lower_target = state.ghost_low + buffer
                    if tick.ask <= lower_target:
                        if not state.touched_opposite:
                            print(f"Trap: Touched Opposite Low at {lower_target} (Buy Setup)")
                            state.touched_opposite = True
                    # Error: Might add continue to prevent tick spikes/maddness
                    # 2. Trigger
                    if state.touched_opposite:
                        # Price back above High - Buffer
                        upper_target = state.ghost_high - buffer
                        if tick.ask >= upper_target:
                            # Above Daily Open
                            if tick.ask > state.daily_open_price:
                                # High Velocity
                                if velocity.is_high_velocity(CONFIG['entry_conditions']['velocity_multiplier']):
                                    print(f"Price >= {upper_target} and  > {state.daily_open_price}")
                                    print(now_cet)
                                    print(f"Entering Buy")
                                    # continue
                                    success, price = execute_trade(symbol, contract_size, 'buy', CONFIG['risk_management']['initial_sl'])
                                    if success:
                                        state.in_trade = True
                                        state.entry_price = price
                                        state.touched_opposite = False # Reset

                # SELL LOGIC
                elif state.bias == 'sell':
                    # 1. Touch Opposite (Trap)
                    upper_target = state.ghost_high - buffer
                    if tick.bid >= upper_target:
                        if not state.touched_opposite:
                            print(f"Trap: Touched Opposite High at{upper_target} (Sell Setup)")
                            state.touched_opposite = True
                    # Error: Might add continue to prevent tick spikes/maddness
                    # 2. Trigger
                    if state.touched_opposite:
                        lower_target = state.ghost_low + buffer
                        if tick.bid <= lower_target:
                            if tick.bid < state.daily_open_price:
                                if velocity.is_high_velocity(CONFIG['entry_conditions']['velocity_multiplier']):
                                    print(f"Price <= {lower_target} and < {state.daily_open_price}")
                                    print(now_cet)
                                    print(f"Entering Sell")
                                    # continue
                                    success, price = execute_trade(symbol, contract_size, 'sell', CONFIG['risk_management']['initial_sl'])
                                    if success:
                                        state.in_trade = True
                                        state.entry_price = price
                                        state.touched_opposite = False

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopping Bot...")
        mt5.shutdown()
