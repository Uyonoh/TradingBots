import os
import time
import csv
from datetime import datetime
import MetaTrader5 as mt5

# ==================== CONFIGURATION ====================
CSV_FILE = "./logs/mt5_margin_log.csv"
LOG_INTERVAL_SECONDS = 1  # Frequency of checks (e.g., every 1 second)
# =======================================================

def initialize_mt5():
    """Initializes connection to the MT5 terminal."""
    if not mt5.initialize():
        print(f"Initialization failed. Error code: {mt5.last_error()}")
        quit()
    print("Successfully connected to MetaTrader 5 Terminal.")

def get_volumes():
    """ Get the total volumes for open positions """
    volume = 0
    positions = mt5.positions_get()
    for position in positions:
        volume += position.volume

    return volume

def log_account_metrics():
    """Continuously monitors and logs margin, equity, and asset state."""
    # Useful data headers for comprehensive visualization later
    headers = [
        "Timestamp",
        "Balance",
        "Equity",
        "Margin",
        "Free_Margin",
        "Margin_Level_Pct",
        "Floating_Profit",
        "Total_Open_Positions",
        "Total Volume"
    ]

    # Check if file exists so we don't overwrite headers on script restarts
    file_exists = os.path.isfile(CSV_FILE)

    # Open the file in append mode ('a') with line-buffering disabled
    with open(CSV_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)

        if not file_exists:
            writer.writerow(headers)
            file.flush()

        print(f"Tracking active. Data logging to '{CSV_FILE}' every {LOG_INTERVAL_SECONDS}s.")
        print("Press Ctrl+C to safely exit the script.\n")

        try:
            while True:
                account_info = mt5.account_info()

                if account_info is None:
                    print(f"Error fetching account info: {mt5.last_error()}")
                    time.sleep(LOG_INTERVAL_SECONDS)
                    continue

                # Fetching data points
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                balance = account_info.balance
                equity = account_info.equity
                margin = account_info.margin
                free_margin = account_info.margin_free
                margin_level = account_info.margin_level
                profit = account_info.profit
                positions_count = mt5.positions_total()
                total_volume = get_volumes()

                # Append row to CSV
                writer.writerow([
                    timestamp, balance, equity, margin,
                    free_margin, margin_level, profit,
                    positions_count,total_volume
                ])
                file.flush()  # Forces data out of the buffer and into the CSV immediately

                # Live console printout to keep an eye on things live
                print(f"[{timestamp}] Equity: ${equity:,.2f} | Margin: ${margin:,.2f} | "
                    f"Margin Level: {margin_level:.2f}% | "
                    f"Open Positions: {positions_count} | Total Volume: {total_volume}")

                time.sleep(LOG_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\nMonitoring stopped by user.")
        finally:
            mt5.shutdown()
            print("MetaTrader 5 connection closed securely.")

if __name__ == "__main__":
    initialize_mt5()
    log_account_metrics()
