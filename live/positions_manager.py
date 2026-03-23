import argparse
import pandas as pd
import numpy as np
import pytz
from datetime import datetime, timedelta, timezone, date, time
import calendar
import time as t_mod
from collections import deque
import os
import sys
import dotenv

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
LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]
MAX_RETRIES = 5
TIMEOUT = 1

DEVIATION = 10    # Slippage tolerance in points
MAGIC_NUM += "0234"
MAGIC_NUM = int(MAGIC_NUM)
r1 = 50

CONFIG = {
    'bias_filter': {'buy_threshold': 0.6, 'sell_threshold': 0.4}, 
    'entry_conditions': {'buffer_pips': 20, 'velocity_multiplier': 2, 'lookback_seconds': 60*60}, 
    'risk_management': {
        'initial_sl_pips': 50, 
        'trailing_stages': [
            {'min_profit': 0,   'max_profit': 70,  'retention': -1}, 
            {'min_profit': 70,  'max_profit': 90,  'retention': 0.7}, 
            {'min_profit': 90,  'max_profit': 120, 'retention': 0.9}, 
            {'min_profit': 120, 'retention': 0.95}
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
        self.curr_pnl = 0.0
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
    
    def close_trade(self):
        print(f"{self.bias} trade closed")
        self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0


def close_positions(symbol, magic):
    """Closes all positions with our Magic Number"""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return
    for pos in positions:
        if pos.magic == magic:
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


def delete_orders(symbol, magic):
    """Delete all orders with our Magic Number"""
    orders = mt5.orders_get(symbol=symbol)
    if orders is None:
        return
    for pos in orders:
        if pos.magic == magic:
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": pos.ticket,
                "comment": "Mandatory Close"
            }
            mt5.order_send(request)
            print("Pending Order Deleted")

    
        
#  ------------------------------------------------------------------
# TIME
#  ------------------------------------------------------------------

CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc
UTC2 = pytz.timezone('Europe/Athens') # EET / CAT
UTC3 = pytz.timezone('Asia/Baghdad') # EAT / MST
LOCAL_ZONE = datetime.now(UTC).astimezone().tzinfo.tzname

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

def get_server_time_cet(symbol):
    """Gets MT5 server time and converts to CET."""
    # Note: MT5 Usually returns time in Broker Time. 
    # We assume Broker Time is aligned with EU markets or we convert.
    # For safety, we trust the broker's current time struct.
    server_time = get_server_time(symbol)
    return server_time.astimezone(CET)


# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Live trading momentum based bot for HFM")

    parser.add_argument("symbol", help="symbol to be traded")
    parser.add_argument("--magic", type=int, default=123456, help="Unique magic number for bot")

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

    contract_size = mt5.symbol_info(symbol).trade_contract_size
    
    print(f"Postion Manager Started on {symbol}...")
    
    last_update_seconds = t_mod.time()
    logged_m = 0

    positions = mt5.positions_get(symbol=symbol)
    my_pos = [p for p in positions if p.magic == MAGIC_NUM]
    states = {}
    

    while True:
        # 1. Hardware Efficiency: Sleep to reduce CPU usage
        t_mod.sleep(0.1) 
        
        # 2. Update Time
        now_cet = get_server_time_cet(symbol)

        # C. Check for existing positions (Recovery/Management)
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            print("Failed to retrieve positions:", mt5.last_error())
            continue

        my_pos = [p for p in positions if p.magic == MAGIC_NUM]
        open_pos = len(my_pos) > 0
        tick = mt5.symbol_info_tick(symbol)

        for pos in my_pos:
            if pos.ticket not in states:
                states[pos.ticket] = StrategyState()
                states[pos.ticket].in_trade = True

        # if not open_pos:
        #     print(f"No positions found for {symbol}, exiting...")
        #     return
        
        total_loss = sum(pos.profit for pos in my_pos if pos.profit < 0)
        max_loss = -3.5
        if total_loss < (max_loss):
            print(f"Loss {total_loss} beyond {max_loss}")
            print(f"    Aborting trade")
            close_positions(symbol, MAGIC_NUM)
            delete_orders(symbol, MAGIC_NUM)
        

        
        # -----------------------------------------------------------
        # EXIT / RISK MANAGEMENT LOGIC
        # -----------------------------------------------------------
        for i, pos in enumerate(my_pos):
            # Mandatory Close (Time)
            close_time = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
            # if now_cet.time() >= close_time:
            #     print(f"{now_cet.time()} -> {close_time}")
            #     print("CLosing all Positions with magic '{MAGIC_NUM}'")
            #     close_positions(symbol)
            #     continue
        
            # Trailing Stop Logic
            current_profit_points = (tick.bid - pos.price_open) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - tick.ask)
            # Adjust for point value
            current_profit_points *= contract_size # in pips
            
            if current_profit_points > states[pos.ticket].max_pnl:
                print(f"Max profit pips: {states[pos.ticket].max_pnl} -> {current_profit_points}")
            
            states[pos.ticket].max_pnl = max(states[pos.ticket].max_pnl, current_profit_points)
            states[pos.ticket].curr_pnl = current_profit_points
            
            # Check Stages
            best_retention = 0.0
            triggered = False
            tp_ratio = 1
            if pos.tp != 0.0:
                tp_pips = abs(pos.price_open - pos.tp) / contract_size
                tp_ratio = tp_pips / 100 # TODO: 100 is based on the max tp of set
            
            for s in CONFIG['risk_management']['trailing_stages']:
                if states[pos.ticket].max_pnl >= s['min_profit'] * tp_ratio:
                    best_retention = s['retention']
                    triggered = True

            if triggered:
                # Calculate new SL
                if best_retention == -1:
                    # Leave sl at original
                    pass
                else:
                    trail_dist = states[pos.ticket].max_pnl * best_retention
                    trail_dist /= contract_size
                    new_sl = (pos.price_open + trail_dist) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - trail_dist)
                    new_sl = round(new_sl, 2)
                    # Only modify if new SL is better (Higher for Buy, Lower for Sell)
                    should_mod = False
                    if pos.type == mt5.ORDER_TYPE_BUY and (pos.sl == 0 or new_sl > pos.sl): should_mod = True
                    if pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or new_sl < pos.sl): should_mod = True
                    
                    if should_mod:
                        print(f"Modified SL: {pos.sl} => {new_sl}")
                        modify_sl(symbol, pos.ticket, new_sl)

    states = {}
    session_closed = False

    while True:
        t_mod.sleep(0.1)

        now_cet = get_server_time_cet(symbol)

        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            print("Failed to retrieve positions:", mt5.last_error())
            continue

        my_pos = list(positions)

        # cleanup closed trades
        open_tickets = {pos.ticket for pos in my_pos}
        for ticket in list(states.keys()):
            if ticket not in open_tickets:
                del states[ticket]

        # register new trades
        for pos in my_pos:
            if pos.ticket not in states:
                states[pos.ticket] = StrategyState()
                states[pos.ticket].in_trade = True

        # ---- Global Loss Check ----
        total_loss = sum(pos.profit for pos in my_pos if pos.profit < 0)

        if total_loss < (-35 * len(my_pos)):
            print(f"Loss {total_loss} beyond threshold")
            close_positions(symbol)
            continue

        # ---- Session Close ----
        close_time = time(CONFIG['session']['end_hour'],
                        CONFIG['session']['end_minute'])

        if now_cet.time() >= close_time and not session_closed:
            print("Session ended — closing all positions")
            close_positions(symbol)
            session_closed = True
            continue

        tick = mt5.symbol_info_tick(symbol)
        symbol_info = mt5.symbol_info(symbol)
        point = symbol_info.point
        digits = symbol_info.digits

        # ---- Per Position Management ----
        for pos in my_pos:
            state = states[pos.ticket]

            current_points = (
                (tick.bid - pos.price_open) / point
                if pos.type == mt5.ORDER_TYPE_BUY
                else (pos.price_open - tick.ask) / point
            )

            state.max_pnl = max(state.max_pnl, current_points)
            state.curr_pnl = current_points

            best_retention = 0.0
            triggered = False

            for s in CONFIG['risk_management']['trailing_stages']:
                if state.max_pnl >= s['min_profit']:
                    best_retention = s['retention']
                    triggered = True

            if triggered and best_retention != -1:
                trail_dist_points = state.max_pnl * best_retention
                trail_price = trail_dist_points * point

                new_sl = (
                    pos.price_open + trail_price
                    if pos.type == mt5.ORDER_TYPE_BUY
                    else pos.price_open - trail_price
                )

                new_sl = round(new_sl, digits)

                should_mod = (
                    pos.type == mt5.ORDER_TYPE_BUY and (pos.sl == 0 or new_sl > pos.sl)
                ) or (
                    pos.type == mt5.ORDER_TYPE_SELL and (pos.sl == 0 or new_sl < pos.sl)
                )

                if should_mod:
                    print(f"Modifying SL {pos.ticket}: {pos.sl} -> {new_sl}")
                    modify_sl(symbol, pos.ticket, new_sl)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopping Positions Manager...")
        mt5.shutdown()
