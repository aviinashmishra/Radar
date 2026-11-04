"""Shopify storefront collector.

Everything here is public, unauthenticated JSON that every Shopify store exposes:
  /products.json          full catalogue with variants, prices, stock flags, tags, timestamps
  /collections.json       collections (a new one usually means a campaign or a sale)
  /collections/<h>/products.json   ordered products - for the best-seller collection that order IS sales rank
  /sitemap_products_1.xml products that exist but may not be listed yet (pre-launch / hidden)

Events produced: new_product, price_drop, price_rise, discount_started, discount_ended, stock_out, restock,
delisted, new_collection, bestseller_move, hidden_product, sale_started.
"""
from __future__ import annotations
import json
import re
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone
from .. import db
from ..config import settings
from .base import Unavailable, get, get_json, tracked

BEST_HANDLE_RX = re.compile(r"best|top-?sell|trending|most-?popular|bestseller", re.I)
PRELAUNCH_RX = re.compile(r"coming[- ]soon|pre-?order|pre-?launch|launching|new[- ]launch|teaser|upcoming", re.I)


def _product_row(brand: str, p: dict) -> dict:
    variants = p.get("variants") or []
    prices = [float(v["price"]) for v in variants if v.get("price") not in (None, "")]
    price = min(prices) if prices else None
    # compare_at for the cheapest variant, if any
    compare_at = None
    if price is not None:
        for v in variants:
            if v.get("price") not in (None, "") and float(v["price"]) == price and v.get("compare_at_price"):
                compare_at = float(v["compare_at_price"])
                break
    available = int(any(v.get("available") for v in variants)) if variants else 0
    image = (p.get("images") or [{}])[0].get("src", "") if p.get("images") else ""
    return {
        "brand": brand, "product_id": int(p["id"]), "handle": p.get("handle", ""), "title": p.get("title", ""),
        "product_type": (p.get("product_type") or "").strip(), "vendor": p.get("vendor", ""),
        "tags": json.dumps(p.get("tags") or []), "price": price, "compare_at": compare_at,
        "available": available, "variants": len(variants), "image": image,
        "published_at": p.get("published_at") or "", "created_at": p.get("created_at") or "",
    }


def fetch_catalogue(base: str) -> list[dict]:
    out, page = [], 1
    while page <= 20:
        data = get_json(f"{base}/products.json?limit=250&page={page}")
        items = (data or {}).get("products") or []
        if not items:
            break
        out.extend(items)
        if len(items) < 250:
            break
        page += 1
    return out


def fetch_sitemap_handles(base: str) -> set[str]:
    handles: set[str] = set()
    for i in (1, 2, 3):
        try:
            r = get(f"{base}/sitemap_products_{i}.xml?from=1&to=99999999999", timeout=20)
        except Exception:
            break
        if r.status_code != 200 or "<urlset" not in r.text[:2000]:
            break
        # sitemaps percent-encode handles (beast%E2%84%A2) while products.json has them decoded (beast™); compare decoded
        handles |= {unquote(h) for h in re.findall(r"/products/([a-z0-9\-_%.]+)</loc>", r.text, re.I)}
    return handles


def _pct(old: float, new: float) -> float:
    return (new - old) / old * 100 if old else 0.0


@tracked("shopify")
def run_brand(brand_name: str) -> int:
    brand = settings.brand(brand_name)
    if not brand or not brand.shopify:
        return 0
    base = brand.shopify
    products = fetch_catalogue(base)
    if not products:
        r = get(f"{base}/products.json?limit=1")
        moved = r.url.split("//", 1)[-1].split("/", 1)[0]
        why = f"HTTP {r.status_code}" + (f", now redirects to {moved}" if moved != brand.host else "")
        had = db.q1("SELECT COUNT(*) c FROM products WHERE brand=?", (brand.name,))["c"]
        if had:  # a store that used to answer and stopped is news in itself (replatform, block, shutdown)
            db.add_event(brand.name, "shopify", "store_unreachable", f"{brand.name} store stopped serving its catalogue ({why})",
                         detail=f"{had} products were tracked until now; their last known prices stay in the radar.",
                         url=r.url, data={"status": r.status_code, "now_at": moved, "products_tracked": had},
                         dedupe_key=f"shopify|{brand.name}|unreachable|{moved}|{r.status_code}")
        raise Unavailable(f"catalogue unavailable: {why}")

    known = {r["product_id"]: r for r in db.q("SELECT * FROM products WHERE brand=?", (brand.name,))}
    first_run = not known
    ts = db.now()
    n_events = 0
    drops = []
    seen_ids = set()

    rows = [_product_row(brand.name, p) for p in products]
    with db.tx() as c:
        for row in rows:
            pid = row["product_id"]
            seen_ids.add(pid)
            old = known.get(pid)
            changed = False
            if old is None:
                c.execute(
                    "INSERT INTO products(brand,product_id,handle,title,product_type,vendor,tags,price,compare_at,"
                    "available,variants,image,published_at,created_at,first_seen,last_seen,last_changed) "
                    "VALUES(:brand,:product_id,:handle,:title,:product_type,:vendor,:tags,:price,:compare_at,"
                    ":available,:variants,:image,:published_at,:created_at,:ts,:ts,:ts)", {**row, "ts": ts})
                c.execute("INSERT INTO price_history(brand,product_id,price,compare_at,available,seen_at) "
                          "VALUES(?,?,?,?,?,?)", (brand.name, pid, row["price"], row["compare_at"], row["available"], ts))
                if not first_run:
                    changed = True
                continue

            # ---- diffs against the last snapshot ----
            op, np_ = old["price"], row["price"]
            if op and np_ and abs(op - np_) >= 1:
                pct = _pct(op, np_)
                kind = "price_drop" if np_ < op else "price_rise"
                if abs(pct) >= 2:
                    changed = True
                    if kind == "price_drop":
                        drops.append((row["title"], op, np_, pct))
                    if db.add_event(brand.name, "shopify", kind,
                                    f"{row['title']}: {settings.currency} {op:,.0f} -> {np_:,.0f} ({pct:+.0f}%)",
                                    detail=f"type={row['product_type'] or 'n/a'} compare_at={row['compare_at']}",
                                    url=f"{base}/products/{row['handle']}",
                                    data={"product_id": pid, "old": op, "new": np_, "pct": round(pct, 1),
                                          "product_type": row["product_type"], "image": row["image"]},
                                    dedupe_key=f"shopify|{brand.name}|{kind}|{pid}|{np_}"):
                        n_events += 1
            oc, nc = old["compare_at"], row["compare_at"]
            if (oc or 0) != (nc or 0) and row["price"]:
                if nc and nc > row["price"] and not (oc and oc > (op or 0)):
                    changed = True
                    off = _pct(nc, row["price"])
                    if db.add_event(brand.name, "shopify", "discount_started",
                                    f"{row['title']} now {abs(off):.0f}% off (MRP {nc:,.0f} -> {row['price']:,.0f})",
                                    url=f"{base}/products/{row['handle']}",
                                    data={"product_id": pid, "mrp": nc, "price": row["price"], "image": row["image"],
                                          "product_type": row["product_type"]},
                                    dedupe_key=f"shopify|{brand.name}|discount_started|{pid}|{row['price']}|{nc}"):
                        n_events += 1
                elif oc and not nc:
                    changed = True
                    if db.add_event(brand.name, "shopify", "discount_ended",
                                    f"{row['title']} discount removed (was MRP {oc:,.0f})",
                                    url=f"{base}/products/{row['handle']}",
                                    data={"product_id": pid, "product_type": row["product_type"]},
                                    dedupe_key=f"shopify|{brand.name}|discount_ended|{pid}|{ts[:10]}"):
                        n_events += 1
            if old["available"] != row["available"]:
                changed = True
                kind = "stock_out" if not row["available"] else "restock"
                rank = db.q1("SELECT rank FROM bestseller_rank WHERE brand=? AND product_id=?", (brand.name, pid))
                if db.add_event(brand.name, "shopify", kind,
                                f"{row['title']} {'went OUT OF STOCK' if kind == 'stock_out' else 'is back in stock'}"
                                + (f" (best-seller #{rank['rank']})" if rank else ""),
                                url=f"{base}/products/{row['handle']}",
                                data={"product_id": pid, "bestseller_rank": rank["rank"] if rank else None,
                                      "price": row["price"], "product_type": row["product_type"], "image": row["image"]},
                                dedupe_key=f"shopify|{brand.name}|{kind}|{pid}|{ts[:13]}"):
                    n_events += 1
            if old["title"] != row["title"] and old["title"]:
                changed = True
                db.add_event(brand.name, "shopify", "renamed", f"'{old['title']}' renamed to '{row['title']}'",
                             url=f"{base}/products/{row['handle']}", data={"product_id": pid},
                             dedupe_key=f"shopify|{brand.name}|renamed|{pid}|{row['title']}")
            c.execute(
                "UPDATE products SET handle=:handle,title=:title,product_type=:product_type,vendor=:vendor,tags=:tags,"
                "price=:price,compare_at=:compare_at,available=:available,variants=:variants,image=:image,"
                "published_at=:published_at,created_at=:created_at,last_seen=:ts"
                + (",last_changed=:ts" if changed else "") + " WHERE brand=:brand AND product_id=:product_id",
                {**row, "ts": ts})
            if changed:
                c.execute("INSERT INTO price_history(brand,product_id,price,compare_at,available,seen_at) "
                          "VALUES(?,?,?,?,?,?)", (brand.name, pid, row["price"], row["compare_at"], row["available"], ts))

    # ---- new products (after the tx so events see the rows) ----
    if not first_run:
        for row in rows:
            if row["product_id"] not in known:
                tags = row["tags"].lower()
                pre = bool(PRELAUNCH_RX.search(tags + " " + row["title"]))
                if db.add_event(brand.name, "shopify", "new_product",
                                f"NEW: {row['title']} at {settings.currency} {row['price'] or 0:,.0f}"
                                + (" [pre-launch tag]" if pre else ""),
                                detail=f"type={row['product_type'] or 'n/a'} variants={row['variants']} "
                                       f"in_stock={bool(row['available'])} tags={row['tags'][:200]}",
                                url=f"{base}/products/{row['handle']}",
                                data={"product_id": row["product_id"], "price": row["price"], "image": row["image"],
                                      "product_type": row["product_type"], "prelaunch": pre},
                                dedupe_key=f"shopify|{brand.name}|new_product|{row['product_id']}"):
                    n_events += 1
        # delisted = known before, absent now
        for pid, old in known.items():
            if pid not in seen_ids and old["last_seen"] and old["last_seen"] >= (
                    datetime.now(timezone.utc) - timedelta(days=2)).isoformat():
                if db.add_event(brand.name, "shopify", "delisted", f"{old['title']} removed from store",
                                data={"product_id": pid, "last_price": old["price"], "product_type": old["product_type"]},
                                dedupe_key=f"shopify|{brand.name}|delisted|{pid}"):
                    n_events += 1
        # many drops at once = a sale just started
        if len(drops) >= max(5, int(0.08 * len(rows))):
            avg = sum(d[3] for d in drops) / len(drops)
            if db.add_event(brand.name, "shopify", "sale_started",
                            f"SALE: {len(drops)} products cut by avg {abs(avg):.0f}% in one sweep",
                            detail="; ".join(f"{t} {o:,.0f}->{n:,.0f}" for t, o, n, _ in drops[:12]),
                            data={"count": len(drops), "avg_pct": round(avg, 1)},
                            dedupe_key=f"shopify|{brand.name}|sale_started|{ts[:10]}"):
                n_events += 1
    else:
        db.add_event(brand.name, "shopify", "baseline",
                     f"Radar started tracking {brand.name}: {len(rows)} products, "
                     f"{sum(1 for r in rows if r['available'])} in stock, "
                     f"{sum(1 for r in rows if r['compare_at'] and r['price'] and r['compare_at'] > r['price'])} discounted",
                     data={"products": len(rows)}, dedupe_key=f"shopify|{brand.name}|baseline")
        # products created in the last 14 days are "recent launches" even on the first run
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        for row in rows:
            if row["created_at"] and row["created_at"][:19] >= cutoff[:19]:
                if db.add_event(brand.name, "shopify", "new_product",
                                f"RECENT LAUNCH: {row['title']} at {settings.currency} {row['price'] or 0:,.0f} "
                                f"(added {row['created_at'][:10]})",
                                detail=f"type={row['product_type'] or 'n/a'} in_stock={bool(row['available'])}",
                                url=f"{base}/products/{row['handle']}",
                                data={"product_id": row["product_id"], "price": row["price"], "image": row["image"],
                                      "product_type": row["product_type"]},
                                dedupe_key=f"shopify|{brand.name}|new_product|{row['product_id']}"):
                    n_events += 1

    n_events += _collections(brand, base, first_run)
    n_events += _bestsellers(brand, base, first_run)
    n_events += _hidden(brand, base, {r["handle"] for r in rows})
    return n_events


def _collections(brand, base, first_run) -> int:
    data = get_json(f"{base}/collections.json?limit=250")
    cols = (data or {}).get("collections") or []
    if not cols:
        return 0
    known = {r["handle"] for r in db.q("SELECT handle FROM collections WHERE brand=?", (brand.name,))}
    ts, n = db.now(), 0
    with db.tx() as c:
        for col in cols:
            h = col.get("handle", "")
            c.execute("INSERT INTO collections(brand,handle,title,products_count,first_seen,last_seen) VALUES(?,?,?,?,?,?) "
                      "ON CONFLICT(brand,handle) DO UPDATE SET title=excluded.title, products_count=excluded.products_count,"
                      " last_seen=excluded.last_seen",
                      (brand.name, h, col.get("title", ""), col.get("products_count", 0), ts, ts))
    if not first_run:
        for col in cols:
            if col.get("handle") not in known:
                if db.add_event(brand.name, "shopify", "new_collection",
                                f"New collection '{col.get('title')}' ({col.get('products_count', 0)} products)",
                                url=f"{base}/collections/{col.get('handle')}",
                                data={"handle": col.get("handle"), "count": col.get("products_count", 0)},
                                dedupe_key=f"shopify|{brand.name}|new_collection|{col.get('handle')}"):
                    n += 1
    return n


def _bestsellers(brand, base, first_run) -> int:
    """Rank movement inside the best-seller collection (collection order = sales order on Shopify)."""
    cols = db.q("SELECT handle,title FROM collections WHERE brand=?", (brand.name,))
    handle = next((c["handle"] for c in cols if BEST_HANDLE_RX.search(c["handle"] + " " + (c["title"] or ""))), None)
    url = f"{base}/collections/{handle}/products.json?limit=50" if handle else f"{base}/collections/all/products.json?limit=50"
    data = get_json(url)
    items = (data or {}).get("products") or []
    if not items:
        return 0
    old = {r["product_id"]: r["rank"] for r in db.q("SELECT product_id,rank FROM bestseller_rank WHERE brand=?", (brand.name,))}
    ts, n = db.now(), 0
    with db.tx() as c:
        c.execute("DELETE FROM bestseller_rank WHERE brand=?", (brand.name,))
        for i, p in enumerate(items, 1):
            c.execute("INSERT INTO bestseller_rank(brand,product_id,rank,seen_at) VALUES(?,?,?,?)", (brand.name, int(p["id"]), i, ts))
    if first_run or not old:
        return 0
    for i, p in enumerate(items, 1):
        pid = int(p["id"])
        prev = old.get(pid)
        if (prev is None and i <= 10) or (prev is not None and prev - i >= 5 and i <= 20):
            if db.add_event(brand.name, "shopify", "bestseller_move",
                            f"{p.get('title')} climbed to best-seller #{i}" + (f" (from #{prev})" if prev else " (new entry)"),
                            url=f"{base}/products/{p.get('handle')}",
                            data={"product_id": pid, "rank": i, "prev": prev, "collection": handle or "all"},
                            dedupe_key=f"shopify|{brand.name}|bestseller_move|{pid}|{i}"):
                n += 1
    return n


def _hidden(brand, base, listed_handles: set[str]) -> int:
    """Products present in the sitemap but absent from the public catalogue = unlisted / pre-launch pages.

    For each new hidden handle we also try /products/<handle>.json: a page that is published but kept out of
    listings answers with full title, price, type and variants, which is pre-launch intel worth reading.
    """
    sm = fetch_sitemap_handles(base)
    if not sm:
        return 0
    hidden = sorted(sm - listed_handles)
    n, probed = 0, 0
    for h in hidden[:40]:
        key = f"shopify|{brand.name}|hidden_product|{h}"
        if db.q1("SELECT 1 FROM events WHERE dedupe_key=?", (key,)):
            continue
        intel, title = {}, h.replace("-", " ")
        if probed < 8:
            probed += 1
            pj = get_json(f"{base}/products/{h}.json")
            p = (pj or {}).get("product")
            if p:
                row = _product_row(brand.name, p)
                title = row["title"]
                intel = {"price": row["price"], "compare_at": row["compare_at"], "product_type": row["product_type"],
                         "variants": row["variants"], "available": bool(row["available"]), "published_at": row["published_at"],
                         "tags": (p.get("tags") or [])[:10], "image": row["image"]}
        detail = ("Page is live but hidden from listings. " + f"Price {settings.currency} {intel['price']:,.0f}"
                  + (f" (MRP {intel['compare_at']:,.0f})" if intel.get("compare_at") else "") + f", type={intel['product_type'] or 'n/a'}, "
                  f"{intel['variants']} variants, {'in stock' if intel['available'] else 'not purchasable yet'}, tags={intel['tags']}"
                  ) if intel else "In sitemap_products.xml but not in /products.json. Page not published yet (404) or test page."
        if db.add_event(brand.name, "shopify", "hidden_product",
                        (f"PRE-LAUNCH PAGE: {title} at {settings.currency} {intel['price']:,.0f}" if intel and intel.get("price")
                         else f"Unlisted product page in sitemap: {title}"),
                        detail=detail, url=f"{base}/products/{h}", data={"handle": h, "prelaunch": bool(intel), **intel}, dedupe_key=key):
            n += 1
    return n


def run() -> int:
    total = 0
    for b in settings.brands:
        if b.shopify:
            total += run_brand(b.name)
    return total
