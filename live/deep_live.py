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
import signal
import sys
from typing import Optional, Dict, Any, List, Tuple, Callable, Union
from functools import wraps
from dataclasses import dataclass
from enum import Enum

dotenv.load_dotenv()

# -------------------------------------------------------------------
# CUSTOM EXCEPTIONS
# -------------------------------------------------------------------
class TradingError(Exception):
    """Base exception for all trading-related errors."""
    pass

class MT5ConnectionError(TradingError):
    """Raised when MT5 connection fails."""
    pass

class MT5OperationError(TradingError):
    """Raised when MT5 operation fails."""
    pass

class ConfigurationError(TradingError):
    """Raised when configuration is invalid."""
    pass

class OrderExecutionError(TradingError):
    """Raised when order execution fails."""
    pass

class SymbolError(TradingError):
    """Raised when symbol operations fail."""
    pass

class TimezoneError(TradingError):
    """Raised when timezone operations fail."""
    pass

# -------------------------------------------------------------------
# RETRY DECORATOR
# -------------------------------------------------------------------
def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
    logger: Optional[logging.Logger] = None
):
    """
    Retry decorator with exponential backoff.
    
    Args:
        max_attempts: Maximum number of attempts
        delay: Initial delay between attempts in seconds
        backoff: Multiplier for delay after each attempt
        exceptions: Exceptions to catch and retry on
        logger: Logger instance for logging retries
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
                    
                    t_mod.sleep(current_delay)
                    current_delay *= backoff
            return None
        return wrapper
    return decorator

# -------------------------------------------------------------------
# CONNECTION MANAGER
# -------------------------------------------------------------------
class MT5ConnectionManager:
    """Manages MT5 connection state and provides reconnection logic."""
    
    def __init__(self, login: int, password: str, server: str, logger: logging.Logger):
        self.login = login
        self.password = password
        self.server = server
        self.logger = logger
        self.connected = False
        self.last_connection_check = 0
        self.connection_check_interval = 60  # Check every 60 seconds
        
    def initialize(self) -> bool:
        """Initialize MT5 connection with retry logic."""
        try:
            self.logger.info(f"Initializing MT5 connection to {self.server}...")
            
            if not mt5.initialize():
                error = mt5.last_error()
                raise MT5ConnectionError(f"MT5 initialization failed: {error}")
            
            self.logger.info("MT5 terminal initialized successfully")
            
            if not mt5.login(login=self.login, password=self.password, server=self.server):
                error = mt5.last_error()
                raise MT5ConnectionError(f"MT5 login failed for account {self.login}: {error}")
            
            self.logger.info(f"Logged in to MT5 account: {self.login}")
            self.connected = True
            self.last_connection_check = t_mod.time()
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize MT5 connection: {e}")
            self.connected = False
            return False
    
    def check_connection(self) -> bool:
        """Check if MT5 connection is still active."""
        current_time = t_mod.time()
        if current_time - self.last_connection_check < self.connection_check_interval:
            return self.connected
        
        self.last_connection_check = current_time
        
        try:
            # Simple check: try to get terminal info
            info = mt5.terminal_info()
            if info is None:
                self.logger.warning("MT5 connection check failed: terminal_info returned None")
                self.connected = False
            else:
                self.connected = True
                
        except Exception as e:
            self.logger.warning(f"MT5 connection check failed: {e}")
            self.connected = False
        
        return self.connected
    
    def reconnect(self) -> bool:
        """Attempt to reconnect to MT5."""
        self.logger.warning("Attempting to reconnect to MT5...")
        
        # Shutdown existing connection
        try:
            mt5.shutdown()
            self.logger.debug("MT5 connection shutdown")
        except Exception as e:
            self.logger.debug(f"Error during shutdown (may be already disconnected): {e}")
        
        # Clear connection state
        self.connected = False
        
        # Try to reconnect with retry logic
        for attempt in range(1, 4):  # 3 attempts
            self.logger.info(f"Reconnection attempt {attempt}/3...")
            if self.initialize():
                self.logger.info("MT5 reconnection successful")
                return True
            
            if attempt < 3:
                wait_time = 5 * attempt  # 5, 10, 15 seconds
                self.logger.info(f"Waiting {wait_time}s before next reconnection attempt...")
                t_mod.sleep(wait_time)
        
        self.logger.error("Failed to reconnect to MT5 after multiple attempts")
        return False
    
    def ensure_connection(self) -> bool:
        """Ensure MT5 connection is active, reconnecting if necessary."""
        if self.check_connection():
            return True
        
        return self.reconnect()
    
    def shutdown(self):
        """Shutdown MT5 connection gracefully."""
        self.logger.info("Shutting down MT5 connection...")
        try:
            mt5.shutdown()
            self.connected = False
            self.logger.info("MT5 connection shutdown successfully")
        except Exception as e:
            self.logger.error(f"Error during MT5 shutdown: {e}")

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
# CONFIGURATION VALIDATION
# -------------------------------------------------------------------
class ConfigValidator:
    """Validates trading configuration."""
    
    @staticmethod
    def validate_config(config: Dict[str, Any]) -> List[str]:
        """
        Validate configuration and return list of errors.
        
        Args:
            config: Configuration dictionary
        
        Returns:
            List of validation error messages
        """
        errors = []
        
        # Validate bias_filter
        bias_filter = config.get('bias_filter', {})
        if 'buy_threshold' not in bias_filter:
            errors.append("Missing 'bias_filter.buy_threshold'")
        elif not 0 <= bias_filter['buy_threshold'] <= 1:
            errors.append("'bias_filter.buy_threshold' must be between 0 and 1")
        
        if 'sell_threshold' not in bias_filter:
            errors.append("Missing 'bias_filter.sell_threshold'")
        elif not 0 <= bias_filter['sell_threshold'] <= 1:
            errors.append("'bias_filter.sell_threshold' must be between 0 and 1")
        
        if bias_filter.get('buy_threshold', 1) <= bias_filter.get('sell_threshold', 0):
            errors.append("'buy_threshold' must be greater than 'sell_threshold'")
        
        # Validate entry_conditions
        entry_conditions = config.get('entry_conditions', {})
        if 'velocity_multiplier' not in entry_conditions:
            errors.append("Missing 'entry_conditions.velocity_multiplier'")
        elif entry_conditions['velocity_multiplier'] < 1:
            errors.append("'velocity_multiplier' must be >= 1")
        
        if 'lookback_period' not in entry_conditions:
            errors.append("Missing 'entry_conditions.lookback_period'")
        elif entry_conditions['lookback_period'] < 60:
            errors.append("'lookback_period' must be at least 60 seconds")
        
        # Validate risk_management
        risk = config.get('risk_management', {})
        if 'initial_sl' not in risk:
            errors.append("Missing 'risk_management.initial_sl'")
        elif risk['initial_sl'] <= 0:
            errors.append("'initial_sl' must be positive")
        
        trailing_stages = risk.get('trailing_stages', [])
        if not trailing_stages:
            errors.append("Missing 'risk_management.trailing_stages'")
        else:
            last_max = -1
            for i, stage in enumerate(trailing_stages):
                if 'min_profit' not in stage:
                    errors.append(f"Stage {i}: missing 'min_profit'")
                if 'retention' not in stage:
                    errors.append(f"Stage {i}: missing 'retention'")
                elif not (-1 <= stage['retention'] <= 1):
                    errors.append(f"Stage {i}: 'retention' must be between -1 and 1")
                
                if stage['min_profit'] <= last_max:
                    errors.append(f"Stage {i}: 'min_profit' must be greater than previous stage's max")
                
                if 'max_profit' in stage:
                    if stage['max_profit'] <= stage['min_profit']:
                        errors.append(f"Stage {i}: 'max_profit' must be greater than 'min_profit'")
                    last_max = stage['max_profit']
                else:
                    # Last stage doesn't need max_profit
                    pass
        
        # Validate session times
        session = config.get('session', {})
        required_times = ['day_open', 'ghost_start', 'ghost_end']
        for time_key in required_times:
            if time_key not in session:
                errors.append(f"Missing 'session.{time_key}'")
            elif not isinstance(session[time_key], time):
                errors.append(f"'session.{time_key}' must be a datetime.time object")
        
        # Check ghost range validity
        if 'ghost_start' in session and 'ghost_end' in session:
            if session['ghost_start'] >= session['ghost_end']:
                errors.append("'ghost_start' must be before 'ghost_end'")
        
        return errors

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------
LOGIN = int(os.environ.get("ACCOUNT_ID", "0"))
PASSWORD = os.environ.get("PASSWORD", "")
SERVER = os.environ.get("SERVER", "")
MAX_RETRIES = 5
TIMEOUT = 1

# Trading parameters
VOLUME = 0.01
DEVIATION = 10
MAGIC_NUM = 1234569

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

# Timezones
CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc
UTC2 = pytz.timezone('Europe/Athens')  # EET / CAT
UTC3 = pytz.timezone('Asia/Baghdad')   # EAT / MST

# Global instances (will be initialized in main)
logger = None
connection_manager = None

# -------------------------------------------------------------------
# SIGNAL HANDLER FOR GRACEFUL SHUTDOWN
# -------------------------------------------------------------------
class SignalHandler:
    """Handles OS signals for graceful shutdown."""
    
    def __init__(self, logger: logging.Logger, connection_manager: 'MT5ConnectionManager'):
        self.logger = logger
        self.connection_manager = connection_manager
        self.shutdown_requested = False
        
    def setup(self):
        """Setup signal handlers."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        self.logger.debug("Signal handlers installed")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        signame = signal.Signals(signum).name
        self.logger.info(f"Received signal {signame} ({signum}), initiating graceful shutdown...")
        self.shutdown_requested = True
    
    def should_shutdown(self) -> bool:
        """Check if shutdown has been requested."""
        return self.shutdown_requested

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

    @retry(max_attempts=3, delay=0.5, exceptions=(MT5OperationError,), logger=logger)
    def get_server_timestamp(self) -> Optional[float]:
        """Get current server timestamp with retry logic."""
        tick = mt5.symbol_info_tick(self.symbol)
        
        if tick is None:
            error = mt5.last_error()
            raise MT5OperationError(f"Failed to get tick for {self.symbol}: {error}")
        
        return tick.time_msc / 1000  # Convert to seconds

    @retry(max_attempts=2, delay=1.0, exceptions=(MT5OperationError,), logger=logger)
    def get_ticks(self, server_time: Optional[float] = None) -> np.ndarray:
        """Fetch ticks from MT5 with retry."""
        if server_time is None:
            server_time = self.get_server_timestamp()
        
        ticks = mt5.copy_ticks_from(self.symbol, server_time, 10000, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            raise MT5OperationError(f"No ticks returned for {self.symbol}")
        return ticks

    def get_tick_timestamps(self, server_time: Optional[float] = None) -> List[float]:
        """Extract timestamps from ticks with error handling."""
        try:
            ticks = self.get_ticks(server_time)
            return [t["time_msc"] / 1000 for t in ticks]
        except MT5OperationError as e:
            self.logger.warning(f"Failed to get tick timestamps: {e}")
            return []

    def on_tick(self) -> None:
        """Process new ticks and update timestamp deque."""
        try:
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
                
        except Exception as e:
            self.logger.error(f"Error in on_tick: {e}")

    def cleanup(self, now: float) -> None:
        """Remove ticks older than 30 seconds."""
        cutoff = now - 30
        while self.tick_timestamps and self.tick_timestamps[0] < cutoff:
            self.tick_timestamps.popleft()

    def update_history(self) -> None:
        """Update density history once per second."""
        try:
            now = self.get_server_timestamp()
            if now is None:
                return
                
            self.cleanup(now)
            current_density = len(self.tick_timestamps) / 30.0
            self.density_history.append(current_density)
            self.last_update = t_mod.time()
            
        except Exception as e:
            self.logger.error(f"Error updating velocity history: {e}")

    def is_high_velocity(self, multiplier: float) -> bool:
        """Check if current velocity exceeds historical average by multiplier."""
        try:
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
            
        except Exception as e:
            self.logger.error(f"Error checking high velocity: {e}")
            return False


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
# CORE LOGIC - with enhanced error handling
# -------------------------------------------------------------------

@retry(max_attempts=3, delay=2.0, exceptions=(TimezoneError,), logger=logger)
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

    try:
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
        
    except Exception as e:
        raise TimezoneError(f"Failed to determine server timezone: {e}")


@retry(max_attempts=2, delay=1.0, exceptions=(MT5OperationError,), logger=logger)
def get_server_time(symbol: str) -> Optional[datetime]:
    """Get MT5 server time with error handling."""
    logger = logging.getLogger("trading_bot.time")
    
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        error = mt5.last_error()
        raise MT5OperationError(f"Failed to get tick for server time {symbol}: {error}")
    
    try:
        server_time = pd.to_datetime(tick.time, unit='s')
        zone = get_server_timezone()
        server_time = zone.localize(server_time)
        return server_time
    except Exception as e:
        raise TimezoneError(f"Error converting server time: {e}")


def get_server_time_cet(symbol: str) -> Optional[datetime]:
    """Gets MT5 server time and converts to CET."""
    logger = logging.getLogger("trading_bot.time")
    
    try:
        server_time = get_server_time(symbol)
        if server_time is None:
            return None
        
        cet_time = server_time.astimezone(CET)
        logger.debug(f"Time conversion: Server={server_time} -> CET={cet_time}")
        return cet_time
        
    except Exception as e:
        logger.error(f"Failed to get CET time: {e}")
        return None


@retry(max_attempts=2, delay=2.0, exceptions=(MT5OperationError,), logger=logger)
def calculate_daily_bias(symbol: str) -> str:
    """
    Calculates bias based on YESTERDAY'S D1 Candle.
    (c - l) / (h - l)
    """
    logger = logging.getLogger("trading_bot.bias")
    
    # Get 2 days of D1 data to ensure we have yesterday completed
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 1, 1)
    if rates is None or len(rates) == 0:
        error = mt5.last_error()
        raise MT5OperationError(f"Error fetching D1 data for {symbol}: {error}")

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
    
    try:
        today = datetime.now()
        start_dt = datetime.combine(today, CONFIG['session']['day_open'])
        end_dt = today

        server_zone = get_server_timezone()
        # Convert to server timezone
        start_dt = server_zone.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(server_zone)
        end_dt = server_zone.localize(end_dt) if end_dt.tzinfo is None else end_dt.astimezone(server_zone)

        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
        if rates is None or len(rates) == 0:
            logger.warning("No M1 data returned for opposite confirmation")
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
        
    except Exception as e:
        logger.error(f"Error checking opposite touch: {e}")
        return False


@retry(max_attempts=3, delay=1.0, exceptions=(MT5OperationError,), logger=logger)
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
        raise MT5OperationError(f"No data returned for ghost range: {symbol}")

    # Calculate Min/Max from the bars
    g_min = float(np.min(rates['low']))
    g_max = float(np.max(rates['high']))
    
    logger.info(f"Ghost range calculated: {g_min:.5f} - {g_max:.5f}")
    return g_min, g_max


@retry(max_attempts=3, delay=1.0, exceptions=(MT5OperationError,), logger=logger)
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
        raise MT5OperationError(f"No data returned for Frankfurt open: {symbol}")
    
    open_price = float(rates[0][1])
    logger.info(f"Frankfurt open price: {open_price:.5f}")
    return open_price


def get_filling_type(symbol: str) -> Optional[int]:
    """Determine order filling type based on symbol info."""
    logger = logging.getLogger("trading_bot.order")
    
    try:
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
        
    except Exception as e:
        logger.error(f"Error getting filling type: {e}")
        return None


def validate_order_params(symbol: str, direction: str, sl_pips: float) -> List[str]:
    """Validate order parameters before execution."""
    errors = []
    
    # Validate symbol
    info = mt5.symbol_info(symbol)
    if info is None:
        errors.append(f"Symbol {symbol} not found")
        return errors
    
    # Validate direction
    if direction not in ['buy', 'sell']:
        errors.append(f"Invalid direction: {direction}")
    
    # Validate stop loss
    if sl_pips <= 0:
        errors.append(f"Stop loss must be positive: {sl_pips}")
    
    # Validate volume
    if VOLUME <= 0:
        errors.append(f"Volume must be positive: {VOLUME}")
    elif VOLUME < info.volume_min:
        errors.append(f"Volume below minimum: {VOLUME} < {info.volume_min}")
    elif VOLUME > info.volume_max:
        errors.append(f"Volume above maximum: {VOLUME} > {info.volume_max}")
    elif VOLUME % info.volume_step != 0:
        errors.append(f"Volume not a multiple of step: {VOLUME} % {info.volume_step} != 0")
    
    return errors


@retry(max_attempts=2, delay=1.0, exceptions=(OrderExecutionError,), logger=logger)
def execute_trade(symbol: str, contract_size: float, direction: str, sl_pips: float) -> Tuple[bool, float]:
    """Send order to MT5 with comprehensive logging and validation."""
    logger = logging.getLogger("trading_bot.order")
    
    # Validate parameters
    validation_errors = validate_order_params(symbol, direction, sl_pips)
    if validation_errors:
        for error in validation_errors:
            logger.error(f"Order validation failed: {error}")
        return False, 0.0
    
    # Get current tick
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        error = mt5.last_error()
        raise OrderExecutionError(f"Cannot get tick for {symbol}: {error}")
    
    # Get filling type
    filling = get_filling_type(symbol)
    if filling is None:
        raise OrderExecutionError(f"Cannot determine filling type for {symbol}")
    
    # Calculate stop loss
    sl_points = sl_pips / contract_size
    
    # Prepare order request
    if direction == 'buy':
        price = tick.ask
        sl_price = price - sl_points
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        sl_price = price + sl_points
        order_type = mt5.ORDER_TYPE_SELL
    
    # Validate prices
    symbol_info = mt5.symbol_info(symbol)
    if price <= 0 or sl_price <= 0:
        raise OrderExecutionError(f"Invalid prices: price={price}, sl={sl_price}")
    
    # Round prices to appropriate digits
    digits = symbol_info.digits
    price = round(price, digits)
    sl_price = round(sl_price, digits)
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": VOLUME,
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "deviation": DEVIATION,
        "magic": MAGIC_NUM,
        "comment": "LiveDemo_Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    
    logger.info(f"Sending {direction} order: price={price:.5f}, sl={sl_price:.5f}, volume={VOLUME}")
    
    result = mt5.order_send(request)
    
    if result is None:
        raise OrderExecutionError(f"No response from MT5 for order")
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        error_msg = f"Order failed: {result.comment} (retcode: {result.retcode})"
        logger.debug(f"Request details: {request}")
        raise OrderExecutionError(error_msg)
    
    logger.info(f"Trade executed: {direction} at {result.price:.5f}, ticket: {result.order}")
    return True, result.price


@retry(max_attempts=2, delay=1.0, exceptions=(MT5OperationError,), logger=logger)
def close_position(symbol: str) -> None:
    """Close all positions with our Magic Number."""
    logger = logging.getLogger("trading_bot.order")
    
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        logger.warning(f"No positions found for {symbol}")
        return
    
    positions_closed = 0
    for pos in positions:
        if pos.magic == MAGIC_NUM:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                error = mt5.last_error()
                raise MT5OperationError(f"Cannot get tick for closing position {pos.ticket}: {error}")
            
            if pos.type == mt5.ORDER_TYPE_BUY:
                price = tick.bid
                close_type = mt5.ORDER_TYPE_SELL
            else:
                price = tick.ask
                close_type = mt5.ORDER_TYPE_BUY
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": pos.ticket,
                "price": price,
                "deviation": DEVIATION,
                "magic": MAGIC_NUM,
                "comment": "Mandatory Close"
            }
            
            logger.info(f"Closing position {pos.ticket} ({'BUY' if pos.type == mt5.ORDER_TYPE_BUY else 'SELL'})")
            result = mt5.order_send(request)
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                error_msg = f"Position close failed: {result.comment}"
                raise MT5OperationError(error_msg)
            else:
                logger.info(f"Position {pos.ticket} closed at {result.price:.5f}")
                positions_closed += 1
    
    if positions_closed > 0:
        logger.info(f"Closed {positions_closed} position(s)")


@retry(max_attempts=2, delay=1.0, exceptions=(MT5OperationError,), logger=logger)
def modify_sl(symbol: str, ticket: int, new_sl: float) -> bool:
    """Modify stop loss for existing position."""
    logger = logging.getLogger("trading_bot.order")
    
    # Validate new SL
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise MT5OperationError(f"Cannot get symbol info for {symbol}")
    
    # Round to appropriate digits
    digits = symbol_info.digits
    new_sl = round(new_sl, digits)
    
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
        error_msg = f"SL modification failed: {result.comment}"
        raise MT5OperationError(error_msg)
    
    logger.info(f"SL modified for ticket {ticket}: {new_sl:.5f}")
    return True


def safe_mt5_call(func: Callable, *args, **kwargs) -> Any:
    """
    Safely call MT5 functions with connection checking and error handling.
    
    Args:
        func: MT5 function to call
        *args: Function arguments
        **kwargs: Function keyword arguments
    
    Returns:
        Function result or None if failed
    """
    global connection_manager, logger
    
    if connection_manager is None or logger is None:
        return None
    
    # Ensure connection is active
    if not connection_manager.ensure_connection():
        logger.error("Cannot perform MT5 operation: connection not available")
        return None
    
    try:
        result = func(*args, **kwargs)
        return result
    except Exception as e:
        logger.error(f"MT5 operation failed: {e}")
        
        # Mark connection as potentially bad
        connection_manager.connected = False
        
        return None

# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------

def main():
    global logger, connection_manager
    
    parser = argparse.ArgumentParser(description="Live trading momentum based bot for HFM")
    parser.add_argument("symbol", help="symbol to be traded")
    parser.add_argument("--log-level", default="INFO", 
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="Logging level (default: INFO)")
    parser.add_argument("--validate-config", action="store_true",
                       help="Validate configuration and exit")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    
    # Initialize logger
    logger = TradingLogger.setup_logging(symbol, args.log_level)
    logger.info(f"Starting trading bot for symbol: {symbol}")
    logger.info(f"Log level: {args.log_level}")
    
    # Validate configuration
    config_errors = ConfigValidator.validate_config(CONFIG)
    if config_errors:
        logger.error("Configuration validation failed:")
        for error in config_errors:
            logger.error(f"  - {error}")
        
        if args.validate_config:
            logger.info("Configuration validation completed with errors")
            sys.exit(1)
        else:
            logger.warning("Proceeding with invalid configuration (use --validate-config to validate)")
    else:
        logger.info("Configuration validation passed")
    
    if args.validate_config:
        logger.info("Configuration validation completed successfully")
        sys.exit(0)
    
    # Initialize connection manager
    connection_manager = MT5ConnectionManager(LOGIN, PASSWORD, SERVER, logger)
    
    # Initialize MT5 connection
    if not connection_manager.initialize():
        logger.error("Failed to initialize MT5 connection")
        sys.exit(1)
    
    # Setup signal handler
    signal_handler = SignalHandler(logger, connection_manager)
    signal_handler.setup()
    
    # Check symbol
    symbol_info = safe_mt5_call(mt5.symbol_info, symbol)
    if symbol_info is None:
        logger.error(f"Symbol {symbol} not found or cannot be selected")
        connection_manager.shutdown()
        sys.exit(1)
    
    if not safe_mt5_call(mt5.symbol_select, symbol, True):
        logger.error(f"Cannot select symbol {symbol}")
        connection_manager.shutdown()
        sys.exit(1)
    
    contract_size = symbol_info.trade_contract_size
    logger.info(f"Symbol {symbol} selected, contract size: {contract_size}")
    logger.info(f"Session hours: {CONFIG['session']['start_hour']:02d}:{CONFIG['session']['start_minute']:02d} - "
                f"{CONFIG['session']['end_hour']:02d}:{CONFIG['session']['end_minute']:02d}")

    # Initialize state and monitoring
    state = StrategyState(symbol)
    velocity = VelocityMonitor(symbol, lookback_seconds=CONFIG['entry_conditions']['lookback_period'])
    
    logger.info("Live trading started")
    
    last_update_seconds = t_mod.time()
    consecutive_errors = 0
    max_consecutive_errors = 10

    try:
        while not signal_handler.should_shutdown():
            # Hardware Efficiency: Sleep to reduce CPU usage
            t_mod.sleep(0.1)
            
            # Check connection
            if not connection_manager.ensure_connection():
                logger.error("Lost connection to MT5 and unable to reconnect")
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"Too many consecutive errors ({consecutive_errors}), shutting down")
                    break
                t_mod.sleep(5)
                continue
            
            # Reset error counter on successful connection
            consecutive_errors = 0
            
            # Update Time
            now = safe_mt5_call(get_server_time, symbol)
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
                bias = safe_mt5_call(calculate_daily_bias, symbol)
                if bias is not None:
                    state.bias = bias
                    logger.info(f"Daily bias set to: {state.bias}")

            # 4. Data Processing (Tick)
            tick = safe_mt5_call(mt5.symbol_info_tick, symbol)
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
                    ghost_range = safe_mt5_call(get_ghost_range, symbol, today_date)
                    if ghost_range is not None:
                        g_min, g_max = ghost_range
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
                    open_price = safe_mt5_call(get_frankfurt_open, symbol, today_date)
                    if open_price is not None:
                        state.daily_open_price = open_price
                        logger.info(f"Market open price recorded: {open_price:.5f}")
                    else:
                        logger.warning("Could not get Frankfurt open price")

            # C. Check for existing positions
            positions = safe_mt5_call(mt5.positions_get, symbol=symbol)
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
                    safe_mt5_call(close_position, symbol)
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
                        safe_mt5_call(modify_sl, symbol, pos.ticket, new_sl)

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
                                        success, price = safe_mt5_call(
                                            execute_trade, symbol, contract_size, 'buy', CONFIG['risk_management']['initial_sl']
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
                                        success, price = safe_mt5_call(
                                            execute_trade, symbol, contract_size, 'sell', CONFIG['risk_management']['initial_sl']
                                        )
                                        if success:
                                            state.in_trade = True
                                            state.entry_price = price
                                            state.direction = 'sell'
                                            state.touched_opposite = False
                                            logger.info(f"Sell trade entered at {price:.5f}")

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
    finally:
        logger.info("Initiating graceful shutdown...")
        
        # Close any open positions
        try:
            positions = safe_mt5_call(mt5.positions_get, symbol=symbol)
            if positions:
                logger.info(f"Closing {len(positions)} open position(s)...")
                safe_mt5_call(close_position, symbol)
        except Exception as e:
            logger.error(f"Error closing positions during shutdown: {e}")
        
        # Shutdown connection
        if connection_manager:
            connection_manager.shutdown()
        
        logger.info("Trading bot stopped gracefully")

if __name__ == "__main__":
    main()