import argparse
import calendar
import os
import sys
import logging
import time as t_mod
from collections import deque
import time as t_mod
from datetime import date, datetime, time, timedelta, timezone
import dotenv
import numpy as np
import pandas as pd
import pytz

from utils import retry
from orders import normalize_price, order, make_request, send_single_order
from position_tests import Tick, total_profit

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

DEVIATION = 10  # Slippage tolerance in points
MAGIC_NUM += "00"
MAGIC_NUM = int(MAGIC_NUM)
r1 = 50

DEFAULT_CONFIG = {
    "bias_filter": {"buy_threshold": 0.6, "sell_threshold": 0.4},
    "entry_conditions": {
        "buffer_pips": 20,
        "velocity_multiplier": 2,
        "lookback_seconds": 60 * 60,
    },
    "risk_management": {
        "initial_sl_pips": 50,
        "trailing_stages": [
            # {"min_profit": 0, "max_profit": 60, "retention": -1},
            # # {'min_profit': 50,  'max_profit': 70,  'retention': 0},
            # {"min_profit": 60, "max_profit": 70, "retention": 0.1},
            # {"min_profit": 70, "max_profit": 90, "retention": 0.7},
            # {"min_profit": 90, "max_profit": 120, "retention": 0.9},
            # {"min_profit": 120, "max_profit": 125, "retention": 0.95},
            # {"min_profit": 125, "retention": 0.99},
            #
            {"min_profit": 0, "max_profit": 100, "retention": -1},
            # {"min_profit": 70, "max_profit": 100, "retention": 0.7},
            {"min_profit": 100, "max_profit": 101, "retention": 0.99},
            {"min_profit": 101, "retention": 0.95},
        ],
    },
    "session": {
        "day_open": time(9, 0),
        "start_hour": 10,
        "start_minute": 0,  # Entry Window Start
        "end_hour": 17,
        "end_minute": 00,  # Mandatory Close
        "ghost_start": time(8, 0),
        "ghost_end": time(8, 30),
    },
}

FOREIGN_CONFIG = {
    "risk_management": {
        "trailing_stages": [
            {"min_profit": 0, "max_profit": 100, "retention": -1},
            {"min_profit": 100, "max_profit": 101, "retention": 0.99},
            {"min_profit": 101, "retention": 0.95},
        ],
    },
    "session": {
        "day_open": time(9, 0),
        "start_hour": 10,
        "start_minute": 0,  # Entry Window Start
        "end_hour": 17,
        "end_minute": 00,  # Mandatory Close
        "ghost_start": time(8, 0),
        "ghost_end": time(8, 30),
    },
}

#  ------------------------------------------------------------------
    # TIME
    #  ------------------------------------------------------------------

CET = pytz.timezone("Europe/Berlin")
UTC = pytz.utc
UTC2 = pytz.timezone("Europe/Athens")  # EET / CAT
UTC3 = pytz.timezone("Asia/Baghdad")  # EAT / MST
LOCAL_ZONE = datetime.now(UTC).astimezone().tzinfo.tzname

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
        self.tp_pips = 0.0
        self.direction = None # 'buy' or 'sell'


class PositionManager:
    def __init__(self, symbol:str, magic:int, args, logger: logging.Logger):
        self.symbol = symbol
        self.magic_num = magic
        self.args = args
        self.logger = logger
        self.max_tp = args.max_tp
        self.orders_deleted = False
        self.foreign_ticket = None
        self.entries = None
        self.boundaries = None # list of 2 (upper and lower boundaries of trade after shrinking)
        self.foreign_activated = False
        self.alien_activated = False
        self.foreign_tp_pips = None
        self.positions_shrunk = False
        self.trailing = False
        self.last_ask = self.last_bid = None
        self.foreign_ticket = self.alien_ticket = None

        self.alien_profit = 7
        self.alien_order_no = 0

        if not self.logger:
            self.logger = logging.getLogger("trading.position_manager")

        if not mt5.initialize():
            err = mt5.last_error()
            self.logger.info(f"MT5 terminal initialization failed: {err}")
            raise ConnectionError(f"Could not connect to MT5 terminal: {err}")

        if not mt5.login(login=LOGIN, password=PASSWORD, server=SERVER):
            err = mt5.last_error()
            self.logger.error(f"Login failed for account {LOGIN}: {err}")
            mt5.shutdown()
            raise PermissionError(f"MT5 login failed: {err}")

        # Check symbol
        if not mt5.symbol_select(self.symbol, True):
            self.logger.error(f"symbol {self.symbol} not found")
            return

        self.symbol_info = mt5.symbol_info(self.symbol)
        self.contract_size = self.symbol_info.trade_contract_size

        self.logger.info(f"Postion Manager Started on {self.symbol}...")

        self.stop_levels = self.symbol_info.trade_stops_level * (10**-self.symbol_info.digits)
        self.spread = self.symbol_info.spread * (10**-self.symbol_info.digits)

        self.states = {}
        self.config = DEFAULT_CONFIG



        self.foreign_tp_pips = (self.max_tp / 2) + (max(self.stop_levels * 2, self.spread)) # Extra padding

        self.logger.info(f"{self.max_tp=}")
        self.logger.info(f"{self.foreign_tp_pips=}")


    @retry(max_attempts=3, delay=0.1)
    def close_position(self, position):
        tick = mt5.symbol_info_tick(self.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": position.volume,
            "type": mt5.ORDER_TYPE_SELL
            if position.type == mt5.ORDER_TYPE_BUY
            else mt5.ORDER_TYPE_BUY,
            "position": position.ticket,
            "price": tick.bid
            if position.type == mt5.ORDER_TYPE_BUY
            else tick.ask,  # Error: Might need to add padding
            "deviation": DEVIATION,
            "magic": self.magic_num,
            "comment": "Mandatory Close",
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.logger.error(f"Failed to close position {position.ticket}")
        self.logger.info("Mandatory Close Executed")

    def close_positions(self, symbol, magic):
        """Closes all positions with our Magic Number"""
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            return
        for pos in positions:
            if pos.magic == magic:
                self.close_position(pos)


    @retry(max_attempts=3, delay=0.1)
    def modify_sl_tp(self, symbol, position, new_sl=None, new_tp=None, force=False):
        tp_mod = True
        sl_mod = True
        if new_sl is None:
            new_sl = position.sl
            sl_mod = False
        if new_tp is None:
            new_tp = position.tp
            tp_mod = False

        if new_sl <= 0:
            self.logger.info(f"SL {new_sl} must be  > 0")
            return False

        # Only modify if new SL is better (Higher for Buy, Lower for Sell)
        should_mod = False
        if position.type == mt5.ORDER_TYPE_BUY  and (new_sl >= position.sl):
            should_mod = True
        if position.type == mt5.ORDER_TYPE_SELL and (new_sl <= position.sl):
            should_mod = True
        if force:
            should_mod = True

        if should_mod:
            modified = ""
            if sl_mod:
                modified += f"Modifying SL: {position.sl} => {new_sl}"
            if tp_mod:
                modified += f" | Modifying TP: {position.tp} => {new_tp}"
            if not (sl_mod or tp_mod):
                modified += f"No modifications made to {position.ticket}"
            self.logger.info(modified)

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": position.ticket,
                "sl": normalize_price(new_sl, self.symbol_info),
                "tp": normalize_price(new_tp, self.symbol_info),
                "magic": self.magic_num,
            }
            result = mt5.order_send(request)
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                if "No changes" in result.comment:
                    return True

                tick = mt5.symbol_info_tick(self.symbol)
                self.last_ask = tick.ask
                self.last_bid = tick.bid
                self.logger.info(f"SL/TP modification Failed [{new_sl} / {new_tp}]: {result.comment}")
                if "Invalid stops" in result.comment:
                    self.logger.info(f"\t Ask: {self.last_ask}, Bid: {self.last_bid} ")
                    self.logger.info(f"\t Ask Diff: [{abs(new_sl - self.last_ask)}/{abs(new_tp - self.last_ask)}]")
                    self.logger.info(f"\t Bid Diff: [{abs(new_sl - self.last_bid)}/{abs(new_tp - self.last_bid)}]")
                    self.logger.info(f"Open Diff: [{abs(new_sl - position.price_open)}/{abs(new_tp - position.price_open)}]")

                raise ValueError(f"SL/TP modification Failed [{new_sl} / {new_tp}]: {result.comment} Code: {result.retcode}")
                return False
            # self.logger.info(f"Modified SL: {pos.sl} => {new_sl}")
            return True
        else:
            return False


    def remove_tp(self, symbol, position):
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": position.ticket,
            "tp": 0.0,
            "sl": position.sl,
            "magic": self.magic_num,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.logger.info(f"TP modification Failed: {result.comment}")

    @retry(max_attempts=2, delay=0.1)
    def move_tp(self, position, margin=20):
        if position.type == mt5.POSITION_TYPE_BUY:
            tp = position.tp + margin
        else:
            tp = position.tp - margin

        self.logger.info(f"Moving tp from {position.tp} to {tp}...")

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": position.ticket,
            "tp": tp,
            "sl": position.sl,
            "magic": self.magic_num,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.logger.info(f"TP modification Failed: {result.comment}")
            raise ValueError(f"TP modification Failed [{position.tp} -> {tp}]: {result.comment}. Code: {result.retcode}")

    @retry(max_attempts=3, delay=0.1)
    def delete_order(self, ticket:int):
        """Delete a single order"""
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
            "comment": "Mandatory Close",
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.logger.info(f"Order delete Failed for {ticket}: {result.comment}")
            raise ValueError(f"Order delete failed for {ticket}: {result.comment}. Code: {result.retcode}")
        self.logger.info("Pending Order Deleted")

    def delete_orders(self, symbol, magic):
        """Delete all orders with our Magic Number"""
        orders = mt5.orders_get(symbol=symbol)
        if orders is None:
            return
        for pos in orders:
            if pos.magic == magic:
                self.delete_order(pos.ticket)


    def get_server_timezone(self, year=None, month=None, day=None):
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


    def get_server_time(self, symbol):
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
        server_time = pd.to_datetime(tick.time, unit="s")
        zone = self.get_server_timezone()
        server_time = zone.localize(server_time)

        return server_time


    def get_server_time_cet(self, symbol):
        """Gets MT5 server time and converts to CET."""
        # Note: MT5 Usually returns time in Broker Time.
        # We assume Broker Time is aligned with EU markets or we convert.
        # For safety, we trust the broker's current time struct.
        server_time = self.get_server_time(symbol)
        return server_time.astimezone(CET)

    def set_boundaries(self):
        """ Set position manager boundaries (top, bottom) """
        # TODO: Take foreign_tp_pips using open max position - entry
        # Recursive called later so need explicit error handling
        try:
            buys = sells = 0
            buy_entry = 100**100
            sell_entry = 0
            for position in self.positions:
                if self.foreign_ticket:
                    if position.ticket == self.foreign_ticket:
                        continue
                if position.type == mt5.POSITION_TYPE_BUY:
                    buys += 1
                    buy_entry = min(buy_entry, position.price_open)
                else:
                    sells +=1
                    sell_entry = max(sell_entry, position.price_open)

            self.logger.info(f"Entries: Buy={buy_entry} | Sell={sell_entry}")
            # tick = mt5.symbol_info_tick(self.symbol)
            # spread = tick.ask - tick.bid
            if (buy_entry == 100**100) and (sell_entry == 0):
                self.positions = [p for p in mt5.positions_get(symbol=self.symbol) if p.magic == self.magic_num]
                if not self.positions:
                    raise ValueError("Empty positions")
                else:
                    self.set_boundaries()
            elif buy_entry == 100**100:
                buy_entry = sell_entry + self.spread
            elif sell_entry == 0:
                sell_entry = buy_entry - self.spread

            p1 = buy_entry + self.foreign_tp_pips
            p2 = sell_entry - self.foreign_tp_pips
            self.logger.info(f"boundaries: {p1} | {p2}, FP= {self.foreign_tp_pips}")

            self.boundaries = (p1, p2)
            self.entries = (buy_entry, sell_entry)
        except Exception as e:
            self.logger.error(f"Unexpected error when setting boundaries: {e}")
            raise Exception(e)

    def shrink_trades(self):
        spread = self.last_ask - self.last_bid
        positions = [p for p in mt5.positions_get(symbol=self.symbol) if p.magic == self.magic_num]
        for pos in positions:
            if pos.ticket == self.foreign_ticket or pos.ticket == self.alien_ticket:
                continue
            if pos.type == mt5.POSITION_TYPE_BUY:
                new_sl = self.boundaries[1]
                new_tp = self.boundaries[0] - spread
            else:
                new_sl = self.boundaries[0]
                new_tp = self.boundaries[1] + spread

            self.logger.info(f"Shrinking {pos.ticket} [{pos.sl} / {pos.tp}] -> [{new_sl} / {new_tp}]")
            self.modify_sl_tp(self.symbol, pos, new_sl=new_sl, new_tp=None, force=True)
            # Only mods TP as sl is same
            self.modify_sl_tp(self.symbol, pos, new_sl=new_sl, new_tp=new_tp, force=True)
            self.logger.info("DONE")

            # Check success
            try:
                self.states[pos.ticket].tp_pips -= self.max_tp - self.foreign_tp_pips
                self.states[pos.ticket].tp_pips = max (0, self.states[pos.ticket].tp_pips)
                self.states[pos.ticket].max_pnl = 0.0
            except KeyError:
                # Position closed or network/terminal error:
                self.modify_sl_tp(self.symbol, pos, new_sl, new_tp)
                self.states[pos.ticket] = StrategyState()
                self.states[pos.ticket].in_trade = True

                self.states[pos.ticket].tp_pips -= self.max_tp - self.foreign_tp_pips
                self.states[pos.ticket].tp_pips = max (0, self.states[pos.ticket].tp_pips)

                self.logger.info(f"Position {pos.ticket}may have already closed")
                # self.open_pos -= 1

        # Adjust max tp for trails
        self.max_tp = self.foreign_tp_pips
        self.positions_shrunk = True

    def calc_pnl(self, positions:list, direction:str, buy_entry:float, sell_entry:float) -> float:
        """
        """
        pnl = 0
        spread = self.last_ask - self.last_bid
        position_type = mt5.POSITION_TYPE_BUY if direction.lower() == "buy" else mt5.POSITION_TYPE_SELL
        multiplier = 1 if direction.lower() == "buy" else -1
        for pos in positions:
            if pos.ticket == self.foreign_ticket:
                continue

            # entry = buy_entry if pos.type == mt5.POSITION_TYPE_BUY else sell_entry
            # if pos.type == position_type:
            #     # pnl += (pos.tp - pos.price_open) * pos.volume * multiplier
            #     # pnl += (-abs(entry - pos.price_open) + self.foreign_tp_pips) * pos.volume * multiplier # Calculate pnl assuming foreign_tp_pips sl and tp
            #     pnl += (entry + self.foreign_tp_pips * multiplier) - pos.price_open) * pos.volume * multiplier # Calculate pnl assuming foreign_tp_pips sl and tp
            # else:
            #     # pnl += (pos.price_open - pos.sl) * pos.volume * multiplier
            #     pnl += (-abs(entry - pos.price_open) - self.foreign_tp_pips) * pos.volume * multiplier # Calculate pnl assuming foreign_tp_pips sl and tp

            if direction.lower() == "buy":
                if pos.type == mt5.POSITION_TYPE_BUY:
                    pnl += ((self.boundaries[0] - pos.price_open) / self.contract_size) * pos.volume
                else:
                    pnl -= ((self.boundaries[0] - pos.price_open) / self.contract_size) * pos.volume
            else:
                if pos.type == mt5.POSITION_TYPE_SELL:
                    pnl += ((pos.price_open - self.boundaries[1]) / self.contract_size) * pos.volume
                else:
                    pnl -= ((pos.price_open - self.boundaries[1]) / self.contract_size) * pos.volume


        return round(pnl, 2)


    @retry(max_attempts=3, delay=0.5)
    def deals(self, buys, buy_entry, buy_pnl, sells, sell_entry, sell_pnl):
        to_date = self.get_server_time(self.symbol) #datetime.now()
        from_date = to_date - timedelta(hours=6)
        pnl = 0

        # buy_pnl = 0 # tp for buy sl for sell
        # sell_pnl = 0 # tp for sell sl for buy
        self.logger.info(f"Fetching deals from {from_date} to {to_date}...")
        deals = mt5.history_deals_get(from_date, to_date, group=self.symbol)
        self.logger.info(f"Deals = {deals}")
        self.logger.info(f"Initial PNLs -- Buy: {buy_pnl}, Sell: {sell_pnl}")
        for deal in deals:
            if not deal.magic == self.magic_num:
                continue

            if deal.entry == mt5.DEAL_ENTRY_IN:
                if deal.type == mt5.DEAL_TYPE_BUY:
                    buy_entry = min(buy_entry, deal.price)
                elif deal.type == mt5.DEAL_TYPE_SELL:
                    sell_entry = max(sell_entry, deal.price)

            if deal.type == mt5.DEAL_TYPE_BUY:
                # Buy entry OR Sell hit sl/tp
                # if deal.profit > 0: # TP direction -> cout + sell
                #     sell_pnl += deal.profit
                # elif deal.profit < 0: # SL -> count buy
                #     buy_pnl += deal.profit
                sells += 1
                pnl += deal.profit
            elif deal.type == mt5.DEAL_TYPE_SELL:
                # if deal.profit > 0:
                #     buy_pnl += deal.profit
                # elif deal.profit < 0:
                #     sell_pnl += deal.profit
                buys += 1
                pnl += deal.profit
            self.logger.info(f"\tUpdated Buy: {buy_pnl}, Sell: {sell_pnl}")

        buy_pnl += pnl
        sell_pnl += pnl
        return buys, buy_entry, buy_pnl, sells, sell_entry, sell_pnl

    def verify_request(self, entry, sl, tp) -> dict:
        if sl < self.stop_levels:
            self.logger.info(f"Adjusting SL from {sl} to {self.stop_levels}")
            sl = self.stop_levels

        if tp < self.stop_levels:
            self.logger.info(f"Adjusting TP from {tp} to {self.stop_levels}")
            tp = self.stop_levels

        if self.last_bid and self.last_ask:
            if self.last_ask  < entry < self.last_bid:
                self.logger.error("Invalid request: Price between ask and bid!")
                return None

        return sl, tp

    def make_foreign_request(self, positions, update=False, half=False):
        buys = sells = 0
        buy_entry = 100**100
        sell_entry = 0
        for position in positions:
            if position.type == 0:
                buys += 1
                buy_entry = min(buy_entry, position.price_open)
            else:
                sells +=1
                sell_entry = max(sell_entry, position.price_open)

        buy_pnl = self.calc_pnl(positions, "buy", buy_entry, sell_entry)
        sell_pnl = self.calc_pnl(positions, "sell", buy_entry, sell_entry)
        if update:
            buys, buy_entry, buy_pnl, sells, sell_entry, sell_pnl = self.deals(buys, buy_entry, buy_pnl, sells, sell_entry, sell_pnl)

        self.logger.info(f"Estimated pnl for buys: {buy_pnl}")
        self.logger.info(f"Estimated pnl for sells: {sell_pnl}")

        if self.entries is not None and self.boundaries is not None:
            if buy_entry != self.entries[0] or sell_entry != self.entries[1]:
                self.logger.error(f"Entry mismatch: {self.entries} != ({buy_entry}, {sell_entry})")
            buy_entry = self.entries[0]
            sell_entry = self.entries[1]

            buy_tp = self.boundaries[0]
            sell_tp = self.boundaries[1]
        else:
            self.set_boundaries()
            return self.make_foreign_request(positions, update, half)

        if half:
            buy_pnl /= 2
            sell_pnl /= 2

        abs_profit_sum = abs(buy_pnl) + abs(sell_pnl)
        price_diff = buy_tp - sell_tp
        lot_size = round(abs_profit_sum/price_diff, 2)
        lot_size = max(lot_size, 0.01)
        self.logger.info(f"ABS_SUM: {abs_profit_sum} | PriceDiff: {price_diff}")
        self.logger.info(f"Using lot size = {lot_size}")
        tick = mt5.symbol_info_tick(self.symbol)

        if buy_pnl > sell_pnl:
            opp_direction = "sell"
            entry = buy_tp  - (abs(buy_pnl)/lot_size)
            offset = entry - buy_entry

            if tick.bid > entry:
                order_type = f"{opp_direction} stop"
            else:
                order_type = f"{opp_direction} limit"
            # tp = args.max_tp + offset
            # sl = args.max_tp - offset
        else:
            opp_direction = "buy"
            entry = sell_tp + (abs(sell_pnl)/lot_size)
            offset = sell_entry - entry

            if tick.ask < entry:
                order_type = f"{opp_direction} stop"
            else:
                order_type = f"{opp_direction} limit"

            # tp = args.max_tp + offset
            # sl = args.max_tp - of
        tp = self.foreign_tp_pips + offset # TP Pips
        sl = self.foreign_tp_pips - offset # SL Pips

        self.logger.info(f"="*50)
        self.logger.info(f"Offset: {offset}")
        self.logger.info(f"Entry: {entry} | TP_PIPS: {tp} | SL_PIPS: {sl}")
        self.logger.info(f"Entries: Buy={buy_entry} | Sell={sell_entry}")
        self.logger.info(f"="*50)
        self.logger.info(f"Placing {opp_direction} stop foreign order ...")

        # Entry
        # entry1 = buy_tp  - (abs(buy_pnl)/lot_size)  # 48815.11
        # entry2 = sell_tp + (abs(sell_pnl)/lot_size) # 48813.28
        # logger.info(f"Entries: {entry1} || {entry2}")
        # entry = (entry1+entry2)/2

        # offset1 = entry - buy_entry
        # offset2 = sell_entry - entry
        # offset = (offset1 + offset2)/2
        # logger.info(f"Offsets: {offset1} || {offset2} ==> {offset}")

        verified = self.verify_request(entry, sl, tp)
        if verified is None:
            self.logger.error("Verification Error")
        else:
            sl, tp = verified

        request = make_request(
            symbol=self.symbol,
            order_type=order_type,
            start_price=entry,
            volume_per_order=lot_size,
            stop_loss_pips=sl,
            take_profit_pips=tp,
            symbol_info=self.symbol_info,
            comment="Foreign order",
            magic=self.magic_num,
        )

        return request


    def foreign_order(self, positions, final=False, half=False):
        """
        get all deals

        get all pos
        get all orders

        track orders, whon only 1 side remains, plsce forign:

        group pos by order type
            buy_pnl: -> for pos:
                pnl += (pos.tp - pos.price_open) * pos.volume if buy
                pnl += (pos.price_open - pos.sl)  * pos.volume (negative val) if sell

            for deal in history_deals_get(from_date, to_date, group=sym):

            pnl /= contract_size
        get ids of all open pos for tracking

        if any pos closes:
            update foreign params

        store forign ref
        if foreign triggered:
            move sls and tps
            if num order != initial + 1:

        """
        # positions = [p for p in mt5.positions_get(symbol=self.symbol) if p.magic == self.magic_num]
        orders    = [o for o in mt5.orders_get(symbol=self.symbol) if o.magic == self.magic_num]

        buy_orders = [o for o in orders if o.type == mt5.ORDER_TYPE_BUY_STOP]
        sell_orders = [o for o in orders if o.type == mt5.ORDER_TYPE_SELL_STOP]

        if not buy_orders or not sell_orders:
            if final:
                self.logger.info("Placing final order [Foreign]...")

                request = self.make_foreign_request(positions, False, half)
                self.foreign_ticket = send_single_order(request)
                # Perform error checks:
                if self.foreign_ticket:
                    while not mt5.orders_get(ticket=self.foreign_ticket):
                        self.logger.info(f"Ticket {self.foreign_ticket} not found, sleeping...")
                        t_mod.sleep(0.5)

                    self.open_pos = len(positions)
                    self.foreign_activated = True
                    return

            if not self.orders_deleted:
                self.logger.info("Side filled, deleting pending orders...")
                self.delete_orders(self.symbol, self.magic_num)
                # self.logger.info("Shrinking trade area...")
                # self.shrink_trades()

                request = self.make_foreign_request(positions, False, half)
                self.foreign_ticket = send_single_order(request)
                # Perform error checks:
                if self.foreign_ticket:
                    while not mt5.orders_get(ticket=self.foreign_ticket):
                        self.logger.info(f"Ticket {self.foreign_ticket} not found, sleeping...")
                        t_mod.sleep(0.5)

                    self.open_pos = len(positions)
                    self.orders_deleted = True

            # elif len(positions) < self.open_pos and not self.foreign_activated:
            #     # Update foreign order if any position closes
            #     order = mt5.orders_get(ticket=self.foreign_ticket)
            #     if order is None:
            #         self.logger.error(f"Order {self.foreign_ticket} not found. Error: {mt5.last_error()}")
            #         self.open_pos = len(positions)
            #         # break
            #         mt5.shutdown()
            #     request = self.make_foreign_request(positions, update=True, half=half)
            #     self.delete_order(self.foreign_ticket)
            #     self.foreign_ticket = send_single_order(request)
            #     self.open_pos = len(positions)

            elif not orders and not self.foreign_activated: #(len(positions) > self.open_pos) or
                # Perhaps foreign order triggered
                self.open_pos = len(positions)
                self.positions = positions
                self.logger.info("Shrinking trade area...")
                self.shrink_trades()
                # return
                # Update foreign order
                position = mt5.positions_get(ticket=self.foreign_ticket)
                if position is None:
                    self.logger.error(f"Position {self.foreign_ticket} not found. Error: {mt5.last_error()}")
                    self.foreign_ticket = int(input("Enter Forign order ticket >> "))
                    if not self.foreign_ticket:
                        self.logger.error("Invalid ticket, exiting...")
                        mt5.shutdown()
                else:
                    self.logger.info(f"Foreign order found")
                    self.logger.info(f"{orders=}")
                    self.logger.info(f"{self.foreign_activated=}")
                    self.foreign_activated = True
                # request = self.make_foreign_request(positions, update=True)
                # request["order"] = self.foreign_ticket
                # self.foreign_ticket = send_single_order(request)
                self.open_pos = len(positions)


    def make_alien_request(self, positions, update=False):
        # 2. alien order is placed in opposing direction 1/4th way [sell][175] with tp ad midway [150]
                #   and sl at top [300]
                #   reduce boundary on opp[sell] side to midway
                #  Order is 0.1 + lots required to negate pnl2. alien order is placed in opposing direction 1/4th way [sell][175] with tp ad midway [150]
                        #   and sl at top [300]
                        #   reduce boundary on opp[sell] side to midway
                        #  Order is 0.1 + lots required to negate pnl2. alien order is placed in opposing direction 1/4th way [sell][175] with tp ad midway [150]
                                #   and sl at top [300]
                                #   reduce boundary on opp[sell] side to midway
                                #  Order is 0.1 + lots required to negate pnl
        buys = sells = 0
        buy_entry = 100**100
        sell_entry = 0
        for position in positions:
            if position.type == mt5.POSITION_TYPE_BUY:
                buys += 1
                buy_entry = min(buy_entry, position.price_open)
            else:
                sells +=1
                sell_entry = max(sell_entry, position.price_open)

        buy_pnl = self.calc_pnl(positions, "buy", buy_entry, sell_entry)
        sell_pnl = self.calc_pnl(positions, "sell", buy_entry, sell_entry)
        if update:
            buys, buy_entry, buy_pnl, sells, sell_entry, sell_pnl = self.deals(buys, buy_entry, buy_pnl, sells, sell_entry, sell_pnl)

        self.logger.info(f"Estimated pnl for buys: {buy_pnl}")
        self.logger.info(f"Estimated pnl for sells: {sell_pnl}")

        if self.entries is not None and self.boundaries is not None:
            if buy_entry != self.entries[0] or sell_entry != self.entries[1]:
                self.logger.error(f"Entry mismatch: {self.entries} != ({buy_entry}, {sell_entry})")
            buy_entry = self.entries[0]
            sell_entry = self.entries[1]

            buy_tp = self.boundaries[0]
            sell_tp = self.boundaries[1]
        else:
            self.set_boundaries()
            return self.make_alien_request(positions, update)



        # abs_profit_sum = abs(buy_pnl) + abs(sell_pnl)
        # price_diff = buy_tp - sell_tp
        # lot_size = round(abs_profit_sum/price_diff, 2)
        # lot_size = max(lot_size, 0.01)
        # self.logger.info(f"ABS_SUM: {abs_profit_sum} | PriceDiff: {price_diff}")
        # self.logger.info(f"Using lot size = {lot_size}")
        tick = mt5.symbol_info_tick(self.symbol)
        spread = self.spread

        if buy_pnl > sell_pnl:
            opp_direction = "sell"
            entry = normalize_price(sell_entry - (sell_entry - sell_tp - spread) / 2, self.symbol_info)
            price_diff = entry - sell_tp - spread
            offset = sell_entry - entry
            # SL and TP pips
            sl = (self.boundaries[0] - entry) * self.contract_size
            tp = (entry - (self.boundaries[1] + spread)) * self.contract_size
            lot_size = round((-sell_pnl * 1.2)/price_diff, 2)
            if lot_size < 0.01:
                self.logger.error(f"Invalid lot size [{lot_size}] for alien order")
                return
            self.logger.info(f"Using lot size = {lot_size}")

            if tick.bid > entry:
                order_type = f"{opp_direction} stop"
            else:
                order_type = f"{opp_direction} limit"
        else:
            opp_direction = "buy"
            entry = normalize_price(buy_entry + (buy_tp - spread - buy_entry) / 2, self.symbol_info)
            price_diff = buy_tp - spread - entry
            offset = entry - buy_entry
            # SL and TP pips
            sl = (entry - self.boundaries[1]) * self.contract_size
            tp = ((self.boundaries[0] - spread) - entry) * self.contract_size
            lot_size = round((-buy_pnl * 1.2)/price_diff, 2)
            if lot_size < 0.01:
                self.logger.error(f"Invalid lot size [{lot_size}] for alien order")
                return
            self.logger.info(f"Using lot size = {lot_size}")

            if tick.ask < entry:
                order_type = f"{opp_direction} stop"
            else:
                order_type = f"{opp_direction} limit"

            # tp = args.max_tp + offset
            # sl = args.max_tp - of
        # offset -= spread
        # tp = self.foreign_tp_pips - offset # TP Pips
        # sl = self.foreign_tp_pips + offset # SL Pips

        self.logger.info(f"="*50)
        self.logger.info(f"price_diff: {price_diff}")
        self.logger.info(f"Entry: {entry} | TP_PIPS: {tp} | SL_PIPS: {sl}")
        self.logger.info(f"Entries: Buy={buy_entry} | Sell={sell_entry}")
        self.logger.info(f"="*50)
        self.logger.info(f"Placing {opp_direction} stop alien order ...")

        verified = self.verify_request(entry, sl, tp)
        if verified is None:
            self.logger.error("Verification Error")
        else:
            sl, tp = verified

        request = make_request(
            symbol=self.symbol,
            order_type=order_type,
            start_price=entry,
            volume_per_order=lot_size,
            stop_loss_pips=sl,
            take_profit_pips=tp,
            symbol_info=self.symbol_info,
            comment=f"Alien order {self.alien_order_no if not update else (self.alien_order_no - 1)}",
            magic=self.magic_num,
        )

        return request

    def alien_order(self, positions):
        # Watch until one side filled then, delete pending orders
        # 1. Determine the direction filled [buy]
        # 2. alien order is placed in opposing direction 1/4th way [sell][175] with tp ad midway [150]
        #   and sl at top [300]
        #   reduce boundary on opp[sell] side to midway
        #  Order is 0.1 + lots required to negate pnl

        # positions = [p for p in mt5.positions_get(symbol=self.symbol) if p.magic == self.magic_num]
        orders    = [o for o in mt5.orders_get(symbol=self.symbol) if o.magic == self.magic_num]

        buy_orders = [o for o in orders if o.type == mt5.ORDER_TYPE_BUY_STOP]
        sell_orders = [o for o in orders if o.type == mt5.ORDER_TYPE_SELL_STOP]

        if not buy_orders or not sell_orders:
            if not self.orders_deleted:
                if not buy_orders:
                    self.alien_side = "sell"
                else:
                    self.alien_side = "buy"

                self.logger.info("Side filled, deleting pending orders...")
                self.set_boundaries()
                self.delete_orders(self.symbol, self.magic_num)
                request = self.make_alien_request(positions)
                self.alien_ticket = send_single_order(request)
                # Perform error checks:
                if self.alien_ticket:
                    while not mt5.orders_get(ticket=self.alien_ticket):
                        self.logger.info(f"Ticket {self.alien_ticket} not found, sleeping...")
                        t_mod.sleep(0.5)

                    self.open_pos = len(positions)
                    self.orders_deleted = True
                    self.alien_order_no += 1

            elif len(positions) < self.open_pos and not self.alien_activated:
                # Update alien order if any position closes
                return
                order = mt5.orders_get(ticket=self.alien_ticket)
                if order is None:
                    self.logger.error(f"Order {self.alien_ticket} not found. Error: {mt5.last_error()}")
                    self.open_pos = len(positions)
                    # break
                    mt5.shutdown()
                request = self.make_alien_request(positions, update=True)
                self.delete_order(self.alien_ticket)
                self.alien_ticket = send_single_order(request)
                self.open_pos = len(positions)

            elif not orders and not self.alien_activated: #(len(positions) > self.open_pos) or
                # Perhaps alien order triggered
                self.open_pos = len(positions)
                self.positions = positions
                if self.alien_order_no == 1:
                    self.logger.info("Shrinking trade area...")
                    self.shrink_trades()
                # return
                # Update foreign order
                position = mt5.positions_get(ticket=self.alien_ticket)
                if position is None:
                    self.logger.error(f"Position {self.alien_ticket} not found. Error: {mt5.last_error()}")
                    self.alien_ticket = int(input("Enter alien order ticket >> "))
                    if not self.alien_ticket:
                        self.logger.error("Invalid ticket, exiting...")
                        mt5.shutdown()
                else:
                    self.logger.info(f"Alien order found")
                    self.logger.info(f"{orders=}")
                    self.logger.info(f"{self.alien_activated=}")
                    if self.alien_order_no >= 1:
                        self.alien_activated = True
                        # if not self.foreign_activated:
                        #     self.foreign_order(self.positions, final=True)
                    else:
                        self.logger.info(f"Placing alien order {self.alien_order_no}")
                        request = self.make_alien_request(positions)
                        self.alien_ticket = send_single_order(request)
                        self.alien_order_no += 1

                # request = self.make_foreign_request(positions, update=True)
                # request["order"] = self.alien_ticket
                # self.alien_ticket = send_single_order(request)
                self.open_pos = len(positions)
            # elif not orders and not self.foreign_activated:
            #     if self.alien_order_no == 2:
            #         self.alien_activated = True
            #         self.foreign_order(self.positions, final=True)

    def monitor_orders(self):
        if not self.orders_deleted:
            # positions = [p for p in mt5.positions_get(symbol=self.symbol) if p.magic == self.magic_num]
            orders    = [o for o in mt5.orders_get(symbol=self.symbol) if o.magic == self.magic_num]

            buy_orders = [o for o in orders if o.type == mt5.ORDER_TYPE_BUY_STOP]
            sell_orders = [o for o in orders if o.type == mt5.ORDER_TYPE_SELL_STOP]

            if not buy_orders or not sell_orders:

                self.logger.info("Side filled, deleting final pending order...")
                last_pos = orders[-1]
                for order in orders:
                    print(order)
                    if order.volume_current >= last_pos.volume_current:
                        last_pos = order

                self.delete_order(last_pos.ticket)
                self.orders_deleted = True

    def run(self, args):

        last_update_seconds = t_mod.time()
        logged_m = 0

        positions = mt5.positions_get(symbol=self.symbol)
        my_pos = [p for p in positions if p.magic == self.magic_num]

        self.positions = my_pos
        self.set_boundaries()
        logged_system = False

        while True:
            # 1. Hardware Efficiency: Sleep to reduce CPU usage
            t_mod.sleep(0.1)
            #self.monitor_orders()
            tick = mt5.symbol_info_tick(self.symbol)
            spread = tick.ask - tick.bid
            self.last_ask = tick.ask
            self.last_bid = tick.bid

            # 2. Update Time
            now_cet = self.get_server_time_cet(self.symbol)

            # C. Check for existing positions (Recovery/Management)
            positions = mt5.positions_get(symbol=self.symbol)
            if positions is None:
                self.logger.error("Failed to retrieve positions:", mt5.last_error())
                continue

            if not positions and not self.args.keep_alive:
                self.logger.info(f"No positions found for {self.symbol}, exiting...")
                self.delete_orders(self.symbol, self.magic_num)
                return



            my_pos = [p for p in positions if p.magic == self.magic_num]

            # Manage foreign order
            # self.foreign_order(my_pos)
            # self.alien_order(my_pos)

            if args.foreign:
                if not logged_system:
                    self.logger.info("Setting up foreign order management systems...")
                    logged_system = True
                self.foreign_order(my_pos)
            elif args.alien:
                if not logged_system:
                    self.logger.info("Setting up alien order management systems...")
                    logged_system = True
                self.alien_order(my_pos)
            else:
                self.logger.info("Proceeding without foreign or alien orders.")

            open_pos = len(my_pos) > 0

            for pos in my_pos:
                if pos.ticket not in self.states:
                    self.states[pos.ticket] = StrategyState()
                    self.states[pos.ticket].in_trade = True
                    self.states[pos.ticket].tp_pips = abs(pos.price_open - pos.tp)

            # -----------------------------------------------------------
            # EXIT / RISK MANAGEMENT LOGIC
            # -----------------------------------------------------------
            for i, pos in enumerate(my_pos):
                # Mandatory Close (Time)
                close_time = time(
                    self.config["session"]["end_hour"], self.config["session"]["end_minute"]
                )
                # if now_cet.time() >= close_time:
                #     logger.info(f"{now_cet.time()} -> {close_time}")
                #     logger.info("CLosing all Positions with magic '{self.magic_num}'")
                #     close_positions(symbol)
                #     continue

                # Trailing Stop Logic
                current_profit_points = (
                    (tick.bid - pos.price_open)
                    if pos.type == mt5.ORDER_TYPE_BUY
                    else (pos.price_open - tick.ask)
                )
                # Adjust for point value
                current_profit_points *= self.contract_size  # in pips

                if current_profit_points > self.states[pos.ticket].max_pnl:
                    self.logger.info(
                        f"Max profit pips: {self.states[pos.ticket].max_pnl} -> {current_profit_points}"
                    )
                    levels = [
                        s["min_profit"] * self.max_tp / 100
                        - (((self.max_tp - self.states[pos.ticket].tp_pips) / self.contract_size) if not self.foreign_activated else 0)
                        - spread
                        for s in self.config["risk_management"]["trailing_stages"]
                    ]
                    self.logger.info(f"\tTrailing levels: {levels}")
                    # logger.info(f"LLL: {abs(pos.price_open - pos.tp)}")
                    # logger.info(f"TP diff: {args.max_tp - (abs(pos.price_open - pos.tp) / contract_size)}")

                self.states[pos.ticket].max_pnl = max(
                    self.states[pos.ticket].max_pnl, current_profit_points
                )
                self.states[pos.ticket].curr_pnl = current_profit_points

                # Check Stages
                best_retention = 0.0
                triggered = False
                tp_ratio = 1
                tp_diff = 0
                trail_scale = self.max_tp / 100  # Initial is based on 100 tp
                dist_to_tp = None
                if pos.tp > 0.0:
                    tp_pips = self.states[pos.ticket].tp_pips / self.contract_size
                    tp_ratio = tp_pips / self.max_tp
                    tp_diff = self.max_tp - tp_pips


                    if pos.type == mt5.ORDER_TYPE_BUY:
                        dist_to_tp = (pos.tp - tick.bid) / self.contract_size
                        if tick.bid >= pos.price_open + self.states[pos.ticket].tp_pips:
                            tp_diff = 0
                    else:
                        dist_to_tp = (tick.ask - pos.tp) / self.contract_size
                        if tick.ask <= pos.price_open - self.states[pos.ticket].tp_pips:
                            tp_diff = 0

                for s in self.config["risk_management"]["trailing_stages"]:
                    # if self.states[pos.ticket].max_pnl >= s['min_profit'] * tp_ratio * trail_scale:
                    scaled_stage = ((s["min_profit"] * trail_scale) - tp_diff) - spread
                    if self.states[pos.ticket].max_pnl >= scaled_stage:
                        best_retention = s["retention"]
                        triggered = True

                if triggered:
                    # Calculate new SL
                    if best_retention == -1:
                        # Leave sl at original
                        continue
                    elif best_retention == 0:
                        # Set all SLs to first order
                        max_pips = -1
                        pos1 = None
                        for p in my_pos:
                            pips = (
                                (tick.bid - p.price_open)
                                if p.type == mt5.ORDER_TYPE_BUY
                                else (p.price_open - tick.ask)
                            )
                            if pips > max_pips:
                                max_pips = pips
                                pos1 = p

                        min_diff = mt5.symbol_info(self.symbol).point
                        new_sl = pos1.price_open + min_diff
                        new_sl = normalize_price(new_sl, self.symbol_info)

                        # Only modify if new SL is better (Higher for Buy, Lower for Sell)
                        should_mod = False
                        if pos.type == mt5.ORDER_TYPE_BUY and (new_sl > pos.sl):
                            should_mod = True
                        if pos.type == mt5.ORDER_TYPE_SELL and (new_sl < pos.sl):
                            should_mod = True

                        if should_mod:
                            self.logger.info(f"Modified SL: {pos.sl} => {new_sl}")
                            success = self.modify_sl_tp(self.symbol, pos, new_sl)

                            for i in range(MAX_RETRIES):
                                if not success:
                                    step = 0#(2) ** i + i + 1
                                    tick = mt5.symbol_info_tick(self.symbol)
                                    price = (
                                        tick.bid
                                        if pos.type == mt5.ORDER_TYPE_BUY
                                        else tick.ask
                                    )
                                    self.logger.info(
                                        f"    Rolling back SL by {step}... [Price at {price}]"
                                    )
                                    self.states[pos.ticket].max_pnl -= step
                                    new_sl = (
                                        (new_sl - step)
                                        if pos.type == mt5.ORDER_TYPE_BUY
                                        else (new_sl + step)
                                    )
                                    success = self.modify_sl_tp(
                                        self.symbol, pos, new_sl
                                    )

                            if dist_to_tp is not None and dist_to_tp <= 20:
                                if pos.tp != 0.0:
                                    self.remove_tp(self.symbol, pos)

                    else:
                        continue
                        trail_dist = self.states[pos.ticket].max_pnl * best_retention
                        trail_dist /= self.contract_size
                        new_sl = (
                            (pos.price_open + trail_dist - self.stop_levels)
                            if pos.type == mt5.ORDER_TYPE_BUY
                            else (pos.price_open - trail_dist + self.stop_levels)
                        )
                        new_sl = normalize_price(new_sl, self.symbol_info)

                        if self.foreign_activated:
                            new_sl = None
                            if pos.type == mt5.ORDER_TYPE_BUY:
                                tp_pips = tick.bid - self.entries[0]
                                trail = tp_pips * 0.95 / self.contract_size
                                price = max(self.boundaries[0] - self.stop_levels, self.entries[0] + trail) #tick.bid - self.stop_levels
                                if price > pos.sl:
                                    if price >= tick.bid - self.stop_levels:
                                        new_sl = normalize_price(price, self.symbol_info)
                            else:
                                tp_pips = self.entries[1] - tick.ask
                                trail = tp_pips * 0.95 / self.contract_size
                                price = min(self.boundaries[1] + self.stop_levels, self.entries[1] - trail)
                                if price < pos.sl:
                                    if price <= tick.ask + self.stop_levels:
                                        new_sl = normalize_price(price, self.symbol_info)

                            if dist_to_tp is not None and dist_to_tp <= 20:
                                if pos.tp != 0.0:
                                    # self.remove_tp(self.symbol, pos)
                                    self.move_tp(pos)

                            if new_sl is None:
                                continue

                        # Only modify if new SL is better (Higher for Buy, Lower for Sell)
                        should_mod = False
                        if pos.type == mt5.ORDER_TYPE_BUY and (new_sl > pos.sl):
                            should_mod = True
                        if pos.type == mt5.ORDER_TYPE_SELL and (new_sl < pos.sl):
                            should_mod = True
                        if pos.sl == 0.0:
                            should_mod = True

                        if should_mod:
                            success = self.modify_sl_tp(self.symbol, pos, new_sl)

                            if not success:
                                self.logger.error(f"Failed to modify {pos.ticket}")
                                self.close_position(pos)
                            else:
                                self.logger.info(f"Modified SL: {pos.sl} => {new_sl}")

                            # for i in range(MAX_RETRIES):
                            #     if not success:
                            #         step = (2) ** i
                            #         tick = mt5.symbol_info_tick(self.symbol)
                            #         price = (
                            #             tick.bid
                            #             if pos.type == mt5.ORDER_TYPE_BUY
                            #             else tick.ask
                            #         )
                            #         self.logger.info(
                            #             f"    SL mod to [{new_sl}] failed with Price [{price} >> [{tick.ask} / {tick.bid} ]]"
                            #         )
                            #         self.logger.info(f"    Rolling back SL by {step}...")
                            #         self.states[pos.ticket].max_pnl -= step
                            #         new_sl = (
                            #             (new_sl - step)
                            #             if pos.type == mt5.ORDER_TYPE_BUY
                            #             else (new_sl + step)
                            #         )
                            #         if pos.type == mt5.ORDER_TYPE_BUY and not (new_sl > pos.sl):
                            #             continue
                            #         if pos.type == mt5.ORDER_TYPE_SELL and not (new_sl < pos.sl):
                            #             continue
                            #         success = self.modify_sl_tp(
                            #             self.symbol, pos, new_sl
                            #         )

                            if dist_to_tp is not None and dist_to_tp <= 20:
                                if pos.tp != 0.0:
                                    # self.remove_tp(self.symbol, pos)
                                    self.move_tp(pos)


# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def int_or_float(value: str) -> float:
    """Parse a value as int or float."""
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{value} is not a valid int or float"
            )

def main(args_list=None):
    parser = argparse.ArgumentParser(
        description="Live trading momentum based bot for HFM"
    )

    parser.add_argument("symbol", help="symbol to be traded")
    parser.add_argument(
        "--magic", type=int, default=MAGIC_NUM, help="Unique magic number for bot"
    )
    parser.add_argument(
        "--entry", type=int_or_float, help="Initial entry of orders/stack"
    )
    parser.add_argument(
        "--max-tp",
        type=int_or_float,
        default=100,
        help="Max tp of active trades. Will be used to scale trails for all trades",
    )
    parser.add_argument(
        "--scale",
        action="store_true",
        help="Scale all monitored trade's trails by max tp",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="Keep manager active with no open positions",
    )
    parser.add_argument(
        "--foreign",
        action="store_true",
        help="Activate foreign order system`",
    )
    parser.add_argument(
        "--alien",
        action="store_true",
        help="Activate alien order system`",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args(args_list)

    symbol = args.symbol.strip() #.upper()
    magic_num = args.magic

    from utils import TradingLogger
    trading_logger = TradingLogger()
    trading_logger.setup_logging(symbol, args.log_level)

    position_manager = PositionManager(symbol, magic_num, args, logger=None)
    try:
        position_manager.run(args)
    except KeyboardInterrupt:
        raise KeyboardInterrupt()
    except Exception as e:
        position_manager.logger.error(f"Unexpected error: {e}")
    finally:
        position_manager.delete_orders(position_manager.symbol, position_manager.magic_num)
        position_manager.logger.info("Cleaned up orders")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Stopping Positions Manager...")
        mt5.shutdown()
