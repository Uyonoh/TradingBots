import os
import time
import argparse
import MetaTrader5 as mt5
# import pandas as pd
from dotenv import load_dotenv
import logging
from orders import order, doublebanger, get_pending_orders, get_open_positions
load_dotenv()

LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]



logging.basicConfig(level=logging.INFO, format='[%(asctime)s.%(msecs)03d] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')

# Example: Place 5 buy limit orders on GER40, starting at 18000.0, spaced 20 pips apart
# place_buy_grid(
#         symbol="GER40",
#         start_price=18000.0,
#         spacing_pips=20.0,
#         num_orders=5,
#         volume_per_order=0.1,
#         stop_loss_pips=50.0,
#         take_profit_pips=100.0
#     )

def man():
    order(
        order_type="sell stop",
        start_price=24628.00,
        spacing_pips=10.0,
        num_orders=10,
        volume_per_order=0.01,
        stop_loss_pips=10.0,
        take_profit_pips=10.0
    )

    order(
        order_type="buy limit",
        start_price=24628.00,
        spacing_pips=10.0,
        num_orders=1,
        volume_per_order=0.01,
        stop_loss_pips=10.0,
        take_profit_pips=10.0
    )

# def double_banger():
    
#     logging.info("Initializing MT5 client...")
#     if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
#         logging.error(f"Initialization failed: {mt5.last_error()}")
#         logging.info(f"{LOGIN=} {PASSWORD=} {SERVER=}")
#         mt5.shutdown()
#         return
    
#     symbol = input("Enter symbol: ").strip().upper()
#     if not symbol in symbol_pips:
#         logging.error("Invalid symbol")
#         return
    
#     start_price = input("Enter Entry price [12345.67]: ")
#     try:
#         start_price = float(start_price)
#     except Exception:
#         logging.error("Price must be a string, not %s", start_price)
#         return

#     symbol_ticks = mt5.symbol_info_tick(symbol)
#     spread = symbol_ticks.ask - symbol_ticks.bid
#     logging.info("Current spread is %s", spread)

#     pip_value = mt5.symbol_info(symbol).point * symbol_pips[symbol]

#     if start_price > symbol_ticks.ask:
#         logging.info("Entry is above market price")
#         if input('Type "BUY" to continue: ') != "BUY":
#             logging.error("Aborting operation")
#             return
#         order(
#             symbol=symbol,
#             order_type="buy stop",
#             start_price=start_price,
#             spacing_pips=10.0,
#             num_orders=10,
#             volume_per_order=0.01,
#             stop_loss_pips=10.0,
#             take_profit_pips=10.0
#         )

#         order(
#             symbol=symbol,
#             order_type="sell limit",
#             start_price=start_price,
#             spacing_pips=10.0,
#             num_orders=1,
#             volume_per_order=0.01,
#             stop_loss_pips=10.0,
#             take_profit_pips=10.0
#         )
        
#         pending = True
#         start_price -= 10 * pip_value
#         while pending:
#             if input('Enter "SELL" to place remaining sell stops: ') == "SELL":
#                 symbol_ticks = mt5.symbol_info_tick(symbol)
#                 if symbol_ticks.ask < start_price:
#                     logging.error("Market price still below %s", start_price)
#                     continue
#                 order(
#                     symbol=symbol,
#                     order_type="sell stop",
#                     start_price=start_price,
#                     spacing_pips=10.0,
#                     num_orders=9,
#                     volume_per_order=0.01,
#                     stop_loss_pips=10.0,
#                     take_profit_pips=10.0
#                 )
#                 pending = False

#     elif start_price < symbol_ticks.bid:
#         logging.info("Entry is below market price")
#         if input('Type "SELL" to continue: ') != "SELL":
#             logging.error("Aborting operation")
#             return
#         order(
#             symbol=symbol,
#             order_type="sell stop",
#             start_price=start_price,
#             spacing_pips=10.0,
#             num_orders=10,
#             volume_per_order=0.01,
#             stop_loss_pips=10.0,
#             take_profit_pips=10.0
#         )

#         order(
#             symbol=symbol,
#             order_type="buy limit",
#             start_price=start_price,
#             spacing_pips=10.0,
#             num_orders=1,
#             volume_per_order=0.01,
#             stop_loss_pips=10.0,
#             take_profit_pips=10.0
#         )
        
#         pending = True
#         start_price += 10 * pip_value
#         while pending:
#             if input('Enter "BUY" to place remaining buy stops: ') == "BUY":
#                 symbol_ticks = mt5.symbol_info_tick(symbol)
#                 if symbol_ticks is None:
#                     print(f"Error for {symbol}: {mt5.last_error()}")
#                     return None
#                 if symbol_ticks.bid > start_price:
#                     logging.error("Market price still above %s", start_price)
#                     continue
#                 order(
#                     symbol=symbol,
#                     order_type="buy stop",
#                     start_price=start_price,
#                     spacing_pips=10.0,
#                     num_orders=9,
#                     volume_per_order=0.01,
#                     stop_loss_pips=10.0,
#                     take_profit_pips=10.0
#                 )
#                 pending = False
    
class TradingBot:
    symbol_pips = {
        "XAU": 10,
        "GER40": 100,
        "#BTCUSD": 1000,
        }
    
    def __init__(self, symbol):
        symbol = symbol.upper()
        self.symbol = symbol
        self.name = f"{symbol}_bot"
        self.magic_number = 123456
        self.logger = logging.getLogger(self.name) # Best practice: named logger

        # 1. Validation
        if symbol not in self.symbol_pips:
            self.logger.error(f"Unsupported symbol: {symbol}")
            raise ValueError(f"Symbol {symbol} not in supported list.")

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
        
        # Calculate Pip Value
        self.pip_value = info.point * self.symbol_pips[symbol]
        
        self.logger.info(f"Successfully initialized {self.name} for {symbol}")
    
    def get_symbol_ticks(self):
        return mt5.symbol_info_tick(self.symbol)

    def get_inputs(self, inputs: list[str]=None) -> dict[str]:
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
                        logging.info("Input must be a number")
                        continue
                valid = True
        
        # Check entry proximity
        active_trades = get_open_positions() + get_pending_orders()
        for row in active_trades:
            if abs(row["entry_price"] - data["entry"]) <= 50:
                self.logger.info("%s order for %s from %s at %s", row["side"], row["symbol"], row["entry_price"], row["entry_time"])
                self.logger.info("Order price within 50 pips of existing order. Continue trade? [y/n]")
                if input("").lower == "y":
                    break
                raise ValueError("Aborting order...")

        return data
    
    def order_buy(self):
        logging.info("Buy order is not implemented yet due to security")
    
    def order_buystop(self):
        print("Buy stop ORDER")
        inputs = self.get_inputs()
        print(inputs)
    
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
            take_profit_pips=inputs["tp pips"]
        )
    
    def order_sell(self):
        logging.info("Sell order is not implemented yet due to security")
    
    def order_sellstop(self):
        print("Sell stop ORDER")
    
    def order_sell_limit(self):
        print("Sell LIMIT ORDER")
    
    def doublebanger(self, inputs: dict=None, **kwargs):
        if inputs is None:
            inputs = self.get_inputs(["spacing_pips", "num_orders"])
        return doublebanger(self, inputs, **kwargs)

    def daily_banger(self):
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_D1, 0, 1)
        day_open = rates[0]["open"]
        breadth = 550

        positions = {
            "top": {
                "entry": day_open + breadth,
            },
            "bottom": {
                "entry": day_open - breadth,
            },
        }
        # sl_pips = args.slpips
        inputs = {
                "lot_size":0.01, "sl pips": 10, "tp pips": 10,
                "spacing_pips": 10, "num_orders": 20
                }
        for pos,v in positions.items():
            inputs["entry"] =  v["entry"]
            positions[pos]["pending_order"] = self.doublebanger(inputs=inputs, confirm=False, autocomplete=True)

        # Wait for orders to be filled
        top_filled = False
        bottom_filled = False
        while not top_filled or bottom_filled:
            self.logger.info("Waiting for orders to be filled..")
            time.sleep(2)

            active_orders = mt5.orders_get(symbol=self.symbol)
            if not active_orders:
                break
            active_tickets = [order.ticket for order in active_orders]

            top_still_pending = positions["top"]["pending_order"] in active_tickets
            bottom_still_pending = positions["bottom"]["pending_order"] in active_tickets

            self.logger.info(f"{top_still_pending=}")
            self.logger.info(f"{bottom_still_pending=}")
            if not top_still_pending and not top_filled:
                self.logger.info("Top order filled, making remaining sell orders...")
                order(
                    symbol=bot.symbol,
                    order_type="sell stop",
                    start_price=positions["top"]["entry"] - inputs["spacing_pips"] * self.pip_value,
                    spacing_pips=inputs["spacing_pips"],
                    num_orders=inputs["num_orders"] - 1,
                    volume_per_order=inputs["lot_size"] ,
                    stop_loss_pips=inputs["sl pips"],
                    take_profit_pips=inputs["tp pips"]
                    )
                top_filled = True
            
            if not bottom_still_pending and not bottom_filled:
                self.logger.info("Bottom order filled, making remaining buy orders...")
                order(
                    symbol=bot.symbol,
                    order_type="buy stop",
                    start_price=positions["top"]["entry"] + inputs["spacing_pips"] * self.pip_value,
                    spacing_pips=inputs["spacing_pips"],
                    num_orders=inputs["num_orders"] - 1,
                    volume_per_order=inputs["lot_size"] ,
                    stop_loss_pips=inputs["sl pips"],
                    take_profit_pips=inputs["tp pips"]
                    )
                bottom_filled = True


    
    def __del__(self):
        """Ensures the connection is closed when the object is destroyed."""
        mt5.shutdown()
        # logging.info is safer than self.logger.info here during cleanup
        logging.info(f"MT5 connection closed for {self.name}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="automate trades")
    parser.add_argument("action",help="Market action")
    parser.add_argument("symbol", help="Symbol to be traded")
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

    dispatch[action]()



# TODO: Make individual order functions that call common dep functions
# set buystop ger40  