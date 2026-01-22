# BTCUSD TICK-BASED BACKTESTING SCRIPT
# Uses MT5.copy_ticks_range for maximum granularity
# Implements ladder strategy with simultaneous buy/sell orders

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import pytz
from typing import List, Dict, Tuple, Optional
import warnings
warnings.filterwarnings('ignore')

# -----------------------------
# 1. TICK-BASED DATA STRUCTURES
# -----------------------------
class Tick:
    def __init__(self, time: datetime, bid: float, ask: float, volume: int):
        self.time = time
        self.bid = bid
        self.ask = ask
        self.volume = volume
        self.mid = (bid + ask) / 2

class LadderOrder:
    """Represents a ladder order in the strategy"""
    def __init__(self, order_id: str, order_type: str, price: float, lot_size: float, 
                 tp_price: float = None, sl_price: float = None, linked_to: str = None):
        self.id = order_id
        self.type = order_type  # "BUY_STOP", "SELL_STOP", "BUY_LIMIT", "SELL_LIMIT"
        self.price = price
        self.lot_size = lot_size
        self.tp = tp_price
        self.sl = sl_price
        self.linked_to = linked_to  # ID of linked opposite order
        self.filled = False
        self.fill_time = None
        self.fill_price = None
        self.active = True
    
    def __repr__(self):
        return f"{self.type}@{self.price:.2f} [TP:{self.tp:.2f}|SL:{self.sl:.2f}]"

class LadderPosition:
    """Represents an open position from ladder strategy"""
    def __init__(self, position_id: str, side: str, entry_price: float, lot_size: float, 
                 tp_price: float, sl_price: float, entry_time: datetime, ladder_level: int):
        self.id = position_id
        self.side = side  # "LONG" or "SHORT"
        self.entry_price = entry_price
        self.lot_size = lot_size
        self.tp = tp_price
        self.sl = sl_price
        self.entry_time = entry_time
        self.exit_price = None
        self.exit_time = None
        self.pnl = 0.0
        self.open = True
        self.ladder_level = ladder_level  # 1st, 2nd, 3rd level in ladder
        self.linked_orders = []  # Orders that were triggered by this position
    
    def __repr__(self):
        status = "OPEN" if self.open else "CLOSED"
        return f"{self.side}_{self.ladder_level}@{self.entry_price:.2f} [{status}]"

# -----------------------------
# 2. TICK-BASED LADDER STRATEGY
# -----------------------------
class TickBasedLadderStrategy:
    def __init__(self, symbol: str = "BTCUSD", account_balance: float = 100):
        self.symbol = symbol
        self.account_balance = account_balance
        self.current_balance = account_balance
        self.equity = account_balance
        
        # Strategy parameters
        self.entry_distance = 10.0  # Fixed distance for ladder steps (in USD)
        self.tp_distance = 5.0      # Take profit distance (in USD)
        self.lot_size = 0.01         # Base lot size (0.01 BTC)
        self.num_steps = 5           # Number of ladder steps above and below
        
        # Daily schedule
        self.trading_start_time = time(0, 0)  # Midnight UTC
        self.trading_end_time = time(23, 59, 59)
        
        # Tracking structures
        self.daily_orders = []      # Orders placed for current day
        self.open_positions = []    # Currently open positions
        self.closed_positions = []  # Closed positions (history)
        self.order_log = []         # Order placement/execution log
        self.trade_log = []         # Trade execution log
        
        # Day tracking
        self.current_day = None
        self.daily_open_price = None
        self.daily_setup_complete = False
        
        # Performance metrics
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.max_profit = 0.0
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        
        # Tick data statistics
        self.total_ticks = 0
        self.processed_ticks = 0
        self.missed_ticks = 0
        
    def initialize_mt5(self):
        """Initialize MT5 connection"""
        if not mt5.initialize():
            print("MT5 initialization failed")
            mt5.shutdown()
            return False
        
        # Select symbol
        if not mt5.symbol_select(self.symbol, True):
            print(f"Symbol {self.symbol} not found")
            mt5.shutdown()
            return False
        
        # Get symbol info
        self.symbol_info = mt5.symbol_info(self.symbol)
        if self.symbol_info is None:
            print(f"Cannot get symbol info for {self.symbol}")
            mt5.shutdown()
            return False
        
        print(f"Initialized {self.symbol}: Point={self.symbol_info.point}, Spread={self.symbol_info.spread}")
        return True
    
    def get_tick_data_range(self, start_date: datetime, end_date: datetime, 
                           max_ticks: int = 10000000) -> List[Tick]:
        """Retrieve tick data for the specified range"""
        print(f"Fetching tick data from {start_date} to {end_date}")
        
        # Convert to timestamp
        from_date = int(start_date.timestamp())
        to_date = int(end_date.timestamp())
        
        # Get ticks
        ticks = mt5.copy_ticks_range(self.symbol, from_date, to_date, mt5.COPY_TICKS_ALL)
        
        if ticks is None or len(ticks) == 0:
            print("No tick data retrieved")
            return []
        
        # Convert to list of Tick objects
        tick_objects = []
        for tick in ticks:
            tick_time = datetime.fromtimestamp(tick['time'])
            tick_obj = Tick(
                time=tick_time,
                bid=tick['bid'],
                ask=tick['ask'],
                volume=tick['volume']
            )
            tick_objects.append(tick_obj)
        
        print(f"Retrieved {len(tick_objects)} ticks")
        return tick_objects
    
    def setup_daily_ladder(self, open_price: float, current_time: datetime):
        """Setup the ladder orders for the day based on opening price"""
        print(f"\n{'='*60}")
        print(f"Setting up daily ladder at {current_time}")
        print(f"Open Price: ${open_price:.2f}")
        print(f"{'='*60}")
        
        self.daily_open_price = open_price
        self.current_day = current_time.date()
        self.daily_orders = []
        
        # Generate order ID prefix
        order_prefix = f"{self.current_day.strftime('%Y%m%d')}"
        
        # Setup BUY ladder above open price
        for i in range(1, self.num_steps + 1):
            order_price = open_price + (i * self.entry_distance)
            
            # Calculate TP and SL
            tp_price = order_price + self.tp_distance
            sl_price = open_price if i == 1 else (open_price + ((i-1) * self.entry_distance))
            
            # Create BUY STOP order
            buy_order = LadderOrder(
                order_id=f"{order_prefix}_BUY_STOP_{i}",
                order_type="BUY_STOP",
                price=order_price,
                lot_size=self.lot_size,
                tp_price=tp_price,
                sl_price=sl_price
            )
            
            # For first level only, create linked SELL LIMIT at same price
            if i == 1:
                sell_limit = LadderOrder(
                    order_id=f"{order_prefix}_SELL_LIMIT_{i}",
                    order_type="SELL_LIMIT",
                    price=order_price,
                    lot_size=self.lot_size,
                    tp_price=order_price - self.tp_distance,
                    sl_price=order_price + self.tp_distance,
                    linked_to=buy_order.id
                )
                buy_order.linked_to = sell_limit.id
                self.daily_orders.append(sell_limit)
                self.log_order(f"Placed SELL_LIMIT at ${order_price:.2f} [TP:${sell_limit.tp:.2f}|SL:${sell_limit.sl:.2f}]")
            
            self.daily_orders.append(buy_order)
            self.log_order(f"Placed BUY_STOP_{i} at ${order_price:.2f} [TP:${tp_price:.2f}|SL:${sl_price:.2f}]")
        
        # Setup SELL ladder below open price
        for i in range(1, self.num_steps + 1):
            order_price = open_price - (i * self.entry_distance)
            
            # Calculate TP and SL
            tp_price = order_price - self.tp_distance
            sl_price = open_price if i == 1 else (open_price - ((i-1) * self.entry_distance))
            
            # Create SELL STOP order
            sell_order = LadderOrder(
                order_id=f"{order_prefix}_SELL_STOP_{i}",
                order_type="SELL_STOP",
                price=order_price,
                lot_size=self.lot_size,
                tp_price=tp_price,
                sl_price=sl_price
            )
            
            # For first level only, create linked BUY LIMIT at same price
            if i == 1:
                buy_limit = LadderOrder(
                    order_id=f"{order_prefix}_BUY_LIMIT_{i}",
                    order_type="BUY_LIMIT",
                    price=order_price,
                    lot_size=self.lot_size,
                    tp_price=order_price + self.tp_distance,
                    sl_price=order_price - self.tp_distance,
                    linked_to=sell_order.id
                )
                sell_order.linked_to = buy_limit.id
                self.daily_orders.append(buy_limit)
                self.log_order(f"Placed BUY_LIMIT at ${order_price:.2f} [TP:${buy_limit.tp:.2f}|SL:${buy_limit.sl:.2f}]")
            
            self.daily_orders.append(sell_order)
            self.log_order(f"Placed SELL_STOP_{i} at ${order_price:.2f} [TP:${tp_price:.2f}|SL:${sl_price:.2f}]")
        
        self.daily_setup_complete = True
        print(f"Daily ladder setup complete: {len(self.daily_orders)} orders placed")
    
    def check_order_triggers(self, tick: Tick):
        """Check if any pending orders are triggered by current tick"""
        triggered_orders = []
        
        for order in self.daily_orders:
            if not order.active or order.filled:
                continue
            
            is_triggered = False
            
            if order.type == "BUY_STOP":
                # Buy stop triggers when ask price reaches or exceeds order price
                if tick.ask >= order.price:
                    is_triggered = True
                    fill_price = max(tick.ask, order.price)  # Slippage simulation
            
            elif order.type == "SELL_STOP":
                # Sell stop triggers when bid price reaches or goes below order price
                if tick.bid <= order.price:
                    is_triggered = True
                    fill_price = min(tick.bid, order.price)  # Slippage simulation
            
            elif order.type == "BUY_LIMIT":
                # Buy limit triggers when ask price reaches or goes below order price
                if tick.ask <= order.price:
                    is_triggered = True
                    fill_price = min(tick.ask, order.price)
            
            elif order.type == "SELL_LIMIT":
                # Sell limit triggers when bid price reaches or exceeds order price
                if tick.bid >= order.price:
                    is_triggered = True
                    fill_price = max(tick.bid, order.price)
            
            if is_triggered:
                order.filled = True
                order.fill_time = tick.time
                order.fill_price = fill_price
                triggered_orders.append(order)
        
        # Process triggered orders
        for order in triggered_orders:
            self.execute_order(order, tick)
    
    def execute_order(self, order: LadderOrder, tick: Tick):
        """Execute a triggered order and open a position"""
        # Determine position side
        if order.type in ["BUY_STOP", "BUY_LIMIT"]:
            side = "LONG"
        else:
            side = "SHORT"
        
        # Determine ladder level from order ID
        level = 1
        if "BUY_STOP_" in order.id:
            level = int(order.id.split("_")[-1])
        elif "SELL_STOP_" in order.id:
            level = int(order.id.split("_")[-1])
        
        # Create position
        position_id = f"{order.id}_POS"
        position = LadderPosition(
            position_id=position_id,
            side=side,
            entry_price=order.fill_price,
            lot_size=order.lot_size,
            tp_price=order.tp,
            sl_price=order.sl,
            entry_time=tick.time,
            ladder_level=level
        )
        
        self.open_positions.append(position)
        
        # If this is a linked order (BUY_STOP with SELL_LIMIT at same price)
        if order.linked_to:
            # Find and trigger the linked order simultaneously
            linked_order = next((o for o in self.daily_orders if o.id == order.linked_to), None)
            if linked_order and not linked_order.filled:
                linked_order.filled = True
                linked_order.fill_time = tick.time
                linked_order.fill_price = order.fill_price
                
                # Execute linked order immediately
                self.execute_order(linked_order, tick)
        
        self.log_trade(f"{side} position opened at ${order.fill_price:.2f} (Level {level})")
        self.total_trades += 1
    
    def check_position_exits(self, tick: Tick):
        """Check if any open positions should be closed (TP/SL)"""
        positions_to_close = []
        
        for position in self.open_positions:
            if not position.open:
                continue
            
            exit_reason = None
            exit_price = None
            
            if position.side == "LONG":
                # Check Take Profit
                if tick.bid >= position.tp:
                    exit_reason = "TP"
                    exit_price = position.tp
                
                # Check Stop Loss
                elif tick.bid <= position.sl:
                    exit_reason = "SL"
                    exit_price = position.sl
            
            else:  # SHORT position
                # Check Take Profit
                if tick.ask <= position.tp:
                    exit_reason = "TP"
                    exit_price = position.tp
                
                # Check Stop Loss
                elif tick.ask >= position.sl:
                    exit_reason = "SL"
                    exit_price = position.sl
            
            if exit_reason:
                positions_to_close.append((position, exit_reason, exit_price))
        
        # Close positions
        for position, reason, price in positions_to_close:
            self.close_position(position, price, tick.time, reason)
    
    def close_position(self, position: LadderPosition, exit_price: float, 
                      exit_time: datetime, reason: str):
        """Close a position and calculate P&L"""
        position.open = False
        position.exit_price = exit_price
        position.exit_time = exit_time
        
        # Calculate P&L
        if position.side == "LONG":
            position.pnl = (exit_price - position.entry_price) * position.lot_size
        else:  # SHORT
            position.pnl = (position.entry_price - exit_price) * position.lot_size
        
        # Update balance
        self.current_balance += position.pnl
        
        # Update statistics
        self.total_pnl += position.pnl
        
        if position.pnl > 0:
            self.winning_trades += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.losing_trades += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        
        # Update max profit/drawdown
        if self.total_pnl > self.max_profit:
            self.max_profit = self.total_pnl
        
        current_drawdown = self.max_profit - self.total_pnl
        if current_drawdown > self.max_drawdown:
            self.max_drawdown = current_drawdown
        
        # Move to closed positions
        self.open_positions.remove(position)
        self.closed_positions.append(position)
        
        holding_time = (exit_time - position.entry_time).total_seconds() / 60  # minutes
        
        self.log_trade(f"{position.side} position closed at ${exit_price:.2f} ({reason}) "
                      f"P&L: ${position.pnl:.2f} | Holding: {holding_time:.1f} min")
    
    def end_of_day_closeout(self, tick: Tick):
        """Close all positions and cancel all orders at end of day"""
        print(f"\n{'='*60}")
        print(f"END OF DAY CLOSEOUT at {tick.time}")
        print(f"{'='*60}")
        
        # Close all open positions at current market price
        positions_to_close = self.open_positions.copy()
        for position in positions_to_close:
            if position.side == "LONG":
                exit_price = tick.bid
            else:
                exit_price = tick.ask
            
            self.close_position(position, exit_price, tick.time, "EOD")
        
        # Cancel all pending orders
        cancelled_count = 0
        for order in self.daily_orders:
            if not order.filled and order.active:
                order.active = False
                cancelled_count += 1
        
        self.log_order(f"Cancelled {cancelled_count} pending orders")
        
        # Reset daily setup
        self.daily_setup_complete = False
        self.daily_open_price = None
    
    def calculate_spread_impact(self, tick: Tick) -> float:
        """Calculate spread impact on order execution"""
        spread = tick.ask - tick.bid
        spread_percentage = (spread / tick.bid) * 100
        
        # Adjust for typical BTCUSD spreads
        if spread_percentage > 0.05:  # > 0.05% spread
            return 1.5  # High spread multiplier
        elif spread_percentage > 0.02:  # > 0.02% spread
            return 1.2  # Medium spread multiplier
        else:
            return 1.0  # Normal spread
    
    def simulate_slippage(self, order_type: str, order_price: float, tick: Tick) -> float:
        """Simulate realistic slippage for BTCUSD"""
        base_slippage = 0.0005  # 0.05% base slippage
        
        # Adjust for order type
        if order_type in ["BUY_STOP", "SELL_LIMIT"]:
            # Slippage for orders triggered on price rise
            slippage = base_slippage * order_price
        else:
            # Slippage for orders triggered on price fall
            slippage = -base_slippage * order_price
        
        # Add randomness
        random_component = np.random.normal(0, slippage * 0.3)
        
        # Adjust for volatility (simplified)
        recent_volatility = abs(tick.ask - tick.bid) / tick.bid
        volatility_multiplier = 1 + (recent_volatility * 10)
        
        final_slippage = slippage * volatility_multiplier + random_component
        
        return final_slippage
    
    def run_tick_backtest(self, tick_data: List[Tick]):
        """Run the backtest on tick-by-tick data"""
        print(f"\n{'='*60}")
        print(f"STARTING TICK-BASED BACKTEST")
        print(f"Total ticks: {len(tick_data)}")
        print(f"Date range: {tick_data[0].time} to {tick_data[-1].time}")
        print(f"{'='*60}")
        
        self.total_ticks = len(tick_data)
        
        # Group ticks by day
        current_day = None
        day_ticks = []
        
        for i, tick in enumerate(tick_data):
            self.processed_ticks += 1
            
            # Progress reporting
            if i % 100000 == 0 and i > 0:
                progress = (i / len(tick_data)) * 100
                print(f"Processed {i:,} ticks ({progress:.1f}%)...")
            
            # Check if new day
            if tick.time.date() != current_day:
                if current_day is not None and day_ticks:
                    # Process previous day
                    self.process_day_ticks(day_ticks)
                
                # Start new day
                current_day = tick.time.date()
                day_ticks = [tick]
            else:
                day_ticks.append(tick)
        
        # Process last day
        if day_ticks:
            self.process_day_ticks(day_ticks)
    
    def process_day_ticks(self, day_ticks: List[Tick]):
        """Process all ticks for a single trading day"""
        if not day_ticks:
            return
        
        day_start = day_ticks[0].time
        day_end = day_ticks[-1].time
        
        print(f"\nProcessing day: {day_start.date()}")
        print(f"Day ticks: {len(day_ticks)}")
        
        # Reset daily state
        self.daily_setup_complete = False
        
        # Process each tick in the day
        for tick in day_ticks:
            # Check if it's time for daily setup
            if (not self.daily_setup_completed and 
                tick.time.time() >= self.trading_start_time):
                
                # Use first tick after start time as open price
                self.setup_daily_ladder(tick.mid, tick.time)
            
            # Only process if setup is complete
            if self.daily_setup_complete:
                # Check order triggers
                self.check_order_triggers(tick)
                
                # Check position exits
                self.check_position_exits(tick)
                
                # Update equity
                self.update_equity(tick)
            
            # Check if end of trading day
            if tick.time.time() >= self.trading_end_time:
                self.end_of_day_closeout(tick)
                break
    
    def update_equity(self, tick: Tick):
        """Update current equity including floating P&L"""
        floating_pnl = 0
        
        for position in self.open_positions:
            if position.side == "LONG":
                floating_pnl += (tick.bid - position.entry_price) * position.lot_size
            else:  # SHORT
                floating_pnl += (position.entry_price - tick.ask) * position.lot_size
        
        self.equity = self.current_balance + floating_pnl
    
    def log_order(self, message: str):
        """Log order-related messages"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[ORDER] {message}"
        self.order_log.append(log_entry)
        print(log_entry)
    
    def log_trade(self, message: str):
        """Log trade-related messages"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[TRADE] {message}"
        self.trade_log.append(log_entry)
        print(log_entry)
    
    def generate_performance_report(self):
        """Generate comprehensive performance report"""
        if not self.closed_positions:
            print("No trades executed during backtest")
            return
        
        print(f"\n{'='*80}")
        print("TICK-BASED BACKTEST PERFORMANCE REPORT")
        print(f"{'='*80}")
        
        # Basic statistics
        initial_balance = self.account_balance
        final_balance = self.current_balance
        total_return = ((final_balance - initial_balance) / initial_balance) * 100
        
        # Calculate additional metrics
        pnl_values = [p.pnl for p in self.closed_positions]
        winning_pnls = [p for p in pnl_values if p > 0]
        losing_pnls = [p for p in pnl_values if p <= 0]
        
        avg_win = np.mean(winning_pnls) if winning_pnls else 0
        avg_loss = np.mean(losing_pnls) if losing_pnls else 0
        largest_win = max(winning_pnls) if winning_pnls else 0
        largest_loss = min(losing_pnls) if losing_pnls else 0
        
        profit_factor = abs(sum(winning_pnls) / sum(losing_pnls)) if sum(losing_pnls) != 0 else float('inf')
        
        # Calculate sharpe ratio (simplified)
        returns = [p.pnl / self.account_balance for p in self.closed_positions]
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if len(returns) > 1 and np.std(returns) > 0 else 0
        
        # Ladder-specific analysis
        long_positions = [p for p in self.closed_positions if p.side == "LONG"]
        short_positions = [p for p in self.closed_positions if p.side == "SHORT"]
        
        # Analyze by ladder level
        ladder_stats = {}
        for position in self.closed_positions:
            level = position.ladder_level
            if level not in ladder_stats:
                ladder_stats[level] = {'count': 0, 'pnl': 0, 'wins': 0}
            
            ladder_stats[level]['count'] += 1
            ladder_stats[level]['pnl'] += position.pnl
            if position.pnl > 0:
                ladder_stats[level]['wins'] += 1
        
        # Print report
        print(f"\nACCOUNT SUMMARY:")
        print(f"{'-'*40}")
        print(f"Initial Balance: ${initial_balance:,.2f}")
        print(f"Final Balance: ${final_balance:,.2f}")
        print(f"Total Return: {total_return:.2f}%")
        print(f"Total P&L: ${self.total_pnl:,.2f}")
        print(f"Maximum Drawdown: ${self.max_drawdown:,.2f}")
        
        print(f"\nTRADE STATISTICS:")
        print(f"{'-'*40}")
        print(f"Total Trades: {self.total_trades}")
        print(f"Winning Trades: {self.winning_trades} ({self.winning_trades/self.total_trades*100:.1f}%)")
        print(f"Losing Trades: {self.losing_trades} ({self.losing_trades/self.total_trades*100:.1f}%)")
        print(f"Profit Factor: {profit_factor:.2f}")
        print(f"Sharpe Ratio: {sharpe:.2f}")
        print(f"Average Win: ${avg_win:.2f}")
        print(f"Average Loss: ${avg_loss:.2f}")
        print(f"Largest Win: ${largest_win:.2f}")
        print(f"Largest Loss: ${largest_loss:.2f}")
        
        print(f"\nPOSITION ANALYSIS:")
        print(f"{'-'*40}")
        if long_positions:
            long_win_rate = len([p for p in long_positions if p.pnl > 0]) / len(long_positions) * 100
            avg_long_hold = np.mean([(p.exit_time - p.entry_time).total_seconds()/60 for p in long_positions])
            print(f"Long Positions: {len(long_positions)} (Win Rate: {long_win_rate:.1f}%, Avg Hold: {avg_long_hold:.1f} min)")
        
        if short_positions:
            short_win_rate = len([p for p in short_positions if p.pnl > 0]) / len(short_positions) * 100
            avg_short_hold = np.mean([(p.exit_time - p.entry_time).total_seconds()/60 for p in short_positions])
            print(f"Short Positions: {len(short_positions)} (Win Rate: {short_win_rate:.1f}%, Avg Hold: {avg_short_hold:.1f} min)")
        
        print(f"\nLADDER LEVEL PERFORMANCE:")
        print(f"{'-'*40}")
        for level in sorted(ladder_stats.keys()):
            stats = ladder_stats[level]
            win_rate = (stats['wins'] / stats['count'] * 100) if stats['count'] > 0 else 0
            print(f"Level {level}: {stats['count']} trades | P&L: ${stats['pnl']:.2f} | Win Rate: {win_rate:.1f}%")
        
        print(f"\nDATA PROCESSING:")
        print(f"{'-'*40}")
        print(f"Total Ticks Processed: {self.processed_ticks:,}")
        print(f"Processing Rate: {self.processed_ticks / len(self.closed_positions) if self.closed_positions else 0:.0f} ticks/trade")
        
        # Save detailed trade log
        self.save_trade_log()
    
    def save_trade_log(self):
        """Save detailed trade log to CSV"""
        if not self.closed_positions:
            return
        
        log_data = []
        for position in self.closed_positions:
            log_data.append({
                'Position_ID': position.id,
                'Side': position.side,
                'Ladder_Level': position.ladder_level,
                'Entry_Time': position.entry_time,
                'Entry_Price': position.entry_price,
                'Exit_Time': position.exit_time,
                'Exit_Price': position.exit_price,
                'P&L': position.pnl,
                'Lot_Size': position.lot_size,
                'TP_Price': position.tp,
                'SL_Price': position.sl,
                'Holding_Minutes': (position.exit_time - position.entry_time).total_seconds() / 60
            })
        
        df = pd.DataFrame(log_data)
        filename = f"btc_ladder_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.to_csv(filename, index=False)
        print(f"\nDetailed trade log saved to: {filename}")

# -----------------------------
# 3. MAIN EXECUTION
# -----------------------------
def run_tick_backtest():
    """Main function to run the tick-based backtest"""
    
    # Configuration
    SYMBOL = "#BTCUSD"
    INITIAL_BALANCE = 100
    
    # Date range for backtest (adjust based on available data)
    START_DATE = datetime(2024, 1, 1)
    END_DATE = datetime(2024, 1, 2)  # One week for testing
    
    print("="*80)
    print("BTCUSD TICK-BASED LADDER STRATEGY BACKTEST")
    print("Strategy: Multiple BUY_STOPs above + SELL_LIMIT at first level")
    print("          Multiple SELL_STOPs below + BUY_LIMIT at first level")
    print(f"Symbol: {SYMBOL}")
    print(f"Date Range: {START_DATE.date()} to {END_DATE.date()}")
    print(f"Entry Distance: $100 | TP Distance: $50")
    print("="*80)
    
    # Initialize strategy
    strategy = TickBasedLadderStrategy(SYMBOL, INITIAL_BALANCE)
    
    # Initialize MT5
    if not strategy.initialize_mt5():
        print("Failed to initialize MT5")
        return
    
    try:
        # Get tick data
        tick_data = strategy.get_tick_data_range(START_DATE, END_DATE)
        
        if not tick_data:
            print("No tick data available for the specified range")
            return
        
        # Run backtest
        strategy.run_tick_backtest(tick_data)
        
        # Generate performance report
        strategy.generate_performance_report()
        
    except Exception as e:
        print(f"Error during backtest: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # Shutdown MT5
        mt5.shutdown()
        print("\nBacktest completed. MT5 connection closed.")

# -----------------------------
# 4. OPTIMIZATION AND PARAMETER TESTING
# -----------------------------
def optimize_parameters():
    """Run optimization for different parameter sets"""
    parameter_sets = [
        {'entry_distance': 50, 'tp_distance': 25, 'lot_size': 0.01},
        {'entry_distance': 100, 'tp_distance': 50, 'lot_size': 0.01},
        {'entry_distance': 150, 'tp_distance': 75, 'lot_size': 0.01},
        {'entry_distance': 200, 'tp_distance': 100, 'lot_size': 0.01},
    ]
    
    results = []
    
    for params in parameter_sets:
        print(f"\nTesting parameters: Entry=${params['entry_distance']}, TP=${params['tp_distance']}")
        
        strategy = TickBasedLadderStrategy("BTCUSD", 10000)
        strategy.entry_distance = params['entry_distance']
        strategy.tp_distance = params['tp_distance']
        strategy.lot_size = params['lot_size']
        
        if strategy.initialize_mt5():
            # Get data (use small sample for optimization)
            tick_data = strategy.get_tick_data_range(
                datetime(2024, 1, 1),
                datetime(2024, 1, 3)
            )
            
            if tick_data:
                strategy.run_tick_backtest(tick_data)
                
                results.append({
                    'params': params,
                    'total_pnl': strategy.total_pnl,
                    'win_rate': (strategy.winning_trades / strategy.total_trades * 100) if strategy.total_trades > 0 else 0,
                    'profit_factor': abs(sum([p.pnl for p in strategy.closed_positions if p.pnl > 0]) / 
                                       sum([p.pnl for p in strategy.closed_positions if p.pnl <= 0])) if strategy.closed_positions else 0
                })
            
            mt5.shutdown()
    
    # Display optimization results
    print(f"\n{'='*60}")
    print("OPTIMIZATION RESULTS")
    print(f"{'='*60}")
    
    for i, result in enumerate(results, 1):
        print(f"\nSet {i}: Entry=${result['params']['entry_distance']}, TP=${result['params']['tp_distance']}")
        print(f"  Total P&L: ${result['total_pnl']:.2f}")
        print(f"  Win Rate: {result['win_rate']:.1f}%")
        print(f"  Profit Factor: {result['profit_factor']:.2f}")

# -----------------------------
# 5. EXECUTION
# -----------------------------
if __name__ == "__main__":
    # Install required packages first
    import sys
    print("Checking required packages...")
    
    try:
        import MetaTrader5
    except ImportError:
        print("Installing MetaTrader5...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "MetaTrader5"])
        import MetaTrader5 as mt5
    
    try:
        import pandas as pd
    except ImportError:
        print("Installing pandas...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas"])
        import pandas as pd
    
    try:
        import numpy as np
    except ImportError:
        print("Installing numpy...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy"])
        import numpy as np
    
    # Run the backtest
    run_tick_backtest()
    
    # Optional: Run optimization
    # optimize_parameters()