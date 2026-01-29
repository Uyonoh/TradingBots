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
        
        start_date = datetime(2026, 1, 28)
        end_date = datetime(2026, 1, 28)

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

if __name__ == "__main__":
    downloader = TickDownloader("GER40")
    downloader.download_ticks()