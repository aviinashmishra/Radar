"""Data repairs that must reach every copy of the database (this machine, the GitHub Actions worker's snapshot).

Each step is versioned in the kv table, so it runs once per database and again only when its rules (or, for
the relevance check, the brand aliases / exclusions in competitors.yaml) change.
The runner calls `run()` before scoring; `python -m radar.maintenance` runs it by hand.

  offbrand  re-check stored videos, search trends, Reddit posts and news against radar/relevance.py and drop the
            ones that were never about the brand (Urban cycles, Polar cooktops, Samsung phones, the US Pebble watch)
  rescore   re-score heuristic-scored events with the current rules (scaled sales, off-market SKUs capped) and drop
            placeholder "test" products, sub-8% "sales", "unknown"-type price wars, tile-only homepage "changes"
            and signals built on oats
"""
from __future__ import annotations
import json
import logging
from . import db, relevance
from .analytics import off_topic
from .config import settings

log = logging.getLogger("radar.maintenance")
RESCORE_VERSION = 7


def _data(row: dict) -> dict:
    try:
        return json.loads(row.get("data") or "{}")
    except ValueError:
        return {}


def _delete_events(ids: list[int]) -> None:
    with db.tx() as c:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            c.execute(f"DELETE FROM events WHERE id IN ({','.join('?' * len(chunk))})", chunk)


def purge_offbrand() -> dict:
    out = {"video": 0, "search_trend": 0, "social": 0, "news": 0, "suggestions": 0, "videos_table": 0}
    sector_seeds = {s.strip() for s in settings.search_seeds if "{brand}" not in s}
    current_seeds = sector_seeds | {s.replace("{brand}", t).strip() for s in settings.search_seeds if "{brand}" in s
                                    for b in settings.brands for t in (b.search_terms or [b.name])}
    drop, moves = [], []
    for b in settings.brands:
        for e in db.q("SELECT id, kind, title, data FROM events WHERE brand=? AND kind IN ('video','search_trend','social','news')",
                      (b.name,)):
            d = _data(e)
            if e["kind"] == "video":
                ok = relevance.relevant(d.get("title") or e["title"], b, d.get("query", ""), strict=True)
            elif e["kind"] == "search_trend":
                s = d.get("suggestion") or ""
                ok = relevance.relevant(s, b, cased=False, strict=True)
                if not ok and (d.get("seed") or "").strip() in sector_seeds:
                    moves.append((relevance.attribute(s, cased=False, strict=True), e["id"]))  # sector demand stays, re-attributed
                    continue
            elif e["kind"] == "social":
                ok = relevance.relevant(e["title"].removeprefix("r/ "), b)
            else:
                ok = relevance.relevant(e["title"], b, d.get("term", ""))
            if not ok:
                drop.append(e["id"])
                out[e["kind"]] += 1
        for v in db.q("SELECT video_id, title, query FROM videos WHERE brand=?", (b.name,)):
            if not relevance.relevant(v["title"], b, v["query"] or "", strict=True):
                with db.tx() as c:
                    c.execute("DELETE FROM videos WHERE video_id=?", (v["video_id"],))
                out["videos_table"] += 1
    _delete_events(drop)
    with db.tx() as c:
        for brand, eid in moves:
            c.execute("UPDATE events SET brand=? WHERE id=?", (brand, eid))
    # stored autocomplete rows: re-attribute sector seeds, drop off-brand rows and seeds the radar no longer asks
    for r in db.q("SELECT seed, suggestion, brand FROM search_suggestions"):
        seed = r["seed"].strip()
        if seed not in current_seeds:
            new = None
        elif seed in sector_seeds:
            new = relevance.attribute(r["suggestion"], cased=False, strict=True)
        else:
            b = settings.brand(r["brand"])
            new = r["brand"] if b and relevance.relevant(r["suggestion"], b, cased=False, strict=True) else None
        if new != r["brand"]:
            with db.tx() as c:
                if new is None:
                    c.execute("DELETE FROM search_suggestions WHERE seed=? AND suggestion=?", (r["seed"], r["suggestion"]))
                else:
                    c.execute("UPDATE search_suggestions SET brand=? WHERE seed=? AND suggestion=?", (new, r["seed"], r["suggestion"]))
            out["suggestions"] += 1
    return out


def rescore() -> dict:
    from .collectors.shopify import is_test_product
    from .enrich import _PRODUCT_KINDS, heuristic
    out = {"test_products": 0, "small_sales": 0, "rescored": 0, "signals": 0}
    drop = []
    for e in db.q("SELECT * FROM events WHERE source='shopify' AND kind IN ('new_product','sale_started')"):
        d = _data(e)
        if e["kind"] == "new_product" and is_test_product(e["title"].split(": ", 1)[-1].split(" at ")[0], d.get("price")):
            drop.append(e["id"])
            out["test_products"] += 1
        elif e["kind"] == "sale_started" and abs(d.get("avg_pct") or 0) < 8:
            drop.append(e["id"])
            out["small_sales"] += 1
            with db.tx() as c:
                out["signals"] += c.execute("DELETE FROM signals WHERE kind='sale_started' AND brand=? AND substr(created_at,1,10)=?",
                                            (e["brand"], e["created_at"][:10])).rowcount
    _delete_events(drop)
    for e in db.q("SELECT * FROM events WHERE enriched=1 OR (enriched=2 AND source='shopify' AND kind IN (%s))"
                  % ",".join("?" * len(_PRODUCT_KINDS)), tuple(_PRODUCT_KINDS)):
        e["data"] = _data(e)
        if e["enriched"] == 1:  # heuristic-scored: recompute with today's rules
            res = heuristic(e)
            new = (res["importance"], res["so_what"][:600], res["counter_move"][:600])
            if new[0] != e["importance"]:
                with db.tx() as c:
                    c.execute("UPDATE events SET importance=?, so_what=?, counter_move=? WHERE id=?", (*new, e["id"]))
                out["rescored"] += 1
        elif off_topic(e["data"].get("product_type") or "", e["title"]) and (e["importance"] or 0) > 3:
            with db.tx() as c:
                c.execute("UPDATE events SET importance=3 WHERE id=?", (e["id"],))
            out["rescored"] += 1
    # "new" videos that were months or years old (a search surfaced them; they are not news)
    from .collectors.youtube import RECENT_RX
    stale = [e["id"] for e in db.q("SELECT id, data FROM events WHERE kind='video'")
             if (_data(e).get("published") or "") and not RECENT_RX.search(_data(e).get("published") or "")
             and any(w in (_data(e).get("published") or "") for w in ("mo ago", "month", "yr ago", "year", "wk ago", "week"))]
    _delete_events(stale)
    out["stale_videos"] = len(stale)
    # homepage "changes" that were only rotating product tiles or a banner line ("0% different")
    from .collectors.pagewatch import TILE_RX
    noise = []
    for e in db.q("SELECT id, data FROM events WHERE kind='homepage_change'"):
        d = _data(e)
        copy = [s for s in d.get("added") or [] if not TILE_RX.search(s)]
        if "new_share" not in d and (not copy or (d.get("similarity") or 0) >= 0.98 and len(copy) < 5):
            noise.append(e["id"])
    _delete_events(noise)
    out["homepage_noise"] = len(noise)
    # price wars built from drops with no product type ("unknown"), before they were grouped by category
    with db.tx() as c:
        out["signals"] += c.execute("DELETE FROM signals WHERE kind='price_war' AND json_extract(data,'$.product_type')='unknown'").rowcount
    # signals that rest on off-market products (FITTR oats best-seller out of stock / taking 1-star reviews)
    for s in db.q("SELECT id, brand, title, data FROM signals WHERE kind IN ('stockout_opportunity','pain_point')"):
        d = _data(s)
        prod = db.q1("SELECT title, product_type FROM products WHERE brand=? AND product_id=?", (s["brand"], d.get("product_id"))) or {}
        if off_topic(prod.get("product_type") or d.get("product_type") or "", prod.get("title") or s["title"]):
            with db.tx() as c:
                c.execute("DELETE FROM signals WHERE id=?", (s["id"],))
            out["signals"] += 1
    return out


NEWS_VERSION = 2


def news_topics() -> dict:
    """Backfill: tag stored news events with a topic and a headline key (cross-feed de-duplication), and merge
    duplicates that two aggregators already created before the key existed."""
    from .collectors.news import norm_title
    from .newsintel import topic_of
    out = {"tagged": 0, "duplicates": 0, "seeds": 0}
    seen: dict[tuple, int] = {}
    drop = []
    for e in db.q("SELECT id, brand, title, data FROM events WHERE source='news' ORDER BY id"):
        d = _data(e)
        key = norm_title(e["title"])
        if key and (e["brand"], key) in seen:
            drop.append(e["id"])
            out["duplicates"] += 1
            continue
        if key:
            seen[(e["brand"], key)] = e["id"]
            with db.tx() as c:
                c.execute("INSERT OR IGNORE INTO seen_urls(brand,source,url,seen_at) VALUES(?,?,?,?)", (e["brand"], "news-title", key, db.now()))
        if not d.get("topic"):
            d["topic"] = topic_of(e["title"], d.get("summary", ""))
            d.setdefault("provider", "google")
            with db.tx() as c:
                c.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(d), e["id"]))
            out["tagged"] += 1
    _delete_events(drop)
    from .collectors.financials import load_seeds
    out["seeds"] = load_seeds()
    # v2: sector-wide articles that were never about the category (laptops, a bottler's IPO, travel gadgets)
    from .collectors.news import sector_topic
    off = [e["id"] for e in db.q("SELECT id, title, data FROM events WHERE source='news' AND brand='Sector'")
           if not sector_topic(f"{e['title']} {_data(e).get('summary', '')}")]
    _delete_events(off)
    out["sector_offtopic"] = len(off)
    # v2: the generic "Press coverage; read the article" lines get the topic-specific why / move
    from .enrich import heuristic
    out["relined"] = 0
    for e in db.q("SELECT * FROM events WHERE source='news' AND enriched<>2 AND (so_what LIKE 'Press coverage%' OR counter_move LIKE 'No action unless%')"):
        e["data"] = _data(e)
        res = heuristic(e)
        with db.tx() as c:
            c.execute("UPDATE events SET so_what=?, counter_move=? WHERE id=?", (res["so_what"][:600], res["counter_move"][:600], e["id"]))
        out["relined"] += 1
    # v2: money titles written before the B/M formatter ("$10,000M" -> "$10.0B")
    import re
    for e in db.q("SELECT id, title FROM events WHERE kind='financial_fact' AND title LIKE '%,000M%'"):
        t = re.sub(r"([$€])([\d,]+)M\b", lambda m: f"{m.group(1)}{float(m.group(2).replace(',', '')) / 1000:,.1f}B"
                   if float(m.group(2).replace(",", "")) >= 1000 else m.group(0), e["title"])
        with db.tx() as c:
            c.execute("UPDATE events SET title=? WHERE id=?", (t, e["id"]))
    return out


IMPACT_VERSION = 1


def impact_backfill(days: int = 60) -> dict:
    """Attach the ₹/month impact estimate (radar/market.py) to events scored before the model existed, or after the
    model's assumptions changed (bump IMPACT_VERSION)."""
    from . import market
    out = {"events": 0}
    for e in db.q("SELECT id, brand, kind, title, data FROM events WHERE created_at>=datetime('now', ?) AND kind<>'baseline' "
                  "AND brand NOT IN ('Sector','NewEntrant')", (f"-{days} days",)):
        d = _data(e)
        imp = market.impact({**e, "data": d})
        if imp or "impact" in d:
            d["impact"] = imp
            if imp is None:
                del d["impact"]
            with db.tx() as c:
                c.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(d, default=str), e["id"]))
            out["events"] += 1
    return out


def run() -> dict:
    done = {}
    if db.kv_get("maint:relevance") != relevance.signature():  # new rules, or aliases / exclude edited in the yaml
        done["offbrand"] = purge_offbrand()
        db.kv_set("maint:relevance", relevance.signature())
    if (db.kv_get("maint:rescore") or 0) < RESCORE_VERSION:
        done["rescore"] = rescore()
        db.kv_set("maint:rescore", RESCORE_VERSION)
    if (db.kv_get("maint:news") or 0) < NEWS_VERSION:
        done["news"] = news_topics()
        db.kv_set("maint:news", NEWS_VERSION)
    if (db.kv_get("maint:impact") or 0) < IMPACT_VERSION:
        done["impact"] = impact_backfill()
        db.kv_set("maint:impact", IMPACT_VERSION)
    if done:
        log.info("maintenance: %s", done)
    return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(run())
