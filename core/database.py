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
    if conn is None:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _thread_local.conn = conn
        with _SCHEMA_LOCK:
            conn.executescript(SCHEMA)
            conn.commit()
            _migrate(conn)
    return conn


MIGRATIONS = [
    # add per-user fields for the 24h check-in
    "ALTER TABLE users ADD COLUMN checkin_time TEXT DEFAULT '08:00'",
    "ALTER TABLE users ADD COLUMN last_checkin_at TEXT",
    "ALTER TABLE users ADD COLUMN last_checkin_result TEXT",
]


def _migrate(conn):
    for stmt in MIGRATIONS:
        column = stmt.split("ADD COLUMN ")[1].split(" ")[0]
        table = stmt.split("ALTER TABLE ")[1].split(" ")[0]
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            try:
                conn.execute(stmt)
                conn.commit()
            except Exception:
                pass


def reset_db():
    path = _path()
    if os.path.exists(path):
        os.remove(path)
    get_connection()