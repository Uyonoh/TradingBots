import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
from deepticks import TickBasedLadderStrategy

class TickDownloader:
    def __init__(self, symbol):
        self.symbol = symbol.upper()

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
    
    def download_ticks(self):
        print("Beginign")
        
        start_date = datetime(2025, 1, 1)
        end_date = datetime(2026, 1, 31)

        from_date = int(start_date.timestamp())
        to_date = int(end_date.timestamp())

        # strategy = TickBasedLadderStrategy("GER40", 100)
        if not self.initialize_mt5():
            print("Failed to initialize MT5")
            return
        
        ticks = mt5.copy_ticks_range(self.symbol, from_date, to_date, mt5.COPY_TICKS_ALL)
        
        if ticks is None or len(ticks) == 0:
            print("Failed to fetch ticks from range, trying alternate methods...")
            ticks = mt5.copy_ticks_from(self.symbol, from_date, 1000000, mt5.COPY_TICKS_ALL)
            if ticks is None or len(ticks) == 0:
                return []
            
        mt5.shutdown()

        ticks_df = pd.DataFrame(ticks)
        ticks_df["time"] = pd.to_datetime(ticks_df["time"], unit="s")

        file_path = f"{self.symbol}_ticks[{start_date.date().isoformat()} - {end_date.date().isoformat()}].csv"
        ticks_df.to_csv(file_path, index=False)
        print(f"Successfully saved ticks to {file_path}")

from datetime import datetime, timedelta
from pathlib import Path
import os
import pytz

CET = pytz.timezone('Europe/Berlin')
UTC = pytz.utc

class DAXTickDataLoader:
    """
    Memory-optimized loading of MT5 tick data.
    Fetches data in monthly chunks, downcasts, saves as Parquet.
    """
    def __init__(self, symbol='GER40', data_dir='./tick_data'):
        self.symbol = symbol
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
    def fetch_ticks_chunk(self, start_dt, end_dt):
        """Fetch ticks for a given chunk (max 1 month) from MT5."""
        if not mt5.initialize():
            raise ConnectionError("MT5 initialize failed")
        ticks = mt5.copy_ticks_range(self.symbol, start_dt, end_dt, mt5.COPY_TICKS_ALL)
        mt5.shutdown()
        if ticks is None or len(ticks) == 0:
            return None
        df = pd.DataFrame(ticks)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('time', inplace=True)
        # Downcast to save memory
        df[['bid','ask','last']] = df[['bid','ask','last']].astype('float32')
        df[['volume','flags']] = df[['volume','flags']].astype('int32')
        return df
    
    def fetch_and_store_range(self, start_date, end_date):
        """Fetch ticks in monthly chunks and store as partitioned Parquet."""
        current = start_date.replace(day=1, hour=0, minute=0, second=0)
        end_date = end_date.replace(hour=23, minute=59, second=59)
        while current < end_date:
            month_end = (current + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
            month_end = min(month_end, end_date)
            print(f"Processing {current.date()} to {month_end.date()}")
            year, month = current.year, current.month
            path = self.data_dir / self.symbol / f"{self.symbol}_{year}_{month:02d}.parquet"

            if not os.path.exists(path):
                df = self.fetch_ticks_chunk(current, month_end)
                if df is not None:   
                    df.to_parquet(path, compression='zstd')
                    print(f"    File saved to {path}")
                else:
                    print("Empty dataset")
            else:
                print("    File already exists")
            current = (month_end + timedelta(seconds=1)).replace(day=1)
        print("Data fetch complete.")
    
    def load_chunk(self, year, month):
        """Load a single monthly parquet file."""
        path = self.data_dir / f"{self.symbol}_{year}_{month:02d}.parquet"
        if not os.path.exists(path): #path.exists():
            raise FileNotFoundError(f"The file at {path} cannot be found!. \nThe current directory is {os.getcwd()}")
        df = pd.read_parquet(path)
        # Ensure UTC index
        df.index = pd.to_datetime(df.index).tz_localize(UTC)
        return df

if __name__ == "__main__":
    # downloader = TickDownloader("GER40")
    # downloader.download_ticks()

    loader = DAXTickDataLoader(symbol='GER40', data_dir='./tick_data')
    loader.fetch_and_store_range(
        start_date=datetime(2026,1,1, tzinfo=UTC),
        end_date=datetime(2026,3,31, tzinfo=UTC)
    )