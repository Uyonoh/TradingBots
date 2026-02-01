import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, timedelta, time
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

SYMBOL = "#BTCUSD"  # Update for your broker (e.g., DE40, DAX40)
VOLUME = 0.01      # Lot size
DEVIATION = 10    # Slippage tolerance in points
MAGIC_NUM = 123456

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
        'start_hour': 5, 'start_minute': 0, # Entry Window Start
        'end_hour': 23, 'end_minute': 30,   # Mandatory Close
        'ghost_start': time(8, 0),
        'ghost_end': time(8, 15)
    }
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
    def __init__(self, lookback_seconds=60):
        self.tick_timestamps = deque()
        self.density_history = deque(maxlen=lookback_seconds)
        self.last_update = t_mod.time()

    def get_server_timestamp(self):
        utc_now = datetime.now(UTC)
        # Error: remove hardcodded hours, should be dynamic
        server_diff = timedelta(hours=2) # UTC+2
        server_time = utc_now + server_diff

        return server_time.timestamp()

    def get_ticks(self, server_time=None):
        if server_time is None:
            server_time = self.get_server_timestamp()

        ticks = mt5.copy_ticks_from(SYMBOL, server_time, 10000, mt5.COPY_TICKS_ALL)
        return ticks
    
    def get_tick_timestamps(self, server_time=None):
        ticks = self.get_ticks(server_time)
        timestamps = [int(t["time_msc"])/1000 for t in ticks]

        return timestamps


    def on_tick(self):
        now = self.get_server_timestamp()
        if len(self.tick_timestamps) == 0:
            timestamps = self.get_tick_timestamps()
        else:
            last_timestamp = self.tick_timestamps[-1]
            timestamps = self.get_tick_timestamps(last_timestamp + 0.001)

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
        if len(self.density_history) < 1: return False
        
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

# -------------------------------------------------------------------
# CORE LOGIC
# -------------------------------------------------------------------
# Error: incorrect implementation, should use UTC2/3 for server
# Might not need server time? Trade CET
def get_server_time():
        utc_now = datetime.now(UTC)
        # Error: remove hardcodded hours, should be dynamic
        server_diff = timedelta(hours=2) # UTC+2
        server_time = utc_now + server_diff

        return server_time

def get_server_time_cet():
    """Gets MT5 server time and converts to CET."""
    # Note: MT5 Usually returns time in Broker Time. 
    # We assume Broker Time is aligned with EU markets or we convert.
    # For safety, we trust the broker's current time struct.
    server_time = get_server_time()
    return server_time.astimezone(CET)

def calculate_daily_bias():
    """
    Calculates bias based on YESTERDAY'S D1 Candle.
    (c - l) / (h - l)
    """
    # Get 2 days of D1 data to ensure we have yesterday completed
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_D1, 1, 1)
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

def get_ghost_range(today_date):
    """
    Fetches M1 bars from 08:00 to 08:15 CET to determine range.
    """
    # Construct CET times
    start_dt = datetime.combine(today_date, CONFIG['session']['ghost_start'])
    end_dt = datetime.combine(today_date, CONFIG['session']['ghost_end'])
    
    # We need to localize these to get correct UTC query for MT5
    # Assuming the machine is running in correct TZ or using timezone aware objects
    tz = CET
    start_dt = tz.localize(start_dt)
    end_dt = tz.localize(end_dt)

    rates = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        return None, None

    # Calculate Min/Max from the bars
    g_min = min(rates['low'])
    g_max = max(rates['high'])
    
    return g_min, g_max

def execute_trade(direction, sl_points):
    """Sends order to MT5"""
    tick = mt5.symbol_info_tick(SYMBOL)
    point = mt5.symbol_info(SYMBOL).point
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": VOLUME,
        "type": mt5.ORDER_TYPE_BUY if direction == 'buy' else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if direction == 'buy' else tick.bid, # Error: Might need to add padding as broker might not allow entry close to current price
        "sl": (tick.ask - sl_points * point) if direction == 'buy' else (tick.bid + sl_points * point),
        "deviation": DEVIATION,
        "magic": MAGIC_NUM,
        "comment": "LiveDemo_Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"Order Failed: {result.comment}")
        return False, 0.0
    
    print(f"Trade Executed: {direction} at {result.price}")
    return True, result.price

def close_position():
    """Closes all positions with our Magic Number"""
    positions = mt5.positions_get(symbol=SYMBOL)
    for pos in positions:
        if pos.magic == MAGIC_NUM:
            tick = mt5.symbol_info_tick(SYMBOL)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": SYMBOL,
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

def modify_sl(ticket, new_sl):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": SYMBOL,
        "position": ticket,
        "sl": new_sl,
        "magic": MAGIC_NUM
    }
    mt5.order_send(request)

# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def main():
    if not mt5.initialize():
        err = mt5.last_error()
        print(f"MT5 terminal initialization failed: {err}")
        raise ConnectionError(f"Could not connect to MT5 terminal: {err}")
    
    if not mt5.login(login=LOGIN, password=PASSWORD, server=SERVER):
        err = mt5.last_error()
        print(f"Login failed for account {LOGIN}: {err}")
        mt5.shutdown()
        raise PermissionError(f"MT5 login failed: {err}")

    # Check Symbol
    if not mt5.symbol_select(SYMBOL, True):
        print(f"Symbol {SYMBOL} not found")
        return

    state = StrategyState()
    velocity = VelocityMonitor(lookback_seconds=CONFIG['entry_conditions']['lookback_period'])
    
    print(f"Live Trading Started on {SYMBOL}...")
    
    last_update_seconds = t_mod.time()

    while True:
        # 1. Hardware Efficiency: Sleep to reduce CPU usage
        t_mod.sleep(0.1) 
        
        # 2. Update Time
        now = get_server_time()
        now_cet = get_server_time_cet()
        today_date = now.date()
        
        # 3. New Day Logic
        if state.current_date != today_date:
            state.reset(today_date)
            state.bias = calculate_daily_bias()
            print(f"Daily Bias Calculated: {state.bias}")

        # 4. Data Processing (Tick)
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None: continue
        
        velocity.on_tick()
        # print(f"30S tikcs: {len(velocity.tick_timestamps)}")
        
        # Update Velocity History every 1 second
        if t_mod.time() - last_update_seconds >= 1.0:
            velocity.update_history()
            last_update_seconds = t_mod.time()

            if (datetime.now().time().minute % 5 == 0) and (datetime.now().time().second == 0):
                print(f"Tick velosity density at {datetime.now().time()}: {velocity.density_history[-1]}")

        # 5. Logic Gates
        
        # A. Capture Ghost Range (Runs once after 08:15)
        if state.ghost_high is None:
            if now_cet.time() > CONFIG['session']['ghost_end']:
                g_min, g_max = get_ghost_range(today_date)
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
            target_open = time(CONFIG['session']['start_hour'], 0)
            if now_cet.time() >= target_open:
                # Error: might need to use assk/bid based on sell/buy
                state.daily_open_price = tick.ask # Approximate open with current Ask
                print(f"Market Open Price Recorded: {state.daily_open_price}")

        # C. Check for existing positions (Recovery/Management)
        positions = mt5.positions_get(symbol=SYMBOL)
        my_pos = [p for p in positions if p.magic == MAGIC_NUM]
        state.in_trade = len(my_pos) > 0
        
        # -----------------------------------------------------------
        # EXIT / RISK MANAGEMENT LOGIC
        # -----------------------------------------------------------
        if state.in_trade:
            pos = my_pos[0]
            
            # Mandatory Close (Time)
            close_time = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
            if now_cet.time() >= close_time:
                print("CLosing all Positions")
                close_position()
                state.in_trade = False
                continue

            # Trailing Stop Logic
            current_profit_points = (tick.bid - pos.price_open) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - tick.ask)
            # Adjust for point value
            point = mt5.symbol_info(SYMBOL).point
            current_profit_points /= point #in pips
            
            state.max_pnl = max(state.max_pnl, current_profit_points)
            
            # Check Stages
            best_retention = 0.0
            triggered = False
            
            for s in CONFIG['risk_management']['trailing_stages']:
                if state.max_pnl >= s['min_profit']:
                    best_retention = s['retention']
                    triggered = True
            
            if triggered:
                # Calculate new SL
                if best_retention == -1:
                    # Error: should set to original sl
                     # Break even + 1 point
                    # sl = CONFIG['risk_management']['initial_sl']
                    # new_sl = pos.price_open + (1.0 * point) if pos.type == mt5.ORDER_TYPE_BUY else pos.price_open - (1.0 * point)
                    pass
                else:
                    trail_dist = state.max_pnl * best_retention * point
                    new_sl = (tick.bid - trail_dist) if pos.type == mt5.ORDER_TYPE_BUY else (tick.ask + trail_dist)
                
                # Only modify if new SL is better (Higher for Buy, Lower for Sell)
                should_mod = False
                if pos.type == mt5.ORDER_TYPE_BUY and new_sl > pos.sl: should_mod = True
                if pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or new_sl < pos.sl): should_mod = True
                
                if should_mod:
                    print("Modified SL")
                    modify_sl(pos.ticket, new_sl)

        # -----------------------------------------------------------
        # ENTRY LOGIC
        # -----------------------------------------------------------
        elif state.bias != "straddle" and state.ghost_high is not None and state.daily_open_price is not None:
            
            # Time Window Check
            start_t = time(CONFIG['session']['start_hour'], CONFIG['session']['start_minute'])
            end_t = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
            
            if start_t <= now_cet.time() < end_t:
                
                buffer = CONFIG['entry_conditions']['15min_buffer']
                
                # BUY LOGIC
                if state.bias == 'buy':
                    # 1. Touch Opposite (Trap)
                    if tick.ask <= state.ghost_low + buffer:
                        if not state.touched_opposite:
                            print("Trap: Touched Opposite Low (Buy Setup)")
                            state.touched_opposite = True
                    # Error: Might add continue to prevent tick spikes/maddness
                    # 2. Trigger
                    if state.touched_opposite:
                        # Price back above High - Buffer
                        if tick.ask >= state.ghost_high - buffer:
                            # Above Daily Open
                            if tick.ask > state.daily_open_price:
                                # High Velocity
                                if velocity.is_high_velocity(CONFIG['entry_conditions']['velocity_multiplier']):
                                    print(f"Entering Buy")
                                    # continue
                                    success, price = execute_trade('buy', CONFIG['risk_management']['initial_sl'])
                                    if success:
                                        state.in_trade = True
                                        state.entry_price = price
                                        state.touched_opposite = False # Reset

                # SELL LOGIC
                elif state.bias == 'sell':
                    # 1. Touch Opposite (Trap)
                    if tick.bid >= state.ghost_high - buffer:
                        if not state.touched_opposite:
                            print("Trap: Touched Opposite High (Sell Setup)")
                            state.touched_opposite = True
                    # Error: Might add continue to prevent tick spikes/maddness
                    # 2. Trigger
                    if state.touched_opposite:
                        if tick.bid <= state.ghost_low + buffer:
                            if tick.bid < state.daily_open_price:
                                if velocity.is_high_velocity(CONFIG['entry_conditions']['velocity_multiplier']):
                                    print(f"Entering Sell")
                                    print(tick)
                                    # continue
                                    success, price = execute_trade('sell', CONFIG['risk_management']['initial_sl'])
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