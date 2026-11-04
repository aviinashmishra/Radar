"""Push-tier feeds: YouTube channel RSS, Shopify blog Atom feeds, any extra RSS listed in config.

Shopify stores expose /blogs/<handle>.atom and /collections/all.atom for free. YouTube exposes
https://www.youtube.com/feeds/videos.xml?channel_id=<id>. Add either under `feeds:` in competitors.yaml:

feeds:
  - { brand: boAt, url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx" }
"""
from __future__ import annotations
import yaml
import feedparser
from .. import db
from ..config import settings, CONFIG_PATH
from .base import get, tracked, log


def _configured() -> list[dict]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    feeds = list(raw.get("feeds") or [])
    # auto-discover Shopify blog feeds (cheap, 404 is harmless)
    for b in settings.brands:
        if b.shopify:
            feeds.append({"brand": b.name, "url": f"{b.shopify}/blogs/news.atom", "auto": True})
            feeds.append({"brand": b.name, "url": f"{b.shopify}/collections/all.atom", "auto": True, "kind": "catalogue"})
    return feeds


@tracked("feeds")
def run() -> int:
    n = 0
    for f in _configured():
        url, brand = f["url"], f.get("brand", "Sector")
        try:
            r = get(url, timeout=20)
        except Exception as e:
            log.info("feed %s failed: %s", url, e)
            continue
        if r.status_code != 200 or "<" not in r.text[:50]:
            continue
        parsed = feedparser.parse(r.text)
        if f.get("kind") == "catalogue":
            continue  # the catalogue atom duplicates products.json - only used as a liveness check
        for e in parsed.entries[:25]:
            link = e.get("link", "")
            if not link:
                continue
            with db.tx() as c:
                if c.execute("SELECT 1 FROM seen_urls WHERE source='feed' AND url=?", (link,)).fetchone():
                    continue
                c.execute("INSERT INTO seen_urls(brand,source,url,seen_at) VALUES(?,?,?,?)", (brand, "feed", link, db.now()))
            kind = "video" if "youtube" in url else "blog"
            if db.add_event(brand, "feed", kind, e.get("title", "")[:300], detail=parsed.feed.get("title", ""),
                            url=link, data={"feed": url}, dedupe_key=f"feed|{link}"):
                n += 1
    return n
