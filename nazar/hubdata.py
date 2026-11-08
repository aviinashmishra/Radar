"""Cross-sector reads. Every function walks the loaded sectors, queries each sector's own SQLite through its package,
tags rows with the sector, and merges. A sector that fails is skipped, never fatal."""
from __future__ import annotations
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from . import engine

log = logging.getLogger("nazar.hub")
NOISE_BRANDS = ("Sector", "NewEntrant")
INDICES = [("^NSEI", "Nifty 50"), ("^BSESN", "Sensex"), ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("USDINR=X", "USD/INR"),
           ("BTC-USD", "Bitcoin"), ("CL=F", "Crude (WTI)"), ("GC=F", "Gold")]


def _data(r: dict) -> dict:
    d = r.get("data")
    if isinstance(d, str):
        try:
            return json.loads(d or "{}")
        except ValueError:
            return {}
    return d or {}


def _tag(sec: engine.Sector, r: dict) -> dict:
    r["sector"] = sec.key
    r["sector_label"] = sec.label
    r["sector_short"] = sec.short
    r["color"] = sec.color
    return r


def _safe(sec: engine.Sector, fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.warning("%s: %s", sec.key, e)
        return default


# ---------------------------------------------------------------- sectors overview
_OVERVIEW: dict = {"rows": None, "at": 0.0, "lock": threading.Lock(), "refreshing": False}
OVERVIEW_TTL = 60.0   # the per-sector summary walks ten databases (~15 s); serve the last one and refresh behind the scenes


def sectors_overview() -> list[dict]:
    """Cached: the first call computes synchronously, later calls answer at once and refresh in the background when stale."""
    rows, age = _OVERVIEW["rows"], time.time() - _OVERVIEW["at"]
    if rows is None:
        with _OVERVIEW["lock"]:
            if _OVERVIEW["rows"] is None:
                _OVERVIEW.update(rows=_sectors_overview(), at=time.time())
        return _OVERVIEW["rows"]
    if age > OVERVIEW_TTL and not _OVERVIEW["refreshing"]:
        _OVERVIEW["refreshing"] = True

        def _bg():
            try:
                _OVERVIEW.update(rows=_sectors_overview(), at=time.time())
            except Exception:  # noqa: BLE001
                log.exception("sector overview refresh failed")
            finally:
                _OVERVIEW["refreshing"] = False
        threading.Thread(target=_bg, daemon=True, name="nazar-overview").start()
    return rows


def _sectors_overview() -> list[dict]:
    out = []
    for sec in engine.sectors():
        def one(sec=sec):
            db = sec.db
            base = "FROM events WHERE enriched>0 AND kind<>'baseline'"
            e24 = db.q1(f"SELECT COUNT(*) c {base} AND created_at>=datetime('now','-1 day')")["c"]
            e7 = db.q1(f"SELECT COUNT(*) c {base} AND created_at>=datetime('now','-7 day')")["c"]
            news7 = db.q1(f"SELECT COUNT(*) c {base} AND kind IN ('news','press') AND created_at>=datetime('now','-7 day')")["c"]
            sig7 = db.q1("SELECT COUNT(*) c FROM signals WHERE created_at>=datetime('now','-7 day')")["c"]
            top = db.q1(f"SELECT id, brand, kind, title, importance, so_what, counter_move, url, created_at {base} AND importance>=5 "
                        f"AND brand NOT IN ('Sector','NewEntrant') AND created_at>=datetime('now','-3 day') ORDER BY importance DESC, id DESC LIMIT 1")
            hot = db.q(f"SELECT brand, COUNT(*) c, MAX(importance) m {base} AND brand NOT IN ('Sector','NewEntrant') AND created_at>=datetime('now','-7 day') "
                       f"GROUP BY brand ORDER BY c DESC LIMIT 3")
            days = {r["d"]: r["c"] for r in db.q(f"SELECT substr(created_at,1,10) d, COUNT(*) c {base} AND created_at>=date('now','-13 day') GROUP BY d")}
            today = datetime.now(timezone.utc).date()
            spark = [days.get((today - timedelta(days=13 - i)).isoformat(), 0) for i in range(14)]
            quotes = db.q1("SELECT COUNT(DISTINCT ticker) c FROM quotes")["c"]
            voice = db.q1("SELECT COUNT(*) c FROM voice")["c"] if db.q1("SELECT name FROM sqlite_master WHERE name='voice'") else 0
            products = db.q1("SELECT COUNT(*) c FROM products")["c"]
            total = db.q1("SELECT COUNT(*) c FROM events")["c"]
            runs = {t: db.kv_get(f"last_run:{t}") for t in ("fast", "medium", "slow", "all")}
            last = max((r["at"] for r in runs.values() if r and r.get("at")), default=None)
            pressure = None
            try:
                mk = sec.sub("market")
                p = mk.pressure(7)
                pressure = {"risk": p.get("total_risk"), "upside": p.get("total_upside")} if sec.settings.self_brand else None
            except Exception:  # noqa: BLE001
                pressure = None
            return {**sec.public(), "events_24h": e24, "events_7d": e7, "news_7d": news7, "signals_7d": sig7, "events_total": total,
                    "top": top, "hot": hot, "spark": spark, "quotes": quotes, "voice": voice, "products": products, "last_run": last,
                    "primed": sec.primed, "pressure": pressure, "db_mb": round(sec.db_path.stat().st_size / 1e6, 1) if sec.db_path.exists() else 0}
        out.append(_safe(sec, one, {**sec.public(), "error": "unavailable", "events_24h": 0, "events_7d": 0, "news_7d": 0, "signals_7d": 0, "spark": [0] * 14, "hot": [], "top": None}))
    return out


# ---------------------------------------------------------------- pulse (cross-sector feed)
def pulse(min_importance: int = 5, limit: int = 120, since: str = "", sector: str = "", kind: str = "", q: str = "", days: int = 7) -> list[dict]:
    rows: list[dict] = []
    for sec in engine.sectors():
        if sector and sec.key != sector:
            continue
        def one(sec=sec):
            sql = ("SELECT id, created_at, brand, source, kind, title, detail, url, importance, so_what, counter_move, category, data FROM events "
                   "WHERE enriched>0 AND kind<>'baseline' AND importance>=? AND created_at>=datetime('now', ?)")
            params: list = [min_importance, f"-{days} day"]
            if since:
                sql += " AND created_at>?"
                params.append(since)
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            if q:
                sql += " AND (title LIKE ? OR brand LIKE ? OR so_what LIKE ?)"
                params += [f"%{q}%"] * 3
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            out = []
            for r in sec.db.q(sql, params):
                d = _data(r)
                r["data"] = {k: d.get(k) for k in ("topic", "publisher", "impact", "pct", "price", "segment") if k in d}
                r["detail"] = (r.get("detail") or "")[:300]
                out.append(_tag(sec, r))
            return out
        rows += _safe(sec, one, [])
    rows.sort(key=lambda r: (r["created_at"], r["importance"]), reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------- market tape
_IDX: dict = {"at": 0.0, "rows": [], "busy": False}


def _refresh_indices() -> None:
    try:
        fin = engine.primary().sub("collectors.financials")
        rows = []
        for t, name in INDICES:
            try:
                q = fin.fetch_quote(t)
            except Exception as e:  # noqa: BLE001
                log.info("index %s failed: %s", t, e)
                q = None
            if q:
                rows.append({"ticker": t, "name": name, "close": q["close"], "prev_close": q["prev_close"], "change_pct": q["change_pct"],
                             "currency": q["currency"], "series": (q.get("closes") or [])[-30:], "day": q["day"]})
        if rows:
            _IDX["rows"] = rows
        _IDX["at"] = time.time()
    finally:
        _IDX["busy"] = False


def indices() -> list[dict]:
    """World indices, rupee, crude, gold, bitcoin: refreshed in the background every 15 minutes."""
    if time.time() - _IDX["at"] > 900 and not _IDX["busy"]:
        _IDX["busy"] = True
        threading.Thread(target=_refresh_indices, daemon=True, name="nazar-indices").start()
    return _IDX["rows"]


def tape() -> dict:
    rows: list[dict] = []
    for sec in engine.sectors():
        def one(sec=sec):
            out = []
            latest = sec.db.q("SELECT q.brand, q.ticker, q.day, q.close, q.prev_close, q.change_pct, q.currency, q.high52, q.low52, q.name, q.seen_at FROM quotes q "
                              "JOIN (SELECT ticker, MAX(day) d FROM quotes GROUP BY ticker) m ON q.ticker=m.ticker AND q.day=m.d ORDER BY q.change_pct DESC")
            for r in latest:
                s = sec.db.kv_get(f"quote_series:{r['ticker']}") or {}
                r["series"] = (s.get("closes") or [])[-30:]
                c = sec.settings.company(r["brand"])
                b = sec.settings.brand(r["brand"])
                r["entity"] = c.get("entity")
                r["origin"] = b.origin if b else ""
                r["pos52"] = round(100 * (r["close"] - r["low52"]) / (r["high52"] - r["low52"]), 0) if r.get("high52") and r.get("low52") and r["high52"] > r["low52"] else None
                out.append(_tag(sec, r))
            return out
        rows += _safe(sec, one, [])
    rows.sort(key=lambda r: r.get("change_pct") or 0, reverse=True)
    return {"quotes": rows, "indices": indices(), "count": len(rows), "updated": max((r["seen_at"] for r in rows), default=None)}


# ---------------------------------------------------------------- universe (every company)
def universe() -> list[dict]:
    out: list[dict] = []
    for sec in engine.sectors():
        def one(sec=sec):
            st, db = sec.settings, sec.db
            seg_names = {g["key"]: g["name"] for g in st.segments}
            e7 = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM events WHERE enriched>0 AND kind<>'baseline' AND created_at>=datetime('now','-7 day') GROUP BY brand")}
            n7 = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM events WHERE kind IN ('news','press') AND created_at>=datetime('now','-7 day') GROUP BY brand")}
            top = {r["brand"]: r for r in db.q("SELECT brand, MAX(importance) m, title FROM events WHERE enriched>0 AND kind<>'baseline' AND created_at>=datetime('now','-7 day') GROUP BY brand")}
            quotes = {r["brand"]: r for r in db.q("SELECT q.brand, q.ticker, q.close, q.change_pct, q.currency, q.day FROM quotes q JOIN (SELECT ticker, MAX(day) d FROM quotes GROUP BY ticker) m ON q.ticker=m.ticker AND q.day=m.d")}
            apps = {r["brand"]: r for r in db.q("SELECT brand, ROUND(AVG(rating),2) rating, SUM(ratings_count) n FROM app_metrics WHERE id IN (SELECT MAX(id) FROM app_metrics GROUP BY package) GROUP BY brand")}
            att = {r["brand"]: r["v"] for r in db.q("SELECT brand, ROUND(AVG(views)) v FROM attention WHERE day>=date('now','-8 day') GROUP BY brand")}
            fin: dict[str, dict] = {}
            for r in db.q("SELECT brand, metric, period, value, currency, unit, confidence FROM financials WHERE metric IN ('revenue','valuation') ORDER BY id"):
                fin.setdefault(r["brand"], {})[r["metric"]] = r
            skus = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM products GROUP BY brand")}
            rows = []
            for b in st.brands:
                c = st.company(b.name)
                rows.append(_tag(sec, {
                    "name": b.name, "self": b.self_brand, "origin": b.origin, "country": b.country or ("India" if b.origin == "Indian" else ""), "tier": b.tier, "kind": b.kind,
                    "segments": [seg_names.get(s, s) for s in b.segments], "website": b.website or b.shopify or "", "store": bool(b.shopify),
                    "entity": c.get("entity"), "hq": c.get("hq"), "founded": c.get("founded"), "ownership": c.get("ownership") or ("listed" if c.get("ticker") else "private"),
                    "ticker": c.get("ticker"), "wikipedia": c.get("wikipedia"), "investors": c.get("investors") or [], "notes": c.get("notes"),
                    "quote": quotes.get(b.name), "events_7d": e7.get(b.name, 0), "news_7d": n7.get(b.name, 0), "top": top.get(b.name), "app": apps.get(b.name),
                    "attention": att.get(b.name), "revenue": (fin.get(b.name) or {}).get("revenue"), "valuation": (fin.get(b.name) or {}).get("valuation"), "skus": skus.get(b.name, 0),
                    "apps": len(b.apps) + len(c.get("ios") or []),
                }))
            return rows
        out += _safe(sec, one, [])
    return out


# ---------------------------------------------------------------- themes (what every sector is talking about)
def themes(days: int = 7) -> dict:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    prev: dict[str, int] = defaultdict(int)
    heads: dict[str, list[dict]] = defaultdict(list)
    brands: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sectors_seen = []
    for sec in engine.sectors():
        sectors_seen.append({"key": sec.key, "label": sec.label, "short": sec.short, "color": sec.color})
        def one(sec=sec):
            rows = sec.db.q("SELECT id, brand, title, url, importance, so_what, created_at, data FROM events WHERE kind IN ('news','press') "
                            "AND created_at>=datetime('now', ?) ORDER BY id DESC LIMIT 4000", (f"-{2 * days} day",))
            cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            for r in rows:
                t = _data(r).get("topic") or "other"
                if r["created_at"] >= cut:
                    counts[t][sec.key] += 1
                    if r["brand"] not in NOISE_BRANDS:
                        brands[t][f"{r['brand']}|{sec.key}"] += 1
                    if (r.get("importance") or 0) >= 5 and len(heads[t]) < 40:
                        heads[t].append(_tag(sec, {k: r[k] for k in ("id", "brand", "title", "url", "importance", "so_what", "created_at")}))
                else:
                    prev[t] += 1
        _safe(sec, one, None)
    out = []
    for t, by in counts.items():
        total = sum(by.values())
        hs = sorted(heads[t], key=lambda h: (h["importance"], h["created_at"]), reverse=True)[:6]
        bs = sorted(brands[t].items(), key=lambda kv: kv[1], reverse=True)[:6]
        out.append({"topic": t, "total": total, "prev": prev.get(t, 0), "change_pct": round(100 * (total - prev.get(t, 0)) / prev[t]) if prev.get(t) else None,
                    "by_sector": dict(by), "headlines": hs, "brands": [{"brand": k.split("|")[0], "sector": k.split("|")[1], "n": n} for k, n in bs]})
    out.sort(key=lambda x: (x["topic"] not in ("general", "other"), x["total"]), reverse=True)  # named topics first, the catch-all last
    return {"days": days, "topics": out, "sectors": sectors_seen}


# ---------------------------------------------------------------- lens (India vs world, listed vs private, attention)
def lens(days: int = 30) -> dict:
    per_sector = []
    attention: list[dict] = []
    countries: dict[str, int] = defaultdict(int)
    ownership: dict[str, int] = defaultdict(int)
    for sec in engine.sectors():
        def one(sec=sec):
            st, db = sec.settings, sec.db
            by = db.q("SELECT brand, COUNT(*) c, SUM(CASE WHEN kind IN ('news','press') THEN 1 ELSE 0 END) n FROM events WHERE enriched>0 AND kind<>'baseline' "
                      "AND created_at>=datetime('now', ?) GROUP BY brand", (f"-{days} day",))
            ind = glob = sector_n = 0
            ind_news = glob_news = 0
            for r in by:
                b = st.brand(r["brand"])
                if not b:
                    sector_n += r["c"]
                elif b.origin == "Indian":
                    ind += r["c"]
                    ind_news += r["n"] or 0
                else:
                    glob += r["c"]
                    glob_news += r["n"] or 0
            for b in st.brands:
                countries[b.country or ("India" if b.origin == "Indian" else "World")] += 1
                c = st.company(b.name)
                ownership[c.get("ownership") or ("listed" if c.get("ticker") else "private")] += 1
            for r in db.q("SELECT brand, article, ROUND(AVG(views)) v, MAX(views) mx FROM attention WHERE day>=date('now','-8 day') GROUP BY brand ORDER BY v DESC LIMIT 8"):
                b = st.brand(r["brand"])
                attention.append(_tag(sec, {**r, "origin": b.origin if b else ""}))
            return {"key": sec.key, "label": sec.label, "short": sec.short, "color": sec.color, "indian_brands": sum(1 for b in st.brands if b.origin == "Indian"),
                    "global_brands": sum(1 for b in st.brands if b.origin != "Indian"), "indian_events": ind, "global_events": glob, "sector_events": sector_n,
                    "indian_news": ind_news, "global_news": glob_news}
        r = _safe(sec, one, None)
        if r:
            per_sector.append(r)
    attention.sort(key=lambda a: a["v"] or 0, reverse=True)
    return {"days": days, "sectors": per_sector, "attention": attention[:24], "countries": sorted(countries.items(), key=lambda kv: kv[1], reverse=True),
            "ownership": dict(ownership)}


# ---------------------------------------------------------------- search and ask
def search(q: str, limit: int = 40) -> dict:
    q = (q or "").strip()
    if not q:
        return {"brands": [], "events": []}
    brands, events = [], []
    for sec in engine.sectors():
        def one(sec=sec):
            st = sec.settings
            for b in st.brands:
                hay = " ".join([b.name, *b.aliases, st.company(b.name).get("entity") or ""]).lower()
                if q.lower() in hay:
                    brands.append(_tag(sec, {"name": b.name, "origin": b.origin, "kind": b.kind, "entity": st.company(b.name).get("entity")}))
            for r in sec.db.q("SELECT id, brand, kind, title, url, importance, created_at FROM events WHERE enriched>0 AND kind<>'baseline' AND (title LIKE ? OR brand LIKE ?) "
                              "ORDER BY id DESC LIMIT 12", (f"%{q}%", f"%{q}%")):
                events.append(_tag(sec, r))
        _safe(sec, one, None)
    events.sort(key=lambda e: e["created_at"], reverse=True)
    return {"brands": brands[:limit], "events": events[:limit]}


def ask(question: str, sector: str | None) -> dict:
    sec = engine.get(sector)
    chat = sec.sub("chat")
    res = chat.ask(question)
    res["sector"] = sec.key
    res["sector_label"] = sec.label
    return res


def run(sector: str | None, tier: str) -> dict:
    sec = engine.get(sector)
    if engine.HOSTED:
        r = sec.sub("cloud").dispatch(tier, sec.key)
        return {"ok": bool(r.get("started")), "message": f"{sec.label}: {r.get('message')}", "url": r.get("url")}
    if sec.scheduler == "external":
        return {"ok": False, "message": f"{sec.label} is collected by another radar process; use its own Run button."}
    if sec.runner.busy():
        return {"ok": False, "message": f"{sec.label}: a run is already in progress."}
    threading.Thread(target=sec.runner.run_tier, args=(tier,), daemon=True, name=f"nazar-run-{sec.key}").start()
    return {"ok": True, "message": f"{sec.label}: {tier} tier started."}


def health() -> dict:
    return {"ok": True, "sectors": [{"key": s.key, "scheduler": s.scheduler, "primed": s.primed, "db": str(s.db_path)} for s in engine.sectors()]}
