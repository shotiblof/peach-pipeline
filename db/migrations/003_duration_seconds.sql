ALTER TABLE videos
  ADD COLUMN IF NOT EXISTS duration_seconds INT;
