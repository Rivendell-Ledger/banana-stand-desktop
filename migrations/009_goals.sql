-- 009: goals (reports and alerts need no storage)

CREATE TABLE IF NOT EXISTS goals (
    id            INTEGER PRIMARY KEY,
    household_id  INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    target_cents  INTEGER NOT NULL,
    saved_cents   INTEGER NOT NULL DEFAULT 0,
    target_date   TEXT,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    notes         TEXT,
    status        TEXT    NOT NULL DEFAULT 'active',  -- active|achieved|paused
    achieved_at   TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goals_household ON goals(household_id, status);

CREATE TABLE IF NOT EXISTS goal_contributions (
    id           INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    goal_id      INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,
    occurred_on  TEXT    NOT NULL,
    note         TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goal_contrib ON goal_contributions(goal_id, occurred_on);
