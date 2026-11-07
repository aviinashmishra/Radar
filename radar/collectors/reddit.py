"""Reddit buzz and complaints via public RSS (no API key). Reddit rate-limits aggressively, so we go slow."""
from __future__ import annotations
import re
import time
from urllib.parse import quote_plus
import feedparser
from .. import db
from ..config import settings
from ..relevance import attribute, relevant
from .base import get, tracked, log

SEARCH = "https://www.reddit.com/search.rss?q={q}&sort=new&t=month"
SUB = "https://www.reddit.com/r/{sub}/new.rss"


def _entries(url: str) -> list:
    try:
        r = get(url, timeout=20, min_gap=4.0, headers={"User-Agent": "PebbleRadar/1.0 (competitive research; contact via repo)"})
    except Exception as e:
        log.warning("reddit fetch failed: %s", e)
        return []
    if r.status_code != 200:
        log.info("reddit %s -> %s (rate limited?)", url, r.status_code)
        return []
    return feedparser.parse(r.text).entries


def _brand_for(text: str) -> str:
    return attribute(text)


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def _ingest(entries, default_brand: str, tag: str) -> int:
    n = 0
    for e in entries:
        link = e.get("link", "")
        if not link:
            continue
        with db.tx() as c:
            if c.execute("SELECT 1 FROM seen_urls WHERE source='reddit' AND url=?", (link,)).fetchone():
                continue
            c.execute("INSERT INTO seen_urls(brand,source,url,seen_at) VALUES(?,?,?,?)", (default_brand, "reddit", link, db.now()))
        title = e.get("title", "")
        text = f"{title} {_plain(e.get('summary', ''))[:600]}"
        if default_brand == "Sector":
            brand = _brand_for(text)
        else:  # a brand search: the post itself must be about the brand, not a deals roundup that matched one word
            b = settings.brand(default_brand)
            brand = default_brand if b and relevant(text, b) else ("Sector" if relevant(text, None) else "")
        if not brand or (brand == "Sector" and tag.startswith("r/")):
            continue  # subreddit firehose: only keep posts that name a tracked brand
        if db.add_event(brand, "reddit", "social", f"r/ {title}", detail=f"{tag} | {e.get('author', '')}",
                        url=link, data={"tag": tag}, dedupe_key=f"reddit|{link}"):
            n += 1
    return n


@tracked("reddit")
def run() -> int:
    n = 0
    cfg = settings.reddit or {}
    for q in cfg.get("searches", []):
        n += _ingest(_entries(SEARCH.format(q=quote_plus(q))), _brand_for(q), f"search:{q}")
        time.sleep(1)
    for sub in cfg.get("subreddits", []):
        n += _ingest(_entries(SUB.format(sub=sub)), "Sector", f"r/{sub}")
        time.sleep(1)
    return n
