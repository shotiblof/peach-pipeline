ALTER TABLE telegram_users
  ADD COLUMN IF NOT EXISTS preferred_language TEXT;

ALTER TABLE telegram_users
  DROP CONSTRAINT IF EXISTS telegram_users_preferred_language_check;

ALTER TABLE telegram_users
  ADD CONSTRAINT telegram_users_preferred_language_check
  CHECK (preferred_language IS NULL OR preferred_language IN ('ru', 'en'));
