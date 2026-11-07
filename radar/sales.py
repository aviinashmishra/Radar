"""Sales reporting: Pebble's own numbers (sales table) by day / week / month / year, segment, channel and region, with
growth against the previous period, and sales *proxies* for every brand the radar cannot invoice-check (review counts,
app rating counts, marketplace rating counts, best-seller ranks) turned into indexed unit estimates.
"""
from __future__ import annotations
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from . import db
from .analytics import categorize
from .config import settings
from .collectors.channels import config as channels_config

KINDS = ("daily", "weekly", "monthly", "yearly")


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def period_bounds(kind: str, period: str | None = None) -> tuple[str, str, str, str, str]:
    """(label, start, end_exclusive, prev_start, prev_end) for a period key: daily YYYY-MM-DD, weekly YYYY-Www, monthly YYYY-MM, yearly YYYY.
    Without a key: the last complete period (yesterday, last ISO week, last month, last year)."""
    today = datetime.now(timezone.utc).date()
    if kind == "daily":
        d = date.fromisoformat(period) if period else today - timedelta(days=1)
        return d.isoformat(), d.isoformat(), (d + timedelta(days=1)).isoformat(), (d - timedelta(days=1)).isoformat(), d.isoformat()
    if kind == "weekly":
        if period:
            y, w = period.split("-W")
            start = date.fromisocalendar(int(y), int(w), 1)
        else:
            start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=7)
        return start.strftime("%G-W%V"), start.isoformat(), end.isoformat(), (start - timedelta(days=7)).isoformat(), start.isoformat()
    if kind == "monthly":
        if period:
            y, m = map(int, period.split("-"))
        else:
            first = today.replace(day=1) - timedelta(days=1)
            y, m = first.year, first.month
        start = date(y, m, 1)
        end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        prev = date(y - (m == 1), 12 if m == 1 else m - 1, 1)
        return f"{y}-{m:02d}", start.isoformat(), end.isoformat(), prev.isoformat(), start.isoformat()
    y = int(period) if period else today.year  # the running year (year to date); earlier years from the period picker
    return str(y), f"{y}-01-01", f"{y + 1}-01-01", f"{y - 1}-01-01", f"{y}-01-01"


def _own(start: str, end: str) -> dict:
    rows = db.q("SELECT day, order_ref, sku, title, segment, channel, state, city, units, revenue FROM sales WHERE day>=? AND day<?", (start, end))
    tot = {"units": sum(r["units"] or 0 for r in rows), "revenue": sum(r["revenue"] or 0 for r in rows), "lines": len(rows),
           "orders": len({r["order_ref"] for r in rows})}
    by = {k: defaultdict(lambda: {"units": 0.0, "revenue": 0.0}) for k in ("segment", "channel", "state", "city", "sku", "day")}
    titles = {}
    for r in rows:
        for k in by:
            key = r[k] or ("unknown" if k != "day" else r["day"])
            by[k][key]["units"] += r["units"] or 0
            by[k][key]["revenue"] += r["revenue"] or 0
        titles[r["sku"]] = r["title"]
    def top(k, n=12):
        return [{"key": key, **v, **({"title": titles.get(key, "")} if k == "sku" else {})} for key, v in sorted(by[k].items(), key=lambda x: -x[1]["revenue"])[:n]]
    return {**tot, "by_segment": top("segment", 10), "by_channel": top("channel", 10), "by_state": top("state", 15), "by_city": top("city", 12),
            "top_skus": top("sku", 12), "by_day": sorted(({"day": d, **v} for d, v in by["day"].items()), key=lambda x: x["day"]),
            "aov": round(tot["revenue"] / tot["orders"]) if tot["orders"] else None}


def _growth(cur: float, prev: float) -> float | None:
    return round(100 * (cur / prev - 1), 1) if prev else None


def proxies(start: str, end: str) -> list[dict]:
    """Per brand: deltas of public counters inside the window, turned into unit estimates with the rates in channels.yaml."""
    rates = (channels_config().get("proxy") or {})
    r_mkt = float(rates.get("marketplace_ratings_per_100_units", 2.0))
    r_app = float(rates.get("app_ratings_per_100_units", 0.8))
    r_store = float(rates.get("store_reviews_per_100_units", 1.5))
    me = _me()
    out = []
    s_iso, e_iso = f"{start}T00:00:00", f"{end}T00:00:00"
    for b in settings.brands:
        # store reviews (Judge.me totals per product): last total before the window end minus last total before the start
        store_delta = 0
        for pid in db.q("SELECT DISTINCT product_id FROM review_stats WHERE brand=?", (b.name,)):
            a = db.q1("SELECT total FROM review_stats WHERE brand=? AND product_id=? AND seen_at<? ORDER BY id DESC LIMIT 1", (b.name, pid["product_id"], s_iso))
            z = db.q1("SELECT total FROM review_stats WHERE brand=? AND product_id=? AND seen_at<? ORDER BY id DESC LIMIT 1", (b.name, pid["product_id"], e_iso))
            if a and z and z["total"] > a["total"]:
                store_delta += z["total"] - a["total"]
        app_delta = 0
        for pkg in db.q("SELECT DISTINCT package FROM app_metrics WHERE brand=?", (b.name,)):
            a = db.q1("SELECT ratings_count FROM app_metrics WHERE package=? AND seen_at<? ORDER BY id DESC LIMIT 1", (pkg["package"], s_iso))
            z = db.q1("SELECT ratings_count FROM app_metrics WHERE package=? AND seen_at<? ORDER BY id DESC LIMIT 1", (pkg["package"], e_iso))
            if a and z and (z["ratings_count"] or 0) > (a["ratings_count"] or 0):
                app_delta += z["ratings_count"] - a["ratings_count"]
        mkt_delta = 0
        for it in db.q("SELECT DISTINCT channel, city, item_key FROM channel_listings WHERE brand=? AND channel IN ('amazon','flipkart')", (b.name,)):
            a = db.q1("SELECT rating_count FROM channel_history WHERE channel=? AND city=? AND item_key=? AND seen_at<? AND rating_count IS NOT NULL ORDER BY id DESC LIMIT 1",
                      (it["channel"], it["city"], it["item_key"], s_iso))
            z = db.q1("SELECT rating_count FROM channel_history WHERE channel=? AND city=? AND item_key=? AND seen_at<? AND rating_count IS NOT NULL ORDER BY id DESC LIMIT 1",
                      (it["channel"], it["city"], it["item_key"], e_iso))
            if a and z and z["rating_count"] > a["rating_count"]:
                mkt_delta += z["rating_count"] - a["rating_count"]
        bs_moves = db.q1("SELECT COUNT(*) c FROM events WHERE brand=? AND kind IN ('bestseller_move','review_surge','stock_out') AND created_at>=? AND created_at<?",
                         (b.name, s_iso, e_iso))["c"]
        # dated evidence: reviews and comments carry their own publish date, so they count even before two snapshots exist
        dated = {r["source"]: r["c"] for r in db.q("SELECT source, COUNT(*) c FROM voice WHERE brand=? AND created>=? AND created<? GROUP BY source",
                                                    (b.name, start, end))}
        store_delta = max(store_delta, dated.get("judgeme", 0))
        app_delta = max(app_delta, dated.get("appstore", 0) + dated.get("play", 0))
        mkt_delta = max(mkt_delta, dated.get("amazon", 0))
        est = store_delta * 100 / r_store + app_delta * 100 / r_app + mkt_delta * 100 / r_mkt
        out.append({"brand": b.name, "self": b.name == me, "store_reviews": store_delta, "app_ratings": app_delta, "marketplace_ratings": mkt_delta,
                    "demand_events": bs_moves, "est_units": round(est), "basis": "store reviews, app ratings and marketplace ratings gained in the window"})
    mx = max((o["est_units"] for o in out), default=0) or 1
    for o in out:
        o["index"] = round(100 * o["est_units"] / mx)
    return sorted(out, key=lambda o: -o["est_units"])


def report(kind: str = "weekly", period: str | None = None) -> dict:
    if kind not in KINDS:
        kind = "weekly"
    label, start, end, pstart, pend = period_bounds(kind, period)
    cur, prev = _own(start, end), _own(pstart, pend)
    cur_seg = {x["key"]: x for x in cur["by_segment"]}
    prev_seg = {x["key"]: x for x in prev["by_segment"]}
    for k, v in cur_seg.items():
        v["growth_pct"] = _growth(v["revenue"], (prev_seg.get(k) or {}).get("revenue", 0))
    pxy = proxies(start, end)
    ppxy = {p["brand"]: p for p in proxies(pstart, pend)}
    for p in pxy:
        p["growth_pct"] = _growth(p["est_units"], (ppxy.get(p["brand"]) or {}).get("est_units", 0))
    has_own = cur["lines"] > 0 or prev["lines"] > 0
    peb = (settings.market.get("pebble") or {}) if settings.market else {}
    return {"kind": kind, "period": label, "start": start, "end": end, "prev_period": {"start": pstart, "end": pend},
            "own": {**cur, "growth": {"revenue_pct": _growth(cur["revenue"], prev["revenue"]), "units_pct": _growth(cur["units"], prev["units"])},
                    "prev": {"revenue": prev["revenue"], "units": prev["units"]}, "has_data": has_own,
                    "sources": [r["source"] for r in db.q("SELECT DISTINCT source FROM sales")],
                    "note": None if has_own else ("No own sales rows yet: import a CSV on the Reports page or set SHOPIFY_ADMIN_TOKEN. "
                                                  + (f"Until then the plan figure is market.yaml's ₹{peb.get('revenue_cr')} cr {peb.get('revenue_period', '')} estimate." if peb.get("revenue_cr") else ""))},
            "proxies": pxy,
            "periods": available(kind)}


def available(kind: str, n: int = 14) -> list[str]:
    """Recent period keys for the selector (complete periods only)."""
    today = datetime.now(timezone.utc).date()
    out = []
    if kind == "daily":
        out = [(today - timedelta(days=i)).isoformat() for i in range(1, n + 1)]
    elif kind == "weekly":
        start = today - timedelta(days=today.weekday() + 7)
        out = [(start - timedelta(days=7 * i)).strftime("%G-W%V") for i in range(n)]
    elif kind == "monthly":
        y, m = today.year, today.month
        for _ in range(n):
            m -= 1
            if m == 0:
                y, m = y - 1, 12
            out.append(f"{y}-{m:02d}")
    else:
        out = [str(today.year - i) for i in range(0, 6)]  # the running year (to date) first
    return out


def series(kind: str = "monthly", n: int = 12) -> list[dict]:
    """Own revenue/units per period for the trend chart (includes the running current period)."""
    today = datetime.now(timezone.utc).date()
    keys = available(kind, n)
    if kind == "daily":
        keys = [today.isoformat()] + keys
    elif kind == "weekly":
        keys = [today.strftime("%G-W%V")] + keys
    elif kind == "monthly":
        keys = [today.strftime("%Y-%m")] + keys
    else:
        keys = [str(today.year)] + keys
    out = []
    for k in reversed(keys):
        _, s, e, _, _ = period_bounds(kind, k)
        r = db.q1("SELECT COALESCE(SUM(units),0) u, COALESCE(SUM(revenue),0) r FROM sales WHERE day>=? AND day<?", (s, e))
        out.append({"period": k, "units": r["u"], "revenue": r["r"]})
    return out
