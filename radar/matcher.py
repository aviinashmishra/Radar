"""Product matcher: pairs every Pebble SKU with its nearest rival SKUs (same category, similar spec words,
nearby price). Powers the price-position index and the `undercut` signal: when a matched rival drops below
Pebble's price the radar says which Pebble SKU is exposed and by how much.
"""
from __future__ import annotations
import re
from collections import defaultdict
from rapidfuzz import fuzz
from . import db, analytics
from .config import settings

SPEC_RX = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|inch|\"|mah|hrs?|hours|w\b|db|atm|ip\d{2}|khz|bt\s?\d)", re.I)
STOP = {"with", "and", "the", "for", "pro", "max", "plus", "new", "wireless", "bluetooth", "smart", "smartwatch", "watch",
        "earbuds", "tws", "black", "blue", "green", "grey", "gray", "silver", "gold", "white", "red", "pink", "edition"}


def _tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9.]+", title.lower()) if len(w) > 2 and w not in STOP}


def _specs(title: str) -> set[str]:
    return {f"{a}{b.lower()}" for a, b in SPEC_RX.findall(title)}


def score(a: dict, b: dict) -> float:
    """0-100. Category must match; price proximity and shared spec tokens dominate; fuzzy title helps."""
    if a["category"] != b["category"] or not a["price"] or not b["price"]:
        return 0.0
    ratio = min(a["price"], b["price"]) / max(a["price"], b["price"])
    price_s = max(0.0, (ratio - 0.5) / 0.5) * 45          # 1.0 -> 45, 0.5 -> 0
    spec_s = min(len(_specs(a["title"]) & _specs(b["title"])), 3) * 10
    tok = len(_tokens(a["title"]) & _tokens(b["title"]))
    tok_s = min(tok, 3) * 5
    fuzzy = fuzz.token_set_ratio(a["title"], b["title"]) * 0.10
    return round(price_s + spec_s + tok_s + fuzzy, 1)


def rebuild(top_k: int = 3) -> int:
    me = settings.self_brand.name if settings.self_brand else ""
    rows = db.q("SELECT brand, product_id, title, product_type, tags, price, available, handle FROM products WHERE price>0")
    for r in rows:
        r["category"] = analytics.categorize(r["product_type"], r["title"], r["tags"] or "")
    skip = ("Accessory", "Other", "Camera & home")  # too heterogeneous to pair SKUs meaningfully
    mine = [r for r in rows if r["brand"] == me and r["category"] not in skip]
    rivals = defaultdict(list)
    for r in rows:
        if r["brand"] != me and r["category"] not in skip:
            rivals[r["category"]].append(r)
    ts = db.now()
    n = 0
    with db.tx() as c:
        c.execute("DELETE FROM product_matches")
        for m in mine:
            cands = sorted(((score(m, r), r) for r in rivals.get(m["category"], [])), key=lambda x: -x[0])
            # best match per rival brand, then top_k overall
            seen_brand, picked = set(), []
            for s, r in cands:
                if s < 35 or r["brand"] in seen_brand:
                    continue
                seen_brand.add(r["brand"])
                picked.append((s, r))
                if len(picked) >= top_k:
                    break
            for s, r in picked:
                c.execute("INSERT OR REPLACE INTO product_matches(self_id,brand,product_id,score,self_title,rival_title,category,"
                          "self_price,rival_price,computed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (m["product_id"], r["brand"], r["product_id"], s, m["title"], r["title"], m["category"], m["price"], r["price"], ts))
                n += 1
    return n


def matches() -> list[dict]:
    me = settings.self_brand.name if settings.self_brand else ""
    rows = db.q("SELECT pm.*, p.handle AS rival_handle, p.available AS rival_available, sp.handle AS self_handle "
                "FROM product_matches pm JOIN products p ON p.brand=pm.brand AND p.product_id=pm.product_id "
                "JOIN products sp ON sp.brand=? AND sp.product_id=pm.self_id ORDER BY pm.self_id, pm.score DESC", (me,))
    out = defaultdict(lambda: {"matches": []})
    for r in rows:
        o = out[r["self_id"]]
        o.update({"self_id": r["self_id"], "self_title": r["self_title"], "self_price": r["self_price"], "category": r["category"],
                  "self_url": settings.self_brand.product_url(r["self_handle"]) if settings.self_brand else ""})
        b = settings.brand(r["brand"])
        o["matches"].append({"brand": r["brand"], "title": r["rival_title"], "price": r["rival_price"], "score": r["score"],
                             "available": bool(r["rival_available"]), "gap_pct": round(100 * (r["self_price"] - r["rival_price"]) / r["rival_price"]) if r["rival_price"] else None,
                             "url": b.product_url(r["rival_handle"]) if b else ""})
    res = list(out.values())
    for o in res:
        gaps = [m["gap_pct"] for m in o["matches"] if m["gap_pct"] is not None]
        o["avg_gap_pct"] = round(sum(gaps) / len(gaps)) if gaps else None
    return sorted(res, key=lambda o: -(o["avg_gap_pct"] or -999))


def undercuts(days: int = 2) -> list[dict]:
    """Matched rivals that moved below Pebble's price in the last N days (via price_drop events)."""
    out = []
    for e in db.q("SELECT brand, data, created_at FROM events WHERE kind='price_drop' AND created_at>=datetime('now', ?)", (f"-{days} days",)):
        import json
        d = json.loads(e["data"] or "{}")
        for m in db.q("SELECT * FROM product_matches WHERE brand=? AND product_id=?", (e["brand"], d.get("product_id"))):
            if d.get("new") and m["self_price"] and d["new"] < m["self_price"] * 0.9:
                out.append({"brand": e["brand"], "rival_title": m["rival_title"], "rival_price": d["new"], "self_title": m["self_title"],
                            "self_price": m["self_price"], "self_id": m["self_id"], "pct_below": round(100 * (1 - d["new"] / m["self_price"]))})
    return out
