-- Editable payment buttons for full-video Telegram offer
INSERT INTO app_settings (key, value, description) VALUES
  ('payment.crypto_url', 'https://t.me/CryptoBot', 'URL кнопки оплаты Crypto Bot'),
  ('payment.stars_url', 'https://t.me/', 'URL кнопки оплаты Telegram Stars'),
  ('payment.crypto_label_ru', 'Оплата Crypto Bot', 'Подпись кнопки Crypto RU'),
  ('payment.crypto_label_en', 'Pay with Crypto Bot', 'Подпись кнопки Crypto EN'),
  ('payment.stars_label_ru', 'Оплата Stars', 'Подпись кнопки Stars RU'),
  ('payment.stars_label_en', 'Pay with Stars', 'Подпись кнопки Stars EN')
ON CONFLICT (key) DO NOTHING;
