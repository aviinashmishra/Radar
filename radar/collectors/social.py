"""Social, community and live-web sensor, for every company in every sector.

Where each platform comes from (no login anywhere; the two Meta platforms use the Graph API when a token is set):

  handles     each company's own accounts, read off the links on its website (kv socials:<brand>, weekly);
              companies.yaml `socials:` overrides them
  X           the public embed timeline of the company's handle: posts, likes, replies, reposts, followers
  Bluesky     public search for posts naming the company, plus whole reply threads of the busiest ones
  Hacker News stories about the company and every comment and reply under them; comments that mention it elsewhere
  Reddit      posts the reddit collector found, with their comment trees (reply nesting kept)
  Lemmy       federated Reddit-style posts and comment trees
  Mastodon    the company's hashtag on mastodon.social
  YouTube     Shorts about the company (views, thumbnails) and the comments under the biggest ones
  WhatsApp    the company's WhatsApp channel: follower count and description
  Substack    the company's own newsletter: posts, reactions, comment threads
  Telegram    the company's public channel preview (where the network allows it)
  Web (live)  GDELT: articles from news sites worldwide in the last hours, live blogs flagged
  Product Hunt launches that name a tracked company
  Instagram   followers, posts and Reels through business_discovery (INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_BUSINESS_ID)
  Facebook    page followers and posts (FACEBOOK_ACCESS_TOKEN, or META_ACCESS_TOKEN with page read access)

Posts land in social_posts, comments and replies in social_comments (parent_id + depth keep the thread shape),
accounts in social_accounts. Notable items (a post going viral, a hot thread, a live blog) also become events, so they
reach Today, alerts and the Nazar hub like every other move.
"""
from __future__ import annotations
import html as _html
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from .. import db
from ..config import settings
from ..relevance import relevant
from .base import get, tracked, log, sha

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "en-IN,en;q=0.9"}
STEP_SECONDS = 300          # no single platform may hold a sector's sweep longer than this
SLOW_EVERY_H = 6            # Shorts, WhatsApp, Substack, Telegram, Meta: a few times a day is plenty

PLATFORM_LABEL = {"x": "X", "bluesky": "Bluesky", "hn": "Hacker News", "reddit": "Reddit", "lemmy": "Lemmy", "mastodon": "Mastodon",
                  "youtube": "YouTube Shorts", "whatsapp": "WhatsApp", "substack": "Substack", "telegram": "Telegram",
                  "web": "Live web", "producthunt": "Product Hunt", "instagram": "Instagram", "facebook": "Facebook",
                  "linkedin": "LinkedIn", "threads": "Threads"}


# ---------------------------------------------------------------- helpers
def _iso(dt: datetime | None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _since(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _plain(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", _html.unescape(s)).strip()


def _num(s) -> int:
    """'1.2M views', '233M followers', '12,345' -> int."""
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        return int(s)
    m = re.search(r"([\d.,]+)\s*([KMB]|lakh|crore|thousand|million|billion)?", str(s), re.I)
    if not m:
        return 0
    v = float(m.group(1).replace(",", "") or 0)
    mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9, "lakh": 1e5, "crore": 1e7}.get((m.group(2) or "").lower(), 1)
    return int(v * mult)


def _about(text: str, b, *, always_context: bool = False) -> bool:
    """The text is about company b: named (relevance rules), and for everyday-word names (Affirm, Hero, Jupiter) or short
    video titles also on-sector once the company's own name is taken out (the sector vocabulary includes the names)."""
    if not relevant(text, b):
        return False
    if not (always_context or b.ambiguous):
        return True
    rest = re.sub(rf"(?i)#?\b{re.escape(b.name)}\b", " ", text)
    for a in b.aliases:
        rest = re.sub(rf"(?i)#?\b{re.escape(a)}\b", " ", rest)
    return relevant(rest, None)


def _sent(text: str) -> float:
    from .voice import sentiment
    return sentiment(text or "", None)


def _jget(url: str, *, params=None, headers=None, min_gap=1.0, timeout=25):
    try:
        r = get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout, min_gap=min_gap)
    except Exception as e:  # noqa: BLE001
        log.info("social %s failed: %s", url.split("?")[0], e)
        return None
    if r.status_code != 200:
        log.info("social %s -> %s", url.split("?")[0], r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        return None


SEEDED: dict[str, bool] = {}   # platform step -> read before this run; events wait until then (the first sweep is backfill)


def _evt(step: str, *a, **kw) -> None:
    if SEEDED.get(step):
        db.add_event(*a, **kw)


def _due(name: str, hours: float) -> bool:
    last = db.kv_get(f"social:step:{name}")
    return not last or last < _iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _done(name: str) -> None:
    db.kv_set(f"social:step:{name}", _iso(None))


def post(platform: str, post_id: str, brand: str, kind: str, *, author: str = "", handle: str = "", official: bool = False,
         title: str = "", text: str = "", url: str = "", thumb: str = "", created: str | None = None, likes: int = 0,
         replies: int = 0, reposts: int = 0, views: int = 0) -> bool:
    """Upsert a post; engagement numbers refresh on every sighting. True when the post is new."""
    text = (text or "").strip()
    with db.tx() as c:
        new = c.execute("SELECT 1 FROM social_posts WHERE platform=? AND post_id=?", (platform, post_id)).fetchone() is None
        c.execute("INSERT INTO social_posts(platform,post_id,brand,kind,author,handle,official,title,text,url,thumb,created,likes,replies,"
                  "reposts,views,sentiment,seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform,post_id) DO UPDATE SET "
                  "likes=excluded.likes, replies=excluded.replies, reposts=excluded.reposts, views=MAX(views, excluded.views), seen_at=excluded.seen_at",
                  (platform, str(post_id)[:300], brand, kind, (author or "")[:120], (handle or "")[:120], int(bool(official)), (title or "")[:300],
                   text[:2000], url or "", thumb or "", created or _iso(None), int(likes or 0), int(replies or 0), int(reposts or 0),
                   int(views or 0), _sent(f"{title} {text}"), db.now()))
    return new


def comment(platform: str, comment_id: str, post_id: str, parent_id: str, depth: int, brand: str, author: str, text: str,
            created: str | None, likes: int = 0, url: str = "") -> bool:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return False
    with db.tx() as c:
        return bool(c.execute("INSERT OR IGNORE INTO social_comments(platform,comment_id,post_id,parent_id,depth,brand,author,text,created,"
                              "likes,sentiment,url,seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (platform, str(comment_id)[:300], str(post_id)[:300], str(parent_id or post_id)[:300], int(depth), brand,
                               (author or "")[:120], text[:2000], created or _iso(None), int(likes or 0), _sent(text), url or "", db.now())).rowcount)


def account(brand: str, platform: str, handle: str, url: str, *, followers: int | None = None, posts_30d: int | None = None,
            last_post: str | None = None, status: str = "found", note: str = "") -> None:
    with db.tx() as c:
        prev = c.execute("SELECT followers FROM social_accounts WHERE brand=? AND platform=?", (brand, platform)).fetchone()
        c.execute("INSERT INTO social_accounts(brand,platform,handle,url,followers,posts_30d,last_post,status,note,checked) VALUES(?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(brand,platform) DO UPDATE SET handle=excluded.handle, url=excluded.url, "
                  "followers=COALESCE(excluded.followers, followers), posts_30d=COALESCE(excluded.posts_30d, posts_30d), "
                  "last_post=COALESCE(excluded.last_post, last_post), status=excluded.status, note=excluded.note, checked=excluded.checked",
                  (brand, platform, handle, url, followers, posts_30d, last_post, status, note[:300], db.now()))
    old = prev["followers"] if prev else None
    if followers and old and old >= 1000 and followers >= old * 1.10:   # a 10% jump in a week is a campaign or a moment
        db.add_event(brand, "social", "community_growth", f"{brand} {PLATFORM_LABEL.get(platform, platform)} audience up {round((followers / old - 1) * 100)}% "
                     f"to {followers:,}", url=url, data={"platform": platform, "from": old, "to": followers},
                     dedupe_key=f"growth|{platform}|{brand}|{followers // max(1, old // 10)}")


def _thread_due(platform: str, post_id: str, hours: float = 12) -> bool:
    k = f"social:thread:{platform}:{sha(str(post_id))}"
    last = db.kv_get(k)
    if last and last > _iso(datetime.now(timezone.utc) - timedelta(hours=hours)):
        return False
    db.kv_set(k, _iso(None))
    return True


# ---------------------------------------------------------------- handles: read off each company's website
LINKS = {
    "x": r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?!intent|share|home|search|hashtag|i/|privacy|tos|explore)@?([A-Za-z0-9_]{2,15})(?![A-Za-z0-9_])",
    "instagram": r"https?://(?:www\.)?instagram\.com/(?!p/|reel/|reels/|explore|accounts|stories|direct)([A-Za-z0-9_.]{2,30})",
    "facebook": r"https?://(?:www\.|m\.|web\.|en-gb\.)?(?:facebook|fb)\.com/(?!sharer|share|dialog|plugins|tr[/?]|groups/|events/|profile\.php|watch|login|help|policies)([A-Za-z0-9.\-]{2,60})",
    "youtube": r"https?://(?:www\.)?youtube\.com/((?:@|c/|channel/|user/)[A-Za-z0-9_.\-]{2,60})",
    "linkedin": r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company/[A-Za-z0-9\-_%.]{2,80}|showcase/[A-Za-z0-9\-_%.]{2,80})",
    "whatsapp": r"https?://(?:www\.)?whatsapp\.com/channel/([A-Za-z0-9]{10,40})",
    "substack": r"https?://([a-z0-9\-]{2,60})\.substack\.com",
    "telegram": r"https?://(?:t\.me|telegram\.me)/(?!share|joinchat|\+|s/)([A-Za-z0-9_]{4,40})",
    "threads": r"https?://(?:www\.)?threads\.(?:net|com)/@([A-Za-z0-9_.]{2,30})",
    "bluesky": r"https?://bsky\.app/profile/([A-Za-z0-9.\-]{3,80})",
}
SKIP = {"x": {"twitter", "x", "share", "intent", "home", "search"}, "instagram": {"instagram"}, "facebook": {"facebook", "sharer.php", "pages"},
        "youtube": set(), "telegram": {"share"}, "substack": {"www", "substack", "on"}}


def _discover(site: str) -> dict:
    try:
        r = get(site, headers=UA, timeout=25, min_gap=1.0)
    except Exception as e:  # noqa: BLE001
        log.info("social handles: %s unreachable: %s", site, e)
        return {}
    if r.status_code != 200:
        return {}
    body = r.text.replace("\\/", "/")
    out = {}
    for platform, rx in LINKS.items():
        found = Counter(m.rstrip("/.").split("?")[0] for m in re.findall(rx, body, re.I))
        for h, _ in found.most_common():
            if h.lower() not in SKIP.get(platform, set()):
                out[platform] = h
                break
    return out


def handles(b) -> dict:
    """{platform: handle} for one company: companies.yaml `socials:` first, else read off its website (cached a week)."""
    override = (settings.company(b.name) or {}).get("socials") or {}
    cached = db.kv_get(f"socials:{b.name}") or {}
    if not cached.get("checked") or cached["checked"] < _iso(datetime.now(timezone.utc) - timedelta(days=7)):
        site = (b.website or b.shopify or "").rstrip("/")
        found = _discover(site + "/") if site else {}
        cached = {**{k: v for k, v in cached.items() if k != "checked"}, **found, "checked": _iso(None)}
        db.kv_set(f"socials:{b.name}", cached)
    return {**{k: v for k, v in cached.items() if k != "checked"}, **override}


def profile_url(platform: str, h: str) -> str:
    return {"x": f"https://x.com/{h}", "instagram": f"https://www.instagram.com/{h}/", "facebook": f"https://www.facebook.com/{h}",
            "youtube": f"https://www.youtube.com/{h}", "linkedin": f"https://www.linkedin.com/{h}", "whatsapp": f"https://www.whatsapp.com/channel/{h}",
            "substack": f"https://{h}.substack.com", "telegram": f"https://t.me/{h}", "threads": f"https://www.threads.net/@{h}",
            "bluesky": f"https://bsky.app/profile/{h}"}.get(platform, "")


# ---------------------------------------------------------------- X (public embed timeline)
X_PER_RUN = 5   # the embed timeline allows a few dozen reads per window across the whole process: rotate through handles


def run_x(socials: dict) -> int:
    n, t0 = 0, time.time()
    until = float(db.kv_get("social:x_backoff_until") or 0)
    if time.time() < until:
        return 0
    todo = [b for b in settings.brands if socials.get(b.name, {}).get("x")]
    for b in todo:   # handles never read yet are listed as found, so the Accounts tab shows them from the first sweep
        if not db.q1("SELECT 1 FROM social_accounts WHERE brand=? AND platform='x'", (b.name,)):
            h = socials[b.name]["x"]
            account(b.name, "x", h, profile_url("x", h), status="found", note="queued: X is read a few handles per sweep")
    off = int(db.kv_get("social:x_offset") or 0) % max(1, len(todo))
    pick = (todo[off:] + todo[:off])[:X_PER_RUN]
    db.kv_set("social:x_offset", off + X_PER_RUN)
    for b in pick:
        h = socials[b.name]["x"]
        if time.time() - t0 > STEP_SECONDS:
            break
        try:
            r = get(f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{h}", headers=UA, timeout=25, min_gap=8.0)
        except Exception as e:  # noqa: BLE001
            account(b.name, "x", h, profile_url("x", h), status="unreachable", note=str(e)[:120])
            continue
        if r.status_code == 429:   # wait for X's own reset time (or 15 minutes), for every sector
            reset = float(r.headers.get("x-rate-limit-reset") or 0) or time.time() + 900
            db.kv_set("social:x_backoff_until", max(reset, time.time() + 300))
            log.info("X embed timeline rate-limited until %s", datetime.fromtimestamp(reset, timezone.utc).isoformat())
            break
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S) if r.status_code == 200 else None
        if not m:
            account(b.name, "x", h, profile_url("x", h), status="unreachable", note=f"embed timeline answered {r.status_code}")
            continue
        try:
            entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
        except (ValueError, KeyError, TypeError):
            entries = []
        followers, recent, last = None, 0, None
        for e in entries:
            tw = (e.get("content") or {}).get("tweet") if isinstance(e, dict) else None
            if not tw:
                continue
            try:
                when = datetime.strptime(tw["created_at"], "%a %b %d %H:%M:%S %z %Y")
            except (KeyError, ValueError):
                continue
            user = tw.get("user") or {}
            own = (user.get("screen_name") or "").lower() == h.lower()
            if own and user.get("followers_count") is not None:
                followers = user["followers_count"]
            if when < _since(180):
                continue
            recent += when >= _since(30)
            last = max(last or "", _iso(when))
            text = _html.unescape(tw.get("full_text") or tw.get("text") or "")
            url = "https://x.com" + (tw.get("permalink") or f"/{h}/status/{tw.get('id_str')}")
            likes, replies, reposts = tw.get("favorite_count") or 0, tw.get("reply_count") or 0, (tw.get("retweet_count") or 0) + (tw.get("quote_count") or 0)
            new = post("x", tw.get("id_str") or url, b.name, "post", author=user.get("name") or h, handle=user.get("screen_name") or h,
                       official=own, text=text, url=url, created=_iso(when), likes=likes, replies=replies, reposts=reposts)
            n += new
            if new and own and when >= _since(3) and (likes >= 2000 or replies >= 200):
                _evt("x", b.name, "x", "viral_post", f"{b.name} on X: {text[:140]}", detail=f"{likes:,} likes · {replies:,} replies · {reposts:,} reposts",
                             url=url, data={"platform": "x", "likes": likes, "replies": replies}, dedupe_key=f"x|{tw.get('id_str')}")
        account(b.name, "x", h, profile_url("x", h), followers=followers, posts_30d=recent, last_post=last, status="live")
    return n


# ---------------------------------------------------------------- Bluesky (public AppView)
BSKY = "https://api.bsky.app/xrpc/"


def _bsky_url(p: dict) -> str:
    return f"https://bsky.app/profile/{p['author']['handle']}/post/{p['uri'].rsplit('/', 1)[-1]}"


def _bsky_walk(node: dict, root: str, brand: str, depth: int) -> int:
    n = 0
    for rep in (node.get("replies") or [])[:60]:
        p = rep.get("post") if isinstance(rep, dict) else None
        if not p:
            continue
        rec = p.get("record") or {}
        n += comment("bluesky", p["uri"], root, (rec.get("reply") or {}).get("parent", {}).get("uri") or root, depth, brand,
                     p["author"].get("displayName") or p["author"]["handle"], rec.get("text", ""), rec.get("createdAt"), p.get("likeCount") or 0, _bsky_url(p))
        n += _bsky_walk(rep, root, brand, depth + 1)
    return n


def run_bluesky() -> int:
    n, t0, threads = 0, time.time(), []
    since = _since(14).strftime("%Y-%m-%dT%H:%M:%SZ")
    for b in settings.brands:
        if time.time() - t0 > STEP_SECONDS:
            break
        d = _jget(BSKY + "app.bsky.feed.searchPosts", params={"q": f'"{b.name}"', "sort": "latest", "limit": 30, "since": since}, min_gap=0.7)
        for p in (d or {}).get("posts", []):
            rec = p.get("record") or {}
            text = rec.get("text", "")
            if not _about(text, b):
                continue
            n += post("bluesky", p["uri"], b.name, "post", author=p["author"].get("displayName") or p["author"]["handle"],
                      handle=p["author"]["handle"], text=text, url=_bsky_url(p), created=rec.get("createdAt"), likes=p.get("likeCount") or 0,
                      replies=p.get("replyCount") or 0, reposts=(p.get("repostCount") or 0) + (p.get("quoteCount") or 0))
            if (p.get("replyCount") or 0) > 0:
                threads.append((p.get("replyCount") or 0, p, b.name))
    for _, p, brand in sorted(threads, key=lambda x: -x[0])[:6]:
        if time.time() - t0 > STEP_SECONDS or not _thread_due("bluesky", p["uri"]):
            continue
        d = _jget(BSKY + "app.bsky.feed.getPostThread", params={"uri": p["uri"], "depth": 8}, min_gap=0.7)
        if d and d.get("thread"):
            n += _bsky_walk(d["thread"], p["uri"], brand, 0)
    return n


# ---------------------------------------------------------------- Hacker News (Algolia)
HN = "https://hn.algolia.com/api/v1/"


def _hn_walk(node: dict, story: str, brand: str, depth: int) -> int:
    n = 0
    for ch in node.get("children") or []:
        if ch.get("type") == "comment" and ch.get("text"):
            n += comment("hn", ch["id"], story, ch.get("parent_id") or story, depth, brand, ch.get("author") or "", _plain(ch["text"]),
                         ch.get("created_at"), 0, f"https://news.ycombinator.com/item?id={ch['id']}")
        n += _hn_walk(ch, story, brand, depth + 1)
    return n


def run_hn() -> int:
    n, t0, stories = 0, time.time(), []
    ts = int(_since(30).timestamp())
    for b in settings.brands:
        if time.time() - t0 > STEP_SECONDS:
            break
        host = urlparse(b.website or b.shopify or "").netloc.replace("www.", "")
        d = _jget(HN + "search_by_date", params={"query": b.name, "tags": "story", "numericFilters": f"created_at_i>{ts}", "hitsPerPage": 20}, min_gap=0.4)
        for h in (d or {}).get("hits", []):
            title, link = h.get("title") or "", h.get("url") or ""
            if not (_about(title, b) or (host and host in link)):
                continue
            sid = h["objectID"]
            new = post("hn", sid, b.name, "thread", author=h.get("author") or "", title=title, text=link,
                       url=f"https://news.ycombinator.com/item?id={sid}", created=h.get("created_at"), likes=h.get("points") or 0,
                       replies=h.get("num_comments") or 0)
            n += new
            if (h.get("num_comments") or 0) >= 3:
                stories.append((h.get("num_comments") or 0, sid, b.name))
            if new and ((h.get("points") or 0) >= 50 or (h.get("num_comments") or 0) >= 30):
                _evt("hn", b.name, "hn", "discussion", f"Hacker News: {title}", detail=f"{h.get('points') or 0} points · {h.get('num_comments') or 0} comments",
                             url=f"https://news.ycombinator.com/item?id={sid}", data={"platform": "hn"}, dedupe_key=f"hn|{sid}")
        # the company named inside other discussions
        d = _jget(HN + "search_by_date", params={"query": b.name, "tags": "comment", "numericFilters": f"created_at_i>{int(_since(14).timestamp())}",
                                                 "hitsPerPage": 15}, min_gap=0.4)
        for h in (d or {}).get("hits", []):
            text = _plain(h.get("comment_text") or "")
            if not _about(text, b):
                continue
            sid = str(h.get("story_id") or "")
            if not sid:
                continue
            post("hn", sid, b.name, "mention", author="", title=h.get("story_title") or "", text=h.get("story_url") or "",
                 url=f"https://news.ycombinator.com/item?id={sid}", created=h.get("created_at"))
            n += comment("hn", h["objectID"], sid, str(h.get("parent_id") or sid), 1, b.name, h.get("author") or "", text, h.get("created_at"),
                         0, f"https://news.ycombinator.com/item?id={h['objectID']}")
    for _, sid, brand in sorted(stories, key=lambda x: -x[0])[:4]:
        if time.time() - t0 > STEP_SECONDS or not _thread_due("hn", sid):
            continue
        d = _jget(HN + f"items/{sid}", min_gap=0.4, timeout=40)
        if d:
            n += _hn_walk(d, sid, brand, 0)
    return n


# ---------------------------------------------------------------- Reddit (threads of the posts the reddit collector found)
def _reddit_walk(listing, post_id: str, brand: str, depth: int) -> int:
    n = 0
    for ch in ((listing or {}).get("data") or {}).get("children") or []:
        if ch.get("kind") != "t1":
            continue
        c = ch.get("data") or {}
        parent = (c.get("parent_id") or "").split("_", 1)[-1]
        n += comment("reddit", c.get("id"), post_id, parent if parent != post_id else post_id, depth, brand, c.get("author") or "",
                     c.get("body") or "", _iso(datetime.fromtimestamp(c.get("created_utc") or time.time(), timezone.utc)), c.get("score") or 0,
                     "https://www.reddit.com" + (c.get("permalink") or ""))
        if isinstance(c.get("replies"), dict):
            n += _reddit_walk(c["replies"], post_id, brand, depth + 1)
    return n


def run_reddit() -> int:
    n, t0 = 0, time.time()
    rows = db.q("SELECT brand, title, url, created_at FROM events WHERE source='reddit' AND brand NOT IN ('Sector','NewEntrant') "
                "AND created_at>=datetime('now','-30 day') ORDER BY id DESC LIMIT 200")
    for r in rows:   # every reddit post about a company is a social post too
        m = re.search(r"/comments/([a-z0-9]+)", r["url"] or "")
        if m:
            post("reddit", m.group(1), r["brand"], "thread", title=re.sub(r"^r/\s*", "", r["title"] or ""), url=r["url"], created=r["created_at"])
    for r in rows[:5]:   # comment trees for the newest few (reddit rate-limits anonymous reads hard)
        m = re.search(r"/comments/([a-z0-9]+)", r["url"] or "")
        if not m or time.time() - t0 > STEP_SECONDS or not _thread_due("reddit", m.group(1), hours=24):
            continue
        d = _jget(r["url"].rstrip("/") + ".json", params={"limit": 100, "depth": 6, "raw_json": 1},
                  headers={"User-Agent": "NazarRadar/1.0 (competitive research)"}, min_gap=6.0)
        if not isinstance(d, list) or len(d) < 2:
            continue
        p = ((d[0].get("data") or {}).get("children") or [{}])[0].get("data") or {}
        post("reddit", m.group(1), r["brand"], "thread", author=p.get("author") or "", title=p.get("title") or r["title"], text=p.get("selftext") or "",
             url=r["url"], created=r["created_at"], likes=p.get("score") or 0, replies=p.get("num_comments") or 0)
        n += _reddit_walk(d[1], m.group(1), r["brand"], 0)
    return n


# ---------------------------------------------------------------- Lemmy (federated, Reddit-style)
LEMMY = "https://lemmy.world/api/v3/"


def run_lemmy() -> int:
    n, t0 = 0, time.time()
    for b in [x for x in settings.brands if not x.ambiguous][:20]:
        if time.time() - t0 > STEP_SECONDS:
            break
        d = _jget(LEMMY + "search", params={"q": b.name, "type_": "Posts", "sort": "New", "limit": 10}, min_gap=1.0)
        for item in (d or {}).get("posts", []):
            p, cnt = item.get("post") or {}, item.get("counts") or {}
            if not _about(f"{p.get('name', '')} {p.get('body', '')}", b):
                continue
            pid = str(p.get("id"))
            n += post("lemmy", pid, b.name, "thread", author=(item.get("creator") or {}).get("name") or "", title=p.get("name") or "",
                      text=(p.get("body") or "")[:1500], url=p.get("ap_id") or "", created=p.get("published"), likes=cnt.get("score") or 0,
                      replies=cnt.get("comments") or 0)
            if (cnt.get("comments") or 0) and _thread_due("lemmy", pid):
                cd = _jget(LEMMY + "comment/list", params={"post_id": pid, "max_depth": 8, "sort": "Top", "limit": 50, "type_": "All"}, min_gap=1.0)
                for ci in (cd or {}).get("comments", []):
                    c = ci.get("comment") or {}
                    path = (c.get("path") or "0").split(".")
                    n += comment("lemmy", str(c.get("id")), pid, path[-2] if len(path) > 2 else pid, max(0, len(path) - 2), b.name,
                                 (ci.get("creator") or {}).get("name") or "", c.get("content") or "", c.get("published"),
                                 (ci.get("counts") or {}).get("score") or 0, c.get("ap_id") or "")
    return n


# ---------------------------------------------------------------- Mastodon (hashtag timeline)
def run_mastodon() -> int:
    n, t0 = 0, time.time()
    for b in settings.brands:
        tag = re.sub(r"[^a-z0-9]", "", b.name.lower())
        if b.ambiguous or len(tag) < 4 or time.time() - t0 > STEP_SECONDS:
            continue
        d = _jget(f"https://mastodon.social/api/v1/timelines/tag/{tag}", params={"limit": 20}, min_gap=1.0)
        for s in d or []:
            text = _plain(s.get("content") or "")
            if not _about(text, b):
                continue
            acct = (s.get("account") or {})
            n += post("mastodon", s.get("uri") or s.get("id"), b.name, "post", author=acct.get("display_name") or acct.get("acct") or "",
                      handle=acct.get("acct") or "", text=text, url=s.get("url") or s.get("uri") or "", created=s.get("created_at"),
                      likes=s.get("favourites_count") or 0, replies=s.get("replies_count") or 0, reposts=s.get("reblogs_count") or 0)
    return n


# ---------------------------------------------------------------- YouTube Shorts
def _walk(o, key):
    if isinstance(o, dict):
        if key in o:
            yield o[key]
        for v in o.values():
            yield from _walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, key)


def run_shorts() -> int:
    n, t0, best = 0, time.time(), []
    for b in settings.brands:
        if time.time() - t0 > STEP_SECONDS:
            break
        try:
            r = get("https://www.youtube.com/results", params={"search_query": f"{b.name} shorts"}, headers=UA, timeout=25, min_gap=1.5)
        except Exception as e:  # noqa: BLE001
            log.info("shorts %s failed: %s", b.name, e)
            continue
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text, re.S)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        seen = 0
        for v in _walk(data, "shortsLockupViewModel"):
            vid = next(iter(_walk(v, "videoId")), None)
            meta = v.get("overlayMetadata") or {}
            title = ((meta.get("primaryText") or {}).get("content")) or ""
            views = _num(((meta.get("secondaryText") or {}).get("content")) or "")
            # short titles are hashtag soup: the company must be named AND the clip must be about the sector
            if not vid or not title or not _about(title, b, always_context=b.tier == "adjacent"):
                continue
            n += post("youtube", vid, b.name, "short", title=title, url=f"https://www.youtube.com/shorts/{vid}",
                      thumb=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", views=views)
            best.append((views, vid, b.name, title))
            if views >= 500_000:
                _evt("shorts", b.name, "youtube", "short_video", f"Short about {b.name}: {title[:120]}", detail=f"{views:,} views",
                             url=f"https://www.youtube.com/shorts/{vid}", data={"platform": "youtube", "views": views}, dedupe_key=f"short|{vid}")
            seen += 1
            if seen >= 8:
                break
    from .voice import youtube_comments
    for views, vid, brand, title in sorted(best, key=lambda x: -x[0])[:3]:
        if time.time() - t0 > STEP_SECONDS or not _thread_due("youtube", vid, hours=48):
            continue
        try:
            for c in youtube_comments(vid, pages=1):
                n += comment("youtube", c["id"], vid, vid, 0, brand, c["author"], c["text"], c["published"], c["likes"],
                             f"https://www.youtube.com/shorts/{vid}?lc={c['id']}")
        except Exception as e:  # noqa: BLE001
            log.info("shorts comments %s failed: %s", vid, e)
    return n


# ---------------------------------------------------------------- WhatsApp channels, Substack, Telegram, LinkedIn / Threads links
def run_owned(socials: dict) -> int:
    n, t0 = 0, time.time()
    for b in settings.brands:
        s = socials.get(b.name, {})
        if time.time() - t0 > STEP_SECONDS:
            break
        if s.get("whatsapp"):
            try:
                r = get(profile_url("whatsapp", s["whatsapp"]), headers=UA, timeout=25, min_gap=1.0)
                desc = _html.unescape((re.search(r'<meta property="og:description" content="([^"]*)"', r.text) or [None, ""])[1])
                followers = _num((re.search(r"([\d.,]+\s*[KMB]?) followers", desc) or [None, ""])[1]) or None
                account(b.name, "whatsapp", s["whatsapp"], profile_url("whatsapp", s["whatsapp"]), followers=followers,
                        status="live" if followers else "found", note=re.sub(r"^Channel\s*•\s*[^•]+•\s*", "", desc)[:200])
            except Exception as e:  # noqa: BLE001
                account(b.name, "whatsapp", s["whatsapp"], profile_url("whatsapp", s["whatsapp"]), status="unreachable", note=str(e)[:120])
        if s.get("substack"):
            pub = s["substack"]
            d = _jget(f"https://{pub}.substack.com/api/v1/archive", params={"sort": "new", "limit": 12}, min_gap=1.0)
            recent = 0
            for p in d or []:
                when = p.get("post_date")
                recent += bool(when and when >= _iso(_since(30)))
                url = p.get("canonical_url") or f"https://{pub}.substack.com/p/{p.get('slug')}"
                likes = sum((p.get("reactions") or {}).values()) if isinstance(p.get("reactions"), dict) else (p.get("reaction_count") or 0)
                n += post("substack", str(p.get("id")), b.name, "newsletter", author=pub, handle=pub, official=True, title=p.get("title") or "",
                          text=p.get("subtitle") or p.get("description") or "", url=url, thumb=p.get("cover_image") or "", created=when,
                          likes=likes, replies=p.get("comment_count") or 0)
                if (p.get("comment_count") or 0) and _thread_due("substack", str(p.get("id")), hours=24):
                    cd = _jget(f"https://{pub}.substack.com/api/v1/post/{p.get('id')}/comments", params={"all_comments": "true", "sort": "best_first"}, min_gap=1.0)

                    def walk(items, parent, depth):
                        k = 0
                        for c in items or []:
                            k += comment("substack", str(c.get("id")), str(p.get("id")), parent, depth, b.name, c.get("name") or "", c.get("body") or "",
                                         c.get("date"), sum((c.get("reactions") or {}).values()) if isinstance(c.get("reactions"), dict) else 0, url)
                            k += walk(c.get("children"), str(c.get("id")), depth + 1)
                        return k
                    n += walk((cd or {}).get("comments"), str(p.get("id")), 0)
            account(b.name, "substack", pub, profile_url("substack", pub), posts_30d=recent, status="live" if d else "found")
        if s.get("telegram"):
            try:
                r = get(f"https://t.me/s/{s['telegram']}", headers=UA, timeout=12, min_gap=1.0)
                subs = _num((re.search(r'<div class="tgme_header_counter">([^<]+)</div>', r.text) or [None, ""])[1]) or None
                for m in list(re.finditer(r'data-post="([^"]+)".*?tgme_widget_message_text[^>]*>(.*?)</div>.*?<time datetime="([^"]+)"', r.text, re.S))[-15:]:
                    n += post("telegram", m.group(1), b.name, "post", author=s["telegram"], handle=s["telegram"], official=True,
                              text=_plain(m.group(2))[:1500], url=f"https://t.me/{m.group(1)}", created=m.group(3))
                account(b.name, "telegram", s["telegram"], profile_url("telegram", s["telegram"]), followers=subs, status="live")
            except Exception as e:  # noqa: BLE001
                account(b.name, "telegram", s["telegram"], profile_url("telegram", s["telegram"]), status="unreachable", note=str(e)[:120])
        for platform in ("linkedin", "threads", "youtube", "bluesky"):
            if s.get(platform):
                account(b.name, platform, s[platform], profile_url(platform, s[platform]), status="found",
                        note="profile link from the company website" if platform in ("linkedin", "threads") else "")
    return n


# ---------------------------------------------------------------- Instagram + Facebook (Meta Graph API, when a token is set)
GRAPH = "https://graph.facebook.com/v21.0/"


def run_meta(socials: dict) -> int:
    n = 0
    ig_token, ig_user = os.environ.get("INSTAGRAM_ACCESS_TOKEN"), os.environ.get("INSTAGRAM_BUSINESS_ID")
    fb_token = os.environ.get("FACEBOOK_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN")
    for b in settings.brands:
        s = socials.get(b.name, {})
        if s.get("instagram"):
            h = s["instagram"]
            if not (ig_token and ig_user):
                account(b.name, "instagram", h, profile_url("instagram", h), status="needs_token",
                        note="Instagram shows profiles only to signed-in users: set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_BUSINESS_ID")
            else:
                fields = (f"business_discovery.username({h}){{followers_count,media_count,media.limit(15){{id,caption,media_type,media_product_type,"
                          "like_count,comments_count,timestamp,permalink,thumbnail_url,media_url}}")
                d = _jget(GRAPH + ig_user, params={"fields": fields, "access_token": ig_token}, min_gap=1.0)
                bd = (d or {}).get("business_discovery")
                if not bd:
                    account(b.name, "instagram", h, profile_url("instagram", h), status="unreachable", note="Graph API did not return this account")
                else:
                    recent = 0
                    for m in (bd.get("media") or {}).get("data", []):
                        reel = (m.get("media_product_type") or "") == "REELS"
                        recent += bool((m.get("timestamp") or "") >= _iso(_since(30)))
                        n += post("instagram", m["id"], b.name, "reel" if reel else "post", author=h, handle=h, official=True,
                                  text=m.get("caption") or "", url=m.get("permalink") or "", thumb=m.get("thumbnail_url") or m.get("media_url") or "",
                                  created=m.get("timestamp"), likes=m.get("like_count") or 0, replies=m.get("comments_count") or 0)
                    account(b.name, "instagram", h, profile_url("instagram", h), followers=bd.get("followers_count"), posts_30d=recent, status="live")
        if s.get("facebook"):
            h = s["facebook"]
            if not fb_token:
                account(b.name, "facebook", h, profile_url("facebook", h), status="needs_token",
                        note="Facebook pages need a Graph API token: set FACEBOOK_ACCESS_TOKEN (Page Public Content Access)")
            else:
                d = _jget(GRAPH + h, params={"fields": "name,followers_count,fan_count,posts.limit(10){id,message,created_time,permalink_url,shares,"
                                                       "comments.limit(0).summary(true),reactions.limit(0).summary(true)}", "access_token": fb_token}, min_gap=1.0)
                if not d:
                    account(b.name, "facebook", h, profile_url("facebook", h), status="needs_token",
                            note="the token cannot read this page (needs Page Public Content Access)")
                else:
                    for p in (d.get("posts") or {}).get("data", []):
                        n += post("facebook", p["id"], b.name, "post", author=d.get("name") or h, handle=h, official=True, text=p.get("message") or "",
                                  url=p.get("permalink_url") or "", created=p.get("created_time"),
                                  likes=((p.get("reactions") or {}).get("summary") or {}).get("total_count") or 0,
                                  replies=((p.get("comments") or {}).get("summary") or {}).get("total_count") or 0,
                                  reposts=(p.get("shares") or {}).get("count") or 0)
                    account(b.name, "facebook", h, profile_url("facebook", h), followers=d.get("followers_count") or d.get("fan_count"), status="live")
    return n


# ---------------------------------------------------------------- live web (GDELT) + Product Hunt
LIVE_RX = re.compile(r"\blive\b|live updates|live blog|as it happened", re.I)


def _web_fallback(pick) -> int:
    """When GDELT is throttling: Google News RSS for the last day, the same live-blog flag."""
    import feedparser
    from urllib.parse import quote_plus
    n = 0
    for b in pick:
        try:
            r = get(f"https://news.google.com/rss/search?q={quote_plus(chr(34) + b.name + chr(34) + ' when:1d')}&hl=en-IN&gl=IN&ceid=IN:en",
                    headers=UA, timeout=25, min_gap=2.0)
        except Exception as e:  # noqa: BLE001
            log.info("live web fallback %s failed: %s", b.name, e)
            continue
        for e in feedparser.parse(r.text).entries[:15]:
            title = re.sub(r"\s+-\s+[^-]+$", "", e.get("title") or "")
            if not _about(title, b):
                continue
            src = (e.get("source") or {}).get("title") or ""
            when = None
            try:
                when = _iso(datetime(*e.published_parsed[:6], tzinfo=timezone.utc)) if e.get("published_parsed") else None
            except (TypeError, ValueError):
                pass
            live = bool(LIVE_RX.search(title))
            new = post("web", sha(e.get("link") or title), b.name, "liveblog" if live else "article", author=src, title=title,
                       text=f"{src} · Google News", url=e.get("link") or "", created=when)
            n += new
            if new and live:
                _evt("web", b.name, "web", "live_update", f"Live coverage: {title[:160]}", detail=src, url=e.get("link") or "",
                     data={"platform": "web", "domain": src}, dedupe_key=f"live|{sha(e.get('link') or title)}")
    return n


def run_web() -> int:
    n = 0
    names = [b for b in settings.brands if not b.ambiguous] or settings.brands
    off = int(db.kv_get("social:gdelt_offset") or 0) % max(1, len(names))
    pick = (names[off:] + names[:off])[:14]   # GDELT queries stay short: rotate through the companies run by run
    db.kv_set("social:gdelt_offset", off + 14)
    terms = [f'"{b.name}"' if " " in b.name else b.name for b in pick if len(b.name) >= 3]
    q = "(" + " OR ".join(terms) + ")" if len(terms) > 1 else (terms[0] if terms else "")
    if not q:
        return 0
    d = None
    if time.time() >= float(db.kv_get("social:gdelt_backoff") or 0):
        d = _jget("https://api.gdeltproject.org/api/v2/doc/doc", params={"query": q, "mode": "artlist", "format": "json", "timespan": "1d",
                                                                         "maxrecords": 100, "sort": "datedesc"}, min_gap=6.0, timeout=40)
        if d is None:   # GDELT throttles an address for a while after bursts: step aside for two hours
            db.kv_set("social:gdelt_backoff", time.time() + 7200)
    if d is None:
        n += _web_fallback(pick[:8])
    for a in (d or {}).get("articles", []):
        title = a.get("title") or ""
        b = next((x for x in pick if _about(title, x)), None)
        if not b:
            continue
        when = None
        try:
            when = _iso(datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
        except ValueError:
            pass
        live = bool(re.search(r"\blive\b|live updates|live blog|as it happened", title, re.I))
        new = post("web", sha(a.get("url") or title), b.name, "liveblog" if live else "article", author=a.get("domain") or "",
                   title=title, text=f"{a.get('domain', '')} · {a.get('sourcecountry', '')} · {a.get('language', '')}", url=a.get("url") or "",
                   thumb=a.get("socialimage") or "", created=when)
        n += new
        if new and live:
            _evt("web", b.name, "web", "live_update", f"Live coverage: {title[:160]}", detail=a.get("domain") or "", url=a.get("url") or "",
                         data={"platform": "web", "domain": a.get("domain")}, dedupe_key=f"live|{sha(a.get('url') or title)}")
    if _due("producthunt", 3):
        try:
            import feedparser
            feed = feedparser.parse(get("https://www.producthunt.com/feed", headers=UA, timeout=25, min_gap=1.0).text)
            for e in feed.entries[:60]:
                text = f"{e.get('title', '')} {_plain(e.get('summary', ''))}"
                b = next((x for x in settings.brands if _about(text, x)), None)
                if b:
                    n += post("producthunt", e.get("id") or e.get("link"), b.name, "launch", title=e.get("title") or "",
                              text=_plain(e.get("summary", ""))[:600], url=e.get("link") or "", created=e.get("published"))
            _done("producthunt")
        except Exception as e:  # noqa: BLE001
            log.info("producthunt failed: %s", e)
    return n


# ---------------------------------------------------------------- the collector
@tracked("social")
def run() -> int:
    from ..schema_ext import ensure
    ensure()
    socials = {}
    for b in settings.brands:
        try:
            socials[b.name] = handles(b)
        except Exception as e:  # noqa: BLE001
            log.info("handles %s failed: %s", b.name, e)
            socials[b.name] = {}
    steps = [("x", lambda: run_x(socials), 0), ("bluesky", run_bluesky, 0), ("hn", run_hn, 0), ("reddit", run_reddit, 0),
             ("lemmy", run_lemmy, 1), ("mastodon", run_mastodon, 1), ("web", run_web, 0), ("shorts", run_shorts, SLOW_EVERY_H),
             ("owned", lambda: run_owned(socials), SLOW_EVERY_H), ("meta", lambda: run_meta(socials), SLOW_EVERY_H)]
    n = 0
    SEEDED.clear()
    SEEDED.update({name: bool(db.kv_get(f"social:step:{name}")) for name, _, _ in steps})
    for name, fn, every_h in steps:
        if every_h and not _due(name, every_h):
            continue
        t = time.time()
        try:
            got = fn()
            n += got
            db.kv_set(f"social:status:{name}", {"at": db.now(), "ok": True, "new": got, "seconds": round(time.time() - t)})
            _done(name)
        except Exception as e:  # noqa: BLE001 - one platform failing never stops the others
            log.warning("social step %s failed: %s", name, e)
            db.kv_set(f"social:status:{name}", {"at": db.now(), "ok": False, "error": str(e)[:200]})
    return n
