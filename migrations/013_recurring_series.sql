-- A merchant can legitimately bill or pay more than once per period (two Social Security
-- deposits landing the same day, for example). `series` numbers those parallel tracks so the
-- scanner can keep one row per track instead of collapsing them into one.
ALTER TABLE recurring ADD COLUMN series INTEGER NOT NULL DEFAULT 0;
