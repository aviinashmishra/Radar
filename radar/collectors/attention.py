"""Public attention index: daily English Wikipedia page views per brand (Wikimedia REST API, free, no key).

A brand's article gets read when people hear about it: a launch, a funding round, a scandal, an IPL ad. Compared with
the brand's own 30-day median, a day at 3x or more becomes an `attention_spike` event, and the series feeds the
Share-of-signal table on the Market page. `wikipedia:` in companies.yaml names the article; brands without one are
skipped (Pebble has none: the English article is the US watch).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from statistics import median
from urllib.parse import quote
from .. import db
from ..config import settings
from .base import get, tracked, log
from . import resolve

API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{a}/daily/{d0}/{d1}"
UA = {"User-Agent": "PebbleRadar/1.0 (competitive research; public data only)"}
SPIKE = 3.0


def fetch(article: str, days: int = 45) -> list[dict]:
    now = datetime.now(timezone.utc)
    d0, d1 = (now - timedelta(days=days)).strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")
    r = get(API.format(a=quote(article.replace(" ", "_"), safe=""), d0=d0, d1=d1), timeout=30, min_gap=0.5, headers=UA)
    if r.status_code != 200:
        return []
    try:
        items = r.json().get("items") or []
    except ValueError:
        return []
    return [{"day": f"{x['timestamp'][:4]}-{x['timestamp'][4:6]}-{x['timestamp'][6:8]}", "views": int(x.get("views") or 0)} for x in items]


@tracked("attention")
def run() -> int:
    n = 0
    for b in settings.brands:
        brand = b.name
        article = resolve.ids(brand).get("wikipedia")  # companies.yaml title, or the one resolve.py found / validated
        if not article:
            continue
        try:
            rows = fetch(article)
        except Exception as e:  # noqa: BLE001
            log.info("wikipedia %s failed: %s", article, e)
            continue
        if not rows:
            continue
        with db.tx() as cx:
            for r in rows:
                cx.execute("INSERT OR REPLACE INTO attention(brand,article,day,views) VALUES(?,?,?,?)", (brand, article, r["day"], r["views"]))
        base = [r["views"] for r in rows[:-1]][-30:]
        last = rows[-1]
        med = median(base) if base else 0
        if med >= 20 and last["views"] >= SPIKE * med:
            url = f"https://en.wikipedia.org/wiki/{quote(article.replace(' ', '_'))}"
            if db.add_event(brand, "wikipedia", "attention_spike",
                            f"{brand}: public attention {last['views'] / med:.1f}x normal ({last['views']:,} Wikipedia views on {last['day']})",
                            detail=f"30-day median {med:,.0f} views a day. Something put the company in the news; check its News tab.",
                            url=url, data={"views": last["views"], "median": med, "day": last["day"], "article": article},
                            dedupe_key=f"wiki|{brand}|{last['day']}"):
                n += 1
    return n
