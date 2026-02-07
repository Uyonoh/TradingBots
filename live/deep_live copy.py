"""
MT5 Trading Bot - Optimized for performance with structured logging and configuration management.

This module provides a high-frequency trading bot for MetaTrader 5 with:
- Performance optimizations for real-time trading
- Structured logging (console + file)
- Comprehensive error handling and recovery
- Configuration management with hot-reload
- Type hints and comprehensive documentation
"""

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
from typing import Optional, Dict, Any, List, Tuple, Callable, Deque, Union
from functools import wraps, lru_cache
from pathlib import Path
import yaml
from dataclasses import dataclass, field
from enum import Enum
import inspect
import hashlib
from abc import ABC, abstractmethod

dotenv.load_dotenv()

# -------------------------------------------------------------------
# TYPE ALIASES
# -------------------------------------------------------------------
PositionType = Union[mt5.TradePosition, Dict[str, Any]]
TickData = Union[mt5.Tick, Dict[str, Any]]
SymbolInfo = Union[mt5.SymbolInfo, Dict[str, Any]]
OrderResult = Union[mt5.OrderSendResult, Dict[str, Any]]
Timestamp = Union[float, int, datetime]

# -------------------------------------------------------------------
# ENUMS
# -------------------------------------------------------------------
class TradeBias(str, Enum):
    """Enum for trade bias direction."""
    BUY = "buy"
    SELL = "sell"
    STRADDLE = "straddle"


class LogLevel(str, Enum):
    """Enum for log levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class OrderStatus(str, Enum):
    """Enum for order status."""
    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# -------------------------------------------------------------------
# DATA CLASSES
# -------------------------------------------------------------------
@dataclass
class TrailingStage:
    """Data class for a trailing stop stage."""
    min_profit: float
    max_profit: Optional[float] = None
    retention: float = -1.0
    
    def __post_init__(self):
        """Validate after initialization."""
        if self.max_profit is not None and self.max_profit <= self.min_profit:
            raise ValueError(f"max_profit ({self.max_profit}) must be greater than min_profit ({self.min_profit})")
        if not -1.0 <= self.retention <= 1.0:
            raise ValueError(f"retention ({self.retention}) must be between -1.0 and 1.0")


@dataclass
class VelocityMetrics:
    """Data class for velocity monitoring metrics."""
    current_density: float = 0.0
    average_density: float = 0.0
    history_size: int = 0
    ticks_count: int = 0
    is_high_velocity: bool = False


@dataclass
class TradeState:
    """Data class for current trade state."""
    in_trade: bool = False
    direction: Optional[TradeBias] = None
    entry_price: float = 0.0
    max_pnl: float = 0.0
    position_ticket: Optional[int] = None
    touched_opposite: bool = False


@dataclass
class DayState:
    """Data class for daily trading state."""
    current_date: Optional[date] = None
    bias: TradeBias = TradeBias.STRADDLE
    ghost_low: Optional[float] = None
    ghost_high: Optional[float] = None
    daily_open_price: Optional[float] = None


@dataclass
class PerformanceMetrics:
    """Data class for performance monitoring."""
    loop_iterations: int = 0
    avg_iteration_time: float = 0.0
    mt5_calls_per_second: float = 0.0
    memory_usage_mb: float = 0.0
    velocity_updates: int = 0
    positions_checked: int = 0


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


class ValidationError(TradingError):
    """Raised when validation fails."""
    pass


# -------------------------------------------------------------------
# DECORATORS
# -------------------------------------------------------------------
def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Exception, ...] = (Exception,),
    logger: Optional[logging.Logger] = None
) -> Callable:
    """
    Retry decorator with exponential backoff.
    
    Args:
        max_attempts: Maximum number of retry attempts
        delay: Initial delay between attempts in seconds
        backoff: Multiplier for delay after each attempt
        exceptions: Tuple of exceptions to catch and retry on
        logger: Logger instance for logging retry attempts
        
    Returns:
        Decorated function with retry logic
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
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


def log_execution_time(logger: Optional[logging.Logger] = None) -> Callable:
    """
    Decorator to log function execution time.
    
    Args:
        logger: Logger instance for logging execution time
        
    Returns:
        Decorated function with execution time logging
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = t_mod.time()
            result = func(*args, **kwargs)
            execution_time = t_mod.time() - start_time
            
            if logger and execution_time > 0.1:  # Only log if it takes > 100ms
                logger.debug(f"{func.__name__} executed in {execution_time:.3f}s")
            
            return result
        return wrapper
    return decorator


def validate_config(func: Callable) -> Callable:
    """
    Decorator to validate configuration before function execution.
    
    Args:
        func: Function to decorate
        
    Returns:
        Decorated function with configuration validation
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs) -> Any:
        if not hasattr(self, 'config_manager') or not self.config_manager.config:
            raise ConfigurationError("Configuration not loaded")
        return func(self, *args, **kwargs)
    return wrapper


# -------------------------------------------------------------------
# CONFIGURATION MANAGEMENT
# -------------------------------------------------------------------
class ConfigValidator:
    """Validates trading configuration with comprehensive checks."""
    
    @staticmethod
    def validate_config(config: Dict[str, Any]) -> List[str]:
        """
        Validate configuration and return list of errors.
        
        Args:
            config: Configuration dictionary to validate
            
        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        
        # Check required top-level sections
        required_sections = ['bias_filter', 'entry_conditions', 'risk_management', 'session']
        for section in required_sections:
            if section not in config:
                errors.append(f"Missing required section: '{section}'")
        
        if errors:  # Can't validate further if sections missing
            return errors
        
        # Validate bias_filter
        errors.extend(ConfigValidator._validate_bias_filter(config['bias_filter']))
        
        # Validate entry_conditions
        errors.extend(ConfigValidator._validate_entry_conditions(config['entry_conditions']))
        
        # Validate risk_management
        errors.extend(ConfigValidator._validate_risk_management(config['risk_management']))
        
        # Validate session
        errors.extend(ConfigValidator._validate_session(config['session']))
        
        # Validate optional sections if present
        if 'timezones' in config:
            errors.extend(ConfigValidator._validate_timezones(config['timezones']))
        
        if 'performance' in config:
            errors.extend(ConfigValidator._validate_performance(config['performance']))
        
        if 'logging' in config:
            errors.extend(ConfigValidator._validate_logging(config['logging']))
        
        if 'trading' in config:
            errors.extend(ConfigValidator._validate_trading_params(config['trading']))
        
        return errors
    
    @staticmethod
    def _validate_bias_filter(bias_filter: Dict[str, Any]) -> List[str]:
        """Validate bias filter configuration."""
        errors = []
        
        if 'buy_threshold' not in bias_filter:
            errors.append("Missing 'bias_filter.buy_threshold'")
        elif not isinstance(bias_filter['buy_threshold'], (int, float)):
            errors.append("'bias_filter.buy_threshold' must be a number")
        elif not 0.5 <= bias_filter['buy_threshold'] <= 1.0:
            errors.append("'bias_filter.buy_threshold' must be between 0.5 and 1.0")
        
        if 'sell_threshold' not in bias_filter:
            errors.append("Missing 'bias_filter.sell_threshold'")
        elif not isinstance(bias_filter['sell_threshold'], (int, float)):
            errors.append("'bias_filter.sell_threshold' must be a number")
        elif not 0.0 <= bias_filter['sell_threshold'] <= 0.5:
            errors.append("'bias_filter.sell_threshold' must be between 0.0 and 0.5")
        
        if 'buy_threshold' in bias_filter and 'sell_threshold' in bias_filter:
            if bias_filter['buy_threshold'] <= bias_filter['sell_threshold']:
                errors.append("'buy_threshold' must be greater than 'sell_threshold'")
        
        return errors
    
    @staticmethod
    def _validate_entry_conditions(entry_conditions: Dict[str, Any]) -> List[str]:
        """Validate entry conditions configuration."""
        errors = []
        
        if 'buffer_pips' not in entry_conditions:
            errors.append("Missing 'entry_conditions.buffer_pips'")
        elif not isinstance(entry_conditions['buffer_pips'], (int, float)):
            errors.append("'entry_conditions.buffer_pips' must be a number")
        elif entry_conditions['buffer_pips'] <= 0:
            errors.append("'entry_conditions.buffer_pips' must be positive")
        
        if 'velocity_multiplier' not in entry_conditions:
            errors.append("Missing 'entry_conditions.velocity_multiplier'")
        elif not isinstance(entry_conditions['velocity_multiplier'], (int, float)):
            errors.append("'entry_conditions.velocity_multiplier' must be a number")
        elif entry_conditions['velocity_multiplier'] < 1.0:
            errors.append("'entry_conditions.velocity_multiplier' must be >= 1.0")
        
        if 'lookback_seconds' not in entry_conditions:
            errors.append("Missing 'entry_conditions.lookback_seconds'")
        elif not isinstance(entry_conditions['lookback_seconds'], int):
            errors.append("'entry_conditions.lookback_seconds' must be an integer")
        elif entry_conditions['lookback_seconds'] < 60:
            errors.append("'entry_conditions.lookback_seconds' must be at least 60")
        
        return errors
    
    @staticmethod
    def _validate_risk_management(risk_management: Dict[str, Any]) -> List[str]:
        """Validate risk management configuration."""
        errors = []
        
        if 'initial_sl_pips' not in risk_management:
            errors.append("Missing 'risk_management.initial_sl_pips'")
        elif not isinstance(risk_management['initial_sl_pips'], (int, float)):
            errors.append("'risk_management.initial_sl_pips' must be a number")
        elif risk_management['initial_sl_pips'] <= 0:
            errors.append("'risk_management.initial_sl_pips' must be positive")
        
        if 'trailing_stages' not in risk_management:
            errors.append("Missing 'risk_management.trailing_stages'")
        elif not isinstance(risk_management['trailing_stages'], list):
            errors.append("'risk_management.trailing_stages' must be a list")
        elif len(risk_management['trailing_stages']) == 0:
            errors.append("'risk_management.trailing_stages' must not be empty")
        else:
            # Validate each trailing stage
            last_max = -float('inf')
            for i, stage in enumerate(risk_management['trailing_stages']):
                if not isinstance(stage, dict):
                    errors.append(f"Stage {i}: must be a dictionary")
                    continue
                
                if 'min_profit' not in stage:
                    errors.append(f"Stage {i}: missing 'min_profit'")
                elif not isinstance(stage['min_profit'], (int, float)):
                    errors.append(f"Stage {i}: 'min_profit' must be a number")
                elif stage['min_profit'] < 0:
                    errors.append(f"Stage {i}: 'min_profit' must be >= 0")
                
                if 'retention' not in stage:
                    errors.append(f"Stage {i}: missing 'retention'")
                elif not isinstance(stage['retention'], (int, float)):
                    errors.append(f"Stage {i}: 'retention' must be a number")
                elif not -1.0 <= stage['retention'] <= 1.0:
                    errors.append(f"Stage {i}: 'retention' must be between -1.0 and 1.0")
                
                if stage.get('min_profit', 0) <= last_max:
                    errors.append(f"Stage {i}: 'min_profit' must be greater than previous stage's max")
                
                if 'max_profit' in stage:
                    if not isinstance(stage['max_profit'], (int, float)):
                        errors.append(f"Stage {i}: 'max_profit' must be a number")
                    elif stage['max_profit'] <= stage.get('min_profit', 0):
                        errors.append(f"Stage {i}: 'max_profit' must be greater than 'min_profit'")
                    last_max = stage['max_profit']
                elif i != len(risk_management['trailing_stages']) - 1:
                    errors.append(f"Stage {i}: only last stage can omit 'max_profit'")
        
        return errors
    
    @staticmethod
    def _validate_session(session: Dict[str, Any]) -> List[str]:
        """Validate session timing configuration."""
        errors = []
        
        time_fields = ['day_open', 'ghost_start', 'ghost_end', 'trading_start', 'trading_end']
        for field in time_fields:
            if field not in session:
                errors.append(f"Missing 'session.{field}'")
            elif not ConfigValidator._is_valid_time(session[field]):
                errors.append(f"'session.{field}' must be in HH:MM format")
        
        # Validate time ranges
        if all(f in session for f in ['ghost_start', 'ghost_end']):
            if ConfigValidator._time_to_minutes(session['ghost_start']) >= ConfigValidator._time_to_minutes(session['ghost_end']):
                errors.append("'ghost_end' must be after 'ghost_start'")
        
        if all(f in session for f in ['trading_start', 'trading_end']):
            if ConfigValidator._time_to_minutes(session['trading_start']) >= ConfigValidator._time_to_minutes(session['trading_end']):
                errors.append("'trading_end' must be after 'trading_start'")
        
        return errors
    
    @staticmethod
    def _validate_timezones(timezones: Dict[str, Any]) -> List[str]:
        """Validate timezone configuration."""
        errors = []
        
        tz_fields = ['winter_tz', 'summer_tz', 'local_tz']
        for field in tz_fields:
            if field in timezones:
                try:
                    pytz.timezone(timezones[field])
                except pytz.UnknownTimeZoneError:
                    errors.append(f"Unknown timezone in 'timezones.{field}': {timezones[field]}")
        
        return errors
    
    @staticmethod
    def _validate_performance(performance: Dict[str, Any]) -> List[str]:
        """Validate performance configuration."""
        errors = []
        
        perf_ranges = {
            'tick_processing_interval': (0.001, 1.0),
            'velocity_update_interval': (0.1, 10.0),
            'position_check_interval': (0.5, 30.0),
            'outside_session_sleep': (1.0, 300.0),
            'cache_ttl_hours': (0.1, 24.0)
        }
        
        for setting, (min_val, max_val) in perf_ranges.items():
            if setting in performance:
                if not isinstance(performance[setting], (int, float)):
                    errors.append(f"'performance.{setting}' must be a number")
                elif not min_val <= performance[setting] <= max_val:
                    errors.append(f"'performance.{setting}' must be between {min_val} and {max_val}")
        
        return errors
    
    @staticmethod
    def _validate_logging(logging_config: Dict[str, Any]) -> List[str]:
        """Validate logging configuration."""
        errors = []
        
        if 'level' in logging_config and logging_config['level'] not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
            errors.append("'logging.level' must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        
        if 'max_file_size_mb' in logging_config:
            if not isinstance(logging_config['max_file_size_mb'], int):
                errors.append("'logging.max_file_size_mb' must be an integer")
            elif logging_config['max_file_size_mb'] <= 0:
                errors.append("'logging.max_file_size_mb' must be positive")
        
        if 'backup_count' in logging_config:
            if not isinstance(logging_config['backup_count'], int):
                errors.append("'logging.backup_count' must be an integer")
            elif logging_config['backup_count'] < 0:
                errors.append("'logging.backup_count' must be >= 0")
        
        return errors
    
    @staticmethod
    def _validate_trading_params(trading: Dict[str, Any]) -> List[str]:
        """Validate trading parameters."""
        errors = []
        
        if 'volume' in trading:
            if not isinstance(trading['volume'], (int, float)):
                errors.append("'trading.volume' must be a number")
            elif trading['volume'] <= 0:
                errors.append("'trading.volume' must be positive")
        
        if 'deviation' in trading:
            if not isinstance(trading['deviation'], int):
                errors.append("'trading.deviation' must be an integer")
            elif not 0 <= trading['deviation'] <= 100:
                errors.append("'trading.deviation' must be between 0 and 100")
        
        if 'magic_number' in trading:
            if not isinstance(trading['magic_number'], int):
                errors.append("'trading.magic_number' must be an integer")
            elif trading['magic_number'] <= 0:
                errors.append("'trading.magic_number' must be positive")
        
        return errors
    
    @staticmethod
    def _is_valid_time(time_str: Any) -> bool:
        """Check if a string is a valid time in HH:MM format."""
        if not isinstance(time_str, str):
            return False
        
        try:
            hour, minute = map(int, time_str.split(':'))
            return 0 <= hour <= 23 and 0 <= minute <= 59
        except (ValueError, AttributeError):
            return False
    
    @staticmethod
    def _time_to_minutes(time_str: str) -> int:
        """Convert HH:MM string to total minutes."""
        hour, minute = map(int, time_str.split(':'))
        return hour * 60 + minute


class ConfigurationManager:
    """
    Manages configuration loading, validation, and hot-reload with comprehensive error handling.
    
    Features:
    - Load from YAML/JSON files with validation
    - Environment variable overrides
    - Hot-reload support
    - Configuration change callbacks
    - Default configuration with sensible defaults
    """
    
    def __init__(self, config_file: Optional[str] = None):
        """
        Initialize configuration manager.
        
        Args:
            config_file: Optional path to configuration file
        """
        self.config_file = config_file
        self.config: Dict[str, Any] = {}
        self.last_modified: float = 0
        self._callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self.logger: Optional[logging.Logger] = None
        
        # Load default configuration
        self._load_default_config()
        
        # Load from file if provided
        if config_file:
            self.load_from_file(config_file)
        
        # Apply environment overrides
        self._apply_env_overrides()
    
    def _load_default_config(self) -> None:
        """Load default configuration with sensible defaults."""
        self.config = {
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
    
    def load_from_file(self, config_file: str) -> bool:
        """
        Load configuration from YAML or JSON file.
        
        Args:
            config_file: Path to configuration file
            
        Returns:
            True if configuration loaded successfully, False otherwise
            
        Raises:
            ConfigurationError: If configuration file is invalid
        """
        try:
            with open(config_file, 'r') as f:
                if config_file.endswith(('.yaml', '.yml')):
                    file_config = yaml.safe_load(f)
                elif config_file.endswith('.json'):
                    file_config = json.load(f)
                else:
                    raise ConfigurationError(f"Unsupported config file format: {config_file}")
            
            # Validate configuration
            errors = ConfigValidator.validate_config(file_config)
            if errors:
                error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
                raise ConfigurationError(error_msg)
            
            # Merge with defaults (file overrides defaults)
            self._merge_configs(self.config, file_config)
            self.config_file = config_file
            self.last_modified = os.path.getmtime(config_file)
            
            if self.logger:
                self.logger.info(f"Configuration loaded from {config_file}")
            
            return True
            
        except yaml.YAMLError as e:
            raise ConfigurationError(f"Error parsing YAML config file: {e}")
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Error parsing JSON config file: {e}")
        except OSError as e:
            raise ConfigurationError(f"Error reading config file: {e}")
        except Exception as e:
            raise ConfigurationError(f"Error loading config file: {e}")
    
    def _merge_configs(self, base: Dict[str, Any], override: Dict[str, Any]) -> None:
        """
        Recursively merge override configuration into base configuration.
        
        Args:
            base: Base configuration dictionary (modified in place)
            override: Override configuration dictionary
        """
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_configs(base[key], value)
            else:
                base[key] = value
    
    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides to configuration."""
        env_mappings = {
            'TRADING_VOLUME': ('trading', 'volume'),
            'TRADING_DEVIATION': ('trading', 'deviation'),
            'TRADING_MAGIC_NUMBER': ('trading', 'magic_number'),
            'LOG_LEVEL': ('logging', 'level')
        }
        
        for env_var, (section, key) in env_mappings.items():
            if env_var in os.environ:
                try:
                    value = os.environ[env_var]
                    
                    # Type conversion based on expected type
                    if key == 'volume':
                        self.config[section][key] = float(value)
                    elif key == 'deviation' or key == 'magic_number':
                        self.config[section][key] = int(value)
                    elif key == 'level':
                        if value.upper() in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                            self.config[section][key] = value.upper()
                except (ValueError, TypeError) as e:
                    if self.logger:
                        self.logger.warning(f"Failed to parse environment variable {env_var}: {e}")
    
    def get_time(self, time_key: str) -> time:
        """
        Get time object from configuration.
        
        Args:
            time_key: Time key in session configuration
            
        Returns:
            time object
            
        Raises:
            KeyError: If time_key not found
            ValueError: If time format is invalid
        """
        if time_key not in self.config['session']:
            raise KeyError(f"Time key '{time_key}' not found in session configuration")
        
        time_str = self.config['session'][time_key]
        if isinstance(time_str, str):
            hour, minute = map(int, time_str.split(':'))
            return time(hour, minute)
        
        return time_str
    
    def get_timezone(self, tz_key: str) -> pytz.tzinfo:
        """
        Get timezone object from configuration.
        
        Args:
            tz_key: Timezone key in timezone configuration
            
        Returns:
            pytz timezone object
            
        Raises:
            KeyError: If tz_key not found
            pytz.UnknownTimeZoneError: If timezone is invalid
        """
        if tz_key not in self.config['timezones']:
            raise KeyError(f"Timezone key '{tz_key}' not found in timezone configuration")
        
        return pytz.timezone(self.config['timezones'][tz_key])
    
    def register_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Register callback for configuration changes.
        
        Args:
            callback: Function to call when configuration changes
        """
        self._callbacks.append(callback)
    
    def notify_callbacks(self) -> None:
        """Notify all registered callbacks of configuration change."""
        for callback in self._callbacks:
            try:
                callback(self.config)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error in config callback: {e}")
    
    def check_for_updates(self) -> bool:
        """
        Check if configuration file has been modified and reload if needed.
        
        Returns:
            True if configuration was updated, False otherwise
        """
        if not self.config_file or not os.path.exists(self.config_file):
            return False
        
        try:
            current_mtime = os.path.getmtime(self.config_file)
            if current_mtime > self.last_modified:
                if self.logger:
                    self.logger.info("Configuration file modified, reloading...")
                
                if self.load_from_file(self.config_file):
                    self.notify_callbacks()
                    return True
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error checking config updates: {e}")
        
        return False
    
    def save_to_file(self, filepath: str) -> bool:
        """
        Save current configuration to file.
        
        Args:
            filepath: Path to save configuration file
            
        Returns:
            True if configuration saved successfully, False otherwise
        """
        try:
            with open(filepath, 'w') as f:
                if filepath.endswith('.json'):
                    json.dump(self.config, f, indent=2, default=str)
                else:
                    yaml.dump(self.config, f, default_flow_style=False)
            
            if self.logger:
                self.logger.info(f"Configuration saved to {filepath}")
            
            return True
        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to save configuration: {e}")
            return False


# -------------------------------------------------------------------
# TIME UTILITIES
# -------------------------------------------------------------------
class TimeCache:
    """
    Caches time-related computations to avoid repeated calculations.
    
    Features:
    - Timezone caching with TTL
    - Automatic cache cleanup
    - Thread-safe operations
    """
    
    _timezone_cache: Dict[date, pytz.tzinfo] = {}
    _cache_ttl_hours: float = 1.0
    
    @classmethod
    def get_timezone(cls, for_date: Optional[date] = None) -> pytz.tzinfo:
        """
        Get timezone for a specific date with caching.
        
        Args:
            for_date: Date to get timezone for (defaults to today)
            
        Returns:
            pytz timezone object
        """
        if for_date is None:
            for_date = datetime.now().date()
        
        # Check cache
        if for_date in cls._timezone_cache:
            return cls._timezone_cache[for_date]
        
        # Calculate and cache
        timezone = cls._calculate_timezone(for_date)
        cls._timezone_cache[for_date] = timezone
        
        # Clean old cache entries
        cls._cleanup_cache()
        
        return timezone
    
    @staticmethod
    def _calculate_timezone(for_date: date) -> pytz.tzinfo:
        """
        Calculate timezone for a specific date.
        
        Args:
            for_date: Date to calculate timezone for
            
        Returns:
            pytz timezone object (UTC+2 or UTC+3 based on DST)
        """
        def last_sunday(year: int, month: int) -> datetime:
            """Find the last Sunday of a given month."""
            last_day = calendar.monthrange(year, month)[1]
            dt = datetime(year, month, last_day, tzinfo=timezone.utc)
            offset = (dt.weekday() + 1) % 7  # Weekday 6 is Sunday
            return dt - timedelta(days=offset)
        
        # DST boundaries (European rules)
        dst_start = last_sunday(for_date.year, 3).replace(hour=1)
        dst_end = last_sunday(for_date.year, 10).replace(hour=1)
        
        # Create datetime for timezone determination
        dt = datetime(for_date.year, for_date.month, for_date.day, 12, 0, 0, tzinfo=timezone.utc)
        
        # Determine offset
        if dst_start <= dt < dst_end:
            return pytz.timezone('Asia/Baghdad')  # UTC+3 (Summer)
        else:
            return pytz.timezone('Europe/Athens')  # UTC+2 (Winter)
    
    @classmethod
    def _cleanup_cache(cls) -> None:
        """Clean up old cache entries."""
        today = datetime.now().date()
        old_dates = [d for d in cls._timezone_cache.keys() 
                    if (today - d).days > 30]  # Keep 30 days of history
        
        for date_key in old_dates:
            del cls._timezone_cache[date_key]
    
    @classmethod
    def clear_cache(cls) -> None:
        """Clear the entire timezone cache."""
        cls._timezone_cache.clear()


class DateTimeUtils:
    """Optimized datetime utilities with caching and type safety."""
    
    @staticmethod
    @lru_cache(maxsize=128)
    def combine_date_time(base_date: date, time_obj: time, tz: pytz.tzinfo) -> datetime:
        """
        Combine date and time with timezone (cached).
        
        Args:
            base_date: Date to combine
            time_obj: Time to combine
            tz: Timezone to apply
            
        Returns:
            Localized datetime object
        """
        dt = datetime.combine(base_date, time_obj)
        return tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)
    
    @staticmethod
    def parse_time(time_str: str) -> time:
        """
        Parse time string in HH:MM format.
        
        Args:
            time_str: Time string in HH:MM format
            
        Returns:
            time object
            
        Raises:
            ValueError: If time string format is invalid
        """
        try:
            hour, minute = map(int, time_str.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"Invalid time: {time_str}")
            return time(hour, minute)
        except (ValueError, AttributeError) as e:
            raise ValueError(f"Invalid time format: {time_str}") from e
    
    @staticmethod
    def is_time_in_range(check_time: time, start_time: time, end_time: time) -> bool:
        """
        Check if a time is within a range, handling overnight ranges.
        
        Args:
            check_time: Time to check
            start_time: Range start time
            end_time: Range end time
            
        Returns:
            True if check_time is within range, False otherwise
        """
        if start_time <= end_time:
            return start_time <= check_time < end_time
        else:
            # Overnight range (e.g., 22:00 to 02:00)
            return check_time >= start_time or check_time < end_time


class SessionTimeManager:
    """
    Manages session time calculations with caching for performance.
    
    Features:
    - Cached session time calculations
    - Time range validation
    - Efficient time comparisons
    """
    
    def __init__(self, config_manager: ConfigurationManager):
        """
        Initialize session time manager.
        
        Args:
            config_manager: Configuration manager instance
        """
        self.config_manager = config_manager
        self._cached_times: Dict[date, Dict[str, datetime]] = {}
        self._current_date: Optional[date] = None
        self._current_times: Optional[Dict[str, datetime]] = None
    
    def get_session_times(self, for_date: date) -> Dict[str, datetime]:
        """
        Get session times for a specific date (cached).
        
        Args:
            for_date: Date to get session times for
            
        Returns:
            Dictionary of session times as datetime objects
        """
        if for_date != self._current_date or self._current_times is None:
            self._current_date = for_date
            self._current_times = self._calculate_session_times(for_date)
        
        return self._current_times
    
    def _calculate_session_times(self, for_date: date) -> Dict[str, datetime]:
        """
        Calculate session times for a date.
        
        Args:
            for_date: Date to calculate session times for
            
        Returns:
            Dictionary of localized datetime objects for each session time
        """
        server_tz = TimeCache.get_timezone(for_date)
        
        # Helper to create localized datetime
        def make_dt(time_key: str) -> datetime:
            time_obj = self.config_manager.get_time(time_key)
            return DateTimeUtils.combine_date_time(for_date, time_obj, server_tz)
        
        return {
            'day_open': make_dt('day_open'),
            'ghost_start': make_dt('ghost_start'),
            'ghost_end': make_dt('ghost_end'),
            'trading_start': make_dt('trading_start'),
            'trading_end': make_dt('trading_end'),
        }
    
    def is_in_trading_hours(self, current_time: datetime) -> bool:
        """
        Check if current time is within trading hours.
        
        Args:
            current_time: Current time to check
            
        Returns:
            True if within trading hours, False otherwise
        """
        session_times = self.get_session_times(current_time.date())
        return session_times['trading_start'] <= current_time < session_times['trading_end']
    
    def should_check_ghost_range(self, current_time: datetime) -> bool:
        """
        Check if we should check for ghost range.
        
        Args:
            current_time: Current time
            
        Returns:
            True if ghost range should be checked, False otherwise
        """
        session_times = self.get_session_times(current_time.date())
        return current_time > session_times['ghost_end']
    
    def should_check_market_open(self, current_time: datetime) -> bool:
        """
        Check if we should check for market open price.
        
        Args:
            current_time: Current time
            
        Returns:
            True if market open should be checked, False otherwise
        """
        session_times = self.get_session_times(current_time.date())
        return current_time >= session_times['day_open']


# -------------------------------------------------------------------
# LOGGING CONFIGURATION
# -------------------------------------------------------------------
class TradingLogger:
    """Configurable logging setup with file rotation and structured output."""
    
    @staticmethod
    def setup_logging(config_manager: ConfigurationManager, symbol: str) -> logging.Logger:
        """
        Configure structured logging based on configuration.
        
        Args:
            config_manager: Configuration manager instance
            symbol: Trading symbol for log file naming
            
        Returns:
            Configured logger instance
        """
        config = config_manager.config
        log_config = config['logging']
        
        # Create log directory if it doesn't exist
        log_dir = log_config['log_directory']
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        
        # Create logger
        logger = logging.getLogger("trading_bot")
        logger.setLevel(getattr(logging, log_config['level'].upper()))
        logger.handlers.clear()  # Remove any existing handlers
        
        # Log format with timestamp, name, level, and message
        formatter = logging.Formatter(
            fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(getattr(logging, log_config['level'].upper()))
        logger.addHandler(console_handler)
        
        # File handler with rotation (if enabled)
        if log_config.get('enable_file_logging', True):
            log_file = os.path.join(log_dir, f"trading_{symbol}_{datetime.now().strftime('%Y%m%d')}.log")
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=log_config.get('max_file_size_mb', 10) * 1024 * 1024,
                backupCount=log_config.get('backup_count', 5)
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.INFO)  # File logs INFO and above
            logger.addHandler(file_handler)
            
            # Separate error log file (if enabled)
            if log_config.get('separate_error_logs', True):
                error_file = os.path.join(log_dir, f"errors_{symbol}_{datetime.now().strftime('%Y%m%d')}.log")
                error_handler = logging.handlers.RotatingFileHandler(
                    error_file,
                    maxBytes=log_config.get('max_file_size_mb', 10) * 512 * 1024,  # Half size for errors
                    backupCount=log_config.get('backup_count', 5)
                )
                error_handler.setFormatter(formatter)
                error_handler.setLevel(logging.WARNING)  # Error file logs WARNING and above
                logger.addHandler(error_handler)
        
        return logger


# -------------------------------------------------------------------
# CONNECTION MANAGEMENT
# -------------------------------------------------------------------
class MT5ConnectionManager:
    """
    Manages MT5 connection lifecycle with reconnection logic.
    
    Features:
    - Connection state tracking
    - Automatic reconnection on failure
    - Connection health checks
    - Graceful shutdown
    """
    
    def __init__(self, logger: logging.Logger):
        """
        Initialize MT5 connection manager.
        
        Args:
            logger: Logger instance for connection events
        """
        self.logger = logger
        self.connected = False
        self.last_connection_check = 0.0
        self.connection_check_interval = 60.0  # Check every 60 seconds
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
    
    def initialize(self) -> bool:
        """
        Initialize MT5 connection with credentials from environment.
        
        Returns:
            True if connection successful, False otherwise
        """
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
            self.reconnect_attempts = 0
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize MT5 connection: {e}")
            self.connected = False
            return False
    
    def check_connection(self) -> bool:
        """
        Check if MT5 connection is still active.
        
        Returns:
            True if connection is active, False otherwise
        """
        current_time = t_mod.time()
        
        # Throttle connection checks
        if current_time - self.last_connection_check < self.connection_check_interval:
            return self.connected
        
        self.last_connection_check = current_time
        
        try:
            # Simple connection check
            info = mt5.terminal_info()
            self.connected = info is not None
            
            if not self.connected:
                self.logger.warning("MT5 connection check failed")
            
            return self.connected
            
        except Exception as e:
            self.logger.warning(f"MT5 connection check failed: {e}")
            self.connected = False
            return False
    
    def reconnect(self) -> bool:
        """
        Attempt to reconnect to MT5.
        
        Returns:
            True if reconnection successful, False otherwise
        """
        self.reconnect_attempts += 1
        
        if self.reconnect_attempts > self.max_reconnect_attempts:
            self.logger.error(f"Maximum reconnection attempts ({self.max_reconnect_attempts}) exceeded")
            return False
        
        self.logger.warning(f"Attempting to reconnect to MT5 (attempt {self.reconnect_attempts}/{self.max_reconnect_attempts})...")
        
        # Shutdown existing connection
        try:
            mt5.shutdown()
            self.logger.debug("MT5 connection shutdown")
        except Exception as e:
            self.logger.debug(f"Error during shutdown: {e}")
        
        self.connected = False
        
        # Wait before reconnection attempt
        wait_time = min(5 * self.reconnect_attempts, 30)  # Exponential backoff, max 30s
        self.logger.info(f"Waiting {wait_time}s before reconnection attempt...")
        t_mod.sleep(wait_time)
        
        # Try to reconnect
        if self.initialize():
            self.logger.info("MT5 reconnection successful")
            return True
        
        return False
    
    def ensure_connection(self) -> bool:
        """
        Ensure MT5 connection is active, reconnecting if necessary.
        
        Returns:
            True if connection is active, False otherwise
        """
        if self.check_connection():
            return True
        
        return self.reconnect()
    
    def shutdown(self) -> None:
        """Shutdown MT5 connection gracefully."""
        self.logger.info("Shutting down MT5 connection...")
        try:
            mt5.shutdown()
            self.connected = False
            self.logger.info("MT5 connection shutdown successfully")
        except Exception as e:
            self.logger.error(f"Error during MT5 shutdown: {e}")


# -------------------------------------------------------------------
# VELOCITY MONITORING
# -------------------------------------------------------------------
class VelocityMonitor:
    """
    Monitors tick velocity for high-frequency trading signals.
    
    Features:
    - Efficient deque-based timestamp tracking
    - Running average calculation
    - Configurable lookback periods
    - High-velocity detection
    """
    
    def __init__(self, symbol: str, config_manager: ConfigurationManager):
        """
        Initialize velocity monitor.
        
        Args:
            symbol: Trading symbol to monitor
            config_manager: Configuration manager instance
        """
        self.symbol = symbol
        self.config_manager = config_manager
        self.config = config_manager.config
        
        # Initialize data structures
        lookback_seconds = self.config['entry_conditions']['lookback_seconds']
        self.tick_timestamps: Deque[float] = deque(maxlen=3000)  # ~100 ticks/sec * 30 sec
        self.density_history: Deque[float] = deque(maxlen=lookback_seconds)
        self.density_sum: float = 0.0  # Running sum for quick average calculation
        
        # State tracking
        self.last_update: float = t_mod.time()
        self.last_tick_fetch: float = 0.0
        self.tick_fetch_interval: float = 0.5  # Fetch ticks every 0.5 seconds
        
        # Performance tracking
        self.metrics = VelocityMetrics()
        
        # Register for config updates
        config_manager.register_callback(self._on_config_changed)
    
    def _on_config_changed(self, new_config: Dict[str, Any]) -> None:
        """
        Handle configuration changes.
        
        Args:
            new_config: New configuration dictionary
        """
        old_lookback = self.density_history.maxlen
        new_lookback = new_config['entry_conditions']['lookback_seconds']
        
        if new_lookback != old_lookback:
            # Create new deque with updated maxlen
            new_history = deque(maxlen=new_lookback)
            new_history.extend(self.density_history)
            self.density_history = new_history
            
            # Recalculate sum
            self.density_sum = sum(self.density_history)
        
        self.config = new_config
    
    def get_server_timestamp(self) -> Optional[float]:
        """
        Get current server timestamp.
        
        Returns:
            Server timestamp in seconds, or None if failed
        """
        tick = mt5.symbol_info_tick(self.symbol)
        return tick.time_msc / 1000.0 if tick is not None else None
    
    def get_tick_timestamps_batch(self, server_time: Optional[float] = None) -> np.ndarray:
        """
        Fetch tick timestamps in batches for efficiency.
        
        Args:
            server_time: Optional starting timestamp for batch
            
        Returns:
            Array of tick timestamps in seconds
        """
        current_time = t_mod.time()
        
        # Throttle tick fetching
        if current_time - self.last_tick_fetch < self.tick_fetch_interval:
            return np.array([])
        
        self.last_tick_fetch = current_time
        
        if server_time is None:
            server_time = self.get_server_timestamp()
            if server_time is None:
                return np.array([])
        
        # Fetch ticks in batches
        ticks = mt5.copy_ticks_from(self.symbol, server_time, 5000, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            return np.array([])
        
        # Extract timestamps efficiently
        timestamps = ticks['time_msc'] / 1000.0
        
        # Skip first tick if it's the same as server_time
        if len(timestamps) > 1 and timestamps[0] <= server_time:
            return timestamps[1:]
        
        return timestamps
    
    def on_tick(self) -> None:
        """Process new ticks and update timestamp deque."""
        now = self.get_server_timestamp()
        if now is None:
            return
        
        # Get new timestamps
        if len(self.tick_timestamps) == 0:
            timestamps = self.get_tick_timestamps_batch(now)
        else:
            timestamps = self.get_tick_timestamps_batch(self.tick_timestamps[-1])
        
        if len(timestamps) > 0:
            self.tick_timestamps.extend(timestamps)
            self.cleanup(now)
    
    def cleanup(self, current_time: float) -> None:
        """
        Remove old ticks from the deque.
        
        Args:
            current_time: Current server time in seconds
        """
        cutoff = current_time - 30.0  # Keep only last 30 seconds
        
        # Remove old timestamps
        while self.tick_timestamps and self.tick_timestamps[0] < cutoff:
            self.tick_timestamps.popleft()
    
    def update_history(self) -> None:
        """Update density history with current tick density."""
        now = self.get_server_timestamp()
        if now is None:
            return
        
        self.cleanup(now)
        
        # Calculate current density (ticks per second)
        current_density = len(self.tick_timestamps) / 30.0
        
        # Update running sum
        if len(self.density_history) == self.density_history.maxlen:
            self.density_sum -= self.density_history[0]
        
        self.density_history.append(current_density)
        self.density_sum += current_density
        self.last_update = t_mod.time()
        
        # Update metrics
        self.metrics.current_density = current_density
        self.metrics.average_density = self.density_sum / max(len(self.density_history), 1)
        self.metrics.history_size = len(self.density_history)
        self.metrics.ticks_count = len(self.tick_timestamps)
    
    def is_high_velocity(self) -> bool:
        """
        Check if current velocity is high relative to historical average.
        
        Returns:
            True if high velocity detected, False otherwise
        """
        # Need sufficient history
        if len(self.density_history) < 10:
            return False
        
        # Check if data is stale
        if t_mod.time() - self.last_update > 600:  # 10 minutes
            return False
        
        current_density = len(self.tick_timestamps) / 30.0
        average_density = self.density_sum / len(self.density_history)
        
        if average_density == 0:
            return False
        
        multiplier = self.config['entry_conditions']['velocity_multiplier']
        is_high = current_density > (average_density * multiplier)
        
        self.metrics.is_high_velocity = is_high
        
        return is_high
    
    def get_metrics(self) -> VelocityMetrics:
        """
        Get current velocity metrics.
        
        Returns:
            VelocityMetrics object with current state
        """
        return self.metrics


# -------------------------------------------------------------------
# SYMBOL INFORMATION CACHE
# -------------------------------------------------------------------
class SymbolInfoCache:
    """
    Caches symbol information to reduce MT5 API calls.
    
    Features:
    - TTL-based caching
    - Automatic cache invalidation
    - Type-safe access to symbol properties
    """
    
    def __init__(self, symbol: str):
        """
        Initialize symbol info cache.
        
        Args:
            symbol: Trading symbol to cache
        """
        self.symbol = symbol
        self._cache: Dict[str, Any] = {}
        self._last_update: float = 0.0
        self._update_interval: float = 300.0  # Update every 5 minutes
    
    def get_info(self) -> Optional[Dict[str, Any]]:
        """
        Get symbol information with caching.
        
        Returns:
            Symbol information dictionary, or None if failed
        """
        current_time = t_mod.time()
        
        # Check if cache is stale or empty
        if (current_time - self._last_update) > self._update_interval or not self._cache:
            info = mt5.symbol_info(self.symbol)
            if info is None:
                return None
            
            # Cache important properties
            self._cache = {
                'contract_size': info.trade_contract_size,
                'digits': info.digits,
                'volume_min': info.volume_min,
                'volume_max': info.volume_max,
                'volume_step': info.volume_step,
                'filling_mode': info.filling_mode,
                'spread': info.spread,
                'trade_mode': info.trade_mode,
                'swap_mode': info.swap_mode,
                'point': info.point,
                'tick_size': info.tick_size,
                'trade_calc_mode': info.trade_calc_mode,
                'trade_tick_size': info.trade_tick_size,
                'trade_tick_value': info.trade_tick_value,
                'trade_contract_size': info.trade_contract_size
            }
            
            self._last_update = current_time
        
        return self._cache
    
    def get_contract_size(self) -> Optional[float]:
        """
        Get contract size from cache.
        
        Returns:
            Contract size, or None if not available
        """
        info = self.get_info()
        return info['contract_size'] if info else None
    
    def clear_cache(self) -> None:
        """Clear the symbol info cache."""
        self._cache.clear()
        self._last_update = 0.0


# -------------------------------------------------------------------
# TRADING STRATEGY
# -------------------------------------------------------------------
class TradingStrategy:
    """
    Implements the core trading strategy with state management.
    
    Features:
    - Daily bias calculation
    - Ghost range detection
    - Entry/exit logic
    - Risk management
    - Trade state tracking
    """
    
    def __init__(self, symbol: str, config_manager: ConfigurationManager):
        """
        Initialize trading strategy.
        
        Args:
            symbol: Trading symbol
            config_manager: Configuration manager instance
        """
        self.symbol = symbol
        self.config_manager = config_manager
        self.config = config_manager.config
        
        # State tracking
        self.day_state = DayState()
        self.trade_state = TradeState()
        
        # Caches
        self._buffer_cache: Dict[float, float] = {}
        
        # Register for config updates
        config_manager.register_callback(self._on_config_changed)
    
    def _on_config_changed(self, new_config: Dict[str, Any]) -> None:
        """
        Handle configuration changes.
        
        Args:
            new_config: New configuration dictionary
        """
        self.config = new_config
        self._buffer_cache.clear()  # Clear buffer cache as contract size might change
    
    def reset_day(self, new_date: date) -> None:
        """
        Reset state for a new trading day.
        
        Args:
            new_date: New trading date
        """
        self.day_state = DayState(current_date=new_date)
        self.trade_state = TradeState()
        self._buffer_cache.clear()
    
    def calculate_daily_bias(self) -> TradeBias:
        """
        Calculate daily bias based on yesterday's D1 candle.
        
        Returns:
            TradeBias (BUY, SELL, or STRADDLE)
            
        Raises:
            MT5OperationError: If D1 data cannot be fetched
        """
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_D1, 1, 1)
        if rates is None or len(rates) == 0:
            raise MT5OperationError(f"Failed to fetch D1 data for {self.symbol}")
        
        r = rates[0]
        high, low, close = r['high'], r['low'], r['close']
        
        if high == low:
            return TradeBias.STRADDLE
        
        # Calculate relative close position
        rc = (close - low) / (high - low)
        
        # Determine bias
        if rc >= self.config['bias_filter']['buy_threshold']:
            return TradeBias.BUY
        elif rc <= self.config['bias_filter']['sell_threshold']:
            return TradeBias.SELL
        else:
            return TradeBias.STRADDLE
    
    def calculate_buffer(self, contract_size: float) -> float:
        """
        Calculate buffer in price units from pips.
        
        Args:
            contract_size: Symbol contract size
            
        Returns:
            Buffer in price units
        """
        if contract_size == 0:
            return 0.0
        
        # Cache buffer calculation
        if contract_size not in self._buffer_cache:
            buffer_pips = self.config['entry_conditions']['buffer_pips']
            self._buffer_cache[contract_size] = buffer_pips / contract_size
        
        return self._buffer_cache[contract_size]
    
    def check_entry_conditions(
        self,
        tick_data: TickData,
        contract_size: float,
        velocity_monitor: VelocityMonitor
    ) -> Tuple[bool, Optional[TradeBias]]:
        """
        Check if entry conditions are met.
        
        Args:
            tick_data: Current tick data
            contract_size: Symbol contract size
            velocity_monitor: Velocity monitor instance
            
        Returns:
            Tuple of (should_enter, direction)
        """
        if self.day_state.bias == TradeBias.STRADDLE:
            return False, None
        
        if self.day_state.ghost_high is None or self.day_state.ghost_low is None:
            return False, None
        
        if self.day_state.daily_open_price is None:
            return False, None
        
        if self.trade_state.in_trade:
            return False, None
        
        buffer = self.calculate_buffer(contract_size)
        
        # BUY logic
        if self.day_state.bias == TradeBias.BUY:
            lower_target = self.day_state.ghost_low + buffer
            
            # Check for opposite touch
            if tick_data.ask <= lower_target and not self.trade_state.touched_opposite:
                self.trade_state.touched_opposite = True
            
            # Check for entry trigger
            if self.trade_state.touched_opposite:
                upper_target = self.day_state.ghost_high - buffer
                
                if (tick_data.ask >= upper_target and 
                    tick_data.ask > self.day_state.daily_open_price and
                    velocity_monitor.is_high_velocity()):
                    return True, TradeBias.BUY
        
        # SELL logic
        elif self.day_state.bias == TradeBias.SELL:
            upper_target = self.day_state.ghost_high - buffer
            
            # Check for opposite touch
            if tick_data.bid >= upper_target and not self.trade_state.touched_opposite:
                self.trade_state.touched_opposite = True
            
            # Check for entry trigger
            if self.trade_state.touched_opposite:
                lower_target = self.day_state.ghost_low + buffer
                
                if (tick_data.bid <= lower_target and 
                    tick_data.bid < self.day_state.daily_open_price and
                    velocity_monitor.is_high_velocity()):
                    return True, TradeBias.SELL
        
        return False, None
    
    def update_trailing_stop(
        self,
        position: PositionType,
        current_price: float,
        contract_size: float
    ) -> Optional[float]:
        """
        Calculate new trailing stop level.
        
        Args:
            position: Current position
            current_price: Current market price
            contract_size: Symbol contract size
            
        Returns:
            New stop loss price, or None if no update needed
        """
        # Calculate current profit in pips
        if position.type == mt5.ORDER_TYPE_BUY:
            current_profit_points = (current_price - position.price_open) * contract_size
        else:
            current_profit_points = (position.price_open - current_price) * contract_size
        
        # Update max profit
        if current_profit_points > self.trade_state.max_pnl:
            self.trade_state.max_pnl = current_profit_points
        
        # Find appropriate trailing stage
        trailing_stages = self.config['risk_management']['trailing_stages']
        
        for stage in trailing_stages:
            if self.trade_state.max_pnl >= stage['min_profit']:
                if stage['retention'] == -1:
                    return None  # Keep original SL
                
                trail_dist = self.trade_state.max_pnl * stage['retention'] / contract_size
                
                if position.type == mt5.ORDER_TYPE_BUY:
                    new_sl = position.price_open + trail_dist
                    should_update = (position.sl == 0 or new_sl > position.sl)
                else:
                    new_sl = position.price_open - trail_dist
                    should_update = (position.sl == 0 or new_sl < position.sl)
                
                if should_update:
                    return new_sl
        
        return None
    
    def update_trade_state(self, has_position: bool, position_ticket: Optional[int] = None) -> None:
        """
        Update trade state based on current positions.
        
        Args:
            has_position: Whether a position exists
            position_ticket: Position ticket if exists
        """
        if self.trade_state.in_trade and not has_position:
            # Position was closed
            self.trade_state = TradeState()
        elif not self.trade_state.in_trade and has_position:
            # New position opened
            self.trade_state.in_trade = True
            self.trade_state.position_ticket = position_ticket


# -------------------------------------------------------------------
# ORDER EXECUTION
# -------------------------------------------------------------------
class OrderManager:
    """
    Manages order execution with validation and error handling.
    
    Features:
    - Order parameter validation
    - Price rounding and normalization
    - Comprehensive error handling
    - Retry logic for failed orders
    """
    
    def __init__(self, symbol: str, config_manager: ConfigurationManager, symbol_info_cache: SymbolInfoCache):
        """
        Initialize order manager.
        
        Args:
            symbol: Trading symbol
            config_manager: Configuration manager instance
            symbol_info_cache: Symbol information cache
        """
        self.symbol = symbol
        self.config_manager = config_manager
        self.config = config_manager.config
        self.symbol_info_cache = symbol_info_cache
    
    @retry(max_attempts=2, delay=1.0, exceptions=(OrderExecutionError,))
    def execute_trade(self, direction: TradeBias, stop_loss_pips: float) -> Tuple[bool, float]:
        """
        Execute a trade with comprehensive validation.
        
        Args:
            direction: Trade direction (BUY or SELL)
            stop_loss_pips: Stop loss in pips
            
        Returns:
            Tuple of (success, execution_price)
            
        Raises:
            OrderExecutionError: If order execution fails
        """
        # Get current tick
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise OrderExecutionError(f"Cannot get tick for {self.symbol}")
        
        # Get symbol info
        symbol_info = self.symbol_info_cache.get_info()
        if symbol_info is None:
            raise OrderExecutionError(f"Cannot get symbol info for {self.symbol}")
        
        # Validate order parameters
        self._validate_order_params(direction, stop_loss_pips, symbol_info)
        
        # Get filling type
        filling = self._get_filling_type(symbol_info)
        if filling is None:
            raise OrderExecutionError(f"Cannot determine filling type for {self.symbol}")
        
        # Calculate prices
        contract_size = symbol_info['contract_size']
        stop_loss_points = stop_loss_pips / contract_size
        
        if direction == TradeBias.BUY:
            price = tick.ask
            stop_loss_price = price - stop_loss_points
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            stop_loss_price = price + stop_loss_points
            order_type = mt5.ORDER_TYPE_SELL
        
        # Round prices to appropriate digits
        digits = symbol_info['digits']
        price = round(price, digits)
        stop_loss_price = round(stop_loss_price, digits)
        
        # Prepare order request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.config['trading']['volume'],
            "type": order_type,
            "price": price,
            "sl": stop_loss_price,
            "deviation": self.config['trading']['deviation'],
            "magic": self.config['trading']['magic_number'],
            "comment": "LiveDemo_Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        
        # Send order
        result = mt5.order_send(request)
        
        if result is None:
            raise OrderExecutionError("No response from MT5")
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise OrderExecutionError(f"Order failed: {result.comment} (retcode: {result.retcode})")
        
        return True, result.price
    
    def _validate_order_params(
        self,
        direction: TradeBias,
        stop_loss_pips: float,
        symbol_info: Dict[str, Any]
    ) -> None:
        """
        Validate order parameters before execution.
        
        Args:
            direction: Trade direction
            stop_loss_pips: Stop loss in pips
            symbol_info: Symbol information
            
        Raises:
            ValidationError: If parameters are invalid
        """
        errors = []
        
        if direction not in [TradeBias.BUY, TradeBias.SELL]:
            errors.append(f"Invalid direction: {direction}")
        
        if stop_loss_pips <= 0:
            errors.append(f"Stop loss must be positive: {stop_loss_pips}")
        
        volume = self.config['trading']['volume']
        if volume <= 0:
            errors.append(f"Volume must be positive: {volume}")
        elif volume < symbol_info['volume_min']:
            errors.append(f"Volume below minimum: {volume} < {symbol_info['volume_min']}")
        elif volume > symbol_info['volume_max']:
            errors.append(f"Volume above maximum: {volume} > {symbol_info['volume_max']}")
        elif volume % symbol_info['volume_step'] != 0:
            errors.append(f"Volume not a multiple of step: {volume} % {symbol_info['volume_step']} != 0")
        
        if errors:
            raise ValidationError("; ".join(errors))
    
    def _get_filling_type(self, symbol_info: Dict[str, Any]) -> Optional[int]:
        """
        Get order filling type based on symbol information.
        
        Args:
            symbol_info: Symbol information
            
        Returns:
            MT5 filling type constant, or None if cannot determine
        """
        filling_mode = symbol_info['filling_mode']
        
        if filling_mode & 1:
            return mt5.ORDER_FILLING_FOK
        elif filling_mode & 2:
            return mt5.ORDER_FILLING_IOC
        else:
            return mt5.ORDER_FILLING_RETURN
    
    def close_position(self, position_ticket: int) -> bool:
        """
        Close an existing position.
        
        Args:
            position_ticket: Position ticket to close
            
        Returns:
            True if position closed successfully, False otherwise
        """
        try:
            # Get position
            positions = mt5.positions_get(ticket=position_ticket)
            if not positions:
                return False
            
            position = positions[0]
            
            # Get current tick
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                return False
            
            # Determine close parameters
            if position.type == mt5.ORDER_TYPE_BUY:
                price = tick.bid
                close_type = mt5.ORDER_TYPE_SELL
            else:
                price = tick.ask
                close_type = mt5.ORDER_TYPE_BUY
            
            # Prepare close request
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": position.volume,
                "type": close_type,
                "position": position_ticket,
                "price": price,
                "deviation": self.config['trading']['deviation'],
                "magic": self.config['trading']['magic_number'],
                "comment": "Mandatory Close"
            }
            
            # Send close order
            result = mt5.order_send(request)
            
            return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
            
        except Exception:
            return False
    
    def modify_stop_loss(self, position_ticket: int, new_stop_loss: float) -> bool:
        """
        Modify stop loss for an existing position.
        
        Args:
            position_ticket: Position ticket to modify
            new_stop_loss: New stop loss price
            
        Returns:
            True if modification successful, False otherwise
        """
        try:
            # Get symbol info for rounding
            symbol_info = self.symbol_info_cache.get_info()
            if symbol_info is None:
                return False
            
            # Round stop loss to appropriate digits
            digits = symbol_info['digits']
            new_stop_loss = round(new_stop_loss, digits)
            
            # Prepare modification request
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": self.symbol,
                "position": position_ticket,
                "sl": new_stop_loss,
                "magic": self.config['trading']['magic_number']
            }
            
            # Send modification
            result = mt5.order_send(request)
            
            return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
            
        except Exception:
            return False


# -------------------------------------------------------------------
# MAIN TRADING BOT
# -------------------------------------------------------------------
class TradingBot:
    """
    Main trading bot orchestrating all components.
    
    Features:
    - Component lifecycle management
    - Main trading loop
    - Performance monitoring
    - Graceful shutdown
    - Configuration hot-reload
    """
    
    def __init__(self, symbol: str, config_file: Optional[str] = None):
        """
        Initialize trading bot.
        
        Args:
            symbol: Trading symbol
            config_file: Optional configuration file path
        """
        self.symbol = symbol
        self.config_file = config_file
        
        # Initialize components (will be set in setup)
        self.config_manager: Optional[ConfigurationManager] = None
        self.logger: Optional[logging.Logger] = None
        self.connection_manager: Optional[MT5ConnectionManager] = None
        self.session_time_manager: Optional[SessionTimeManager] = None
        self.symbol_info_cache: Optional[SymbolInfoCache] = None
        self.velocity_monitor: Optional[VelocityMonitor] = None
        self.trading_strategy: Optional[TradingStrategy] = None
        self.order_manager: Optional[OrderManager] = None
        
        # State tracking
        self.signal_handler: Optional[SignalHandler] = None
        self.performance_metrics = PerformanceMetrics()
        self.is_running = False
        
        # Performance tracking
        self.last_velocity_update = 0.0
        self.last_position_check = 0.0
        self.last_metrics_log = 0.0
        self.loop_iterations = 0
    
    def setup(self) -> bool:
        """
        Set up all components of the trading bot.
        
        Returns:
            True if setup successful, False otherwise
        """
        try:
            # Initialize configuration manager
            self.config_manager = ConfigurationManager(self.config_file)
            
            # Setup logging
            self.logger = TradingLogger.setup_logging(self.config_manager, self.symbol)
            self.config_manager.logger = self.logger
            
            # Log configuration summary
            config_summary = json.dumps(self.config_manager.config, indent=2, default=str)
            self.logger.info(f"Configuration loaded:\n{config_summary}")
            
            # Initialize connection manager
            self.connection_manager = MT5ConnectionManager(self.logger)
            
            # Connect to MT5
            if not self.connection_manager.initialize():
                self.logger.error("Failed to connect to MT5")
                return False
            
            # Check symbol
            if not mt5.symbol_select(self.symbol, True):
                self.logger.error(f"Symbol {self.symbol} not available")
                return False
            
            # Initialize other components
            self.session_time_manager = SessionTimeManager(self.config_manager)
            self.symbol_info_cache = SymbolInfoCache(self.symbol)
            self.velocity_monitor = VelocityMonitor(self.symbol, self.config_manager)
            self.trading_strategy = TradingStrategy(self.symbol, self.config_manager)
            self.order_manager = OrderManager(
                self.symbol, self.config_manager, self.symbol_info_cache
            )
            
            # Setup signal handler
            self.signal_handler = SignalHandler(self.logger, self.connection_manager)
            self.signal_handler.setup()
            
            # Log successful setup
            self.logger.info(f"Trading bot setup complete for {self.symbol}")
            
            return True
            
        except Exception as e:
            if self.logger:
                self.logger.error(f"Setup failed: {e}")
            else:
                print(f"Setup failed: {e}")
            return False
    
    def run(self) -> None:
        """Run the main trading loop."""
        if not self.setup():
            return
        
        self.logger.info("Starting trading bot...")
        self.is_running = True
        
        try:
            self._main_loop()
        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
        except Exception as e:
            self.logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
        finally:
            self.shutdown()
    
    def _main_loop(self) -> None:
        """Main trading loop."""
        config = self.config_manager.config
        perf_config = config['performance']
        
        while self.is_running and not self.signal_handler.should_shutdown():
            loop_start = t_mod.time()
            self.loop_iterations += 1
            
            try:
                # Check for configuration updates
                if self.config_file:
                    self.config_manager.check_for_updates()
                
                # Ensure MT5 connection
                if not self.connection_manager.ensure_connection():
                    self.logger.error("Lost connection to MT5")
                    t_mod.sleep(5)
                    continue
                
                # Get current market data
                current_data = self._get_current_market_data()
                if not current_data:
                    t_mod.sleep(perf_config['tick_processing_interval'])
                    continue
                
                tick_data, server_time = current_data
                
                # Check if in trading hours
                if not self.session_time_manager.is_in_trading_hours(server_time):
                    self.logger.debug(f"Outside trading hours: {server_time.time()}")
                    t_mod.sleep(perf_config['outside_session_sleep'])
                    continue
                
                # Update day state if needed
                self._update_day_state(server_time.date())
                
                # Update velocity monitor
                self._update_velocity_monitor()
                
                # Process trading logic
                self._process_trading_logic(tick_data, server_time)
                
                # Update performance metrics
                self._update_performance_metrics(loop_start)
                
            except Exception as e:
                self.logger.error(f"Error in main loop iteration: {e}")
                t_mod.sleep(1)  # Brief pause on error
            
            # Adaptive sleep
            self._adaptive_sleep(loop_start, perf_config['tick_processing_interval'])
    
    def _get_current_market_data(self) -> Optional[Tuple[TickData, datetime]]:
        """
        Get current market data and server time.
        
        Returns:
            Tuple of (tick_data, server_time), or None if failed
        """
        tick_data = mt5.symbol_info_tick(self.symbol)
        if tick_data is None:
            return None
        
        # Convert server time to datetime
        server_time = pd.to_datetime(tick_data.time, unit='s')
        server_tz = TimeCache.get_timezone(server_time.date())
        server_time = server_tz.localize(server_time) if server_time.tzinfo is None else server_time.astimezone(server_tz)
        
        return tick_data, server_time
    
    def _update_day_state(self, current_date: date) -> None:
        """
        Update day state if date has changed.
        
        Args:
            current_date: Current date
        """
        if self.trading_strategy.day_state.current_date != current_date:
            self.logger.info(f"New trading day: {current_date}")
            
            # Reset strategy for new day
            self.trading_strategy.reset_day(current_date)
            
            # Calculate daily bias
            try:
                bias = self.trading_strategy.calculate_daily_bias()
                self.trading_strategy.day_state.bias = bias
                self.logger.info(f"Daily bias: {bias}")
            except Exception as e:
                self.logger.error(f"Failed to calculate daily bias: {e}")
    
    def _update_velocity_monitor(self) -> None:
        """Update velocity monitor at appropriate intervals."""
        current_time = t_mod.time()
        update_interval = self.config_manager.config['performance']['velocity_update_interval']
        
        if current_time - self.last_velocity_update >= update_interval:
            self.velocity_monitor.on_tick()
            self.velocity_monitor.update_history()
            self.last_velocity_update = current_time
            
            # Log metrics periodically
            if current_time - self.last_metrics_log >= 300:  # Every 5 minutes
                metrics = self.velocity_monitor.get_metrics()
                self.logger.info(f"Velocity metrics: {metrics}")
                self.last_metrics_log = current_time
    
    def _process_trading_logic(self, tick_data: TickData, server_time: datetime) -> None:
        """
        Process trading logic including entry/exit decisions.
        
        Args:
            tick_data: Current tick data
            server_time: Current server time
        """
        # Get session times
        session_times = self.session_time_manager.get_session_times(server_time.date())
        
        # Update ghost range if needed
        if (self.trading_strategy.day_state.ghost_high is None and 
            self.session_time_manager.should_check_ghost_range(server_time)):
            
            self._update_ghost_range(server_time.date(), session_times)
        
        # Update daily open price if needed
        if (self.trading_strategy.day_state.daily_open_price is None and 
            self.session_time_manager.should_check_market_open(server_time)):
            
            self._update_daily_open_price(server_time.date(), session_times)
        
        # Check for mandatory close
        if server_time >= session_times['trading_end']:
            self._handle_mandatory_close()
            return
        
        # Update positions
        self._update_positions(tick_data)
        
        # Check for entry if not in trade
        if not self.trading_strategy.trade_state.in_trade:
            self._check_entry_conditions(tick_data)
    
    def _update_ghost_range(self, current_date: date, session_times: Dict[str, datetime]) -> None:
        """
        Update ghost range for current day.
        
        Args:
            current_date: Current date
            session_times: Session times for current date
        """
        try:
            rates = mt5.copy_rates_range(
                self.symbol,
                mt5.TIMEFRAME_M1,
                session_times['ghost_start'],
                session_times['ghost_end']
            )
            
            if rates is not None and len(rates) > 0:
                ghost_low = float(np.min(rates['low']))
                ghost_high = float(np.max(rates['high']))
                
                self.trading_strategy.day_state.ghost_low = ghost_low
                self.trading_strategy.day_state.ghost_high = ghost_high
                
                self.logger.info(f"Ghost range updated: {ghost_low:.5f} - {ghost_high:.5f}")
            else:
                self.logger.warning("No data available for ghost range")
                
        except Exception as e:
            self.logger.error(f"Failed to update ghost range: {e}")
    
    def _update_daily_open_price(self, current_date: date, session_times: Dict[str, datetime]) -> None:
        """
        Update daily open price.
        
        Args:
            current_date: Current date
            session_times: Session times for current date
        """
        try:
            rates = mt5.copy_rates_range(
                self.symbol,
                mt5.TIMEFRAME_M1,
                session_times['day_open'],
                session_times['day_open'] + timedelta(minutes=1)
            )
            
            if rates is not None and len(rates) > 0:
                open_price = float(rates[0][1])
                self.trading_strategy.day_state.daily_open_price = open_price
                self.logger.info(f"Daily open price: {open_price:.5f}")
            else:
                self.logger.warning("No data available for daily open price")
                
        except Exception as e:
            self.logger.error(f"Failed to update daily open price: {e}")
    
    def _handle_mandatory_close(self) -> None:
        """Handle mandatory close at end of trading session."""
        self.logger.info("Mandatory close triggered")
        
        if self.trading_strategy.trade_state.in_trade and self.trading_strategy.trade_state.position_ticket:
            success = self.order_manager.close_position(self.trading_strategy.trade_state.position_ticket)
            if success:
                self.logger.info("Position closed successfully")
                self.trading_strategy.update_trade_state(False)
            else:
                self.logger.error("Failed to close position")
    
    def _update_positions(self, tick_data: TickData) -> None:
        """
        Update positions and handle trailing stops.
        
        Args:
            tick_data: Current tick data
        """
        current_time = t_mod.time()
        check_interval = self.config_manager.config['performance']['position_check_interval']
        
        if current_time - self.last_position_check < check_interval:
            return
        
        self.last_position_check = current_time
        self.performance_metrics.positions_checked += 1
        
        # Get current positions
        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            positions = []
        
        # Filter positions by magic number
        my_positions = [
            p for p in positions 
            if p.magic == self.config_manager.config['trading']['magic_number']
        ]
        
        # Update trade state
        has_position = len(my_positions) > 0
        position_ticket = my_positions[0].ticket if my_positions else None
        self.trading_strategy.update_trade_state(has_position, position_ticket)
        
        # Handle trailing stops for open position
        if has_position:
            position = my_positions[0]
            contract_size = self.symbol_info_cache.get_contract_size()
            
            if contract_size:
                current_price = tick_data.bid if position.type == mt5.ORDER_TYPE_BUY else tick_data.ask
                
                new_stop_loss = self.trading_strategy.update_trailing_stop(
                    position, current_price, contract_size
                )
                
                if new_stop_loss is not None:
                    success = self.order_manager.modify_stop_loss(position.ticket, new_stop_loss)
                    if success:
                        self.logger.info(f"Stop loss updated to {new_stop_loss:.5f}")
    
    def _check_entry_conditions(self, tick_data: TickData) -> None:
        """
        Check entry conditions and execute trades if met.
        
        Args:
            tick_data: Current tick data
        """
        contract_size = self.symbol_info_cache.get_contract_size()
        if not contract_size:
            return
        
        should_enter, direction = self.trading_strategy.check_entry_conditions(
            tick_data, contract_size, self.velocity_monitor
        )
        
        if should_enter and direction:
            self.logger.info(f"Entry conditions met for {direction}")
            
            try:
                success, price = self.order_manager.execute_trade(
                    direction,
                    self.config_manager.config['risk_management']['initial_sl_pips']
                )
                
                if success:
                    self.logger.info(f"Trade executed: {direction} at {price:.5f}")
                    self.trading_strategy.trade_state.in_trade = True
                    self.trading_strategy.trade_state.direction = direction
                    self.trading_strategy.trade_state.entry_price = price
                    self.trading_strategy.trade_state.touched_opposite = False
                else:
                    self.logger.error("Trade execution failed")
                    
            except Exception as e:
                self.logger.error(f"Trade execution error: {e}")
    
    def _update_performance_metrics(self, loop_start: float) -> None:
        """
        Update and log performance metrics.
        
        Args:
            loop_start: Start time of current loop iteration
        """
        loop_time = t_mod.time() - loop_start
        
        # Update running average
        if self.performance_metrics.loop_iterations == 0:
            self.performance_metrics.avg_iteration_time = loop_time
        else:
            alpha = 0.1  # Exponential moving average factor
            self.performance_metrics.avg_iteration_time = (
                alpha * loop_time + 
                (1 - alpha) * self.performance_metrics.avg_iteration_time
            )
        
        self.performance_metrics.loop_iterations += 1
        
        # Log metrics every 1000 iterations
        if self.loop_iterations % 1000 == 0:
            self.logger.debug(
                f"Performance: {self.performance_metrics.avg_iteration_time:.4f}s/iter, "
                f"iterations: {self.loop_iterations}"
            )
    
    def _adaptive_sleep(self, loop_start: float, target_interval: float) -> None:
        """
        Adaptive sleep to maintain target loop interval.
        
        Args:
            loop_start: Start time of current loop iteration
            target_interval: Target loop interval in seconds
        """
        elapsed = t_mod.time() - loop_start
        sleep_time = max(0.0, target_interval - elapsed)
        
        if sleep_time > 0:
            t_mod.sleep(sleep_time)
    
    def shutdown(self) -> None:
        """Shutdown trading bot gracefully."""
        self.is_running = False
        self.logger.info("Shutting down trading bot...")
        
        try:
            # Close any open positions
            positions = mt5.positions_get(symbol=self.symbol)
            if positions:
                self.logger.info(f"Closing {len(positions)} open positions...")
                for position in positions:
                    if position.magic == self.config_manager.config['trading']['magic_number']:
                        self.order_manager.close_position(position.ticket)
            
        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}")
        
        # Shutdown connection manager
        if self.connection_manager:
            self.connection_manager.shutdown()
        
        self.logger.info(f"Trading bot shutdown complete. Total iterations: {self.loop_iterations}")


# -------------------------------------------------------------------
# COMMAND LINE INTERFACE
# -------------------------------------------------------------------
def main() -> None:
    """Main entry point for the trading bot."""
    parser = argparse.ArgumentParser(
        description="MT5 Trading Bot - Optimized for performance with structured logging",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python trading_bot.py EURUSD
  python trading_bot.py GBPUSD --config config.yaml
  python trading_bot.py EURUSD --log-level DEBUG --hot-reload
  python trading_bot.py GBPUSD --validate-config --config config.yaml
        """
    )
    
    parser.add_argument(
        "symbol",
        help="Trading symbol (e.g., EURUSD, GBPUSD)"
    )
    
    parser.add_argument(
        "--config",
        help="Path to configuration file (YAML or JSON)"
    )
    
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Logging level (default: INFO)"
    )
    
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate configuration and exit"
    )
    
    parser.add_argument(
        "--generate-config",
        metavar="FILE",
        help="Generate configuration template file"
    )
    
    parser.add_argument(
        "--save-config",
        metavar="FILE",
        help="Save current configuration to file"
    )
    
    parser.add_argument(
        "--hot-reload",
        action="store_true",
        help="Enable hot-reload of configuration file"
    )
    
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Run in test mode (no actual trades)"
    )
    
    args = parser.parse_args()
    
    # Generate config template if requested
    if args.generate_config:
        _generate_config_template(args.generate_config)
        return
    
    # Create basic configuration manager for validation
    config_manager = ConfigurationManager(args.config)
    
    # Override logging level from command line
    config_manager.config['logging']['level'] = args.log_level
    
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
            sys.exit(0)
    
    # Save configuration if requested
    if args.save_config:
        if config_manager.save_to_file(args.save_config):
            print(f"Configuration saved to {args.save_config}")
        else:
            print(f"Failed to save configuration to {args.save_config}")
        return
    
    # Create and run trading bot
    bot = TradingBot(args.symbol.strip().upper(), args.config)
    
    # Override config with test mode if specified
    if args.test_mode:
        config_manager.config['trading']['volume'] = 0.001  # Minimal volume for testing
        print("Running in test mode - trades will use minimal volume")
    
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nTrading bot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


def _generate_config_template(filepath: str) -> None:
    """
    Generate a configuration template file.
    
    Args:
        filepath: Path to save template file
    """
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
    
    try:
        with open(filepath, 'w') as f:
            yaml.dump(template, f, default_flow_style=False)
        print(f"Configuration template generated: {filepath}")
    except Exception as e:
        print(f"Failed to generate config template: {e}")


if __name__ == "__main__":
    main()