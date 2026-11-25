-- 018_password_hint.sql — a user-chosen hint, shown on the sign-in page.
-- Stored in clear text on purpose: it is meant to be readable before sign-in, so it
-- must never contain the password itself. Empty/NULL means "no hint".
ALTER TABLE users ADD COLUMN password_hint TEXT;
