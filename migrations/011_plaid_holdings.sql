-- Plaid Investments: holdings are synced, not typed in. Keep them distinguishable
-- from manual rows so the Portfolio form never overwrites a broker's numbers.
ALTER TABLE holdings ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE securities ADD COLUMN external_id TEXT;
ALTER TABLE plaid_items ADD COLUMN holdings_synced_at TEXT;
