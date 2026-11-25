-- 015: a category can be kept out of the Budget tab without leaving the taxonomy
-- (Transactions filters and reports still use it).
ALTER TABLE categories ADD COLUMN hide_from_budget INTEGER NOT NULL DEFAULT 0;
