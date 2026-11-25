-- 008: portfolio — securities, holdings, flows, options
--
-- A holding is the only place a share count lives; the account balance is derived from holdings
-- when the account has any, so net worth can never double count.

CREATE TABLE IF NOT EXISTS securities (
    id           INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    symbol       TEXT    NOT NULL,
    name         TEXT,
    kind         TEXT    NOT NULL DEFAULT 'stock',  -- stock|etf|fund|bond|crypto|cash|other
    created_at   TEXT    NOT NULL,
    UNIQUE (household_id, symbol)
);

CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY,
    household_id    INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    security_id     INTEGER NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
    quantity        REAL    NOT NULL DEFAULT 0,
    cost_basis_cents INTEGER NOT NULL DEFAULT 0,   -- total paid, not per share
    price_cents     INTEGER NOT NULL DEFAULT 0,    -- latest price per share
    priced_on       TEXT,
    notes           TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (account_id, security_id)
);
CREATE INDEX IF NOT EXISTS idx_holdings_household ON holdings(household_id);

CREATE TABLE IF NOT EXISTS investment_flows (
    id           INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    account_id   INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    occurred_on  TEXT    NOT NULL,
    amount_cents INTEGER NOT NULL,                  -- positive = money contributed
    note         TEXT,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS option_positions (
    id               INTEGER PRIMARY KEY,
    household_id     INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    account_id       INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    underlying       TEXT    NOT NULL,
    option_type      TEXT    NOT NULL DEFAULT 'put',  -- call|put
    strike_cents     INTEGER NOT NULL,
    expiry_on        TEXT    NOT NULL,
    contracts        INTEGER NOT NULL DEFAULT 1,
    premium_cents    INTEGER NOT NULL DEFAULT 0,      -- per share, positive = credit received
    opened_on        TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'open', -- open|closed|expired|assigned
    closed_on        TEXT,
    close_price_cents INTEGER,
    notes            TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_options_household ON option_positions(household_id, status);
