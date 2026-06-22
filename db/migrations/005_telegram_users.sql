CREATE TABLE IF NOT EXISTS telegram_users (
  id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT NOT NULL DEFAULT '',
  last_name TEXT,
  language_code TEXT,
  is_premium BOOLEAN NOT NULL DEFAULT false,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  start_count INT NOT NULL DEFAULT 0,
  last_start_payload TEXT
);

CREATE INDEX IF NOT EXISTS telegram_users_last_seen_idx
  ON telegram_users (last_seen_at DESC);
