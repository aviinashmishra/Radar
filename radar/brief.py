"""Auto-written daily / weekly brief. Claude or Gemini writes it when available; otherwise a structured digest."""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db, enrich, alerts, analytics
from .config import settings


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
    return "\n".join(lines)


def write(days: int = 7, push: bool = True) -> str:
    ev, sig = _gather(days)
    body = None
    if settings.ai_enabled and (ev or sig):
        bench = analytics.benchmark()
        prompt = (f"Write the {'weekly' if days >= 7 else 'daily'} competitor brief for Pebble's founders. Max 350 words. "
                  "Structure: 1) Three things to act on this week, each with the concrete move. 2) What each major rival did. "
                  "3) Pricing / white-space observations. 4) One risk and one opportunity. Plain text, short lines, no markdown tables.\n\n"
                  f"SIGNALS:\n{json.dumps(sig)[:6000]}\n\nEVENTS:\n{json.dumps(ev)[:30000]}\n\nBENCHMARK:\n{json.dumps(bench)[:6000]}")
        body = enrich.ask_text(prompt)
    if not body:
        body = _template(days, ev, sig)
    with db.tx() as c:
        c.execute("INSERT INTO briefs(created_at,period,body) VALUES(?,?,?)", (db.now(), f"{days}d", body))
    if push:
        alerts.send_text(body)
        from . import exports
        exports.send_email(f"Pebble Radar {'weekly' if days >= 7 else 'daily'} brief", body)
    return body
