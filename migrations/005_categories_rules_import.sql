-- 005: categories, rules, merchant aliases, CSV import
--
-- category_source records who set a transaction's category. 'manual' is sacred: the rules
-- engine and imports may only touch rows whose category is NULL or came from a rule/import.

ALTER TABLE categories ADD COLUMN archived_at TEXT;
ALTER TABLE categories ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;

ALTER TABLE transactions ADD COLUMN raw_merchant TEXT;
ALTER TABLE transactions ADD COLUMN category_source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE transactions ADD COLUMN import_batch_id INTEGER;

CREATE TABLE IF NOT EXISTS category_rules (
    id              INTEGER PRIMARY KEY,
    household_id    INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL DEFAULT 100,
    conditions_json TEXT    NOT NULL,
    action_type     TEXT    NOT NULL CHECK (action_type IN ('category', 'tag', 'transfer', 'exclude')),
    action_value    TEXT,
    apply_to        TEXT    NOT NULL DEFAULT 'uncategorized',  -- uncategorized | all
    match_count     INTEGER NOT NULL DEFAULT 0,
    last_run_at     TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_household ON category_rules(household_id, priority, id);

CREATE TABLE IF NOT EXISTS merchant_aliases (
    id           INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    pattern      TEXT    NOT NULL,          -- normalized raw merchant
    alias        TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    UNIQUE (household_id, pattern)
);

CREATE TABLE IF NOT EXISTS import_profiles (
    id           INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    account_id   INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    delimiter    TEXT    NOT NULL DEFAULT ',',
    has_header   INTEGER NOT NULL DEFAULT 1,
    date_col     TEXT    NOT NULL,
    desc_col     TEXT    NOT NULL,
    amount_col   TEXT,
    debit_col    TEXT,
    credit_col   TEXT,
    category_col TEXT,
    memo_col     TEXT,
    date_format  TEXT    NOT NULL DEFAULT '%m/%d/%Y',
    invert_sign  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS import_batches (
    id              INTEGER PRIMARY KEY,
    household_id    INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    account_id      INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    profile_id      INTEGER REFERENCES import_profiles(id) ON DELETE SET NULL,
    filename        TEXT,
    row_count       INTEGER NOT NULL DEFAULT 0,
    imported_count  INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);
