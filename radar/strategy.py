"""Strategy engine: turns everything the radar knows into a playbook for Pebble.

  scorecard   per segment: market size and growth (market.yaml), Pebble's share and price position, competitive intensity
              (Herfindahl index of the shares), white space, subscription-free angle, channel and regional coverage ->
              a verdict: defend / attack / enter / watch
  usp         claims Pebble can own: themes rivals get complaints about where Pebble does not (voice), what Pebble is
              praised for, independent-accuracy evidence on the watchlist, price gaps
  problems    what to fix first: Pebble's own complaint themes, channels where rivals are live and Pebble is not,
              states where rivals index high and Pebble low, marketplace undercuts of Pebble's D2C price, app rating vs field
  threats / opportunities   open signals, launch predictors, rivals' funding and losses, store-dry and stock-out windows
  plan        30 / 60 / 90-day actions generated from the above
  memo        an AI-written strategy memo over the same JSON when a key is set (cached for a day)
Every figure carries its basis so the founders can argue with the assumptions, not the arithmetic.
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re
from . import db, analytics
from .config import settings


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _try(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional modules (market, finance, newsintel) may be absent or empty
        return default


def _hhi(shares: dict) -> int:
    return round(sum((float(v)) ** 2 for v in shares.values()))


def scorecard() -> list[dict]:
    """One row per segment with attractiveness, Pebble's position and a verdict."""
    me = _me()
    from . import market as mk  # the peer module; falls back to settings.market when its helpers are missing
    sizes = _try(lambda: mk.sizes()["segments"], [])
    if not sizes:
        raw = (settings.market or {}).get("segments") or {}
        sizes = [{"key": k, "name": s.get("name", k), "units_m": s.get("units_m"), "growth_pct": s.get("growth_pct"), "value_cr": s.get("value_cr"),
                  "shares": s.get("shares_pct") or {}, "pebble_share_pct": (s.get("shares_pct") or {}).get(me), "trend": s.get("trend", ""), "confidence": s.get("confidence")}
                 for k, s in raw.items()]
    bench = {b["category"]: b for b in _try(analytics.benchmark, [])}
    ws = _try(analytics.whitespace, {"cells": {}})
    gaps_by_cat = defaultdict(list)
    for k, v in ws["cells"].items():
        if v.get("gap"):
            cat, band = k.split("|")
            gaps_by_cat[cat].append(band)
    watch = _try(analytics.watchlist_overview, [])
    from . import channels as _ch
    chm = _try(lambda: _ch.matrix(), {"brands": []})
    pebble_ch = next((r for r in chm["brands"] if r["self"]), None)
    rows = []
    for s in sizes:
        key = s["key"]
        cat = analytics.SEGMENT_CATEGORY.get(key, s["name"])
        b = bench.get(cat) or {}
        shares = s.get("shares") or {}
        hhi = _hhi(shares)
        share = s.get("pebble_share_pct")
        growth = s.get("growth_pct") or 0
        value = s.get("value_cr") or 0
        seg_watch = [w for w in watch if w.get("segment") == key and not w.get("self")]
        subs = sum(1 for w in seg_watch if w.get("subscription") == "Mandatory")
        gap = b.get("price_gap_pct")
        attractiveness = min(100, max(0, 40 + growth * 0.6 + min(30, value / 300) - max(0, hhi - 1500) / 100))
        position = min(100, max(0, (share or 0) * 4 + (b.get("self") or {}).get("skus", 0) * 3 + (10 if gap is not None and -15 <= gap <= 5 else 0)))
        if share and share >= 15:
            verdict = "defend"
        elif growth >= 20 and (share or 0) < 10:
            verdict = "attack" if (b.get("self") or {}).get("skus") else "enter"
        elif growth < 0 and (share or 0) < 5:
            verdict = "watch"
        else:
            verdict = "attack" if (b.get("self") or {}).get("skus") else "enter"
        why = []
        if growth >= 20:
            why.append(f"growing {growth:+.0f}% a year")
        elif growth < 0:
            why.append(f"shrinking {growth:.0f}% a year")
        if hhi >= 2500:
            why.append(f"concentrated (HHI {hhi}): {', '.join(list(shares)[:2])} dominate")
        elif hhi and hhi < 1500:
            why.append(f"fragmented (HHI {hhi}): share is up for grabs")
        if gap is not None:
            why.append(f"Pebble median {gap:+.0f}% vs field")
        if gaps_by_cat.get(cat):
            why.append(f"white space at ₹{', ₹'.join(gaps_by_cat[cat][:3])}")
        if subs:
            why.append(f"{subs} rival products need a subscription: 'no subscription, ever' is a wedge")
        if pebble_ch:
            live_ch = [k for k, c in pebble_ch["cells"].items() if c["status"] == "live"]
            if len(live_ch) <= 1:
                why.append("Pebble is live on at most one marketplace the radar reads")
        rows.append({"segment": key, "name": s["name"], "value_cr": value, "growth_pct": growth, "pebble_share_pct": share, "pebble_rank": s.get("pebble_rank"),
                     "hhi": hhi, "leaders": list(shares)[:3], "attractiveness": round(attractiveness), "position": round(position), "verdict": verdict,
                     "price_gap_pct": gap, "pebble_skus": (b.get("self") or {}).get("skus", 0), "field_skus": (b.get("field") or {}).get("skus", 0),
                     "white_space": gaps_by_cat.get(cat, []), "subscription_rivals": subs, "trend": s.get("trend", ""), "confidence": s.get("confidence"),
                     "why": why})
    order = {"defend": 0, "attack": 1, "enter": 2, "watch": 3}
    return sorted(rows, key=lambda r: (order[r["verdict"]], -r["attractiveness"]))


def usp() -> dict:
    """Claims Pebble can own, with the evidence behind each."""
    me = _me()
    from . import voice as vz
    vs = _try(lambda: vz.summary(120), {"brands": []})
    mine = next((b for b in vs["brands"] if b["self"]), None)
    field = [b for b in vs["brands"] if not b["self"]]
    claims = []
    # 1) rivals' complaint themes where Pebble's complaint share is lower
    agg = defaultdict(lambda: {"neg": 0, "items": 0, "brands": defaultdict(int), "quote": None})
    for b in field:
        for t, v in b["themes"].items():
            a = agg[t]
            a["neg"] += v["neg"]
            a["items"] += b["items"]
            if v["neg"]:
                a["brands"][b["brand"]] += v["neg"]
    for t, a in sorted(agg.items(), key=lambda x: -x[1]["neg"]):
        if a["neg"] < 5:
            continue
        field_pct = 100 * a["neg"] / max(1, a["items"])
        mine_pct = (100 * (mine["themes"].get(t) or {}).get("neg", 0) / mine["items"]) if mine and mine["items"] else None
        if mine_pct is None or mine_pct <= field_pct:
            top = sorted(a["brands"].items(), key=lambda x: -x[1])[:3]
            q = next(iter(vz.items(brand=top[0][0], theme=t, polarity="neg", days=120, limit=1)), None) if top else None
            claims.append({"kind": "attack", "theme": t, "claim": f"Own '{t}' - rivals take {a['neg']} complaints about it (" + ", ".join(f"{b} {n}" for b, n in top) + ")",
                           "field_neg_pct": round(field_pct, 1), "pebble_neg_pct": round(mine_pct, 1) if mine_pct is not None else None,
                           "rivals": [b for b, _ in top], "quote": q and {"brand": q["brand"], "text": q["text"], "source": q["source"]},
                           "basis": "share of customer comments / reviews that complain about the theme, last 120 days"})
    # 2) what Pebble is praised for
    if mine:
        for t in mine["top_praise"]:
            v = mine["themes"][t]
            q = next(iter(vz.items(brand=me, theme=t, polarity="pos", days=120, limit=1)), None)
            claims.append({"kind": "lead", "theme": t, "claim": f"Lead with '{t}': Pebble's own buyers praise it ({v['pos']} positive mentions)",
                           "quote": q and {"brand": me, "text": q["text"], "source": q["source"]}, "basis": "Pebble's own reviews and comments"})
    # 3) subscription-free and price gaps from the watchlist / benchmark
    watch = _try(analytics.watchlist_overview, [])
    subs = [w for w in watch if w.get("subscription") == "Mandatory"]
    if subs:
        claims.append({"kind": "wedge", "theme": "subscription", "claim": f"No subscription, ever: {len(subs)} rival products need one (" + ", ".join(w["name"] for w in subs[:4]) + ")",
                       "basis": "watchlist.yaml subscription column; 12-month cost on the Segments page"})
    indep = [w for w in watch if str(w.get("evidence", "")).startswith("Independent") and not w.get("self")]
    if indep:
        claims.append({"kind": "gap", "theme": "accuracy evidence", "claim": f"Publish independent accuracy data: {len(indep)} rival products already have it (" + ", ".join(w["brand"] for w in indep[:4]) + ")",
                       "basis": "evidence column of the watchlist"})
    for b in _try(analytics.benchmark, []):
        if b.get("price_gap_pct") is not None and b["price_gap_pct"] <= -15 and (b.get("self") or {}).get("skus"):
            claims.append({"kind": "lead", "theme": "price", "claim": f"{b['category']}: Pebble's median is {abs(b['price_gap_pct'])}% below the field - say it",
                           "basis": "live store catalogues (Pricing page)"})
    return {"claims": claims[:14], "pebble_voice": mine and {"items": mine["items"], "avg_rating": mine["avg_rating"], "neg_pct": mine["neg_pct"],
                                                              "top_complaints": mine["top_complaints"], "top_praise": mine["top_praise"]}}


def problems() -> list[dict]:
    """What to fix first, most expensive first."""
    me = _me()
    out = []
    from . import voice as vz, channels as ch, regions as rg
    vs = _try(lambda: vz.summary(120, me), {"brands": []})
    mine = vs["brands"][0] if vs["brands"] else None
    if mine:
        for t in mine["top_complaints"]:
            v = mine["themes"][t]
            q = next(iter(vz.items(brand=me, theme=t, polarity="neg", days=120, limit=1)), None)
            out.append({"area": "product", "severity": min(10, 4 + v["neg"] // 2), "problem": f"Pebble buyers complain about {t} ({v['neg']} negative mentions)",
                        "fix": f"Route '{t}' complaints to product; publish a fix note and reply to the reviews.",
                        "quote": q and {"text": q["text"], "source": q["source"]}, "basis": "voice table, last 120 days"})
    m = _try(ch.matrix, {"brands": [], "channels": []})
    peb = next((r for r in m["brands"] if r["self"]), None)
    if peb:
        names = {c["key"]: c["name"] for c in m["channels"]}
        for key, cell in peb["cells"].items():
            rivals_live = sum(1 for r in m["brands"] if not r["self"] and r["cells"][key]["status"] == "live")
            rivals_rep = sum(1 for r in m["brands"] if not r["self"] and r["cells"][key]["status"] != "none")
            if cell["status"] == "none" and rivals_live >= 3:
                out.append({"area": "distribution", "severity": 7, "problem": f"Pebble is not on {names.get(key, key)}; {rivals_live} rivals are live there",
                            "fix": f"List the top 5 SKUs on {names.get(key, key)} (or fix the search ranking: the radar searches '{settings.self_brand.name if settings.self_brand else 'Pebble'} + segment').",
                            "basis": "channel listings, last 7 days"})
            elif cell["status"] == "none" and rivals_rep >= 4 and key in ("blinkit", "zepto", "instamart", "croma", "reliance"):
                out.append({"area": "distribution", "severity": 5, "problem": f"No sign of Pebble on {names.get(key, key)}; {rivals_rep} rivals are reported there",
                            "fix": "Open a conversation with the channel's category team; quick commerce is where impulse wearable buys moved.", "basis": "news/press mentions"})
        if peb["qc_cities"] and peb["qc_cities"] < len(m["cities"]) // 2:
            out.append({"area": "distribution", "severity": 5, "problem": f"Pebble is on Blinkit in only {peb['qc_cities']} of {len(m['cities'])} cities the radar reads",
                        "fix": "Ask Blinkit for dark-store coverage in the missing cities; the Regions page lists them.", "basis": "Blinkit shelves per city"})
    for u in _try(lambda: ch.parity(me), []):
        out.append({"area": "pricing", "severity": 8, "problem": f"{u['channel']} undercuts Pebble's own store by {u['gap_pct']}% on {u['title'][:60]}",
                    "fix": "Align the marketplace price or add D2C-only value (bundle, extended warranty, faster delivery).", "basis": "channel_undercut event", "url": u["url"]})
    r = _try(rg.overview, {"gaps": []})
    for g in r["gaps"][:3]:
        out.append({"area": "regions", "severity": 5, "problem": f"{g['state']}: rivals index {g['field_interest']} in search interest, Pebble {g['pebble_interest']}",
                    "fix": f"Regional creators and marketplace ads in {g['state']}; check offline distribution there.", "basis": "Google Trends interest by state"})
    apps = _try(analytics.app_health, [])
    mine_apps = [a for a in apps if a["brand"] == me and a.get("rating")]
    field_apps = [a for a in apps if a["brand"] != me and a.get("rating")]
    if mine_apps and field_apps:
        fa = sum(a["rating"] for a in field_apps) / len(field_apps)
        worst = {}
        for a in mine_apps:  # one row per app name (Play and App Store rows share the name): the weaker store
            key = re.sub(r"[^a-z]", "", (a["name"] or "").lower())[:12]
            if key not in worst or a["rating"] < worst[key]["rating"]:
                worst[key] = a
        for a in worst.values():
            if a["rating"] < fa - 0.3:
                out.append({"area": "app", "severity": 6, "problem": f"{a['name']} rates {a['rating']} vs a field average of {fa:.1f}",
                            "fix": "The app is the product for a screenless band: fix sync/pairing first (see the app theme on the Reviews page).", "basis": "Google Play / App Store ratings"})
    for s in db.q("SELECT kind, MAX(title) title, MAX(importance) importance FROM signals WHERE brand=? AND COALESCE(status,'open')='open' AND created_at>=? GROUP BY kind", (me, _since(30))):
        out.append({"area": "signal", "severity": s["importance"] or 5, "problem": s["title"], "fix": "See the Today page.", "basis": "signal engine"})
    return sorted(out, key=lambda x: -x["severity"])[:14]


def threats_and_opportunities() -> dict:
    me = _me()
    threats, opps, seen = [], [], set()
    for s in db.q("SELECT kind, brand, title, importance, data FROM signals WHERE COALESCE(status,'open')='open' AND created_at>=? ORDER BY importance DESC, id DESC", (_since(14),)):
        if s["brand"] == me or (s["brand"], s["kind"]) in seen:
            continue
        seen.add((s["brand"], s["kind"]))
        item = {"brand": s["brand"], "title": s["title"], "importance": s["importance"], "kind": s["kind"]}
        (opps if s["kind"] in ("stockout_opportunity", "store_dry", "pain_point") else threats).append(item)
    for e in db.q("SELECT brand, kind, title, importance, data FROM events WHERE kind IN ('financial_fact','financial_result','stock_move','hidden_product') "
                  "AND importance>=6 AND created_at>=? AND brand<>? ORDER BY importance DESC LIMIT 20", (_since(45), me)):
        d = json.loads(e["data"] or "{}")
        if (e["brand"], e["kind"]) in seen:
            continue
        seen.add((e["brand"], e["kind"]))
        item = {"brand": e["brand"], "title": e["title"], "importance": e["importance"], "kind": e["kind"]}
        if e["kind"] in ("financial_fact", "financial_result") and d.get("metric") in ("net_loss",) or "loss" in e["title"].lower():
            opps.append({**item, "why": "a loss-making rival cuts marketing before it cuts price"})
        elif e["kind"] in ("financial_fact",) and d.get("metric") in ("funding_round", "valuation"):
            threats.append({**item, "why": "fresh money becomes ads and discounts within a quarter"})
        elif e["kind"] == "hidden_product":
            threats.append({**item, "why": "launch being prepared"})
    launch = db.kv_get("launch_scores") or {}
    for b, v in list(launch.items())[:5]:
        if b != me and v.get("score", 0) >= 6 and (b, "launch_predictor") not in seen:
            threats.append({"brand": b, "title": f"{b} launch score {v['score']}", "importance": 6, "kind": "launch_predictor", "why": "; ".join(v.get("why", [])[:2])})
    from . import market as mk
    pressure = _try(lambda: mk.pressure(30), None)
    return {"threats": threats[:12], "opportunities": opps[:12], "pressure": pressure and {"total_risk": pressure["total_risk"], "total_upside": pressure["total_upside"],
                                                                                            "by_brand": pressure["by_brand"][:6], "by_segment": pressure["by_segment"][:6]}}


def plan(card: list[dict], claims: dict, probs: list[dict], to: dict) -> dict:
    """30 / 60 / 90-day actions assembled from the findings (deterministic; the AI memo narrates them)."""
    d30, d60, d90 = [], [], []
    for p in probs[:4]:
        d30.append(f"Fix: {p['problem']} -> {p['fix']}")
    for o in to["opportunities"][:2]:
        d30.append(f"Capture: {o['title']}")
    for c in [c for c in claims["claims"] if c["kind"] == "attack"][:2]:
        d30.append(f"Ad angle: {c['claim']}")
    for s in [s for s in card if s["verdict"] == "defend"][:2]:
        d60.append(f"Defend {s['name']}: hold price position ({s['price_gap_pct']:+.0f}% vs field) and fill white space at ₹{', ₹'.join(s['white_space'][:2]) or 'none'}"
                   if s["price_gap_pct"] is not None else f"Defend {s['name']}: keep the #{s.get('pebble_rank') or '?'} position with a refresh this quarter")
    for s in [s for s in card if s["verdict"] in ("attack", "enter")][:2]:
        d60.append(f"{'Enter' if s['verdict'] == 'enter' else 'Push'} {s['name']} (₹{s['value_cr']:,.0f} cr, {s['growth_pct']:+.0f}%/yr, HHI {s['hhi']}): " + (s["why"][0] if s["why"] else ""))
    for c in [c for c in claims["claims"] if c["kind"] in ("wedge", "gap")][:2]:
        d60.append(c["claim"])
    for t in to["threats"][:3]:
        d90.append(f"Prepare for: {t['title']}" + (f" ({t['why']})" if t.get("why") else ""))
    d90.append("Publish independent accuracy validation for the band/ring line and a 'no subscription' comparison page.")
    d90.append("Review this playbook against the monthly sector report; re-score the segments with real sales (Reports page).")
    return {"30": d30[:8], "60": d60[:8], "90": d90[:8]}


def playbook(refresh: bool = False) -> dict:
    cached = db.kv_get("strategy_playbook")
    if cached and not refresh and cached.get("at", "") > (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat():
        return cached["data"]
    card = scorecard()
    claims = usp()
    probs = problems()
    to = threats_and_opportunities()
    data = {"generated_at": db.now(), "scorecard": card, "usp": claims, "problems": probs, **to, "plan": plan(card, claims, probs, to),
            "swot": {"strengths": [c["claim"] for c in claims["claims"] if c["kind"] == "lead"][:5],
                     "weaknesses": [p["problem"] for p in probs if p["area"] in ("product", "app", "pricing")][:5],
                     "opportunities": [c["claim"] for c in claims["claims"] if c["kind"] in ("attack", "wedge", "gap")][:3] + [o["title"] for o in to["opportunities"][:3]],
                     "threats": [t["title"] for t in to["threats"][:5]]}}
    db.kv_set("strategy_playbook", {"at": db.now(), "data": data})
    return data


def memo(refresh: bool = False) -> str | None:
    """AI strategy memo (~400 words) over the playbook JSON; None without a key."""
    from . import enrich
    if not settings.ai_enabled:
        return None
    cached = db.kv_get("strategy_memo")
    if cached and not refresh and cached.get("at", "") > (datetime.now(timezone.utc) - timedelta(days=1)).isoformat():
        return cached["text"]
    pb = playbook()
    prompt = ("You are advising Pebble's founders. From this playbook JSON write a strategy memo, max 450 words, plain text, short lines: "
              "1) Where to play (which segments to defend / attack / enter and why) 2) How to win (3 USP claims with the evidence) "
              "3) What to fix first 4) Threats to prepare for 5) The 30/60/90 plan as three short lists. Use the numbers given; do not invent any.\n\n"
              + json.dumps({k: pb[k] for k in ("scorecard", "usp", "problems", "threats", "opportunities", "plan")}, default=str)[:40000])
    text = enrich.ask_text(prompt, max_tokens=1500)
    if text:
        db.kv_set("strategy_memo", {"at": db.now(), "text": text})
    return text
