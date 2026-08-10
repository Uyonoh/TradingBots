#!/usr/bin/env python3
"""
Create and initialize the trading bots database.
This script creates the SQLite database file and all required tables.
"""

import sqlite3
import os

# Database configuration
DB_NAME = "trades.db.sqlite3"


def create_database():
    """Create the database and all tables."""
    
    # Remove existing database to ensure clean creation
    if os.path.exists(DB_NAME):
        print(f"Removing existing database: {DB_NAME}")
        os.remove(DB_NAME)
    
    print(f"Creating database: {DB_NAME}")
    
    # Connect to the database (this creates the file)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    try:
        # Drop tables if they exist (for clean creation)
        cursor.execute("DROP TABLE IF EXISTS deal_history")
        cursor.execute("DROP TABLE IF EXISTS deals")
        cursor.execute("DROP TABLE IF EXISTS positions")
        
        # Create positions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                volume REAL NOT NULL,
                entry_price REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                strategy_id INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticket_id)
            )
        """)
        
        # Create deals table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                volume REAL NOT NULL,
                price REAL NOT NULL,
                commission REAL DEFAULT 0.0,
                swap REAL DEFAULT 0.0,
                profit REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticket_id)
            )
        """)
        
        # Create deal_history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                deal_type TEXT NOT NULL,
                volume REAL NOT NULL,
                price REAL NOT NULL,
                profit REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create indexes for better query performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_ticket ON positions(ticket_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deals_symbol ON deals(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deals_ticket ON deals(ticket_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deal_history_symbol ON deal_history(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_deal_history_deal_id ON deal_history(deal_id)")
        
        # Commit the changes
        conn.commit()
        
        print("Database created successfully!")
        print(f"Database file: {os.path.abspath(DB_NAME)}")
        print("Tables created: positions, deals, deal_history")
        
        # Verify the tables were created
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        print(f"\nTables in database:")
        for table in tables:
            print(f"  - {table[0]}")
            
        # Show table schemas
        print(f"\nTable schemas:")
        for table in tables:
            table_name = table[0]
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = cursor.fetchall()
            print(f"\n{table_name}:")
            for col in columns:
                print(f"  - {col[1]}: {col[2]}")
        
        return True
        
    except Exception as e:
        print(f"Error creating database: {e}")
        conn.rollback()
        return False
        
    finally:
        conn.close()


if __name__ == "__main__":
    create_database()
