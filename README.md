# MT5 Trading Bot - Commented Documentation

  This is the main README file for the MT5 Trading Bot project
  All lines in this file are commented out to serve as documentation within the codebase

  -----------------------------------------------------------------
 ## PROJECT OVERVIEW
  -----------------------------------------------------------------

   This project implements a high-performance trading bot for MetaTrader 5
   The bot features algorithmic trading with real-time market analysis
   It is designed for production use with comprehensive error handling

  -----------------------------------------------------------------
 ## FEATURES SUMMARY
  -----------------------------------------------------------------

 ### ✅ Performance Optimized
  - Efficient tick processing with adaptive timing intervals
  - Cached calculations reduce redundant computations
  - Optimized data structures for minimal memory usage

 ### ✅ Structured Logging System
  - Multi-level logging (DEBUG, INFO, WARNING, ERROR, CRITICAL)
  - Log rotation prevents disk space exhaustion
  - Separate error logs for easier debugging

 ### ✅ Robust Error Handling
  - Automatic retry with exponential backoff
  - Connection recovery during network issues
  - Graceful shutdown on system signals

 ### ✅ Configuration Management
  - YAML/JSON configuration file support
  - Environment variable overrides
  - Hot-reload capability without restart

 ### ✅ Code Quality
  - Full type hints throughout the codebase
  - Comprehensive unit and integration tests
  - Modular architecture for maintainability

  -----------------------------------------------------------------
 ## INSTALLATION INSTRUCTIONS
  -----------------------------------------------------------------

  1. Clone the repository from the source control system
    git clone <repository-url>
    cd mt5-trading-bot

  2. Create and activate a Python virtual environment
    python -m venv venv
    source venv/bin/activate  # Linux/Mac
    venv\Scripts\activate     # Windows

  3. Install required dependencies
    pip install -r requirements.txt

  4. Configure environment variables in .env file
    ACCOUNT_ID=your_account_number
    PASSWORD=your_password
    SERVER=your_broker_server

  -----------------------------------------------------------------
 ## CONFIGURATION SETUP
  -----------------------------------------------------------------

  Generate a configuration template file:
     python trading_bot.py --generate-config config.yaml

  Example configuration structure:
  bias_filter:
    buy_threshold: 0.6    # Threshold for buy bias calculation
    sell_threshold: 0.4   # Threshold for sell bias calculation

  entry_conditions:
    buffer_pips: 10.0     # Buffer size in pips for entry/exit
    velocity_multiplier: 2.0  # Multiplier for high velocity detection
    lookback_seconds: 3600    # Lookback period for velocity calculation

  risk_management:
    initial_sl_pips: 50.0     # Initial stop loss in pips
    trailing_stages:          # Progressive trailing stop stages
      - min_profit: 0
        max_profit: 30
        retention: -1         # -1 means keep initial stop loss

  session:
    day_open: "09:00"         # Market open time (CET)
    ghost_start: "08:00"      # Start of ghost range period
    ghost_end: "08:30"        # End of ghost range period
    trading_start: "10:00"    # Start of trading session
    trading_end: "17:00"      # End of trading session

  -----------------------------------------------------------------
 ## USAGE EXAMPLES
  -----------------------------------------------------------------

  Basic usage with default configuration:
     python trading_bot.py EURUSD

  Custom configuration file:
     python trading_bot.py GBPUSD --config config.yaml

  Enable debug logging for troubleshooting:
     python trading_bot.py EURUSD --log-level DEBUG

  Validate configuration without executing trades:
     python trading_bot.py EURUSD --validate-config

  Enable hot-reload for configuration changes:
     python trading_bot.py EURUSD --config config.yaml --hot-reload

  Run in test mode with minimal trade size:
     python trading_bot.py EURUSD --test-mode

  -----------------------------------------------------------------
 ## TRADING STRATEGY EXPLANATION
  -----------------------------------------------------------------

  The bot implements a momentum-based trading strategy:

 1. Daily Bias Calculation
     - Analyzes yesterday's D1 candle close position
     - Calculates (close - low) / (high - low) ratio
     - Determines bias: BUY if ratio > buy_threshold
     - Determines bias: SELL if ratio < sell_threshold

 2. Ghost Range Detection
     - Captures price range during 08:00-08:30 CET
     - Establishes support/resistance levels
     - Used for entry/exit decision making

 3. Velocity Monitoring
     - Tracks tick density over 30-second windows
     - Compares current density to historical average
     - Identifies high-velocity periods for entries

 4. Entry Conditions
     - BUY: Price touches lower ghost range + buffer, then rises
     - SELL: Price touches upper ghost range - buffer, then falls
     - Requires: Price above/below daily open + high velocity

 5. Risk Management
     - Initial stop loss protects against adverse moves
     - Trailing stop locks in profits progressively
     - Mandatory close at session end prevents overnight risk

 -----------------------------------------------------------------
 ## ARCHITECTURE COMPONENTS
 -----------------------------------------------------------------

  TradingBot - Main orchestrator class managing the trading loop

  ConfigurationManager - Handles config loading, validation, hot-reload

  MT5ConnectionManager - Manages MT5 connection lifecycle and recovery

  TradingStrategy - Implements core trading logic and decision making

  VelocityMonitor - Analyzes tick velocity for market condition detection

  OrderManager - Executes trades with validation and error handling

  SessionTimeManager - Handles timezone conversions and session timing

  SymbolInfoCache - Caches symbol information to reduce API calls

 -----------------------------------------------------------------
 ## LOGGING SYSTEM
 -----------------------------------------------------------------

  Log files are stored in the 'logs' directory by default

  Main trading log: logs/trading_{SYMBOL}_{DATE}.log
  Error-specific log: logs/errors_{SYMBOL}_{DATE}.log

  Log format includes timestamp, component name, level, and message
  Example: 2024-01-15 10:30:15 - trading_bot.strategy - INFO - Daily bias: BUY

  Log rotation is configured to prevent disk space issues
  Maximum file size: 10MB (configurable)
  Backup count: 5 files per log type

  -----------------------------------------------------------------
 ## ERROR HANDLING STRATEGY
  -----------------------------------------------------------------

 ### Retry Mechanism:
  - Exponential backoff for failed operations
  - Maximum 3 retry attempts by default
  - Specific exception handling per operation type

 ### Connection Recovery:
  - Periodic connection health checks
  - Automatic reconnection on failure
  - Graceful degradation when disconnected

 ### Validation:
  - Configuration validation at startup
  - Parameter validation before trade execution
  - Runtime checks for data integrity

  -----------------------------------------------------------------
 ## PERFORMANCE OPTIMIZATIONS
  -----------------------------------------------------------------

 ### Caching Strategy:
  - Timezone calculations cached for 1 hour
  - Symbol information cached for 5 minutes
  - Buffer calculations cached per contract size

 ### Efficient Data Processing:
  - Deque data structure for tick timestamp tracking
  - Running sum for velocity average calculation
  - Numpy arrays for batch operations

 ### Adaptive Timing:
  - Configurable processing intervals
  - Adaptive sleep based on loop execution time
  - Outside trading hours sleep optimization

  -----------------------------------------------------------------
 ## TESTING FRAMEWORK
  -----------------------------------------------------------------

 ### Unit Tests:
  - Test individual components in isolation
  - Mock MT5 dependencies for reliable testing
  - Cover core business logic paths

 ### Integration Tests:
  - Test component interactions
  - Require MT5 connection (skipped by default)
  - Validate end-to-end workflows

 #### Test Execution:
     python -m pytest test_trading_bot.py -v          # Run all tests
     python -m pytest test_trading_bot.py -k "config" # Run specific tests

  -----------------------------------------------------------------
 ## DEVELOPMENT GUIDELINES
  -----------------------------------------------------------------

 ### Code Style:
  - Use type hints for all function parameters and returns
  - Follow Google-style docstring format
  - Maintain 100-character line length limit

 #### Type Checking:
     mypy trading_bot.py          # Type checking
     mypy --strict trading_bot.py # Strict type checking

 #### Code Formatting:
     black trading_bot.py         # Auto-format code
     isort trading_bot.py         # Sort imports

  -----------------------------------------------------------------
 ## DEPLOYMENT CHECKLIST
  -----------------------------------------------------------------

 ### Pre-deployment Verification:
  1. Configuration file validated with --validate-config
  2. Environment variables properly set in .env file
  3. Log directory exists and has write permissions
  4. MT5 terminal is running and accessible
  5. Test mode verification completed successfully

 ### Production Monitoring:
  - Monitor log files for errors and warnings
  - Track performance metrics in logs
  - Set up alerts for connection issues
  - Regular backup of configuration files

  -----------------------------------------------------------------
 ## TROUBLESHOOTING GUIDE
  -----------------------------------------------------------------

 ### Common Issues and Solutions:

 #### MT5 Connection Failed:
  - Verify MT5 terminal is running and logged in
  - Check account credentials in .env file
  - Ensure server address matches broker configuration

 #### Symbol Not Found:
  - Confirm symbol exists with your broker
  - Check symbol is enabled in MT5 Market Watch
  - Verify correct symbol format (e.g., EURUSD not EUR/USD)

 #### Configuration Errors:
  - Validate configuration with --validate-config flag
  - Check YAML/JSON syntax for errors
  - Verify all required configuration sections exist

 #### Permission Issues:
  - Ensure write permissions for log directory
  - Check file permissions for configuration files
  - Verify Python environment has necessary privileges

  -----------------------------------------------------------------
 ## SECURITY CONSIDERATIONS
  -----------------------------------------------------------------

 ### Credential Management:
  - Store credentials in .env file, not in code
  - Use environment variables for sensitive data
  - Restrict file permissions on configuration files

 ### Network Security:
  - Use secure connections to MT5 terminal
  - Implement connection timeout handling
  - Validate all incoming market data

 ### Risk Controls:
  - Implement maximum position size limits
  - Set daily loss limits
  - Include emergency stop functionality

  -----------------------------------------------------------------
 ## DISCLAIMER
  -----------------------------------------------------------------

 #### This software is for educational and research purposes only

 #### Trading financial instruments carries significant risk
 #### Past performance is not indicative of future results
 #### Always conduct thorough testing in demo accounts

 #### The authors assume no responsibility for financial losses
 #### Users are solely responsible for their trading decisions

  -----------------------------------------------------------------
 ## SUPPORT AND CONTRIBUTION
  -----------------------------------------------------------------

 ### Issue Reporting:
  - Use the issue tracker for bug reports
  - Include detailed reproduction steps
  - Provide relevant log excerpts

 ### Feature Requests:
  - Describe the desired functionality
  - Explain the use case