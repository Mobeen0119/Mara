import os
import sqlite3
import threading

_thread_local = threading.local()
_DEFAULT_DIR = None
_SCHEMA_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  password_hash TEXT NOT NULL,
  is_guest INTEGER DEFAULT 0,
  verified INTEGER DEFAULT 0,
  token TEXT,
  openai_key TEXT,
  llm_settings TEXT,
  checkin_time TEXT DEFAULT '08:00',
  last_checkin_at TEXT,
  last_checkin_result TEXT,
  blocked_windows TEXT DEFAULT '[]',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  deadline TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  reminder_time TEXT DEFAULT '09:00',
  constraints TEXT DEFAULT '[]',
  display_title TEXT NOT NULL,
  goal_msg TEXT,
  chat_history TEXT DEFAULT '[]',
  plan_summary TEXT,
  scheduled_times TEXT DEFAULT '[]',
  plan_status TEXT DEFAULT 'active',
  deadline_missed INTEGER DEFAULT 0,
  manually_succeeded INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  start_time TEXT,
  end_time TEXT,
  duration_min INTEGER DEFAULT 60,
  status TEXT DEFAULT 'pending',
  order_idx INTEGER DEFAULT 0,
  FOREIGN KEY (goal_id) REFERENCES goals (id)
);

CREATE TABLE IF NOT EXISTS blockers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT DEFAULT 'open',
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interventions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  goal_id INTEGER,
  message TEXT NOT NULL,
  type TEXT DEFAULT 'nudge',
  acknowledged INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  kind TEXT,
  message TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS general_chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  snapshot TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS check_ins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  goal_id INTEGER,
  result TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER,
  user_id INTEGER,
  filename TEXT,
  path TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL,
  last_seen_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS email_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  purpose TEXT NOT NULL DEFAULT 'verify',
  expires_at TEXT NOT NULL,
  used INTEGER DEFAULT 0,
  FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

-- Idempotency ledger for scheduled work: one row per (user, goal, kind, local day).
CREATE TABLE IF NOT EXISTS job_runs (
  user_id INTEGER NOT NULL,
  goal_id INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL,
  day TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, goal_id, kind, day)
);

CREATE INDEX IF NOT EXISTS idx_actions_goal ON actions (goal_id, date);
CREATE INDEX IF NOT EXISTS idx_actions_user_date ON actions (user_id, date);
CREATE INDEX IF NOT EXISTS idx_goals_user ON goals (user_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

CREATE TABLE IF NOT EXISTS stakes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  goal_id INTEGER NOT NULL,
  punishment TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  enforced_at TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def _path():
    if _DEFAULT_DIR is not None:
        return os.path.join(_DEFAULT_DIR, "eloise.db")
    return os.environ.get("ELOISE_STORAGE_DIR") and os.path.join(
        os.environ["ELOISE_STORAGE_DIR"], "eloise.db"
    ) or "storage/eloise.db"


def set_storage_dir(d):
    global _DEFAULT_DIR
    _DEFAULT_DIR = d


def get_connection():
    path = _path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = getattr(_thread_local, "conn", None)
    # A cached connection is only valid for the database file it was opened on
    # (the storage dir can change: tests, reset_db).
    if conn is not None and getattr(_thread_local, "path", None) != path:
        try:
            conn.close()
        except Exception:
            pass
        conn = None
    if conn is None:
        conn = sqlite3.connect(path)
        _thread_local.path = path
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # Serialize concurrent writers/readers (plan bg threads, scheduler,
        # HTTP workers all open their own thread-local connection). Without a
        # busy timeout, a writer can get "database is locked" and readers can
        # see transient stale/empty rows. 8s is plenty for local writes.
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        _thread_local.conn = conn
        with _SCHEMA_LOCK:
            conn.executescript(SCHEMA)
            conn.commit()
            _migrate(conn)
    return conn


MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN checkin_time TEXT DEFAULT '08:00'",
    "ALTER TABLE users ADD COLUMN last_checkin_at TEXT",
    "ALTER TABLE users ADD COLUMN last_checkin_result TEXT",
    "ALTER TABLE users ADD COLUMN onboarding_done INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN blocked_windows TEXT DEFAULT '[]'",
    "ALTER TABLE goals ADD COLUMN details TEXT DEFAULT '{}'",
    "ALTER TABLE users ADD COLUMN persona TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN location TEXT DEFAULT 'Pakistan'",
    "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT ''",
    "ALTER TABLE goals ADD COLUMN outline TEXT DEFAULT ''",
]

# One-shot data migrations, keyed by PRAGMA user_version.
#   v1: every pre-existing session token was a permanent bearer secret (and the
#       old repo shipped a database full of them) -> invalidate them all, and
#       promote the oldest real (non-guest) account to admin so a deployment is
#       never left with nobody able to configure the LLM.
def _data_migration_v1(conn):
    conn.execute("UPDATE users SET token=NULL")
    has_admin = conn.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone()
    if not has_admin:
        first = conn.execute(
            "SELECT id FROM users WHERE is_guest=0 ORDER BY id LIMIT 1"
        ).fetchone()
        if first:
            conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (first["id"],))


_DATA_MIGRATIONS = [_data_migration_v1]


def _migrate(conn):
    for stmt in MIGRATIONS:
        column = stmt.split("ADD COLUMN ")[1].split(" ")[0]
        table = stmt.split("ALTER TABLE ")[1].split(" ")[0]
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column in cols:
            continue
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError as exc:
            # A concurrent process may have added it between our check and the
            # ALTER; anything else is a real failure and must be loud.
            if "duplicate column" not in str(exc).lower():
                raise
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, fn in enumerate(_DATA_MIGRATIONS, start=1):
        if version < i:
            fn(conn)
            conn.execute(f"PRAGMA user_version={i}")
            conn.commit()


def reset_db():
    path = _path()
    if os.path.exists(path):
        os.remove(path)
    get_connection()