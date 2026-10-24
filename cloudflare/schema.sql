-- Cloudflare D1 Schema for AlphaFlow Serverless SaaS
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  api_key TEXT NOT NULL,
  label TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  cooldown_until INTEGER DEFAULT 0,
  success_count INTEGER DEFAULT 0,
  quota_hits INTEGER DEFAULT 0,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS render_jobs (
  job_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  step INTEGER DEFAULT 0,
  progress INTEGER DEFAULT 0,
  log TEXT,
  duration_sec REAL DEFAULT 0,
  tts_model_used TEXT,
  viral_title TEXT,
  telegram_file_id TEXT,
  params_json TEXT,
  created_at INTEGER NOT NULL
);
