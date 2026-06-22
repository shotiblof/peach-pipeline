INSERT INTO app_settings (key, value, description) VALUES
  (
    'namevids.caption_link',
    'https://t.me/jesovixxx',
    'Ссылка в caption namevids (Telegram-канал или другой URL)'
  )
ON CONFLICT (key) DO UPDATE SET
  value = EXCLUDED.value,
  description = EXCLUDED.description,
  updated_at = now();
