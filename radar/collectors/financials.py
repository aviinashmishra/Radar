"""Money sensor: what the companies behind the brands earn, raise and are worth.

Three free sources, all configured in config/companies.yaml:
  quotes     Yahoo Finance daily chart for listed parents (Titan, Garmin, Zepp Health, Alphabet, Samsung, Palred):
             close, 1-day move, 52-week range. A move of 5% or more becomes a `stock_move` event.
  EDGAR      SEC XBRL company facts for US-listed parents: revenue, net income, gross and operating profit per fiscal
             year, straight from the 10-K / 20-F. A newly filed year becomes a `financial_result` event.
  news       Google News + Bing searches for revenue / profit / loss / funding / valuation per brand, then a fact
             extractor (regex; Claude or Gemini when a key is set) turns "₹3,122 crore revenue in FY24" into a row of
             the financials table with the publisher as source. A new revenue, profit, loss, funding or valuation
             figure becomes a `financial_fact` event.
Seeded rows from companies.yaml are loaded on every run (INSERT OR IGNORE), so the dashboard is never empty.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta, timezone
from .. import db
from ..config import settings
from .base import get, tracked, log
from . import news as newsmod

YAHOO = "https://query2.finance.yahoo.com/v8/finance/chart/{t}?range=1y&interval=1d"
EDGAR = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
EDGAR_TAGS = {"revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue"],
              "net_profit": ["NetIncomeLoss", "ProfitLoss"], "gross_profit": ["GrossProfit"], "operating_profit": ["OperatingIncomeLoss"]}
MOVE_PCT = 5.0
NEWS_LOOKBACK_DAYS = 45
MONEY_METRICS = ("revenue", "net_profit", "net_loss", "ebitda", "valuation", "funding_round", "funding_total")
SEC_UA = "Pebble Radar research tool {contact}"


def _ua() -> str:
    # EDGAR's fair-access rule: declare the tool and a contact. SEC_CONTACT in .env; a placeholder keeps it working.
    return SEC_UA.format(contact=settings.sec_contact or "radar@example.com")


# ---------------------------------------------------------------- seeds
def load_seeds() -> int:
    n = 0
    for brand, c in settings.companies.items():
        for f in c.get("financials") or []:
            if not f.get("metric") or f.get("value") is None:
                continue
            with db.tx() as cx:
                cur = cx.execute("INSERT OR IGNORE INTO financials(brand,metric,period,value,currency,unit,source,url,confidence,seen_at) "
                                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (brand, f["metric"], str(f.get("period", "")), float(f["value"]), f.get("currency", "INR"),
                                  f.get("unit", "crore" if f.get("currency", "INR") == "INR" else "million"),
                                  f.get("source", "companies.yaml"), f.get("url", ""), f.get("confidence", "estimate"), db.now()))
                n += cur.rowcount
    return n


# ---------------------------------------------------------------- quotes
def fetch_quote(ticker: str) -> dict | None:
    r = get(YAHOO.format(t=ticker), timeout=30, min_gap=1.0,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
    if r.status_code != 200:
        return None
    try:
        res = r.json()["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    meta = res.get("meta") or {}
    closes = [c for c in ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close", []) if c is not None]
    stamps = res.get("timestamp") or []
    if not closes:
        return None
    # last two daily closes of the series: chartPreviousClose is the close before the whole 1-year range, not yesterday's
    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    day = datetime.fromtimestamp(stamps[-1], tz=timezone.utc).strftime("%Y-%m-%d") if stamps else db.now()[:10]
    return {"ticker": ticker, "day": day, "close": float(last), "prev_close": float(prev),
            "change_pct": round(100 * (last - prev) / prev, 2) if prev else 0.0, "currency": meta.get("currency", ""),
            "high52": meta.get("fiftyTwoWeekHigh") or max(closes), "low52": meta.get("fiftyTwoWeekLow") or min(closes),
            "name": meta.get("longName") or meta.get("shortName") or ticker,
            "closes": closes[-260:], "month_ago": closes[-22] if len(closes) >= 22 else closes[0]}


def run_quotes() -> int:
    n = 0
    for brand, c in settings.companies.items():
        t = c.get("ticker")
        if not t:
            continue
        try:
            q = fetch_quote(t)
        except Exception as e:  # noqa: BLE001
            log.info("quote %s failed: %s", t, e)
            continue
        if not q:
            continue
        with db.tx() as cx:
            cx.execute("INSERT OR REPLACE INTO quotes(brand,ticker,day,close,prev_close,change_pct,currency,high52,low52,market_cap,name,seen_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (brand, t, q["day"], q["close"], q["prev_close"], q["change_pct"], q["currency"],
                                                          q["high52"], q["low52"], None, q["name"], db.now()))
        db.kv_set(f"quote_series:{t}", {"day": q["day"], "closes": q["closes"]})
        url = f"https://finance.yahoo.com/quote/{t}"
        if abs(q["change_pct"]) >= MOVE_PCT:
            direction = "jumped" if q["change_pct"] > 0 else "fell"
            if db.add_event(brand, "finance", "stock_move", f"{q['name']} shares {direction} {abs(q['change_pct']):.1f}% ({t})",
                            detail=f"Close {q['close']:,.2f} {q['currency']} vs {q['prev_close']:,.2f}. 52-week range {q['low52']:,.0f}-{q['high52']:,.0f}.",
                            url=url, data={**{k: v for k, v in q.items() if k != "closes"}, "entity": c.get("entity")},
                            dedupe_key=f"quote|{t}|{q['day']}"):
                n += 1
        elif q["high52"] and q["close"] >= q["high52"] * 0.999:
            if db.add_event(brand, "finance", "stock_move", f"{q['name']} at a 52-week high ({q['close']:,.0f} {q['currency']})",
                            detail="The market is paying up for this parent; expect investment in the category.", url=url,
                            data={**{k: v for k, v in q.items() if k != "closes"}, "entity": c.get("entity")},
                            dedupe_key=f"quote|{t}|52high|{q['day'][:7]}"):
                n += 1
        elif q["low52"] and q["close"] <= q["low52"] * 1.001:
            if db.add_event(brand, "finance", "stock_move", f"{q['name']} at a 52-week low ({q['close']:,.0f} {q['currency']})",
                            detail="Pressure on the parent usually means tighter marketing budgets or deeper discounting to move stock.",
                            url=url, data={**{k: v for k, v in q.items() if k != "closes"}, "entity": c.get("entity")},
                            dedupe_key=f"quote|{t}|52low|{q['day'][:7]}"):
                n += 1
    return n


# ---------------------------------------------------------------- EDGAR
def _annual(units: dict) -> list[dict]:
    """One value per fiscal year end: full-year (10-K / 20-F) rows, latest filing wins."""
    unit = next((u for u in ("USD", "EUR", "CNY") if u in units), None) or next(iter(units), None)
    if not unit:
        return []
    best: dict[str, dict] = {}
    for x in units[unit]:
        if x.get("fp") != "FY" or x.get("form") not in ("10-K", "10-K/A", "20-F", "20-F/A", "40-F") or not x.get("start"):
            continue
        try:
            span = (datetime.fromisoformat(x["end"]) - datetime.fromisoformat(x["start"])).days
        except ValueError:
            continue
        if not 340 <= span <= 380:
            continue
        cur = best.get(x["end"])
        if cur is None or x.get("filed", "") > cur.get("filed", ""):
            best[x["end"]] = dict(x, unit=unit)
    return [best[k] for k in sorted(best)]


def run_edgar() -> int:
    n = 0
    for brand, c in settings.companies.items():
        cik = c.get("sec_cik")
        if not cik:
            continue
        try:
            r = get(EDGAR.format(cik=int(cik)), timeout=60, min_gap=1.0, headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"})
        except Exception as e:  # noqa: BLE001
            log.info("edgar %s failed: %s", brand, e)
            continue
        if r.status_code != 200:
            log.info("edgar %s -> %s (set SEC_CONTACT=you@domain in .env if this is 403)", brand, r.status_code)
            continue
        try:
            facts = r.json().get("facts") or {}
        except ValueError:
            continue
        gaap = {**facts.get("ifrs-full", {}), **facts.get("us-gaap", {})}
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}&type=10-K"
        got: dict[str, dict[str, float]] = {}
        for metric, tags in EDGAR_TAGS.items():
            tag = next((t for t in tags if t in gaap), None)
            if not tag:
                continue
            for row in _annual(gaap[tag]["units"])[-6:]:
                period = f"FY{row['end'][:4]}"
                val = row["val"] / 1e6
                m = metric
                if metric == "net_profit" and val < 0:
                    m, val = "net_loss", -val
                got.setdefault(period, {})[m] = val
                with db.tx() as cx:
                    cx.execute("INSERT OR IGNORE INTO financials(brand,metric,period,value,currency,unit,source,url,confidence,seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (brand, m, period, round(val, 2), row["unit"], "million", f"SEC EDGAR {row.get('form', '10-K')} filed {row.get('filed', '')}", url, "filing", db.now()))
        if not got:
            continue
        latest = sorted(got)[-1]
        prior = sorted(got)[-2] if len(got) > 1 else None
        rev, cur = got[latest].get("revenue"), got[latest].get("net_profit") or -(got[latest].get("net_loss") or 0)
        growth = f" ({100 * (rev / got[prior]['revenue'] - 1):+.0f}% YoY)" if prior and rev and got[prior].get("revenue") else ""
        if rev and db.add_event(brand, "finance", "financial_result",
                                f"{c.get('entity', brand)} {latest}: revenue ${rev / 1000:,.2f}B{growth}, net {'income' if cur >= 0 else 'loss'} ${abs(cur) / 1000:,.2f}B",
                                detail="Annual figures from the SEC filing (XBRL company facts). See the Financials page for the series.",
                                url=url, data={"period": latest, **got[latest], "entity": c.get("entity")}, dedupe_key=f"edgar|{brand}|{latest}"):
            n += 1
    return n


# ---------------------------------------------------------------- money news + fact extraction
_AMT = re.compile(r"(?P<cur>₹|rs\.?|inr|\$|usd|us\$|€|eur)?\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<unit>crore|cr\b|lakh|million|mn\b|billion|bn\b|k\b)?", re.I)
_METRIC = [
    ("net_loss",      re.compile(r"net loss|loss(es)? (of|widen|narrow|at|to)|posted a loss|slipped into (a )?loss|in the red", re.I)),
    ("net_profit",    re.compile(r"net profit|profit after tax|\bpat\b|profit of|turn(s|ed)? profitable|posts? (a )?profit|swung to (a )?profit|net income", re.I)),
    ("ebitda",        re.compile(r"ebitda", re.I)),
    ("valuation",     re.compile(r"valuation|valued at|valuing (it|the company)", re.I)),
    ("funding_round", re.compile(r"raises|raised|secures|bags|funding (round|of)|series [a-f]\b|seed round|pre-?series|investment of", re.I)),
    ("revenue",       re.compile(r"revenue|turnover|topline|top line|operating income|sales of|income from operations|gmv", re.I)),
]
_FY = re.compile(r"\bFY\s?'?(\d{2}|\d{4})(?:[-/](\d{2,4}))?\b", re.I)
_QTR = re.compile(r"\bQ([1-4])\s?(?:FY\s?'?)?(\d{2}|\d{4})\b", re.I)
_CY = re.compile(r"\b(20\d{2})\b")
_SENT = re.compile(r"(?<=[.!?])\s+|\s+\|\s+|\s+-\s+(?=[A-Z])")


def _period(text: str, published: str) -> str:
    if m := _QTR.search(text):
        return f"Q{m.group(1)}FY{m.group(2)[-2:]}"
    if m := _FY.search(text):
        return f"FY{(m.group(2) or m.group(1))[-2:]}"
    if m := _CY.search(text):
        return m.group(1)
    return (published or db.now())[:4]


def _to_row(cur: str, num: float, unit: str) -> tuple[str, str, float] | None:
    """Normalise an amount: INR -> crore, USD/EUR -> million."""
    cur = (cur or "").lower()
    unit = (unit or "").lower().rstrip(".")
    inr = cur in ("₹", "rs", "rs.", "inr") or unit in ("crore", "cr", "lakh")
    if inr:
        mult = {"crore": 1, "cr": 1, "lakh": 0.01, "million": 0.1, "mn": 0.1, "billion": 100, "bn": 100, "k": 0.0001, "": 1e-7}[unit]
        return ("INR", "crore", num * mult)
    if cur in ("$", "usd", "us$", "€", "eur"):
        mult = {"million": 1, "mn": 1, "billion": 1000, "bn": 1000, "k": 0.001, "": 1e-6, "crore": 10, "cr": 10, "lakh": 0.1}[unit]
        return ("USD" if cur != "€" and cur != "eur" else "EUR", "million", num * mult)
    return None


def extract_facts(text: str, published: str = "") -> list[dict]:
    """Regex extraction of (metric, period, value) from a headline/summary. Nearest amount to the metric word wins."""
    out = []
    for sent in _SENT.split(text or ""):
        amounts = []
        for m in _AMT.finditer(sent):
            if not (m.group("cur") or m.group("unit")):
                continue
            try:
                num = float(m.group("num").replace(",", ""))
            except ValueError:
                continue
            row = _to_row(m.group("cur") or "", num, m.group("unit") or "")
            if row:
                amounts.append((m.start(), row))
        if not amounts:
            continue
        for metric, rx in _METRIC:
            mm = rx.search(sent)
            if not mm:
                continue
            pos, (currency, unit, val) = min(amounts, key=lambda a: abs(a[0] - mm.start()))
            if (currency == "INR" and not 0.5 <= val <= 2_000_000) or (currency != "INR" and not 0.1 <= val <= 5_000_000):
                continue
            if metric in ("funding_round", "valuation") and currency == "INR" and val < 1:
                continue
            out.append({"metric": metric, "period": _period(sent, published), "value": round(val, 2), "currency": currency, "unit": unit})
            amounts = [a for a in amounts if a[0] != pos]
            if not amounts:
                break
    return out


AI_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "integer"}, "metric": {"type": "string", "enum": list(MONEY_METRICS) + ["none"]},
    "period": {"type": "string", "description": "FY25, Q2FY26 or a calendar year; empty if unknown"},
    "value": {"type": "number", "description": "INR in crore, USD/EUR in million; 0 if none"},
    "currency": {"type": "string", "enum": ["INR", "USD", "EUR", "none"]}},
    "required": ["id", "metric", "period", "value", "currency"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}
AI_SYSTEM = ("Extract the single most important financial fact from each news headline/summary about an Indian or global consumer "
             "electronics company. Metrics: revenue, net_profit, net_loss, ebitda, valuation, funding_round, funding_total. "
             "Convert INR to crore (1 crore = 10 million; 1 lakh = 0.01 crore) and USD/EUR to million. Fiscal periods as FY25, "
             "Q2FY26 or a calendar year. If there is no concrete figure, metric='none'.")


def _ai_extract(items: list[dict]) -> dict[int, dict] | None:
    """Claude / Gemini extraction over a batch of {id, text, published}; None when AI is off or the call fails."""
    if not settings.ai_enabled:
        return None
    from .. import enrich
    res = enrich.json_call(AI_SYSTEM, "Headlines:\n" + json.dumps(items), AI_SCHEMA)
    if not res or not res.get("items"):
        return None
    return {it["id"]: it for it in res["items"] if it.get("metric") not in (None, "none") and it.get("value")}


def run_money_news() -> int:
    """Per-brand money searches, then fact extraction over every recent news event."""
    n = 0
    terms = settings.money_terms or ["revenue", "funding"]
    for b in settings.brands:
        c = settings.company(b.name)
        names = [b.name]
        ent = (c.get("entity") or "").split("(")[0].strip()
        if ent and ent.lower() != b.name.lower() and len(ent.split()) <= 4:
            names.append(re.sub(r"\b(pvt|private|ltd|limited|inc|corp|corporation|co|oy)\.?\b", "", ent, flags=re.I).strip())
        q = " OR ".join(f'"{x}"' for x in names if x) + " (" + " OR ".join(f'"{t}"' for t in terms[:5]) + ")"
        items = newsmod.fetch_google(q, recency="45d")
        if "bing" in newsmod.PROVIDERS:
            items += newsmod.fetch_bing(f"{names[-1]} revenue OR funding OR valuation OR loss OR profit")
        n += newsmod._ingest(b.name, b.name, items, extra={"money": True})
    return n + extract_from_news()


def extract_from_news(days: int = NEWS_LOOKBACK_DAYS) -> int:
    n = 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.q("SELECT id, brand, title, url, data, created_at FROM events WHERE source='news' AND brand NOT IN ('Sector','NewEntrant') "
                "AND created_at>=? AND (title LIKE '%crore%' OR title LIKE '%₹%' OR title LIKE '%Rs%' OR title LIKE '%$%' OR title LIKE '%million%' "
                "OR title LIKE '%billion%' OR data LIKE '%crore%' OR data LIKE '%million%' OR data LIKE '%billion%') ORDER BY id DESC LIMIT 400", (since,))
    done = set(db.kv_get("fin_extracted") or [])
    todo = [r for r in rows if r["id"] not in done]
    if not todo:
        return 0
    batch_in = []
    for r in todo:
        d = json.loads(r["data"] or "{}")
        r["text"] = f"{r['title']}. {d.get('summary', '')}"
        r["published"] = d.get("published") or r["created_at"]
        r["publisher"] = d.get("publisher") or ""
        batch_in.append({"id": r["id"], "text": r["text"][:500], "published": r["published"][:10]})
    ai = None
    for i in range(0, len(batch_in), 30):
        part = _ai_extract(batch_in[i:i + 30])
        if part:
            ai = {**(ai or {}), **part}
    for r in todo:
        facts = []
        if ai and r["id"] in ai:
            it = ai[r["id"]]
            facts = [{"metric": it["metric"], "period": it.get("period") or r["published"][:4], "value": float(it["value"]),
                      "currency": it["currency"], "unit": "crore" if it["currency"] == "INR" else "million"}]
        else:
            facts = extract_facts(r["text"], r["published"])
        for f in facts[:3]:
            src = r["publisher"] or "news"
            with db.tx() as cx:
                ins = cx.execute("INSERT OR IGNORE INTO financials(brand,metric,period,value,currency,unit,source,url,confidence,seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (r["brand"], f["metric"], f["period"], f["value"], f["currency"], f["unit"], src, r["url"],
                                  "ai" if ai and r["id"] in ai else "extracted", db.now())).rowcount
            if ins and f["metric"] in MONEY_METRICS:
                sym = "$" if f["currency"] == "USD" else "€"
                amt = (f"₹{f['value']:,.0f} cr" if f["currency"] == "INR" else
                       f"{sym}{f['value'] / 1000:,.1f}B" if f["value"] >= 1000 else f"{sym}{f['value']:,.0f}M")
                if db.add_event(r["brand"], "finance", "financial_fact", f"{r['brand']} {f['metric'].replace('_', ' ')} {amt} ({f['period']})",
                                detail=f"From: {r['title'][:160]} ({src})", url=r["url"],
                                data={**f, "event_id": r["id"], "publisher": src}, dedupe_key=f"fin|{r['brand']}|{f['metric']}|{f['period']}"):
                    n += 1
    done |= {r["id"] for r in todo}
    db.kv_set("fin_extracted", sorted(done)[-3000:])
    return n


@tracked("financials")
def run() -> int:
    load_seeds()
    n = 0
    for step in (run_quotes, run_edgar, run_money_news):
        try:
            n += step()
        except Exception as e:  # noqa: BLE001 - one source failing must not hide the others
            log.exception("financials step %s failed: %s", step.__name__, e)
    return n
