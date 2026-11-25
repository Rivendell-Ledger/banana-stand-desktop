-- 016: daily closes for the benchmarks and portfolio symbols (Yahoo chart API cache)
CREATE TABLE IF NOT EXISTS market_bars (
    symbol      TEXT    NOT NULL,
    as_of_date  TEXT    NOT NULL,
    close_cents INTEGER NOT NULL,
    fetched_at  TEXT    NOT NULL,
    PRIMARY KEY (symbol, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_market_bars_symbol ON market_bars(symbol, as_of_date);
