from datetime import datetime
import time as t_mod
import logging
import logging.handlers
import os

from functools import wraps, lru_cache
from typing import Optional, Dict, Any, List, Tuple, Callable, Union


class TradingLogger:
    """Centralized logging configuration for trading system."""

    def __init__(self):
        self.symbol = None
        self.log_level = "INFO"

    def setup_logging(
        self, symbol: str, log_level: str = "INFO", name: str = "trading"
    ) -> logging.Logger:
        """
        Configure structured logging with console and file handlers.
        """
        self.symbol = symbol
        self.log_level = log_level

        log_dir = "logs"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, log_level.upper()))
        logger.handlers.clear()


        formatter = logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(getattr(logging, log_level.upper()))
        logger.addHandler(console_handler)

        # File handler with rotation
        log_file = os.path.join(
            log_dir,
            f"{symbol}_{name}_{datetime.now().strftime('%Y%m%d')}.log",
        )
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)

        # Error file handler
        error_file = os.path.join(
            log_dir, f"errors_{symbol}_{datetime.now().strftime('%Y%m%d')}.log"
        )
        error_handler = logging.handlers.RotatingFileHandler(
            error_file, maxBytes=5 * 1024 * 1024, backupCount=10
        )
        error_handler.setFormatter(formatter)
        error_handler.setLevel(logging.WARNING)
        logger.addHandler(error_handler)

        return logger

    def reset_logger(self):
        return self.setup_logging(self.symbol, self.log_level)


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
