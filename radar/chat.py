"""Ask the Radar: natural-language questions over everything collected.

With Claude or Gemini: a tool-use loop where the model queries the database itself (events, products, price history,
benchmark, white-space, winners). Without a key: a keyword search over events so the box always answers.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from . import db, analytics
from .config import settings
from .enrich import _client_or_none

TOOLS = [
    {"name": "search_events", "description": "Search radar events (launches, price changes, news, social, page changes). "
                                             "Returns newest first.", "strict": True,
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords; empty for all"},
         "brand": {"type": "string", "description": "brand name or empty"},
         "kind": {"type": "string", "description": "event kind such as new_product, price_drop, news, or empty"},
         "days": {"type": "integer", "description": "look-back window in days"},
         "limit": {"type": "integer"}}, "required": ["query", "brand", "kind", "days", "limit"], "additionalProperties": False}},
    {"name": "search_products", "description": "Search the live catalogue of all tracked stores (title, type, price, stock).", "strict": True,
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "brand": {"type": "string"},
                                                       "max_price": {"type": "number"}, "limit": {"type": "integer"}},
                      "required": ["query", "brand", "max_price", "limit"], "additionalProperties": False}},
    {"name": "price_history", "description": "Price and stock history for one product id of a brand.", "strict": True,
     "input_schema": {"type": "object", "properties": {"brand": {"type": "string"}, "product_id": {"type": "integer"}},
                      "required": ["brand", "product_id"], "additionalProperties": False}},
    {"name": "benchmark", "description": "Pebble vs competitors per category: median price, SKUs, stock, discounts.", "strict": True,
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "whitespace", "description": "Category x price-band map showing where competitors have SKUs and Pebble has none.", "strict": True,
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "winners", "description": "Competitor products that appear to be selling best right now.", "strict": True,
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"], "additionalProperties": False}},
    {"name": "signals", "description": "Cross-event signals: price wars, stock-out opportunities, launch predictions, momentum, undercuts.", "strict": True,
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"], "additionalProperties": False}},
    {"name": "matches", "description": "Each Pebble SKU with its nearest rival SKUs and the price gap (price-position index).", "strict": True,
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "influencers", "description": "YouTube creators covering the sector: videos, views, which brands they cover.", "strict": True,
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"], "additionalProperties": False}},
    {"name": "pain_points", "description": "Complaint themes mined from rivals' 1-2 star reviews, with example review text.", "strict": True,
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "search_demand", "description": "Google autocomplete suggestions per seed query (what buyers are typing) and brand share.", "strict": True,
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
    {"name": "app_health", "description": "Rivals' companion apps on Google Play: rating, rating count, installs, last update.", "strict": True,
     "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
]


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat(timespec="seconds")


def run_tool(name: str, a: dict):
    if name == "search_events":
        sql, p = "SELECT id,created_at,brand,kind,title,importance,so_what,counter_move,url FROM events WHERE created_at>=? AND kind<>'baseline'", [_since(a.get("days") or 30)]
        if a.get("brand"):
            sql += " AND brand LIKE ?"; p.append(f"%{a['brand']}%")
        if a.get("kind"):
            sql += " AND kind=?"; p.append(a["kind"])
        for w in re.findall(r"\w+", a.get("query") or "")[:6]:
            sql += " AND (title LIKE ? OR detail LIKE ?)"; p += [f"%{w}%", f"%{w}%"]
        sql += " ORDER BY id DESC LIMIT ?"; p.append(min(a.get("limit") or 30, 100))
        return db.q(sql, p)
    if name == "search_products":
        sql, p = "SELECT brand,product_id,title,product_type,price,compare_at,available,handle FROM products WHERE 1=1", []
        if a.get("brand"):
            sql += " AND brand LIKE ?"; p.append(f"%{a['brand']}%")
        if a.get("max_price"):
            sql += " AND price<=?"; p.append(a["max_price"])
        for w in re.findall(r"\w+", a.get("query") or "")[:6]:
            sql += " AND (title LIKE ? OR product_type LIKE ? OR tags LIKE ?)"; p += [f"%{w}%"] * 3
        sql += " ORDER BY price LIMIT ?"; p.append(min(a.get("limit") or 30, 100))
        return db.q(sql, p)
    if name == "price_history":
        return db.q("SELECT price,compare_at,available,seen_at FROM price_history WHERE brand=? AND product_id=? ORDER BY seen_at",
                    (a["brand"], a["product_id"]))
    if name == "benchmark":
        return analytics.benchmark()
    if name == "whitespace":
        ws = analytics.whitespace()
        return {"gaps": [k for k, v in ws["cells"].items() if v["gap"]], "cells": ws["cells"]}
    if name == "winners":
        return analytics.winners(a.get("limit") or 20)
    if name == "signals":
        return db.q("SELECT created_at,kind,brand,title,detail,importance FROM signals WHERE created_at>=? ORDER BY id DESC", (_since(a.get("days") or 30),))
    if name == "matches":
        from . import matcher
        return matcher.matches()
    if name == "influencers":
        return analytics.influencers(a.get("limit") or 20)
    if name == "pain_points":
        return analytics.review_pain_points()
    if name == "search_demand":
        return analytics.search_demand()
    if name == "app_health":
        return analytics.app_health()
    return {"error": f"unknown tool {name}"}


SYSTEM = (f"You are 'Ask the Radar', the analyst for Pebble (pebblecart.com) in the sector: {settings.sector}. "
          "Answer questions using the tools over the radar's own database. Always call at least one tool before answering. "
          "Be concrete: name brands, products, prices in INR, dates. Keep answers under 200 words unless asked for a report. "
          "End with one recommended move for Pebble when relevant. Today is " + datetime.now().strftime("%d %b %Y") + ".")


def ask(question: str, history: list[dict] | None = None) -> dict:
    if settings.ai_provider == "gemini":
        from . import gemini
        answer = gemini.chat(SYSTEM, TOOLS, question, history, run_tool)
        return {"answer": answer if answer is not None else "The model declined this question.", "mode": "ai"}
    client = _client_or_none()
    if client is None:
        return {"answer": _fallback(question), "mode": "keyword"}
    messages = list(history or []) + [{"role": "user", "content": question}]
    try:
        for _ in range(8):
            resp = client.messages.create(model=settings.model, max_tokens=4000, system=SYSTEM, tools=TOOLS,
                                          messages=messages, output_config={"effort": "medium"})
            if resp.stop_reason == "refusal":
                return {"answer": "The model declined this question.", "mode": "ai"}
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                break
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        out = run_tool(block.name, block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out, default=str)[:60000]})
                    except Exception as e:  # noqa: BLE001
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True})
            messages.append({"role": "user", "content": results})
    except Exception as e:  # noqa: BLE001 - same contract as the Gemini path: answer with the failure, never 500
        return {"answer": f"Claude call failed: {str(e)[:300]}", "mode": "ai"}
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {"answer": text, "mode": "ai"}


def _fallback(question: str) -> str:
    q = question.lower()
    days = 30
    m = re.search(r"(\d+)\s*day", q)
    if m:
        days = int(m.group(1))
    brand = next((b.name for b in settings.brands if b.name.lower() in q), "")
    kind = ""
    for k, rx in (("new_product", r"launch|new product"), ("price_drop", r"price (drop|cut)|cheaper|discount"),
                  ("stock_out", r"out of stock|stockout"), ("news", r"news|press"), ("hiring", r"hir|job")):
        if re.search(rx, q):
            kind = k
            break
    words = " ".join(w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 3 and w not in ("what", "which", "last", "days", "week", "about", "have", "they", "their", "with", "from", "this", "that", brand.lower()))[:60]
    rows = run_tool("search_events", {"query": words if not kind else "", "brand": brand, "kind": kind, "days": days, "limit": 15})
    head = "Top matches"
    if not rows and words:
        rows = run_tool("search_events", {"query": "", "brand": brand, "kind": kind, "days": days, "limit": 15})
        head = f"Nothing mentions \"{words}\"; latest {'events for ' + brand if brand else 'events'} instead"
    if not rows:
        return "Nothing matching in the radar yet. (Add GEMINI_API_KEY or ANTHROPIC_API_KEY to .env for full natural-language answers.)"
    lines = [f"{head} (keyword mode; add GEMINI_API_KEY or ANTHROPIC_API_KEY for AI answers):"]
    for r in rows:
        lines.append(f"- {r['created_at'][:10]} [{r['importance'] or '-'}] {r['brand']} {r['kind']}: {r['title'][:110]}")
    return "\n".join(lines)
