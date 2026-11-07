"""News intelligence over the events table: topic taxonomy, story clustering across publishers, share of voice,
velocity per brand, and what is trending in India right now.

Stories: news events of the last N days are grouped by brand and fuzzy headline similarity (rapidfuzz), so a launch
covered by eight outlets is one story with eight sources, a first-seen time and the highest importance score.
"""
from __future__ import annotations
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from rapidfuzz import fuzz
from . import db
from .config import settings

TOPICS = [
    ("financials",   r"revenue|turnover|net (loss|profit)|profit|\bloss(es)?\b|\bpat\b|ebitda|results|quarter|q[1-4]\s?fy|fy ?'?\d{2}|margin|filing|mca\b"),
    ("funding",      r"raises|raised|funding|series [a-f]\b|seed round|investor|valuation|valued at|\bipo\b|drhp|sebi|listing|acqui|merger|stake|buyout"),
    ("launch",       r"launch|unveil|introduc|debut|announce[sd]? .*(watch|band|ring|buds|earbud|speaker)|new .*(watch|band|ring|buds|earbud|speaker)|goes on sale|pre-?order|teas"),
    ("pricing",      r"price cut|price drop|slash|discount|cheaper|offer|deal|sale\b|cashback|under ₹|under rs|starting at"),
    ("legal",        r"lawsuit|sues|patent|\bitc\b|court|ban(ned)?|recall|regulat|\bbis\b|customs|tariff|duty|antitrust|complaint|penalty|notice"),
    ("leadership",   r"appoint|names? .* as (ceo|cfo|coo|cmo|head|chief)|joins as|steps down|resign|hires|chief (executive|marketing|technology|product)|co-?founder"),
    ("partnership",  r"partner|tie-?up|collab|team(s|ed)? up|alliance|signs? .* with|brand ambassador|sponsor"),
    ("distribution", r"flipkart|amazon|croma|reliance digital|vijay sales|retail|offline|stores?|quick commerce|blinkit|zepto|expands? to|enters? (the )?(us|uk|uae|europe|india)"),
    ("manufacturing", r"manufactur|factory|plant|make in india|\bpli\b|assembl|supply chain|component|localis"),
    ("marketing",    r"campaign|ad film|advert|ipl\b|cricket|influencer|celebrit|ambassador|tvc|#"),
    ("market_data",  r"\bidc\b|counterpoint|canalys|shipments?|market share|\bgrew\b|declin|wearables market|rank(ed|ing)"),
    ("reviews",      r"review|hands-?on|tested|vs\.?|comparison|should you buy|worth it|first impressions"),
]
_TOPIC_RX = [(t, re.compile(rx, re.I)) for t, rx in TOPICS]


def topic_of(title: str, summary: str = "") -> str:
    text = f"{title} {summary}"
    for t, rx in _TOPIC_RX:
        if rx.search(title) or (t in ("financials", "funding", "legal", "manufacturing") and rx.search(text)):
            return t
    return "general"


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower())


def _same_story(a: str, b: str) -> bool:
    """Two headline keys tell the same story. Token-set similarity is forgiving on long headlines, but a three-word
    key ("samsung galaxy watch", a topic page) is a subset of half the brand's headlines, so short keys must match
    almost exactly."""
    if min(len(a.split()), len(b.split())) < 5:
        return fuzz.ratio(a, b) >= 88
    return fuzz.token_set_ratio(a, b) >= 70


def stories(days: int = 14, brand: str | None = None, topic: str | None = None, limit: int = 80, q: str | None = None,
            sort: str = "strength") -> list[dict]:
    """Cluster news events into stories. Same brand + headline similarity >= 70 (token set ratio) within the window.
    `q` filters headlines and summaries by text; `sort` is "strength" (importance + breadth) or "newest"."""
    sql, p = ("SELECT id, created_at, brand, title, url, importance, so_what, counter_move, category, data FROM events "
              "WHERE source='news' AND created_at>=?"), [_since(days)]
    if brand:
        sql += " AND brand=?"; p.append(brand)
    if q and q.strip():
        sql += " AND (title LIKE ? OR data LIKE ?)"; p += [f"%{q.strip()}%", f"%{q.strip()}%"]
    rows = db.q(sql + " ORDER BY id DESC LIMIT 1500", p)
    for r in rows:
        r["data"] = json.loads(r.get("data") or "{}")
        r["topic"] = r["data"].get("topic") or topic_of(r["title"], r["data"].get("summary", ""))
    if q and q.strip():  # the LIKE above is a coarse prefilter; match whole words in the headline or summary
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(q.strip()), re.I)
        rows = [r for r in rows if rx.search(r["title"]) or rx.search(r["data"].get("summary") or "")]
    if topic:
        rows = [r for r in rows if r["topic"] == topic]
    clusters: list[dict] = []
    by_brand: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        k = _key(r["title"])
        home = None
        for c in by_brand[r["brand"]]:
            if _same_story(k, c["key"]):
                home = c
                break
        if home is None:
            home = {"key": k, "brand": r["brand"], "title": r["title"], "topic": r["topic"], "importance": r["importance"] or 0,
                    "first_seen": r["created_at"], "last_seen": r["created_at"], "so_what": r["so_what"], "counter_move": r["counter_move"],
                    "impact": r["data"].get("impact"), "summary": (r["data"].get("summary") or "")[:300], "category": r["category"],
                    "sources": [], "ids": [], "publishers": set(), "providers": set()}
            by_brand[r["brand"]].append(home)
            clusters.append(home)
        pub = r["data"].get("publisher") or ""
        home["sources"].append({"id": r["id"], "title": r["title"], "url": r["url"], "publisher": pub,
                                "published": r["data"].get("published", "")[:10], "provider": r["data"].get("provider", "google")})
        home["ids"].append(r["id"])
        home["publishers"].add(pub)
        home["providers"].add(r["data"].get("provider", "google"))
        home["first_seen"] = min(home["first_seen"], r["created_at"])
        home["last_seen"] = max(home["last_seen"], r["created_at"])
        if (r["importance"] or 0) > home["importance"]:
            home["importance"], home["title"], home["so_what"], home["counter_move"] = r["importance"], r["title"], r["so_what"], r["counter_move"]
            home["impact"] = r["data"].get("impact") or home["impact"]
            home["summary"] = (r["data"].get("summary") or home["summary"])[:300]
    out = []
    for c in clusters:
        c["publishers"] = sorted(x for x in c["publishers"] if x)
        c["providers"] = sorted(c["providers"])
        c["n_sources"] = len(c["sources"])
        c["sources"] = c["sources"][:12]
        del c["key"]
        out.append(c)
    # strongest stories first: importance plus breadth of coverage, newest among equals
    out.sort(key=lambda c: c["last_seen"], reverse=True)
    if sort != "newest":
        out.sort(key=lambda c: -(c["importance"] + min(c["n_sources"], 6)))
    return out[:limit]


def share_of_voice(days: int = 30) -> list[dict]:
    """News mentions per brand in the window, with the previous window for the trend, and the topic mix."""
    me = settings.self_brand.name if settings.self_brand else ""
    cur = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM events WHERE source='news' AND created_at>=? GROUP BY brand", (_since(days),))}
    prev = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM events WHERE source='news' AND created_at>=? AND created_at<? GROUP BY brand",
                                            (_since(2 * days), _since(days)))}
    topics = defaultdict(lambda: defaultdict(int))
    for r in db.q("SELECT brand, data FROM events WHERE source='news' AND created_at>=?", (_since(days),)):
        d = json.loads(r["data"] or "{}")
        topics[r["brand"]][d.get("topic") or "general"] += 1
    total = sum(v for b, v in cur.items() if b != "Sector") or 1
    out = []
    for b in settings.brands:
        c = cur.get(b.name, 0)
        out.append({"brand": b.name, "self": b.name == me, "mentions": c, "prev": prev.get(b.name, 0),
                    "share_pct": round(100 * c / total, 1), "topics": dict(sorted(topics[b.name].items(), key=lambda x: -x[1]))})
    out.sort(key=lambda x: -x["mentions"])
    return out


def velocity(weeks: int = 8) -> dict:
    """Weekly news mentions per brand for sparklines."""
    labels = []
    now = datetime.now(timezone.utc)
    for i in range(weeks - 1, -1, -1):
        labels.append((now - timedelta(weeks=i)).strftime("%G-W%V"))
    series = defaultdict(lambda: {w: 0 for w in labels})
    for r in db.q("SELECT brand, created_at FROM events WHERE source='news' AND created_at>=?", (_since(7 * weeks),)):
        w = datetime.fromisoformat(r["created_at"]).strftime("%G-W%V")
        if w in series[r["brand"]]:
            series[r["brand"]][w] += 1
    return {"weeks": labels, "brands": {b: [v[w] for w in labels] for b, v in series.items()}}


def topic_mix(days: int = 30) -> list[dict]:
    counts = defaultdict(int)
    for r in db.q("SELECT data FROM events WHERE source='news' AND created_at>=?", (_since(days),)):
        counts[json.loads(r["data"] or "{}").get("topic") or "general"] += 1
    return [{"topic": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: -x[1])]


def publishers(days: int = 30, limit: int = 15) -> list[dict]:
    counts = defaultdict(lambda: {"stories": 0, "brands": set()})
    for r in db.q("SELECT brand, data FROM events WHERE source='news' AND created_at>=?", (_since(days),)):
        pub = json.loads(r["data"] or "{}").get("publisher") or ""
        if pub:
            counts[pub]["stories"] += 1
            counts[pub]["brands"].add(r["brand"])
    return [{"publisher": p, "stories": v["stories"], "brands": len(v["brands"])}
            for p, v in sorted(counts.items(), key=lambda x: -x[1]["stories"])[:limit]]


def trending(limit: int = 20) -> list[dict]:
    return db.q("SELECT title, traffic, brand, segment, url, seen_at FROM trending ORDER BY id DESC LIMIT ?", (limit,))


def search_pulse(days: int = 7, limit: int = 10) -> list[dict]:
    """What India is starting to search for in the category (Google autocomplete, the searchdemand collector): the
    sector's own trend feed when nothing from it is on Google's national trending list."""
    return db.q("SELECT id, brand, title, url, importance, created_at, data FROM events WHERE kind='search_trend' AND created_at>=? "
                "ORDER BY importance DESC, id DESC LIMIT ?", (_since(days), limit))


def overview(days: int = 14) -> dict:
    st = stories(days)
    pulse = search_pulse()
    for r in pulse:
        r["suggestion"] = json.loads(r.pop("data") or "{}").get("suggestion") or r["title"]
    return {"days": days, "stories": st, "share": share_of_voice(30), "velocity": velocity(), "topics": topic_mix(30),
            "publishers": publishers(30), "trending": trending(), "trends_checked": db.kv_get("trends_checked"), "search_pulse": pulse,
            "last_run": db.kv_get("collect:news") or {},
            "stats": {"stories": len(st), "articles": sum(c["n_sources"] for c in st),
                      "multi_source": sum(1 for c in st if c["n_sources"] >= 3),
                      "brands": len({c["brand"] for c in st})}}
