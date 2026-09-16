-- Minimal schema for the risk rails. Four tables, no venue or strategy state.

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,         -- Format: OE-YYYY-MM-DD-NNN
    market_id TEXT NOT NULL,
    prediction_id INTEGER,
    direction TEXT NOT NULL,           -- 'YES' | 'NO'
    entry_price DECIMAL NOT NULL,
    quantity DECIMAL NOT NULL,
    size_usd DECIMAL NOT NULL,
    kelly_fraction REAL,
    edge_at_entry REAL,
    confidence TEXT,                   -- 'HIGH' | 'MEDIUM' | 'LOW'
    signal_source TEXT NOT NULL DEFAULT 'prediction',
    paper_trade INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'closed' | 'cancelled'
    exit_price DECIMAL,
    pnl_usd DECIMAL,
    pnl_pct REAL,
    clv REAL,                         -- Closing Line Value: positive = beat the close = real edge
    loss_classification TEXT,          -- 'good_loss' | 'bad_loss' | 'unlucky' | NULL
    order_id TEXT,                     -- venue order ID
    fill_status TEXT,                  -- 'filled' | 'partial' | 'unfilled'
    slippage DECIMAL,
    opened_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    closed_at TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(market_id),
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    bankroll DECIMAL NOT NULL,
    peak_bankroll DECIMAL NOT NULL,    -- High-water mark
    drawdown_pct REAL,
    drawdown_tier TEXT,                -- 'NORMAL' | 'REDUCED' | 'KILLED'
    total_trades INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    losing_trades INTEGER NOT NULL DEFAULT 0,
    win_rate REAL,
    profit_factor REAL,
    daily_pnl DECIMAL,
    cumulative_pnl DECIMAL,
    brier_score REAL,
    category_performance_json TEXT,    -- Per-category JSON breakdown
    platform_performance_json TEXT,    -- Per-platform JSON breakdown
    api_costs_usd DECIMAL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS api_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,             -- 'inference' | 'market-data' | 'other'
    endpoint TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd DECIMAL NOT NULL,
    model TEXT,
    context TEXT,                      -- What the call was for
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_positions INTEGER,
    total_cost REAL,
    total_value REAL,
    unrealized_pnl REAL,
    free_cash REAL,
    ai_value REAL,
    sports_value REAL,
    snapshot_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    question TEXT NOT NULL,
    description TEXT,
    category TEXT,
    end_date TEXT,
    token_id_yes TEXT,
    token_id_no TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolution_outcome TEXT,          -- 'YES' | 'NO' | NULL if unresolved
    volume_24h DECIMAL,
    liquidity DECIMAL,
    last_price_yes REAL,
    last_price_no REAL,
    spread REAL,
    flagged INTEGER NOT NULL DEFAULT 0,
    flag_reason TEXT,
    first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_scanned TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    metadata_json TEXT                -- Extra market metadata as JSON
);
