"""Review velocity + pain-point mining through the Judge.me widget (used by most of the tracked stores).

For each store's top best-sellers we read total review count and the newest reviews. Review growth per
week is a free sales proxy; 1-2 star bodies are the complaints Pebble can design and advertise against.
Events: review_surge, complaint.
"""
from __future__ import annotations
import html as htmllib
import re
from .. import db
from ..config import settings
from .base import get, tracked, log

WIDGET = "https://judge.me/reviews/reviews_for_widget"
TOP_N = 12


def fetch(shop: str, domain: str, product_id: int, per_page: int = 10) -> dict | None:
    r = get(WIDGET, params={"url": shop, "shop_domain": domain, "platform": "shopify", "per_page": per_page,
                            "product_id": product_id, "sort_by": "created_at", "sort_dir": "desc"}, timeout=25, min_gap=1.0)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    h = d.get("html", "") or ""
    reviews = []
    for block in re.split(r"<div class='jdgm-rev ", h)[1:]:
        score = re.search(r"data-score='(\d)'", block)
        date = re.search(r"data-content='([^']+)'", block)
        title = re.search(r"jdgm-rev__title'>([^<]*)<", block)
        body = re.search(r"jdgm-rev__body'>(.*?)</div>", block, re.S)
        rid = re.search(r"data-review-id='([^']+)'", block)
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", body.group(1))).strip() if body else ""
        text = re.sub(r"\s+", " ", text)
        reviews.append({"key": rid.group(1) if rid else f"{date.group(1) if date else ''}|{text[:40]}",
                        "rating": int(score.group(1)) if score else None, "title": htmllib.unescape(title.group(1)).strip() if title else "",
                        "body": text[:1000], "created": date.group(1)[:19] if date else ""})
    return {"total": int(d.get("total_count") or 0), "reviews": reviews}


@tracked("reviews")
def run() -> int:
    n = 0
    stacks = {r["brand"]: r for r in db.q("SELECT brand, shop, apps FROM techstack")}
    for b in settings.brands:
        st = stacks.get(b.name)
        if not b.shopify or not st or not st["shop"] or "Judge.me" not in (st["apps"] or ""):
            continue
        domain = b.host
        tops = db.q("SELECT p.product_id, p.title, p.handle FROM bestseller_rank r JOIN products p ON p.brand=r.brand AND "
                    "p.product_id=r.product_id WHERE r.brand=? ORDER BY r.rank LIMIT ?", (b.name, TOP_N))
        for p in tops:
            try:
                d = fetch(st["shop"], domain, p["product_id"])
            except Exception as e:  # noqa: BLE001
                log.info("judge.me %s/%s failed: %s", b.name, p["product_id"], e)
                continue
            if d is None:
                continue
            prev = db.q1("SELECT total, seen_at FROM review_stats WHERE brand=? AND product_id=? ORDER BY id DESC LIMIT 1", (b.name, p["product_id"]))
            with db.tx() as c:
                c.execute("INSERT INTO review_stats(brand,product_id,total,seen_at) VALUES(?,?,?,?)", (b.name, p["product_id"], d["total"], db.now()))
                for rv in d["reviews"]:
                    c.execute("INSERT OR IGNORE INTO reviews(brand,product_id,review_key,rating,title,body,created,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                              (b.name, p["product_id"], rv["key"], rv["rating"], rv["title"], rv["body"], rv["created"], db.now()))
            url = f"{b.shopify}/products/{p['handle']}"
            if prev and d["total"] - prev["total"] >= max(5, int(prev["total"] * 0.05)):
                if db.add_event(b.name, "reviews", "review_surge", f"{p['title'][:80]} gained {d['total'] - prev['total']} reviews (now {d['total']})",
                                detail="Review velocity tracks sales. Something is moving this SKU.", url=url,
                                data={"product_id": p["product_id"], "total": d["total"], "prev": prev["total"]},
                                dedupe_key=f"reviews|{b.name}|{p['product_id']}|{d['total']}"):
                    n += 1
            if prev:  # complaints only after the baseline, so day one is not 100 old reviews
                for rv in d["reviews"]:
                    if rv["rating"] and rv["rating"] <= 2 and len(rv["body"]) > 25:
                        if db.add_event(b.name, "reviews", "complaint", f"{rv['rating']}★ on {p['title'][:60]}: {rv['body'][:140]}",
                                        detail=rv["body"][:800], url=url, data={"product_id": p["product_id"], "rating": rv["rating"]},
                                        dedupe_key=f"complaint|{b.name}|{p['product_id']}|{rv['key']}"):
                            n += 1
    return n
