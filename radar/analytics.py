"""On-demand analytics for the dashboard and the chat: white-space map, self-benchmark, winner detector."""
from __future__ import annotations
import json
import re
from collections import defaultdict
from . import db
from .config import settings

CATEGORY_RULES = [
    ("Smart ring", r"smart ?ring|\bring\b(?!.*(light|charg|watch|stand|holder|lamp))"),
    ("Smart band", r"screenless|smart band|health band|fitness band|fitness tracker|\bband\b|\bqore\b|cirqa|\bwhoop\b|helio strap|fitbit air"),
    ("Smartwatch", r"smart ?watch|\bwatch\b"),
    ("TWS earbuds", r"\btws\b|earbud|\bbuds?\b|dockets|airdopes|true wireless|earpods"),
    ("Neckband", r"neckband|wireless earphone|bassheads"),
    ("Headphones", r"headphone|on-?ear|over-?ear"),
    ("Speaker", r"speaker|soundbar|\bstone\b|party"),
    ("Power bank", r"power ?bank|powerbank"),
    ("Charger & cable", r"charger|cable|adapter|wireless charg|\bgan\b"),
    ("Camera & home", r"camera|cctv|wifi|home|bulb|plug|strip"),
    ("Accessory", r"strap|case|cover|guard|stand|holder|accessor"),
]
# segment key (config/competitors.yaml) <-> category name used across the radar
SEGMENT_CATEGORY = {"band": "Smart band", "ring": "Smart ring", "watch": "Smartwatch", "earbuds": "TWS earbuds",
                    "neckband": "Neckband", "headphones": "Headphones", "speaker": "Speaker", "powerbank": "Power bank",
                    "charger": "Charger & cable"}
CATEGORY_SEGMENT = {v: k for k, v in SEGMENT_CATEGORY.items()}

_PART_RX = re.compile(r"\bstraps?\b|\bcase\b|cover|guard|protector|charging cable|charger for|dock for|accessor|sizing kit|combo|"
                      r"\b(sport|classic|silicone|metal|nylon|leather|milanese|loop|twill|dual tone)\s+band\b")
_POWER_RX = re.compile(r"\bcharger\b|\bcable\b|adapter|charging (pad|dock|station)|power ?bank")
_BAND_NAMES_RX = re.compile(r"helio strap|\bwhoop\b|cirqa|fitbit air|polar loop|rep - 24x7|\barc (note|pro|go)\b|\bqore\b")
_TITLE_FIRST = [("Smart ring", r"smart ?ring|\bring\b(?!.*(light|charg|watch|stand|holder|lamp))"),
                ("Smartwatch", r"smart ?watch"),
                ("Smart band", r"screenless|smart ?band|health band|fitness band|fitness tracker|\bqore\b|cirqa|\bwhoop\b|helio strap|fitbit air"),
                ("TWS earbuds", r"\btws\b|earbud|\bbuds?\b|dockets|airdopes|true wireless|earpods"),
                ("Neckband", r"neckband|bassheads"),
                ("Smartwatch", r"smart ?watch|\bwatch\b")]


def categorize(product_type: str, title: str, tags: str = "") -> str:
    pt = (product_type or "").lower()
    t = (title or "").lower()
    text = f"{pt} {t} {tags}".lower()
    is_part = bool(_PART_RX.search(t) or _PART_RX.search(pt))
    # 0) named screenless bands first ("Helio Strap" is a band, not a strap) - unless it is a part for one
    if _BAND_NAMES_RX.search(t) and not re.search(r"\bfor\b|\bset\b|combo|cable|charger|sizing|\bstraps\b", t):
        return "Smart band"
    # 1) parts and power accessories are never products of the segment they mention
    if is_part:
        return "Accessory"
    if _POWER_RX.search(t) and not re.search(r"\bwatch\b|earbud|speaker", t):
        return "Power bank" if re.search(r"power ?bank", t) else "Charger & cable"
    # 2) an explicit store product type wins for watches (Fire-Boltt "Ninja X Ring" is a smartwatch)
    if re.search(r"smart ?watch|\bwatch", pt) and not re.search(r"smart ?ring|\bband\b", t):
        return "Smartwatch"
    # 3) the title names the form factor more reliably than most stores' product_type ("Headphones" for earbuds)
    for name, rx in _TITLE_FIRST:
        if re.search(rx, t):
            return name
    for name, rx in CATEGORY_RULES:
        if re.search(rx, pt):
            return name
    for name, rx in CATEGORY_RULES:
        if re.search(rx, text):
            return name
    return "Other"


def _band(price: float) -> str:
    b = settings.price_bands
    for lo, hi in zip(b, b[1:]):
        if lo <= price < hi:
            return f"{lo:,}-{hi:,}" if hi < 10**6 else f"{lo:,}+"
    return "?"


def bands() -> list[str]:
    b = settings.price_bands
    return [f"{lo:,}-{hi:,}" if hi < 10**6 else f"{lo:,}+" for lo, hi in zip(b, b[1:])]


def whitespace() -> dict:
    """category x price band -> number of competitor SKUs, number of Pebble SKUs, example competitor products."""
    me = settings.self_brand.name if settings.self_brand else ""
    grid = defaultdict(lambda: {"competitors": 0, "self": 0, "brands": set(), "examples": []})
    for p in db.q("SELECT brand,title,product_type,tags,price,available FROM products WHERE price IS NOT NULL AND price>0"):
        cat = categorize(p["product_type"], p["title"], p["tags"] or "")
        if cat in ("Accessory", "Other"):
            continue
        cell = grid[(cat, _band(p["price"]))]
        if p["brand"] == me:
            cell["self"] += 1
        else:
            cell["competitors"] += 1
            cell["brands"].add(p["brand"])
            if len(cell["examples"]) < 3:
                cell["examples"].append(f"{p['brand']}: {p['title'][:40]} ({p['price']:,.0f})")
    cats = sorted({k[0] for k in grid})
    out = {"bands": bands(), "categories": cats, "cells": {}}
    for (cat, band), v in grid.items():
        out["cells"][f"{cat}|{band}"] = {"competitors": v["competitors"], "self": v["self"],
                                          "brands": sorted(v["brands"]), "examples": v["examples"],
                                          "gap": v["self"] == 0 and v["competitors"] >= 3}
    return out


def benchmark() -> list[dict]:
    """Pebble vs the field per category: median price, SKU count, in-stock %, discount depth."""
    me = settings.self_brand.name if settings.self_brand else ""
    rows = db.q("SELECT brand,title,product_type,tags,price,compare_at,available FROM products WHERE price>0")
    agg = defaultdict(lambda: defaultdict(list))
    for p in rows:
        cat = categorize(p["product_type"], p["title"], p["tags"] or "")
        if cat in ("Accessory", "Other"):
            continue
        agg[cat][p["brand"]].append(p)
    out = []
    for cat, brands in agg.items():
        def stats(ps):
            prices = sorted(x["price"] for x in ps)
            disc = [1 - x["price"] / x["compare_at"] for x in ps if x["compare_at"] and x["compare_at"] > x["price"]]
            return {"skus": len(ps), "median_price": prices[len(prices) // 2] if prices else None,
                    "min_price": prices[0] if prices else None,
                    "in_stock_pct": round(100 * sum(1 for x in ps if x["available"]) / len(ps)) if ps else 0,
                    "avg_discount_pct": round(100 * sum(disc) / len(disc)) if disc else 0}
        mine = stats(brands.get(me, []))
        others = [p for b, ps in brands.items() if b != me for p in ps]
        field = stats(others)
        per_brand = {b: stats(ps) for b, ps in brands.items() if b != me}
        out.append({"category": cat, "self": mine, "field": field, "brands": per_brand,
                    "price_gap_pct": round(100 * (mine["median_price"] - field["median_price"]) / field["median_price"])
                    if mine["median_price"] and field["median_price"] else None})
    return sorted(out, key=lambda x: -x["field"]["skus"])


def winners(limit: int = 25) -> list[dict]:
    """Which competitor products are actually selling: best-seller rank + restock churn + discount + ad longevity."""
    me = settings.self_brand.name if settings.self_brand else ""
    ranks = {(r["brand"], r["product_id"]): r["rank"] for r in db.q("SELECT brand,product_id,rank FROM bestseller_rank")}
    flips = defaultdict(int)
    for r in db.q("SELECT brand,product_id,COUNT(*) c FROM price_history GROUP BY brand,product_id HAVING c>1"):
        flips[(r["brand"], r["product_id"])] = r["c"] - 1
    out = []
    for p in db.q("SELECT brand,product_id,title,handle,product_type,price,compare_at,available,image FROM products WHERE price>0"):
        key = (p["brand"], p["product_id"])
        rank = ranks.get(key)
        if rank is None and flips[key] == 0:
            continue
        score = 0.0
        if rank:
            score += max(0, 30 - rank)
        score += min(flips[key], 10) * 2
        if p["compare_at"] and p["compare_at"] > p["price"]:
            score += 3
        if not p["available"]:
            score += 2  # selling out is a signal too
        out.append({**p, "rank": rank, "changes": flips[key], "score": round(score, 1),
                    "category": categorize(p["product_type"], p["title"]),
                    "url": f"{settings.brand(p['brand']).shopify}/products/{p['handle']}" if settings.brand(p["brand"]) and settings.brand(p["brand"]).shopify else ""})
    out = [o for o in out if o["brand"] != me]
    return sorted(out, key=lambda x: -x["score"])[:limit]


def pain_points(days: int = 30) -> list[dict]:
    """Complaint themes mined from social/news titles (cheap keyword pass; Claude does better via chat)."""
    themes = {"battery": r"battery|charge", "build": r"broke|break|crack|cheap build|quality", "software": r"app|bug|update|sync|bluetooth",
              "service": r"service|warranty|support|refund|replacement", "accuracy": r"accura|heart rate|spo2|step count",
              "sound": r"sound|bass|mic|call quality|noise cancel|anc", "display": r"display|screen|brightness"}
    rows = db.q("SELECT brand,title FROM events WHERE source IN ('reddit','news','feed') AND created_at>=datetime('now', ?)", (f"-{days} days",))
    counts = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for t, rx in themes.items():
            if re.search(rx, r["title"], re.I):
                counts[r["brand"]][t] += 1
    return [{"brand": b, "themes": dict(sorted(ts.items(), key=lambda x: -x[1]))} for b, ts in counts.items()]


def influencers(limit: int = 25) -> list[dict]:
    """Creators covering the sector: which brands they cover, how many videos, total views."""
    me = settings.self_brand.name if settings.self_brand else ""
    agg = defaultdict(lambda: {"videos": 0, "views": 0, "brands": defaultdict(int), "latest": "", "sample": ""})
    for v in db.q("SELECT channel, brand, views, published, title, video_id FROM videos WHERE channel<>'' ORDER BY views DESC"):
        a = agg[v["channel"]]
        a["videos"] += 1
        a["views"] += v["views"] or 0
        a["brands"][v["brand"]] += 1
        if not a["sample"]:
            a["sample"], a["latest"] = v["title"], v["video_id"]
    out = []
    for ch, a in agg.items():
        owned = next((b.name for b in settings.brands if b.name.lower().replace("-", "") in ch.lower().replace("-", "").replace(" ", "")), None)
        out.append({"channel": ch, "videos": a["videos"], "views": a["views"], "brands": dict(a["brands"]), "owned_by": owned,
                    "covers_pebble": me in a["brands"], "sample": a["sample"], "url": f"https://www.youtube.com/watch?v={a['latest']}"})
    # brand-owned channels last: the point of the map is independent creators Pebble can reach
    return sorted(out, key=lambda x: (x["owned_by"] is not None, -x["views"], -x["videos"]))[:limit]


def app_health() -> list[dict]:
    """Latest Play Store metrics per app, with the change since the first capture."""
    out = []
    for pkg in db.q("SELECT DISTINCT package FROM app_metrics"):
        cur = db.q1("SELECT * FROM app_metrics WHERE package=? ORDER BY id DESC LIMIT 1", (pkg["package"],))
        first = db.q1("SELECT rating, ratings_count, seen_at FROM app_metrics WHERE package=? ORDER BY id LIMIT 1", (pkg["package"],))
        out.append({**cur, "rating_delta": round(cur["rating"] - first["rating"], 2) if first and first["rating"] else 0,
                    "ratings_gained": (cur["ratings_count"] or 0) - (first["ratings_count"] or 0), "since": first["seen_at"] if first else cur["seen_at"]})
    return sorted(out, key=lambda x: -(x["ratings_count"] or 0))


def search_demand() -> dict:
    """Autocomplete suggestions grouped by seed, newest first; plus suggestions that mention each brand."""
    rows = db.q("SELECT seed, suggestion, brand, first_seen FROM search_suggestions ORDER BY seed, first_seen DESC")
    by_seed = defaultdict(list)
    for r in rows:
        by_seed[r["seed"].strip()].append({"q": r["suggestion"], "brand": r["brand"], "first_seen": r["first_seen"][:10]})
    by_brand = defaultdict(int)
    for r in rows:
        by_brand[r["brand"]] += 1
    return {"seeds": dict(by_seed), "brand_share": dict(sorted(by_brand.items(), key=lambda x: -x[1]))}


def sale_cadence() -> list[dict]:
    """How often each rival cuts prices: median days between discount sweeps, and the last one."""
    out = []
    for b in db.q("SELECT DISTINCT brand FROM events WHERE kind IN ('price_drop','sale_started')"):
        days = sorted({r["d"] for r in db.q("SELECT substr(created_at,1,10) d FROM events WHERE brand=? AND kind IN ('price_drop','sale_started')", (b["brand"],))})
        gaps = []
        from datetime import date
        for a, c in zip(days, days[1:]):
            gaps.append((date.fromisoformat(c) - date.fromisoformat(a)).days)
        gaps = [g for g in gaps if g > 0]
        med = sorted(gaps)[len(gaps) // 2] if gaps else None
        out.append({"brand": b["brand"], "drop_days": len(days), "median_gap_days": med, "last": days[-1] if days else None})
    return sorted(out, key=lambda x: -x["drop_days"])


def review_pain_points(days: int = 60) -> list[dict]:
    """Complaint themes from 1-2 star reviews, per brand."""
    themes = {"battery": r"battery|charg", "build quality": r"broke|break|crack|cheap|quality|flimsy|loose", "app / connectivity": r"app|bug|update|sync|bluetooth|connect|disconnect",
              "service / warranty": r"service|warranty|support|refund|replace|return", "accuracy": r"accura|heart rate|spo2|step|sleep|track",
              "sound / mic": r"sound|bass|mic|call|noise|anc|volume", "display": r"display|screen|bright|touch", "delivery": r"deliver|courier|late|packag"}
    rows = db.q("SELECT brand, product_id, rating, body FROM reviews WHERE rating<=2 AND created>=datetime('now', ?)", (f"-{days} days",))
    counts = defaultdict(lambda: defaultdict(int))
    examples = defaultdict(dict)
    for r in rows:
        for t, rx in themes.items():
            if re.search(rx, r["body"], re.I):
                counts[r["brand"]][t] += 1
                examples[r["brand"]].setdefault(t, r["body"][:160])
    return [{"brand": b, "low_star_reviews": sum(ts.values()), "themes": dict(sorted(ts.items(), key=lambda x: -x[1])), "examples": examples[b]}
            for b, ts in sorted(counts.items(), key=lambda x: -sum(x[1].values()))]


def segments_list() -> list[dict]:
    return [dict(sg, category=SEGMENT_CATEGORY.get(sg["key"], sg["name"])) for sg in settings.segments]


def _watchlist_rows(segment: str | None = None, brand: str | None = None) -> list[dict]:
    """Curated watchlist products (watchlist.yaml) with their latest live price. Filter by segment and/or brand."""
    from .collectors.watchlist import load as load_watchlist
    out = []
    for item in load_watchlist():
        if segment and item.get("segment") != segment:
            continue
        if brand and item.get("brand", "").lower() != brand.lower():
            continue
        latest = db.q1("SELECT price, status, source, seen_at FROM watch_prices WHERE name=? ORDER BY id DESC LIMIT 1", (item["name"],))
        price = (latest or {}).get("price") or item.get("price_inr")
        sub = item.get("subscription_inr_per_month")
        out.append({"name": item["name"], "brand": item["brand"], "price": price, "sheet_price": item.get("price_inr"),
                    "live": bool(latest and latest.get("source") not in (None, "sheet")), "status": (latest or {}).get("status") or item.get("status"),
                    "sheet_status": item.get("status"), "subscription": item.get("subscription", "None"), "sub_monthly": sub,
                    "tco12": (price + 12 * sub) if price and sub else price, "origin": item.get("origin"), "evidence": item.get("evidence"),
                    "url": item.get("url"), "note": item.get("note"), "self": bool(item.get("pebble_baseline")),
                    "delta_pct": round(100 * (price - item["price_inr"]) / item["price_inr"]) if price and item.get("price_inr") else None,
                    "seen_at": (latest or {}).get("seen_at"), "source": "watchlist", "segment": item.get("segment")})
    return out


def brand_meta(name: str) -> dict:
    """Static facts about a brand from competitors.yaml, with segment display names."""
    b = settings.brand(name)
    names = {sg["key"]: sg["name"] for sg in settings.segments}
    if not b:
        return {"segments": [], "segment_names": [], "origin": None, "tier": None, "watch_pages": [], "apps": [], "shopify": None}
    return {"segments": b.segments, "segment_names": [names.get(k, k) for k in b.segments], "origin": b.origin, "tier": b.tier,
            "watch_pages": b.watch_pages, "apps": b.apps, "shopify": b.shopify}


def segment_ladder(segment: str, per_brand: int = 6) -> dict:
    """Everything the radar knows about one segment: a price ladder of store SKUs + watchlist products,
    Pebble's position, and the last 14 days of events from brands in that segment."""
    me = settings.self_brand.name if settings.self_brand else ""
    cat = SEGMENT_CATEGORY.get(segment, segment)
    ranks = {(r["brand"], r["product_id"]): r["rank"] for r in db.q("SELECT brand, product_id, rank FROM bestseller_rank")}
    by_brand = defaultdict(list)
    for p in db.q("SELECT brand, product_id, title, product_type, tags, price, compare_at, available, handle FROM products WHERE price>0"):
        if categorize(p["product_type"], p["title"], p["tags"] or "") != cat:
            continue
        b = settings.brand(p["brand"])
        by_brand[p["brand"]].append({"name": p["title"], "brand": p["brand"], "price": p["price"], "compare_at": p["compare_at"],
                                     "status": "on sale" if p["available"] else "sold out", "self": p["brand"] == me,
                                     "rank": ranks.get((p["brand"], p["product_id"])), "source": "store",
                                     "origin": b.origin if b else "Indian", "subscription": "None",
                                     "url": f"{b.shopify}/products/{p['handle']}" if b and b.shopify else ""})
    store = []
    for brand, items in by_brand.items():
        items.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0, x["price"]))
        store += items[: (50 if brand == me else per_brand)]
    watch = _watchlist_rows(segment)

    def norm(name: str, brand: str) -> str:
        n = re.sub(r"[^a-z0-9]", "", name.lower().replace(brand.lower(), "").split("|")[0].replace("smart ring", "").replace("smart band", ""))
        return n
    watch_keys = {norm(w["name"], w["brand"]) for w in watch}
    ladder = watch + [s_ for s_ in store if norm(s_["name"], s_["brand"]) not in watch_keys]
    ladder.sort(key=lambda x: (x["price"] is None, x["price"] or 0))
    mine = [x["price"] for x in ladder if x["self"] and x["price"]]
    field = sorted(x["price"] for x in ladder if not x["self"] and x["price"])
    brands_in = sorted({x["brand"] for x in ladder if not x["self"]})
    seg_brands = [b.name for b in settings.brands if segment in b.segments]
    pulse = []
    for e in db.q("SELECT id, created_at, brand, source, kind, title, importance, so_what, counter_move, url, data FROM events "
                  "WHERE created_at>=datetime('now','-14 days') AND kind<>'baseline' ORDER BY importance DESC, id DESC LIMIT 400"):
        d = json.loads(e["data"] or "{}")
        in_seg = d.get("segment") == segment or (d.get("product_type") and categorize(d.get("product_type"), e["title"]) == cat) \
            or (e["brand"] in seg_brands and categorize("", e["title"]) == cat) \
            or (e["brand"] in seg_brands and len(settings.brand(e["brand"]).segments) == 1)
        if in_seg:
            e["data"] = d
            pulse.append(e)
        if len(pulse) >= 30:
            break
    return {"segment": segment, "category": cat, "ladder": ladder, "brands": brands_in,
            "stats": {"pebble_min": min(mine) if mine else None, "pebble_max": max(mine) if mine else None, "pebble_skus": len(mine),
                      "field_min": field[0] if field else None, "field_median": field[len(field) // 2] if field else None,
                      "field_max": field[-1] if field else None, "rivals": len(brands_in),
                      "subscription_mandatory": sum(1 for x in watch if x["subscription"] == "Mandatory"),
                      "global": sum(1 for x in ladder if x.get("origin") == "Global" and not x["self"]),
                      "independent_evidence": sum(1 for x in watch if str(x.get("evidence", "")).startswith("Independent"))},
            "pulse": pulse}


def watchlist_overview() -> list[dict]:
    out = []
    for sg in settings.segments:
        out += _watchlist_rows(sg["key"])
    return out
