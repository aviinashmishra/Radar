"""Auto-written daily / weekly brief. Claude or Gemini writes it when available; otherwise a structured digest."""
from __future__ import annotations
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db, enrich, alerts, analytics, market
from .config import settings

log = logging.getLogger(__name__)


def _gather(days: int) -> tuple[list[dict], list[dict]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    ev = db.q("SELECT brand,kind,title,importance,so_what,counter_move,url,category FROM events "
              "WHERE created_at>=? AND kind<>'baseline' ORDER BY importance DESC, id DESC LIMIT 150", (since,))
    sig = db.q("SELECT kind,brand,title,detail,importance FROM signals WHERE created_at>=? ORDER BY importance DESC", (since,))
    return ev, sig


def _template(days: int, ev: list[dict], sig: list[dict]) -> str:
    by_brand = defaultdict(list)
    for e in ev:
        by_brand[e["brand"]].append(e)
    lines = [f"PEBBLE COMPETITOR RADAR - {'weekly' if days >= 7 else 'daily'} brief ({datetime.now().strftime('%d %b %Y')})", ""]
    if sig:
        lines.append("SIGNALS")
        lines += [f"- [{s['importance']}] {s['title']}" for s in sig[:8]]
        lines.append("")
    top = [e for e in ev if (e["importance"] or 0) >= 7][:10]
    if top:
        lines.append("TOP EVENTS")
        lines += [f"- [{e['importance']}] {e['brand']}: {e['title'][:110]}" + (f"\n    -> {e['counter_move']}" if e.get("counter_move") else "") for e in top]
        lines.append("")
    lines.append("BY BRAND")
    for b, es in sorted(by_brand.items(), key=lambda x: -len(x[1])):
        kinds = defaultdict(int)
        for e in es:
            kinds[e["kind"]] += 1
        lines.append(f"- {b}: {len(es)} events (" + ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])[:4]) + ")")
    ws = analytics.whitespace()
    gaps = [k for k, v in ws["cells"].items() if v["gap"]]
    if gaps:
        lines += ["", "WHITE SPACE (3+ competitor SKUs, 0 Pebble SKUs)"] + [f"- {g.replace('|', ' @ INR ')}" for g in gaps[:8]]
    ctx = _context(days)
    if ctx["pressure"]["top"]:
        lines += ["", f"MONEY AT STAKE ({days}d, model in config/market.yaml)",
                  f"- at risk {market.fmt_inr(ctx['pressure']['total_risk'])}/month · upside {market.fmt_inr(ctx['pressure']['total_upside'])}/month"]
        lines += [f"- {t['brand']}: {t['label']} ({t['kind']}: {t['title'][:70]})" for t in ctx["pressure"]["top"][:5]]
    if ctx["stories"]:
        lines += ["", "NEWS STORIES (most covered)"] + [f"- {s['brand']} [{s['topic']}, {s['n_sources']} outlets]: {s['title'][:100]}" for s in ctx["stories"][:6]]
    if ctx["money"]:
        lines += ["", "FINANCIALS"] + [f"- {m}" for m in ctx["money"][:6]]
    if ctx.get("shelf"):
        lines += ["", "ON THE SHELF (marketplaces + quick commerce)"] + [f"- {s}" for s in ctx["shelf"][:6]]
    if ctx.get("voice"):
        lines += ["", "BUYER VOICE (reviews and comments)"] + [f"- {s}" for s in ctx["voice"][:6]]
    return "\n".join(lines)


def _context(days: int) -> dict:
    """Market and money context shared by the template and the AI prompt."""
    from . import finance, newsintel
    pressure = market.pressure(max(days, 7))
    stories = [{k: v for k, v in s.items() if k not in ("sources", "ids")} for s in newsintel.stories(days=max(days, 7), limit=12)]
    money = []
    for p in finance.peers()["brands"]:
        if p["self"] or not p.get("revenue"):
            continue
        r = p["revenue"]
        amt = f"₹{r['value']:,.0f} cr" if r["currency"] == "INR" else f"{r['currency']} {r['value']:,.0f}M"
        money.append(f"{p['brand']} ({p.get('entity') or '-'}): revenue {amt} {r['period']}"
                     + (f", {p['revenue_growth_pct']:+.0f}% YoY" if p.get("revenue_growth_pct") is not None else "")
                     + (f", margin {p['profit']['margin_pct']:+.0f}%" if p.get("profit") and p["profit"].get("margin_pct") is not None else "")
                     + (f", {p['scale_vs_pebble']}x Pebble" if p.get("scale_vs_pebble") else "") + f" [{r['confidence']}]")
    sizes = market.sizes()
    shelf, voice_lines = [], []
    try:
        from . import channels as ch, voice as vz
        me = settings.self_brand.name if settings.self_brand else ""
        mx = ch.matrix(max(days, 7))
        mine = next((b for b in mx["brands"] if b["brand"] == me), None)
        if mine:
            live = [f"{c['name']} {v.get('listings', 0)}" for c in mx["channels"] for v in [mine["cells"].get(c["key"], {})] if v.get("listings")]
            shelf.append(f"Pebble live on: {', '.join(live) if live else 'no marketplace listing read yet'}")
        for p in [p for p in ch.parity() if p["self"] and p["gap_pct"] <= -8][:3]:
            shelf.append(f"{p['channel']} sells {p['title'][:50]} at INR {p['marketplace_price']:,.0f}, {abs(p['gap_pct'])}% under pebblecart")
        for e in db.q("SELECT brand, title FROM events WHERE source='channels' AND importance>=6 AND created_at>=datetime('now', ?) ORDER BY importance DESC LIMIT 4", (f"-{max(days, 7)} days",)):
            shelf.append(f"{e['brand']}: {e['title'][:90]}")
        vs = vz.summary(max(days, 7))
        me_v = next((b for b in vs["brands"] if b["self"]), None)
        if me_v:
            worst = sorted(me_v["themes"].items(), key=lambda t: -t[1]["neg"])[:2]
            voice_lines.append(f"Pebble: {me_v['items']} items, {me_v['neg_pct']}% negative (field {vs['stats']['neg_pct_field']}%)"
                               + ("; top complaints " + ", ".join(f"{t} x{v['neg']}" for t, v in worst if v["neg"]) if worst else ""))
        for b in sorted([b for b in vs["brands"] if not b["self"] and b["items"] >= 10], key=lambda b: -b["neg_pct"])[:4]:
            top = max(b["themes"].items(), key=lambda t: t[1]["neg"], default=None)
            voice_lines.append(f"{b['brand']}: {b['neg_pct']}% negative of {b['items']}" + (f", mostly {top[0]}" if top and top[1]["neg"] else ""))
    except Exception:  # noqa: BLE001
        log.exception("brief: shelf/voice context failed")
    return {"pressure": pressure, "stories": stories, "money": money, "shelf": shelf, "voice": voice_lines,
            "market": [{k: s[k] for k in ("key", "value_cr", "growth_pct", "pebble_share_pct", "implied_share_pct", "pebble_rank")} for s in sizes["segments"]]}


def write(days: int = 7, push: bool = True) -> str:
    ev, sig = _gather(days)
    body = None
    if settings.ai_enabled and (ev or sig):
        bench = analytics.benchmark()
        ctx = _context(days)
        prompt = (f"Write the {'weekly' if days >= 7 else 'daily'} competitor brief for {settings.me if settings.self_brand else 'a leadership team competing in this sector'} ({settings.sector}). Max 450 words. "
                  "Structure: 1) Three things to act on this week, each with the concrete move and the ₹/month at stake where the model "
                  "gives one. 2) What each major rival did. 3) Pricing / white-space observations. 4) Money and market: what rivals' "
                  f"financials, funding and share prices mean for {settings.me if settings.self_brand else 'a challenger'}, and where the market is growing or shrinking. 5) One risk and one "
                  "opportunity. Plain text, short lines, no markdown tables. Mark model estimates as estimates.\n\n"
                  f"SIGNALS:\n{json.dumps(sig)[:6000]}\n\nEVENTS:\n{json.dumps(ev)[:26000]}\n\nBENCHMARK:\n{json.dumps(bench)[:5000]}\n\n"
                  f"IMPACT MODEL (INR/month):\n{json.dumps({k: ctx['pressure'][k] for k in ('total_risk', 'total_upside', 'by_brand', 'by_segment', 'top')}, default=str)[:5000]}\n\n"
                  f"NEWS STORIES:\n{json.dumps(ctx['stories'])[:5000]}\n\nRIVAL FINANCIALS:\n{json.dumps(ctx['money'])[:3000]}\n\n"
                  f"MARKET SIZES (India, INR crore/yr):\n{json.dumps(ctx['market'])[:2000]}")
        body = enrich.ask_text(prompt)
    if not body:
        body = _template(days, ev, sig)
    body = settings.localize(body) or body
    with db.tx() as c:
        c.execute("INSERT INTO briefs(created_at,period,body) VALUES(?,?,?)", (db.now(), f"{days}d", body))
    if push:
        alerts.send_text(body)
        from . import exports
        exports.send_email(f"{settings.sector_label or 'Radar'} {'weekly' if days >= 7 else 'daily'} brief", body)
    return body
