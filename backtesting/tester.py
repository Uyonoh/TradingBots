from collections import namedtuple

# -----------------------------
# 1. Core Data Structures
# -----------------------------
Tick = namedtuple("Tick", ["time", "bid", "ask"])

class PendingOrder:
    def __init__(self, order_type, price, lot_size, linked_sl=None):
        self.order_type = order_type  # "buy_stop", "sell_stop", "buy_limit", "sell_limit"
        self.price = price
        self.lot_size = lot_size
        self.linked_sl = linked_sl  # previous step entry for SL
        self.filled = False

class Position:
    def __init__(self, side, entry_price, lot_size, sl, tp):
        self.side = side  # "long" or "short"
        self.entry_price = entry_price
        self.lot_size = lot_size
        self.sl = sl
        self.tp = tp
        self.open = True
        self.exit_price = None
        self.exit_time = None

class Ladder:
    def __init__(self, base_price, step_distance, num_steps, direction):
        self.base_price = base_price
        self.step_distance = step_distance
        self.num_steps = num_steps
        self.direction = direction  # "up" or "down"
        self.step = 0
        self.pending_orders = []

class ActiveDay:
    def __init__(self, ladders):
        self.ladders = ladders  # list of Ladder instances
        self.positions = []     # all open positions
        self.equity = 0

# -----------------------------
# 2. Helper Functions
# -----------------------------
def order_crossed_tick(order, tick):
    if order.order_type in ("buy_stop", "buy_limit"):
        return tick.ask >= order.price
    elif order.order_type in ("sell_stop", "sell_limit"):
        return tick.bid <= order.price
    return False

def fill_order(order, tick, ladder, active_day, tp_distance):
    # Create Position
    side = "long" if order.order_type in ("buy_stop", "buy_limit") else "short"
    entry_price = tick.ask if side == "long" else tick.bid
    sl = order.linked_sl
    tp = entry_price + tp_distance if side == "long" else entry_price - tp_distance
    pos = Position(side, entry_price, order.lot_size, sl, tp)
    active_day.positions.append(pos)
    order.filled = True
    ladder.step += 1
    # Return new step info (useful for next SL)
    return pos

def close_position(pos, price, tick_time):
    pos.open = False
    pos.exit_price = price
    pos.exit_time = tick_time

def update_equity(active_day):
    # For simplicity, equity = sum of unrealized P/L of open positions
    total = 0
    for pos in active_day.positions:
        if pos.open:
            if pos.side == "long":
                total += (pos.entry_price - pos.entry_price)  # placeholder
            else:
                total += (pos.entry_price - pos.entry_price)  # placeholder
    active_day.equity = total

def cancel_all_orders(active_day):
    for ladder in active_day.ladders:
        for order in ladder.pending_orders:
            order.filled = True  # mark as consumed

# -----------------------------
# 3. Tick Replay Engine
# -----------------------------
def run_tick_backtest(ticks, base_price, step_distance, num_steps, tp_distance, EOD_time):
    # Initialize ladders
    up_ladder = Ladder(base_price, step_distance, num_steps, "up")
    down_ladder = Ladder(base_price, step_distance, num_steps, "down")
    
    # Build pending orders (up ladder)
    for i in range(1, num_steps + 1):
        price = base_price + i * step_distance
        sl = base_price if i == 1 else base_price + (i - 1) * step_distance
        up_ladder.pending_orders.append(PendingOrder("buy_stop", price, 1.0, linked_sl=sl))
    
    # Build pending orders (down ladder)
    for i in range(1, num_steps + 1):
        price = base_price - i * step_distance
        sl = base_price if i == 1 else base_price - (i - 1) * step_distance
        down_ladder.pending_orders.append(PendingOrder("sell_stop", price, 1.0, linked_sl=sl))
    
    active_day = ActiveDay([up_ladder, down_ladder])
    
    # Tick processing loop
    for tick in ticks:
        # 1. SL/TP resolution
        for pos in active_day.positions:
            if pos.open:
                if pos.side == "long":
                    if tick.bid <= pos.sl:
                        close_position(pos, pos.sl, tick.time)
                    elif tick.bid >= pos.tp:
                        close_position(pos, pos.tp, tick.time)
                else:  # short
                    if tick.ask >= pos.sl:
                        close_position(pos, pos.sl, tick.time)
                    elif tick.ask <= pos.tp:
                        close_position(pos, pos.tp, tick.time)
        
        # 2. Pending order fills
        for ladder in active_day.ladders:
            for order in ladder.pending_orders:
                if not order.filled and order_crossed_tick(order, tick):
                    fill_order(order, tick, ladder, active_day, tp_distance)
        
        # 3. Update equity
        update_equity(active_day)
        
        # 4. End-of-day check
        if tick.time >= EOD_time:
            for pos in active_day.positions:
                if pos.open:
                    price = tick.bid if pos.side == "long" else tick.ask
                    close_position(pos, price, tick.time)
            cancel_all_orders(active_day)
            break
    
    return active_day

# -----------------------------
# 4. Usage Example
# -----------------------------
if __name__ == "__main__":
    # Example synthetic ticks
    ticks = [
        Tick(time=0, bid=10000, ask=10001),
        Tick(time=1, bid=10010, ask=10011),
        Tick(time=2, bid=10020, ask=10021),
        Tick(time=3, bid=10015, ask=10016),
        Tick(time=4, bid=10005, ask=10006),
        Tick(time=5, bid=9995, ask=9996),
        Tick(time=6, bid=10005, ask=10006),
    ]
    
    base_price = 10000
    step_distance = 10
    num_steps = 3
    tp_distance = 10
    EOD_time = 10  # arbitrary end-of-day
    
    result = run_tick_backtest(ticks, base_price, step_distance, num_steps, tp_distance, EOD_time)
    
    # Print results
    for pos in result.positions:
        print(f"{pos.side} | Entry: {pos.entry_price} | SL: {pos.sl} | TP: {pos.tp} | Exit: {pos.exit_price} | Open: {pos.open}")
