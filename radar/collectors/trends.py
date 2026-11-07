"""What India is searching for right now: the Google Trends "trending now" RSS for India (free, no key).

Each trending topic is matched against the tracked brands (radar/relevance.py) and the segment keywords. A match
is stored in the `trending` table and becomes a `trending_now` event: a rival trending nationally is either a
launch, an ad burst or a controversy, and either way it is the moment to bid on their name.
"""
from __future__ import annotations
import re
import feedparser
from .. import db
from ..config import settings
from ..relevance import attribute, relevant
from .base import get, tracked, log

RSS = "https://trends.google.com/trending/rss?geo=IN"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _segment(text: str) -> str:
    for sg in settings.segments:
        if re.search(sg.get("keywords") or "$^", text, re.I):
            return sg["key"]
    return ""


def fetch() -> list[dict]:
    r = get(RSS, timeout=45, min_gap=1.0, headers=UA)
    if r.status_code != 200:
        return []
    feed = feedparser.parse(r.text)
    out = []
    for e in feed.entries:
        title = e.get("title", "")
        traffic = e.get("ht_approx_traffic") or e.get("approx_traffic") or ""
        news = []
        for k in ("ht_news_item_title", "news_item_title"):
            if e.get(k):
                news.append(e[k])
        link = e.get("ht_news_item_url") or e.get("news_item_url") or e.get("link") or ""
        out.append({"title": title, "traffic": traffic, "news": " ".join(news), "url": link})
    return out


@tracked("trends")
def run() -> int:
    n = 0
    try:
        items = fetch()
    except Exception as e:  # noqa: BLE001
        log.info("google trends failed: %s", e)
        return 0
    ts = db.now()
    db.kv_set("trends_checked", {"at": ts, "n": len(items), "sample": [i["title"] for i in items[:10]]})
    for it in items:
        text = f"{it['title']} {it['news']}"
        brand = attribute(text, strict=True)
        seg = _segment(text)
        if brand == "Sector" and not (seg and relevant(text, None)):
            continue
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO trending(title,traffic,brand,segment,url,seen_at) VALUES(?,?,?,?,?,?)",
                      (it["title"], it["traffic"], brand, seg, it["url"], ts))
        if db.add_event(brand, "trends", "trending_now", f"Trending in India: \"{it['title']}\"" + (f" ({it['traffic']} searches)" if it["traffic"] else ""),
                        detail=(it["news"][:300] or "Google Trends, India, right now."), url=it["url"] or f"https://trends.google.com/trending?geo=IN",
                        data={"traffic": it["traffic"], "segment": seg}, dedupe_key=f"trend|{it['title'].lower()}|{ts[:10]}"):
            n += 1
    return n
