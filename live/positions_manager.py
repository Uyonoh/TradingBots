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
import logging
import logging.handlers

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
MAGIC_NUM += "00"
MAGIC_NUM = int(MAGIC_NUM)
r1 = 50

CONFIG = {
    'bias_filter': {'buy_threshold': 0.6, 'sell_threshold': 0.4}, 
    'entry_conditions': {'buffer_pips': 20, 'velocity_multiplier': 2, 'lookback_seconds': 60*60}, 
    'risk_management': {
        'initial_sl_pips': 50, 
        'trailing_stages': [
            {'min_profit': 0,   'max_profit': 60,  'retention': -1}, 
            # {'min_profit': 50,  'max_profit': 70,  'retention': 0},
            {'min_profit': 60,  'max_profit': 80,  'retention': 0.1},
            {'min_profit': 80,  'max_profit': 90,  'retention': 0.7},
            {'min_profit': 90,  'max_profit': 120, 'retention': 0.9}, 
            {'min_profit': 120,  'max_profit': 200, 'retention': 0.95}, 
            {'min_profit': 200, 'retention': 0.99}
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

class TradingLogger:
    """Centralized logging configuration for trading system."""
    def __init__(self):
        self.symbol = None
        self.log_level = "INFO"
    
    def setup_logging(self, symbol: str, log_level: str = "INFO", name: str = "position_manager") -> logging.Logger:
        """
        Configure structured logging with console and file handlers.
        """
        self.symbol = symbol
        self.log_level = log_level

        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, log_level.upper()))
        logger.handlers.clear()
        
        formatter = logging.Formatter(
            fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(getattr(logging, log_level.upper()))
        logger.addHandler(console_handler)
        
        # File handler with rotation
        log_file = os.path.join(log_dir, f"{symbol}_position_manager_{datetime.now().strftime('%Y%m%d')}.log")
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
        
        # Error file handler
        error_file = os.path.join(log_dir, f"errors_{symbol}_{datetime.now().strftime('%Y%m%d')}.log")
        error_handler = logging.handlers.RotatingFileHandler(
            error_file,
            maxBytes=5*1024*1024,
            backupCount=10
        )
        error_handler.setFormatter(formatter)
        error_handler.setLevel(logging.WARNING)
        logger.addHandler(error_handler)
        
        return logger
    
    def reset_logger(self):
        return self.setup_logging(self.symbol, self.log_level)



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


def close_positions(symbol, magic, logger: logging.Logger):
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
            logger.info("Mandatory Close Executed")

def modify_sl(symbol, ticket, new_sl, logger: logging.Logger):
    if new_sl <= 0:
        logger.info(f"SL {new_sl} must be  > 0")
        return False
    
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": new_sl,
        "magic": MAGIC_NUM
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.info(f"SL modification Failed to {new_sl}: {result.comment}")
        return False
    return True

def remove_tp(symbol, position, logger: logging.Logger):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": position.ticket,
        "tp": 0.0,
        "sl": position.sl,
        "magic": MAGIC_NUM
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.info(f"TP modification Failed: {result.comment}")


def delete_orders(symbol, magic, logger: logging.Logger):
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
            logger.info("Pending Order Deleted")

    
        
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

default_lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.02, 0.01, 0.01, 0.01, 0.02]
hybid_lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.07]
def calculate_gross_profit(direction:str, entry: int|float, open_pos:int, tick, lots:list[float]=default_lots, spacing: int=10):
    if direction.lower() == "buy":
        dir_multiplier = 1
        price = tick.bid
    else:
        dir_multiplier = -1
        price = tick.ask

    p0 = (price - entry) * dir_multiplier
    lots_sum = sum(lots[:open_pos])
    weighted_lots_sum = sum([i*l for (i, l) in enumerate(lots[:open_pos])])
    profit = (p0 * lots_sum) - (spacing * weighted_lots_sum)

    return round(profit, 5)

def calculate_loss_excess(direction:str, entry: int|float, SL:int, open_pos:int, tick, lots:list[float]=default_lots, spacing: int=10):
    if direction.lower() == "buy":
        dir_multiplier = 1
        price = tick.bid
    else:
        dir_multiplier = -1
        price = tick.ask

    p0 = (price - entry) * dir_multiplier
    p0 += SL
    lots_clipped = [l for (i, l) in enumerate(lots) if p0 < i*spacing]
    idx = 0
    for i, l in enumerate(lots):
        if p0 < i*spacing:
            idx = i
            break

    lots_sum = sum(lots[idx:open_pos])
    weighted_lots_sum = sum([i*l for (i, l) in enumerate(lots[:open_pos])][idx:])
    loss = (p0 * lots_sum) - (spacing * weighted_lots_sum)

    return round(loss, 5)

class Tick:
    def __init__(self, ask, spread):
        self.ask = ask
        self.bid = self.ask - spread



# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def main(args_list=None):
    parser = argparse.ArgumentParser(description="Live trading momentum based bot for HFM")

    parser.add_argument("symbol", help="symbol to be traded")
    parser.add_argument("--magic", type=int, default=MAGIC_NUM, help="Unique magic number for bot")
    parser.add_argument("--max-tp", type=int, default=100, help="Max tp of active trades. Will be used to scale trails for all trades")
    parser.add_argument("--scale", action="store_true", help="Scale all monitored trade's trails by max tp")
    parser.add_argument("--keep-alive", action="store_true", help="Keep manager active with no open positions")
    parser.add_argument("--log-level", default="INFO", 
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Logging level")

    args = parser.parse_args(args_list)

    symbol = args.symbol.strip().upper()
    magic_num = args.magic

    trading_logger = TradingLogger()
    logger = trading_logger.setup_logging(symbol, args.log_level)

    # ask, bid -> buy, sell
    # use after entry sell, buy
    # spread = 0
    # tick = Tick(200+100, spread)
    # entry_a = 200
    # entry_b = entry_a - spread
    # sl = 100
    # buy_pos = 10
    # sell_pos = 1
    # lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.07] #[0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.02, 0.02, 0.03, 0.07]
    # logger.info(f"Total lots: {sum([i*l for (i, l) in enumerate(lots)])}")
    # # lots = [0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.14]
    # # lots = default_lots
    # p1 = calculate_gross_profit("buy", entry_a, buy_pos, tick, lots=lots)
    # p2 = calculate_gross_profit("sell", entry_b, sell_pos, tick, lots=lots)
    # e = calculate_loss_excess("sell", entry_b, sl, sell_pos, tick, lots=lots)
    # logger.info(f'Profit: {p1}')
    # logger.info(f'Loss: {p2}')
    # logger.info(f'Excess: {e}')
    # logger.info(f"NET P: {p1+p2-e}")
    # return

    if not mt5.initialize():
        err = mt5.last_error()
        logger.info(f"MT5 terminal initialization failed: {err}")
        raise ConnectionError(f"Could not connect to MT5 terminal: {err}")
    
    if not mt5.login(login=LOGIN, password=PASSWORD, server=SERVER):
        err = mt5.last_error()
        logger.error(f"Login failed for account {LOGIN}: {err}")
        mt5.shutdown()
        raise PermissionError(f"MT5 login failed: {err}")

    # Check symbol
    if not mt5.symbol_select(symbol, True):
        logger.error(f"symbol {symbol} not found")
        return

    contract_size = mt5.symbol_info(symbol).trade_contract_size
    
    logger.info(f"Postion Manager Started on {symbol}...")
    
    last_update_seconds = t_mod.time()
    logged_m = 0

    positions = mt5.positions_get(symbol=symbol)
    my_pos = [p for p in positions if p.magic == magic_num]
    states = {}
    

    while True:
        # 1. Hardware Efficiency: Sleep to reduce CPU usage
        t_mod.sleep(0.1) 
        
        # 2. Update Time
        now_cet = get_server_time_cet(symbol)

        # C. Check for existing positions (Recovery/Management)
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            logger.error("Failed to retrieve positions:", mt5.last_error())
            continue

        my_pos = [p for p in positions if p.magic == magic_num]
        open_pos = len(my_pos) > 0
        tick = mt5.symbol_info_tick(symbol)
        spread = tick.ask - tick.bid

        for pos in my_pos:
            if pos.ticket not in states:
                states[pos.ticket] = StrategyState()
                states[pos.ticket].in_trade = True

        if not open_pos and not args.keep_alive:
            delete_orders(symbol, magic_num, logger)
            logger.info(f"No positions found for {symbol}, exiting...")
            return
        
        total_loss = sum(pos.profit for pos in my_pos if pos.profit < 0)
        pos_len = len(my_pos)
        max_loss = max(-10, pos_len * -1)
        max_loss /= (100 / args.max_tp)
        if total_loss < (max_loss):
            logger.info(f"Loss {total_loss} beyond {max_loss} with {pos_len} position(s)")
            logger.info(f"    Aborting trade")
            close_positions(symbol, magic_num, logger)
            delete_orders(symbol, magic_num, logger)
            return
        

        
        # -----------------------------------------------------------
        # EXIT / RISK MANAGEMENT LOGIC
        # -----------------------------------------------------------
        for i, pos in enumerate(my_pos):
            # Mandatory Close (Time)
            close_time = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
            # if now_cet.time() >= close_time:
            #     logger.info(f"{now_cet.time()} -> {close_time}")
            #     logger.info("CLosing all Positions with magic '{MAGIC_NUM}'")
            #     close_positions(symbol)
            #     continue
        
            # Trailing Stop Logic
            current_profit_points = (tick.bid - pos.price_open) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - tick.ask)
            # Adjust for point value
            current_profit_points *= contract_size # in pips
            
            if current_profit_points > states[pos.ticket].max_pnl:
                logger.info(f"Max profit pips: {states[pos.ticket].max_pnl} -> {current_profit_points}")
                # logger.info(f"LLL: {abs(pos.price_open - pos.tp)}")
                # logger.info(f"TP diff: {args.max_tp - (abs(pos.price_open - pos.tp) / contract_size)}")
            
            states[pos.ticket].max_pnl = max(states[pos.ticket].max_pnl, current_profit_points)
            states[pos.ticket].curr_pnl = current_profit_points
            
            # Check Stages
            best_retention = 0.0
            triggered = False
            tp_ratio = 1
            tp_diff = 0
            trail_scale = args.max_tp / 100 # Initial is based on 100 tp
            dist_to_tp = None
            if pos.tp > 0.0:
                tp_pips = abs(pos.price_open - pos.tp) / contract_size
                tp_ratio = tp_pips / args.max_tp
                tp_diff = args.max_tp - tp_pips

                if pos.type == mt5.ORDER_TYPE_BUY:
                    dist_to_tp = pos.tp - tick.bid
                else:
                    dist_to_tp = tick.ask - pos.tp

                
            
            for s in CONFIG['risk_management']['trailing_stages']:
                # if states[pos.ticket].max_pnl >= s['min_profit'] * tp_ratio * trail_scale:
                if states[pos.ticket].max_pnl >= ((s['min_profit'] - tp_diff) * trail_scale) - spread:
                    best_retention = s['retention']
                    triggered = True

            if triggered:
                # Calculate new SL
                if best_retention == -1:
                    # Leave sl at original
                    pass
                elif best_retention == 0:
                    # Set all SLs to first order
                    max_pips = -1
                    pos1 = None
                    for p in my_pos:
                        pips = (tick.bid - p.price_open) if p.type == mt5.ORDER_TYPE_BUY else (p.price_open - tick.ask)
                        if pips > max_pips:
                            max_pips = pips
                            pos1 = p

                    min_diff = mt5.symbol_info(symbol).point
                    new_sl = pos1.price_open + min_diff
                    new_sl = round(new_sl, 5)

                    # Only modify if new SL is better (Higher for Buy, Lower for Sell)
                    should_mod = False
                    if pos.type == mt5.ORDER_TYPE_BUY and (new_sl > pos.sl): should_mod = True
                    if pos.type == mt5.ORDER_TYPE_SELL and ( new_sl < pos.sl): should_mod = True
                    
                    if should_mod:
                        logger.info(f"Modified SL: {pos.sl} => {new_sl}")
                        success = modify_sl(symbol, pos.ticket, new_sl, logger)

                        for i in range(MAX_RETRIES):
                            if not success:
                                step = (2)**i + i + 1
                                tick = mt5.symbol_info_tick(symbol)
                                price =  tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                                logger.info(f"    Rolling back SL by {step}... [Price at {price}]")
                                states[pos.ticket].max_pnl -= step
                                new_sl = (new_sl - step) if pos.type == mt5.ORDER_TYPE_BUY else (new_sl + step)
                                success = modify_sl(symbol, pos.ticket, new_sl, logger)

                        if dist_to_tp is not None and dist_to_tp <= 20:
                            if pos.tp != 0.0:
                                remove_tp(symbol, pos, logger)

                else:
                    trail_dist = states[pos.ticket].max_pnl * best_retention
                    trail_dist /= contract_size
                    new_sl = (pos.price_open + trail_dist) if pos.type == mt5.ORDER_TYPE_BUY else (pos.price_open - trail_dist)
                    new_sl = round(new_sl, 5)
                    # Only modify if new SL is better (Higher for Buy, Lower for Sell)
                    should_mod = False
                    if pos.type == mt5.ORDER_TYPE_BUY and (new_sl > pos.sl): should_mod = True
                    if pos.type == mt5.ORDER_TYPE_SELL and (new_sl < pos.sl): should_mod = True
                    if pos.sl == 0.0: should_mod = True
                    
                    if should_mod:
                        logger.info(f"Modified SL: {pos.sl} => {new_sl}")
                        success = modify_sl(symbol, pos.ticket, new_sl, logger)

                        for i in range(MAX_RETRIES):
                            if not success:
                                step = (2)**i + i + 1
                                tick = mt5.symbol_info_tick(symbol)
                                price =  tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                                logger.info(f"    SL mod to [{new_sl}] failed with Price [{price}]")
                                logger.info(f"    Rolling back SL by {step}...")
                                states[pos.ticket].max_pnl -= step
                                new_sl = (new_sl - step) if pos.type == mt5.ORDER_TYPE_BUY else (new_sl + step)
                                success = modify_sl(symbol, pos.ticket, new_sl, logger)

                        if dist_to_tp is not None and dist_to_tp <= 20:
                            if pos.tp != 0.0:
                                remove_tp(symbol, pos, logger)

    states = {}
    session_closed = False

    while True:
        t_mod.sleep(0.1)

        now_cet = get_server_time_cet(symbol)

        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            logger.info("Failed to retrieve positions:", mt5.last_error())
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
            logger.info(f"Loss {total_loss} beyond threshold")
            close_positions(symbol)
            continue

        # ---- Session Close ----
        close_time = time(CONFIG['session']['end_hour'],
                        CONFIG['session']['end_minute'])

        if now_cet.time() >= close_time and not session_closed:
            logger.info("Session ended — closing all positions")
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

            if triggered and best_retention != -1 and best_retention != 0:
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
                    logger.info(f"Modifying SL {pos.ticket}: {pos.sl} -> {new_sl}")
                    modify_sl(symbol, pos.ticket, new_sl)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Stopping Positions Manager...")
        mt5.shutdown()
