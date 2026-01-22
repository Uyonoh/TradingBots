# BTCUSD Backtesting Script with Volatility-Adaptive Features
# Specifically designed for Bitcoin/USD with high volatility considerations

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import pytz
from typing import List, Dict, Tuple, Optional
import warnings
warnings.filterwarnings('ignore')

# -----------------------------
# 1. BTC-SPECIFIC CONSTANTS AND SETTINGS
# -----------------------------
class BTCUSDSettings:
    """BTCUSD-specific trading parameters"""
    # Trading hours (24/7 for crypto, but MT5 might have gaps)
    MARKET_OPEN = time(0, 0)
    MARKET_CLOSE = time(23, 59, 59)
    
    # Typical BTCUSD volatility metrics
    AVERAGE_DAILY_RANGE = 0.03  # 3% average daily range
    MAX_SPREAD_PIPS = 50  # Maximum expected spread in pips (0.5% for BTC)
    MIN_SPREAD_PIPS = 5   # Minimum expected spread
    
    # Slippage considerations for high volatility
    SLIPPAGE_MULTIPLIER = 1.5  # Multiply expected slippage during high volatility
    
    # Liquidity considerations
    LIQUIDITY_HOURS = {
        'high': [time(14, 0), time(22, 0)],  # NY + London overlap
        'medium': [time(0, 0), time(14, 0)],
        'low': [time(22, 0), time(23, 59, 59)]
    }
    
    # News/event times that affect BTC
    VOLATILE_PERIODS = [
        (time(14, 30), time(15, 30)),  # US economic releases
        (time(20, 0), time(21, 0)),    # FOMC meetings
        (time(0, 0), time(1, 0)),      # Asian session open
    ]

# -----------------------------
# 2. VOLATILITY-ADAPTIVE LADDER STRATEGY
# -----------------------------
class VolatilityAdaptiveLadderStrategy:
    def __init__(self, symbol: str = "BTCUSD"):
        self.symbol = symbol
        self.btc_settings = BTCUSDSettings()
        self.pending_orders = []
        self.open_positions = []
        self.trade_log = []
        self.equity_history = []
        self.volatility_history = []
        
        # Volatility-adaptive parameters
        self.current_volatility = 0.0
        self.volatility_period = 20  # Period for volatility calculation (hours)
        self.atr_period = 14
        
        # Dynamic risk parameters
        self.base_lot_size = 0.01  # Base lot size for BTC (0.01 BTC = ~$500 at $50k/BTC)
        self.max_risk_per_trade = 0.02  # Max 2% risk per trade
        self.max_daily_risk = 0.10  # Max 10% daily drawdown
        
        # Scheduled times (UTC) - optimized for BTC liquidity
        self.scheduled_times = [
            time(0, 0),   # Midnight UTC (Asian open)
            time(8, 0),   # London open
            time(14, 0),  # NY open
            time(20, 0)   # NY close/London open overlap
        ]
        
        # Dynamic ladder parameters
        self.base_step_distance = None  # Will be set based on volatility
        self.num_steps = 3
        self.tp_multiplier = 1.5  # TP = volatility * multiplier
        
        # Risk management
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.max_daily_trades = 10
        
        # Order execution quality tracking
        self.slippage_log = []
        self.spread_log = []
        
        # Market state tracking
        self.last_tick_time = None
        self.is_gap = False
        
    def initialize_mt5(self):
        """Initialize MT5 with BTC-specific settings"""
        if not mt5.initialize():
            print("MT5 initialization failed")
            mt5.shutdown()
            return False
        
        # Configure symbol settings
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info is None:
            print(f"Symbol {self.symbol} not found")
            mt5.shutdown()
            return False
        
        # Enable symbol for trading
        if not symbol_info.visible:
            if not mt5.symbol_select(self.symbol, True):
                print(f"Failed to select {self.symbol}")
                mt5.shutdown()
                return False
        
        print(f"BTCUSD initialized: Point={symbol_info.point}, Digits={symbol_info.digits}")
        print(f"Trade allowed: {symbol_info.trade_mode}, Spread: {symbol_info.spread}")
        return True
    
    def calculate_volatility(self, prices: pd.Series, period: int = 20) -> float:
        """Calculate current volatility using multiple methods"""
        if len(prices) < period:
            return self.btc_settings.AVERAGE_DAILY_RANGE
        
        # Method 1: ATR (Average True Range)
        high = prices.rolling(window=period).max()
        low = prices.rolling(window=period).min()
        atr = (high - low).mean() / prices.iloc[-1]
        
        # Method 2: Historical volatility (standard deviation of returns)
        returns = prices.pct_change().dropna()
        hist_vol = returns.std() * np.sqrt(365)  # Annualized
        
        # Method 3: Parkinson volatility (uses high-low range)
        log_hl = np.log(high / low)
        parkinson_vol = np.sqrt((1 / (4 * np.log(2))) * (log_hl ** 2).mean())
        
        # Combine methods with weights
        combined_vol = (atr * 0.4 + hist_vol * 0.3 + parkinson_vol * 0.3)
        
        # Cap volatility for stability
        max_vol = 0.15  # 15% max volatility
        return min(combined_vol, max_vol)
    
    def get_historical_data(self, start_date: datetime, end_date: datetime, 
                           timeframe=mt5.TIMEFRAME_H1):
        """Fetch historical data with BTC-specific considerations"""
        # Use smaller timeframe for crypto due to higher volatility
        rates = mt5.copy_rates_range(self.symbol, timeframe, start_date, end_date)
        if rates is None or len(rates) == 0:
            print(f"No data for {self.symbol} from {start_date} to {end_date}")
            return None
        
        df = pd.DataFrame(rates)
        print(df.keys())
        df['time'] = pd.to_datetime(df['time'], unit='s')
        
        # Calculate additional metrics for BTC
        df['spread'] = df['spread']  #(df['ask'] - df['bid']) / df['ask'] * 10000  # Spread in pips
        df['volume_btc'] = df['tick_volume'] / 1000  # Approximate BTC volume
        
        # Identify gaps (common in crypto weekends on some brokers)
        df['time_diff'] = df['time'].diff().dt.total_seconds()
        df['is_gap'] = df['time_diff'] > (timeframe * 60 * 2)  # More than 2 periods gap
        
        return df
    
    def adjust_for_volatility(self, current_price: float, current_time: datetime) -> Dict:
        """Adjust trading parameters based on current volatility and market conditions"""
        # Get current market conditions
        spread = self.get_current_spread()
        liquidity_level = self.get_liquidity_level(current_time)
        is_volatile_period = self.is_volatile_time_period(current_time)
        
        # Dynamic step distance based on volatility
        if self.current_volatility > 0.05:  # High volatility (>5%)
            step_distance = self.current_volatility * current_price * 0.5
            tp_distance = step_distance * 1.0
            lot_multiplier = 0.5  # Reduce position size in high volatility
        elif self.current_volatility > 0.02:  # Medium volatility (2-5%)
            step_distance = self.current_volatility * current_price * 0.3
            tp_distance = step_distance * 1.5
            lot_multiplier = 0.75
        else:  # Low volatility (<2%)
            step_distance = self.current_volatility * current_price * 0.2
            tp_distance = step_distance * 2.0
            lot_multiplier = 1.0
        
        # Adjust for spread
        if spread > self.btc_settings.MAX_SPREAD_PIPS * 0.7:
            step_distance *= 1.5  # Wider steps in high spread
            lot_multiplier *= 0.7  # Smaller lots
        
        # Adjust for liquidity
        if liquidity_level == 'low':
            step_distance *= 1.2
            lot_multiplier *= 0.8
        
        # Adjust for volatile periods
        if is_volatile_period:
            step_distance *= 1.3
            tp_distance *= 0.8  # Tighter TP during news
        
        # Ensure minimum step distance (at least 0.5% of price)
        min_step = current_price * 0.005
        step_distance = max(step_distance, min_step)
        
        # Calculate dynamic lot size
        risk_adjusted_lot = self.base_lot_size * lot_multiplier
        
        # Apply daily risk limits
        if self.daily_pnl < -self.max_daily_risk * 100:
            risk_adjusted_lot *= 0.5  # Halve position size if daily loss exceeded
        
        return {
            'step_distance': step_distance,
            'tp_distance': tp_distance,
            'lot_size': risk_adjusted_lot,
            'spread': spread,
            'liquidity': liquidity_level,
            'is_volatile': is_volatile_period
        }
    
    def get_current_spread(self) -> float:
        """Get current spread in pips"""
        try:
            tick = mt5.symbol_info_tick(self.symbol)
            if tick:
                spread = (tick.ask - tick.bid) / tick.ask * 10000
                self.spread_log.append(spread)
                return spread
        except:
            pass
        return self.btc_settings.MIN_SPREAD_PIPS
    
    def get_liquidity_level(self, current_time: datetime) -> str:
        """Determine current liquidity level based on time of day"""
        current_hour = current_time.time()
        
        for period in self.btc_settings.VOLATILE_PERIODS:
            if period[0] <= current_hour <= period[1]:
                return 'low'  # Assume lower liquidity during volatile news
        
        for level, hours in self.btc_settings.LIQUIDITY_HOURS.items():
            if hours[0] <= current_hour <= hours[1]:
                return level
        
        return 'medium'
    
    def is_volatile_time_period(self, current_time: datetime) -> bool:
        """Check if current time is in a typically volatile period"""
        current_hour = current_time.time()
        
        for start, end in self.btc_settings.VOLATILE_PERIODS:
            if start <= current_hour <= end:
                return True
        
        # Also check for weekends (some crypto brokers have gaps)
        if current_time.weekday() >= 5:  # Saturday or Sunday
            return True
        
        return False
    
    def place_scheduled_orders(self, current_price: float, current_time: datetime, df: pd.DataFrame):
        """Place ladder orders at scheduled times with volatility adaptation"""
        current_date = current_time.date()
        
        # Check if we've already placed orders at this scheduled time today
        for order in self.pending_orders:
            if order.get('date') == current_date and order.get('scheduled_time') == current_time.time():
                return
        
        # Check if current time matches any scheduled time (within 5-minute window)
        current_time_only = current_time.time()
        for scheduled_time in self.scheduled_times:
            time_diff = abs((datetime.combine(current_date, scheduled_time) - 
                           datetime.combine(current_date, current_time_only)).total_seconds())
            
            # If within 5 minutes of scheduled time
            if time_diff <= 300:  # 5 minutes tolerance
                # Calculate current volatility
                recent_prices = df[df['time'] <= current_time]['close']
                self.current_volatility = self.calculate_volatility(recent_prices.tail(100))
                
                # Get volatility-adjusted parameters
                params = self.adjust_for_volatility(current_price, current_time)
                
                # Clear existing pending orders (cancel previous ladder)
                self.cancel_all_pending_orders()
                
                # Create new ladder orders
                self._create_volatility_adjusted_ladder(
                    current_price, 
                    params['step_distance'],
                    params['tp_distance'],
                    params['lot_size'],
                    current_date,
                    current_time.time()
                )
                
                self.log_event(f"""
                Scheduled order placement at {current_time}
                Volatility: {self.current_volatility:.4%}
                Step Distance: ${params['step_distance']:.2f}
                Lot Size: {params['lot_size']:.3f} BTC
                Spread: {params['spread']:.1f} pips
                Liquidity: {params['liquidity']}
                """)
                break
    
    def _create_volatility_adjusted_ladder(self, base_price: float, step_distance: float, 
                                         tp_distance: float, lot_size: float,
                                         order_date, scheduled_time):
        """Create ladder orders adjusted for current market conditions"""
        # Create upward ladder (buy stops)
        for i in range(self.num_steps):
            order_price = base_price + (i + 1) * step_distance
            
            # Dynamic SL based on volatility
            sl_distance = tp_distance * 1.2 if self.current_volatility > 0.03 else tp_distance
            sl = order_price - sl_distance
            tp = order_price + tp_distance
            
            order = {
                'type': 'BUY_STOP',
                'price': order_price,
                'sl': sl,
                'tp': tp,
                'lot': lot_size,
                'filled': False,
                'date': order_date,
                'scheduled_time': scheduled_time,
                'volatility': self.current_volatility
            }
            self.pending_orders.append(order)
        
        # Create downward ladder (sell stops)
        for i in range(self.num_steps):
            order_price = base_price - (i + 1) * step_distance
            
            sl_distance = tp_distance * 1.2 if self.current_volatility > 0.03 else tp_distance
            sl = order_price + sl_distance
            tp = order_price - tp_distance
            
            order = {
                'type': 'SELL_STOP',
                'price': order_price,
                'sl': sl,
                'tp': tp,
                'lot': lot_size,
                'filled': False,
                'date': order_date,
                'scheduled_time': scheduled_time,
                'volatility': self.current_volatility
            }
            self.pending_orders.append(order)
    
    def simulate_order_fill(self, order: Dict, current_price: float, 
                          current_time: datetime) -> float:
        """Simulate order fill with slippage for BTC"""
        # Calculate expected slippage
        base_slippage = self.current_volatility * current_price * 0.001  # 0.1% of volatility
        
        # Increase slippage during high volatility
        if self.current_volatility > 0.05:
            base_slippage *= 2
        
        # Increase slippage for larger orders
        if order['lot'] > 0.1:  # More than 0.1 BTC
            base_slippage *= (1 + order['lot'] / 10)
        
        # Random slippage component (realistic for crypto)
        random_slippage = np.random.normal(0, base_slippage * 0.5)
        
        if order['type'] == 'BUY_STOP':
            fill_price = order['price'] + base_slippage + random_slippage
        else:  # SELL_STOP
            fill_price = order['price'] - base_slippage + random_slippage
        
        # Log slippage
        self.slippage_log.append({
            'time': current_time,
            'expected': order['price'],
            'actual': fill_price,
            'slippage': abs(fill_price - order['price']),
            'volatility': self.current_volatility
        })
        
        return fill_price
    
    def check_gaps_and_adjust(self, current_price: float, previous_price: float, 
                            current_time: datetime, previous_time: datetime):
        """Check for price gaps and adjust strategy accordingly"""
        time_gap = (current_time - previous_time).total_seconds()
        
        # Significant time gap (more than 1 hour)
        if time_gap > 3600:
            self.is_gap = True
            gap_percentage = abs(current_price - previous_price) / previous_price
            
            if gap_percentage > 0.02:  # More than 2% gap
                self.log_event(f"Significant gap detected: {gap_percentage:.2%} over {time_gap/3600:.1f} hours")
                
                # Cancel all orders if gap is too large
                if gap_percentage > 0.05:  # More than 5% gap
                    self.cancel_all_pending_orders()
                    self.log_event("Cancelled all orders due to large gap")
                
                # Adjust strategy parameters
                self.base_lot_size *= 0.8  # Reduce lot size after gap
                
            return True
        return False
    
    def cancel_all_pending_orders(self):
        """Cancel all pending orders"""
        cancelled = [o for o in self.pending_orders if not o['filled']]
        self.pending_orders = [o for o in self.pending_orders if o['filled']]
        return cancelled
    
    def run_backtest(self, start_date: datetime, end_date: datetime, 
                    initial_balance: float = 10000):
        """Run backtest with BTC-specific considerations"""
        print(f"\n{'='*60}")
        print(f"BTCUSD BACKTEST: {start_date.date()} to {end_date.date()}")
        print(f"Initial Balance: ${initial_balance:,.2f}")
        print(f"{'='*60}")
        
        # Get historical data
        df = self.get_historical_data(start_date, end_date)
        if df is None:
            return
        
        # Initialize tracking
        balance = initial_balance
        self.last_tick_time = df.iloc[0]['time']
        previous_price = df.iloc[0]['close']
        
        print(f"Data loaded: {len(df)} bars from {df.iloc[0]['time']} to {df.iloc[-1]['time']}")
        
        # Main backtest loop
        for idx, row in df.iterrows():
            current_time = row['time']
            current_price = row['close']
            
            # Check for gaps
            if self.check_gaps_and_adjust(current_price, previous_price, 
                                        current_time, self.last_tick_time):
                # Reset after gap handling
                self.is_gap = False
            
            # Update volatility
            if idx >= 100:  # Enough data for volatility calculation
                recent_prices = df.iloc[max(0, idx-100):idx+1]['close']
                self.current_volatility = self.calculate_volatility(recent_prices)
                self.volatility_history.append({
                    'time': current_time,
                    'volatility': self.current_volatility,
                    'price': current_price
                })
            
            # 1. Place scheduled orders
            self.place_scheduled_orders(current_price, current_time, df)
            
            # 2. Check order triggers
            self.check_order_triggers(current_price, current_time, balance)
            
            # 3. Check position exits
            self.check_position_exits(current_price, current_time, balance)
            
            # 4. Update equity and risk metrics
            equity = self.calculate_equity(current_price, balance)
            self.equity_history.append({
                'time': current_time,
                'equity': equity,
                'balance': balance,
                'floating': equity - balance
            })
            
            # Update daily P&L
            if current_time.date() != self.last_tick_time.date():
                self.daily_pnl = 0
                self.daily_trades = 0
            
            # Update trackers
            previous_price = current_price
            self.last_tick_time = current_time
            
            # Early stop if significant drawdown
            if equity < initial_balance * 0.7:  # 30% drawdown
                self.log_event(f"STOPPING: 30% drawdown reached at {current_time}")
                break
        
        # Close all positions at end of backtest
        self.close_all_positions(df.iloc[-1]['close'], df.iloc[-1]['time'], balance)
        
        print(f"\nBacktest completed. Final equity: ${self.equity_history[-1]['equity']:,.2f}")
    
    def check_order_triggers(self, current_price: float, current_time: datetime, balance: float):
        """Check if any pending orders should be triggered"""
        for order in self.pending_orders[:]:
            if order['filled']:
                continue
            
            # Check if order is triggered
            if order['type'] == 'BUY_STOP' and current_price >= order['price']:
                fill_price = self.simulate_order_fill(order, current_price, current_time)
                self.execute_order(order, fill_price, current_time, balance)
            
            elif order['type'] == 'SELL_STOP' and current_price <= order['price']:
                fill_price = self.simulate_order_fill(order, current_price, current_time)
                self.execute_order(order, fill_price, current_time, balance)
    
    def execute_order(self, order: Dict, fill_price: float, current_time: datetime, balance: float):
        """Execute a triggered order with position sizing based on balance"""
        # Calculate position value in USD
        position_value = fill_price * order['lot']
        
        # Ensure position size doesn't exceed risk limits
        max_position_value = balance * self.max_risk_per_trade
        if position_value > max_position_value:
            # Adjust lot size to respect risk limits
            adjusted_lot = max_position_value / fill_price
            order['lot'] = adjusted_lot
        
        position = {
            'type': order['type'].split('_')[0].lower(),  # 'buy' or 'sell'
            'entry_price': fill_price,
            'entry_time': current_time,
            'sl': order['sl'],
            'tp': order['tp'],
            'lot': order['lot'],
            'open': True,
            'order_volatility': order['volatility'],
            'position_value': fill_price * order['lot']
        }
        
        self.open_positions.append(position)
        order['filled'] = True
        self.daily_trades += 1
        
        self.log_event(f"""
        {position['type'].upper()} position opened:
        Entry: ${fill_price:,.2f}
        Lot: {order['lot']:.3f} BTC (${position['position_value']:,.2f})
        SL: ${order['sl']:,.2f} | TP: ${order['tp']:,.2f}
        Volatility at entry: {order['volatility']:.2%}
        """)
    
    def check_position_exits(self, current_price: float, current_time: datetime, balance: float):
        """Check if any positions should be closed"""
        for position in self.open_positions[:]:
            if not position['open']:
                continue
            
            # Calculate current P&L
            if position['type'] == 'buy':
                current_pnl = (current_price - position['entry_price']) * position['lot']
                # Check SL/TP
                if current_price <= position['sl']:
                    self.close_position(position, position['sl'], current_time, 'SL', balance)
                elif current_price >= position['tp']:
                    self.close_position(position, position['tp'], current_time, 'TP', balance)
            else:  # sell
                current_pnl = (position['entry_price'] - current_price) * position['lot']
                # Check SL/TP
                if current_price >= position['sl']:
                    self.close_position(position, position['sl'], current_time, 'SL', balance)
                elif current_price <= position['tp']:
                    self.close_position(position, position['tp'], current_time, 'TP', balance)
            
            # Trailing stop for extreme moves
            if abs(current_pnl) > position['position_value'] * 0.10:  # 10% move
                self.consider_trailing_stop(position, current_price, current_time, balance)
    
    def consider_trailing_stop(self, position: Dict, current_price: float, 
                             current_time: datetime, balance: float):
        """Implement trailing stop for large moves"""
        if position['type'] == 'buy':
            move_percentage = (current_price - position['entry_price']) / position['entry_price']
            if move_percentage > 0.15:  # 15% profit
                # Move SL to breakeven + 5%
                new_sl = position['entry_price'] * 1.05
                if new_sl > position['sl']:
                    position['sl'] = new_sl
                    self.log_event(f"Trailing SL moved to ${new_sl:,.2f} for {position['type']} position")
        else:  # sell
            move_percentage = (position['entry_price'] - current_price) / position['entry_price']
            if move_percentage > 0.15:
                new_sl = position['entry_price'] * 0.95
                if new_sl < position['sl']:
                    position['sl'] = new_sl
                    self.log_event(f"Trailing SL moved to ${new_sl:,.2f} for {position['type']} position")
    
    def close_position(self, position: Dict, close_price: float, close_time: datetime, 
                      reason: str, balance: float):
        """Close a position and update balance"""
        position['close_price'] = close_price
        position['close_time'] = close_time
        position['open'] = False
        position['close_reason'] = reason
        
        # Calculate P&L
        if position['type'] == 'buy':
            pnl = (close_price - position['entry_price']) * position['lot']
        else:
            pnl = (position['entry_price'] - close_price) * position['lot']
        
        position['pnl'] = pnl
        position['pnl_percentage'] = pnl / position['position_value']
        
        # Update daily P&L
        self.daily_pnl += pnl
        
        # Update balance
        balance += pnl
        
        self.log_event(f"""
        {position['type'].upper()} position closed ({reason}):
        Entry: ${position['entry_price']:,.2f} | Exit: ${close_price:,.2f}
        P&L: ${pnl:,.2f} ({position['pnl_percentage']:.2%})
        Holding period: {(close_time - position['entry_time']).total_seconds()/3600:.1f} hours
        """)
        
        return balance
    
    def close_all_positions(self, current_price: float, current_time: datetime, balance: float):
        """Close all open positions at end of backtest"""
        for position in self.open_positions[:]:
            if position['open']:
                self.close_position(position, current_price, current_time, 'EOD', balance)
    
    def calculate_equity(self, current_price: float, balance: float) -> float:
        """Calculate current equity including floating P&L"""
        floating_pnl = 0
        for position in self.open_positions:
            if position['open']:
                if position['type'] == 'buy':
                    floating_pnl += (current_price - position['entry_price']) * position['lot']
                else:
                    floating_pnl += (position['entry_price'] - current_price) * position['lot']
        
        return balance + floating_pnl
    
    def log_event(self, message: str):
        """Log trading events"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message.strip()}"
        self.trade_log.append(log_entry)
        # Only print important events
        if "position opened" in message or "position closed" in message or "STOPPING" in message:
            print(log_entry[:200] + "..." if len(log_entry) > 200 else log_entry)
    
    def generate_detailed_report(self):
        """Generate comprehensive BTC backtest report"""
        if not self.equity_history:
            print("No backtest data available")
            return
        
        print(f"\n{'='*60}")
        print("BTCUSD BACKTEST DETAILED REPORT")
        print(f"{'='*60}")
        
        # Basic statistics
        initial_equity = self.equity_history[0]['equity']
        final_equity = self.equity_history[-1]['equity']
        total_return = (final_equity - initial_equity) / initial_equity * 100
        
        closed_positions = [p for p in self.open_positions if not p['open']]
        
        if not closed_positions:
            print("No positions were closed during backtest.")
            return
        
        # Calculate performance metrics
        pnl_values = [p['pnl'] for p in closed_positions]
        total_pnl = sum(pnl_values)
        
        winning_trades = [p for p in closed_positions if p['pnl'] > 0]
        losing_trades = [p for p in closed_positions if p['pnl'] <= 0]
        
        win_rate = len(winning_trades) / len(closed_positions) * 100 if closed_positions else 0
        avg_win = np.mean([p['pnl'] for p in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([p['pnl'] for p in losing_trades]) if losing_trades else 0
        profit_factor = abs(sum([p['pnl'] for p in winning_trades]) / 
                          sum([p['pnl'] for p in losing_trades])) if losing_trades else float('inf')
        
        # Risk metrics
        equity_series = pd.Series([e['equity'] for e in self.equity_history])
        returns = equity_series.pct_change().dropna()
        
        sharpe_ratio = np.sqrt(365) * returns.mean() / returns.std() if returns.std() > 0 else 0
        max_drawdown = self.calculate_max_drawdown(equity_series)
        
        # Volatility analysis
        avg_volatility = np.mean([v['volatility'] for v in self.volatility_history]) if self.volatility_history else 0
        
        # Slippage analysis
        if self.slippage_log:
            avg_slippage = np.mean([s['slippage'] for s in self.slippage_log])
        else:
            avg_slippage = 0
        
        print(f"\nPERFORMANCE SUMMARY:")
        print(f"Initial Equity: ${initial_equity:,.2f}")
        print(f"Final Equity: ${final_equity:,.2f}")
        print(f"Total Return: {total_return:.2f}%")
        print(f"Total P&L: ${total_pnl:,.2f}")
        print(f"Sharpe Ratio: {sharpe_ratio:.2f}")
        print(f"Max Drawdown: {max_drawdown:.2%}")
        
        print(f"\nTRADE STATISTICS:")
        print(f"Total Trades: {len(closed_positions)}")
        print(f"Winning Trades: {len(winning_trades)} ({win_rate:.1f}%)")
        print(f"Losing Trades: {len(losing_trades)}")
        print(f"Average Win: ${avg_win:,.2f}")
        print(f"Average Loss: ${avg_loss:,.2f}")
        print(f"Profit Factor: {profit_factor:.2f}")
        print(f"Average Volatility: {avg_volatility:.2%}")
        print(f"Average Slippage: ${avg_slippage:,.2f}")
        
        print(f"\nPOSITION ANALYSIS:")
        buy_trades = [p for p in closed_positions if p['type'] == 'buy']
        sell_trades = [p for p in closed_positions if p['type'] == 'sell']
        
        if buy_trades:
            buy_win_rate = len([p for p in buy_trades if p['pnl'] > 0]) / len(buy_trades) * 100
            print(f"Buy Trades: {len(buy_trades)} (Win Rate: {buy_win_rate:.1f}%)")
        
        if sell_trades:
            sell_win_rate = len([p for p in sell_trades if p['pnl'] > 0]) / len(sell_trades) * 100
            print(f"Sell Trades: {len(sell_trades)} (Win Rate: {sell_win_rate:.1f}%)")
        
        print(f"\nEXIT REASONS:")
        exit_reasons = {}
        for pos in closed_positions:
            reason = pos['close_reason']
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        
        for reason, count in exit_reasons.items():
            percentage = count / len(closed_positions) * 100
            print(f"  {reason}: {count} trades ({percentage:.1f}%)")
    
    def calculate_max_drawdown(self, equity_series: pd.Series) -> float:
        """Calculate maximum drawdown"""
        cumulative = equity_series.values
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        return abs(drawdown.min())


# -----------------------------
# 3. MAIN EXECUTION
# -----------------------------
if __name__ == "__main__":
    # Configuration for BTCUSD
    SYMBOL = "#BTCUSD"
    
    # Backtest period (choose period with typical BTC volatility)
    START_DATE = datetime(2026, 1, 1)
    END_DATE = datetime(2026, 1, 15)
    
    # Initial capital
    INITIAL_BALANCE = 10000  # $10,000
    
    print("="*60)
    print("BTCUSD BACKTESTING SCRIPT")
    print("Special considerations for high volatility and 24/7 market")
    print("="*60)
    
    # Initialize strategy
    strategy = VolatilityAdaptiveLadderStrategy(SYMBOL)
    
    # Initialize MT5
    if strategy.initialize_mt5():
        try:
            # Run backtest
            strategy.run_backtest(START_DATE, END_DATE, INITIAL_BALANCE)
            
            # Generate detailed report
            strategy.generate_detailed_report()
            
            # Optional: Save logs to file
            with open(f"btc_backtest_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log", 'w') as f:
                for log in strategy.trade_log[-1000:]:  # Last 1000 logs
                    f.write(log + "\n")
            
        except Exception as e:
            print(f"Error during backtest: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            # Shutdown MT5
            mt5.shutdown()
            print("\nBacktest completed. MT5 connection closed.")
    else:
        print("Failed to initialize MT5. Backtest aborted.")