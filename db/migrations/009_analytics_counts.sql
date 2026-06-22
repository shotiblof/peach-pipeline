-- Daily aggregates only (no raw events). Cheap UPSERT per beacon.
CREATE TABLE IF NOT EXISTS analytics_counts (
  day DATE NOT NULL DEFAULT CURRENT_DATE,
  metric TEXT NOT NULL,
  dimension TEXT NOT NULL DEFAULT '',
  count BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, metric, dimension)
);

CREATE INDEX IF NOT EXISTS analytics_counts_day_metric_idx
  ON analytics_counts (day DESC, metric);
