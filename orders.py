import os
import argparse
import MetaTrader5 as mt5
import sqlite3
# import pandas as pd
from dotenv import load_dotenv
import logging
load_dotenv()
# PnL = price_delta × volume × contract_size

get_db_connection = lambda: sqlite3.connect("trades.db.sqlite3")

LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]

symbol_pips = {
        "XAU": 10,
        "GER40": 100,
        "USA30": 100,
        "USA100": 100,
        "#BTCUSD": 1000,
        }


def order(order_type, start_price, spacing_pips, num_orders, volume_per_order, stop_loss_pips=None, take_profit_pips=None, symbol="GER40"):
    """
    Places a grid of Buy Limit orders.
    
    Args:
        symbol (str): Trading symbol (e.g., 'GER40', 'BTCUSD').
        start_price (float): Price for the first order.
        spacing_pips (float): Distance between orders in pips.
        num_orders (int): Total number of buy orders to place.
        volume_per_order (float): Trade volume for each order.
        stop_loss_pips (float): Optional SL distance in pips for all orders.
        take_profit_pips (float): Optional TP distance in pips for all orders.
    """

    ALLOWED_ORDERS = {
        "buy stop": mt5.ORDER_TYPE_BUY_STOP,
        "buy limit": mt5.ORDER_TYPE_BUY_LIMIT,
        # "buy stop limit": mt5.ORDER_TYPE_BUY_STOP_LIMIT,
        "sell stop": mt5.ORDER_TYPE_SELL_STOP,
        "sell limit": mt5.ORDER_TYPE_SELL_LIMIT,        
        # "sell stop limit": mt5.ORDER_TYPE_SELL_STOP_LIMIT
    }

    logging.info("Recieved %s Order", order_type.capitalize())
    # 1. Connect to MT5 (Update login/password/server for your broker)
    # logging.info("Initializing MT5 client...")
    # if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
    #     logging.error(f"Initialization failed: {mt5.last_error()}")
    #     logging.info(f"{LOGIN=} {PASSWORD=} {SERVER=}")
    #     mt5.shutdown()
    #     return
    
    # 2. Prepare order request template[citation:1][citation:2]
    logging.info("Preparing request...")
    
    point = mt5.symbol_info(symbol).point
    pip_value = point * symbol_pips[symbol]

    valid_order = ALLOWED_ORDERS.get(order_type.lower(), None)
    if valid_order is None:
        logging.error(f"Invalid order type: {order_type}")
        return
    
    side = "BUY" if "buy" in order_type.lower() else "SELL"
    direction_factor = 1 if side == "BUY"  else -1
    # Controls the direction of progressive limit trades
    limit_direction_factor = -1 if "limit" in order_type.lower() else 1

    orders = []
    for i in range(num_orders):
        order_price = start_price + (i * spacing_pips * pip_value * direction_factor * limit_direction_factor)
        
        # Calculate SL/TP prices if provided
        sl_price = order_price - (stop_loss_pips * pip_value * direction_factor) if stop_loss_pips else 0.0
        tp_price = order_price + (take_profit_pips * pip_value * direction_factor) if take_profit_pips else 0.0

        # Validate price, sl and tp by order type
        if side == "BUY" and not (sl_price < tp_price):
            logging.error((f"Configuration error: sl - {sl_price} "
                          f"greater than tp - {tp_price} for a buy"))
            return
        if side == "SELL" and not (sl_price > tp_price):
            logging.error((f"Configuration error: sl - {sl_price} "
                          f"less than tp - {tp_price} for a sell"))
            return
        
        request = {
            "action": mt5.TRADE_ACTION_PENDING,       # Place a pending order[citation:1]
            "symbol": symbol,
            "volume": volume_per_order,
            "type": valid_order,
            "price": round(order_price, 6),          # Price of the pending order
            "sl": round(sl_price, 6) if sl_price else 0.0,
            "tp": round(tp_price, 6) if tp_price else 0.0,
            "deviation": 20,                         # Max price deviation in points
            "magic": 123456,                         # Unique EA/script identifier
            "comment": f"Grid {order_type} order {i+1}",
            "type_time": mt5.ORDER_TIME_GTC,         # Good Till Cancelled
            "type_filling": mt5.ORDER_FILLING_IOC,   # Execution policy[citation:1]
        }
        
        # 3. Send the order
        # logging.info("\n" + "="*50)
        # logging.info(f"Sending Request: \n{request}")
        # logging.info("="*50)
        result = mt5.order_send(request)
        
        # 4. Check and print result
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Order {i+1} failed, retcode={result.retcode}, error={mt5.last_error()}")
        else:
            logging.info(f"Order {i+1} placed successfully for {symbol} at {order_price}")
            save_new_position(result.order, symbol, side, volume_per_order, order_price)

        orders.append(result.order)
    
    return orders if len(orders) > 1 else orders[0]



def place_buy_grid(start_price, spacing_pips, num_orders, volume_per_order, stop_loss_pips=None, take_profit_pips=None, symbol="GER40"):
    """
    Places a grid of Buy Limit orders.
    
    Args:
        symbol (str): Trading symbol (e.g., 'GER40', 'BTCUSD').
        start_price (float): Price for the first order.
        spacing_pips (float): Distance between orders in pips.
        num_orders (int): Total number of buy orders to place.
        volume_per_order (float): Trade volume for each order.
        stop_loss_pips (float): Optional SL distance in pips for all orders.
        take_profit_pips (float): Optional TP distance in pips for all orders.
    """

    logging.info("Recieved Buy Limit Order")
    # 1. Connect to MT5 (Update login/password/server for your broker)
    logging.info("Initializing MT5 client...")
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        logging.error(f"Initialization failed: {mt5.last_error()}")
        logging.info(f"{LOGIN=} {PASSWORD=} {SERVER=}")
        mt5.shutdown()
        return
    
    # 2. Prepare order request template[citation:1][citation:2]
    logging.info("Preparing request...")
    
    point = mt5.symbol_info(symbol).point
    
    pip_value = point * symbol_pips[symbol]
    
    for i in range(num_orders):
        order_price = start_price - (i * spacing_pips * pip_value)
        
        # Calculate SL/TP prices if provided
        sl_price = order_price - (stop_loss_pips * pip_value) if stop_loss_pips else 0.0
        tp_price = order_price + (take_profit_pips * pip_value) if take_profit_pips else 0.0
        
        request = {
            "action": mt5.TRADE_ACTION_PENDING,       # Place a pending order[citation:1]
            "symbol": symbol,
            "volume": volume_per_order,
            "type": mt5.ORDER_TYPE_BUY_LIMIT,        # Use BUY_STOP for orders above price[citation:1]
            "price": round(order_price, 6),          # Price of the pending order
            "sl": round(sl_price, 6) if sl_price else 0.0,
            "tp": round(tp_price, 6) if tp_price else 0.0,
            "deviation": 20,                         # Max price deviation in points
            "magic": 123456,                         # Unique EA/script identifier
            "comment": f"Grid buy order {i+1}",
            "type_time": mt5.ORDER_TIME_GTC,         # Good Till Cancelled
            "type_filling": mt5.ORDER_FILLING_IOC,   # Execution policy[citation:1]
        }
        
        # 3. Send the order
        logging.info("\n" + "="*50)
        logging.info(f"Sending Request: \n{request}")
        logging.info("="*50)
        result = mt5.order_send(request)
        
        # 4. Check and print result
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"Order {i+1} failed, retcode={result.retcode}, error={mt5.last_error()}")
        else:
            logging.info(f"Order {i+1} placed successfully for {symbol} at {order_price}")
    
def doublebanger_orders(bot, inputs, direction, autocomplete=False):
    positions = []
    initial_entry = inputs["entry"]
    sl_pips = inputs["sl pips"]
    tp_pips = inputs["tp pips"]
    orders = inputs["num_orders"]
    lot_size = 0.01
    spacing_pips = tp_pips / orders
    dir_multiplier = 1 if direction == "buy" else -1

    # Only need to track this, for following orders
    opp_direction = "sell" if direction == "buy" else "buy"
    opp_entry = initial_entry - inputs["spread"] * dir_multiplier
    pending_order = order(
        symbol=bot.symbol,
        order_type=f"{opp_direction} limit",
        start_price=opp_entry,
        spacing_pips=0,
        num_orders=1,
        volume_per_order=lot_size,
        stop_loss_pips=sl_pips,
        take_profit_pips=tp_pips
    )
    positions.append(pending_order)
    # positions.insert(0, pending_order)

    for i in range(orders):
        lots = lot_size
        if i in [2, 5, 9]:
            lots = lot_size * 2
        entry = initial_entry + (i * spacing_pips * dir_multiplier)
        tp = tp_pips - (i * orders)
        sl = sl_pips
    
        pos = order(
            symbol=bot.symbol,
            order_type=f"{direction} stop",
            start_price=entry,
            spacing_pips=0,
            num_orders=1,
            volume_per_order=lots,
            stop_loss_pips=sl,
            take_profit_pips=tp
        )
        positions.append(pos)
    
    
    if not autocomplete:
        pending = True
        # dir_multiplier = -1 if direction == "buy" else 1
        # initial_entry = initial_entry - (spacing_pips * bot.pip_value * dir_multiplier)
        while pending:
            if input(f'Enter "{direction.upper()}" to place remaining sell stops: ') == direction.upper():
                symbol_ticks = mt5.symbol_info_tick(bot.symbol)
                # Using initial directional bias
                if direction == "sell": # initial = buy
                    if symbol_ticks.ask < initial_entry:
                        bot.logger.error("Market price still below %s", initial_entry)
                        continue
                else:
                    if symbol_ticks.bid > initial_entry: # Initial = sell
                        bot.logger.error("Market price still above %s", initial_entry)
                        continue
                for i in range(1, orders):
                    lots = lot_size
                    if i in [2, 5, 9]:
                        lots = lot_size * 2
                    entry = initial_entry - (i * spacing_pips * dir_multiplier)
                    tp = tp_pips - (i * orders)
                    sl = sl_pips
                
                    order(
                        symbol=bot.symbol,
                        order_type=f"{direction} stop",
                        start_price=entry,
                        spacing_pips=0,
                        num_orders=1,
                        volume_per_order=lots,
                        stop_loss_pips=sl,
                        take_profit_pips=tp
                    )
                pending = False
    else:
        return positions

def doublebanger(bot, inputs, confirm=True, autocomplete=False):
    symbol_ticks = bot.get_symbol_ticks()
    spread = symbol_ticks.ask - symbol_ticks.bid
    inputs["spread"] = spread
    bot.logger.info("Current spread is %s", spread)

    if inputs["entry"] > symbol_ticks.ask:
        bot.logger.info("Entry is above market price")
        if confirm:
            if input('Type "BUY" to continue: ') != "BUY":
                bot.logger.error("Aborting operation")
                return
        positions =  doublebanger_orders(bot, inputs, "buy", autocomplete)
    elif inputs["entry"] < symbol_ticks.bid:
        bot.logger.info("Entry is below market price")
        if confirm:
            if input('Type "SELL" to continue: ') != "SELL":
                bot.logger.error("Aborting operation")
                return
        positions = doublebanger_orders(bot, inputs, "sell", autocomplete)

    if autocomplete:
        return positions
    

def doublebanger1(bot, inputs, confirm=True, autocomplete=False):
    symbol_ticks = bot.get_symbol_ticks()
    spread = symbol_ticks.ask - symbol_ticks.bid
    bot.logger.info("Current spread is %s", spread)

    if inputs["entry"] > symbol_ticks.ask:
        bot.logger.info("Entry is above market price")
        if confirm:
            if input('Type "BUY" to continue: ') != "BUY":
                bot.logger.error("Aborting operation")
                return
        order(
            symbol=bot.symbol,
            order_type="buy stop",
            start_price=inputs["entry"],
            spacing_pips=inputs["spacing_pips"],
            num_orders=inputs["num_orders"],
            volume_per_order=inputs["lot_size"] ,
            stop_loss_pips=inputs["sl pips"],
            take_profit_pips=inputs["tp pips"]
        )
        # Only need to track this, for following orders
        pending_order = order(
            symbol=bot.symbol,
            order_type="sell limit",
            start_price=inputs["entry"],
            spacing_pips=inputs["spacing_pips"],
            num_orders=1,
            volume_per_order=inputs["lot_size"] ,
            stop_loss_pips=inputs["sl pips"],
            take_profit_pips=inputs["tp pips"]
        )
        
        if not autocomplete:
            pending = True
            inputs["entry"] -= 10 * bot.pip_value
            while pending:
                if input('Enter "SELL" to place remaining sell stops: ') == "SELL":
                    symbol_ticks = mt5.symbol_info_tick(bot.symbol)
                    if symbol_ticks.ask < inputs["entry"]:
                        bot.logger.error("Market price still below %s", inputs["entry"])
                        continue
                    order(
                        symbol=bot.symbol,
                        order_type="sell stop",
                        start_price=inputs["entry"],
                        spacing_pips=inputs["spacing_pips"],
                        num_orders=inputs["num_orders"] - 1,
                        volume_per_order=inputs["lot_size"] ,
                        stop_loss_pips=inputs["sl pips"],
                        take_profit_pips=inputs["tp pips"]
                    )
                    pending = False
    elif inputs["entry"] < symbol_ticks.bid:
        bot.logger.info("Entry is below market price")
        if confirm:
            if input('Type "SELL" to continue: ') != "SELL":
                bot.logger.error("Aborting operation")
                return
        order(
            symbol=bot.symbol,
            order_type="sell stop",
            start_price=inputs["entry"],
            spacing_pips=inputs["spacing_pips"],
            num_orders=inputs["num_orders"],
            volume_per_order=inputs["lot_size"] ,
            stop_loss_pips=inputs["sl pips"],
            take_profit_pips=inputs["tp pips"]
        )
        pending_order = order(
            symbol=bot.symbol,
            order_type="buy limit",
            start_price=inputs["entry"],
            spacing_pips=inputs["spacing_pips"],
            num_orders=1,
            volume_per_order=inputs["lot_size"] ,
            stop_loss_pips=inputs["sl pips"],
            take_profit_pips=inputs["tp pips"]
        )
        
        if not autocomplete:
            pending = True
            inputs["entry"] += 10 * bot.pip_value
            while pending:
                if input('Enter "BUY" to place remaining buy stops: ') == "BUY":
                    symbol_ticks = mt5.symbol_info_tick(bot.symbol)
                    if symbol_ticks is None:
                        print(f"Error for {bot.symbol}: {mt5.last_error()}")
                        return None
                    if symbol_ticks.bid > inputs["entry"]:
                        bot.logger.error("Market price still above %s", inputs["entry"])
                        continue
                    order(
                        symbol=bot.symbol,
                        order_type="buy stop",
                        start_price=inputs["entry"],
                        spacing_pips=inputs["spacing_pips"],
                        num_orders=inputs["num_orders"] - 1,
                        volume_per_order=inputs["lot_size"] ,
                        stop_loss_pips=inputs["sl pips"],
                        take_profit_pips=inputs["tp pips"]
                    )
                    pending = False

    if autocomplete:
        return pending_order
    
history_query = """
        INSERT INTO deal_history (deal_id, symbol, deal_type, volume, price, profit)
        VALUES (%s, %s, %s, %s, %s, %s)
        """

def write_db(query, args, message="Successful db write"):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        try:
            cursor.execute(query, args)
            conn.commit()
            logging.info(message)
        except sqlite3.Error as err:
            logging.error(f"DB error: {err}")
        finally:
            cursor.close()
            conn.close()

def save_new_position(ticket_id, symbol, side, volume, price, status="PENDING", strategy_id=1):
        args = (ticket_id, symbol, side, volume, price, status, strategy_id)
        query = f"""
        INSERT INTO positions (
            ticket_id,
            symbol,
            side,
            volume,
            entry_price,
            status,
            strategy_id
            )

        VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        message = f"symbol: Position {ticket_id} [{side}] saved to DB."
        
        write_db(query, args, message)

def read_db(query):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    if conn:
        cursor = conn.cursor()
        cursor.execute(query)
        records = cursor.fetchall()
        cursor.close()
        conn.close()
        return records
    return []

def get_open_positions():
    query = "SELECT * FROM positions WHERE status = 'OPEN'"
    return read_db(query)

def get_pending_orders():
    query = "SELECT * FROM positions WHERE status = 'PENDING'"
    return read_db(query)

def get_deals(ticket_id=676081850):
    if not mt5.initialize(login=LOGIN, password=PASSWORD, server=SERVER):
        logging.error(f"Initialization failed: {mt5.last_error()}")
        logging.info(f"{LOGIN=} {PASSWORD=} {SERVER=}")

    from datetime import datetime, timedelta

    yesterday = datetime.now() - timedelta(days=1)
    yesterday = datetime(2026, 1, 1)
    today = datetime.now()

    orders = get_pending_orders()
    orders = [order for order in orders if order["ticket_id"] == ticket_id ]
    deals = mt5.history_deals_get(yesterday, today)
    position_id = [d for d in deals if d.order == ticket_id][0].position_id
    deals = [d for d in deals if d.position_id == position_id]


    mt5.shutdown()
    return deals

def save_new_deal(symbol, deal):
    query ="""
    INSERT INTO deals
    (ticket_id, symbol, side, volume, price, commission, swap, profit)
    VALUES
    (?, ?, ?, ?, ?, ?, ?, ?)
    """
    args = (deal.order, symbol, deal.type, deal.volume, deal.price, deal.commission, deal.swap, deal.profit)
    message = f"symbol: Deal {deal.order} [{deal.type}] saved to DB."

    write_db(query, args, message)

# print(get_deals(665015112))