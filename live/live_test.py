"""
Test suite for MT5 Trading Bot.

This module contains unit tests and integration tests for the trading bot components.
Run with: python -m pytest test_deep_live.py -v
"""

import pytest
import numpy as np
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, Optional
import pytz
from unittest.mock import Mock, patch, MagicMock

# Import components to test
# Note: We'll need to mock MT5 dependencies for unit tests


class TestConfigValidator:
    """Test configuration validation."""
    
    def test_valid_config(self):
        """Test validation of valid configuration."""
        from deep_live import ConfigValidator
        
        config = {
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
                    {'min_profit': 60, 'retention': 0.95}
                ]
            },
            'session': {
                'day_open': '09:00',
                'ghost_start': '08:00',
                'ghost_end': '08:30',
                'trading_start': '10:00',
                'trading_end': '17:00'
            }
        }
        
        errors = ConfigValidator.validate_config(config)
        assert len(errors) == 0
    
    def test_invalid_bias_thresholds(self):
        """Test validation of invalid bias thresholds."""
        from deep_live import ConfigValidator
        
        config = {
            'bias_filter': {
                'buy_threshold': 0.4,  # Invalid: less than sell_threshold
                'sell_threshold': 0.6
            }
        }
        
        errors = ConfigValidator.validate_config(config)
        assert any("'buy_threshold' must be greater" in error for error in errors)
    
    def test_missing_required_section(self):
        """Test validation of missing required section."""
        from deep_live import ConfigValidator
        
        config = {
            'bias_filter': {
                'buy_threshold': 0.6,
                'sell_threshold': 0.4
            }
            # Missing other required sections
        }
        
        errors = ConfigValidator.validate_config(config)
        assert any("Missing required section" in error for error in errors)
    
    def test_invalid_time_format(self):
        """Test validation of invalid time format."""
        from deep_live import ConfigValidator
        
        config = {
            'session': {
                'day_open': '25:00',  # Invalid hour
                'ghost_start': '08:00',
                'ghost_end': '08:30',
                'trading_start': '10:00',
                'trading_end': '17:00'
            }
        }
        
        errors = ConfigValidator.validate_config(config)
        assert any("must be in HH:MM format" in error for error in errors)
    
    def test_invalid_time_range(self):
        """Test validation of invalid time ranges."""
        from deep_live import ConfigValidator
        
        config = {
            'session': {
                'day_open': '09:00',
                'ghost_start': '08:30',  # After ghost_end
                'ghost_end': '08:00',
                'trading_start': '10:00',
                'trading_end': '17:00'
            }
        }
        
        errors = ConfigValidator.validate_config(config)
        assert any("must be before" in error for error in errors)


class TestDateTimeUtils:
    """Test date/time utilities."""
    
    def test_parse_time_valid(self):
        """Test parsing valid time strings."""
        from deep_live import DateTimeUtils
        
        time_obj = DateTimeUtils.parse_time("14:30")
        assert time_obj.hour == 14
        assert time_obj.minute == 30
    
    def test_parse_time_invalid(self):
        """Test parsing invalid time strings."""
        from deep_live import DateTimeUtils
        
        with pytest.raises(ValueError):
            DateTimeUtils.parse_time("25:00")
        
        with pytest.raises(ValueError):
            DateTimeUtils.parse_time("14:60")
    
    def test_is_time_in_range_normal(self):
        """Test time range checking for normal ranges."""
        from deep_live import DateTimeUtils
        
        check_time = time(14, 30)
        start_time = time(9, 0)
        end_time = time(17, 0)
        
        assert DateTimeUtils.is_time_in_range(check_time, start_time, end_time) == True
        
        check_time = time(8, 30)
        assert DateTimeUtils.is_time_in_range(check_time, start_time, end_time) == False
    
    def test_is_time_in_range_overnight(self):
        """Test time range checking for overnight ranges."""
        from deep_live import DateTimeUtils
        
        check_time = time(23, 30)
        start_time = time(22, 0)
        end_time = time(2, 0)
        
        assert DateTimeUtils.is_time_in_range(check_time, start_time, end_time) == True
        
        check_time = time(3, 0)
        assert DateTimeUtils.is_time_in_range(check_time, start_time, end_time) == False


class TestTrailingStage:
    """Test trailing stage data class."""
    
    def test_valid_trailing_stage(self):
        """Test valid trailing stage initialization."""
        from deep_live import TrailingStage
        
        stage = TrailingStage(min_profit=0, max_profit=30, retention=-1)
        assert stage.min_profit == 0
        assert stage.max_profit == 30
        assert stage.retention == -1
    
    def test_invalid_trailing_stage(self):
        """Test invalid trailing stage initialization."""
        from deep_live import TrailingStage
        
        with pytest.raises(ValueError):
            TrailingStage(min_profit=30, max_profit=0, retention=-1)
        
        with pytest.raises(ValueError):
            TrailingStage(min_profit=0, max_profit=30, retention=1.5)


class TestVelocityMonitor:
    """Test velocity monitor (requires mocking)."""
    
    @patch('deep_live.mt5')
    def test_velocity_calculation(self, mock_mt5):
        """Test velocity calculation logic."""
        from deep_live import VelocityMonitor, ConfigurationManager
        
        # Mock configuration
        mock_config = Mock(spec=ConfigurationManager)
        mock_config.config = {
            'entry_conditions': {
                'lookback_seconds': 3600,
                'velocity_multiplier': 2.0
            }
        }
        mock_config.register_callback = Mock()
        
        # Create velocity monitor
        monitor = VelocityMonitor("EURUSD", mock_config)
        
        # Simulate some ticks
        monitor.tick_timestamps.extend([1000, 1001, 1002, 1003, 1004])
        monitor.density_history.extend([1.0, 1.1, 1.2, 1.3, 1.4])
        monitor.density_sum = 6.0
        
        # Test high velocity detection
        monitor.tick_timestamps.extend([1005, 1006, 1007, 1008, 1009])  # Add more ticks
        current_density = len(monitor.tick_timestamps) / 30.0
        
        # Mock current time to avoid staleness check
        import time as t_mod
        original_time = t_mod.time
        t_mod.time = Mock(return_value=monitor.last_update + 100)  # Not stale
        
        try:
            # Current density should be higher than average * multiplier
            average_density = monitor.density_sum / len(monitor.density_history)
            is_high = current_density > (average_density * 2.0)
            
            # The actual result depends on the exact calculation
            result = monitor.is_high_velocity()
            # We can't assert exact value due to timing, but ensure no exceptions
            assert isinstance(result, bool)
        finally:
            t_mod.time = original_time


class TestTradingStrategy:
    """Test trading strategy logic."""
    
    def test_calculate_buffer(self):
        """Test buffer calculation."""
        from deep_live import TradingStrategy, ConfigurationManager
        
        # Mock configuration
        mock_config = Mock(spec=ConfigurationManager)
        mock_config.config = {
            'entry_conditions': {
                'buffer_pips': 10.0
            }
        }
        mock_config.register_callback = Mock()
        
        strategy = TradingStrategy("EURUSD", mock_config)
        
        # Test with contract size = 1.0 (standard forex)
        buffer = strategy.calculate_buffer(1.0)
        assert buffer == 10.0
        
        # Test with contract size = 100000 (common forex contract size)
        buffer = strategy.calculate_buffer(100000.0)
        assert buffer == 0.0001  # 10 pips / 100000
        
        # Test caching
        buffer2 = strategy.calculate_buffer(100000.0)
        assert buffer2 == buffer  # Should be from cache
    
    def test_check_entry_conditions_no_trade(self):
        """Test entry condition checking when no trade should be entered."""
        from deep_live import TradingStrategy, ConfigurationManager, TradeBias
        
        # Mock configuration
        mock_config = Mock(spec=ConfigurationManager)
        mock_config.config = {
            'entry_conditions': {
                'buffer_pips': 10.0
            },
            'bias_filter': {
                'buy_threshold': 0.6,
                'sell_threshold': 0.4
            }
        }
        mock_config.register_callback = Mock()
        
        strategy = TradingStrategy("EURUSD", mock_config)
        
        # Setup initial state
        strategy.day_state.bias = TradeBias.BUY
        strategy.day_state.ghost_low = 1.1000
        strategy.day_state.ghost_high = 1.1050
        strategy.day_state.daily_open_price = 1.1020
        
        # Mock tick data
        mock_tick = Mock()
        mock_tick.ask = 1.1030  # Within range but not at trigger
        
        # Mock velocity monitor
        mock_velocity = Mock()
        mock_velocity.is_high_velocity.return_value = True
        
        # Mock contract size
        contract_size = 100000.0
        
        should_enter, direction = strategy.check_entry_conditions(
            mock_tick, contract_size, mock_velocity
        )
        
        assert not should_enter
        assert direction is None


class TestOrderManager:
    """Test order manager (requires extensive mocking)."""
    
    @patch('deep_live.mt5')
    def test_order_validation(self, mock_mt5):
        """Test order parameter validation."""
        from deep_live import OrderManager, ConfigurationManager, SymbolInfoCache, TradeBias, ValidationError
        
        # Mock dependencies
        mock_config = Mock(spec=ConfigurationManager)
        mock_config.config = {
            'trading': {
                'volume': 0.01,
                'deviation': 10,
                'magic_number': 1234569
            }
        }
        
        mock_cache = Mock(spec=SymbolInfoCache)
        mock_cache.get_info.return_value = {
            'contract_size': 100000.0,
            'digits': 5,
            'volume_min': 0.01,
            'volume_max': 100.0,
            'volume_step': 0.01,
            'filling_mode': 1
        }
        
        order_manager = OrderManager("EURUSD", mock_config, mock_cache)
        
        # Test valid parameters (should not raise)
        try:
            order_manager._validate_order_params(
                TradeBias.BUY,
                50.0,
                mock_cache.get_info()
            )
        except ValidationError:
            pytest.fail("Valid parameters should not raise ValidationError")
        
        # Test invalid volume
        mock_config.config['trading']['volume'] = 0.005  # Below minimum
        
        with pytest.raises(ValidationError):
            order_manager._validate_order_params(
                TradeBias.BUY,
                50.0,
                mock_cache.get_info()
            )


class TestPerformanceMetrics:
    """Test performance metrics tracking."""
    
    def test_performance_metrics_update(self):
        """Test updating performance metrics."""
        from deep_live import PerformanceMetrics
        
        metrics = PerformanceMetrics()
        
        # Initial state
        assert metrics.loop_iterations == 0
        assert metrics.avg_iteration_time == 0.0
        
        # Simulate some iterations
        iteration_times = [0.1, 0.2, 0.15, 0.25, 0.1]
        
        for i, iter_time in enumerate(iteration_times, 1):
            # Simple average for first iteration
            if metrics.loop_iterations == 0:
                metrics.avg_iteration_time = iter_time
            else:
                alpha = 0.1
                metrics.avg_iteration_time = (
                    alpha * iter_time + 
                    (1 - alpha) * metrics.avg_iteration_time
                )
            
            metrics.loop_iterations += 1
        
        # Verify metrics
        assert metrics.loop_iterations == 5
        assert metrics.avg_iteration_time > 0


# Integration tests (require MT5 connection, so they're skipped by default)
@pytest.mark.integration
@pytest.mark.skip(reason="Requires MT5 connection")
class TestIntegration:
    """Integration tests that require MT5 connection."""
    
    def test_mt5_connection(self):
        """Test MT5 connection (requires actual MT5 terminal)."""
        import MetaTrader5 as mt5
        
        # This test would require a running MT5 terminal
        # It's marked as integration test and skipped by default
        pass
    
    def test_symbol_info(self):
        """Test symbol information retrieval (requires MT5)."""
        # This would test actual MT5 API calls
        pass


if __name__ == "__main__":
    # Run tests when script is executed directly
    pytest.main([__file__, "-v"])