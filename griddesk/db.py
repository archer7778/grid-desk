"""SQLite storage. WAL mode lets the web server read while the fetch job writes."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS regional (
  region TEXT NOT NULL, t_ms INTEGER NOT NULL,
  demand REAL, price REAL, avail_gen REAL, net_interchange REAL,
  PRIMARY KEY (region, t_ms)
);
CREATE TABLE IF NOT EXISTS rooftop (
  region TEXT NOT NULL, t_ms INTEGER NOT NULL, mw REAL NOT NULL,
  PRIMARY KEY (region, t_ms)
);
CREATE TABLE IF NOT EXISTS mix (
  region TEXT NOT NULL, t_ms INTEGER NOT NULL, fuel TEXT NOT NULL, mw REAL NOT NULL,
  PRIMARY KEY (region, t_ms, fuel)
);
CREATE TABLE IF NOT EXISTS units (
  duid TEXT NOT NULL, t_ms INTEGER NOT NULL, mw REAL NOT NULL,
  PRIMARY KEY (duid, t_ms)
);
CREATE INDEX IF NOT EXISTS units_t ON units (t_ms);
CREATE TABLE IF NOT EXISTS duids (
  duid TEXT PRIMARY KEY, region TEXT, fuel TEXT, station TEXT, capacity_mw REAL
);
CREATE TABLE IF NOT EXISTS interconnectors (
  ic TEXT NOT NULL, t_ms INTEGER NOT NULL, flow REAL, export_limit REAL, import_limit REAL,
  PRIMARY KEY (ic, t_ms)
);
CREATE TABLE IF NOT EXISTS notices (
  id INTEGER PRIMARY KEY, t_ms INTEGER, type TEXT, region TEXT, lor INTEGER, kind TEXT,
  reference TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS notices_t ON notices (t_ms);
CREATE TABLE IF NOT EXISTS weather (
  region TEXT NOT NULL, t_ms INTEGER NOT NULL, temp REAL NOT NULL,
  PRIMARY KEY (region, t_ms)
);
CREATE TABLE IF NOT EXISTS files (name TEXT PRIMARY KEY, fetched_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fetches (
  id INTEGER PRIMARY KEY, t_ms INTEGER NOT NULL, ok INTEGER NOT NULL, message TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_ms():
    return int(time.time() * 1000)


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA journal_size_limit=4194304")
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM fetches WHERE id NOT IN (SELECT id FROM fetches ORDER BY id DESC LIMIT 500)")
    conn.commit()
    return conn


def connect_ro(path):
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def open_db(cfg):
    return connect(cfg.db_path)


def meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def record_fetch(conn, ok, message):
    conn.execute("INSERT INTO fetches(t_ms,ok,message) VALUES(?,?,?)", (now_ms(), 1 if ok else 0, str(message)[:500]))
    conn.commit()
