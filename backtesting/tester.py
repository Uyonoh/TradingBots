from collections import namedtuple

# -----------------------------
# 1. Core Data Structures
# -----------------------------
Tick = namedtuple("Tick", ["time", "bid", "ask"])

class PendingOrder:
    def __init__(self, order_type, price, lot_size, linked_sl=None, linked_tp=None):
        self.order_type = order_type  # "buy_stop", "sell_stop", "buy_limit", "sell_limit"
        self.price = price
        self.lot_size = lot_size
        self.linked_sl = linked_sl
        self.linked_tp = linked_tp
        self.filled = False

    def __repr__(self):
        return f"{self.order_type} order at {self.price} [{self.linked_tp} -> {self.linked_sl}]"
    
    

class Position:
    def __init__(self, side, entry_price, lot_size, sl, tp):
        self.side = side
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
        self.ladders = ladders
        self.positions = []
        self.equity = 0

# -----------------------------
# 2. Helper Functions
# -----------------------------
def order_crossed_tick(order, tick):
    if order.order_type in ("buy_stop", "sell_limit"):
        return tick.ask >= order.price
    elif order.order_type in ("sell_stop", "buy_limit"):
        return tick.bid <= order.price
    return False

def fill_order(order, tick, ladder, active_day, tp_distance, log):
    side = "long" if order.order_type in ("buy_stop", "buy_limit") else "short"
    entry_price = tick.ask if side == "long" else tick.bid
    sl = order.linked_sl
    tp = entry_price + tp_distance if side == "long" else entry_price - tp_distance
    pos = Position(side, entry_price, order.lot_size, sl, tp)
    active_day.positions.append(pos)
    order.filled = True
    ladder.step += 1
    log.append(f"{tick.time}: {side.upper()} ENTRY at {entry_price} | SL: {sl} | TP: {tp}")
    return pos

def close_position(pos, price, tick_time, log):
    if pos.open:
        pos.open = False
        pos.exit_price = price
        pos.exit_time = tick_time
        log.append(f"{tick_time}: {pos.side.upper()} EXIT at {price}")

def update_equity(active_day):
    # placeholder: add more realistic P/L if needed
    total = 0
    for pos in active_day.positions:
        if pos.open:
            total += 0  # unrealized P/L can be calculated here
    active_day.equity = total

def cancel_all_orders(active_day, log, tick_time):
    for ladder in active_day.ladders:
        for order in ladder.pending_orders:
            if not order.filled:
                order.filled = True
                log.append(f"{tick_time}: CANCELLED ORDER {order.order_type} @ {order.price}")

# -----------------------------
# 3. Tick Replay Engine
# -----------------------------
def run_tick_backtest(ticks, base_price, step_distance, num_steps, tp_distance, EOD_time):
    log = []
    # Initialize ladders
    up_ladder = Ladder(base_price, step_distance, num_steps, "up")
    down_ladder = Ladder(base_price, step_distance, num_steps, "down")

    # Build pending orders (up ladder)
    initial_dist = 20
    for i in range(num_steps):
        price = base_price + i * step_distance + initial_dist
        sl = price - tp_distance
        # Add linked sell limit at first entry
        linked_sell_limit = None
        if i == 0:
            sell_sl = price + tp_distance
            linked_sell_limit = PendingOrder("sell_limit", price, 1.0, linked_sl=sell_sl)
            up_ladder.pending_orders.append(linked_sell_limit)
        up_ladder.pending_orders.append(PendingOrder("buy_stop", price, 1.0, linked_sl=sl))

    # Build pending orders (down ladder)
    for i in range(num_steps):
        price = base_price - i * step_distance - initial_dist
        sl = price + tp_distance
        # Add linked buy limit at first entry
        linked_buy_limit = None
        if i == 0:
            buy_sl = price - tp_distance
            linked_buy_limit = PendingOrder("buy_limit", price, 1.0, linked_sl=buy_sl)
            down_ladder.pending_orders.append(linked_buy_limit)
        down_ladder.pending_orders.append(PendingOrder("sell_stop", price, 1.0, linked_sl=sl))

    active_day = ActiveDay([up_ladder, down_ladder])

    # Tick processing loop
    for tick in ticks:
        # 1. SL/TP resolution
        for pos in active_day.positions:
            if pos.open:
                if pos.side == "long":
                    if tick.bid <= pos.sl:
                        close_position(pos, pos.sl, tick.time, log)
                    elif tick.bid >= pos.tp:
                        close_position(pos, pos.tp, tick.time, log)
                else:  # short
                    if tick.ask >= pos.sl:
                        close_position(pos, pos.sl, tick.time, log)
                    elif tick.ask <= pos.tp:
                        close_position(pos, pos.tp, tick.time, log)

        # 2. Pending order fills
        # Loop to handle cascading multiple fills on same tick
        orders_to_check = [o for l in active_day.ladders for o in l.pending_orders if not o.filled]
        while orders_to_check:
            filled_this_round = []
            for order in orders_to_check:
                ladder = next(l for l in active_day.ladders if order in l.pending_orders)
                if order_crossed_tick(order, tick):
                    fill_order(order, tick, ladder, active_day, tp_distance, log)
                    filled_this_round.append(order)
            if not filled_this_round:
                break  # nothing else to fill
            # Remove filled orders
            orders_to_check = [o for o in orders_to_check if not o.filled]

        # 3. Update equity
        update_equity(active_day)

        # 4. End-of-day check
        if tick.time >= EOD_time:
            for pos in active_day.positions:
                if pos.open:
                    price = tick.bid if pos.side == "long" else tick.ask
                    close_position(pos, price, tick.time, log)
            cancel_all_orders(active_day, log, tick.time)
            break

    return active_day, log

# -----------------------------
# 4. Usage Example
# -----------------------------
if __name__ == "__main__":
    ticks = [
        Tick(time=0, bid=10000, ask=10001),
        Tick(time=1, bid=10010, ask=10011),
        Tick(time=2, bid=10020, ask=10021),
        Tick(time=3, bid=10022, ask=10023),
        Tick(time=4, bid=10040, ask=10041),
        Tick(time=5, bid=10038, ask=10038),
        Tick(time=6, bid=10025, ask=10025),
        Tick(time=7, bid=10020, ask=10021),
        Tick(time=9, bid=10017, ask=1019),
        Tick(time=10, bid=10015, ask=10016),
        Tick(time=11, bid=10005, ask=10006),
        Tick(time=12, bid=9995, ask=9996),
        Tick(time=13, bid=10005, ask=10006),
    ]
    
    base_price = 10000
    step_distance = 10
    num_steps = 3
    tp_distance = 10
    EOD_time = 10
    
    active_day, log = run_tick_backtest(ticks, base_price, step_distance, num_steps, tp_distance, EOD_time)
    
    print("\n--- TRADE LOG ---")
    for entry in log:
        print(entry)
