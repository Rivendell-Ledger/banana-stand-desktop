-- 007: recurring bills and income
--
-- A recurring row is a claim about the future: this merchant charges about this much,
-- this often, next around this date. Suggestions come from history; confirmation is human.

CREATE TABLE IF NOT EXISTS recurring (
    id               INTEGER PRIMARY KEY,
    household_id     INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name             TEXT    NOT NULL,
    merchant_key     TEXT    NOT NULL,
    account_id       INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    category_id      INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    cadence          TEXT    NOT NULL DEFAULT 'monthly',   -- weekly|biweekly|monthly|quarterly|semiannual|annual
    interval_days    INTEGER NOT NULL DEFAULT 30,
    anchor_day       INTEGER,                              -- day of month for monthly
    amount_cents     INTEGER NOT NULL DEFAULT 0,           -- positive magnitude
    kind             TEXT    NOT NULL DEFAULT 'bill',      -- bill|income
    next_due_on      TEXT    NOT NULL,
    last_seen_on     TEXT,
    confidence       INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'confirmed', -- suggested|confirmed|ignored|ended
    notes            TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recurring_household ON recurring(household_id, status);
