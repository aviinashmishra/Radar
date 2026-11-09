"""AI enrichment: category, importance 1-10, one-line "so what", and a concrete counter-move for Pebble.

With ANTHROPIC_API_KEY (or an `ant auth login` profile) events go to Claude, or with GEMINI_API_KEY to Gemini,
in batches with a strict JSON schema. Without a key a keyword heuristic scores them, so the pipeline is always live.
"""
from __future__ import annotations
import json
import logging
import re
from . import db
from .analytics import off_topic
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

def _perspective() -> tuple[str, str]:
    """(who the analyst works for, how to weigh events). Pebble's original framing when Pebble is the self brand; any other
    self brand by name; a neutral desk ("Nazar") when the sector is watched without a seat in it."""
    me = settings.self_brand
    if me:
        site = (me.host or "").replace("www.", "")
        who = f"the competitive-intelligence analyst for {me.name}" + (f" ({site})" if site else "") + f", a company in this sector"
        weight = (f"importance 1-10 for {me.name}'s leadership. {me.name}'s own events (brand={me.name}) are self-benchmarks: score them as "
                  f"information (3-5) and in counter_move say what {me.name} should check about itself.")
        subject = me.name
    else:
        who = "the analyst at Nazar, a neutral market-intelligence desk watching this sector for a reader who competes in it"
        weight = "importance 1-10 for a leadership team competing in this sector (a challenger, not the incumbent)."
        subject = "a company competing in this sector"
    return who, weight, subject


_WHO, _WEIGHT, _SUBJECT = _perspective()
_CORE_SEGS = ", ".join(s.get("name", s.get("key", "")) for s in settings.segments if s.get("show", True)) or "the core segments"
SYSTEM = f"""You are {_WHO}. Sector: {settings.sector}.
You receive raw events detected by an automated radar watching the sector's companies: stores, prices, news, filings, apps, social, pages.
For each event return:
- category: one of {", ".join(CATEGORIES)}
- {_WEIGHT} 9-10 = act today (major launch by a top rival, price war on a core
  category, funding/acquisition, regulation that changes the rules). 6-8 = worth a look this week. 3-5 = background. 1-2 = noise
  (minor SKU tweak, accessory, generic news, duplicate).
- so_what: one sentence, specific, says what it means for {_SUBJECT}. No fluff.
- counter_move: one concrete action {_SUBJECT} could test this week (price, bundle, ad angle, content, channel,
  product spec, partnership). Mention actual numbers when the event has them. If nothing to do, say "No action".
Weight: events in the core segments ({_CORE_SEGS}) matter most.
Sector/NewEntrant events: score high only when a real new company or category shift is visible.
{(settings.vocab or {}).get("noise_note") or "Products and stories outside the sector's segments score 1-3."}
Money events (financial_fact, financial_result, stock_move; news with topic financials/funding): a rival's revenue, loss,
funding round or valuation is strategic context (6-8 for direct rivals, 4-6 for global parents whose group figures
are not comparable). In so_what say what the scale or runway means for pricing power in this sector."""

# ---------------- heuristic fallback ----------------
_KIND_BASE = {"new_product": 6, "sale_started": 8, "price_drop": 5, "price_rise": 3, "discount_started": 4,
              "discount_ended": 3, "stock_out": 4, "restock": 3, "delisted": 3, "new_collection": 5,
              "bestseller_move": 5, "hidden_product": 6, "news": 4, "social": 3, "blog": 3,
              "homepage_change": 5, "page_change": 4, "hiring": 5, "new_ad": 4, "new_domain": 3, "baseline": 2,
              "renamed": 2, "app_update": 4, "app_rating_change": 5, "app_growth": 5, "video": 4, "search_trend": 5,
              "stack_change": 5, "theme_change": 5, "review_surge": 6, "complaint": 4, "visual_change": 5,
              "watch_price_change": 6, "watch_status_change": 6, "watch_page_change": 3, "store_unreachable": 6,
              "stock_move": 5, "financial_result": 6, "financial_fact": 6, "attention_spike": 5, "trending_now": 6, "press": 5,
              # marketplace / quick-commerce / voice sensors (collectors channels, qcommerce, voice)
              "channel_undercut": 7, "channel_price_drop": 5, "channel_price_rise": 3, "channel_listed": 4, "channel_delisted": 4,
              "channel_oos": 4, "channel_back": 3, "channel_rating_drop": 5, "qc_expansion": 6, "voice_wave": 6, "praise_wave": 4,
              # social / web / research layer (collectors social, research)
              "viral_post": 6, "discussion": 5, "short_video": 5, "live_update": 5, "community_growth": 5, "research": 5}
_KIND_CAT = {"viral_post": "marketing", "discussion": "reviews_buzz", "short_video": "marketing", "live_update": "other",
             "community_growth": "marketing", "research": "product_launch",
             "new_product": "product_launch", "hidden_product": "product_launch", "sale_started": "promotion",
             "price_drop": "pricing", "price_rise": "pricing", "discount_started": "promotion", "discount_ended": "promotion",
             "stock_out": "stock", "restock": "stock", "delisted": "product_launch", "new_collection": "promotion",
             "bestseller_move": "reviews_buzz", "news": "other", "social": "reviews_buzz", "video": "marketing",
             "blog": "marketing", "homepage_change": "marketing", "page_change": "other", "hiring": "hiring",
             "new_ad": "marketing", "new_domain": "new_entrant", "baseline": "other", "renamed": "other",
             "app_update": "product_launch", "app_rating_change": "reviews_buzz", "app_growth": "reviews_buzz",
             "search_trend": "reviews_buzz", "stack_change": "marketing", "theme_change": "marketing", "review_surge": "reviews_buzz",
             "complaint": "reviews_buzz", "visual_change": "marketing",
             "watch_price_change": "pricing", "watch_status_change": "stock", "watch_page_change": "marketing",
             "store_unreachable": "other", "stock_move": "funding", "financial_result": "funding", "financial_fact": "funding",
             "attention_spike": "reviews_buzz", "trending_now": "reviews_buzz", "press": "marketing",
             "channel_undercut": "pricing", "channel_price_drop": "pricing", "channel_price_rise": "pricing", "channel_listed": "product_launch",
             "channel_delisted": "stock", "channel_oos": "stock", "channel_back": "stock", "channel_rating_drop": "reviews_buzz",
             "qc_expansion": "partnership", "voice_wave": "reviews_buzz", "praise_wave": "reviews_buzz"}
_TOPIC_CAT = {"financials": "funding", "funding": "funding", "launch": "product_launch", "pricing": "pricing", "legal": "regulatory",
              "leadership": "hiring", "partnership": "partnership", "distribution": "partnership", "manufacturing": "other",
              "marketing": "marketing", "market_data": "other", "reviews": "reviews_buzz"}
_V = settings.vocab or {}
_HOT = re.compile(_V["hot"], re.I) if _V.get("hot") else re.compile(
    r"launch|unveil|introduc|debut|new .*(watch|earbud|tws|speaker)|funding|raises|acqui|\bipo\b|partner|"
    r"price cut|slash|\bsale\b|record|market share|\bidc\b|counterpoint|lawsuit|\bban(ned)?\b|recall", re.I)
_CORE = re.compile(_V["core"], re.I) if _V.get("core") else re.compile(r"watch|earbud|tws|buds|band|ring", re.I)
_NOISE = re.compile(_V["noise"], re.I) if _V.get("noise") else re.compile(
    r"cable|strap|case|cover|sticker|screen guard|adapter|gift card|refurbished|prebook pass|warranty|"
    r"protection plan|\bups\b|inflator|keyboard|mouse|dashcam|projector|soundbar|trimmer", re.I)


_GRIPE = re.compile(r"defect|warranty|refund|broke|worst|scam|complain|dispute|not working|stopped working|replacement", re.I)
_PRODUCT_KINDS = {"new_product", "price_drop", "price_rise", "discount_started", "discount_ended", "stock_out", "restock",
                  "delisted", "bestseller_move", "hidden_product", "renamed"}


def _own_channel(brand: str, channel: str) -> bool:
    key = re.sub(r"[^a-z0-9]", "", brand.lower())
    return bool(key) and key in re.sub(r"[^a-z0-9]", "", channel.lower())


_NEWS_LINES = {
    "launch": ("{b} launched or teased a product: launch coverage is when the spec sheet and the price get fixed in buyers' minds.",
               "Pull the spec and price from the article, place it on the segment ladder against Pebble's nearest SKU and brief marketing on the delta this week."),
    "pricing": ("{b} is moving price or running an offer in the press; the market reads it as the new floor.",
                "Check Pebble's price on the matching SKU (Pricing page) and decide today: match, bundle, or hold and message value."),
    "funding": ("{b} is raising money or being valued: fresh capital turns into ads, discounts and distribution within two quarters.",
                "Note the round on Financials and budget for heavier marketplace discounting and creator spend from them."),
    "financials": ("Results for {b}: growth and margin say how long they can afford a price fight.",
                   "Read the growth and loss figures on Financials; a loss-making rival discounting is burning runway, a profitable one is here to stay."),
    "legal": ("Courts, patents, BIS or customs around {b}: a ban or a recall is a stock-out for the whole category.",
              "Check whether the issue touches Pebble's own SKUs or suppliers; if the rival's product is pulled, bid on its keywords."),
    "leadership": ("A senior hire or exit at {b} shows where they will invest next.",
                   "Read the role: a new head of offline retail or quick commerce means that channel is their next push."),
    "partnership": ("{b} tied up with a partner: co-marketing and bundles reach buyers Pebble does not pay to reach.",
                    "Map the partner's audience; if it overlaps Pebble's core buyer, line up an equivalent partner or a counter-bundle."),
    "distribution": ("{b} is changing where it sells; shelf presence decides impulse purchases under ₹5,000.",
                     "Open Channels for the same city or marketplace and make sure Pebble is listed, priced right and in stock there."),
    "manufacturing": ("{b} is investing in making in India or its supply chain: lower landed cost and PLI benefits turn into price cuts later.",
                      "Note the capacity and the timeline; expect price pressure in that category once the plant ships."),
    "marketing": ("{b} is running a campaign: ad pressure on shared keywords and creators rises for weeks.",
                  "Watch CPCs on shared keywords and answer with Pebble's strongest creative rather than matching their spend."),
    "market_data": ("Tracker numbers (IDC, Counterpoint) ranking {b}: the public scoreboard investors and retailers read.",
                    "Update the shares in config/market.yaml if the article carries fresh figures; use the ranking in Pebble's trade pitch."),
    "reviews": ("A review of {b}: the strengths and weaknesses buyers will quote back.",
                "Read the verdict; put the weak point on Pebble's comparison page and match the praised feature."),
    "general": ("Press coverage of {b}.", "Read it and file as context unless the brand keeps appearing in the news this week."),
}


def _news_lines(ev: dict, d: dict) -> tuple[str, str]:
    """Why it matters and what to do for a news event, by topic (the AI path writes its own lines when a key is set)."""
    why, move = _NEWS_LINES.get(d.get("topic") or "general", _NEWS_LINES["general"])
    name = "A category player" if ev["brand"] in ("Sector", "NewEntrant") else ev["brand"]
    pub = d.get("publisher")
    return why.format(b=name) + (f" ({pub})" if pub and d.get("topic") in (None, "general") else ""), move


def heuristic(ev: dict) -> dict:
    kind, title = ev["kind"], ev["title"]
    score = _KIND_BASE.get(kind, 4)
    text = f"{title} {ev.get('detail', '')}"
    if _HOT.search(text):
        score += 2
    if _CORE.search(text):
        score += 1
    if _NOISE.search(text) and ev.get("source") in ("shopify", "watchlist"):  # cables and warranty plans, not a warranty fight
        score -= 2
    if kind == "social" and _GRIPE.search(text):
        score += 2  # a buyer's public complaint is an ad angle
    if ev["brand"] in ("Sector", "NewEntrant"):
        score -= 1
    if ev["brand"] == (settings.self_brand.name if settings.self_brand else ""):
        score = min(score, 5)
    d = ev.get("data") or {}
    if kind in ("price_drop", "watch_price_change", "channel_price_drop", "channel_undercut") and abs(d.get("pct", 0)) >= 20:
        score += 2
    if kind in ("price_drop", "price_rise", "watch_price_change", "channel_price_drop", "channel_price_rise") and 0 < abs(d.get("pct", 0)) < 5:
        score -= 2  # a 4% trim is housekeeping, not a price move
    if kind == "voice_wave" and d.get("polarity") == "negative" and ev["brand"] != (settings.self_brand.name if settings.self_brand else ""):
        score += 1  # a wave of buyer complaints about a rival is an ad angle; Pebble's own waves stay at the self cap of 5
    if kind.startswith("watch_") and d.get("segment") in ("band", "ring"):
        score += 1  # Pebble's core segments
    if kind == "stock_out" and d.get("bestseller_rank"):
        score += 2
    if title.startswith("RECENT LAUNCH"):
        score = min(score, 5)  # first-sweep backfill: informative, not urgent (stays under the default alert threshold of 6)
    if kind in ("new_product", "hidden_product") and d.get("prelaunch"):
        score += 2
    official = kind == "video" and _own_channel(ev["brand"], d.get("channel") or "")
    if kind == "video" and (d.get("views") or 0) >= 100_000 and not official:
        score += 2
    if kind == "app_rating_change" and "fell" in title:
        score += 1
    if kind == "sale_started":  # scale with depth and breadth, not with the word "SALE" in the title
        depth, count = abs(d.get("avg_pct") or 0), d.get("count") or 0
        score = 5 + (depth >= 15) + (depth >= 30) + (count >= 20) + (count >= 50)
    if ev.get("source") == "shopify" and kind in _PRODUCT_KINDS:
        if off_topic(d.get("product_type") or "", title):
            score = min(score - 2, 3)  # oats, door sensors, projectors: in the feed, never on Today or in alerts
    cat = _KIND_CAT.get(kind, "other")
    if kind in ("news", "press"):
        low = text.lower()
        if d.get("topic") in _TOPIC_CAT:
            cat = _TOPIC_CAT[d["topic"]]
        else:
            for c, rx in (("funding", r"fund|raise|invest|ipo|valuation"), ("product_launch", r"launch|unveil|introduc|debut"),
                          ("partnership", r"partner|tie-up|collab"), ("pricing", r"price|discount|cheap"),
                          ("hiring", r"hire|appoint|ceo|cfo|joins"), ("regulatory", r"bis|govt|regulat|ban|recall")):
                if re.search(rx, low):
                    cat = c
                    break
        if d.get("topic") in ("funding", "financials", "legal") and ev["brand"] not in ("Sector", "NewEntrant"):
            score += 1  # money and courts move faster than marketing
        if d.get("topic") == "reviews" and kind == "news":
            score -= 1
    if kind == "stock_move":
        score = 4 + (abs(d.get("change_pct") or 0) >= 8) + (abs(d.get("change_pct") or 0) >= 12) + ("52-week" in title)
    if kind == "financial_fact":
        score = {"revenue": 6, "funding_round": 7, "valuation": 7, "net_loss": 6, "net_profit": 6}.get(d.get("metric"), 5)
        b = settings.brand(ev["brand"])
        if b and b.tier == "adjacent":
            score -= 1
    if kind == "trending_now":
        score = 7 if ev["brand"] not in ("Sector",) else 5
    score = max(1, min(10, score))
    if official:  # the brand's own channel is marketing, not a creator Pebble could seed
        return {"category": "marketing", "importance": min(score, 6),
                "so_what": f"{ev['brand']} posted on its own channel ({(d.get('views') or 0):,} views): the message they are pushing.",
                "counter_move": "Watch the claims and the offer; answer the strongest one on Pebble's comparison page."}
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
        "stock_move": "A parent under market pressure discounts to clear stock; one being rewarded invests. Read the results call summary on the Financials page.",
        "financial_result": "Compare the growth rate and margin with Pebble's own plan; a rival growing faster at a loss is buying share.",
        "financial_fact": "Add the figure to the board deck: rival scale, burn and funding set the price floor Pebble will meet next quarter.",
        "attention_spike": "Open the brand's News tab to see what caused it, then decide whether Pebble should ride the conversation or stay out.",
        "trending_now": "Bid on the trending term today, publish a comparison page, and post from Pebble's handles while the search volume lasts.",
        "press": "Read the announcement: official newsroom posts precede launches and partnerships by days.",
        "channel_undercut": "A marketplace listing undercuts Pebble's equivalent. Match on the same marketplace or add a bundle; check whether the seller is the brand or a reseller.",
        "channel_price_drop": "Check Pebble's marketplace price on the nearest SKU; marketplace cuts spread to the D2C store within days.",
        "channel_price_rise": "Room to hold or lift Pebble's price on that SKU; watch whether the rise sticks for a week.",
        "channel_listed": "A new marketplace listing: compare specs and price, and read the first reviews for the angle they sell on.",
        "channel_delisted": "A listing vanished: a refresh or a quality problem. Bid on its model name while buyers search for it.",
        "channel_oos": "Out of stock on a marketplace: push Pebble's equivalent with 'in stock, ships today' on the same marketplace.",
        "channel_back": "The rival SKU is back on the shelf; expect its ads to resume. Check Pebble's rank for the category keyword.",
        "channel_rating_drop": "Marketplace rating slid: pull the recent 1-2★ reviews and turn the recurring complaint into a comparison claim.",
        "qc_expansion": "A rival went live on quick commerce in a new city: check Pebble's dark-store coverage there and the delivery-time claim in ads.",
        "voice_wave": "A wave of buyer complaints on one theme: answer it in Pebble's product page copy and creatives this week.",
        "praise_wave": "Buyers praise one feature: that is the spec Pebble has to match or reframe in its comparison page.",
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
        "stock_move": f"The market just repriced {ev['brand']}'s parent" + (f" ({d.get('entity')})" if d.get("entity") else "") + "; budgets and discounting follow the share price.",
        "financial_fact": f"A public {str(d.get('metric', '')).replace('_', ' ')} figure for {ev['brand']} ({d.get('period', '')}): their scale and runway relative to Pebble.",
        "financial_result": "Annual filing figures: the cleanest read on a global rival's growth and margin.",
        "attention_spike": "Public attention jumped; a launch, a campaign or a controversy is under way.",
        "trending_now": "The brand or category is trending nationally on Google right now.",
    }.get(kind, detail[:160] if detail else f"{kind.replace('_', ' ').capitalize()} at {ev['brand']}.")
    counter = moves.get(kind, "No action unless it repeats.")
    if kind == "news":
        so_what, counter = _news_lines(ev, d)
    return {"category": cat, "importance": score, "so_what": so_what, "counter_move": counter}


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


def json_call(system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> dict | None:
    """One structured-output call on the active provider (Claude or Gemini). None when AI is off or the call fails,
    so every caller falls back to its heuristic. Shared by event scoring and the financial-fact extractor."""
    client = _client_or_none()
    if client is None and settings.ai_provider != "gemini":
        return None
    if client is None:
        from . import gemini
        return gemini.json_call(system, prompt, schema, max_tokens)
    kwargs = dict(
        model=settings.model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": schema}, "effort": "low"},
    )
    try:
        try:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except TypeError:  # older SDK without fallbacks support
            resp = client.messages.create(**kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("Claude structured call failed, falling back to heuristic: %s", e)
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text)
    except ValueError:
        return None


def _llm_batch(events: list[dict]) -> dict[int, dict] | None:
    payload = [{"id": e["id"], "brand": e["brand"], "source": e["source"], "kind": e["kind"], "title": e["title"],
                "detail": (e.get("detail") or "")[:400], "data": {k: v for k, v in (e.get("data") or {}).items()
                                                                  if k in ("old", "new", "pct", "price", "mrp", "rank", "prev",
                                                                           "bestseller_rank", "product_type", "prelaunch", "topic",
                                                                           "metric", "period", "value", "currency", "change_pct")}}
               for e in events]
    items = (json_call(SYSTEM, "Classify these events. Return one item per id.\n\n" + json.dumps(payload), SCHEMA) or {}).get("items")
    return {it["id"]: it for it in items} if items else None


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
        from . import market
        with db.tx() as c:
            for ev in batch:
                res = (llm or {}).get(ev["id"]) or heuristic(ev)
                importance = int(res["importance"])
                if ev.get("source") == "shopify" and ev["kind"] in _PRODUCT_KINDS and off_topic((ev.get("data") or {}).get("product_type") or "", ev["title"]):
                    importance = min(importance, 3)  # same guard for the model: off-market SKUs never alert
                # the ₹/month impact estimate (market.yaml model) travels with the event, so alerts and cards can show it
                try:
                    imp = market.impact(ev)
                except Exception:  # noqa: BLE001 - a model misconfiguration must never block scoring
                    imp = None
                data_sql = ""
                params: list = [res["category"], max(1, min(10, importance)), (settings.localize(res["so_what"]) or "")[:600],
                                (settings.localize(res["counter_move"]) or "")[:600],
                                2 if llm and ev["id"] in llm else 1]
                if imp:
                    data_sql = ", data=?"
                    params.append(json.dumps({**(ev.get("data") or {}), "impact": imp}, default=str))
                c.execute(f"UPDATE events SET category=?, importance=?, so_what=?, counter_move=?, enriched=?{data_sql} WHERE id=?", (*params, ev["id"]))
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
