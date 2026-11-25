-- 003: transactions, categories, tags, splits, full-text search
--
-- Sign convention: amount_cents < 0 is money leaving the household (expense, card
-- purchase, transfer out). > 0 is money arriving (income, refund, transfer in).
-- Liabilities stay positive magnitudes on the accounts table; the sign lives here.

CREATE TABLE IF NOT EXISTS categories (
    id            INTEGER PRIMARY KEY,
    household_id  INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'expense'
                  CHECK (kind IN ('expense', 'income', 'transfer')),
    parent_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    color         TEXT,
    is_system     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_categories_household ON categories(household_id, kind, name);

CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY,
    household_id     INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    posted_on        TEXT    NOT NULL,              -- YYYY-MM-DD
    amount_cents     INTEGER NOT NULL,
    merchant         TEXT    NOT NULL,
    notes            TEXT,
    category_id      INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    is_pending       INTEGER NOT NULL DEFAULT 0,
    is_transfer      INTEGER NOT NULL DEFAULT 0,
    excluded         INTEGER NOT NULL DEFAULT 0,    -- keep the row, out of spend math
    transfer_peer_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    external_id      TEXT,                          -- provider id (Plaid, Phase 6)
    fingerprint      TEXT,                          -- CSV dedupe (Phase 3)
    deleted_at       TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_household_date ON transactions(household_id, posted_on DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_txn_account         ON transactions(account_id, posted_on DESC);
CREATE INDEX IF NOT EXISTS idx_txn_category        ON transactions(category_id);
CREATE INDEX IF NOT EXISTS idx_txn_deleted         ON transactions(deleted_at);
CREATE INDEX IF NOT EXISTS idx_txn_fingerprint     ON transactions(household_id, fingerprint);

CREATE TABLE IF NOT EXISTS transaction_splits (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    amount_cents   INTEGER NOT NULL,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_split_txn ON transaction_splits(transaction_id);

CREATE TABLE IF NOT EXISTS tags (
    id           INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    UNIQUE (household_id, name)
);

CREATE TABLE IF NOT EXISTS transaction_tags (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id         INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (transaction_id, tag_id)
);

-- Full-text search over merchant and notes. External content keeps the index in
-- step with the table through triggers; the list query still filters deleted_at.
CREATE VIRTUAL TABLE IF NOT EXISTS transactions_fts USING fts5(
    merchant,
    notes,
    content='transactions',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS txn_ai AFTER INSERT ON transactions BEGIN
    INSERT INTO transactions_fts(rowid, merchant, notes)
    VALUES (new.id, new.merchant, coalesce(new.notes, ''));
END;

CREATE TRIGGER IF NOT EXISTS txn_ad AFTER DELETE ON transactions BEGIN
    INSERT INTO transactions_fts(transactions_fts, rowid, merchant, notes)
    VALUES ('delete', old.id, old.merchant, coalesce(old.notes, ''));
END;

CREATE TRIGGER IF NOT EXISTS txn_au AFTER UPDATE ON transactions BEGIN
    INSERT INTO transactions_fts(transactions_fts, rowid, merchant, notes)
    VALUES ('delete', old.id, old.merchant, coalesce(old.notes, ''));
    INSERT INTO transactions_fts(rowid, merchant, notes)
    VALUES (new.id, new.merchant, coalesce(new.notes, ''));
END;
