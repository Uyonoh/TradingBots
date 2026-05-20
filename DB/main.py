import argparse
import datetime
from utils import TradingLogger
import os
import sys
import time

# import pandas as pd
from dotenv import load_dotenv

from positions_manager import main as launch_pos_manager
from orders import (
    doublebanger,
    get_open_positions,
    get_pending_orders,
    normalize_price,
    order,
)

load_dotenv()

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

LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]
MAGIC_NUM += "00"
MAGIC_NUM = int(MAGIC_NUM)


def man():
    order(
        order_type="sell stop",
        start_price=24628.00,
        spacing_pips=10.0,
        num_orders=10,
        volume_per_order=0.01,
        stop_loss_pips=10.0,
        take_profit_pips=10.0,
        magic=self.magic_number,
    )

    order(
        order_type="buy limit",
        start_price=24628.00,
        spacing_pips=10.0,
        num_orders=1,
        volume_per_order=0.01,
        stop_loss_pips=10.0,
        take_profit_pips=10.0,
        magic=self.magic_number,
    )


class TradingBot:
    def __init__(self, symbol):
        symbol = symbol.upper()
        self.symbol = symbol
        self.name = f"{symbol}_bot"
        now = datetime.datetime.now()
        self.magic_number = now.hour*100*100 + now.minute*100 + now.second
        self.contract_size = None
        trading_logger = TradingLogger()
        self.logger = trading_logger.setup_logging(symbol, args.log_level)

        # 2. MT5 Initialization
        self.logger.info("Initializing MT5 connection...")
        if not mt5.initialize():
            err = mt5.last_error()
            self.logger.error(f"MT5 terminal initialization failed: {err}")
            raise ConnectionError(f"Could not connect to MT5 terminal: {err}")

        # 3. Secure Login (Ensure LOGIN/PASSWORD/SERVER are defined elsewhere)
        if not mt5.login(login=LOGIN, password=PASSWORD, server=SERVER):
            err = mt5.last_error()
            self.logger.error(f"Login failed for account {LOGIN}: {err}")
            mt5.shutdown()
            raise PermissionError(f"MT5 login failed: {err}")

        # 4. Symbol Info Retrieval
        info = mt5.symbol_info(symbol)
        if info is None:
            self.logger.error(f"Symbol {symbol} not found in Market Watch.")
            mt5.shutdown()
            raise NameError(f"Symbol {symbol} is unavailable.")

        self.symbol_info = mt5.symbol_info(symbol)
        self.contract_size = self.symbol_info.trade_contract_size

        # Handle Magic number
        # positions = [p for p in mt5.positions_get() if p.symbol == self.symbol]
        # for pos in positions:
        #     if pos.magic == self.magic_number:
        #         print(f"Magic {self.magic_number} exists, incrementing...")
        #         self.magic_number += 1

        # orders = [o for o in mt5.orders_get() if o.symbol == self.symbol]
        # for order in orders:
        #     if order.magic == self.magic_number:
        #         print(f"Magic {self.magic_number} exists, incrementing...")
        #         self.magic_number += 1

        self.logger.info(f"Successfully initialized {self.name} for {symbol}")
        if not self.contract_size:
            self.logger.error(f"No contract: {symbol}")
            raise ValueError(f"Symbol {symbol} did not return a contract size.")

    def get_symbol_ticks(self):
        return mt5.symbol_info_tick(self.symbol)

    def get_inputs(self, inputs: list[str] = None) -> dict[str]:
        if inputs is None:
            inputs = []

        inputs = ["entry", "sl pips", "tp pips"] + inputs
        data = {}
        for key in inputs:
            valid = False
            while not valid:
                data[key] = input(f"Enter {key}: ")
                # Try to convert to int, then float, then throw error
                try:
                    data[key] = int(data[key])
                except Exception:
                    try:
                        data[key] = float(data[key])
                    except Exception:
                        self.logger.info(
                            f"{key} : '{data[key]}' saved as string. Could not convert to number"
                        )
                        continue
                valid = True

        # Check entry proximity
        active_trades = get_open_positions() + get_pending_orders()
        for row in active_trades:
            if abs(row["entry_price"] - data["entry"]) <= 50:
                self.logger.info(
                    "%s order for %s from %s at %s",
                    row["side"],
                    row["symbol"],
                    row["entry_price"],
                    row["entry_time"],
                )
                self.logger.info(
                    "Order price within 50 pips of existing order. Continue trade? [y/n]"
                )
                if input("").lower() == "y":
                    break
                raise ValueError("Aborting order...")

        return data

    def order_buy(self):
        self.logger.info("Buy order is not implemented yet due to security")

    def order_buystop(self):
        print("Buy stop ORDER")
        inputs = self.get_inputs()
        order(
            symbol=self.symbol,
            order_type="buy stop",
            start_price=inputs["entry"],
            spacing_pips=10.0,
            num_orders=1,
            volume_per_order=0.01,
            stop_loss_pips=inputs["sl pips"],
            take_profit_pips=inputs["tp pips"],
            magic=self.magic_number,
        )

    def order_buylimit(self):
        inputs = self.get_inputs()
        order(
            symbol=self.symbol,
            order_type="buy limit",
            start_price=inputs["entry"],
            spacing_pips=10.0,
            num_orders=1,
            volume_per_order=0.01,
            stop_loss_pips=inputs["sl pips"],
            take_profit_pips=inputs["tp pips"],
            magic=self.magic_number,
        )

    def order_sell(self):
        self.logger.info("Sell order is not implemented yet due to security")

    def order_sellstop(self):
        print("Sell stop ORDER")

    def order_sell_limit(self):
        print("Sell LIMIT ORDER")

    def banger(self, inputs: dict = None, **kwargs):
        if inputs is None:
            inputs = self.get_inputs(["num_orders"])
        return doublebanger(self, inputs, **kwargs)

    def doublebanger(self, args, **kwargs):
        inputs: dict = kwargs.get("inputs", None)
        if inputs is None:
            inputs = self.get_inputs(["num_orders", "spacing_pips"])

        if args.time:
            try:
                h, m, *s = (int(t) for t in args.time.split(":"))
                s = int(s[0]) if len(s) > 0 else 0
                now = datetime.datetime.now()
                target_time = now.replace(hour=h, minute=m, second=s, microsecond=0)
                end_time = target_time + datetime.timedelta(
                    minutes=10, seconds=59, microseconds=99
                )
                if end_time < now:
                    target_time = target_time + datetime.timedelta(hours=24)
                    end_time = end_time + datetime.timedelta(hours=24)
                is_entry = False
                while not is_entry:
                    now = datetime.datetime.now()
                    print(f"GTE start: {target_time <= now}")
                    print(f"LTE end: {now <= end_time}")
                    print(f"entry: {target_time <= now <= end_time}")
                    if target_time <= now <= end_time:
                        is_entry = True
                        continue
                    print(f"Time not up to {target_time}...")
                    if (target_time - now).seconds >= 65 * 60:
                        print("Sleeping for 1 hour")
                        time.sleep(60 * 60)
                    elif (target_time - now).seconds >= 30 * 60:
                        print("Sleeping for 10 minutes")
                        time.sleep(10 * 60)
                    elif (target_time - now).seconds >= 10 * 60:
                        print("Sleeping for 5 minutes")
                        time.sleep(5 * 60)
                    elif (target_time - now).seconds >= 3 * 60:
                        print("Sleeping for 1 minute")
                        time.sleep(1 * 60)
                    else:
                        time.sleep(10)
            except Exception as e:
                raise Exception(f"Data error: {e}")

        comment = "Double " + args.comment
        inputs["entry"] = normalize_price(
            inputs["entry"], self.symbol_info, is_entry=True
        )
        results = doublebanger(
            self, inputs, magic=self.magic_number, comment=comment, confirm=False, autocomplete=True
        )
        pending_order = results["orders"][0]
        opp_direction = results["opp_direction"]
        dir_multiplier = results["dir_multiplier"]
        order_filled = False

        initial_entry = inputs["entry"]
        sl_pips = inputs["sl pips"]
        tp_pips = inputs["tp pips"]
        orders = inputs["num_orders"]
        spacing_pips = inputs["spacing_pips"]
        lot_size = inputs.get("lot_size", 0.01)

        if not isinstance(spacing_pips, (int, float)):
            spacing_pips = 10  # tp_pips / orders
            self.logger.info(f"Using spacing: {spacing_pips}")

        doubled_pos = [2, 5, 9]

        symbol_ticks = self.get_symbol_ticks()
        spread = symbol_ticks.ask - symbol_ticks.bid
        initial_entry = initial_entry - (spread * dir_multiplier)

        order_still_pending = True
        # dir_multiplier = -1 if direction == "buy" else 1
        # initial_entry = initial_entry - (spacing_pips * bot.contract_size * dir_multiplier)
        while order_still_pending:
            self.logger.info("Waiting for orders to be filled..")
            time.sleep(0.1)

            active_orders = mt5.orders_get(symbol=self.symbol)
            if not active_orders:
                break
            active_tickets = [order.ticket for order in active_orders]
            order_still_pending = pending_order in active_tickets

            if not order_still_pending:
                for i in range(1, orders):
                    lots = lot_size
                    if i in doubled_pos:
                        lots = lot_size * 2

                    if i == (orders - 1):
                        lot_list = [
                            0.01,
                            0.01,
                            0.02,
                            0.01,
                            0.01,
                            0.02,
                            0.01,
                            0.01,
                            0.01,
                            0.02,
                        ]
                        final_lot = sum(lot_list[orders - 1 :])
                        lots = final_lot

                    entry = initial_entry - (i * spacing_pips * dir_multiplier)
                    tp = tp_pips - (i * spacing_pips)
                    sl = sl_pips



                    order(
                        symbol=bot.symbol,
                        order_type=f"{opp_direction} stop",
                        start_price=entry,
                        spacing_pips=0,
                        num_orders=1,
                        volume_per_order=lots,
                        stop_loss_pips=sl,
                        take_profit_pips=tp,
                        comment=comment,
                        magic=self.magic_number,
                    )
                order_filled = True
                launch_pos_manager([self.symbol, "--magic", str(self.magic_number), "--max-tp", str(tp_pips)])

    def daily_banger(self, args, **kwargs):
        # rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_D1, 0, 1)
        # day_open = rates[0]["open"]
        # rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_H1, 1, 1)
        # day_open = rates[0]["close"]  # H1 open = last close
        # Use current price and offset

        if args.time:
            try:
                h, m, *s = (int(t) for t in args.time.split(":"))
                s = int(s[0]) if len(s) > 0 else 0
                now = datetime.datetime.now()
                target_time = now.replace(hour=h, minute=m, second=s, microsecond=0)
                end_time = target_time + datetime.timedelta(
                    minutes=10, seconds=59, microseconds=99
                )
                if end_time < now:
                    target_time = target_time + datetime.timedelta(hours=24)
                    end_time = end_time + datetime.timedelta(hours=24)
                is_entry = False
                while not is_entry:
                    now = datetime.datetime.now()
                    print(f"GTE start: {target_time <= now}")
                    print(f"LTE end: {now <= end_time}")
                    print(f"entry: {target_time <= now <= end_time}")
                    if target_time <= now <= end_time:
                        is_entry = True
                        continue
                    print(f"Time not up to {target_time}...")
                    if (target_time - now).seconds >= 65 * 60:
                        print("Sleeping for 1 hour")
                        time.sleep(60 * 60)
                    elif (target_time - now).seconds >= 30 * 60:
                        print("Sleeping for 10 minutes")
                        time.sleep(10 * 60)
                    elif (target_time - now).seconds >= 10 * 60:
                        print("Sleeping for 5 minutes")
                        time.sleep(5 * 60)
                    elif (target_time - now).seconds >= 3 * 60:
                        print("Sleeping for 1 minute")
                        time.sleep(1 * 60)
                    else:
                        time.sleep(10)
            except Exception as e:
                raise Exception(f"Data error: {e}")

        # min_dist = mt5.symbol_info(self.symbol).trade_stops_level
        # digits = mt5.symbol_info(self.symbol).digits
        # min_pips = (min_dist * (10 ** -digits)) + 2
        tick = mt5.symbol_info_tick(self.symbol)
        day_open = round((tick.ask + tick.bid) / 2, 5)
        breadth = (
            max(args.breadth, self.symbol_info.spread * (10**-self.symbol_info.digits))
            + (tick.ask - tick.bid) / 2
        )
        # breadth if breadth > min_pips else min_pips

        positions = {
            "top": {
                "entry": normalize_price(
                    day_open + breadth, self.symbol_info, is_entry=True
                ),
            },
            "bottom": {
                "entry": normalize_price(
                    day_open - breadth, self.symbol_info, is_entry=True
                ),
            },
        }
        # sl_pips = args.slpips
        inputs = {
            "lot_size": 0.01,
            "sl pips": 100,
            "tp pips": 100,
            "spacing_pips": 10,
            "num_orders": 6,
        }

        if args.target == 50:
            inputs = {
                "lot_size": 0.01,
                "sl pips": 100/2,
                "tp pips": 100/2,
                "spacing_pips": 10/2,
                "num_orders": 6,
            }
        elif args.target == 200:
            inputs = {
                "lot_size": 0.01,
                "sl pips": 100*2,
                "tp pips": 100*2,
                "spacing_pips": 10*2,
                "num_orders": 6,
            }
        elif args.target == 400:
            inputs = {
                "lot_size": 0.01,
                "sl pips": 100 * 4,
                "tp pips": 100 * 4,
                "spacing_pips": 10 * 5,
                "num_orders": 8,
            }
        comment = "Day " + args.comment + " "

        self.logger.info(f"Initiating daily banger with inputs: {inputs}")
        self.logger.info(f"MAGIC NUMBER: {self.magic_number}")

        for pos, v in positions.items():
            inputs["entry"] = v["entry"]
            positions[pos]["positions"] = self.banger(
                inputs=inputs, magic=self.magic_number, comment=comment + pos, confirm=False, autocomplete=True
            )["orders"]
            self.logger.info(f"{positions[pos]['positions']=}")
            positions[pos]["pending_order"] = positions[pos]["positions"][0]

        # Wait for orders to be filled
        top_filled = False
        bottom_filled = False

        sl_pips = inputs["sl pips"]
        tp_pips = inputs["tp pips"]
        orders = inputs["num_orders"]
        lot_size = inputs["lot_size"]
        symbol_ticks = self.get_symbol_ticks()
        spacing_pips = inputs["spacing_pips"]
        spread = symbol_ticks.ask - symbol_ticks.bid

        if not isinstance(spacing_pips, (int, float)):
            spacing_pips = 10  # tp_pips / orders
            self.logger.info(f"Using spacing: {spacing_pips}")

        doubled_pos = [2, 5, 9]

        logged_t = None
        while not (top_filled or bottom_filled):
            if datetime.datetime.now().minute != logged_t:
                self.logger.info("Waiting for orders to be filled..")
                logged_t = datetime.datetime.now().minute
            time.sleep(0.1)

            active_orders = mt5.orders_get(symbol=self.symbol)
            if not active_orders:
                break
            active_tickets = [order.ticket for order in active_orders]

            top_still_pending = positions["top"]["pending_order"] in active_tickets
            bottom_still_pending = (
                positions["bottom"]["pending_order"] in active_tickets
            )

            # self.logger.info(f"    {top_still_pending=}")
            # self.logger.info(f"    {bottom_still_pending=}")
            if not top_still_pending and not top_filled:
                self.logger.info("Top order filled, making remaining sell orders...")
                initial_entry = positions["top"]["entry"]
                direction = "sell"
                dir_multiplier = -1 if direction == "buy" else 1
                initial_entry -= spread
                for i in range(1, orders):
                    lots = lot_size
                    if i in doubled_pos:
                        lots = lot_size * 2

                    if i == (orders - 1):
                        lot_list = [
                            0.01,
                            0.01,
                            0.02,
                            0.01,
                            0.01,
                            0.02,
                            0.01,
                            0.01,
                            0.01,
                            0.02,
                        ]
                        final_lot = sum(lot_list[orders - 1 :])
                        lots = final_lot

                    entry = initial_entry - (i * spacing_pips * dir_multiplier)
                    tp = tp_pips - (i * spacing_pips)
                    sl = sl_pips

                    order(
                        symbol=self.symbol,
                        order_type=f"{direction} stop",
                        start_price=entry,
                        spacing_pips=0,
                        num_orders=1,
                        volume_per_order=lots,
                        stop_loss_pips=sl,
                        take_profit_pips=tp,
                        comment=comment + "top",
                        magic=self.magic_number,
                    )
                    # ==============================================
                top_filled = True

            if not bottom_still_pending and not bottom_filled:
                self.logger.info("Bottom order filled, making remaining buy orders...")
                initial_entry = positions["bottom"]["entry"]
                direction = "buy"
                dir_multiplier = -1 if direction == "buy" else 1
                initial_entry += spread
                for i in range(1, orders):
                    lots = lot_size
                    if i in doubled_pos:
                        lots = lot_size * 2

                    if i == (orders - 1):
                        lot_list = [
                            0.01,
                            0.01,
                            0.02,
                            0.01,
                            0.01,
                            0.02,
                            0.01,
                            0.01,
                            0.01,
                            0.02,
                        ]
                        final_lot = sum(lot_list[orders - 1 :])
                        lots = final_lot

                    entry = initial_entry - (i * spacing_pips * dir_multiplier)
                    tp = tp_pips - (i * spacing_pips)
                    sl = sl_pips

                    order(
                        symbol=self.symbol,
                        order_type=f"{direction} stop",
                        start_price=entry,
                        spacing_pips=0,
                        num_orders=1,
                        volume_per_order=lots,
                        stop_loss_pips=sl,
                        take_profit_pips=tp,
                        comment=comment + "bottom",
                        magic=self.magic_number,
                    )
                bottom_filled = True
        # When one is filed cancel the other
        level = "top" if bottom_filled else "bottom"
        entry = "top" if bottom_filled else "bottom"
        active_tickets = positions[level]["positions"]
        for i, ticket in enumerate(active_tickets):
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
                "comment": "Opposite side filled",
            }
            result = mt5.order_send(request)
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                print(
                    f"Order {i + 1} on {level} failed, ticket={ticket}, retcode={result.retcode}, error={mt5.last_error()}"
                )
            else:
                print(f"Canceled order no {i + 1} on {level}: {ticket}")

        launch_pos_manager([self.symbol,"--magic", str(self.magic_number), "--max-tp", str(tp_pips), "--entry", str(initial_entry)])

    def __del__(self):
        """Ensures the connection is closed when the object is destroyed."""
        try:
            mt5.shutdown()
            # logger.info is safer than self.logger.info here during cleanup
            self.logger.info(f"MT5 connection closed for {self.name}")
        except Exception as e:
            self.logger.error(f"Shutdown failed with error:\n\t{e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="automate trades")
    parser.add_argument("action", help="Market action")
    parser.add_argument("symbol", help="Symbol to be traded")
    parser.add_argument("--log-level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Logging level")
    parser.add_argument(
        "--comment",
        help="Comment for trade deals",
        default=datetime.datetime.now().strftime("%H:%M:%S"),
    )
    parser.add_argument("--time", help="start time with window of 5mins [hh:mm]")
    parser.add_argument("--target", type=int, default=0, help="Target tp pips")
    parser.add_argument("--breadth", type=int, default=5, help="Spacing from price to daily bangers")
    args = parser.parse_args()

    bot = TradingBot(args.symbol)
    dispatch = {
        "buy": bot.order_buy,
        "buystop": bot.order_buystop,
        "buylimit": bot.order_buylimit,
        "sell": bot.order_sell,
        "sellstop": bot.order_sellstop,
        "selllimit": bot.order_sell_limit,
        "doublebanger": bot.doublebanger,
        "dailybanger": bot.daily_banger,
    }

    action = args.action.replace("_", "")
    if not action in dispatch.keys():
        # return
        pass

    dispatch[action](args)


# TODO: Make individual order functions that call common dep functions
# set buystop ger40
