"""YouTube search sensor + influencer map.

Searches "<brand> <query>" sorted by upload date and records each video with channel and views.
New videos become `video` events; the analytics layer turns the table into an influencer leaderboard
(which creators cover which rivals, how many views they pull) so Pebble can reach those creators first.
"""
from __future__ import annotations
import json
import re
from urllib.parse import quote_plus
from .. import db
from ..config import settings
from .base import get, tracked, log

URL = "https://www.youtube.com/results?search_query={q}&sp=CAI%253D"  # CAI= : sort by upload date


def _walk(o):
    if isinstance(o, dict):
        if "videoRenderer" in o:
            yield o["videoRenderer"]
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _views(s: str) -> int:
    m = re.search(r"([\d,.]+)\s*([KML]?)", (s or "").replace("lakh", "L"))
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"K": 1e3, "M": 1e6, "L": 1e5}.get(m.group(2), 1))


def search(q: str) -> list[dict]:
    r = get(URL.format(q=quote_plus(q)), timeout=25, min_gap=2.0,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                     "Accept-Language": "en-IN,en;q=0.9"})
    m = re.search(r"ytInitialData\s*=\s*(\{.*?\})\s*;\s*</script>", r.text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out = []
    for v in _walk(data):
        try:
            out.append({"video_id": v["videoId"], "title": v["title"]["runs"][0]["text"],
                        "channel": v.get("ownerText", {}).get("runs", [{}])[0].get("text", ""),
                        "views": _views(v.get("viewCountText", {}).get("simpleText", "")),
                        "published": v.get("publishedTimeText", {}).get("simpleText", "")})
        except (KeyError, IndexError):
            continue
    return out[:20]


@tracked("youtube")
def run() -> int:
    n = 0
    first = db.q1("SELECT COUNT(*) c FROM videos")["c"] == 0
    for b in settings.brands:
        for q in settings.youtube_queries:
            query = f"{b.name} {q}"
            try:
                vids = search(query)
            except Exception as e:  # noqa: BLE001
                log.info("youtube %s failed: %s", query, e)
                continue
            for v in vids:
                if db.q1("SELECT 1 FROM videos WHERE video_id=?", (v["video_id"],)):
                    continue
                if not re.search(re.escape(b.name.split("-")[0]), v["title"], re.I):
                    continue  # the search engine drifted off-brand
                with db.tx() as c:
                    c.execute("INSERT OR IGNORE INTO videos(video_id,brand,title,channel,views,published,query,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                              (v["video_id"], b.name, v["title"], v["channel"], v["views"], v["published"], query, db.now()))
                if first and "hour" not in v["published"] and "day" not in v["published"]:
                    continue  # on the first run only this week's videos become events; the rest seed the influencer map
                if db.add_event(b.name, "youtube", "video", f"{v['channel']}: {v['title']}"[:300],
                                detail=f"{v['views']:,} views · {v['published']} · q={query}",
                                url=f"https://www.youtube.com/watch?v={v['video_id']}", data=v, dedupe_key=f"yt|{v['video_id']}"):
                    n += 1
    return n
