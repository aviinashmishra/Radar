"""Google News RSS per brand + sector-wide searches. Free, no key, India edition."""
from __future__ import annotations
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
import feedparser
from .. import db
from ..config import settings
from .base import get, tracked, log

GN = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
MAX_AGE_DAYS = 21
# A headline must look like our sector, otherwise "Noise", "Hammer" or "Pebble" pull in Trump speeches and geology.
import re
SECTOR_RX = re.compile(r"smart ?watch|wearable|earbud|ear ?phone|headphone|neckband|\btws\b|audio|speaker|soundbar|charger|"
                       r"power ?bank|gadget|electronics|smart ?ring|fitness band|bluetooth|\banc\b|launch|unveil|d2c|"
                       r"e-?commerce|shopify|amazon|flipkart|idc|counterpoint|funding|acqui|ipo|nexxbase|imagine marketing|"
                       r"srk powertech|boat|noise colorfit|fire-?boltt|boult|ptron|zebronics|portronics|ambrane|gizmore|"
                       r"crossbeats|corseca|blaupunkt|fastrack|mivi|cultsport|beatxp|pebble", re.I)


# Brand news must name the brand. Google News happily returns "Galaxy S27" for "Fitbit India" and a Norton motorcycle
# for "Urban wearables", so the headline is checked for the brand itself, then for what the story is about.
CATEGORY_RX = re.compile(r"smart ?watch|wearable|earbud|ear ?phone|headphone|neckband|\btws\b|speaker|soundbar|charger|power ?bank|"
                         r"smart ?ring|\brings?\b|fitness|\bbands?\b|tracker|bluetooth|gadget|electronics|\baudio\b|health|sleep|"
                         r"\bd2c\b|\bwatch(es)?\b|\bbuds\b|\banc\b", re.I)
BUSINESS_RX = re.compile(r"funding|raises|\bipo\b|acqui|valuation|revenue|market share|\bidc\b|counterpoint|investor|layoff", re.I)
PRODUCT_TALK_RX = re.compile(r"price|specs|features|battery|review|unbox|\bai\b|₹|\brs\.? ?\d", re.I)
AMBIGUOUS = {"noise", "hammer", "pebble", "urban", "luna", "polar", "boat", "titan", "fire", "ring"}  # everyday English words
GENERIC_HEAD = {"just", "imagine", "google", "the", "galaxy"}                                     # term openers that are not the brand
COLLOCATIONS = re.compile(r"noise[- ]?(cancel\w*|reduc\w*|isolat\w*)|white noise|active noise", re.I)


def relevant(title: str, summary: str, term: str, brand=None) -> bool:
    """Sector searches only need to look like the sector. Brand searches need the brand named in the headline; brands
    whose name is an everyday word (Noise, Urban, boAt) also need product or business context, and global brands
    (tier: adjacent) only count for wearables or business news, not their phones and tablets."""
    if brand is None:
        return bool(SECTOR_RX.search(f"{title} {summary}"))
    text = COLLOCATIONS.sub(" ", title)
    names = {w for w in (brand.name, term.split()[0]) if w.lower() not in GENERIC_HEAD}
    # an internal capital (boAt, BoAt, pTron, beatXP) is unmistakably the brand
    styled = any(t[1:] != t[1:].lower() and t != t.upper() for w in names
                 for t in re.findall(rf"(?<![A-Za-z]){re.escape(w)}(?![a-z])", text, re.I))
    if styled:
        return True
    hit = ambiguous = False
    for w in names:
        if w.lower() in AMBIGUOUS:
            if re.search(rf"(?<![A-Za-z])({re.escape(w.capitalize())}|{re.escape(w.upper())})(?![a-z])", text):
                hit = ambiguous = True
        elif re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", text.lower()):
            hit = True
    if not hit:
        return False
    if brand.tier == "adjacent":
        return bool(CATEGORY_RX.search(text) or BUSINESS_RX.search(text))
    if ambiguous:
        return bool(CATEGORY_RX.search(text) or BUSINESS_RX.search(text) or PRODUCT_TALK_RX.search(text))
    return True


def _fetch(term: str) -> list[dict]:
    try:
        r = get(GN.format(q=quote_plus(f"{term} when:21d")), timeout=20, min_gap=1.5)
    except Exception as e:
        log.warning("news fetch failed for %s: %s", term, e)
        return []
    feed = feedparser.parse(r.text)
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc)
        if pub and pub < cutoff:
            continue
        src = ""
        if getattr(e, "source", None):
            src = e.source.get("title", "")
        out.append({"title": e.get("title", ""), "link": e.get("link", ""), "published": pub.isoformat() if pub else "",
                    "source": src, "summary": e.get("summary", "")})
    return out


def _ingest(brand: str, term: str, items: list[dict]) -> int:
    n = 0
    b = settings.brand(brand)
    for it in items:
        if not it["link"] or not relevant(it["title"], it["summary"], term, b):
            continue
        with db.tx() as c:
            exists = c.execute("SELECT 1 FROM seen_urls WHERE source='news' AND url=?", (it["link"],)).fetchone()
            if exists:
                continue
            c.execute("INSERT INTO seen_urls(brand,source,url,seen_at) VALUES(?,?,?,?)", (brand, "news", it["link"], db.now()))
        if db.add_event(brand, "news", "news", it["title"], detail=f"{it['source']} | {it['published'][:10]} | q={term}",
                        url=it["link"], data={"publisher": it["source"], "published": it["published"], "term": term},
                        dedupe_key=f"news|{it['link']}"):
            n += 1
    return n


@tracked("news")
def run_brand(brand_name: str) -> int:
    brand = settings.brand(brand_name)
    if not brand:
        return 0
    n = 0
    for term in (brand.news_terms or [brand.name]):
        n += _ingest(brand.name, term, _fetch(term))
    return n


@tracked("news_sector")
def run_sector() -> int:
    n = 0
    for term in settings.sector_news_terms:
        n += _ingest("Sector", term, _fetch(term))
    return n


def run() -> int:
    total = sum(run_brand(b.name) for b in settings.brands)
    total += run_sector()
    return total
