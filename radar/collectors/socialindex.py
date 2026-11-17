"""X, Instagram and Facebook (posts, Reels, videos) for every company, without a platform login.

None of the three lets an anonymous client search it, and Instagram and Facebook hide profiles behind a sign-in. So:

  find   a web search API (BRAVE_API_KEY: Brave Search, or SERPER_API_KEY: Google results through Serper) lists the
         posts, Reels, videos and profiles that name each company (`site:x.com "Zepto"`, past week). A result's snippet
         already carries the caption, the author and the date, for Instagram also likes and comments, and for a profile
         its followers. Searches are capped per sector per day (SOCIAL_SEARCH_DAILY, default 30) and spread over the
         day's runs, the seat's own company first.
  link   free, no key: X posts and Facebook Reels / videos that the rest of the conversation links to (an HN story on an
         X post, a Reddit thread sharing a Reel, a Bluesky post quoting a tweet) are picked up from what the other
         platforms already collected.
  fill   the platforms' own embeds, made for any website, add what a snippet lacks: X's embed data for one post (likes,
         replies, views on video, the post it answers, so complaints thread under the post they reply to), Facebook's
         video embed for a Reel or video (author, caption, likes, comments, shares, thumbnail) and Facebook's page embed
         (followers, no key needed).
  token  with X_BEARER_TOKEN set, X API v2 recent search adds every post naming the company and the replies under the
         busiest ones; the Meta Graph tokens (social.run_meta) read whole Instagram and Facebook feeds.
"""
from __future__ import annotations
import html as _html
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from .. import db
from ..config import settings
from .base import get, log, session
from . import social as S

DAILY = 30                # searches per sector per day unless SOCIAL_SEARCH_DAILY says otherwise
RUN_SHARE = 8             # a run spends at most 1/8 of the day's searches, so they spread over the day
ENRICH_X = 24             # X posts filled from the embed per run
ENRICH_FB = 12            # Facebook Reels and videos filled from the embed per run
PROFILE_EVERY_D = 7       # Facebook follower counts (page embed)
MAX_AGE_D = 180           # older results are not stored
PROVIDERS = (("brave", "BRAVE_API_KEY"), ("serper", "SERPER_API_KEY"))


# ---------------------------------------------------------------- the web index (a search API)
def _text(s: str) -> str:
    s = re.sub(r"</?(?:b|strong|em)>", "", s or "")   # search engines bold query words inside words ("#<b>zepto</b>")
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


_REL = re.compile(r"^(\d+)\s+(minute|hour|day|week|month)s?\s+ago(?:\s*[·—-]\s*|$)", re.I)
_ABS = re.compile(r"^([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})(?:\s*[·—-]\s*|$)")


def _date(s: str) -> str | None:
    s = re.sub(r"\s+", " ", (s or "").replace(".", "")).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return S._iso(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    return None


def _lead_date(s: str) -> tuple[str | None, str]:
    """'4 days ago · text' / 'Oct 17, 2026 — text' -> (iso date, text)."""
    s = s or ""
    m = _REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n), "day": timedelta(days=n),
                 "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
        return S._iso(datetime.now(timezone.utc) - delta), s[m.end():]
    m = _ABS.match(s)
    if m:
        return _date(m.group(1)), s[m.end():]
    return None, s


def _when(s: str | None) -> str | None:
    """A result's date as the API gives it: ISO, '4 days ago' or 'Oct 17, 2026'."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return S._iso(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    except ValueError:
        return _lead_date(str(s))[0]


class Refused(Exception):
    """The search API did not answer (or today's searches are spent); the rest of the step goes on without it."""


def provider() -> tuple[str, str] | None:
    for name, env in PROVIDERS:
        if os.environ.get(env):
            return name, os.environ[env]
    return None


def _daily() -> int:
    try:
        return max(0, int(os.environ.get("SOCIAL_SEARCH_DAILY") or DAILY))
    except ValueError:
        return DAILY


def searches_left() -> int:
    used = db.kv_get("social:search_used") or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _daily() - (int(used.get("n") or 0) if used.get("day") == today else 0)


def _spend() -> None:
    used = db.kv_get("social:search_used") or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = int(used.get("n") or 0) if used.get("day") == today else 0
    db.kv_set("social:search_used", {"day": today, "n": n + 1})


def search(q: str, *, week: bool = True) -> list[dict]:
    """Web results for q: [{url, title, snippet, when}]. Raises Refused without a key, past the budget, or on an error."""
    prov = provider()
    if not prov:
        raise Refused("no search key")
    if time.time() < float(db.kv_get("social:search_backoff") or 0):
        raise Refused("the search API is resting after an error")
    if searches_left() <= 0:
        raise Refused("today's searches are spent")
    _spend()
    name, key = prov
    try:
        if name == "brave":
            r = get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": 20, **({"freshness": "pw"} if week else {})},
                    headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=25, min_gap=1.1)
        else:
            r = session.post("https://google.serper.dev/search", json={"q": q, "num": 20, "gl": "in", **({"tbs": "qdr:w"} if week else {})},
                             headers={"X-API-KEY": key, "Content-Type": "application/json"}, timeout=25)
    except Exception as e:  # noqa: BLE001
        raise Refused(f"{name} search failed: {e}") from e
    if r.status_code != 200:
        db.kv_set("social:search_backoff", time.time() + (3600 if r.status_code in (401, 402, 403, 429) else 600))
        db.kv_set("social:search_error", {"at": db.now(), "provider": name, "status": r.status_code, "body": r.text[:200]})
        raise Refused(f"{name} search answered {r.status_code}")
    try:
        d = r.json()
    except ValueError as e:
        raise Refused(f"{name} search answered no JSON") from e
    out = []
    if name == "brave":
        for it in (d.get("web") or {}).get("results") or []:
            when, snip = _lead_date(_text(it.get("description") or ""))
            out.append({"url": it.get("url") or "", "title": _text(it.get("title") or ""), "snippet": snip,
                        "when": _when(it.get("page_age")) or _when(it.get("age")) or when})
    else:
        for it in d.get("organic") or []:
            when, snip = _lead_date(_text(it.get("snippet") or ""))
            out.append({"url": it.get("link") or "", "title": _text(it.get("title") or ""), "snippet": snip, "when": _when(it.get("date")) or when})
    return [h for h in out if h["url"].startswith("http")]


def _phrase(b) -> str:
    """What to search for: the name in quotes; an everyday-word name also gets its sector words ("Pebble" smartwatch)."""
    from ..relevance import AMBIGUOUS
    q = f'"{b.name}"'
    if (b.ambiguous or b.name.lower() in AMBIGUOUS) and b.search_terms:
        extra = re.sub(rf"(?i)\b{re.escape(b.name)}\b", " ", b.search_terms[0]).strip()
        if extra:
            q += f" {extra}"
    return q


def _rel(text: str, b, hs: dict) -> bool:
    """About company b: one of its own @handles, or the shared relevance rules (social._about)."""
    for p in ("x", "instagram", "facebook"):
        h = hs.get(p)
        if isinstance(h, str) and len(h) >= 3 and re.search(rf"(?i)@{re.escape(h)}(?![A-Za-z0-9_])", text):
            return True
    if S._about(text, b):
        return True
    # an everyday-word name ("Zepto", "Porter") in a business sentence next to another company of the sector is about it
    # ("use instamart or blinkit, zepto support never answers")
    from ..relevance import relevant
    return bool(relevant(text, b) and S.BUSINESS.search(text) and any(
        o is not b and not o.ambiguous and len(o.name) >= 4 and re.search(rf"(?i)(?<![A-Za-z]){re.escape(o.name)}(?![A-Za-z])", text)
        for o in settings.brands))


def _fresh(when: str | None) -> bool:
    return not when or when >= S._iso(S._since(MAX_AGE_D))


def _stale(platform: str, pid: str, hours: float = 12) -> bool:
    """True when the post is new here or its numbers were last refreshed more than `hours` ago."""
    r = db.q1("SELECT seen_at FROM social_posts WHERE platform=? AND post_id=?", (platform, pid))
    return not r or (r["seen_at"] or "") < (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _same(name: str, b) -> bool:
    """A display name that is the company's own ("Zepto", "Flipkart India", "Ola Official")."""
    n, t = _norm(name), _norm(b.name)
    return bool(t) and n in (t, t + "india", t + "official", t + "app", t + "in", t + "hq")


def _adopt(b, hs: dict, platform: str, handle: str) -> None:
    """A company whose website links no account of this platform: take the one it shows up under with its own name.
    Kept in kv socials:<brand> next to the website's links, so the other steps (X timeline, accounts) use it too."""
    if hs.get(platform) or not handle:
        return
    hs[platform] = handle
    cached = db.kv_get(f"socials:{b.name}") or {}
    if not cached.get(platform):
        db.kv_set(f"socials:{b.name}", {**cached, platform: handle})
        log.info("social index: %s on %s is @%s (its own name on the account)", b.name, platform, handle)


# ---------------------------------------------------------------- X
X_STATUS = re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{6,25})", re.I)
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _xtoken(tid: str) -> str:
    """The token X's embed script sends with a post id: id / 1e15 * pi in base 36, zeros and the point dropped."""
    v = int(tid) / 1e15 * math.pi
    ip, frac, s = int(v), v - int(v), ""
    while ip:
        ip, r = divmod(ip, 36)
        s = _B36[r] + s
    s = (s or "0") + "."
    for _ in range(11):
        frac *= 36
        d = int(frac)
        s += _B36[d]
        frac -= d
    return re.sub(r"0+|\.", "", s)


def _tweet(tid: str) -> dict | None:
    """One post as X's embeds see it (cdn.syndication.twimg.com): text, author, likes, replies, media, the parent."""
    d = S._jget("https://cdn.syndication.twimg.com/tweet-result", params={"id": tid, "lang": "en", "token": _xtoken(tid)}, min_gap=1.0)
    if not isinstance(d, dict) or not d.get("id_str") or d.get("__typename") == "TweetTombstone":
        return None
    return d


def _xdate(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return S._iso(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        try:
            return S._iso(datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y"))
        except ValueError:
            return None


def _store_tweet(d: dict, b, hs: dict, when: str | None) -> int:
    user = d.get("user") or {}
    sn = user.get("screen_name") or ""
    if sn and user.get("verified_type") == "Business" and _same(user.get("name") or "", b):
        _adopt(b, hs, "x", sn)
    own_h = (hs.get("x") or "").lower()
    text = _html.unescape(d.get("text") or "")
    own = bool(own_h) and sn.lower() == own_h
    if not own and not _rel(text, b, hs):
        return 0
    media = d.get("mediaDetails") or []
    video = d.get("video") or {}
    is_video = bool(video) or any((m.get("type") or "") in ("video", "animated_gif") for m in media)
    thumb = video.get("poster") or next((m.get("media_url_https") for m in media if m.get("media_url_https")), "") or ""
    created = _xdate(d.get("created_at")) or when
    if not _fresh(created):
        return 0
    pid = d.get("in_reply_to_status_id_str")
    kind = "video" if is_video else "reply" if pid else "post"
    url = f"https://x.com/{sn or 'i'}/status/{d['id_str']}"
    likes, replies = int(d.get("favorite_count") or 0), int(d.get("conversation_count") or d.get("reply_count") or 0)
    reposts = int(d.get("retweet_count") or 0) + int(d.get("quote_count") or 0)
    views = S._num(video.get("viewCount"))
    new = S.post("x", d["id_str"], b.name, kind, author=user.get("name") or sn, handle=sn, official=own, text=text, url=url, thumb=thumb,
                 created=created, likes=likes, replies=replies, reposts=reposts, views=views)
    if pid:   # a reply: it joins the conversation under the post it answers, which is kept as the thread's header
        par = d.get("parent") or {}
        pu = par.get("user") or {}
        if par.get("id_str"):
            psn = pu.get("screen_name") or ""
            ptext = _html.unescape(par.get("text") or "")
            pown = bool(own_h) and psn.lower() == own_h
            S.post("x", par["id_str"], b.name, "post" if (pown or _rel(ptext, b, hs)) else "context", author=pu.get("name") or psn,
                   handle=psn, official=pown, text=ptext, url=f"https://x.com/{psn or 'i'}/status/{par['id_str']}",
                   created=_xdate(par.get("created_at")), likes=par.get("favorite_count") or 0, replies=par.get("reply_count") or 0,
                   reposts=par.get("retweet_count") or 0)
        S.comment("x", d["id_str"], pid, pid, 0, b.name, user.get("name") or sn, text, created, likes, url)
    if new and created and created >= S._iso(S._since(3)) and (likes >= 2000 or replies >= 200):
        S._evt("index", b.name, "x", "viral_post", f"{'' if own else 'About '}{b.name} on X: {text[:140]}",
               detail=f"{likes:,} likes · {replies:,} replies · {reposts:,} reposts" + ("" if own else f" · by @{sn}"),
               url=url, data={"platform": "x", "likes": likes, "replies": replies, "official": own}, dedupe_key=f"x|{d['id_str']}")
    return int(new)


def _x_hit(hit: dict, b, hs: dict, budget: dict) -> int:
    m = X_STATUS.match(hit["url"])
    if not m:
        pr = re.match(r"^(.+?)\s+\(@([A-Za-z0-9_]{1,15})\)\s*/\s*(?:X|Twitter)", hit["title"])
        if pr and _same(pr.group(1), b) and re.match(rf"https?://(?:www\.)?(?:x|twitter)\.com/{pr.group(2)}/?$", hit["url"], re.I):
            _adopt(b, hs, "x", pr.group(2))
        return 0
    if not _fresh(hit["when"]):
        return 0
    tid = m.group(2)
    if not _stale("x", tid):
        return 0
    d = None
    if budget["x"] > 0 and budget["x_fail"] < 3:
        budget["x"] -= 1
        d = _tweet(tid)
        budget["x_fail"] = 0 if d else budget["x_fail"] + 1
    if d:
        return _store_tweet(d, b, hs, hit["when"])
    if not (hit["title"] or hit["snippet"]):
        return 0   # a bare link with nothing to read
    # the embed did not answer (or this run's share is spent): keep what the search result shows
    author = re.split(r"\s+on X\b|\s+on Twitter\b", hit["title"])[0].strip()
    quoted = re.search(r'on (?:X|Twitter):\s*"(.+?)(?:"|\.\.\.|…|$)', hit["title"])
    text = hit["snippet"] or (quoted.group(1) if quoted else "")
    if not _rel(f"{hit['title']} {text}", b, hs):
        return 0
    own = bool(hs.get("x")) and m.group(1).lower() == hs["x"].lower()
    return int(S.post("x", tid, b.name, "post", author=author, handle=m.group(1), official=own, text=text,
                      url=f"https://x.com/{m.group(1)}/status/{tid}", created=hit["when"]))


def _x(b, hs: dict, budget: dict, first: bool) -> int:
    hits = search(f"site:x.com {_phrase(b)}", week=not first)
    hits.sort(key=lambda h: bool(X_STATUS.match(h["url"])))   # the company's profile first, so its own posts count as official
    return sum(_x_hit(h, b, hs, budget) for h in hits)


# ---------------------------------------------------------------- Instagram
IG_POST = re.compile(r"https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9_.]{2,30}/)?(reel|reels|p|tv)/([A-Za-z0-9_-]{5,40})", re.I)
IG_PROFILE = re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:reels/?)?(?:[?#].*)?$", re.I)
IG_RESERVED = {"explore", "popular", "accounts", "stories", "direct", "about", "legal", "reels", "reel", "p", "tv", "web"}
IG_META = re.compile(r"(?:(?P<likes>[\d.,]+\s*[KMB]?)\s+likes?,?\s*)?(?:(?P<comments>[\d.,]+\s*[KMB]?)\s+comments?\s*)?(?:-\s*|^)@?(?P<handle>[A-Za-z0-9_.]{2,30})"
                     r"\s+on\s+(?P<date>[A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})\s*:?\s*", re.I)
IG_TITLE = re.compile(r"^(.+?)\s+\(@([A-Za-z0-9_.]{2,30})\)\s*[•·]\s*Instagram", re.I)


def _official_30d(platform: str, brand: str) -> int | None:
    n = db.q1("SELECT COUNT(*) n FROM social_posts WHERE platform=? AND brand=? AND official=1 AND created>=?",
              (platform, brand, S._iso(S._since(30))))["n"]
    return n or None


def _ig_followers(hit: dict, b, h: str) -> bool:
    f = re.search(r"([\d.,]+\s*[KMB]?)\s+Followers", hit["snippet"], re.I)
    if not f:
        return False
    posts = re.search(r"([\d.,]+\s*[KMB]?)\s+Posts", hit["snippet"], re.I)
    S.account(b.name, "instagram", h, S.profile_url("instagram", h), followers=S._num(f.group(1)), posts_30d=_official_30d("instagram", b.name),
              status="live", note=f"followers from the web index{f' · {S._num(posts.group(1)):,} posts in all' if posts else ''}")
    return True


def _ig_hit(hit: dict, b, hs: dict) -> int:
    m = IG_POST.match(hit["url"])
    if not m:
        pr, t = IG_PROFILE.match(hit["url"]), IG_TITLE.match(hit["title"])
        if pr and t and pr.group(1).lower() == t.group(2).lower() and pr.group(1).lower() not in IG_RESERVED:
            if not hs.get("instagram") and _same(t.group(1), b):
                _adopt(b, hs, "instagram", t.group(2))
            if (hs.get("instagram") or "").lower() == t.group(2).lower():
                _ig_followers(hit, b, hs["instagram"])
        return 0
    code, reel = m.group(2), m.group(1).lower() in ("reel", "reels", "tv")
    snip, title = hit["snippet"], hit["title"]
    meta = IG_META.search(snip)
    likes = comments = 0
    handle, when, caption = "", hit["when"], snip
    if meta:
        likes, comments = S._num(meta.group("likes")), S._num(meta.group("comments"))
        handle = meta.group("handle")
        when = _date(meta.group("date")) or when
        after = snip[meta.end():].strip()
        caption = re.sub(r'["“”]\.?\s*$', "", (after or snip[:meta.start()]).strip()).strip().strip('"“”').strip()
    if not _fresh(when):
        return 0
    author = re.split(r"\s+\|\s+|\s+on Instagram\b", title)[0].strip()
    own_h = (hs.get("instagram") or "").lower()
    own = bool(own_h) and handle.lower() == own_h
    if not handle and " | " in title and _same(author, b):   # "Zepto | caption": the title leads with the account's display name
        own, handle = True, hs.get("instagram") or ""
    text = caption or re.sub(r"^.*?\|\s*", "", title)
    if not own and not _rel(f"{title} {text}", b, hs):
        return 0
    url = f"https://www.instagram.com/{'reel' if reel else 'p'}/{code}/"
    new = S.post("instagram", code, b.name, "reel" if reel else "post", author=author or handle, handle=handle, official=own, text=text,
                 url=url, created=when, likes=likes, replies=comments)
    if new and when and when >= S._iso(S._since(3)) and (likes >= 50_000 or comments >= 2_000):
        S._evt("index", b.name, "instagram", "viral_post", f"{'Reel' if reel else 'Post'} {'by' if own else 'about'} {b.name} on Instagram: {text[:120]}",
               detail=f"{likes:,} likes · {comments:,} comments" + ("" if own else f" · by @{handle or author}"), url=url,
               data={"platform": "instagram", "likes": likes, "comments": comments, "official": own}, dedupe_key=f"ig|{code}")
    return int(new)


def _instagram(b, hs: dict, first: bool) -> int:
    # a first visit reads without the past-week filter: the last months' Reels and the profile (followers) come with it
    hits = search(f"site:instagram.com {_phrase(b)}", week=not first)
    hits.sort(key=lambda h: bool(IG_POST.match(h["url"])))   # profiles first: an adopted handle marks the posts official
    return sum(_ig_hit(h, b, hs) for h in hits)


# ---------------------------------------------------------------- Facebook
FB_HOST = r"https?://(?:www\.|m\.|web\.|mbasic\.)?facebook\.com/"
FB_REEL = re.compile(FB_HOST + r"reel/(\d{6,25})", re.I)
FB_VIDEO = re.compile(FB_HOST + r"(?:([A-Za-z0-9.\-]{2,80})/videos/(?:[^/?#]+/)?(\d{6,25})|watch/?\?v=(\d{6,25}))", re.I)
FB_POST = re.compile(FB_HOST + r"([A-Za-z0-9.\-]{2,80})/posts/(?:[^/?#]+/)?([A-Za-z0-9]{6,80})", re.I)


def _fb_embed(url: str) -> dict | None:
    """Facebook's own video embed for a Reel or video: author, caption, likes, comments, shares, thumbnail."""
    try:
        r = get("https://www.facebook.com/plugins/video.php", params={"href": url, "show_text": "true", "width": 500, "locale": "en_US"},
                headers=S.UA, timeout=25, min_gap=2.0)
    except Exception as e:  # noqa: BLE001
        log.info("facebook embed %s failed: %s", url, e)
        return None
    if r.status_code != 200:
        return None
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S)
    if 'title="Like"' not in body and "userContent" not in body:
        return None

    def count(label: str) -> int:
        m = re.search(rf'title="{label}"[^>]*>(?:\s*<i[^>]*></i>)*\s*([^<]*)<', body)
        return S._num(m.group(1)) if m else 0
    author = re.search(r'<a title="([^"]+)"[^>]*href="[^"]*"', body)
    cap = re.search(r'data-testid="post_message"[^>]*>(.*?)</div>', body, re.S)
    thumb = next((t for t, h in re.findall(r'<img[^>]*src="(https://[^"]*fbcdn[^"]+)"[^>]*height="(\d+)"', body) if int(h) >= 100), "")
    return {"author": _html.unescape(author.group(1)) if author else "", "text": _text(cap.group(1)) if cap else "",
            "likes": count("Like"), "comments": count("Comment"), "shares": count("Share"), "thumb": _html.unescape(thumb)}


def _fb_hit(hit: dict, b, hs: dict, budget: dict) -> int:
    url, title, snip = hit["url"], hit["title"], hit["snippet"]
    own_h = _norm(hs.get("facebook") or "")
    m = FB_REEL.match(url)
    v = None if m else FB_VIDEO.match(url)
    p = None if (m or v) else FB_POST.match(url)
    if not (m or v or p) or not _fresh(hit["when"]):
        return 0
    if p:
        if not (title or snip):
            return 0
        page, pid = p.group(1), p.group(2)
        author = re.split(r"\s+-\s+", title)[0].strip() if " - " in title else page
        if not own_h and _same(author, b) and _norm(b.name) in _norm(page):
            _adopt(b, hs, "facebook", page)
            own_h = _norm(page)
        own = bool(own_h) and _norm(page) == own_h
        text = snip or re.sub(r"^[^-]+-\s+", "", title)
        if not own and not _rel(f"{title} {text}", b, hs):
            return 0
        return int(S.post("facebook", f"{page}/{pid}", b.name, "post", author=author, handle=page, official=own, text=text,
                          url=url.split("?")[0], created=hit["when"]))
    vid = m.group(1) if m else (v.group(2) or v.group(3))
    kind = "reel" if m else "video"
    page = "" if m else (v.group(1) or "")
    canon = f"https://www.facebook.com/reel/{vid}/" if m else url.split("&")[0]
    e = None
    if budget["fb"] > 0 and _stale("facebook", vid):
        budget["fb"] -= 1
        e = _fb_embed(canon)
    if not e and not (title or snip):
        return 0
    author = (e or {}).get("author") or (re.split(r"\s+\|\s+", re.sub(r"\s*-\s*Facebook\s*$", "", title))[-1].strip() if " | " in title else page)
    text = (e or {}).get("text") or snip or re.sub(r"\s*-\s*Facebook\s*$", "", title)
    own = bool(own_h) and (_norm(page) == own_h or _norm(author) in (own_h, _norm(b.name)))
    if not own and not _rel(f"{title} {text}", b, hs):
        return 0
    likes, comments, shares = (e or {}).get("likes", 0), (e or {}).get("comments", 0), (e or {}).get("shares", 0)
    new = S.post("facebook", vid, b.name, kind, author=author, handle=page, official=own, text=text, url=canon, thumb=(e or {}).get("thumb", ""),
                 created=hit["when"], likes=likes, replies=comments, reposts=shares)
    if new and hit["when"] and hit["when"] >= S._iso(S._since(3)) and (likes >= 20_000 or shares >= 5_000):
        S._evt("index", b.name, "facebook", "short_video", f"Facebook {kind} {'by' if own else 'about'} {b.name}: {text[:120]}",
               detail=f"{likes:,} likes · {comments:,} comments · {shares:,} shares", url=canon,
               data={"platform": "facebook", "likes": likes, "official": own}, dedupe_key=f"fb|{vid}")
    return int(new)


def _facebook(b, hs: dict, budget: dict, first: bool) -> int:
    # one search finds the company's page posts, videos and Reels and everyone else's that name it
    return sum(_fb_hit(h, b, hs, budget) for h in search(f"site:facebook.com {_phrase(b)}", week=not first))


def _fb_profile(b, hs: dict) -> None:
    """Followers from Facebook's page embed (no key, no login)."""
    h = hs.get("facebook")
    if not h:
        return
    try:
        r = get("https://www.facebook.com/plugins/page.php", params={"href": f"https://www.facebook.com/{h}", "tabs": "timeline", "width": 500,
                                                                     "height": 600, "small_header": "false", "locale": "en_US"},
                headers=S.UA, timeout=25, min_gap=2.0)
        f = re.search(r"([\d][\d,.]*\s*[KMB]?)\s+followers", _text(r.text)) if r.status_code == 200 else None
    except Exception as e:  # noqa: BLE001
        log.info("facebook page embed %s failed: %s", h, e)
        f = None
    if f:
        S.account(b.name, "facebook", h, S.profile_url("facebook", h), followers=S._num(f.group(1)), posts_30d=_official_30d("facebook", b.name),
                  status="live", note="followers from Facebook's page embed")
    elif not db.q1("SELECT 1 FROM social_accounts WHERE brand=? AND platform='facebook' AND status='live'", (b.name,)):
        S.account(b.name, "facebook", h, S.profile_url("facebook", h), status="found", note="the page embed showed no follower count")


# ---------------------------------------------------------------- links inside what the other platforms collected (free)
LINKED = re.compile(r"https?://(?:www\.|mobile\.|m\.|web\.)?(?:x\.com|twitter\.com|facebook\.com)/[^\s\"'<>)\]]+", re.I)


def _harvest(b, hs: dict, budget: dict) -> int:
    """X posts and Facebook Reels / videos the rest of the conversation about b links to: an HN story on an X post, a
    Reddit thread sharing a Reel. No search needed; the embeds fill them in, relevance is checked on their own text."""
    since = S._iso(S._since(14))
    rows = db.q("SELECT title, text, url, created FROM social_posts WHERE brand=? AND platform NOT IN ('x','instagram','facebook') AND created>=?",
                (b.name, since))
    rows += db.q("SELECT '' title, text, url, created FROM social_comments WHERE brand=? AND platform NOT IN ('x','instagram','facebook') "
                 "AND created>=? AND (text LIKE '%x.com/%' OR text LIKE '%twitter.com/%' OR text LIKE '%facebook.com/%')", (b.name, since))
    n, seen = 0, set()
    for r in rows:
        for u in LINKED.findall(" ".join(str(r.get(k) or "") for k in ("title", "text", "url"))):
            u = u.rstrip(".,;:!?")
            key = (X_STATUS.match(u) or FB_REEL.match(u) or FB_VIDEO.match(u))
            if not key or key.group(0) in seen:
                continue
            seen.add(key.group(0))
            hit = {"url": key.group(0), "title": "", "snippet": "", "when": None}
            n += _x_hit(hit, b, hs, budget) if X_STATUS.match(u) else _fb_hit(hit, b, hs, budget)
    return n


# ---------------------------------------------------------------- X API v2 (only with X_BEARER_TOKEN)
def _x_api(pick: list, socials: dict) -> int:
    tok = os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")
    if not tok or time.time() < float(db.kv_get("social:xapi_backoff_until") or 0):
        return 0
    head = {"Authorization": f"Bearer {tok}"}
    fields = {"max_results": 50, "tweet.fields": "created_at,public_metrics,conversation_id,referenced_tweets,author_id",
              "expansions": "author_id", "user.fields": "username,name"}
    n, busiest = 0, []

    def read(query: str, b, hs: dict, as_replies_of: str = "") -> int:
        k = 0
        try:
            r = get("https://api.x.com/2/tweets/search/recent", params={"query": query, **fields}, headers=head, timeout=25, min_gap=1.0)
        except Exception as e:  # noqa: BLE001
            log.info("X API search failed: %s", e)
            return 0
        if r.status_code == 429:
            db.kv_set("social:xapi_backoff_until", float(r.headers.get("x-rate-limit-reset") or time.time() + 900))
            raise Refused("X API rate limit")
        if r.status_code != 200:
            db.kv_set("social:status:xapi", {"at": db.now(), "ok": False, "error": f"X API answered {r.status_code}: {r.text[:160]}"})
            raise Refused(f"X API answered {r.status_code}")
        d = r.json()
        users = {u["id"]: u for u in (d.get("includes") or {}).get("users", [])}
        for t in d.get("data") or []:
            u = users.get(t.get("author_id"), {})
            pm = t.get("public_metrics") or {}
            own = bool(hs.get("x")) and (u.get("username") or "").lower() == hs["x"].lower()
            if not as_replies_of and not own and not _rel(t.get("text", ""), b, hs):
                continue
            url = f"https://x.com/{u.get('username') or 'i'}/status/{t['id']}"
            parent = next((x["id"] for x in t.get("referenced_tweets") or [] if x.get("type") == "replied_to"), "")
            created = _xdate(t.get("created_at"))
            if as_replies_of:
                k += S.comment("x", t["id"], as_replies_of, parent or as_replies_of, 0 if parent in ("", as_replies_of) else 1, b.name,
                               u.get("name") or u.get("username") or "", t.get("text", ""), created, pm.get("like_count") or 0, url)
                continue
            k += S.post("x", t["id"], b.name, "reply" if parent else "post", author=u.get("name") or "", handle=u.get("username") or "",
                        official=own, text=t.get("text", ""), url=url, created=created, likes=pm.get("like_count") or 0,
                        replies=pm.get("reply_count") or 0, reposts=(pm.get("retweet_count") or 0) + (pm.get("quote_count") or 0),
                        views=pm.get("impression_count") or 0)
            if (pm.get("reply_count") or 0) >= 5:
                busiest.append((pm["reply_count"], t.get("conversation_id") or t["id"], b, hs))
        return k
    try:
        for b in pick:
            hs = {k: v for k, v in (socials.get(b.name) or {}).items() if isinstance(v, str)}
            q = f"({_phrase(b)}{' OR @' + hs['x'] if hs.get('x') else ''}) -is:retweet"
            n += read(q, b, hs)
        for _, conv, b, hs in sorted(busiest, key=lambda x: -x[0])[:3]:   # the replies under the busiest posts
            if S._thread_due("xapi", conv, hours=12):
                n += read(f"conversation_id:{conv}", b, hs, as_replies_of=conv)
        db.kv_set("social:status:xapi", {"at": db.now(), "ok": True, "new": n})
    except Refused as e:
        log.info("X API: %s", e)
    return n


# ---------------------------------------------------------------- the step
def _rotation(k: int) -> list:
    """The next k companies for the search part: the seat's own company once a day first, then the rest in turn."""
    brands = list(settings.brands)
    off = int(db.kv_get("social:index_offset") or 0) % max(1, len(brands))
    order = brands[off:] + brands[:off]
    me = settings.self_brand
    if me:
        order = [x for x in order if x is not me]
        if (db.kv_get(f"social:index_seen:{me.name}") or "") < S._iso(S._since(1)):
            order = [me] + order
    return order[:k]


def status() -> dict:
    """For the Platforms tab: is a search key set, how much of today's budget is left, the last error."""
    prov = provider()
    return {"provider": prov[0] if prov else None, "left": searches_left() if prov else 0, "daily": _daily(),
            "error": db.kv_get("social:search_error"), "x_api": bool(os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN"))}


def run(socials: dict) -> int:
    t0, n = time.time(), 0
    budget = {"x": ENRICH_X, "x_fail": 0, "fb": ENRICH_FB}
    hs_of = {b.name: {k: v for k, v in (socials.get(b.name) or {}).items() if isinstance(v, str)} for b in settings.brands}
    # free part, every company: links the other platforms collected, Facebook followers once a week
    for b in settings.brands:
        if time.time() - t0 > S.STEP_SECONDS / 2:
            break
        hs = hs_of[b.name]
        n += _harvest(b, hs, budget)
        if (db.kv_get(f"social:index_profiles:{b.name}") or "") < S._iso(S._since(PROFILE_EVERY_D)):
            _fb_profile(b, hs)
            db.kv_set(f"social:index_profiles:{b.name}", S._iso(None))
    # search part: a share of today's searches, three per company (X, Instagram, Facebook)
    done, picked = 0, []
    if provider():
        share = min(searches_left(), max(3, _daily() // RUN_SHARE))
        picked = _rotation(max(0, share // 3))
        try:
            for b in picked:
                if time.time() - t0 > S.STEP_SECONDS:
                    break
                hs = hs_of[b.name]
                first = not db.kv_get(f"social:index_seen:{b.name}")
                n += _x(b, hs, budget, first)
                n += _instagram(b, hs, first)
                n += _facebook(b, hs, budget, first)
                db.kv_set(f"social:index_seen:{b.name}", S._iso(None))
                if b is not settings.self_brand:
                    done += 1
        except Refused as e:
            log.info("social index: search stopped: %s", e)
        finally:
            db.kv_set("social:index_offset", int(db.kv_get("social:index_offset") or 0) + done)
    if not picked and (os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")):
        brands = list(settings.brands)
        off = int(db.kv_get("social:xapi_offset") or 0) % max(1, len(brands))
        picked = (brands[off:] + brands[:off])[:3]
        db.kv_set("social:xapi_offset", off + 3)
    n += _x_api(picked, socials)
    db.kv_set("social:index_info", {**status(), "at": db.now()})   # the hosted page has no keys: it reads this
    return n
