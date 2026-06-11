CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  repo_slug TEXT NOT NULL,
  checkout_path TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  base TEXT NOT NULL,
  verify_cmd TEXT NOT NULL,
  require_new_test INTEGER NOT NULL,
  coding_model TEXT NOT NULL,
  branch TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  pr_url TEXT,
  question TEXT,
  answer TEXT,
  opencode_pid INTEGER,
  token_usage INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  ended_at TEXT,
  summary TEXT,
  tokens_used INTEGER
);

CREATE TABLE IF NOT EXISTS parked_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  question TEXT NOT NULL,
  parked_at TEXT NOT NULL,
  answer TEXT,
  answered_at TEXT,
  FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS daemon_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_messages (
    msg_id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_discord_messages_task_id ON discord_messages(task_id);

CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    pr_url TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    clean INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_runs_pr_url ON review_runs(pr_url);

CREATE TABLE IF NOT EXISTS pr_watches (
    pr_url TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    repo_slug TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    branch TEXT NOT NULL,
    base TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'watching',
    fix_attempts INTEGER NOT NULL DEFAULT 0,
    last_signature TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pr_watches_state ON pr_watches(state);
