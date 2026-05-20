import argparse
import datetime
import logging
import os
import sys
import time
from typing import Optional, Dict, Any, List, Tuple, Callable, Union
from functools import wraps, lru_cache

# import pandas as pd
from dotenv import load_dotenv

from trident import TradingBot
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


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s.%(msecs)03d] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


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
                    
                    time.sleep(current_delay)
                    current_delay *= backoff
            return None
        return wrapper
    return decorator



def str_to_time(time_str):
    h, m, *s = (int(t) for t in time_str.split(":"))
    s = int(s[0]) if len(s) > 0 else 0
    now = datetime.datetime.now()
    target_time = now.replace(hour=h, minute=m, second=s, microsecond=0)

    if target_time < now:
        target_time = target_time + datetime.timedelta(hours=24)

    return target_time

def check_time_sleep(target_time:time):
    try:
        now = datetime.datetime.now()
        end_time = target_time + datetime.timedelta(
            minutes=10, seconds=59, microseconds=99
        )
        if end_time < now:
            target_time = target_time + datetime.timedelta(hours=24)
            end_time = end_time + datetime.timedelta(hours=24)
        is_entry = False
        while not is_entry:
            now = datetime.datetime.now()
            # logging.info(f"GTE start: {target_time <= now}")
            # logging.info(f"LTE end: {now <= end_time}")
            # logging.info(f"entry: {target_time <= now <= end_time}")
            if target_time <= now <= end_time:
                is_entry = True
                continue
            logging.info(f"Time not up to {target_time}...")
            if (target_time - now).seconds >= 65 * 60:
                logging.info("Sleeping for 1 hour")
                time.sleep(60 * 60)
            elif (target_time - now).seconds >= 30 * 60:
                logging.info("Sleeping for 10 minutes")
                time.sleep(10 * 60)
            elif (target_time - now).seconds >= 10 * 60:
                logging.info("Sleeping for 5 minutes")
                time.sleep(5 * 60)
            elif (target_time - now).seconds >= 3 * 60:
                logging.info("Sleeping for 1 minute")
                time.sleep(1 * 60)
            else:
                time.sleep(10)
    except Exception as e:
        raise Exception(f"Data error: {e}")
    
@retry(max_attempts=3, delay=1.0, exceptions=(Exception,), logger=logging)
def execute_entry(exec_targets, i):
    exec_times = list(exec_targets.keys())
    key = exec_times[i]
    entry_time = str_to_time(key)
    start = str_to_time(exec_targets[key]) + datetime.timedelta(hours=1) # Get the 5:00 candle, for some reason have to +1
    end = start + datetime.timedelta(minutes=10)

    # Get entries 10 min before time
    check_time_sleep(entry_time - datetime.timedelta(minutes=10))
    candle = mt5.copy_rates_range(bot.symbol, mt5.TIMEFRAME_M15, start, end)[0] # First
    entries = [candle["open"], candle["close"]]
    logging.info(f"Entry: {entries}")

    check_time_sleep(entry_time)
    bot.daily_banger(args, entries=entries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="automate trades")
    # parser.add_argument("action", help="Market action")
    parser.add_argument("symbol", help="Symbol to be traded")
    parser.add_argument(
        "--comment",
        help="Comment for trade deals",
        default=datetime.datetime.now().strftime("%H:%M:%S"),
    )
    # parser.add_argument("--time", help="start time with window of 5mins [hh:mm]")
    args = parser.parse_args()

    if not mt5.initialize():
        err = mt5.last_error()
        logging.error(f"MT5 terminal initialization failed: {err}")
        raise ConnectionError(f"Could not connect to MT5 terminal: {err}")
    # 3. Secure Login (Ensure LOGIN/PASSWORD/SERVER are defined elsewhere)
    if not mt5.login(login=LOGIN, password=PASSWORD, server=SERVER):
        err = mt5.last_error()
        logging.error(f"Login failed for account {LOGIN}: {err}")
        mt5.shutdown()
        raise PermissionError(f"MT5 login failed: {err}")

    bot = TradingBot(args.symbol)

    # Execution time : target entry
    exec_targets = {
        "02:00": "1:00",
        "08:00": "4:00",
    }

    # Rewrite entry times as local time
    # Server is +2 of local
    exec_targets = {
        "00:00": "1:00",
        "06:00": "4:00",
    }
    exec_times = list(exec_targets.keys())

    # At 2:00, place first db at day 15m open/close
    # key = exec_times[0]
    # entry_time = str_to_time(key)
    # start = str_to_time(exec_targets[key]) + datetime.timedelta(hours=1) # Get the 1 am candle, for some reason have to +1
    # end = start + datetime.timedelta(minutes=10)

    # # Get entries 10 min before time
    # check_time_sleep(entry_time - datetime.timedelta(minutes=10))
    # candle = mt5.copy_rates_range(bot.symbol, mt5.TIMEFRAME_M15, start, end)[0] # First
    # entries = [candle["open"], candle["close"]]

    # check_time_sleep(entry_time)
    # bot.daily_banger(args, entries=entries)

    execute_entry(exec_targets, 0)

    # =========================================================
    # at 8:00, second db at 4:00 close (consider 5:00 open) 15m
    # =========================================================
    key = exec_times[1]
    entry_time = str_to_time(key)

    # Get entries 10 min before time
    check_time_sleep(entry_time - datetime.timedelta(minutes=10))
    start = str_to_time(exec_targets[key]) + datetime.timedelta(hours=1) # Get the 5:00 candle, for some reason have to +1
    end = start + datetime.timedelta(minutes=10)
    candle = mt5.copy_rates_range(bot.symbol, mt5.TIMEFRAME_M15, start, end)[0] # First
    inputs = {
        "entry": candle["close"],
        "sl pips": 70,
        "tp pips": 70,
        "num_orders": 5,
        "spacing_pips": 10,
        "lot_size": 0.01,
    }

    logging.info(f"Entry: {inputs['entry']}")

    check_time_sleep(entry_time)
    bot.doublebanger(args, inputs=inputs)


# TODO: Make individual order functions that call common dep functions
# set buystop ger40
