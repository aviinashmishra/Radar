"""News per brand + sector-wide searches from two aggregators (Google News RSS and Bing News RSS; India editions,
free, no key). Every item is run through radar/relevance.py, tagged with a topic (launch, funding, financials,
leadership, legal, ...) by radar/newsintel.py, and de-duplicated across aggregators by normalised headline so the
same story from two feeds is one event.
"""
from __future__ import annotations
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
import feedparser
from .. import db
from ..config import settings
from ..relevance import relevant as _relevant
from .base import get, tracked, log

GN = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
BING = "https://www.bing.com/news/search?q={q}&format=rss&cc=IN&setlang=en"
MAX_AGE_DAYS = 21
PROVIDERS = ("google", "bing")


def relevant(title: str, summary: str, term: str, brand=None) -> bool:
    """Sector searches only need to look like the sector; brand searches need the brand named in the headline,
    with product or business context for everyday-word names and global brands (rules in radar/relevance.py)."""
    return _relevant(title, brand, term, summary=summary)


def norm_title(title: str) -> str:
    """Headline key for cross-feed de-duplication: lowercase, no publisher suffix, no punctuation."""
    t = re.split(r"\s+[-|–]\s+[^-|–]{2,40}$", title or "", maxsplit=1)[0]   # "… - Times of India"
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()[:140]


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _when(entry) -> datetime | None:
    if getattr(entry, "published_parsed", None):
        return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
    if entry.get("published"):
        try:
            return parsedate_to_datetime(entry["published"]).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


def _entries(url: str, provider: str, min_gap: float) -> list[dict]:
    try:
        r = get(url, timeout=25, min_gap=min_gap,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
                if provider == "bing" else None)
    except Exception as e:  # noqa: BLE001
        log.warning("%s news fetch failed: %s", provider, e)
        return []
    if r.status_code != 200:
        log.info("%s news %s -> %s", provider, url[:80], r.status_code)
        return []
    feed = feedparser.parse(r.text)
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    for e in feed.entries:
        pub = _when(e)
        if pub and pub < cutoff:
            continue
        src = ""
        if getattr(e, "source", None):
            src = e.source.get("title", "") if isinstance(e.source, dict) else str(e.source)
        if not src:
            src = e.get("news_source") or e.get("source_title") or ""
        link = e.get("link", "")
        if not src and link:
            src = urlparse(link).netloc.replace("www.", "")
        out.append({"title": _plain(e.get("title", "")), "link": link, "published": pub.isoformat() if pub else "",
                    "source": src, "summary": _plain(e.get("summary", "") or e.get("description", ""))[:600], "provider": provider})
    return out


def fetch_google(term: str, recency: str = "21d") -> list[dict]:
    return _entries(GN.format(q=quote_plus(f"{term} when:{recency}")), "google", 1.5)


def fetch_bing(term: str) -> list[dict]:
    return _entries(BING.format(q=quote_plus(term)), "bing", 1.0)


def _fetch(term: str) -> list[dict]:
    items = fetch_google(term)
    if "bing" in PROVIDERS:
        items += fetch_bing(term)
    return items


_SECTOR_RX = None


def sector_topic(text: str) -> bool:
    """True when a sector-wide article is actually about the category (a segment word or 'wearable'); keeps
    'Coca-Cola bottler files for IPO' and laptop round-ups out of the sector feed."""
    global _SECTOR_RX
    if _SECTOR_RX is None:
        alts = [sg.get("keywords") for sg in settings.segments if sg.get("keywords")] + [r"wearable", r"hearable", r"audio brand"]
        _SECTOR_RX = re.compile("|".join(f"(?:{a})" for a in alts), re.I)
    return bool(_SECTOR_RX.search(text or ""))


def _ingest(brand: str, term: str, items: list[dict], *, extra: dict | None = None) -> int:
    from ..newsintel import topic_of
    from ..relevance import attribute
    n = 0
    b = settings.brand(brand)
    sector = brand == "Sector"
    for it in items:
        if not it["link"] or not it["title"] or not relevant(it["title"], it["summary"], term, b):
            continue
        if sector:  # a sector search that names a tracked brand ("Oura Ring 5 launches in India") files under that brand
            if not sector_topic(f"{it['title']} {it['summary']}"):
                continue
            brand = attribute(it["title"])
        key = norm_title(it["title"])
        with db.tx() as c:
            if c.execute("SELECT 1 FROM seen_urls WHERE source='news' AND url=?", (it["link"],)).fetchone():
                continue
            if key and c.execute("SELECT 1 FROM seen_urls WHERE source='news-title' AND url=?", (key,)).fetchone():
                continue  # the same headline from the other aggregator
            c.execute("INSERT INTO seen_urls(brand,source,url,seen_at) VALUES(?,?,?,?)", (brand, "news", it["link"], db.now()))
            if key:
                c.execute("INSERT OR IGNORE INTO seen_urls(brand,source,url,seen_at) VALUES(?,?,?,?)", (brand, "news-title", key, db.now()))
        topic = topic_of(it["title"], it["summary"])
        data = {"publisher": it["source"], "published": it["published"], "term": term, "provider": it["provider"],
                "topic": topic, "summary": it["summary"][:400], **(extra or {})}
        if db.add_event(brand, "news", "news", it["title"], detail=f"{it['source']} | {it['published'][:10]} | {topic} | q={term}",
                        url=it["link"], data=data, dedupe_key=f"news|{it['link']}"):
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
