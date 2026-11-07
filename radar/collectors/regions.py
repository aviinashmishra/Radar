"""Where in India each brand is wanted, talked about and stocked.

  trends     Google Trends "interest by sub-region" for each brand's search term, geo=IN, last 90 days (no key; the
             endpoint wants a cookie first and throttles, so brands are refreshed once a week with gentle pacing)
  mentions   Indian states and cities named in news, Reddit, video titles and customer comments about each brand
  qcommerce  cities where the brand's products sit on a quick-commerce shelf (from collectors/channels.py)
  sales      Pebble's own orders by state / city (collectors/sales.py), joined live by radar/regions.py
All of it lands in region_interest(brand, region, level, basis, period, value) and feeds the Regions page, the
reports and the strategy module's "where to expand" list.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timedelta, timezone
from .. import db, schema_ext
from ..config import settings
from .base import tracked, log

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# state -> (lat, lon) centroid, used by the dot map; also the lexicon for mention scanning
STATES = {
    "Andhra Pradesh": (15.9, 79.7), "Arunachal Pradesh": (28.2, 94.7), "Assam": (26.2, 92.9), "Bihar": (25.1, 85.3), "Chhattisgarh": (21.3, 81.9),
    "Goa": (15.3, 74.0), "Gujarat": (22.3, 71.2), "Haryana": (29.1, 76.1), "Himachal Pradesh": (31.1, 77.2), "Jharkhand": (23.6, 85.3),
    "Karnataka": (15.3, 75.7), "Kerala": (10.9, 76.3), "Madhya Pradesh": (23.5, 78.7), "Maharashtra": (19.8, 75.7), "Manipur": (24.7, 93.9),
    "Meghalaya": (25.5, 91.4), "Mizoram": (23.2, 92.9), "Nagaland": (26.2, 94.6), "Odisha": (20.9, 84.8), "Punjab": (31.1, 75.3),
    "Rajasthan": (27.0, 74.2), "Sikkim": (27.5, 88.5), "Tamil Nadu": (11.1, 78.7), "Telangana": (18.1, 79.0), "Tripura": (23.9, 91.9),
    "Uttar Pradesh": (26.8, 80.9), "Uttarakhand": (30.1, 79.1), "West Bengal": (22.9, 87.8), "Delhi": (28.6, 77.2),
    "Jammu and Kashmir": (33.8, 76.6), "Ladakh": (34.2, 77.6), "Chandigarh": (30.7, 76.8), "Puducherry": (11.9, 79.8),
    "Andaman and Nicobar Islands": (11.7, 92.7), "Dadra and Nagar Haveli and Daman and Diu": (20.3, 73.0), "Lakshadweep": (10.6, 72.6),
}
CITIES = {  # city -> (state, lat, lon)
    "Mumbai": ("Maharashtra", 19.08, 72.88), "Delhi": ("Delhi", 28.61, 77.21), "New Delhi": ("Delhi", 28.61, 77.21), "Gurugram": ("Haryana", 28.46, 77.03),
    "Gurgaon": ("Haryana", 28.46, 77.03), "Noida": ("Uttar Pradesh", 28.54, 77.39), "Bengaluru": ("Karnataka", 12.97, 77.59), "Bangalore": ("Karnataka", 12.97, 77.59),
    "Hyderabad": ("Telangana", 17.39, 78.49), "Chennai": ("Tamil Nadu", 13.08, 80.27), "Kolkata": ("West Bengal", 22.57, 88.36), "Pune": ("Maharashtra", 18.52, 73.86),
    "Ahmedabad": ("Gujarat", 23.02, 72.57), "Surat": ("Gujarat", 21.17, 72.83), "Jaipur": ("Rajasthan", 26.91, 75.79), "Lucknow": ("Uttar Pradesh", 26.85, 80.95),
    "Kanpur": ("Uttar Pradesh", 26.45, 80.33), "Nagpur": ("Maharashtra", 21.15, 79.09), "Indore": ("Madhya Pradesh", 22.72, 75.86), "Bhopal": ("Madhya Pradesh", 23.26, 77.41),
    "Patna": ("Bihar", 25.59, 85.14), "Chandigarh": ("Chandigarh", 30.73, 76.78), "Kochi": ("Kerala", 9.93, 76.27), "Coimbatore": ("Tamil Nadu", 11.02, 76.96),
    "Visakhapatnam": ("Andhra Pradesh", 17.69, 83.22), "Vijayawada": ("Andhra Pradesh", 16.51, 80.65), "Bhubaneswar": ("Odisha", 20.3, 85.82), "Guwahati": ("Assam", 26.14, 91.74),
    "Ludhiana": ("Punjab", 30.9, 75.86), "Amritsar": ("Punjab", 31.63, 74.87), "Dehradun": ("Uttarakhand", 30.32, 78.03), "Ranchi": ("Jharkhand", 23.34, 85.31),
    "Raipur": ("Chhattisgarh", 21.25, 81.63), "Thiruvananthapuram": ("Kerala", 8.52, 76.94), "Mysuru": ("Karnataka", 12.3, 76.65), "Mangaluru": ("Karnataka", 12.91, 74.86),
    "Vadodara": ("Gujarat", 22.31, 73.18), "Rajkot": ("Gujarat", 22.3, 70.8), "Nashik": ("Maharashtra", 19.99, 73.79), "Agra": ("Uttar Pradesh", 27.18, 78.01),
    "Varanasi": ("Uttar Pradesh", 25.32, 82.97), "Jodhpur": ("Rajasthan", 26.29, 73.02), "Goa": ("Goa", 15.3, 74.0), "Srinagar": ("Jammu and Kashmir", 34.08, 74.8),
    "Madurai": ("Tamil Nadu", 9.93, 78.12), "Thane": ("Maharashtra", 19.22, 72.98), "Faridabad": ("Haryana", 28.41, 77.31), "Ghaziabad": ("Uttar Pradesh", 28.67, 77.42),
}
_PLACE_RX = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(map(re.escape, list(STATES) + list(CITIES)), key=len, reverse=True)) + r")(?![a-z])")
GT_EXPLORE = "https://trends.google.com/trends/api/explore"
GT_GEO = "https://trends.google.com/trends/api/widgetdata/comparedgeo"
GT_WORDS = {"band": "band", "ring": "ring", "watch": "smartwatch", "earbuds": "earbuds", "speaker": "speaker"}


def trends_keyword(brand) -> str:
    """"Noise" alone is a dictionary word; "noise smartwatch" is the brand. Brands with a safe name go bare."""
    from ..relevance import AMBIGUOUS
    term = (brand.search_terms or [brand.name])[0]
    if brand.name.lower() in AMBIGUOUS and term.lower() == brand.name.lower():
        seg = next((s for s in brand.segments if s in GT_WORDS), None)
        return f"{term} {GT_WORDS.get(seg, 'smartwatch')}"
    return term


def interest_by_region(keyword: str, session=None) -> list[dict]:
    """Google Trends sub-region values (0-100) for one keyword in India over the last 90 days."""
    from .base import session as S
    sess = session or S
    hdr = {"User-Agent": CHROME_UA, "Accept-Language": "en-IN,en;q=0.9"}
    if not sess.cookies.get("NID", domain=".google.com"):
        sess.get("https://trends.google.com/trends/?geo=IN", headers=hdr, timeout=25)
        time.sleep(2)
    req = {"comparisonItem": [{"keyword": keyword, "geo": "IN", "time": "today 3-m"}], "category": 0, "property": ""}
    r = sess.get(GT_EXPLORE, params={"hl": "en-IN", "tz": "-330", "req": json.dumps(req)}, headers=hdr, timeout=30)
    if r.status_code == 429:
        raise RuntimeError("429")
    if r.status_code != 200:
        return []
    widgets = json.loads(r.text[5:]).get("widgets") or []
    w = next((x for x in widgets if x.get("id") == "GEO_MAP"), None)
    if not w:
        return []
    time.sleep(1.5)
    r2 = sess.get(GT_GEO, params={"hl": "en-IN", "tz": "-330", "req": json.dumps(w["request"]), "token": w["token"]}, headers=hdr, timeout=30)
    if r2.status_code == 429:
        raise RuntimeError("429")
    if r2.status_code != 200:
        return []
    rows = (json.loads(r2.text[5:]).get("default") or {}).get("geoMapData") or []
    return [{"region": x.get("geoName"), "code": x.get("geoCode"), "value": (x.get("value") or [0])[0]} for x in rows if x.get("hasData", [True])[0]]


def run_trends(max_brands: int = 12) -> int:
    """A dozen brands per run (one week between refreshes per brand) keeps Google Trends from throttling."""
    n, period = 0, datetime.now(timezone.utc).strftime("%Y-%m")
    backoff = db.kv_get("gt_backoff_until")
    if backoff and backoff > db.now():
        log.info("google trends: backing off until %s", backoff)
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    todo = []
    for b in settings.brands:
        last = db.q1("SELECT MAX(seen_at) m FROM region_interest WHERE brand=? AND basis='trends'", (b.name,)) or {}
        if not last.get("m") or last["m"] < cutoff:
            todo.append(b)
    for b in todo[:max_brands]:
        kw = trends_keyword(b)
        try:
            rows = interest_by_region(kw)
        except RuntimeError:
            until = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(timespec="seconds")
            db.kv_set("gt_backoff_until", until)
            log.info("google trends throttled; resuming after %s", until)
            break
        except Exception as e:  # noqa: BLE001
            log.info("trends %s failed: %s", kw, e)
            continue
        with db.tx() as c:
            for r in rows:
                if r["region"] in STATES:
                    c.execute("INSERT OR REPLACE INTO region_interest(brand,region,level,basis,period,value,detail,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                              (b.name, r["region"], "state", "trends", period, float(r["value"] or 0), kw, db.now()))
        n += len(rows)
        time.sleep(4)
    return n


def run_mentions(days: int = 90) -> int:
    """Count places named next to each brand in the last N days of text (news, social, video titles, comments)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    period = f"{days}d"
    counts: dict[tuple, dict] = {}
    rows = db.q("SELECT brand, title, detail FROM events WHERE created_at>=? AND source IN ('news','feed','reddit','youtube','press') "
                "AND brand NOT IN ('Sector','NewEntrant')", (since,))
    rows += db.q("SELECT brand, text title, '' detail FROM voice WHERE created>=?", (since[:10],))
    for r in rows:
        for m in _PLACE_RX.finditer(f"{r['title']} {r['detail'] or ''}"):
            place = m.group(1)
            key = (r["brand"], place, "city" if place in CITIES else "state")
            a = counts.setdefault(key, {"n": 0, "sample": r["title"][:120]})
            a["n"] += 1
    with db.tx() as c:
        c.execute("DELETE FROM region_interest WHERE basis='mentions' AND period=?", (period,))
        for (brand, place, level), a in counts.items():
            c.execute("INSERT OR REPLACE INTO region_interest(brand,region,level,basis,period,value,detail,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                      (brand, place, level, "mentions", period, float(a["n"]), a["sample"], db.now()))
    return len(counts)


def run_qcommerce() -> int:
    """Quick-commerce availability per city from the channel listings (how many live SKUs per brand per city)."""
    period = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = db.q("SELECT brand, city, COUNT(*) n, SUM(available) a, MIN(price) p FROM channel_listings WHERE city<>'' GROUP BY brand, city")
    with db.tx() as c:
        for r in rows:
            c.execute("INSERT OR REPLACE INTO region_interest(brand,region,level,basis,period,value,detail,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                      (r["brand"], r["city"], "city", "qcommerce", period, float(r["a"] or 0),
                       f"{r['n']} listed, from ₹{r['p']:,.0f}" if r["p"] else f"{r['n']} listed", db.now()))
    return len(rows)


@tracked("regions")
def run() -> int:
    schema_ext.ensure()
    for step in (run_trends, run_mentions, run_qcommerce):
        try:
            log.info("regions %s -> %s rows", step.__name__, step())
        except Exception as e:  # noqa: BLE001
            log.exception("regions step %s failed: %s", step.__name__, e)
    return 0  # facts, not events
