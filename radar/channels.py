"""Distribution analytics over channel_listings / channel_history / channel_mentions: the brand x channel matrix,
one brand's shelves, price parity between a brand's own store and the marketplaces, and the quick-commerce city map."""
from __future__ import annotations
import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db
from .config import settings
from .collectors.channels import channels as channel_defs, cities as city_defs


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def matrix(days: int = 7) -> dict:
    """brand x channel: live listings (seen in the last `days`), min price, mean rating, total ratings, cities, mentions."""
    me = _me()
    defs = channel_defs()
    live = defaultdict(lambda: {"listings": 0, "in_stock": 0, "min_price": None, "ratings": [], "rating_count": 0, "cities": set(), "sponsored": 0})
    for r in db.q("SELECT brand, channel, city, price, available, rating, rating_count, sponsored FROM channel_listings WHERE last_seen>=?", (_since(days),)):
        c = live[(r["brand"], r["channel"])]
        c["listings"] += 1
        c["in_stock"] += r["available"] or 0
        c["sponsored"] += r["sponsored"] or 0
        if r["price"]:
            c["min_price"] = r["price"] if c["min_price"] is None else min(c["min_price"], r["price"])
        if r["rating"]:
            c["ratings"].append(r["rating"])
        c["rating_count"] += r["rating_count"] or 0
        if r["city"]:
            c["cities"].add(r["city"])
    ment = defaultdict(lambda: {"n": 0, "last": "", "title": "", "url": ""})
    for r in db.q("SELECT brand, channel, COUNT(*) n, MAX(seen_at) last FROM channel_mentions GROUP BY brand, channel"):
        ment[(r["brand"], r["channel"])].update({"n": r["n"], "last": r["last"]})
    for r in db.q("SELECT brand, channel, title, url, seen_at FROM channel_mentions WHERE (brand, channel, seen_at) IN "
                  "(SELECT brand, channel, MAX(seen_at) FROM channel_mentions GROUP BY brand, channel)"):
        ment[(r["brand"], r["channel"])].update({"title": r["title"], "url": r["url"]})
    rows = []
    for b in settings.brands:
        cells, present = {}, 0
        for ch in defs:
            c = live.get((b.name, ch["key"]))
            m = ment.get((b.name, ch["key"]))
            cell = {"listings": c["listings"] if c else 0, "in_stock": c["in_stock"] if c else 0, "min_price": c["min_price"] if c else None,
                    "rating": round(sum(c["ratings"]) / len(c["ratings"]), 2) if c and c["ratings"] else None,
                    "rating_count": c["rating_count"] if c else 0, "cities": sorted(c["cities"]) if c else [], "sponsored": c["sponsored"] if c else 0,
                    "mentions": m["n"] if m else 0, "last_mention": m["last"][:10] if m else "", "mention_title": m["title"] if m else "",
                    "mention_url": m["url"] if m else ""}
            cell["status"] = "live" if cell["listings"] else ("reported" if cell["mentions"] else "none")
            present += cell["status"] != "none"
            cells[ch["key"]] = cell
        rows.append({"brand": b.name, "self": b.name == me, "origin": b.origin, "cells": cells, "channels_present": present,
                     "qc_cities": len(cells.get("blinkit", {}).get("cities", []))})
    rows.sort(key=lambda r: (not r["self"], -r["channels_present"], -r["qc_cities"]))
    return {"channels": defs, "brands": rows, "days": days, "cities": [c["name"] for c in city_defs()],
            "stats": {"listings": db.q1("SELECT COUNT(*) c FROM channel_listings")["c"],
                      "live_brands": sum(1 for r in rows if any(c["listings"] for c in r["cells"].values())),
                      "mentions": db.q1("SELECT COUNT(*) c FROM channel_mentions")["c"]}}


def brand(name: str, days: int = 30) -> dict:
    """One brand's shelves: every listing per channel with a price trail, plus mention evidence for the rest."""
    defs = {c["key"]: c for c in channel_defs()}
    out = defaultdict(list)
    for r in db.q("SELECT * FROM channel_listings WHERE brand=? ORDER BY channel, city, COALESCE(rating_count,0) DESC", (name,)):
        hist = db.q("SELECT price, available, rating, rating_count, seen_at FROM channel_history WHERE channel=? AND city=? AND item_key=? ORDER BY id DESC LIMIT 40",
                    (r["channel"], r["city"], r["item_key"]))
        r["history"] = list(reversed(hist))
        r["first_price"] = next((h["price"] for h in r["history"] if h["price"]), None)
        out[r["channel"]].append(r)
    mentions = db.q("SELECT channel, title, url, seen_at FROM channel_mentions WHERE brand=? ORDER BY seen_at DESC LIMIT 40", (name,))
    events = db.q("SELECT id, created_at, kind, title, url, importance, data FROM events WHERE brand=? AND source='channels' ORDER BY id DESC LIMIT 40", (name,))
    for e in events:
        e["data"] = json.loads(e["data"] or "{}")
    return {"brand": name, "channels": [{**defs.get(k, {"key": k, "name": k}), "listings": v} for k, v in out.items()],
            "mentions": mentions, "events": events, "parity": parity(name)}


_PARITY_CACHE: dict = {"at": None, "rows": []}


def _same_model(a: str, b: str) -> bool:
    """Guard on the fuzzy match: model numbers must agree (Champ 3 is not Champ 2; VS104 is not VS102)."""
    import re
    tok = lambda s: set(re.findall(r"(?<![a-z0-9])[a-z]*[0-9]+[a-z]*(?![a-z0-9])", s.lower()))  # noqa: E731
    na, nb = tok(a), tok(b)
    na = {t for t in na if not re.fullmatch(r"\d{4,}|\d+mah|\d+w|\d+h|\d+m|\d+mm|\d+hz", t)}
    nb = {t for t in nb if not re.fullmatch(r"\d{4,}|\d+mah|\d+w|\d+h|\d+m|\d+mm|\d+hz", t)}
    if not na or not nb:
        return True
    return bool(na & nb)


def _parity_compute() -> None:
    """The fuzzy match of every marketplace listing against every store SKU takes ~20 s on a full database, so it runs
    here, off the request thread; parity() serves the last result and refreshes it in the background."""
    from .collectors.channels import _match_own
    rows = []
    store_brands = [r["brand"] for r in db.q("SELECT DISTINCT brand FROM products WHERE price>0")]
    for b in store_brands:
        bb = settings.brand(b)
        prods = db.q("SELECT product_id, title, price, available, handle FROM products WHERE brand=? AND price>0", (b,))
        if not prods:
            continue
        for li in db.q("SELECT channel, city, item_key, title, price, mrp, available, rating, rating_count, url, last_seen FROM channel_listings "
                       "WHERE brand=? AND price>0 AND last_seen>=? ORDER BY channel, COALESCE(rating_count,0) DESC", (b, _since(10))):
            m = _match_own(li["title"], prods)
            if not m or not _same_model(li["title"], m["title"]):
                continue
            gap = round((li["price"] - m["price"]) / m["price"] * 100, 1)
            rows.append({"brand": b, "self": b == _me(), "channel": li["channel"], "city": li["city"], "title": li["title"], "marketplace_price": li["price"],
                         "mrp": li["mrp"], "store_price": m["price"], "store_title": m["title"], "gap_pct": gap, "available": li["available"],
                         "rating": li["rating"], "rating_count": li["rating_count"], "url": li["url"],
                         "store_url": bb.product_url(m["handle"]) if bb and m.get("handle") else None, "when": li["last_seen"][:10]})
    rows.sort(key=lambda r: (not r["self"], r["gap_pct"]))
    _PARITY_CACHE.update(at=datetime.now(timezone.utc), rows=rows)


_PARITY_LOCK = threading.Lock()
_PARITY_THREAD: threading.Thread | None = None


def _parity_refresh() -> threading.Thread:
    global _PARITY_THREAD
    with _PARITY_LOCK:
        if _PARITY_THREAD is None or not _PARITY_THREAD.is_alive():
            _PARITY_THREAD = threading.Thread(target=_parity_compute, daemon=True, name="radar-parity")
            _PARITY_THREAD.start()
        return _PARITY_THREAD


def parity(name: str | None = None, max_age_s: int = 600, wait_s: float = 3.0) -> list[dict]:
    """Marketplace price vs the brand's own store for every current listing that fuzzy-matches a store SKU (live, not event-driven).
    gap_pct < 0: the marketplace sells it cheaper than the brand's D2C store (channel conflict); > 0: the store is the cheapest place.
    Served from a cache that a background thread refreshes; the very first call waits up to `wait_s` for it."""
    now = datetime.now(timezone.utc)
    if _PARITY_CACHE["at"] is None or (now - _PARITY_CACHE["at"]).total_seconds() > max_age_s:
        t = _parity_refresh()
        if _PARITY_CACHE["at"] is None and wait_s:
            t.join(wait_s)
    rows = _PARITY_CACHE["rows"]
    return [r for r in rows if r["brand"] == name] if name else rows


def warm() -> None:
    """Called once after the server starts so the first Channels / Pricing page is already answered."""
    _parity_refresh()


def qc_map() -> dict:
    """City x brand live SKUs on quick commerce, with the city coordinates for the Regions map."""
    cities = {c["name"]: c for c in city_defs()}
    grid = defaultdict(dict)
    for r in db.q("SELECT city, brand, COUNT(*) n, SUM(available) a, MIN(price) p FROM channel_listings WHERE city<>'' AND last_seen>=? GROUP BY city, brand", (_since(7),)):
        grid[r["city"]][r["brand"]] = {"listed": r["n"], "in_stock": r["a"] or 0, "from_price": r["p"]}
    out = []
    for name, c in cities.items():
        brands = grid.get(name, {})
        out.append({"city": name, "state": c.get("state"), "lat": c["lat"], "lon": c["lon"], "brands": brands,
                    "brand_count": len(brands), "pebble": brands.get(_me(), {}).get("in_stock", 0)})
    return {"cities": out, "brands": sorted({b for g in grid.values() for b in g})}


def price_index(days: int = 30) -> list[dict]:
    """Per brand: how deep the marketplaces discount vs MRP, and where the lowest price sits (store vs Amazon vs Flipkart)."""
    me = _me()
    out = []
    for b in settings.brands:
        rows = db.q("SELECT channel, price, mrp FROM channel_listings WHERE brand=? AND price>0 AND last_seen>=?", (b.name, _since(days)))
        if not rows:
            continue
        disc = [1 - r["price"] / r["mrp"] for r in rows if r["mrp"] and r["mrp"] > r["price"]]
        by_ch = defaultdict(list)
        for r in rows:
            by_ch[r["channel"]].append(r["price"])
        store = db.q("SELECT price, compare_at FROM products WHERE brand=? AND price>0", (b.name,))
        sdisc = [1 - r["price"] / r["compare_at"] for r in store if r["compare_at"] and r["compare_at"] > r["price"]]
        out.append({"brand": b.name, "self": b.name == me, "listings": len(rows), "avg_discount_pct": round(100 * sum(disc) / len(disc)) if disc else 0,
                    "store_avg_discount_pct": round(100 * sum(sdisc) / len(sdisc)) if sdisc else None,
                    "median_by_channel": {k: sorted(v)[len(v) // 2] for k, v in by_ch.items()}})
    return sorted(out, key=lambda x: (not x["self"], -x["listings"]))
