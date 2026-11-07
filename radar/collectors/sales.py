"""Pebble's own sales, line by line, so the reports can show real money next to the rivals' proxies.

Two ways in, both optional:
  Shopify Admin API   set SHOPIFY_ADMIN_TOKEN (Admin API access token of a custom app with read_orders) and, if the
                      store's admin host differs from the storefront, SHOPIFY_STORE=<name>.myshopify.com. Every hour the
                      radar pulls new and updated orders (status any, since the last sync minus two days) and stores one
                      row per line item: day, order, SKU, title, channel (online store / POS / draft / app), state and
                      city from the shipping address, units, net revenue.
  CSV / JSON import   POST /api/sales/import (the Reports page has the form) or `python -m radar.collectors.sales file.csv`.
                      Columns, any order, case-insensitive: date, sku, title, channel, state, city, units, revenue
                      (order_ref optional). Use it for Amazon / Flipkart / offline sales reports exported elsewhere.
Rows are keyed by (source, order, sku), so re-importing a file is safe.
"""
from __future__ import annotations
import csv
import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from .. import db, schema_ext
from ..analytics import categorize
from ..config import settings
from .base import tracked, log

API_VERSION = "2025-01"
COLS = {"day": ("date", "day", "order_date", "created_at", "order date"), "order_ref": ("order", "order_ref", "order id", "order_id", "order name", "name"),
        "sku": ("sku", "variant sku", "product code", "asin", "fsn"), "title": ("title", "product", "product title", "item", "lineitem name", "name"),
        "channel": ("channel", "source", "marketplace", "platform"), "state": ("state", "province", "shipping province", "shipping state", "region"),
        "city": ("city", "shipping city"), "units": ("units", "qty", "quantity", "lineitem quantity", "orders"), "revenue": ("revenue", "amount", "sales", "net sales", "total", "value", "lineitem price")}


def _norm_state(s: str) -> str:
    s = (s or "").strip()
    fixes = {"NCT of Delhi": "Delhi", "Delhi NCR": "Delhi", "Bangalore": "Karnataka", "Orissa": "Odisha", "Pondicherry": "Puducherry",
             "Uttaranchal": "Uttarakhand", "Jammu & Kashmir": "Jammu and Kashmir", "Andaman & Nicobar": "Andaman and Nicobar Islands"}
    return fixes.get(s, s.title() if s.islower() or s.isupper() else s)


def upsert(rows: list[dict], source: str) -> int:
    """Insert line rows (day, order_ref, sku, title, channel, state, city, units, revenue). Returns rows added."""
    schema_ext.ensure()
    n = 0
    with db.tx() as c:
        for r in rows:
            day = str(r.get("day") or "")[:10]
            if not re.match(r"\d{4}-\d{2}-\d{2}", day):
                continue
            title = str(r.get("title") or r.get("sku") or "")[:200]
            seg = r.get("segment") or categorize("", title)
            n += c.execute("INSERT OR REPLACE INTO sales(day,order_ref,sku,title,segment,channel,state,city,units,revenue,source,imported_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (day, str(r.get("order_ref") or f"{day}:{title[:40]}")[:80], str(r.get("sku") or title)[:80], title, seg,
                            str(r.get("channel") or "online store")[:60], _norm_state(str(r.get("state") or "")), str(r.get("city") or "").strip().title()[:60],
                            float(r.get("units") or 1), float(r.get("revenue") or 0), source, db.now())).rowcount
    return n


# ---------------------------------------------------------------- CSV / JSON import
def _num(v) -> float:
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)) or 0)
    except ValueError:
        return 0.0


def _date(v) -> str:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%b %d, %Y"):
        try:
            return datetime.strptime(s[:len(fmt) + 4] if "T" in fmt else s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def import_text(text: str, source: str = "csv") -> dict:
    """Parse CSV (or TSV) text with flexible headers and store it. Returns {rows, added, skipped, columns}."""
    text = text.lstrip("﻿")
    dialect = csv.excel_tab if text.count("\t") > text.count(",") else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    mapping = {}
    for field, names in COLS.items():
        for h in headers:
            if h in names and field not in mapping:
                mapping[field] = h
    rows = []
    for raw in reader:
        low = {k.strip().lower(): v for k, v in raw.items() if k}
        get = lambda f: low.get(mapping.get(f, ""), "")  # noqa: E731
        rows.append({"day": _date(get("day")), "order_ref": get("order_ref"), "sku": get("sku"), "title": get("title"), "channel": get("channel") or source,
                     "state": get("state"), "city": get("city"), "units": _num(get("units")) or 1, "revenue": _num(get("revenue"))})
    added = upsert(rows, source)
    return {"rows": len(rows), "added": added, "skipped": len(rows) - added, "columns": mapping}


def import_json(items: list[dict], source: str = "api") -> dict:
    rows = [{**it, "day": _date(it.get("day") or it.get("date")), "units": _num(it.get("units") or 1), "revenue": _num(it.get("revenue"))} for it in items]
    added = upsert(rows, source)
    return {"rows": len(rows), "added": added, "skipped": len(rows) - added}


# ---------------------------------------------------------------- Shopify Admin API
def shopify_store() -> str | None:
    s = os.environ.get("SHOPIFY_STORE")
    if s:
        return s.replace("https://", "").strip("/")
    me = settings.self_brand
    return me.host if me and me.shopify else None


def fetch_orders(since_iso: str) -> list[dict]:
    from .base import session
    token, store = os.environ.get("SHOPIFY_ADMIN_TOKEN"), shopify_store()
    if not token or not store:
        return []
    url = f"https://{store}/admin/api/{API_VERSION}/orders.json"
    params = {"status": "any", "limit": 250, "updated_at_min": since_iso, "fields": "id,name,created_at,source_name,line_items,shipping_address,"
              "financial_status,cancelled_at,total_discounts,current_total_price"}
    out = []
    for _ in range(40):
        r = session.get(url, params=params, headers={"X-Shopify-Access-Token": token, "Accept": "application/json"}, timeout=60)
        if r.status_code != 200:
            log.warning("shopify orders -> %s %s", r.status_code, r.text[:200])
            break
        out += (r.json().get("orders") or [])
        link = r.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not m:
            break
        url, params = m.group(1), {}
    return out


def rows_from_orders(orders: list[dict]) -> list[dict]:
    rows = []
    for o in orders:
        if o.get("cancelled_at") or o.get("financial_status") in ("voided", "refunded"):
            continue
        addr = o.get("shipping_address") or {}
        day = (o.get("created_at") or "")[:10]
        for li in o.get("line_items") or []:
            qty = float(li.get("quantity") or 0)
            price = float(li.get("price") or 0) * qty - sum(float(d.get("amount") or 0) for d in li.get("discount_allocations") or [])
            rows.append({"day": day, "order_ref": o.get("name") or str(o.get("id")), "sku": li.get("sku") or str(li.get("variant_id") or li.get("product_id")),
                         "title": li.get("title") or "", "channel": {"web": "online store", "pos": "POS", "shopify_draft_order": "draft"}.get(o.get("source_name"), o.get("source_name") or "online store"),
                         "state": addr.get("province") or "", "city": addr.get("city") or "", "units": qty, "revenue": round(price, 2)})
    return rows


@tracked("sales")
def run() -> int:
    schema_ext.ensure()
    if not os.environ.get("SHOPIFY_ADMIN_TOKEN"):
        return 0
    last = db.kv_get("sales_sync_at") or (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
    since = (datetime.fromisoformat(last) - timedelta(days=2)).isoformat(timespec="seconds")
    orders = fetch_orders(since)
    added = upsert(rows_from_orders(orders), "shopify")
    db.kv_set("sales_sync_at", db.now())
    log.info("sales: %s orders, %s line rows", len(orders), added)
    return 0  # facts, not events


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        print(import_text(open(sys.argv[1], encoding="utf-8-sig").read(), source=sys.argv[2] if len(sys.argv) > 2 else "csv"))
    else:
        print(run())
