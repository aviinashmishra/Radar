"""Distribution sensor: where each brand is sold, at what price, with what rating.

Reads the public search results of the channels in config/channels.yaml:
  amazon    www.amazon.in/s?k=...            ASIN, title, price, MRP, rating, rating count, sponsored flag, position
  flipkart  www.flipkart.com/search?q=...    product id, title, price, MRP, rating, rating count (from the page state)
  blinkit   blinkit.com/v1/layout/search     per city (lat/lon): product id, price, MRP, inventory, rating; the shelf is
                                             hyperlocal, so each city is read on its own (collectors/qcommerce.py, daily)
Every listing is attributed to a tracked brand with radar/relevance.py (a search for "boAt earbuds" also returns Noise
and Boult; each goes to its own brand) and kept in channel_listings with a price/stock/rating trail in channel_history.
Diffs become events; the first read of a (channel, city, query) is a baseline and raises none.

  channel_listed        a new listing of a tracked brand appeared in the results
  channel_delisted      a listing has not been seen for `delisted_after_days` although its query still runs
  channel_price_drop    price fell >= price_move_pct   (channel_price_rise for the other way)
  channel_oos / channel_back   out of stock / back in stock on the channel
  channel_rating_drop   rating fell by 0.2 or more
  channel_undercut      the marketplace sells the brand's own product >= undercut_pct below the brand's D2C store price
  qc_expansion          a brand's products turned up on a quick-commerce shelf in a city for the first time
Retailers that block automated reads (Croma, Reliance Digital) and app-only quick commerce (Zepto, Instamart) get
their presence from news / press / social mentions instead (`scan_mentions`, run daily).
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import yaml
from .. import db, schema_ext
from ..analytics import categorize
from ..config import settings, CONFIG_PATH
from ..relevance import attribute
from .base import get, tracked, log

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def config() -> dict:
    p = Path(CONFIG_PATH).parent / "channels.yaml"
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def channels() -> list[dict]:
    return config().get("channels") or []


def cities() -> list[dict]:
    return config().get("cities") or []


def _cfg(key: str, default):
    return config().get(key, default)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def queries_for(brand, *, quick: bool = False) -> list[str]:
    """Search phrases for one brand: "{brand} {segment}" per segment it sells in, then the bare brand."""
    words = _cfg("segment_words", {}) or {}
    terms = brand.search_terms or [brand.name]
    out: list[str] = []
    for tpl in _cfg("queries", ["{brand} {segment}", "{brand}"]):
        if "{segment}" in tpl:
            for seg in brand.segments:
                w = words.get(seg)
                if w and any(sg["key"] == seg and sg.get("show") for sg in settings.segments):
                    out.append(tpl.replace("{brand}", terms[0]).replace("{segment}", w))
        else:
            out.append(tpl.replace("{brand}", terms[0]))
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    n = 1 if quick else int(_cfg("marketplace_queries_per_brand", 2))
    return uniq[:n]


# ---------------------------------------------------------------- readers
class Blocked(RuntimeError):
    """The marketplace answered with a throttle page (503 / captcha): nothing was read, so nothing must be marked as read."""


def _clean_title(t: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", t)).strip()[:200]


def _amazon_brand(block: str) -> str:
    """The brand line Amazon shows above the title (its own short <h2 class="a-size-mini">): empty when absent."""
    for m in re.findall(r"<h2[^>]*a-size-mini[^>]*>(.*?)</h2>", block, re.S):
        t = _clean_title(m)
        if 1 <= len(t.split()) <= 3 and len(t) <= 30:
            return t
    return ""


def _amazon_title(block: str) -> str:
    """The product title of one search-result block. Amazon's 2026 layout puts the brand in its own <h2> and the full
    title in the aria-label of the title <h2> (prefixed "Sponsored Ad - " for ads); older layouts keep it in the span.
    Take the longest candidate across h2 aria-labels, h2 spans and the image alt."""
    cands = re.findall(r'<h2[^>]*aria-label="([^"]+)"', block)
    cands += [re.sub(r"<[^>]+>", " ", m) for m in re.findall(r"<h2[^>]*>(.*?)</h2>", block, re.S)]
    cands += re.findall(r'<img[^>]*alt="([^"]{20,})"', block)
    best = max((_clean_title(c) for c in cands), key=len, default="")
    best = re.sub(r"^\s*Sponsored Ad\s*-\s*", "", best).strip()
    return best if len(best) > 3 else ""


def read_amazon(query: str) -> list[dict]:
    r = get("https://www.amazon.in/s", params={"k": query}, timeout=30, min_gap=5.0,
            headers={"User-Agent": CHROME_UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-IN,en;q=0.9"})
    if r.status_code != 200 or "captcha" in r.text[:5000].lower() or "api-services-support" in r.text[:3000]:
        raise Blocked(f"amazon {r.status_code}")
    blocks = re.split(r'(?=<div[^>]*data-component-type="s-search-result")', r.text)[1:]
    if not blocks and len(r.text) < 20000 and "no results" not in r.text.lower():
        # soft block: HTTP 200 with a ~2 KB blank page (no results grid, no "No results for" copy)
        raise Blocked(f"amazon soft block ({len(r.text)} bytes)")
    out = []
    for pos, it in enumerate(blocks, 1):
        asin = re.search(r'data-asin="([A-Z0-9]{10})"', it)
        title = _amazon_title(it)
        if not asin or not title:
            continue
        brand_hint = _amazon_brand(it)
        if brand_hint and brand_hint.lower() not in title.lower():
            title = f"{brand_hint} {title}"[:200]  # the 2026 layout keeps the brand out of the title text
        price = re.search(r'<span class="a-price-whole">([\d,]+)', it)
        mrp = re.search(r'<span class="a-price a-text-price"[^>]*>.*?<span class="a-offscreen">₹([\d,]+)', it, re.S)
        rating = re.search(r'<span class="a-icon-alt">([\d.]+) out of 5', it)
        cnt = re.search(r'aria-label="([\d,]+) ratings?"', it) or re.search(r'<span class="a-size-base s-underline-text">\(?([\d,]+)', it)
        out.append({"item_key": asin.group(1), "title": title, "brand_hint": brand_hint,
                    "price": float(price.group(1).replace(",", "")) if price else None,
                    "mrp": float(mrp.group(1).replace(",", "")) if mrp else None,
                    "available": 1 if price else 0, "rating": float(rating.group(1)) if rating else None,
                    "rating_count": int(cnt.group(1).replace(",", "")) if cnt else None,
                    "url": f"https://www.amazon.in/dp/{asin.group(1)}", "sponsored": int("Sponsored" in it), "position": pos})
    return out


def _walk(o, pred):
    if isinstance(o, dict):
        if pred(o):
            yield o
        for v in o.values():
            yield from _walk(v, pred)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, pred)


def read_flipkart(query: str) -> list[dict]:
    r = get("https://www.flipkart.com/search", params={"q": query}, timeout=30, min_gap=2.5,
            headers={"User-Agent": CHROME_UA, "Accept": "text/html,application/xhtml+xml"})
    if r.status_code in (403, 429, 503):
        raise Blocked(f"flipkart {r.status_code}")
    if r.status_code != 200:
        return []
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>", r.text, re.S)
    if not m:
        return []
    try:
        state = json.loads(m.group(1))
    except ValueError:
        return []
    out, seen = [], set()
    for pos, p in enumerate(_walk(state, lambda o: "productId" in o and "pricing" in o and o.get("title")), 1):
        pid = p["productId"]
        if pid in seen:
            continue
        seen.add(pid)
        pr = p.get("pricing") or {}
        price = ((pr.get("finalPrice") or {}).get("value"))
        mrp = ((pr.get("mrp") or {}).get("value"))
        out.append({"item_key": pid, "title": str(p.get("title") or "")[:200], "price": float(price) if price else None,
                    "mrp": float(mrp) if mrp else None, "available": 1 if price else 0,
                    "rating": float(p["averageRating"]) if p.get("averageRating") else None,
                    "rating_count": int(p["totalRatingCount"]) if p.get("totalRatingCount") else None,
                    "url": f"https://www.flipkart.com/-/p/{p.get('itemId') or pid}?pid={pid}", "sponsored": 0, "position": pos})
    return out


def _post(url: str, *, min_gap: float = 1.5, **kw):
    """POST twin of base.get (same session, same per-host pacing)."""
    from .base import session, _last_hit
    import time
    host = url.split("//", 1)[-1].split("/", 1)[0]
    wait = _last_hit.get(host, 0) + min_gap - time.time()
    if wait > 0:
        time.sleep(wait)
    r = session.post(url, timeout=30, **kw)
    _last_hit[host] = time.time()
    return r


def read_blinkit(query: str, city: dict) -> list[dict]:
    r = _post("https://blinkit.com/v1/layout/search", params={"q": query}, json={},
              headers={"User-Agent": CHROME_UA, "lat": str(city["lat"]), "lon": str(city["lon"]), "app_client": "consumer_web",
                       "Accept": "application/json"})
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    out, seen = [], set()
    # the tracking payload carries the flat fields (brand, price, mrp, inventory, rating); the card carries the display name
    for pos, p in enumerate(_walk(data, lambda o: "product_id" in o and "price" in o and "name" in o and isinstance(o.get("name"), str)), 1):
        pid = str(p["product_id"])
        if pid in seen:
            continue
        seen.add(pid)
        inv = p.get("inventory")
        out.append({"item_key": pid, "title": str(p.get("name") or "")[:200], "brand_hint": p.get("brand") or "",
                    "price": float(p["price"]) if p.get("price") not in (None, "") else None,
                    "mrp": float(p["mrp"]) if p.get("mrp") not in (None, "") else None,
                    "available": 1 if (inv is None or int(inv or 0) > 0) else 0,
                    "rating": float(p["rating"]) if p.get("rating") not in (None, "", 0, "0") else None, "rating_count": None,
                    "url": f"https://blinkit.com/prn/x/prid/{pid}", "sponsored": int(bool(p.get("ads_campaign_id"))), "position": pos})
    return out


READERS = {"amazon": read_amazon, "flipkart": read_flipkart}


# ---------------------------------------------------------------- attribution and diffing
ACCESSORY_RX = re.compile(r"compatible with|compatible for|replacement|\bstraps?\b|\bbelt\b|screen (guard|protector)|protector|\bcase\b|\bcover\b|"
                          r"charging (cable|dock|cradle|cord)|\bcharger for\b|sizing kit|device not included|\bfor\s+\S+\s+(smart ?watch|band|ring|buds|earbuds)\b|"
                          r"\badapter\b|\bholder\b|\bstand\b", re.I)


def _owner(listing: dict, searched) -> str | None:
    """Which tracked brand this listing belongs to (the searched brand first, then any other tracked brand).
    Third-party straps, cases and "compatible with" accessories belong to nobody: a ₹186 strap for a Pebble watch is not
    a Pebble listing, and the brand's own sizing kits and cables are not the products the matrix is about."""
    if ACCESSORY_RX.search(listing["title"]) or categorize("", listing["title"]) == "Accessory":
        return None
    hint = _norm(listing.get("brand_hint") or "")
    if hint:
        for b in settings.brands:
            if hint == _norm(b.name) or hint in {_norm(a) for a in b.aliases}:
                return b.name
    title = listing["title"]
    from ..relevance import relevant
    if searched and relevant(title, searched, strict=True):
        return searched.name
    b = attribute(title, strict=True)
    return None if b == "Sector" else b


def _monthly_key(iso: str) -> str:
    return iso[:7]


def ingest(channel: dict, city: str, query: str, listings: list[dict], searched=None) -> int:
    """Store the listings of one search and raise events for what changed. Returns new events."""
    n, now = 0, db.now()
    key = f"channel_seen:{channel['key']}|{city}|{query.lower()}"
    first = not db.kv_get(key)
    pct_min, under_pct = float(_cfg("price_move_pct", 8)), float(_cfg("undercut_pct", 8))
    cap = int(_cfg("max_listings_per_query", 20))
    own = {}  # brand -> own-store products, for the undercut check
    for ls in listings[:cap]:
        brand = _owner(ls, searched)
        if not brand:
            continue
        prev = db.q1("SELECT * FROM channel_listings WHERE channel=? AND city=? AND item_key=?", (channel["key"], city, ls["item_key"]))
        cat = categorize("", ls["title"])
        with db.tx() as c:
            c.execute("INSERT INTO channel_listings(channel,city,item_key,brand,title,price,mrp,available,rating,rating_count,url,query,sponsored,"
                      "position,category,first_seen,last_seen,seen_runs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
                      "ON CONFLICT(channel,city,item_key) DO UPDATE SET brand=excluded.brand, title=excluded.title, price=excluded.price, "
                      "mrp=excluded.mrp, available=excluded.available, rating=COALESCE(excluded.rating, rating), "
                      "rating_count=COALESCE(excluded.rating_count, rating_count), url=excluded.url, query=excluded.query, "
                      "sponsored=excluded.sponsored, position=excluded.position, last_seen=excluded.last_seen, seen_runs=seen_runs+1",
                      (channel["key"], city, ls["item_key"], brand, ls["title"], ls["price"], ls["mrp"], ls["available"], ls["rating"],
                       ls["rating_count"], ls["url"], query, ls["sponsored"], ls["position"], cat, now, now))
            if not prev or prev["price"] != ls["price"] or prev["available"] != ls["available"] or (ls["rating"] and prev["rating"] != ls["rating"]) \
                    or (ls["rating_count"] and prev["rating_count"] != ls["rating_count"]):
                c.execute("INSERT INTO channel_history(channel,city,item_key,price,available,rating,rating_count,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                          (channel["key"], city, ls["item_key"], ls["price"], ls["available"], ls["rating"], ls["rating_count"], now))
        where = f"{channel['name']}" + (f" ({city})" if city else "")
        data = {"channel": channel["key"], "city": city, "item_key": ls["item_key"], "price": ls["price"], "mrp": ls["mrp"],
                "rating": ls["rating"], "rating_count": ls["rating_count"], "query": query, "product_type": cat, "position": ls["position"]}
        if first:
            continue  # baseline for this shelf
        if not prev:
            if channel.get("type") == "quick_commerce" and not db.q1("SELECT 1 FROM channel_listings WHERE channel=? AND city=? AND brand=? AND item_key<>?",
                                                                      (channel["key"], city, brand, ls["item_key"])):
                if db.add_event(brand, "channels", "qc_expansion", f"{brand} is now on {where}: {ls['title'][:80]}",
                                detail=f"First {brand} product seen on this quick-commerce shelf in {city}" + (f" at ₹{ls['price']:,.0f}" if ls["price"] else "") + ".",
                                url=ls["url"], data=data, dedupe_key=f"qc|{channel['key']}|{city}|{brand}"):
                    n += 1
            elif db.add_event(brand, "channels", "channel_listed", f"{brand} listed on {where}: {ls['title'][:80]}" + (f" at ₹{ls['price']:,.0f}" if ls["price"] else ""),
                              detail=f"New listing in the results for \"{query}\"" + (f", rating {ls['rating']} from {ls['rating_count']:,}" if ls["rating"] and ls["rating_count"] else "") + ".",
                              url=ls["url"], data=data, dedupe_key=f"chl|{channel['key']}|{city}|{ls['item_key']}"):
                n += 1
        else:
            if prev["price"] and ls["price"] and prev["price"] != ls["price"]:
                pct = 100 * (ls["price"] - prev["price"]) / prev["price"]
                if abs(pct) >= pct_min:
                    kind = "channel_price_drop" if pct < 0 else "channel_price_rise"
                    if db.add_event(brand, "channels", kind, f"{ls['title'][:70]} on {where}: ₹{prev['price']:,.0f} -> ₹{ls['price']:,.0f} ({pct:+.0f}%)",
                                    detail=f"Marketplace price move; MRP ₹{ls['mrp']:,.0f}." if ls["mrp"] else "Marketplace price move.",
                                    url=ls["url"], data={**data, "old": prev["price"], "new": ls["price"], "pct": round(pct, 1)},
                                    dedupe_key=f"chp|{channel['key']}|{city}|{ls['item_key']}|{ls['price']}"):
                        n += 1
            if prev["available"] and not ls["available"]:
                if db.add_event(brand, "channels", "channel_oos", f"{ls['title'][:70]} out of stock on {where}", detail="Demand on the channel is unmet right now.",
                                url=ls["url"], data=data, dedupe_key=f"choos|{channel['key']}|{city}|{ls['item_key']}|{now[:10]}"):
                    n += 1
            elif not prev["available"] and ls["available"]:
                if db.add_event(brand, "channels", "channel_back", f"{ls['title'][:70]} back in stock on {where}", url=ls["url"], data=data,
                                dedupe_key=f"chback|{channel['key']}|{city}|{ls['item_key']}|{now[:10]}"):
                    n += 1
            if prev["rating"] and ls["rating"] and prev["rating"] - ls["rating"] >= 0.2:
                if db.add_event(brand, "channels", "channel_rating_drop", f"{ls['title'][:70]} rating fell {prev['rating']} -> {ls['rating']} on {where}",
                                detail="Buyers on the marketplace are turning; read the newest reviews on the Reviews page.", url=ls["url"], data=data,
                                dedupe_key=f"chrat|{channel['key']}|{city}|{ls['item_key']}|{ls['rating']}"):
                    n += 1
        # channel conflict: the marketplace sells the brand's own product below its D2C price
        if ls["price"] and channel.get("type") == "marketplace":
            if brand not in own:
                own[brand] = db.q("SELECT product_id, title, price, handle FROM products WHERE brand=? AND price>0", (brand,))
            match = _match_own(ls["title"], own[brand])
            if match and match["price"] and ls["price"] <= match["price"] * (1 - under_pct / 100):
                gap = round(100 * (match["price"] - ls["price"]) / match["price"])
                b = settings.brand(brand)
                if db.add_event(brand, "channels", "channel_undercut",
                                f"{channel['name']} sells {brand}'s {match['title'][:50]} at ₹{ls['price']:,.0f}, {gap}% below its own store (₹{match['price']:,.0f})",
                                detail="Channel conflict: the marketplace listing undercuts the D2C price. For a rival this is where their buyers go; "
                                       "for Pebble it is margin and D2C traffic leaking to the marketplace.",
                                url=ls["url"], data={**data, "store_price": match["price"], "gap_pct": gap, "store_url": b.product_url(match["handle"]) if b else ""},
                                dedupe_key=f"chund|{channel['key']}|{ls['item_key']}|{_monthly_key(now)}"):
                    n += 1
    if first:
        db.kv_set(key, now)
    return n


def _match_own(title: str, products: list[dict]) -> dict | None:
    if not products:
        return None
    from rapidfuzz import fuzz, process
    t = re.sub(r"[\(\[].*?[\)\]]", " ", title).lower()
    best = process.extractOne(t, {p["product_id"]: p["title"].lower() for p in products}, scorer=fuzz.token_set_ratio)
    if not best or best[1] < 82:
        return None
    return next(p for p in products if p["product_id"] == best[2])


def delist_stale(channel_key: str, city: str = "") -> int:
    """Listings not seen for `delisted_after_days` although the channel was read today -> channel_delisted."""
    days = int(_cfg("delisted_after_days", 4))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    n = 0
    for r in db.q("SELECT * FROM channel_listings WHERE channel=? AND city=? AND last_seen<? AND seen_runs>=2", (channel_key, city, cutoff)):
        if not db.kv_get(f"channel_seen:{channel_key}|{city}|{(r['query'] or '').lower()}"):
            continue
        ch = next((c for c in channels() if c["key"] == channel_key), {"name": channel_key})
        if db.add_event(r["brand"], "channels", "channel_delisted", f"{r['title'][:70]} no longer in {ch['name']}{' (' + city + ')' if city else ''} results",
                        detail=f"Last seen {r['last_seen'][:10]} at ₹{r['price']:,.0f}." if r["price"] else f"Last seen {r['last_seen'][:10]}.",
                        url=r["url"], data={"channel": channel_key, "city": city, "item_key": r["item_key"], "last_price": r["price"]},
                        dedupe_key=f"chdel|{channel_key}|{city}|{r['item_key']}|{r['last_seen'][:7]}"):
            n += 1
        with db.tx() as c:
            c.execute("DELETE FROM channel_listings WHERE channel=? AND city=? AND item_key=?", (channel_key, city, r["item_key"]))
    return n


# ---------------------------------------------------------------- mentions (retail presence from the text streams)
def scan_mentions(days: int = 60) -> int:
    """News, press, feed and social events that name a channel next to a brand -> channel_mentions (presence evidence)."""
    pats = [(c, re.compile(c["mention"], re.I)) for c in channels() if c.get("mention")]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    n = 0
    for e in db.q("SELECT id, brand, title, detail, url, created_at, data FROM events WHERE created_at>=? AND source IN ('news','feed','reddit','press','youtube') "
                  "AND brand NOT IN ('Sector','NewEntrant')", (since,)):
        d = json.loads(e["data"] or "{}") if e["data"] else {}
        text = f"{e['title']} {d.get('summary', '')}"
        for c, rx in pats:
            if rx.search(text):
                with db.tx() as cx:
                    n += cx.execute("INSERT OR IGNORE INTO channel_mentions(brand,channel,event_id,title,url,seen_at) VALUES(?,?,?,?,?,?)",
                                    (e["brand"], c["key"], e["id"], e["title"][:200], e["url"], e["created_at"])).rowcount
    return n


# ---------------------------------------------------------------- runs
def _due(brand_name: str, channel_key: str = "") -> bool:
    hrs = float(_cfg("marketplace_refresh_hours", 6))
    last = db.kv_get(f"channel_last:{brand_name}|{channel_key}" if channel_key else f"channel_last:{brand_name}")
    if not last:
        return True
    return datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(hours=hrs)


def _blocked_until(channel_key: str) -> datetime | None:
    v = db.kv_get(f"channel_block_until:{channel_key}")
    return datetime.fromisoformat(v) if v else None


@tracked("channels")
def run() -> int:
    """Marketplaces (Amazon.in, Flipkart) for every brand whose listings are older than marketplace_refresh_hours."""
    schema_ext.ensure()
    n = 0
    now = datetime.now(timezone.utc)
    live = [c for c in channels() if c.get("reader") in READERS and not ((bu := _blocked_until(c["key"])) and bu > now)]
    skipped = {c["key"] for c in channels() if c.get("reader") in READERS} - {c["key"] for c in live}
    if skipped:
        log.info("channels: %s still in throttle back-off, skipped this run", ", ".join(sorted(skipped)))
    blocked: dict[str, int] = {}
    for b in settings.brands:
        for ch in live:
            if ch["key"] in blocked or not _due(b.name, ch["key"]):
                continue
            ok = True
            for q in queries_for(b):
                listings = None
                for attempt in (1, 2):
                    try:
                        listings = READERS[ch["reader"]](q)
                        break
                    except Blocked as e:
                        log.info("%s %r blocked (%s), attempt %d", ch["key"], q, e, attempt)
                        time.sleep(8 * attempt)
                    except Exception as e:  # noqa: BLE001
                        log.info("%s %r failed: %s", ch["key"], q, e)
                        break
                if listings is None:
                    ok = False
                    blocked[ch["key"]] = blocked.get(ch["key"], 0) + 1
                    if blocked[ch["key"]] >= 2:  # two consecutive throttles: give the marketplace 45 minutes and read the rest next run
                        db.kv_set(f"channel_block_until:{ch['key']}", (now + timedelta(minutes=45)).isoformat(timespec="seconds"))
                        log.info("%s: throttled twice, backing off 45 min", ch["key"])
                    break
                if listings:
                    n += ingest(ch, "", q, listings, searched=b)
            if ok:  # only a successfully read (brand, channel) pair counts as refreshed
                db.kv_set(f"channel_last:{b.name}|{ch['key']}", db.now())
                blocked.pop(ch["key"], None)
    for ch in live:
        if ch["key"] not in blocked:
            n += delist_stale(ch["key"])
    return n


def run_quick_commerce() -> int:
    """Blinkit shelves city by city, one query per brand (daily: it is ~12 cities x ~30 brands requests)."""
    schema_ext.ensure()
    n = 0
    ch = next((c for c in channels() if c.get("reader") == "blinkit"), None)
    if not ch:
        return 0
    for city in cities():
        for b in settings.brands:
            for q in queries_for(b, quick=True):
                try:
                    listings = read_blinkit(q, city)
                except Exception as e:  # noqa: BLE001
                    log.info("blinkit %s %r failed: %s", city["name"], q, e)
                    continue
                if listings:
                    n += ingest(ch, city["name"], q, listings, searched=b)
        n += delist_stale(ch["key"], city["name"])
    return n
