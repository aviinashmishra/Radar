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
CREATE TABLE IF NOT EXISTS social_posts (
  platform TEXT NOT NULL, post_id TEXT NOT NULL, brand TEXT, kind TEXT, author TEXT, handle TEXT, official INTEGER DEFAULT 0,
  title TEXT, text TEXT, url TEXT, thumb TEXT, created TEXT, likes INTEGER DEFAULT 0, replies INTEGER DEFAULT 0,
  reposts INTEGER DEFAULT 0, views INTEGER DEFAULT 0, sentiment REAL, seen_at TEXT, PRIMARY KEY (platform, post_id));
CREATE INDEX IF NOT EXISTS ix_social_brand ON social_posts(brand, created);
CREATE INDEX IF NOT EXISTS ix_social_created ON social_posts(created);
CREATE TABLE IF NOT EXISTS social_comments (
  platform TEXT NOT NULL, comment_id TEXT NOT NULL, post_id TEXT, parent_id TEXT, depth INTEGER DEFAULT 0, brand TEXT,
  author TEXT, text TEXT, created TEXT, likes INTEGER DEFAULT 0, sentiment REAL, url TEXT, seen_at TEXT,
  PRIMARY KEY (platform, comment_id));
CREATE INDEX IF NOT EXISTS ix_social_comments_post ON social_comments(platform, post_id);
CREATE INDEX IF NOT EXISTS ix_social_comments_brand ON social_comments(brand, created);
CREATE TABLE IF NOT EXISTS social_accounts (
  brand TEXT NOT NULL, platform TEXT NOT NULL, handle TEXT, url TEXT, followers INTEGER, posts_30d INTEGER, last_post TEXT,
  status TEXT, note TEXT, checked TEXT, PRIMARY KEY (brand, platform));
CREATE TABLE IF NOT EXISTS papers (
  source TEXT NOT NULL, paper_id TEXT NOT NULL, brand TEXT, relation TEXT, title TEXT, venue TEXT, published TEXT,
  cited_by INTEGER DEFAULT 0, authors TEXT, url TEXT, abstract TEXT, topic TEXT, seen_at TEXT, PRIMARY KEY (source, paper_id));
CREATE INDEX IF NOT EXISTS ix_papers_brand ON papers(brand, published);
"""


def ensure(conn=None) -> None:
    from . import db
    with db._lock:
        c = conn or db.connect()
        c.executescript(SCHEMA)
        c.commit()
