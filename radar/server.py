"""FastAPI app: live dashboard (SSE), JSON API, Ask-the-Radar chat, and the embedded scheduler.

    python -m radar.server            -> http://127.0.0.1:8080  (scheduler runs inside, nothing else to start)
    RADAR_NO_SCHEDULER=1              -> API only (use when GitHub Actions / cron runs the collectors)
    on Vercel (app.py)                -> cloud mode: serves the snapshot GitHub Actions publishes (see cloud.py)
    RADAR_PASSWORD=...                -> browser asks for a password (any user name) before showing anything
"""
from __future__ import annotations
import asyncio
import hmac
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
import requests
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response, FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from . import db, analytics, chat, brief, runner, matcher, exports, bot, cloud, signals as sig, newsintel, finance, market, channels, regions, perspective
from urllib.parse import quote, unquote
from .config import settings, ROOT, SEAT

log = logging.getLogger("radar.server")
STATIC = Path(__file__).parent / "static"
SHOTS = settings.db_path.parent / "shots"  # next to the DB, so Docker's /data volume keeps them
_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the scheduler (and the Telegram bot when configured) with the server; stop them with it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.connect()
    perspective.apply_saved()  # whose seat the dashboard writes from (stored in the db; default neutral)
    # in cloud mode GitHub Actions runs the collectors and Telegram calls /api/telegram/webhook instead
    if os.environ.get("RADAR_NO_SCHEDULER") != "1" and not cloud.HOSTED:
        threading.Thread(target=runner.loop, args=(_stop,), daemon=True, name="radar-scheduler").start()
        log.info("embedded scheduler started")
    if settings.telegram_token and not cloud.HOSTED:
        threading.Thread(target=bot.loop, args=(_stop,), daemon=True, name="radar-telegram").start()
    # the marketplace-vs-store price match is slow on a full database: compute it once now, off the request path
    threading.Timer(20, _warm_caches).start()
    yield
    _stop.set()


def _warm_caches() -> None:
    from . import channels
    try:
        channels.warm()
    except Exception as e:  # noqa: BLE001
        log.warning("parity warm-up failed: %s", e)


app = FastAPI(title="Pebble Competitor Radar", version="1.2", lifespan=lifespan)


@app.middleware("http")
async def gate(request: Request, call_next):
    """Password check (when RADAR_PASSWORD is set), and in cloud mode the latest snapshot before answering."""
    path = request.url.path
    if cloud.needs_password(path) and not cloud.authorized(request.headers.get("authorization")):
        return Response("Password required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Pebble Radar", charset="UTF-8"'})
    if cloud.HOSTED and path != "/favicon.ico":
        await run_in_threadpool(cloud.refresh)
    raw = request.cookies.get(SEAT_COOKIE)      # this browser's seat for this sector; absent = the server's default
    token = SEAT.set(unquote(raw)) if raw is not None else None
    try:
        return await call_next(request)
    finally:
        if token is not None:
            SEAT.reset(token)


SEAT_COOKIE = f"seat_{settings.sector_key or 'iot'}"


def _ev(r: dict) -> dict:
    r["data"] = json.loads(r.get("data") or "{}")
    return r


_ROOT = Path(__file__).resolve().parents[1]


def _code_mtime() -> float:
    """Newest modification time across the radar package and its dashboard files."""
    newest = 0.0
    for p in (_ROOT / "radar").rglob("*.py"):  # static files are read fresh on every request; only Python needs a restart
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


BUILD_LOADED = _code_mtime()   # what this process is running


def build_info() -> dict:
    """Whether the files on disk are newer than the running process (the classic 'I updated but it 404s' problem)."""
    on_disk = _code_mtime()
    return {"loaded": BUILD_LOADED, "on_disk": on_disk, "stale": on_disk > BUILD_LOADED + 1, "pid": os.getpid(), "hosted": cloud.HOSTED}


def _relaunch() -> None:
    """Start a fresh server process on the same host/port once this one has let go of the socket, then exit."""
    import subprocess
    import sys
    host, port = os.environ.get("RADAR_HOST", "127.0.0.1"), os.environ.get("RADAR_PORT", "8080")
    child = (f"import socket, time, os\n"
             f"for _ in range(60):\n"
             f"    s = socket.socket(); s.settimeout(0.5)\n"
             f"    if s.connect_ex(({host!r}, {int(port)})) != 0: s.close(); break\n"
             f"    s.close(); time.sleep(0.5)\n"
             f"os.chdir({str(ROOT)!r})\n"
             f"from radar.server import main\nmain()\n")
    log_path = _ROOT / "data" / "server.log"
    log_path.parent.mkdir(exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    with open(log_path, "ab") as out:
        subprocess.Popen([sys.executable, "-X", "utf8", "-c", child], cwd=str(_ROOT), stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True, env={**os.environ, "RADAR_RESTARTED": "1"})
    time.sleep(1.0)
    log.info("restarting: handing port %s to a fresh process", port)
    os._exit(0)


@app.get("/", response_class=HTMLResponse)
def index():
    # the dashboard is one file that changes with every update: never let a browser keep an old copy
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(FAVICON, media_type="image/svg+xml")


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#2a78d6"/>'
           '<g fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="7"/>'
           '<circle cx="12" cy="12" r="3"/><path d="M12 5v7l4.5 4.5"/></g></svg>')


@app.get("/api/stats")
def stats():
    me = settings.self_brand.name if settings.self_brand else ""
    return {
        "brands": [{"name": b.name, "self": b.self_brand, "shopify": b.shopify} for b in settings.brands],
        "self": me, "own_data": settings.own_data, "sector": settings.sector, "currency": settings.currency,
        "sector_key": settings.sector_key, "sector_label": settings.sector_label, "me": settings.me, "perspective": settings.perspective,
        "ai": settings.ai_enabled, "provider": settings.ai_provider, "model": settings.ai_model,
        "telegram": bool(settings.telegram_token and settings.telegram_chat), "threshold": settings.alert_threshold,
        "products": db.q1("SELECT COUNT(*) c FROM products")["c"],
        "events_total": db.q1("SELECT COUNT(*) c FROM events")["c"],
        "events_24h": db.q1("SELECT COUNT(*) c FROM events WHERE created_at>=datetime('now','-1 day')")["c"],
        "signals_7d": db.q1("SELECT COUNT(*) c FROM signals WHERE created_at>=datetime('now','-7 day')")["c"],
        "build": build_info(),
        "news_7d": db.q1("SELECT COUNT(*) c FROM events WHERE source='news' AND created_at>=datetime('now','-7 day')")["c"],
        "financial_facts": db.q1("SELECT COUNT(*) c FROM financials")["c"],
        "listed": db.q1("SELECT COUNT(DISTINCT ticker) c FROM quotes")["c"],
        "sources": ["Shopify stores", "watchlist pages", "Google News", "Bing News", "newsroom + analyst RSS", "Reddit", "YouTube",
                    "Google autocomplete", "Google Trends", "Judge.me reviews", "Google Play", "Apple App Store", "Wikipedia page views",
                    "Yahoo Finance quotes", "SEC EDGAR filings", "homepage screenshots", "tech stack", "crt.sh domains", "Meta Ad Library"],
        "last_runs": {t: db.kv_get(f"last_run:{t}") for t in ("fast", "medium", "slow", "all")},
        "runs": db.q("SELECT collector, brand, finished_at, ok, events, note FROM runs ORDER BY id DESC LIMIT 25"),
        "festival": sig.nearest_festival(),
        "hosted": cloud.info(),
    }


@app.get("/api/events")
def events(brand: str = "", kind: str = "", min_importance: int = 0, q: str = "", days: int = 30, limit: int = 200, after_id: int = 0):
    sql, p = "SELECT * FROM events WHERE created_at>=datetime('now', ?) AND kind<>'baseline'", [f"-{days} days"]
    if brand:
        sql += " AND brand=?"; p.append(brand)
    if kind:
        sql += " AND kind=?"; p.append(kind)
    if min_importance:
        sql += " AND importance>=?"; p.append(min_importance)
    if q:
        sql += " AND (title LIKE ? OR detail LIKE ? OR so_what LIKE ?)"; p += [f"%{q}%"] * 3
    if after_id:
        sql += " AND id>?"; p.append(after_id)
    sql += " ORDER BY id DESC LIMIT ?"; p.append(min(limit, 1000))
    return [_ev(r) for r in db.q(sql, p)]


@app.get("/api/stream")
async def stream(after_id: int = 0):
    """Server-sent events: pushes every newly enriched event and signal as it lands."""
    async def gen():
        last = after_id or (db.q1("SELECT MAX(id) m FROM events") or {}).get("m") or 0
        last_sig = (db.q1("SELECT MAX(id) m FROM signals") or {}).get("m") or 0
        # serverless functions bill for open connections, so in cloud mode the stream ends after 25 s and the
        # browser waits a minute before reconnecting (the dashboard itself polls /api/updates there instead)
        end = time.monotonic() + 25 if cloud.HOSTED else None
        if end:
            yield "retry: 60000\n\n"
        yield f"event: hello\ndata: {json.dumps({'after_id': last})}\n\n"
        while end is None or time.monotonic() < end:
            rows = db.q("SELECT * FROM events WHERE id>? AND enriched>0 AND kind<>'baseline' ORDER BY id LIMIT 50", (last,))
            for r in rows:
                last = max(last, r["id"])
                yield f"event: event\ndata: {json.dumps(_ev(r), default=str)}\n\n"
            srows = db.q("SELECT * FROM signals WHERE id>? ORDER BY id LIMIT 20", (last_sig,))
            for s in srows:
                last_sig = max(last_sig, s["id"])
                yield f"event: signal\ndata: {json.dumps(s, default=str)}\n\n"
            if not rows and not srows:
                yield ": ping\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/updates")
def updates(after_id: int = 0, after_sig: int = 0):
    """Polling twin of /api/stream for serverless hosting. after_id=0 returns the current high-water marks."""
    top = (db.q1("SELECT MAX(id) m FROM events") or {}).get("m") or 0
    top_sig = (db.q1("SELECT MAX(id) m FROM signals") or {}).get("m") or 0
    if not after_id:
        return {"after_id": top, "after_sig": top_sig, "events": [], "signals": []}
    ev = [_ev(r) for r in db.q("SELECT * FROM events WHERE id>? AND enriched>0 AND kind<>'baseline' ORDER BY id LIMIT 50", (after_id,))]
    sg = [dict(s, data=json.loads(s["data"] or "{}")) for s in db.q("SELECT * FROM signals WHERE id>? ORDER BY id LIMIT 20", (after_sig,))]
    return {"after_id": ev[-1]["id"] if len(ev) == 50 else max(top, after_id),
            "after_sig": sg[-1]["id"] if len(sg) == 20 else max(top_sig, after_sig), "events": ev, "signals": sg}


def _sig(s: dict) -> dict:
    s["data"] = json.loads(s["data"] or "{}")
    if "impact" not in s["data"]:
        try:
            imp = market.impact(s)
        except Exception:  # noqa: BLE001
            imp = None
        if imp:
            s["data"]["impact"] = imp
    return s


@app.get("/api/signals")
def signals_api(days: int = 30):
    return [_sig(s) for s in db.q("SELECT * FROM signals WHERE created_at>=datetime('now', ?) ORDER BY id DESC LIMIT 100", (f"-{days} days",))]


# ---------------------------------------------------------------- news, money, market
@app.get("/api/news")
def news_api(days: int = 14, brand: str = "", topic: str = "", q: str = "", sort: str = "strength"):
    """Story clusters (news across publishers), share of voice, velocity, topics, publishers, trending now.
    With a brand, topic, text query or sort it returns just the matching stories."""
    if brand or topic or q or sort != "strength":
        return {"days": days, "stories": newsintel.stories(days, brand or None, topic or None, q=q, sort=sort),
                "topic_list": [t for t, _ in newsintel.TOPICS]}
    return {**newsintel.overview(days), "topic_list": [t for t, _ in newsintel.TOPICS]}


# every collector the runner knows, by name, with the tier the cloud worker runs it in (the Refresh buttons and
# /api/collect/<name> accept any of them: news, financials, resolve, reviews, appstore, attention, regions, ...)
from . import collectors as _collectors
COLLECT_TIER = {m.__name__.rsplit(".", 1)[-1]: tier
                for tier, mods in (("fast", _collectors.FAST), ("medium", _collectors.MEDIUM), ("slow", _collectors.SLOW)) for m in mods}


@app.post("/api/collect/{name}")
def collect_now(name: str):
    """Run one collector now (the Refresh buttons on News and Financials). Local: a background thread, poll GET for
    the result. Cloud: the dashboard cannot collect, so the worker's tier is dispatched instead."""
    if name not in COLLECT_TIER:
        raise HTTPException(404, f"unknown collector {name}")
    if cloud.HOSTED:
        if msg := cloud.public_lock():
            return {"state": "locked", "message": msg}
        return {"state": "dispatched", **cloud.dispatch(COLLECT_TIER[name], settings.sector_key or None)}
    if runner.busy():
        return {"state": "busy", "message": "A sweep is already running; this source refreshes with it."}
    threading.Thread(target=runner.run_one, args=(name,), daemon=True).start()
    return {"state": "running", "started": db.now()}


@app.get("/api/collect/{name}")
def collect_status(name: str):
    return db.kv_get(f"collect:{name}") or {"state": "idle"}


@app.post("/api/restart")
def restart_server():
    """Local mode: relaunch the server so it picks up updated code (the dashboard offers this when it sees a newer
    build on disk or a 404 from a known route). Cloud mode redeploys on push, so there is nothing to restart."""
    if cloud.HOSTED:
        return {"ok": False, "message": "Cloud mode: Vercel redeploys on every push; nothing to restart here."}
    if runner.busy():
        return {"ok": False, "message": "A sweep is running; restart when it finishes (see Settings)."}
    threading.Thread(target=_relaunch, daemon=True).start()
    return {"ok": True, "message": "Restarting… the dashboard reconnects in a few seconds.", "pid": os.getpid()}


@app.get("/api/financials")
def financials_api():
    return finance.peers()


@app.get("/api/financials/{name}")
def financials_brand_api(name: str):
    return finance.profile(name)


@app.get("/api/market/size")
def market_size_api():
    return market.sizes()


@app.get("/api/market/share")
def market_share_api(days: int = 30):
    return market.share_of_signal(days)


@app.get("/api/market/pressure")
def market_pressure_api(days: int = 30):
    return market.pressure(days)


@app.get("/api/attention")
def attention_api(days: int = 30):
    """Daily Wikipedia page views per brand for sparklines, plus the latest trending-in-India matches."""
    series = {}
    for r in db.q("SELECT brand, day, views FROM attention WHERE day>=date('now', ?) ORDER BY brand, day", (f"-{days} days",)):
        series.setdefault(r["brand"], []).append({"day": r["day"], "views": r["views"]})
    return {"series": series, "trending": newsintel.trending(15)}


@app.get("/api/brand/{name}")
def brand_page(name: str):
    b = settings.brand(name)
    prods = db.q("SELECT product_id,title,product_type,price,compare_at,available,handle,image,first_seen,last_changed "
                 "FROM products WHERE brand=? ORDER BY last_changed DESC LIMIT 500", (name,))
    for p in prods:
        p["category"] = analytics.categorize(p["product_type"], p["title"])
        p["url"] = b.product_url(p["handle"]) if b else ""
    best = db.q("SELECT r.rank, p.title, p.price, p.available, p.handle FROM bestseller_rank r JOIN products p "
                "ON p.brand=r.brand AND p.product_id=r.product_id WHERE r.brand=? ORDER BY r.rank LIMIT 20", (name,))
    for p in best:
        p["url"] = b.product_url(p["handle"]) if b else ""
    watch = analytics._watchlist_rows(brand=name)
    for w in watch:   # price trail for the sparkline / history line
        w["history"] = db.q("SELECT price, status, seen_at FROM watch_prices WHERE name=? AND price>0 ORDER BY id", (w["name"],))
    fin = finance.profile(name)
    return {"brand": name, "shopify": b.shopify if b else None, "meta": analytics.brand_meta(name), "watchlist": watch,
            "events": [_ev(r) for r in db.q("SELECT * FROM events WHERE brand=? AND kind<>'baseline' ORDER BY id DESC LIMIT 300", (name,))],
            "products": prods,
            "bestsellers": best,
            "launch": (db.kv_get("launch_scores") or {}).get(name),
            "stories": newsintel.stories(days=60, brand=name, limit=40),
            "finance": fin,
            "pressure": [p for p in market.pressure(30)["by_brand"] if p["brand"] == name][:1],
            "attention": db.q("SELECT day, views FROM attention WHERE brand=? AND day>=date('now','-30 days') ORDER BY day", (name,)),
            "voice": _voice_view(name),
            "shelf": _safe(lambda: channels.brand(name), None),
            "regions": _safe(lambda: regions.brand_states(name), None)}


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        log.exception("brand page section failed")
        return default


def _voice_view(name: str) -> dict | None:
    try:
        from . import voice
        return voice.brand_view(name, 90)
    except Exception:  # noqa: BLE001 - the brand page must render even if the voice table is missing
        log.exception("voice view failed for %s", name)
        return None


@app.get("/api/products")
def products(q: str = "", brand: str = "", limit: int = 100):
    sql, p = "SELECT brand,product_id,title,product_type,price,compare_at,available,handle,image FROM products WHERE price>0", []
    if brand:
        sql += " AND brand=?"; p.append(brand)
    for w in q.split()[:5]:
        sql += " AND (title LIKE ? OR product_type LIKE ?)"; p += [f"%{w}%", f"%{w}%"]
    sql += " ORDER BY price LIMIT ?"; p.append(min(limit, 500))
    rows = db.q(sql, p)
    for r in rows:
        r["category"] = analytics.categorize(r["product_type"], r["title"])
    return rows


@app.get("/api/price_history")
def price_history(brand: str, product_id: int):
    return db.q("SELECT price,compare_at,available,seen_at FROM price_history WHERE brand=? AND product_id=? ORDER BY seen_at", (brand, product_id))


@app.get("/api/whitespace")
def whitespace():
    return analytics.whitespace()


@app.get("/api/benchmark")
def benchmark():
    return analytics.benchmark()


@app.get("/api/winners")
def winners(limit: int = 25):
    return analytics.winners(limit)


@app.get("/api/launch_scores")
def launch_scores():
    return db.kv_get("launch_scores") or {}


@app.get("/api/pain_points")
def pain_points():
    return analytics.pain_points()


@app.get("/api/briefs")
def briefs():
    return db.q("SELECT id,created_at,period,body FROM briefs ORDER BY id DESC LIMIT 10")


class ChatIn(BaseModel):
    question: str


class PerspectiveIn(BaseModel):
    brand: str = ""   # a brand of this sector, or "" for the neutral view


@app.get("/api/perspective")
def perspective_get():
    """Whose seat the dashboard is written from, and the brands it could be written for."""
    return perspective.state()


@app.post("/api/perspective")
def perspective_set(body: PerspectiveIn, response: Response):
    """Switch the seat for this browser (cookie, a year) and, when this server owns its database, make it the
    default for everyone else too. The page reloads itself afterwards. Works the same on Vercel, where the
    database is a read-only snapshot: the cookie is the whole state."""
    name = body.brand.strip()
    if name and settings.brand(name) is None:
        raise HTTPException(404, f"{name!r} is not a brand in this sector")
    if not cloud.HOSTED:
        perspective.set(name)
    response.set_cookie(SEAT_COOKIE, quote(name, safe=""), max_age=365 * 86400, samesite="lax", path="/")
    SEAT.set(name)
    return perspective.state()


# ---------------------------------------------------------------- social, web and research layer (radar/social.py)
@app.get("/api/social")
def social_overview(days: int = 30):
    from . import social
    return social.overview(min(max(days, 1), 365))


@app.get("/api/social/progress")
def social_progress():
    from . import social
    return social.progress()


@app.post("/api/social/refresh")
def social_refresh():
    """Read every platform for this sector now (the page's Refresh button). Cloud: the worker's medium tier."""
    from . import social
    if cloud.HOSTED:
        if msg := cloud.public_lock():
            return {"state": "locked", "message": msg}
        return {"state": "dispatched", **cloud.dispatch("medium", settings.sector_key or None)}
    return social.kick(force=True)


@app.get("/api/social/posts")
def social_posts(platform: str = "", brand: str = "", kind: str = "", days: int = 30, limit: int = 120, q: str = ""):
    from . import social
    return social.posts(platform, brand, kind, min(max(days, 1), 365), limit, q)


@app.get("/api/social/threads")
def social_threads(platform: str = "", brand: str = "", days: int = 30, limit: int = 25):
    from . import social
    return social.threads(platform, brand, min(max(days, 1), 365), limit)


@app.get("/api/social/accounts")
def social_accounts():
    from . import social
    return social.accounts()


class ReplyIn(BaseModel):
    platform: str
    comment_id: str


@app.post("/api/social/reply")
def social_reply(body: ReplyIn):
    """A reply draft for one comment, from the current seat; the page copies it and opens the comment to post it."""
    from . import social
    allow_ai = not (cloud.HOSTED and cloud.public_lock())   # a deployment without a password never spends the AI key
    out = social.reply_draft(body.platform, body.comment_id, allow_ai=allow_ai)
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out


@app.post("/api/research/refresh")
def research_refresh():
    from . import social
    if cloud.HOSTED:
        if msg := cloud.public_lock():
            return {"state": "locked", "message": msg}
        return {"state": "dispatched", **cloud.dispatch("slow", settings.sector_key or None)}
    p = social.research_kick(force=True)
    return {"state": "running" if p.get("running") else "idle", **p}


@app.get("/api/research")
def research_api(days: int = 730, brand: str = "", relation: str = ""):
    from . import social
    return social.research(min(max(days, 30), 3650), brand, relation)


@app.post("/api/chat")
def chat_api(body: ChatIn):
    if settings.ai_enabled and (msg := cloud.public_lock()):  # keyword search is free, so it still answers
        return {"answer": chat._fallback(body.question), "mode": "locked", "note": msg}
    return chat.ask(body.question)


@app.post("/api/run")
def run_now(tier: str = Query("fast", pattern="^(fast|medium|slow|all)$")):
    """Kick a collection tier in the background and return immediately (or say a sweep is already running)."""
    if cloud.HOSTED:  # the collectors live on GitHub Actions in cloud mode
        if msg := cloud.public_lock():
            return {"started": None, "message": msg}
        return cloud.dispatch(tier, settings.sector_key or None)
    if runner.busy():
        return {"started": None, "busy": True}
    threading.Thread(target=runner.run_tier, args=(tier,), daemon=True).start()
    return {"started": tier}


@app.post("/api/brief")
def brief_now(days: int = 7, push: bool = False):
    if settings.ai_enabled and (msg := cloud.public_lock()):
        return {"body": None, "message": msg}
    body = brief.write(days=days, push=push)
    # in cloud mode the database is a read-only copy: the brief shows now, the workflow writes the scheduled ones
    return {"body": body, "message": "Brief written. Scheduled briefs come from GitHub Actions; this one is not kept."
            if cloud.HOSTED else None}


@app.get("/api/matches")
def matches_api():
    return {"matches": matcher.matches(), "undercuts": matcher.undercuts(days=7)}


@app.get("/api/influencers")
def influencers_api(limit: int = 25):
    return analytics.influencers(limit)


@app.get("/api/app_health")
def app_health_api():
    return analytics.app_health()


@app.get("/api/search_demand")
def search_demand_api():
    return analytics.search_demand()


@app.get("/api/sale_cadence")
def sale_cadence_api():
    return analytics.sale_cadence()


@app.get("/api/review_pain_points")
def review_pain_points_api():
    return analytics.review_pain_points()


@app.get("/api/techstack")
def techstack_api():
    return [dict(r, apps=json.loads(r["apps"] or "[]")) for r in db.q("SELECT brand, shop, theme, apps, seen_at FROM techstack ORDER BY brand")]


@app.get("/api/screenshots")
def screenshots_api():
    rows = db.q("SELECT brand, url, path, diff_pct, taken_at FROM screenshots ORDER BY id DESC LIMIT 100")
    for r in rows:
        r["src"] = f"/shots/{Path(r['path']).name}"
    return rows


@app.get("/shots/{name}")
def shot(name: str):
    name = Path(name).name
    mime = cloud._mime(name)
    if cloud.HOSTED:  # screenshots live on the `data` release next to the database snapshot
        try:
            data = cloud.shot(name)
        except Exception:  # noqa: BLE001
            raise HTTPException(404)
        # names carry the date, so a file never changes: browsers and Vercel's CDN can keep it
        return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=86400, s-maxage=2592000, immutable"})
    # serve from where the collector recorded it (older captures may predate SHOTS moving next to the DB)
    row = db.q1("SELECT path FROM screenshots WHERE path LIKE ? ORDER BY id DESC LIMIT 1", (f"%{name}",))
    p = Path(row["path"]) if row and Path(row["path"]).name == name else SHOTS / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type=mime)


@app.get("/api/export/{table}.csv")
def export_csv(table: str):
    if table not in exports.TABLES:
        raise HTTPException(404, f"unknown table; choose one of {', '.join(exports.TABLES)}")
    return Response(exports.csv_bytes(table), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=radar_{table}.csv"})


@app.get("/api/telegram/preview")
def telegram_preview(cmd: str = "/status"):
    """Dry-run a bot command without Telegram, so the bot can be tested from the browser."""
    return {"cmd": cmd, "reply": bot.handle(cmd)}


@app.post("/api/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request):
    """Cloud mode bot: Telegram calls this for every message (set it up with /api/telegram/connect)."""
    secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not (settings.telegram_token and cloud.TG_SECRET and hmac.compare_digest(secret, cloud.TG_SECRET)):
        raise HTTPException(403)
    msg = (await request.json()).get("message") or {}
    cid = str((msg.get("chat") or {}).get("id", ""))
    if msg.get("text") and settings.telegram_chat and cid == str(settings.telegram_chat):  # only your chat
        await run_in_threadpool(bot.reply, cid, msg["text"])
    return {"ok": True}


@app.post("/api/telegram/connect")
def telegram_connect(request: Request, off: bool = False):
    """Point the Telegram bot at this deployment (or back to polling with ?off=1, for the local server)."""
    if not settings.telegram_token:
        raise HTTPException(400, "set TELEGRAM_BOT_TOKEN first")
    if msg := cloud.public_lock():
        raise HTTPException(403, msg)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    url = f"{proto}://{host}/api/telegram/webhook"
    api = f"https://api.telegram.org/bot{settings.telegram_token}/"
    r = requests.post(api + ("deleteWebhook" if off else "setWebhook"), timeout=15,
                      json={} if off else {"url": url, "secret_token": cloud.TG_SECRET, "allowed_updates": ["message"]})
    return {"webhook": None if off else url, "telegram": r.json()}


@app.get("/api/cron/heartbeat", include_in_schema=False)
def cron_heartbeat(request: Request):
    """Vercel cron (daily): warns on Telegram / webhook and restarts the worker when the snapshot goes stale."""
    secret = os.environ.get("CRON_SECRET", "")
    trusted = bool(secret) and hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}")
    return cloud.heartbeat(act=trusted)



# ---------------------------------------------------------------- views for the redesigned dashboard
class StatusIn(BaseModel):
    status: str


@app.post("/api/events/{event_id}/status")
def set_event_status(event_id: int, body: StatusIn):
    if body.status not in ("open", "done", "snoozed"):
        raise HTTPException(400, "status must be open, done or snoozed")
    with db.tx() as c:
        if not c.execute("UPDATE events SET status=? WHERE id=?", (body.status, event_id)).rowcount:
            raise HTTPException(404, "no such event")
    # cloud mode serves a read-only snapshot, so the browser remembers the decision (scope tells it to)
    return {"id": event_id, "status": body.status, "scope": "browser" if cloud.HOSTED else "server"}


@app.post("/api/signals/{signal_id}/status")
def set_signal_status(signal_id: int, body: StatusIn):
    if body.status not in ("open", "done", "snoozed"):
        raise HTTPException(400, "status must be open, done or snoozed")
    with db.tx() as c:
        if not c.execute("UPDATE signals SET status=? WHERE id=?", (body.status, signal_id)).rowcount:
            raise HTTPException(404, "no such signal")
    return {"id": signal_id, "status": body.status, "scope": "browser" if cloud.HOSTED else "server"}


@app.get("/api/today")
def today():
    """Everything the Today view needs in one call: actions, open signals, what changed, by brand."""
    me = settings.self_brand.name if settings.self_brand else ""
    actions, per = [], {}
    for r in db.q("SELECT * FROM events WHERE enriched>0 AND importance>=6 AND COALESCE(status,'open')='open' AND kind<>'baseline' "
                  "AND created_at>=datetime('now','-3 days') ORDER BY importance DESC, id DESC LIMIT 60"):
        k = (r["brand"], r["kind"])
        if per.get(k, 0) >= (1 if r["kind"] in ("search_trend", "social", "video", "page_change", "homepage_change") else 2):
            continue
        per[k] = per.get(k, 0) + 1
        actions.append(_ev(r))
        if len(actions) >= 8:
            break
    signals_open, seen_sig = [], set()
    for s in db.q("SELECT * FROM signals WHERE COALESCE(status,'open')='open' AND created_at>=datetime('now','-7 days') ORDER BY importance DESC, id DESC LIMIT 40"):
        key = (s["brand"], (s["title"] or "").strip().lower())
        if key in seen_sig:   # the same rule firing again on the same brand is one card, not three
            continue
        seen_sig.add(key)
        signals_open.append(dict(s, data=json.loads(s["data"] or "{}")))
        if len(signals_open) >= 10:
            break
    by_brand = {}
    for r in db.q("SELECT brand, kind, COUNT(*) c, MAX(importance) m FROM events WHERE created_at>=datetime('now','-1 day') "
                  "AND kind<>'baseline' GROUP BY brand, kind"):
        b = by_brand.setdefault(r["brand"], {"brand": r["brand"], "total": 0, "kinds": {}, "max_importance": 0, "self": r["brand"] == me})
        b["total"] += r["c"]
        b["kinds"][r["kind"]] = r["c"]
        b["max_importance"] = max(b["max_importance"], r["m"] or 0)
    done_today = db.q1("SELECT COUNT(*) c FROM events WHERE status='done' AND created_at>=datetime('now','-7 days')")["c"]
    for s in signals_open:
        if "impact" not in s["data"]:
            try:
                imp = market.impact(s)
            except Exception:  # noqa: BLE001
                imp = None
            if imp:
                s["data"]["impact"] = imp
    pressure = market.pressure(30)
    movers = [_ev(r) for r in db.q("SELECT * FROM events WHERE kind='stock_move' AND created_at>=datetime('now','-2 days') ORDER BY id DESC LIMIT 6")]
    stories = newsintel.stories(days=7, limit=6)
    return {"actions": actions, "signals": signals_open,
            "by_brand": sorted(by_brand.values(), key=lambda x: (-x["max_importance"], -x["total"])),
            "done_7d": done_today, "festival": sig.nearest_festival(),
            "last_sweep": db.kv_get("last_run:fast") or db.kv_get("last_run:all"),
            "kpis": {"listings_live": _safe(lambda: db.q1("SELECT COUNT(*) c FROM channel_listings WHERE last_seen>=datetime('now','-2 days')")["c"], 0),
                     "pebble_qc_cities": _safe(lambda: db.q1("SELECT COUNT(DISTINCT city) c FROM channel_listings WHERE brand=? AND city<>'' AND available=1 AND last_seen>=datetime('now','-2 days')", (me,))["c"], 0),
                     "voice_7d": _safe(lambda: db.q1("SELECT COUNT(*) c FROM voice WHERE created>=date('now','-7 days')")["c"], 0),
                     "voice_neg_7d": _safe(lambda: db.q1("SELECT ROUND(100.0*SUM(sentiment<0)/MAX(COUNT(*),1)) c FROM voice WHERE created>=date('now','-7 days')")["c"] or 0, 0),
                     "events_24h": db.q1("SELECT COUNT(*) c FROM events WHERE created_at>=datetime('now','-1 day') AND kind<>'baseline'")["c"],
                     "stories_7d": db.q1("SELECT COUNT(*) c FROM events WHERE source='news' AND created_at>=datetime('now','-7 days')")["c"],
                     "risk_30d": pressure["total_risk"], "upside_30d": pressure["total_upside"],
                     "trending": db.q1("SELECT COUNT(*) c FROM trending WHERE seen_at>=datetime('now','-1 day')")["c"],
                     "money_7d": db.q1("SELECT COUNT(*) c FROM events WHERE kind IN ('financial_fact','financial_result','stock_move') AND created_at>=datetime('now','-7 days')")["c"]},
            "movers": movers, "stories": stories, "pressure_top": pressure["by_brand"][:5]}


@app.get("/api/brands/overview")
def brands_overview():
    """Per brand: 14-day activity sparkline, launch score, catalogue stats, last notable move."""
    me = settings.self_brand.name if settings.self_brand else ""
    launch = db.kv_get("launch_scores") or {}
    days = [r["d"] for r in db.q("SELECT date('now', '-' || value || ' days') d FROM json_each(?) ORDER BY d", (json.dumps(list(range(13, -1, -1))),))]
    counts = {}
    for r in db.q("SELECT brand, substr(created_at,1,10) d, COUNT(*) c, MAX(importance) m FROM events WHERE created_at>=datetime('now','-14 days') "
                  "AND kind<>'baseline' GROUP BY brand, d"):
        counts.setdefault(r["brand"], {})[r["d"]] = r["c"]
    cat = {r["brand"]: r for r in db.q("SELECT brand, COUNT(*) skus, ROUND(100.0*SUM(available)/COUNT(*)) in_stock, "
                                       "SUM(CASE WHEN compare_at>price THEN 1 ELSE 0 END) discounted FROM products WHERE price>0 GROUP BY brand")}
    med = {}
    for r in db.q("SELECT brand, price FROM products WHERE price>0 ORDER BY brand, price"):
        med.setdefault(r["brand"], []).append(r["price"])
    last = {r["brand"]: r for r in db.q("SELECT brand, title, kind, importance, created_at, id FROM events WHERE id IN "
                                        "(SELECT MAX(id) FROM events WHERE kind<>'baseline' AND importance>=5 GROUP BY brand)")}
    watch = {}
    for w in analytics._watchlist_rows():
        watch.setdefault(w["brand"], []).append(w)
    out = []
    for b in settings.brands:   # configured brands only; "Sector" (sector-wide news) lives in the feed, not here
        name = b.name
        series = [counts.get(name, {}).get(d, 0) for d in days]
        prices = med.get(name, [])
        wl = watch.get(name, [])
        wprices = sorted(x["price"] for x in wl if x["price"])
        out.append({"brand": name, "self": name == me, "shopify": b.shopify, "meta": analytics.brand_meta(name),
                    "spark": series, "events_14d": sum(series), "launch": (launch.get(name) or {}).get("score", 0),
                    "skus": (cat.get(name) or {}).get("skus", 0), "in_stock": (cat.get(name) or {}).get("in_stock"),
                    "discounted": (cat.get(name) or {}).get("discounted", 0),
                    "median_price": prices[len(prices) // 2] if prices else (wprices[len(wprices) // 2] if wprices else None),
                    "watchlist": len(wl), "watch_live": sum(1 for x in wl if x["live"]),
                    "coverage": "store" if b.shopify else ("watchlist" if wl else "news"),
                    "last": last.get(name)})
    return {"days": days, "brands": sorted(out, key=lambda x: (not x["self"], -x["launch"], -x["events_14d"]))}


@app.get("/api/radar")
def radar_ring():
    """Threat radar: distance from centre = how quiet a rival is (score 0-100), angle fixed per brand."""
    me = settings.self_brand.name if settings.self_brand else ""
    launch = db.kv_get("launch_scores") or {}
    ev = {r["brand"]: r for r in db.q("SELECT brand, COUNT(*) c, AVG(importance) a, MAX(importance) m FROM events "
                                      "WHERE created_at>=datetime('now','-7 days') AND kind<>'baseline' GROUP BY brand")}
    sigs = {r["brand"]: r["c"] for r in db.q("SELECT brand, COUNT(*) c FROM signals WHERE created_at>=datetime('now','-7 days') GROUP BY brand")}
    out = []
    for b in settings.brands:
        if b.name == me:
            continue
        e = ev.get(b.name) or {"c": 0, "a": 0, "m": 0}
        score = min(100, (launch.get(b.name) or {}).get("score", 0) * 1.5 + min(e["c"], 30) * 0.8 + (e["m"] or 0) * 2 + sigs.get(b.name, 0) * 6)
        out.append({"brand": b.name, "score": round(score), "events_7d": e["c"], "max_importance": e["m"] or 0,
                    "launch": (launch.get(b.name) or {}).get("score", 0), "signals_7d": sigs.get(b.name, 0),
                    "why": (launch.get(b.name) or {}).get("why", [])[:3]})
    return sorted(out, key=lambda x: -x["score"])



@app.get("/api/segments")
def segments_api():
    return analytics.segments_list()


@app.get("/api/segment/{key}")
def segment_api(key: str):
    if key not in {sg["key"] for sg in settings.segments}:
        raise HTTPException(404, "unknown segment")
    return analytics.segment_ladder(key)


@app.get("/api/watchlist")
def watchlist_api():
    return analytics.watchlist_overview()


@app.get("/api/ids")
def ids_api():
    """How many brands have resolved identifiers (app ids, Wikipedia title, SEC CIK, ticker); from collectors/resolve.py when present."""
    try:
        from .collectors import resolve  # type: ignore[attr-defined]
        return resolve.coverage()
    except Exception:  # noqa: BLE001
        return {}


@app.get("/api/health")
def health():
    return {"ok": True, "db": str(settings.db_path), "time": db.now(), "hosted": cloud.HOSTED}


# ---------------------------------------------------------------- distribution, voice, regions, sales, reports, strategy
@app.get("/static/{name}", include_in_schema=False)
def static_file(name: str):
    p = STATIC / Path(name).name
    if not p.exists() or p.suffix not in (".js", ".css"):
        raise HTTPException(404)
    return FileResponse(p, media_type="text/javascript" if p.suffix == ".js" else "text/css", headers={"Cache-Control": "no-cache"})


@app.get("/api/channels")
def channels_api(days: int = 7):
    from . import channels
    return channels.matrix(days)


@app.get("/api/channels/parity")
def channels_parity(brand: str = ""):
    from . import channels
    return channels.parity(brand or None)


@app.get("/api/channels/qc")
def channels_qc():
    from . import channels
    return channels.qc_map()


@app.get("/api/channels/price_index")
def channels_price_index(days: int = 30):
    from . import channels
    return channels.price_index(days)


@app.get("/api/channels/{name}")
def channels_brand(name: str):
    from . import channels
    return channels.brand(name)


@app.get("/api/voice")
def voice_api(days: int = 90, brand: str = ""):
    from . import voice
    return voice.summary(days, brand or None)


@app.get("/api/voice/matrix")
def voice_matrix(days: int = 90):
    from . import voice
    return voice.theme_matrix(days)


@app.get("/api/voice/items")
def voice_items(brand: str = "", source: str = "", theme: str = "", polarity: str = "", q: str = "", days: int = 180, limit: int = 100):
    from . import voice
    return voice.items(brand, source, theme, polarity, q, days, limit)


@app.get("/api/voice/timeline")
def voice_timeline(brand: str, weeks: int = 12):
    from . import voice
    return voice.timeline(brand, weeks)


@app.get("/api/voice/market")
def voice_market(days: int = 90):
    """The field as a whole: demand themes, segments, sources, weekly mood, ratings scoreboard, search asks, conclusion."""
    from . import voice
    return voice.market(days)


@app.get("/api/voice/brand/{name}")
def voice_brand(name: str, days: int = 90):
    from . import voice
    return voice.brand_view(name, days)


@app.post("/api/voice/market_digest")
def voice_market_digest(days: int = 90):
    from . import voice
    if msg := cloud.public_lock():
        return {"text": None, "message": msg}
    t = voice.market_digest(days)
    return {"text": t, "message": None if t else "No AI key configured (GEMINI_API_KEY or ANTHROPIC_API_KEY)."}


@app.post("/api/voice/digest")
def voice_digest(brand: str, days: int = 90):
    from . import voice
    if settings.ai_enabled and (msg := cloud.public_lock()):
        return {"text": None, "message": msg}
    return {"text": voice.digest(brand, days), "ai": settings.ai_enabled}


@app.get("/api/regions")
def regions_api(days: int = 90):
    from . import regions
    return regions.overview(days)


@app.get("/api/sales/report")
def sales_report(kind: str = "weekly", period: str = ""):
    from . import sales
    return sales.report(kind, period or None)


@app.get("/api/sales/series")
def sales_series(kind: str = "monthly", n: int = 12):
    from . import sales
    return sales.series(kind, n)


class SalesIn(BaseModel):
    csv: str | None = None
    rows: list[dict] | None = None
    source: str = "csv"


@app.post("/api/sales/import")
def sales_import(body: SalesIn):
    """Import Pebble's sales as CSV text or JSON rows (date, sku, title, channel, state, city, units, revenue)."""
    from .collectors import sales as sc
    if msg := cloud.public_lock():
        raise HTTPException(403, msg)
    if body.csv:
        res = sc.import_text(body.csv, body.source or "csv")
    elif body.rows:
        res = sc.import_json(body.rows, body.source or "api")
    else:
        raise HTTPException(400, "send csv text or rows")
    res["note"] = ("Cloud mode serves a read-only snapshot: the rows show until the next worker run. Import on the worker instead "
                   "(python -m radar.collectors.sales file.csv) or set SHOPIFY_ADMIN_TOKEN." if cloud.HOSTED else None)
    return res


@app.get("/api/sales/template.csv")
def sales_template():
    body = "date,order,sku,title,channel,state,city,units,revenue\n2026-11-01,#1001,QORE-BLK,Pebble Qore Black,online store,Maharashtra,Mumbai,1,2999\n"
    return Response(body, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=pebble_sales_template.csv"})


@app.get("/api/reports")
def reports_list(kind: str = "", limit: int = 40):
    from . import reports
    return {"reports": reports.list_reports(kind, limit), "kinds": list(reports.KINDS), "due": reports.due()}


@app.get("/api/reports/{report_id}")
def report_get(report_id: int):
    r = db.q1("SELECT id, created_at, kind, period, title, body, data FROM reports WHERE id=?", (report_id,))
    if not r:
        raise HTTPException(404)
    r["data"] = json.loads(r["data"] or "{}")
    return r


@app.get("/api/reports/{report_id}.md")
def report_md(report_id: int):
    r = db.q1("SELECT kind, period, body FROM reports WHERE id=?", (report_id,))
    if not r:
        raise HTTPException(404)
    return Response(r["body"], media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=pebble_{r['kind']}_{r['period']}.md"})


@app.post("/api/reports/build")
def report_build(kind: str = Query("weekly", pattern="^(daily|weekly|monthly|yearly|sector)$"), period: str = "", push: bool = False):
    from . import reports
    if msg := cloud.public_lock():
        return {"id": None, "message": msg}
    r = reports.build(kind, period or None, push=push, ai=settings.ai_enabled)
    r["message"] = "Report written. In cloud mode it shows now but is not kept; the worker writes the scheduled ones." if cloud.HOSTED else None
    return r


@app.get("/api/strategy")
def strategy_api(refresh: bool = False):
    from . import strategy
    return strategy.playbook(refresh=refresh)


@app.post("/api/strategy/memo")
def strategy_memo(refresh: bool = False):
    from . import strategy
    if settings.ai_enabled and (msg := cloud.public_lock()):
        return {"text": None, "message": msg}
    return {"text": strategy.memo(refresh=refresh), "ai": settings.ai_enabled}


def main() -> None:
    import uvicorn
    uvicorn.run("radar.server:app", host=os.environ.get("RADAR_HOST", "127.0.0.1"),
                port=int(os.environ.get("RADAR_PORT", "8080")), log_level="info")


if __name__ == "__main__":
    # also works when double-clicked or run as `python radar/server.py`
    import sys
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    main()
