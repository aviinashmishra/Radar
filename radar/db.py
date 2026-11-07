"""SQLite store. One file, zero setup. Swap for Postgres later by changing this module only."""
from __future__ import annotations
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import settings

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  brand TEXT NOT NULL, product_id INTEGER NOT NULL, handle TEXT, title TEXT, product_type TEXT,
  vendor TEXT, tags TEXT, price REAL, compare_at REAL, available INTEGER, variants INTEGER,
  image TEXT, published_at TEXT, created_at TEXT, first_seen TEXT, last_seen TEXT, last_changed TEXT,
  PRIMARY KEY (brand, product_id));
CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, product_id INTEGER, price REAL, compare_at REAL,
  available INTEGER, seen_at TEXT);
CREATE INDEX IF NOT EXISTS ix_ph ON price_history(brand, product_id, seen_at);
CREATE TABLE IF NOT EXISTS collections (
  brand TEXT, handle TEXT, title TEXT, products_count INTEGER, first_seen TEXT, last_seen TEXT,
  PRIMARY KEY (brand, handle));
CREATE TABLE IF NOT EXISTS bestseller_rank (
  brand TEXT, product_id INTEGER, rank INTEGER, seen_at TEXT, PRIMARY KEY (brand, product_id));
CREATE TABLE IF NOT EXISTS seen_urls (brand TEXT, source TEXT, url TEXT, seen_at TEXT, PRIMARY KEY (source, url));
CREATE TABLE IF NOT EXISTS page_snapshots (
  url TEXT PRIMARY KEY, brand TEXT, text_hash TEXT, text TEXT, captured_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL, brand TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
  title TEXT NOT NULL, detail TEXT, url TEXT, data TEXT,
  category TEXT, importance INTEGER, so_what TEXT, counter_move TEXT, enriched INTEGER DEFAULT 0,
  alerted INTEGER DEFAULT 0, dedupe_key TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS ix_ev_created ON events(created_at);
CREATE INDEX IF NOT EXISTS ix_ev_brand ON events(brand, created_at);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, kind TEXT, brand TEXT, title TEXT, detail TEXT,
  importance INTEGER, data TEXT, dedupe_key TEXT UNIQUE, alerted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS briefs (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, period TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, collector TEXT, brand TEXT, started_at TEXT, finished_at TEXT,
  ok INTEGER, events INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS app_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, package TEXT, name TEXT, rating REAL, ratings_count INTEGER,
  downloads TEXT, updated_on TEXT, seen_at TEXT);
CREATE INDEX IF NOT EXISTS ix_app ON app_metrics(package, seen_at);
CREATE TABLE IF NOT EXISTS search_suggestions (
  seed TEXT, suggestion TEXT, brand TEXT, first_seen TEXT, last_seen TEXT, PRIMARY KEY (seed, suggestion));
CREATE TABLE IF NOT EXISTS techstack (brand TEXT PRIMARY KEY, shop TEXT, theme TEXT, apps TEXT, seen_at TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  brand TEXT, product_id INTEGER, review_key TEXT, rating INTEGER, title TEXT, body TEXT, created TEXT, seen_at TEXT,
  PRIMARY KEY (brand, product_id, review_key));
CREATE TABLE IF NOT EXISTS review_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, product_id INTEGER, total INTEGER, seen_at TEXT);
CREATE INDEX IF NOT EXISTS ix_rs ON review_stats(brand, product_id, seen_at);
CREATE TABLE IF NOT EXISTS screenshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, url TEXT, path TEXT, diff_pct REAL, taken_at TEXT);
CREATE TABLE IF NOT EXISTS product_matches (
  self_id INTEGER, brand TEXT, product_id INTEGER, score REAL, self_title TEXT, rival_title TEXT, category TEXT,
  self_price REAL, rival_price REAL, computed_at TEXT, PRIMARY KEY (self_id, brand, product_id));
CREATE TABLE IF NOT EXISTS watch_prices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, brand TEXT, price REAL, status TEXT, page_hash TEXT, source TEXT, seen_at TEXT);
CREATE INDEX IF NOT EXISTS ix_wp ON watch_prices(name, seen_at);
CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY, brand TEXT, title TEXT, channel TEXT, views INTEGER, published TEXT, query TEXT, seen_at TEXT);
CREATE TABLE IF NOT EXISTS quotes (
  brand TEXT, ticker TEXT, day TEXT, close REAL, prev_close REAL, change_pct REAL, currency TEXT, high52 REAL, low52 REAL,
  market_cap REAL, name TEXT, seen_at TEXT, PRIMARY KEY (ticker, day));
CREATE TABLE IF NOT EXISTS financials (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, metric TEXT, period TEXT, value REAL, currency TEXT, unit TEXT,
  source TEXT, url TEXT, confidence TEXT, seen_at TEXT, UNIQUE (brand, metric, period, source));
CREATE INDEX IF NOT EXISTS ix_fin ON financials(brand, metric, period);
CREATE TABLE IF NOT EXISTS attention (
  brand TEXT, article TEXT, day TEXT, views INTEGER, PRIMARY KEY (article, day));
CREATE TABLE IF NOT EXISTS trending (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, traffic TEXT, brand TEXT, segment TEXT, url TEXT, seen_at TEXT, UNIQUE (title, seen_at));
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    with _lock:
        return _conn or _open()


def _open() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        c = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        from .schema_ext import SCHEMA as SCHEMA_EXT  # distribution, voice, regions, sales, reports tables
        c.executescript(SCHEMA_EXT)
        for table in ("events", "signals"):  # additive migration: workflow status for the Today view
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if "status" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN status TEXT DEFAULT 'open'")
        c.commit()
        _conn = c
    return _conn


def close() -> None:
    """Fold the WAL back into the main file and close, so radar.db alone is complete (the cloud snapshot is that one file)."""
    global _conn
    if _conn is not None:
        with _lock:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _conn.close()
            _conn = None


@contextmanager
def tx():
    with _lock:  # connect() inside the lock: cloud.install() swaps the database file under this same lock
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def q(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in connect().execute(sql, params).fetchall()]


def q1(sql: str, params=()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def kv_get(k: str, default=None):
    r = q1("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def kv_set(k: str, v) -> None:
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))


def add_event(brand: str, source: str, kind: str, title: str, detail: str = "", url: str = "",
              data: dict | None = None, dedupe_key: str | None = None) -> int | None:
    """Insert an event. Returns new id, or None when the dedupe key already exists."""
    key = dedupe_key or f"{source}|{brand}|{kind}|{title}"[:500]
    with tx() as c:
        try:
            cur = c.execute(
                "INSERT INTO events(created_at,brand,source,kind,title,detail,url,data,dedupe_key) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now(), brand, source, kind, title[:400], (detail or "")[:4000], url or "",
                 json.dumps(data or {}, default=str), key))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def add_signal(kind: str, brand: str, title: str, detail: str, importance: int, data: dict | None,
               dedupe_key: str) -> int | None:
    with tx() as c:
        try:
            cur = c.execute(
                "INSERT INTO signals(created_at,kind,brand,title,detail,importance,data,dedupe_key) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (now(), kind, brand, title[:400], detail[:4000], importance,
                 json.dumps(data or {}, default=str), dedupe_key))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def log_run(collector: str, brand: str, started: str, ok: bool, events: int, note: str = "") -> None:
    with tx() as c:
        c.execute("INSERT INTO runs(collector,brand,started_at,finished_at,ok,events,note) VALUES(?,?,?,?,?,?,?)",
                  (collector, brand, started, now(), int(ok), events, note[:500]))
