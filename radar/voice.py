"""Voice-of-customer analytics over the `voice` table: per-brand sentiment and themes, Pebble vs the field, the
theme matrix (who is weak where), weekly timelines and quotable items. Optional AI digest per brand."""
from __future__ import annotations
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db
from .config import settings
from .collectors.voice import THEMES

THEME_KEYS = [t for t, _ in THEMES]
SOURCES = ["youtube", "appstore", "play", "amazon", "judgeme"]


_APP_SPECIFIC = re.compile(r"sync|bluetooth|connect|disconnect|pair|crash|bug|login|log in|update|firmware|notification|lag|hang|freez", re.I)


def _themes(r: dict) -> list[str]:
    """Stored themes, minus the 'app' tag on store reviews that only say 'app' because they are app reviews."""
    ts = json.loads(r["themes"] or "[]")
    if "app" in ts and r["source"] in ("appstore", "play") and not _APP_SPECIFIC.search(r["text"] or ""):
        ts = [t for t in ts if t != "app"]
    return ts


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def _day(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def summary(days: int = 90, brand: str | None = None) -> dict:
    sql, p = "SELECT brand, source, rating, sentiment, themes, text, created, likes, url, ref_title FROM voice WHERE created>=?", [_day(days)]
    if brand:
        sql += " AND brand=?"; p.append(brand)
    rows = db.q(sql, p)
    me = _me()
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["brand"], {"items": 0, "rated": 0, "rating_sum": 0.0, "pos": 0, "neg": 0, "sent_sum": 0.0, "sources": defaultdict(int),
                                        "themes": defaultdict(lambda: {"pos": 0, "neg": 0, "n": 0}), "quotes": {"pos": [], "neg": []}})
        a["items"] += 1
        a["sources"][r["source"]] += 1
        s = r["sentiment"] or 0
        a["sent_sum"] += s
        if r["rating"]:
            a["rated"] += 1
            a["rating_sum"] += r["rating"]
        pol = "pos" if s >= 0.3 else "neg" if s <= -0.3 else None
        if pol:
            a[pol] += 1
            if len(a["quotes"][pol]) < 4 and len(r["text"]) > 30:
                a["quotes"][pol].append({"text": r["text"][:220], "source": r["source"], "created": r["created"], "url": r["url"], "likes": r["likes"], "ref": r["ref_title"]})
        for t in _themes(r):
            th = a["themes"][t]
            th["n"] += 1
            if pol:
                th[pol] += 1
    out = []
    for b in settings.brands:
        a = agg.get(b.name)
        if not a:
            continue
        themes = {t: v for t, v in sorted(a["themes"].items(), key=lambda x: -x[1]["n"])}
        out.append({"brand": b.name, "self": b.name == me, "items": a["items"], "avg_rating": round(a["rating_sum"] / a["rated"], 2) if a["rated"] else None,
                    "rated": a["rated"], "pos": a["pos"], "neg": a["neg"], "neg_pct": round(100 * a["neg"] / a["items"]) if a["items"] else 0,
                    "sentiment": round(a["sent_sum"] / a["items"], 2) if a["items"] else 0, "sources": dict(a["sources"]), "themes": themes,
                    "top_complaints": [t for t, v in sorted(themes.items(), key=lambda x: -x[1]["neg"]) if v["neg"] >= 2][:4],
                    "top_praise": [t for t, v in sorted(themes.items(), key=lambda x: -x[1]["pos"]) if v["pos"] >= 2][:4], "quotes": a["quotes"]})
    out.sort(key=lambda x: (not x["self"], -x["items"]))
    field = [o for o in out if not o["self"]]
    field_neg = round(sum(o["neg"] for o in field) / max(1, sum(o["items"] for o in field)) * 100)
    for o in out:
        o["conclusion"] = brand_conclusion(o, field_neg)
    return {"days": days, "brands": out, "themes": THEME_KEYS, "sources": SOURCES,
            "stats": {"items": len(rows), "brands": len(out), "neg_pct_field": field_neg,
                      "avg_rating_field": round(sum(o["avg_rating"] * o["rated"] for o in field if o["avg_rating"]) / max(1, sum(o["rated"] for o in field)), 2)}}


def theme_matrix(days: int = 90) -> dict:
    """brand x theme: share of that brand's items that complain about the theme (what to attack / what to fix)."""
    s = summary(days)
    rows = []
    for b in s["brands"]:
        cells = {}
        for t in THEME_KEYS:
            v = b["themes"].get(t) or {"pos": 0, "neg": 0, "n": 0}
            cells[t] = {"neg_pct": round(100 * v["neg"] / b["items"]) if b["items"] else 0, "pos_pct": round(100 * v["pos"] / b["items"]) if b["items"] else 0, **v}
        rows.append({"brand": b["brand"], "self": b["self"], "items": b["items"], "cells": cells})
    return {"themes": THEME_KEYS, "brands": rows}


def items(brand: str = "", source: str = "", theme: str = "", polarity: str = "", q: str = "", days: int = 180, limit: int = 100) -> list[dict]:
    sql, p = "SELECT * FROM voice WHERE created>=?", [_day(days)]
    if brand:
        sql += " AND brand=?"; p.append(brand)
    if source:
        sql += " AND source=?"; p.append(source)
    if theme:
        sql += " AND themes LIKE ?"; p.append(f'%"{theme}"%')
    if polarity == "neg":
        sql += " AND sentiment<=-0.3"
    elif polarity == "pos":
        sql += " AND sentiment>=0.3"
    if q:
        sql += " AND text LIKE ?"; p.append(f"%{q}%")
    sql += " ORDER BY created DESC, likes DESC LIMIT ?"; p.append(min(limit, 500))
    rows = db.q(sql, p)
    for r in rows:
        r["themes"] = json.loads(r["themes"] or "[]")
    return rows


def timeline(brand: str, weeks: int = 12) -> list[dict]:
    out = defaultdict(lambda: {"items": 0, "neg": 0, "pos": 0})
    for r in db.q("SELECT created, sentiment FROM voice WHERE brand=? AND created>=?", (brand, _day(7 * weeks))):
        try:
            w = datetime.fromisoformat(r["created"][:10]).strftime("%G-W%V")
        except ValueError:
            continue
        out[w]["items"] += 1
        if (r["sentiment"] or 0) <= -0.3:
            out[w]["neg"] += 1
        elif (r["sentiment"] or 0) >= 0.3:
            out[w]["pos"] += 1
    return [{"week": w, **v} for w, v in sorted(out.items())]


def digest(brand: str, days: int = 90) -> str | None:
    """AI-written 120-word read of what buyers say about one brand (cached a day); None without a key."""
    from . import enrich
    if not settings.ai_enabled:
        return None
    key = f"voice_digest:{brand}:{days}"
    cached = db.kv_get(key)
    if cached and cached.get("at", "") > (datetime.now(timezone.utc) - timedelta(days=1)).isoformat():
        return cached["text"]
    s = summary(days, brand)
    if not s["brands"]:
        return None
    b = s["brands"][0]
    sample = items(brand=brand, days=days, limit=60)
    prompt = (f"Summarise what buyers say about {brand} in India from these {b['items']} comments and reviews (last {days} days). "
              f"Average rating {b['avg_rating']}, {b['neg_pct']}% negative. Themes: {json.dumps({t: v for t, v in list(b['themes'].items())[:8]})}. "
              "Write 4 short lines: 1) what they love 2) what they complain about 3) the one fix or claim Pebble should make against this "
              "4) one quote. Plain text.\n\nSAMPLE:\n" + "\n".join(f"- [{r['source']} {r['rating'] or ''}] {r['text'][:200]}" for r in sample))
    text = enrich.ask_text(prompt, max_tokens=600)
    if text:
        db.kv_set(key, {"at": db.now(), "text": text})
    return text


# ---------------------------------------------------------------- market and user-demand perspective
FEATURE_ASKS = [
    ("blood pressure / ECG",  r"\bbp\b|blood pressure|\becg\b"),
    ("bluetooth calling",     r"calling|bt call|bluetooth call"),
    ("AMOLED / big display",  r"amoled|oled|big (screen|display)|1\.\d+ ?inch|2 ?inch|display"),
    ("battery life",          r"battery|backup|long lasting|days battery"),
    ("sleep / stress / HRV",  r"sleep|stress|hrv|recovery|readiness"),
    ("GPS / sports modes",    r"\bgps\b|running|cycling|sports? mode|marathon|fitness"),
    ("ANC / sound",           r"\banc\b|noise cancel|bass|sound quality|dolby|spatial"),
    ("waterproof",            r"water ?proof|swim|ip68|ip67"),
    ("price point",           r"under ?\d|below ?\d|cheap|budget|price|affordable|best .* (under|below)"),
    ("for women / kids",      r"women|girls|ladies|kids|children|boys|men\b"),
    ("premium / design",      r"premium|luxury|stylish|metal|titanium|slim|design"),
    ("rings",                 r"\bring\b|smart ring"),
    ("screenless bands",      r"screenless|whoop|fitness band|band\b"),
    ("compatibility",         r"iphone|ios|android|compatib"),
]
_ASK_RX = [(t, re.compile(rx, re.I)) for t, rx in FEATURE_ASKS]


def _segment_of(ref_title: str, text: str, brand: str) -> str:
    """The product title names the segment for store and Amazon reviews; app-store and YouTube items fall back to the
    brand's main segment, and only an explicit product word in the text overrides that."""
    for sg in settings.segments:
        if sg.get("keywords") and re.search(sg["keywords"], ref_title or "", re.I):
            return sg["key"]
    b = settings.brand(brand)
    strong = re.search(r"smart ?ring|ring|earbud|tws|buds|neckband|headphone|speaker|smart ?watch|watch|charger|power ?bank", text or "", re.I)
    if strong:
        for sg in settings.segments:
            if sg.get("keywords") and re.search(sg["keywords"], strong.group(0), re.I):
                return sg["key"]
    return (b.segments[0] if b and b.segments else "") or "other"


def market(days: int = 90) -> dict:
    """The field as a whole: what buyers talk about (by theme, split into praise and complaints), how each segment is
    rated, every source's weight, the weekly mood, the ratings scoreboard, what India asks for in search, and a
    written conclusion for Pebble. All from the voice table (plus app_metrics, reviews, channel_listings,
    search_suggestions), no AI needed."""
    me = _me()
    rows = db.q("SELECT brand, source, rating, sentiment, themes, text, ref_title, created FROM voice WHERE created>=?", (_day(days),))
    themes: dict[str, dict] = defaultdict(lambda: {"n": 0, "pos": 0, "neg": 0, "brands": set(), "rival_neg": 0, "pebble_neg": 0, "pebble_n": 0})
    segs: dict[str, dict] = defaultdict(lambda: {"items": 0, "rated": 0, "rating_sum": 0.0, "neg": 0, "pos": 0, "themes": defaultdict(lambda: {"pos": 0, "neg": 0}), "brands": set()})
    sources: dict[str, dict] = defaultdict(lambda: {"items": 0, "rated": 0, "rating_sum": 0.0, "neg": 0})
    weeks: dict[str, dict] = defaultdict(lambda: {"items": 0, "neg": 0, "pos": 0, "pebble": 0, "pebble_neg": 0})
    for r in rows:
        s = r["sentiment"] or 0
        pol = "pos" if s >= 0.3 else "neg" if s <= -0.3 else None
        own = r["brand"] == me
        ts = _themes(r)
        for t in ts:
            th = themes[t]
            th["n"] += 1
            th["brands"].add(r["brand"])
            if pol:
                th[pol] += 1
                if pol == "neg":
                    th["rival_neg" if not own else "pebble_neg"] += 1
            if own:
                th["pebble_n"] += 1
        sg = segs[_segment_of(r["ref_title"], r["text"], r["brand"])]
        sg["items"] += 1
        sg["brands"].add(r["brand"])
        if r["rating"]:
            sg["rated"] += 1
            sg["rating_sum"] += r["rating"]
        if pol:
            sg[pol] += 1
            for t in ts:
                sg["themes"][t][pol] += 1
        so = sources[r["source"]]
        so["items"] += 1
        if r["rating"]:
            so["rated"] += 1
            so["rating_sum"] += r["rating"]
        if pol == "neg":
            so["neg"] += 1
        try:
            w = datetime.fromisoformat((r["created"] or "")[:10]).strftime("%G-W%V")
        except ValueError:
            w = ""
        if w:
            wk = weeks[w]
            wk["items"] += 1
            if pol:
                wk[pol] += 1
            if own:
                wk["pebble"] += 1
                if pol == "neg":
                    wk["pebble_neg"] += 1
    total = len(rows) or 1
    demand = []
    for t, v in themes.items():
        pos_pct = round(100 * v["pos"] / v["n"]) if v["n"] else 0
        neg_pct = round(100 * v["neg"] / v["n"]) if v["n"] else 0
        demand.append({"theme": t, "n": v["n"], "share_pct": round(100 * v["n"] / total, 1), "pos": v["pos"], "neg": v["neg"], "pos_pct": pos_pct, "neg_pct": neg_pct,
                       "brands": len(v["brands"]), "rival_neg": v["rival_neg"], "pebble_neg": v["pebble_neg"], "pebble_n": v["pebble_n"],
                       "read": ("table stakes: buyers praise it, Pebble must match" if pos_pct >= 55 and v["n"] >= 10 else
                                "open wound: the field gets complaints here, a claim to own" if neg_pct >= 45 and v["n"] >= 10 else
                                "contested: praise and complaints in equal measure" if v["n"] >= 10 else "thin data")})
    demand.sort(key=lambda d: -d["n"])
    by_segment = []
    for k, v in segs.items():
        name = next((sg.get("name") or sg["key"] for sg in settings.segments if sg["key"] == k), k)
        tops = sorted(v["themes"].items(), key=lambda x: -x[1]["neg"])
        praise = sorted(v["themes"].items(), key=lambda x: -x[1]["pos"])
        by_segment.append({"segment": k, "name": name, "items": v["items"], "brands": len(v["brands"]),
                           "avg_rating": round(v["rating_sum"] / v["rated"], 2) if v["rated"] else None,
                           "neg_pct": round(100 * v["neg"] / v["items"]) if v["items"] else 0, "pos_pct": round(100 * v["pos"] / v["items"]) if v["items"] else 0,
                           "top_complaint": tops[0][0] if tops and tops[0][1]["neg"] else None, "top_praise": praise[0][0] if praise and praise[0][1]["pos"] else None})
    by_segment.sort(key=lambda x: -x["items"])
    by_source = [{"source": k, "items": v["items"], "avg_rating": round(v["rating_sum"] / v["rated"], 2) if v["rated"] else None,
                  "neg_pct": round(100 * v["neg"] / v["items"]) if v["items"] else 0} for k, v in sorted(sources.items(), key=lambda x: -x[1]["items"])]
    labels = []
    now = datetime.now(timezone.utc)
    for i in range(11, -1, -1):
        labels.append((now - timedelta(weeks=i)).strftime("%G-W%V"))
    trend = [{"week": w, **weeks.get(w, {"items": 0, "neg": 0, "pos": 0, "pebble": 0, "pebble_neg": 0})} for w in labels]
    for t in trend:
        t["neg_pct"] = round(100 * t["neg"] / t["items"]) if t["items"] else None
    return {"days": days, "items": len(rows), "demand": demand, "by_segment": by_segment, "by_source": by_source, "trend": trend,
            "asks": asks(), "scoreboard": scoreboard(days), "conclusion": market_conclusion(days, demand, by_segment, rows),
            "last_run": db.kv_get("collect:voice") or {}}


def asks(limit: int = 14) -> list[dict]:
    """What India asks for: Google autocomplete suggestions (the searchdemand sensor) grouped into feature asks,
    with the brands people attach to each ask. Demand before it is a purchase."""
    rows = db.q("SELECT seed, suggestion, brand, first_seen FROM search_suggestions")
    out: dict[str, dict] = defaultdict(lambda: {"n": 0, "brands": defaultdict(int), "examples": [], "new_30d": 0})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for r in rows:
        s = r["suggestion"] or ""
        for t, rx in _ASK_RX:
            if rx.search(s):
                a = out[t]
                a["n"] += 1
                if r["brand"] and r["brand"] != "Sector":
                    a["brands"][r["brand"]] += 1
                if len(a["examples"]) < 4 and s not in a["examples"]:
                    a["examples"].append(s)
                if (r["first_seen"] or "") >= cutoff:
                    a["new_30d"] += 1
    res = [{"ask": t, "n": v["n"], "new_30d": v["new_30d"], "examples": v["examples"],
            "brands": [b for b, _ in sorted(v["brands"].items(), key=lambda x: -x[1])[:4]]} for t, v in out.items()]
    res.sort(key=lambda x: -x["n"])
    return res[:limit]


def scoreboard(days: int = 90) -> list[dict]:
    """Every public rating per brand in one row: Google Play, App Store, the brand's own store, Amazon.in listings,
    and the voice table's items and negative share for the window."""
    me = _me()
    apps = db.q("SELECT brand, package, rating, ratings_count FROM app_metrics a WHERE id=(SELECT MAX(id) FROM app_metrics b WHERE b.package=a.package)")
    own = {r["brand"]: r for r in db.q("SELECT brand, COUNT(*) n, AVG(rating) r FROM reviews GROUP BY brand")}
    amz = {r["brand"]: r for r in db.q("SELECT brand, COUNT(*) n, SUM(rating*COALESCE(rating_count,1))/SUM(COALESCE(rating_count,1)) r, SUM(rating_count) c "
                                        "FROM channel_listings WHERE channel='amazon' AND rating>0 GROUP BY brand")}
    voice = {b["brand"]: b for b in summary(days)["brands"]}
    out = []
    for b in settings.brands:
        play = [a for a in apps if a["brand"] == b.name and not a["package"].startswith("ios:") and a["rating"]]
        ios = [a for a in apps if a["brand"] == b.name and a["package"].startswith("ios:") and a["rating"]]
        play.sort(key=lambda a: -(a["ratings_count"] or 0))
        ios.sort(key=lambda a: -(a["ratings_count"] or 0))
        v = voice.get(b.name) or {}
        o, a = own.get(b.name), amz.get(b.name)
        row = {"brand": b.name, "self": b.name == me,
               "play": play[0]["rating"] if play else None, "play_count": play[0]["ratings_count"] if play else None,
               "ios": ios[0]["rating"] if ios else None, "ios_count": ios[0]["ratings_count"] if ios else None,
               "store": round(o["r"], 2) if o else None, "store_count": o["n"] if o else None,
               "amazon": round(a["r"], 2) if a and a["r"] else None, "amazon_count": a["c"] if a else None,
               "voice_items": v.get("items", 0), "voice_rating": v.get("avg_rating"), "neg_pct": v.get("neg_pct"),
               "top_complaints": v.get("top_complaints", []), "top_praise": v.get("top_praise", [])}
        # the composite only trusts a store with a real sample (20 app ratings, 5 store reviews); tiny samples still show
        vals = [x for x, c, mn in ((row["play"], row["play_count"], 20), (row["ios"], row["ios_count"], 20), (row["store"], row["store_count"], 5),
                                   (row["amazon"], row["amazon_count"], 20)) if x and (c or 0) >= mn]
        row["composite"] = round(sum(vals) / len(vals), 2) if vals else None
        if vals or row["voice_items"]:
            out.append(row)
    out.sort(key=lambda r: (not r["self"], -(r["composite"] or 0), -r["voice_items"]))
    return out


def _pct(n: int, d: int) -> int:
    return round(100 * n / d) if d else 0


def brand_conclusion(b: dict, field_neg_pct: int) -> str:
    """One paragraph a product manager could paste into a deck: scale of evidence, mood, what they praise, what they
    complain about, and what Pebble should do with it."""
    me = _me()
    own = b["brand"] == me
    n, neg, rating = b["items"], b["neg_pct"], b["avg_rating"]
    srcs = ", ".join(f"{k} {v}" for k, v in sorted(b["sources"].items(), key=lambda x: -x[1]))
    mood = ("buyers are happy" if neg <= max(10, field_neg_pct - 8) and (rating is None or rating >= 3.8) else
            "buyers are unhappy" if neg >= field_neg_pct + 10 or (rating is not None and rating < 3.0) else "a mixed picture")
    parts = [f"{n} comments and reviews ({srcs})" + (f", average {rating}★" if rating else "") + f": {mood}, {neg}% negative against {field_neg_pct}% for the field."]
    if b["top_praise"]:
        parts.append("Praised for " + ", ".join(b["top_praise"][:3]) + ".")
    if b["top_complaints"]:
        parts.append("Complaints cluster on " + ", ".join(b["top_complaints"][:3]) + ".")
    weak = [t for t in b["top_complaints"] if t not in b["top_praise"]]   # a theme both praised and attacked is contested, not a lead
    strong = [t for t in b["top_praise"] if t not in b["top_complaints"]]
    contested = [t for t in b["top_complaints"] if t in b["top_praise"]]
    if own:
        if weak:
            parts.append(f"Fix {weak[0]} first: it is the theme Pebble's own buyers raise most, and rivals will quote it.")
        elif contested:
            parts.append(f"{contested[0]} divides Pebble's buyers: read the complaints by model before changing anything.")
        else:
            parts.append("No complaint theme repeats yet; keep the evidence base growing (App Store and Play reviews are read daily).")
    else:
        if weak:
            parts.append(f"Against {b['brand']}, Pebble's comparison page should lead on {weak[0]}" +
                         (f" and show proof on {strong[0]}, where {b['brand']} is strong." if strong else "."))
        elif contested:
            parts.append(f"Buyers split on {b['brand']}'s {contested[0]}: a model-by-model claim beats a blanket one.")
        elif strong:
            parts.append(f"{b['brand']} is winning on {strong[0]}; Pebble's claim there must be at least as strong.")
    if n < 15:
        parts.append("Thin evidence so far; read it as a hint, not a verdict.")
    return " ".join(parts)


def market_conclusion(days: int, demand: list[dict], by_segment: list[dict], rows: list[dict]) -> str:
    me = _me()
    if not rows:
        return ("No comments or reviews collected yet. The voice collector reads App Store and Google Play reviews, YouTube comments, "
                "Amazon.in product pages and the brands' own store reviews with the daily tier; press Refresh reviews to start now.")
    n = len(rows)
    neg = sum(1 for r in rows if (r["sentiment"] or 0) <= -0.3)
    pos = sum(1 for r in rows if (r["sentiment"] or 0) >= 0.3)
    mine = [r for r in rows if r["brand"] == me]
    mine_neg = sum(1 for r in mine if (r["sentiment"] or 0) <= -0.3)
    brands = len({r["brand"] for r in rows})
    big = [d for d in demand if d["n"] >= 10]
    stakes = [d["theme"] for d in big if d["pos_pct"] >= 55][:3]
    wounds = sorted([d for d in big if d["neg_pct"] >= 45], key=lambda d: -d["rival_neg"])[:3]
    parts = [f"{n:,} buyer comments and reviews across {brands} brands in {days} days: {_pct(pos, n)}% positive, {_pct(neg, n)}% negative."]
    if stakes:
        parts.append("Table stakes (praised across the field): " + ", ".join(stakes) + ".")
    if wounds:
        parts.append("Open wounds (the field gets complaints here): " + ", ".join(f"{d['theme']} ({d['neg_pct']}% negative)" for d in wounds) +
                     f". The first of these is the claim Pebble can own if its own buyers do not raise it.")
    if by_segment:
        worst = min((s for s in by_segment if s["items"] >= 40), key=lambda s: -s["neg_pct"], default=None)
        best = max((s for s in by_segment if s["items"] >= 40 and s["avg_rating"]), key=lambda s: s["avg_rating"], default=None)
        if worst:
            parts.append(f"By segment, {worst['name']} draws the most complaints ({worst['neg_pct']}% negative" +
                         (f", mostly {worst['top_complaint']}" if worst["top_complaint"] else "") + ")" +
                         (f"; {best['name']} is the happiest at {best['avg_rating']}★." if best and best["segment"] != worst["segment"] else "."))
    if mine:
        d = _pct(mine_neg, len(mine)) - _pct(neg, n)
        parts.append(f"Pebble: {len(mine)} items, {_pct(mine_neg, len(mine))}% negative, " +
                     (f"{abs(d)} points better than the field." if d < -2 else f"{d} points worse than the field." if d > 2 else "in line with the field."))
    else:
        parts.append("No Pebble items in the window yet: Pebble's own store, Play and App Store reviews land here on the next daily run.")
    return " ".join(parts)


def brand_view(brand: str, days: int = 90) -> dict:
    """Everything the brand page's Reviews tab needs: the summary row, a written conclusion, the weekly timeline,
    the ratings scoreboard row and the latest quotable items."""
    s = summary(days, brand)
    field = summary(days)["stats"]["neg_pct_field"]
    b = s["brands"][0] if s["brands"] else None
    row = next((r for r in scoreboard(days) if r["brand"] == brand), None)
    return {"brand": brand, "days": days, "summary": b, "conclusion": brand_conclusion(b, field) if b else None, "field_neg_pct": field,
            "timeline": timeline(brand), "scoreboard": row, "items": items(brand=brand, days=days, limit=30),
            "digest": (db.kv_get(f"voice_digest:{brand}:{days}") or {}).get("text")}


def market_digest(days: int = 90) -> str | None:
    """AI-written read of the whole field (cached a day); None without a key."""
    from . import enrich
    if not settings.ai_enabled:
        return None
    key = f"voice_market_digest:{days}"
    cached = db.kv_get(key)
    if cached and cached.get("at", "") > (datetime.now(timezone.utc) - timedelta(days=1)).isoformat():
        return cached["text"]
    m = market(days)
    if not m["items"]:
        return None
    prompt = ("You advise Pebble, an Indian D2C wearables and audio brand. From this voice-of-customer data across its rivals "
              f"(last {days} days), write 6 short lines: 1) what Indian buyers want most right now 2) the complaint the field shares "
              "3) which rival is winning hearts and why 4) which rival is losing and why 5) the one claim Pebble should own 6) the one "
              "thing Pebble must fix. Plain text, specific, no preamble.\n\n"
              f"THEMES: {json.dumps([{k: d[k] for k in ('theme', 'n', 'pos_pct', 'neg_pct', 'read')} for d in m['demand'][:10]])}\n"
              f"SEGMENTS: {json.dumps(m['by_segment'][:6])}\nASKS: {json.dumps([{k: a[k] for k in ('ask', 'n', 'examples')} for a in m['asks'][:8]])}\n"
              f"SCOREBOARD: {json.dumps([{k: r[k] for k in ('brand', 'composite', 'neg_pct', 'top_complaints', 'top_praise')} for r in m['scoreboard'][:12]])}\n"
              f"HEURISTIC READ: {m['conclusion']}")
    text = enrich.ask_text(prompt, max_tokens=700)
    if text:
        db.kv_set(key, {"at": db.now(), "text": text})
    return text
