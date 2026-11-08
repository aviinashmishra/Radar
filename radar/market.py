"""Market model: segment sizes, Pebble's position, share of signal, and the "₹ at risk" impact estimate.

Every number comes from config/market.yaml (sizes, Pebble revenue and mix, assumptions) and the radar's own data
(live ASPs, SKU counts, news / search / creator / app / attention shares). The impact estimate is deliberately simple
and fully stated, so a founder can argue with the assumptions instead of the arithmetic:

  monthly segment revenue   = pebble.revenue_cr * mix[segment] / 12
  rival price cut of p%     = monthly segment revenue * overlap(rival) * elasticity * p/100        (capped)
  rival launch              = monthly segment revenue * overlap(rival) * launch_shift_pct/100
  rival stock-out / dry     = upside: segment value / 12 * rival share * capture_pct/100
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db
from .analytics import CATEGORY_SEGMENT, categorize
from .config import settings

CR = 1e7  # rupees in one crore


def _m() -> dict:
    return settings.market or {}


def _a(key: str, default: float) -> float:
    return float(((_m().get("assumptions") or {}).get(key, default)))


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def segment_of(event: dict) -> str | None:
    """Which segment an event belongs to: explicit data.segment, else the product title/type, else a one-segment brand."""
    d = event.get("data") or {}
    if isinstance(d, str):
        try:
            d = json.loads(d or "{}")
        except ValueError:
            d = {}
    if d.get("segment"):
        return d["segment"]
    cat = categorize(d.get("product_type") or "", event.get("title") or "")
    if cat in CATEGORY_SEGMENT:
        return CATEGORY_SEGMENT[cat]
    b = settings.brand(event.get("brand") or "")
    if b and len(b.segments) == 1:
        return b.segments[0]
    return None


def monthly_segment_revenue(segment: str) -> float:
    p = settings.self_market
    mix = (p.get("segment_mix_pct") or {}).get(segment, 0)
    return float(p.get("revenue_cr") or 0) * CR * mix / 100 / 12


def rival_share(brand: str, segment: str) -> float | None:
    seg = (_m().get("segments") or {}).get(segment) or {}
    s = (seg.get("shares_pct") or {}).get(brand)
    return float(s) if s is not None else None


def overlap(brand: str, segment: str | None) -> float:
    b = settings.brand(brand)
    base = _a("overlap_adjacent", 0.15) if (b and b.tier == "adjacent") else _a("overlap_core", 0.5)
    if segment and (s := rival_share(brand, segment)) is not None:
        base *= max(0.3, min(2.0, s / 20))
    return base


def impact(event: dict, kind: str | None = None) -> dict | None:
    """INR per month at risk (or upside) for one event or signal. None when nothing sensible can be said."""
    brand, k = event.get("brand") or "", kind or event.get("kind") or ""
    if not brand or brand in (_me(), "Sector", "NewEntrant"):
        return None
    d = event.get("data") or {}
    if isinstance(d, str):
        try:
            d = json.loads(d or "{}")
        except ValueError:
            d = {}
    seg = segment_of({**event, "data": d})
    if not seg and k in ("store_dry", "launch_predictor", "price_war", "stockout_opportunity", "momentum"):
        b = settings.brand(brand)   # brand-level signals: the brand's first (main) segment
        seg = b.segments[0] if b and b.segments else None
    if not seg:
        return None
    msr = monthly_segment_revenue(seg)
    seg_cfg = (_m().get("segments") or {}).get(seg) or {}
    seg_month_value = float(seg_cfg.get("value_cr") or 0) * CR / 12
    el, cap = _a("price_elasticity", 1.2), _a("max_share_at_risk", 0.4)
    ov = overlap(brand, seg)
    share = rival_share(brand, seg)
    amount, direction, basis = 0.0, "risk", ""
    pct = abs(float(d.get("pct") or d.get("avg_pct") or d.get("pct_below") or 0))
    if k in ("price_drop", "watch_price_change", "undercut") and pct:
        if k == "watch_price_change" and float(d.get("pct") or 0) > 0:
            return None
        amount = msr * ov * el * pct / 100
        basis = f"{pct:.0f}% rival price move × elasticity {el} × overlap {ov:.2f} on Pebble's {seg} revenue"
    elif k == "sale_started":
        breadth = min(1.0, float(d.get("count") or 1) / 20)
        amount = msr * ov * el * pct / 100 * breadth
        basis = f"{pct:.0f}% average cut across {d.get('count') or '?'} SKUs × elasticity {el} × overlap {ov:.2f}"
    elif k == "price_war":
        amount = msr * _a("overlap_core", 0.5) * el * 0.10
        basis = f"three rivals cutting: assume a 10% field move × elasticity {el}"
    elif k in ("new_product", "hidden_product", "launch_predictor", "watch_status_change") or (k == "news" and d.get("topic") == "launch"):
        if k == "watch_status_change" and "sold" in (event.get("title") or "").lower():
            return None
        amount = msr * ov * _a("launch_shift_pct", 3) / 100
        basis = f"a credible launch shifts {_a('launch_shift_pct', 3):.0f}% of Pebble's {seg} buyers × overlap {ov:.2f}"
    elif k in ("stock_out", "stockout_opportunity", "store_dry"):
        s = share if share is not None else 5.0
        rival_month = seg_month_value * s / 100                      # the rival's monthly sales in this segment
        if k == "store_dry":
            sku_share, what = 1.0, "its whole store is dry"
        else:
            sku_share = 0.03 if d.get("bestseller_rank") else 0.005  # one SKU: a best-seller is ~3% of a rival's segment sales
            what = "a best-seller" if d.get("bestseller_rank") else "one SKU"
        amount = rival_month * sku_share * _a("stockout_capture_pct", 10) / 100
        direction = "upside"
        basis = (f"rival holds ~{s:.0f}% of a ₹{seg_cfg.get('value_cr', 0):,.0f} cr segment, {what} ({sku_share:.0%} of its sales); "
                 f"capture {_a('stockout_capture_pct', 10):.0f}% of that stuck demand")
    else:
        return None
    if direction == "risk":
        amount = min(amount, msr * cap)
    if amount < 1000:
        return None
    return {"inr_month": round(amount, -3), "direction": direction, "segment": seg, "basis": basis,
            "label": ("≈ " + fmt_inr(amount) + "/month " + ("at risk" if direction == "risk" else "upside"))}


def fmt_inr(n: float) -> str:
    n = float(n or 0)
    if n >= CR:
        return f"₹{n / CR:,.1f} cr"
    if n >= 1e5:
        return f"₹{n / 1e5:,.1f} L"
    return f"₹{n:,.0f}"


def sizes() -> dict:
    """Segment sizes with live context: Pebble SKUs, rival count, live median price from the stores."""
    me = _me()
    live = defaultdict(lambda: {"prices": [], "pebble": 0, "brands": set()})
    for p in db.q("SELECT brand, title, product_type, tags, price FROM products WHERE price>0"):
        cat = categorize(p["product_type"], p["title"], p["tags"] or "")
        seg = CATEGORY_SEGMENT.get(cat)
        if not seg:
            continue
        if p["brand"] == me:
            live[seg]["pebble"] += 1
        else:
            live[seg]["prices"].append(p["price"])
            live[seg]["brands"].add(p["brand"])
    pebble = settings.self_market
    out = []
    for key, s in (_m().get("segments") or {}).items():
        prices = sorted(live[key]["prices"])
        units, asp = float(s.get("units_m") or 0), float(s.get("asp_inr") or 0)
        value = float(s.get("value_cr") or (units * 1e6 * asp / CR))
        share = (s.get("shares_pct") or {}).get(me)
        mix = (pebble.get("segment_mix_pct") or {}).get(key, 0)
        peb_rev = float(pebble.get("revenue_cr") or 0) * mix / 100
        out.append({"key": key, "name": s.get("name", key), "units_m": units, "growth_pct": s.get("growth_pct"), "asp_inr": asp,
                    "value_cr": round(value), "trend": s.get("trend", ""), "confidence": s.get("confidence", "estimate"),
                    "pebble_share_pct": share, "pebble_rank": s.get("pebble_rank"), "pebble_rank_source": s.get("pebble_rank_source"),
                    "pebble_revenue_cr": round(peb_rev, 1), "implied_share_pct": round(100 * peb_rev / value, 1) if value else None,
                    "shares": dict(sorted((s.get("shares_pct") or {}).items(), key=lambda x: -x[1])),
                    "live_median_price": prices[len(prices) // 2] if prices else None, "live_rivals": len(live[key]["brands"]),
                    "live_pebble_skus": live[key]["pebble"],
                    "show": any(sg["key"] == key and sg.get("show") for sg in settings.segments)})
    total_value = sum(o["value_cr"] for o in out)
    return {"year": _m().get("year"), "sources": _m().get("sources") or [], "segments": out,
            "total_value_cr": round(total_value), "pebble": {**pebble, "mix": pebble.get("segment_mix_pct") or {}},
            "assumptions": _m().get("assumptions") or {}}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def share_of_signal(days: int = 30) -> list[dict]:
    """Where each brand stands on the signals the radar can see, each normalised to the field leader = 100."""
    me = _me()
    news = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM events WHERE source='news' AND created_at>=? GROUP BY brand", (_since(days),))}
    search = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM search_suggestions GROUP BY brand")}
    views = {r["brand"]: r["v"] for r in db.q("SELECT brand, SUM(views) v FROM videos GROUP BY brand")}
    apps = {}
    for r in db.q("SELECT brand, package, MAX(id) mid FROM app_metrics GROUP BY package"):
        row = db.q1("SELECT ratings_count FROM app_metrics WHERE id=?", (r["mid"],)) or {}
        apps[r["brand"]] = apps.get(r["brand"], 0) + (row.get("ratings_count") or 0)
    att = {r["brand"]: r["v"] for r in db.q("SELECT brand, SUM(views) v FROM attention WHERE day>=date('now', ?) GROUP BY brand", (f"-{days} days",))}
    skus = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM products WHERE price>0 GROUP BY brand")}
    rows = []
    for b in settings.brands:
        rows.append({"brand": b.name, "self": b.name == me, "origin": b.origin, "news": news.get(b.name, 0), "search": search.get(b.name, 0),
                     "creator_views": views.get(b.name, 0) or 0, "app_ratings": apps.get(b.name, 0), "attention": att.get(b.name, 0) or 0,
                     "skus": skus.get(b.name, 0)})
    weights = {"news": 0.25, "search": 0.2, "creator_views": 0.2, "app_ratings": 0.2, "attention": 0.15}
    for k in weights:
        mx = max((r[k] for r in rows), default=0) or 1
        for r in rows:
            r[f"{k}_idx"] = round(100 * r[k] / mx)
    for r in rows:
        have = [k for k in weights if any(x[k] for x in rows)]
        r["composite"] = round(sum(weights[k] * r[f"{k}_idx"] for k in have) / (sum(weights[k] for k in have) or 1))
    rows.sort(key=lambda r: -r["composite"])
    return rows


def pressure(days: int = 30) -> dict:
    """Sum of the impact estimates over the window: by rival and by segment, risk and upside apart."""
    by_brand = defaultdict(lambda: {"risk": 0.0, "upside": 0.0, "events": 0})
    by_seg = defaultdict(lambda: {"risk": 0.0, "upside": 0.0, "events": 0})
    top = []
    for e in db.q("SELECT id, created_at, brand, kind, title, importance, data FROM events WHERE created_at>=? AND kind<>'baseline' "
                  "AND brand NOT IN ('Sector','NewEntrant') ORDER BY id DESC LIMIT 3000", (_since(days),)):
        imp = impact(e)
        if not imp:
            continue
        for bucket, key in ((by_brand, e["brand"]), (by_seg, imp["segment"])):
            bucket[key][imp["direction"]] += imp["inr_month"]
            bucket[key]["events"] += 1
        top.append({"id": e["id"], "brand": e["brand"], "kind": e["kind"], "title": e["title"], "created_at": e["created_at"],
                    "importance": e["importance"], **imp})
    for s in db.q("SELECT id, created_at, brand, kind, title, importance, data FROM signals WHERE created_at>=?", (_since(days),)):
        imp = impact(s)
        if not imp:
            continue
        by_brand[s["brand"]][imp["direction"]] += imp["inr_month"]
        by_brand[s["brand"]]["events"] += 1
        by_seg[imp["segment"]][imp["direction"]] += imp["inr_month"]
    top.sort(key=lambda x: -x["inr_month"])
    seen_top: set = set()   # the same SKU cut three times in a month is one line in the top list
    top = [t for t in top if (k := (t["brand"], t["kind"], t["title"][:40])) not in seen_top and not seen_top.add(k)]
    names = {sg["key"]: sg["name"] for sg in settings.segments}
    # many small moves on the same SKUs add up past what a segment earns: sums are capped at the segment's monthly
    # revenue (risk) and at the segment's monthly market value times the capture rate (upside)
    total_month = float(settings.self_market.get("revenue_cr") or 0) * CR / 12
    cap_share = _a("max_share_at_risk", 0.4)
    for s, v in by_seg.items():
        v["risk"] = min(v["risk"], monthly_segment_revenue(s) * cap_share * 2)
        seg_cfg = (_m().get("segments") or {}).get(s) or {}
        v["upside"] = min(v["upside"], float(seg_cfg.get("value_cr") or 0) * CR / 12 * _a("stockout_capture_pct", 10) / 100)
    for b, v in by_brand.items():
        v["risk"] = min(v["risk"], total_month * cap_share)
        v["upside"] = min(v["upside"], total_month)
    return {"days": days,
            "by_brand": sorted([{"brand": b, **v} for b, v in by_brand.items()], key=lambda x: -(x["risk"] + x["upside"]))[:15],
            "by_segment": sorted([{"segment": s, "name": names.get(s, s), **v} for s, v in by_seg.items()], key=lambda x: -(x["risk"] + x["upside"])),
            "total_risk": min(sum(v["risk"] for v in by_seg.values()), total_month * cap_share),
            "total_upside": sum(v["upside"] for v in by_seg.values()),
            "top": top[:12], "pebble_monthly_revenue": total_month}
