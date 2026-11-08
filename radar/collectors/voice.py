"""Voice of the customer: what buyers say about every brand, in their own words, from four public streams.

  youtube   comments under the newest / biggest videos about each brand (YouTube's own innertube endpoint, no key)
  appstore  App Store reviews of the companion apps in the India store (Apple's public review RSS; ids in companies.yaml)
  play      Google Play reviews of the companion apps (the store's batchexecute endpoint; package ids in competitors.yaml)
  amazon    the reviews shown on the Amazon.in product pages of each brand's most-rated listings (collectors/channels.py)
Judge.me reviews from the brands' own Shopify stores (collectors/reviews.py) join them in the `voice` table too, so
the Reviews page and the reports read one table. Every item gets a sentiment score (-1..1: rating when there is one,
else a lexicon that knows Hinglish) and themes (battery, accuracy, app, build, comfort, sound, service, value ...).

Events: voice_wave   >= 5 negative items on one theme for one brand in 7 days (what to advertise against, or fix)
        praise_wave  >= 8 positive items on one theme for a rival in 7 days (what they are winning on)
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timedelta, timezone
from .. import db, schema_ext
from ..config import settings
from ..relevance import relevant
from .base import get, tracked, log

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

THEMES = [
    ("battery",   r"battery|charg|backup|drain|standby|playtime|play time"),
    ("accuracy",  r"accura|inaccura|heart ?rate|\bhr\b|spo2|oxygen|step ?count|steps|sleep (track|data|score)|calorie|\bgps\b|distance|readings?"),
    ("app",       r"\bapp\b|sync|bluetooth|connect|disconnect|pair|update|bug|crash|login|firmware|notification"),
    ("build",     r"build|broke|break|crack|scratch|flimsy|loose|durab|cheap (plastic|build|quality)|quality|stopped working|dead|defect"),
    ("comfort",   r"comfort|\bfit\b|fits|tight|loose|light ?weight|heavy|bulky|strap|rash|irritat|size|sizing"),
    ("display",   r"display|screen|bright|touch|always[- ]on|\baod\b|resolution|visib"),
    ("sound",     r"sound|bass|treble|audio|\bmic\b|call quality|\banc\b|noise cancel|volume|latency|clarity"),
    ("service",   r"service|warranty|support|refund|replace|return|customer care|complain|helpline|ticket|response"),
    ("value",     r"value for money|worth|overpriced|expensive|cheap\b|price|paisa|vasool|budget|affordable|cost"),
    ("delivery",  r"deliver|courier|packag|late|arrived|shipping|seal|box"),
    ("design",    r"design|look|premium|stylish|colou?r|finish|aesthetic|beautiful|ugly"),
    ("water",     r"water|sweat|swim|shower|ip6|ip67|ip68|resist"),
    ("features",  r"feature|function|mode|gesture|voice assist|alexa|siri|nfc|payment|call(ing)? feature|menstr|stress|hrv|recovery|readiness"),
]
_THEME_RX = [(t, re.compile(rx, re.I)) for t, rx in (settings.voice_themes or THEMES)]  # a sector can bring its own themes
POS = re.compile(r"\b(love|loved|loving|great|awesome|excellent|amazing|perfect|best|good|nice|superb|fantastic|brilliant|happy|satisfied|"
                 r"recommend|worth|impressed|accurate|smooth|fast|premium|solid|comfortable|value for money|mast|badhiya|bad(h)?iya|"
                 r"achha|accha|acha|zabardast|kamaal|paisa vasool|shandaar|op\b|🔥|❤|👍|😍)\b|🔥|❤|👍|😍", re.I)
NEG = re.compile(r"\b(bad|worst|poor|terrible|horrible|awful|useless|waste|disappoint\w*|broke\w*|broken|defect\w*|fake|scam|cheat\w*|"
                 r"refund|return\w*|never|not working|stopped|dead|issue|issues|problem|problems|fail\w*|slow|lag\w*|inaccura\w*|wrong|"
                 r"pathetic|regret|frustrat\w*|annoy\w*|hate|bakwas|bekar|bekaar|kharab|faltu|ghatiya|fraud|third class|not worth|"
                 r"overpriced|dont buy|don't buy|do not buy|avoid|👎|😡|😠)\b|👎|😡|😠", re.I)


def sentiment(text: str, rating: float | None) -> float:
    """-1..1. A star rating is the buyer's own verdict; free text falls back to the lexicon."""
    if rating:
        return round((float(rating) - 3) / 2, 2)
    t = text or ""
    p, n = len(POS.findall(t)), len(NEG.findall(t))
    if not p and not n:
        return 0.0
    return round((p - n) / (p + n), 2)


def themes_of(text: str) -> list[str]:
    return [t for t, rx in _THEME_RX if rx.search(text or "")]


def store(source: str, item_key: str, brand: str, ref: str, ref_title: str, author: str, rating: float | None, text: str,
          created: str, likes: int = 0, url: str = "") -> bool:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text and not rating:
        return False
    with db.tx() as c:
        ins = c.execute("INSERT OR IGNORE INTO voice(source,item_key,brand,ref,ref_title,author,rating,text,created,likes,sentiment,themes,url,seen_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (source, item_key, brand, ref, (ref_title or "")[:200], (author or "")[:80], rating, text[:1500], created or db.now()[:10],
                         int(likes or 0), sentiment(text, rating), json.dumps(themes_of(text)), url, db.now())).rowcount
    return bool(ins)


# ---------------------------------------------------------------- YouTube comments (innertube)
YT = "https://www.youtube.com/youtubei/v1/next"
CTX = {"context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00", "hl": "en", "gl": "IN"}}}
_REL = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)


def _rel_date(s: str) -> str:
    m = _REL.search(s or "")
    if not m:
        return db.now()[:10]
    n, unit = int(m.group(1)), m.group(2).lower()
    days = {"second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _yt_post(body: dict) -> dict | None:
    from .base import session
    r = session.post(YT, params={"prettyPrint": "false"}, json={**CTX, **body}, timeout=30,
                     headers={"User-Agent": CHROME_UA, "Content-Type": "application/json", "Accept-Language": "en-IN,en;q=0.9"})
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _find(o, key):
    if isinstance(o, dict):
        if key in o:
            yield o[key]
        for v in o.values():
            yield from _find(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _find(v, key)


def youtube_comments(video_id: str, pages: int = 2) -> list[dict]:
    d = _yt_post({"videoId": video_id})
    if not d:
        return []
    token = None
    for sec in _find(d, "itemSectionRenderer"):
        if sec.get("sectionIdentifier") == "comment-item-section":
            token = next(_find(sec, "token"), None)
            break
    out = []
    for _ in range(pages):
        if not token:
            break
        time.sleep(1.5)
        d = _yt_post({"continuation": token})
        if not d:
            break
        for e in _find(d, "commentEntityPayload"):
            props, author, bar = e.get("properties") or {}, e.get("author") or {}, e.get("toolbar") or {}
            text = ((props.get("content") or {}).get("content")) or ""
            likes = re.sub(r"[^\dKM.]", "", bar.get("likeCountNotliked") or "0")
            mult = 1000 if likes.endswith("K") else 1_000_000 if likes.endswith("M") else 1
            try:
                lk = int(float(likes.rstrip("KM") or 0) * mult)
            except ValueError:
                lk = 0
            out.append({"id": props.get("commentId"), "text": text, "author": author.get("displayName", ""),
                        "published": _rel_date(props.get("publishedTime", "")), "likes": lk})
        token = None
        for cmd in _find(d, "continuationCommand"):
            if isinstance(cmd, dict) and cmd.get("token") and "replies" not in json.dumps(cmd)[:200]:
                token = cmd["token"]
                break
    return out


def run_youtube(per_brand: int = 3) -> int:
    n = 0
    done = set(db.kv_get("voice_yt_done") or [])
    for b in settings.brands:
        vids = db.q("SELECT video_id, title, channel, views, published FROM videos WHERE brand=? ORDER BY seen_at DESC, views DESC LIMIT 12", (b.name,))
        picked = [v for v in vids if v["video_id"] not in done][:per_brand]
        for v in picked:
            try:
                comments = youtube_comments(v["video_id"])
            except Exception as e:  # noqa: BLE001
                log.info("yt comments %s failed: %s", v["video_id"], e)
                comments = []
            done.add(v["video_id"])
            for c in comments:
                if not c["id"] or len(c["text"]) < 8:
                    continue
                if store("youtube", c["id"], b.name, v["video_id"], v["title"], c["author"], None, c["text"], c["published"], c["likes"],
                         f"https://www.youtube.com/watch?v={v['video_id']}&lc={c['id']}"):
                    n += 1
    db.kv_set("voice_yt_done", sorted(done)[-2000:])
    return n


# ---------------------------------------------------------------- App Store reviews (RSS)
def appstore_reviews(app_id: int, page: int = 1) -> list[dict]:
    r = get(f"https://itunes.apple.com/in/rss/customerreviews/page={page}/id={app_id}/sortBy=mostRecent/json", timeout=25, min_gap=1.0)
    if r.status_code != 200:
        return []
    try:
        feed = r.json().get("feed") or {}
    except ValueError:
        return []
    out = []
    for e in feed.get("entry") or []:
        if not isinstance(e, dict) or "im:rating" not in e:
            continue
        lab = lambda k: (e.get(k) or {}).get("label", "") if isinstance(e.get(k), dict) else ""  # noqa: E731
        out.append({"id": lab("id"), "rating": float(lab("im:rating") or 0) or None, "title": lab("title"), "text": lab("content"),
                    "author": ((e.get("author") or {}).get("name") or {}).get("label", ""), "created": lab("updated")[:10], "version": lab("im:version")})
    return out


def run_appstore() -> int:
    from . import resolve
    n = 0
    for b in settings.brands:
        for app_id in resolve.ids(b.name)["ios"]:
            for page in (1, 2):
                try:
                    revs = appstore_reviews(int(app_id), page)
                except Exception as e:  # noqa: BLE001
                    log.info("appstore reviews %s failed: %s", app_id, e)
                    break
                new = 0
                for rv in revs:
                    if store("appstore", rv["id"], b.name, f"ios:{app_id}", f"App Store · {b.name}", rv["author"], rv["rating"],
                             f"{rv['title']}. {rv['text']}".strip(". "), rv["created"], 0, f"https://apps.apple.com/in/app/id{app_id}?see-all=reviews"):
                        new += 1
                n += new
                if not revs or (page == 1 and new < len(revs) // 2):
                    break  # page 2 only when page 1 was mostly new
    return n


# ---------------------------------------------------------------- Google Play reviews (batchexecute)
def play_reviews(package: str, count: int = 40, token: str | None = None) -> tuple[list[dict], str | None]:
    from urllib.parse import quote
    from .base import session
    req = [None, None, [2, 2, [count, token]], [package, 7]]
    body = "f.req=" + quote(json.dumps([[["UsvDTd", json.dumps(req), None, "generic"]]]))
    r = session.post("https://play.google.com/_/PlayStoreUi/data/batchexecute", params={"rpcids": "UsvDTd", "hl": "en", "gl": "IN"},
                     data=body, headers={"User-Agent": CHROME_UA, "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}, timeout=30)
    if r.status_code != 200:
        return [], None
    line = next((ln for ln in r.text.splitlines() if '"wrb.fr"' in ln and "UsvDTd" in ln), None)
    if not line:
        return [], None
    try:
        inner = json.loads(json.loads(line)[0][2])
    except (ValueError, IndexError, TypeError):
        return [], None
    out = []
    for rv in inner[0] or []:
        try:
            ts = rv[5][0] if isinstance(rv[5], list) else None
            out.append({"id": rv[0], "author": (rv[1] or [""])[0], "rating": float(rv[2]) if rv[2] else None, "text": rv[4] or "",
                        "created": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else db.now()[:10],
                        "likes": int(rv[6] or 0), "version": rv[10] if len(rv) > 10 else ""})
        except (IndexError, TypeError):
            continue
    nxt = inner[1][1] if len(inner) > 1 and inner[1] else None
    return out, nxt


def run_play() -> int:
    from . import resolve
    n = 0
    for b in settings.brands:
        for pkg in resolve.ids(b.name)["apps"]:
            try:
                revs, _ = play_reviews(pkg)
            except Exception as e:  # noqa: BLE001
                log.info("play reviews %s failed: %s", pkg, e)
                continue
            for rv in revs:
                if store("play", rv["id"], b.name, pkg, f"Google Play · {b.name}", rv["author"], rv["rating"], rv["text"], rv["created"], rv["likes"],
                         f"https://play.google.com/store/apps/details?id={pkg}&showAllReviews=true"):
                    n += 1
            time.sleep(1.0)
    return n


# ---------------------------------------------------------------- Amazon product-page reviews
def amazon_reviews(asin: str) -> list[dict]:
    r = get(f"https://www.amazon.in/dp/{asin}", timeout=30, min_gap=3.0, headers={"User-Agent": CHROME_UA, "Accept": "text/html"})
    if r.status_code != 200:
        return []
    out = []
    for blk in re.split(r'(?=<div[^>]*data-hook="review"[^>]*>)', r.text)[1:]:
        rid = re.search(r'id="([A-Z0-9]{10,16})"', blk[:300])
        star = re.search(r"(\d(?:\.\d)?) out of 5 stars", blk)
        title = re.search(r'data-hook="review-title"[^>]*>(.*?)</(?:a|h5|span)>', blk, re.S) or re.search(r'data-hook="reviewTitle"[^>]*>(.*?)</h5>', blk, re.S)
        # two layouts: the classic review-body span and the 2026 card layout (reviewRichContentContainer)
        body = (re.search(r'data-hook="review-body"[^>]*>(.*?)</div>', blk, re.S)
                or re.search(r'data-hook="reviewRichContentContainer"[^>]*>(.*?)</div>', blk, re.S))
        date = re.search(r'data-hook="review-date"[^>]*>[^<]*?on ([^<]+)<', blk)
        author = re.search(r'class="a-profile-name">([^<]+)<', blk)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", (body.group(1) if body else ""))).strip()
        ttl = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", (title.group(1) if title else ""))).strip()
        if not (star or text):
            continue
        created = db.now()[:10]
        if date:
            for fmt in ("%d %B %Y", "%B %d, %Y"):
                try:
                    created = datetime.strptime(date.group(1).strip(), fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
        out.append({"id": rid.group(1) if rid else f"{asin}:{created}:{text[:30]}", "rating": float(star.group(1)) if star else None,
                    "text": f"{ttl}. {text}".strip(". "), "author": author.group(1) if author else "", "created": created})
    return out


def run_amazon(per_brand: int = 2) -> int:
    n = 0
    done = db.kv_get("voice_amz_done") or {}
    today = db.now()[:10]
    for b in settings.brands:
        tops = db.q("SELECT item_key, title FROM channel_listings WHERE channel='amazon' AND brand=? ORDER BY COALESCE(rating_count,0) DESC LIMIT ?",
                    (b.name, per_brand))
        for t in tops:
            if done.get(t["item_key"]) == today:
                continue
            try:
                revs = amazon_reviews(t["item_key"])
            except Exception as e:  # noqa: BLE001
                log.info("amazon reviews %s failed: %s", t["item_key"], e)
                revs = []
            done[t["item_key"]] = today
            for rv in revs:
                if store("amazon", rv["id"], b.name, t["item_key"], t["title"], rv["author"], rv["rating"], rv["text"], rv["created"], 0,
                         f"https://www.amazon.in/dp/{t['item_key']}#customerReviews"):
                    n += 1
    db.kv_set("voice_amz_done", dict(list(done.items())[-400:]))
    return n


# ---------------------------------------------------------------- Judge.me rows already in `reviews` -> voice
def sync_judgeme() -> int:
    n = 0
    rows = db.q("SELECT r.brand, r.product_id, r.review_key, r.rating, r.title, r.body, r.created, p.title ptitle, p.handle FROM reviews r "
                "LEFT JOIN products p ON p.brand=r.brand AND p.product_id=r.product_id WHERE NOT EXISTS "
                "(SELECT 1 FROM voice v WHERE v.source='judgeme' AND v.item_key=r.brand||'|'||r.review_key) LIMIT 2000")
    for r in rows:
        b = settings.brand(r["brand"])
        if store("judgeme", f"{r['brand']}|{r['review_key']}", r["brand"], str(r["product_id"]), r["ptitle"] or "", "", r["rating"],
                 f"{r['title'] or ''}. {r['body'] or ''}".strip(". "), (r["created"] or "")[:10], 0, b.product_url(r["handle"]) if b else ""):
            n += 1
    return n


# ---------------------------------------------------------------- waves
def waves() -> int:
    n, me = 0, settings.self_brand.name if settings.self_brand else ""
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    agg: dict[tuple, dict] = {}
    for r in db.q("SELECT brand, themes, sentiment, text, source, url FROM voice WHERE created>=?", (since,)):
        for t in json.loads(r["themes"] or "[]"):
            k = (r["brand"], t, "neg" if r["sentiment"] <= -0.3 else "pos" if r["sentiment"] >= 0.3 else "mixed")
            a = agg.setdefault(k, {"n": 0, "quotes": [], "sources": set()})
            a["n"] += 1
            a["sources"].add(r["source"])
            if len(a["quotes"]) < 3:
                a["quotes"].append(r["text"][:140])
    for (brand, theme, pol), a in agg.items():
        if pol == "neg" and a["n"] >= 5:
            own = brand == me
            if db.add_event(brand, "voice", "voice_wave", f"{brand}: {a['n']} complaints about {theme} this week",
                            detail=("Pebble's own buyers are unhappy about this; route to product/support." if own else
                                    f"Rival weakness buyers talk about. Quote: \"{a['quotes'][0]}\"") + f" Sources: {', '.join(sorted(a['sources']))}.",
                            url="", data={"theme": theme, "count": a["n"], "polarity": "negative", "quotes": a["quotes"], "sources": sorted(a["sources"])},
                            dedupe_key=f"voice|{brand}|{theme}|neg|{week}"):
                n += 1
        elif pol == "pos" and a["n"] >= 8 and brand != me:
            if db.add_event(brand, "voice", "praise_wave", f"{brand}: buyers praise its {theme} ({a['n']} positive mentions this week)",
                            detail=f"What the rival is winning on; Pebble's claim here must be at least as strong. Quote: \"{a['quotes'][0]}\"",
                            url="", data={"theme": theme, "count": a["n"], "polarity": "positive", "quotes": a["quotes"], "sources": sorted(a["sources"])},
                            dedupe_key=f"voice|{brand}|{theme}|pos|{week}"):
                n += 1
    return n


@tracked("voice")
def run() -> int:
    schema_ext.ensure()
    n = 0
    for step in (sync_judgeme, run_appstore, run_play, run_youtube, run_amazon):
        try:
            got = step()
            log.info("voice %s -> %s items", step.__name__, got)
        except Exception as e:  # noqa: BLE001
            log.exception("voice step %s failed: %s", step.__name__, e)
    try:
        n += waves()
    except Exception as e:  # noqa: BLE001
        log.exception("voice waves failed: %s", e)
    return n
