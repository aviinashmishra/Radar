"""Push-tier feeds: YouTube channel RSS, Shopify blog Atom feeds, any extra RSS listed in config.

Shopify stores expose /blogs/<handle>.atom and /collections/all.atom for free. YouTube exposes
https://www.youtube.com/feeds/videos.xml?channel_id=<id>. Add either under `feeds:` in competitors.yaml:

feeds:
  - { brand: boAt, url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx" }
"""
from __future__ import annotations
import re
import yaml
import feedparser
from .. import db
from ..config import settings, CONFIG_PATH
from ..relevance import attribute, relevant
from .base import get, tracked, log


def _configured() -> list[dict]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    feeds = list(raw.get("feeds") or [])
    # auto-discover Shopify blog feeds (cheap, 404 is harmless)
    for b in settings.brands:
        if b.shopify and b.platform == "shopify":
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
        first = not db.q1("SELECT 1 FROM seen_urls WHERE source='feed' AND url LIKE ?", (f"%{url.split('//', 1)[-1].split('/', 1)[0]}%",))
        for e in parsed.entries[:25]:
            link = e.get("link", "")
            if not link:
                continue
            title = re.sub(r"<[^>]+>", " ", e.get("title", "")).strip()[:300]
            summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("summary", "") or ""))[:400]
            owner = brand
            if f.get("filter") == "sector":   # an analyst or industry feed: keep only sector items, filed under the brand named
                if not relevant(f"{title} {summary}", None):
                    continue
                owner = attribute(title)
            with db.tx() as c:
                if c.execute("SELECT 1 FROM seen_urls WHERE source='feed' AND url=?", (link,)).fetchone():
                    continue
                c.execute("INSERT INTO seen_urls(brand,source,url,seen_at) VALUES(?,?,?,?)", (owner, "feed", link, db.now()))
            if first and f.get("kind") in ("press", "industry"):
                continue  # a newly added newsroom feed is a baseline, not twenty-five pieces of news
            kind = "video" if "youtube" in url else ("press" if f.get("kind") in ("press", "industry") else "blog")
            data = {"feed": url, "publisher": parsed.feed.get("title", ""), "summary": summary}
            if kind == "press":
                from ..newsintel import topic_of
                data["topic"] = topic_of(title, summary)
            if db.add_event(owner, "feed", kind, title, detail=parsed.feed.get("title", "") + (f" | {data.get('topic')}" if data.get("topic") else ""),
                            url=link, data=data, dedupe_key=f"feed|{link}"):
                n += 1
    return n
