-- How the Retirement projection should treat this account's money.
-- '' means "infer from type and name" (the old behaviour).
ALTER TABLE accounts ADD COLUMN tax_treatment TEXT NOT NULL DEFAULT '';
