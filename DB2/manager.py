import argparse
import datetime
import subprocess
import sys
import os
import time
import logging
import dotenv

from doublebanger import main as launcher


if sys.platform == "linux":
    from mt5linux import MetaTrader5

    mt5 = MetaTrader5()
    islinux = True
elif sys.platform == "win32":
    import MetaTrader5 as mt5

    islinux = False
else:
    raise RuntimeError(f"Unknown platform {sys.platform}. Must be 'win32' or 'linux'")

dotenv.load_dotenv()
LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]

def initialize_mt5(symbol, logger):
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

def get_excluded(args, logger):
    if not args.exclude:
        return None
    numbers = args.exclude.split(",")
    try:
        numbers = [int(n.strip()) for n in numbers]
        return numbers
    except Exception as e:
        logger.error(f"Failed to extract excluded magic numbers: {e}")
        return None

def positions_exist(symbol, excluded_magic):
    # return True if in trade else False
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return False

    for m in excluded_magic:
        positions = [p for p in positions if p.magic != m]

    if positions:
        return True
    return False

def orders_exist(symbol, excluded_magic):
    # return True if in trade else False
    orders = mt5.orders_get(symbol=symbol)
    if orders is None:
        return False

    for m in excluded_magic:
        orders = [o for o in orders if o.magic != m]

    if orders:
        return True
    return False

def check_trades(symbol, logger, excluded_magic:list[int]=None) -> bool:
    # return True if in trade else False
    if not mt5.initialize():
        initialize_mt5(symbol, logger)

    if excluded_magic is None:
        excluded_magic = [0]
    else:
        excluded_magic = [0] + excluded_magic

    positions = positions_exist(symbol, excluded_magic)
    orders = orders_exist(symbol, excluded_magic)

    logger.info(f"{positions=}, {orders=}, {excluded_magic=}")

    return positions or orders


def start_trade(symbol, args, logger):
    if not mt5.initialize():
        initialize_mt5(symbol, logger)

    # launcher(["dailybanger", symbol, "--target", str(args.tp_pips),
    #     "--log-level", args.log_level])
    launch_in_new_window(symbol, args.tp_pips, args.log_level, logger)

def launch_in_new_window(symbol, tp_pips, log_level, logger, system=None):
    try:
        subprocess.Popen(
            [
                sys.executable,  # Path to current Python interpreter
                "doublebanger.py",  # Script to run
                "dailybanger",
                symbol,
                "--target", str(tp_pips),
                "--log-level", log_level
            ] + ([system] if system is not None else []),
            # creationflags=subprocess.CREATE_NEW_CONSOLE  # Open in new console window
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Launched dailybanger.py in a new console window.")
    except Exception as e:
        logger.info(f"Error launching script: {e}")

def main():

    desc = """MT5 Auto trade script.
    The script monitors active trades at a set interval and enters a DB
    using defined parameters. A trade only activates if ther is no active trade
    at the time of check.
    """
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument("symbol", help="Symbol/Asset to be traded")
    parser.add_argument("--interval", default=60, type=int, help="Monitoring interval in seconds")
    parser.add_argument("--tp-pips",default=50, type=int, help="SL and TP in pips (Same for both)")
    parser.add_argument("--time", help="start time with window of 5mins [hh:mm]")
    parser.add_argument("--log-level", default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Logging level")
    parser.add_argument("--exclude", help="Comma serparated string of excluded magic numbers. These are treated as non-existent")
    parser.add_argument("--preload", action="store_true",
                       help="Load module and await start command for speed up")
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

    args = parser.parse_args()
    symbol = args.symbol #.upper()
    interval = int(args.interval)

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.DEBUG,
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logger = logging.getLogger()
    logger.setLevel(args.log_level.upper())

    logger.info(f"Initializing terminal for {symbol}...")
    initialize_mt5(symbol, logger)
    logger.info("\tParsing excluded magic numbers...")
    excluded = get_excluded(args, logger)
    logger.info(f"\tExcluding all of {excluded}.")
    logger.info("Initialization successful.")

    if args.preload:
        start = False
        while not start:
            start = input("Do you want to begin now? [y/n] ").lower() == "y"
            time.sleep(1)

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
                    time.sleep(1)
        except Exception as e:
            raise Exception(f"Data error: {e}")

    logger.info(f"Starting process for {symbol}")

    live = True
    while live:
        # Check for trades: True if in trade else False
        try:
            if not check_trades(symbol, logger, excluded):
                start_trade(args.symbol, args, logger)
            else:
                logger.info(f"Positions exists for {symbol}, sleeping for {interval} seconds...")
        except Exception as e:
            logger.error(f"Unhandled Exception: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
