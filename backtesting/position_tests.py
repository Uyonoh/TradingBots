


default_lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.02, 0.01, 0.01, 0.01, 0.02]
hybid_lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.07]
def calculate_gross_profit(direction:str, entry: int|float, open_pos:int, tick, lots:list[float]=default_lots, spacing: int=50):
    if direction.lower() == "buy":
        dir_multiplier = 1
        price = tick.bid
    else:
        dir_multiplier = -1
        price = tick.ask

    p0 = (price - entry) * dir_multiplier
    lots_sum = sum(lots[:open_pos])
    weighted_lots_sum = sum([i*l for (i, l) in enumerate(lots[:open_pos])])
    profit = (p0 * lots_sum) - (spacing * weighted_lots_sum)

    return round(profit, 5)

def calculate_loss_excess(direction:str, entry: int|float, SL:int, open_pos:int, tick, lots:list[float]=default_lots, spacing: int=50):
    if direction.lower() == "buy":
        dir_multiplier = 1
        price = tick.bid
    else:
        dir_multiplier = -1
        price = tick.ask

    p0 = (price - entry) * dir_multiplier
    p0 += SL
    if p0 >= SL: return 0
    lots_clipped = [l for (i, l) in enumerate(lots) if p0 < i*spacing]

    # Determine indexes of  positions beyond SL
    idx = 0
    for i, l in enumerate(lots):
        if p0 < i*spacing:
            idx = i
            break

    lots_sum = sum(lots[idx:open_pos])
    weighted_lots_sum = sum([i*l for (i, l) in enumerate(lots[:open_pos])][idx:])
    loss = (p0 * lots_sum) - (spacing * weighted_lots_sum)

    return round(loss, 5)

class Tick:
    def __init__(self, ask, spread):
        self.ask = ask
        self.bid = self.ask - spread

def main():
    # ask, bid -> buy, sell
    # use after entry sell, buy
    spread = 0
    p_ticks = 400
    tick = Tick(200+p_ticks, spread)
    entry_a = 200
    entry_b = entry_a - spread
    sl = 400
    buy_pos = 8
    sell_pos = 1
    lots = [0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.21] #[0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.02, 0.02, 0.03, 0.07]
    spacing = 50
    # print(f"Total lots: {sum([i*l for (i, l) in enumerate(lots)])}")
    # lots = [0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.14]
    # lots = default_lots

    spread = 0
    p_ticks = 100
    p_ticks += spread
    tick = Tick(200+p_ticks, spread)
    entry_a = 200
    entry_b = entry_a - spread
    sl = 50
    buy_pos = 6
    sell_pos = 1
    lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.07]
    # lots = [0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.07]
    # lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.02, 0.01, 0.01, 0.01, 0.02]
    spacing = 10

    # =========================================
    #           USA30 1-4
    # =========================================

    # spread = 0
    # p_ticks = 70
    # p_ticks += spread
    # tick = Tick(200+p_ticks, spread)
    # entry_a = 200
    # entry_b = entry_a - spread
    # sl = 70
    # buy_pos = 6
    # sell_pos = 5
    # lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.07]
    # # lots = [0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.07]
    # # lots = [0.01, 0.01, 0.02, 0.01, 0.01, 0.02, 0.01, 0.01, 0.01, 0.02]
    # spacing = 10

    p1 = calculate_gross_profit("buy", entry_a, buy_pos, tick, lots=lots, spacing=spacing)
    p2 = calculate_gross_profit("sell", entry_b, sell_pos, tick, lots=lots, spacing=spacing)
    e1 = calculate_loss_excess("buy", entry_b, sl, buy_pos, tick, lots=lots, spacing=spacing)
    e2 = calculate_loss_excess("sell", entry_b, sl, sell_pos, tick, lots=lots, spacing=spacing)
    print(f'Profit: {p1}')
    print(f'Loss: {p2}')
    print(f'Excess on buys: {e1}')
    print(f'Excess on sells: {e2}')
    print(f"GROSS P: {p1+p2}")
    print(f"NET P: {p1+p2-e1-e2}")
    return

if __name__ == "__main__":
    main()
