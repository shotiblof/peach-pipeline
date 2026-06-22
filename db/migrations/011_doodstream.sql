-- DoodStream fallback host + expiry tracking for 60-day free tier refresh
ALTER TABLE videos
  ADD COLUMN IF NOT EXISTS host_provider TEXT NOT NULL DEFAULT 'vidara';

ALTER TABLE videos
  DROP CONSTRAINT IF EXISTS videos_host_provider_chk;

ALTER TABLE videos
  ADD CONSTRAINT videos_host_provider_chk
  CHECK (host_provider IN ('vidara', 'doodstream'));

ALTER TABLE videos
  ADD COLUMN IF NOT EXISTS dood_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS videos_dood_refresh_idx
  ON videos (dood_expires_at)
  WHERE host_provider = 'doodstream' AND status = 'published';

-- upload_accounts.provider was ('namevids','vidara') — extend for doodstream API keys
ALTER TABLE upload_accounts
  DROP CONSTRAINT IF EXISTS upload_accounts_provider_check;

ALTER TABLE upload_accounts
  ADD CONSTRAINT upload_accounts_provider_check
  CHECK (provider IN ('namevids', 'vidara', 'doodstream'));
