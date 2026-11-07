"""Distribution analytics over channel_listings / channel_history / channel_mentions: the brand x channel matrix,
one brand's shelves, price parity between a brand's own store and the marketplaces, and the quick-commerce city map."""
from __future__ import annotations
import json
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


def parity(name: str | None = None) -> list[dict]:
    """Marketplace price vs the brand's own store for matched products (the channel_undercut events, latest per listing)."""
    sql, p = ("SELECT id, brand, created_at, title, url, data FROM events WHERE kind='channel_undercut' AND created_at>=?", [_since(45)])
    if name:
        sql += " AND brand=?"; p.append(name)
    seen, out = set(), []
    for e in db.q(sql + " ORDER BY id DESC", p):
        d = json.loads(e["data"] or "{}")
        k = (d.get("channel"), d.get("item_key"))
        if k in seen:
            continue
        seen.add(k)
        out.append({"brand": e["brand"], "channel": d.get("channel"), "title": e["title"], "marketplace_price": d.get("price"), "store_price": d.get("store_price"),
                    "gap_pct": d.get("gap_pct"), "url": e["url"], "store_url": d.get("store_url"), "when": e["created_at"][:10]})
    return out


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
