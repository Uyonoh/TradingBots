# get_mt5_data.py
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime

def get_mt5_ohlc_data(symbol, tf, start_pos, count):
    timeframes = {
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
        "M1": mt5.TIMEFRAME_M1,
    }
    timeframe = timeframes[tf]
    if not mt5.initialize():
        print(f"initialize() failed, error code = {mt5.last_error()}")
        return None

    # Request historical data
    rates = mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
    mt5.shutdown()

    if rates is None or len(rates) == 0:
        print("No data retrieved")
        return None

    # Create a DataFrame and format it for backtesting.py
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    
    # Rename columns to match backtesting.py requirements (case sensitive)
    df.rename(columns={
        'open': 'Open',
        'high': 'High',
        'low': 'Low',
        'close': 'Close',
        'tick_volume': 'Volume'
    }, inplace=True)
    
    return df

# # Fetch 10,000 bars of daily EURUSD data
# data = get_mt5_ohlc_data("EURUSD", mt5.TIMEFRAME_D1, 0, 10000)

# if data is not None:
#     print(f"Data shape: {data.shape}")
#     print(data.head())
#     # Save to a pickle file or use the DataFrame directly in the next script
#     data.to_pickle("EURUSD_D1_Data.pkl")
