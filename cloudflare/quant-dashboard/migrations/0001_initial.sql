-- Idempotent baseline for the read-only dashboard pointer database.
CREATE TABLE IF NOT EXISTS generations (
  generation_id TEXT PRIMARY KEY,
  market_as_of TEXT NOT NULL,
  data_timestamp_utc TEXT NOT NULL,
  generated_at_utc TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL UNIQUE,
  company_count INTEGER NOT NULL,
  triggered_company_count INTEGER NOT NULL,
  conditional_company_count INTEGER NOT NULL,
  pending_company_count INTEGER NOT NULL,
  source_commit TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_generation (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  generation_id TEXT NOT NULL REFERENCES generations(generation_id),
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_generations_market_as_of
  ON generations(market_as_of DESC);
