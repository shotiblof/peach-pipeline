-- Poster (jpg) separate from swipe preview mp4
ALTER TABLE videos ADD COLUMN IF NOT EXISTS poster_path TEXT;
