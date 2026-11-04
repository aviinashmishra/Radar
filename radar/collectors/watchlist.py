"""Watchlist: curated rival products (from the Competitors sheet) tracked on their own pages, Shopify or not.

For every product with a `url` we read the live selling price and availability:
  1. schema.org JSON-LD offers (Ultrahuman, Samsung, Polar, Gabit, FITTR, Sekyo ...)
  2. meta price tags
  3. the rupee amount on the page closest to the last known price (Google Store and other JS-heavy pages)
Sites that block plain requests (Whoop, Croma) are rendered with Playwright when it is installed.
Events: watch_price_change, watch_status_change (sold out / back / pre-order), watch_page_change.
"""
from __future__ import annotations
import json
import re
from .. import db
from ..config import settings, ROOT
from .base import get, clean_text, sha, tracked, log

RUPEE_RX = re.compile(r"(?:₹|Rs\.?|INR)\s?([\d,]{4,9})")
SOLD_RX = re.compile(r"sold out|out of stock|notify me|currently unavailable", re.I)
PRE_RX = re.compile(r"pre-?order|coming soon|notify when available|waitlist", re.I)


def _ld_prices(html: str) -> list[float]:
    out: list[float] = []
    for m in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.strip())
        except ValueError:
            continue

        def walk(o):
            if isinstance(o, dict):
                for k in ("price", "lowPrice"):
                    v = o.get(k)
                    if v is not None and re.fullmatch(r"[\d.]+", str(v)):
                        out.append(float(v))
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(data)
    return [p for p in out if p >= 100]


def _render(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_context(locale="en-IN", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36").new_page()
            pg.goto(url, timeout=60000, wait_until="domcontentloaded")
            pg.wait_for_timeout(4000)
            html = pg.content()
            b.close()
            return html
    except Exception as e:  # noqa: BLE001
        log.info("render %s failed: %s", url, e)
        return None


def read_page(url: str, baseline: float | None) -> dict | None:
    html = None
    try:
        r = get(url, timeout=30)
        if r.status_code == 200 and len(r.text) > 2000:
            html = r.text
    except Exception as e:  # noqa: BLE001
        log.info("watchlist %s: %s", url, e)
    if html is None:
        html = _render(url)
    if not html:
        return None
    text = clean_text(html)
    prices = _ld_prices(html)
    meta = re.search(r'(?:itemprop|property)="(?:product:price:amount|price)"\s+content="([\d.]+)"', html)
    if not prices and meta:
        prices = [float(meta.group(1))]
    source = "jsonld" if prices else "text"
    if not prices:
        amounts = [int(x.replace(",", "")) for x in RUPEE_RX.findall(text)]
        if amounts and baseline:
            cand = [a for a in amounts if 0.4 * baseline <= a <= 1.6 * baseline]
            prices = [float(min(cand, key=lambda v: abs(v - baseline)))] if cand else []
    price = min(prices) if prices else None
    low = text.lower()
    if PRE_RX.search(low) and not re.search(r"add to cart|buy now", low):
        status = "pre-order"
    elif SOLD_RX.search(low) and not re.search(r"add to cart|buy now", low):
        status = "sold out"
    else:
        status = "on sale"
    return {"price": price, "status": status, "hash": sha(text[:20000]), "source": source}


def load() -> list[dict]:
    import yaml
    p = ROOT / "config" / "watchlist.yaml"
    if not p.exists():
        return []
    return list((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("products") or [])


@tracked("watchlist")
def run() -> int:
    n = 0
    for item in load():
        url = item.get("url")
        key = item["name"]
        prev = db.q1("SELECT * FROM watch_prices WHERE name=? ORDER BY id DESC LIMIT 1", (key,))
        if not url:
            if not prev:  # no page: record the sheet's baseline once so the ladder still shows it
                with db.tx() as c:
                    c.execute("INSERT INTO watch_prices(name,brand,price,status,page_hash,source,seen_at) VALUES(?,?,?,?,?,?,?)",
                              (key, item["brand"], item.get("price_inr"), item.get("status", "unknown"), "", "sheet", db.now()))
            continue
        baseline = (prev or {}).get("price") or item.get("price_inr")
        res = read_page(url, baseline)
        if res is None:
            continue
        with db.tx() as c:
            c.execute("INSERT INTO watch_prices(name,brand,price,status,page_hash,source,seen_at) VALUES(?,?,?,?,?,?,?)",
                      (key, item["brand"], res["price"], res["status"], res["hash"], res["source"], db.now()))
        if not prev:
            continue
        if res["price"] and prev["price"] and abs(res["price"] - prev["price"]) / prev["price"] >= 0.03:
            pct = (res["price"] - prev["price"]) / prev["price"] * 100
            if db.add_event(item["brand"], "watchlist", "watch_price_change",
                            f"{key}: {settings.currency} {prev['price']:,.0f} -> {res['price']:,.0f} ({pct:+.0f}%)",
                            detail=f"segment={item.get('segment')} subscription={item.get('subscription')} origin={item.get('origin')}",
                            url=url, data={"old": prev["price"], "new": res["price"], "pct": round(pct, 1), "segment": item.get("segment"),
                                           "product_type": item.get("segment")},
                            dedupe_key=f"watch|{key}|price|{res['price']}"):
                n += 1
        if res["status"] != prev["status"]:
            if db.add_event(item["brand"], "watchlist", "watch_status_change", f"{key} is now {res['status']} (was {prev['status']})",
                            url=url, data={"old": prev["status"], "new": res["status"], "segment": item.get("segment")},
                            dedupe_key=f"watch|{key}|status|{res['status']}|{db.now()[:10]}"):
                n += 1
        if prev["page_hash"] and res["hash"] != prev["page_hash"] and res["price"] == prev["price"]:
            db.add_event(item["brand"], "watchlist", "watch_page_change", f"{key} product page changed",
                         detail="Copy, offer or spec text on the product page changed while the price held.", url=url,
                         data={"segment": item.get("segment")}, dedupe_key=f"watch|{key}|page|{res['hash']}")
    return n
