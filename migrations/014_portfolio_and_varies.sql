-- Manual accounts the household wants reflected in the Portfolio, and a flag for recurring
-- bills whose amount swings month to month (utilities) so the forecast can say so.
ALTER TABLE accounts ADD COLUMN include_in_portfolio INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recurring ADD COLUMN amount_varies INTEGER NOT NULL DEFAULT 0;

-- Opt-in only: nothing joins the portfolio until the household ticks the box.
