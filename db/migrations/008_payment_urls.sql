-- Peach payment links (Crypto Bot invoice + Stars channel)
UPDATE app_settings
SET value = 'https://t.me/send?start=IV5MoyI6cJ5l', updated_at = now()
WHERE key = 'payment.crypto_url';

UPDATE app_settings
SET value = 'https://t.me/+9nriWOalzSMzYzVh', updated_at = now()
WHERE key = 'payment.stars_url';
