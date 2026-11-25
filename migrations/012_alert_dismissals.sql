-- Alerts are evaluated on demand, so "clear" records the dismissal, not the alert.
-- Scoped to a day: a condition that still holds tomorrow deserves to be seen again.
CREATE TABLE alert_dismissals (
  household_id   INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
  alert_key      TEXT NOT NULL,
  dismissal_date TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (household_id, alert_key, dismissal_date)
);

CREATE INDEX ix_alert_dismissals_day ON alert_dismissals(household_id, dismissal_date);
