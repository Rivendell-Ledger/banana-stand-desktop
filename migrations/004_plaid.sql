-- 004: Plaid connections
--
-- Access tokens are stored encrypted (Fernet, key in secret/fernet.key, mode 600).
-- Nothing in this table is safe to log: never print access_token_enc or the plaintext.

CREATE TABLE IF NOT EXISTS plaid_items (
    id                INTEGER PRIMARY KEY,
    household_id      INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    plaid_item_id     TEXT    NOT NULL UNIQUE,          -- Plaid's item_id
    access_token_enc  TEXT    NOT NULL,
    institution_id    TEXT,
    institution_name  TEXT,
    cursor            TEXT,                             -- /transactions/sync cursor
    status            TEXT    NOT NULL DEFAULT 'good',  -- good | login_required | error
    error_code        TEXT,
    last_sync_at      TEXT,
    last_sync_summary TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plaid_items_household ON plaid_items(household_id);

-- One local account per Plaid account. The columns already exist on accounts.
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_plaid_account
    ON accounts(plaid_account_id) WHERE plaid_account_id IS NOT NULL;

ALTER TABLE transactions ADD COLUMN plaid_item_id INTEGER REFERENCES plaid_items(id);

-- Idempotent upserts from /transactions/sync.
CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_plaid_external
    ON transactions(household_id, external_id) WHERE external_id IS NOT NULL;
