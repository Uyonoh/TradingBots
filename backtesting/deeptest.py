# MT5 Backtesting Script with Scheduled Order Placement
# This script implements a ladder strategy with scheduled order placement
# Place this in your MetaTrader 5's Scripts folder or convert to MQL5

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, time, timedelta
import pytz
from typing import List, Dict, Tuple
import numpy as np

# -----------------------------
# 1. Core Trading Strategy Class
# -----------------------------
class LadderTradingStrategy:
    def __init__(self, symbol: str, base_price: float = None):
        self.symbol = symbol
        self.base_price = base_price
        self.pending_orders = []
        self.open_positions = []
        self.trade_log = []
        self.equity_history = []
        
        # Strategy parameters (adjustable)
        self.step_distance = 10 #0.0010  # 10 pips for EURUSD
        self.num_steps = 3
        self.tp_distance = 10 #0.0010    # 10 pips
        self.lot_size = 0.1
        self.max_positions = 5
        
        # Scheduled times (UTC) - example: 12 AM and 4 PM
        self.scheduled_times = [
            time(0, 0),   # 12:00 AM
            # time(16, 0)   # 4:00 PM
        ]
        
        # Track which days we've already placed orders
        self.placed_orders_dates = set()
    
    def initialize_mt5(self):
        """Initialize MT5 connection"""
        if not mt5.initialize():
            print("MT5 initialization failed")
            mt5.shutdown()
            return False
        return True
    
    def get_historical_data(self, start_date: datetime, end_date: datetime, timeframe=mt5.TIMEFRAME_M1):
        """Fetch historical data for backtesting"""
        rates = mt5.copy_rates_range(self.symbol, timeframe, start_date, end_date)
        print(rates)
        if rates is None:
            print(f"No data for {self.symbol}")
            return None
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        return df
    
    def calculate_stop_levels(self, entry_price: float, is_long: bool) -> Tuple[float, float]:
        """Calculate stop loss and take profit levels"""
        if is_long:
            sl = entry_price - self.tp_distance
            tp = entry_price + self.tp_distance
        else:
            sl = entry_price + self.tp_distance
            tp = entry_price - self.tp_distance
        return sl, tp
    
    def place_pending_orders(self, current_price: float, current_time: datetime):
        """Place ladder orders at scheduled times"""
        # Check if we've already placed orders today
        current_date = current_time.date()
        if current_date in self.placed_orders_dates:
            return
        
        # Check if current time matches any scheduled time (within tolerance)
        current_time_only = current_time.time()
        for scheduled_time in self.scheduled_times:
            time_diff = abs((datetime.combine(current_date, scheduled_time) - 
                           datetime.combine(current_date, current_time_only)).total_seconds())
            
            # If within 1 minute of scheduled time
            if time_diff <= 60:
                self.base_price = current_price
                self._create_ladder_orders()
                self.placed_orders_dates.add(current_date)
                self.log_event(f"Placed ladder orders at {current_time}")
                break
    
    def _create_ladder_orders(self):
        """Create the ladder of pending orders"""
        # Clear existing pending orders
        self.pending_orders.clear()
        
        # Create upward ladder (buy stops)
        for i in range(self.num_steps):
            order_price = self.base_price + (i + 1) * self.step_distance
            sl, tp = self.calculate_stop_levels(order_price, is_long=True)
            
            order = {
                'type': mt5.ORDER_TYPE_BUY_STOP,
                'price': order_price,
                'sl': sl,
                'tp': tp,
                'lot': self.lot_size,
                'filled': False
            }
            self.pending_orders.append(order)
        
        # Create downward ladder (sell stops)
        for i in range(self.num_steps):
            order_price = self.base_price - (i + 1) * self.step_distance
            sl, tp = self.calculate_stop_levels(order_price, is_long=False)
            
            order = {
                'type': mt5.ORDER_TYPE_SELL_STOP,
                'price': order_price,
                'sl': sl,
                'tp': tp,
                'lot': self.lot_size,
                'filled': False
            }
            self.pending_orders.append(order)
    
    def check_order_triggers(self, current_price: float, current_time: datetime):
        """Check if any pending orders should be triggered"""
        for order in self.pending_orders[:]:  # Create copy for iteration
            if order['filled']:
                continue
            
            if order['type'] == mt5.ORDER_TYPE_BUY_STOP:
                if current_price >= order['price']:
                    self.execute_order(order, current_price, current_time)
            
            elif order['type'] == mt5.ORDER_TYPE_SELL_STOP:
                if current_price <= order['price']:
                    self.execute_order(order, current_price, current_time)
    
    def execute_order(self, order: Dict, current_price: float, current_time: datetime):
        """Execute a triggered order"""
        # In backtesting, we simulate order execution
        position = {
            'type': 'buy' if order['type'] in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_STOP] else 'sell',
            'entry_price': order['price'],
            'entry_time': current_time,
            'sl': order['sl'],
            'tp': order['tp'],
            'lot': order['lot'],
            'open': True
        }
        
        self.open_positions.append(position)
        order['filled'] = True
        
        self.log_event(f"{position['type'].upper()} position opened at {order['price']:.5f}")
    
    def check_position_exits(self, current_price: float, current_time: datetime):
        """Check if any positions should be closed"""
        for position in self.open_positions[:]:
            if not position['open']:
                continue
            
            if position['type'] == 'buy':
                if current_price <= position['sl']:
                    self.close_position(position, current_price, current_time, 'SL')
                elif current_price >= position['tp']:
                    self.close_position(position, current_price, current_time, 'TP')
            
            elif position['type'] == 'sell':
                if current_price >= position['sl']:
                    self.close_position(position, current_price, current_time, 'SL')
                elif current_price <= position['tp']:
                    self.close_position(position, current_price, current_time, 'TP')
    
    def close_position(self, position: Dict, close_price: float, close_time: datetime, reason: str):
        """Close a position"""
        position['close_price'] = close_price
        position['close_time'] = close_time
        position['open'] = False
        position['close_reason'] = reason
        
        # Calculate P&L
        if position['type'] == 'buy':
            pnl = (close_price - position['entry_price']) * position['lot'] * 100000  # Simplified for Forex
        else:
            pnl = (position['entry_price'] - close_price) * position['lot'] * 100000
        
        position['pnl'] = pnl
        
        self.log_event(f"{position['type'].upper()} position closed at {close_price:.5f} ({reason}), P&L: ${pnl:.2f}")
    
    def calculate_equity(self, current_price: float):
        """Calculate current equity"""
        closed_pnl = sum(pos.get('pnl', 0) for pos in self.open_positions if not pos['open'])
        
        # Calculate unrealized P&L
        unrealized_pnl = 0
        for pos in self.open_positions:
            if pos['open']:
                if pos['type'] == 'buy':
                    unrealized_pnl += (current_price - pos['entry_price']) * pos['lot'] * 100000
                else:
                    unrealized_pnl += (pos['entry_price'] - current_price) * pos['lot'] * 100000
        
        return closed_pnl + unrealized_pnl
    
    def log_event(self, message: str):
        """Log trading events"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"{timestamp} - {message}"
        self.trade_log.append(log_entry)
        print(log_entry)
    
    def run_backtest(self, start_date: datetime, end_date: datetime):
        """Run the backtest"""
        print(f"\nStarting backtest from {start_date} to {end_date}")
        print(f"Symbol: {self.symbol}")
        print(f"Scheduled times: {[t.strftime('%H:%M') for t in self.scheduled_times]}")
        print("-" * 50)
        
        # Get historical data
        df = self.get_historical_data(start_date, end_date)
        if df is None:
            return
        
        # Run through each bar
        for idx, row in df.iterrows():
            print(idx)
            current_time = row['time']
            current_price = (row['high'] + row['low']) / 2  # Use typical price
            
            # 1. Check if it's time to place scheduled orders
            self.place_pending_orders(current_price, current_time)
            
            # 2. Check if any pending orders are triggered
            self.check_order_triggers(current_price, current_time)
            
            # 3. Check if any positions should be closed
            self.check_position_exits(current_price, current_time)
            
            # 4. Update equity
            equity = self.calculate_equity(current_price)
            self.equity_history.append({
                'time': current_time,
                'equity': equity,
                'price': current_price
            })
            
            # Clean up filled orders
            self.pending_orders = [o for o in self.pending_orders if not o['filled']]
            
            # Limit open positions
            if len([p for p in self.open_positions if p['open']]) > self.max_positions:
                # Close oldest position
                open_positions = [p for p in self.open_positions if p['open']]
                if open_positions:
                    self.close_position(open_positions[0], current_price, current_time, 'MAX_POSITIONS')
    
    def generate_report(self):
        """Generate backtest report"""
        print("\n" + "="*50)
        print("BACKTEST REPORT")
        print("="*50)
        
        # Calculate statistics
        closed_positions = [p for p in self.open_positions if not p['open']]
        
        if not closed_positions:
            print("No positions were closed during backtest.")
            return
        
        # Calculate P&L statistics
        pnl_values = [p['pnl'] for p in closed_positions]
        total_pnl = sum(pnl_values)
        winning_trades = [p for p in closed_positions if p['pnl'] > 0]
        losing_trades = [p for p in closed_positions if p['pnl'] <= 0]
        
        win_rate = len(winning_trades) / len(closed_positions) * 100 if closed_positions else 0
        avg_win = np.mean([p['pnl'] for p in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([p['pnl'] for p in losing_trades]) if losing_trades else 0
        
        print(f"\nPerformance Summary:")
        print(f"Total Trades: {len(closed_positions)}")
        print(f"Winning Trades: {len(winning_trades)} ({win_rate:.1f}%)")
        print(f"Losing Trades: {len(losing_trades)}")
        print(f"Total P&L: ${total_pnl:.2f}")
        print(f"Average Win: ${avg_win:.2f}")
        print(f"Average Loss: ${avg_loss:.2f}")
        
        # Show trade log
        print(f"\nTrade Log (last 10 trades):")
        for log in self.trade_log[-10:]:
            print(f"  {log}")
        
        # Show final equity curve
        if self.equity_history:
            print(f"\nFinal Equity: ${self.equity_history[-1]['equity']:.2f}")


# -----------------------------
# 2. Main Execution
# -----------------------------
if __name__ == "__main__":
    # Configuration
    SYMBOL = "EURUSD"
    START_DATE = datetime(2024, 1, 1)
    END_DATE = datetime(2024, 1, 31)
    
    # Initialize strategy
    strategy = LadderTradingStrategy(SYMBOL)
    
    # Initialize MT5
    if strategy.initialize_mt5():
        try:
            # Run backtest
            strategy.run_backtest(START_DATE, END_DATE)
            
            # Generate report
            strategy.generate_report()
            
        finally:
            # Shutdown MT5
            mt5.shutdown()
            print("\nBacktest completed. MT5 connection closed.")
    else:
        print("Failed to initialize MT5. Backtest aborted.")