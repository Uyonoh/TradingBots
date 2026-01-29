import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz
import time
import threading
from typing import Dict, Optional, Tuple
import logging
from dataclasses import dataclass
from enum import Enum
import json

# For MT5 (MetaTrader 5) - adjust for your broker/platform
import MetaTrader5 as mt5
# Alternative: use OANDA API or IBKR API

# -------------------------------------------------------------------
# 1. CONFIGURATION & LOGGING
# -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('dax_live_trading.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

PRO_SETUP = {
    "bias_filter": {"enabled": True, "buy_threshold": 0.75, "sell_threshold": 0.25},
    "entry_conditions": {"15min_buffer": 10, "velocity_multiplier": 1.5, "lookback_period": "60min"},
    "risk_management": {
        "initial_sl": 70,
        "trailing_stages": [
            {"min_profit": 0, "max_profit": 50, "retention": -1},
            {"min_profit": 50, "retention": -1}
        ],
        "tp_override": {"fast_threshold": 30, "slow_threshold": 180},
        "max_daily_trades": 1,
        "max_loss_per_trade": 2.0  # % of equity
    },
    "session_constraints": {"entry_start": "08:15", "mandatory_close": "17:30"},
    "broker": {
        "symbol": "GER40",  # MT5 symbol
        "account": 12345678,
        "password": "your_password",
        "server": "YourBrokerServer",
        "timeframe": mt5.TIMEFRAME_M1,
        "lot_size_multiplier": 1.0
    }
}

CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc

# -------------------------------------------------------------------
# 2. DATA STRUCTURES & ENUMS
# -------------------------------------------------------------------

class TradeDirection(Enum):
    BUY = "buy"
    SELL = "sell"
    NONE = "none"

class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"

@dataclass
class TickData:
    timestamp: datetime
    bid: float
    ask: float
    mid: float = None
    
    def __post_init__(self):
        if self.mid is None:
            self.mid = (self.bid + self.ask) / 2

@dataclass
class Trade:
    id: str
    direction: TradeDirection
    entry_price: float
    entry_time: datetime
    stop_loss: float
    max_profit: float = 0.0
    current_price: float = None
    exit_price: float = None
    exit_time: datetime = None
    status: OrderStatus = OrderStatus.OPEN
    pnl: float = 0.0
    reason: str = ""
    retention: float = 0.6

# -------------------------------------------------------------------
# 3. MARKET DATA MANAGER
# -------------------------------------------------------------------

class MarketDataManager:
    """Handles real-time tick data and signal calculations"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.ticks_buffer = []  # Store ticks for velocity calculation
        self.daily_ticks = []   # Store today's ticks for daily bias
        self.ghost_range = {"low": None, "high": None}
        self.daily_bias = None
        self.velocity_signal = False
        self.last_velocity_update = None
        
        # For velocity calculation
        self.tick_counts = []  # Store 1-second tick counts
        self.density_history = []  # Store 30-second densities
        
        # Initialize MT5
        self.init_mt5()
        
    def init_mt5(self):
        """Initialize MetaTrader 5 connection"""
        if not mt5.initialize():
            logger.error("MT5 initialization failed")
            raise ConnectionError("Failed to connect to MT5")
        logger.info("MT5 initialized successfully")
        
    def get_latest_tick(self) -> TickData:
        """Fetch latest tick from MT5"""
        try:
            tick = mt5.symbol_info_tick(PRO_SETUP["broker"]["symbol"])
            if tick:
                return TickData(
                    timestamp=datetime.now(UTC),
                    bid=tick.bid,
                    ask=tick.ask
                )
        except Exception as e:
            logger.error(f"Error fetching tick: {e}")
        return None
    
    def calculate_velocity(self, new_tick: TickData) -> bool:
        """
        Calculate velocity signal in real-time
        Returns True if velocity condition is met
        """
        current_time = new_tick.timestamp
        
        # Update tick counts for current second
        if not self.tick_counts:
            self.tick_counts.append((current_time, 1))
        else:
            last_time, count = self.tick_counts[-1]
            if current_time - last_time < timedelta(seconds=1):
                self.tick_counts[-1] = (last_time, count + 1)
            else:
                self.tick_counts.append((current_time, 1))
                
        # Keep only last 3600 seconds (60 minutes)
        cutoff = current_time - timedelta(minutes=60)
        self.tick_counts = [(t, c) for t, c in self.tick_counts if t > cutoff]
        
        # Calculate 30-second density
        thirty_sec_ago = current_time - timedelta(seconds=30)
        recent_ticks = sum(c for t, c in self.tick_counts if t > thirty_sec_ago)
        current_density = recent_ticks / 30
        
        # Calculate 60-minute average density
        if len(self.tick_counts) > 60:
            avg_density = sum(c for _, c in self.tick_counts) / len(self.tick_counts)
        else:
            avg_density = current_density
            
        # Update density history
        self.density_history.append(current_density)
        if len(self.density_history) > 3600:
            self.density_history.pop(0)
        
        # Check velocity condition
        velocity_threshold = avg_density * self.config["entry_conditions"]["velocity_multiplier"]
        self.velocity_signal = current_density > velocity_threshold
        
        return self.velocity_signal
    
    def update_daily_bias(self):
        """Calculate daily bias based on today's data so far"""
        if not self.daily_ticks:
            return
        
        # Convert to CET for daily calculation
        cet_ticks = []
        for tick in self.daily_ticks:
            cet_time = tick.timestamp.astimezone(CET)
            if cet_time.hour >= 8:  # Only consider data from market open
                cet_ticks.append(tick)
        
        if not cet_ticks:
            return
            
        mid_prices = [tick.mid for tick in cet_ticks]
        current_close = mid_prices[-1]
        daily_high = max(mid_prices)
        daily_low = min(mid_prices)
        
        if daily_high != daily_low:
            rc = (current_close - daily_low) / (daily_high - daily_low)
            
            if rc >= self.config["bias_filter"]["buy_threshold"]:
                self.daily_bias = TradeDirection.BUY
            elif rc <= self.config["bias_filter"]["sell_threshold"]:
                self.daily_bias = TradeDirection.SELL
            else:
                self.daily_bias = TradeDirection.NONE
        else:
            self.daily_bias = TradeDirection.NONE
            
        logger.info(f"Daily bias updated: {self.daily_bias}, RC: {rc if 'rc' in locals() else 'N/A'}")
    
    def update_ghost_range(self):
        """Update ghost range from 08:00-08:15 CET data"""
        if not self.daily_ticks:
            return
            
        ghost_data = []
        for tick in self.daily_ticks:
            cet_time = tick.timestamp.astimezone(CET)
            if cet_time.hour == 8 and cet_time.minute < 15:
                ghost_data.append(tick.mid)
        
        if ghost_data:
            self.ghost_range["low"] = min(ghost_data)
            self.ghost_range["high"] = max(ghost_data)
            logger.info(f"Ghost range updated: {self.ghost_range}")
    
    def is_in_session(self) -> bool:
        """Check if current time is within trading session"""
        now_cet = datetime.now(CET)
        current_time = now_cet.time()
        entry_start = datetime.strptime(self.config["session_constraints"]["entry_start"], "%H:%M").time()
        mandatory_close = datetime.strptime(self.config["session_constraints"]["mandatory_close"], "%H:%M").time()
        
        return entry_start <= current_time < mandatory_close
    
    def should_avoid_spread(self) -> bool:
        """Check if current time has wide spreads"""
        now_cet = datetime.now(CET)
        hour, minute = now_cet.hour, now_cet.minute
        
        # Avoid first 5 minutes and last 5 minutes
        if (hour == 8 and minute < 5) or (hour == 17 and minute > 25):
            return True
        return False

# -------------------------------------------------------------------
# 4. ORDER MANAGER
# -------------------------------------------------------------------

class OrderManager:
    """Handles order placement, modification, and tracking"""
    
    def __init__(self, config: Dict, data_manager: MarketDataManager):
        self.config = config
        self.data_manager = data_manager
        self.current_trade = None
        self.trade_history = []
        self.equity = 10000.0  # Starting equity
        self.daily_trades = 0
        self.daily_pnl = 0.0
        
    def calculate_position_size(self, stop_distance_ticks: float) -> float:
        """Calculate lot size based on risk management"""
        # Risk per trade: 2% of equity
        risk_amount = self.equity * (self.config["risk_management"]["max_loss_per_trade"] / 100)
        
        # Convert ticks to value (DAX: 1 tick = 1 EUR per lot)
        tick_value = 1.0
        
        # Calculate lots
        lots = risk_amount / (stop_distance_ticks * tick_value)
        
        # Apply multiplier from config
        lots *= self.config["broker"]["lot_size_multiplier"]
        
        # Round to allowed lot size
        min_lot = 0.01
        lots = max(min_lot, round(lots, 2))
        
        logger.info(f"Calculated lot size: {lots}, Risk: {risk_amount:.2f} EUR")
        return lots
    
    def place_order(self, direction: TradeDirection, entry_price: float, 
                   stop_loss: float, reason: str = "") -> Optional[Trade]:
        """Place an order with the broker"""
        
        if self.current_trade is not None:
            logger.warning("Cannot open new trade - one already open")
            return None
            
        # Check daily trade limit
        if self.daily_trades >= self.config["risk_management"]["max_daily_trades"]:
            logger.warning(f"Daily trade limit reached: {self.daily_trades}")
            return None
        
        # Calculate position size
        stop_distance = abs(entry_price - stop_loss)
        lots = self.calculate_position_size(stop_distance)
        
        # Prepare order request
        symbol = self.config["broker"]["symbol"]
        order_type = mt5.ORDER_TYPE_BUY if direction == TradeDirection.BUY else mt5.ORDER_TYPE_SELL
        sl_points = int(stop_distance * 10)  # Convert to points (DAX: 1 point = 0.1)
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": entry_price,
            "sl": stop_loss,
            "tp": 0.0,  # No take profit, using trailing stops
            "deviation": 10,
            "magic": 234000,
            "comment": f"DAX_Momentum_{reason}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        try:
            # Send order
            result = mt5.order_send(request)
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(f"Order failed: {result.comment}")
                return None
            
            # Create trade object
            trade = Trade(
                id=str(result.order),
                direction=direction,
                entry_price=entry_price,
                entry_time=datetime.now(UTC),
                stop_loss=stop_loss,
                status=OrderStatus.OPEN
            )
            
            self.current_trade = trade
            self.daily_trades += 1
            
            logger.info(f"Order placed: {direction} at {entry_price}, SL: {stop_loss}, Lots: {lots}")
            return trade
            
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None
    
    def update_trailing_stop(self, current_price: float):
        """Update trailing stop based on profit retention rules"""
        if not self.current_trade or self.current_trade.status != OrderStatus.OPEN:
            return
            
        trade = self.current_trade
        
        # Calculate current P&L
        if trade.direction == TradeDirection.BUY:
            trade.pnl = current_price - trade.entry_price
        else:
            trade.pnl = trade.entry_price - current_price
            
        # Update max profit
        trade.max_profit = max(trade.max_profit, trade.pnl)
        
        # Apply trailing stop logic
        initial_sl = self.config["risk_management"]["initial_sl"]
        stages = self.config["risk_management"]["trailing_stages"]
        
        retention = 0.6  # Default retention
        
        # Find applicable retention stage
        for stage in stages:
            if trade.max_profit >= stage["min_profit"]:
                if "max_profit" not in stage or trade.max_profit <= stage["max_profit"]:
                    retention = stage["retention"]
        
        # Calculate new stop level
        if retention == -1:
            # Fixed stop loss
            if trade.direction == TradeDirection.BUY:
                new_sl = trade.entry_price - initial_sl
            else:
                new_sl = trade.entry_price + initial_sl
        else:
            # Trailing stop
            trail_distance = trade.max_profit * retention
            if trade.direction == TradeDirection.BUY:
                new_sl = trade.entry_price + trail_distance
            else:
                new_sl = trade.entry_price - trail_distance
        
        # Only move stop in favorable direction
        if (trade.direction == TradeDirection.BUY and new_sl > trade.stop_loss) or \
           (trade.direction == TradeDirection.SELL and new_sl < trade.stop_loss):
            
            # Update stop loss with broker
            self.modify_stop_loss(trade, new_sl)
            trade.stop_loss = new_sl
            trade.retention = retention
    
    def modify_stop_loss(self, trade: Trade, new_sl: float) -> bool:
        """Modify stop loss with broker"""
        try:
            position = mt5.positions_get(ticket=int(trade.id))
            if position:
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": int(trade.id),
                    "sl": new_sl,
                    "tp": 0.0,
                    "deviation": 10,
                }
                result = mt5.order_send(request)
                return result.retcode == mt5.TRADE_RETCODE_DONE
        except Exception as e:
            logger.error(f"Error modifying stop loss: {e}")
        return False
    
    def close_trade(self, reason: str = "") -> bool:
        """Close current trade"""
        if not self.current_trade:
            return False
            
        trade = self.current_trade
        
        try:
            # Get current market price
            tick = self.data_manager.get_latest_tick()
            if not tick:
                return False
                
            # Determine close price
            close_price = tick.bid if trade.direction == TradeDirection.BUY else tick.ask
            
            # Prepare close request
            order_type = mt5.ORDER_TYPE_SELL if trade.direction == TradeDirection.BUY else mt5.ORDER_TYPE_BUY
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.config["broker"]["symbol"],
                "volume": 0.01,  # Will get actual volume from position
                "type": order_type,
                "position": int(trade.id),
                "price": close_price,
                "deviation": 10,
                "magic": 234000,
                "comment": f"Close_{reason}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            
            result = mt5.order_send(request)
            
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                # Update trade record
                trade.exit_price = close_price
                trade.exit_time = datetime.now(UTC)
                trade.status = OrderStatus.CLOSED
                trade.reason = reason
                
                # Calculate final P&L
                if trade.direction == TradeDirection.BUY:
                    trade.pnl = close_price - trade.entry_price
                else:
                    trade.pnl = trade.entry_price - close_price
                
                # Update equity
                self.equity += trade.pnl
                self.daily_pnl += trade.pnl
                
                # Move to history
                self.trade_history.append(trade)
                self.current_trade = None
                
                logger.info(f"Trade closed: {trade.direction} at {close_price}, P&L: {trade.pnl:.2f}, Reason: {reason}")
                return True
                
        except Exception as e:
            logger.error(f"Error closing trade: {e}")
            
        return False
    
    def check_stop_loss(self, current_price: float) -> bool:
        """Check if stop loss is hit"""
        if not self.current_trade:
            return False
            
        trade = self.current_trade
        
        if trade.direction == TradeDirection.BUY and current_price <= trade.stop_loss:
            self.close_trade(reason="stop_loss")
            return True
        elif trade.direction == TradeDirection.SELL and current_price >= trade.stop_loss:
            self.close_trade(reason="stop_loss")
            return True
            
        return False

# -------------------------------------------------------------------
# 5. STRATEGY ENGINE
# -------------------------------------------------------------------

class StrategyEngine:
    """Main strategy logic for entry and exit decisions"""
    
    def __init__(self, data_manager: MarketDataManager, order_manager: OrderManager):
        self.data_manager = data_manager
        self.order_manager = order_manager
        self.config = data_manager.config
        
        # Entry state tracking
        self.touched_opposite = False
        self.entry_buffer = self.config["entry_conditions"]["15min_buffer"]
        
        # For logging and monitoring
        self.last_check_time = None
        self.entry_attempts = 0
        
    def check_entry_conditions(self, tick: TickData) -> Optional[Trade]:
        """Check if entry conditions are met"""
        
        # Basic checks
        if not self.data_manager.is_in_session():
            return None
            
        if self.data_manager.should_avoid_spread():
            return None
            
        if self.order_manager.current_trade is not None:
            return None
            
        if self.data_manager.daily_bias is None or self.data_manager.daily_bias == TradeDirection.NONE:
            return None
            
        if not self.data_manager.ghost_range["low"] or not self.data_manager.ghost_range["high"]:
            return None
            
        # Get current values
        current_price = tick.mid
        ghost_low = self.data_manager.ghost_range["low"]
        ghost_high = self.data_manager.ghost_range["high"]
        
        # Check bias-specific entry conditions
        if self.data_manager.daily_bias == TradeDirection.BUY:
            # BUY ENTRY LOGIC
            
            # Check if touched opposite side first
            if not self.touched_opposite:
                if current_price <= ghost_low + self.entry_buffer:
                    self.touched_opposite = True
                    logger.info(f"BUY trap activated - touched low: {current_price}")
                return None
            
            # Check entry condition
            if (current_price >= ghost_high - self.entry_buffer and 
                self.data_manager.velocity_signal):
                
                # Calculate stop loss
                stop_loss = ghost_low - self.config["risk_management"]["initial_sl"]
                
                # Place order
                trade = self.order_manager.place_order(
                    direction=TradeDirection.BUY,
                    entry_price=current_price,
                    stop_loss=stop_loss,
                    reason="velocity_entry"
                )
                
                if trade:
                    self.entry_attempts += 1
                    self.touched_opposite = False
                    
                return trade
                
        elif self.data_manager.daily_bias == TradeDirection.SELL:
            # SELL ENTRY LOGIC
            
            # Check if touched opposite side first
            if not self.touched_opposite:
                if current_price >= ghost_high - self.entry_buffer:
                    self.touched_opposite = True
                    logger.info(f"SELL trap activated - touched high: {current_price}")
                return None
            
            # Check entry condition
            if (current_price <= ghost_low + self.entry_buffer and 
                self.data_manager.velocity_signal):
                
                # Calculate stop loss
                stop_loss = ghost_high + self.config["risk_management"]["initial_sl"]
                
                # Place order
                trade = self.order_manager.place_order(
                    direction=TradeDirection.SELL,
                    entry_price=current_price,
                    stop_loss=stop_loss,
                    reason="velocity_entry"
                )
                
                if trade:
                    self.entry_attempts += 1
                    self.touched_opposite = False
                    
                return trade
        
        return None
    
    def check_exit_conditions(self, tick: TickData):
        """Check if exit conditions are met"""
        if not self.order_manager.current_trade:
            return
            
        # Mandatory session close
        if not self.data_manager.is_in_session():
            self.order_manager.close_trade(reason="session_end")
            return
            
        # Check stop loss
        if self.order_manager.check_stop_loss(tick.mid):
            return
            
        # Update trailing stop
        self.order_manager.update_trailing_stop(tick.mid)
        
        # Optional: Check for early exit conditions
        # (Could add profit targets or time-based exits here)

# -------------------------------------------------------------------
# 6. MAIN TRADING BOT
# -------------------------------------------------------------------

class DAXTradingBot:
    """Main trading bot orchestrating all components"""
    
    def __init__(self, config: Dict = None):
        self.config = config or PRO_SETUP
        self.running = False
        self.thread = None
        
        # Initialize components
        self.data_manager = MarketDataManager(self.config)
        self.order_manager = OrderManager(self.config, self.data_manager)
        self.strategy_engine = StrategyEngine(self.data_manager, self.order_manager)
        
        # State tracking
        self.day_reset_done = False
        self.last_daily_reset = None
        
    def reset_daily_state(self):
        """Reset daily state variables"""
        current_date = datetime.now(CET).date()
        
        if self.last_daily_reset != current_date:
            self.data_manager.daily_bias = None
            self.data_manager.ghost_range = {"low": None, "high": None}
            self.strategy_engine.touched_opposite = False
            self.order_manager.daily_trades = 0
            self.order_manager.daily_pnl = 0.0
            self.last_daily_reset = current_date
            self.day_reset_done = False
            
            logger.info(f"Daily state reset for {current_date}")
    
    def check_market_open(self) -> bool:
        """Check if market is open"""
        now_cet = datetime.now(CET)
        
        # Market hours: 08:00 - 22:00 CET (adjust as needed)
        market_open = datetime.strptime("08:00", "%H:%M").time()
        market_close = datetime.strptime("22:00", "%H:%M").time()
        
        # Check weekday (DAX trades Mon-Fri)
        if now_cet.weekday() >= 5:  # Saturday or Sunday
            return False
            
        current_time = now_cet.time()
        return market_open <= current_time <= market_close
    
    def update_ghost_range_and_bias(self):
        """Update ghost range at 08:15 and bias throughout the day"""
        now_cet = datetime.now(CET)
        
        # Update ghost range at 08:15
        if now_cet.hour == 8 and now_cet.minute == 15 and not self.day_reset_done:
            self.data_manager.update_ghost_range()
            self.day_reset_done = True
            logger.info("Ghost range calculated")
        
        # Update daily bias periodically
        if now_cet.minute % 15 == 0:  # Every 15 minutes
            self.data_manager.update_daily_bias()
    
    def trading_loop(self):
        """Main trading loop"""
        logger.info("Starting trading loop...")
        
        while self.running:
            try:
                # Check market is open
                if not self.check_market_open():
                    time.sleep(60)
                    continue
                
                # Reset daily state if new day
                self.reset_daily_state()
                
                # Update ghost range and bias
                self.update_ghost_range_and_bias()
                
                # Get latest tick
                tick = self.data_manager.get_latest_tick()
                if not tick:
                    time.sleep(0.1)
                    continue
                
                # Store tick for calculations
                self.data_manager.daily_ticks.append(tick)
                self.data_manager.ticks_buffer.append(tick)
                
                # Keep buffer manageable
                if len(self.data_manager.ticks_buffer) > 10000:
                    self.data_manager.ticks_buffer = self.data_manager.ticks_buffer[-5000:]
                
                # Calculate velocity signal
                velocity_active = self.data_manager.calculate_velocity(tick)
                
                # Check entry conditions
                entry_trade = self.strategy_engine.check_entry_conditions(tick)
                
                # Check exit conditions
                self.strategy_engine.check_exit_conditions(tick)
                
                # Log status periodically
                current_time = datetime.now(CET)
                if current_time.minute % 5 == 0 and current_time.second < 1:
                    self.log_status(tick)
                
                # Small sleep to prevent CPU overload
                time.sleep(0.05)
                
            except Exception as e:
                logger.error(f"Error in trading loop: {e}")
                time.sleep(1)
    
    def log_status(self, tick: TickData):
        """Log current trading status"""
        status = {
            "time": datetime.now(CET).strftime("%H:%M:%S"),
            "price": tick.mid,
            "bias": self.data_manager.daily_bias.value if self.data_manager.daily_bias else "None",
            "ghost_range": self.data_manager.ghost_range,
            "velocity": self.data_manager.velocity_signal,
            "touched_opposite": self.strategy_engine.touched_opposite,
            "current_trade": self.order_manager.current_trade.id if self.order_manager.current_trade else "None",
            "equity": self.order_manager.equity,
            "daily_trades": self.order_manager.daily_trades,
            "daily_pnl": self.order_manager.daily_pnl
        }
        
        logger.info(f"Status: {status}")
    
    def start(self):
        """Start the trading bot"""
        if self.running:
            logger.warning("Bot is already running")
            return
            
        self.running = True
        self.thread = threading.Thread(target=self.trading_loop, daemon=True)
        self.thread.start()
        logger.info("Trading bot started")
    
    def stop(self):
        """Stop the trading bot"""
        self.running = False
        
        # Close any open trades
        if self.order_manager.current_trade:
            self.order_manager.close_trade(reason="bot_stop")
        
        # Shutdown MT5
        mt5.shutdown()
        
        logger.info("Trading bot stopped")
    
    def get_performance_report(self) -> Dict:
        """Generate performance report"""
        if not self.order_manager.trade_history:
            return {"message": "No trades yet"}
        
        trades = self.order_manager.trade_history
        
        # Calculate metrics
        winning_trades = [t for t in trades if t.pnl > 0]
        losing_trades = [t for t in trades if t.pnl <= 0]
        
        win_rate = len(winning_trades) / len(trades) * 100 if trades else 0
        total_pnl = sum(t.pnl for t in trades)
        avg_win = np.mean([t.pnl for t in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([t.pnl for t in losing_trades]) if losing_trades else 0
        
        return {
            "total_trades": len(trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "current_equity": round(self.order_manager.equity, 2),
            "daily_pnl": round(self.order_manager.daily_pnl, 2),
            "daily_trades": self.order_manager.daily_trades
        }

# -------------------------------------------------------------------
# 7. WEB DASHBOARD (Optional)
# -------------------------------------------------------------------

from flask import Flask, jsonify, render_template
import json

app = Flask(__name__)
bot_instance = None

@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/api/status')
def get_status():
    if not bot_instance:
        return jsonify({"error": "Bot not initialized"})
    
    # Get latest tick
    tick = bot_instance.data_manager.get_latest_tick()
    
    status = {
        "running": bot_instance.running,
        "time": datetime.now(CET).strftime("%Y-%m-%d %H:%M:%S"),
        "price": tick.mid if tick else 0,
        "bias": bot_instance.data_manager.daily_bias.value if bot_instance.data_manager.daily_bias else "None",
        "ghost_range": bot_instance.data_manager.ghost_range,
        "velocity": bot_instance.data_manager.velocity_signal,
        "current_trade": {
            "id": bot_instance.order_manager.current_trade.id if bot_instance.order_manager.current_trade else None,
            "direction": bot_instance.order_manager.current_trade.direction.value if bot_instance.order_manager.current_trade else None,
            "entry_price": bot_instance.order_manager.current_trade.entry_price if bot_instance.order_manager.current_trade else None,
            "pnl": bot_instance.order_manager.current_trade.pnl if bot_instance.order_manager.current_trade else None
        },
        "equity": bot_instance.order_manager.equity
    }
    
    return jsonify(status)

@app.route('/api/performance')
def get_performance():
    if not bot_instance:
        return jsonify({"error": "Bot not initialized"})
    
    report = bot_instance.get_performance_report()
    return jsonify(report)

@app.route('/api/start', methods=['POST'])
def start_bot():
    if bot_instance and not bot_instance.running:
        bot_instance.start()
        return jsonify({"message": "Bot started"})
    return jsonify({"message": "Bot already running or not initialized"})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    if bot_instance and bot_instance.running:
        bot_instance.stop()
        return jsonify({"message": "Bot stopped"})
    return jsonify({"message": "Bot not running"})

# -------------------------------------------------------------------
# 8. MAIN EXECUTION
# -------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='DAX Institutional Momentum Trading Bot')
    parser.add_argument('--mode', choices=['console', 'dashboard'], default='console',
                       help='Run mode: console or dashboard')
    parser.add_argument('--config', type=str, help='Path to config file')
    
    args = parser.parse_args()
    
    # Load config if provided
    if args.config:
        with open(args.config, 'r') as f:
            config = json.load(f)
    else:
        config = PRO_SETUP
    
    # Initialize bot
    bot = DAXTradingBot(config)
    
    if args.mode == 'dashboard':
        bot_instance = bot
        bot.start()
        print("Starting dashboard on http://localhost:5000")
        app.run(debug=False, host='0.0.0.0', port=5000)
    else:
        # Console mode
        try:
            print("Starting DAX Trading Bot...")
            print("Press Ctrl+C to stop")
            
            bot.start()
            
            # Keep main thread alive
            while True:
                time.sleep(1)
                
                # Print status every 30 seconds
                if datetime.now().second % 30 == 0:
                    report = bot.get_performance_report()
                    print(f"\n--- Status Update ---")
                    print(f"Equity: {report['current_equity']:.2f} EUR")
                    print(f"Daily P&L: {report['daily_pnl']:.2f} EUR")
                    print(f"Total Trades: {report['total_trades']}")
                    print(f"Win Rate: {report['win_rate']:.1f}%")
                    
        except KeyboardInterrupt:
            print("\nStopping bot...")
            bot.stop()
            sys.exit(0)