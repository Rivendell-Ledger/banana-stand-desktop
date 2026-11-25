-- 006: monthly budgets
--
-- One row per category per month. A category with no row has no budget.
-- rollover carries the unspent balance into the following month (negative carries too).

CREATE TABLE IF NOT EXISTS budgets (
    id           INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    month        TEXT    NOT NULL,              -- YYYY-MM
    category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    budget_cents INTEGER NOT NULL DEFAULT 0,
    rollover     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (household_id, month, category_id)
);
CREATE INDEX IF NOT EXISTS idx_budgets_month ON budgets(household_id, month);

CREATE TABLE IF NOT EXISTS monthly_plans (
    household_id          INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    month                 TEXT    NOT NULL,
    expected_income_cents INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    PRIMARY KEY (household_id, month)
);
