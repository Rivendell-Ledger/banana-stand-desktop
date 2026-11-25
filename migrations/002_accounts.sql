-- 002_accounts.sql — Phase 1: accounts, balance history, net worth snapshots
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id            INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
  institution             TEXT,
  name                    TEXT NOT NULL,
  mask                    TEXT,
  type                    TEXT NOT NULL,
  subtype                 TEXT,
  currency                TEXT NOT NULL DEFAULT 'USD',
  current_balance_cents   INTEGER NOT NULL DEFAULT 0,
  available_balance_cents INTEGER,
  credit_limit_cents      INTEGER,
  apr_bps                 INTEGER,
  include_in_net_worth    INTEGER NOT NULL DEFAULT 1,
  include_in_budget       INTEGER NOT NULL DEFAULT 1,
  is_manual               INTEGER NOT NULL DEFAULT 1,
  manual_balance_as_of    TEXT,
  plaid_item_id           TEXT,
  plaid_account_id        TEXT,
  display_order           INTEGER NOT NULL DEFAULT 0,
  archived_at             TEXT,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_accounts_household ON accounts(household_id, archived_at);

CREATE TABLE IF NOT EXISTS account_balances (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  as_of_date      TEXT NOT NULL,
  balance_cents   INTEGER NOT NULL,
  available_cents INTEGER,
  source          TEXT NOT NULL DEFAULT 'manual',
  created_at      TEXT NOT NULL,
  UNIQUE(account_id, as_of_date)
);
CREATE INDEX IF NOT EXISTS ix_balances_account_date ON account_balances(account_id, as_of_date DESC);

CREATE TABLE IF NOT EXISTS net_worth_snapshots (
  as_of_date      TEXT NOT NULL,
  household_id    INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
  assets_cents    INTEGER NOT NULL,
  liabilities_cents INTEGER NOT NULL,
  net_cents       INTEGER NOT NULL,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (as_of_date, household_id)
);
