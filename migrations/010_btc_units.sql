-- Bitcoin accounts hold a quantity (satoshis, 1e-8 BTC) as the source of truth.
-- current_balance_cents stays the USD value, recomputed from the live BTC price.
ALTER TABLE accounts ADD COLUMN units_sats INTEGER;
