"""Reports: daily, weekly, monthly and yearly performance reports plus the sector / industry report, built from every
layer of the radar (own sales and rivals' sales proxies, store moves, signals, news, finance, distribution, voice of
the customer, regions, market model, strategy) and written as Markdown with a JSON appendix.

  python -m radar.reports --kind weekly [--period 2026-W44] [--push]   build one report (and push it to Telegram / email)
  python -m radar.reports --due [--push]                               build whatever is due today (the runner and the
                                                                       GitHub Actions workflow call this every day)
A report is stored in the `reports` table keyed by (kind, period), so re-building replaces it. Claude / Gemini write
the executive summary when a key is set; the sections below it are always data-driven.
"""
from __future__ import annotations
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from . import db, schema_ext, analytics, sales as sl
from .config import settings

log = logging.getLogger("radar.reports")
KINDS = ("daily", "weekly", "monthly", "yearly", "sector")
SIGNAL_DAYS = {"daily": 1, "weekly": 7, "monthly": 31, "yearly": 366, "sector": 90}


def _me() -> str:
    return settings.self_brand.name if settings.self_brand else ""


def _try(fn, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.info("report section skipped: %s", e)
        return default


def _inr(n: float | None) -> str:
    if n is None:
        return "–"
    n = float(n)
    if n >= 1e7:
        return f"₹{n / 1e7:,.2f} cr"
    if n >= 1e5:
        return f"₹{n / 1e5:,.1f} L"
    return f"₹{n:,.0f}"


def _pct(v) -> str:
    return "–" if v is None else f"{v:+.0f}%"


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_nothing in this period_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- gather
def gather(kind: str, period: str | None = None) -> dict:
    me = _me()
    days = SIGNAL_DAYS[kind]
    sr = sl.report(kind if kind in sl.KINDS else "monthly", period)
    start, end = sr["start"], sr["end"]
    s_iso, e_iso = f"{start}T00:00:00", f"{end}T00:00:00"
    if kind == "sector":
        s_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        e_iso = db.now()
    events = db.q("SELECT id, created_at, brand, kind, title, importance, so_what, counter_move, url FROM events WHERE created_at>=? AND created_at<? "
                  "AND kind<>'baseline' ORDER BY importance DESC, id DESC LIMIT 400", (s_iso, e_iso))
    by_kind, by_brand = {}, {}
    for e in events:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        by_brand[e["brand"]] = by_brand.get(e["brand"], 0) + 1
    signals = db.q("SELECT created_at, kind, brand, title, importance FROM signals WHERE created_at>=? AND created_at<? ORDER BY importance DESC LIMIT 30", (s_iso, e_iso))
    from . import channels as ch, voice as vz, regions as rg, strategy as st
    g = {"kind": kind, "period": sr["period"], "start": start, "end": end, "generated_at": db.now(), "sales": sr,
         "events": events[:40], "event_counts": {"total": len(events), "by_kind": dict(sorted(by_kind.items(), key=lambda x: -x[1])[:12]),
                                                 "by_brand": dict(sorted(by_brand.items(), key=lambda x: -x[1])[:12])},
         "signals": signals, "benchmark": _try(analytics.benchmark, []), "whitespace_gaps": [k for k, v in (_try(analytics.whitespace, {"cells": {}})["cells"]).items() if v.get("gap")][:10],
         "winners": _try(lambda: analytics.winners(10), []), "channels": _try(lambda: ch.matrix(7 if kind == "daily" else 30), None),
         "parity": _try(ch.parity, []), "voice": _try(lambda: vz.summary(max(7, days)), None), "regions": _try(lambda: rg.overview(max(30, days)), None)}
    from . import newsintel, finance, market as mk_mod
    g["news"] = _try(lambda: newsintel.overview(max(7, min(days, 90))), None)
    g["finance"] = _try(lambda: finance.peers(), None)
    mk = mk_mod
    g["market"] = _try(lambda: mk.sizes(), None) if mk else None
    g["pressure"] = _try(lambda: mk.pressure(max(7, min(days, 90))), None) if mk else None
    g["share_of_signal"] = _try(lambda: mk.share_of_signal(max(7, min(days, 90))), None) if mk else None
    if kind in ("monthly", "yearly", "sector"):
        g["strategy"] = _try(lambda: st.playbook(refresh=True), None)
    return g


# ---------------------------------------------------------------- render
def render(g: dict) -> str:
    me = _me() or "Pebble"
    kind, period = g["kind"], g["period"]
    title = {"daily": f"Daily report · {period}", "weekly": f"Weekly report · {period}", "monthly": f"Monthly report · {period}",
             "yearly": f"Annual report · {period}", "sector": f"Sector & industry report · {g['generated_at'][:10]}"}[kind]
    L = [f"# {me} Radar - {title}", f"_window {g['start']} to {g['end']} · generated {g['generated_at'][:16].replace('T', ' ')} UTC_", ""]
    sr = g["sales"]
    own = sr["own"]
    # 1 sales
    L.append("## 1. Sales")
    if own["has_data"]:
        prev_word = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year", "sector": "month"}[kind]
        L.append(f"**Revenue {_inr(own['revenue'])}** ({_pct(own['growth']['revenue_pct'])} vs previous {prev_word}) · "
                 f"**{own['units']:,.0f} units** ({_pct(own['growth']['units_pct'])}) · {own['orders']:,} orders · AOV {_inr(own['aov'])}")
        L.append("")
        L.append("**By segment**")
        L.append(_table(["Segment", "Revenue", "Units", "Growth"], [[s["key"], _inr(s["revenue"]), f"{s['units']:,.0f}", _pct(s.get("growth_pct"))] for s in own["by_segment"]]))
        L.append("**By channel**")
        L.append(_table(["Channel", "Revenue", "Units"], [[s["key"], _inr(s["revenue"]), f"{s['units']:,.0f}"] for s in own["by_channel"]]))
        L.append("**By state**")
        L.append(_table(["State", "Revenue", "Units"], [[s["key"], _inr(s["revenue"]), f"{s['units']:,.0f}"] for s in own["by_state"][:10]]))
        L.append("**Top SKUs**")
        L.append(_table(["SKU", "Revenue", "Units"], [[f"{s['title'][:40] or s['key']}", _inr(s["revenue"]), f"{s['units']:,.0f}"] for s in own["top_skus"][:10]]))
    else:
        L.append(own["note"] or "No own sales data in the window.")
        L.append("")
    L.append("**Sales proxies, Pebble and rivals** (units estimated from reviews and ratings gained in the window; index 100 = strongest)")
    L.append(_table(["Brand", "Index", "Est. units", "Store reviews", "App ratings", "Marketplace ratings", "vs prev"],
                    [[("**" + p["brand"] + "**") if p["self"] else p["brand"], p["index"], f"{p['est_units']:,}", p["store_reviews"], p["app_ratings"], p["marketplace_ratings"], _pct(p.get("growth_pct"))]
                     for p in sr["proxies"][:12] if p["est_units"] or p["self"] or p["demand_events"]]))
    # 2 what moved
    L.append("## 2. What rivals did")
    ec = g["event_counts"]
    L.append(f"{ec['total']} events · " + " · ".join(f"{k.replace('_', ' ')} {v}" for k, v in list(ec["by_kind"].items())[:8]))
    L.append("")
    L.append(_table(["Imp.", "Brand", "Event", "So what"], [[e["importance"] or "–", e["brand"], e["title"][:90], (e["so_what"] or "")[:110]] for e in g["events"][:15]]))
    if g["signals"]:
        L.append("**Signals**")
        L.append("\n".join(f"- [{s['importance']}] {s['title']}" for s in g["signals"][:10]) + "\n")
    # 3 pricing
    L.append("## 3. Pricing and white space")
    L.append(_table(["Category", f"{me} median", "Field median", "Gap", f"{me} SKUs", "Field SKUs", "Field discount"],
                    [[b["category"], _inr(b["self"]["median_price"]) if b["self"]["median_price"] else "none", _inr(b["field"]["median_price"]), _pct(b["price_gap_pct"]),
                      b["self"]["skus"], b["field"]["skus"], f"{b['field']['avg_discount_pct']}%"] for b in g["benchmark"][:10]]))
    if g["whitespace_gaps"]:
        L.append("White space (3+ rival SKUs, none from " + me + "): " + ", ".join(x.replace("|", " @ ₹") for x in g["whitespace_gaps"]) + "\n")
    if g["winners"]:
        L.append("Rival products that are selling: " + "; ".join(f"{w['brand']} {w['title'][:40]} ({_inr(w['price'])})" for w in g["winners"][:6]) + "\n")
    # 4 distribution
    if g["channels"]:
        L.append("## 4. Distribution")
        m = g["channels"]
        keys = [c["key"] for c in m["channels"]]
        rows = []
        for b in m["brands"][:16]:
            cells = []
            for k in keys:
                c = b["cells"][k]
                cells.append(("●" if c["status"] == "live" else "○" if c["status"] == "reported" else "·") + (f" {len(c['cities'])}c" if c["cities"] else ""))
            rows.append([("**" + b["brand"] + "**") if b["self"] else b["brand"]] + cells)
        L.append(_table(["Brand"] + [c["name"][:12] for c in m["channels"]], rows))
        L.append("● live listing · ○ reported in news/press · Nc = Blinkit cities\n")
        if g["parity"]:
            L.append("**Marketplace undercuts of own-store prices**")
            L.append("\n".join(f"- {u['brand']} on {u['channel']}: {u['title'][:100]}" for u in g["parity"][:8]) + "\n")
    # 5 voice
    if g["voice"] and g["voice"]["brands"]:
        L.append("## 5. Voice of the customer")
        v = g["voice"]
        L.append(_table(["Brand", "Items", "Avg rating", "Negative", "Top complaints", "Top praise"],
                        [[("**" + b["brand"] + "**") if b["self"] else b["brand"], b["items"], b["avg_rating"] or "–", f"{b['neg_pct']}%", ", ".join(b["top_complaints"]) or "–", ", ".join(b["top_praise"]) or "–"]
                         for b in v["brands"][:14]]))
        for b in v["brands"][:6]:
            q = (b["quotes"]["neg"] or b["quotes"]["pos"])
            if q:
                L.append(f"- {b['brand']} ({q[0]['source']}): \"{q[0]['text'][:160]}\"")
        L.append("")
    # 6 news + finance
    if g["news"]:
        n = g["news"]
        L.append("## 6. News and share of voice")
        L.append(_table(["Brand", "Mentions", "Prev", "Share", "Topics"], [[s["brand"], s["mentions"], s["prev"], f"{s['share_pct']}%", ", ".join(list(s["topics"])[:3])] for s in n["share"][:10] if s["mentions"]]))
        L.append("\n".join(f"- [{s['importance'] or '–'}] {s['brand']}: {s['title'][:100]} ({s['n_sources']} sources)" for s in n["stories"][:10]) + "\n")
    if g["finance"]:
        f = g["finance"]
        L.append("## 7. Money")
        rows = []
        for p in f["brands"][:14]:
            if not (p.get("revenue") or p.get("quote") or p.get("valuation")):
                continue
            rev = p["revenue"]
            rows.append([("**" + p["brand"] + "**") if p["self"] else p["brand"], p.get("entity") or "–",
                         f"{rev['value']:,.0f} {rev['currency']} {rev['unit']} ({rev['period']}, {rev['confidence']})" if rev else "–",
                         _pct(p.get("revenue_growth_pct")), (f"{p['profit']['signed']:+,.0f} {p['profit']['currency']}" if p.get("profit") else "–"),
                         (f"{p['quote']['close']:,.0f} {p['quote']['currency']} ({p['quote']['change_pct']:+.1f}%)" if p.get("quote") else "–")])
        L.append(_table(["Brand", "Company", "Revenue", "Growth", "Profit", "Share price"], rows))
    # 8 regions
    if g["regions"]:
        r = g["regions"]
        L.append("## 8. Regions")
        if r["sales"]["total_revenue"]:
            L.append("Own sales by state: " + ", ".join(f"{s['state']} {_inr(s['revenue'])}" for s in r["sales"]["top_states"][:8]) + "\n")
        if r["strongholds"]:
            L.append(f"{me} search strongholds: " + ", ".join(f"{s['state']} ({s['pebble_interest']})" for s in r["strongholds"][:6]) + "\n")
        if r["gaps"]:
            L.append("Where rivals out-index " + me + ": " + "; ".join(f"{x['state']} ({x['why']})" for x in r["gaps"][:6]) + "\n")
        qc = [c for c in r["cities"] if c["qc_brands"]]
        if qc:
            L.append("Quick commerce: " + ", ".join(f"{c['city']} {c['qc_brands']} brands" + (f" ({me} {c['qc_pebble']} live)" if c["qc_pebble"] else f" ({me} absent)") for c in qc[:10]) + "\n")
    # 9 market + pressure
    if g["market"]:
        mkt = g["market"]
        L.append("## 9. Market model")
        L.append(_table(["Segment", "Size (₹ cr)", "Growth", f"{me} share", "Leaders", "Live median"],
                        [[s["name"], f"{s['value_cr']:,.0f}", _pct(s.get("growth_pct")), f"{s['pebble_share_pct']}%" if s.get("pebble_share_pct") is not None else "–",
                          ", ".join(list(s["shares"])[:3]), _inr(s.get("live_median_price")) if s.get("live_median_price") else "–"] for s in mkt["segments"]]))
        L.append(f"Total addressable (tracked segments): ₹{mkt['total_value_cr']:,.0f} cr · sources: {'; '.join(mkt['sources'][:2])}\n")
    if g["pressure"]:
        p = g["pressure"]
        L.append(f"**Pressure model**: ≈ {_inr(p['total_risk'])}/month at risk from rival moves, ≈ {_inr(p['total_upside'])}/month upside from their stock-outs "
                 "(see market.yaml for the assumptions). Biggest: " + "; ".join(f"{b['brand']} {_inr(b['risk'])} risk" for b in p["by_brand"][:4]) + "\n")
    if g.get("share_of_signal"):
        L.append("Share of signal (composite of news, search, creators, apps, attention; leader = 100): " +
                 ", ".join(f"{s['brand']} {s['composite']}" for s in g["share_of_signal"][:10]) + "\n")
    # 10 strategy
    if g.get("strategy"):
        s = g["strategy"]
        L.append("## 10. Strategy")
        L.append(_table(["Segment", "Verdict", "Attractiveness", "Position", "Why"], [[c["name"], c["verdict"], c["attractiveness"], c["position"], "; ".join(c["why"][:2])] for c in s["scorecard"]]))
        L.append("**USP claims**\n" + "\n".join(f"- {c['claim']}" for c in s["usp"]["claims"][:6]) + "\n")
        L.append("**Fix first**\n" + "\n".join(f"- [{p['severity']}] {p['problem']} -> {p['fix']}" for p in s["problems"][:6]) + "\n")
        for h, items in s["plan"].items():
            L.append(f"**Next {h} days**\n" + "\n".join(f"- {x}" for x in items) + "\n")
    L.append("---")
    L.append("_Sources: the radar's own database (stores, marketplaces, quick commerce, news, YouTube, Reddit, app stores, Google Trends, "
             "Wikipedia, Yahoo Finance, SEC EDGAR, config/market.yaml, config/companies.yaml). Estimates are marked as such._")
    return "\n".join(L)


def exec_summary(g: dict, body: str) -> str | None:
    from . import enrich
    if not settings.ai_enabled:
        return None
    prompt = (f"Write the executive summary (max 180 words, plain text, 5-7 short lines) for this {g['kind']} report for Pebble's founders. "
              "Lead with the number that matters most, then the three moves to make. Only use figures from the report.\n\n" + body[:30000])
    return enrich.ask_text(prompt, max_tokens=700)


# ---------------------------------------------------------------- build / store / schedule
def build(kind: str, period: str | None = None, push: bool = False, ai: bool = True) -> dict:
    schema_ext.ensure()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    g = gather(kind, period)
    body = settings.localize(render(g)) or ""
    summary = exec_summary(g, body) if ai else None
    if summary:
        body = body.replace("\n\n## 1. Sales", f"\n\n## Executive summary\n{summary.strip()}\n\n## 1. Sales", 1)
    title = body.splitlines()[0].lstrip("# ")
    data = {k: g[k] for k in ("sales", "event_counts", "signals", "whitespace_gaps", "parity") if k in g}
    data["summary"] = summary
    with db.tx() as c:
        c.execute("INSERT INTO reports(created_at,kind,period,title,body,data) VALUES(?,?,?,?,?,?) ON CONFLICT(kind,period) DO UPDATE SET "
                  "created_at=excluded.created_at, title=excluded.title, body=excluded.body, data=excluded.data",
                  (db.now(), kind, g["period"] if kind != "sector" else g["generated_at"][:7], title, body, json.dumps(data, default=str)))
    row = db.q1("SELECT id, created_at, kind, period, title FROM reports WHERE kind=? AND period=?", (kind, g["period"] if kind != "sector" else g["generated_at"][:7]))
    if push:
        from . import alerts, exports
        head = summary or "\n".join(body.splitlines()[:25])
        alerts.send_text(f"{title}\n\n{head[:3500]}")
        exports.send_email(title, body)
    return {**row, "body": body}


def due(today: datetime | None = None) -> list[str]:
    """Which reports are due now and not yet written: daily after 08:00, weekly on Monday, monthly and sector on the 1st, yearly on 1 Jan."""
    now = today or datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)  # IST
    out = []
    if now.hour >= 8:
        yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if not db.q1("SELECT 1 FROM reports WHERE kind='daily' AND period=?", (yday,)):
            out.append("daily")
        if now.weekday() == 0:
            wk = (now - timedelta(days=7)).strftime("%G-W%V")
            if not db.q1("SELECT 1 FROM reports WHERE kind='weekly' AND period=?", (wk,)):
                out.append("weekly")
        if now.day == 1:
            first = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if not db.q1("SELECT 1 FROM reports WHERE kind='monthly' AND period=?", (first,)):
                out.append("monthly")
            if not db.q1("SELECT 1 FROM reports WHERE kind='sector' AND period=?", (now.strftime("%Y-%m"),)):
                out.append("sector")
            if now.month == 1 and not db.q1("SELECT 1 FROM reports WHERE kind='yearly' AND period=?", (str(now.year - 1),)):
                out.append("yearly")
    return out


def run_due(push: bool = True) -> list[dict]:
    out = []
    for kind in due():
        try:
            out.append(build(kind, push=push))
            log.info("report %s written", kind)
        except Exception as e:  # noqa: BLE001
            log.exception("report %s failed: %s", kind, e)
    return out


def list_reports(kind: str = "", limit: int = 40) -> list[dict]:
    sql, p = "SELECT id, created_at, kind, period, title, substr(body, 1, 400) preview FROM reports", []
    if kind:
        sql += " WHERE kind=?"; p.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"; p.append(limit)
    return db.q(sql, p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pebble Radar reports")
    ap.add_argument("--kind", choices=KINDS)
    ap.add_argument("--period")
    ap.add_argument("--due", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if a.due:
            for r in run_due(push=a.push):
                print(r["kind"], r["period"], r["title"])
        elif a.kind:
            print(build(a.kind, a.period, push=a.push, ai=not a.no_ai)["body"])
        else:
            ap.print_help()
    finally:
        db.close()
