"""Financial profiles for the Financials page and the chat: the company behind each brand, its latest revenue, profit,
growth, valuation, funding and share price, with the source and confidence of every figure, and how it scales
against Pebble.

Rows come from the `financials` table (seeds from companies.yaml, SEC EDGAR filings, facts extracted from news) and
the `quotes` table (daily closes of listed parents). When several sources report the same metric for the same
period, the most trustworthy wins: filing > reported > ai > extracted > estimate.
"""
from __future__ import annotations
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import db
from .config import settings

RANK = {"filing": 5, "reported": 4, "ai": 3, "extracted": 2, "estimate": 1}
FX_INR = {"INR": 1.0, "USD": 84.0, "EUR": 92.0, "KRW": 0.06, "CNY": 11.6}   # rough, for scale comparisons only
PROFIT_METRICS = ("net_profit", "net_loss")


def period_key(p: str) -> float:
    """FY25 -> 2025.0, Q2FY26 -> 2025.5 (Indian FY ends in March), 2024 -> 2024.0; unknown -> 0."""
    p = (p or "").strip().upper()
    if m := re.match(r"Q([1-4])FY(\d{2,4})", p):
        y = int(m.group(2)) if len(m.group(2)) == 4 else 2000 + int(m.group(2))
        return y - 1 + int(m.group(1)) * 0.25
    if m := re.match(r"FY(\d{2,4})", p):
        y = int(m.group(1))
        return float(y if y > 100 else 2000 + y)
    if m := re.match(r"(20\d{2})", p):
        return float(m.group(1)) + 0.75  # a calendar year ends after FY of the same number
    return 0.0


def to_inr_cr(value: float, currency: str, unit: str) -> float | None:
    """Everything to INR crore for the scale comparison."""
    if value is None:
        return None
    mult = {"crore": 1e7, "million": 1e6, "billion": 1e9, "lakh": 1e5, "": 1}.get((unit or "").lower(), 1)
    return value * mult * FX_INR.get((currency or "INR").upper(), 1.0) / 1e7


def _best(rows: list[dict]) -> dict | None:
    """The most trustworthy row for the latest annual period (quarters only when no annual figure exists)."""
    annual = [r for r in rows if not r["period"].upper().startswith("Q")]
    pool = annual or rows
    if not pool:
        return None
    latest = max(period_key(r["period"]) for r in pool)
    cands = [r for r in pool if period_key(r["period"]) == latest]
    return max(cands, key=lambda r: (RANK.get(r["confidence"], 0), r["seen_at"]))


def _series(rows: list[dict]) -> list[dict]:
    """One value per period, most trustworthy row each, oldest first."""
    by_p = defaultdict(list)
    for r in rows:
        if not r["period"].upper().startswith("Q"):
            by_p[r["period"]].append(r)
    out = []
    for p, rs in by_p.items():
        r = max(rs, key=lambda x: (RANK.get(x["confidence"], 0), x["seen_at"]))
        out.append({"period": p, "value": r["value"], "currency": r["currency"], "unit": r["unit"], "confidence": r["confidence"], "key": period_key(p)})
    return sorted(out, key=lambda x: x["key"])


def fmt_money(value: float | None, currency: str = "INR", unit: str = "crore") -> str:
    """₹3,100 cr · $1.8B · €40M, for the bot, the brief and event titles."""
    if value is None:
        return "–"
    cur = (currency or "INR").upper()
    if cur == "INR":
        if (unit or "").lower() == "crore":
            return f"₹{value:,.0f} cr"
        return f"₹{value:,.0f}"
    sym = {"USD": "$", "EUR": "€"}.get(cur, cur + " ")
    if (unit or "").lower() == "billion":
        return f"{sym}{value:,.1f}B"
    if abs(value) >= 1000:
        return f"{sym}{value / 1000:,.1f}B"
    return f"{sym}{value:,.0f}M"


def links_of(c: dict) -> dict:
    out = {}
    if c.get("sec_cik"):
        out["sec"] = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(c['sec_cik']):010d}&type=10-K&dateb=&owner=include&count=10"
    if c.get("ticker"):
        out["yahoo"] = f"https://finance.yahoo.com/quote/{c['ticker']}"
    if c.get("wikipedia"):
        out["wikipedia"] = "https://en.wikipedia.org/wiki/" + str(c["wikipedia"]).replace(" ", "_")
    return out


def quote_of(brand: str) -> dict | None:
    q = db.q1("SELECT * FROM quotes WHERE brand=? ORDER BY day DESC LIMIT 1", (brand,))
    if not q:
        return None
    series = db.kv_get(f"quote_series:{q['ticker']}") or {}
    closes = series.get("closes") or []
    month = closes[-22] if len(closes) >= 22 else (closes[0] if closes else None)
    rng = (q["high52"] or 0) - (q["low52"] or 0)
    return {**q, "change_30d_pct": round(100 * (q["close"] / month - 1), 1) if month else None,
            "pos52": round(100 * (q["close"] - (q["low52"] or 0)) / rng) if rng else None, "closes": closes[-130:]}


def profile(brand: str) -> dict:
    c = settings.company(brand)
    b = settings.brand(brand)
    rows = db.q("SELECT * FROM financials WHERE brand=? ORDER BY seen_at", (brand,))
    me = settings.self_brand.name if settings.self_brand else ""
    peb = (settings.market.get("pebble") or {}) if settings.market else {}
    if brand == me and not any(r["metric"] == "revenue" for r in rows) and peb.get("revenue_cr"):
        # Pebble's own figure lives in market.yaml (it drives the impact model); show it here with its confidence
        rows.append({"brand": brand, "metric": "revenue", "period": str(peb.get("revenue_period") or ""), "value": float(peb["revenue_cr"]),
                     "currency": "INR", "unit": "crore", "source": "config/market.yaml (pebble.revenue_cr)", "url": "",
                     "confidence": peb.get("revenue_confidence") or "estimate", "seen_at": db.now()})
    by_metric = defaultdict(list)
    for r in rows:
        by_metric[r["metric"]].append(r)
    rev = _best(by_metric["revenue"])
    rev_series = _series(by_metric["revenue"])
    growth = None
    if rev and len(rev_series) >= 2:
        cur = next((s for s in rev_series if s["period"] == rev["period"]), None)
        prev = [s for s in rev_series if cur and s["key"] < cur["key"]]
        if cur and prev and prev[-1]["value"] and prev[-1]["currency"] == cur["currency"]:
            growth = round(100 * (cur["value"] / prev[-1]["value"] - 1), 1)
    profit_rows = by_metric["net_profit"] + by_metric["net_loss"]
    prof = _best(profit_rows)
    profit = None
    if prof:
        signed = prof["value"] if prof["metric"] == "net_profit" else -prof["value"]
        profit = {**prof, "signed": signed}
        if rev and rev["period"] == prof["period"] and rev["value"] and rev["currency"] == prof["currency"]:
            profit["margin_pct"] = round(100 * signed / rev["value"], 1)
    val = _best(by_metric["valuation"])
    fund_total = _best(by_metric["funding_total"])
    fund_round = _best(by_metric["funding_round"])
    q = quote_of(brand)
    rev_inr = to_inr_cr(rev["value"], rev["currency"], rev["unit"]) if rev else None
    # scale against Pebble: the wearables segment of a conglomerate when it is on file, never the whole group
    seg_rev = _best(by_metric["segment_revenue"])
    scope = c.get("scope") or ("company" if c else None)
    scale, scale_basis = None, None
    if brand != me and peb.get("revenue_cr"):
        if seg_rev:
            scale = round(to_inr_cr(seg_rev["value"], seg_rev["currency"], seg_rev["unit"]) / float(peb["revenue_cr"]), 1)
            scale_basis = "segment"
        elif rev_inr and scope != "parent":
            scale = round(rev_inr / float(peb["revenue_cr"]), 1)
            scale_basis = "company"
        elif rev_inr:
            scale_basis = "parent"
    money_events = db.q("SELECT id, created_at, kind, title, url, importance FROM events WHERE brand=? AND kind IN "
                        "('financial_fact','financial_result','stock_move') ORDER BY id DESC LIMIT 12", (brand,))
    news_fin = db.q("SELECT id, created_at, title, url, data FROM events WHERE brand=? AND source='news' AND (data LIKE '%\"topic\": \"financials\"%' "
                    "OR data LIKE '%\"topic\": \"funding\"%') ORDER BY id DESC LIMIT 10", (brand,))
    for r in news_fin:
        r["publisher"] = json.loads(r.pop("data") or "{}").get("publisher", "")
    return {"brand": brand, "self": brand == me, "entity": c.get("entity"), "hq": c.get("hq"), "founded": c.get("founded"),
            "ownership": c.get("ownership", "private" if c else None), "ticker": c.get("ticker"), "investors": c.get("investors") or [],
            "notes": c.get("notes"), "origin": b.origin if b else None, "tier": b.tier if b else None,
            "revenue": rev, "revenue_inr_cr": round(rev_inr) if rev_inr else None, "revenue_growth_pct": growth, "revenue_series": rev_series,
            "profit": profit, "profit_series": _series([dict(r, value=(r["value"] if r["metric"] == "net_profit" else -r["value"])) for r in profit_rows]),
            "valuation": val, "funding_total": fund_total, "funding_round": fund_round, "quote": q, "scale_vs_pebble": scale,
            "scale_basis": scale_basis, "scope": scope, "segment_revenue": seg_rev, "links": links_of(c),
            "rows": rows, "money_events": money_events, "news": news_fin,
            "sources": sorted({r["source"] for r in rows})[:6], "confidence": max((RANK.get(r["confidence"], 0) for r in rows), default=0)}


def peers() -> dict:
    """Every brand's profile in one table, Pebble first, then by revenue scale."""
    out = [profile(b.name) for b in settings.brands]
    for p in out:
        p.pop("rows", None)
        p.pop("money_events", None)
        p.pop("news", None)
        if p.get("quote"):
            p["quote"] = {k: v for k, v in p["quote"].items() if k != "closes"}
    out.sort(key=lambda p: (not p["self"], -(p["revenue_inr_cr"] or 0), p["brand"]))
    listed = [p for p in out if p.get("quote")]
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    recent = db.q("SELECT id, created_at, brand, kind, title, url, importance, data FROM events WHERE kind IN ('financial_fact','financial_result','stock_move') "
                  "AND created_at>=? ORDER BY importance DESC, id DESC LIMIT 20", (since,))
    for r in recent:
        r["data"] = json.loads(r["data"] or "{}")
    timeline = db.q("SELECT brand, metric, period, value, currency, unit, source, url, confidence FROM financials "
                    "WHERE metric IN ('funding_round','valuation','funding_total') ORDER BY seen_at")
    best: dict[tuple, dict] = {}
    for t in timeline:  # one row per brand, metric and period: the most trustworthy source, then the latest
        k = (t["brand"], t["metric"], str(t["period"]))
        if k not in best or RANK.get(t["confidence"], 0) >= RANK.get(best[k]["confidence"], 0):
            best[k] = t
    timeline = list(best.values())
    for t in timeline:
        t["key"] = period_key(str(t["period"]))
        t["label"] = fmt_money(t["value"], t["currency"], t["unit"])
    timeline.sort(key=lambda t: (-t["key"], t["brand"]))
    return {"brands": out, "listed": listed, "recent": recent, "fx": FX_INR, "funding_timeline": timeline[:40],
            "last_run": db.kv_get("collect:financials") or {},
            "stats": {"with_revenue": sum(1 for p in out if p["revenue"]), "listed": len(listed),
                      "filings": sum(1 for p in out if p["revenue"] and p["revenue"]["confidence"] == "filing"),
                      "facts": db.q1("SELECT COUNT(*) c FROM financials")["c"]}}
