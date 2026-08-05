-- Trading Bots Database Schema
-- SQLite Database: trades.db.sqlite3

-- Drop tables if they exist (for fresh creation)
DROP TABLE IF EXISTS deal_history;
DROP TABLE IF EXISTS deals;
DROP TABLE IF EXISTS positions;

-- Positions table: Stores trading positions (both open and pending)
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,        -- 'BUY' or 'SELL'
    volume REAL NOT NULL,      -- Volume/lot size
    entry_price REAL NOT NULL, -- Entry price for the position
    status TEXT NOT NULL DEFAULT 'PENDING',  -- 'OPEN', 'PENDING', 'CLOSED', etc.
    strategy_id INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticket_id)
);

-- Deals table: Stores individual deals/trades
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,        -- 'BUY' or 'SELL'
    volume REAL NOT NULL,      -- Volume/lot size
    price REAL NOT NULL,       -- Deal execution price
    commission REAL DEFAULT 0.0,
    swap REAL DEFAULT 0.0,
    profit REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticket_id)
);

-- Deal history table: Stores historical deal information
CREATE TABLE IF NOT EXISTS deal_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    deal_type TEXT NOT NULL,  -- Type of deal (buy, sell, etc.)
    volume REAL NOT NULL,      -- Volume/lot size
    price REAL NOT NULL,       -- Deal price
    profit REAL DEFAULT 0.0,  -- Profit/loss from the deal
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_ticket ON positions(ticket_id);
CREATE INDEX IF NOT EXISTS idx_deals_symbol ON deals(symbol);
CREATE INDEX IF NOT EXISTS idx_deals_ticket ON deals(ticket_id);
CREATE INDEX IF NOT EXISTS idx_deal_history_symbol ON deal_history(symbol);
CREATE INDEX IF NOT EXISTS idx_deal_history_deal_id ON deal_history(deal_id);
