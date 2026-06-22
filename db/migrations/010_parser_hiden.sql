-- hiden.live catalog state (auto-fallback to ebalka when hiden_complete)
ALTER TABLE parser_state
  ADD COLUMN IF NOT EXISTS hiden_current_page INT,
  ADD COLUMN IF NOT EXISTS hiden_total_pages INT,
  ADD COLUMN IF NOT EXISTS hiden_complete BOOLEAN NOT NULL DEFAULT false;

INSERT INTO app_settings (key, value, description) VALUES
  ('parser.primary_source', 'hiden', 'Primary catalog: hiden until hiden_complete, then ebalka'),
  ('hiden.source_origin', 'https://hiden.live', 'hiden.live base URL for parser + uploader')
ON CONFLICT (key) DO NOTHING;
