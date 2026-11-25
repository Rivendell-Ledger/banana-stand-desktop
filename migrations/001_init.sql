-- 001_init.sql — Phase 0: household, auth, sessions, settings, audit
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS households (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'USD',
  timezone      TEXT NOT NULL DEFAULT 'America/New_York',
  buffer_cents  INTEGER NOT NULL DEFAULT 150000,
  week_start    INTEGER NOT NULL DEFAULT 6,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id   INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
  email          TEXT NOT NULL UNIQUE,
  display_name   TEXT NOT NULL,
  password_hash  TEXT NOT NULL,
  failed_logins  INTEGER NOT NULL DEFAULT 0,
  locked_until   TEXT,
  created_at     TEXT NOT NULL,
  last_login_at  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  ip           TEXT,
  user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS settings (
  household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
  key          TEXT NOT NULL,
  value_json   TEXT NOT NULL,
  PRIMARY KEY (household_id, key)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  household_id INTEGER,
  user_id      INTEGER,
  action       TEXT NOT NULL,
  target_type  TEXT,
  target_id    TEXT,
  meta_json    TEXT,
  ip           TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log(created_at DESC);
