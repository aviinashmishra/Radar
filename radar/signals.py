"""Signal engine: cross-event rules that turn single events into decisions.

price_war            3+ brands cut prices in the same product type within 48h
sale_started         a brand cut many prices at once (from shopify collector) - labelled with the nearest festival
stockout_opportunity a competitor best-seller (rank <= 10) is out of stock
launch_predictor     hidden products + new products + 'launch' news + new collections + page changes in 14d
momentum             a brand's event volume this week vs last week doubled
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db
from .config import settings


def _since(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def nearest_festival(when: datetime | None = None) -> tuple[str, int] | None:
    when = when or datetime.now(timezone.utc)
    best = None
    for f in settings.festivals:
        for year in (when.year, when.year + 1):
            try:
                d = datetime(year, int(f["month"]), int(f["day"]), tzinfo=timezone.utc)
            except ValueError:
                continue
            delta = (d - when).days
            if -5 <= delta <= 45 and (best is None or abs(delta) < abs(best[1])):
                best = (f["name"], delta)
    return best


def _self() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def run() -> int:
    n = 0
    me = _self()
    today = db.now()[:10]

    # ---- price war ----
    drops = db.q("SELECT brand, data FROM events WHERE kind='price_drop' AND created_at>=? AND brand<>?", (_since(2), me))
    by_type: dict[str, set] = defaultdict(set)
    for d in drops:
        t = (json.loads(d["data"] or "{}").get("product_type") or "").lower() or "unknown"
        by_type[t].add(d["brand"])
    for t, brands in by_type.items():
        if len(brands) >= 3:
            if db.add_signal("price_war", "Sector", f"Price war brewing in '{t}': {', '.join(sorted(brands))} all cut prices in 48h",
                             "Three or more rivals cut prices in the same category. Decide quickly whether Pebble defends "
                             "price or defends margin with bundles/value messaging.", 9,
                             {"product_type": t, "brands": sorted(brands)}, f"price_war|{t}|{today}"):
                n += 1

    # ---- sale started + festival label ----
    for s in db.q("SELECT brand, title, data, created_at FROM events WHERE kind='sale_started' AND created_at>=?", (_since(1),)):
        fest = nearest_festival()
        label = f" {fest[1]} days before {fest[0]}" if fest and fest[1] >= 0 else (f" during {fest[0]}" if fest else "")
        if db.add_signal("sale_started", s["brand"], f"{s['brand']} started a sale{label}", s["title"], 8,
                         {"festival": fest}, f"sale|{s['brand']}|{s['created_at'][:10]}"):
            n += 1

    # ---- stock-out opportunity ----
    for e in db.q("SELECT brand, title, url, data FROM events WHERE kind='stock_out' AND created_at>=? AND brand<>?", (_since(1), me)):
        d = json.loads(e["data"] or "{}")
        if d.get("bestseller_rank") and d["bestseller_rank"] <= 10:
            if db.add_signal("stockout_opportunity", e["brand"],
                             f"{e['brand']} best-seller #{d['bestseller_rank']} is out of stock - capture that demand now",
                             f"{e['title']}. Bid on its model name and category keywords, push Pebble's equivalent at "
                             f"{settings.currency} {d.get('price') or 0:,.0f} or below, and message 'in stock, ships today'.",
                             8, {"url": e["url"], **d}, f"stockout|{e['brand']}|{d.get('product_id')}|{today}"):
                n += 1

    # ---- launch predictor ----
    weights = {"hidden_product": 3, "new_product": 2, "new_collection": 1, "homepage_change": 1, "page_change": 1,
               "hiring": 1, "new_ad": 1}
    rows = db.q("SELECT brand, kind, title FROM events WHERE created_at>=? AND brand NOT IN ('Sector','NewEntrant')", (_since(14),))
    score: dict[str, int] = defaultdict(int)
    why: dict[str, list] = defaultdict(list)
    per_kind: dict[tuple, int] = defaultdict(int)
    for r in rows:
        w = weights.get(r["kind"], 0)
        if r["kind"] == "news" and any(k in r["title"].lower() for k in ("launch", "teaser", "unveil", "coming soon", "upcoming")):
            w = 2
        if not w:
            continue
        k = (r["brand"], r["kind"])
        if per_kind[k] >= 8:  # cap each evidence type so a 40-SKU colour drop does not swamp the score
            continue
        per_kind[k] += w
        score[r["brand"]] += w
        if len(why[r["brand"]]) < 6:
            why[r["brand"]].append(f"[{r['kind']}] {r['title'][:90]}")
    db.kv_set("launch_scores", {b: {"score": s, "why": why[b]} for b, s in sorted(score.items(), key=lambda x: -x[1])})
    for b, s in score.items():
        if s >= 10 and b != me:
            if db.add_signal("launch_predictor", b, f"{b} is likely launching soon (launch score {s}, 14-day window)",
                             "\n".join(why[b]), 7, {"score": s, "evidence": why[b]}, f"launch|{b}|{today}"):
                n += 1

    # ---- undercut: a matched rival SKU dropped >10% below the Pebble SKU it competes with ----
    from . import matcher
    for u in matcher.undercuts(days=2):
        if db.add_signal("undercut", u["brand"],
                         f"{u['brand']} now {u['pct_below']}% below Pebble's {u['self_title'][:50]} ({settings.currency} {u['rival_price']:,.0f} vs {u['self_price']:,.0f})",
                         f"Matched rival: {u['rival_title'][:90]}. Decide today: match, add a bundle, or defend on spec.", 8,
                         u, f"undercut|{u['brand']}|{u['self_id']}|{today}"):
            n += 1

    # ---- complaint cluster: 3+ low-star reviews on one rival SKU in a week = a weakness to advertise against ----
    for r in db.q("SELECT brand, json_extract(data,'$.product_id') pid, COUNT(*) c, MIN(title) t FROM events WHERE kind='complaint' "
                  "AND created_at>=? GROUP BY brand, pid HAVING c>=3", (_since(7),)):
        if db.add_signal("pain_point", r["brand"], f"{r['brand']} is taking complaints on one SKU ({r['c']} low-star reviews this week)",
                         f"{r['t']}. Pull the review bodies from the brand page and turn the recurring complaint into a Pebble ad angle.",
                         6, {"product_id": r["pid"], "count": r["c"]}, f"pain|{r['brand']}|{r['pid']}|{today}"):
            n += 1

    # ---- momentum ----
    this_w = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM events WHERE created_at>=? AND kind<>'baseline' GROUP BY brand", (_since(7),))}
    last_w = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM events WHERE created_at>=? AND created_at<? GROUP BY brand", (_since(14), _since(7)))}
    for b, c in this_w.items():
        prev = last_w.get(b, 0)
        if prev >= 3 and c >= 2 * prev and b not in ("Sector", "NewEntrant", me):
            if db.add_signal("momentum", b, f"{b} activity doubled this week ({prev} -> {c} events)",
                             "Something is going on. Open the brand timeline.", 6, {"prev": prev, "now": c}, f"momentum|{b}|{today}"):
                n += 1
    return n
