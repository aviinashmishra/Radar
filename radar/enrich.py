"""AI enrichment: category, importance 1-10, one-line "so what", and a concrete counter-move for Pebble.

With ANTHROPIC_API_KEY (or an `ant auth login` profile) events go to Claude, or with GEMINI_API_KEY to Gemini,
in batches with a strict JSON schema. Without a key a keyword heuristic scores them, so the pipeline is always live.
"""
from __future__ import annotations
import json
import logging
import re
from . import db
from .config import settings

log = logging.getLogger("radar.enrich")

CATEGORIES = ["product_launch", "pricing", "promotion", "stock", "marketing", "hiring", "funding", "partnership",
              "regulatory", "reviews_buzz", "new_entrant", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    # range lives in the description: structured outputs reject minimum/maximum, so it is clamped on save
                    "importance": {"type": "integer", "description": "1-10"},
                    "so_what": {"type": "string"},
                    "counter_move": {"type": "string"},
                },
                "required": ["id", "category", "importance", "so_what", "counter_move"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM = f"""You are the competitive-intelligence analyst for Pebble (pebblecart.com), an Indian D2C brand selling
smartwatches, TWS earbuds, speakers and chargers. Sector: {settings.sector}.
You receive raw events detected by an automated radar watching competitors' stores, news, social and pages.
For each event return:
- category: one of {", ".join(CATEGORIES)}
- importance 1-10 for Pebble's leadership. 9-10 = act today (major launch by a top rival, price war on a core
  category, funding/acquisition). 6-8 = worth a look this week. 3-5 = background. 1-2 = noise (minor SKU tweak,
  accessory, generic news, duplicate).
- so_what: one sentence, specific, says what it means for Pebble. No fluff.
- counter_move: one concrete action Pebble could test this week (price, bundle, ad angle, content, channel,
  product spec). Mention actual numbers when the event has them. If nothing to do, say "No action".
Weight: smartwatch and earbud events matter most. Pebble's own events (brand=Pebble) are self-benchmarks: score
them as information (3-5) and in counter_move say what Pebble should check about itself.
Sector/NewEntrant events: score high only when a real new brand or category shift is visible."""

# ---------------- heuristic fallback ----------------
_KIND_BASE = {"new_product": 6, "sale_started": 8, "price_drop": 5, "price_rise": 3, "discount_started": 4,
              "discount_ended": 3, "stock_out": 4, "restock": 3, "delisted": 3, "new_collection": 5,
              "bestseller_move": 5, "hidden_product": 6, "news": 4, "social": 3, "blog": 3,
              "homepage_change": 5, "page_change": 4, "hiring": 5, "new_ad": 4, "new_domain": 3, "baseline": 2,
              "renamed": 2, "app_update": 4, "app_rating_change": 5, "app_growth": 5, "video": 4, "search_trend": 5,
              "stack_change": 5, "theme_change": 5, "review_surge": 6, "complaint": 4, "visual_change": 5,
              "watch_price_change": 6, "watch_status_change": 6, "watch_page_change": 3, "store_unreachable": 6}
_KIND_CAT = {"new_product": "product_launch", "hidden_product": "product_launch", "sale_started": "promotion",
             "price_drop": "pricing", "price_rise": "pricing", "discount_started": "promotion", "discount_ended": "promotion",
             "stock_out": "stock", "restock": "stock", "delisted": "product_launch", "new_collection": "promotion",
             "bestseller_move": "reviews_buzz", "news": "other", "social": "reviews_buzz", "video": "marketing",
             "blog": "marketing", "homepage_change": "marketing", "page_change": "other", "hiring": "hiring",
             "new_ad": "marketing", "new_domain": "new_entrant", "baseline": "other", "renamed": "other",
             "app_update": "product_launch", "app_rating_change": "reviews_buzz", "app_growth": "reviews_buzz",
             "search_trend": "reviews_buzz", "stack_change": "marketing", "theme_change": "marketing", "review_surge": "reviews_buzz",
             "complaint": "reviews_buzz", "visual_change": "marketing",
             "watch_price_change": "pricing", "watch_status_change": "stock", "watch_page_change": "marketing",
             "store_unreachable": "other"}
_HOT = re.compile(r"launch|unveil|introduc|debut|new .*(watch|earbud|tws|speaker)|funding|raises|acqui|\bipo\b|partner|"
                  r"price cut|slash|\bsale\b|record|market share|\bidc\b|counterpoint|lawsuit|\bban(ned)?\b|recall", re.I)
_CORE = re.compile(r"watch|earbud|tws|buds|band|ring", re.I)
_NOISE = re.compile(r"cable|strap|case|cover|sticker|screen guard|adapter|gift card|refurbished|prebook pass|warranty|"
                    r"protection plan|\bups\b|inflator|keyboard|mouse|dashcam|projector|soundbar|trimmer", re.I)


def heuristic(ev: dict) -> dict:
    kind, title = ev["kind"], ev["title"]
    score = _KIND_BASE.get(kind, 4)
    text = f"{title} {ev.get('detail', '')}"
    if _HOT.search(text):
        score += 2
    if _CORE.search(text):
        score += 1
    if _NOISE.search(text):
        score -= 2
    if ev["brand"] in ("Sector", "NewEntrant"):
        score -= 1
    if ev["brand"] == (settings.self_brand.name if settings.self_brand else ""):
        score = min(score, 5)
    d = ev.get("data") or {}
    if kind in ("price_drop", "watch_price_change") and abs(d.get("pct", 0)) >= 20:
        score += 2
    if kind.startswith("watch_") and d.get("segment") in ("band", "ring"):
        score += 1  # Pebble's core segments
    if kind == "stock_out" and d.get("bestseller_rank"):
        score += 2
    if title.startswith("RECENT LAUNCH"):
        score = min(score, 5)  # first-sweep backfill: informative, not urgent (stays under the default alert threshold of 6)
    if kind in ("new_product", "hidden_product") and d.get("prelaunch"):
        score += 2
    if kind == "video" and (d.get("views") or 0) >= 100_000:
        score += 2
    if kind == "app_rating_change" and "fell" in title:
        score += 1
    cat = _KIND_CAT.get(kind, "other")
    if kind == "news":
        low = text.lower()
        for c, rx in (("funding", r"fund|raise|invest|ipo|valuation"), ("product_launch", r"launch|unveil|introduc|debut"),
                      ("partnership", r"partner|tie-up|collab"), ("pricing", r"price|discount|cheap"),
                      ("hiring", r"hire|appoint|ceo|cfo|joins"), ("regulatory", r"bis|govt|regulat|ban|recall")):
            if re.search(rx, low):
                cat = c
                break
    score = max(1, min(10, score))
    moves = {
        "price_drop": "Check Pebble's price on the nearest equivalent SKU and decide: match, bundle, or hold and message value.",
        "sale_started": "Mirror with a time-boxed offer on your top 5 SKUs and bid on their brand + 'sale' keywords.",
        "new_product": "Compare spec sheet and price against Pebble's closest model; brief marketing on the delta.",
        "hidden_product": "Open the URL, capture specs/price before launch, and prepare a comparison page.",
        "stock_out": "Run search and social ads on that product's keywords while they cannot fulfil demand.",
        "bestseller_move": "Study why it sells (price point, feature, creative) and test the same angle.",
        "hiring": "Read the role: it tells you which category or channel they are investing in next.",
        "homepage_change": "Look at the new hero/offer and decide if Pebble's homepage message still wins.",
        "new_collection": "A new collection is usually a campaign. Check the offer mechanics and timing.",
        "new_domain": "Visit the site; if it is a real brand in our category, add it to competitors.yaml.",
        "search_trend": "Demand is forming for this query. Check Pebble ranks for it; if not, publish a comparison page and bid on it.",
        "video": "A creator just covered them. Check the channel size; if it is a fit, send them a Pebble unit this week.",
        "complaint": "Turn the recurring complaint into a Pebble ad angle and a product-spec checklist item.",
        "review_surge": "This SKU is selling. Benchmark Pebble's equivalent on price and the top-3 spec claims.",
        "app_rating_change": "If falling: run 'app that just works' messaging; if rising: study what they fixed.",
        "stack_change": "Look at what the new app does (checkout, BNPL, reviews) and decide if Pebble needs the same.",
        "visual_change": "Open the before/after screenshots on the dashboard and read the new offer.",
        "watch_price_change": "A watchlist rival moved price. Check the segment ladder and decide whether Pebble's position still holds.",
        "watch_status_change": "Availability changed on a watchlist product. Sold out = demand to capture; back in stock = pressure returns.",
        "store_unreachable": "Open their new site: a replatform often comes with a relaunch or new pricing. Update the store URL in competitors.yaml.",
    }
    detail = (ev.get("detail") or "").strip()
    so_what = {
        "hidden_product": detail[:200] if d.get("prelaunch") else "Unlisted page: either a launch being prepared or a test SKU. Low cost to check.",
        "new_product": f"New SKU in {d.get('product_type') or 'their range'}" + (f" at {settings.currency} {d['price']:,.0f}" if d.get("price") else "") + "; compare against Pebble's nearest model.",
        "price_drop": f"{abs(d.get('pct', 0)):.0f}% cut on a {d.get('product_type') or 'product'}; the field may follow." if d else "Price cut.",
        "sale_started": "Broad discounting started; expect ad pressure and marketplace price matching this week.",
        "stock_out": "They cannot fulfil this demand right now." + (" It is a best-seller." if d.get("bestseller_rank") else ""),
        "bestseller_move": "Rank in the store's best-seller collection reflects sales order.",
        "news": "Press coverage; read the article for launch, pricing or funding detail.",
        "video": f"Creator coverage ({(d.get('views') or 0):,} views).",
        "search_trend": "A new query is forming in Google autocomplete.",
        "complaint": "A verified buyer's complaint on a rival best-seller.",
    }.get(kind, detail[:160] if detail else f"{kind.replace('_', ' ').capitalize()} at {ev['brand']}.")
    return {"category": cat, "importance": score,
            "so_what": so_what,
            "counter_move": moves.get(kind, "No action unless it repeats.")}


# ---------------- Claude / Gemini path ----------------
_client = None


def _client_or_none():
    """Anthropic client when Claude is the active provider; None otherwise (Gemini goes through radar.gemini)."""
    global _client
    if settings.ai_provider != "claude":
        return None
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=120.0, max_retries=2)
    return _client


def _llm_batch(events: list[dict]) -> dict[int, dict] | None:
    client = _client_or_none()
    if client is None and settings.ai_provider != "gemini":
        return None
    payload = [{"id": e["id"], "brand": e["brand"], "source": e["source"], "kind": e["kind"], "title": e["title"],
                "detail": (e.get("detail") or "")[:400], "data": {k: v for k, v in (e.get("data") or {}).items()
                                                                  if k in ("old", "new", "pct", "price", "mrp", "rank", "prev",
                                                                           "bestseller_rank", "product_type", "prelaunch")}}
               for e in events]
    prompt = "Classify these events. Return one item per id.\n\n" + json.dumps(payload)
    if client is None:
        from . import gemini
        items = (gemini.json_call(SYSTEM, prompt, SCHEMA) or {}).get("items")
        return {it["id"]: it for it in items} if items else None
    kwargs = dict(
        model=settings.model, max_tokens=8000, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"},
    )
    try:
        try:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except TypeError:  # older SDK without fallbacks support
            resp = client.messages.create(**kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("Claude enrichment failed, falling back to heuristic: %s", e)
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        items = json.loads(text)["items"]
    except (ValueError, KeyError):
        return None
    return {it["id"]: it for it in items}


def enrich_pending(limit: int = 200) -> int:
    rows = db.q("SELECT * FROM events WHERE enriched=0 ORDER BY id LIMIT ?", (limit,))
    if not rows:
        return 0
    for r in rows:
        r["data"] = json.loads(r.get("data") or "{}")
    done = 0
    for i in range(0, len(rows), 25):
        batch = rows[i:i + 25]
        llm = _llm_batch(batch)
        with db.tx() as c:
            for ev in batch:
                res = (llm or {}).get(ev["id"]) or heuristic(ev)
                c.execute("UPDATE events SET category=?, importance=?, so_what=?, counter_move=?, enriched=? WHERE id=?",
                          (res["category"], max(1, min(10, int(res["importance"]))), res["so_what"][:600], res["counter_move"][:600],
                           2 if llm and ev["id"] in llm else 1, ev["id"]))
                done += 1
    return done


def ask_text(prompt: str, system: str = SYSTEM, max_tokens: int = 4000) -> str | None:
    """Plain text completion used by the brief writer. None when AI is off."""
    if settings.ai_provider == "gemini":
        from . import gemini
        return gemini.text(prompt, system, max_tokens)
    client = _client_or_none()
    if client is None:
        return None
    try:
        with client.messages.stream(model=settings.model, max_tokens=max_tokens, system=system,
                                    messages=[{"role": "user", "content": prompt}],
                                    output_config={"effort": "medium"}) as s:
            msg = s.get_final_message()
        if msg.stop_reason == "refusal":
            return None
        return "".join(b.text for b in msg.content if b.type == "text")
    except Exception as e:  # noqa: BLE001
        log.warning("Claude text call failed: %s", e)
        return None
