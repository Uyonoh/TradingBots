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
import json
from typing import Optional, Dict, Any, List, Tuple, Callable
from functools import wraps, lru_cache
from pathlib import Path
import yaml

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
# CONFIGURATION MANAGEMENT
# -------------------------------------------------------------------
class ConfigValidator:
    """Validates trading configuration."""
    
    @staticmethod
    def validate_config(config: Dict[str, Any]) -> List[str]:
        """
        Validate configuration and return list of errors.
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
        
        if 'lookback_seconds' not in entry_conditions:
            errors.append("Missing 'entry_conditions.lookback_seconds'")
        elif entry_conditions['lookback_seconds'] < 60:
            errors.append("'lookback_seconds' must be at least 60 seconds")
        
        # Validate risk_management
        risk = config.get('risk_management', {})
        if 'initial_sl_pips' not in risk:
            errors.append("Missing 'risk_management.initial_sl_pips'")
        elif risk['initial_sl_pips'] <= 0:
            errors.append("'initial_sl_pips' must be positive")
        
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
                    if i != len(trailing_stages) - 1:
                        errors.append("Only the last stage can have null max_profit")
        
        # Validate session times
        session = config.get('session', {})
        required_times = ['day_open', 'ghost_start', 'ghost_end', 'trading_start', 'trading_end']
        for time_key in required_times:
            if time_key not in session:
                errors.append(f"Missing 'session.{time_key}'")
        
        # Validate time format strings
        for time_key in required_times:
            if time_key in session:
                time_str = session[time_key]
                try:
                    if isinstance(time_str, str):
                        hour, minute = map(int, time_str.split(':'))
                        if not (0 <= hour <= 23 and 0 <= minute <= 59):
                            errors.append(f"'session.{time_key}' has invalid time: {time_str}")
                except (ValueError, AttributeError):
                    errors.append(f"'session.{time_key}' must be in HH:MM format, got: {time_str}")
        
        # Check ghost range validity
        if 'ghost_start' in session and 'ghost_end' in session:
            try:
                ghost_start = session['ghost_start']
                ghost_end = session['ghost_end']
                if isinstance(ghost_start, str) and isinstance(ghost_end, str):
                    start_h, start_m = map(int, ghost_start.split(':'))
                    end_h, end_m = map(int, ghost_end.split(':'))
                    if (end_h < start_h) or (end_h == start_h and end_m <= start_m):
                        errors.append("'ghost_end' must be after 'ghost_start'")
            except (ValueError, AttributeError):
                pass
        
        # Check trading range validity
        if 'trading_start' in session and 'trading_end' in session:
            try:
                trading_start = session['trading_start']
                trading_end = session['trading_end']
                if isinstance(trading_start, str) and isinstance(trading_end, str):
                    start_h, start_m = map(int, trading_start.split(':'))
                    end_h, end_m = map(int, trading_end.split(':'))
                    if (end_h < start_h) or (end_h == start_h and end_m <= start_m):
                        errors.append("'trading_end' must be after 'trading_start'")
            except (ValueError, AttributeError):
                pass
        
        # Validate timezones
        timezones = config.get('timezones', {})
        tz_keys = ['winter_tz', 'summer_tz', 'local_tz']
        for tz_key in tz_keys:
            if tz_key in timezones:
                try:
                    pytz.timezone(timezones[tz_key])
                except pytz.UnknownTimeZoneError:
                    errors.append(f"Unknown timezone: {timezones[tz_key]}")
        
        # Validate performance settings
        performance = config.get('performance', {})
        perf_settings = {
            'tick_processing_interval': (0.001, 1.0),
            'velocity_update_interval': (0.1, 10.0),
            'position_check_interval': (0.5, 30.0),
            'outside_session_sleep': (1.0, 300.0),
            'cache_ttl_hours': (0.1, 24.0)
        }
        
        for setting, (min_val, max_val) in perf_settings.items():
            if setting in performance:
                if not (min_val <= performance[setting] <= max_val):
                    errors.append(f"'performance.{setting}' must be between {min_val} and {max_val}")
        
        # Validate logging
        logging_config = config.get('logging', {})
        if 'level' in logging_config and logging_config['level'] not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
            errors.append("'logging.level' must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        
        return errors


class ConfigurationManager:
    """Manages configuration loading, validation, and updates."""
    
    def __init__(self, config_file: Optional[str] = None):
        self.config_file = config_file
        self.config = self._load_default_config()
        self.last_modified = 0
        self.logger = logging.getLogger("trading_bot.config")
        self._callbacks = []
        
        if config_file and os.path.exists(config_file):
            self.load_from_file(config_file)
        else:
            self._apply_env_overrides()
    
    def _load_default_config(self) -> Dict[str, Any]:
        """Load default configuration."""
        return {
            'bias_filter': {
                'buy_threshold': 0.6,
                'sell_threshold': 0.4
            },
            'entry_conditions': {
                'buffer_pips': 10.0,
                'velocity_multiplier': 2.0,
                'lookback_seconds': 3600
            },
            'risk_management': {
                'initial_sl_pips': 50.0,
                'trailing_stages': [
                    {'min_profit': 0, 'max_profit': 30, 'retention': -1},
                    {'min_profit': 30, 'max_profit': 60, 'retention': 0.5},
                    {'min_profit': 60, 'max_profit': 90, 'retention': 0.7},
                    {'min_profit': 90, 'max_profit': 120, 'retention': 0.8},
                    {'min_profit': 120, 'max_profit': 150, 'retention': 0.9},
                    {'min_profit': 150, 'retention': 0.95}
                ]
            },
            'session': {
                'day_open': '09:00',
                'ghost_start': '08:00',
                'ghost_end': '08:30',
                'trading_start': '10:00',
                'trading_end': '17:00'
            },
            'timezones': {
                'winter_tz': 'Europe/Athens',
                'summer_tz': 'Asia/Baghdad',
                'local_tz': 'Europe/Berlin'
            },
            'performance': {
                'tick_processing_interval': 0.1,
                'velocity_update_interval': 1.0,
                'position_check_interval': 2.0,
                'outside_session_sleep': 60.0,
                'cache_ttl_hours': 1.0
            },
            'logging': {
                'level': 'INFO',
                'enable_file_logging': True,
                'max_file_size_mb': 10,
                'backup_count': 5,
                'separate_error_logs': True,
                'log_directory': 'logs'
            },
            'trading': {
                'volume': 0.01,
                'deviation': 10,
                'magic_number': 1234569
            }
        }
    
    def _apply_env_overrides(self):
        """Apply environment variable overrides."""
        # Trading parameters from environment
        if 'VOLUME' in os.environ:
            try:
                self.config['trading']['volume'] = float(os.environ['VOLUME'])
            except ValueError:
                pass
        
        # Logging level from environment
        if 'LOG_LEVEL' in os.environ:
            log_level = os.environ['LOG_LEVEL'].upper()
            if log_level in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                self.config['logging']['level'] = log_level
    
    def load_from_file(self, config_file: str) -> bool:
        """Load configuration from YAML or JSON file."""
        try:
            with open(config_file, 'r') as f:
                if config_file.endswith(('.yaml', '.yml')):
                    file_config = yaml.safe_load(f)
                elif config_file.endswith('.json'):
                    file_config = json.load(f)
                else:
                    self.logger.error(f"Unsupported config file format: {config_file}")
                    return False
            
            # Validate the loaded config
            errors = ConfigValidator.validate_config(file_config)
            if errors:
                self.logger.error(f"Configuration validation failed:")
                for error in errors:
                    self.logger.error(f"  - {error}")
                return False
            
            # Merge with defaults (file config overrides defaults)
            self._merge_configs(self.config, file_config)
            self.config_file = config_file
            self.last_modified = os.path.getmtime(config_file)
            
            self.logger.info(f"Configuration loaded from {config_file}")
            return True
            
        except yaml.YAMLError as e:
            self.logger.error(f"Error parsing YAML config file: {e}")
            return False
        except json.JSONDecodeError as e:
            self.logger.error(f"Error parsing JSON config file: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error loading config file: {e}")
            return False
    
    def _merge_configs(self, base: Dict[str, Any], override: Dict[str, Any]):
        """Recursively merge override config into base config."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_configs(base[key], value)
            else:
                base[key] = value
    
    def get_time(self, time_key: str) -> time:
        """Get time object from config string."""
        time_str = self.config['session'][time_key]
        if isinstance(time_str, str):
            hour, minute = map(int, time_str.split(':'))
            return time(hour, minute)
        return time_str
    
    def get_timezone(self, tz_key: str) -> pytz.tzinfo:
        """Get timezone object from config."""
        tz_str = self.config['timezones'][tz_key]
        return pytz.timezone(tz_str)
    
    def register_callback(self, callback: Callable):
        """Register callback for configuration changes."""
        self._callbacks.append(callback)
    
    def notify_callbacks(self):
        """Notify all registered callbacks of configuration change."""
        for callback in self._callbacks:
            try:
                callback(self.config)
            except Exception as e:
                self.logger.error(f"Error in config callback: {e}")
    
    def check_for_updates(self) -> bool:
        """Check if config file has been modified and reload if needed."""
        if not self.config_file or not os.path.exists(self.config_file):
            return False
        
        try:
            current_mtime = os.path.getmtime(self.config_file)
            if current_mtime > self.last_modified:
                self.logger.info("Configuration file modified, reloading...")
                if self.load_from_file(self.config_file):
                    self.notify_callbacks()
                    return True
        except Exception as e:
            self.logger.error(f"Error checking config updates: {e}")
        
        return False
    
    def save_to_file(self, filepath: str) -> bool:
        """Save current configuration to file."""
        try:
            with open(filepath, 'w') as f:
                if filepath.endswith('.json'):
                    json.dump(self.config, f, indent=2, default=str)
                else:
                    yaml.dump(self.config, f, default_flow_style=False)
            
            self.logger.info(f"Configuration saved to {filepath}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to save configuration: {e}")
            return False

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
# TIME OPTIMIZATION UTILITIES
# -------------------------------------------------------------------
class TimeCache:
    """Caches time-related computations to avoid repeated calculations."""
    
    _timezone_cache: Dict[date, pytz.tzinfo] = {}
    _last_timezone_update: Optional[datetime] = None
    _timezone_cache_ttl: int = 3600  # 1 hour
    
    @classmethod
    def clear_timezone_cache(cls):
        """Clear timezone cache."""
        cls._timezone_cache.clear()
        cls._last_timezone_update = None
    
    @classmethod
    def get_timezone(cls, for_date: Optional[date] = None) -> pytz.tzinfo:
        """Get timezone with caching."""
        if for_date is None:
            for_date = datetime.now().date()
        
        # Check cache
        if for_date in cls._timezone_cache:
            return cls._timezone_cache[for_date]
        
        # Calculate and cache
        timezone = cls._calculate_timezone(for_date)
        cls._timezone_cache[for_date] = timezone
        
        # Clean old cache entries (older than 30 days)
        today = datetime.now().date()
        old_dates = [d for d in cls._timezone_cache.keys() 
                    if (today - d).days > 30]
        for d in old_dates:
            del cls._timezone_cache[d]
        
        return timezone
    
    @staticmethod
    def _calculate_timezone(for_date: date) -> pytz.tzinfo:
        """Calculate timezone for a specific date."""
        # Helper to find the last Sunday of a given month
        def last_sunday(year: int, month: int) -> datetime:
            last_day = calendar.monthrange(year, month)[1]
            dt = datetime(year, month, last_day, tzinfo=timezone.utc)
            offset = (dt.weekday() + 1) % 7  # Weekday 6 is Sunday
            return dt - timedelta(days=offset)
        
        # DST boundaries
        dst_start = last_sunday(for_date.year, 3).replace(hour=1)
        dst_end = last_sunday(for_date.year, 10).replace(hour=1)
        
        # Create datetime for timezone determination
        dt = datetime(for_date.year, for_date.month, for_date.day, 12, 0, 0, tzinfo=timezone.utc)
        
        # Determine offset
        if dst_start <= dt < dst_end:
            return pytz.timezone('Asia/Baghdad')  # UTC+3
        else:
            return pytz.timezone('Europe/Athens')  # UTC+2


class DateTimeUtils:
    """Optimized datetime utilities with caching."""
    
    @staticmethod
    @lru_cache(maxsize=128)
    def combine_date_time(base_date: date, time_obj: time, tz: pytz.tzinfo) -> datetime:
        """Combine date and time with timezone (cached)."""
        dt = datetime.combine(base_date, time_obj)
        return tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
    
    @staticmethod
    def now_in_timezone(tz: pytz.tzinfo) -> datetime:
        """Get current time in specified timezone."""
        return datetime.now(timezone.utc).astimezone(tz)


class SessionTimeManager:
    """Manages and caches session time calculations."""
    
    def __init__(self, config_manager: ConfigurationManager, symbol: str, logger: logging.Logger):
        self.config_manager = config_manager
        self.symbol = symbol
        self.logger = logger
        
        # Cache structures
        self._cached_session_times: Dict[date, Dict[str, datetime]] = {}
        self._current_date: Optional[date] = None
        self._current_times: Optional[Dict[str, datetime]] = None
        
    def get_session_times(self, for_date: date) -> Dict[str, datetime]:
        """Get session times for a specific date (cached)."""
        if for_date != self._current_date or self._current_times is None:
            self._current_date = for_date
            self._current_times = self._calculate_session_times(for_date)
        
        return self._current_times
    
    def _calculate_session_times(self, for_date: date) -> Dict[str, datetime]:
        """Calculate session times for a date."""
        server_tz = TimeCache.get_timezone(for_date)
        
        # Helper function to create localized datetime
        def make_dt(time_key: str) -> datetime:
            time_obj = self.config_manager.get_time(time_key)
            return DateTimeUtils.combine_date_time(for_date, time_obj, server_tz)
        
        return {
            'day_open': make_dt('day_open'),
            'ghost_start': make_dt('ghost_start'),
            'ghost_end': make_dt('ghost_end'),
            'session_start': make_dt('trading_start'),
            'session_end': make_dt('trading_end'),
        }
    
    def is_in_trading_hours(self, current_time: datetime) -> bool:
        """Check if current time is within trading hours."""
        session_times = self.get_session_times(current_time.date())
        return session_times['session_start'] <= current_time < session_times['session_end']
    
    def should_check_ghost_range(self, current_time: datetime) -> bool:
        """Check if we should check for ghost range."""
        session_times = self.get_session_times(current_time.date())
        return current_time > session_times['ghost_end']
    
    def should_check_market_open(self, current_time: datetime) -> bool:
        """Check if we should check for market open."""
        session_times = self.get_session_times(current_time.date())
        return current_time >= session_times['day_open']

# -------------------------------------------------------------------
# CONNECTION MANAGER
# -------------------------------------------------------------------
class MT5ConnectionManager:
    """Manages MT5 connection state and provides reconnection logic."""
    
    def __init__(self, config_manager: ConfigurationManager, logger: logging.Logger):
        self.config_manager = config_manager
        self.logger = logger
        self.connected = False
        self.last_connection_check = 0
        self.connection_check_interval = 60
        
    def initialize(self) -> bool:
        """Initialize MT5 connection with retry logic."""
        try:
            # Get credentials from environment
            login = int(os.environ.get("ACCOUNT_ID", "0"))
            password = os.environ.get("PASSWORD", "")
            server = os.environ.get("SERVER", "")
            
            self.logger.info(f"Initializing MT5 connection to {server}...")
            
            if not mt5.initialize():
                error = mt5.last_error()
                raise MT5ConnectionError(f"MT5 initialization failed: {error}")
            
            self.logger.info("MT5 terminal initialized successfully")
            
            if not mt5.login(login=login, password=password, server=server):
                error = mt5.last_error()
                raise MT5ConnectionError(f"MT5 login failed for account {login}: {error}")
            
            self.logger.info(f"Logged in to MT5 account: {login}")
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
            info = mt5.terminal_info()
            self.connected = info is not None
            return self.connected
        except Exception:
            self.connected = False
            return False
    
    def reconnect(self) -> bool:
        """Attempt to reconnect to MT5."""
        self.logger.warning("Attempting to reconnect to MT5...")
        
        try:
            mt5.shutdown()
        except Exception:
            pass
        
        self.connected = False
        
        for attempt in range(1, 4):
            self.logger.info(f"Reconnection attempt {attempt}/3...")
            if self.initialize():
                self.logger.info("MT5 reconnection successful")
                return True
            
            if attempt < 3:
                wait_time = 5 * attempt
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
    def setup_logging(config_manager: ConfigurationManager, symbol: str) -> logging.Logger:
        """
        Configure structured logging with console and file handlers.
        """
        config = config_manager.config
        log_config = config['logging']
        
        # Create logs directory if it doesn't exist
        log_dir = log_config['log_directory']
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # Create logger
        logger = logging.getLogger("trading_bot")
        logger.setLevel(getattr(logging, log_config['level'].upper()))
        
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
        console_handler.setLevel(getattr(logging, log_config['level'].upper()))
        logger.addHandler(console_handler)
        
        if log_config.get('enable_file_logging', True):
            # File handler with rotation
            log_file = os.path.join(log_dir, f"trading_{symbol}_{datetime.now().strftime('%Y%m%d')}.log")
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=log_config.get('max_file_size_mb', 10) * 1024 * 1024,
                backupCount=log_config.get('backup_count', 5)
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.INFO)  # File gets INFO and above
            logger.addHandler(file_handler)
            
            # Separate error file handler
            if log_config.get('separate_error_logs', True):
                error_file = os.path.join(log_dir, f"errors_{symbol}_{datetime.now().strftime('%Y%m%d')}.log")
                error_handler = logging.handlers.RotatingFileHandler(
                    error_file,
                    maxBytes=log_config.get('max_file_size_mb', 10) * 512 * 1024,
                    backupCount=log_config.get('backup_count', 5)
                )
                error_handler.setFormatter(formatter)
                error_handler.setLevel(logging.WARNING)  # Error file gets WARNING and above
                logger.addHandler(error_handler)
        
        return logger

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
# OPTIMIZED VELOCITY MONITOR
# -------------------------------------------------------------------
class OptimizedVelocityMonitor:
    """
    Optimized velocity monitor with efficient data structures and calculations.
    """
    
    def __init__(self, symbol: str, config_manager: ConfigurationManager):
        self.symbol = symbol
        self.config_manager = config_manager
        self.config = config_manager.config
        
        lookback_seconds = self.config['entry_conditions']['lookback_seconds']
        self.tick_timestamps = deque(maxlen=3000)
        self.density_history = deque(maxlen=lookback_seconds)
        self.density_sum = 0.0
        self.last_update = t_mod.time()
        self.last_tick_fetch = 0
        self.tick_fetch_interval = 0.5
        self.logger = logging.getLogger("trading_bot.velocity")
        
        # Register for config updates
        config_manager.register_callback(self._on_config_changed)

    def _on_config_changed(self, new_config: Dict[str, Any]):
        """Handle configuration changes."""
        old_lookback = self.density_history.maxlen
        new_lookback = new_config['entry_conditions']['lookback_seconds']
        
        if new_lookback != old_lookback:
            self.logger.info(f"Updating velocity lookback from {old_lookback}s to {new_lookback}s")
            
            # Create new deque with updated maxlen
            new_history = deque(maxlen=new_lookback)
            new_history.extend(self.density_history)
            self.density_history = new_history
            
            # Recalculate sum
            self.density_sum = sum(self.density_history)
        
        self.config = new_config

    def get_server_timestamp(self) -> Optional[float]:
        """Get current server timestamp efficiently."""
        tick = mt5.symbol_info_tick(self.symbol)
        return tick.time_msc / 1000 if tick is not None else None

    def get_tick_timestamps_batch(self, server_time: Optional[float] = None) -> np.ndarray:
        """Fetch ticks in batches for better performance."""
        current_time = t_mod.time()
        if current_time - self.last_tick_fetch < self.tick_fetch_interval:
            return np.array([])
        
        self.last_tick_fetch = current_time
        
        if server_time is None:
            server_time = self.get_server_timestamp()
            if server_time is None:
                return np.array([])
        
        ticks = mt5.copy_ticks_from(self.symbol, server_time, 5000, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            return np.array([])
        
        timestamps = ticks['time_msc'] / 1000.0
        return timestamps[1:] if len(timestamps) > 1 else timestamps

    def on_tick(self) -> None:
        """Process new ticks efficiently."""
        now = self.get_server_timestamp()
        if now is None:
            return
        
        if len(self.tick_timestamps) == 0:
            timestamps = self.get_tick_timestamps_batch(now)
        else:
            timestamps = self.get_tick_timestamps_batch(self.tick_timestamps[-1])
        
        if len(timestamps) > 0:
            self.tick_timestamps.extend(timestamps)
            self.cleanup(now)

    def cleanup(self, now: float) -> None:
        """Remove old ticks efficiently."""
        cutoff = now - 30
        while self.tick_timestamps and self.tick_timestamps[0] < cutoff:
            self.tick_timestamps.popleft()

    def update_history(self) -> None:
        """Update density history with running sum optimization."""
        now = self.get_server_timestamp()
        if now is None:
            return
            
        self.cleanup(now)
        
        current_density = len(self.tick_timestamps) / 30.0
        
        if len(self.density_history) == self.density_history.maxlen:
            self.density_sum -= self.density_history[0]
        
        self.density_history.append(current_density)
        self.density_sum += current_density
        self.last_update = t_mod.time()

    def is_high_velocity(self) -> bool:
        """Check for high velocity using cached average."""
        if len(self.density_history) < 10:
            return False
        if t_mod.time() - self.last_update < 60 * 10:
            return False
        
        current_density = len(self.tick_timestamps) / 30.0
        avg_density = self.density_sum / len(self.density_history)
        
        if avg_density == 0:
            return False
        
        multiplier = self.config['entry_conditions']['velocity_multiplier']
        is_high = current_density > (avg_density * multiplier)
        
        if is_high:
            self.logger.debug(f"High velocity: {current_density:.2f} > {avg_density:.2f} × {multiplier}")
        
        return is_high
    
    def get_current_metrics(self) -> Dict[str, float]:
        """Get current velocity metrics for monitoring."""
        return {
            'current_density': len(self.tick_timestamps) / 30.0,
            'avg_density': self.density_sum / max(len(self.density_history), 1),
            'history_size': len(self.density_history),
            'ticks_count': len(self.tick_timestamps)
        }


class SymbolInfoCache:
    """Cache for symbol information to reduce MT5 API calls."""
    
    def __init__(self, symbol: str, logger: logging.Logger):
        self.symbol = symbol
        self.logger = logger
        self._cache: Dict[str, Any] = {}
        self._last_update: float = 0
        self._update_interval: float = 300
        
    def get_info(self) -> Optional[Dict[str, Any]]:
        """Get symbol information with caching."""
        current_time = t_mod.time()
        
        if (current_time - self._last_update) > self._update_interval or not self._cache:
            info = mt5.symbol_info(self.symbol)
            if info is None:
                self.logger.warning(f"Failed to get symbol info for {self.symbol}")
                return None
            
            self._cache = {
                'contract_size': info.trade_contract_size,
                'digits': info.digits,
                'volume_min': info.volume_min,
                'volume_max': info.volume_max,
                'volume_step': info.volume_step,
                'filling_mode': info.filling_mode,
                'spread': info.spread,
                'trade_mode': info.trade_mode,
                'swap_mode': info.swap_mode
            }
            self._last_update = current_time
            self.logger.debug(f"Updated symbol info cache for {self.symbol}")
        
        return self._cache
    
    def get_contract_size(self) -> Optional[float]:
        """Get contract size from cache."""
        info = self.get_info()
        return info['contract_size'] if info else None
    
    def clear_cache(self):
        """Clear the cache."""
        self._cache.clear()
        self._last_update = 0

# -------------------------------------------------------------------
# OPTIMIZED STRATEGY STATE
# -------------------------------------------------------------------
class OptimizedStrategyState:
    """Optimized state management with caching and efficient updates."""
    
    def __init__(self, symbol: str, config_manager: ConfigurationManager):
        self.symbol = symbol
        self.config_manager = config_manager
        self.config = config_manager.config
        
        self.current_date = None
        self.bias = "straddle"
        self.ghost_high = None
        self.ghost_low = None
        self.daily_open_price = None
        self.touched_opposite = False
        
        self.in_trade = False
        self.max_pnl = 0.0
        self.entry_price = 0.0
        self.direction = None
        self.logger = logging.getLogger("trading_bot.state")
        
        self._buffer_cache: Dict[float, float] = {}
        
        # Register for config updates
        config_manager.register_callback(self._on_config_changed)

    def _on_config_changed(self, new_config: Dict[str, Any]):
        """Handle configuration changes."""
        self.config = new_config
        self._buffer_cache.clear()
        self.logger.debug("Strategy state configuration updated")

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
        self.entry_price = 0.0
        self.direction = None
        self._buffer_cache.clear()
        self.logger.debug(f"State reset for {new_date}")

    def close_trade(self) -> None:
        """Clean up trade state after position closure."""
        self.logger.info(f"Trade closed. Bias: {self.bias}")
        self.touched_opposite = False
        self.in_trade = False
        self.max_pnl = 0.0
        self.entry_price = 0.0
        self.direction = None

    def calculate_buffer(self, contract_size: float) -> float:
        """Calculate buffer with caching."""
        if contract_size == 0:
            return 0.0
        
        if contract_size not in self._buffer_cache:
            buffer = self.config['entry_conditions']['buffer_pips'] / contract_size
            self._buffer_cache[contract_size] = buffer
        
        return self._buffer_cache[contract_size]

    def update_trade_status(self, has_position: bool) -> None:
        """Update trade status efficiently."""
        if self.in_trade and not has_position:
            self.close_trade()
        elif not self.in_trade and has_position:
            self.in_trade = True
            self.logger.info(f"Trade status updated: in_trade={self.in_trade}")

# -------------------------------------------------------------------
# OPTIMIZED CORE LOGIC
# -------------------------------------------------------------------
@retry(max_attempts=2, delay=1.0, exceptions=(MT5OperationError,))
def calculate_daily_bias(symbol: str, config: Dict[str, Any]) -> str:
    """
    Optimized daily bias calculation.
    """
    logger = logging.getLogger("trading_bot.bias")
    
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
    if rc >= config['bias_filter']['buy_threshold']:
        bias = "buy"
    elif rc <= config['bias_filter']['sell_threshold']:
        bias = "sell"
    
    logger.info(f"Daily bias: {bias} (rc={rc:.3f})")
    return bias


@retry(max_attempts=2, delay=1.0, exceptions=(MT5OperationError,))
def get_ghost_range(symbol: str, today_date: date, session_times: Dict[str, datetime]) -> Tuple[Optional[float], Optional[float]]:
    """
    Optimized ghost range calculation.
    """
    logger = logging.getLogger("trading_bot.ghost_range")
    
    start_dt = session_times['ghost_start']
    end_dt = session_times['ghost_end']
    
    logger.debug(f"Fetching ghost range: {start_dt} to {end_dt}")
    
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        raise MT5OperationError(f"No data returned for ghost range: {symbol}")

    g_min = float(np.min(rates['low']))
    g_max = float(np.max(rates['high']))
    
    logger.info(f"Ghost range: {g_min:.5f} - {g_max:.5f}")
    return g_min, g_max


@retry(max_attempts=2, delay=1.0, exceptions=(MT5OperationError,))
def get_frankfurt_open(symbol: str, today_date: date, session_times: Dict[str, datetime]) -> Optional[float]:
    """Optimized Frankfurt open price fetch."""
    logger = logging.getLogger("trading_bot.open_price")
    
    start_dt = session_times['day_open']
    
    logger.debug(f"Fetching Frankfurt open at {start_dt}")
    
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start_dt, start_dt + timedelta(minutes=1))
    if rates is None or len(rates) == 0:
        raise MT5OperationError(f"No data returned for Frankfurt open: {symbol}")
    
    open_price = float(rates[0][1])
    logger.info(f"Frankfurt open price: {open_price:.5f}")
    return open_price


def get_filling_type_from_cache(symbol_info_cache: SymbolInfoCache) -> Optional[int]:
    """Get filling type from cached symbol info."""
    info = symbol_info_cache.get_info()
    if info is None:
        return None
    
    filling_mode = info['filling_mode']
    
    if filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    elif filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    else:
        return mt5.ORDER_FILLING_RETURN


def validate_order_params_with_cache(symbol_info_cache: SymbolInfoCache, config: Dict[str, Any], direction: str, sl_pips: float) -> List[str]:
    """Validate order parameters using cached symbol info."""
    errors = []
    
    info = symbol_info_cache.get_info()
    if info is None:
        errors.append("Cannot get symbol info")
        return errors
    
    if direction not in ['buy', 'sell']:
        errors.append(f"Invalid direction: {direction}")
    
    if sl_pips <= 0:
        errors.append(f"Stop loss must be positive: {sl_pips}")
    
    volume = config['trading']['volume']
    if volume <= 0:
        errors.append(f"Volume must be positive: {volume}")
    elif volume < info['volume_min']:
        errors.append(f"Volume below minimum: {volume} < {info['volume_min']}")
    elif volume > info['volume_max']:
        errors.append(f"Volume above maximum: {volume} > {info['volume_max']}")
    elif volume % info['volume_step'] != 0:
        errors.append(f"Volume not a multiple of step: {volume} % {info['volume_step']} != 0")
    
    return errors


@retry(max_attempts=2, delay=1.0, exceptions=(OrderExecutionError,))
def execute_trade_with_cache(
    symbol: str, 
    symbol_info_cache: SymbolInfoCache, 
    config: Dict[str, Any],
    direction: str, 
    sl_pips: float
) -> Tuple[bool, float]:
    """Optimized trade execution using cached symbol info."""
    logger = logging.getLogger("trading_bot.order")
    
    # Validate parameters
    validation_errors = validate_order_params_with_cache(symbol_info_cache, config, direction, sl_pips)
    if validation_errors:
        for error in validation_errors:
            logger.error(f"Order validation failed: {error}")
        return False, 0.0
    
    # Get tick
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        error = mt5.last_error()
        raise OrderExecutionError(f"Cannot get tick for {symbol}: {error}")
    
    # Get symbol info
    info = symbol_info_cache.get_info()
    if info is None:
        raise OrderExecutionError(f"Cannot get symbol info for {symbol}")
    
    # Get filling type
    filling = get_filling_type_from_cache(symbol_info_cache)
    if filling is None:
        raise OrderExecutionError(f"Cannot determine filling type for {symbol}")
    
    # Calculate prices
    contract_size = info['contract_size']
    sl_points = sl_pips / contract_size
    
    if direction == 'buy':
        price = tick.ask
        sl_price = price - sl_points
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        sl_price = price + sl_points
        order_type = mt5.ORDER_TYPE_SELL
    
    # Round prices
    digits = info['digits']
    price = round(price, digits)
    sl_price = round(sl_price, digits)
    
    # Prepare request
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": config['trading']['volume'],
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "deviation": config['trading']['deviation'],
        "magic": config['trading']['magic_number'],
        "comment": "LiveDemo_Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    
    logger.info(f"Sending {direction} order: price={price:.5f}, sl={sl_price:.5f}")
    
    result = mt5.order_send(request)
    
    if result is None:
        raise OrderExecutionError("No response from MT5")
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise OrderExecutionError(f"Order failed: {result.comment} (retcode: {result.retcode})")
    
    logger.info(f"Trade executed: {direction} at {result.price:.5f}, ticket: {result.order}")
    return True, result.price


def safe_mt5_call(func: Callable, *args, **kwargs) -> Any:
    """
    Optimized safe MT5 call with connection checking.
    """
    global connection_manager, logger
    
    if connection_manager is None or logger is None:
        return None
    
    if not connection_manager.ensure_connection():
        logger.error("Cannot perform MT5 operation: connection not available")
        return None
    
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.error(f"MT5 operation failed: {e}")
        connection_manager.connected = False
        return None

# -------------------------------------------------------------------
# MAIN FUNCTION
# -------------------------------------------------------------------
def main():
    """Main function with configuration management."""
    parser = argparse.ArgumentParser(description="Configuration-aware trading bot")
    parser.add_argument("symbol", help="Symbol to be traded")
    parser.add_argument("--config", help="Path to configuration file (YAML/JSON)")
    parser.add_argument("--log-level", default=None,
                       choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                       help="Override logging level")
    parser.add_argument("--validate-config", action="store_true",
                       help="Validate configuration and exit")
    parser.add_argument("--generate-config", metavar="FILE",
                       help="Generate configuration template file")
    parser.add_argument("--save-config", metavar="FILE",
                       help="Save current configuration to file")
    parser.add_argument("--hot-reload", action="store_true",
                       help="Enable hot-reload of configuration file")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    
    # Initialize configuration manager
    config_manager = ConfigurationManager(args.config)
    
    # Generate config template if requested
    if args.generate_config:
        # Create a comprehensive template
        template = {
            "description": "Trading Bot Configuration",
            "bias_filter": {
                "buy_threshold": 0.6,
                "sell_threshold": 0.4
            },
            "entry_conditions": {
                "buffer_pips": 10.0,
                "velocity_multiplier": 2.0,
                "lookback_seconds": 3600
            },
            "risk_management": {
                "initial_sl_pips": 50.0,
                "trailing_stages": [
                    {"min_profit": 0, "max_profit": 30, "retention": -1},
                    {"min_profit": 30, "max_profit": 60, "retention": 0.5},
                    {"min_profit": 60, "max_profit": 90, "retention": 0.7},
                    {"min_profit": 90, "max_profit": 120, "retention": 0.8},
                    {"min_profit": 120, "max_profit": 150, "retention": 0.9},
                    {"min_profit": 150, "retention": 0.95}
                ]
            },
            "session": {
                "day_open": "09:00",
                "ghost_start": "08:00",
                "ghost_end": "08:30",
                "trading_start": "10:00",
                "trading_end": "17:00"
            },
            "timezones": {
                "winter_tz": "Europe/Athens",
                "summer_tz": "Asia/Baghdad",
                "local_tz": "Europe/Berlin"
            },
            "performance": {
                "tick_processing_interval": 0.1,
                "velocity_update_interval": 1.0,
                "position_check_interval": 2.0,
                "outside_session_sleep": 60.0,
                "cache_ttl_hours": 1.0
            },
            "logging": {
                "level": "INFO",
                "enable_file_logging": True,
                "max_file_size_mb": 10,
                "backup_count": 5,
                "separate_error_logs": True,
                "log_directory": "logs"
            },
            "trading": {
                "volume": 0.01,
                "deviation": 10,
                "magic_number": 1234569
            }
        }
        
        with open(args.generate_config, 'w') as f:
            yaml.dump(template, f, default_flow_style=False)
        
        print(f"Configuration template generated: {args.generate_config}")
        return
    
    # Validate configuration if requested
    if args.validate_config:
        errors = ConfigValidator.validate_config(config_manager.config)
        if errors:
            print("Configuration validation failed:")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)
        else:
            print("Configuration validation successful!")
            return
    
    # Override logging level if specified
    if args.log_level:
        config_manager.config['logging']['level'] = args.log_level
    
    # Setup logging
    global logger
    logger = TradingLogger.setup_logging(config_manager, symbol)
    logger.info(f"Starting trading bot for {symbol}")
    logger.info(f"Using configuration: {args.config if args.config else 'default'}")
    
    # Save config if requested
    if args.save_config:
        if config_manager.save_to_file(args.save_config):
            logger.info(f"Configuration saved to {args.save_config}")
        else:
            logger.error(f"Failed to save configuration to {args.save_config}")
    
    # Initialize connection manager
    global connection_manager
    connection_manager = MT5ConnectionManager(config_manager, logger)
    
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
    
    # Initialize caches and managers
    symbol_info_cache = SymbolInfoCache(symbol, logger)
    session_time_manager = SessionTimeManager(config_manager, symbol, logger)
    
    # Get contract size
    contract_size = symbol_info.trade_contract_size
    logger.info(f"Symbol {symbol} selected, contract size: {contract_size}")
    
    # Initialize state and monitoring
    state = OptimizedStrategyState(symbol, config_manager)
    velocity = OptimizedVelocityMonitor(symbol, config_manager)
    
    config = config_manager.config
    logger.info(f"Trading session: {config['session']['trading_start']} to {config['session']['trading_end']}")
    logger.info(f"Risk management: {config['risk_management']['initial_sl_pips']} pip initial SL")
    
    # Performance tracking
    last_velocity_update = t_mod.time()
    last_position_check = t_mod.time()
    last_metrics_log = t_mod.time()
    loop_iterations = 0
    iteration_start_time = t_mod.time()
    
    # Performance configuration
    perf_config = config['performance']
    tick_interval = perf_config['tick_processing_interval']
    velocity_interval = perf_config['velocity_update_interval']
    position_check_interval = perf_config['position_check_interval']
    outside_session_sleep = perf_config['outside_session_sleep']
    
    try:
        while not signal_handler.should_shutdown():
            loop_iterations += 1
            iteration_start_time = t_mod.time()
            
            # Check for configuration updates if hot-reload enabled
            if args.hot_reload:
                config_manager.check_for_updates()
            
            # Check connection
            if not connection_manager.ensure_connection():
                logger.error("Lost connection to MT5")
                t_mod.sleep(5)
                continue
            
            # Get current time
            tick_info = safe_mt5_call(mt5.symbol_info_tick, symbol)
            if tick_info is None:
                logger.debug(f"No tick data for {symbol}")
                t_mod.sleep(tick_interval)
                continue
            
            # Get server time from tick
            server_time = pd.to_datetime(tick_info.time, unit='s')
            server_tz = TimeCache.get_timezone(server_time.date())
            server_time = server_tz.localize(server_time) if server_time.tzinfo is None else server_time.astimezone(server_tz)
            
            # Check if we're in trading hours
            if not session_time_manager.is_in_trading_hours(server_time):
                logger.debug(f"Outside trading hours: {server_time}")
                t_mod.sleep(outside_session_sleep)
                continue
            
            today_date = server_time.date()
            
            # New Day Logic
            if state.current_date != today_date:
                state.reset(today_date)
                bias = safe_mt5_call(calculate_daily_bias, symbol, config)
                if bias is not None:
                    state.bias = bias
                    logger.info(f"Daily bias set to: {state.bias}")
            
            # Process tick data
            velocity.on_tick()
            
            # Update velocity history at fixed interval
            current_time = t_mod.time()
            if current_time - last_velocity_update >= velocity_interval:
                velocity.update_history()
                last_velocity_update = current_time
                
                # Log metrics every 5 minutes
                if current_time - last_metrics_log >= 300:
                    metrics = velocity.get_current_metrics()
                    logger.info(f"Velocity metrics: {metrics}")
                    last_metrics_log = current_time
            
            # Get session times for today
            session_times = session_time_manager.get_session_times(today_date)
            
            # Capture Ghost Range (once after ghost_end)
            if state.ghost_high is None and session_time_manager.should_check_ghost_range(server_time):
                ghost_range = safe_mt5_call(get_ghost_range, symbol, today_date, session_times)
                if ghost_range is not None:
                    g_min, g_max = ghost_range
                    state.ghost_low = g_min
                    state.ghost_high = g_max
                    logger.info(f"Ghost range locked: {g_min:.5f} - {g_max:.5f}")
            
            # Capture Daily Open
            if state.daily_open_price is None and session_time_manager.should_check_market_open(server_time):
                open_price = safe_mt5_call(get_frankfurt_open, symbol, today_date, session_times)
                if open_price is not None:
                    state.daily_open_price = open_price
                    logger.info(f"Market open price: {open_price:.5f}")
            
            # Check positions at reduced frequency
            if current_time - last_position_check >= position_check_interval:
                positions = safe_mt5_call(mt5.positions_get, symbol=symbol)
                last_position_check = current_time
            else:
                positions = []
            
            if positions is None:
                positions = []
            
            my_pos = [p for p in positions if p.magic == config['trading']['magic_number']]
            open_pos = len(my_pos) > 0
            
            # Update trade status
            state.update_trade_status(open_pos)
            
            # Handle open positions
            if state.in_trade and my_pos:
                pos = my_pos[0]
                
                # Mandatory Close (Time)
                if server_time >= session_times['session_end']:
                    logger.info(f"Mandatory close triggered: {server_time}")
                    safe_mt5_call(mt5.positions_close, pos.ticket)
                    continue
                
                # Trailing Stop Logic
                if pos.type == mt5.ORDER_TYPE_BUY:
                    current_profit_points = (tick_info.bid - pos.price_open) * contract_size
                else:
                    current_profit_points = (pos.price_open - tick_info.ask) * contract_size
                
                if current_profit_points > state.max_pnl:
                    state.max_pnl = current_profit_points
                    logger.debug(f"New max profit: {state.max_pnl:.2f} pips")
                
                # Apply trailing stop
                for stage in config['risk_management']['trailing_stages']:
                    if state.max_pnl >= stage['min_profit'] and stage['retention'] != -1:
                        trail_dist = state.max_pnl * stage['retention'] / contract_size
                        
                        if pos.type == mt5.ORDER_TYPE_BUY:
                            new_sl = pos.price_open + trail_dist
                            should_modify = (pos.sl == 0 or new_sl > pos.sl)
                        else:
                            new_sl = pos.price_open - trail_dist
                            should_modify = (pos.sl == 0 or new_sl < pos.sl)
                        
                        if should_modify:
                            logger.info(f"Modifying SL to {new_sl:.5f}")
                            safe_mt5_call(mt5.order_modify, pos.ticket, sl=new_sl)
                            break
            
            # Entry Logic
            elif state.bias != "straddle" and state.ghost_high is not None and state.daily_open_price is not None:
                if session_time_manager.is_in_trading_hours(server_time):
                    buffer = state.calculate_buffer(contract_size)
                    
                    # BUY LOGIC
                    if state.bias == 'buy':
                        lower_target = state.ghost_low + buffer
                        upper_target = state.ghost_high - buffer
                        
                        if tick_info.ask <= lower_target and not state.touched_opposite:
                            logger.info(f"Trap: Touched opposite low at {lower_target:.5f}")
                            state.touched_opposite = True
                        
                        if (state.touched_opposite and 
                            tick_info.ask >= upper_target and 
                            tick_info.ask > state.daily_open_price and
                            velocity.is_high_velocity()):
                            
                            logger.info(f"Buy entry: Price={tick_info.ask:.5f} >= {upper_target:.5f}")
                            success, price = safe_mt5_call(
                                execute_trade_with_cache, symbol, symbol_info_cache, config, 'buy', 
                                config['risk_management']['initial_sl_pips']
                            )
                            if success:
                                state.in_trade = True
                                state.entry_price = price
                                state.direction = 'buy'
                                state.touched_opposite = False
                    
                    # SELL LOGIC
                    elif state.bias == 'sell':
                        upper_target = state.ghost_high - buffer
                        lower_target = state.ghost_low + buffer
                        
                        if tick_info.bid >= upper_target and not state.touched_opposite:
                            logger.info(f"Trap: Touched opposite high at {upper_target:.5f}")
                            state.touched_opposite = True
                        
                        if (state.touched_opposite and 
                            tick_info.bid <= lower_target and 
                            tick_info.bid < state.daily_open_price and
                            velocity.is_high_velocity()):
                            
                            logger.info(f"Sell entry: Price={tick_info.bid:.5f} <= {lower_target:.5f}")
                            success, price = safe_mt5_call(
                                execute_trade_with_cache, symbol, symbol_info_cache, config, 'sell',
                                config['risk_management']['initial_sl_pips']
                            )
                            if success:
                                state.in_trade = True
                                state.entry_price = price
                                state.direction = 'sell'
                                state.touched_opposite = False
            
            # Adaptive sleep based on processing time
            iteration_time = t_mod.time() - iteration_start_time
            sleep_time = max(0.0, tick_interval - iteration_time)
            if sleep_time > 0:
                t_mod.sleep(sleep_time)
            
            # Log performance every 1000 iterations
            if loop_iterations % 1000 == 0:
                avg_iteration_time = iteration_time / 1000
                logger.debug(f"Performance: {avg_iteration_time:.4f}s per iteration")
                iteration_start_time = t_mod.time()
    
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        logger.info("Initiating graceful shutdown...")
        
        # Close positions
        try:
            positions = safe_mt5_call(mt5.positions_get, symbol=symbol)
            if positions:
                logger.info(f"Closing {len(positions)} positions...")
                for pos in positions:
                    if pos.magic == config['trading']['magic_number']:
                        safe_mt5_call(mt5.positions_close, pos.ticket)
        except Exception as e:
            logger.error(f"Error closing positions: {e}")
        
        # Shutdown
        if connection_manager:
            connection_manager.shutdown()
        
        logger.info(f"Trading bot stopped. Total iterations: {loop_iterations}")


if __name__ == "__main__":
    main()