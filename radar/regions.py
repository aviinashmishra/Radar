"""Regional analytics: search interest per state (Google Trends), mentions, quick-commerce shelves per city, Pebble's
own sales per state and city, and the gaps between where demand is and where Pebble sells."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db
from .config import settings
from .collectors.regions import STATES, CITIES


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def _latest_period(basis: str) -> str | None:
    r = db.q1("SELECT period FROM region_interest WHERE basis=? ORDER BY seen_at DESC LIMIT 1", (basis,))
    return r["period"] if r else None


def overview(days: int = 90) -> dict:
    me = _me()
    # search interest by state (0-100 per brand, Google's own scaling) -> per state: Pebble value, field average, leader
    trends = defaultdict(dict)
    tp = _latest_period("trends")
    if tp:
        for r in db.q("SELECT brand, region, value FROM region_interest WHERE basis='trends' AND level='state'"):
            trends[r["region"]][r["brand"]] = r["value"]
    mentions = defaultdict(dict)
    for r in db.q("SELECT brand, region, level, value, detail FROM region_interest WHERE basis='mentions'"):
        mentions[r["region"]][r["brand"]] = {"n": r["value"], "sample": r["detail"], "level": r["level"]}
    qc = defaultdict(dict)
    for r in db.q("SELECT brand, region, value, detail FROM region_interest WHERE basis='qcommerce' AND period=(SELECT MAX(period) FROM region_interest WHERE basis='qcommerce')"):
        qc[r["region"]][r["brand"]] = {"in_stock": r["value"], "detail": r["detail"]}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    sales_state = {r["state"]: r for r in db.q("SELECT state, SUM(units) units, SUM(revenue) revenue, COUNT(DISTINCT order_ref) orders FROM sales WHERE day>=? AND state<>'' GROUP BY state", (since,))}
    sales_city = db.q("SELECT city, state, SUM(units) units, SUM(revenue) revenue FROM sales WHERE day>=? AND city<>'' GROUP BY city, state ORDER BY revenue DESC LIMIT 40", (since,))
    total_rev = sum(r["revenue"] or 0 for r in sales_state.values()) or 0
    states = []
    for name, (lat, lon) in STATES.items():
        t = trends.get(name, {})
        field = [v for b, v in t.items() if b != me]
        leader = max(((v, b) for b, v in t.items() if b != me), default=(None, None))
        s = sales_state.get(name) or {}
        m = mentions.get(name, {})
        states.append({"state": name, "lat": lat, "lon": lon, "pebble_interest": t.get(me), "field_interest": round(sum(field) / len(field)) if field else None,
                       "leader": leader[1], "leader_interest": leader[0], "interest": {b: v for b, v in sorted(t.items(), key=lambda x: -x[1])[:8]},
                       "sales_units": s.get("units") or 0, "sales_revenue": s.get("revenue") or 0, "sales_orders": s.get("orders") or 0,
                       "sales_share_pct": round(100 * (s.get("revenue") or 0) / total_rev, 1) if total_rev else None,
                       "mentions": sum(v["n"] for v in m.values()), "pebble_mentions": (m.get(me) or {}).get("n", 0)})
    # gaps: states where rivals' interest is high and Pebble's is low (or sales are thin)
    gaps = []
    for s in states:
        if s["field_interest"] is None:
            continue
        pi = s["pebble_interest"] or 0
        gap = s["field_interest"] - pi
        if gap >= 15:
            gaps.append({"state": s["state"], "gap": gap, "pebble_interest": pi, "field_interest": s["field_interest"], "leader": s["leader"],
                         "sales_share_pct": s["sales_share_pct"], "why": f"{s['leader']} indexes {s['leader_interest']} here vs Pebble {pi}"})
    gaps.sort(key=lambda g: -g["gap"])
    strong = sorted([s for s in states if (s["pebble_interest"] or 0) >= 60], key=lambda s: -(s["pebble_interest"] or 0))[:8]
    cities = []
    city_sales = {(r["city"]): r for r in sales_city}
    for name, (st, lat, lon) in CITIES.items():
        q = qc.get(name, {})
        cs = city_sales.get(name) or {}
        m = mentions.get(name, {})
        if not (q or cs or m):
            continue
        cities.append({"city": name, "state": st, "lat": lat, "lon": lon, "qc_brands": len(q), "qc_pebble": (q.get(me) or {}).get("in_stock", 0),
                       "qc": {b: v["in_stock"] for b, v in sorted(q.items(), key=lambda x: -x[1]["in_stock"])[:10]},
                       "sales_units": cs.get("units") or 0, "sales_revenue": cs.get("revenue") or 0, "mentions": sum(v["n"] for v in m.values())})
    brands_trends = defaultdict(dict)
    for st, t in trends.items():
        for b, v in t.items():
            brands_trends[b][st] = v
    brand_rows = []
    for b in settings.brands:
        t = brands_trends.get(b.name)
        if not t:
            continue
        top = sorted(t.items(), key=lambda x: -x[1])[:5]
        brand_rows.append({"brand": b.name, "self": b.name == me, "states_with_data": len(t), "top_states": [{"state": s, "value": v} for s, v in top],
                           "national_mean": round(sum(t.values()) / len(t))})
    return {"days": days, "trends_period": tp, "states": states, "cities": cities, "gaps": gaps[:10], "strongholds": strong, "brands": brand_rows,
            "sales": {"total_revenue": total_rev, "states_with_sales": len(sales_state), "top_states": sorted(sales_state.values(), key=lambda r: -(r["revenue"] or 0))[:10],
                      "top_cities": sales_city[:12]},
            "stats": {"trend_brands": len(brand_rows), "qc_cities": len([c for c in cities if c["qc_brands"]]), "mention_places": len(mentions)}}


def brand_states(name: str, limit: int = 10) -> dict:
    """One brand's demand map for its brand page: latest Google Trends period by state, q-commerce cities in stock, place mentions."""
    period = _latest_period("trends")
    states = db.q("SELECT region, value, detail FROM region_interest WHERE brand=? AND basis='trends' AND level='state' AND period=? ORDER BY value DESC LIMIT ?",
                  (name, period, limit)) if period else []
    qc = db.q("SELECT city, COUNT(*) n, SUM(available) a, MIN(price) p FROM channel_listings WHERE brand=? AND city<>'' AND last_seen>=date('now','-7 days') GROUP BY city ORDER BY a DESC",
              (name,))
    mentions = db.q("SELECT region, level, value, detail FROM region_interest WHERE brand=? AND basis='mentions' ORDER BY value DESC LIMIT 8", (name,))
    return {"period": period, "states": states, "qc": qc, "mentions": mentions}
