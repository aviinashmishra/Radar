"""Tables for the distribution, voice-of-customer, regional, sales and reporting layers.

Kept apart from db.SCHEMA so the modules that own them (collectors/channels.py, collectors/voice.py,
collectors/regions.py, collectors/sales.py, reports.py) can evolve without touching the core schema.
`ensure()` is idempotent and runs on every connection open (db._open) and at the top of each owning collector.
"""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_listings (
  channel TEXT NOT NULL, city TEXT NOT NULL DEFAULT '', item_key TEXT NOT NULL, brand TEXT, title TEXT, price REAL, mrp REAL,
  available INTEGER, rating REAL, rating_count INTEGER, url TEXT, query TEXT, sponsored INTEGER DEFAULT 0, position INTEGER,
  category TEXT, first_seen TEXT, last_seen TEXT, seen_runs INTEGER DEFAULT 1, PRIMARY KEY (channel, city, item_key));
CREATE INDEX IF NOT EXISTS ix_chl_brand ON channel_listings(brand, channel);
CREATE TABLE IF NOT EXISTS channel_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, city TEXT, item_key TEXT, price REAL, available INTEGER, rating REAL,
  rating_count INTEGER, seen_at TEXT);
CREATE INDEX IF NOT EXISTS ix_chh ON channel_history(channel, city, item_key, seen_at);
CREATE TABLE IF NOT EXISTS channel_mentions (
  brand TEXT, channel TEXT, event_id INTEGER, title TEXT, url TEXT, seen_at TEXT, PRIMARY KEY (brand, channel, event_id));
CREATE TABLE IF NOT EXISTS voice (
  source TEXT NOT NULL, item_key TEXT NOT NULL, brand TEXT, ref TEXT, ref_title TEXT, author TEXT, rating REAL, text TEXT,
  created TEXT, likes INTEGER DEFAULT 0, sentiment REAL, themes TEXT, url TEXT, seen_at TEXT, PRIMARY KEY (source, item_key));
CREATE INDEX IF NOT EXISTS ix_voice_brand ON voice(brand, created);
CREATE TABLE IF NOT EXISTS region_interest (
  brand TEXT, region TEXT, level TEXT, basis TEXT, period TEXT, value REAL, detail TEXT, seen_at TEXT,
  PRIMARY KEY (brand, region, basis, period));
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, order_ref TEXT, sku TEXT, title TEXT, segment TEXT, channel TEXT,
  state TEXT, city TEXT, units REAL, revenue REAL, source TEXT, imported_at TEXT, UNIQUE (source, order_ref, sku));
CREATE INDEX IF NOT EXISTS ix_sales_day ON sales(day);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, kind TEXT, period TEXT, title TEXT, body TEXT, data TEXT,
  UNIQUE (kind, period));
"""


def ensure(conn=None) -> None:
    from . import db
    with db._lock:
        c = conn or db.connect()
        c.executescript(SCHEMA)
        c.commit()
