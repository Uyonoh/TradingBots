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
import logging
import logging.handlers
from typing import Optional, Dict, Any, List, Tuple

dotenv.load_dotenv()

# -------------------------------------------------------------------
# LOGGING CONFIGURATION
# -------------------------------------------------------------------
class TradingLogger:
    """Centralized logging configuration for trading system."""
    
    @staticmethod
    def setup_logging(symbol: str, log_level: str = "INFO") -> logging.Logger:
        """
        Configure structured logging with console and file handlers.
        
        Args:
            symbol: Trading symbol for log file naming
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        
        Returns:
            Configured logger instance
        """
        # Create logs directory if it doesn't exist
        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # Create logger
        logger = logging.getLogger("trading_bot")
        logger.setLevel(getattr(logging, log_level.upper()))
        
        # Remove existing handlers to avoid duplicates
        logger.handlers.clear()
        
        # Log format
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
        log_file = os.path.join(log_dir, f"trading_{symbol}_{datetime.now().strftime('%Y%m%d')}.log")
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10 MB
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)  # File gets INFO and above
        logger.addHandler(file_handler)
        
        # Separate error file handler
        error_file = os.path.join(log_dir, f"errors_{symbol}_{datetime.now().strftime('%Y%m%d')}.log")
        error_handler = logging.handlers.RotatingFileHandler(
            error_file,
            maxBytes=5*1024*1024,  # 5 MB
            backupCount=10
        )
        error_handler.setFormatter(formatter)
        error_handler.setLevel(logging.WARNING)  # Error file gets WARNING and above
        logger.addHandler(error_handler)
        
        return logger

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------
LOGIN = int(os.environ["ACCOUNT_ID"])
PASSWORD = os.environ["PASSWORD"]
SERVER = os.environ["SERVER"]
MAX_RETRIES = 5
TIMEOUT = 1

# Configuration - consolidated and cleaned
CONFIG = {
    'bias_filter': {
        'buy_threshold': 0.6, 
        'sell_threshold': 0.4
    }, 
    'entry_conditions': {
        '15min_buffer': 10, 
        'velocity_multiplier': 2, 
        'lookback_period': 60*60
    }, 
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
        'start_hour': 10, 
        'start_minute': 0,
        'end_hour': 17, 
        'end_minute': 0,
        'ghost_start': time(8, 0),
        'ghost_end': time(8, 30)
    },
    'logging': {
        'level': 'INFO',
        'enable_file_logging': True
    }
}

# Trading parameters
VOLUME = 0.01
DEVIATION = 10
MAGIC_NUM = 1234569

# Timezones
CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc
UTC2 = pytz.timezone('Europe/Athens')  # EET / CAT
UTC3 = pytz.timezone('Asia/Baghdad')   # EAT / MST

# Global logger instance (will be initialized in main)
logger = None

# -------------------------------------------------------------------
# HELPER CLASSES
# -------------------------------------------------------------------

class VelocityMonitor:
    """
    Live implementation of the density/velocity logic.
    Uses a deque to track tick timestamps efficiently.
    """
    def __init__(self, symbol: str, lookback_seconds: int = 60):
        self.symbol = symbol
        self.tick_timestamps = deque()
        self.density_history = deque(maxlen=lookback_seconds)
        self.last_update = t_mod.time()
        self.logger = logging.getLogger("trading_bot.velocity")

    def get_server_timestamp(self) -> Optional[float]:
        """Get current server timestamp with retry logic."""
        for attempt in range(MAX_RETRIES + 1):
            tick = mt5.symbol_info_tick(self.symbol)
            
            if tick is not None:
                return tick.time_msc / 1000  # Convert to seconds
                
            if attempt < MAX_RETRIES:
                self.logger.debug(f"Retry {attempt + 1}/{MAX_RETRIES} for {self.symbol}...")
                t_mod.sleep(0.1 * (attempt + 1))
            else:
                self.logger.error(f"Failed to get tick for {self.symbol} after {MAX_RETRIES} retries.")
                
        return None

    def get_ticks(self, server_time: Optional[float] = None) -> np.ndarray:
        """Fetch ticks from MT5."""
        if server_time is None:
            server_time = self.get_server_timestamp()
        
        ticks = mt5.copy_ticks_from(self.symbol, server_time, 10000, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            return np.array([])
        return ticks

    def get_tick_timestamps(self, server_time: Optional[float] = None) -> List[float]:
        """Extract timestamps from ticks."""
        ticks = self.get_ticks(server_time)
        if len(ticks) == 0:
            return []
        return [t["time_msc"] / 1000 for t in ticks]

    def on_tick(self) -> None:
        """Process new ticks and update timestamp deque."""
        now = self.get_server_timestamp()
        if now is None:
            return
        
        if len(self.tick_timestamps) == 0:
            timestamps = self.get_tick_timestamps(now)
        else:
            last_timestamp = self.tick_timestamps[-1]
            timestamps = self.get_tick_timestamps(last_timestamp)
        
        if timestamps:
            self.tick_timestamps.extend(timestamps)
            self.cleanup(now)

    def cleanup(self, now: float) -> None:
        """Remove ticks older than 30 seconds."""
        cutoff = now - 30
        while self.tick_timestamps and self.tick_timestamps[0] < cutoff:
            self.tick_timestamps.popleft()

    def update_history(self) -> None:
        """Update density history once per second."""
        now = self.get_server_timestamp()
        if now is None:
            return
            
        self.cleanup(now)
        current_density = len(self.tick_timestamps) / 30.0
        self.density_history.append(current_density)
        self.last_update = t_mod.time()

    def is_high_velocity(self, multiplier: float) -> bool:
        """Check if current velocity exceeds historical average by multiplier."""
        if len(self.density_history) < 10:
            return False
        if t_mod.time() - self.last_update < 60 * 10:
            return False
        
        current_density = len(self.tick_timestamps) / 30.0
        avg_density = sum(self.density_history) / len(self.density_history)
        
        if avg_density == 0:
            return False
        
        is_high = current_density > (avg_density * multiplier)
        if is_high:
            self.logger.debug(
                f"High velocity detected: {current_density:.2f} > {avg_density:.2f} * {multiplier}"
            )
        return is_high


class StrategyState:
    """Keeps track of daily state to survive loop cycles."""
    def __init__(self, symbol: str):
        self.symbol = symbol
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
        self.direction = None  # 'buy' or 'sell'
        self.logger = logging.getLogger("trading_bot.state")

    def reset(self, new_date: date) -> None:
        """Reset state for a new trading day."""
        self.logger.info(f"--- NEW DAY: {new_date} ---")
        self.current_date = new_date
        self.bias = "straddle"
        self.ghost_high = None
        self.ghost_low = None
        self.daily_open_price = None
        self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0
        self.logger.debug(f"State reset for {new_date}")

    def close_trade(self) -> None:
        """Clean up trade state after position closure."""
        self.logger.info(f"Trade closed. Bias: {self.bias}")
        self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0
        self.entry_price = 0.0
        self.direction = None

    def update_trade_status(self, has_position: bool) -> None:
        """Update trade status based on current positions."""
        if self.in_trade and not has_position:
            self.close_trade()
        elif not self.in_trade and has_position:
            self.in_trade = True
            self.logger.info(f"Trade status updated: in_trade={self.in_trade}")

# -------------------------------------------------------------------
# CORE LOGIC - with logging improvements
# -------------------------------------------------------------------

def get_server_timezone(year: Optional[int] = None, 
                        month: Optional[int] = None, 
                        day: Optional[int] = None) -> pytz.tzinfo:
    """
    Returns the current server timezone based on:
    Winter: GMT+2 (Europe/Athens)
    Summer: GMT+3 (Asia/Baghdad)
    """
    logger = logging.getLogger("trading_bot.time")
    
    if year is None or month is None or day is None:
        now_utc = datetime.now(timezone.utc)
        year = now_utc.year
        month = now_utc.month
        day = now_utc.day

    now_utc = datetime(year, month, day, tzinfo=timezone.utc)

    # Helper to find the last Sunday of a given month
    def last_sunday(year: int, month: int) -> datetime:
        last_day = calendar.monthrange(year, month)[1]
        dt = datetime(year, month, last_day, tzinfo=timezone.utc)
        offset = (dt.weekday() + 1) % 7  # Weekday 6 is Sunday
        return dt - timedelta(days=offset)

    # DST boundaries
    dst_start = last_sunday(year, 3).replace(hour=1)
    dst_end = last_sunday(year, 10).replace(hour=1)

    # Determine offset
    if dst_start <= now_utc < dst_end:
        zone = UTC3
        logger.debug(f"Using summer timezone: {zone}")
    else:
        zone = UTC2
        logger.debug(f"Using winter timezone: {zone}")

    return zone


def get_server_time(symbol: str) -> Optional[datetime]:
    """Get MT5 server time with error handling."""
    logger = logging.getLogger("trading_bot.time")
    
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        logger.warning(f"Failed to get tick for server time: {symbol}")
        return None
    
    try:
        server_time = pd.to_datetime(tick.time, unit='s')
        zone = get_server_timezone()
        server_time = zone.localize(server_time)
        return server_time
    except Exception as e:
        logger.error(f"Error converting server time: {e}")
        return None


def get_server_time_cet(symbol: str) -> Optional[datetime]:
    """Gets MT5 server time and converts to CET."""
    logger = logging.getLogger("trading_bot.time")
    
    server_time = get_server_time(symbol)
    if server_time is None:
        return None
    
    cet_time = server_time.astimezone(CET)
    logger.debug(f"Time conversion: Server={server_time} -> CET={cet_time}")
    return cet_time


def calculate_daily_bias(symbol: str) -> str:
    """
    Calculates bias based on YESTERDAY'S D1 Candle.
    (c - l) / (h - l)
    """
    logger = logging.getLogger("trading_bot.bias")
    
    # Get 2 days of D1 data to ensure we have yesterday completed
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, 1)
    if rates is None or len(rates) == 0:
        logger.error("Error fetching D1 data for bias calculation")
        return "straddle"

    r = rates[0]
    high, low, close = r['high'], r['low'], r['close']
    
    if high == low:
        logger.warning(f"D1 candle has high=low for {symbol}")
        return "straddle"
    
    rc = (close - low) / (high - low)
    
    bias = "straddle"
    if rc >= CONFIG['bias_filter']['buy_threshold']:
        bias = "buy"
    elif rc <= CONFIG['bias_filter']['sell_threshold']:
        bias = "sell"
    
    logger.info(f"Daily bias calculated: {bias} (rc={rc:.3f}, thresholds={CONFIG['bias_filter']})")
    return bias


def touched_opposite(symbol: str, bias: str, target: float) -> bool:
    """Check if price has touched opposite side of ghost range."""
    logger = logging.getLogger("trading_bot.opposite_check")
    
    today = datetime.now()
    start_dt = datetime.combine(today, CONFIG['session']['day_open'])
    end_dt = today

    server_zone = get_server_timezone()
    # Convert to server timezone
    start_dt = server_zone.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(server_zone)
    end_dt = server_zone.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(server_zone)

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        logger.warning("Error fetching M1 data for opposite confirmation")
        return False

    for r in rates:
        high, low, close = r['high'], r['low'], r['close']

        if bias == "buy" and low <= target:
            logger.info(f"Touched opposite (buy bias): low={low:.5f} <= target={target:.5f}")
            return True
        elif bias == "sell" and high >= target:
            logger.info(f"Touched opposite (sell bias): high={high:.5f} >= target={target:.5f}")
            return True
    
    return False


def get_ghost_range(symbol: str, today_date: date) -> Tuple[Optional[float], Optional[float]]:
    """
    Fetches M1 bars from CET to determine ghost range.
    """
    logger = logging.getLogger("trading_bot.ghost_range")
    
    # Construct CET times
    start_dt = datetime.combine(today_date, CONFIG['session']['ghost_start'])
    end_dt = datetime.combine(today_date, CONFIG['session']['ghost_end'])
    
    # Convert to server timezone
    server_zone = get_server_timezone()
    start_dt = server_zone.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(server_zone)
    end_dt = server_zone.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(server_zone)

    logger.debug(f"Fetching ghost range: {start_dt} to {end_dt}")
    
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        logger.warning(f"No data returned for ghost range: {symbol}")
        return None, None

    # Calculate Min/Max from the bars
    g_min = float(np.min(rates['low']))
    g_max = float(np.max(rates['high']))
    
    logger.info(f"Ghost range calculated: {g_min:.5f} - {g_max:.5f}")
    return g_min, g_max


def get_frankfurt_open(symbol: str, today_date: date) -> Optional[float]:
    """Fetch Frankfurt open price."""
    logger = logging.getLogger("trading_bot.open_price")
    
    start_dt = datetime.combine(today_date, CONFIG['session']['day_open'])
    
    # Convert to server timezone
    server_zone = get_server_timezone()
    start_dt = server_zone.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(server_zone)

    logger.debug(f"Fetching Frankfurt open at {start_dt}")
    
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, start_dt)
    if rates is None or len(rates) == 0:
        logger.warning(f"No data returned for Frankfurt open: {symbol}")
        return None
    
    open_price = float(rates[0][1])
    logger.info(f"Frankfurt open price: {open_price:.5f}")
    return open_price


def get_filling_type(symbol: str) -> Optional[int]:
    """Determine order filling type based on symbol info."""
    logger = logging.getLogger("trading_bot.order")
    
    info = mt5.symbol_info(symbol)
    if info is None:
        logger.error(f"Cannot get symbol info for: {symbol}")
        return None
    
    # Check bitmask for allowed modes
    if info.filling_mode & 1:
        filling = mt5.ORDER_FILLING_FOK
        logger.debug(f"Filling type: FOK for {symbol}")
    elif info.filling_mode & 2:
        filling = mt5.ORDER_FILLING_IOC
        logger.debug(f"Filling type: IOC for {symbol}")
    else:
        filling = mt5.ORDER_FILLING_RETURN
        logger.debug(f"Filling type: RETURN for {symbol}")
    
    return filling


def execute_trade(symbol: str, contract_size: float, direction: str, sl_pips: float) -> Tuple[bool, float]:
    """Send order to MT5 with comprehensive logging."""
    logger = logging.getLogger("trading_bot.order")
    
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        logger.error(f"Cannot get tick for {symbol}")
        return False, 0.0
    
    filling = get_filling_type(symbol)
    if filling is None:
        return False, 0.0
    
    sl_points = sl_pips / contract_size
    
    # Prepare order request
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": VOLUME,
        "type": mt5.ORDER_TYPE_BUY if direction == 'buy' else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if direction == 'buy' else tick.bid,
        "sl": (tick.ask - sl_points) if direction == 'buy' else (tick.bid + sl_points),
        "deviation": DEVIATION,
        "magic": MAGIC_NUM,
        "comment": "LiveDemo_Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    
    logger.info(f"Sending {direction} order: price={request['price']:.5f}, sl={request['sl']:.5f}")
    
    result = mt5.order_send(request)
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"Order failed: {result.comment} (retcode: {result.retcode})")
        logger.debug(f"Request details: {request}")
        return False, 0.0
    
    logger.info(f"Trade executed: {direction} at {result.price:.5f}, ticket: {result.order}")
    return True, result.price


def close_position(symbol: str) -> None:
    """Close all positions with our Magic Number."""
    logger = logging.getLogger("trading_bot.order")
    
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        logger.warning(f"No positions found for {symbol}")
        return
    
    for pos in positions:
        if pos.magic == MAGIC_NUM:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.error(f"Cannot get tick for closing position {pos.ticket}")
                continue
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "price": tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask,
                "deviation": DEVIATION,
                "magic": MAGIC_NUM,
                "comment": "Mandatory Close"
            }
            
            logger.info(f"Closing position {pos.ticket} ({'BUY' if pos.type == mt5.ORDER_TYPE_BUY else 'SELL'})")
            result = mt5.order_send(request)
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(f"Position close failed: {result.comment}")
            else:
                logger.info(f"Position {pos.ticket} closed at {result.price:.5f}")


def modify_sl(symbol: str, ticket: int, new_sl: float) -> bool:
    """Modify stop loss for existing position."""
    logger = logging.getLogger("trading_bot.order")
    
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": new_sl,
        "magic": MAGIC_NUM
    }
    
    logger.debug(f"Modifying SL for ticket {ticket}: new_sl={new_sl:.5f}")
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"SL modification failed: {result.comment}")
        return False
    
    logger.info(f"SL modified for ticket {ticket}: {new_sl:.5f}")
    return True


# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def main():
    global logger
    
    parser = argparse.ArgumentParser(description="Live trading momentum based bot for HFM")
    parser.add_argument("symbol", help="symbol to be traded")
    parser.add_argument("--log-level", default="INFO", 
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Logging level (default: INFO)")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    
    # Initialize logger
    logger = TradingLogger.setup_logging(symbol, args.log_level)
    logger.info(f"Starting trading bot for symbol: {symbol}")
    logger.info(f"Log level: {args.log_level}")
    logger.info(f"Configuration loaded: {CONFIG}")

    # Initialize MT5 connection
    if not mt5.initialize():
        err = mt5.last_error()
        logger.error(f"MT5 terminal initialization failed: {err}")
        raise ConnectionError(f"Could not connect to MT5 terminal: {err}")
    
    logger.info("MT5 terminal initialized successfully")
    
    if not mt5.login(login=LOGIN, password=PASSWORD, server=SERVER):
        err = mt5.last_error()
        logger.error(f"Login failed for account {LOGIN}: {err}")
        mt5.shutdown()
        raise PermissionError(f"MT5 login failed: {err}")
    
    logger.info(f"Logged in to MT5 account: {LOGIN}")

    # Check symbol
    if not mt5.symbol_select(symbol, True):
        logger.error(f"Symbol {symbol} not found or cannot be selected")
        mt5.shutdown()
        return

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        logger.error(f"Cannot get symbol info for {symbol}")
        mt5.shutdown()
        return
    
    contract_size = symbol_info.trade_contract_size
    logger.info(f"Symbol {symbol} selected, contract size: {contract_size}")

    # Initialize state and monitoring
    state = StrategyState(symbol)
    velocity = VelocityMonitor(symbol, lookback_seconds=CONFIG['entry_conditions']['lookback_period'])
    
    logger.info("Live trading started")
    logger.info(f"Session hours: {CONFIG['session']['start_hour']:02d}:{CONFIG['session']['start_minute']:02d} - "
                f"{CONFIG['session']['end_hour']:02d}:{CONFIG['session']['end_minute']:02d}")
    
    last_update_seconds = t_mod.time()

    try:
        while True:
            # Hardware Efficiency: Sleep to reduce CPU usage
            t_mod.sleep(0.1)
            
            # Update Time
            now = get_server_time(symbol)
            if now is None:
                logger.warning("Could not get server time, skipping iteration")
                continue
                
            now_cet = get_server_time_cet(symbol)
            if now_cet is None:
                logger.warning("Could not convert to CET time, skipping iteration")
                continue
                
            today_date = now.date()
            
            # 3. New Day Logic
            if state.current_date != today_date:
                state.reset(today_date)
                state.bias = calculate_daily_bias(symbol)
                logger.info(f"Daily bias set to: {state.bias}")

            # 4. Data Processing (Tick)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.debug(f"No tick data for {symbol}")
                continue
            
            velocity.on_tick()
            
            # Update Velocity History every 1 second
            if t_mod.time() - last_update_seconds >= 1.0:
                if len(velocity.density_history) > 0:
                    avg_vel = sum(velocity.density_history) / len(velocity.density_history)
                    logger.debug(f"Tick velocity: current={len(velocity.tick_timestamps)/30.0:.2f}, avg={avg_vel:.2f}")
                
                velocity.update_history()
                last_update_seconds = t_mod.time()

                # Log velocity every 5 minutes
                if datetime.now().time().minute % 5 == 0 and datetime.now().time().second == 0:
                    if len(velocity.density_history) > 0:
                        logger.info(f"Velocity snapshot: {velocity.density_history[-1]:.2f}")

            # 5. Logic Gates
            
            # A. Capture Ghost Range (Runs once after 08:30)
            if state.ghost_high is None:
                if now_cet.time() > CONFIG['session']['ghost_end']:
                    g_min, g_max = get_ghost_range(symbol, today_date)
                    if g_min is not None and g_max is not None:
                        state.ghost_low = g_min
                        state.ghost_high = g_max
                        logger.info(f"Ghost range locked: {g_min:.5f} - {g_max:.5f}")
                    else:
                        logger.warning("Waiting for ghost data...")
                        t_mod.sleep(5)
                        continue

            # B. Capture Daily Open (Frankfurt 09:00)
            if state.daily_open_price is None:
                target_open = CONFIG['session']['day_open']
                if now_cet.time() >= target_open:
                    open_price = get_frankfurt_open(symbol, today_date)
                    if open_price is not None:
                        state.daily_open_price = open_price
                        logger.info(f"Market open price recorded: {open_price:.5f}")
                    else:
                        logger.warning("Could not get Frankfurt open price")

            # C. Check for existing positions
            positions = mt5.positions_get(symbol=symbol)
            if positions is None:
                positions = []
            
            my_pos = [p for p in positions if p.magic == MAGIC_NUM]
            open_pos = len(my_pos) > 0

            # Update trade status
            state.update_trade_status(open_pos)
            
            # Multiple positions warning
            if len(my_pos) > 1:
                logger.warning(f"Multiple positions open: {len(my_pos)}")
                for p in my_pos:
                    logger.warning(f"  - Ticket {p.ticket}: {p.type} {p.volume} @ {p.price_open}")
            
            # -----------------------------------------------------------
            # EXIT / RISK MANAGEMENT LOGIC
            # -----------------------------------------------------------
            if state.in_trade and my_pos:
                pos = my_pos[0]
                
                # Mandatory Close (Time)
                close_time = time(CONFIG['session']['end_hour'], CONFIG['session']['end_minute'])
                if now_cet.time() >= close_time:
                    logger.info(f"Mandatory close triggered: {now_cet.time()} >= {close_time}")
                    close_position(symbol)
                    continue

                # Trailing Stop Logic
                if pos.type == mt5.ORDER_TYPE_BUY:
                    current_profit_points = (tick.bid - pos.price_open) * contract_size
                else:
                    current_profit_points = (pos.price_open - tick.ask) * contract_size
                
                logger.debug(f"Current profit: {current_profit_points:.2f} pips, Max: {state.max_pnl:.2f}")
                
                if current_profit_points > state.max_pnl:
                    state.max_pnl = current_profit_points
                    logger.info(f"New max profit: {state.max_pnl:.2f} pips")

                # Check Stages
                best_retention = 0.0
                triggered = False
                
                for s in CONFIG['risk_management']['trailing_stages']:
                    if state.max_pnl >= s['min_profit']:
                        best_retention = s['retention']
                        triggered = True
                
                if triggered and best_retention != -1:
                    trail_dist = state.max_pnl * best_retention
                    trail_dist /= contract_size
                    
                    if pos.type == mt5.ORDER_TYPE_BUY:
                        new_sl = pos.price_open + trail_dist
                        should_modify = (pos.sl == 0 or new_sl > pos.sl)
                    else:
                        new_sl = pos.price_open - trail_dist
                        should_modify = (pos.sl == 0 or new_sl < pos.sl)
                    
                    if should_modify:
                        new_sl = round(new_sl, 5)
                        logger.info(f"Modifying SL to {new_sl:.5f} (retention: {best_retention})")
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
                                logger.info(f"Trap: Touched opposite low at {lower_target:.5f} (Buy Setup)")
                                state.touched_opposite = True
                        
                        # 2. Trigger
                        if state.touched_opposite:
                            upper_target = state.ghost_high - buffer
                            if tick.ask >= upper_target:
                                if tick.ask > state.daily_open_price:
                                    if velocity.is_high_velocity(CONFIG['entry_conditions']['velocity_multiplier']):
                                        logger.info(f"Entry conditions met: Price={tick.ask:.5f} >= {upper_target:.5f}, "
                                                   f"above open={state.daily_open_price:.5f}, high velocity")
                                        success, price = execute_trade(
                                            symbol, contract_size, 'buy', CONFIG['risk_management']['initial_sl']
                                        )
                                        if success:
                                            state.in_trade = True
                                            state.entry_price = price
                                            state.direction = 'buy'
                                            state.touched_opposite = False
                                            logger.info(f"Buy trade entered at {price:.5f}")

                    # SELL LOGIC
                    elif state.bias == 'sell':
                        # 1. Touch Opposite (Trap)
                        upper_target = state.ghost_high - buffer
                        if tick.bid >= upper_target:
                            if not state.touched_opposite:
                                logger.info(f"Trap: Touched opposite high at {upper_target:.5f} (Sell Setup)")
                                state.touched_opposite = True
                        
                        # 2. Trigger
                        if state.touched_opposite:
                            lower_target = state.ghost_low + buffer
                            if tick.bid <= lower_target:
                                if tick.bid < state.daily_open_price:
                                    if velocity.is_high_velocity(CONFIG['entry_conditions']['velocity_multiplier']):
                                        logger.info(f"Entry conditions met: Price={tick.bid:.5f} <= {lower_target:.5f}, "
                                                   f"below open={state.daily_open_price:.5f}, high velocity")
                                        success, price = execute_trade(
                                            symbol, contract_size, 'sell', CONFIG['risk_management']['initial_sl']
                                        )
                                        if success:
                                            state.in_trade = True
                                            state.entry_price = price
                                            state.direction = 'sell'
                                            state.touched_opposite = False
                                            logger.info(f"Sell trade entered at {price:.5f}")

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
    finally:
        logger.info("Shutting down MT5 connection")
        mt5.shutdown()
        logger.info("Trading bot stopped")


if __name__ == "__main__":
    main()